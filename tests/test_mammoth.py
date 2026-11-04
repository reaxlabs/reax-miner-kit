"""Offline regression checks for the measured mammoth start contract and Rune migration."""
import os
os.environ.setdefault('REAX_OFFLINE', '1')
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch
from reaxlib import cmd_install as io, cmd_selfcheck, cmd_update, descriptor, state

ROOT = Path(__file__).resolve().parents[1]
D = json.loads((ROOT / 'releases/current.json').read_text())
P = D['pools'][1]

class MammothTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = dict(pool='s1-pro', mode='mainnet', network='finney', netuid=92,
                           mainnet_confirmed=True, release_id=D['release_id'], manifest_id=P['manifest_id'])

    def test_render_matches_miner_run_line(self):
        # Frozen options from reax-subnet 0db24fd docs/MINER.md (runtime port 8112 -> miner 8102).
        # Keep this fixture independent of the descriptor to detect drift.
        expected = dict(S1M_INTERNAL_TOKEN_FILE='/run/runtime-token', RUNTIME_UDS='', RUNTIME_HOST='127.0.0.1',
                        RUNTIME_PORT='8102', REAX_MODEL='mammoth-1', REAX_PROFILE='reference', CIE_LAZY_LOAD='1',
                        REAX_MAX_WAIT_MS='4', FLA_CACHE_MODE='strict',
                        FLA_CONFIG_DIR='/opt/reax/fla-configs/rtx-pro-6000-blackwell-server',
                        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        # Parse the published run line as well as comparing frozen expected values.
        words = shlex.split((ROOT/'tests/fixtures/mammoth-run.sh').read_text().replace('\\\n', ' '))
        documented = dict(words[i+1].split('=', 1) for i, word in enumerate(words) if word == '-e')
        documented['S1M_INTERNAL_TOKEN_FILE'] = '/run/runtime-token'  # same read-only token, controller path
        documented['RUNTIME_PORT'] = '8102'  # validator uses 8112 during overlap; single-pool miner uses 8102
        # Offline flags are explicit in validator compose (in addition to the MINER.md run line).
        documented.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
        self.assertEqual(documented, expected)
        io.render(self.home, D, self.config)
        # The stdlib controller emits JSON-quoted scalars; inspect the environment block without a YAML dependency.
        text = (self.home / 'compose.yml').read_text()
        envtext = text.split('    environment:\n',1)[1].split('    volumes:',1)[0]
        actual = {}
        for line in envtext.splitlines():
            key, val = line.strip().split(': ',1)
            actual[key] = json.loads(val) if val.startswith('"') else val
        self.assertEqual(actual, dict(expected, TRITON_CACHE_DIR='/tmp/triton'))
        for option in ('read_only: true', '/tmp:rw,exec,nosuid,nodev,size=16g', 'shm_size: "2g"',
                       'pids_limit: 4096', 'cap_drop: [ALL]', 'no-new-privileges:true', 'init: true', 'network_mode: host'):
            self.assertIn(option, text)
        self.assertNotIn('command:', text)
        self.assertNotIn('RUNE_', text)
        self.assertIn(str(io.verified_configs(P['runtime'])) + ':' + expected['FLA_CONFIG_DIR'] + ':ro', text)
        self.assertEqual(cmd_selfcheck.miner_env(self.home)['REAX_MODEL'], 's1-pro')
        self.assertIn(P['weights']['revision'] + ':/model:ro', text)

    def test_config_bytes_match_pinned_source_receipt(self):
        directory = io.verified_configs(P['runtime'])
        # SHA256SUMS itself is pinned in the controller, not trusted from a descriptor fetch.
        self.assertEqual(hashlib.sha256((directory/'SHA256SUMS').read_bytes()).hexdigest(),
                         '2afd76102700d3a26673cd4bcc4372bbf8cf0bc548580aa5da6b4560eab0fc9d')
        for line in (directory/'SHA256SUMS').read_text().splitlines():
            digest, name = line.split()
            self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(), digest)

    def test_corrupt_config_fails_before_install_children(self):
        import shutil
        directory = self.home/'configs'/'rtx-pro-6000-blackwell-server'
        shutil.copytree(io.verified_configs(P['runtime']), directory)
        (directory/'_meta.json').write_text('corrupt')
        with patch.object(io, '__file__', str(self.home/'reaxlib/cmd_install.py')):
            with self.assertRaisesRegex(ValueError, 'checksum failed'):
                io.verified_configs(P['runtime'])

    @patch.object(io.sys, 'version_info', (3, 10, 12))
    def test_cc_fix_refuses_before_state_or_children(self):
        args = argparse.Namespace(home=str(self.home/'new'), mode='mainnet', pool='s1-pro', confirm_mainnet=True, cc_fix=True)
        with patch.object(io, 'descriptor', return_value=D), patch.object(io, 'runner') as run:
            result=io.install(args)
        self.assertEqual(result.code(),12)
        self.assertIn('unverified for mammoth',result.summary)
        run.assert_not_called()
        self.assertFalse((self.home/'new').exists())

    def test_rune_manifest_requires_confirmation_before_mutation(self):
        old=dict(self.config, manifest_id='5b11440bf57b5434', release_id='1.2.0rc11.post7')
        state.write_json(self.home/'config.json',old)
        (self.home/'compose.yml').write_text('old compose')
        before={p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()}
        args=argparse.Namespace(home=str(self.home),action='apply',accept_manifest_change=False)
        with patch.object(io,'descriptor',return_value=D),patch.object(io,'install') as install:
            result=cmd_update.handle(args)
        self.assertEqual(result.code(),11)
        install.assert_not_called()
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.home.iterdir() if p.is_file()})

    def test_failed_upgrade_restores_weights_marker_and_files(self):
        from reaxlib.result import Result
        old=dict(self.config, manifest_id='5b11440bf57b5434', release_id='1.2.0rc11.post7')
        state.write_json(self.home/'config.json',old)
        state.write_json(self.home/'state.json',dict(steps={'weights':{'info':{'repo':'surogate/rune-26b-a4b-GGUF'}}}))
        (self.home/'compose.yml').write_text('old compose')
        (self.home/'miner.env').write_text('REAX_MODEL=s1-pro\n')
        before={n:(self.home/n).read_bytes() for n in ('config.json','state.json','compose.yml','miner.env')}
        def failed(args):
            for n in before: (self.home/n).write_text('changed')
            return Result('install','error','failed',exit_code=13)
        args=argparse.Namespace(home=str(self.home),action='apply',accept_manifest_change=True)
        with patch.object(io,'descriptor',return_value=D),patch.object(io,'install',side_effect=failed),patch.object(cmd_update,'compose',return_value=(0,'','')):
            result=cmd_update.handle(args)
        self.assertEqual(result.code(),13)
        self.assertEqual(before,{n:(self.home/n).read_bytes() for n in before})

    def test_new_selfcheck_refuses_installed_rune_before_managers(self):
        old = dict(self.config, manifest_id='5b11440bf57b5434')
        (self.home/'compose.yml').write_text('old compose')
        (self.home/'miner.env').write_text('REAX_MODEL=s1-pro\n')
        args = argparse.Namespace(home=str(self.home), mode='mainnet')
        with patch.object(io, 'descriptor', return_value=D), patch.object(cmd_selfcheck, 'compose') as run:
            result = cmd_selfcheck.selfcheck(args, config=old)
        self.assertEqual(result.code(), 10)
        self.assertIn('Installed manifest differs', result.summary)
        run.assert_not_called()

    def test_other_pool_runtime_pins_unchanged(self):
        old=json.loads((ROOT/'tests/fixtures/post7-descriptor.json').read_text())
        for key in ('s1_fast_runtime','s1_lite_runtime','s1_lite_runtime_jetson','s1_cpu_runtime'):
            self.assertEqual(D['images']['digests'][key],old['images']['digests'][key])
        for ident in ('s1-fast','s1-lite','s1-cpu'):
            self.assertEqual(next(p for p in D['pools'] if p['id']==ident),next(p for p in old['pools'] if p['id']==ident))
