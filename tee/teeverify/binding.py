"""Session binding: one digest ties validator nonce, enclave key, TLS key and miner hotkey together.

The same 32-byte digest D is
  * the NVIDIA GPU attestation nonce (SPDM GET_MEASUREMENTS nonce / NRAS eat_nonce) for every GPU, and
  * report_data[0:32] of the CPU quote (TDX REPORTDATA / SNP REPORT_DATA).
report_data[32:64] = G = SHA256(domain || sorted GPU device identities) that the in-CVM agent read from the GPUs it
attested ITSELF (collected first, quote second). The validator recomputes G from the GPU evidence it verified, so GPU
evidence borrowed from another machine (which can also be produced for D, since D is computable from public fields)
does not match the quote. Freshness comes from the validator nonce; the X25519 key that receives data keys and the TLS
key the gateway pins were chosen inside the attested CVM; the hotkey ties the session to one miner.

Chutes uses sha256(nonce_hex + e2e_pubkey_b64) as ASCII; we use a length-prefixed, domain-separated encoding so
no two field combinations can collide.
"""
from __future__ import annotations

import hashlib
import hmac
import json

DOMAIN = b"SN92-EU-TEE-SESSION-v1"


def _lp(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def session_digest(validator_nonce: bytes, enclave_pubkey: bytes, tls_spki_sha256: bytes, miner_hotkey: str) -> bytes:
    if len(validator_nonce) != 32:
        raise ValueError("validator nonce must be 32 bytes")
    if len(enclave_pubkey) != 32:
        raise ValueError("enclave X25519 public key must be 32 bytes")
    if len(tls_spki_sha256) != 32:
        raise ValueError("tls_spki_sha256 must be 32 bytes")
    if not miner_hotkey:
        raise ValueError("miner hotkey required")
    h = hashlib.sha256()
    h.update(_lp(DOMAIN))
    for part in (validator_nonce, enclave_pubkey, tls_spki_sha256, miner_hotkey.encode()):
        h.update(_lp(part))
    return h.digest()


GPU_DOMAIN = b"SN92-EU-TEE-GPUSET-v1"


def gpu_set_digest(device_ids: list[str]) -> bytes:
    """device_ids = hex SHA-256 of each GPU's per-device certificate key (nvidia.device_identity). Order-free, no dups."""
    if len(set(device_ids)) != len(device_ids):
        raise ValueError("duplicate GPU identity")
    h = hashlib.sha256(_lp(GPU_DOMAIN))
    for d in sorted(x.lower() for x in device_ids):
        h.update(_lp(bytes.fromhex(d)))
    return h.digest()


def expected_report_data(digest: bytes, device_ids: list[str]) -> bytes:
    """64-byte report_data the guest must request: D || G."""
    return digest + gpu_set_digest(device_ids)


def report_data_matches(report_data: bytes, digest: bytes, device_ids: list[str]) -> bool:
    return len(report_data) == 64 and hmac.compare_digest(report_data, expected_report_data(digest, device_ids))


def workload_digest(workload: dict) -> bytes:
    """SHA-256 of the UTF-8, sorted-key, compact canonical workload JSON."""
    keys = {"serving_image", "agent_version", "runbook_version"}
    if not isinstance(workload, dict) or set(workload) != keys or any(
        not isinstance(workload[k], str) or not workload[k] for k in keys
    ):
        raise ValueError("invalid workload")
    return hashlib.sha256(json.dumps(workload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).digest()


def session_digest_v2(validator_nonce: bytes, enclave_pubkey: bytes, tls_spki_sha256: bytes,
                      wg_pubkey: bytes, miner_hotkey: str, workload_hash: bytes) -> bytes:
    if any(len(x) != 32 for x in (validator_nonce, enclave_pubkey, tls_spki_sha256, workload_hash)):
        raise ValueError("nonce, keys and workload digest must be 32 bytes")
    if len(wg_pubkey) not in (0, 32) or not isinstance(miner_hotkey, str) or not miner_hotkey:
        raise ValueError("invalid WireGuard key or hotkey")
    h = hashlib.sha256()
    for part in (b"SN92-EU-TEE-SESSION-v2", validator_nonce, enclave_pubkey, tls_spki_sha256,
                 wg_pubkey, miner_hotkey.encode(), workload_hash):
        h.update(_lp(part))
    return h.digest()
