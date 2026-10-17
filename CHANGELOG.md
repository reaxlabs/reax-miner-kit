# Changelog

## Unreleased

- Several runtime backends behind one hotkey (needs miner release 1.2.0rc8 or later; the kit refuses it on older descriptors): `install --gpus 0,1 [--backend-bind IP] [--backend-token-files F,F]` renders one sidecar per GPU (`device_ids`, published as `BIND:8101+10*i`, bridge network so several s1-pro vLLMs do not clash on :8000), writes `REAX_S1_FAST_RUNTIME_URLS` / `REAX_S1_PRO_RUNTIME_URLS` (`URL` or `URL|TOKEN_FILE`) and mounts optional per-backend token files. Only for s1-fast and s1-pro, only loopback/private bind addresses. `selfcheck` starts and checks every sidecar and prints one verdict per backend. `install --no-fanout` returns to one sidecar; without `--gpus` the rendered files are unchanged. New `docs/MULTI-GPU.md`.

- Release descriptor 1.2.0rc3 (reax-subnet commit 6f12faa): capacity scoring v2. Pay inside a pool follows verified throughput (linear, up to a cap far above one machine); the miner image raises the per-caller limits to 16 concurrent / 240 per minute so a probe can measure full speed. `reaxctl update check` / `update apply` moves an installed miner to the new images.

- Merged the tiered-pools kit branch into the 1.2.0rc1 descriptor: `releases/current.json` now enables four pools (s1-fast, s1-pro, s1-lite, s1-cpu). s1-lite (Plumb-4B GGUF, 8 GB NVIDIA GPU or Jetson Orin, manifest `8c7297e14e5e89bf`) and s1-cpu (Gutsy 0.8B GGUF, no GPU, manifest `f2c9981f0766f2a6`) are `enabled_at_launch` and no longer `dev_only`; the `dev_only` mechanism stays for future pools. Images `reax-s1-lite-runtime`, `reax-s1-lite-runtime-jetson`, `reax-s1-cpu-runtime` carry `sha256:TBD-*` placeholders (now six in total).
- `doctor` checks the selected pool (default s1-fast) for host-wide blockers; per-pool hardware eligibility (`hardware_ok`, `tier`, `dev_only`) and `recommended_pool` (s1-fast, else s1-lite, else s1-cpu) are reported for all pools. A host that only fits s1-cpu is not reported ready while s1-fast is selected. Jetson detection (`/etc/nv_tegra_release`) and aarch64 CPU hosts are supported.
- `install --pool s1-lite|s1-cpu` renders the llama.cpp sidecar (ports 8103/8104, `runtime: nvidia` on Jetson, no GPU reservation for s1-cpu), downloads only the listed GGUF file, and sets `REAX_FEATURE_TIERED_POOLS=1`. Sidecar ports 8101-8104 and 8000 are never miner ports.
- Release 1.2.0rc1 descriptor (`releases/current.json`): s1-pro pool enabled (Surogate Rune 26B-A4B v3, manifest `5b11440bf57b5434`, commitment `reax-s1/1:s1-pro:5b11440bf57b5434`, 80 GB+ GPU, deterministic runtime, gated HF weights from the miner's own account); s1-fast unchanged (`ff6e01b983d48024`). Image digests are `sha256:TBD-*` placeholders until the images are published; validation refuses placeholders once images are public or the release is live. The 1.1.0-rc2 descriptor is kept as `releases/1.1.0rc2.json`.
- `doctor` for s1-pro: unmeasured-GPU warning (A100/H200/B200), confidential-compute detection with the UVA-fix hint, Hugging Face gate check (`--check-hf`; the token is never printed). Only the selected pool (default s1-fast) can raise capacity blockers, so an s1-fast host is not blocked by s1-pro.
- `install --pool s1-pro` renders `RUNE_DETERMINISTIC=1`; `--cc-fix` adds `REAX_VLLM_DISABLE_UVA=1` and a read-only mount of `runtimes/vllm-ccfix` from a reviewed subnet checkout.
- New `service/docker-compose.s1-pro.yml`, `docker-compose.s1-pro-ccfix.yml`, `miner.env.example` and `reax-s1-pro.service`; README s1-pro quickstart and competitions links; `docs/S1-PRO.md`. "Disabled at launch" wording removed.
- Fresh-machine fixes: README apt line includes `python3-venv`; `doctor` reports missing venv support and Pythons without tarfile extraction filters (needed by the rehearsal) before anything is installed; a pip-less venv from an earlier failed attempt is recreated instead of failing every retry.
- Failures of git, pip and the weights download show their reason (`detail`), with tokens and URL credentials masked.
- Text output shows plan steps, failed `verify` checks and localnet processes; `localnet up` suggests the next commands; `net-policy` lists the ghcr.io and PyPI downloads of the first rehearsal.
- Release descriptor pins the current subnet source commit.
- New `reaxctl selfcheck` for testnet/mainnet: runs the subnet release's reference self-check against the runtime sidecar before registration; `plan` lists it after `install`, `verify --selfcheck` adds it as a checklist item.

## 0.1.5

Trust the `reaxlabs` GitHub organisation as the source and image owner. Update the bundled source and image references for the repository move. The default descriptor URL is the `main` branch of this repository.

## 0.1.3

New `reaxctl uninstall` (stops miner/service/timer, removes the state directory, keeps model weights unless `--purge-weights`, never touches wallets, needs `--yes`); a fetched descriptor may only name code and images owned by trusted GitHub owners and a public source needs an exact commit; `doctor` reports `rehearsal_possible` only when the source is available (`rehearsal_capable` separately); pool display names are the plain model names; vulnerabilities are reported privately through GitHub.

## 0.1.2

Found by running the docker path on a real RTX 4090 host (Ubuntu 24.04, Docker 27, NVIDIA toolkit): `install --mode mainnet` built both images from the pinned source, downloaded the pinned weights, rendered the hardened compose file; the runtime sidecar became healthy as a non-root user with all capabilities dropped and was unreachable from the host's own address.

- doctor: no longer crashes when `docker info` reports `"Runtimes": null` (unreachable daemon); probe timeouts are realistic (a cold `nvidia-smi` or Docker CLI takes seconds); an unverifiable clock (containers have no timedatectl/chrony) is a warning, not a blocker.
- install: weights download uses its own HF cache directory (an inherited, unwritable `HF_HOME` made hf_xet fail); the failure carries a short detail line; source origin is compared as the raw configured URL (git `insteadOf` rewrites no longer cause a false mismatch).
- Unexpected errors print the exception type and location on stderr (never the message); `REAX_DEBUG=1` prints a traceback.

## 0.1.1

Harden descriptor validation and refresh (one-hour cache, offline mode, bounded same-origin
HTTPS fetch and fallback warnings). Close the install manifest gate bypass. Isolate Python
helpers and restrict child environments. Protect hotkey creation, state directories and atomic
file writes; honour wallet overrides. Harden systemd and compose, redact and label untrusted
logs/health, validate external-IP probes, contain single-member tar extraction and guard PID
reuse. Localnet registration now explains its throw-away registration flow. Document policy
flag limitations, rollback locking/pruning and rehearsal RPC restrictions. Add offline regressions.

## 0.1.0

First release of the REAX miner kit. Native localnet rehearsal works with access to the pinned subnet source. Real-network operations stay blocked until the release descriptor opens them. Includes read-only diagnosis, setup plans, hotkey-only wallet tooling, pinned installation, runtime verification, service management and confirmed manifest updates. Images and subnet source are used once the descriptor marks them public.
