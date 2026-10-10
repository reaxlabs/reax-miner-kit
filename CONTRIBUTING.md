# Contributing to the REAX miner kit

Thanks for helping. This repository contains `reaxctl`, a Python 3.8+ standard-library CLI that prepares a machine for mining on the REAX Bittensor subnet.

## Before you start

- Read [SPEC.md](SPEC.md) (the safety contract) and [AGENTS.md](AGENTS.md) (development rules). Changes must not weaken the safety rules: no coldkeys or recovery phrases, no payments, no telemetry.
- Open an issue first for anything larger than a small fix, so we can agree on the approach.
- Report security problems privately, as described in [SECURITY.md](SECURITY.md), not in a public issue.

## Development

```sh
python3 -m unittest discover -s tests
python3 -m py_compile reaxctl reaxlib/*.py
./reaxctl spec --json | python3 -m json.tool
./reaxctl doctor --json
```

The same steps run in CI on Python 3.8, 3.10, 3.12 and 3.13. Tests are offline and use fakes and temporary directories; do not add real wallet, Docker, chain or GPU operations to the unit suite.

## Pull requests

- Keep each pull request focused and add or update tests for behaviour changes.
- Controller code uses only the standard library, list-form subprocess calls (never `shell=True`) and private file modes for secrets.
- Do not change image digests, model revisions or pins in `releases/current.json` unless the pull request is a release update; see [releases/README.md](releases/README.md).
- Never include keys, recovery phrases, tokens, wallet files or unredacted logs in issues or pull requests.

By contributing you agree that your contribution is licensed under the MIT License of this repository.
