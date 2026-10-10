import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from release_fixtures import live_private_release
from reaxlib import cmd_install, cmd_selfcheck, localnet_ops
from reaxlib.cli import main
from reaxlib.cmd_doctor import step_graph
from reaxlib.cmd_info import COMMANDS, RULES

PASS = {'pass': True, 'items': 640, 'answered': 640, 'failed': 0, 'failures': [], 'distance_median': 0.07, 'distance_p99': 0.4,
        'distance_max': 0.8, 'items_over_tau_item': 0, 'round_scores': 1.0, 'round_strikes': 0.0, 'decisions_per_s': 40.2,
        'seconds': 15.9, 'model': 's1-fast', 'manifest_id': 'm1'}
FAIL = dict(PASS, **{'pass': False, 'distance_median': 0.9, 'distance_max': 3.1, 'round_scores': 0.4, 'round_strikes': 0.2})
MISSING = (127, '', 'docker: Error response from daemon: failed to create task for container: exec: "reax-selfcheck": executable file not found in $PATH: unknown.')


class FakeDocker:
    def __init__(self, run, health='healthy', state='running'):
        self.run, self.health, self.state, self.calls = run, health, state, []

    def __call__(self, cmd, timeout=120, **kwargs):
        self.calls.append(cmd)
        if cmd[:2] == ['docker', 'compose'] and 'ps' in cmd:
            if self.state is None:
                return 0, '[]', ''
            return 0, json.dumps([{'Service': 'runtime', 'State': self.state, 'Health': self.health}]), ''
        if cmd[:2] == ['docker', 'compose'] and 'config' in cmd:
            return 0, json.dumps({'services': {'miner': {'image': 'ghcr.io/reaxlabs/reax-miner@sha256:' + 'a' * 64}}}), ''
        if cmd[:2] == ['docker', 'compose'] and 'up' in cmd:
            self.state, self.health = 'running', 'starting'
            return 0, '', ''
        if cmd[:2] == ['docker', 'run']:
            return self.run
        return 0, '', ''


class SelfcheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        cmd_install.write_config(self.home, {'mode': 'testnet', 'pool': 's1-fast', 'miner_port': 8091})
        (self.home / 'compose.yml').touch()
        (self.home / 'miner.env').write_text('REAX_MODEL=s1-fast\nREAX_NETUID=41\nREAX_WALLET_HOTKEY=miner1\n'
                                             'REAX_RUNTIME_TOKEN_FILE=/run/runtime-token\nREAX_S1_FAST_RUNTIME_URL=http://127.0.0.1:8101\n')
        release = live_private_release()
        release['status']['testnet_netuid'] = 41
        self.release = patch.object(cmd_install, 'descriptor', return_value=release)
        self.release.start()

    def tearDown(self):
        self.release.stop()
        self.tmp.cleanup()

    def call(self, fake, *argv):
        out = io.StringIO()
        with patch.object(cmd_install, 'runner', fake), contextlib.redirect_stdout(out):
            rc = main(list(argv or ['selfcheck']) + ['--home', str(self.home), '--json'])
        return rc, json.loads(out.getvalue())

    def run_cmd(self, fake):
        return next(c for c in fake.calls if c[:2] == ['docker', 'run'])

    def test_pass_reports_numbers_and_runs_with_miner_settings_only(self):
        fake = FakeDocker((0, json.dumps(PASS) + '\n', ''))
        rc, r = self.call(fake)
        self.assertEqual(rc, 0)
        self.assertTrue(r['ok'])
        self.assertIn('640/640 answered', r['summary'])
        self.assertEqual(r['data']['round_scores'], 1.0)
        self.assertTrue(r['data']['untrusted'])
        cmd = self.run_cmd(fake)
        self.assertEqual(cmd[-4:], ['--entrypoint', 'reax-selfcheck', 'ghcr.io/reaxlabs/reax-miner@sha256:' + 'a' * 64, '--json'])
        self.assertIn('REAX_S1_FAST_RUNTIME_URL=http://127.0.0.1:8101', cmd)
        self.assertIn('REAX_MODEL=s1-fast', cmd)
        self.assertIn(str(self.home / 'secrets/runtime-token') + ':/run/runtime-token:ro', cmd)
        self.assertFalse(any('wallet' in part.lower() or 'HOTKEY' in part for part in cmd))
        self.assertNotIn('up', [c[4] for c in fake.calls if c[:2] == ['docker', 'compose']])

    def test_fail_exits_20_with_fix(self):
        rc, r = self.call(FakeDocker((1, json.dumps(FAIL), '')))
        self.assertEqual(rc, 20)
        self.assertEqual(r['status'], 'error')
        self.assertIn('0.4 score', r['summary'])
        self.assertIn('Do not register', r['summary'])

    def test_pass_flag_and_exit_code_must_agree(self):
        rc, _ = self.call(FakeDocker((1, json.dumps(PASS), '')))
        self.assertEqual(rc, 20)

    def test_cannot_run_is_blocked_with_reason(self):
        rc, r = self.call(FakeDocker((2, json.dumps({'pass': False, 'error': 'runtime sidecar at http://127.0.0.1:8101 is not healthy'}), '')))
        self.assertEqual(rc, 10)
        self.assertEqual(r['status'], 'blocked')
        self.assertIn('cannot run: runtime sidecar', r['summary'])

    def test_missing_entrypoint_asks_for_update(self):
        rc, r = self.call(FakeDocker(MISSING))
        self.assertEqual(rc, 10)
        self.assertIn('predates reax-selfcheck', r['summary'])
        self.assertEqual(r['next'][0]['command'], 'reaxctl update check')

    def test_unreadable_output_is_an_error(self):
        rc, r = self.call(FakeDocker((0, 'Traceback (most recent call last):\n', 'boom')))
        self.assertEqual(rc, 1)
        self.assertEqual(r['status'], 'error')
        self.assertIn('readable result', r['summary'])

    def test_localnet_refuses_without_running_docker(self):
        cmd_install.write_config(self.home, {'mode': 'localnet', 'pool': 's1-fast'})
        fake = FakeDocker((0, json.dumps(PASS), ''))
        rc, r = self.call(fake)
        self.assertEqual(rc, 10)
        self.assertIn('hash runtime', r['summary'])
        self.assertEqual(fake.calls, [])

    def test_unhealthy_sidecar_refuses(self):
        fake = FakeDocker((0, json.dumps(PASS), ''), health='unhealthy')
        rc, r = self.call(fake)
        self.assertEqual(rc, 10)
        self.assertIn('not healthy', r['summary'])
        self.assertFalse(any(c[:2] == ['docker', 'run'] for c in fake.calls))

    def test_stopped_sidecar_is_started_alone_then_waits(self):
        fake = FakeDocker((0, json.dumps(PASS), ''), state=None)
        rc, r = self.call(fake)
        self.assertEqual(rc, 10)
        self.assertIn('starting', r['summary'])
        up = next(c for c in fake.calls if 'up' in c)
        self.assertEqual(up[-1], 'runtime')
        self.assertFalse(any(c[:2] == ['docker', 'run'] for c in fake.calls))

    def test_no_start_leaves_sidecar_stopped(self):
        fake = FakeDocker((0, json.dumps(PASS), ''), state=None)
        rc, r = self.call(fake, 'selfcheck', '--no-start')
        self.assertEqual(rc, 10)
        self.assertFalse(any('up' in c for c in fake.calls))

    def test_requires_install(self):
        (self.home / 'miner.env').unlink()
        rc, r = self.call(FakeDocker((0, json.dumps(PASS), '')))
        self.assertEqual(rc, 10)
        self.assertIn('Run install', r['summary'])

    def test_verify_selfcheck_item_never_starts_containers(self):
        fake = FakeDocker(MISSING, state=None)
        with patch.object(localnet_ops, 'health', return_value=None):
            rc, r = self.call(fake, 'verify', '--selfcheck')
        self.assertEqual(rc, 20)
        item = next(i for i in r['data']['checklist'] if i['item'] == 'reference selfcheck')
        self.assertFalse(item['pass'])
        self.assertFalse(any('up' in c for c in fake.calls))

    def test_verify_selfcheck_pass_item(self):
        with patch.object(localnet_ops, 'health', return_value=None):
            rc, r = self.call(FakeDocker((0, json.dumps(PASS), '')), 'verify', '--selfcheck')
        item = next(i for i in r['data']['checklist'] if i['item'] == 'reference selfcheck')
        self.assertTrue(item['pass'])

    def test_plan_and_spec_list_selfcheck_before_register(self):
        self.assertIn('selfcheck', COMMANDS)
        self.assertTrue(any('selfcheck' in rule for rule in RULES))
        for mode in ('testnet', 'mainnet'):
            ids = [s['id'] for s in step_graph(mode, live=True)]
            self.assertLess(ids.index('install'), ids.index('selfcheck'))
            self.assertLess(ids.index('selfcheck'), ids.index('register'))
        self.assertNotIn('selfcheck', [s['id'] for s in step_graph('localnet')])


if __name__ == '__main__':
    unittest.main()
