"""Offline checks for the kit-mounted mammoth autotune seed."""
import os
os.environ.setdefault('REAX_OFFLINE', '1')
import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from reaxlib import cmd_install as io, cmd_runtime, triton_seed, state
from test_s1_pro import run_doctor, bundled


class TritonSeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = dict(pool='s1-pro', mode='mainnet', network='finney', netuid=92, mainnet_confirmed=True)

    def test_seed_is_the_exact_reviewed_artifact(self):
        info = triton_seed.inspect()
        self.assertTrue(info['present'])
        self.assertTrue(info['ok'])
        self.assertEqual(info['sha256'], '51b996aa4e577fa923c4d6142b79c613a9aba2e79506dff111277ba8e8b2a326')
        self.assertEqual(Path(info['path']).stat().st_size, 816)
        self.assertEqual(triton_seed.verify(), triton_seed.DIRECTORY)

    def test_render_has_readonly_mount_and_exact_wrapper(self):
        io.render(self.home, bundled(), self.config)
        text = (self.home/'compose.yml').read_text()
        runtime, miner = text.split('  miner:', 1)
        self.assertIn(str(triton_seed.DIRECTORY) + ':/seedctl:ro', runtime)
        self.assertIn('TRITON_CACHE_DIR: /tmp/triton', runtime)
        line = next(l for l in runtime.splitlines() if l.strip().startswith('entrypoint:'))
        # Compose reduces $$ escapes back to $ before invoking the shell.
        entrypoint = json.loads(line.split('entrypoint: ', 1)[1])
        self.assertEqual([a.replace('$$', '$') for a in entrypoint], triton_seed.ENTRYPOINT)
        self.assertNotIn('/seedctl', miner)
        self.assertIn(bundled()['images']['digests']['s1_pro_runtime'], runtime)

    def test_all_other_pools_and_legacy_rune_do_not_use_seed(self):
        for pool in ('s1-fast', 's1-lite', 's1-cpu'):
            with patch.object(triton_seed, 'verify', side_effect=AssertionError('must not check')):
                io.render(self.home, bundled(), dict(self.config, pool=pool))
            self.assertNotIn('/seedctl', (self.home/'compose.yml').read_text())
            self.assertNotIn('TRITON_CACHE_DIR', (self.home/'compose.yml').read_text())
        old = copy.deepcopy(bundled())
        old['pools'][1].pop('runtime')
        io.render(self.home, old, self.config)
        self.assertNotIn('/seedctl', (self.home/'compose.yml').read_text())

    def test_missing_corrupt_and_symlink_seed_refused(self):
        directory = self.home/'seed'
        path = directory/triton_seed.FILENAME
        path.parent.mkdir(parents=True)
        for content in (None, b'corrupt', b''):
            if content is not None:
                path.write_bytes(content)
            with patch.object(triton_seed, 'DIRECTORY', directory):
                with self.assertRaisesRegex(ValueError, 'missing or checksum mismatch'):
                    triton_seed.verify()
        path.unlink()
        path.symlink_to(triton_seed.DIRECTORY/triton_seed.FILENAME)
        with patch.object(triton_seed, 'DIRECTORY', directory):
            with self.assertRaises(ValueError):
                triton_seed.verify()

    def test_corrupt_seed_blocks_install_before_children_or_state(self):
        args = argparse.Namespace(home=str(self.home/'new'), mode='mainnet', pool='s1-pro', confirm_mainnet=True)
        with patch.object(triton_seed, 'DIRECTORY', self.home/'missing'), patch.object(io, 'descriptor', return_value=bundled()), patch.object(io, 'runner') as run:
            result = io.install(args)
        self.assertEqual(result.code(), 10)
        self.assertIn('Mammoth Triton seed', result.summary)
        run.assert_not_called()
        self.assertFalse((self.home/'new').exists())

    def test_corrupt_seed_refuses_every_compose_start_path(self):
        state.write_json(self.home/'config.json', self.config)
        for action in ('up', 'restart', 'start'):
            with patch.object(triton_seed, 'DIRECTORY', self.home/'missing'), patch.object(io, 'descriptor', return_value=bundled()), patch.object(io, 'runner') as run:
                rc, _, err = cmd_runtime.compose(self.home, action)
            self.assertEqual(rc, 10)
            self.assertIn('checksum mismatch', err)
            run.assert_not_called()
        # Stopping is always allowed, including when a seed has been removed.
        with patch.object(io, 'runner', return_value=(0, '', '')) as run:
            self.assertEqual(cmd_runtime.compose(self.home, 'down')[0], 0)
        run.assert_called_once()

    def test_doctor_reports_present_and_exact_sha_for_mammoth(self):
        result = run_doctor('NVIDIA RTX PRO 6000 Blackwell Server Edition, 97887, 580.65')
        self.assertTrue(result.data['triton_seed']['present'])
        self.assertTrue(result.data['triton_seed']['ok'])
        self.assertEqual(result.data['triton_seed']['sha256'], triton_seed.SHA256)
        self.assertIn('Mammoth Triton seed: present and verified', result.render_text())
        self.assertIn(triton_seed.SHA256, result.render_text())
        self.assertNotIn('triton_seed', run_doctor('NVIDIA H100, 81559, 580.65', pool='s1-fast').data)

    def test_doctor_blocks_missing_seed_without_throwing(self):
        with patch.object(triton_seed, 'DIRECTORY', self.home/'missing'):
            result = run_doctor('NVIDIA RTX PRO 6000 Blackwell Server Edition, 97887, 580.65')
        self.assertFalse(result.data['triton_seed']['present'])
        self.assertFalse(result.data['real_mining_possible'])
        self.assertIn('triton_seed', [b['id'] for b in result.data['blockers']])

    def test_render_refuses_corrupt_seed_before_writing_compose(self):
        with patch.object(triton_seed, 'DIRECTORY', self.home/'missing'):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                io.render(self.home, bundled(), self.config)
        self.assertFalse((self.home/'compose.yml').exists())

    def test_manual_templates_include_seed_and_preflight(self):
        root = Path(__file__).resolve().parents[1]
        text = (root/'service/docker-compose.s1-pro.yml').read_text()
        self.assertIn('REAX_TRITON_SEED', text)
        self.assertIn(':/seedctl:ro', text)
        self.assertIn('TRITON_CACHE_DIR: /tmp/triton', text)
        self.assertIn('exec "$$0" "$$@"', text)
        self.assertIn('triton_seed.py && docker compose', text)
        unit = (root/'service/reax-s1-pro.service').read_text()
        self.assertIn('ExecStartPre=/usr/bin/python3 @KIT@/reaxlib/triton_seed.py', unit)
