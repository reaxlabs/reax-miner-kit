"""`reaxctl answer-test`: the EU operator check. It compares the answers of your runtime with the reference answers
(fidelity of the answers), not their bits. The test itself (`reax-answertest`) ships with the subnet; this command is
the kit side: it finds the tool, runs it against the local runtime sidecar and reports one `reaxctl/1` result.

Assumed tool contract (mirrors `reax-selfcheck`): `reax-answertest --model <id> --json` reads REAX_<ID>_RUNTIME_URL and
REAX_RUNTIME_TOKEN_FILE from its environment, prints one JSON object (with a boolean `pass`) as its last stdout line and
exits 0 (passed), 1 (failed) or 2 (cannot run). Until it exists in the subnet release this command explains that.
"""
from __future__ import annotations

import json
import os
import shutil

from .cli import add_common
from .result import Result
from . import cmd_install as io
from .cmd_runtime import NOTICE, sanitize
from .cmd_selfcheck import loopback_url, miner_env, miner_image, missing_entrypoint, runtime_state

TOOL = 'reax-answertest'
UNAVAILABLE = ('The answer test (' + TOOL + ') is not part of this release yet. EU operators will run it here once the subnet ships it; '
               'until then there is nothing to run. It replaces the bit-identity self-check for EU operators.')
KEYS = ('pass', 'items', 'answered', 'failed', 'agreement', 'agreement_min', 'concurrency', 'decisions_per_s', 'seconds', 'model', 'failures', 'error')


def numbers(report):
    parts = ['{}/{} answered'.format(report.get('answered'), report.get('items'))]
    if report.get('agreement') is not None:
        parts.append('agreement {}'.format(report['agreement']) + (' (needs {})'.format(report['agreement_min']) if report.get('agreement_min') is not None else ''))
    if report.get('concurrency') is not None:
        parts.append('at concurrency {}'.format(report['concurrency']))
    return '; '.join(parts)


def available_on_path():
    return shutil.which(TOOL)


def answer_test(args, home=None, config=None):
    home = home or io.home_of(args)
    config = io.read_config(home) if config is None else config
    if not (home / 'compose.yml').exists() or not (home / 'miner.env').exists():
        return Result('answer-test', 'blocked', 'Run install before the answer test.', exit_code=10)
    env = miner_env(home)
    token = home / 'secrets/runtime-token'
    if any(not loopback_url(v) for k, v in env.items() if k.endswith('_RUNTIME_URL')):
        return Result('answer-test', 'blocked', 'miner.env names a runtime URL that is not on loopback; rerun install.', exit_code=10)
    model = env.get('REAX_MODEL') or config.get('pool')
    on_path = available_on_path()
    image = None if on_path else miner_image(home)
    if not on_path and not image:
        return Result('answer-test', 'blocked', 'The installed compose.yml is incomplete or Docker is not running, so the miner image cannot be found; start Docker or rerun install.', exit_code=10)
    state = runtime_state(home)
    if state != 'healthy':
        return Result('answer-test', 'blocked', 'The runtime sidecar is not healthy (state: {}); start it and wait for it, then rerun reaxctl answer-test.'.format(state),
                      data={'runtime': state}, exit_code=10, next=[{'command': 'reaxctl logs --lines 200', 'why': 'Inspect the runtime sidecar.'}])
    if on_path:
        child = dict(os.environ, REAX_RUNTIME_TOKEN_FILE=str(token), **{k: v for k, v in env.items() if k.startswith('REAX_') and k != 'REAX_RUNTIME_TOKEN_FILE'})
        rc, out, err = io.runner([on_path, '--model', model, '--json'], timeout=1800, env=child)
    else:
        target = env.get('REAX_RUNTIME_TOKEN_FILE', '')
        if not target.startswith('/') or ':' in target or ':' in str(token) or not token.is_file():
            return Result('answer-test', 'blocked', 'The runtime token file is missing; rerun install.', exit_code=10)
        name = 'reax-answertest-{}'.format(os.getpid())
        cmd = ['docker', 'run', '--rm', '--name', name, '--network', 'host', '--user', '{}:{}'.format(os.getuid(), os.getgid()), '--cap-drop', 'ALL',
               '--security-opt', 'no-new-privileges:true', '-v', str(token) + ':' + target + ':ro']
        for key in sorted(env):
            cmd += ['-e', key + '=' + env[key]]
        cmd += ['--entrypoint', TOOL, image, '--model', model, '--json']
        try:
            rc, out, err = io.runner(cmd, timeout=1800)
        finally:
            io.runner(['docker', 'rm', '-f', name], timeout=60)
        if missing_entrypoint(rc, err) or (rc in (126, 127) and TOOL in err):
            return Result('answer-test', 'blocked', UNAVAILABLE, exit_code=12, data={'available': False, 'checked': 'PATH and the miner image'})
    try:
        report = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except ValueError:
        report = None
    if not isinstance(report, dict) or rc not in (0, 1, 2):
        return Result('answer-test', 'error', 'The answer test did not return a readable result.', data={'detail': io.detail(err or out), 'exit_code': rc}, exit_code=13 if rc == 13 else 1)
    data = sanitize(home, {k: report[k] for k in KEYS if k in report})
    data.update(untrusted=True, notice=NOTICE, available=True)
    if rc == 2:
        return Result('answer-test', 'blocked', 'The answer test cannot run: {}'.format(data.get('error') or 'see detail'), data=data, exit_code=10)
    if rc == 0 and report.get('pass') is True:
        return Result('answer-test', summary='Answer test passed: ' + numbers(data) + '.', data=data)
    return Result('answer-test', 'error', 'Answer test failed: ' + numbers(data) + '. Do not offer this setup to REAX until the answers match the reference model.', data=data, exit_code=20)


def register(sub):
    p = sub.add_parser('answer-test', help='EU operators: check your runtime answers against the reference (not bit identity)')
    add_common(p)
    p.set_defaults(func=answer_test)
