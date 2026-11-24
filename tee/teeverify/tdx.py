"""Intel TDX quote verification.

Crypto + TCB evaluation (PCK chain to Intel SGX Root CA, QE identity, TCB info, CRLs) is delegated to dcap-qvl
(Phala's Rust implementation, the same library Chutes uses). On top we add an independent byte-level parse of the
TD report and cross-check it against dcap-qvl's output, then apply SN92 policy: TCB status, debug bit, MRTD/RTMR
golden values, report_data binding, optional platform (PPID) registry.

Quote v4 layout: 48-byte header, then the 584-byte TD report (TD 1.0). Quote v5 adds a 6-byte body descriptor
(type u16, size u32) after the header; type 2 = TD 1.0 report, type 3 = TD 1.5 report (648 bytes, adds TEE_TCB_SVN2
and MRSERVICETD). The fields we use sit at the same offsets inside the body for both.
"""
from __future__ import annotations

import hmac
import json
from typing import Any

from .errors import EvidenceError, Report, fail_closed

TEE_TYPE_TDX = 0x81

# offset, length inside the TD report body
_TD_FIELDS = {
    "tee_tcb_svn": (0, 16),
    "mr_seam": (16, 48),
    "mr_signer_seam": (64, 48),
    "seam_attributes": (112, 8),
    "td_attributes": (120, 8),
    "xfam": (128, 8),
    "mr_td": (136, 48),
    "mr_config_id": (184, 48),
    "mr_owner": (232, 48),
    "mr_owner_config": (280, 48),
    "rt_mr0": (328, 48),
    "rt_mr1": (376, 48),
    "rt_mr2": (424, 48),
    "rt_mr3": (472, 48),
    "report_data": (520, 64),
}

TD_ATTR_DEBUG = 1 << 0
TD_ATTR_SEPT_VE_DISABLE = 1 << 28
TD_ATTR_PKS = 1 << 30
TD_ATTR_MIGRATABLE = 1 << 29
# default: DEBUG off, MIGRATABLE off, SEPT_VE_DISABLE on; all TUD (bits 0-7) and reserved bits must be 0
DEFAULT_ATTR_MASK = 0xFF | TD_ATTR_SEPT_VE_DISABLE | TD_ATTR_MIGRATABLE
DEFAULT_ATTR_EXPECTED = TD_ATTR_SEPT_VE_DISABLE


def parse_quote(quote: bytes) -> dict[str, Any]:
    if len(quote) < 48 + 584:
        raise EvidenceError("quote too short")
    version = int.from_bytes(quote[0:2], "little")
    att_key_type = int.from_bytes(quote[2:4], "little")
    tee_type = int.from_bytes(quote[4:8], "little")
    if tee_type != TEE_TYPE_TDX:
        raise EvidenceError(f"not a TDX quote (tee_type=0x{tee_type:x})")
    if version == 4:
        body_off, body_type = 48, 2
    elif version == 5:
        body_type = int.from_bytes(quote[48:50], "little")
        body_size = int.from_bytes(quote[50:54], "little")
        if body_type not in (2, 3):
            raise EvidenceError(f"unsupported v5 body type {body_type}")
        if body_size not in (584, 648):
            raise EvidenceError(f"unexpected v5 body size {body_size}")
        body_off = 54
    else:
        raise EvidenceError(f"unsupported quote version {version}")
    body = quote[body_off: body_off + 584]
    out: dict[str, Any] = {"version": version, "att_key_type": att_key_type, "body_type": body_type}
    for name, (off, ln) in _TD_FIELDS.items():
        out[name] = body[off: off + ln].hex()
    attrs = int.from_bytes(bytes.fromhex(out["td_attributes"]), "little")
    out["td_attributes_int"] = attrs
    out["debug"] = bool(attrs & TD_ATTR_DEBUG)
    out["sept_ve_disable"] = bool(attrs & TD_ATTR_SEPT_VE_DISABLE)
    return out


def dcap_verify(quote: bytes, collateral: dict | None, now: int) -> dict[str, Any]:
    """Run dcap-qvl. With collateral=None it fetches collateral from Intel PCS (network)."""
    try:
        import dcap_qvl  # type: ignore
    except ImportError as e:  # fail closed
        raise EvidenceError("dcap-qvl not installed; cannot verify TDX quote signature") from e
    try:
        if collateral is None:
            import asyncio

            col = asyncio.run(dcap_qvl.get_collateral_from_pcs(quote))
        else:
            col = dcap_qvl.QuoteCollateralV3.from_json(json.dumps(collateral))
        res = dcap_qvl.verify(quote, col, int(now))
    except Exception as e:  # dcap-qvl raises ValueError/RuntimeError on any failure
        raise EvidenceError(f"DCAP verification failed: {e}") from e
    return json.loads(res.to_json())


