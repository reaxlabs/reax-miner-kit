"""Model catalog: one entry per mineable model.

Today's pools (`pools[]` of the release descriptor) and the per-model pots of the portfolio (`models[]`, optional)
are read through one view, so `models`, `up` and `switch` treat them the same. A new model is a new `models[]`
entry in the descriptor, not new code. See releases/README.md, "Models (per-model pots)".
"""
from __future__ import annotations

import re

STATUSES = ('live', 'coming', 'retired')
MODEL_ID = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
POT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
ENV_KEY = re.compile(r'[A-Z][A-Z0-9_]*')
ENV_VALUE = re.compile(r'[A-Za-z0-9_./:-]*')
PLACEHOLDER = re.compile(r'(?:sha256:)?TBD-[A-Za-z0-9_-]+')
DIGEST = re.compile(r'sha256:[0-9a-fA-F]{64}')
REVISION = re.compile(r'[0-9a-fA-F]{40}')
MANIFEST_LIVE = re.compile(r'[0-9a-f]{16}')
MANIFEST_ANY = re.compile(r'(?:[0-9a-f]{16}|TBD-[A-Za-z0-9_]+)')
IMAGE = re.compile(r'[a-z0-9][a-z0-9./_-]*')
WEIGHT_REPO = re.compile(r'[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+')
CACHE_ID = re.compile(r'[a-z0-9][a-z0-9.-]{0,63}')
CACHE_PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*')
PACKAGED_RUNTIME = 'model-runtime/1'
RAM_TOLERANCE = 0.9  # nominal RAM sizes: an "8 GB" board reports about 7.4 GiB

MODEL_KEYS = {'id', 'name', 'pot', 'status', 'weights', 'runtime', 'gpu', 'manifest_id', 'commitment', 'disk_gb',
              'unit_throughput', 'min_ram_gb', 'min_cpu_cores', 'arch', 'accelerator', 'tier', 'jetson', 'miner_env', 'notes'}
WEIGHT_KEYS = {'repo', 'revision', 'gated', 'size_gb', 'files', 'licence', 'format', 'cache_id', 'dependencies'}
RUNTIME_KEYS = {'image', 'digest', 'port', 'env', 'host_var', 'port_var', 'contract'}
GPU_KEYS = {'class', 'min_vram_gb', 'max_vram_gb', 'count', 'vendor', 'measured', 'unmeasured_note'}
THROUGHPUT_KEYS = {'value', 'unit', 'per', 'note'}
# Set by the kit itself in the sidecar environment; a descriptor must not override them.
RESERVED_ENV = {'S1M_INTERNAL_TOKEN_FILE', 'GGUF_MODEL_DIR', 'GGUF_N_GPU_LAYERS'}


def is_placeholder(value):
    return isinstance(value, str) and bool(PLACEHOLDER.fullmatch(value))


def _number(value, positive=True):
    return type(value) in (int, float) and (value > 0 if positive else value >= 0)


def _env(env, label, reserved=()):
    if not isinstance(env, dict):
        raise ValueError(label + ' must be an object')
    for key, value in env.items():
        if not isinstance(key, str) or not ENV_KEY.fullmatch(key) or not isinstance(value, str) or not ENV_VALUE.fullmatch(value):
            raise ValueError('invalid ' + label)
        if key in reserved:
            raise ValueError(label + ' sets a variable the kit controls: ' + key)


def _cache_entry(item):
    cache_id = item.get('cache_id')
    if not isinstance(cache_id, str) or not CACHE_ID.fullmatch(cache_id):
        raise ValueError('invalid packaged weights cache id')
    files = item.get('files')
    if (not isinstance(files, list) or not files or any(
            not isinstance(path, str) or not CACHE_PATH.fullmatch(path)
            or any(part in ('.', '..') for part in path.split('/')) for path in files)
            or len(set(files)) != len(files)):
        raise ValueError('invalid packaged weights file list')


