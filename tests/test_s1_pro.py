import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import copy
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from reaxlib import cmd_doctor, cmd_install as install, descriptor
from reaxlib.cmd_doctor import diagnose

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/core_a100.json').read_text())
SMI = 'nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits'
TOKEN = 'hf_SECRETSECRETSECRET'


def bundled():
    return json.loads(descriptor.BUNDLED.read_text())


def with_placeholders():
    # in-test copy of the bundled descriptor with the pre-publication sha256:TBD-* digests injected
    value = bundled()
    value['images']['public'] = False
    value['images']['digests'] = {k: 'sha256:TBD-' + k for k in value['images']['digests']}
    return value


def run_doctor(smi_row, cc=None, pool='s1-pro', check_hf=False, release=None):
    commands = dict(FIXTURE['commands'])
    commands[SMI] = {'returncode': 0, 'stdout': smi_row + '\n', 'stderr': ''}
    if cc is not None:
        commands['nvidia-smi conf-compute -f'] = {'returncode': 0, 'stdout': 'CC status: ' + cc + '\n', 'stderr': ''}
    probe = lambda cmd, timeout: commands.get(' '.join(cmd), {'returncode': 1, 'stdout': '', 'stderr': ''})
    args = argparse.Namespace(home='/nonexistent', pool=pool, probe_ip=False, check_hf=check_hf)
    return diagnose(args, host=dict(FIXTURE['host']), probe=probe, release=release or bundled())


class DescriptorTest(unittest.TestCase):
    def test_bundled_is_1_2_0rc3_with_s1_pro_enabled(self):
        value = descriptor.validate(bundled())
        self.assertEqual(value['release_id'], '1.2.0rc3')
        pools = {p['id']: p for p in value['pools']}
        self.assertTrue(pools['s1-pro']['enabled_at_launch'])
        self.assertTrue(pools['s1-fast']['enabled_at_launch'])
        self.assertEqual(pools['s1-pro']['manifest_id'], '5b11440bf57b5434')
        self.assertEqual(pools['s1-pro']['commitment'], 'reax-s1/1:s1-pro:5b11440bf57b5434')
        self.assertEqual(pools['s1-fast']['manifest_id'], 'ff6e01b983d48024')
        self.assertEqual(pools['s1-pro']['gpu']['min_vram_gb'], 80)
        self.assertIsNone(pools['s1-pro']['gpu'].get('max_vram_gb'))  # H200/B200 are larger than 96 GB
        self.assertEqual(pools['s1-pro']['runtime']['env'], {'RUNE_DETERMINISTIC': '1'})

    def test_previous_release_descriptor_stays_valid_and_unchanged(self):
        old = descriptor.validate(json.loads((ROOT / 'releases/1.1.0rc2.json').read_text()))
        self.assertEqual(old['release_id'], '1.1.0rc2')
        self.assertFalse(next(p for p in old['pools'] if p['id'] == 's1-pro')['enabled_at_launch'])

    def test_bundled_has_published_digests_and_public_source(self):
        value = bundled()
        self.assertEqual(descriptor.placeholder_digests(value), [])
        self.assertEqual(len(descriptor.PLACEHOLDER_DIGEST.findall(descriptor.BUNDLED.read_text())), 0)
        self.assertTrue(value['images']['public'])
        digests = value['images']['digests']
        self.assertEqual(sorted(digests), ['miner', 's1_cpu_runtime', 's1_fast_runtime', 's1_lite_runtime', 's1_lite_runtime_jetson', 's1_pro_runtime'])
        for key, digest in digests.items():
            self.assertRegex(digest, r'^sha256:[0-9a-f]{64}$', key)
        self.assertTrue(value['source']['public'])
        self.assertEqual(value['source']['commit'], '6f12faac558ea8aa629c23cc01a92b5ccb3acb4e')
        descriptor.validate(value)

    def test_placeholders_all_in_digests_only(self):
        value = with_placeholders()
        self.assertEqual(descriptor.placeholder_digests(value), ['miner', 's1_cpu_runtime', 's1_fast_runtime', 's1_lite_runtime', 's1_lite_runtime_jetson', 's1_pro_runtime'])
        self.assertEqual(len(descriptor.PLACEHOLDER_DIGEST.findall(json.dumps(value))), 6)
        self.assertFalse(value['images']['public'])
        descriptor.validate(value)

    def test_placeholders_refused_in_final_release(self):
        for change in ({'images': {'public': True}}, {'status': {'live': True, 'launch_status': 'live'}}):
            value = with_placeholders()
            for section, fields in change.items():
                value[section].update(fields)
            with self.assertRaisesRegex(ValueError, 'placeholder'):
                descriptor.validate(value)

    def test_final_release_with_real_digests_validates(self):
        value = bundled()
        value['images']['public'] = True
        value['images']['digests'] = {k: 'sha256:' + c * 64 for k, c in (('miner', 'a'), ('s1_fast_runtime', 'b'), ('s1_pro_runtime', 'c'))}
        value['source'].update(public=True, commit='1' * 40)
        descriptor.validate(value)
        self.assertEqual(descriptor.placeholder_digests(value), [])

    def test_image_ref_pins_the_published_digest(self):
        value = bundled()
        self.assertEqual(install.image_ref(value, 's1_pro_runtime'), 'ghcr.io/reaxlabs/reax-s1-pro-runtime@' + value['images']['digests']['s1_pro_runtime'])

    def test_image_ref_never_pins_a_placeholder(self):
        value = with_placeholders()
        self.assertEqual(install.image_ref(value, 's1_pro_runtime'), 'ghcr.io/reaxlabs/reax-s1-pro-runtime:1.2.0-rc3')

    def test_doctor_is_quiet_about_placeholders_for_the_bundled_release(self):
        result = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65')
        self.assertEqual(result.data['placeholder_digests'], [])
        self.assertFalse(any('placeholder image digests' in w for w in result.warnings))

    def test_doctor_warns_about_placeholders(self):
        result = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65', release=with_placeholders())
        self.assertEqual(result.data['placeholder_digests'], ['miner', 's1_cpu_runtime', 's1_fast_runtime', 's1_lite_runtime', 's1_lite_runtime_jetson', 's1_pro_runtime'])
        self.assertTrue(any('placeholder image digests' in w for w in result.warnings))
        self.assertFalse(result.data['real_mining_possible'])


