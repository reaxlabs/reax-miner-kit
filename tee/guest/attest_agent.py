"""SN92 EU-tier guest attestation agent (runs INSIDE the confidential VM; prototype, untested on hardware).

On start: generates the X25519 enclave key in memory (never written to disk) and reads the TLS server certificate
created at boot. On POST /attest {"nonce": hex32, "hotkey": ss58} it returns an evidence bundle
(schema sn92-eu-tee-evidence/1, see teeverify/bundle.py):
  * GPU evidence first (nonce D), then
  * CPU evidence via Linux configfs-tsm (/sys/kernel/config/tsm/report), works for TDX ("tdx_guest") and SNP
    ("sev_guest"); report_data = D || G (G = digest of the local GPU identities). For SNP the host may supply the VCEK/VLEK in auxblob.
  * GPU evidence via NVIDIA's attestation SDK (nv-attestation-sdk) with nonce = session digest. The validator, not
    the agent, sends it to NRAS.
The agent never sees validator secrets and never logs request bodies.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey  # noqa: E402

from teeverify import binding, nvidia  # noqa: E402

TSM_ROOT = Path(os.environ.get("SN92_TSM_ROOT", "/sys/kernel/config/tsm/report"))


class TsmError(Exception):
    pass


def tsm_report(report_data: bytes, root: Path = TSM_ROOT) -> dict:
    """Get a TDX quote or SNP report through configfs-tsm. Returns {"provider", "outblob", "auxblob"}."""
    if len(report_data) != 64:
        raise TsmError("report_data must be 64 bytes")
    entry = root / f"sn92-{uuid.uuid4().hex[:12]}"
    entry.mkdir()
    try:
        provider = (entry / "provider").read_text().strip()
        if provider == "sev_guest" and (entry / "privlevel").exists():
            (entry / "privlevel").write_text("0")
        # read the generation AFTER every attribute write except inblob: each write bumps it (live probe 24 Nov)
        gen0 = int((entry / "generation").read_text().strip() or 0)
        (entry / "inblob").write_bytes(report_data)
        out = (entry / "outblob").read_bytes()
        aux = (entry / "auxblob").read_bytes() if (entry / "auxblob").exists() else b""
        gen1 = int((entry / "generation").read_text().strip() or 0)
        if gen1 != gen0 + 1:  # someone else wrote inblob concurrently: the report may not carry our data
            raise TsmError(f"configfs-tsm generation race ({gen0}->{gen1})")
        return {"provider": provider, "outblob": out, "auxblob": aux}
    finally:
        try:
            entry.rmdir()
        except OSError:
            pass


SNP_CERT_GUIDS = {  # GUID table entries in the SNP extended-report certificate blob
    "63da758d-e664-4564-adc5-f4b93be8accd": "vcek",
    "a8074bc2-a25a-483e-aae6-39c045a0b8a1": "vlek",
    "4ab7b379-bbac-4fe4-a02f-05aef327c782": "ask",
    "c0b406a4-a803-4952-9743-3fb6014cd0ae": "ark",
}


def parse_snp_certs(aux: bytes) -> dict[str, bytes]:
    """auxblob = table of {guid[16], offset u32, length u32}, terminated by an all-zero entry."""
    out: dict[str, bytes] = {}
    i = 0
    while i + 24 <= len(aux):
        guid = aux[i:i + 16]
        if guid == bytes(16):
            break
        off = int.from_bytes(aux[i + 16:i + 20], "little")
        ln = int.from_bytes(aux[i + 20:i + 24], "little")
        name = SNP_CERT_GUIDS.get(str(uuid.UUID(bytes_le=guid)))
        if name and off + ln <= len(aux):
            out[name] = aux[off:off + ln]
        i += 24
    return out


def gpu_evidence(nonce_hex: str) -> list[dict]:
    """Collect per-GPU SPDM evidence with NVIDIA's SDK (inside the CVM only)."""
    from nv_attestation_sdk import attestation  # type: ignore

    client = attestation.Attestation()
    client.set_nonce(nonce_hex)
    client.add_verifier(attestation.Devices.GPU, attestation.Environment.REMOTE, "", "")
    return client.get_evidence()  # SDK 2.7.3 (live probe 24 Nov): no ppcie_mode kwarg


class Agent:
    def __init__(self, tls_cert_path: str, tsm_root: Path = TSM_ROOT, gpu_collector=gpu_evidence):
        self._priv = X25519PrivateKey.generate()  # in TD/SNP memory only
        self.pub = self._priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        cert = x509.load_pem_x509_certificate(Path(tls_cert_path).read_bytes())
        spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.tls_spki_sha256 = hashlib.sha256(spki).digest()
        self.tsm_root = tsm_root
        self.gpu_collector = gpu_collector

    def bundle(self, nonce_hex: str, hotkey: str) -> dict:
        nonce = bytes.fromhex(nonce_hex)
        digest = binding.session_digest(nonce, self.pub, self.tls_spki_sha256, hotkey)
        # GPUs FIRST (nonce D), then the CPU quote commits to the identities of exactly these local GPUs
        gpus = self.gpu_collector(digest.hex())
        ids = [nvidia.device_identity(base64.b64decode(e["certificate"])) for e in gpus]
        cpu_raw = tsm_report(binding.expected_report_data(digest, ids), self.tsm_root)
        if cpu_raw["provider"] == "tdx_guest":
            cpu = {"type": "tdx", "quote": base64.b64encode(cpu_raw["outblob"]).decode()}
        elif cpu_raw["provider"] == "sev_guest":
            certs = parse_snp_certs(cpu_raw["auxblob"])
            leaf = certs.get("vlek") or certs.get("vcek")
            cpu = {"type": "snp", "report": base64.b64encode(cpu_raw["outblob"]).decode()}
            if leaf:  # otherwise the validator fetches the VCEK from AMD KDS itself
                cpu["vcek"] = base64.b64encode(leaf).decode()
        else:
            raise TsmError(f"unknown TSM provider {cpu_raw['provider']}")
        return {
            "schema": "sn92-eu-tee-evidence/1",
            "miner_hotkey": hotkey,
            "validator_nonce": nonce_hex,
            "enclave_pubkey": self.pub.hex(),
            "tls_spki_sha256": self.tls_spki_sha256.hex(),
            "cpu": cpu,
            "gpu": {"evidence_list": [{k: e[k] for k in ("evidence", "certificate", "arch") if k in e} for e in gpus]},
        }


def serve(agent: Agent, host: str = "127.0.0.1", port: int = 8701) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no request logging at all
            pass

        def do_POST(self):
            if self.path != "/attest":
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if n > 4096:
                    raise ValueError("body too large")
                req = json.loads(self.rfile.read(n))
                body = json.dumps(agent.bundle(req["nonce"], req["hotkey"])).encode()
                self.send_response(200)
            except Exception as e:  # no detail beyond the error class
                body = json.dumps({"error": type(e).__name__}).encode()
                self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    serve(Agent(os.environ.get("SN92_TLS_CERT", "/run/sn92/tls/server.crt")))
