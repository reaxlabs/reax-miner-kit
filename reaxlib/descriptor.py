"""Validated release pins; supported commands refresh from the configured descriptor by default."""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlparse
from pathlib import Path
import urllib.error
import urllib.request

from . import state

DEFAULT_URL = 'https://raw.githubusercontent.com/reaxlabs/reax-miner-kit/main/releases/current.json'
BUNDLED = Path(__file__).resolve().parent.parent / 'releases' / 'current.json'


def url():
    return os.environ.get('REAX_RELEASES_URL', DEFAULT_URL)


# A fetched descriptor is unsigned data; it may only point at code and images owned by these GitHub owners (new owners need a new kit release).
TRUSTED_OWNERS = ('reaxlabs',)
CORE_IMAGES = ('miner', 's1_fast_runtime', 's1_pro_runtime')
OPTIONAL_IMAGES = ('s1_lite_runtime', 's1_lite_runtime_jetson', 's1_cpu_runtime')
POOL_IDS = ('s1-fast', 's1-pro', 's1-lite', 's1-cpu')


FILE_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]*')


def validate_pool_extras(pool):
    """Strict checks for the optional tiered-pool fields; absent fields keep the s1-fast/s1-pro meaning."""
    if 'tier' in pool and pool['tier'] not in ('gpu', 'small', 'cpu'):
        raise ValueError('invalid pool tier')
    if 'arch' in pool:
        arch = pool['arch']
        if not isinstance(arch, list) or not arch or len(set(map(str, arch))) != len(arch) or any(a not in ('x86_64', 'aarch64') for a in arch):
            raise ValueError('invalid pool arch')
    if 'accelerator' in pool and pool['accelerator'] not in ('nvidia', 'none'):
        raise ValueError('invalid pool accelerator')
    for key in ('jetson', 'dev_only'):
        if key in pool and type(pool[key]) is not bool:
            raise ValueError('invalid pool flag ' + key)
    if pool.get('jetson'):
        if pool.get('accelerator', 'nvidia') != 'nvidia' or 'aarch64' not in pool.get('arch', ['x86_64']):
            raise ValueError('a jetson pool needs the nvidia accelerator and aarch64 arch')
    for key in ('min_ram_gb', 'min_cpu_cores'):
        if key in pool and (type(pool[key]) not in (int, float) or pool[key] <= 0):
            raise ValueError('invalid pool ' + key)
    if 'files' in pool['weights']:
        files = pool['weights']['files']
        if (not isinstance(files, list) or not files or len(set(map(str, files))) != len(files)
                or any(not isinstance(f, str) or not FILE_NAME.fullmatch(f) or f.endswith('.') for f in files)):
            raise ValueError('invalid weights file list')


