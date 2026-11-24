"""AMD SEV-SNP attestation report verification (report versions 2, 3 and 5; Milan, Genoa, Turin).

Trust chain: AMD Root Key (ARK, pinned by SPKI hash) -> AMD SEV Key (ASK) -> VCEK (per chip + per TCB),
all RSA-PSS SHA-384; the report is ECDSA P-384 / SHA-384 signed by the VCEK over bytes 0x000..0x29F.
The VCEK certificate is fetched by the validator from AMD KDS:
  https://kdsintf.amd.com/vcek/v1/{Milan|Genoa|Turin}/{chip_id_hex}?blSPL=..&teeSPL=..&snpSPL=..&ucodeSPL=..
so the miner cannot substitute a key: the URL is derived from the signed report itself.
VLEK-signed reports (cloud providers sign with a Versioned Loaded Endorsement Key that AMD issues to the CSP) are
accepted when policy allows: chain ARK -> ASVK (CN=SEV-VLEK-<product>) -> VLEK, chain from
https://kdsintf.amd.com/vlek/v1/<product>/cert_chain; the VLEK cert comes from the CSP (no hwID; CSP id extension).
"""
from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .errors import EvidenceError, Report, fail_closed

REPORT_SIZE = 0x4A0
SIGNED_LEN = 0x2A0

# SHA-256 of the DER SubjectPublicKeyInfo of each AMD Root Key, read from https://kdsintf.amd.com/vcek/v1/<p>/cert_chain
# on 2026-11-23 (copies in teeverify/roots/). Pinning the key, not the cert, survives re-issuance with the same key.
ARK_SPKI_SHA256 = {
    "Milan": "9f056bee44377e29308cb5ffa895bdfb62d18881fa6bed8d6f075b0204089cb9",
    "Genoa": "429a69c9422aa258ee4d8db5fcda9c6470ef15f8cd5a9cebd6cbc7d90b863831",
    "Turin": "4f125410563a2ab9a50356f9243f6fe0b6f73de98603f53f90339c70e9d7ad08",
}
ROOTS_DIR = Path(__file__).with_name("roots")

POLICY_SMT = 1 << 16
POLICY_RESERVED1 = 1 << 17
POLICY_MIGRATE_MA = 1 << 18
POLICY_DEBUG = 1 << 19
POLICY_SINGLE_SOCKET = 1 << 20

OID_PRODUCT = "1.3.6.1.4.1.3704.1.2"
OID_BL = "1.3.6.1.4.1.3704.1.3.1"
OID_TEE = "1.3.6.1.4.1.3704.1.3.2"
OID_SNP = "1.3.6.1.4.1.3704.1.3.3"
OID_UCODE = "1.3.6.1.4.1.3704.1.3.8"
OID_FMC = "1.3.6.1.4.1.3704.1.3.9"
OID_HWID = "1.3.6.1.4.1.3704.1.4"
OID_CSP_ID = "1.3.6.1.4.1.3704.1.5"


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "little")


def _u64(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 8], "little")


def decode_tcb(tcb: int, product: str) -> dict[str, int]:
    b = tcb.to_bytes(8, "little")
    if product == "Turin":
        return {"fmc": b[0], "bootloader": b[1], "tee": b[2], "snp": b[3], "microcode": b[7]}
    return {"bootloader": b[0], "tee": b[1], "snp": b[6], "microcode": b[7]}


