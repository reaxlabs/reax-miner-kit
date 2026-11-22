import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import contextlib
import io as _io
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reaxlib import cmd_answertest as at, cmd_install as io
from reaxlib.cli import main

ENV = 'REAX_MODEL=kev-4b\nREAX_RUNTIME_TOKEN_FILE=/run/runtime-token\nREAX_KEV_4B_RUNTIME_URL=http://127.0.0.1:8105\n'


class AnswerTestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        io.write_config(self.home, {'mode': 'mainnet', 'pool': 'kev-4b'})
        (self.home / 'compose.yml').write_text('x\n')
        (self.home / 'miner.env').write_text(ENV)
        (self.home / 'secrets').mkdir()
        (self.home / 'secrets' / 'runtime-token').write_text('t' * 64 + '\n')
        self.runs = []
        self.reply = (0, json.dumps({'pass': True, 'items': 10, 'answered': 10, 'agreement': 0.99, 'agreement_min': 0.95}) + '\n', '')
        self.on_path = None
        self.state = 'healthy'
        for target in (patch.object(at, 'available_on_path', side_effect=lambda: self.on_path),
                       patch.object(at, 'miner_image', return_value='ghcr.io/reaxlabs/reax-miner@sha256:' + 'a' * 64),
                       patch.object(at, 'runtime_state', side_effect=lambda home: self.state),
                       patch.object(io, 'runner', side_effect=self.runner)):
            target.start()
            self.addCleanup(target.stop)

    def runner(self, cmd, **kwargs):
        self.runs.append((cmd, kwargs))
        return self.reply if cmd[0] != 'docker' or cmd[1] == 'run' else (0, '', '')

    def run_it(self):
        return at.answer_test(SimpleNamespace(home=str(self.home), mode=None))

    def test_not_installed(self):
        (self.home / 'miner.env').unlink()
        self.assertEqual(self.run_it().code(), 10)

    def test_tool_missing_everywhere_is_explained_with_exit_12(self):
        self.reply = (127, '', 'exec: "reax-answertest": executable file not found in $PATH')
        result = self.run_it()
        self.assertEqual(result.code(), 12)
        self.assertIn('not part of this release yet', result.summary)
        self.assertFalse(result.data['available'])
        docker_run = next(cmd for cmd, _ in self.runs if cmd[:2] == ['docker', 'run'])
        self.assertIn('--entrypoint', docker_run)
        self.assertEqual(docker_run[docker_run.index('--entrypoint') + 1], 'reax-answertest')
        self.assertNotIn('--privileged', docker_run)
        self.assertIn('ALL', docker_run)

    def test_tool_on_path_passes(self):
        self.on_path = '/usr/local/bin/reax-answertest'
        result = self.run_it()
        self.assertTrue(result.ok, result.summary)
        self.assertIn('agreement 0.99', result.summary)
        cmd, kwargs = self.runs[0]
        self.assertEqual(cmd, ['/usr/local/bin/reax-answertest', '--model', 'kev-4b', '--json'])
        self.assertEqual(kwargs['env']['REAX_RUNTIME_TOKEN_FILE'], str(self.home / 'secrets/runtime-token'))
        self.assertEqual(kwargs['env']['REAX_KEV_4B_RUNTIME_URL'], 'http://127.0.0.1:8105')

    def test_in_the_miner_image_passes(self):
        result = self.run_it()
        self.assertTrue(result.ok, result.summary)
        self.assertTrue(result.data['available'])

    def test_failed_answers_exit_20(self):
        self.on_path = '/x/reax-answertest'
        self.reply = (1, json.dumps({'pass': False, 'items': 10, 'answered': 6}) + '\n', '')
        result = self.run_it()
        self.assertEqual(result.code(), 20)
        self.assertIn('6/10 answered', result.summary)

    def test_cannot_run_and_unreadable_output(self):
        self.on_path = '/x/reax-answertest'
        self.reply = (2, json.dumps({'error': 'no runtime'}) + '\n', '')
        self.assertEqual(self.run_it().code(), 10)
        self.reply = (0, 'garbage', '')
        self.assertEqual(self.run_it().code(), 1)

    def test_unhealthy_runtime_and_public_runtime_url_are_refused(self):
        self.state = 'starting'
        self.assertEqual(self.run_it().code(), 10)
        self.state = 'healthy'
        (self.home / 'miner.env').write_text(ENV.replace('127.0.0.1', '8.8.8.8'))
        self.assertEqual(self.run_it().code(), 10)

    def test_cli_command_exists(self):
        out = _io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(['answer-test', '--json', '--offline', '--home', str(self.tmp.name) + '/nothing'])
        self.assertEqual(rc, 10)
        self.assertEqual(json.loads(out.getvalue())['command'], 'answer-test')


if __name__ == '__main__':
    unittest.main()
