import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import copy
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from reaxlib import cmd_install as install
from release_fixtures import live_private_release, registered_pending_release

ROOT = pathlib.Path(__file__).resolve().parents[1]
D = registered_pending_release()


def args(home, **kwargs):
    return SimpleNamespace(home=str(home), mode=kwargs.get('mode', 'localnet'), pool=kwargs.get('pool', 's1-fast'), confirm_mainnet=kwargs.get('confirm_mainnet', False), **{k:v for k,v in kwargs.items() if k not in ('mode','pool','confirm_mainnet')})


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / 'state'
        envpatch = patch.dict(os.environ, REAX_WALLET_DIR='')
        envpatch.start()
        self.addCleanup(envpatch.stop)
        patcher = patch.object(install.sys, 'version_info', (3, 10, 12))  # tools need Python 3.10 even if the controller runs on 3.8
        patcher.start()
        self.addCleanup(patcher.stop)
    def tearDown(self):
        self.tmp.cleanup()
    def test_launch_gate_creates_nothing(self):
        d = registered_pending_release()
        with patch.object(install, 'descriptor', return_value=d), patch.object(install, 'runner') as runner:
            self.assertEqual(install.install(args(self.home, mode='mainnet')).code(), 12)
            self.assertEqual(install.install(args(self.home, mode='mainnet', confirm_mainnet=True)).code(), 12)
            self.assertEqual(install.install(args(self.home, mode='testnet')).code(), 12)
            runner.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_live_private_install_refuses_without_running_children(self):
        with patch.object(install, 'descriptor', return_value=live_private_release()), \
                patch.object(install, 'runner', return_value=(0, '', '')) as runner, \
                patch.object(install, 'prepare_tools') as prepare_tools, \
                patch.object(install.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 ** 15)), \
                patch.dict(os.environ, REAX_SOURCE_DIR=''):
            result = install.install(args(self.home, mode='mainnet', confirm_mainnet=True))
        self.assertEqual(result.code(), 12)
        self.assertIn('not yet marked available', result.summary.lower())
        self.assertEqual([call.args[0] for call in runner.call_args_list], [['docker', 'info'], ['docker', 'compose', 'version']])
        prepare_tools.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_network_routing_requires_live_and_uses_network_specific_netuid(self):
        d = copy.deepcopy(D)
        d['status'].update(netuid=92, testnet_netuid=41, live=True)
        self.assertIsNone(install.gate(d, 'mainnet', confirm=True))
        self.assertIsNone(install.gate(d, 'testnet'))
        d['status']['testnet_netuid'] = None
        self.assertEqual(install.gate(d, 'testnet').code(), 12)
        d['status'].update(testnet_netuid=41, live=False)
        self.assertEqual(install.gate(d, 'mainnet', confirm=True).code(), 12)
    def test_confirmation_is_separate(self):
        d = copy.deepcopy(D)
        d['status'].update(live=True, netuid=1)
        with patch.object(install, 'descriptor', return_value=d):
            self.assertEqual(install.install(args(self.home, mode='mainnet')).code(), 11)
        self.assertFalse(self.home.exists())
    def test_localnet_idempotent_private_permissions(self):
        source = pathlib.Path(self.tmp.name) / 'source'
        (source / 'scripts/localnet').mkdir(parents=True)
        (source / 'scripts/localnet/start.sh').touch()
        with patch.object(install, 'descriptor', return_value=D), patch.object(install, 'prepare_tools', return_value=None), patch.dict(os.environ, REAX_SOURCE_DIR=str(source)):
            self.assertTrue(install.install(args(self.home)).ok)
            secret = (self.home / 'secrets/runtime-token').read_text()
            self.assertTrue(install.install(args(self.home)).ok)
            self.assertEqual(secret, (self.home / 'secrets/runtime-token').read_text())
            self.assertEqual((self.home / 'secrets/runtime-token').stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / 'compose.yml').exists())
    def test_render_digests_and_least_wallet_mounts(self):
        self.home.mkdir()
        d = copy.deepcopy(D)
        d['images']['digests'] = {'miner':'sha256:'+'a'*64, 's1_fast_runtime':'sha256:'+'b'*64}
        install.render(self.home, d, {'pool':'s1-fast','network':'finney','netuid':4, 'wallet_path':'/owned/wallets','wallet_name':'x','hotkey_name':'h'})
        text = (self.home / 'compose.yml').read_text()
        self.assertIn('@sha256:'+'a'*64, text)
        self.assertIn('@sha256:'+'b'*64, text)
        self.assertIn('/owned/wallets/x/hotkeys/h:', text)
        self.assertIn('/owned/wallets/x/coldkeypub.txt:', text)
        self.assertNotIn('/owned/wallets:/', text)
        self.assertNotIn('ports:', text)
        self.assertNotIn('/coldkey:', text)
        self.assertEqual(text.count('network_mode: host'), 2)
        self.assertIn('http://127.0.0.1:8101', (self.home / 'miner.env').read_text())
        self.assertEqual((self.home / 'miner.env').stat().st_mode & 0o777, 0o600)
    def test_wallet_mount_cannot_escape_to_coldkey(self):
        self.home.mkdir()
        with self.assertRaises(ValueError):
            install.render(self.home, D, {'pool':'s1-fast','network':'finney','netuid':4,'hotkey_name':'../../coldkey'})
    def test_tools_satisfied_skips_pip(self):
        (self.home / 'venv/bin').mkdir(parents=True)
        (self.home / 'venv/bin/python').touch()
        (self.home / 'venv/bin/pip').touch()
        with patch.object(install, 'runner', return_value=(0,'','')) as run:
            install.prepare_tools(self.home, D)
        self.assertEqual(run.call_count, 1)
        self.assertIn('bittensor==11.1.0', run.call_args[0][0])
    def test_venv_without_pip_is_recreated(self):
        # python3 -m venv without ensurepip leaves bin/python but no pip behind
        (self.home / 'venv/bin').mkdir(parents=True)
        (self.home / 'venv/bin/python').touch()
        with patch.object(install, 'runner', return_value=(0,'','')) as run:
            install.prepare_tools(self.home, D)
        self.assertEqual(run.call_args_list[0][0][0][-3:], ['venv', '--clear', str(self.home / 'venv')])
    def test_missing_venv_support_names_package(self):
        with patch.object(install, 'runner', return_value=(1,'','')):
            result = install.prepare_tools(self.home, D)
        self.assertEqual(result.code(), 10)
        self.assertIn('python3-venv', result.summary)
    def test_failed_tool_install_reports_detail(self):
        (self.home / 'venv/bin').mkdir(parents=True)
        (self.home / 'venv/bin/python').touch()
        (self.home / 'venv/bin/pip').touch()
        with patch.object(install, 'runner', side_effect=[(1,'',''), (1,'','Collecting x\nERROR: No matching distribution found for bittensor==11.1.0\n')]):
            result = install.prepare_tools(self.home, D)
        self.assertEqual(result.code(), 13)
        self.assertEqual(result.data['detail'], 'ERROR: No matching distribution found for bittensor==11.1.0')
        self.assertIn('detail: ERROR: No matching', result.render_text())
    def test_failed_source_clone_reports_git_reason(self):
        d = copy.deepcopy(D)
        d['source']['public'] = True
        err = "Cloning into 'source'...\nfatal: unable to access 'https://github.com/reaxlabs/reax-subnet/': Could not resolve host: github.com\n"
        with patch.dict(install.os.environ, {'REAX_SOURCE_DIR': ''}), patch.object(install, 'runner', return_value=(128,'',err)):
            source, result = install.prepare_source(self.home, d)
        self.assertIsNone(source)
        self.assertIn('Could not resolve host', result.data['detail'])
    def test_prelaunch_localnet_install_names_only_the_source(self):
        with patch.dict(install.os.environ, {'REAX_SOURCE_DIR': ''}), patch.object(install, 'descriptor', return_value=D), patch.object(install, 'runner') as run:
            result = install.install(args(self.home, mode='localnet'))
        self.assertEqual(result.code(), 12)
        self.assertIn('mark the REAX source as public', result.summary)
        run.assert_not_called()
    def test_localnet_install_requires_tarfile_filters(self):
        with patch.dict(install.os.environ, {'REAX_SOURCE_DIR': '/src'}), patch.object(install, 'descriptor', return_value=D), \
                patch.object(install, 'tarfile', SimpleNamespace()), patch.object(install, 'runner') as run:
            result = install.install(args(self.home, mode='localnet'))
        self.assertEqual(result.code(), 10)
        self.assertIn('3.11.4+', result.summary)
        run.assert_not_called()
    def test_tool_detail_masks_tokens(self):
        self.assertEqual(install.detail('fatal: bad ghp_' + 'A' * 20), 'fatal: bad ghp_***')
        self.assertEqual(install.detail('fatal: bad gho_' + 'B' * 20), 'fatal: bad gho_***')
        masked = install.detail("fatal: unable to access 'https://x-access-token:secret123@github.com/a/b/'")
        self.assertNotIn('secret123', masked)
        self.assertIn('https://***@github.com', masked)
        self.assertNotIn('pw123', install.detail('error: https://user:pw123@host/x'))
        self.assertNotIn('abc.def', install.detail('error: Authorization: Bearer abc.def'))
        self.assertNotIn('xyz', install.detail('error: authorization: xyz'))
        self.assertEqual(install.detail('error: token file missing'), 'error: token file missing')
    def test_symlinked_venv_is_never_cleared(self):
        target = self.home / 'elsewhere'
        target.mkdir(parents=True)
        (target / 'keep').touch()
        (self.home / 'venv').symlink_to(target)
        with patch.object(install, 'runner') as run:
            result = install.prepare_tools(self.home, D)
        self.assertEqual(result.code(), 10)
        run.assert_not_called()
        self.assertTrue((target / 'keep').exists())
    def test_low_disk_stops_before_tools(self):
        with patch.object(install, 'descriptor', return_value=D), patch.object(install.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)), patch.object(install, 'runner') as run:
            self.assertEqual(install.install(args(self.home)).code(), 10)
            run.assert_not_called()
    def test_unknown_pool(self):
        with patch.object(install, 'descriptor', return_value=D):
            self.assertEqual(install.install(args(self.home, pool='missing')).code(), 2)
