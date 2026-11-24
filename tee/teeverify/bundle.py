"""One-call verification of an SN92 EU-tier evidence bundle.

Trust rule (after the 23 Nov independent review): the bundle carries ONLY miner-side evidence. Everything that decides
trust comes from the validator as separate arguments: the nonce it issued, the hotkey it challenged, the TLS key it
saw on its own connection, the Intel collateral it fetched, the NRAS response it obtained by posting the raw GPU
evidence itself, and the RTT probes its own probe service measured.

Bundle (JSON, schema "sn92-eu-tee-evidence/1"), produced by the in-CVM attestation agent:
{
  "schema": "sn92-eu-tee-evidence/1",
  "miner_hotkey": "5F...",
  "validator_nonce": "<64 hex>",
  "enclave_pubkey": "<64 hex, X25519, generated inside the CVM at boot>",
  "tls_spki_sha256": "<64 hex, SHA-256 of the CVM's TLS server SubjectPublicKeyInfo>",
  "cpu": {"type": "tdx", "quote": "<b64>"}
       | {"type": "snp", "report": "<b64>", "vcek": "<b64 DER VCEK or VLEK>", "cert_chain": "<optional PEM; default = AMD KDS copy in roots/>"},
  "gpu": {"evidence_list": [{"evidence": "<b64 SPDM transcript>", "certificate": "<b64 PEM chain>", "arch": "HOPPER"}]}
}
report_data = D || G where D = binding.session_digest(...) and G = binding.gpu_set_digest(device ids of evidence_list).
"""
from __future__ import annotations

import base64
import binascii
import hmac
import time
from dataclasses import dataclass, field
from typing import Any

from . import binding, location, nvidia, snp, tdx
from .errors import Report

SUPPORTS_BINDING_V2 = True
SCHEMA = "sn92-eu-tee-evidence/1"
SCHEMA_V2 = "sn92-eu-tee-evidence/2"


@dataclass
class Verdict:
    ok: bool
    session_digest: str
    reports: list[Report] = field(default_factory=list)
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def key_release_allowed(self) -> bool:
        return self.ok

    def failed(self) -> list[str]:
        return [f"{r.kind}.{c.name}: {c.detail}" for r in self.reports for c in r.failed()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "assurance_level": self.claims.get('assurance_level'),
            "key_release_allowed": self.key_release_allowed,
            "session_digest": self.session_digest,
            "failed": self.failed(),
            "reports": [r.to_dict() for r in self.reports],
            "claims": self.claims,
        }


class BundleShapeError(ValueError):
    pass


def valid_assurance(policy):
    if not isinstance(policy, dict) or policy.get('required_assurance', 'T1') not in ('T1', 'T2'):
        return False
    level = policy.get('assurance_level')
    if 'assurance_level' in policy and level not in ('T1', 'T2'):
        return False
    cpus = policy.get('allowed_cpu_tee', ['snp', 'tdx'])
    if not isinstance(cpus, list) or not cpus or any(cpu not in ('snp', 'tdx') for cpu in cpus):
        return False
    for cpu in ('snp', 'tdx'):
        for suffix in ('_measurements', '_measurements_t2'):
            if cpu + suffix in policy and not isinstance(policy[cpu + suffix], list):
                return False
    if legacy_assurance(policy):
        return level == 'T1' or all(policy.get(cpu + '_measurement_mode') == 'enforce' for cpu in cpus)
    return any(cpu + suffix in policy for cpu in cpus for suffix in ('_measurements', '_measurements_t2'))


def legacy_assurance(policy):
    """One-release compatibility; even an empty T2 list selects per-image levels."""
    return policy.get('assurance_level') if not any(
        cpu + '_measurements_t2' in policy for cpu in ('snp', 'tdx')) else None


def meets_required_assurance(level, policy):
    ranks = {'T1': 1, 'T2': 2}
    return level in ranks and policy.get('required_assurance', 'T1') in ranks and ranks[level] >= ranks[policy.get('required_assurance', 'T1')]


