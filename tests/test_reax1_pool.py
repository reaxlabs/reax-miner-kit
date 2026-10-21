import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import copy
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import cmd_install as install, descriptor
from reaxlib.cmd_doctor import diagnose

BUNDLED = json.loads(descriptor.BUNDLED.read_text())
POOL = next(p for p in BUNDLED['pools'] if p['id'] == 'reax-1')


class Reax1DescriptorTests(unittest.TestCase):
    def test_planned_pool_validates_and_pins_the_public_revision(self):
        descriptor.validate(copy.deepcopy(BUNDLED))
        self.assertEqual(POOL['weights']['repo'], 'decisionmodels/reax-1')
        self.assertEqual(POOL['weights']['revision'], 'ca2a0cc54a537c1f5cf24e5b09387cfb357b6518')
        self.assertFalse(POOL['weights']['gated'])
        self.assertEqual(POOL['gpu']['min_vram_gb'], 48)
        self.assertEqual(POOL['commitment'], 'reax-s1/1:reax-1:' + POOL['manifest_id'])
        self.assertEqual(POOL['sidecar_port'], 8105)
        self.assertIn(8105, BUNDLED['requirements']['never_publish_ports'])
        self.assertIn('reax-1', BUNDLED['requirements']['disk_gb'])

    def test_not_active_and_no_invented_image_digest(self):
        self.assertFalse(POOL['enabled_at_launch'])
        self.assertTrue(POOL['planned'])
        self.assertIn('TODO', POOL['runtime_image_note'])
        self.assertIn('reax_1_runtime', BUNDLED['images'])
        self.assertNotIn('reax_1_runtime', BUNDLED['images']['digests'])  # TODO(release owner): the image is not built yet
        self.assertEqual(descriptor.placeholder_digests(BUNDLED), [])  # a TBD placeholder would be refused in a live descriptor

    def test_other_pools_are_unchanged(self):
        by_id = {p['id']: p for p in BUNDLED['pools']}
        self.assertEqual([p for p in by_id if p != 'reax-1'], ['s1-fast', 's1-pro', 's1-lite', 's1-cpu'])
        self.assertEqual(by_id['s1-pro']['commitment'], 'reax-s1/1:s1-pro:5b11440bf57b5434')
        self.assertTrue(all(by_id[i]['enabled_at_launch'] for i in ('s1-fast', 's1-pro', 's1-lite', 's1-cpu')))

    def test_the_pool_needs_its_image_and_a_private_port(self):
        broken = copy.deepcopy(BUNDLED)
        del broken['images']['reax_1_runtime']
        with self.assertRaises(ValueError):
            descriptor.validate(broken)
        broken = copy.deepcopy(BUNDLED)
        broken['requirements']['never_publish_ports'].remove(8105)
        with self.assertRaises(ValueError):
            descriptor.validate(broken)


class Reax1InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(_os.environ, REAX_WALLET_DIR='')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_render_uses_the_s1_fast_sidecar_convention_on_port_8105(self):
        install.render(self.home, copy.deepcopy(BUNDLED), {'pool': 'reax-1', 'network': 'test', 'netuid': 4})
        compose = (self.home / 'compose.yml').read_text()
        env = (self.home / 'miner.env').read_text()
        for line in ('RUNTIME_HOST: 127.0.0.1', 'RUNTIME_PORT: "8105"', 'driver: nvidia', '/weights/reax-1:/model:ro'):
            self.assertIn(line, compose)
        self.assertIn('ghcr.io/reaxlabs/reax-reax-1-runtime:', compose)  # no digest yet: tag reference, TODO(release owner)
        self.assertIn('REAX_MODEL=reax-1', env)
        self.assertIn('REAX_REAX_1_RUNTIME_URL=http://127.0.0.1:8105', env)
        self.assertNotIn('REAX_ALLOW_DEV_MODELS', env)

    def test_miner_port_cannot_be_the_reax1_sidecar_port(self):
        with self.assertRaises(ValueError):
            install.render(self.home, copy.deepcopy(BUNDLED), {'pool': 'reax-1', 'network': 'test', 'netuid': 4, 'miner_port': 8105})

    def test_mainnet_install_refuses_the_planned_pool(self):
        args = argparse.Namespace(home=str(self.home), mode='mainnet', pool='reax-1', confirm_mainnet=True)
        with patch.object(install, 'descriptor', return_value=copy.deepcopy(BUNDLED)):
            result = install.install(args)
        self.assertNotEqual(result.status, 'ok')
        self.assertEqual(result.exit_code, 12)
        self.assertIn('not enabled', result.summary)


if __name__ == '__main__':
    unittest.main()
