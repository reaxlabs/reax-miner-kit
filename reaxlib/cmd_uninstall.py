"""uninstall: stop everything this tool started and remove its state directory. Wallets are never touched."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import state
from .cli import add_common, build_parser
from .result import Result

KEEP_WITHOUT_PURGE = ('weights', 'hf-cache')


def _sub(args, argv):
    """Run another reaxctl command in-process, return its Result (never raises)."""
    parsed = build_parser().parse_args(argv + ['--home', str(state.home(args)), '--yes', '--offline'])
    try:
        return parsed.func(parsed)
    except Exception as exc:  # best effort: a missing service must not stop the removal
        return Result(command=' '.join(argv), status='error', summary=type(exc).__name__)


def _safe_target(home: Path) -> bool:
    resolved = home.resolve()
    if resolved in (Path('/'), Path.home().resolve()) or len(resolved.parts) < 3:
        return False
    leftovers_only = bool(list(resolved.iterdir())) and all(child.name in KEEP_WITHOUT_PURGE for child in resolved.iterdir())  # after an earlier uninstall that kept the weights
    return (resolved / 'config.json').is_file() or (resolved / 'state.json').is_file() or leftovers_only


def handle(args):
    home = state.home(args)
    if not home.exists():
        return Result('uninstall', summary='Nothing to remove: ' + str(home) + ' does not exist.')
    if not _safe_target(home):
        return Result('uninstall', status='blocked', summary='Refusing to remove ' + str(home) + ': it does not look like a reaxctl state directory.', exit_code=10)
    purge = bool(getattr(args, 'purge_weights', False))
    keep = () if purge else KEEP_WITHOUT_PURGE
    doomed = sorted(p.name for p in home.iterdir() if p.name not in keep)
    if not getattr(args, 'yes', False):
        return Result('uninstall', status='needs_human', summary='Uninstall needs your confirmation.',
                      data={'home': str(home), 'would_remove': doomed, 'kept': [k for k in KEEP_WITHOUT_PURGE if (home / k).exists() and not purge], 'wallets': 'never touched'},
                      needs_human={'action': 'Confirm that this miner setup should be removed.', 'why': 'This stops the miner, the service and the update timer and deletes the state directory (keys in your wallet directory stay).',
                                   'steps': ['Run: reaxctl uninstall --yes' + (' --purge-weights' if purge else ' [--purge-weights to also delete the model weights]')],
                                   'cost': {'kind': 'none', 'note': 'Nothing is charged; a registered hotkey stays registered on the network.'}})
    steps = {}
    for name, argv in (('localnet', ['localnet', 'down']), ('miner', ['stop']), ('service', ['service', 'uninstall']), ('update-timer', ['update', 'timer', 'uninstall'])):
        steps[name] = _sub(args, argv).status
    removed = []
    for child in sorted(home.iterdir()):
        if child.name in keep:
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(str(child))
        removed.append(child.name)
    if not any(home.iterdir()):
        home.rmdir()
    return Result('uninstall', summary='Removed ' + str(home) + (' (model weights kept)' if keep and (home / 'weights').exists() else '') + '. Wallets were not touched.',
                  data={'steps': steps, 'removed': removed, 'wallets': 'never touched'})


def register(sub):
    p = sub.add_parser('uninstall', help='stop everything and remove the state directory (wallets are never touched)')
    add_common(p)
    p.add_argument('--purge-weights', action='store_true', help='also delete the downloaded model weights')
    p.set_defaults(func=handle)
