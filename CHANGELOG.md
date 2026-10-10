# Changelog

## 0.1.5

Trust the `reaxlabs` GitHub organisation as the source and image owner. Update the bundled source and image references for the repository move..

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
