import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import cmd_install as install, descriptor, sysprobe
from reaxlib.cmd_doctor import diagnose

BUNDLED = json.loads(descriptor.BUNDLED.read_text())
LAUNCH = copy.deepcopy(BUNDLED)  # the bundled 1.2.0rc10 descriptor: all four pools enabled
for _pool in BUNDLED['pools']:  # the tests below exercise the development-pool rules, so mark lite/cpu as dev-only in their working copy
    if _pool['id'] in ('s1-lite', 's1-cpu'):
        _pool.update(enabled_at_launch=False, dev_only=True)
GOOD_CMDS = {
    'docker --version': 'Docker version 27',
    'docker compose version': 'v2.30',
    'timedatectl show -p NTPSynchronized --value': 'yes',
}
SMI = 'nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits'


def host(**kw):
    base = {'os': 'linux', 'arch': 'x86_64', 'cpu_cores': 8, 'ram_gb': 64, 'disk_free_gb': 200,
            'python_version': '3.11.8', 'groups': ['docker'], 'systemd_available': True,
            'miner_port_free': True, 'wsl_gpu_path': False, 'jetson': None}
    base.update(kw)
    return base


def doctor(h, smi=None, runtime=True, pool=None, mode=None):
    cmds = {k: {'returncode': 0, 'stdout': v, 'stderr': ''} for k, v in GOOD_CMDS.items()}
    cmds['docker info --format {{json .}}'] = {'returncode': 0, 'stdout': json.dumps({'Runtimes': {'runc': {}, **({'nvidia': {}} if runtime else {})}}), 'stderr': ''}
    if runtime:
        cmds['nvidia-container-cli --version'] = {'returncode': 0, 'stdout': '1.17', 'stderr': ''}
    if smi:
        cmds[SMI] = {'returncode': 0, 'stdout': smi, 'stderr': ''}
    with tempfile.TemporaryDirectory() as tmp:
        args = argparse.Namespace(home=tmp, mode=mode, pool=pool, probe_ip=False)
        return diagnose(args, probe=lambda cmd, timeout: cmds.get(' '.join(cmd), {'returncode': 127, 'stdout': '', 'stderr': ''}), host=h, release=copy.deepcopy(BUNDLED))


def pools(result):
    return {p['id']: p for p in result.data['eligible_pools']}


def with_pool(**changes):
    d = copy.deepcopy(BUNDLED)
    pool = next(p for p in d['pools'] if p['id'] == 's1-lite')
    for key, value in changes.items():
        if value is KeyError:
            pool.pop(key, None)
        else:
            pool[key] = value
    return d