def parse_report(report: bytes) -> dict[str, Any]:
    if len(report) != REPORT_SIZE:
        raise EvidenceError(f"SNP report must be {REPORT_SIZE} bytes, got {len(report)}")
    version = _u32(report, 0x00)
    if version not in (2, 3, 5):
        raise EvidenceError(f"unsupported SNP report version {version}")
    flags = _u32(report, 0x48)
    out = {
        "version": version,
        "guest_svn": _u32(report, 0x04),
        "policy": _u64(report, 0x08),
        "family_id": report[0x10:0x20].hex(),
        "image_id": report[0x20:0x30].hex(),
        "vmpl": _u32(report, 0x30),
        "signature_algo": _u32(report, 0x34),
        "current_tcb": _u64(report, 0x38),
        "platform_info": _u64(report, 0x40),
        "author_key_en": bool(flags & 1),
        "mask_chip_id": bool(flags & 2),
        "signing_key": (flags >> 2) & 7,  # 0 VCEK, 1 VLEK
        "report_data": report[0x50:0x90].hex(),
        "measurement": report[0x90:0xC0].hex(),
        "host_data": report[0xC0:0xE0].hex(),
        "id_key_digest": report[0xE0:0x110].hex(),
        "author_key_digest": report[0x110:0x140].hex(),
        "report_id": report[0x140:0x160].hex(),
        "reported_tcb": _u64(report, 0x180),
        "chip_id": report[0x1A0:0x1E0].hex(),
        "committed_tcb": _u64(report, 0x1E0),
        "launch_tcb": _u64(report, 0x1F8),
    }
    if version >= 3:
        out["cpuid_fam_id"] = report[0x188]
        out["cpuid_mod_id"] = report[0x189]
    p = out["policy"]
    out["policy_debug"] = bool(p & POLICY_DEBUG)
    out["policy_migrate_ma"] = bool(p & POLICY_MIGRATE_MA)
    out["policy_smt"] = bool(p & POLICY_SMT)
    return out


def report_product(parsed: dict[str, Any]) -> str:
    """Product from signed CPUID fields; v2 reports have no such fields."""
    product = {(0x19, 0x01): "Milan", (0x19, 0x11): "Genoa", (0x1a, 0x02): "Turin"}.get(
        (parsed.get("cpuid_fam_id"), parsed.get("cpuid_mod_id")))
    if not product:
        raise EvidenceError("unknown or missing report CPUID product")
    return product


def _report_signature(report: bytes) -> bytes:
    r = int.from_bytes(report[0x2A0:0x2A0 + 72], "little")
    s = int.from_bytes(report[0x2A0 + 72:0x2A0 + 144], "little")
    return encode_dss_signature(r, s)


