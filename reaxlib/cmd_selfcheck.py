"""Reference self-check: run the release's reax-selfcheck in the installed miner image against the runtime sidecar."""
import json
import os
from .cli import add_common
from .result import Result
from . import cmd_install as io
from .cmd_runtime import NOTICE, compose, sanitize

ENTRYPOINT = 'reax-selfcheck'
FAIL_FIX = ('Do not register or mine with this setup: check that the weights are the pinned revision, that the pinned runtime image '
            'runs (reaxctl update check), and that the GPU is not shared with other work; then rerun reaxctl selfcheck.')


def _containers(out):
    try:
        value = json.loads(out) if out.lstrip().startswith('[') else [json.loads(x) for x in out.splitlines() if x.strip()]
    except ValueError:
        return None
    return value if isinstance(value, list) and all(isinstance(c, dict) for c in value) else None


def runtime_state(home):
    """'healthy', 'starting', 'unhealthy', 'stopped' or 'unknown' for the compose runtime service."""
    rc, out, _ = compose(home, 'ps', '--all', '--format', 'json', 'runtime')
    containers = _containers(out) if rc == 0 else None
    if containers is None:
        return 'unknown'
    runtime = next((c for c in containers if c.get('Service', 'runtime') == 'runtime'), None)
    if not runtime or runtime.get('State') != 'running':
        return 'stopped'
    return runtime.get('Health') or 'unknown'


def miner_image(home):
    rc, out, _ = compose(home, 'config', '--format', 'json')
    try:
        image = json.loads(out)['services']['miner']['image'] if rc == 0 else None
    except (ValueError, KeyError, TypeError):
        image = None
    return image if isinstance(image, str) and image else None


def miner_env(home):
    """The selfcheck settings from the rendered miner.env (compose escapes $ as $$)."""
    values = {}
    try:
        lines = (home / 'miner.env').read_text(encoding='utf-8').splitlines()
    except OSError:
        return values
    for line in lines:
        key, sep, value = line.partition('=')
        if sep and (key in ('REAX_MODEL', 'REAX_RUNTIME_TOKEN_FILE') or (key.startswith('REAX_') and key.endswith('_RUNTIME_URL'))):
            values[key] = value.replace('$$', '$')
    return values


def missing_entrypoint(rc, err):
    text = err.lower()
    return rc in (126, 127) and ('executable file not found' in text or 'no such file or directory' in text or ENTRYPOINT in text)


def summarize(report):
    keys = ('pass', 'items', 'answered', 'failed', 'distance_median', 'distance_p99', 'distance_max', 'items_over_tau_item',
            'round_scores', 'round_strikes', 'decisions_per_s', 'seconds', 'model', 'manifest_id', 'failures', 'error')
    return {k: report[k] for k in keys if k in report}


def numbers(report):
    parts = ['{}/{} answered'.format(report.get('answered'), report.get('items'))]
    if report.get('distance_median') is not None:
        parts.append('distance median {} p99 {} max {}'.format(report.get('distance_median'), report.get('distance_p99'), report.get('distance_max')))
    if report.get('round_scores') is not None:
        parts.append('simulated rounds {} score, {} strike'.format(report.get('round_scores'), report.get('round_strikes')))
    if report.get('decisions_per_s') is not None:
        parts.append('{} decisions/s'.format(report.get('decisions_per_s')))
    return '; '.join(parts)