class DescriptorFieldTests(unittest.TestCase):
    def test_bundled_enables_four_pools_with_subnet_manifests(self):
        descriptor.validate(copy.deepcopy(LAUNCH))
        by_id = {p['id']: p for p in LAUNCH['pools']}
        self.assertEqual(list(by_id), ['s1-fast', 's1-pro', 's1-lite', 's1-cpu'])
        for pool in by_id.values():
            self.assertTrue(pool['enabled_at_launch'])
            self.assertNotIn('dev_only', pool)
        self.assertEqual(by_id['s1-lite']['commitment'], 'reax-s1/1:s1-lite:8c7297e14e5e89bf')
        self.assertEqual(by_id['s1-cpu']['commitment'], 'reax-s1/1:s1-cpu:f2c9981f0766f2a6')
        self.assertEqual(LAUNCH['images']['tag'], '1.2.0-rc10')
        self.assertEqual(descriptor.placeholder_digests(LAUNCH), [])
        self.assertTrue(LAUNCH['images']['public'])
        self.assertEqual(sorted(LAUNCH['images']['digests']), ['miner', 's1_cpu_runtime', 's1_fast_runtime', 's1_lite_runtime', 's1_lite_runtime_jetson', 's1_pro_runtime'])
        for key, digest in LAUNCH['images']['digests'].items():
            self.assertRegex(digest, r'^sha256:[0-9a-f]{64}$', key)
        self.assertTrue(LAUNCH['source']['public'])
        self.assertEqual(LAUNCH['source']['commit'], '131b10713919c8ea763a33499f9188254b9e0ffe')

    def test_bundled_has_both_new_pools_disabled_and_dev_only(self):
        descriptor.validate(copy.deepcopy(BUNDLED))
        by_id = {p['id']: p for p in BUNDLED['pools']}
        for ident, port in (('s1-lite', 8103), ('s1-cpu', 8104)):
            self.assertFalse(by_id[ident]['enabled_at_launch'])
            self.assertTrue(by_id[ident]['dev_only'])
            self.assertEqual(by_id[ident]['sidecar_port'], port)
            self.assertIn(port, BUNDLED['requirements']['never_publish_ports'])
        self.assertEqual(by_id['s1-cpu']['commitment'], 'reax-s1/1:s1-cpu:f2c9981f0766f2a6')
        self.assertTrue(BUNDLED['pools'][0]['enabled_at_launch'])
        self.assertNotIn('dev_only', by_id['s1-fast'])

    def test_good_variations(self):
        for changes in ({'tier': 'small'}, {'arch': ['aarch64']}, {'min_ram_gb': 6, 'min_cpu_cores': 2}, {'dev_only': False}):
            with self.subTest(changes=changes):
                descriptor.validate(with_pool(**changes))

    def test_bad_values(self):
        bad = [{'tier': 'huge'}, {'arch': []}, {'arch': ['riscv']}, {'arch': 'x86_64'}, {'arch': ['x86_64', 'x86_64']},
               {'accelerator': 'amd'}, {'jetson': 'yes'}, {'dev_only': 1}, {'min_ram_gb': 0}, {'min_cpu_cores': '4'},
               {'jetson': True, 'arch': ['x86_64']}, {'jetson': True, 'accelerator': 'none'}]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                descriptor.validate(with_pool(**changes))

    def test_weights_files(self):
        for files in ([], 'a.gguf', ['../x.gguf'], ['a/b.gguf'], [''], ['a.gguf', 'a.gguf'], [3]):
            d = with_pool()
            next(p for p in d['pools'] if p['id'] == 's1-lite')['weights']['files'] = files
            with self.subTest(files=files), self.assertRaises(ValueError):
                descriptor.validate(d)

    def test_accelerator_none_gpu_rules(self):
        d = copy.deepcopy(BUNDLED)
        cpu = next(p for p in d['pools'] if p['id'] == 's1-cpu')
        for gpu in ({'min_vram_gb': 0, 'count': 1, 'vendor': 'none'}, {'min_vram_gb': 4, 'count': 0, 'vendor': 'none'}, {'min_vram_gb': 0, 'count': 0, 'vendor': 'NVIDIA'}):
            broken = copy.deepcopy(d)
            next(p for p in broken['pools'] if p['id'] == 's1-cpu')['gpu'] = gpu
            with self.subTest(gpu=gpu), self.assertRaises(ValueError):
                descriptor.validate(broken)
        # an NVIDIA pool still needs a real GPU
        lite = with_pool(gpu={'min_vram_gb': 0, 'count': 0, 'vendor': 'none'})
        with self.assertRaises(ValueError):
            descriptor.validate(lite)

    def test_new_pool_needs_its_image_key(self):
        d = copy.deepcopy(BUNDLED)
        del d['images']['s1_cpu_runtime']
        with self.assertRaises(ValueError):
            descriptor.validate(d)
        d = copy.deepcopy(BUNDLED)
        del d['images']['s1_lite_runtime_jetson']
        with self.assertRaises(ValueError):
            descriptor.validate(d)

    def test_unknown_pool_and_unprotected_port_rejected(self):
        d = with_pool()
        d['pools'][2]['id'] = 's1-huge'
        d['pools'][2]['commitment'] = 'reax-s1/1:s1-huge:' + d['pools'][2]['manifest_id']
        with self.assertRaises(ValueError):
            descriptor.validate(d)
        d = copy.deepcopy(BUNDLED)
        d['requirements']['never_publish_ports'].remove(8104)
        with self.assertRaises(ValueError):
            descriptor.validate(d)


class JetsonProbeTests(unittest.TestCase):
    def test_release_file_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel, model = pathlib.Path(tmp, 'rel'), pathlib.Path(tmp, 'model')
            rel.write_text('# R36 (release), REVISION: 4.0, GCID: 1, BOARD: generic, EABI: aarch64\n')
            model.write_bytes(b'NVIDIA Jetson Orin Nano Developer Kit\0')
            self.assertEqual(sysprobe.detect_jetson(str(rel), str(model)), {'l4t': '36.4.0', 'model': 'NVIDIA Jetson Orin Nano Developer Kit'})
            self.assertEqual(sysprobe.detect_jetson(str(pathlib.Path(tmp, 'none')), str(model))['l4t'], None)
            self.assertIsNone(sysprobe.detect_jetson(str(pathlib.Path(tmp, 'none')), str(pathlib.Path(tmp, 'none2'))))
            model.write_bytes(b'Raspberry Pi 5\0')
            self.assertIsNone(sysprobe.detect_jetson(str(pathlib.Path(tmp, 'none')), str(model)))


