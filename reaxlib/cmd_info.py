"""Machine-readable command contract and the tool's operating boundaries."""
from __future__ import annotations

import os

from . import __version__, descriptor, state
from .cli import add_common
from .cmd_doctor import step_graph
from .result import Result

RULES = [
    'Flags --confirm-mainnet, --accept-manifest-change and --yes are agent-typable policy gates; the real money gate is human signing in their own wallet.',
    'Never create, read, print, store or ask for a coldkey or any recovery phrase; use hotkey files and public coldkeypub only.',
    'Never spend real TAO, sign a payment, or register on mainnet; the human approves in their own wallet.',
    'Default to localnet rehearsal. Real-network modes require a live descriptor and netuid; mainnet requires human-approved --confirm-mainnet.',
    'Rewards are variable subnet emissions; can be zero.',
    'Never publish sidecar ports 8101, 8102 or 8000; miner port defaults to 8091.',
    'No telemetry; disclose every contacted endpoint in net-policy.',
]
COMMANDS = ['doctor', 'plan', 'install', 'wallet hotkey', 'wallet coldkeypub', 'wallet status', 'register', 'start', 'stop', 'restart', 'status', 'logs', 'run', 'verify', 'service install', 'service uninstall', 'service status', 'update check', 'update apply', 'update timer install', 'update timer uninstall', 'localnet up', 'localnet down', 'localnet status', 'uninstall', 'spec', 'agent-rules', 'net-policy', 'version']


def version(args):
    return Result('version', summary='reaxctl ' + __version__, data={'version': __version__, 'release_id': descriptor.load(args)['release_id']})


def spec(args):
    release = descriptor.load(args)
    return Result('spec', summary='Machine-readable command contract.', data={'commands': COMMANDS, 'modes': ['localnet', 'testnet', 'mainnet'], 'mode_default': 'localnet', 'step_graph': step_graph('mainnet', live=release['status']['live']), 'localnet_step_graph': step_graph(), 'exit_codes': {'0': 'ok', '2': 'usage', '10': 'requirement unmet', '11': 'needs_human', '12': 'not available yet', '13': 'network/download failure', '20': 'verification failed', '1': 'unexpected'}, 'rules': RULES})


def agent_rules(args):
    return Result('agent-rules', summary='Follow these rules for every operation.', data={'rules': RULES}, warnings=RULES)


def net_policy(args):
    release = descriptor.load(args)
    config = state.load_config(args)
    local_chain = config.get('network') if config.get('mode') == 'localnet' else None
    if not local_chain or not local_chain.startswith('ws://127.0.0.1:'):
        local_chain = 'ws://127.0.0.1:' + str(config.get('base_port', 19951))
    endpoints = [
        {'url': descriptor.url(), 'purpose': 'release descriptor', 'condition': 'doctor/plan/install/register/start/update unless --offline or REAX_OFFLINE=1; one-hour cache'},
        {'url': 'https://ghcr.io', 'purpose': 'pinned Docker images and native localnet chain image', 'condition': 'install or update; first localnet up'},
        {'url': 'https://huggingface.co', 'purpose': 'pinned model weights and reachability', 'condition': 'install or doctor --probe-ip'},
        {'url': 'https://cdn-lfs.huggingface.co', 'purpose': 'weight downloads', 'condition': 'Hugging Face download redirect'},
        {'url': 'https://cas-bridge.xethub.hf.co', 'purpose': 'weight downloads', 'condition': 'Hugging Face download redirect'},
        {'url': release['source']['git'], 'purpose': 'pinned rehearsal source', 'condition': 'localnet install'},
        {'url': 'https://pypi.org', 'purpose': 'pinned wallet/tools packages', 'condition': 'install tool venv; localnet up rehearsal dependency'},
        {'url': 'https://files.pythonhosted.org', 'purpose': 'tool package downloads', 'condition': 'install tool venv; localnet up rehearsal dependency'},
        {'url': local_chain, 'purpose': 'local rehearsal chain', 'condition': 'localnet'},
        {'url': 'wss://entrypoint-finney.opentensor.ai:443', 'purpose': 'mainnet chain', 'condition': 'live descriptor and human-approved mainnet'},
        {'url': 'wss://test.finney.opentensor.ai:443', 'purpose': 'testnet chain', 'condition': 'live descriptor and testnet netuid'},
        {'url': 'https://api.ipify.org', 'purpose': 'public IP echo', 'condition': 'only --probe-ip'},
    ]
    if os.environ.get('HF_ENDPOINT'):
        endpoints.append({'url': os.environ['HF_ENDPOINT'], 'purpose': 'configured Hugging Face SDK endpoint', 'condition': 'install/localnet SDK downloads'})
    endpoints.extend([{'url': 'tcp://<configured global external_ip>:' + str(p), 'purpose': 'sidecar privacy probe', 'condition': 'verify --probe-ip only'} for p in release['requirements']['never_publish_ports']])
    endpoints.extend([{'url': 'https://registry-1.docker.io / https://nvcr.io', 'purpose': 'source Dockerfile base images', 'condition': 'private source compose build'}, {'url': 'https://huggingface.co', 'purpose': 'rehearsal miner/validator SDK models', 'condition': 'localnet up'}])
    return Result('net-policy', summary='No telemetry. Descriptor refresh is disclosed; --offline suppresses it.', data={'telemetry': False, 'endpoints': endpoints, 'never_publish_ports': release['requirements']['never_publish_ports']}, warnings=['Registration page is opened by the human; reaxctl does not submit a transaction.'])


def register(sub):
    for name, handler in [('version', version), ('spec', spec), ('agent-rules', agent_rules), ('net-policy', net_policy)]:
        parser = sub.add_parser(name); add_common(parser)
        parser.set_defaults(func=handler)