def _hex32(v: Any, name: str) -> bytes:
    if not isinstance(v, str) or len(v) != 64:
        raise BundleShapeError(f"{name} must be 64 hex chars")
    try:
        return bytes.fromhex(v)
    except ValueError as e:
        raise BundleShapeError(f"{name} not hex") from e


def _b64(v: Any, name: str) -> bytes:
    if not isinstance(v, str):
        raise BundleShapeError(f"{name} must be a base64 string")
    try:
        return base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError) as e:
        raise BundleShapeError(f"{name} not base64") from e


def _check_shape(b: Any) -> None:
    if not isinstance(b, dict):
        raise BundleShapeError("bundle must be an object")
    for k in ("miner_hotkey", "validator_nonce", "enclave_pubkey", "tls_spki_sha256"):
        if not isinstance(b.get(k), str) or not b[k]:
            raise BundleShapeError(f"{k} missing or not a string")
    cpu = b.get("cpu")
    if not isinstance(cpu, dict) or cpu.get("type") not in ("tdx", "snp"):
        raise BundleShapeError("cpu must be an object with type tdx|snp")
    if cpu["type"] == "tdx":
        _b64(cpu.get("quote"), "cpu.quote")
    else:
        _b64(cpu.get("report"), "cpu.report")
        if cpu.get("vcek") is not None:
            _b64(cpu["vcek"], "cpu.vcek")
        if cpu.get("cert_chain") is not None and not isinstance(cpu.get("cert_chain"), str):
            raise BundleShapeError("cpu.cert_chain must be a PEM string if present")
    gpu = b.get("gpu")
    if not isinstance(gpu, dict) or not isinstance(gpu.get("evidence_list"), list) or not gpu["evidence_list"]:
        raise BundleShapeError("gpu.evidence_list must be a non-empty list")
    for i, e in enumerate(gpu["evidence_list"]):
        if not isinstance(e, dict):
            raise BundleShapeError(f"gpu.evidence_list[{i}] must be an object")
        _b64(e.get("evidence"), f"gpu.evidence_list[{i}].evidence")
        _b64(e.get("certificate"), f"gpu.evidence_list[{i}].certificate")


def gpu_device_ids(bundle: dict[str, Any]) -> list[str]:
    return [nvidia.device_identity(base64.b64decode(e["certificate"])) for e in bundle["gpu"]["evidence_list"]]


def verify_bundle(
    bundle: Any,
    policy: dict[str, Any],
    *,
    expected_nonce_hex: str,
    expected_hotkey: str,
    expected_tls_spki_hex: str,
    expected_wg_pubkey: str | bytes | None = None,
    snp_vcek_der: bytes | None = None,
    registered_site: dict[str, Any] | None = None,
    now: int | None = None,
    jwks: dict[str, Any] | None = None,
    nras_response: Any = None,
    tdx_collateral: dict[str, Any] | None = None,
    rtt_probes: list[dict[str, Any]] | None = None,
) -> Verdict:
    """All keyword arguments are validator-side facts. Never fill them from the bundle."""
    try:
        return _verify(bundle, policy, expected_nonce_hex, expected_hotkey, expected_tls_spki_hex, now, jwks,
                       nras_response, tdx_collateral, rtt_probes, expected_wg_pubkey, snp_vcek_der, registered_site)
    except Exception as e:  # noqa: BLE001 - untrusted input must never crash the validator loop
        r = Report("bundle")
        r.add("internal_error", False, f"{type(e).__name__} on untrusted input")
        return Verdict(False, "", [r])


