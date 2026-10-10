import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reaxlib import cmd_uninstall, descriptor

BUNDLED = json.loads((Path(__file__).resolve().parent.parent / 'releases/current.json').read_text())


class DescriptorTrustTests(unittest.TestCase):
    def test_bundled_descriptor_is_valid(self):
        descriptor.validate(copy.deepcopy(BUNDLED))

    def test_foreign_source_and_image_owners_are_rejected(self):
        bad = copy.deepcopy(BUNDLED)
        bad['source']['git'] = 'https://github.com/someone-else/reax-subnet'
        with self.assertRaises(ValueError):
            descriptor.validate(bad)

    def test_trusted_owner_is_accepted(self):
        for owner in ('reaxlabs',):
            with self.subTest(owner=owner):
                release = copy.deepcopy(BUNDLED)
                release['source']['git'] = f'https://github.com/{owner}/reax-subnet'
                for image in ('miner', 's1_fast_runtime', 's1_pro_runtime'):
                    release['images'][image] = f'ghcr.io/{owner}/' + release['images'][image].rsplit('/', 1)[1]
                descriptor.validate(release)

    def test_owner_lookalikes_are_rejected_by_exact_path_segment(self):
        for owner in ('reaxlabs-evil', 'reaxIabs'):
            with self.subTest(owner=owner):
                release = copy.deepcopy(BUNDLED)
                release['source']['git'] = f'https://github.com/{owner}/reax-subnet'
                for image in ('miner', 's1_fast_runtime', 's1_pro_runtime'):
                    release['images'][image] = f'ghcr.io/{owner}/' + release['images'][image].rsplit('/', 1)[1]
                with self.assertRaises(ValueError):
                    descriptor.validate(release)
        bad = copy.deepcopy(BUNDLED)
        bad['images']['miner'] = 'ghcr.io/someone-else/reax-miner'
        with self.assertRaises(ValueError):
            descriptor.validate(bad)

    def test_public_source_needs_an_exact_commit(self):
        bad = copy.deepcopy(BUNDLED)
        bad['source'].pop('commit', None)
        bad['source']['public'] = True
        with self.assertRaises(ValueError):
            descriptor.validate(bad)
        bad['source']['commit'] = 'a' * 40
        descriptor.validate(bad)


class UninstallTests(unittest.TestCase):
    def args(self, home, yes=False, purge=False):
        return argparse.Namespace(home=str(home), yes=yes, purge_weights=purge, mode=None, offline=True, json=True, cmd='uninstall')

    def test_requires_confirmation_then_removes_state_but_keeps_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'a' / 'state'
            (home / 'weights/s1-fast').mkdir(parents=True)
            (home / 'secrets').mkdir()
            (home / 'secrets/runtime-token').write_text('x')
            (home / 'config.json').write_text('{}')
            result = cmd_uninstall.handle(self.args(home))
            self.assertEqual(result.status, 'needs_human')
            self.assertTrue((home / 'secrets').exists())
            with patch.object(cmd_uninstall, '_sub', return_value=type('R', (), {'status': 'ok'})()):
                result = cmd_uninstall.handle(self.args(home, yes=True))
            self.assertTrue(result.ok)
            self.assertFalse((home / 'secrets').exists())
            self.assertTrue((home / 'weights/s1-fast').exists())
            with patch.object(cmd_uninstall, '_sub', return_value=type('R', (), {'status': 'ok'})()):
                cmd_uninstall.handle(self.args(home, yes=True, purge=True))
            self.assertFalse(home.exists())

    def test_refuses_directories_that_are_not_reaxctl_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'a' / 'important'
            home.mkdir(parents=True)
            (home / 'photos').write_text('keep')
            result = cmd_uninstall.handle(self.args(home, yes=True))
            self.assertEqual(result.code(), 10)
            self.assertTrue((home / 'photos').exists())


if __name__ == '__main__':
    unittest.main()
