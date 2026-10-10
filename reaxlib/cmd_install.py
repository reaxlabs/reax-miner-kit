"""Installation and shared runtime adapters; no external imports on the CLI path."""
import json
import os
import re
import pathlib
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
from .cli import add_common
from .result import Result


def runner(cmd, timeout=120, env=None, spawn=False, log=None, foreground=False):
    """The sole subprocess boundary, replaceable by tests."""
    try:
        from .state import child_env
        env = dict(child_env(env), HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1', PIP_DISABLE_PIP_VERSION_CHECK='1', GIT_TERMINAL_PROMPT='0')
        if spawn:
            return subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        p = subprocess.run(cmd, env=env, timeout=timeout, stdin=subprocess.DEVNULL,
                           stdout=sys.stderr if foreground else subprocess.PIPE,
                           stderr=sys.stderr if foreground else subprocess.PIPE, universal_newlines=True)
        return p.returncode, p.stdout or '', p.stderr or ''
    except (OSError, subprocess.TimeoutExpired) as e:
        return 13, '', str(e)


def detail(err):
    """Last stderr line of a failed tool, bounded and with tokens masked."""
    lines = err.strip().splitlines() or ['']
    line = next((x for x in reversed(lines) if x.lower().startswith(('fatal:', 'error:'))), lines[-1])
    line = re.sub(r'(hf_|gh[opsur]_|github_pat_)[A-Za-z0-9_]{8,}', r'\1***', line)
    line = re.sub(r'://[^/@\s]+@', '://***@', line)  # credentials embedded in a URL
    line = re.sub(r'(?i)\b(bearer|basic)\s+\S+', r'\1 ***', line)
    return re.sub(r'(?i)(authorization:)\s+(?!bearer\b|basic\b)\S+', r'\1 ***', line)[:300]


def home_of(args):
    from .state import home
    return home(args)


def read_config(home):
    try:
        from .state import read_config as read
        return read(home)
    except ImportError:
        p = home / 'config.json'
        return json.loads(p.read_text()) if p.exists() else {}


def write_config(home, value):
    try:
        from .state import write_config as write
        return write(home, value)
    except ImportError:
        write_json(home / 'config.json', value)


def write_json(path, value):
    from .state import write_json as atomic_write
    atomic_write(path, value)


def mark(home, name, info):
    try:
        from .state import mark_step
        mark_step(home, name, info)
    except ImportError:
        p = home / 'state.json'
        s = json.loads(p.read_text()) if p.exists() else {}
        s.setdefault('steps', {})[name] = {'at': time.time(), 'info': info}
        write_json(p, s)


def descriptor(home):
    try:
        from .descriptor import load
        return load(home, fetch=True)
    except ImportError:
        p = home / 'releases/current.json'
        if not p.exists():
            p = pathlib.Path(__file__).resolve().parent.parent / 'releases/current.json'
        return json.loads(p.read_text())


def human(command, action, why, steps, link=None):
    return Result(command, 'needs_human', action, needs_human={
        'action': action, 'why': why, 'steps': steps,
        'cost': {'kind': 'none', 'note': 'No payment is performed by reaxctl.'}, 'link': link})


def pool_of(d, name):
    return next((p for p in d['pools'] if p['id'] == name), None)


def gate(d, mode, confirm=False):
    status = d['status']
    uid = status.get('testnet_netuid') if mode == 'testnet' else status.get('netuid')
    if mode != 'localnet' and (not status.get('live') or uid is None):
        return Result('install', 'blocked', 'The release descriptor does not mark REAX as live. Real-network modes require live status and their own configured netuid.', exit_code=12,
                      next=[{'command': 'reaxctl plan', 'why': 'Review what you can prepare today.'}])
    if mode == 'mainnet' and not confirm:
        return human('install', 'Confirm the mainnet setup.', 'A human-approved mainnet flow is required.',
                     ['Repeat install with --confirm-mainnet after human approval.'])


def prepare_source(home, d):
    from .descriptor import validate
    validate(d)
    provided = os.environ.get('REAX_SOURCE_DIR') or read_config(home).get('source_dir')
    if provided:
        source = pathlib.Path(provided).expanduser().resolve()
        if not (source / 'scripts/localnet/start.sh').is_file():
            return None, Result('install', 'blocked', 'REAX_SOURCE_DIR is not a subnet checkout.', exit_code=10)
        if not d['source'].get('public'):
            return source, None
        if not (source / '.git').exists():
            return None, Result('install', 'blocked', 'Public source override needs a verifiable Git checkout.', exit_code=10)
    elif not d['source'].get('public'):
        return None, Result('install', 'blocked', 'The release descriptor does not mark the source as available; provide REAX_SOURCE_DIR for rehearsal.', exit_code=12)
    else:
        source = home / 'source'
    if not (source / '.git').exists():
        rc, _, err = runner(['git', 'clone', '--no-checkout', '--', d['source']['git'], str(source)], timeout=300)
        if rc:
            return None, Result('install', 'error', 'Source download failed.', data={'detail': detail(err)}, exit_code=13)
    rc, origin, _ = runner(['git', '-C', str(source), 'config', '--get', 'remote.origin.url'])  # raw value: `remote get-url` applies insteadOf rewrites
    if rc or origin.strip() != d['source']['git']:
        return None, Result('install', 'blocked', 'Existing source origin differs from descriptor.', exit_code=10)
    rc, _, err = runner(['git', '-C', str(source), 'fetch', '--tags', 'origin'])
    if rc:
        return None, Result('install', 'error', 'Source tag fetch failed.', data={'detail': detail(err)}, exit_code=13)
    rc, _, err = runner(['git', '-C', str(source), 'checkout', '--detach', d['source']['ref'], '--'])
    if rc:
        return None, Result('install', 'error', 'Pinned source checkout failed.', data={'detail': detail(err)}, exit_code=13)
    if d['source'].get('commit'):
        rc, commit, _ = runner(['git', '-C', str(source), 'rev-parse', 'HEAD'])
        if rc or commit.strip().lower() != d['source']['commit'].lower():
            return None, Result('install', 'blocked', 'Checked-out source commit differs from descriptor.', exit_code=10)
    return source, None


def prepare_tools(home, d):
    from .descriptor import validate
    validate(d)
    py = home / 'venv/bin/python'
    # A venv created without ensurepip has a python but no pip; recreate it once venv support exists.
    if not py.exists() or not (home / 'venv/bin/pip').exists():
        if (home / 'venv').is_symlink():  # never empty a directory the kit does not own
            return Result('install', 'blocked', '{} is a symlink; remove it and retry install.'.format(home / 'venv'), exit_code=10)
        rc, _, _ = runner([sys.executable, '-I', '-m', 'venv', '--clear', str(home / 'venv')])
        if rc:
            return Result('install', 'blocked', 'Python venv support is missing. On Debian/Ubuntu run: sudo apt-get install -y python3-venv, then retry install.', exit_code=10)
    specs = [k + (v if v.startswith(('=', '<', '>', '~', '!')) else '==' + v) for k, v in d.get('tools', {}).items()]
    # pip's resolver verifies descriptor constraints even when the venv predates this tool.
    check = "import importlib.metadata as m; from packaging.requirements import Requirement; import sys; rs=[Requirement(x) for x in sys.argv[1:]]; sys.exit(0 if all(m.version(r.name) in r.specifier for r in rs) else 1)"
    rc, _, _ = runner([str(py), '-I', '-c', check] + specs)
    if rc:
        rc, _, err = runner([str(py), '-I', '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple'] + specs, timeout=900)
        if rc:
            return Result('install', 'error', 'Pinned tool installation failed; retry install.', data={'detail': detail(err)}, exit_code=13)
    mark(home, 'tools', {'requirements': specs})


def image_ref(d, key):
    images = d['images']
    digest = (images.get('digests') or {}).get(key)
    if digest:
        return images[key] + '@' + digest
    return images[key] + ':' + (images.get('tag') or d['release_id'])


def render(home, d, config, source=None):
    from .descriptor import validate
    validate(d)
    pool = pool_of(d, config['pool'])
    q = lambda value: json.dumps(str(value).replace("$", "$$"))  # JSON quoted scalars are valid YAML scalars.
    key = pool['id'].replace('-', '_') + '_runtime'
    private = not d['images'].get('public')
    uid = '{}:{}'.format(os.getuid(), os.getgid())
    token = str(home / 'secrets/runtime-token')
    wallet = pathlib.Path(os.environ.get('REAX_WALLET_DIR') or config.get('wallet_path', '~/.bittensor/wallets')).expanduser() / config.get('wallet_name', 'reax')
    hotkey = config.get('hotkey_name', 'miner1')
    import re
    if any(not re.fullmatch(r'[A-Za-z0-9_.-]+', str(x)) or x in ('.', '..') for x in (config.get('wallet_name', 'reax'), hotkey)):
        raise ValueError('Wallet and hotkey names must be single safe path components')
    port = config.get('miner_port', 8091)
    if type(port) is not int or not 1024 <= port <= 65535 or port in (8101, 8102, 8000):
        raise ValueError('miner_port must be an integer 1024–65535 excluding sidecar ports')
    if config.get('external_ip'):
        import ipaddress
        try:
            address = ipaddress.ip_address(config['external_ip'])
            valid_ip = address.is_global and not address.is_multicast and '%' not in str(address)
        except (ValueError, TypeError):
            valid_ip = False
        if not valid_ip:
            raise ValueError('external_ip must be a global IPv4 or IPv6 literal')
    services = ['services:']
    for name, imagekey in [('runtime', key), ('miner', 'miner')]:
        services += ['  ' + name + ':', '    image: ' + q(image_ref(d, imagekey)), '    user: ' + q(uid),
                     '    init: true', '    network_mode: host', '    restart: unless-stopped', '    cap_drop: [ALL]', '    security_opt: ["no-new-privileges:true"]', '    logging:', '      driver: json-file', '      options:', '        max-size: "10m"', '        max-file: "3"']
        if private and source:
            services += ['    build:', '      context: ' + q(str(source)), '      dockerfile: ' + q('Dockerfile' if name == 'miner' else 'runtimes/' + pool['id'] + '/Dockerfile')]
        if name == 'runtime':
            services += ['    environment:', '      S1M_INTERNAL_TOKEN_FILE: /run/runtime-token',
                         '      ' + ('RUNTIME_HOST' if pool['id'] == 's1-fast' else 'RUNE_HOST') + ': 127.0.0.1',
                         '      ' + ('RUNTIME_PORT' if pool['id'] == 's1-fast' else 'RUNE_PORT') + ': ' + q(str(pool['sidecar_port'])),
                         '    volumes:', '      - ' + q(token + ':/run/runtime-token:ro'),
                         '      - ' + q(str(home / 'weights' / pool['id']) + ':/model:ro'),
                         '    deploy:', '      resources:', '        reservations:',
                         '          devices:', '            - driver: nvidia', '              count: 1', '              capabilities: [gpu]']
        else:
            services += ['    env_file: ' + q(str(home / 'miner.env')),
                         '    depends_on:', '      runtime:', '        condition: service_healthy',
                         '    volumes:', '      - ' + q(token + ':/run/runtime-token:ro'),
                         '      - ' + q(str(wallet / 'hotkeys' / hotkey) + ':/wallets/' + config.get('wallet_name', 'reax') + '/hotkeys/' + hotkey + ':ro'),
                         '      - ' + q(str(wallet / 'coldkeypub.txt') + ':/wallets/' + config.get('wallet_name', 'reax') + '/coldkeypub.txt:ro'),
                         '    ulimits:', '      nofile: {soft: 65536, hard: 65536}']
    from .state import write_text
    write_text(home / 'compose.yml', '\n'.join(services) + '\n', 0o644)
    values = {'REAX_MECHANISM': 's1', 'REAX_MODEL': pool['id'], 'REAX_NETWORK': config['network'],
              'REAX_NETUID': config['netuid'], 'REAX_WALLET_PATH': '/wallets',
              'REAX_WALLET_NAME': config.get('wallet_name', 'reax'), 'REAX_WALLET_HOTKEY': hotkey,
              'REAX_RUNTIME_TOKEN_FILE': '/run/runtime-token', 'REAX_PUBLISH_COMMITMENT': '1',
              'REAX_' + pool['id'].upper().replace('-', '_') + '_RUNTIME_URL': 'http://127.0.0.1:' + str(pool['sidecar_port']),
              'REAX_EXTERNAL_IP': config.get('external_ip', ''), 'REAX_MINER_PORT': config.get('miner_port', 8091),
              'REAX_EXTERNAL_PORT': config.get('miner_port', 8091)}
    if any('\n' in str(v) or '\r' in str(v) for v in values.values()):
        raise ValueError('Environment values cannot contain newlines')
    envfile = home / 'miner.env'
    from .state import write_text
    write_text(envfile, '\n'.join(k + '=' + str(v).replace('$', '$$') for k, v in values.items()) + '\n')


def install(args):
    home = home_of(args)
    d = descriptor(args)
    config = read_config(home)
    mode = getattr(args, 'mode', None) or config.get('mode', 'localnet')
    failure = gate(d, mode, getattr(args, 'confirm_mainnet', False))
    if failure:
        return failure
    minimum_python = tuple(int(x) for x in d.get('requirements', {}).get('python_min', '3.10').split('.'))
    if sys.version_info[:2] < minimum_python:
        return Result('install', 'blocked', 'Run install with Python {} or newer for the miner tools.'.format('.'.join(map(str, minimum_python))), exit_code=10)
    if mode == 'localnet' and not hasattr(tarfile, 'data_filter'):
        from .cmd_doctor import TARFILE_MESSAGE, TARFILE_FIX
        return Result('install', 'blocked', TARFILE_MESSAGE + ' ' + TARFILE_FIX, exit_code=10)
    name = getattr(args, 'pool', None) or config.get('pool', 's1-fast')
    pool = pool_of(d, name)
    if not pool:
        return Result('install', 'error', 'Unknown pool.', exit_code=2)
    if 'manifest_id' in config and config['manifest_id'] != pool['manifest_id'] and not getattr(args, 'accept_manifest_change', False):
        return human('install', 'Approve the manifest change.', 'A changed manifest resets the miner score.', ['Review and repeat with --accept-manifest-change.'])
    if mode != 'localnet' and not pool.get('enabled_at_launch', False):
        return Result('install', 'blocked', 'This pool is not enabled.', exit_code=12)
    ancestor = home
    while not ancestor.exists():
        ancestor = ancestor.parent
    required = 1 if mode == 'localnet' else d['requirements']['disk_gb'][name]
    if shutil.disk_usage(str(ancestor)).free < required * 1024 ** 3:
        return Result('install', 'blocked', 'Free at least {} GB before installing.'.format(required), exit_code=10)
    if mode != 'localnet':
        for cmd in (['docker', 'info'], ['docker', 'compose', 'version']):
            if runner(cmd)[0]:
                return Result('install', 'blocked', 'Docker daemon and compose plugin must be available.', exit_code=10)
        if not d['images'].get('public') and not os.environ.get('REAX_SOURCE_DIR'):
            return Result('install', 'blocked', 'Release images are not yet marked available; supply REAX_SOURCE_DIR for a local build.', exit_code=12)
    if (mode == 'localnet' or not d['images'].get('public')) and not (os.environ.get('REAX_SOURCE_DIR') or read_config(home).get('source_dir') or d['source'].get('public')):
        missing = 'source' if mode == 'localnet' else 'source and images'
        return Result('install', 'blocked', 'The release descriptor does not yet mark the REAX {} as public, so this cannot run until launch unless REAX_SOURCE_DIR points to a subnet checkout you have reviewed. Doctor, plan and wallet guidance work today; see https://reax.dev/mine/ for status.'.format(missing),
                      exit_code=12, next=[{'command': 'reaxctl doctor', 'why': 'Check this machine.'}])
    from .state import private_dir
    private_dir(home)
    failure = prepare_tools(home, d)
    if failure:
        return failure
    source = None
    if mode == 'localnet' or not d['images'].get('public'):
        source, failure = prepare_source(home, d)
        if failure:
            return failure
        mark(home, 'source', {'path': str(source), 'ref': d['source']['ref']})
    config.update(mode=mode, pool=name, release_id=d['release_id'], manifest_id=pool['manifest_id'])
    if mode == 'mainnet':
        config['mainnet_confirmed'] = True
    config.setdefault('wallet_name', 'reax')
    config.setdefault('hotkey_name', 'miner1')
    config.setdefault('miner_port', 8091)
    config['network'] = 'local' if mode == 'localnet' else ('test' if mode == 'testnet' else d['status']['network'])
    config['netuid'] = None if mode == 'localnet' else d['status']['testnet_netuid' if mode == 'testnet' else 'netuid']
    if source:
        config['source_dir'] = str(source)
    secret = home / 'secrets/runtime-token'
    private_dir(secret.parent)
    if secret.is_symlink():
        return Result('install', 'blocked', 'Runtime token path must not be a symbolic link.', exit_code=10)
    if not secret.exists():
        fd = os.open(str(secret), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_hex(32) + '\n')
    fd = os.open(str(secret), os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    (home / 'logs').mkdir(exist_ok=True)
    write_config(home, config)
    if mode != 'localnet':
        render(home, d, config, source)
        rc, _, _ = runner(['docker', 'compose', '-f', str(home / 'compose.yml'), 'pull' if d['images'].get('public') else 'build'], timeout=1800)
        if rc:
            return Result('install', 'error', 'Image download or build failed.', exit_code=13)
        mark(home, 'images', {'images': d['images']})
        target = home / 'weights' / name
        target.mkdir(parents=True, exist_ok=True)
        script = 'from huggingface_hub import snapshot_download; import sys; snapshot_download(repo_id=sys.argv[1],revision=sys.argv[2],local_dir=sys.argv[3])'
        # own HF cache dir: an inherited HF_HOME can be unwritable for this user (found on a real GPU host: xet cache permission denied)
        hf_env = dict(os.environ, HF_HOME=str(home / 'hf-cache'))
        rc, _, hf_err = runner([str(home / 'venv/bin/python'), '-I', '-c', script, pool['weights']['repo'], pool['weights']['revision'], str(target)], timeout=7200, env=hf_env)
        if rc:
            if pool['weights'].get('gated'):
                return human('install', 'Accept the weights gate with your own Hugging Face account.',
                             'The gated model requires account access; reaxctl never asks for your token.',
                             ['Accept access on the model page.', str(home / 'venv/bin/hf') + ' auth login', 'Retry reaxctl install.'],
                             'https://huggingface.co/' + pool['weights']['repo'])
            return Result('install', 'error', 'Pinned weights download failed; retry install.', data={'detail': detail(hf_err)}, exit_code=13)
        mark(home, 'weights', pool['weights'])
    mark(home, 'install', {'release_id': d['release_id'], 'mode': mode})
    return Result('install', summary='Installation prepared.', data={'home': str(home), 'mode': mode, 'token_path': str(secret)},
                  next=[{'command': 'reaxctl localnet up' if mode == 'localnet' else 'reaxctl wallet status', 'why': 'Continue setup.'}])


def register(sub):
    p = sub.add_parser('install')
    add_common(p)
    p.add_argument('--pool', default=None)
    p.add_argument('--accept-manifest-change', action='store_true')
    p.add_argument('--confirm-mainnet', action='store_true')
    p.set_defaults(func=install)