def validate_model(release, model, seen_ids, seen_ports):
    """Strict checks for one `models[]` entry; raises ValueError like descriptor.validate."""
    from .descriptor import TRUSTED_OWNERS  # lazy: descriptor imports this module
    if not isinstance(model, dict):
        raise ValueError('descriptor model must be an object')
    missing = {'id', 'name', 'pot', 'status', 'weights', 'runtime', 'gpu', 'manifest_id', 'commitment', 'disk_gb'} - set(model)
    if missing:
        raise ValueError('descriptor model is missing required fields')
    if set(model) - MODEL_KEYS:
        raise ValueError('unknown descriptor model fields')
    mid = model['id']
    if not isinstance(mid, str) or not MODEL_ID.fullmatch(mid) or mid in seen_ids:
        raise ValueError('invalid or duplicate model id')
    seen_ids.add(mid)
    if not isinstance(model['name'], str) or not model['name'] or len(model['name']) > 80 or '\n' in model['name']:
        raise ValueError('invalid model name')
    if not isinstance(model['pot'], str) or not POT_ID.fullmatch(model['pot']):
        raise ValueError('invalid model pot id')
    if model['status'] not in STATUSES:
        raise ValueError('invalid model status')
    live = model['status'] == 'live'
    if not _number(model['disk_gb']):
        raise ValueError('invalid model disk requirement')
    # weights
    weights = model['weights']
    if not isinstance(weights, dict) or {'repo', 'revision', 'gated', 'size_gb'} - set(weights) or set(weights) - WEIGHT_KEYS:
        raise ValueError('invalid model weights')
    if not isinstance(weights['repo'], str) or not WEIGHT_REPO.fullmatch(weights['repo']):
        raise ValueError('invalid model weights repo')
    if not (isinstance(weights['revision'], str) and (REVISION.fullmatch(weights['revision']) or (not live and PLACEHOLDER.fullmatch(weights['revision'])))):
        raise ValueError('invalid model weights revision (a live model needs a 40-hex revision)')
    if type(weights['gated']) is not bool or not _number(weights['size_gb']):
        raise ValueError('invalid model weights flags')
    runtime = model['runtime']
    packaged = isinstance(runtime, dict) and runtime.get('contract') == PACKAGED_RUNTIME
    if packaged:
        if weights['gated']:
            raise ValueError('packaged model weights must be publicly downloadable')
        _cache_entry(weights)
        ids = {weights['cache_id']}
        dependencies = weights.get('dependencies', [])
        if not isinstance(dependencies, list):
            raise ValueError('invalid packaged weights dependencies')
        for dep in dependencies:
            if (not isinstance(dep, dict) or set(dep) != {'cache_id', 'repo', 'revision', 'files'}
                    or not isinstance(dep['repo'], str) or not WEIGHT_REPO.fullmatch(dep['repo'])
                    or not isinstance(dep['revision'], str) or not REVISION.fullmatch(dep['revision'])):
                raise ValueError('invalid packaged weights dependency pin')
            _cache_entry(dep)
            if dep['cache_id'] in ids:
                raise ValueError('duplicate packaged weights cache id')
            ids.add(dep['cache_id'])
    elif set(weights) & {'cache_id', 'dependencies'}:
        raise ValueError('packaged weights fields require the packaged runtime contract')
    elif 'files' in weights:
        files = weights['files']
        from .descriptor import FILE_NAME
        if not isinstance(files, list) or not files or len(set(map(str, files))) != len(files) or any(not isinstance(f, str) or not FILE_NAME.fullmatch(f) or f.endswith('.') for f in files):
            raise ValueError('invalid model weights file list')
    for key in ('licence', 'format'):
        if key in weights and (not isinstance(weights[key], str) or not re.fullmatch(r'[A-Za-z0-9 ._+/-]{1,60}', weights[key])):
            raise ValueError('invalid model weights ' + key)
    # runtime sidecar
    if not isinstance(runtime, dict) or {'image', 'digest', 'port'} - set(runtime) or set(runtime) - RUNTIME_KEYS:
        raise ValueError('invalid model runtime')
    if 'contract' in runtime and not packaged:
        raise ValueError('unknown model runtime contract')
    if packaged and (runtime.get('env') or 'host_var' in runtime or 'port_var' in runtime):
        raise ValueError('packaged runtime uses its built-in settings')
    image = runtime['image']
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise ValueError('invalid descriptor image')
    parts = image.split('/')
    if len(parts) < 3 or any(part in ('', '.', '..') for part in parts) or parts[0] != 'ghcr.io':
        raise ValueError('descriptor image registry must be ghcr.io')
    if parts[1] not in TRUSTED_OWNERS:
        raise ValueError('descriptor image owner is not trusted by this kit version')
    digest = runtime['digest']
    if not (isinstance(digest, str) and (DIGEST.fullmatch(digest) or (not live and PLACEHOLDER.fullmatch(digest)))):
        raise ValueError('invalid model runtime digest (a live model needs a sha256 digest)')
    port = runtime['port']
    if type(port) is not int or port not in release['requirements']['never_publish_ports']:
        raise ValueError('invalid model sidecar port (it must be listed in never_publish_ports)')
    if port in seen_ports:
        raise ValueError('duplicate sidecar port')
    seen_ports.add(port)
    _env(runtime.get('env', {}), 'model runtime environment', RESERVED_ENV)
    for key in ('host_var', 'port_var'):
        if key in runtime and (not isinstance(runtime[key], str) or not ENV_KEY.fullmatch(runtime[key])):
            raise ValueError('invalid model runtime ' + key)
    # hardware
    gpu = model['gpu']
    if not isinstance(gpu, dict) or {'min_vram_gb', 'count', 'vendor'} - set(gpu) or set(gpu) - GPU_KEYS:
        raise ValueError('invalid model gpu')
    cpu_only = model.get('accelerator', 'nvidia') == 'none'
    if model.get('accelerator', 'nvidia') not in ('nvidia', 'none'):
        raise ValueError('invalid model accelerator')
    if cpu_only:
        if gpu['count'] != 0 or type(gpu['count']) is not int or gpu['vendor'] != 'none' or type(gpu['min_vram_gb']) not in (int, float) or gpu['min_vram_gb'] != 0:
            raise ValueError('invalid model GPU requirement')
    elif type(gpu['count']) is not int or gpu['count'] < 1 or not isinstance(gpu['vendor'], str) or not _number(gpu['min_vram_gb']):
        raise ValueError('invalid model GPU requirement')
    if gpu.get('max_vram_gb') is not None and (not _number(gpu['max_vram_gb']) or gpu['max_vram_gb'] < gpu['min_vram_gb']):
        raise ValueError('invalid maximum VRAM')
    if 'class' in gpu and (not isinstance(gpu['class'], str) or not re.fullmatch(r'[A-Za-z0-9 ._+-]{1,40}', gpu['class'])):
        raise ValueError('invalid model GPU class')
    if 'measured' in gpu and (not isinstance(gpu['measured'], list) or any(not isinstance(x, str) for x in gpu['measured'])):
        raise ValueError('invalid model measured GPU list')
    if 'tier' in model and model['tier'] not in ('gpu', 'small', 'cpu'):
        raise ValueError('invalid model tier')
    if 'arch' in model:
        arch = model['arch']
        if not isinstance(arch, list) or not arch or len(set(map(str, arch))) != len(arch) or any(a not in ('x86_64', 'aarch64') for a in arch):
            raise ValueError('invalid model arch')
    if 'jetson' in model and type(model['jetson']) is not bool:
        raise ValueError('invalid model jetson flag')
    for key in ('min_ram_gb', 'min_cpu_cores'):
        if key in model and not _number(model[key]):
            raise ValueError('invalid model ' + key)
    # unit throughput: what one unit (one GPU of the stated class) is expected to serve; informational for miners
    if 'unit_throughput' in model:
        unit = model['unit_throughput']
        if not isinstance(unit, dict) or set(unit) - THROUGHPUT_KEYS or 'unit' not in unit or not isinstance(unit['unit'], str) or not re.fullmatch(r'[a-z_]{1,24}', unit['unit']):
            raise ValueError('invalid model unit throughput')
        if unit.get('value') is not None and not _number(unit['value']):
            raise ValueError('invalid model unit throughput value')
        if 'per' in unit and unit['per'] not in ('gpu', 'machine'):
            raise ValueError('invalid model unit throughput basis')
        if 'note' in unit and (not isinstance(unit['note'], str) or len(unit['note']) > 200):
            raise ValueError('invalid model unit throughput note')
    # chain identity
    manifest = model['manifest_id']
    if not isinstance(manifest, str) or not (MANIFEST_LIVE.fullmatch(manifest) if live else MANIFEST_ANY.fullmatch(manifest)):
        raise ValueError('invalid model manifest id (a live model needs a 16-hex manifest)')
    if model['commitment'] != 'reax-s1/1:' + mid + ':' + manifest:
        raise ValueError('invalid descriptor commitment')
    if 'miner_env' in model:
        _env(model['miner_env'], 'model miner environment')
        if any(not k.startswith('REAX_') for k in model['miner_env']):
            raise ValueError('model miner environment keys must start with REAX_')
    if 'notes' in model and (not isinstance(model['notes'], str) or len(model['notes']) > 400):
        raise ValueError('invalid model notes')


