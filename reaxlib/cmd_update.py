"""Descriptor-gated updates with local rollback artifacts."""
import json
import os
import pathlib
import shutil
import shlex
import time
from .cli import add_common
from .result import Result
from . import cmd_install as io
from . import cmd_service as services
from .cmd_runtime import compose
from . import localnet_ops


def timer(args):
    home = io.home_of(args)
    directory, user = services.location(args)
    command = 'update timer ' + args.timer_action
    if user is None:
        return Result(command, 'blocked', 'Automatic update timers require systemd.', exit_code=10)
    if not user and os.geteuid() != 0:
        return io.human(command, 'Install the update timer with administrator privileges.', 'System units require root.',
                        ['sudo ' + shlex.quote(str(pathlib.Path(__file__).resolve().parent.parent / 'reaxctl')) + ' update timer ' + args.timer_action + ' --system --home ' + shlex.quote(str(home))])
    names = ('reax-miner-update.service', 'reax-miner-update.timer')
    if args.timer_action == 'install':
        directory.mkdir(parents=True, exist_ok=True)
        for name in names:
            from .state import write_text
            write_text(directory / name, services.render_template(name, home, pathlib.Path(__file__).resolve().parent.parent / 'reaxctl', user=user), 0o644)
        rc, _, _ = services.systemctl(user, 'daemon-reload')
        if not rc:
            rc, _, _ = services.systemctl(user, 'enable', '--now', names[1])
    else:
        rc, _, _ = services.systemctl(user, 'disable', '--now', names[1])
        if rc and any((directory / name).exists() for name in names):
            return Result(command, 'error', 'Timer could not be disabled; files retained.', exit_code=10)
        for name in names:
            p = directory / name
            if p.exists():
                p.unlink()
        rc, _, _ = services.systemctl(user, 'daemon-reload')
    return Result(command, 'error' if rc else 'ok', 'Update timer action completed.' if not rc else 'Systemd timer action failed.', exit_code=10 if rc else 0)


def handle(args):
    if args.action == 'timer':
        return timer(args)
    home = io.home_of(args)
    config = io.read_config(home)
    d = io.descriptor(home)
    pool = io.pool_of(d, config.get('pool', 's1-fast'))
    if not pool:
        return Result('update', 'blocked', 'Installed pool is absent from this release.', exit_code=12)
    changed = config.get('manifest_id') != pool['manifest_id']
    available = config.get('release_id') != d['release_id'] or changed
    data = {'installed_release': config.get('release_id'), 'available_release': d['release_id'],
            'manifest_changed': changed, 'update_available': available}
    if args.action == 'check' or not available:
        return Result('update ' + args.action, summary='Release comparison completed.', data=data)
    if changed and not args.accept_manifest_change:
        return io.human('update apply', 'Approve the manifest change.',
                        'A changed manifest resets the miner score to zero and must be recognized by validators.',
                        ['Review the release notes and repeat with --accept-manifest-change.'])
    if not changed and not d.get('update', {}).get('auto_safe'):
        return io.human('update apply', 'Review this update before applying it.',
                        'The descriptor does not classify it as automatically safe.', ['Review the release with its operator.'])
    if not config:
        return Result('update apply', 'blocked', 'Install before updating.', exit_code=10)
    failure = io.gate(d, config.get('mode', 'localnet'), True)
    if failure:
        failure.command = 'update apply'
        return failure
    rollback = home / 'rollback' / str(time.time_ns())
    from .state import private_dir, write_text
    private_dir(home / "rollback")
    private_dir(rollback)
    for name in ('compose.yml', 'miner.env', 'config.json'):
        if (home / name).exists():
            write_text(rollback / name, (home / name).read_text(), 0o644 if name == 'compose.yml' else 0o600)
    # Include old image pins rather than just the newly fetched descriptor.
    io.write_json(rollback / 'images.json', {'compose': (home / 'compose.yml').read_text() if (home / 'compose.yml').exists() else None,
                                           'release_id': config.get('release_id'), 'manifest_id': config.get('manifest_id')})
    args.pool = config.get('pool', 's1-fast')
    args.mode = config.get('mode', 'localnet')
    args.confirm_mainnet = True
    was_running = args.mode == 'localnet' and len(localnet_ops.processes(home)) == 5 and all(localnet_ops.alive(r) for r in localnet_ops.processes(home).values())
    result = io.install(args)
    if result.ok and was_running:
        localnet_ops.down(home)
        result = localnet_ops.up(args)
    if result.ok and args.mode != 'localnet':
        rc, _, _ = compose(home, 'up', '-d', timeout=600)
        if rc:
            result = Result('update apply', 'error', 'Updated containers failed to start.', exit_code=13)
    if not result.ok:
        for name in ('compose.yml', 'miner.env', 'config.json'):
            if (rollback / name).exists():
                write_text(home / name, (rollback / name).read_text(), 0o644 if name == 'compose.yml' else 0o600)
        if args.mode != 'localnet' and (home / 'compose.yml').exists():
            compose(home, 'up', '-d', timeout=300)
        if was_running:
            localnet_ops.up(args)
        result.command = 'update apply'
        result.data['rollback'] = str(rollback)
        result.warnings.append('Previous configuration restored; check runtime status.')
        return result
    io.mark(home, 'update', {'release_id': d['release_id'], 'rollback': str(rollback)})
    return Result('update apply', summary='Update applied.', data=dict(data, rollback=str(rollback)))


def register(sub):
    p = sub.add_parser('update')
    actions = p.add_subparsers(dest='action', required=True)
    for action in ('check', 'apply'):
        a = actions.add_parser(action)
        add_common(a)
        a.add_argument('--accept-manifest-change', action='store_true')
        a.add_argument('--auto', action='store_true')
        a.set_defaults(func=handle)
    t = actions.add_parser('timer')
    timer_actions = t.add_subparsers(dest='timer_action', required=True)
    for action in ('install', 'uninstall'):
        a = timer_actions.add_parser(action)
        add_common(a)
        group = a.add_mutually_exclusive_group()
        group.add_argument('--user', action='store_true')
        group.add_argument('--system', action='store_true')
        a.set_defaults(func=handle)
