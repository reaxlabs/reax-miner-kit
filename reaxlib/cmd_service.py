"""Service installation without interactive privilege escalation."""
import os
import pathlib
import platform
import shlex
from xml.sax.saxutils import escape
from .cli import add_common
from .result import Result
from . import cmd_install as io


def location(args):
    if platform.system() == 'Darwin':
        return pathlib.Path.home() / 'Library/LaunchAgents', None
    user = getattr(args, 'user', False) or (os.geteuid() != 0 and not getattr(args, 'system', False))
    return (pathlib.Path.home() / '.config/systemd/user' if user else pathlib.Path('/etc/systemd/system')), user


def render_template(name, home, executable, darwin=False, user=True):
    root = pathlib.Path(__file__).resolve().parent.parent
    text = (root / 'service' / name).read_text()
    values = {'EXEC': str(executable), 'HOME': str(home), 'WORKDIR': str(root), 'LOG': str(home / 'logs/service.log'), 'USER': str(os.environ.get('SUDO_UID') or os.getuid())}
    for key, value in values.items():
        if any(c in value for c in ('\n', '\r', '\0')):
            raise ValueError('Service paths cannot contain control characters')
        # systemd percent specifiers and quotes must not interpret path characters.
        if key == 'USER':
            if not user and (not value.isdigit() or int(value) == 0):
                raise ValueError('System units require a non-root invoking user (sudo preserves SUDO_UID)')
            text = text.replace('User=@USER@', '' if user else 'User=' + value)
            continue
        if key == 'WORKDIR' and not darwin:
            if any(c.isspace() for c in value):
                raise ValueError('WorkingDirectory cannot contain whitespace')
            text = text.replace('@WORKDIR@', value.replace('%', '%%'))
            continue
        value = escape(value) if darwin else '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'
        text = text.replace('@' + key + '@', value)
    return text


def systemctl(user, *args):
    return io.runner(['systemctl'] + (['--user'] if user else []) + list(args))


def privilege(args, command):
    return io.human(command, 'Install the system unit with administrator privileges.',
                    'reaxctl never invokes interactive sudo.',
                    ['sudo ' + shlex.quote(str(pathlib.Path(__file__).resolve().parent.parent / 'reaxctl')) +
                     ' service ' + args.action + ' --system --home ' + shlex.quote(str(io.home_of(args))) + ' --json'])


def handle(args):
    home = io.home_of(args)
    directory, user = location(args)
    darwin = user is None
    name = 'dev.reax.miner.plist' if darwin else 'reax-miner.service'
    target = directory / name
    command = 'service ' + args.action
    if args.action == 'status':
        rc, out, _ = io.runner(['launchctl', 'print', 'gui/{}/dev.reax.miner'.format(os.getuid())]) if darwin else systemctl(user, 'is-active', name)
        return Result(command, summary='Service status.', data={'installed': target.exists(), 'active': rc == 0, 'detail': out.strip(), 'path': str(target)})
    if not darwin and not user and os.geteuid() != 0:
        return privilege(args, command)
    if darwin and io.read_config(home).get('mode', 'localnet') != 'localnet':
        return Result(command, 'blocked', 'macOS services support the localnet rehearsal.', exit_code=10)
    if args.action == 'install':
        home.mkdir(parents=True, exist_ok=True)
        (home / 'logs').mkdir(exist_ok=True)
        directory.mkdir(parents=True, exist_ok=True)
        executable = pathlib.Path(__file__).resolve().parent.parent / 'reaxctl'
        from .state import write_text, private_dir
        private_dir(home)
        write_text(target, render_template(name, home, executable, darwin, user), 0o644)
        if darwin:
            io.runner(['launchctl', 'bootout', 'gui/' + str(os.getuid()), str(target)])
            rc, _, _ = io.runner(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(target)])
        else:
            rc, _, _ = systemctl(user, 'daemon-reload')
            if not rc:
                rc, _, _ = systemctl(user, 'enable', '--now', name)
            if not rc:
                rc, _, _ = systemctl(user, 'is-active', name)
        if rc:
            return Result(command, 'error', 'Service manager refused the unit; inspect its logs.', exit_code=10)
        if user:
            rc, out, _ = io.runner(['loginctl', 'show-user', str(os.getuid()), '-p', 'Linger', '--value'])
            if rc or out.strip() != 'yes':
                return io.human(command, 'Enable lingering for unattended operation.',
                                'The user service can stop after logout.', ['sudo loginctl enable-linger ' + shlex.quote(str(os.getuid()))])
        return Result(command, summary='Service installed.', data={'path': str(target)})
    if darwin:
        rc, _, _ = io.runner(['launchctl', 'bootout', 'gui/' + str(os.getuid()), str(target)])
    else:
        rc, _, _ = systemctl(user, 'disable', '--now', name)
    if rc and target.exists():
        return Result(command, 'error', 'Service could not be disabled; unit retained.', exit_code=10)
    if target.exists():
        target.unlink()
    if not darwin:
        systemctl(user, 'daemon-reload')
    return Result(command, summary='Service uninstalled; wallets retained.')


def register(sub):
    p = sub.add_parser('service')
    actions = p.add_subparsers(dest='action', required=True)
    for action in ('install', 'uninstall', 'status'):
        a = actions.add_parser(action)
        add_common(a)
        group = a.add_mutually_exclusive_group()
        group.add_argument('--user', action='store_true')
        group.add_argument('--system', action='store_true')
        a.set_defaults(func=handle)
