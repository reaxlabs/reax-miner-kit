import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import contextlib
import io as _io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import cmd_models, descriptor, models
from reaxlib.cli import main
from kit_fixtures import GPU_4090, GPU_PRO_6000, HOST, base_release, example_release, live_model, release_with
from release_fixtures import write_release


class DescriptorModelsTest(unittest.TestCase):
    def test_bundled_and_example_descriptors_validate(self):
        descriptor.validate(base_release())
        descriptor.validate(example_release())

    def test_bundled_release_has_no_models_key(self):
        # the portfolio is only in the example file; kits older than this change reject a descriptor with an unknown key
        self.assertNotIn('models', base_release())

    def test_example_models_are_all_coming_with_placeholders(self):
        example = example_release()
        self.assertEqual(len(example['models']), 13)  # reax-1-3 is not part of the v2 pots for now (#86886)
        for model in example['models']:
            self.assertEqual(model['status'], 'coming')
            self.assertTrue(models.is_placeholder(model['runtime']['digest']), model['id'])
            self.assertRegex(model['weights']['revision'], r'^[0-9a-f]{40}$')
            self.assertIsNone(model['unit_throughput']['value'])
            self.assertEqual(model['commitment'], 'reax-s1/1:{}:{}'.format(model['id'], model['manifest_id']))
        for entry in models.catalog(example):
            if entry['kind'] == 'model':
                self.assertFalse(models.availability(example, entry)[0])

    def test_portfolio_hardware_and_serving_notes(self):
        rows = {m['id']: m for m in example_release()['models']}
        self.assertEqual(rows['mammoth-1']['notes'].split('.')[0], 'EU operators only')
        self.assertEqual(rows['blink-v0-3']['gpu']['min_vram_gb'], 48)
        self.assertIn('Blackwell only', rows['blink-v0-3']['notes'])
        self.assertEqual(rows['kev-4b']['gpu']['min_vram_gb'], 16)
        self.assertIn('One request at a time', rows['kev-4b']['notes'])
        self.assertIn('bf16 only', rows['elephant-1']['notes'])
        self.assertIn('Up to 8 requests in flight', rows['torchcast-decision-4b']['notes'])
        self.assertEqual(rows['laya']['accelerator'], 'none')
        self.assertIn('CPU torch or ONNX, or GPU fp32 only', rows['laya']['notes'])
        self.assertNotIn('bison-1', rows)

    def test_model_fields_are_parsed(self):
        release = release_with(live_model(unit_throughput={'unit': 'decisions_per_s', 'per': 'gpu', 'value': None, 'note': 'unmeasured'}))
        descriptor.validate(release)
        entry = models.get(release, 'kev-4b')
        self.assertEqual((entry['kind'], entry['status'], entry['pot']), ('model', 'live', 'pot-kev-4b'))
        self.assertEqual(entry['gpu']['class'], '24 GB')
        self.assertEqual(entry['runtime']['digest'], 'sha256:' + 'b' * 64)
        self.assertEqual(entry['runtime']['port'], 8105)
        self.assertEqual(entry['miner_env'], {'REAX_FEATURE_MODEL_POTS': '1'})
        self.assertIsNone(entry['unit_throughput']['value'])
        self.assertTrue(models.availability(release, entry)[0])

    def test_legacy_pools_map_to_models(self):
        release = base_release()
        by_id = {e['id']: e for e in models.catalog(release)}
        self.assertEqual(sorted(by_id), ['s1-cpu', 's1-fast', 's1-lite', 's1-pro'])
        self.assertEqual(by_id['s1-fast']['pot'], 's1-fast')
        self.assertEqual(by_id['s1-fast']['runtime']['port'], 8101)
        self.assertEqual(by_id['s1-fast']['runtime']['digest'], release['images']['digests']['s1_fast_runtime'])
        self.assertEqual({e['status'] for e in by_id.values()}, {'live'})

    def test_pool_status_and_pot_are_optional_catalog_fields(self):
        release = base_release()
        next(p for p in release['pools'] if p['id'] == 's1-cpu').update(status='retired', pot='legacy-cpu', unit_throughput={'unit': 'decisions_per_s', 'value': 5})
        descriptor.validate(release)
        entry = models.get(release, 's1-cpu')
        self.assertEqual((entry['status'], entry['pot']), ('retired', 'legacy-cpu'))
        self.assertIn('retired', models.availability(release, entry)[1])
        for bad in ({'status': 'sold'}, {'pot': 'Bad Pot'}, {'unit_throughput': {'value': 3}}):
            broken = base_release()
            broken['pools'][0].update(bad)
            with self.assertRaises(ValueError, msg=str(bad)):
                descriptor.validate(broken)

    def reject(self, mutate, message):
        release = release_with(live_model())
        mutate(release)
        with self.assertRaisesRegex(ValueError, message):
            descriptor.validate(release)

    def test_invalid_models_are_rejected(self):
        cases = [
            (lambda r: r['models'][0].update(extra_field=1), 'unknown descriptor model fields'),
            (lambda r: r['models'][0].pop('pot'), 'missing required'),
            (lambda r: r['models'][0].update(status='sold'), 'status'),
            (lambda r: r['models'][0].update(id='S1_Bad'), 'model id'),
            (lambda r: r['models'].append(live_model(port=8106)), 'duplicate model id'),
            (lambda r: r['models'][0].update(id='s1-fast', commitment='reax-s1/1:s1-fast:0123456789abcdef'), 'duplicate model id'),
            (lambda r: r['models'][0]['runtime'].update(port=9999), 'never_publish_ports'),
            (lambda r: r['models'][0]['runtime'].update(port=8101), 'duplicate sidecar port'),
            (lambda r: r['models'][0]['runtime'].update(image='docker.io/reaxlabs/x'), 'ghcr.io'),
            (lambda r: r['models'][0]['runtime'].update(image='ghcr.io/evil/x'), 'not trusted'),
            (lambda r: r['models'][0]['runtime'].update(digest='sha256:TBD-kev'), 'live model needs a sha256 digest'),
            (lambda r: r['models'][0]['weights'].update(revision='main'), 'revision'),
            (lambda r: r['models'][0]['weights'].update(revision='TBD-kev'), 'live model needs a 40-hex'),
            (lambda r: r['models'][0].update(manifest_id='TBD-kev', commitment='reax-s1/1:kev-4b:TBD-kev'), 'live model needs a 16-hex'),
            (lambda r: r['models'][0].update(commitment='reax-s1/1:kev-4b:ffffffffffffffff'), 'commitment'),
            (lambda r: r['models'][0]['runtime'].update(env={'S1M_INTERNAL_TOKEN_FILE': '/x'}), 'controls'),
            (lambda r: r['models'][0]['runtime'].update(env={'lower': 'x'}), 'environment'),
            (lambda r: r['models'][0].update(miner_env={'PATH': '/x'}), 'REAX_'),
            (lambda r: r['models'][0]['gpu'].update(count=0), 'GPU'),
            (lambda r: r['models'][0]['gpu'].update(max_vram_gb=8), 'maximum VRAM'),
            (lambda r: r['models'][0].update(unit_throughput={'unit': 'x', 'value': -1}), 'throughput'),
            (lambda r: r['models'][0].update(disk_gb=0), 'disk'),
            (lambda r: r.update(models={'a': 1}), 'must be a list'),
        ]
        for mutate, message in cases:
            with self.subTest(message=message):
                self.reject(mutate, message)

    def test_coming_model_may_carry_placeholders_but_not_live(self):
        coming = live_model(status='coming')
        coming['weights']['revision'] = 'TBD-kev'
        coming['runtime']['digest'] = 'sha256:TBD-kev'
        coming['manifest_id'] = 'TBD-kev'
        coming['commitment'] = 'reax-s1/1:kev-4b:TBD-kev'
        descriptor.validate(release_with(coming))
        coming['status'] = 'live'
        with self.assertRaises(ValueError):
            descriptor.validate(release_with(coming))

    def test_cpu_model_needs_no_gpu_fields(self):
        cpu = live_model('tiny', port=8106, accelerator='none', tier='cpu', arch=['x86_64', 'aarch64'], min_ram_gb=4, min_cpu_cores=2,
                         gpu={'min_vram_gb': 0, 'count': 0, 'vendor': 'none'}, disk_gb=3)
        cpu['commitment'] = 'reax-s1/1:tiny:0123456789abcdef'
        release = release_with(cpu)
        descriptor.validate(release)
        self.assertEqual(models.need_text(models.get(release, 'tiny')), 'no GPU (CPU only, 2 cores, 4 GB RAM)')


