"""CLI (validator side):
  python -I -m teeverify verify --bundle b.json --policy p.json --nonce <hex> --hotkey <ss58> --tls-spki <hex>
                               [--jwks j.json --nras n.json] [--collateral c.json] [--rtt probes.json] [--now ts]
Every option except --bundle is a validator-side fact (issued nonce, challenged hotkey, SPKI seen on the validator's own
TLS connection, NRAS response the validator fetched, Intel collateral it fetched, RTT probes its probe service measured).
Exit 0 = verdict OK (key release allowed), 1 = rejected, 2 = usage error."""
from __future__ import annotations

import argparse
import json
import sys

from .bundle import verify_bundle


def _load(path: str | None):
    if not path:
        return None
    with open(path) as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="teeverify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--bundle", required=True)
    v.add_argument("--policy", required=True)
    v.add_argument("--nonce", required=True, help="nonce this validator issued")
    v.add_argument("--hotkey", required=True, help="hotkey this validator challenged")
    v.add_argument("--tls-spki", required=True, help="SHA-256 hex of the SPKI on the validator's own TLS connection to the node")
    v.add_argument("--jwks", help="NRAS JWKS JSON (validator-fetched)")
    v.add_argument("--nras", help="NRAS response JSON (validator posted the bundle's raw evidence itself)")
    v.add_argument("--collateral", help="Intel collateral JSON (validator-fetched; bundle collateral is ignored)")
    v.add_argument("--rtt", help="RTT probe results JSON list (validator's probe service)")
    v.add_argument("--now", type=int)
    a = ap.parse_args(argv)
    verdict = verify_bundle(
        _load(a.bundle), _load(a.policy),
        expected_nonce_hex=a.nonce, expected_hotkey=a.hotkey, expected_tls_spki_hex=a.tls_spki, now=a.now,
        jwks=_load(a.jwks), nras_response=_load(a.nras), tdx_collateral=_load(a.collateral), rtt_probes=_load(a.rtt),
    )
    json.dump(verdict.to_dict(), sys.stdout, indent=1)
    print()
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    sys.exit(main())