class DoctorTieredTests(unittest.TestCase):
    def test_x86_no_gpu_runs_s1_cpu(self):
        r = doctor(host(ram_gb=8, cpu_cores=4), pool='s1-cpu')
        p = pools(r)
        self.assertTrue(p['s1-cpu']['ok'])
        self.assertFalse(p['s1-fast']['ok'])
        self.assertFalse(p['s1-lite']['ok'])
        self.assertEqual(r.data['recommended_pool'], 's1-cpu')
        ids = [b['id'] for b in r.data['blockers']]
        self.assertFalse({'gpu', 'driver', 'toolkit', 'os', 'ram_gb', 'cpu_cores'} & set(ids), ids)
        self.assertFalse(any(i.startswith('gpu_capacity') for i in ids))
        self.assertEqual(r.status, 'ok')

    def test_no_gpu_without_pool_keeps_fast_blockers_but_recommends_cpu(self):
        r = doctor(host())
        self.assertIn('gpu', [b['id'] for b in r.data['blockers']])
        self.assertEqual([i for i, p in pools(r).items() if p['ok']], [])  # dev pools are opt-in
        self.assertTrue(pools(r)['s1-cpu']['hardware_ok'])
        self.assertEqual(r.data['recommended_pool'], 's1-cpu')

    def test_x86_12gb_nvidia_runs_s1_lite(self):
        r = doctor(host(), smi='RTX 3060, 12288, 580.95', pool='s1-lite')
        p = pools(r)
        self.assertTrue(p['s1-lite']['ok'], p['s1-lite'])
        self.assertFalse(p['s1-fast']['ok'])
        self.assertTrue(p['s1-cpu']['hardware_ok'])
        self.assertEqual(r.data['recommended_pool'], 's1-lite')
        self.assertEqual(r.data['blockers'], [])

    def test_x86_12gb_with_old_driver_blocks_lite(self):
        r = doctor(host(), smi='RTX 3060, 12288, 570.1', pool='s1-lite')
        self.assertFalse(pools(r)['s1-lite']['ok'])
        self.assertIn('driver', [b['id'] for b in r.data['blockers']])

    def test_jetson_orin_runs_lite_and_cpu(self):
        jetson = {'l4t': '36.4.0', 'model': 'NVIDIA Jetson Orin Nano Developer Kit'}
        h = host(arch='aarch64', cpu_cores=6, ram_gb=7.4, jetson=jetson)
        r = doctor(h, pool='s1-lite')
        p = pools(r)
        self.assertTrue(p['s1-lite']['ok'], p['s1-lite'])
        self.assertTrue(p['s1-cpu']['hardware_ok'])
        self.assertFalse(p['s1-fast']['ok'])
        self.assertEqual(r.data['jetson'], jetson)
        self.assertEqual(r.data['recommended_pool'], 's1-lite')
        self.assertEqual(r.data['blockers'], [])
        r = doctor(h, pool='s1-cpu')
        self.assertTrue(pools(r)['s1-cpu']['ok'])
        self.assertEqual(r.data['blockers'], [])

    def test_jetson_needs_l4t_36_and_nvidia_runtime(self):
        h = host(arch='aarch64', cpu_cores=6, ram_gb=7.4, jetson={'l4t': '35.6.0', 'model': 'Jetson'})
        r = doctor(h, pool='s1-lite')
        self.assertFalse(pools(r)['s1-lite']['ok'])
        self.assertIn('driver', [b['id'] for b in r.data['blockers']])
        h['jetson']['l4t'] = '36.4.0'
        r = doctor(h, runtime=False, pool='s1-lite')
        self.assertIn('toolkit', [b['id'] for b in r.data['blockers']])

    def test_jetson_with_too_little_memory(self):
        h = host(arch='aarch64', cpu_cores=6, ram_gb=3.7, jetson={'l4t': '36.4.0', 'model': 'Jetson'})
        self.assertFalse(pools(doctor(h, pool='s1-lite'))['s1-lite']['ok'])

    def test_aarch64_non_jetson_only_cpu(self):
        h = host(arch='aarch64', cpu_cores=8, ram_gb=16)
        r = doctor(h, pool='s1-cpu')
        p = pools(r)
        self.assertTrue(p['s1-cpu']['ok'])
        self.assertFalse(p['s1-lite']['hardware_ok'])
        self.assertFalse(p['s1-fast']['hardware_ok'])
        self.assertEqual(r.data['recommended_pool'], 's1-cpu')
        self.assertEqual(r.data['blockers'], [])
        r = doctor(h, smi='GH200, 98304, 580.95', pool='s1-lite')  # a discrete NVIDIA GPU on aarch64 is not a Jetson
        self.assertFalse(pools(r)['s1-lite']['ok'])

    def test_cpu_pool_size_limits(self):
        r = doctor(host(cpu_cores=2, ram_gb=2), pool='s1-cpu')
        self.assertFalse(pools(r)['s1-cpu']['ok'])
        self.assertEqual({'ram_gb', 'cpu_cores'}, {b['id'] for b in r.data['blockers']} & {'ram_gb', 'cpu_cores'})
        self.assertFalse(pools(doctor(host(disk_free_gb=2), pool='s1-cpu'))['s1-cpu']['ok'])

    def test_unchanged_24gb_x86_host(self):
        r = doctor(host(), smi='RTX 4090, 24564, 580.95')
        p = pools(r)
        self.assertEqual([i for i, v in p.items() if v['ok']], ['s1-fast'])
        self.assertEqual(r.data['recommended_pool'], 's1-fast')
        self.assertEqual(r.data['blockers'], [])
        self.assertEqual(p['s1-pro']['reasons'], ['Not the selected pool; listed for reference.'])
        self.assertTrue(p['s1-lite']['hardware_ok'])

    def test_mainnet_never_offers_dev_pools(self):
        r = doctor(host(), pool='s1-cpu', mode='mainnet')
        self.assertFalse(pools(r)['s1-cpu']['ok'])
        self.assertIn('dev_pool_mainnet', [b['id'] for b in r.data['blockers']])
        r = doctor(host(), mode='mainnet')
        self.assertIsNone(r.data['recommended_pool'])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(_os.environ, REAX_WALLET_DIR='')
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, pool, **extra):
        install.render(self.home, copy.deepcopy(BUNDLED), dict({'pool': pool, 'network': 'test', 'netuid': 4}, **extra))
        return (self.home / 'compose.yml').read_text(), (self.home / 'miner.env').read_text()

    def test_cpu_pool(self):
        compose, env = self.render('s1-cpu')
        self.assertNotIn('nvidia', compose)
        self.assertNotIn('devices:', compose)
        self.assertNotIn('deploy:', compose)
        self.assertIn('ghcr.io/reaxlabs/reax-s1-cpu-runtime@' + BUNDLED['images']['digests']['s1_cpu_runtime'], compose)
        for line in ('RUNTIME_HOST: 127.0.0.1', 'RUNTIME_PORT: "8104"', 'GGUF_MODEL_DIR: /model', 'GGUF_N_GPU_LAYERS: "0"', 'S1M_INTERNAL_TOKEN_FILE: /run/runtime-token'):
            self.assertIn(line, compose)
        self.assertIn('/weights/s1-cpu:/model:ro', compose)
        self.assertIn('REAX_MODEL=s1-cpu', env)
        self.assertIn('REAX_S1_CPU_RUNTIME_URL=http://127.0.0.1:8104', env)
        self.assertIn('REAX_FEATURE_TIERED_POOLS=1', env)
        self.assertIn('REAX_ALLOW_DEV_MODELS=1', env)

    def test_lite_on_x86_reserves_gpu(self):
        compose, env = self.render('s1-lite')
        self.assertIn('driver: nvidia', compose)
        self.assertIn('capabilities: [gpu]', compose)
        self.assertNotIn('runtime: nvidia', compose)
        self.assertIn('ghcr.io/reaxlabs/reax-s1-lite-runtime@' + BUNDLED['images']['digests']['s1_lite_runtime'], compose)
        self.assertNotIn('jetson', compose)
        self.assertIn('RUNTIME_PORT: "8103"', compose)
        self.assertIn('GGUF_N_GPU_LAYERS: "-1"', compose)
        self.assertIn('REAX_S1_LITE_RUNTIME_URL=http://127.0.0.1:8103', env)
        self.assertIn('REAX_ALLOW_DEV_MODELS=1', env)

    def test_lite_on_jetson_uses_nvidia_runtime_and_jetson_image(self):
        compose, _ = self.render('s1-lite', jetson=True)
        self.assertIn('    runtime: nvidia', compose)
        self.assertIn('reax-s1-lite-runtime-jetson@' + BUNDLED['images']['digests']['s1_lite_runtime_jetson'], compose)
        self.assertNotIn('devices:', compose)
        self.assertIn('GGUF_N_GPU_LAYERS: "-1"', compose)

    def test_launch_pool_render_has_no_dev_flags(self):
        compose, env = self.render('s1-fast')
        self.assertNotIn('REAX_ALLOW_DEV_MODELS', env)
        self.assertNotIn('REAX_FEATURE_TIERED_POOLS', env)
        self.assertIn('RUNTIME_PORT: "8101"', compose)
        self.assertIn('driver: nvidia', compose)

    def test_new_sidecar_ports_are_not_valid_miner_ports(self):
        for port in (8103, 8104):
            with self.assertRaises(ValueError):
                install.render(self.home, copy.deepcopy(BUNDLED), {'pool': 's1-cpu', 'network': 'test', 'netuid': 4, 'miner_port': port})