def selfcheck(args, home=None, config=None, start=None):
    home = home or io.home_of(args)
    config = io.read_config(home) if config is None else config
    mode = getattr(args, 'mode', None) or config.get('mode', 'localnet')
    if mode != config.get('mode', 'localnet'):
        return Result('selfcheck', 'blocked', 'Requested mode differs from installed mode; run install first.', exit_code=10)
    if mode == 'localnet':
        return Result('selfcheck', 'blocked', 'The reference self-check needs the real model runtime; the localnet rehearsal uses a hash runtime. Install with --mode testnet or --mode mainnet on the GPU host.', exit_code=10)
    if not (home / 'compose.yml').exists() or not (home / 'miner.env').exists():
        return Result('selfcheck', 'blocked', 'Run install before the self-check.', exit_code=10)
    failure = io.gate(io.descriptor(home), mode, config.get('mainnet_confirmed', False))
    if failure:
        failure.command = 'selfcheck'
        return failure
    state = runtime_state(home)
    if start is None:
        start = not getattr(args, 'no_start', False)
    if state == 'stopped' and start:
        # Only the sidecar: the miner service needs a registered hotkey, the check does not.
        rc, _, err = compose(home, 'up', '-d', '--no-deps', 'runtime', timeout=300)
        if rc:
            return Result('selfcheck', 'error', 'Starting the runtime sidecar failed.', data={'detail': io.detail(err)}, exit_code=13)
        state = runtime_state(home)
    if state != 'healthy':
        message = {'starting': 'The runtime sidecar is starting (hashing weights and loading the model can take up to 10 minutes); rerun reaxctl selfcheck once it is healthy.',
                   'stopped': 'The runtime sidecar is not running; run reaxctl selfcheck without --no-start to start it.'}
        return Result('selfcheck', 'blocked', message.get(state, 'The runtime sidecar is not healthy; inspect reaxctl logs, fix the runtime, then rerun reaxctl selfcheck.'),
                      data={'runtime': state}, exit_code=10, next=[{'command': 'reaxctl logs --lines 200', 'why': 'Inspect the runtime sidecar.'}])
    image = miner_image(home)
    env = miner_env(home)
    token_target = env.get('REAX_RUNTIME_TOKEN_FILE', '')
    if not image or 'REAX_MODEL' not in env or not token_target.startswith('/') or ':' in token_target:
        return Result('selfcheck', 'blocked', 'The installed compose.yml or miner.env is incomplete; rerun install.', exit_code=10)
    # A plain container with only the token mounted: compose run would bind the wallet paths, which may not exist before registration.
    cmd = ['docker', 'run', '--rm', '--network', 'host', '--user', '{}:{}'.format(os.getuid(), os.getgid()), '--cap-drop', 'ALL',
           '--security-opt', 'no-new-privileges:true', '-v', str(home / 'secrets/runtime-token') + ':' + token_target + ':ro']
    for key in sorted(env):
        cmd += ['-e', key + '=' + env[key]]
    cmd += ['--entrypoint', ENTRYPOINT, image, '--json']
    rc, out, err = io.runner(cmd, timeout=1800)
    if missing_entrypoint(rc, err):
        return Result('selfcheck', 'blocked', 'The installed subnet release predates reax-selfcheck; update to the current release, then rerun reaxctl selfcheck.',
                      exit_code=10, next=[{'command': 'reaxctl update check', 'why': 'Compare installed pins with the current release.'}])
    try:
        report = json.loads(out.strip().splitlines()[-1]) if out.strip() else None
    except ValueError:
        report = None
    if not isinstance(report, dict) or rc not in (0, 1, 2):
        return Result('selfcheck', 'error', 'The self-check did not return a readable result.', data={'detail': io.detail(err or out), 'exit_code': rc}, exit_code=13 if rc == 13 else 1)
    data = sanitize(home, summarize(report))
    data.update(untrusted=True, notice=NOTICE)
    if rc == 2:
        return Result('selfcheck', 'blocked', 'The self-check cannot run: {}'.format(data.get('error') or 'see detail'), data=data, exit_code=10,
                      next=[{'command': 'reaxctl logs --lines 200', 'why': 'Inspect the runtime sidecar.'}])
    if rc == 0 and report.get('pass') is True:
        return Result('selfcheck', summary='Reference self-check passed: ' + numbers(data) + '.', data=data)
    return Result('selfcheck', 'error', 'Reference self-check failed: ' + numbers(data) + '. ' + FAIL_FIX, data=data, exit_code=20)


def register(sub):
    p = sub.add_parser('selfcheck')
    add_common(p)
    p.add_argument('--no-start', action='store_true', help='do not start a stopped runtime sidecar')
    p.set_defaults(func=selfcheck)
