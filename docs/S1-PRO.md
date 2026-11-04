# s1-pro mammoth-1 (kit 0.1.9, release 1.2.0rc11.post8.1-kit1)

Validators accept only manifest `3f3558e583b0a1d6` since 30 October 2026 09:00 UTC.
The commitment is `reax-s1/1:s1-pro:3f3558e583b0a1d6`.
Weights: public MIT `baselabs/mammoth-1@61cec44c281c693992e3d6ebeb44dd3402dfa186`, about 55 GB.
No Hugging Face gate or login is required.

| Item | Requirement |
| --- | --- |
| GPU | One NVIDIA GPU with at least 48 GB, R580+ driver |
| Measured | RTX PRO 6000 Blackwell Server Edition (96 GB), peak 35.2 GB |
| Other GPUs | No pinned kernel configs; must pass reax-selfcheck, at your own risk |
| Free disk | 100 GB: about 55 GB weights + 30 GB runtime image + headroom |
| Runtime | Digest-pinned reax-engine-ref-mammoth; no command-line arguments |

Run `./reaxctl doctor --pool s1-pro`, resolve blockers, then
`./reaxctl install --pool s1-pro --mode mainnet --confirm-mainnet` after approval.
An installed Rune miner changes manifest and resets its score: review and explicitly accept with
`./reaxctl update apply --accept-manifest-change`. Never put that flag in a timer.
Weights use a revision-specific directory so Rune files cannot contaminate mammoth; keep the old
weights until the upgrade is verified. Failed updates restore configuration and the weights marker.

The runtime uses `REAX_MODEL=mammoth-1`; the miner and selfcheck use `REAX_MODEL=s1-pro`.
It listens on 127.0.0.1:8102. Do not publish the sidecar port. FLA strict mode and the bundled,
checksum-verified Server Edition configs are mandatory. The root filesystem is read-only;
/tmp is an executable 16 GB tmpfs, shared memory is 2 GB and the PID limit is 4096.
Both HF offline variables are set. The Rune `--cc-fix` is unverified for mammoth and is refused.

First start can take about eight minutes to compile kernels. Run `./reaxctl selfcheck --json`
and require PASS before registration or serving. Kit 0.1.9 selfcheck refuses an installed manifest that differs from the current pool. The kit render has not been run on a GPU.
Registration remains a human step in your own wallet.

Kit 0.1.8 cannot render the pinned config mount. The descriptor disables s1-pro for that controller;
it reports "This pool is not enabled." Upgrade the kit to 0.1.9 before installing or applying the
manifest change. Merely fetching the descriptor does not stop existing containers.
Other pool runtimes stay on post7. An explicit update replaces their shared miner image but keeps
their weights, manifest, runtime digest and existing per-model limits.

For manual setup see [the compose template](../service/docker-compose.s1-pro.yml) and
[the systemd unit](../service/reax-s1-pro.service). For several models, see [MULTIMODEL.md](MULTIMODEL.md).
The single-backend probe limits remain 64 / 728 / 16 / 128; the per-hotkey cap is 640 decisions/s.

The kit also ships a single Triton autotune seed for `mammoth-reference/1` at
`configs/mammoth-triton-seed/UBI36QQLJDLOXDYUOXUAFH7IJLNLP3IQAUE3ETMXLNEPEZIFLWYQ/chunk_fwd_kernel_o.autotune.json`.
Its SHA256 is `51b996aa4e577fa923c4d6142b79c613a9aba2e79506dff111277ba8e8b2a326` (816 bytes),
pinned in `reaxlib/triton_seed.py`. Install, render and kit-managed starts verify it;
`doctor --pool s1-pro --json` reports `data.triton_seed` with presence, actual SHA and validity.
A missing or changed seed blocks starting. Reinstall the kit to restore it. Existing installations
must rerun install to render the new mount and entrypoint; fetching a descriptor alone does not update compose.
The directory is mounted read-only at `/seedctl`; the entrypoint copies it into the fresh writable
`/tmp/triton` cache before `python -m engine.serve`. The image and manifest remain unchanged.
The seed selects BK/BV 64 with 4 warps for the measured key. This change has only offline validation;
fresh GPU starts using this rendered compose and a passing selfcheck remain required before adoption.

For manual Compose, export `REAX_TRITON_SEED=<absolute-kit-path>/configs/mammoth-triton-seed` and
run `python3 <absolute-kit-path>/reaxlib/triton_seed.py && docker compose -f docker-compose.s1-pro.yml up -d`
before each start. The manual systemd unit uses the same preflight; replace its new `@KIT@`
placeholder with that absolute kit path. Keep the full kit directory at that path while installed.
The controller checks do not intercept direct Docker commands or Docker's automatic restart policy.

There is no gVisor/runsc setting or render path in this kit. `runtime: nvidia` belongs to the Jetson
pool, not to gVisor. Validator engine isolation is managed outside the kit: the subnet release branch has
`src/reax_subnet/engrunner.py` (`RELEASE_RUNTIME="runsc"`, release preflight and `docker run --runtime`),
configured by `REAX_ENGINE_DOCKER_RUNTIME`. Its no-network / host-UDS policy is separate from this
loopback sidecar. This change does not select runsc for either the trusted sidecar or miner.