def _verify(bundle, policy, expected_nonce_hex, expected_hotkey, expected_tls_spki_hex, now, jwks, nras_response,
            tdx_collateral, rtt_probes, expected_wg_pubkey, snp_vcek_der, registered_site) -> Verdict:
    now = int(time.time()) if now is None else int(now)
    head = Report("bundle")
    if not head.add('assurance_level', valid_assurance(policy), 'valid measurement assurance policy required'):
        return Verdict(False, '', [head])
    try:
        _check_shape(bundle)
        vn = _hex32(bundle["validator_nonce"], "validator_nonce")
        pub = _hex32(bundle["enclave_pubkey"], "enclave_pubkey")
        tls = _hex32(bundle["tls_spki_sha256"], "tls_spki_sha256")
        exp_nonce = _hex32(expected_nonce_hex, "expected_nonce_hex")
        exp_tls = _hex32(expected_tls_spki_hex, "expected_tls_spki_hex")
    except BundleShapeError as e:
        head.add("shape", False, str(e))
        return Verdict(False, "", [head])
    version = bundle.get("schema")
    v2 = version == SCHEMA_V2 and bundle.get("binding") == "v2"
    v1 = version == SCHEMA and "binding" not in bundle
    head.add("schema", v1 or v2, "schema and binding version agree")
    claims = {'assurance_level': None}
    if v2:
        try:
            wg = _b64(bundle.get("wg_pubkey"), "wg_pubkey")
            if len(wg) not in (0, 32):
                raise BundleShapeError("invalid wg_pubkey length")
            whash = binding.workload_digest(bundle.get("workload"))
            if bundle.get("agent_version") != bundle["workload"]["agent_version"]:
                raise BundleShapeError("agent_version mismatch")
            claims["workload_digest"] = whash.hex()
            if expected_wg_pubkey is None:
                head.add("wg_key_is_expected", False, "validator WireGuard key required")
            else:
                expected_wg = _b64(expected_wg_pubkey, "expected_wg_pubkey") if isinstance(expected_wg_pubkey, str) else expected_wg_pubkey
                head.add("wg_key_is_expected", isinstance(expected_wg, bytes) and len(expected_wg) == 32 and hmac.compare_digest(wg, expected_wg), "WireGuard key matches validator")
        except (BundleShapeError, ValueError, TypeError, KeyError) as e:
            head.add("shape", False, str(e))
            return Verdict(False, "", [head])
    elif expected_wg_pubkey is not None:
        head.add("wg_key_is_expected", False, "v1 has no WireGuard binding")
    head.add("nonce_is_ours", hmac.compare_digest(vn, exp_nonce), "bundle nonce == nonce this validator issued")
    head.add("hotkey_is_challenged_one", isinstance(expected_hotkey, str) and bool(expected_hotkey)
             and hmac.compare_digest(bundle["miner_hotkey"].encode(), expected_hotkey.encode()), "bundle hotkey == challenged hotkey")
    head.add("tls_key_is_this_connection", hmac.compare_digest(tls, exp_tls), "bound TLS SPKI == SPKI on the validator's own TLS connection")
    digest = binding.session_digest_v2(vn, pub, tls, wg, bundle["miner_hotkey"], whash) if v2 else binding.session_digest(vn, pub, tls, bundle["miner_hotkey"])
    reports = [head]

    # GPU identities (from the per-device certificate chains) go into report_data[32:64]
    ids = gpu_device_ids(bundle)
    if not head.add("gpu_ids_distinct", len(set(ids)) == len(ids), f"{len(set(ids))} distinct of {len(ids)} GPU evidence entries"):
        return Verdict(False, digest.hex(), reports, claims)
    gset = binding.gpu_set_digest(ids)
    head.add("min_gpus", len(ids) >= int(policy.get("gpu_min_count", 1)), f"{len(ids)} >= {policy.get('gpu_min_count', 1)}")
    rd = digest + gset

    cpu = bundle["cpu"]
    platform_id = None
    head.add("cpu_type_allowed", cpu["type"] in policy.get("allowed_cpu_tee", ["tdx", "snp"]), cpu["type"])
    if cpu["type"] == "tdx":
        if tdx_collateral is None and not policy.get("tdx_fetch_collateral", False):
            r = Report("tdx")
            r.add("collateral", False, "no validator-fetched Intel collateral (bundle collateral is never used)")
        else:
            r = tdx.verify_quote(base64.b64decode(cpu["quote"]), tdx_collateral, now, policy, rd)
            platform_id = r.claims.get("ppid")
    else:
        vcek_der = snp_vcek_der if snp_vcek_der is not None else _b64(cpu.get("vcek"), "cpu.vcek")
        chain = cpu["cert_chain"].encode() if cpu.get("cert_chain") else snp.kds_chain_for(vcek_der)
        r = snp.verify_report(base64.b64decode(cpu["report"]), vcek_der, chain, now, policy, rd)
        cid = r.claims.get("chip_id")
        platform_id = cid if cid and cid != "00" * 64 else None
    reports.append(r)
    claims['assurance_level'] = legacy_assurance(policy) or r.claims.get('assurance_level')
    head.add('assurance_below_required', meets_required_assurance(claims['assurance_level'], policy),
             'CPU measurement assurance must meet required_assurance')

    mode = policy.get("gpu_mode", "nras")
    if mode == "nras":
        if jwks is None or nras_response is None:
            g = Report("nvidia-nras")
            g.add("validator_nras", False, "validator must post the raw evidence to NRAS itself and pass the response + JWKS")
        else:
            g = nvidia.verify_nras(nras_response, jwks, digest, now, policy)
            n_tok = len(nras_response[1]) if isinstance(nras_response, list) and len(nras_response) > 1 and isinstance(nras_response[1], dict) else -1
            g.add("token_count_matches_evidence", n_tok == len(ids), f"{n_tok} NRAS GPU tokens for {len(ids)} evidence entries")
        reports.append(g)
    elif mode == "local":
        if policy.get('gpu_allow_local_mode') is not True:
            g = Report('gpu')
            g.add('gpu_allow_local_mode', False, 'local appraisal requires explicit permission')
            reports.append(g)
            return Verdict(False, digest.hex(), reports, claims)
        for i, e in enumerate(bundle["gpu"]["evidence_list"]):
            g = nvidia.verify_local(base64.b64decode(e["evidence"]), base64.b64decode(e["certificate"]), digest, now, policy)
            g.kind = f"nvidia-local[{i}]"
            reports.append(g)
    else:
        g = Report("gpu")
        g.add("gpu_mode", False, f"unknown gpu_mode {mode!r}")
        reports.append(g)

    if policy.get("require_eu_location", True):
        reports.append(location.verify_location(platform_id, {"rtt_probes": rtt_probes or [],
                                                               'registered_site': registered_site}, policy))

    ok = all(rep.ok for rep in reports)
    return Verdict(ok, digest.hex(), reports, claims)


