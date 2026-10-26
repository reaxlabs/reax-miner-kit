import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import json
import pathlib
import re
import unittest

from reaxlib import cmd_install as install, descriptor

ROOT = pathlib.Path(__file__).resolve().parents[1]
START_BLOCK = '9331987'


def bundled():
    return json.loads(descriptor.BUNDLED.read_text())


class MultiModelTemplateTest(unittest.TestCase):
    def setUp(self):
        self.compose = (ROOT / 'service/docker-compose.multimodel.example.yml').read_text()
        self.docs = (ROOT / 'docs/MULTIMODEL.md').read_text()
        self.value = bundled()
        self.pools = {p['id']: p for p in self.value['pools']}

    def test_images_follow_the_bundled_release_tag(self):
        tag = self.value['images']['tag']
        for key in ('miner', 's1_lite_runtime', 's1_cpu_runtime'):
            self.assertIn('image: %s:%s' % (self.value['images'][key], tag), self.compose)

    def test_commitment_comment_matches_the_descriptor(self):
        lite, cpu = self.pools['s1-lite'], self.pools['s1-cpu']
        self.assertIn('reax-s1m/1:s1-lite:%s,s1-cpu:%s' % (lite['manifest_id'], cpu['manifest_id']), self.compose)

    def test_each_model_has_its_own_loopback_sidecar_and_url(self):
        for name, pool in (('S1_LITE', self.pools['s1-lite']), ('S1_CPU', self.pools['s1-cpu'])):
            port = pool['sidecar_port']
            self.assertIn('RUNTIME_PORT: "%d"' % port, self.compose)
            self.assertIn('REAX_%s_RUNTIME_URL: http://127.0.0.1:%d' % (name, port), self.compose)
        self.assertIn('REAX_MODELS: s1-lite,s1-cpu', self.compose)
        self.assertEqual(self.compose.count('RUNTIME_HOST: 127.0.0.1'), 2)
        self.assertNotIn('REAX_MODEL:', self.compose)
        self.assertNotIn('ports:', self.compose)
        self.assertNotIn('0.0.0.0', self.compose)
        for port in install.SIDECAR_PORTS:
            self.assertNotIn(':%d:' % port, self.compose)

    def test_hardening_and_no_coldkey_secret(self):
        self.assertEqual(self.compose.count('cap_drop: [ALL]'), 3)
        self.assertEqual(self.compose.count('no-new-privileges:true'), 3)
        self.assertIn('hotkeys/${REAX_WALLET_HOTKEY', self.compose)
        self.assertIn('coldkeypub.txt', self.compose)
        self.assertNotRegex(self.compose, r'wallets:/wallets')  # never mount the whole wallet directory
        self.assertNotIn('mnemonic', self.compose.lower())

    def test_gpu_reservation_only_for_the_gpu_model(self):
        self.assertEqual(self.compose.count('driver: nvidia'), 1)
        self.assertIn('GGUF_N_GPU_LAYERS: "-1"', self.compose)
        self.assertIn('GGUF_N_GPU_LAYERS: "0"', self.compose)

    def test_docs_name_the_start_block_and_the_tools(self):
        for text in (self.docs, self.compose, (ROOT / 'README.md').read_text()):
            self.assertIn(START_BLOCK, text)
        for needle in ('REAX_MODELS', 'reax-commit', 'reax-selfcheck', '--dry-run', 'withdraw competition', '--no-deps',
                       'REAX_FORCE_MULTIROLE', 'reax-s1m/1', 'hand-made `btcli` commit'):
            self.assertIn(needle, self.docs)

    def test_docs_table_matches_descriptor_ports_and_images(self):
        for pool_id, key in (('s1-fast', 's1_fast_runtime'), ('s1-pro', 's1_pro_runtime'), ('s1-lite', 's1_lite_runtime'), ('s1-cpu', 's1_cpu_runtime')):
            row = next(line for line in self.docs.splitlines() if line.startswith('| ' + pool_id + ' '))
            self.assertIn('`%s`' % key, row)
            self.assertIn(str(self.pools[pool_id]['sidecar_port']), row)

    def test_docs_have_no_money_wording(self):
        text = (self.docs + self.compose).lower()
        for word in ('earn', 'profit', 'income', 'revenue', 'return on', 'reward', 'price', ' pay'):
            self.assertNotIn(word, text)

    def test_weights_commands_use_the_pinned_revisions(self):
        for pool_id in ('s1-lite', 's1-cpu'):
            w = self.pools[pool_id]['weights']
            self.assertIn('hf download %s %s --revision %s' % (w['repo'], w['files'][0], w['revision']), self.docs)


class BundledDescriptorTest(unittest.TestCase):
    def test_bundled_copy_is_live_so_the_offline_fallback_does_not_block_mainnet(self):
        value = descriptor.validate(bundled())
        self.assertEqual(value['release_id'], '1.2.0rc11.post7')
        self.assertTrue(value['status']['live'])
        self.assertEqual((value['status']['launch_status'], value['status']['network'], value['status']['netuid']), ('live', 'finney', 92))
        self.assertIsNone(install.gate(value, 'mainnet', confirm=True))

    def test_minimum_release_is_unchanged(self):
        self.assertEqual(bundled()['minimum_release'], '1.1.0rc2')


class StartBlockIsNotGatedInTheKitTest(unittest.TestCase):
    def test_kit_has_no_block_gate_of_its_own(self):
        # The start block is enforced by the miner image and reax-commit (elements.py of the subnet release); the kit
        # must not carry a second copy that could drift from it.
        for path in list((ROOT / 'reaxlib').glob('*.py')) + [ROOT / 'reaxctl']:
            self.assertNotIn(START_BLOCK, path.read_text(), str(path))


if __name__ == '__main__':
    unittest.main()
