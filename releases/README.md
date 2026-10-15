# Release descriptor

`current.json` uses schema `reax-release/1`. It is bundled with the kit and may be cached under `<REAX_HOME>/releases/current.json`. Doctor, plan, install, register, start and update refresh it by default, using a one-hour cache. Validation or network failure falls back to valid cached or bundled data. Use `--offline` or `REAX_OFFLINE=1` to suppress refresh.

| Field | Meaning |
| --- | --- |
| `schema` | Exact schema identifier |
| `release_id`, `minimum_release` | Pinned release and declared minimum compatible release (minimum is metadata; not currently an enforced upgrade gate) |
| `status.launch_status`, `status.live` | `launching_soon` or `registered_starting_soon` records pre-start status; `live:false` keeps real modes closed. `live:true` opens them after owner start and release readiness. |
| `status.netuid`, `status.testnet_netuid`, `status.network` | Mainnet/testnet subnet IDs (testnet stays null until assigned) and mainnet network name |
| `status.notes` | Human launch context |
| `images.miner`, `images.s1_fast_runtime`, `images.s1_pro_runtime` | Container repository names |
| `images.tag`, `images.digests`, `images.public` | Image tag, per-image SHA256 digest map (or null), public-access flag; digest references take precedence. Each field is a release gate. |
| `source.git`, `source.ref`, optional `source.commit`, `source.public` | Subnet repository, exact pinned tag, optional expected commit SHA for that tag, and independent public-availability gate |
| `pools[].id`, `model` | Safe pool identifier and descriptive model name |
| `pools[].weights` | HF `repo`, immutable `revision`, `gated`, `size_gb`; optional gate note |
| `pools[].manifest_id`, `commitment` | Approved scoring manifest and chain commitment |
| `pools[].gpu` | `min_vram_gb`, optional `max_vram_gb`, GPU `count`, `vendor`, measured hardware and optional notes |
| `pools[].sidecar_port`, `enabled_at_launch` | Private runtime port and launch eligibility |
| `pools[].runtime` | Optional: `deterministic`, `env` (sidecar environment), `cc_fix_env` (confidential-compute UVA fix) |
| `pools[].gpu.unmeasured` | Optional GPU name fragments with no REAX measurement; `doctor` warns |
| `requirements` | CPU cores, RAM GB, per-pool disk GB, driver/Python minimum, open ports and never-publish ports; OS/NTP notes |
| `tools` | Tool package constraints installed in the venv |
| `links` | Entry, guide, wallet, register, FAQ and project URLs |
| `update.channel`, `auto_safe`, `manifest_change_requires_confirm` | Release channel and update policy; manifest confirmation is enforced by the controller regardless of descriptor flags |

Optional descriptive notes do not grant execution authority. No descriptor flag authorizes a real payment. The CLI controller needs Python 3.8+; tool installation uses `requirements.python_min`.

## Release checklist

1. Verify assigned subnet IDs and current chain status. Record an assigned mainnet netuid even while `status.live:false`; set `status.live:true` and `status.launch_status:"live"` only after the owner starts the subnet and the full release is ready. Set `testnet_netuid` only when a testnet subnet is assigned. Keep unavailable network IDs null.
2. Build images from the reviewed subnet source commit. Record the immutable `images.tag` and every required `images.digests` entry. Set `images.public:true` only when the matching GHCR packages are public; verify an unauthenticated pull and loopback sidecar binding.
3. Set `source.public:true` only when the exact reviewed source tag or commit is public. Confirm tool and model revisions, gates and access permissions.
4. Verify launch-enabled pools, approved manifests/commitments, resource requirements, ports and links. s1-pro is enabled in 1.2.0rc1 (`enabled_at_launch:true`, `gpu.min_vram_gb` 80, no `max_vram_gb` so H200/B200 fit, `gpu.unmeasured` lists cards without a measurement); a pool is only enabled with explicit approval.
5. Update `release_id`, declared `minimum_release` and release notes. Review manifest changes; they reset scores and require human confirmation.
6. Update the bundled descriptor, the public kit copy at https://raw.githubusercontent.com/reaxlabs/reax-miner-kit/main/releases/current.json and https://reax.dev/miner.json together. Check that source ref/commit, image tag/digests and public flags agree. Keep `status.live:false` until the owner-start watcher confirms chain start; the watcher updates only the status fields.
7. Run the unit suite, compatibility CI, public download checks and a controlled real miner rehearsal through human registration. Never perform payments from the kit.
8. Publish the reviewed kit git tag and announce its commit at https://reax.dev/mine/. Verify a fresh clone of the tag against it; retain release evidence.

Release candidates in preparation may carry `images.digests` values of the form `sha256:TBD-<image>` (one place, the `digests` object). They are accepted only while `images.public` and `status.live` are false; with either set to true validation fails with "placeholder image digests", so a final descriptor cannot ship them. `doctor` warns while placeholders remain, and install never pins a placeholder (it uses the tag). Fill all three digests and set `images.public:true` in one edit. `pools[].runtime.env` holds extra runtime sidecar environment (s1-pro: `RUNE_DETERMINISTIC=1`) and `runtime.cc_fix_env` the confidential-compute fix. Previous descriptors are kept as `releases/<release>.json` (1.1.0rc2).

Kit 0.1.5 validates descriptors before caching or consumption. Images must use ghcr.io;
live public images need sha256 digests. Tool keys are bittensor/typer/huggingface_hub, with
restricted version specs. Source URLs use HTTPS GitHub; refs are version tags or 40-hex
commits, and optional source.commit is checked after public checkout. Refreshed descriptors
must not downgrade known release versions or regress a live launch. Set --offline or
REAX_OFFLINE=1 to suppress refresh. The validated descriptor remains unsigned: HTTPS and
schema checks are not a cryptographic publisher signature.

### Tiered pools (optional pool fields)

`s1-lite` and `s1-cpu` use these optional `pools[]` fields (absent means the `s1-fast`/`s1-pro` behaviour): `tier` (`gpu`, `small`, `cpu`), `arch` (list of `x86_64`/`aarch64`, default `["x86_64"]`), `accelerator` (`nvidia`, `none`; `none` allows `gpu.count: 0`, `gpu.vendor: "none"`, `gpu.min_vram_gb: 0`), `jetson` (bool; needs `nvidia` and `aarch64`), `weights.files` (exact file names; only these are downloaded), `min_ram_gb` / `min_cpu_cores` (per-pool override of `requirements`), and `dev_only` (localnet/testnet only; mainnet install is refused). Image keys `s1_lite_runtime`, `s1_lite_runtime_jetson` (linux/arm64, Jetson) and `s1_cpu_runtime` are optional in `images`; in 1.2.0rc1 they carry `sha256:TBD-*` placeholders like the others until the subnet's docker.yml publishes them. Manifest ids of `s1-lite` (`8c7297e14e5e89bf`) and `s1-cpu` (`f2c9981f0766f2a6`) must equal the subnet release's manifests (`sha256` of the canonical JSON, first 16 hex). Sidecar ports 8103 and 8104 are listed in `never_publish_ports`.
