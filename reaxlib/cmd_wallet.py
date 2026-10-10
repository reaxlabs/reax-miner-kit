"""Hotkey-only creation, public coldkey import, and human registration handoff."""
from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import re

from . import descriptor, ss58, state, sysprobe
from .cli import add_common
from .result import Result


class WalletNameError(ValueError):
    """A safe, user-facing usage error for path component validation."""


def wallet_paths(args):
    config = state.load_config(args)
    name = getattr(args, 'wallet_name', None) or config.get('wallet_name', 'reax')
    hotkey = getattr(args, 'hotkey_name', None) or config.get('hotkey_name', 'miner1')
    for value in (name, hotkey):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
            raise WalletNameError('wallet and hotkey names must contain only letters, numbers, underscores or hyphens')
    base = os.environ.get('REAX_WALLET_DIR')
    root = (Path(base) if base else Path.home() / '.bittensor' / 'wallets') / name
    return root, root / 'hotkeys' / hotkey, root / 'coldkeypub.txt'


def public_address(path):
    value = state.read_json(path)
    if isinstance(value, dict):
        address = value.get('ss58Address')
        if address:
            try:
                ss58.decode(address)
                return address
            except ValueError:
                pass
    return None


# All library stdout/stderr is captured inside the child: wallet libraries may print a mnemonic.
BACKEND_SCRIPT = '''
import contextlib, io, json, os, sys
os.umask(0o077)
result = None
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            from bittensor_wallet import Wallet
        except ImportError:
            from bittensor.wallet import Wallet
        name, hotkey, wallet_path = sys.argv[1:]
        for directory in (wallet_path, os.path.join(wallet_path, name), os.path.join(wallet_path, name, 'hotkeys')):
            if os.path.islink(directory):
                raise ValueError('Wallet directories must not be symlinks')
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        if os.path.islink(os.path.join(wallet_path, name, 'hotkeys', hotkey)):
            raise ValueError('Hotkey target must not be a symlink')
        wallet = Wallet(name=name, hotkey=hotkey, path=wallet_path)
        if not wallet.hotkey_file.exists_on_device():
            wallet.create_new_hotkey(n_words=12, use_password=False, overwrite=False, suppress=True)
        address = wallet.hotkey.ss58_address
        os.chmod(wallet.hotkey_file.path, 0o600)
        result = {'ss58': address}
except BaseException:
    result = {'error': 'Hotkey backend failed; no secret output was retained.'}
print(json.dumps(result))
'''


def venv_backend(args, root):
    python = state.home(args) / 'venv' / 'bin' / 'python'
    name = root.name
    hotkey = wallet_paths(args)[1].name
    output = sysprobe.run([str(python), '-I', '-c', BACKEND_SCRIPT, name, hotkey, str(root.parent)], timeout=30)
    try:
        data = json.loads(output['stdout'])
        address = data['ss58']
        ss58.decode(address)
        return address
    except (ValueError, KeyError, TypeError):
        raise ValueError('Hotkey backend failed; no secret output was retained.')


def hotkey(args, backend=None):
    root, path, _ = wallet_paths(args)
    if path.is_symlink():
        return Result('wallet hotkey', status='blocked', summary='Hotkey path must not be a symbolic link.')
    if path.exists():
        address = public_address(path)
        if address:
            return Result('wallet hotkey', summary='Existing hotkey: ' + address + '.', data={'ss58': address, 'path': str(path)}, warnings=['Keep the hotkey file private; never paste recovery phrases.'])
        return Result('wallet hotkey', status='blocked', summary='Existing hotkey could not be read as a public address; it was preserved.', next=[{'command': 'reaxctl wallet status', 'why': 'Check this wallet without overwriting any key.'}])
    if backend is None and not (state.home(args) / 'venv' / 'bin' / 'python').is_file():
        return Result('wallet hotkey', status='blocked', summary='Tool venv is missing; install it before creating the hotkey.', next=[{'command': 'reaxctl install', 'why': 'Install the pinned wallet tools.'}])
    try:
        address = (backend or venv_backend)(args, root)
        ss58.decode(address)
        if not path.is_file():
            raise ValueError('backend did not create a hotkey file')
        os.chmod(path, 0o600)
    except Exception:
        return Result('wallet hotkey', status='error', summary='Hotkey creation failed; no recovery phrase was retained or displayed.')
    config = state.load_config(args)
    config.update(wallet_name=root.name, hotkey_name=path.name, hotkey_ss58=address)
    state.save_config(args, config)
    return Result('wallet hotkey', summary='Hotkey created without a coldkey: ' + address + '.', data={'ss58': address, 'path': str(path)},
                  warnings=['Keep the hotkey file private; never paste recovery phrases.'], next=[{'command': 'reaxctl wallet status', 'why': 'Check the public wallet details.'}])


