import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
"""Regression checks for public kit behavior and private state boundaries."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import cmd_install, descriptor, state, ss58
from reaxlib.cmd_doctor import diagnose, plan, step_graph
from reaxlib.cmd_wallet import wallet_status
from reaxlib.cli import main
from release_fixtures import live_private_release, registered_pending_release, write_release

ROOT = Path(__file__).resolve().parents[1]


class PolishTests(unittest.TestCase):
    def test_real_commands_process_exit_12(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ('mainnet', 'testnet'):
                for command in ('install', 'register', 'start'):
                    with self.subTest(mode=mode, command=command):
                        home = Path(tmp) / (mode + '-' + command)
                        write_release(home, registered_pending_release())
                        proc = subprocess.run([sys.executable, str(ROOT / 'reaxctl'), command,
                                               '--mode', mode, '--home', str(home), '--offline', '--json'],
                                              capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 12, proc.stderr)
                        result = json.loads(proc.stdout)
                        if command == 'register':
                            self.assertIn('no real-network action was taken', result['summary'])
                        else:
                            self.assertIn('does not mark REAX as live', result['summary'])
                        self.assertFalse((home / 'config.json').exists())
                        self.assertFalse((home / 'secrets').exists())

            home = Path(tmp) / 'live-private-register'
            write_release(home, live_private_release())
            proc = subprocess.run([sys.executable, str(ROOT / 'reaxctl'), 'register',
                                   '--mode', 'mainnet', '--home', str(home), '--offline', '--json'],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 11, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result['data']['netuid'], 92)
            self.assertTrue(result['data']['available'])

    def test_per_step_costs_and_mode_blocks(self):
        for mode in ('localnet', 'testnet', 'mainnet'):
            for live in (False, True):
                steps = step_graph(mode, live=live)
                for step in steps:
                    if step['id'] == 'register':
                        if mode == 'localnet':
                            self.assertEqual(step['cost'], {'kind': 'none', 'note': 'Test TAO only.'})
                        else:
                            self.assertEqual(step['cost']['kind'], 'tao')
                            self.assertEqual(step['cost']['note'], 'The registration page shows the live fee; you approve in your own wallet')
                    else:
                        self.assertEqual(step['cost'], {'kind': 'none', 'note': 'No payment by reaxctl.'})
                    self.assertEqual(bool(step.get('blocked')), mode != 'localnet' and not live and step['id'] not in ('doctor', 'choose-mode'))

    def test_plan_network_and_disabled_pool_blocks(self):
        release = copy.deepcopy(descriptor.load('/nonexistent'))
        release['status'].update(live=True, netuid=1, testnet_netuid=None)
        with patch('reaxlib.cmd_doctor.descriptor.load', return_value=release), patch('reaxlib.cmd_doctor.diagnose') as fake:
            fake.return_value.data = {}
            result = plan(argparse.Namespace(home='/nonexistent', mode='testnet', pool='s1-fast'))
            self.assertEqual(result.data['steps'][2]['blocked'], 'network_unavailable')
            result = plan(argparse.Namespace(home='/nonexistent', mode='mainnet', pool='s1-pro'))
            self.assertEqual(result.data['steps'][2]['blocked'], 'pool_disabled')
            result = plan(argparse.Namespace(home='/nonexistent', mode='localnet', pool='s1-pro'))
            self.assertFalse(any('blocked' in step for step in result.data['steps']))

    def test_disabled_pool_quiet_until_requested(self):
        fixture = json.loads((ROOT / 'tests/fixtures/core_4090.json').read_text())
        fixture['host']['disk_free_gb'] = 30
        for selected in (None, 's1-pro'):
            result = diagnose(argparse.Namespace(home='/nonexistent', pool=selected, probe_ip=False),
                              host=fixture['host'], probe=lambda cmd, timeout: fixture['commands'].get(' '.join(cmd), {'returncode': 1, 'stdout': '', 'stderr': ''}))
            pro = next(p for p in result.data['eligible_pools'] if p['id'] == 's1-pro')
            self.assertFalse(pro['enabled_at_launch'])
            self.assertEqual(any('s1-pro' in b['id'] for b in result.data['blockers']), selected == 's1-pro')
            if selected is None:
                self.assertIn('listed for reference', pro['reasons'][0])
                self.assertFalse(any('80 GB' in w for w in result.warnings))
            text = result.render_text()
            headings = ['Verdict', 'What works today (rehearsal)', 'What is needed for real mining', 'Next']
            self.assertEqual(sorted(headings, key=text.index), headings)
            if result.data['blockers']:
                self.assertIn('Fix:', text)

    def test_rehearsal_wallet_status_never_opens_secret_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, REAX_WALLET_DIR=str(Path(tmp) / 'regular')):
            home = Path(tmp)
            wallet = home / 'localnet/wallets/miner'
            (wallet / 'hotkeys').mkdir(parents=True)
            (wallet / 'hotkeys/default').write_text('DO NOT READ HOTKEY SECRET')
            (wallet / 'coldkey').write_text('DO NOT READ COLDKEY SECRET')
            address = ss58.encode(bytes(range(32)))
            state.write_json(wallet / 'coldkeypub.txt', {'ss58Address': address})
            state.write_json(home / 'localnet/subnet.json', {'roles': {'miner': {'hotkey': address}}})
            regular = home / 'regular/reax/hotkeys'
            regular.mkdir(parents=True)
            (regular / 'miner1').write_text('DO NOT READ REGULAR HOTKEY SECRET')
            original = Path.open
            def safe_open(path, *args, **kwargs):
                self.assertNotIn(path.name, ('default', 'coldkey', 'miner1'))
                return original(path, *args, **kwargs)
            with patch.object(Path, 'open', safe_open):
                result = wallet_status(argparse.Namespace(home=tmp, mode='localnet', wallet_name=None, hotkey_name=None))
            item = result.data['rehearsal_wallets']['wallets'][0]
            self.assertEqual(item['hotkey_ss58'], address)
            self.assertEqual(item['coldkeypub_ss58'], address)
            self.assertEqual(result.data['rehearsal_wallets']['path'], str(home / 'localnet/wallets'))
            self.assertNotIn('SECRET', json.dumps(result.to_dict()))
            self.assertIn(str(home / 'localnet/wallets'), result.render_text())

    def test_atomic_runtime_writer_preserves_unrelated_temp_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            unrelated = home / 'unrelated'
            unrelated.write_text('keep')
            (home / 'config.tmp').symlink_to(unrelated)
            cmd_install.write_json(home / 'config.json', {'ready': True})
            self.assertEqual(unrelated.read_text(), 'keep')
            self.assertEqual((home / 'config.json').stat().st_mode & 0o777, 0o600)

    def test_pool_id_traversal_rejected(self):
        for ident in ('../outside', '/outside', '.', '..', 'x/y'):
            release = copy.deepcopy(descriptor.load('/nonexistent'))
            release['requirements']['disk_gb'][ident] = 20
            release['pools'][0]['id'] = ident
            with self.assertRaises(ValueError):
                descriptor.validate(release)

    def test_home_resolution_and_control_characters(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, REAX_HOME=tmp):
            self.assertEqual(state.home(), cmd_install.home_of(argparse.Namespace(home=None)))
            self.assertTrue(state.home('relative').is_absolute())
            with self.assertRaises(ValueError):
                state.home(tmp + '\nInjectedUnit=1')

    def test_unexpected_errors_hide_exception_details(self):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with patch('reaxlib.cmd_info.version', side_effect=RuntimeError('SECRET_VALUE')), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(['version', '--json']), 1)
        self.assertNotIn('SECRET_VALUE', out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())['status'], 'error')

    def test_env_writer_does_not_follow_existing_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            unrelated = home / 'unrelated'
            unrelated.write_text('keep')
            (home / 'miner.env').symlink_to(unrelated)
            with self.assertRaises(ValueError):
                cmd_install.render(home, descriptor.load('/nonexistent'), {'pool': 's1-fast', 'network': 'finney', 'netuid': 1})
            self.assertEqual(unrelated.read_text(), 'keep')
            self.assertTrue((home / 'miner.env').is_symlink())

    def test_net_policy_tracks_rehearsal_port(self):
        from reaxlib.cmd_info import net_policy
        with tempfile.TemporaryDirectory() as tmp:
            state.write_json(Path(tmp) / 'config.json', {'mode': 'localnet', 'network': 'ws://127.0.0.1:21001'})
            result = net_policy(argparse.Namespace(home=tmp))
            self.assertIn('ws://127.0.0.1:21001', [e['url'] for e in result.data['endpoints']])

    def test_service_template_rejects_path_newlines(self):
        from reaxlib.cmd_service import render_template
        with self.assertRaises(ValueError):
            render_template('reax-miner.service', Path('/tmp/home'), '/tmp/reaxctl\nInjectedUnit=1')

    def test_archive_extraction_contains_paths_and_links(self):
        import io
        import tarfile
        from reaxlib.localnet_ops import FETCH
        if not hasattr(tarfile, 'data_filter'):
            self.skipTest('Extraction filters require the runtime Python security update')
        for name, link in (('../outside', None), ('escape', '../../outside'), ('../escape', '/etc/passwd')):
            with self.subTest(name=name, link=link), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                archive = home / 'test.tar'
                member = tarfile.TarInfo(name)
                with tarfile.open(str(archive), 'w') as stream:
                    if link is None:
                        member.size = 1
                        stream.addfile(member, io.BytesIO(b'x'))
                    else:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                        stream.addfile(member)
                script = home / 'extract.py'
                script.write_text("import tarfile,sys\nwith tarfile.open(" + repr(str(archive)) + ") as f: f.extractall(sys.argv[1])\n")
                dest = home / 'root'
                dest.mkdir()
                proc = subprocess.run([sys.executable, '-c', FETCH, str(script), str(dest)], capture_output=True, text=True)
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse((home / 'outside').exists())

    def test_private_source_blocks_rehearsal_before_any_install_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            args = argparse.Namespace(home=str(home), mode='localnet', pool='s1-fast', confirm_mainnet=False)
            env = {k: v for k, v in cmd_install.os.environ.items() if k != 'REAX_SOURCE_DIR'}
            with patch.object(cmd_install.sys, 'version_info', (3, 10, 12)), patch.dict(cmd_install.os.environ, env, clear=True):
                result = cmd_install.install(args)
            self.assertEqual(result.code(), 12)
            self.assertIn('does not yet mark the REAX source', result.summary)
            self.assertFalse(home.exists())

    def test_token_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / 'home'
            (home / 'secrets').mkdir(parents=True)
            unrelated = Path(tmp) / 'private'
            unrelated.write_text('keep')
            (home / 'secrets/runtime-token').symlink_to(unrelated)
            args = argparse.Namespace(home=str(home), mode='localnet', pool='s1-fast', confirm_mainnet=False)
            with patch.object(cmd_install.sys, 'version_info', (3, 10, 12)), patch.object(cmd_install, 'prepare_tools', return_value=None), patch.object(cmd_install, 'prepare_source', return_value=(Path(tmp), None)), patch.dict(cmd_install.os.environ, {'REAX_SOURCE_DIR': tmp}):
                result = cmd_install.install(args)
            self.assertEqual(result.code(), 10)
            self.assertEqual(unrelated.read_text(), 'keep')
