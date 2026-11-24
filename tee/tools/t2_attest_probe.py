"""One /attest call to an SN92 agent (e.g. a T2 RELEASE boot without SSH).

Prints only the bundle keys, the per-boot WireGuard public key and the SNP report MEASUREMENT/policy; never evidence
blobs. The output is UNVERIFIED: use it to learn the fresh wg_pubkey to pin, then run the real verifier, which checks
that the bound evidence carries that key. Usage: python -I tools/t2_attest_probe.py <host> <miner hotkey>
"""
import base64, os, secrets, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from verifier.service import Network  # noqa: E402


def snp_reports(o, path=''):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from snp_reports(v, path + '/' + k)
    elif isinstance(o, str) and len(o) > 1000:
        for dec in (lambda s: base64.b64decode(s, validate=True), bytes.fromhex):
            try:
                raw = dec(o)
            except Exception:
                continue
            if len(raw) >= 1184 and int.from_bytes(raw[0:4], 'little') in (2, 3, 5):
                yield path, raw
            break


def main(host, hotkey):
    b, _ = Network(call_timeout=60).challenge({'attest_url': f'https://{host}:8443', 'hotkey': hotkey},
                                              secrets.token_bytes(32))
    print('keys:', sorted(b))
    print('binding:', b.get('binding'), 'wg_pubkey:', b.get('wg_pubkey'), 'workload:', b.get('workload'))
    for path, raw in snp_reports(b):
        print('snp report at', path, 'version', int.from_bytes(raw[0:4], 'little'), 'MEASUREMENT',
              raw[0x90:0xC0].hex(), 'policy', hex(int.from_bytes(raw[8:16], 'little')))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
