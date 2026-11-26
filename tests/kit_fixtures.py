"""Shared fixtures for the one-line kit tests (models catalog, `up`, `switch`, install.sh)."""
import copy
import json
import pathlib

from reaxlib import descriptor

ROOT = pathlib.Path(__file__).resolve().parents[1]

HOST = {'os': 'linux', 'arch': 'x86_64', 'cpu_cores': 16, 'ram_gb': 64.0, 'disk_free_gb': 500.0, 'jetson': None,
        'python_version': '3.12.3', 'miner_port_free': True}
GPU_4090 = [{'name': 'NVIDIA GeForce RTX 4090', 'vendor': 'NVIDIA', 'vram_gb': 23.99, 'driver_ok': True}]
GPU_PRO_6000 = [{'name': 'NVIDIA RTX PRO 6000 Blackwell Server Edition', 'vendor': 'NVIDIA', 'vram_gb': 95.5, 'driver_ok': True}]


def base_release():
    return json.loads(descriptor.BUNDLED.read_text())


def example_release():
    return json.loads((ROOT / 'releases' / 'portfolio-v1.json').read_text())


def live_model(mid='kev-4b', port=8105, **over):
    model = {'id': mid, 'name': 'Kev 4B', 'pot': 'pot-' + mid, 'status': 'live',
             'weights': {'repo': 'jaredpalmer/kev-4b', 'revision': 'a' * 40, 'gated': False, 'size_gb': 8, 'licence': 'apache-2.0'},
             'runtime': {'image': 'ghcr.io/reaxlabs/reax-model-' + mid, 'digest': 'sha256:' + 'b' * 64, 'port': port, 'env': {'REAX_MODEL': mid}},
             'gpu': {'class': '24 GB', 'min_vram_gb': 24, 'count': 1, 'vendor': 'NVIDIA'},
             'unit_throughput': {'unit': 'decisions_per_s', 'per': 'gpu', 'value': 90},
             'manifest_id': '0123456789abcdef', 'commitment': 'reax-s1/1:{}:0123456789abcdef'.format(mid), 'disk_gb': 20,
             'miner_env': {'REAX_FEATURE_MODEL_POTS': '1'}}
    model.update(over)
    return model


def release_with(*extra_models, **kwargs):
    value = base_release()
    value['requirements']['never_publish_ports'] = value['requirements']['never_publish_ports'] + list(kwargs.get('ports', (8105, 8106, 8107)))
    value['models'] = list(extra_models)
    return copy.deepcopy(value)


def fake_download(cmd):
    """Act like snapshot_download for a mocked runner: write the listed files (or one file) under local_dir."""
    if len(cmd) > 6 and cmd[2] == '-c' and 'snapshot_download' in cmd[3]:
        target = pathlib.Path(cmd[6])
        for name in cmd[7:] or ['model.safetensors']:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'weights')
