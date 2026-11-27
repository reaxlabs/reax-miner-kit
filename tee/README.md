# TEE node bundle

Operator guide: [../docs/TEE-OPERATOR.md](../docs/TEE-OPERATOR.md).

| Path | What it is |
| --- | --- |
| `guest/` | node installer (`setup.sh`), GPU check, WireGuard key, serving and egress units, runbook |
| `agent/` | in-VM attestation agent (TLS on :8443) |
| `teeverify/` | the evidence checks our verifier runs (used by the agent for binding; read it to see what we check) |
| `policy/` | T1 stock-image policy and historical T2 test policy |
| `image/` | T2 measured-image build (dm-verity root, initramfs) |
| `tools/` | SNP golden-measurement and T2 probe helpers |
| `GOLDEN-T2-2026-11-24.json` | historical T2 test image measurement and artifact hashes |