class FitTest(unittest.TestCase):
    def setUp(self):
        self.release = release_with(live_model(), live_model('big-pair', port=8106, gpu={'min_vram_gb': 80, 'count': 2, 'vendor': 'NVIDIA', 'max_vram_gb': 200},
                                                              commitment='reax-s1/1:big-pair:0123456789abcdef'))

    def fit(self, model_id, host=None, gpus=None):
        return models.fit(self.release, models.get(self.release, model_id), dict(HOST, **(host or {})), GPU_4090 if gpus is None else gpus)

    def test_a_24gb_card_fits_the_24gb_models_only(self):
        self.assertTrue(self.fit('s1-fast')['fits'])
        self.assertTrue(self.fit('kev-4b')['fits'])
        self.assertTrue(self.fit('s1-lite')['fits'])
        verdict = self.fit('s1-pro')
        self.assertFalse(verdict['fits'])
        self.assertIn('48', verdict['reasons'][0])
        self.assertIn('RTX 4090', verdict['reasons'][0])

    def test_big_card_fits_everything_with_one_gpu(self):
        self.assertTrue(self.fit('s1-pro', gpus=GPU_PRO_6000)['fits'])
        self.assertTrue(self.fit('kev-4b', gpus=GPU_PRO_6000)['fits'])
        self.assertFalse(self.fit('big-pair', gpus=GPU_PRO_6000)['fits'])  # needs two GPUs
        self.assertTrue(self.fit('big-pair', gpus=GPU_PRO_6000 * 2)['fits'])

    def test_no_gpu_fits_only_cpu_pool(self):
        self.assertTrue(self.fit('s1-cpu', gpus=[])['fits'])
        verdict = self.fit('s1-fast', gpus=[])
        self.assertFalse(verdict['fits'])
        self.assertIn('no NVIDIA GPU', verdict['reasons'][0])

    def test_old_driver_is_named(self):
        old = [dict(GPU_4090[0], driver_ok=False)]
        self.assertIn('driver is too old', self.fit('s1-fast', gpus=old)['reasons'][0])

    def test_architecture_ram_cpu_and_disk(self):
        self.assertIn('x86_64', self.fit('s1-fast', host={'arch': 'aarch64'})['reasons'][0])
        self.assertIn('RAM', ' '.join(self.fit('s1-fast', host={'ram_gb': 8.0})['reasons']))
        self.assertIn('CPU cores', ' '.join(self.fit('s1-fast', host={'cpu_cores': 2})['reasons']))
        disk = self.fit('s1-pro', host={'disk_free_gb': 50.0}, gpus=GPU_PRO_6000)
        self.assertFalse(disk['fits'])
        self.assertIn('100 GB free disk', disk['reasons'][0])
        self.assertIn('RAM', ' '.join(self.fit('s1-lite', host={'ram_gb': 5.0})['reasons']))  # s1-lite needs 8 GB (tolerance 90 %)
        self.assertTrue(self.fit('s1-lite', host={'ram_gb': 7.4})['fits'])

    def test_max_vram_is_respected(self):
        small = live_model('small', port=8107, gpu={'min_vram_gb': 8, 'count': 1, 'vendor': 'NVIDIA', 'max_vram_gb': 16}, commitment='reax-s1/1:small:0123456789abcdef')
        release = release_with(small)
        verdict = models.fit(release, models.get(release, 'small'), HOST, GPU_4090)
        self.assertFalse(verdict['fits'])

    def test_detect_gpus_parses_nvidia_smi_and_flags_old_drivers(self):
        out = 'NVIDIA GeForce RTX 4090, 24564, 580.65.06\nNVIDIA GeForce RTX 3090, 24576, 570.1\nnot, a, valid, row\n'
        gpus = models.detect_gpus(lambda cmd: {'returncode': 0, 'stdout': out, 'stderr': ''}, HOST)
        self.assertEqual([(g['name'], g['driver_ok']) for g in gpus], [('NVIDIA GeForce RTX 4090', True), ('NVIDIA GeForce RTX 3090', False)])
        self.assertAlmostEqual(gpus[0]['vram_gb'], 24564 / 1024)
        self.assertEqual(models.detect_gpus(lambda cmd: {'returncode': 127, 'stdout': '', 'stderr': 'no'}, HOST), [])

    def test_jetson_reports_unified_memory(self):
        host = dict(HOST, arch='aarch64', ram_gb=8.0, jetson={'l4t': '36.4.0', 'model': 'NVIDIA Jetson Orin Nano'})
        gpus = models.detect_gpus(lambda cmd: {'returncode': 127, 'stdout': '', 'stderr': ''}, host)
        self.assertEqual(gpus[0]['vram_gb'], 8.0)
        self.assertTrue(models.fit(self.release, models.get(self.release, 's1-lite'), host, gpus)['fits'])


class ModelsCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / 'state'
        self.addCleanup(self.tmp.cleanup)

    def call(self, *argv, gpus=None, release=None):
        if release is not None:
            write_release(self.home, release)
        out = _io.StringIO()
        with patch.object(cmd_models, 'host_view', return_value=(HOST, GPU_4090 if gpus is None else gpus)), contextlib.redirect_stdout(out):
            rc = main(list(argv) + ['--home', str(self.home), '--offline'])
        return rc, out.getvalue()

    def test_json_lists_models_with_status_need_and_fit(self):
        rc, text = self.call('models', '--json', release=example_release())
        self.assertEqual(rc, 0)
        result = json.loads(text)
        self.assertEqual(result['schema'], 'reaxctl/1')
        rows = {r['id']: r for r in result['data']['models']}
        self.assertEqual(rows['s1-fast']['status'], 'live')
        self.assertTrue(rows['s1-fast']['fits'])
        self.assertFalse(rows['s1-pro']['fits'])
        self.assertEqual(rows['autotrust-decider-pro-35b']['status'], 'coming')
        self.assertFalse(rows['autotrust-decider-pro-35b']['installable'])
        self.assertIn('96+ GB', rows['autotrust-decider-pro-35b']['needs'])
        self.assertIsNone(rows['torchcast-decision-31b']['unit_throughput']['value'])
        self.assertEqual(rows['s1-fast']['pot'], 's1-fast')
        self.assertEqual(rows['s1-fast']['runtime']['port'], 8101)
        self.assertEqual([r['status'] for r in result['data']['models']], sorted((r['status'] for r in result['data']['models']), key={'live': 0, 'coming': 1, 'retired': 2}.get))
        self.assertEqual(result['next'][0]['command'], 'reaxctl up --model s1-fast')

    def test_text_table_and_filters(self):
        rc, text = self.call('models', '--status', 'coming', release=example_release())
        self.assertEqual(rc, 0)
        self.assertIn('MODEL', text)
        self.assertIn('autotrust-decider-pro-35b', text)
        self.assertNotIn('s1-fast', text)

    def test_retired_hidden_unless_asked_or_installed(self):
        release = base_release()
        next(p for p in release['pools'] if p['id'] == 's1-cpu')['status'] = 'retired'
        rows = json.loads(self.call('models', '--json', release=release)[1])['data']['models']
        self.assertNotIn('s1-cpu', [r['id'] for r in rows])
        rows = json.loads(self.call('models', '--json', '--all')[1])['data']['models']
        self.assertEqual(next(r for r in rows if r['id'] == 's1-cpu')['status'], 'retired')
        from reaxlib import state
        state.save_config(__import__('argparse').Namespace(home=str(self.home)), {'pool': 's1-cpu'})
        rows = json.loads(self.call('models', '--json')[1])['data']['models']
        self.assertTrue(next(r for r in rows if r['id'] == 's1-cpu')['installed'])

    def test_no_gpu_machine_is_told_what_fits(self):
        rc, text = self.call('models', '--json', gpus=[], release=base_release())
        result = json.loads(text)
        rows = {r['id']: r for r in result['data']['models']}
        self.assertTrue(rows['s1-cpu']['fits'])
        self.assertFalse(rows['s1-fast']['fits'])
        self.assertIn('no NVIDIA GPU', rows['s1-fast']['fit_reasons'][0])
        self.assertEqual(result['next'][0]['command'], 'reaxctl up --model s1-cpu')


if __name__ == '__main__':
    unittest.main()