def _match_measurement_set(parsed: dict[str, Any], allowed: list[dict[str, str]]) -> tuple[bool, str]:
    """allowed = [{"name": "...", "mr_td": hex, "rt_mr0": hex, ...}] ; every key given must match exactly."""
    for entry in allowed:
        keys = [k for k in entry if k in _TD_FIELDS]
        if keys and all(hmac.compare_digest(parsed[k], entry[k].lower()) for k in keys):
            return True, entry.get("name", "unnamed")
    return False, ""


@fail_closed("tdx")
def verify_quote(
    quote: bytes,
    collateral: dict | None,
    now: int,
    policy: dict[str, Any],
    expected_report_data: bytes | None,
) -> Report:
    rep = Report("tdx")
    try:
        parsed = parse_quote(quote)
    except EvidenceError as e:
        rep.add("parse", False, str(e))
        return rep
    rep.add("parse", True, f"quote v{parsed['version']}")
    rep.claims["td"] = {k: parsed[k] for k in ("mr_td", "rt_mr0", "rt_mr1", "rt_mr2", "rt_mr3", "mr_seam", "tee_tcb_svn")}

    try:
        verified = dcap_verify(quote, collateral, now)
    except EvidenceError as e:
        rep.add("dcap_signature_and_collateral", False, str(e))
        return rep
    rep.add("dcap_signature_and_collateral", True, "PCK chain, QE identity and TCB info verified by dcap-qvl")

    # independent parse must agree with the library's view of the signed body
    td = (verified.get("report") or {}).get("TD10") or (verified.get("report") or {}).get("TD15") or {}
    mism = [k for k in ("mr_td", "rt_mr0", "rt_mr1", "rt_mr2", "rt_mr3", "report_data", "td_attributes") if td.get(k, "").lower() != parsed[k]]
    rep.add("parse_cross_check", not mism, "fields differ: " + ",".join(mism) if mism else "own parse == dcap-qvl")

    status = verified.get("status", "Unknown")
    advisories = set(verified.get("advisory_ids") or [])
    allowed_status = set(policy.get("tdx_allowed_tcb_status", ["UpToDate"]))
    allowed_adv = set(policy.get("tdx_allowed_advisories", []))
    rep.claims["tcb_status"] = status
    rep.claims["advisory_ids"] = sorted(advisories)
    rep.claims["ppid"] = verified.get("ppid")
    rep.add("tcb_status", status in allowed_status, f"{status} (allowed {sorted(allowed_status)})")
    bad_adv = advisories - allowed_adv
    rep.add("advisories", not bad_adv, "unaccepted: " + ",".join(sorted(bad_adv)) if bad_adv else "none unaccepted")

    attrs = parsed["td_attributes_int"]
    rep.add("debug_off", not parsed["debug"], f"td_attributes=0x{attrs:016x}")
    mask = int(policy.get("tdx_td_attributes_mask", DEFAULT_ATTR_MASK))
    want = int(policy.get("tdx_td_attributes_expected", DEFAULT_ATTR_EXPECTED))
    rep.add("td_attributes", attrs & mask == want, f"attrs&0x{mask:x}=0x{attrs & mask:x}, expected 0x{want:x} (no debug/TUD, not migratable, SEPT_VE_DISABLE)")
    xfams = [x.lower() for x in policy.get("tdx_allowed_xfam", [])]
    if xfams:
        rep.add("xfam", parsed["xfam"] in xfams, parsed["xfam"])
    min_eval = policy.get("tdx_min_tcb_evaluation_data_number")
    if min_eval is not None:
        got = None
        try:
            got = int(json.loads(collateral["tcb_info"])["tcbEvaluationDataNumber"]) if collateral else None
        except Exception:
            got = None
        rep.add("tcb_evaluation_number", got is not None and got >= int(min_eval), f"collateral TCB evaluation {got} >= {min_eval}")

    seams = [s.lower() for s in policy.get("tdx_allowed_mr_seam", [])]
    if seams:
        rep.add("mr_seam", parsed["mr_seam"] in seams, parsed["mr_seam"][:16] + "...")

    allowed_t2 = policy.get("tdx_measurements_t2", [])
    allowed_sets = allowed_t2 + policy.get("tdx_measurements", [])
    ok, name = _match_measurement_set(parsed, allowed_sets)
    mode = policy.get("tdx_measurement_mode", "enforce")
    if mode == "record":
        rep.add("measurement_recorded", True, "signed measurements recorded")
    else:
        rep.add("measurements", mode == "enforce" and ok, f"matched golden set '{name}'" if ok else "MRTD/RTMR not in any golden set")
    rep.claims["measurement_set"] = name or None
    rep.claims['assurance_level'] = (
        ('T2' if _match_measurement_set(parsed, allowed_t2)[0] else 'T1')
        if mode == 'enforce' and ok else None)

    if expected_report_data is not None:
        rd = bytes.fromhex(parsed["report_data"])
        rep.add("report_data_binding", hmac.compare_digest(rd, expected_report_data), "D || G (session digest, GPU set)")
    else:
        rep.add("report_data_binding", False, "no expected report_data given (fail closed)")
    return rep
