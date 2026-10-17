"""Several runtime backends (one sidecar per GPU) behind one hotkey. Needs miner release 1.2.0rc8 or later."""
import ipaddress
import os
import re
from .descriptor import release_key

MIN_RELEASE = '1.2.0rc8'
POOLS = ('s1-fast', 's1-pro')  # the miner refuses URL lists for s1-lite and s1-cpu
PORT_STEP = 10  # backend i listens on sidecar_port + 10 * i (8101, 8111, 8121 ...)
MAX_BACKENDS = 16
PRIVATE = [ipaddress.ip_network(n) for n in ('127.0.0.0/8', '::1/128', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7')]


def private_host(host):
    """Loopback or RFC1918/ULA literal, the only addresses the miner accepts without REAX_ALLOW_PUBLIC_RUNTIME."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in net for net in PRIVATE if net.version == address.version)


def service(i):
    return 'runtime' if i == 0 else 'runtime-{}'.format(i)


def services(config):
    return [service(i) for i in range(len(config.get('backends') or [None]))]


def release_ok(release_id):
    return bool(release_key(release_id)) and release_key(release_id) >= release_key(MIN_RELEASE)


def plan(pool, gpus, bind='127.0.0.1', token_files=None):
    """The backend list stored in the config; raises ValueError with a message for the operator."""
    if pool['id'] not in POOLS:
        raise ValueError('Several backends are only supported for s1-fast and s1-pro (the miner refuses URL lists for s1-lite and s1-cpu).')
    ids = [x.strip() for x in str(gpus).split(',')]
    if not ids or any(not re.fullmatch(r'\d{1,3}', x) for x in ids) or len(set(ids)) != len(ids) or len(ids) > MAX_BACKENDS:
        raise ValueError('--gpus must be a comma list of distinct GPU indexes from nvidia-smi -L, for example 0,1 (at most {}).'.format(MAX_BACKENDS))
    if not private_host(bind):
        raise ValueError('--backend-bind must be a loopback or private-network IP literal (127.0.0.1, 10.x, 172.16-31.x, 192.168.x); sidecar ports are never published.')
    files = [x.strip() for x in token_files.split(',')] if token_files else []
    if files and len(files) != len(ids):
        raise ValueError('--backend-token-files needs exactly one file per GPU, in the same order.')
    out = []
    for i, gpu in enumerate(ids):
        port = pool['sidecar_port'] + PORT_STEP * i
        if port > 65535:
            raise ValueError('Too many backends for the port range.')
        entry = {'gpu': gpu, 'bind': bind, 'port': port}
        if files:
            path = os.path.abspath(os.path.expanduser(files[i]))
            if any(c in path for c in ':,|\n\r"') or os.path.islink(path) or not os.path.isfile(path):
                raise ValueError('Backend token file {} must be an existing regular file (not a link) without : , | in its path.'.format(files[i]))
            if os.stat(path).st_mode & 0o077:
                raise ValueError('Backend token file {} must not be readable by group or others (chmod 600).'.format(files[i]))
            entry['token_file'] = path
        out.append(entry)
    if len({e.get('token_file') for e in out} - {None}) not in (0, len(out)):
        raise ValueError('Backend token files must be distinct files.')
    return out


def token_target(i):
    return '/run/backend-token-{}'.format(i)


def urls(backends):
    """The REAX_<MODEL>_RUNTIME_URLS value: URL or URL|TOKEN_FILE (the path inside the miner container)."""
    out = []
    for i, b in enumerate(backends):
        host = '[{}]'.format(b['bind']) if ':' in b['bind'] else b['bind']
        out.append('http://{}:{}'.format(host, b['port']) + ('|' + token_target(i) if b.get('token_file') else ''))
    return ','.join(out)


def ports(config):
    return {b['port'] for b in config.get('backends') or []}
