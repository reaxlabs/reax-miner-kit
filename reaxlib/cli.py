"""CLI skeleton. Each command module exposes `register(sub)`; handlers take (args) and return a Result.

Modules are imported lazily by name so a broken module cannot break `doctor` or `spec`.
Owner slice per module is noted in MODULES (see briefs): core = kit-core, run = kit-run.
"""
from __future__ import annotations

import argparse
import importlib
import sys

from . import __version__
from .result import EXIT_UNEXPECTED, EXIT_USAGE, Result, emit

# (module, owner slice)
MODULES = [
    ("reaxlib.cmd_info", "core"),      # version, spec, agent-rules, net-policy
    ("reaxlib.cmd_doctor", "core"),    # doctor, plan
    ("reaxlib.cmd_wallet", "core"),    # wallet hotkey|coldkeypub|status, register
    ("reaxlib.cmd_install", "run"),    # install
    ("reaxlib.cmd_runtime", "run"),    # start, stop, restart, status, logs, run, verify
    ("reaxlib.cmd_selfcheck", "run"),  # selfcheck
    ("reaxlib.cmd_service", "run"),    # service install|uninstall|status
    ("reaxlib.cmd_update", "run"),     # update check|apply|timer
    ("reaxlib.cmd_localnet", "run"),   # localnet up|down|status
    ("reaxlib.cmd_uninstall", "run"),  # uninstall
]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reaxctl", description="Set up a machine for mining on the REAX subnet.")
    p.add_argument("--version", action="version", version=f"reaxctl {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    for name, _owner in MODULES:
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name == name:
                continue  # module not written yet
            raise
        mod.register(sub)
    return p


def add_common(p: argparse.ArgumentParser) -> None:
    """Every command calls this on its parser so global flags work after the command name."""
    p.add_argument("--offline", action="store_true", help="use cached or bundled release only")
    p.add_argument("--json", action="store_true", help="print exactly one JSON object on stdout")
    p.add_argument("--yes", "-y", action="store_true", help="non-interactive consent for non-cost, non-key actions")
    p.add_argument("--home", default=None, help="state dir (default $REAX_HOME or ~/.reax-miner)")
    p.add_argument("--mode", choices=["localnet", "testnet", "mainnet"], default=None, help="network mode (default: from config, else localnet)")


def _report_unexpected(exc) -> None:
    """stderr only: exception type and location (never the message, which may hold a secret); full traceback with REAX_DEBUG=1."""
    import os
    import traceback
    frames = traceback.extract_tb(exc.__traceback__)
    where = "{}:{} in {}".format(os.path.basename(frames[-1].filename), frames[-1].lineno, frames[-1].name) if frames else "?"
    sys.stderr.write("reaxctl: unexpected {} at {} (set REAX_DEBUG=1 for a traceback)\n".format(type(exc).__name__, where))
    if os.environ.get("REAX_DEBUG") == "1":
        traceback.print_exception(type(exc), exc, exc.__traceback__)


def main(argv) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    as_json = getattr(args, "json", False)
    try:
        from . import descriptor
        descriptor.begin(args)
        result = args.func(args)
        if 'health' in result.data:
            from .cmd_runtime import sanitize, NOTICE
            from .state import home
            result.data = sanitize(home(args), result.data)
            result.data.update(untrusted=True, notice=NOTICE)
        result.warnings.extend(descriptor.warnings())
        if args.cmd in ('doctor', 'plan'):
            result.data['releases_source'] = descriptor.current_source()
    except ValueError as exc:
        from .cmd_wallet import WalletNameError
        result = Result(command=getattr(args, 'cmd', '?'), status='error', summary=str(exc), exit_code=2 if isinstance(exc, WalletNameError) else 10)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # last resort: never a raw traceback on stdout in --json mode
        _report_unexpected(exc)
        result = Result(command=getattr(args, "cmd", "?"), status="error", summary="Unexpected error; no potentially secret exception details were displayed.", exit_code=EXIT_UNEXPECTED)
    return emit(result, as_json)