def coldkeypub(args):
    try:
        key = ss58.public_key_hex(args.ss58)
        root, _, path = wallet_paths(args)
    except ValueError as exc:
        return Result('wallet coldkeypub', status='error', summary=str(exc), exit_code=2)
    value = {'accountId': key, 'publicKey': key, 'cryptoType': 1, 'ss58Address': args.ss58}
    if path.exists() and state.read_json(path) != value:
        return Result('wallet coldkeypub', status='blocked', summary='A different public coldkey file already exists; it was preserved.')
    state.write_json(path, value)
    config = state.load_config(args)
    config['wallet_name'] = root.name
    state.save_config(args, config)
    return Result('wallet coldkeypub', summary='Public coldkey address saved; no coldkey or recovery phrase was used.', data={'ss58': args.ss58, 'public_key': key, 'path': str(path)}, next=[{'command': 'reaxctl register', 'why': 'Review the registration handoff.'}])


def wallet_status(args):
    root, hot, cold = wallet_paths(args)
    config = state.load_config(args)
    cached_address = config.get('hotkey_ss58') if config.get('wallet_name') == root.name and config.get('hotkey_name') == hot.name else None
    if cached_address:
        try:
            ss58.decode(cached_address)
        except (ValueError, TypeError):
            cached_address = None
    data = {}
    warnings = []
    for name, path in [('hotkey', hot), ('coldkeypub', cold)]:
        exists = path.is_file()
        permissions = format(path.stat().st_mode & 0o777, '04o') if exists else None
        data[name] = {'path': str(path), 'exists': exists, 'permissions': permissions, 'ss58': (cached_address if name == 'hotkey' else public_address(path)) if exists else None}
        if name == 'hotkey' and exists and permissions != '0600':
            warnings.append('Hotkey permissions should be 0600; run chmod 600 on the reported hotkey path.')
    if state.mode(args) == 'localnet':
        root = state.home(args) / 'localnet' / 'wallets'
        data['rehearsal_wallets'] = {'path': str(root), 'wallets': [
            {'name': name, 'path': str(root / name),
             'hotkey_ss58': state.read_json(state.home(args) / 'localnet' / 'subnet.json').get('roles', {}).get(name, {}).get('hotkey'),
             'coldkeypub_ss58': public_address(root / name / 'coldkeypub.txt')}
            for name in ('owner', 'miner', 'validator') if (root / name).is_dir()]}
    return Result('wallet status', summary='Only public addresses and file permissions are shown.', data=data, warnings=warnings)


def register_localnet(args):
    return Result('register', summary='localnet up registers its throw-away miner.', next=[{'command': 'reaxctl localnet up', 'why': 'Start the disposable rehearsal.'}])


def registration(args):
    wallet_paths(args)
    release = descriptor.load(args, fetch=True)
    mode = state.mode(args)
    if mode == 'localnet':
        return register_localnet(args)
    _, path, _ = wallet_paths(args)
    address = public_address(path)
    link = release['links']['register']
    live = release['status']['live']
    netuid = release['status']['netuid' if mode == 'mainnet' else 'testnet_netuid']
    available = live and netuid is not None
    steps = ['Open ' + link, 'Use the hotkey public SS58: ' + address if address else 'Create a hotkey with reaxctl wallet hotkey first.', 'The page shows the live fee, you approve in your own wallet.']
    return Result('register', status='needs_human', summary='Registration requires your approval in your own wallet.' if available else 'Registration is launching soon; no real-network action was taken.',
                  data={'mode': mode, 'hotkey_ss58': address, 'netuid': netuid, 'available': available},
                  needs_human={'action': 'Fund and register the hotkey when registration opens.', 'why': 'reaxctl never spends real TAO or signs registration.', 'steps': steps, 'cost': {'kind': 'tao', 'note': 'The page shows the live fee; you approve in your own wallet.'}, 'link': link},
                  warnings=['Never paste a recovery phrase. Rewards are variable subnet emissions; can be zero.'],
                  next=[] if available else [{'command': 'reaxctl plan --mode localnet', 'why': 'Try the rehearsal while registration is launching soon.'}],
                  exit_code=11 if available else 12)


def register(sub):
    wallet = sub.add_parser('wallet')
    commands = wallet.add_subparsers(dest='wallet_command')
    for name, handler in [('hotkey', hotkey), ('coldkeypub', coldkeypub), ('status', wallet_status)]:
        parser = commands.add_parser(name); add_common(parser)
        parser.add_argument('--wallet-name', default=None)
        parser.add_argument('--hotkey-name', default=None)
        if name == 'coldkeypub':
            parser.add_argument('--ss58', required=True)
        parser.set_defaults(func=handler)
    parser = sub.add_parser('register'); add_common(parser)
    parser.add_argument('--wallet-name', default=None)
    parser.add_argument('--hotkey-name', default=None)
    parser.add_argument('--confirm-mainnet', action='store_true')
    parser.set_defaults(func=registration)