def validate_models(release):
    """Validate the optional top-level `models` list of a descriptor (called from descriptor.validate)."""
    entries = release.get('models')
    if entries is None:
        return
    if not isinstance(entries, list):
        raise ValueError('descriptor models must be a list')
    ids = {p['id'] for p in release['pools']}
    ports = {p['sidecar_port'] for p in release['pools']}
    for model in entries:
        validate_model(release, model, ids, ports)  # ids/ports start with the pools': a model never reuses a pool id or port


def validate_pool_catalog_fields(pool):
    """Optional catalog fields of a legacy pool: pot, status, unit_throughput."""
    if 'status' in pool and pool['status'] not in STATUSES:
        raise ValueError('invalid pool status')
    if 'pot' in pool and (not isinstance(pool['pot'], str) or not POT_ID.fullmatch(pool['pot'])):
        raise ValueError('invalid pool pot id')
    if 'unit_throughput' in pool:
        unit = pool['unit_throughput']
        if not isinstance(unit, dict) or set(unit) - THROUGHPUT_KEYS or not isinstance(unit.get('unit'), str) or (unit.get('value') is not None and not _number(unit['value'])):
            raise ValueError('invalid pool unit throughput')


# ----------------------------------------------------------------------------- catalog


def _from_pool(release, pool):
    from .cmd_install import runtime_image_key
    from .descriptor import pool_enabled
    key = runtime_image_key(pool)
    digest = (release['images'].get('digests') or {}).get(key)
    status = pool.get('status') or ('live' if pool_enabled(pool) else 'coming')
    gpu = dict(pool['gpu'])
    runtime = pool.get('runtime') or {}
    return {'id': pool['id'], 'name': pool['model'], 'pot': pool.get('pot', pool['id']), 'status': status, 'kind': 'pool',
            'weights': dict(pool['weights']), 'gpu': gpu, 'accelerator': pool.get('accelerator', 'nvidia'),
            'arch': list(pool.get('arch', ['x86_64'])), 'tier': pool.get('tier', 'gpu'), 'jetson': bool(pool.get('jetson')),
            'min_ram_gb': pool.get('min_ram_gb'), 'min_cpu_cores': pool.get('min_cpu_cores'),
            'disk_gb': release['requirements']['disk_gb'][pool['id']], 'manifest_id': pool['manifest_id'],
            'commitment': pool['commitment'], 'unit_throughput': pool.get('unit_throughput'),
            'runtime': {'image': release['images'][key], 'digest': digest, 'port': pool['sidecar_port'], 'env': dict(runtime.get('env') or {})},
            'dev_only': bool(pool.get('dev_only'))}


