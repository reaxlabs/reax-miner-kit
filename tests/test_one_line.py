import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import contextlib
import io as _io
import itertools
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reaxlib import cmd_answertest, cmd_doctor, cmd_install as io, cmd_models, cmd_runtime, cmd_selfcheck, cmd_service, cmd_up, models
from reaxlib.cli import main
from reaxlib.result import Result
from kit_fixtures import GPU_4090, HOST, base_release, example_release, live_model, release_with

DOCTOR_OK = Result('doctor', data={'blockers': [], 'nvidia_container_toolkit': True, 'docker': {'nvidia_runtime': True}})


def up_args(home, model='kev-4b', **kw):
    values = dict(home=str(home), model=model, mode=None, eu=False, gpu=None, dry_run=False, wait_minutes=20, wait_registered=0,
                  confirm_mainnet=True, no_service=False, offline=True, yes=False, json=False, purge_old=False)
    values.update(kw)
    return SimpleNamespace(**values)


class Harness(unittest.TestCase):
    """Replaces every boundary (Docker, install, self-check, service, status) with a recorder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.home = root / 'state'
        self.wallets = root / 'wallets'
        self.release = release_with(live_model(), live_model('other-4b', port=8106, commitment='reax-s1/1:other-4b:0123456789abcdef', manifest_id='0123456789abcdef'))
        self.compose_calls = []
        self.compose_rc = {}          # tuple(args) -> rc
        self.installs = []
        self.install_result = None    # replace the default Result
        self.selfchecks = 0
        self.selfcheck_result = Result('selfcheck', summary='Reference self-check passed.')
        self.answer_result = Result('answer-test', summary='Answer test passed.')
        self.runtime = ['healthy']    # successive runtime_state() answers; the last repeats
        self.status = {'running': True, 'registered': True, 'commitment_published': True, 'last_validator_score': 0.9}
        self.diagnosis = DOCTOR_OK
        self.service = Result('service install', summary='Service installed.')
        self.unit_calls = []
        self.unit_was_active = False
        self.host = (HOST, GPU_4090)
        self.sleeps = []
        self.logs = []
        for target, kwargs in (
                (patch.object(io, 'descriptor', side_effect=lambda home: self.release), {}),
                (patch.object(cmd_models, 'host_view', side_effect=lambda a, r: self.host), {}),
                (patch.object(io, 'install', side_effect=self.fake_install), {}),
                (patch.object(cmd_runtime, 'compose', side_effect=self.compose), {}),
                (patch.object(cmd_selfcheck, 'runtime_state', side_effect=self.runtime_state), {}),
                (patch.object(cmd_selfcheck, 'selfcheck', side_effect=self.selfcheck), {}),
                (patch.object(cmd_answertest, 'answer_test', side_effect=lambda *a, **k: self.answer_result), {}),
                (patch.object(cmd_runtime, 'status_data', side_effect=lambda home, config: dict(self.status)), {}),
                (patch.object(cmd_doctor, 'diagnose', side_effect=lambda a, release=None: self.diagnosis), {}),
                (patch.object(cmd_service, 'handle', side_effect=lambda a: self.service), {}),
                (patch.object(cmd_up, 'unit_active', side_effect=lambda a: self.unit_was_active), {}),
                (patch.object(cmd_up, 'unit_do', side_effect=lambda a, action: self.unit_calls.append(action)), {}),
                (patch.object(cmd_up, 'sleep', side_effect=self.sleeps.append), {}),
                (patch.object(cmd_up, 'log', side_effect=self.logs.append), {}),
                (patch.object(cmd_up, 'clock', side_effect=itertools.count(0, 30)), {}),
                (patch.dict(_os.environ, REAX_WALLET_DIR=str(self.wallets)), {})):
            target.start()
            self.addCleanup(target.stop)

    # fakes ---------------------------------------------------------------
    def compose(self, home, *args, **kwargs):
        self.compose_calls.append(args)
        return self.compose_rc.get(args, 0), '', 'compose said no' if self.compose_rc.get(args) else ''

    def runtime_state(self, home):
        return self.runtime.pop(0) if len(self.runtime) > 1 else self.runtime[0]

    def selfcheck(self, args, home=None, config=None, start=None):
        self.selfchecks += 1
        return self.selfcheck_result

    def fake_install(self, args):
        self.installs.append((args.pool, args.mode, getattr(args, 'gpu', None), getattr(args, 'accept_manifest_change', False)))
        if self.install_result is not None:
            return self.install_result
        home = pathlib.Path(args.home)
        entry = models.get(self.release, args.pool)
        old = io.read_config(home)
        io.write_config(home, dict(old, mode=args.mode, pool=args.pool, release_id=self.release['release_id'], manifest_id=entry['manifest_id'], network='finney', netuid=92,
                                   **({'gpu_index': args.gpu} if getattr(args, 'gpu', None) is not None else {})))
        (home / 'compose.yml').write_text('NEW COMPOSE ' + args.pool + '\n')
        (home / 'miner.env').write_text('NEW ENV ' + args.pool + '\n')
        io.mark(home, 'weights', entry['weights'])
        io.mark(home, 'images', {})
        return Result('install', summary='Installation prepared.')

    def make_wallet(self):
        hot = self.wallets / 'reax' / 'hotkeys'
        hot.mkdir(parents=True)
        (hot / 'miner1').write_text('{}')
        (self.wallets / 'reax' / 'coldkeypub.txt').write_text('{}')

    def preinstall(self, pool='other-4b', mode='mainnet', **extra):
        """A finished setup of `pool` as `up` would leave it."""
        self.home.mkdir(parents=True, exist_ok=True)
        entry = models.get(self.release, pool)
        io.write_config(self.home, dict(mode=mode, pool=pool, release_id=self.release['release_id'], manifest_id=entry['manifest_id'], network='finney', netuid=92, **extra))
        (self.home / 'compose.yml').write_text('OLD COMPOSE\n')
        (self.home / 'miner.env').write_text('OLD ENV\n')
        io.mark(self.home, 'weights', entry['weights'])
        io.mark(self.home, 'images', {})
        old = self.home / 'weights' / pool
        old.mkdir(parents=True, exist_ok=True)
        (old / 'model.bin').write_text('old weights')

    def run_up(self, **kw):
        return cmd_up.up(up_args(self.home, **kw))

    def run_switch(self, model='kev-4b', **kw):
        return cmd_up.switch(up_args(self.home, model=model, **kw))

    def step_status(self, result):
        return {s['id']: s['status'] for s in result.data['steps']}


class UpDryRunTest(Harness):
    def test_dry_run_changes_nothing_and_lists_the_steps(self):
        with patch.object(io, 'runner', side_effect=AssertionError('no subprocess in a dry run')):
            result = self.run_up(dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.code(), 0)
        self.assertEqual([s['id'] for s in result.data['steps']], ['doctor', 'install', 'runtime', 'selfcheck', 'wallet', 'miner', 'service', 'register'])
        self.assertTrue(result.data['dry_run'] and result.data['ready'] and not result.data['would_block'])
        self.assertEqual(result.data['commitment'], 'reax-s1/1:kev-4b:0123456789abcdef')
        self.assertEqual(self.compose_calls, [])
        self.assertEqual(self.installs, [])
        self.assertFalse(self.home.exists())

    def test_dry_run_reports_hardware_that_does_not_fit_but_still_exits_zero(self):
        self.host = (HOST, [])
        result = self.run_up(dry_run=True)
        self.assertEqual(result.code(), 0)
        self.assertFalse(result.data['ready'])
        self.assertIn('no NVIDIA GPU', result.data['would_block'][0])

    def test_dry_run_of_a_coming_model_exits_12(self):
        self.release = example_release()
        result = self.run_up(model='autotrust-decider-pro-35b', dry_run=True)
        self.assertEqual(result.code(), 12)
        self.assertIn('not live yet', result.warnings[0])
        self.assertEqual(result.data['model_status'], 'coming')

    def test_unknown_model_exits_2_and_lists_live_models(self):
        result = self.run_up(model='nonsense')
        self.assertEqual(result.code(), 2)
        self.assertIn('kev-4b', result.summary)

    def test_eu_dry_run_swaps_selfcheck_for_the_answer_test(self):
        result = self.run_up(dry_run=True, eu=True)
        self.assertEqual([s['id'] for s in result.data['steps']][3], 'answer-test')
        self.assertIn('not bit identity', result.data['eu_path']['verification'])
        self.assertTrue(any('contract' in line for line in result.data['eu_path']['steps']))

    def test_gpu_number_is_checked_against_the_machine(self):
        result = self.run_up(dry_run=True, gpu=3)
        self.assertFalse(result.data['ready'])
        self.assertIn('GPU 3 was not found', result.data['would_block'][0])
        self.assertEqual(self.run_up(gpu=99).code(), 2)

    def test_localnet_is_refused(self):
        self.assertEqual(self.run_up(mode='localnet').code(), 10)

    def test_cli_dry_run_json_offline_with_the_bundled_release(self):
        self.release = base_release()
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(['up', '--model', 's1-fast', '--dry-run', '--json', '--offline', '--home', str(self.home)])
        self.assertEqual(rc, 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result['command'], result['schema']), ('up', 'reaxctl/1'))
        self.assertEqual(result['data']['commitment'], 'reax-s1/1:s1-fast:ff6e01b983d48024')

    def test_up_and_switch_need_a_model(self):
        for command in ('up', 'switch'):
            with contextlib.redirect_stderr(_io.StringIO()), self.assertRaises(SystemExit) as caught:
                main([command, '--offline'])
            self.assertEqual(caught.exception.code, 2)


class UpRunTest(Harness):
    def test_full_setup_runs_the_steps_in_order(self):
        self.make_wallet()
        result = self.run_up()
        self.assertTrue(any('downloading weights' in line for line in self.logs))  # progress goes to stderr, never into --json
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(self.installs, [('kev-4b', 'mainnet', None, False)])
        self.assertEqual(self.compose_calls, [('up', '-d', '--no-deps', 'runtime'), ('up', '-d')])
        self.assertEqual(self.selfchecks, 1)
        self.assertEqual(self.step_status(result), {'doctor': 'ok', 'install': 'ok', 'runtime': 'ok', 'selfcheck': 'ok', 'wallet': 'ok', 'miner': 'ok', 'service': 'ok', 'register': 'ok'})
        self.assertEqual(io.read_config(self.home)['eu'], False)
        self.assertEqual(result.next[0]['command'], 'reaxctl verify')

    def test_second_run_is_a_no_op_with_status(self):
        self.make_wallet()
        self.assertTrue(self.run_up().ok)
        self.installs.clear()
        self.compose_calls.clear()
        self.selfchecks = 0
        result = self.run_up()
        self.assertTrue(result.ok)
        self.assertTrue(result.data['already_set_up'])
        self.assertEqual((self.installs, self.compose_calls, self.selfchecks), ([], [], 0))
        self.assertTrue(result.data['registered'])
        self.assertIn('already set up', result.summary)

    def test_second_run_with_stopped_runtime_continues_without_reinstalling(self):
        self.make_wallet()
        self.assertTrue(self.run_up().ok)
        self.installs.clear()
        self.compose_calls.clear()
        self.runtime = ['stopped', 'healthy']
        result = self.run_up()
        self.assertTrue(result.ok)
        self.assertEqual(self.installs, [])
        self.assertEqual(self.step_status(result)['install'], 'skipped')
        self.assertEqual(self.compose_calls[0], ('up', '-d', '--no-deps', 'runtime'))

    def test_a_different_gpu_number_is_not_the_same_setup(self):
        self.host = (HOST, GPU_4090 * 2)
        self.make_wallet()
        self.assertTrue(self.run_up(gpu=0).ok)
        self.installs.clear()
        self.assertTrue(self.run_up(gpu=0).data['already_set_up'])  # same GPU: a no-op
        self.assertEqual(self.installs, [])
        result = self.run_up(gpu=1)  # another GPU: the compose file must be rendered again
        self.assertTrue(result.ok)
        self.assertEqual([i[2] for i in self.installs], [1])

    def test_new_release_reinstalls(self):
        self.make_wallet()
        self.assertTrue(self.run_up().ok)
        self.installs.clear()
        self.release = dict(self.release, release_id='next-release')
        self.assertTrue(self.run_up().ok)
        self.assertEqual(len(self.installs), 1)

    def test_doctor_blocker_stops_with_the_fix_and_installs_nothing(self):
        self.diagnosis = Result('doctor', data={'blockers': [{'id': 'docker_client', 'message': 'Docker Engine is not installed.', 'fix': 'Install Docker Engine'},
                                                           {'id': 'ntp', 'message': 'Clock.', 'fix': 'Enable NTP'}],
                                                'nvidia_container_toolkit': True, 'docker': {'nvidia_runtime': True}})
        result = self.run_up()
        self.assertEqual(result.code(), 10)
        self.assertIn('Docker Engine is not installed. Fix: Install Docker Engine', result.summary)
        self.assertEqual(len(result.data['blockers']), 2)
        self.assertEqual(self.installs, [])
        self.assertEqual(result.next[0]['command'], 'reaxctl up --model kev-4b')

    def test_localnet_only_blocker_is_ignored_and_portfolio_models_get_the_fit_check(self):
        self.diagnosis = Result('doctor', data={'blockers': [{'id': 'python_tarfile', 'message': 'x', 'fix': 'y'}], 'nvidia_container_toolkit': True, 'docker': {'nvidia_runtime': True}})
        self.make_wallet()
        self.assertTrue(self.run_up().ok)
        import shutil
        shutil.rmtree(str(self.home))
        self.host = (HOST, [])  # a portfolio model: up applies models.fit itself
        result = self.run_up(model='other-4b')
        self.assertEqual(result.code(), 10)
        self.assertIn('no NVIDIA GPU', result.summary)

    def test_missing_nvidia_toolkit_blocks_a_portfolio_model(self):
        self.diagnosis = Result('doctor', data={'blockers': [], 'nvidia_container_toolkit': False, 'docker': {'nvidia_runtime': False}})
        result = self.run_up()
        self.assertEqual(result.code(), 10)
        self.assertIn('NVIDIA container runtime', result.summary)

    def test_install_failure_stops(self):
        self.install_result = Result('install', 'error', 'Pinned weights download failed; retry install.', exit_code=13)
        result = self.run_up()
        self.assertEqual((result.command, result.code()), ('up', 13))
        self.assertEqual(result.data['steps'][-1]['id'], 'install')
        self.assertEqual(self.compose_calls, [])

    def test_runtime_that_never_loads_stops_with_one_next_step(self):
        self.runtime = ['starting']
        result = self.run_up(wait_minutes=1)
        self.assertEqual(result.code(), 10)
        self.assertIn('still loading', result.summary)
        self.assertEqual(result.next[0]['command'], 'reaxctl up --model kev-4b')
        self.assertTrue(self.sleeps)
        self.assertEqual(self.selfchecks, 0)

    def test_runtime_that_exits_is_an_error(self):
        self.runtime = ['stopped']
        result = self.run_up()
        self.assertEqual(result.code(), 13)
        self.assertEqual(result.next[0]['command'], 'reaxctl logs --lines 200')

    def test_failed_selfcheck_stops_before_the_miner(self):
        self.make_wallet()
        self.selfcheck_result = Result('selfcheck', 'error', 'Reference self-check failed: 3/10 answered. Do not register.', exit_code=20)
        result = self.run_up()
        self.assertEqual((result.command, result.code()), ('up', 20))
        self.assertIn('Do not register', result.summary)
        self.assertNotIn(('up', '-d'), self.compose_calls)
        self.assertEqual(result.data['steps'][-1]['id'], 'selfcheck')

    def test_missing_wallet_is_the_human_step_and_the_miner_is_not_started(self):
        result = self.run_up()
        self.assertEqual(result.code(), 11)
        self.assertEqual(result.status, 'needs_human')
        steps = ' '.join(result.needs_human['steps'])
        self.assertIn('reaxctl wallet hotkey', steps)
        self.assertIn('reaxctl wallet coldkeypub --ss58', steps)
        self.assertIn('Run again: reaxctl up --model kev-4b', steps)
        self.assertNotIn('coldkey phrase', steps)
        self.assertNotIn(('up', '-d'), self.compose_calls)

    def test_hotkey_present_but_coldkeypub_missing_asks_only_for_the_public_address(self):
        self.make_wallet()
        (self.wallets / 'reax' / 'coldkeypub.txt').unlink()
        result = self.run_up()
        self.assertEqual(result.code(), 11)
        steps = result.needs_human['steps']
        self.assertEqual(steps[0], 'Hotkey: found')
        self.assertIn('--ss58', steps[1])

    def test_unregistered_hotkey_ends_with_the_registration_handoff_but_the_miner_runs(self):
        self.make_wallet()
        self.status = {'running': True, 'registered': None, 'commitment_published': None}
        with patch.object(cmd_up.cmd_wallet, 'registration', return_value=Result('register', 'needs_human', 'x', data={'hotkey_ss58': '5Fhot'},
                                                                                needs_human={'action': 'Fund and register the hotkey', 'why': 'w', 'steps': ['Open https://reax.dev/register/']}, exit_code=11)):
            result = self.run_up()
        self.assertEqual(result.code(), 11)
        self.assertEqual(result.needs_human['action'], 'Fund and register the hotkey')
        self.assertEqual(self.step_status(result)['miner'], 'ok')
        self.assertEqual(self.step_status(result)['register'], 'needs_human')
        self.assertEqual(result.data['hotkey_ss58'], '5Fhot')

    def test_service_trouble_is_a_warning_not_a_failure(self):
        self.make_wallet()
        self.service = Result('service install', 'error', 'Service manager refused the unit; inspect its logs.', exit_code=10)
        result = self.run_up()
        self.assertTrue(result.ok)
        self.assertEqual(self.step_status(result)['service'], 'warning')
        self.assertTrue(any('No service installed' in w for w in result.warnings))

    def test_no_service_flag(self):
        self.make_wallet()
        with patch.object(cmd_service, 'handle', side_effect=AssertionError('no service')):
            result = self.run_up(no_service=True)
        self.assertEqual(self.step_status(result)['service'], 'skipped')

    def test_another_installed_model_points_to_switch(self):
        self.preinstall('other-4b')
        result = self.run_up(model='kev-4b')
        self.assertEqual(result.code(), 11)
        self.assertEqual(result.needs_human['steps'], ['reaxctl switch --model kev-4b'])
        self.assertEqual(self.installs, [])

    def test_mainnet_needs_the_confirmation_flag(self):
        result = self.run_up(confirm_mainnet=False)
        self.assertEqual(result.code(), 11)
        self.assertEqual(self.installs, [])

    def test_coming_model_is_refused_with_exit_12(self):
        self.release = example_release()
        result = self.run_up(model='autotrust-decider-pro-35b')
        self.assertEqual(result.code(), 12)
        self.assertEqual(self.installs, [])

    def test_gpu_number_reaches_install(self):
        self.host = (HOST, GPU_4090 * 2)
        self.make_wallet()
        self.assertTrue(self.run_up(gpu=1).ok)
        self.assertEqual(self.installs[0][2], 1)

    def test_eu_runs_the_answer_test_and_skips_the_bit_identity_selfcheck(self):
        self.make_wallet()
        self.answer_result = Result('answer-test', summary='Answer test passed: 10/10 answered.')
        result = self.run_up(eu=True)
        self.assertTrue(result.ok)
        self.assertEqual(self.selfchecks, 0)
        self.assertEqual(self.step_status(result)['answer-test'], 'ok')
        self.assertTrue(io.read_config(self.home)['eu'])
        self.assertIn('eu_path', result.data)
        self.assertIn('EU operator path (answer test (fidelity of the answers), not bit identity):', result.data['text_lines'])
        self.assertIn('answer-test', ' '.join(result.data['text_lines']))

    def test_eu_without_the_answer_test_continues_with_a_warning(self):
        self.make_wallet()
        self.answer_result = Result('answer-test', 'blocked', 'The answer test (reax-answertest) is not part of this release yet.', exit_code=12)
        result = self.run_up(eu=True)
        self.assertTrue(result.ok)
        self.assertEqual(self.step_status(result)['answer-test'], 'skipped')
        self.assertTrue(any('not part of this release yet' in w for w in result.warnings))
        self.assertEqual(self.selfchecks, 0)

    def test_eu_failed_answer_test_stops(self):
        self.make_wallet()
        self.answer_result = Result('answer-test', 'error', 'Answer test failed: 6/10 answered.', exit_code=20)
        result = self.run_up(eu=True)
        self.assertEqual(result.code(), 20)
        self.assertNotIn(('up', '-d'), self.compose_calls)


class SwitchTest(Harness):
    def setUp(self):
        super().setUp()
        self.preinstall('other-4b')

    def files(self):
        return {name: (self.home / name).read_text() for name in ('compose.yml', 'miner.env')}

    def test_switch_swaps_model_keeps_old_weights_and_prints_the_commitment(self):
        result = self.run_switch()
        self.assertTrue(result.ok, result.summary)
        self.assertEqual(io.read_config(self.home)['pool'], 'kev-4b')
        self.assertEqual(self.installs, [('kev-4b', 'mainnet', None, True)])
        self.assertEqual(self.compose_calls, [('down',), ('up', '-d', '--no-deps', 'runtime'), ('up', '-d')])
        self.assertEqual(self.selfchecks, 1)
        self.assertEqual(self.files(), {'compose.yml': 'NEW COMPOSE kev-4b\n', 'miner.env': 'NEW ENV kev-4b\n'})
        self.assertEqual(result.data['commitment'], 'reax-s1/1:kev-4b:0123456789abcdef')
        self.assertEqual(result.data['commitment_before'], 'reax-s1/1:other-4b:0123456789abcdef')
        self.assertIn('reax-commit miner show', result.data['check_commitment'])
        self.assertIn('Commitment now: reax-s1/1:kev-4b:0123456789abcdef', result.data['text_lines'])
        self.assertTrue((self.home / 'weights' / 'other-4b' / 'model.bin').exists())
        self.assertTrue(result.data['old_weights_kept'])
        self.assertTrue(any('score' in w and 'starts from zero' in w for w in result.warnings))

    def test_purge_old_removes_only_the_old_weights(self):
        (self.home / 'weights' / 'kev-4b').mkdir()
        (self.home / 'weights' / 'kev-4b' / 'new.bin').write_text('new')
        result = self.run_switch(purge_old=True)
        self.assertTrue(result.ok)
        self.assertFalse((self.home / 'weights' / 'other-4b').exists())
        self.assertTrue((self.home / 'weights' / 'kev-4b' / 'new.bin').exists())
        self.assertFalse(result.data['old_weights_kept'])

    def test_purge_old_refuses_symlinks_and_odd_ids(self):
        outside = pathlib.Path(self.tmp.name) / 'precious'
        outside.mkdir()
        (outside / 'keep').write_text('x')
        link = self.home / 'weights' / 'linked'
        link.symlink_to(outside)
        self.assertFalse(cmd_up.purge_old(self.home, 'linked'))
        self.assertFalse(cmd_up.purge_old(self.home, '../state'))
        self.assertTrue((outside / 'keep').exists())

    def test_selfcheck_failure_rolls_back_to_the_old_setup(self):
        self.selfcheck_result = Result('selfcheck', 'error', 'Reference self-check failed: 2/10 answered.', exit_code=20)
        result = self.run_switch()
        self.assertEqual((result.command, result.code()), ('switch', 20))
        self.assertTrue(result.data['rolled_back'])
        self.assertFalse(result.data['restart_failed'])
        self.assertEqual(result.data['failed_step'], 'selfcheck')
        self.assertTrue(result.summary.startswith('Switch to kev-4b failed and other-4b was restored. Reference self-check failed'))
        self.assertEqual(self.files(), {'compose.yml': 'OLD COMPOSE\n', 'miner.env': 'OLD ENV\n'})
        self.assertEqual(io.read_config(self.home)['pool'], 'other-4b')
        self.assertEqual(io.read_config(self.home)['manifest_id'], '0123456789abcdef')
        # the half-started new runtime is stopped, then the old pair comes back
        self.assertEqual(self.compose_calls, [('down',), ('up', '-d', '--no-deps', 'runtime'), ('down',), ('up', '-d')])
        self.assertTrue((self.home / 'weights' / 'other-4b' / 'model.bin').exists())
        self.assertNotIn(('up', '-d'), self.compose_calls[:3])
        self.assertTrue(any('previous setup (other-4b) was restored' in w for w in result.warnings))
        self.assertTrue(pathlib.Path(result.data['rollback_dir']).is_dir())

    def test_runtime_that_does_not_come_up_rolls_back(self):
        self.runtime = ['unhealthy']
        result = self.run_switch()
        self.assertEqual(result.code(), 13)
        self.assertTrue(result.data['rolled_back'])
        self.assertEqual(self.files()['compose.yml'], 'OLD COMPOSE\n')
        self.assertEqual(self.compose_calls[-2:], [('down',), ('up', '-d')])
        self.assertEqual(self.selfchecks, 0)

    def test_fetch_failure_leaves_the_old_miner_running_untouched(self):
        self.install_result = Result('install', 'error', 'Pinned weights download failed; retry install.', exit_code=13)
        result = self.run_switch()
        self.assertEqual(result.code(), 13)
        self.assertTrue(result.data['rolled_back'])
        self.assertEqual(self.compose_calls, [])  # nothing was stopped
        self.assertEqual(self.files()['compose.yml'], 'OLD COMPOSE\n')

    def test_old_setup_that_cannot_restart_is_reported(self):
        self.selfcheck_result = Result('selfcheck', 'error', 'Reference self-check failed.', exit_code=20)
        self.compose_rc[('up', '-d')] = 1
        result = self.run_switch()
        self.assertTrue(result.data['restart_failed'])
        self.assertIn('could not be restarted automatically', result.summary)
        self.assertTrue(any('did not restart' in w for w in result.warnings))

    def test_miner_start_failure_rolls_back(self):
        self.compose_rc[('up', '-d')] = 1  # fails for the new miner start and the rollback restart
        result = self.run_switch()
        self.assertEqual(result.code(), 13)
        self.assertEqual(result.data['failed_step'], 'miner')
        self.assertEqual(self.files()['compose.yml'], 'OLD COMPOSE\n')

    def test_service_is_paused_during_the_switch_and_restored_after_a_rollback(self):
        self.unit_was_active = True
        self.selfcheck_result = Result('selfcheck', 'error', 'bad', exit_code=20)
        self.run_switch()
        self.assertEqual(self.unit_calls, ['stop', 'start'])

    def test_service_is_restarted_after_a_good_switch(self):
        self.unit_was_active = True
        self.assertTrue(self.run_switch().ok)
        self.assertEqual(self.unit_calls, ['stop', 'start'])

    def test_machine_that_does_not_fit_is_refused_before_anything_is_stopped(self):
        self.host = (HOST, [])
        result = self.run_switch()
        self.assertEqual(result.code(), 10)
        self.assertIn('no NVIDIA GPU', result.summary)
        self.assertEqual((self.installs, self.compose_calls), ([], []))
        self.assertEqual(io.read_config(self.home)['pool'], 'other-4b')

    def test_same_model_is_a_no_op(self):
        result = self.run_switch(model='other-4b')
        self.assertTrue(result.ok)
        self.assertFalse(result.data['changed'])
        self.assertEqual((self.installs, self.compose_calls), ([], []))

    def test_coming_and_unknown_models_are_refused(self):
        self.release = example_release()  # autotrust-decider-pro-35b is "coming"; the installed other-4b is not in it any more
        self.assertEqual(self.run_switch(model='autotrust-decider-pro-35b').code(), 12)
        self.assertEqual(self.run_switch(model='nonsense').code(), 2)

    def test_retired_target_is_refused(self):
        next(p for p in self.release['pools'] if p['id'] == 's1-cpu')['status'] = 'retired'
        result = self.run_switch(model='s1-cpu')
        self.assertEqual(result.code(), 12)
        self.assertIn('retired', result.summary)

    def test_nothing_installed_or_localnet(self):
        empty = pathlib.Path(self.tmp.name) / 'empty'
        self.assertEqual(cmd_up.switch(up_args(empty, model='kev-4b')).code(), 10)
        io.write_config(self.home, dict(io.read_config(self.home), mode='localnet'))
        self.assertEqual(self.run_switch().code(), 10)

    def test_dry_run_changes_nothing(self):
        result = self.run_switch(dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual((self.installs, self.compose_calls), ([], []))
        self.assertEqual(self.files()['compose.yml'], 'OLD COMPOSE\n')
        self.assertEqual([s['id'] for s in result.data['steps']], ['fetch', 'stop', 'runtime', 'selfcheck', 'miner'])
        self.assertEqual(result.data['commitment_before'], 'reax-s1/1:other-4b:0123456789abcdef')

    def test_old_model_that_left_the_descriptor_still_switches(self):
        self.release['models'] = [m for m in self.release['models'] if m['id'] != 'other-4b']
        result = self.run_switch()
        self.assertTrue(result.ok)
        self.assertEqual(result.data['commitment_before'], 'reax-s1/1:other-4b:0123456789abcdef')

    def test_eu_setting_is_kept_and_uses_the_answer_test(self):
        io.write_config(self.home, dict(io.read_config(self.home), eu=True))
        result = self.run_switch()
        self.assertTrue(result.ok)
        self.assertEqual(self.selfchecks, 0)
        self.assertTrue(io.read_config(self.home)['eu'])
        self.assertEqual([s['id'] for s in result.data['steps']][3], 'answer-test')


class CatalogInstallTest(unittest.TestCase):
    """The real install/render path for a portfolio model (no Docker, no network)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / 'state'
        self.release = release_with(live_model())
        self.calls = []
        for target in (patch.object(io.sys, 'version_info', (3, 10, 12)),
                       patch.dict(_os.environ, REAX_WALLET_DIR='', REAX_SOURCE_DIR=''),
                       patch.object(io, 'descriptor', side_effect=lambda home: self.release),
                       patch.object(io, 'prepare_tools', return_value=None),
                       patch.object(io.shutil, 'disk_usage', return_value=SimpleNamespace(free=10 ** 15)),
                       patch.object(io, 'runner', side_effect=self.runner)):
            target.start()
            self.addCleanup(target.stop)

    def runner(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd == ['docker', 'version', '--format', '{{.Server.Version}}']:
            return 0, '28.0.1\n', ''
        return 0, '', ''

    def install(self, **kw):
        values = dict(home=str(self.home), mode='mainnet', pool='kev-4b', confirm_mainnet=True, accept_manifest_change=False, cc_fix=False, gpu=None)
        values.update(kw)
        return io.install(SimpleNamespace(**values))

    def test_model_entry_renders_compose_and_env(self):
        result = self.install()
        self.assertTrue(result.ok, result.summary)
        compose = (self.home / 'compose.yml').read_text()
        self.assertIn('ghcr.io/reaxlabs/reax-model-kev-4b@sha256:' + 'b' * 64, compose)
        self.assertIn('RUNTIME_PORT: "8105"', compose)
        self.assertIn('REAX_MODEL: "kev-4b"', compose)
        self.assertIn('count: 1', compose)
        self.assertNotIn('device_ids', compose)
        env = dict(line.split('=', 1) for line in (self.home / 'miner.env').read_text().splitlines())
        self.assertEqual(env['REAX_MODEL'], 'kev-4b')
        self.assertEqual(env['REAX_KEV_4B_RUNTIME_URL'], 'http://127.0.0.1:8105')
        self.assertEqual(env['REAX_FEATURE_MODEL_POTS'], '1')
        self.assertEqual(io.read_config(self.home)['manifest_id'], '0123456789abcdef')
        download = next(c for c in self.calls if 'huggingface_hub' in ' '.join(c) or 'snapshot_download' in ' '.join(c))
        self.assertIn('jaredpalmer/kev-4b', download)
        self.assertIn('a' * 40, download)
        self.assertTrue((self.home / 'weights' / 'kev-4b' / ('a' * 40)).is_dir())

    def test_gpu_number_pins_the_device(self):
        self.assertTrue(self.install(gpu=1).ok)
        compose = (self.home / 'compose.yml').read_text()
        self.assertIn('device_ids: ["1"]', compose)
        self.assertNotIn('count: 1', compose)
        self.assertEqual(io.read_config(self.home)['gpu_index'], 1)

    def test_packaged_runtime_prefetches_primary_and_base_and_exposes_only_loopback(self):
        model = self.release['models'][0]
        model['runtime'].pop('env')
        model['runtime']['contract'] = models.PACKAGED_RUNTIME
        model['weights'].update(cache_id='kev-4b', files=['adapter_config.json', 'tokenizer/tokenizer.json'],
                                dependencies=[{'cache_id': 'kev-base', 'repo': 'Qwen/Qwen3.5-4B-Base',
                                               'revision': 'd' * 40, 'files': ['config.json', 'model.safetensors']}])
        result = self.install()
        self.assertTrue(result.ok, result.summary)
        runtime, miner = (self.home / 'compose.yml').read_text().split('  miner:\n')
        self.assertIn('"127.0.0.1:8105:8080"', runtime)
        self.assertNotIn('network_mode: host', runtime)
        self.assertIn('network_mode: host', miner)
        self.assertIn('S1M_INTERNAL_TOKEN_FILE: /run/runtime-token', runtime)
        self.assertIn('HF_HUB_OFFLINE: "1"', runtime)
        cache = self.home / 'weights' / 'kev-4b' / 'packaged'
        self.assertIn(str(cache) + ':/weights"', runtime)
        self.assertNotIn(':/model', runtime)
        downloads = [cmd[4:] for cmd in self.calls if len(cmd) > 3 and 'snapshot_download' in cmd[3]]
        self.assertEqual(downloads, [
            ['jaredpalmer/kev-4b', 'a' * 40, str(cache / 'kev-4b' / ('a' * 40)),
             'adapter_config.json', 'tokenizer/tokenizer.json'],
            ['Qwen/Qwen3.5-4B-Base', 'd' * 40, str(cache / 'kev-base' / ('d' * 40)),
             'config.json', 'model.safetensors'],
        ])

    def test_packaged_runtime_rejects_escape_paths_and_binding_overrides(self):
        model = self.release['models'][0]
        model['runtime'].update(contract=models.PACKAGED_RUNTIME, env={'RUNTIME_ALLOW_NO_AUTH': '1'})
        model['weights'].update(cache_id='kev-4b', files=['tokenizer/../weights.bin'])
        with self.assertRaisesRegex(ValueError, 'packaged weights file list'):
            io.render(self.home, self.release, {'pool': 'kev-4b'})
        model['weights']['files'] = ['adapter_config.json']
        with self.assertRaisesRegex(ValueError, 'built-in settings'):
            io.render(self.home, self.release, {'pool': 'kev-4b'})

    def test_coming_model_is_refused_before_any_command_runs(self):
        coming = live_model(status='coming')
        coming['weights']['revision'] = 'TBD-kev'
        coming['runtime']['digest'] = 'sha256:TBD-kev'
        coming['manifest_id'] = 'TBD-kev'
        coming['commitment'] = 'reax-s1/1:kev-4b:TBD-kev'
        self.release = release_with(coming)
        result = self.install()
        self.assertEqual(result.code(), 12)
        self.assertIn('not live yet', result.summary)
        self.assertEqual(self.calls, [])

    def test_miner_port_cannot_be_a_model_sidecar_port(self):
        self.home.mkdir()
        io.write_config(self.home, {'miner_port': 8105})
        with self.assertRaises(ValueError):
            self.install()

    def test_legacy_pool_install_is_unchanged(self):
        result = self.install(pool='s1-fast')
        self.assertTrue(result.ok, result.summary)
        compose = (self.home / 'compose.yml').read_text()
        self.assertIn('reax-s1-fast-runtime@sha256:' + base_release()['images']['digests']['s1_fast_runtime'].split(':')[1], compose)
        self.assertIn('count: 1', compose)


if __name__ == '__main__':
    unittest.main()
