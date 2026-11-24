"""HTTPS challenge service. Run only inside the target confidential VM."""
import base64
import datetime as dt
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, x25519
from cryptography.x509.oid import NameOID
from teeverify import binding, nvidia
from . import evidence

LOG = logging.getLogger("sn92-attest-agent")

def log(msg, level="info"):
    LOG.info(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "level": level, "app": "sn92-attest-agent", "msg": msg}, separators=(",", ":")))

def workload_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if st.st_uid != 0 or not stat.S_ISREG(st.st_mode) or st.st_mode & 0o022 or st.st_size > 4096:
            raise ValueError("workload config must be root-owned and not group/world writable")
        with os.fdopen(fd, "rb") as f:
            fd = -1
            workload = json.load(f)
        binding.workload_digest(workload)
        return workload
    finally:
        if fd != -1:
            os.close(fd)

def miner_hotkey():
    hotkey = os.environ.get('SN92_MINER_HOTKEY', '')
    if not re.fullmatch(r'[1-9A-HJ-NP-Za-km-z]{47,48}', hotkey):
        raise ValueError('SN92_MINER_HOTKEY must have SS58 shape')
    return hotkey

def validator_allow():
    value = os.environ.get('SN92_VALIDATOR_ALLOW')
    if value is None:
        return None
    if not value.strip():
        raise ValueError('SN92_VALIDATOR_ALLOW must contain CIDRs')
    return tuple(ipaddress.ip_network(part.strip(), strict=False) for part in value.split(','))

def wg_key():
    raw = os.environ.get("SN92_WG_PUBLIC_KEY")
    if raw is None:
        iface = os.environ.get("SN92_WG_INTERFACE", "wg0")
        if not re.fullmatch(r"[A-Za-z0-9_.=-]{1,15}", iface):
            raise ValueError("invalid interface")
        raw = subprocess.check_output(["wg", "show", iface, "public-key"], text=True, timeout=5).strip()
    result = base64.b64decode(raw, validate=True)
    if len(result) not in (0, 32):
        raise ValueError("invalid WireGuard public key")
    return result

