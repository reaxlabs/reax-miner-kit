"""SN92 EU-tier guest attestation agent (runs INSIDE the confidential VM; prototype).

On start: generates the X25519 enclave key in memory (never written to disk) and reads the TLS server certificate
created at boot. On POST /attest {"nonce": hex32, "hotkey": ss58} it returns an evidence bundle
(schema sn92-eu-tee-evidence/2, see INTERFACES.md):
  * GPU evidence first (nonce D), then
  * CPU evidence via Linux configfs-tsm (/sys/kernel/config/tsm/report), works for TDX ("tdx_guest") and SNP
    ("sev_guest"); report_data = D || G (G = digest of the local GPU identities). For SNP the host may supply the VCEK/VLEK in auxblob.
  * GPU evidence via nvattest, with the NVIDIA SDK as a missing-CLI fallback. The validator sends it to NRAS.
The agent never sees validator secrets and never logs request bodies.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import subprocess
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


class SDKUnavailable(Exception):
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
    entries = []
    for i in range(0, len(aux) - 23, 24):
        guid = aux[i:i + 16]
        off = int.from_bytes(aux[i + 16:i + 20], "little")
        ln = int.from_bytes(aux[i + 20:i + 24], "little")
        if guid == bytes(16):
            if off or ln:
                raise TsmError("invalid SNP certificate terminator")
            table_end = i + 24
            break
        if not ln or off + ln > len(aux):
            raise TsmError("invalid SNP certificate entry")
        entries.append((guid, off, ln))
    else:
        raise TsmError("unterminated SNP certificate table")
    for guid, off, ln in entries:
        if off < table_end:
            raise TsmError("overlapping SNP certificate table")
        name = SNP_CERT_GUIDS.get(str(uuid.UUID(bytes_le=guid)))
        if name:
            if name in out:
                raise TsmError("duplicate SNP certificate")
            out[name] = aux[off:off + ln]
    return out



def gpu_evidence(nonce_hex: str, nvattest_path: str | None = None) -> list[dict]:
    """Use nvattest; fall back to the SDK only if the executable is absent."""
    path = nvattest_path or os.environ.get("SN92_NVATTEST_PATH", "/usr/bin/nvattest")
    if not os.path.isabs(path):
        raise ValueError("nvattest path must be absolute")
    try:
        result = subprocess.run([path, "--format", "json", "collect-evidence", "--nonce", nonce_hex],
                                capture_output=True, check=True, timeout=120)
    except FileNotFoundError:
        try:
            from nv_attestation_sdk import attestation  # type: ignore
        except ImportError as e:
            raise SDKUnavailable("NVIDIA attestation SDK unavailable") from e
        client = attestation.Attestation()
        client.set_nonce(nonce_hex)
        client.add_verifier(attestation.Devices.GPU, attestation.Environment.REMOTE, "", "")
        return client.get_evidence()
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or type(payload.get("result_code")) is not int or payload["result_code"] != 0:
        raise ValueError("nvattest failed")
    entries = payload.get("evidences")
    if not isinstance(entries, list) or not entries:
        raise ValueError("missing GPU evidence")
    for item in entries:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k] for k in ("arch", "certificate", "evidence", "nonce")):
            raise ValueError("invalid GPU evidence")
        if item["nonce"] != nonce_hex:
            raise ValueError("GPU nonce mismatch")
    return entries



def snp_ext_report(report_data: bytes, device="/dev/sev-guest") -> dict:
    """SNP_GET_EXT_REPORT Linux UAPI, untested on hardware.

    snp_report_req: user_data[64], vmpl u32, reserved[28];
    snp_ext_report_req: report request, certs_address u64, certs_len u32 + padding;
    snp_guest_request_ioctl: msg_version u8, padding[7], req_data u64,
    resp_data u64, fw_err u64. Response: status u32, report_size u32,
    reserved[24], report[1184]. certs_address is a userspace pointer.
    """
    import ctypes
    import fcntl
    import struct
    if len(report_data) != 64:
        raise TsmError("invalid report data")
    certs = ctypes.create_string_buffer(16384)
    request = ctypes.create_string_buffer(120)
    request.raw = report_data + bytes(32) + struct.pack("=QI", ctypes.addressof(certs), len(certs)) + bytes(4)
    response = ctypes.create_string_buffer(4000)
    ioctl_request = bytearray(struct.pack("=B7xQQQ", 1, ctypes.addressof(request), ctypes.addressof(response), 0))
    with open(device, "rb", buffering=0) as f:
        fcntl.ioctl(f.fileno(), 0xC0205302, ioctl_request, True)
    if struct.unpack_from("=Q", ioctl_request, 24)[0]:
        raise TsmError("SNP firmware error")
    status, size = struct.unpack_from("=II", response.raw)
    cert_len = struct.unpack_from("=I", request.raw, 104)[0]
    if status or size != 1184 or cert_len > len(certs):
        raise TsmError("invalid SNP extended report")
    return {"provider": "sev_guest", "outblob": response.raw[32:32+size], "auxblob": certs.raw[:cert_len]}
