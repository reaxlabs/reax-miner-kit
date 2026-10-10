from .cli import add_common
from . import cmd_install as io
from . import localnet_ops


def handle(args):
    if args.action == 'up':
        return localnet_ops.up(args)
    home = io.home_of(args)
    return localnet_ops.down(home) if args.action == 'down' else localnet_ops.status(home)


def register(sub):
    p = sub.add_parser('localnet')
    actions = p.add_subparsers(dest='action', required=True)
    for action in ('up', 'down', 'status'):
        a = actions.add_parser(action)
        add_common(a)
        if action == 'up':
            a.add_argument('--host', default=None, help='assigned private IPv4 for the rehearsal miner')
            a.add_argument('--base-port', type=int, default=None)
            a.add_argument('--timeout', type=int, default=120)
        a.set_defaults(func=handle)