class Agent:
    def __init__(self, tls_hash, workload, wg, hotkey, tsm_root=Path('/sys/kernel/config/tsm/report'), gpu_collector=None, cpu_collector=None):
        self._private = x25519.X25519PrivateKey.generate()
        self.pub = self._private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.tls_hash, self.workload, self.wg, self.hotkey = tls_hash, workload, wg, hotkey
        if not isinstance(hotkey, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{47,48}", hotkey):
            raise ValueError("invalid miner hotkey")
        binding.workload_digest(workload)
        self.root, self.gpu_collector = Path(tsm_root), gpu_collector or evidence.gpu_evidence
        self.cpu_collector = cpu_collector
        self.lock, self.recent = threading.Lock(), []

    def admit(self, source):
        with self.lock:
            now = time.monotonic()
            self.recent = [(t, ip) for t, ip in self.recent if now - t < 10]
            if len(self.recent) >= 6 or any(ip == source for _, ip in self.recent):
                return False
            self.recent.append((now, source))
            return True

    def bundle(self, nonce_hex, hotkey):
        if not isinstance(nonce_hex, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", nonce_hex):
            raise ValueError("invalid nonce")
        if not isinstance(hotkey, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{47,48}", hotkey):
            raise ValueError("invalid hotkey")
        if hotkey != self.hotkey:
            raise ValueError("hotkey mismatch")
        digest = binding.session_digest_v2(bytes.fromhex(nonce_hex), self.pub, self.tls_hash, self.wg, hotkey, binding.workload_digest(self.workload))
        gpus = self.gpu_collector(digest.hex())
        if not isinstance(gpus, list) or not gpus:
            raise ValueError("missing GPU evidence")
        clean = []
        for gpu in gpus:
            item = {k: gpu[k] for k in ("evidence", "certificate", "arch") if k in gpu}
            if not base64.b64decode(item["evidence"], validate=True) or not base64.b64decode(item["certificate"], validate=True):
                raise ValueError("empty GPU evidence")
            clean.append(item)
        ids = [nvidia.device_identity(base64.b64decode(g["certificate"], validate=True)) for g in clean]
        rd = binding.expected_report_data(digest, ids)
        if self.cpu_collector:
            cpu_raw = self.cpu_collector(rd, self.root)
        elif self.root.is_dir():
            cpu_raw = evidence.tsm_report(rd, self.root)
        else:
            cpu_raw = evidence.snp_ext_report(rd)
        out = cpu_raw['outblob']
        if not out:
            raise ValueError("empty CPU evidence")
        if cpu_raw['provider'] == 'tdx_guest':
            cpu = {"type": "tdx", "quote": base64.b64encode(out).decode()}
        elif cpu_raw['provider'] == 'sev_guest':
            certs = evidence.parse_snp_certs(cpu_raw['auxblob']) if cpu_raw['auxblob'] else {}
            leaf = certs.get('vlek') or certs.get('vcek')
            cpu = {"type": "snp", "report": base64.b64encode(out).decode()}
            if leaf:
                cpu['vcek'] = base64.b64encode(leaf).decode()
            if certs.get('ask') and certs.get('ark'):
                cpu['cert_chain'] = ''.join(x509.load_der_x509_certificate(certs[k]).public_bytes(serialization.Encoding.PEM).decode() for k in ('ask', 'ark'))
        else:
            raise ValueError("unknown CPU provider")
        return {"schema":"sn92-eu-tee-evidence/2", "binding":"v2", "miner_hotkey":hotkey,
                "validator_nonce":nonce_hex, "enclave_pubkey":self.pub.hex(), "tls_spki_sha256":self.tls_hash.hex(),
                "wg_pubkey":base64.b64encode(self.wg).decode(), "workload":self.workload,
                "agent_version":self.workload['agent_version'], "cpu":cpu, "gpu":{"evidence_list":clean}}

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def reply(self, status, obj):
        body = json.dumps(obj, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
    def do_GET(self):
        self.reply(200, {'ok':True}) if self.path == '/healthz' else self.reply(404, {'error':'not found'})
    def do_POST(self):
        if self.path != '/attest':
            return self.reply(404, {'error':'not found'})
        try:
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                return self.reply(400, {'error':'invalid request'})
            size_s = self.headers['Content-Length']
            if not re.fullmatch(r'[0-9]{1,8}', size_s):
                return self.reply(400, {'error':'invalid request'})
            size = int(size_s)
            if size > 4096:
                return self.reply(413, {'error':'request too large'})
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict) or set(request) != {'nonce', 'hotkey'}:
                raise ValueError('invalid request')
            if request['hotkey'] != self.server.agent.hotkey:
                return self.reply(403, {'error':'hotkey'})
            if not self.server.agent.admit(self.client_address[0]):
                return self.reply(429, {'error':'rate limited'})
            result = self.server.agent.bundle(request['nonce'], request['hotkey'])
            log('attestation completed')
            self.reply(200, result)
        except evidence.SDKUnavailable:
            log('NVIDIA attestation SDK unavailable', 'error')
            self.reply(503, {'error':'NVIDIA attestation SDK unavailable'})
        except Exception:
            log('attestation failed', 'error')
            self.reply(400, {'error':'attestation failed'})

class AttestationServer(ThreadingHTTPServer):
    daemon_threads = True

    def process_request_thread(self, request, client_address):
        connection = request
        timer = None
        try:
            connection = self.context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            connection.settimeout(10)
            connection.do_handshake()
            connection.settimeout(30)
            # A socket timeout alone resets on each read; close at the absolute deadline.
            def expire():
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(30, expire)
            timer.daemon = True
            timer.start()
            if self.allowed is not None and not any(ipaddress.ip_address(client_address[0]) in net for net in self.allowed):
                body = b'{"error":"forbidden"}'
                connection.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            else:
                self.finish_request(connection, client_address)
        except (OSError, ssl.SSLError):
            pass
        except Exception:
            self.handle_error(connection, client_address)
        finally:
            if timer is not None:
                timer.cancel()
            self.shutdown_request(connection)

    def handle_error(self, request, client_address):
        log('request handling failed', 'error')


def tls_context(tmpfs):
    # SSLContext needs file paths; reject non-tmpfs directories before placing the key there.
    resolved = os.path.realpath(tmpfs)
    mounts = []
    for line in Path('/proc/mounts').read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and (resolved == parts[1] or resolved.startswith(parts[1].rstrip('/') + '/')):
            mounts.append((len(parts[1]), parts[2]))
    if not mounts or max(mounts)[1] != 'tmpfs':
        raise ValueError('SN92_TLS_TMPDIR must be on tmpfs')
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'sn92-attest-agent')])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-dt.timedelta(minutes=1))
            .not_valid_after(now+dt.timedelta(days=1)).sign(key, hashes.SHA256()))
    spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory(dir=resolved) as tmp:
        kp, cp = Path(tmp)/'key', Path(tmp)/'cert'
        kp.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        os.chmod(kp, 0o600)
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        context.load_cert_chain(str(cp), str(kp))
    return context, hashlib.sha256(spki).digest()

def main():
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    hotkey = miner_hotkey()
    allowed = validator_allow()
    context, tls_hash = tls_context(os.environ.get('SN92_TLS_TMPDIR', '/run/sn92/tls'))
    agent = Agent(tls_hash, workload_file(os.environ.get('SN92_WORKLOAD_CONFIG', '/etc/sn92/workload.json')), wg_key(), hotkey)
    host = os.environ.get('SN92_LISTEN_HOST', '0.0.0.0')
    port = int(os.environ.get('SN92_LISTEN_PORT', '8443'))
    wg_host = os.environ.get('SN92_WG_LISTEN_HOST', '')
    # A wildcard bind already accepts traffic to the WireGuard address.
    hosts = [host] + ([wg_host] if wg_host and host not in ('0.0.0.0', '::', wg_host) else [])
    servers = []
    running = []
    try:
        for address in hosts:
            server = AttestationServer((address, port), Handler)
            servers.append(server)
            server.agent = agent
            server.context, server.allowed = context, allowed
        for server in servers[1:]:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            running.append(server)
        log('service started')
        servers[0].serve_forever()
    finally:
        for server in running:
            server.shutdown()
        for server in servers:
            server.server_close()

if __name__ == '__main__':
    main()
