"""install.sh: syntax, lint, usage errors, dependency report, sudo discipline, and the clone/verify path against a local git repo."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'install.sh'
BASH = shutil.which('bash')
GIT = shutil.which('git')
GIT_ENV = ['-c', 'user.email=test@example.com', '-c', 'user.name=test', '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false']


def sh(*args, env=None, path=None, cwd=None):
    environment = {k: v for k, v in os.environ.items() if not k.startswith('REAX_')}
    environment.update(env or {})
    if path is not None:
        environment['PATH'] = path
    done = subprocess.run([BASH, str(SCRIPT)] + list(args), env=environment, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    return done.returncode, done.stdout, done.stderr


def python_ok():
    """The clone tests run the real python3 checks of the script: skip where PATH's python3 is older than 3.10 (the CI 3.8 leg)."""
    try:
        return subprocess.run(['python3', '-c', 'import sys, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


class StaticTest(unittest.TestCase):
    def test_bash_syntax(self):
        self.assertEqual(subprocess.run([BASH, '-n', str(SCRIPT)]).returncode, 0)

    def test_shellcheck_when_available(self):
        shellcheck = shutil.which('shellcheck')
        if not shellcheck:
            self.skipTest('shellcheck is not installed')
        done = subprocess.run([shellcheck, '-s', 'bash', str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_script_is_executable_and_runs_only_from_its_last_line(self):
        self.assertTrue(os.access(str(SCRIPT), os.X_OK))
        lines = SCRIPT.read_text().splitlines()
        self.assertEqual(lines[0], '#!/usr/bin/env bash')
        self.assertIn('set -euo pipefail', lines)
        self.assertEqual(lines[-1], 'main "$@"')  # a truncated download defines functions and runs nothing
        self.assertEqual([i for i, line in enumerate(lines) if re.match(r'^main\b', line) and not line.startswith('main()')], [len(lines) - 1])

    def test_sudo_only_inside_install_deps(self):
        text = SCRIPT.read_text()
        body = text[text.index('install_deps() {'):text.index('fetch_kit() {')]
        outside = text.replace(body, '')
        self.assertEqual([line for line in outside.splitlines() if re.match(r'^\s*(sudo|su|doas)\s', line)], [])
        self.assertIn('apt-get install -y "${APT_PACKAGES[@]}"', body)
        self.assertIn('APT_PACKAGES=(git python3 python3-venv ca-certificates)', text)
        self.assertNotRegex(text, r'(?m)^\s*(rm|dd|mkfs|chmod|chown)\b')

    def test_only_models_and_up_are_run_through_reaxctl(self):
        # never a wallet, register or service command: the human step is printed by reaxctl, not run by the installer
        text = SCRIPT.read_text()
        self.assertEqual(sorted(set(re.findall(r'reaxctl" +([a-z-]+)', text))), ['models'])  # models for --list, the model check and the unknown-model hint
        self.assertEqual(re.findall(r'local args=\((\w+) ', text), ['up'])
        self.assertEqual(sorted(set(re.findall(r'args\+=\((--[a-z-]+)', text))), ['--confirm-mainnet', '--eu', '--gpu', '--home'])
        self.assertEqual(re.findall(r'exec python3 "\$KIT_DIR/reaxctl" "\$\{args\[@\]\}"', text), ['exec python3 "$KIT_DIR/reaxctl" "${args[@]}"'])


class UsageTest(unittest.TestCase):
    def test_help(self):
        rc, out, _ = sh('--help')
        self.assertEqual(rc, 0)
        for word in ('--model', '--eu', '--commit', '--install-deps', '--dry-run'):
            self.assertIn(word, out)

    def test_usage_errors_exit_2(self):
        for args in ([], ['--model'], ['--model', 'a b'], ['--model', 's1-fast', '--tag', 'main'], ['--model', 's1-fast', '--commit', 'abc'],
                     ['--model', 's1-fast', '--mode', 'localnet'], ['--model', 's1-fast', '--gpu', 'x'], ['--model', 's1-fast', '--nope'], ['--model', '$(touch /tmp/pwned)']):
            with self.subTest(args=args):
                rc, _, err = sh(*args)
                self.assertEqual(rc, 2, err)


class MachineCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        for name in ('uname', 'id'):
            os.symlink(shutil.which(name), str(self.bin / name))

    def test_missing_basics_are_listed_with_exact_fix_commands_and_nothing_is_changed(self):
        target = self.root / 'kit'
        rc, out, _ = sh('--dry-run', '--model', 's1-fast', '--dir', str(target), env={'HOME': str(self.root)}, path=str(self.bin))
        self.assertEqual(rc, 10)
        self.assertIn('git is missing', out)
        self.assertIn('python3 is missing', out)
        self.assertIn('Docker is not installed', out)
        self.assertIn('run the same command again', out)
        self.assertFalse(target.exists())
        if pathlib.Path('/etc/os-release').exists() and re.search(r'debian|ubuntu', pathlib.Path('/etc/os-release').read_text(), re.I):
            self.assertIn('sudo apt-get update && sudo apt-get install -y git python3 python3-venv ca-certificates', out)

    def test_install_deps_runs_only_the_listed_apt_packages(self):
        osr = pathlib.Path('/etc/os-release')
        if not (osr.exists() and re.search(r'debian|ubuntu', osr.read_text(), re.I)):
            self.skipTest('needs a Debian-like host to take the apt path')
        log = self.root / 'calls.log'
        for name in ('sudo', 'apt-get'):
            fake = self.bin / name
            fake.write_text('#!/bin/sh\necho "{} $*" >> "{}"\n'.format(name, log))
            fake.chmod(0o755)
        rc, out, _ = sh('--install-deps', '--model', 's1-fast', '--dir', str(self.root / 'kit'), env={'HOME': str(self.root)}, path=str(self.bin))
        self.assertEqual(rc, 10)  # the fakes install nothing, so the recheck still fails
        calls = [re.sub(r'^sudo ', '', line) for line in log.read_text().splitlines()]
        self.assertEqual(calls, ['apt-get update', 'apt-get install -y git python3 python3-venv ca-certificates'])
        self.assertFalse((self.root / 'kit').exists())

    def test_dry_run_with_install_deps_does_not_call_sudo(self):
        log = self.root / 'calls.log'
        fake = self.bin / 'sudo'
        fake.write_text('#!/bin/sh\necho "$*" >> "{}"\n'.format(log))
        fake.chmod(0o755)
        sh('--dry-run', '--install-deps', '--model', 's1-fast', env={'HOME': str(self.root)}, path=str(self.bin))
        self.assertFalse(log.exists())


@unittest.skipUnless(GIT and BASH, 'needs git and bash')
class CloneTest(unittest.TestCase):
    TAG = 'v0.0.1-test'

    def setUp(self):
        if not python_ok():
            self.skipTest('needs python3 >= 3.10 with venv support on PATH')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.src = self.make_repo(self.root / 'src')
        self.commit = self.git(self.src, 'rev-parse', 'HEAD').strip()
        self.kit = self.root / 'kit'

    def git(self, cwd, *args):
        return subprocess.run(['git'] + GIT_ENV + list(args), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, check=True).stdout

    def make_repo(self, target, mutate=None):
        shutil.copytree(str(ROOT), str(target), ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc'))
        if mutate:
            mutate(target)
        self.git(target, 'init', '-q')
        self.git(target, 'add', '-A')
        self.git(target, 'commit', '-q', '-m', 'kit')
        self.git(target, 'tag', self.TAG)
        return target

    def run_script(self, *extra, **kw):
        args = ['--no-up', '--model', 's1-fast', '--repo', str(self.src), '--tag', self.TAG, '--dir', str(self.kit)] + list(extra)
        return sh(*args, env={'HOME': str(self.root)}, **kw)

    def test_clone_verify_and_idempotent_rerun(self):
        rc, out, err = self.run_script('--commit', self.commit)
        self.assertEqual(rc, 0, err)
        self.assertIn('cloned', out)
        self.assertIn('Commit verified', out)
        self.assertIn('Release descriptor validated', out)
        self.assertTrue((self.kit / 'reaxctl').exists())
        before = self.git(self.kit, 'rev-parse', 'HEAD')
        rc, out, err = self.run_script('--commit', self.commit)
        self.assertEqual(rc, 0, err)
        self.assertIn('already in', out)
        self.assertNotIn('cloned', out)
        self.assertEqual(self.git(self.kit, 'rev-parse', 'HEAD'), before)

    def test_missing_commit_is_loudly_unverified_but_a_wrong_commit_stops(self):
        rc, out, err = self.run_script()
        self.assertEqual(rc, 0)
        self.assertIn('UNVERIFIED', err)
        self.assertIn(self.commit, err)
        rc, out, err = self.run_script('--commit', '0' * 40)
        self.assertEqual(rc, 20)
        self.assertIn('commit mismatch', err)

    def test_wrong_commit_on_a_fresh_clone_also_stops(self):
        rc, _, err = self.run_script('--commit', 'f' * 40)
        self.assertEqual(rc, 20)
        self.assertIn('Do not continue', err)

    def test_missing_tag_is_a_download_error(self):
        rc, _, err = sh('--no-up', '--model', 's1-fast', '--repo', str(self.src), '--tag', 'v9.9.9', '--dir', str(self.kit), env={'HOME': str(self.root)})
        self.assertEqual(rc, 13)
        self.assertFalse(self.kit.exists() and any(self.kit.iterdir()))
        self.assertEqual(self.run_script()[0], 0)
        rc, _, err = sh('--no-up', '--model', 's1-fast', '--repo', str(self.src), '--tag', 'v9.9.9', '--dir', str(self.kit), env={'HOME': str(self.root)})
        self.assertEqual(rc, 13)
        self.assertIn('does not exist', err)

    def test_existing_directory_is_never_overwritten_or_touched(self):
        self.kit.mkdir()
        (self.kit / 'mine.txt').write_text('precious')
        rc, _, err = self.run_script()
        self.assertEqual(rc, 10)
        self.assertEqual((self.kit / 'mine.txt').read_text(), 'precious')
        shutil.rmtree(str(self.kit))
        self.assertEqual(self.run_script()[0], 0)
        (self.kit / 'README.md').write_text('local edit')
        rc, _, err = self.run_script()
        self.assertEqual(rc, 10)
        self.assertIn('local changes', err)
        self.assertEqual((self.kit / 'README.md').read_text(), 'local edit')

    def test_checkout_of_another_origin_is_refused(self):
        self.assertEqual(self.run_script()[0], 0)
        other = self.make_repo(self.root / 'other')
        rc, _, err = sh('--no-up', '--model', 's1-fast', '--repo', str(other), '--tag', self.TAG, '--dir', str(self.kit), env={'HOME': str(self.root)})
        self.assertEqual(rc, 10)
        self.assertIn('not ' + str(other), err)

    def test_a_kit_with_a_bad_release_descriptor_is_refused(self):
        def corrupt(target):
            path = target / 'releases' / 'current.json'
            path.write_text(path.read_text().replace('sha256:', 'sha256:zz', 1))
        self.src = self.make_repo(self.root / 'bad', corrupt)
        rc, out, err = self.run_script()
        self.assertEqual(rc, 20)
        self.assertIn('did not validate', err)

    def test_list_clones_the_kit_and_shows_models_without_installing(self):
        rc, out, err = sh('--list', '--repo', str(self.src), '--tag', self.TAG, '--dir', str(self.kit), env={'HOME': str(self.root), 'REAX_OFFLINE': '1'})
        self.assertEqual(rc, 0, err)
        self.assertIn('s1-fast', out)
        self.assertIn('THIS MACHINE', out)
        self.assertIn('rerun with --model', out)
        self.assertFalse((self.root / '.reax-miner').exists())

    def test_dry_run_creates_nothing(self):
        rc, out, _ = sh('--dry-run', '--model', 's1-fast', '--repo', str(self.src), '--tag', self.TAG, '--dir', str(self.kit), env={'HOME': str(self.root)})
        self.assertIn(rc, (0, 10))  # 10 when this machine has no Docker: the report must still not change anything
        self.assertIn('Dry run', out)
        self.assertFalse(self.kit.exists())

    def test_without_no_up_an_unknown_model_is_reported_before_docker_is_touched(self):
        rc, out, err = sh('--model', 'no-such-model', '--repo', str(self.src), '--tag', self.TAG, '--dir', str(self.kit), env={'HOME': str(self.root)})
        self.assertEqual(rc, 2)
        self.assertIn('unknown model', err)
        self.assertIn('s1-fast', out)


if __name__ == '__main__':
    unittest.main()