def validate(value):
    def require(obj, keys):
        if not isinstance(obj, dict) or any(key not in obj for key in keys):
            raise ValueError('release descriptor is missing required fields')
    require(value, ['schema', 'release_id', 'status', 'minimum_release', 'images', 'source', 'pools', 'requirements', 'links', 'update'])
    if not isinstance(value['release_id'], str) or not value['release_id'] or not isinstance(value['minimum_release'], str):
        raise ValueError('invalid release identifier')
    allowed = {'schema', 'release_id', 'updated', 'status', 'minimum_release', 'images', 'source', 'pools', 'requirements', 'links', 'update', 'tools'}
    if set(value) - allowed:
        raise ValueError('unknown release descriptor fields')
    if value['schema'] != 'reax-release/1':
        raise ValueError('unsupported release descriptor schema')
    require(value['status'], ['launch_status', 'live', 'netuid', 'network', 'testnet_netuid'])
    if (value['status']['launch_status'] not in ('launching_soon', 'registered_starting_soon', 'live')
            or not isinstance(value['status']['network'], str)):
        raise ValueError('invalid launch status or network')
    if not isinstance(value['status']['live'], bool):
        raise ValueError('descriptor live must be boolean')
    for key in ('netuid', 'testnet_netuid'):
        num = value['status'][key]
        if num is not None and (type(num) is not int or num < 0):
            raise ValueError('descriptor netuid must be a nonnegative integer or null')
    require(value['images'], ['miner', 's1_fast_runtime', 's1_pro_runtime', 'tag', 'digests', 'public'])
    if not isinstance(value['images']['public'], bool):
        raise ValueError('images public must be boolean')
    require(value['source'], ['git', 'ref', 'public'])
    if not isinstance(value['source']['git'], str) or not isinstance(value['source']['ref'], str) or not isinstance(value['source']['public'], bool):
        raise ValueError('invalid source pins')
    require(value['requirements'], ['cpu_cores', 'ram_gb', 'disk_gb', 'driver_min', 'open_ports', 'never_publish_ports', 'python_min'])
    requirements = value['requirements']
    for key in ('cpu_cores', 'ram_gb'):
        if type(requirements[key]) not in (int, float) or requirements[key] <= 0:
            raise ValueError('invalid host requirements')
    if not isinstance(requirements['disk_gb'], dict):
        raise ValueError('disk requirements must be an object')
    if not isinstance(requirements['python_min'], str) or not all(part.isdigit() for part in requirements['python_min'].split('.')):
        raise ValueError('invalid Python requirement')
    for key in ('open_ports', 'never_publish_ports'):
        if not isinstance(requirements[key], list) or any(type(port) is not int or not 1 <= port <= 65535 for port in requirements[key]):
            raise ValueError('invalid ports')
    require(value['links'], ['register'])
    if not isinstance(value['links']['register'], str) or not value['links']['register'].startswith('https://'):
        raise ValueError('invalid registration link')
    require(value['update'], ['channel', 'auto_safe', 'manifest_change_requires_confirm'])
    if not isinstance(value['pools'], list) or not value['pools']:
        raise ValueError('descriptor pools must be a nonempty list')
    ids = set()
    for pool in value['pools']:
        require(pool, ['id', 'model', 'weights', 'manifest_id', 'commitment', 'gpu', 'sidecar_port', 'enabled_at_launch'])
        if not isinstance(pool['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]+', pool['id']) or pool['id'] in ids:
            raise ValueError('invalid or duplicate pool id')
        ids.add(pool['id'])
        if type(requirements['disk_gb'].get(pool['id'])) not in (int, float) or requirements['disk_gb'][pool['id']] <= 0:
            raise ValueError('missing pool disk requirement')
        require(pool['weights'], ['repo', 'revision', 'gated', 'size_gb'])
        require(pool['gpu'], ['min_vram_gb', 'count', 'vendor'])
        validate_pool_extras(pool)
        cpu_only = pool.get('accelerator', 'nvidia') == 'none'
        if cpu_only:
            if pool['gpu']['count'] != 0 or type(pool['gpu']['count']) is not int or pool['gpu']['vendor'] != 'none':
                raise ValueError('invalid GPU count or vendor')
            if type(pool['gpu']['min_vram_gb']) not in (int, float) or pool['gpu']['min_vram_gb'] != 0:
                raise ValueError('invalid GPU requirement')
        else:
            if type(pool['gpu']['count']) is not int or pool['gpu']['count'] < 1 or not isinstance(pool['gpu']['vendor'], str):
                raise ValueError('invalid GPU count or vendor')
            if type(pool['gpu']['min_vram_gb']) not in (int, float) or pool['gpu']['min_vram_gb'] <= 0:
                raise ValueError('invalid GPU requirement')
        maximum = pool['gpu'].get('max_vram_gb')
        if maximum is not None and (type(maximum) not in (int, float) or maximum < pool['gpu']['min_vram_gb']):
            raise ValueError('invalid maximum VRAM')
    def matches(pattern, item, label):
        if not isinstance(item, str) or not re.fullmatch(pattern, item):
            raise ValueError('invalid descriptor ' + label)
    matches(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value['release_id'], 'release id')
    matches(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value['minimum_release'], 'minimum release')
    for key in CORE_IMAGES + tuple(k for k in OPTIONAL_IMAGES if k in value['images']):
        matches(r'[a-z0-9][a-z0-9./_-]*', value['images'][key], 'image')
        parts = value['images'][key].split('/')
        if len(parts) < 3 or any(part in ('', '.', '..') for part in parts) or parts[0] != 'ghcr.io':
            raise ValueError('descriptor image registry must be ghcr.io')
        if parts[1] not in TRUSTED_OWNERS:
            raise ValueError('descriptor image owner is not trusted by this kit version')
    digests = value['images']['digests']
    if digests is not None:
        if not isinstance(digests, dict) or set(digests) - set(CORE_IMAGES + OPTIONAL_IMAGES):
            raise ValueError('invalid descriptor digests')
        for digest in digests.values():
            matches(r'sha256:[0-9a-fA-F]{64}', digest, 'digest')
    if value['status']['live'] and value['images']['public'] and any(not (digests or {}).get(k) for k in ('miner', 's1_fast_runtime', 's1_pro_runtime')):
        raise ValueError('live public images require sha256 digests')
    if value['images']['tag'] is not None:
        matches(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', value['images']['tag'], 'image tag')
    matches(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', value['source']['git'], 'source git URL')
    matches(r'(?:[0-9a-fA-F]{40}|v[0-9][A-Za-z0-9_.-]*)', value['source']['ref'], 'source ref (version tag or commit)')
    if value['source']['git'].split('/')[3] not in TRUSTED_OWNERS:
        raise ValueError('descriptor source owner is not trusted by this kit version')
    if 'commit' in value['source']:
        matches(r'[0-9a-fA-F]{40}', value['source']['commit'], 'source commit')
    if value['source'].get('public') and 'commit' not in value['source']:
        raise ValueError('a public source needs an exact commit')
    tools = value.get('tools', {})
    if not isinstance(tools, dict) or set(tools) - {'bittensor', 'typer', 'huggingface_hub'}:
        raise ValueError('invalid descriptor tools')
    for spec in tools.values():
        matches(r'(==|<=|<|>=|~=)?[0-9A-Za-z.*+!-]+', spec, 'tool version')
    if not {8101, 8102, 8000}.issubset(requirements['never_publish_ports']):
        raise ValueError('descriptor must protect all sidecar ports')
    for k in ('auto_safe', 'manifest_change_requires_confirm'):
        if type(value['update'][k]) is not bool:
            raise ValueError('descriptor update flags must be boolean')
    registration = urlparse(value['links']['register'])
    if registration.username or registration.password or registration.hostname not in ('reax.dev',):
        raise ValueError('invalid descriptor registration host')
    for pool in value['pools']:
        if pool['id'] not in POOL_IDS:
            raise ValueError('unsupported descriptor pool id')
        for image in ((pool['id'].replace('-', '_') + '_runtime',) + (('s1_lite_runtime_jetson',) if pool.get('jetson') else ())):
            if image not in value['images']:
                raise ValueError('release descriptor is missing required fields')
        if type(pool['sidecar_port']) is not int or pool['sidecar_port'] not in requirements['never_publish_ports']:
            raise ValueError('invalid descriptor sidecar port')
        if type(pool['enabled_at_launch']) is not bool or type(pool['weights']['gated']) is not bool:
            raise ValueError('invalid descriptor pool flags')
        matches(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+', pool['weights']['repo'], 'weights repo')
        matches(r'[0-9a-fA-F]{40}', pool['weights']['revision'], 'weights revision')
        matches(r'[A-Za-z0-9_-]+', pool['manifest_id'], 'manifest id')
        if pool['commitment'] != 'reax-s1/1:' + pool['id'] + ':' + pool['manifest_id']:
            raise ValueError('invalid descriptor commitment')
    return value


_warnings = []
_source = 'bundled'
_offline = False
_session = None


def begin(args):
    global _warnings, _offline, _session
    _warnings = []
    _session = None
    _offline = getattr(args, 'offline', False) or os.environ.get('REAX_OFFLINE') == '1'


def warnings():
    return list(dict.fromkeys(_warnings))


def current_source():
    return _source


def safe_url(address):
    parsed = urlparse(address)
    if parsed.username or parsed.password or not parsed.hostname or parsed.fragment:
        raise ValueError('invalid release URL')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1')):
        raise ValueError('release URL requires HTTPS (HTTP only on localhost)')
    return parsed


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original, target = safe_url(req.full_url), safe_url(newurl)
        if (original.scheme, original.hostname, original.port) != (target.scheme, target.hostname, target.port):
            raise ValueError('release redirect leaves origin')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def release_key(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:(?:-?rc)(\d+))?', value or '')
    return tuple(map(int, match.groups()[:3])) + ((int(match.group(4)) if match.group(4) else 1000000),) if match else None


def load(args=None, fetch=False, timeout=5):
    global _source, _session
    cache = state.home(args) / 'releases' / 'current.json'
    if _session and _session[0] == cache and fetch:
        return _session[1]
    value = None
    for path, source in ((cache, 'cache'), (BUNDLED, 'bundled')):
        try:
            value = validate(state.read_json(path))
            _source = source
            break
        except ValueError:
            if path == cache and path.exists():
                _warnings.append('Cached release validation failed; using bundled descriptor.')
            continue
    if value is None:
        raise ValueError('no valid cached or bundled release descriptor')
    fetched_at = state.read_json(cache.parent / 'fetched_at').get('at', 0)
    fresh = isinstance(fetched_at, (int, float)) and 0 <= time.time() - fetched_at < 3600
    if fetch and not (_offline or getattr(args, 'offline', False) or os.environ.get('REAX_OFFLINE') == '1') and not (_source == 'cache' and fresh):
        try:
            safe_url(url())
            opener = urllib.request.build_opener(SameHostRedirect())
            with opener.open(url(), timeout=timeout) as response:
                raw = response.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError('release response exceeds 256 KB')
            candidate = validate(json.loads(raw))
            old, new = release_key(value['release_id']), release_key(candidate['release_id'])
            if old and new and new < old:
                raise ValueError('release downgrade refused')
            if value['status']['live'] and not candidate['status']['live']:
                raise ValueError('release launch regression refused')
            state.private_dir(state.home(args))
            state.write_json(cache, candidate)
            state.write_json(cache.parent / 'fetched_at', {'at': time.time()})
            value, _source = candidate, 'fetched'
        except (OSError, ValueError, TypeError) as exc:
            reason = re.sub(r'\s+', ' ', str(exc.reason if type(exc) is urllib.error.URLError else exc))[:120]
            _warnings.append('Release fetch or validation failed (' + reason + '); using validated ' + _source + ' descriptor.')
    installed = state.load_config(args).get('release_id')
    old, minimum = release_key(installed), release_key(value['minimum_release'])
    if old and minimum and old < minimum:
        _warnings.append('Installed release is older than minimum_release; review an update.')
    if fetch:
        _session = (cache, value)
    return value


load_descriptor = load
