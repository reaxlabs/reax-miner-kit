import argparse
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import descriptor, state
from reaxlib.cli import build_parser
from release_fixtures import registered_pending_release


class DescriptorTests(unittest.TestCase):
    def test_home_precedence_and_atomic_permissions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict('os.environ', {'REAX_HOME': tmp}):
            self.assertEqual(state.home(), Path(tmp))
            args = argparse.Namespace(home=tmp + '/explicit')
            self.assertEqual(state.home(args), Path(tmp + '/explicit'))
            state.save_config(args, {'mode': 'testnet'})
            self.assertEqual(state.load_config(args)['mode'], 'testnet')
            self.assertEqual((state.home(args) / 'config.json').stat().st_mode & 0o777, 0o600)
            state.save_state(args, {'completed': ['doctor']})
            self.assertEqual(state.load_state(args)['completed'], ['doctor'])

    def test_cache_missing_or_invalid_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(home=tmp)
            bundled = descriptor.load(args)
            state.write_json(Path(tmp) / 'releases/current.json', {'bad': True})
            self.assertEqual(descriptor.load(args), bundled)
            with patch('urllib.request.urlopen', side_effect=OSError('offline')):
                self.assertEqual(descriptor.load(args, fetch=True), bundled)

    def test_fetch_validates_before_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(home=tmp)
            value = descriptor.load(args)
            value['release_id'] = 'test-release'
            with patch.dict('os.environ', REAX_OFFLINE='0'), patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(json.dumps(value).encode())):
                descriptor.begin(args)
                self.assertEqual(descriptor.load(args, fetch=True)['release_id'], 'test-release')
            self.assertEqual(descriptor.load(args)['release_id'], 'test-release')

    def test_registered_starting_soon_records_netuid_without_opening_gates(self):
        value = registered_pending_release()
        self.assertEqual(value['status']['launch_status'], 'registered_starting_soon')
        self.assertEqual(value['status']['netuid'], 92)
        self.assertIsNone(value['status']['testnet_netuid'])
        self.assertFalse(value['status']['live'])
        self.assertFalse(value['images']['public'])
        self.assertFalse(value['source']['public'])
        descriptor.validate(value)

    def test_info_handlers(self):
        parser = build_parser()
        for name in ['version', 'spec', 'agent-rules', 'net-policy']:
            args = parser.parse_args([name, '--json'])
            result = args.func(args)
            self.assertTrue(result.ok)
            self.assertEqual(result.to_dict()['schema'], 'reaxctl/1')

    def test_malformed_nested_cache_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(home=tmp)
            bundled = descriptor.load(args)
            for section, field, value in [('requirements', 'disk_gb', []), ('status', 'live', 'yes'), ('images', 'public', 'yes')]:
                malformed = copy.deepcopy(bundled)
                malformed[section][field] = value
                state.write_json(Path(tmp) / 'releases/current.json', malformed)
                self.assertEqual(descriptor.load(args), bundled)
