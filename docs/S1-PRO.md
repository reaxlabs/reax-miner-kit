# s1-pro walk-through (release 1.2.0rc1)

s1-pro serves Surogate Rune 26B-A4B v3 (`surogate/rune-26b-a4b-GGUF@c6b360d47895bb77bdf3805a13ee5a5557ab1921`, bf16
safetensors, about 52 GB). Manifest `5b11440bf57b5434`, commitment `reax-s1/1:s1-pro:5b11440bf57b5434`. Hardware, share and
calibration details are in the subnet repository (`docs/hardware.md`, `docs/MINER.md`).

## Requirements

| Item | Value |
| --- | --- |
| GPU | one NVIDIA GPU, 80–96 GB class, driver R580+ |
| Measured | RTX PRO 6000 96 GB, H100 80 GB (bit-identical) |
| Not measured | H200, B200 (expected to work); A100 80 GB (at your own risk) |
| Disk | about 80 GB free (weights 52 GB plus the runtime image) |
| Runtime | `reax-s1-pro-runtime`, deterministic vLLM 0.30.0, `RUNE_DETERMINISTIC=1`; stock vLLM and FP8 fail |
| Hugging Face | gated repo, "auto" approval: accept it with your own account; REAX never mirrors the weights |

## With reaxctl

1. `./reaxctl doctor --pool s1-pro --check-hf` — fix every blocker. Warnings tell you about unmeasured cards, a missing HF token,
   a gate that is not accepted, and confidential-compute mode.
2. Accept the gate at https://huggingface.co/surogate/rune-26b-a4b-GGUF and run `hf auth login` (the venv provides `hf`). Never paste
   the token into `reaxctl` or an agent chat.
3. `./reaxctl install --pool s1-pro --mode mainnet --confirm-mainnet` after human approval. On confidential-compute GPUs
   (`nvidia-smi conf-compute -f` prints ON) add `--cc-fix` with `REAX_SOURCE_DIR` pointing to a reviewed subnet checkout.
4. `./reaxctl selfcheck` must pass before you register. Registration is a human step in your own wallet.
5. `./reaxctl start`, `./reaxctl verify`, `./reaxctl service install`.

Switching an installed miner from s1-fast to s1-pro changes the manifest and resets scores: it needs `--accept-manifest-change` and
human confirmation. A miner serves one pool per hotkey.

## Without reaxctl

[`service/docker-compose.s1-pro.yml`](../service/docker-compose.s1-pro.yml) (loopback-only sidecar on 8102, deterministic flag,
non-root, capabilities dropped), the optional [`docker-compose.s1-pro-ccfix.yml`](../service/docker-compose.s1-pro-ccfix.yml) override
and [`reax-s1-pro.service`](../service/reax-s1-pro.service). The setup commands are in the header comment of the compose file.

## Competitions

Engine competition and use-case tracks start 16 October 2026 12:00 UTC (50/50 split). No new mining mode is needed:
https://reax.co/competitions and `docs/COMPETITIONS.md` in https://github.com/reaxlabs/reax-subnet (branch `release/1.2-rc`).
