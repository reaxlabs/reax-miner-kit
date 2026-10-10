import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import copy
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from reaxlib import cmd_install as io, cmd_service as service, cmd_update as update
from reaxlib.result import Result

D = json.loads((pathlib.Path(__file__).resolve().parents[1] / 'releases/current.json').read_text())


class ServiceUpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / 'state'
        self.home.mkdir()
        self.units = pathlib.Path(self.tmp.name) / 'units'
        self.bin = pathlib.Path(self.tmp.name) / 'bin'
        self.bin.mkdir()
        for name in ('systemctl','launchctl','nvidia-smi','loginctl'):
            p = self.bin / name
            p.write_text('#!/bin/sh\nexit 0\n')
            p.chmod(0o755)
        self.path = patch.dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'])
        self.path.start()
        io.write_config(self.home, {'mode':'localnet','pool':'s1-fast','release_id':'old','manifest_id':D['pools'][0]['manifest_id']})
    def tearDown(self):
        self.path.stop()
        self.tmp.cleanup()
    def args(self, **kw):
        d = dict(home=str(self.home), action='apply', accept_manifest_change=False, mode=None, system=False, user=False, auto=False)
        d.update(kw)
        return SimpleNamespace(**d)
    def test_manifest_confirmation_never_installs(self):
        d = copy.deepcopy(D)
        d['pools'][0]['manifest_id'] = 'new'
        with patch.object(io, 'descriptor', return_value=d), patch.object(io, 'install') as install:
            self.assertEqual(update.handle(self.args()).code(), 11)
            install.assert_not_called()
    def test_failed_update_restores_previous(self):
        previous = (self.home / 'config.json').read_text()
        (self.home / 'compose.yml').write_text('original digest')
        def broken(args):
            (self.home / 'config.json').write_text('{}')
            (self.home / 'compose.yml').write_text('bad')
            return Result('install','error','download failed',exit_code=13)
        with patch.object(io, 'descriptor', return_value=D), patch.object(io, 'install', side_effect=broken):
            result = update.handle(self.args())
        self.assertEqual(result.code(), 13)
        self.assertEqual((self.home / 'config.json').read_text(), previous)
        self.assertEqual((self.home / 'compose.yml').read_text(), 'original digest')
        self.assertTrue((pathlib.Path(result.data['rollback']) / 'images.json').exists())
    def test_system_privilege_returns_exact_sudo_step(self):
        with patch.object(service, 'location', return_value=(self.units, False)), patch.object(os, 'geteuid', return_value=1000), patch.object(io, 'runner') as run:
            result = service.handle(self.args(action='install'))
        self.assertEqual(result.code(), 11)
        self.assertIn('sudo ', result.needs_human['steps'][0])
        run.assert_not_called()
    def test_user_unit_install_uninstall_with_fake_systemctl(self):
        with patch.object(service, 'location', return_value=(self.units, True)):
            result = service.handle(self.args(action='install'))
            self.assertEqual(result.code(), 11)  # no lingering on this fake host
            text = (self.units / 'reax-miner.service').read_text()
            self.assertNotIn('@HOME@', text)
            self.assertIn(' run --home ', text)
            self.assertEqual(service.handle(self.args(action='uninstall')).code(), 0)
        self.assertFalse((self.units / 'reax-miner.service').exists())
    def test_launchd_rehearsal_template(self):
        import plistlib
        with patch.object(service, 'location', return_value=(self.units, None)):
            result = service.handle(self.args(action='install'))
        self.assertEqual(result.code(), 0)
        data = plistlib.loads((self.units / 'dev.reax.miner.plist').read_bytes())
        self.assertEqual(data['ProgramArguments'][1], 'run')
    def test_timer_uses_auto(self):
        with patch.object(service, 'location', return_value=(self.units, True)):
            result = update.timer(self.args(timer_action='install'))
        self.assertEqual(result.code(), 0)
        self.assertIn('--auto --json', (self.units / 'reax-miner-update.service').read_text())
        self.assertIn('RandomizedDelaySec=1h', (self.units / 'reax-miner-update.timer').read_text())