def _from_model(release, model):
    gpu = dict(model['gpu'])
    runtime = model['runtime']
    return {'id': model['id'], 'name': model['name'], 'pot': model['pot'], 'status': model['status'], 'kind': 'model',
            'weights': dict(model['weights']), 'gpu': gpu, 'accelerator': model.get('accelerator', 'nvidia'),
            'arch': list(model.get('arch', ['x86_64'])), 'tier': model.get('tier', 'gpu'), 'jetson': bool(model.get('jetson')),
            'min_ram_gb': model.get('min_ram_gb'), 'min_cpu_cores': model.get('min_cpu_cores'), 'disk_gb': model['disk_gb'],
            'manifest_id': model['manifest_id'], 'commitment': model['commitment'], 'unit_throughput': model.get('unit_throughput'),
            'runtime': {'image': runtime['image'], 'digest': runtime['digest'], 'port': runtime['port'], 'env': dict(runtime.get('env') or {}),
                        'host_var': runtime.get('host_var', 'RUNTIME_HOST'), 'port_var': runtime.get('port_var', 'RUNTIME_PORT')},
            'miner_env': dict(model.get('miner_env') or {}), 'notes': model.get('notes'), 'dev_only': False}


def catalog(release):
    """All models of a descriptor: the legacy pools first, then `models[]`."""
    return [_from_pool(release, p) for p in release['pools']] + [_from_model(release, m) for m in release.get('models') or []]


