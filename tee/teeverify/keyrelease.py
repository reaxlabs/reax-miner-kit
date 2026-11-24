"""Release a customer data key only to an attested enclave.

The enclave generates an X25519 key pair inside the CVM at boot; the public key is bound into report_data through
binding.session_digest(). The validator / key broker wraps the data key (DEK) to that public key:
  ephemeral X25519 -> ECDH -> HKDF-SHA256(info = DOMAIN || session_digest || key_id) -> AES-256-GCM(DEK, aad=same)
Only the private key inside the attested CVM can unwrap it. The wrapped blob can travel over any untrusted relay
(miner host, our gateway, logs) without exposing the DEK. Leases: the blob carries an expiry; the enclave must drop
the DEK at expiry and re-attest to get a new one (enforced by the attested image, revocable by stopping renewals).
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DOMAIN = b"SN92-EU-TEE-KEYRELEASE-v1"


@dataclass
class WrappedKey:
    key_id: str
    expires_at: int
    epk: bytes
    nonce: bytes
    ciphertext: bytes

    def to_dict(self) -> dict:
        return {"key_id": self.key_id, "expires_at": self.expires_at, "epk": self.epk.hex(), "nonce": self.nonce.hex(), "ct": self.ciphertext.hex()}

    @classmethod
    def from_dict(cls, d: dict) -> "WrappedKey":
        return cls(d["key_id"], int(d["expires_at"]), bytes.fromhex(d["epk"]), bytes.fromhex(d["nonce"]), bytes.fromhex(d["ct"]))


def _aad(session: bytes, key_id: str, expires_at: int) -> bytes:
    return DOMAIN + session + key_id.encode() + expires_at.to_bytes(8, "big")


def _kek(shared: bytes, aad: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=aad).derive(shared)


class KeyReleaseRefused(Exception):
    pass


def wrap_for_enclave(dek: bytes, enclave_pubkey: bytes, session: bytes, key_id: str, ttl_s: int, verdict_ok: bool, now: int | None = None) -> WrappedKey:
    if not verdict_ok:
        raise KeyReleaseRefused("attestation verdict not OK; data key is never released")
    if len(dek) != 32:
        raise ValueError("DEK must be 32 bytes")
    now = int(time.time()) if now is None else now
    exp = now + int(ttl_s)
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(enclave_pubkey))
    aad = _aad(session, key_id, exp)
    nonce = os.urandom(12)
    ct = AESGCM(_kek(shared, aad)).encrypt(nonce, dek, aad)
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return WrappedKey(key_id, exp, epk, nonce, ct)


def unwrap_in_enclave(blob: WrappedKey, enclave_priv: X25519PrivateKey, session: bytes, now: int | None = None) -> bytes:
    """Reference for the guest side (runs inside the CVM)."""
    now = int(time.time()) if now is None else now
    if now > blob.expires_at:
        raise KeyReleaseRefused("lease expired; re-attest")
    shared = enclave_priv.exchange(X25519PublicKey.from_public_bytes(blob.epk))
    aad = _aad(session, blob.key_id, blob.expires_at)
    return AESGCM(_kek(shared, aad)).decrypt(blob.nonce, blob.ciphertext, aad)
