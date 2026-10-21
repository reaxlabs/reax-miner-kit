# Several models on one hotkey, and a competition entry next to it (release 1.2.0rc10)

From chain block **9331987** (about 13:00 UTC on 21 October 2026) one hotkey can

* serve **several s1 models** behind one miner endpoint (`REAX_MODELS`), and
* hold **one competition entry** (use-case/generalist track or engine track) next to its inference element ("dual role").

Both start at the same block, `MULTIMODEL_START_BLOCK = DUALROLE_START_BLOCK = 9331987` in `elements.py` of the subnet release.
Before it, one model per hotkey and one element per commitment: validators read no role from anything else. The miner image
and `reax-commit` enforce this themselves (below), so the kit adds no block check of its own. Source of truth: "Several models
on one hotkey" in `docs/MINER.md` of https://github.com/reaxlabs/reax-subnet and `docs/DESIGN-MULTIROLE.md` there.

`reaxctl install` still installs **one pool** (one runtime sidecar, `REAX_MODEL`). For several models you run the same pinned
images with a compose file that has one sidecar per model; [`service/docker-compose.multimodel.example.yml`](../service/docker-compose.multimodel.example.yml)
is a complete example (s1-lite plus s1-cpu). Do not run `reaxctl install` again in the same state directory: it would render a
single-pool `compose.yml` and `miner.env` over your own. `reaxctl verify` reads the multi-model `/health` and works against
the pool you installed.

## What the miner needs

| Model | Sidecar image key (`releases/current.json`) | Port | Environment of the miner |
| --- | --- | --- | --- |
| s1-fast | `s1_fast_runtime` | 8101 | `REAX_S1_FAST_RUNTIME_URL=http://127.0.0.1:8101` |
| s1-pro | `s1_pro_runtime` | 8102 | `REAX_S1_PRO_RUNTIME_URL=http://127.0.0.1:8102` |
| s1-lite | `s1_lite_runtime` (`s1_lite_runtime_jetson` on a Jetson) | 8103 | `REAX_S1_LITE_RUNTIME_URL=http://127.0.0.1:8103` |
| s1-cpu | `s1_cpu_runtime` | 8104 | `REAX_S1_CPU_RUNTIME_URL=http://127.0.0.1:8104` |
| reax-1 (planned, not active) | `reax_1_runtime` (digest TODO) | 8105 | `REAX_REAX_1_RUNTIME_URL=http://127.0.0.1:8105` |

* `REAX_MODELS=s1-lite,s1-cpu` (comma-separated, each model at most once, at least two; the first is the primary model). Leave
  `REAX_MODEL` unset: if you set it, it must be in the list.
* Every model has its **own sidecar**, own port and own weights directory, with the **same** `REAX_RUNTIME_TOKEN_FILE` token. Two
  models must not share a URL. Hardware is per model: the sidecars together must fit the machine (you save registrations, not
  hardware). Never publish the sidecar ports 8101-8105 or 8000.
* Limits (`REAX_MAX_CONCURRENT`, `REAX_PER_CALLER_CONCURRENCY`, `REAX_PER_CALLER_PER_MINUTE`) apply **per model**, with the
  image defaults 8 / 32 / 456.
* `GET /health` on the miner port returns `{"protocol": "reax-s1/1", "version": ..., "models": {"<model>": "<manifest_id>", ...}}`.
* The miner publishes `reax-s1m/1:<model>:<manifest_id>,<model>:<manifest_id>` on start (canonical order s1-fast, s1-pro,
  s1-lite, s1-cpu, reax-1; at most four models fit the 128-byte field); the manifest ids are the `manifest_id` values of the pools in `releases/current.json`. A competition
  element already on the hotkey is kept.
* **Before block 9331987** the miner refuses to start with two or more models (it exits with `refusing to start: ... count from
  block 9331987`; with `restart: unless-stopped` it keeps restarting). Start it after the block, or keep the old single-model
  setup until then. `REAX_FORCE_MULTIROLE=1` overrides this, and the same check in `reax-commit`, but nobody scores the result
  before the start block: use it for local tests only, never on mainnet, and never from an agent.

## Run it