def _spki_sha256(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def _pss_verify(issuer: x509.Certificate, cert: x509.Certificate) -> None:
    issuer.public_key().verify(
        cert.signature,
        cert.tbs_certificate_bytes,
        padding.PSS(mgf=padding.MGF1(hashes.SHA384()), salt_length=48),
        hashes.SHA384(),
    )


def _ext_raw(cert: x509.Certificate, oid: str) -> bytes | None:
    for e in cert.extensions:
        if e.oid.dotted_string == oid:
            return getattr(e.value, "value", None)
    return None


def _der_int(raw: bytes | None) -> int | None:
    if not raw or raw[0] != 0x02:
        return None
    return int.from_bytes(raw[2:2 + raw[1]], "big")


def _vcek_product(vcek: x509.Certificate) -> str:
    raw = _ext_raw(vcek, OID_PRODUCT) or b""
    # IA5String: 0x16 len "Milan-B0"
    name = raw[2:].decode(errors="replace") if raw[:1] == b"\x16" else raw.decode(errors="replace")
    return name.split("-")[0]


def _in_validity(cert: x509.Certificate, now: int) -> bool:
    return cert.not_valid_before_utc.timestamp() <= now <= cert.not_valid_after_utc.timestamp()


@fail_closed("sev-snp")
def verify_report(
    report: bytes,
    vcek_der: bytes,
    chain_pem: bytes,
    now: int,
    policy: dict[str, Any],
    expected_report_data: bytes | None,
) -> Report:
    rep = Report("sev-snp")
    try:
        parsed = parse_report(report)
    except EvidenceError as e:
        rep.add("parse", False, str(e))
        return rep
    rep.add("parse", True, f"report v{parsed['version']}")
    allowed_keys = [0, 1] if policy.get("snp_allow_vlek", True) else [0]
    rep.add("signing_key_allowed", parsed["signing_key"] in allowed_keys, f"signing_key={parsed['signing_key']} (0=VCEK, 1=VLEK) allowed {allowed_keys}")
    rep.claims.update({k: parsed[k] for k in ("measurement", "host_data", "chip_id", "vmpl", "guest_svn")})

    try:
        vcek = x509.load_der_x509_certificate(vcek_der)
        chain = x509.load_pem_x509_certificates(chain_pem)
    except Exception as e:
        rep.add("certs_load", False, str(e))
        return rep
    by_cn = {c.subject.rfc4514_string().split(",")[0]: c for c in chain}
    ark = next((c for k, c in by_cn.items() if k.startswith("CN=ARK-")), None)
    if ark is None:
        rep.add("chain_shape", False, "need ARK (CN=ARK-*)")
        return rep
    product = ark.subject.rfc4514_string().split(",")[0].removeprefix("CN=ARK-")
    vlek = parsed["signing_key"] == 1
    ask = by_cn.get(f"CN=SEV-VLEK-{product}" if vlek else f"CN=SEV-{product}")
    if ask is None:
        rep.add("chain_shape", False, f"need {'ASVK CN=SEV-VLEK-' if vlek else 'ASK CN=SEV-'}{product} for signing_key={parsed['signing_key']}")
        return rep
    rep.claims["product"] = product
    pins = policy.get("snp_ark_spki_sha256", ARK_SPKI_SHA256)
    if not rep.add("ark_pinned", hmac.compare_digest(_spki_sha256(ark), pins.get(product, "")), f"ARK-{product}"):
        return rep
    try:
        _pss_verify(ark, ark)
        _pss_verify(ark, ask)
        _pss_verify(ask, vcek)
        rep.add("chain_signatures", True, "ARK self-signed, ARK->ASK, ASK->VCEK (RSA-PSS SHA-384)")
    except InvalidSignature:
        rep.add("chain_signatures", False, "certificate chain signature invalid")
        return rep
    rep.add("chain_validity", all(_in_validity(c, now) for c in (ark, ask, vcek)), "notBefore/notAfter at verification time")
    vprod = _vcek_product(vcek)
    rep.add("vcek_product", vprod == product, f"VCEK product {vprod} vs ARK {product}")

    leaf_cn = vcek.subject.rfc4514_string().split(",")[0]
    rep.add("leaf_type_matches", leaf_cn == ("CN=SEV-VLEK" if vlek else "CN=SEV-VCEK"), leaf_cn)
    rep.add("signature_algo", parsed["signature_algo"] == 1, "ECDSA P-384 SHA-384")
    pub = vcek.public_key()
    try:
        if not isinstance(pub, ec.EllipticCurvePublicKey):
            raise InvalidSignature()
        pub.verify(_report_signature(report), report[:SIGNED_LEN], ec.ECDSA(hashes.SHA384()))
        rep.add("report_signature", True, "VCEK signature over report[0:0x2A0]")
    except InvalidSignature:
        rep.add("report_signature", False, "report signature invalid")
        return rep

    if parsed["version"] >= 3:
        try:
            report_prod = report_product(parsed)
        except EvidenceError:
            report_prod = None
        rep.add("report_product", report_prod == product == vprod, "signed CPUID agrees with certificate product")

    # VCEK must be the one for THIS chip and THIS reported TCB (otherwise an old, weaker TCB key could sign)
    hwid = _ext_raw(vcek, OID_HWID) or b""
    if hwid[:1] == b"\x04" and len(hwid) in (66, 10) and hwid[1] == len(hwid) - 2:
        hwid = hwid[2:]
    chip_hex = parsed["chip_id"]
    if product == "Turin":
        # Turin VCEKs carry an 8-byte hwID; the report's CHIP_ID holds it in the first 8 bytes, the rest must be zero
        # (seen live on Verda 1RTXPRO6000.30V.CC, 24 Nov 2026).
        if chip_hex[16:].strip("0") != "":
            chip_hex = "nonzero-tail:" + chip_hex
        else:
            chip_hex = chip_hex[:16]
    if vlek:
        csp = _ext_raw(vcek, OID_CSP_ID) or b""
        csp_name = csp[2:].decode(errors="replace") if csp[:1] in (b"\x16", b"\x0c") else csp.decode(errors="replace")
        rep.claims["csp_id"] = csp_name
        allowed_csp = policy.get("snp_allowed_csp_ids", [])
        if allowed_csp:
            rep.add("vlek_csp_allowed", csp_name in allowed_csp, f"CSP id {csp_name!r}")
    elif not parsed["mask_chip_id"]:
        rep.add("vcek_hwid_matches_chip_id", bool(hwid) and hwid.hex() == chip_hex, "VCEK hwID == report CHIP_ID")
    tcb = decode_tcb(parsed["reported_tcb"], product)
    cert_tcb = {
        "bootloader": _der_int(_ext_raw(vcek, OID_BL)),
        "tee": _der_int(_ext_raw(vcek, OID_TEE)),
        "snp": _der_int(_ext_raw(vcek, OID_SNP)),
        "microcode": _der_int(_ext_raw(vcek, OID_UCODE)),
    }
    if product == "Turin":
        cert_tcb["fmc"] = _der_int(_ext_raw(vcek, OID_FMC))
    rep.claims["reported_tcb"] = tcb
    rep.add("vcek_tcb_matches_reported_tcb", cert_tcb == tcb, f"cert {cert_tcb} vs report {tcb}")
    mins = policy.get("snp_min_tcb", {}).get(product)
    if not mins:  # fail closed: old-firmware VCEKs still chain to AMD, so a minimum is mandatory
        rep.add("min_tcb", False, f"no minimum TCB configured for {product}")
    else:
        low = {k: (tcb.get(k), v) for k, v in mins.items() if tcb.get(k, -1) < v}
        rep.add("min_tcb", not low, f"below minimum: {low}" if low else f"meets {mins}")

    rep.add("debug_off", not parsed["policy_debug"], f"policy=0x{parsed['policy']:x}")
    if policy.get("snp_forbid_migration_agent", True):
        rep.add("no_migration_agent", not parsed["policy_migrate_ma"], "MIGRATE_MA bit clear")
    allowed_vmpl = policy.get("snp_allowed_vmpl", [0])
    rep.add("vmpl", parsed["vmpl"] in allowed_vmpl, f"vmpl={parsed['vmpl']} allowed {allowed_vmpl}")

    golden_t1 = [m.lower() for m in policy.get("snp_measurements", [])]
    golden_t2 = [m.lower() for m in policy.get("snp_measurements_t2", [])]
    golden = golden_t1 + golden_t2
    mode = policy.get("snp_measurement_mode", "enforce")
    if mode == "record":
        rep.add("measurement_recorded", True, "signed launch digest recorded")
    else:
        rep.add("measurement", mode == "enforce" and parsed["measurement"] in golden, "launch digest in golden list")
    rep.claims['assurance_level'] = (
        ('T2' if parsed['measurement'] in golden_t2 else 'T1')
        if mode == 'enforce' and parsed['measurement'] in golden else None)
    hosts = [h.lower() for h in policy.get("snp_allowed_host_data", [])]
    if hosts:
        rep.add("host_data", parsed["host_data"] in hosts, "HOST_DATA (image manifest digest) allowed")

    if expected_report_data is not None:
        rep.add("report_data_binding", hmac.compare_digest(bytes.fromhex(parsed["report_data"]), expected_report_data), "D || G (session digest, GPU set)")
    else:
        rep.add("report_data_binding", False, "no expected report_data given (fail closed)")
    return rep


def kds_vcek_url(parsed: dict[str, Any], product: str) -> str:
    """URL the validator itself uses to fetch the VCEK (never trust a miner-supplied VCEK blindly; verify the chain)."""
    t = decode_tcb(parsed["reported_tcb"], product)
    q = f"blSPL={t['bootloader']}&teeSPL={t['tee']}&snpSPL={t['snp']}&ucodeSPL={t['microcode']}"
    if product == "Turin":
        q = f"fmcSPL={t['fmc']}&" + q
        chip = parsed["chip_id"][:16]  # Turin KDS uses the first 8 bytes
    else:
        chip = parsed["chip_id"]
    return f"https://kdsintf.amd.com/vcek/v1/{product}/{chip}?{q}"


def kds_chain_for(vcek_der: bytes) -> bytes:
    """AMD KDS chains (VCEK and VLEK variants) for the product named in the leaf certificate, from roots/."""
    product = _vcek_product(x509.load_der_x509_certificate(vcek_der))
    if product not in ARK_SPKI_SHA256:
        raise EvidenceError(f"unknown AMD product {product!r}")
    return (ROOTS_DIR / f"amd_{product}_cert_chain.pem").read_bytes() + (ROOTS_DIR / f"amd_{product}_vlek_cert_chain.pem").read_bytes()