def get(release, model_id):
    return next((e for e in catalog(release) if e['id'] == model_id), None)


def model_entry(release, model_id):
    """The raw `models[]` entry (None for a legacy pool)."""
    return next((m for m in release.get('models') or [] if m['id'] == model_id), None)


def pool_view(release, model_id):
    """A pool-shaped dict for a `models[]` entry so install/render/verify/update work unchanged; None for unknown ids."""
    model = model_entry(release, model_id)
    if model is None:
        return None
    runtime = model['runtime']
    ref = None
    if not is_placeholder(runtime['digest']):
        ref = runtime['image'] + '@' + runtime['digest']
    view = {'id': model['id'], 'model': model['name'], 'weights': dict(model['weights']), 'manifest_id': model['manifest_id'],
            'commitment': model['commitment'], 'gpu': dict(model['gpu']), 'sidecar_port': runtime['port'],
            'enabled_at_launch': model['status'] == 'live', 'tier': model.get('tier', 'gpu'), 'arch': list(model.get('arch', ['x86_64'])),
            'accelerator': model.get('accelerator', 'nvidia'), 'jetson': bool(model.get('jetson')),
            'runtime': {'env': dict(runtime.get('env') or {}), 'host_var': runtime.get('host_var', 'RUNTIME_HOST'), 'port_var': runtime.get('port_var', 'RUNTIME_PORT')},
            'catalog': True, 'status': model['status'], 'image_ref': ref, 'disk_gb': model['disk_gb'],
            'miner_env': dict(model.get('miner_env') or {})}
    if runtime.get('contract') == PACKAGED_RUNTIME:
        view['runtime']['contract'] = PACKAGED_RUNTIME
    for key in ('min_ram_gb', 'min_cpu_cores'):
        if key in model:
            view[key] = model[key]
    return view


def availability(release, entry):
    """(ok, reason): whether the model can be installed now. A reason is a plain sentence."""
    if entry['status'] == 'retired':
        return False, '{} is retired. Pick a live model with: reaxctl models'.format(entry['id'])
    if entry['status'] == 'coming':
        return False, '{} is announced but not live yet. Pick a live model with: reaxctl models'.format(entry['id'])
    if entry.get('dev_only'):
        return False, '{} is a development-network model; it is not available for a real miner.'.format(entry['id'])
    if is_placeholder(entry['weights']['revision']) or is_placeholder(entry['runtime']['digest']) or is_placeholder(entry['manifest_id']):
        return False, '{} still has unpinned weights, image or manifest in this release.'.format(entry['id'])
    return True, ''


# ----------------------------------------------------------------------------- hardware fit