class InstallTests(unittest.TestCase):
    def args(self, home, pool, mode, **kw):
        return argparse.Namespace(home=str(home), mode=mode, pool=pool, confirm_mainnet=True, accept_manifest_change=False, **kw)

    def live(self):
        d = copy.deepcopy(BUNDLED)
        d['status'].update(live=True, launch_status='live', netuid=92, testnet_netuid=41)
        d['images']['public'] = True
        d['images']['digests'] = {k: 'sha256:' + format(i + 1, 'x') * 64 for i, k in enumerate(d['images']['digests'])}
        return d

    def test_mainnet_refuses_dev_pools_before_any_work(self):
        for pool in ('s1-lite', 's1-cpu'):
            with tempfile.TemporaryDirectory() as tmp, patch.object(install, 'descriptor', return_value=self.live()), patch.object(install, 'runner') as runner:
                home = pathlib.Path(tmp) / 'h'
                result = install.install(self.args(home, pool, 'mainnet'))
                self.assertEqual(result.code(), 12)
                self.assertIn('development-network pool', result.summary)
                runner.assert_not_called()
                self.assertFalse(home.exists())

    def test_testnet_download_fetches_only_listed_file(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return 0, '', ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(install, 'descriptor', return_value=self.live()), \
                patch.object(install, 'runner', side_effect=runner), patch.object(install, 'prepare_tools', return_value=None), \
                patch.object(install.sys, 'version_info', (3, 11, 4)), patch.dict(_os.environ, REAX_WALLET_DIR=''):
            home = pathlib.Path(tmp) / 'h'
            result = install.install(self.args(home, 's1-cpu', 'testnet'))
            self.assertTrue(result.ok, result.summary)
        download = next(c for c in calls if '-c' in c)
        self.assertEqual(download[-4:], ['kouhxp/gutsy', 'f9995fe26a337f1a4ea3f1a5ed38c9799e5a8f63', str(home / 'weights/s1-cpu'), 'gutsy-0.8b-v04-q8_0.gguf'])
        self.assertIn('allow_patterns', download[download.index('-c') + 1])

    def test_disk_requirement_per_pool(self):
        self.assertEqual(BUNDLED['requirements']['disk_gb']['s1-lite'], 8)
        self.assertEqual(BUNDLED['requirements']['disk_gb']['s1-cpu'], 3)
        with tempfile.TemporaryDirectory() as tmp, patch.object(install, 'descriptor', return_value=self.live()), \
                patch.object(install.shutil, 'disk_usage', return_value=type('U', (), {'free': 2 * 1024 ** 3})()), \
                patch.object(install.sys, 'version_info', (3, 11, 4)), patch.object(install, 'runner') as runner:
            result = install.install(self.args(pathlib.Path(tmp) / 'h', 's1-cpu', 'testnet'))
        self.assertEqual(result.code(), 10)
        self.assertIn('3 GB', result.summary)
        runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