class DoctorS1ProTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(_os.environ, {'HF_TOKEN': '', 'HUGGING_FACE_HUB_TOKEN': '', 'HF_HOME': '/nonexistent-hf'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_h100_80gb_fits_without_unmeasured_warning(self):
        result = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65')
        pro = next(p for p in result.data['eligible_pools'] if p['id'] == 's1-pro')
        self.assertTrue(pro['ok'])
        self.assertFalse(any('gpu_capacity' in b['id'] for b in result.data['blockers']))
        self.assertFalse(any('not been measured' in w for w in result.warnings))

    def test_rtx_pro_6000_96gb_and_h200_are_not_over_the_maximum(self):
        for row in ('NVIDIA RTX PRO 6000 Blackwell Server Edition, 97887, 580.65', 'NVIDIA H200, 143771, 580.65'):
            pro = next(p for p in run_doctor(row).data['eligible_pools'] if p['id'] == 's1-pro')
            self.assertTrue(pro['ok'], row)

    def test_a100_and_h200_warn_unmeasured(self):
        for row, name in (('NVIDIA A100-SXM4-80GB, 81920, 580.65', 'A100'), ('NVIDIA H200, 143771, 580.65', 'H200')):
            result = run_doctor(row)
            self.assertTrue(any('not been measured' in w and name in w for w in result.warnings), row)

    def test_small_card_blocks_selected_s1_pro_only(self):
        row = 'NVIDIA GeForce RTX 4090, 24564, 580.65'
        self.assertTrue(any(b['id'] == 'gpu_capacity_s1-pro' for b in run_doctor(row).data['blockers']))
        fast = run_doctor(row, pool=None)
        self.assertFalse(any('s1-pro' in b['id'] for b in fast.data['blockers']))
        self.assertTrue(next(p for p in fast.data['eligible_pools'] if p['id'] == 's1-fast')['ok'])

    def test_confidential_compute_on_points_to_uva_fix(self):
        result = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65', cc='ON')
        self.assertTrue(result.data['confidential_compute']['on'])
        self.assertTrue(any('REAX_VLLM_DISABLE_UVA' in w and '--cc-fix' in w for w in result.warnings))
        off = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65', cc='OFF')
        self.assertFalse(off.data['confidential_compute']['on'])
        self.assertFalse(any('REAX_VLLM_DISABLE_UVA' in w for w in off.warnings))

    def test_missing_hf_token_warns_and_token_is_never_printed(self):
        result = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65')
        self.assertFalse(result.data['hf_gate']['token_present'])
        self.assertTrue(any('Hugging Face token' in w for w in result.warnings))
        with patch.dict(_os.environ, {'HF_TOKEN': TOKEN}):
            result = run_doctor('NVIDIA H100 80GB HBM3, 81559, 580.65')
        self.assertTrue(result.data['hf_gate']['token_present'])
        self.assertNotIn(TOKEN, json.dumps(result.data) + result.render_text() + ' '.join(result.warnings))

    def test_gate_check_reports_access_without_leaking_token(self):
        weights = bundled()['pools'][1]['weights']
        seen = []

        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *exc): return False

        def allowed(request, timeout):
            seen.append(request)
            return Response(b'{}')

        def denied(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 403, 'gated', {}, None)

        with patch.dict(_os.environ, {'HF_TOKEN': TOKEN}):
            ok = cmd_doctor.hf_gate_status(weights, True, opener=allowed)
            no = cmd_doctor.hf_gate_status(weights, True, opener=denied)
        self.assertEqual((ok['checked'], ok['accessible']), (True, True))
        self.assertEqual((no['checked'], no['accessible']), (True, False))
        self.assertIn(weights['revision'], seen[0].full_url)
        self.assertNotIn(TOKEN, json.dumps([ok, no]))
        self.assertFalse(cmd_doctor.hf_gate_status(weights, False)['checked'])


class RenderS1ProTest(unittest.TestCase):
    def render(self, **config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        base = {'pool': 's1-pro', 'network': 'finney', 'netuid': 92, 'wallet_name': 'reax', 'hotkey_name': 'miner1'}
        base.update(config)
        with patch.dict(_os.environ, REAX_WALLET_DIR=str(home / 'w')):
            install.render(home, bundled(), base)
        return (home / 'compose.yml').read_text(), (home / 'miner.env').read_text()

    def test_deterministic_flag_loopback_and_no_cc_fix_by_default(self):
        compose, env = self.render()
        self.assertIn('RUNE_DETERMINISTIC: "1"', compose)
        self.assertIn('RUNE_HOST: 127.0.0.1', compose)
        self.assertNotIn('REAX_VLLM_DISABLE_UVA', compose)
        self.assertNotIn('vllm-ccfix', compose)
        self.assertIn('REAX_MODEL=s1-pro', env)
        self.assertIn('REAX_S1_PRO_RUNTIME_URL=http://127.0.0.1:8102', env)

    def test_cc_fix_adds_env_and_read_only_mount(self):
        compose, _ = self.render(cc_fix=True, ccfix_dir='/srv/subnet/runtimes/vllm-ccfix')
        self.assertIn('REAX_VLLM_DISABLE_UVA: "1"', compose)
        self.assertIn('PYTHONPATH: "/opt/reax-ccfix"', compose)
        self.assertIn('/srv/subnet/runtimes/vllm-ccfix:/opt/reax-ccfix:ro', compose)

    def test_s1_fast_render_is_unchanged_by_s1_pro_flags(self):
        compose, _ = self.render(pool='s1-fast', cc_fix=True, ccfix_dir='/x')
        self.assertNotIn('RUNE_DETERMINISTIC', compose)
        self.assertNotIn('ccfix', compose)

    def test_hostile_descriptor_runtime_env_is_rejected(self):
        value = bundled()
        value['pools'][1]['runtime']['env'] = {'RUNE_DETERMINISTIC': '1\nEVIL: x'}
        with self.assertRaises(ValueError):
            install.pool_runtime_env(value['pools'][1], {})


if __name__ == '__main__':
    unittest.main()


class ServiceTemplateTest(unittest.TestCase):
    def read(self, name):
        return (ROOT / 'service' / name).read_text()

    def test_compose_template_is_deterministic_loopback_and_cc_fix_is_separate(self):
        compose = self.read('docker-compose.s1-pro.yml')
        for needle in ('RUNE_DETERMINISTIC: "1"', 'RUNE_HOST: 127.0.0.1', 'REAX_MODEL: s1-pro', '5b11440bf57b5434', 'cap_drop: [ALL]'):
            self.assertIn(needle, compose)
        self.assertNotIn('ports:', compose)
        self.assertNotIn('0.0.0.0', compose)
        self.assertNotIn('REAX_VLLM_DISABLE_UVA', compose)
        fix = self.read('docker-compose.s1-pro-ccfix.yml')
        self.assertIn('REAX_VLLM_DISABLE_UVA: "1"', fix)
        self.assertIn(':/opt/reax-ccfix:ro', fix)

    def test_templates_contain_no_secrets(self):
        for name in ('docker-compose.s1-pro.yml', 'docker-compose.s1-pro-ccfix.yml', 'miner.env.example', 'reax-s1-pro.service'):
            text = self.read(name)
            self.assertNotIn('hf_', text.replace('hf_xet', ''))
            self.assertNotIn('mnemonic', text.lower())