def detect_gpus(call, host):
    """NVIDIA GPUs as doctor reports them: [{'name', 'vram_gb', 'driver_ok', 'jetson'?}]. `call(cmd)` returns the sysprobe dict."""
    jetson = host.get('jetson')
    if jetson:
        l4t = jetson.get('l4t')
        try:
            ok = bool(l4t) and int(str(l4t).split('.')[0]) >= 36
        except ValueError:
            ok = False
        return [{'name': jetson.get('model') or 'NVIDIA Jetson', 'vendor': 'NVIDIA', 'vram_gb': host.get('ram_gb'), 'driver_ok': ok, 'jetson': True}]
    gpus = []
    smi = call(['nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits'])
    if smi['returncode'] == 0:
        for line in smi['stdout'].splitlines():
            fields = [part.strip() for part in line.split(',')]
            try:
                name, memory, driver = fields
                gpus.append({'name': name, 'vendor': 'NVIDIA', 'vram_gb': float(memory) / 1024, 'driver_ok': int(driver.split('.')[0]) >= 580})
            except (ValueError, IndexError):
                continue
    return gpus


def need_text(entry):
    """One plain sentence about the hardware a model needs."""
    gpu = entry['gpu']
    if entry['accelerator'] == 'none':
        cpu = entry.get('min_cpu_cores')
        ram = entry.get('min_ram_gb')
        return 'no GPU (CPU only' + (', {} cores'.format(cpu) if cpu else '') + (', {} GB RAM'.format(ram) if ram else '') + ')'
    text = '{}x NVIDIA GPU with {}+ GB VRAM'.format(gpu['count'], gpu['min_vram_gb'])
    if gpu.get('max_vram_gb') is not None:
        text += ' (at most {} GB)'.format(gpu['max_vram_gb'])
    if entry.get('jetson'):
        text += ' or a Jetson Orin'
    return text


def fit(release, entry, host, gpus):
    """Does this machine fit the model? Same rules as `doctor`, per model: {'fits': bool, 'reasons': [..]}."""
    from .cmd_doctor import pool_host_reason
    reasons = []
    host_reason = pool_host_reason(entry, host)
    if host_reason:
        reasons.append(host_reason)
    gpu_req = entry['gpu']
    if entry['accelerator'] == 'nvidia' and not host_reason:
        fitting = [g for g in gpus if g.get('driver_ok') and g.get('vram_gb') is not None and g['vram_gb'] >= gpu_req['min_vram_gb'] - 1
                   and (gpu_req.get('max_vram_gb') is None or g['vram_gb'] <= gpu_req['max_vram_gb'])]  # a "24 GB" card reports ~23.99
        if len(fitting) < gpu_req['count']:
            seen = [g for g in gpus if g.get('vram_gb') is not None]
            if not seen:
                reasons.append('Needs {}; no NVIDIA GPU found.'.format(need_text(entry)))
            elif any(not g.get('driver_ok') for g in seen):
                reasons.append('Needs {}; the NVIDIA driver is too old (R580+ or JetPack 6 needed).'.format(need_text(entry)))
            else:
                reasons.append('Needs {}; this machine has {}.'.format(need_text(entry), ', '.join('{} ({:.0f} GB)'.format(g['name'], g['vram_gb']) for g in seen)))
    req = release['requirements']
    for key, label, override in (('cpu_cores', 'CPU cores', entry.get('min_cpu_cores')), ('ram_gb', 'GB RAM', entry.get('min_ram_gb'))):
        nominal = override if override is not None else req[key]
        floor = nominal * (RAM_TOLERANCE if key == 'ram_gb' and override is not None else 1)
        have = host.get(key)
        if have is None or have < floor:
            reasons.append('Needs at least {} {}.'.format(nominal, label))
    free = host.get('disk_free_gb')
    if free is not None and free < entry['disk_gb']:
        reasons.append('Needs {} GB free disk; {:.0f} GB free.'.format(entry['disk_gb'], free))
    return {'fits': not reasons, 'reasons': reasons}


def disk_required(release, pool):
    """Free disk (GB) install needs for a pool or catalog pool view."""
    return pool.get('disk_gb') or release['requirements']['disk_gb'][pool['id']]
