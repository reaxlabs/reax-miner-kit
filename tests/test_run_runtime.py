import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import contextlib
import io
import json
import os
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from reaxlib import cmd_install, cmd_runtime, localnet_ops
from reaxlib.cli import main


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.bin = self.home / 'bin'
        self.bin.mkdir()
        docker = self.bin / 'docker'
        docker.write_text('#!/bin/sh\ncase "$*" in\n*"ps --all"*) echo \'[{"State":"running","Service":"miner"},{"State":"running","Service":"runtime"}]\';;\n*"logs"*) echo "published commitment reax-s1/1:s1-fast:test; miner uid 1 listening on 127.0.0.1; score=1.000";;\n*) exit 0;;\nesac\n')
        docker.chmod(0o755)
        self.path = patch.dict(os.environ, PATH=str(self.bin)+':'+os.environ['PATH'])
        self.path.start()
        cmd_install.write_config(self.home, {'mode':'testnet', 'pool':'s1-fast','miner_port':8091})
        (self.home / 'compose.yml').touch()
    def tearDown(self):
        self.path.stop()
        self.tmp.cleanup()
    def call(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(list(argv)+['--home',str(self.home),'--json'])
        return rc, json.loads(out.getvalue())
    def test_fake_docker_status(self):
        with patch.object(localnet_ops, 'health', return_value={'protocol':'reax-s1/1'}):
            rc, result = self.call('status')
        self.assertEqual(rc, 0)
        self.assertEqual(len(result['data']['containers']), 2)
        self.assertTrue(result['data']['commitment_published'])
    def test_stop_works_even_when_launch_closed(self):
        self.assertEqual(self.call('stop')[0], 0)
        self.assertEqual(self.call('start')[0], 12)
    def test_verify_fails_unknown_health(self):
        with patch.object(localnet_ops, 'health', return_value=None):
            rc, r = self.call('verify')
        self.assertEqual(rc, 20)
        self.assertTrue(any(not i['pass'] and i['fix'] for i in r['data']['checklist']))
    def test_localnet_verify_explains_round_wait(self):
        cmd_install.write_config(self.home, {'mode':'localnet', 'pool':'s1-fast', 'release_id':'1.1.0rc2'})
        with patch.object(localnet_ops, 'processes', return_value={}), patch.object(localnet_ops, 'health', return_value=None):
            rc, r = self.call('verify')
        self.assertEqual(rc, 20)
        rounds = next(i for i in r['data']['checklist'] if i['item'] == 'validator rounds')
        self.assertIn('rerun reaxctl verify in a minute', rounds['fix'])
    def test_verify_text_lists_failed_checks(self):
        out = io.StringIO()
        with patch.object(localnet_ops, 'health', return_value=None), contextlib.redirect_stdout(out):
            main(['verify', '--home', str(self.home)])
        self.assertIn('  failed: health: Check the miner port and runtime logs.', out.getvalue())
    def test_localnet_up_points_to_next_steps(self):
        with patch.object(localnet_ops, 'processes', return_value={}):
            result = localnet_ops.started(self.home)
        self.assertEqual(result.command, 'localnet up')
        self.assertEqual([n['command'] for n in result.next], ['reaxctl wallet status', 'reaxctl verify'])
    def test_status_text_lists_processes(self):
        with patch.object(localnet_ops, 'processes', return_value={'miner': {'pid': 1}}), patch.object(localnet_ops, 'alive', return_value=False):
            text = localnet_ops.status(self.home).render_text()
        self.assertIn('  miner: stopped', text)
    def test_localnet_fetch_failure_reports_reason(self):
        cmd_install.write_config(self.home, {'mode':'localnet', 'pool':'s1-fast'})
        (self.home / 'venv/bin').mkdir(parents=True)
        (self.home / 'venv/bin/python').touch()
        class FreeSocket:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def setsockopt(self, *a): pass
            def bind(self, address): pass
        reason = 'this Python lacks tarfile extraction filters; use Python >= 3.10.12/3.11.4/3.12'
        with patch.object(localnet_ops, 'processes', return_value={}), patch.object(localnet_ops, 'down'), \
                patch.object(cmd_install, 'descriptor'), patch.object(cmd_install, 'prepare_source', return_value=(self.home, None)), \
                patch.object(localnet_ops.socket, 'socket', FreeSocket), patch.object(cmd_install, 'runner', return_value=(1, '', reason + '\n')):
            result = localnet_ops.up(SimpleNamespace(home=str(self.home), mode=None, base_port=None, host='10.0.0.2'))
        self.assertEqual(result.code(), 13)
        self.assertEqual(result.data['detail'], reason)
        self.assertIn('fetch-node.log', result.summary)
    def test_localnet_verify_points_to_localnet_up(self):
        cmd_install.write_config(self.home, {'mode':'localnet', 'pool':'s1-fast'})
        with patch.object(localnet_ops, 'processes', return_value={}), patch.object(localnet_ops, 'health', return_value=None):
            rc, r = self.call('verify')
        self.assertEqual(r['summary'], 'Run reaxctl localnet up and inspect reaxctl logs.')
    def test_error_log_is_not_commitment_evidence(self):
        evidence = cmd_runtime.log_evidence('could not publish commitment; hotkey is not registered')
        self.assertIsNone(evidence['commitment_published'])
        self.assertIsNone(evidence['registered'])
    def test_zero_served_is_not_a_validator_round(self):
        self.assertFalse(cmd_runtime.log_evidence('served ok=0 rejected=0')['validator_rounds_seen'])
        self.assertTrue(cmd_runtime.log_evidence('served ok=1 rejected=0')['validator_rounds_seen'])
    def test_logs_line_bounds(self):
        self.assertEqual(self.call('logs', '--lines', '-2')[0], 2)
    def test_secret_log_redaction(self):
        (self.home / 'secrets').mkdir()
        (self.home / 'secrets/runtime-token').write_text('a'*64)
        self.assertNotIn('a'*64, cmd_runtime.safe_logs(self.home, 'hello '+'a'*64))
    def test_stale_pid_is_never_signalled(self):
        (self.home / 'localnet').mkdir()
        cmd_install.write_json(self.home / 'localnet/processes.json', {'x':{'pid':os.getpid(),'birth':'invalid'}})
        with patch.object(os, 'killpg') as kill:
            localnet_ops.down(self.home)
            kill.assert_not_called()
    def test_realnet_dev_funding_refused(self):
        with patch.object(cmd_install, 'runner') as run:
            self.assertEqual(localnet_ops.fund(self.home, 'address').code(), 10)
            run.assert_not_called()
    def test_invalid_baseport(self):
        cmd_install.write_config(self.home, {'mode':'localnet'})
        (self.home / 'venv/bin').mkdir(parents=True)
        (self.home / 'venv/bin/python').touch()
        with patch.object(cmd_install, 'prepare_source', return_value=(self.home, None)):
            self.assertEqual(localnet_ops.up(SimpleNamespace(home=str(self.home),mode=None,base_port=65535)).code(), 2)