```sh
export REAX_UID=$(id -u) REAX_GID=$(id -g) REAX_WALLET_NAME=<coldkey name> REAX_WALLET_HOTKEY=<hotkey name>
cd <directory with the compose file>
mkdir -p secrets && openssl rand -hex 32 > secrets/runtime-token && chmod 0600 secrets/runtime-token
cp miner.env.example miner.env        # network, netuid, wallet and hotkey names, REAX_EXTERNAL_IP / PORT
# weights: pinned revisions from releases/current.json (s1-lite and s1-cpu are public; s1-pro is gated: your own HF account)
hf download crh225/plumb-4b-GGUF plumb-4b-v5-Q8_0.gguf --revision 71c1c57e523219124f7be9c486d475d19891185f --local-dir /srv/reax/s1-lite
hf download kouhxp/gutsy gutsy-0.8b-v04-q8_0.gguf --revision f9995fe26a337f1a4ea3f1a5ed38c9799e5a8f63 --local-dir /srv/reax/s1-cpu
REAX_S1_LITE_WEIGHTS=/srv/reax/s1-lite REAX_S1_CPU_WEIGHTS=/srv/reax/s1-cpu \
  docker compose -f docker-compose.multimodel.example.yml up -d
docker compose -f docker-compose.multimodel.example.yml logs miner | tail    # "published commitment reax-s1m/1:..."
curl -s http://127.0.0.1:8091/health
```

Registration of the hotkey is a human step in your own wallet, as for one model (`reaxctl wallet`, https://reax.dev/register/).
For another combination copy the sidecar block of each model from the table (s1-fast and s1-pro use `RUNE_HOST`/`RUNE_PORT`
and the s1-pro compose file in this repository for the sidecar settings: `RUNE_DETERMINISTIC=1`, the gated weights).
Run `reax-selfcheck --model <model>` per model before you register (see below).

## Commands inside the miner image

The miner image ships `reax-commit`, `reax-selfcheck` and `reax-engine-submit`. Run them with the compose file you use for the miner,
so they get the same wallet mount, network and environment, and without starting another miner (`--no-deps`). `reax-selfcheck`
needs the sidecar of that model to be running (`docker compose ... up -d runtime-s1-lite`):

```sh
C="docker compose -f docker-compose.multimodel.example.yml run --rm --no-deps"   # or: -f ~/.reax-miner/compose.yml (one-model install)
$C --entrypoint reax-commit miner show                  # fields on chain and how this release reads them
$C --entrypoint reax-selfcheck miner --model s1-lite --json
```

`reax-commit show` prints the fields on chain, whether validators read them at the current block, and per model whether the
manifest id matches this release (`matches this release` / `STALE`).

### Dual role: one competition entry next to inference

Never write the competition entry with a hand-made `btcli` commit: every commitment write replaces all fields and would drop your
inference element. Use the tools; they keep the other element:

```sh
$C --entrypoint reax-commit miner set "reax-spec/1:<track_id>:<owner>/<repo>@<40-hex revision>" --dry-run   # prints the fields it would write
$C --entrypoint reax-commit miner set "reax-spec/1:<track_id>:<owner>/<repo>@<40-hex revision>"             # writes [inference element, new element]
$C --entrypoint reax-engine-submit miner <image>@sha256:<64-hex digest> --dry-run                           # engine track instead (see below)
$C --entrypoint reax-commit miner withdraw competition --dry-run                                           # removes only the competition element
```

* A commitment holds at most one inference and one competition element (three fields in total on finney). `set` replaces a
  competition element of the same kind and keeps the inference element; the miner's own 10-minute re-publish touches only the
  inference element, so the two never overwrite each other.
* `reax-engine-submit` refuses on finney while the subnet owner holds the engine track closed; `--dry-run` only prints the
  element text. Drop `--dry-run` to write it once the engine track is open.
* The competition entry is evaluated on the subnet's own worker, never on your serving GPU. Track ids, rules, deadlines and
  floors are on https://reax.co/competitions.
* Before block 9331987 `set` and `reax-engine-submit` refuse when the other kind exists: the message offers `--replace-all`
  (writes the new element alone and **drops the other one**, today's behaviour) or `REAX_FORCE_MULTIROLE=1`. Neither is needed
  after the block. Always run `--dry-run` first and read the printed fields.
* Changing a manifest id (a model or manifest change in an update) restarts that model's score; the kit's
  `--accept-manifest-change` rule still applies to `reaxctl update`.

## Rules for agents

* Do not set `REAX_FORCE_MULTIROLE`, pass `--replace-all`, or write a competition element without the person's explicit approval of
  the exact element text. Run `reax-commit ... --dry-run` and show the fields first.
* A commitment write is signed with the hotkey; the coldkey is never involved and never needed. Do not mount a coldkey into a container.
