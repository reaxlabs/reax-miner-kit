"""Offline host diagnosis and the dry-run onboarding graph."""
from __future__ import annotations

import json
import re
import ipaddress
import os
import urllib.request

from . import descriptor, state, sysprobe
from .cli import add_common
from .result import Result

DOCKER_DOC = 'https://docs.docker.com/engine/install/'
NVIDIA_DOC = 'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html'


def diagnose(args, probe=None, host=None, release=None):
    probe = probe or sysprobe.run
    release = release or descriptor.load(args, fetch=True)
    config = state.load_config(args)
    host = host or sysprobe.facts(state.home(args), config.get('miner_port', 8091))
    blockers, warnings, gpus = [], [], []

    def block(key, message, fix):
        blockers.append({'id': key, 'message': message, 'fix': fix})

    def call(cmd):
        return probe(cmd, timeout=10)  # a cold nvidia-smi or docker CLI can take several seconds

    smi = call(['nvidia-smi', '--query-gpu=name,memory.total,driver_version', '--format=csv,noheader,nounits'])
    if smi['returncode'] == 0:
        for line in smi['stdout'].splitlines():
            fields = [part.strip() for part in line.split(',')]
            try:
                name, memory, driver = fields
                major = int(driver.split('.')[0])
                gpus.append({'name': name, 'vendor': 'NVIDIA', 'vram_gb': float(memory) / 1024,
                             'driver_version': driver, 'driver_ok': major >= 580})
            except (ValueError, IndexError):
                warnings.append('Could not parse one nvidia-smi GPU row.')
    if not gpus:
        pci = call(['lspci'])
        for line in pci['stdout'].splitlines():
            if re.search(r'VGA|3D controller|Display controller', line, re.I):
                vendor = 'AMD' if re.search(r'\bAMD\b|Radeon|\bATI\b', line, re.I) else 'NVIDIA' if 'NVIDIA' in line else 'unknown'
                gpus.append({'name': line, 'vendor': vendor, 'vram_gb': None, 'driver_version': None, 'driver_ok': False})
        if host['os'] == 'macos' and host['arch'] in ('arm64', 'aarch64'):
            gpus.append({'name': 'Apple GPU', 'vendor': 'Apple', 'vram_gb': None, 'driver_version': None, 'driver_ok': False})
    if not any(gpu['vendor'] == 'NVIDIA' and gpu['vram_gb'] is not None for gpu in gpus):
        block('gpu', 'No usable NVIDIA GPU detected; AMD and Apple GPUs are unsupported for mining.', 'Use a Linux machine with an NVIDIA GPU and driver R580+ (a Mac or AMD machine can only act as a controller): https://www.nvidia.com/en-us/drivers/')
    elif any(not gpu['driver_ok'] for gpu in gpus if gpu['vendor'] == 'NVIDIA'):
        block('driver', 'NVIDIA driver R580 or newer is required for CUDA 13.', 'Install an R580+ driver using https://www.nvidia.com/en-us/drivers/')
    supported = host['os'] in ('linux', 'wsl2') and host['arch'].lower() in ('x86_64', 'amd64')
    if not supported:
        block('os', 'Real mining requires Linux x86_64 or WSL2 with NVIDIA passthrough.', 'Mine on Linux x86_64 (or WSL2 with an NVIDIA GPU). WSL: https://docs.nvidia.com/cuda/wsl-user-guide/index.html')
    version = call(['docker', '--version'])
    info = call(['docker', 'info', '--format', '{{json .}}'])
    compose = call(['docker', 'compose', 'version'])
    info_data = {}
    if info['returncode'] == 0:
        try:
            info_data = json.loads(info['stdout'])
        except ValueError:
            warnings.append('Docker info was not valid JSON; NVIDIA runtime could not be verified.')
    runtimes = list(info_data.get('Runtimes') or {}) if isinstance(info_data, dict) else []  # docker prints "Runtimes": null when the daemon is unreachable
    docker = {'client': version['returncode'] == 0, 'daemon_reachable': info['returncode'] == 0,
              'in_group': 'docker' in host.get('groups', []), 'compose': compose['returncode'] == 0,
              'runtimes': runtimes, 'nvidia_runtime': 'nvidia' in runtimes}
    if not docker['client']:
        block('docker_client', 'Docker Engine is not installed.', 'Install Docker Engine for your distro: ' + DOCKER_DOC)
    elif not docker['daemon_reachable']:
        denied = 'permission denied' in (info['stderr'] + info['stdout']).lower()
        fix = ('Grant this user Docker access, then log out and back in: https://docs.docker.com/engine/install/linux-postinstall/' if denied else 'Start Docker Engine and check its daemon: ' + DOCKER_DOC)
        block('docker_daemon', 'Docker daemon access denied.' if denied else 'Docker daemon is unreachable.', fix)
    if not docker['compose']:
        block('compose', 'Docker Compose plugin is unavailable.', 'Install the Compose plugin: https://docs.docker.com/compose/install/linux/')
    toolkit = call(['nvidia-container-cli', '--version'])
    toolkit_ok = toolkit['returncode'] == 0 or docker['nvidia_runtime']
    if not toolkit_ok or not docker['nvidia_runtime']:
        block('toolkit', 'NVIDIA container runtime is not configured.', 'Install NVIDIA Container Toolkit for your distro and configure Docker: ' + NVIDIA_DOC)
    ntp = call(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'])
    ntp_ok = ntp['returncode'] == 0 and ntp['stdout'].strip().lower() in ('yes', 'true', 'ntpsynchronized=yes')
    ntp_known = ntp['returncode'] == 0
    if not ntp_ok:
        chrony = call(['chronyc', 'tracking'])
        ntp_known = ntp_known or chrony['returncode'] == 0
        ntp_ok = chrony['returncode'] == 0 and bool(re.search(r'Leap status\s*:\s*Normal', chrony['stdout']))
    if not ntp_ok and not ntp_known:
        warnings.append('Clock synchronization could not be checked here (no timedatectl or chrony, typical in containers). Make sure the host clock is synchronized: validators reject requests with more than a few seconds of skew.')
    elif not ntp_ok:
        block('ntp', 'Clock synchronization could not be verified.', 'Enable your OS time synchronization service; on Linux use timedatectl set-ntp true or configure chrony: https://chrony-project.org/documentation.html')
    if not host['miner_port_free']:
        block('miner_port', 'The configured miner port is in use.', 'Stop the conflicting listener or choose a free miner_port in config.json; keep sidecar ports private.')
    req = release['requirements']
    for key, minimum, label in [('cpu_cores', req['cpu_cores'], 'CPU cores'), ('ram_gb', req['ram_gb'], 'RAM GB')]:
        if host.get(key) is None or host[key] < minimum:
            block(key, 'Mining requires at least {} {}.'.format(minimum, label), 'Use a host with the required capacity.')
    python_version = tuple(int(x) for x in host['python_version'].split('.')[:2])
    python_min = tuple(int(x) for x in req['python_min'].split('.')[:2])
    rehearsal = python_version >= python_min
    if not rehearsal:
        block('python', 'The rehearsal requires Python ' + req['python_min'] + ' or newer.', 'Install the required Python version: https://www.python.org/downloads/')
    eligible = []
    for pool in release['pools']:
        active = pool['enabled_at_launch'] or getattr(args, 'pool', None) == pool['id']
        gpu_req = pool['gpu']
        reasons = []
        fitting = [gpu for gpu in gpus if gpu['vendor'].lower() == gpu_req['vendor'].lower() and gpu['driver_ok'] and gpu['vram_gb'] is not None and gpu['vram_gb'] >= gpu_req['min_vram_gb'] - 1 and (gpu_req.get('max_vram_gb') is None or gpu['vram_gb'] <= gpu_req['max_vram_gb'])]
        if len(fitting) < gpu_req['count']:
            reasons.append('Requires {} {} GPU(s) with {} GB VRAM and R580+ driver.'.format(gpu_req['count'], gpu_req['vendor'], gpu_req['min_vram_gb']))
            if active:
                block('gpu_capacity_' + pool['id'], reasons[-1], 'Select a pool matching your GPU capacity; supported hardware is listed in the release descriptor.')
        # a '24 GB' card reports ~23.99 GiB, so only warn well below the nominal size
        elif active and any(gpu['vram_gb'] < gpu_req['min_vram_gb'] * 0.97 for gpu in fitting):
            warnings.append(pool['id'] + ': usable VRAM is slightly below the nominal minimum; accepted within 1 GB tolerance.')
        if not supported:
            reasons.append('Linux x86_64 or WSL2 is required.')
        if host['disk_free_gb'] < req['disk_gb'][pool['id']]:
            reasons.append('Insufficient free disk space for this pool.')
            if active:
                block('disk_' + pool['id'], reasons[-1], 'Free space in REAX_HOME or select a larger disk; {} GB required.'.format(req['disk_gb'][pool['id']]))
        eligible.append({'id': pool['id'], 'ok': not reasons, 'enabled_at_launch': pool['enabled_at_launch'],
                         'reasons': reasons if active else ['Not enabled at launch; listed for reference.']})
    available = any(pool['ok'] for pool in eligible)
    common_blockers = [item for item in blockers if not item['id'].startswith(('disk_', 'gpu_capacity_'))]
    launch_pools = {pool['id'] for pool in release['pools'] if pool['enabled_at_launch']}
    ready = any(pool['ok'] and pool['id'] in launch_pools for pool in eligible) and not common_blockers and release['status']['live'] and release['status']['netuid'] is not None and release['images']['public']
    source_ok = bool(os.environ.get('REAX_SOURCE_DIR')) or bool(release['source'].get('public'))
    data = dict(host, gpus=gpus, docker=docker, nvidia_container_toolkit=toolkit_ok, ntp_synchronized=ntp_ok,
                eligible_pools=eligible, rehearsal_capable=rehearsal, rehearsal_possible=bool(rehearsal and source_ok), rehearsal_source_available=source_ok, real_mining_possible=bool(ready), blockers=blockers,
                mode_default='localnet', huggingface={'checked': False, 'reachable': None}, public_ip=None)
    if getattr(args, 'probe_ip', False):
        for endpoint, key in [('https://api.ipify.org', 'public_ip'), ('https://huggingface.co', 'huggingface')]:
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as response:
                    value = response.read(128).decode().strip()
                data[key] = str(ipaddress.ip_address(value)) if key == 'public_ip' else {'checked': True, 'reachable': True}
            except (OSError, ValueError):
                warnings.append('Network probe failed: ' + endpoint)
                if key == 'huggingface':
                    data[key] = {'checked': True, 'reachable': False}
    warnings.extend(item['message'] + ' Fix: ' + item['fix'] for item in blockers)
    if rehearsal and source_ok:
        summary, nxt = 'Localnet rehearsal is possible; see mining requirements.', {'command': 'reaxctl plan --mode localnet', 'why': 'Review the rehearsal steps.'}
    elif rehearsal:
        summary = 'Host checked. The free rehearsal needs the REAX source, which the release descriptor does not yet mark as available.'
        nxt = {'command': 'reaxctl plan', 'why': 'Review what you can prepare today.'}
    else:
        summary, nxt = 'Host requirements checked.', {'command': 'reaxctl plan', 'why': 'Review the setup steps.'}
    return Result('doctor', status='ok' if rehearsal or available else 'blocked', summary=summary, data=data, warnings=warnings, next=[nxt])


def step_graph(mode='localnet', pool='s1-fast', live=False, unavailable=None):
    entries = [('doctor', 'agent', 'none', True, 'reaxctl doctor'),
               ('choose-mode', 'agent', 'none', True, 'reaxctl plan --mode ' + mode),
               ('install', 'agent', 'none', True, 'reaxctl install --pool ' + pool + ' --mode ' + mode),
               ('hotkey', 'agent', 'none', False, 'reaxctl wallet hotkey'),
               ('coldkey', 'human', 'none', False, None),
               ('coldkeypub', 'agent', 'none', True, 'reaxctl wallet coldkeypub --ss58 <public-address>'),
               ('register', 'agent' if mode == 'localnet' else 'human', 'none' if mode == 'localnet' else 'tao', False, 'reaxctl register --mode ' + mode),
               ('start', 'agent', 'none', True, 'reaxctl start'),
               ('verify', 'agent', 'none', True, 'reaxctl verify'),
               ('service', 'agent', 'none', True, 'reaxctl service install'),
               ('update-timer', 'agent', 'none', True, 'reaxctl update timer install')]
    steps = []
    for ident, who, cost, reversible, command in entries:
        if mode == 'localnet' and ident in ('coldkey', 'coldkeypub'):
            continue
        item = {'id': ident, 'who': who, 'cost': {'kind': cost, 'note': 'Test TAO only.' if ident == 'register' and mode == 'localnet' else 'The registration page shows the live fee; you approve in your own wallet' if cost == 'tao' else 'No payment by reaxctl.'}, 'reversible': reversible, 'command': command}
        if ident == 'coldkey':
            item['instructions'] = 'Create the coldkey in Talisman or btcli on another machine; never paste its recovery phrase here.'
        if mode != 'localnet' and (not live or unavailable) and ident not in ('doctor', 'choose-mode'):
            item['blocked'] = unavailable or 'launching_soon'
        steps.append(item)
    return steps


def plan(args):
    release = descriptor.load(args, fetch=True)
    chosen = state.mode(args)
    pool = getattr(args, 'pool', None) or state.load_config(args).get('pool', 's1-fast')
    if pool not in [p['id'] for p in release['pools']]:
        return Result('plan', status='error', summary='Unknown pool.', exit_code=2)
    diagnosis = diagnose(args, release=release)
    def graph(mode):
        selected = next(p for p in release['pools'] if p['id'] == pool)
        reason = None
        if release['status']['live']:
            if release['status']['testnet_netuid' if mode == 'testnet' else 'netuid'] is None:
                reason = 'network_unavailable'
            elif not selected['enabled_at_launch']:
                reason = 'pool_disabled'
        return step_graph(mode, pool, release['status']['live'], reason)
    return Result('plan', summary='Review the ordered setup steps before making changes.',
                  data={'mode': chosen, 'pool': pool, 'steps': graph(chosen),
                        'mainnet_steps': graph('mainnet'), 'doctor': diagnosis.data},
                  warnings=['Rewards are variable subnet emissions; can be zero.'],
                  next=[{'command': 'reaxctl install --pool ' + pool + ' --mode ' + chosen, 'why': 'Prepare the selected mode.'}])


def register(sub):
    parser = sub.add_parser('doctor'); add_common(parser)
    parser.add_argument('--probe-ip', action='store_true')
    parser.add_argument('--pool', default=None)
    parser.set_defaults(func=diagnose)
    parser = sub.add_parser('plan'); add_common(parser)
    parser.add_argument('--pool', default=None)
    parser.set_defaults(func=plan)
