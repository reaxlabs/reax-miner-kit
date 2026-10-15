# Runtime operations

Run `python3 reaxctl install --mode localnet --home /tmp/reax-rehearsal --json` first.
While the descriptor does not mark the source public, set `REAX_SOURCE_DIR` to a read-only subnet checkout. Once it does, the
release descriptor supplies the git source and pinned ref. The source is never edited.
The installed source path is retained so later commands need no environment override.

`reaxctl localnet up --home /tmp/reax-rehearsal --base-port 19951 --json` starts
three native local subtensor authorities, a hash runtime miner and a validator.
The RPC ports are base/base+1/base+2, peer ports base+5/base+6/base+7, and the
miner uses base+9. The chain refuses loopback miner addresses, so the miner binds
to an assigned private IPv4 selected from local interfaces (or set `--host`). Chain
RPC stays on loopback; the hash runtime is in-process, with no public sidecar. Choose an unused range. Readiness has bounded timeouts; failed
startup tears down owned processes. `localnet down` signals only tracked process
groups (Linux pid start times guard against recycled pids). Logs and pidfiles are
in `<home>/localnet/`. `localnet status` reports process liveness and miner health.
Rehearsal funding uses disposable public development identities in memory; only
hotkey and public-key files are written. No user coldkey is read or created.

The first rehearsal needs online access to the source's native fetch script's
pinned `ghcr.io/raofoundation/subtensor-localnet` image. Its layer hashes are
verified by that script. A contained extraction adapter rewrites absolute image
symlinks into relative links inside `<home>/localnet/node/rootfs`; path traversal
and links outside that root remain rejected. Offchain workers and node telemetry are disabled. Keep that directory for offline
restarts. Tool/dependency installation needs PyPI when dependencies are missing.
The CLI itself remains stdlib-only; the miner subprocess uses its isolated venv.

For Docker modes, the descriptor must mark the network live and supply its netuid.
Mainnet install also requires `--confirm-mainnet` through a human-approved flow.
Install checks disk and Docker daemon/compose, installs descriptor tool requirements,
creates a 0600 token, renders compose/env, obtains images and pinned local weight
snapshots. Images use digests when supplied. Private images require a source checkout
and local builds. Gated weights require the user's own HF account and `hf auth login`;
reaxctl never asks for a token. Retry install to resume. Localnet does not download
model weights or require Docker/GPU.

Both containers use host networking with the runtime bound to loopback by the pinned
runtime configuration. Only the selected hotkey and `coldkeypub.txt` are mounted;
the wallet directory and coldkey are never mounted. Keep miner port 8091 reachable
for real networks; never publish sidecar ports 8101/8102/8103/8104/8000. Run `wallet status`
and complete human registration before starting a real miner. Variable subnet
emissions can be zero.

`selfcheck --json` (testnet/mainnet) checks the runtime before registration: it starts
only the runtime sidecar if it is stopped, waits for it to be healthy (rerun while it
loads), then runs the release's `reax-selfcheck` in the installed miner image with the
runtime token and the miner's model/sidecar settings, no wallet mounts. Exit 0 pass,
20 fail, 10 when it cannot run. The check loads the GPU for about a minute, so `verify`
runs it only with `--selfcheck`. Run it before the miner starts serving: on a serving miner it shares the GPU with live validator requests (it warns).

`start`, `stop`, `restart` and `run` manage the installed mode. `run` keeps the
supervisor in the foreground. `status --json` returns processes/containers, health,
and log-derived registration/commitment/round evidence (unknown values stay null).
`logs --lines 100 --json` returns a bounded log tail with the runtime token redacted.
`verify --json` reports checks and exits 20 when health, pins, chain evidence,
validator rounds, or NTP checks fail. `verify --probe-ip` additionally checks
sidecar ports at the configured external IP; lack of a public IP fails this check.
For localnet all processes use the same host clock.

`service install --user --json` installs a systemd user unit. Enable lingering with
the returned exact command if requested. `--system` requires root; reaxctl returns
a sudo instruction rather than running interactive sudo. On macOS a LaunchAgent
supports the rehearsal. `service status` and `service uninstall` manage the unit.
`run` is the unit's foreground supervisor. Systemd templates quote absolute paths.

`update check` compares installed release/manifest with the descriptor. `update apply`
requires `--accept-manifest-change` for changed manifests, since scores reset to zero.
The descriptor must classify same-manifest updates as safe. Previous compose, env,
config and image references are retained in `<home>/rollback/<timestamp>/`; failed
updates restore them and attempt to restart the previous compose. Inspect status after
a rollback. `update timer install --user` installs a daily systemd timer with a random
one-hour delay; `update timer uninstall` removes it. Timer updates cannot accept a
manifest change automatically.

Tests: `python3 -m unittest discover -s tests -v`. Tests replace external managers
with PATH fakes; they never invoke real Docker, sudo, or GPU tools. Docker/GPU serving
is verified against fakes in the test suite, not on a real GPU host.

Kit 0.1.5: system units run as the non-root invoking UID with NoNewPrivileges/PrivateTmp.
Compose drops all capabilities and rotates logs; wallet mounts honour REAX_WALLET_DIR.
Install and update both require explicit acceptance for a stored manifest change. See
README's hardening section for offline refresh, locking/pruning guidance and RPC limitations.

## Small-GPU, Jetson and CPU pools (beta, development networks only)

Two further pools are described in the release descriptor with `dev_only: true` and
`enabled_at_launch: false`. They run only in `localnet` and `testnet`; `install` refuses
them on mainnet, and the miner needs `REAX_FEATURE_TIERED_POOLS=1` and
`REAX_ALLOW_DEV_MODELS=1` (install writes both into `miner.env` for these pools). The
runtime images are not published yet, so a real install needs a local build via
`REAX_SOURCE_DIR`.

| Pool | Model | Hardware | Sidecar port | Disk |
| --- | --- | --- | --- | --- |
| `s1-lite` | Plumb-4B v5 GGUF Q8_0 (llama.cpp CUDA) | NVIDIA GPU with 8 GB or more on x86_64, or a Jetson Orin (JetPack 6, L4T R36+, 8 GB unified memory) | 8103 | 8 GB |
| `s1-cpu` | Gutsy 0.8B v0.4 GGUF Q8_0 (llama.cpp CPU) | No GPU; Linux x86_64 (AVX2) or aarch64; 4+ cores, 4+ GB RAM | 8104 | 3 GB |

`reaxctl doctor --pool s1-lite` (or `--pool s1-cpu`) checks only what that pool needs:
the NVIDIA driver, container runtime and GPU are not required for `s1-cpu`. On a Jetson
the driver check is the L4T release from `/etc/nv_tegra_release`, GPU memory is system
RAM, and Docker must have the `nvidia` runtime registered. Without `--pool`, doctor
reports `hardware_ok` for each pool and a `recommended_pool` (`s1-fast`, else `s1-lite`,
else `s1-cpu`). Weights are fetched with `weights.files`, so only the single GGUF file
is downloaded. On a Jetson the compose file uses `runtime: nvidia` and the
`s1_lite_runtime_jetson` image; on x86 it reserves the NVIDIA device.
