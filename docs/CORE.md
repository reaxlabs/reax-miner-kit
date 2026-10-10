# Core commands

The core runtime uses only Python's standard library. `cmd_info` registers version, spec,
agent-rules and net-policy. `cmd_doctor` registers read-only doctor and plan; plan includes
the complete human registration graph with launch blocks and a localnet graph using dev accounts.
`cmd_wallet` registers hotkey creation, public coldkey import, wallet status and registration.
No core command installs software, starts a service or submits a real chain transaction.

`state.home(args)` resolves `--home`, then `REAX_HOME`, then `~/.reax-miner`.
`load_config/save_config` and `load_state/save_state` use JSON, atomic replace and 0600 files.
`descriptor.load(args, fetch=False)` validates cached releases and falls back to the bundled
release. Passing `fetch=True` explicitly refreshes from `REAX_RELEASES_URL` with a bounded
request; failed downloads or invalid descriptors fall back to validated local data.

`ss58.encode`, `ss58.decode` and `ss58.public_key_hex` implement the 32-byte prefix-42
format with the SS58PRE blake2b checksum. Public coldkey files match the verified btcli
11.1 serialization (`accountId`, `publicKey`, `ss58Address`, `cryptoType: 1`). A different
existing public key is preserved. Hotkey files and existing encrypted/unrecognized keys
are never overwritten.

Hotkey creation executes the tool venv Python and prefers `bittensor_wallet.Wallet`.
The pinned Bittensor 11.1 package does not depend on that module, so the backend falls
back to its native `bittensor.wallet.Wallet`. Both paths create only an unencrypted
hotkey with overwrite disabled and mnemonic output suppressed. Child stdout/stderr
is captured; only a validated address or a generic error escapes. Missing tools return
`blocked` with `reaxctl install` as the next action.

`register_localnet(args)` provides guidance. `register` returns ok and explains that `localnet up` registers its throw-away miner.
Real-network registration always returns a human handoff, with exit 12 while the
descriptor is not live or the selected network lacks a netuid. It submits no payment
or transaction, so `--confirm-mainnet` is accepted but does not authorize execution here.

# Verification and injection

Run `python3 -m unittest discover -s tests -v`. The `tests/fixtures/core_*.json` host
fixtures contain host facts plus command results (`returncode`, `stdout`, `stderr`).
Call `diagnose(args, probe=fake_run, host=fake_facts, release=fake_descriptor)` to inject
fixtures. Every external host command goes through `sysprobe.run(cmd, timeout)`;
`sysprobe.facts` supplies platform, resource, filesystem and port facts. Its platform
checks can be patched independently to exercise WSL2 and native Windows detection.
Doctor refreshes the descriptor by default (one-hour TTL); use `--offline` for no descriptor request. Hugging Face is marked unchecked until an explicit
`--probe-ip` run. That flag enables the single IP echo and a Hugging Face reachability check.

Call `hotkey(args, backend=fake_backend)` to verify creation without installing dependencies.
The fake receives `(args, wallet_root)`, creates the hotkey file and returns its address.
For an optional real integration check, run `tests/core_wallet_integration.py` using a
throwaway venv containing `bittensor==11.1.0` and `typer<0.27.2`; add `bittensor-wallet`
to also exercise that backend. All test wallets stay inside a temporary directory.

Hardware eligibility is separate from launch readiness. VRAM divides MiB by 1024 and
accepts one GB below nominal minima with a warning. Real mining additionally requires
Docker/Compose/NVIDIA runtime, host requirements, clock synchronization, a live netuid,
public images and an eligible launch-enabled pool. Docker access errors include the
vendor's post-installation permission fix. Driver versions below R580 are blocked.
