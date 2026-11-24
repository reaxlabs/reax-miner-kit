"""NVIDIA confidential-computing GPU evidence (Hopper H100/H200, Blackwell B200).

Two verification paths:

1. NRAS (production default, what Chutes and nvtrust use): the evidence is posted to
   https://nras.attestation.nvidia.com/v3/attest/gpu and NVIDIA returns an EAT: an overall JWT plus one detached JWT
   per GPU, ES384-signed with a key from https://nras.attestation.nvidia.com/.well-known/jwks.json (keys rotate every
   few hours; look up by kid). NRAS checks the device cert chain + OCSP and compares the measurements against the
   signed reference values (RIMs) for that driver/VBIOS. We verify the token signatures, the submods digests and the
   claims ourselves. The validator calls NRAS itself; a miner-supplied token is only accepted if its eat_nonce is the
   session digest the validator chose.

2. Local (offline, cross-check and NRAS-outage fallback): parse the SPDM GET_MEASUREMENTS transcript, verify the
   device cert chain to the pinned NVIDIA Device Identity CA, verify the ECDSA P-384 signature and the nonce. This
   proves "genuine NVIDIA GPU, fresh, these measurements" but NOT that the measurements are golden (no RIM/OCSP);
   policy may pin expected measurement values per index.

Never accept tokens from NVIDIA's *local* verifier as proof: it signs its own JWTs HS256 with the literal key
'secret' (issuer LOCAL_GPU_VERIFIER). Only raw SPDM evidence or NRAS ES384 tokens count.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .errors import EvidenceError, Report, fail_closed

NRAS_URL = "https://nras.attestation.nvidia.com/v3/attest/gpu"
NRAS_JWKS_URL = "https://nras.attestation.nvidia.com/.well-known/jwks.json"
NRAS_ISSUER = "https://nras.attestation.nvidia.com"
# SPKI SHA-256 of the NRAS GPU signing intermediates under "NVIDIA Attestation Service CA 001", read from the live JWKS
# on 2026-11-23 (004: 136 keys, valid to 2029-12-08; 005: 72 keys, valid to 2030-10-08). An unknown intermediate fails
# closed; refresh this list when NVIDIA adds one.
NRAS_INTERMEDIATE_SPKI_SHA256 = [
    "fd32837f954e2c45db073105166dfe6985ae0480bb113fba63b091a75affe896",
    "ba14d00377699189b6762c22c744a9dbdc62aa9396097ada7150426c6ff39afc",
]
ROOTS_DIR = Path(__file__).with_name("roots")

SPDM_REQ_LEN = 37
SIG_LEN = 96  # P-384 raw r||s

OPAQUE_NAMES = {
    3: "driver_version", 4: "gpu_info", 5: "sku", 6: "vbios_version", 7: "manufacturer_id", 8: "tamper_detection",
    9: "smc", 10: "vpr", 11: "nvdec0_status", 12: "msrscnt", 13: "cprinfo", 14: "board_id", 15: "chip_sku",
    16: "chip_sku_mod", 17: "project", 18: "project_sku", 19: "project_sku_mod", 20: "fwid",
    21: "protected_pcie_status", 22: "switch_pdi", 23: "floorswept_ports", 24: "position_id",
    25: "lock_switch_status", 32: "gpu_link_conn", 33: "sys_enable_status", 34: "opaque_data_version",
    35: "chip_info", 36: "feature_flag",
}

# GPU silicon accepted for CC. NRAS reports the short name ("GH100"), the local path the device-cert CN
# ("GH100 A01 GSP BROM"), so we match prefixes. GH100 = H100/H200, GB100 = B200, GB202 = RTX PRO 6000 Blackwell
# Server Edition (single-GPU passthrough CC only, driver >= 580.65.06; Workstation/Max-Q editions are not CC-capable).
DEFAULT_HWMODEL_PREFIXES = ["GH100", "GB100", "GB202", "GB20X"]  # GB20X: NRAS name for RTX PRO 6000 Server Ed. (live 24 Nov)


def hwmodel_allowed(hwmodel: object, policy: dict[str, Any]) -> bool:
    prefixes = policy.get("gpu_allowed_hwmodel_prefixes", DEFAULT_HWMODEL_PREFIXES)
    return isinstance(hwmodel, str) and any(hwmodel == p or hwmodel.startswith(p + " ") for p in prefixes)


# Claims every detached per-GPU NRAS token must carry (value or predicate). Configurable via policy.
DEFAULT_GPU_CLAIMS = {
    "measres": "success",
    "x-nvidia-gpu-arch-check": True,
    "x-nvidia-gpu-attestation-report-nonce-match": True,
    "x-nvidia-gpu-attestation-report-signature-verified": True,
    "secboot": True,
    "dbgstat": "disabled",
}


# ----------------------------------------------------------------------------------------------- SPDM (local path)
def parse_spdm_report(raw: bytes) -> dict[str, Any]:
    if len(raw) < SPDM_REQ_LEN + 8 + 32 + 2 + SIG_LEN:
        raise EvidenceError("SPDM transcript too short")
    req, resp = raw[:SPDM_REQ_LEN], raw[SPDM_REQ_LEN:]
    if req[1] != 0xE0:
        raise EvidenceError(f"request code 0x{req[1]:02x} is not GET_MEASUREMENTS")
    if resp[1] != 0x60:
        raise EvidenceError(f"response code 0x{resp[1]:02x} is not MEASUREMENTS")
    nblocks = resp[4]
    rec_len = int.from_bytes(resp[5:8], "little")
    rec = resp[8:8 + rec_len]
    o = 8 + rec_len
    resp_nonce = resp[o:o + 32]
    o += 32
    opaque_len = int.from_bytes(resp[o:o + 2], "little")
    o += 2
    opaque = resp[o:o + opaque_len]
    o += opaque_len
    sig = resp[o:]
    if len(sig) != SIG_LEN:
        raise EvidenceError(f"signature length {len(sig)} != {SIG_LEN}")
    blocks: dict[int, str] = {}
    i = 0
    if len(rec) != rec_len:
        raise EvidenceError("measurement record truncated")
    while i < len(rec):
        if i + 4 > len(rec):
            raise EvidenceError("truncated measurement block header")
        idx, spec, size = rec[i], rec[i + 1], int.from_bytes(rec[i + 2:i + 4], "little")
        if i + 4 + size > len(rec) or size < 3:
            raise EvidenceError("measurement block overruns record")
        m = rec[i + 4:i + 4 + size]
        if spec != 1:
            raise EvidenceError("measurement spec is not DMTF")
        vsize = int.from_bytes(m[1:3], "little")
        blocks[idx] = m[3:3 + vsize].hex()
        i += 4 + size
    if len(blocks) != nblocks:
        raise EvidenceError(f"block count {len(blocks)} != header {nblocks}")
    fields: dict[str, Any] = {}
    j = 0
    while j + 4 <= len(opaque):
        t = int.from_bytes(opaque[j:j + 2], "little")
        n = int.from_bytes(opaque[j + 2:j + 4], "little")
        v = opaque[j + 4:j + 4 + n]
        fields[OPAQUE_NAMES.get(t, f"type_{t}")] = v
        j += 4 + n
    drv = fields.get("driver_version", b"").split(b"\x00")[0].decode(errors="replace")
    vb = fields.get("vbios_version", b"")
    vbios = ""
    if len(vb) >= 8:  # little-endian u64 rendered like nvidia-smi: 96.00.74.00.1C
        v = vb[:8][::-1].hex()
        vbios = ".".join([v[8:10], v[10:12], v[12:14], v[14:16], v[6:8]]).upper()
    proj = fields.get("project", b"").split(b"\x00")[0].decode(errors="replace")
    ppcie = fields.get("protected_pcie_status")
    return {
        "spdm_version": f"{req[0] >> 4}.{req[0] & 0xF}",
        "request_nonce": req[4:36].hex(),
        "response_nonce": resp_nonce.hex(),
        "measurements": blocks,
        "driver_version": drv,
        "vbios_version": vbios,
        "project": proj,
        "protected_pcie_status": int.from_bytes(ppcie, "little") if ppcie else None,
        "signed": req + resp[:-SIG_LEN],
        "signature": sig,
    }


def _ecdsa_link(issuer: x509.Certificate, cert: x509.Certificate) -> None:
    """Signature + issuer-name check for one chain link (ECDSA or RSA issuer). Raises InvalidSignature."""
    try:
        cert.verify_directly_issued_by(issuer)
    except (ValueError, TypeError) as e:
        raise InvalidSignature(str(e)) from e


def _spki(c: x509.Certificate) -> bytes:
    return c.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)


def device_identity(chain_pem: bytes) -> str:
    """Stable per-GPU identity: SHA-256 of the per-device BROM certificate public key (chain[1])."""
    certs = x509.load_pem_x509_certificates(chain_pem)
    return hashlib.sha256(_spki(certs[1] if len(certs) > 1 else certs[0])).hexdigest()


@fail_closed("nvidia-local")
def verify_local(evidence: bytes, chain_pem: bytes, expected_nonce: bytes, now: int, policy: dict[str, Any], root_pem: bytes | None = None) -> Report:
    rep = Report("nvidia-local")
    try:
        p = parse_spdm_report(evidence)
    except EvidenceError as e:
        rep.add("parse", False, str(e))
        return rep
    rep.add("parse", True, f"SPDM {p['spdm_version']}, {len(p['measurements'])} measurement blocks")
    try:
        certs = x509.load_pem_x509_certificates(chain_pem)
        root = x509.load_pem_x509_certificate(root_pem or (ROOTS_DIR / "nvidia_device_identity_ca.pem").read_bytes())
    except Exception as e:
        rep.add("certs_load", False, str(e))
        return rep
    try:
        for child, parent in zip(certs, certs[1:]):
            _ecdsa_link(parent, child)
        _ecdsa_link(root, certs[-1])
        for c in certs[1:]:
            bc = c.extensions.get_extension_for_class(x509.BasicConstraints).value
            if not bc.ca:
                raise InvalidSignature("intermediate is not a CA")
        rep.add("cert_chain", True, f"{len(certs)} certs to NVIDIA Device Identity CA, intermediates CA:true")
    except InvalidSignature:
        rep.add("cert_chain", False, "device certificate chain signature invalid")
        return rep
    rep.add("root_pinned", _spki(certs[-1]) == _spki(root), "chain ends at pinned root key")
    rep.add("cert_validity", all(c.not_valid_before_utc.timestamp() <= now <= c.not_valid_after_utc.timestamp() for c in certs), "validity windows")
    leaf = certs[0]
    hwmodel = leaf.issuer.rfc4514_string().split(",")[0].removeprefix("CN=")
    rep.claims.update({k: p[k] for k in ("driver_version", "vbios_version", "project", "protected_pcie_status", "request_nonce")})
    rep.claims["hwmodel"] = hwmodel
    rep.claims["device_id"] = device_identity(chain_pem)
    r = int.from_bytes(p["signature"][:48], "big")
    s = int.from_bytes(p["signature"][48:], "big")
    try:
        leaf.public_key().verify(encode_dss_signature(r, s), p["signed"], ec.ECDSA(hashes.SHA384()))
        rep.add("report_signature", True, "ECDSA P-384 over SPDM request||response")
    except InvalidSignature:
        rep.add("report_signature", False, "SPDM signature invalid")
        return rep
    rep.add("nonce", hmac.compare_digest(bytes.fromhex(p["request_nonce"]), expected_nonce), "SPDM request nonce == session digest")
    rep.add("hwmodel", hwmodel_allowed(hwmodel, policy), hwmodel)
    _driver_check(rep, p["driver_version"], policy)
    pinned = policy.get("gpu_local_measurements", {})  # {"<driver>|<vbios>": {"<index>": "<hex>"}}
    key = f"{p['driver_version']}|{p['vbios_version']}"
    if key in pinned and not pinned[key]:
        rep.add("golden_measurements", False, f"empty reference set for {key}")
    elif key in pinned:
        diff = [i for i, v in pinned[key].items() if p["measurements"].get(int(i)) != v.lower()]
        rep.add("golden_measurements", not diff, f"mismatch at {diff}" if diff else f"{len(pinned[key])} indices match")
    elif policy.get("gpu_local_require_golden", True):
        rep.add("golden_measurements", False, f"no pinned reference values for {key}; use NRAS (RIM) path")
    return rep


def _ver_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        try:
            out.append(int(part))
        except ValueError:
            break
    return tuple(out)


def _driver_check(rep: Report, version: str, policy: dict[str, Any]) -> None:
    mn = policy.get("gpu_min_driver_version")
    if mn:
        rep.add("driver_version", bool(version) and _ver_tuple(version) >= _ver_tuple(mn), f"{version or '?'} >= {mn}")


# ----------------------------------------------------------------------------------------------- NRAS (default path)
def nras_request_body(evidence_list: list[dict[str, str]], nonce_hex: str, arch: str = "HOPPER") -> dict[str, Any]:
    return {
        "nonce": nonce_hex,
        "arch": arch,
        "evidence_list": [{"evidence": e["evidence"], "certificate": e["certificate"]} for e in evidence_list],
        "claims_version": "3.0",
    }


def nras_attest(evidence_list: list[dict[str, str]], nonce_hex: str, arch: str = "HOPPER", timeout: int = 60) -> Any:
    """Network call, validator side. Returns NRAS JSON ([["JWT", overall], {"GPU-0": detached, ...}])."""
    import requests

    r = requests.post(NRAS_URL, json=nras_request_body(evidence_list, nonce_hex, arch), timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_jwks(timeout: int = 30) -> dict[str, Any]:
    import requests

    r = requests.get(NRAS_JWKS_URL, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _b64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _verify_jwt(token: Any, jwks: dict[str, Any], now: int, leeway: int, policy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(token, str):
        raise EvidenceError("token must be a string")
    try:
        h64, p64, s64 = token.split(".")
        header = json.loads(_b64url(h64))
        claims = json.loads(_b64url(p64))
        sig = _b64url(s64)
    except Exception as e:
        raise EvidenceError(f"malformed JWT: {type(e).__name__}") from e
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise EvidenceError("JWT header/claims must be JSON objects")
    if header.get("alg") != "ES384":
        raise EvidenceError(f"unexpected alg {header.get('alg')}")
    key = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
    if not key or not key.get("x5c"):
        raise EvidenceError(f"kid {header.get('kid')} not in JWKS")
    chain = [x509.load_der_x509_certificate(base64.b64decode(c)) for c in key["x5c"]]
    leaf = chain[0]
    try:
        for child, parent in zip(chain, chain[1:]):
            _ecdsa_link(parent, child)
    except InvalidSignature as e:
        raise EvidenceError("JWKS x5c chain invalid") from e
    pins = policy.get("nras_intermediate_spki_sha256", NRAS_INTERMEDIATE_SPKI_SHA256)
    if pins and len(chain) < 2:
        raise EvidenceError("x5c chain lacks the intermediate")
    if pins and hashlib.sha256(_spki(chain[-1])).hexdigest() not in pins:
        raise EvidenceError("x5c intermediate not pinned (unknown NRAS intermediate)")
    top_issuer = chain[-1].issuer.rfc4514_string()
    want = policy.get("nras_x5c_top_issuer_contains", "CN=NVIDIA Attestation Service CA")
    if want not in top_issuer:
        raise EvidenceError(f"x5c chain not issued under '{want}' (got {top_issuer})")
    if not (leaf.not_valid_before_utc.timestamp() - leeway <= now <= leaf.not_valid_after_utc.timestamp() + leeway):
        raise EvidenceError("NRAS signing certificate not valid at verification time")
    if len(sig) != 96:
        raise EvidenceError("ES384 signature must be 96 bytes")
    try:
        leaf.public_key().verify(
            encode_dss_signature(int.from_bytes(sig[:48], "big"), int.from_bytes(sig[48:], "big")),
            f"{h64}.{p64}".encode(),
            ec.ECDSA(hashes.SHA384()),
        )
    except InvalidSignature as e:
        raise EvidenceError("JWT signature invalid") from e
    if claims.get("iss") != NRAS_ISSUER:
        raise EvidenceError(f"issuer {claims.get('iss')}")
    if not (claims.get("nbf", 0) - leeway <= now <= claims.get("exp", 0) + leeway):
        raise EvidenceError("token outside nbf/exp window")
    return claims


@fail_closed("nvidia-nras")
def verify_nras(response: Any, jwks: dict[str, Any], expected_nonce: bytes, now: int, policy: dict[str, Any]) -> Report:
    rep = Report("nvidia-nras")
    leeway = int(policy.get("nras_leeway_seconds", 60))
    try:
        overall_tok = response[0][1]
        detached = response[1]
        if not (response[0][0] == "JWT" and isinstance(detached, dict) and isinstance(overall_tok, str)):
            raise ValueError
    except Exception:
        rep.add("shape", False, "expected [[\"JWT\", token], {\"GPU-n\": token}]")
        return rep
    try:
        ov = _verify_jwt(overall_tok, jwks, now, leeway, policy)
        rep.add("overall_signature", True, "ES384 via JWKS kid, x5c chain, iss, nbf/exp")
    except EvidenceError as e:
        rep.add("overall_signature", False, str(e))
        return rep
    rep.add("eat_nonce", hmac.compare_digest(str(ov.get("eat_nonce", "")).lower(), expected_nonce.hex()), "eat_nonce == session digest")
    rep.add("overall_result", ov.get("x-nvidia-overall-att-result") is True, f"x-nvidia-overall-att-result={ov.get('x-nvidia-overall-att-result')}")
    submods = ov.get("submods", {})
    rep.add("submods_cover_all", set(submods) == set(detached) and bool(detached), f"{len(detached)} GPU tokens")
    required = policy.get("gpu_required_claims", DEFAULT_GPU_CLAIMS)
    ueids: list[str] = []
    gpus: dict[str, Any] = {}
    if not isinstance(submods, dict):
        submods = {}
    for name, tok in sorted(detached.items()):
        dig = submods.get(name)
        ok_dig = (
            isinstance(tok, str) and isinstance(dig, list) and len(dig) == 2 and dig[0] == "DIGEST"
            and isinstance(dig[1], list) and len(dig[1]) == 2 and dig[1][0] == "SHA-256" and isinstance(dig[1][1], str)
            and hmac.compare_digest(dig[1][1], hashlib.sha256(tok.encode()).hexdigest())
        )
        rep.add(f"{name}.digest", ok_dig, "detached token hash == overall submods digest")
        try:
            c = _verify_jwt(tok, jwks, now, leeway, policy)
        except EvidenceError as e:
            rep.add(f"{name}.signature", False, str(e))
            continue
        rep.add(f"{name}.signature", True, "")
        if "x-nvidia-error-details" in c:
            rep.add(f"{name}.no_error", False, json.dumps(c["x-nvidia-error-details"].get("message")))
            continue
        bad = {k: c.get(k) for k, v in required.items() if c.get(k) != v}
        rep.add(f"{name}.claims", not bad, f"wrong: {bad}" if bad else "all required claims")
        rep.add(f"{name}.eat_nonce", hmac.compare_digest(str(c.get("eat_nonce", "")).lower(), expected_nonce.hex()), "")
        rep.add(f"{name}.hwmodel", hwmodel_allowed(c.get("hwmodel"), policy), str(c.get("hwmodel")))
        _driver_check(rep, str(c.get("x-nvidia-gpu-driver-version", "")), policy)
        ueid = c.get("ueid")
        rep.add(f"{name}.ueid_present", isinstance(ueid, str) and bool(ueid.strip()), "GPU UEID present")
        ueids.append(ueid if isinstance(ueid, str) else "")
        gpus[name] = {k: c.get(k) for k in ("hwmodel", "ueid", "x-nvidia-gpu-driver-version", "x-nvidia-gpu-vbios-version")}
    rep.add("distinct_gpus", len(set(ueids)) == len(ueids), "every GPU has a distinct UEID")
    rep.add("min_gpus", len(detached) >= int(policy.get("gpu_min_count", 1)), f"{len(detached)} >= {policy.get('gpu_min_count', 1)}")
    rep.claims["gpus"] = gpus
    return rep