def nras_for_bundle(bundle: dict[str, Any], arch: str, attest=None) -> Any:
    """Validator side: post the bundle's raw GPU evidence to NRAS with nonce D and return the response, to be passed
    to verify_bundle(nras_response=...). The miner never supplies an NRAS token."""
    _check_shape(bundle)
    args = (_hex32(bundle["validator_nonce"], "validator_nonce"), _hex32(bundle["enclave_pubkey"], "enclave_pubkey"),
            _hex32(bundle["tls_spki_sha256"], "tls_spki_sha256"))
    if bundle.get("schema") == SCHEMA_V2 and bundle.get("binding") == "v2":
        wg = _b64(bundle.get("wg_pubkey"), "wg_pubkey")
        digest = binding.session_digest_v2(*args, wg, bundle["miner_hotkey"], binding.workload_digest(bundle.get("workload")))
    elif bundle.get("schema") == SCHEMA and "binding" not in bundle:
        digest = binding.session_digest(*args, bundle["miner_hotkey"])
    else:
        raise BundleShapeError("schema and binding version disagree")
    attest = attest or nvidia.nras_attest
    return attest(bundle["gpu"]["evidence_list"], digest.hex(), arch)


__all__ = ["SCHEMA", "Verdict", "verify_bundle", "nras_for_bundle", "gpu_device_ids"]
