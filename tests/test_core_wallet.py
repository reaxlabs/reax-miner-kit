import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import ss58, state
from reaxlib.cmd_wallet import hotkey, coldkeypub, wallet_status, registration
from release_fixtures import registered_pending_release


class WalletTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        envpatch = patch.dict(os.environ, REAX_WALLET_DIR='')
        envpatch.start()
        self.addCleanup(envpatch.stop)
        self.patch = patch('pathlib.Path.home', return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.args = argparse.Namespace(home=str(self.root / 'reax'), mode=None, wallet_name='reax', hotkey_name='miner1')
        self.address = ss58.encode(bytes(range(32)))

    def test_hotkey_fake_backend_only_and_idempotent(self):
        calls = []
        def backend(args, root):
            calls.append(True)
            state.write_json(root / 'hotkeys' / args.hotkey_name, {'ss58Address': self.address, 'secretPhrase': 'must never be shown'})
            return self.address
        first = hotkey(self.args, backend=backend)
        second = hotkey(self.args, backend=backend)
        self.assertEqual(first.status, 'ok')
        self.assertEqual(second.data['ss58'], self.address)
        self.assertEqual(len(calls), 1)
        path = self.root / '.bittensor/wallets/reax/hotkeys/miner1'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((path.parent.parent / 'coldkey').exists())
        self.assertNotIn('must never be shown', json.dumps(first.to_dict()) + json.dumps(wallet_status(self.args).to_dict()))

    def test_missing_venv(self):
        result = hotkey(self.args)
        self.assertEqual(result.status, 'blocked')
        self.assertEqual(result.next[0]['command'], 'reaxctl install')

    def test_coldkeypub_roundtrip_and_refuse_overwrite(self):
        self.args.ss58 = self.address
        self.assertEqual(coldkeypub(self.args).status, 'ok')
        path = self.root / '.bittensor/wallets/reax/coldkeypub.txt'
        value = json.loads(path.read_text())
        self.assertEqual(value['publicKey'], '0x' + bytes(range(32)).hex())
        self.assertEqual(value['cryptoType'], 1)
        self.assertNotIn('secretPhrase', value)
        self.assertEqual(coldkeypub(self.args).status, 'ok')
        self.args.ss58 = ss58.encode(b'\x01' * 32)
        self.assertEqual(coldkeypub(self.args).status, 'blocked')
        self.assertEqual(json.loads(path.read_text()), value)

    def test_bad_address_and_phrase_are_not_echoed(self):
        for address in ['bad-address', ' '.join(['secret'] * 12), ' '.join(['secret'] * 24)]:
            self.args.ss58 = address
            result = coldkeypub(self.args)
            self.assertEqual(result.code(), 2)
            self.assertNotIn(address, json.dumps(result.to_dict()))
        self.assertIn('recovery phrase', result.summary)

    def test_registration_unlaunched_needs_human(self):
        self.args.mode = 'mainnet'
        with patch('reaxlib.cmd_wallet.descriptor.load', return_value=registered_pending_release()):
            result = registration(self.args)
        self.assertEqual(result.status, 'needs_human')
        self.assertEqual(result.code(), 12)
        self.assertEqual(result.needs_human['link'], 'https://reax.dev/register/')

    def test_name_path_escape(self):
        self.args.wallet_name = '../escape'
        self.args.ss58 = self.address
        self.assertEqual(coldkeypub(self.args).code(), 2)

    def test_localnet_hook(self):
        with patch('reaxlib.cmd_wallet.importlib.import_module', side_effect=ModuleNotFoundError('not installed')):
            self.assertEqual(registration(self.args).status, 'ok')


class SS58Tests(unittest.TestCase):
    def test_known_alice(self):
        key = bytes.fromhex('d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d')
        self.assertEqual(ss58.decode(ss58.encode(key)), key)

    def test_checksum_and_wrong_prefix(self):
        address = ss58.encode(bytes(range(32)))
        with self.assertRaises(ValueError):
            ss58.decode(address[:-1] + ('1' if address[-1] != '1' else '2'))
        with self.assertRaises(ValueError):
            ss58.decode('1' * 48)
