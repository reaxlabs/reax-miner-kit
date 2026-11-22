"""`reaxctl up --model <id>` sets up a miner for one model; `reaxctl switch --model <id>` moves it to another model.

`up` runs the existing steps in order and stops at the first one that fails, with one clear next step:
doctor -> install (weights and runtime image by digest) -> runtime start -> selfcheck (EU: answer test) -> wallet
check -> miner start -> service install -> registration handoff. Every step is idempotent: run `up` again after a fix
and it continues; run it on a finished setup and it only reports the status.

The kit never creates or reads a coldkey, never pays and never registers a hotkey: registration is the human step.
`switch` fetches the new model next to the old one (old weights stay), swaps the runtime, checks it, restarts the miner
(which publishes the new commitment itself) and restores the old setup when the check fails.
"""
from __future__ import annotations

import argparse
import sys
import time

from . import models, state
from .cli import add_common
from .result import Result
from . import cmd_install as io
from . import cmd_models
from . import cmd_runtime
from . import cmd_selfcheck
from . import cmd_service
from . import cmd_wallet
from . import cmd_answertest

DEFAULT_MODE = 'mainnet'
UNIT = 'reax-miner.service'
SNAPSHOT_FILES = ('compose.yml', 'miner.env', 'config.json', 'state.json')
POLL_SECONDS = 10


def log(message):
    """Progress for the person watching: stderr only, so --json stdout stays one object."""
    sys.stderr.write('reaxctl: {}\n'.format(message))
    sys.stderr.flush()


def sleep(seconds):  # replaceable by tests
    time.sleep(seconds)


def clock():
    return time.monotonic()


def _ns(args, **over):
    ns = argparse.Namespace(**vars(args))
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _step(sid, status, what, **extra):
    item = {'id': sid, 'status': status, 'what': what}
    item.update(extra)
    return item


def _lines(steps):
    return ['[{}] {} - {}'.format(s['status'], s['id'], s['what']) for s in steps]


def _failed(command, result, steps, step_id, model, **extra):
    """Turn a failing child result into this command's result: same exit code, same message, plus the steps so far."""
    steps = steps + [_step(step_id, 'failed' if result.status != 'needs_human' else 'needs_human', result.summary)]
    result.command = command
    result.data = dict(result.data, model=model, steps=steps, text_lines=_lines(steps), **extra)
    return result


def resolve(args, command):
    home = io.home_of(args)
    release = io.descriptor(home)
    entry = models.get(release, args.model)
    if entry is None:
        live = ', '.join(e['id'] for e in models.catalog(release) if e['status'] == 'live') or 'none'
        return home, release, None, Result(command, 'error', 'Unknown model "{}". Live models: {}.'.format(str(args.model)[:60], live), exit_code=2,
                                           next=[{'command': 'reaxctl models', 'why': 'List the models and what they need.'}])
    return home, release, entry, None


def selected_gpus(gpus, index):
    if index is None:
        return gpus, None
    if not 0 <= index < len(gpus):
        return [], 'GPU {} was not found; this machine reports {} NVIDIA GPU(s) (numbers start at 0).'.format(index, len(gpus))
    return [gpus[index]], None


def verification_step(eu):
    return ('answer-test', 'EU operator check: compare the answers with the reference (not bit identity).') if eu else \
           ('selfcheck', 'Reference self-check: your runtime must reproduce the reference decisions bit for bit.')


def plan(release, entry, mode, eu, gpu):
    runtime, weights = entry['runtime'], entry['weights']
    digest = runtime['digest']
    image = runtime['image'].split('/')[-1] + (('@' + digest[:19] + '...' if len(digest) > 40 else '@' + digest) if digest else ':<release tag>')
    vid, vwhat = verification_step(eu)
    steps = [_step('doctor', 'planned', 'Check this machine, read-only: GPU, driver, Docker, NVIDIA toolkit, clock, ports.', who='agent', command='reaxctl doctor --pool ' + entry['id']),
             _step('install', 'planned', 'Download weights {}@{} ({} GB) and the runtime image {}.'.format(weights['repo'], str(weights['revision'])[:8], weights['size_gb'], image), who='agent',
                   command='reaxctl install --pool {} --mode {}'.format(entry['id'], mode)),
             _step('runtime', 'planned', 'Start the runtime sidecar on 127.0.0.1:{}{} and wait until it is healthy.'.format(runtime['port'], '' if gpu is None else ' (GPU {})'.format(gpu)), who='agent'),
             _step(vid, 'planned', vwhat, who='agent', command='reaxctl ' + vid),
             _step('wallet', 'planned', 'Check that a hotkey and the public coldkey address exist. You create or import them; the kit never touches a coldkey.', who='human if missing',
                   command='reaxctl wallet hotkey; reaxctl wallet coldkeypub --ss58 <address>'),
             _step('miner', 'planned', 'Start the miner. It publishes the commitment {} itself.'.format(entry['commitment']), who='agent', command='reaxctl start'),
             _step('service', 'planned', 'Install a systemd user service so the miner survives reboots (skipped where systemd is missing).', who='agent', command='reaxctl service install'),
             _step('register', 'planned', 'Register the hotkey in your own wallet. You approve and pay the fee; the kit never pays.', who='human', command='reaxctl register')]
    return steps


def eu_path(release, entry):
    link = (release.get('links') or {}).get('eu_operator') or release['links']['guide']
    return {'verification': 'answer test (fidelity of the answers), not bit identity',
            'steps': ['Sign the REAX EU operator contract (sub-processing agreement and miner agreement). This is a human step; guide: ' + link,
                      'Run the answer test on your hardware: reaxctl answer-test (it checks your runtime answers against the reference).',
                      'Tell REAX your hotkey address and GPU count so it is added to the EU operator registry (human step).',
                      'Offer as much parallelism as your hardware serves with answer quality that still passes the test.'],
            'answer_test_command': 'reaxctl answer-test'}


def installed_same(home, release, entry, config, gpu=None):
    """True when the model, release and weights recorded by install match what is wanted now (second run = no-op).
    A different --gpu is not the same setup: the compose file pins the device."""
    if config.get('pool') != entry['id'] or config.get('release_id') != release['release_id'] or config.get('manifest_id') != entry['manifest_id']:
        return False
    if gpu is not None and config.get('gpu_index') != gpu:
        return False
    if not (home / 'compose.yml').exists() or not (home / 'miner.env').exists():
        return False
    steps = state.read_json(home / 'state.json').get('steps', {})
    done = (steps.get('weights') or {}).get('info') or {}
    return done.get('revision') == entry['weights']['revision'] and 'images' in steps


# ----------------------------------------------------------------------------- shared step helpers


def doctor_failure(args, release, entry, mode, same, host, gpus):
    """Run the read-only host check for this model; return a blocked Result or None."""
    from . import cmd_doctor
    diagnosis = cmd_doctor.diagnose(_ns(args, pool=entry['id'], mode=mode), release=release)
    blockers = [b for b in diagnosis.data.get('blockers', [])
                if b['id'] != 'python_tarfile'  # only the localnet rehearsal unpacks a chain image
                and not (same and b['id'] == 'miner_port')]  # our own miner may hold the port on a re-run
    if entry['kind'] == 'model':  # diagnose knows the legacy pools; for a portfolio model apply the same rules here
        for reason in models.fit(release, entry, host, gpus)['reasons']:
            blockers.append({'id': 'fit', 'message': reason, 'fix': 'Pick a model that fits this machine: reaxctl models'})
        if entry['accelerator'] == 'nvidia' and not (diagnosis.data.get('nvidia_container_toolkit') and diagnosis.data.get('docker', {}).get('nvidia_runtime')):
            blockers.append({'id': 'toolkit', 'message': 'NVIDIA container runtime is not configured.', 'fix': 'Install NVIDIA Container Toolkit for your distro and configure Docker: ' + cmd_doctor.NVIDIA_DOC})
    if not blockers:
        return None
    first = blockers[0]
    return Result('up', 'blocked', '{} Fix: {}'.format(first['message'], first['fix']), exit_code=10,
                  data={'blockers': blockers, 'model': entry['id']}, next=[{'command': 'reaxctl up --model ' + entry['id'], 'why': 'Run again after the fix; finished steps are skipped.'}])


def start_runtime(home, entry, wait_minutes, steps):
    """Start only the runtime sidecar and wait for its health check. Returns a failure Result or None."""
    rc, _, err = cmd_runtime.compose(home, 'up', '-d', '--no-deps', 'runtime', timeout=600)
    if rc:
        return Result('up', 'error', 'Starting the runtime sidecar failed.', data={'detail': io.detail(err)}, exit_code=13,
                      next=[{'command': 'reaxctl logs --lines 200', 'why': 'Inspect the runtime.'}])
    deadline = clock() + wait_minutes * 60
    while True:
        status = cmd_selfcheck.runtime_state(home)
        if status == 'healthy':
            return None
        if status in ('stopped', 'unhealthy'):
            return Result('up', 'error', 'The runtime sidecar is {}; it did not come up.'.format(status), exit_code=13,
                          data={'runtime': status}, next=[{'command': 'reaxctl logs --lines 200', 'why': 'Inspect the runtime.'}])
        if clock() >= deadline:
            return Result('up', 'blocked', 'The runtime sidecar is still loading after {} minutes (weights hashing and model load can be slow on a cold disk).'.format(wait_minutes),
                          exit_code=10, data={'runtime': status}, next=[{'command': 'reaxctl up --model ' + entry['id'], 'why': 'Run again; it waits for the runtime and continues.'}])
        sleep(POLL_SECONDS)


def verify_runtime(args, home, config, mode, eu):
    """Selfcheck (bit identity) or, for EU operators, the answer test. Returns (Result, step status)."""
    a = _ns(args, mode=mode)
    if eu:
        result = cmd_answertest.answer_test(a, home, config)
        if result.exit_code == 12:  # the answer test is not shipped yet: not a failure of the operator's setup
            return result, 'skipped'
        return result, 'ok' if result.ok else 'failed'
    result = cmd_selfcheck.selfcheck(a, home, config, start=False)
    return result, 'ok' if result.ok else 'failed'


def wait_status(home, config, key, seconds):
    """Poll the status evidence (`registered`, `commitment_published`) for up to `seconds`."""
    deadline = clock() + seconds
    while True:
        data = cmd_runtime.status_data(home, config)
        if data.get(key) is True:
            return True
        if clock() >= deadline:
            return False
        sleep(POLL_SECONDS)


def service_step(args, home, mode):
    """Best effort: a missing systemd must not undo a working miner. Returns (status, message)."""
    result = cmd_service.handle(_ns(args, action='install', user=False, system=False, home=str(home), mode=mode))
    if result.ok:
        return 'ok', 'Service installed.'
    if result.status == 'needs_human':
        steps = (result.needs_human or {}).get('steps') or []
        return 'warning', '{} {}'.format((result.needs_human or {}).get('action', result.summary), ' '.join(steps)).strip()
    return 'warning', 'No service installed ({}); the containers still restart by themselves with Docker.'.format(result.summary)


# ----------------------------------------------------------------------------- up


def up(args):
    home, release, entry, failure = resolve(args, 'up')
    if failure:
        return failure
    mode = args.mode or DEFAULT_MODE
    eu = bool(args.eu)
    config = io.read_config(home)
    pool_gpu = args.gpu
    if pool_gpu is not None and not 0 <= pool_gpu <= 64:
        return Result('up', 'error', '--gpu must be a GPU number between 0 and 64.', exit_code=2)
    if mode == 'localnet':
        return Result('up', 'blocked', 'up sets up a real miner. For the free practice run use: reaxctl plan --mode localnet', exit_code=10)
    available, why = models.availability(release, entry)
    host, all_gpus = cmd_models.host_view(args, release)
    gpus, gpu_error = selected_gpus(all_gpus, pool_gpu)
    verdict = models.fit(release, entry, host, gpus)
    if gpu_error:
        verdict = {'fits': False, 'reasons': [gpu_error] + verdict['reasons']}
    steps = plan(release, entry, mode, eu, pool_gpu)
    base = {'model': entry['id'], 'name': entry['name'], 'pot': entry['pot'], 'model_status': entry['status'], 'mode': mode, 'eu': eu,
            'commitment': entry['commitment'], 'fit': verdict}
    if eu:
        base['eu_path'] = eu_path(release, entry)
    if args.dry_run:
        would_block = ([why] if not available else []) + verdict['reasons']
        data = dict(base, dry_run=True, ready=not would_block, would_block=would_block, steps=steps)
        data['text_lines'] = _lines(steps) + (['Would stop at: ' + would_block[0]] if would_block else ['This machine is ready for this model.'])
        if eu:
            data['text_lines'] += ['EU operator path:'] + ['  - ' + line for line in base['eu_path']['steps']]
        return Result('up', summary='Dry run for {}: {} step(s); nothing was changed.'.format(entry['id'], len(steps)), data=data,
                      warnings=[] if available else [why], exit_code=0 if available else 12,
                      status='ok' if available else 'blocked',
                      next=[{'command': 'reaxctl up --model {} --mode {}{} --confirm-mainnet'.format(entry['id'], mode, ' --eu' if eu else ''), 'why': 'Do it for real; a person approves the mainnet setup.'}] if available else [])
    if not available:
        return Result('up', 'blocked', why, exit_code=12, data=dict(base, steps=steps), next=[{'command': 'reaxctl models', 'why': 'List the models you can mine now.'}])
    if config.get('pool') and config['pool'] != entry['id'] and (home / 'compose.yml').exists():
        failure = io.human('up', 'Switch the installed model with one command.', 'This machine already mines {}. A switch keeps the old weights and rolls back when the new model fails its check.'.format(config['pool']),
                           ['reaxctl switch --model ' + entry['id']])
        failure.data = dict(base, installed_model=config['pool'])
        return failure
    gated = io.gate(release, mode, args.confirm_mainnet)
    if gated:
        gated.command = 'up'
        gated.data = dict(base, steps=steps)
        return gated
    same = installed_same(home, release, entry, config, pool_gpu)
    done = []
    if same:  # idempotent second run: report instead of redoing
        runtime_state = cmd_selfcheck.runtime_state(home)
        status = cmd_runtime.status_data(home, config)
        if runtime_state == 'healthy' and status.get('running'):
            done = [_step(s['id'], 'skipped', 'already done') for s in steps if s['id'] != 'register']
            done.append(_step('register', 'ok' if status.get('registered') else 'needs_human', 'registered' if status.get('registered') else 'Registration not seen yet: run reaxctl register for the steps.'))
            registered = bool(status.get('registered'))
            return Result('up', status='ok' if registered else 'needs_human', exit_code=0 if registered else 11,
                          summary='{} is already set up and running.'.format(entry['id']) + ('' if registered else ' The hotkey is not registered yet.'),
                          data=dict(base, already_set_up=True, steps=done, text_lines=_lines(done),
                                    running=True, registered=registered, commitment_published=status.get('commitment_published'), last_validator_score=status.get('last_validator_score')),
                          next=[{'command': 'reaxctl verify', 'why': 'Check health, commitment and validator rounds.'}])
    # 1 doctor
    log('up {}: 1/8 checking this machine'.format(entry['id']))
    failure = doctor_failure(args, release, entry, mode, same, host, gpus)
    if failure:
        return _failed('up', failure, done, 'doctor', entry['id'], **{k: v for k, v in base.items() if k != 'model'})
    done.append(_step('doctor', 'ok', 'This machine fits {}.'.format(entry['id'])))
    # 2 install
    if same:
        done.append(_step('install', 'skipped', 'weights and image already installed for release ' + release['release_id']))
    else:
        log('up {}: 2/8 downloading weights ({} GB) and the runtime image; this can take many minutes'.format(entry['id'], entry['weights']['size_gb']))
        installed = io.install(_ns(args, pool=entry['id'], mode=mode, confirm_mainnet=args.confirm_mainnet, accept_manifest_change=False, cc_fix=False, gpu=pool_gpu))
        if not installed.ok:
            return _failed('up', installed, done, 'install', entry['id'], **{k: v for k, v in base.items() if k != 'model'})
        done.append(_step('install', 'ok', 'weights and runtime image ready'))
        config = io.read_config(home)
    config['eu'] = eu
    io.write_config(home, config)
    # 3 runtime
    log('up {}: 3/8 starting the runtime and waiting until it has loaded the model (up to {} minutes)'.format(entry['id'], args.wait_minutes))
    failure = start_runtime(home, entry, args.wait_minutes, done)
    if failure:
        return _failed('up', failure, done, 'runtime', entry['id'], **{k: v for k, v in base.items() if k != 'model'})
    done.append(_step('runtime', 'ok', 'runtime sidecar is healthy on 127.0.0.1:{}'.format(entry['runtime']['port'])))
    warnings = []
    # 4 selfcheck or answer test
    vid, _ = verification_step(eu)
    log('up {}: 4/8 running the {} (loads the GPU for a few minutes)'.format(entry['id'], vid))
    result, outcome = verify_runtime(args, home, config, mode, eu)
    if outcome == 'failed':
        return _failed('up', result, done, vid, entry['id'], **{k: v for k, v in base.items() if k != 'model'})
    done.append(_step(vid, outcome, result.summary if outcome == 'ok' else 'not available yet; ' + result.summary))
    if outcome == 'skipped':
        warnings.append(result.summary)
    # 5 wallet
    try:
        _, hotkey, coldkeypub = cmd_wallet.wallet_paths(args)
    except ValueError as exc:
        return Result('up', 'error', str(exc), exit_code=2, data=dict(base, steps=done))
    missing = [name for name, path in (('hotkey', hotkey), ('public coldkey address', coldkeypub)) if not path.is_file()]
    if missing:
        done.append(_step('wallet', 'needs_human', 'missing: ' + ', '.join(missing)))
        human = io.human('up', 'Create the hotkey and import your public coldkey address, then register.',
                         'The miner needs a hotkey file on this machine and the public address of your coldkey. reaxctl never creates, reads or asks for a coldkey or recovery phrase.',
                         ['reaxctl wallet hotkey' if 'hotkey' in missing else 'Hotkey: found',
                          'reaxctl wallet coldkeypub --ss58 <your public coldkey address>' if 'public coldkey address' in missing else 'Public coldkey address: found',
                          'Register the hotkey in your own wallet (you approve and pay the fee): ' + release['links']['register'],
                          'Run again: reaxctl up --model {}'.format(entry['id'])], release['links']['register'])
        human.data = dict(base, steps=done, text_lines=_lines(done))
        human.warnings = warnings
        return human
    done.append(_step('wallet', 'ok', 'hotkey and public coldkey address found'))
    # 6 miner
    log('up {}: 6/8 starting the miner'.format(entry['id']))
    rc, _, err = cmd_runtime.compose(home, 'up', '-d', timeout=600)
    if rc:
        return Result('up', 'error', 'Starting the miner failed.', exit_code=13, data=dict(base, steps=done + [_step('miner', 'failed', io.detail(err))], detail=io.detail(err)),
                      next=[{'command': 'reaxctl logs --lines 200', 'why': 'Inspect the miner.'}])
    done.append(_step('miner', 'ok', 'miner started; it publishes {} itself'.format(entry['commitment'])))
    # 7 service (best effort)
    if args.no_service:
        done.append(_step('service', 'skipped', '--no-service'))
    else:
        outcome, message = service_step(args, home, mode)
        done.append(_step('service', outcome, message))
        if outcome == 'warning':
            warnings.append(message)
    # 8 registration handoff
    log('up {}: 8/8 looking for the registration in the miner log'.format(entry['id']))
    registered = wait_status(home, config, 'registered', args.wait_registered)
    io.mark(home, 'up', {'model': entry['id'], 'release_id': release['release_id'], 'eu': eu})
    if registered:
        done.append(_step('register', 'ok', 'registration seen in the miner log'))
        return Result('up', summary='{} is set up and running.'.format(entry['id']), data=dict(base, steps=done, text_lines=_lines(done)), warnings=warnings,
                      next=[{'command': 'reaxctl verify', 'why': 'Check health, commitment and validator rounds.'}])
    done.append(_step('register', 'needs_human', 'the hotkey is not registered yet'))
    registration = cmd_wallet.registration(_ns(args, mode=mode, wallet_name=None, hotkey_name=None))
    needs = registration.needs_human or {'action': 'Register the hotkey in your own wallet.', 'why': 'reaxctl never pays or registers.', 'steps': [release['links']['register']]}
    return Result('up', 'needs_human', 'The miner is running. Register the hotkey in your own wallet; it starts serving by itself afterwards.', needs_human=needs, warnings=warnings,
                  data=dict(base, steps=done, text_lines=_lines(done), hotkey_ss58=registration.data.get('hotkey_ss58')),
                  next=[{'command': 'reaxctl verify', 'why': 'Run after registering.'}], exit_code=11)


# ----------------------------------------------------------------------------- switch


def snapshot(home):
    from .state import private_dir, write_text
    snap = home / 'rollback' / ('switch-' + str(time.time_ns()))
    private_dir(home / 'rollback')
    private_dir(snap)
    for name in SNAPSHOT_FILES:
        if (home / name).exists():
            write_text(snap / name, (home / name).read_text(), 0o644 if name == 'compose.yml' else 0o600)
    return snap


def restore(home, snap):
    from .state import write_text
    for name in SNAPSHOT_FILES:
        if (snap / name).exists():
            write_text(home / name, (snap / name).read_text(), 0o644 if name == 'compose.yml' else 0o600)


def unit_active(args):
    """Is the miner service running? It would start the miner before the new model passed its check, so switch pauses it."""
    directory, user = cmd_service.location(args)
    if user is None:
        return False
    return cmd_service.systemctl(user, 'is-active', UNIT)[0] == 0


def unit_do(args, action):
    directory, user = cmd_service.location(args)
    if user is not None:
        cmd_service.systemctl(user, action, UNIT)


def purge_old(home, old_id):
    """Delete only <home>/weights/<old id>; never anything else."""
    import re
    import shutil
    base = home / 'weights'
    target = base / old_id
    if not re.fullmatch(r'[A-Za-z0-9_-]+', old_id or '') or target.is_symlink() or not target.is_dir() or target.resolve().parent != base.resolve():
        return False
    shutil.rmtree(str(target))
    return True


def switch(args):
    home, release, entry, failure = resolve(args, 'switch')
    if failure:
        return failure
    config = io.read_config(home)
    old_id, mode, eu = config.get('pool'), config.get('mode', 'localnet'), bool(config.get('eu'))
    if not old_id or not (home / 'compose.yml').exists():
        return Result('switch', 'blocked', 'No miner is set up here yet.', exit_code=10, next=[{'command': 'reaxctl up --model ' + entry['id'], 'why': 'Set up this machine for the model.'}])
    if mode == 'localnet':
        return Result('switch', 'blocked', 'switch changes a real miner; this machine only has the localnet rehearsal.', exit_code=10)
    base = {'model': entry['id'], 'previous_model': old_id, 'mode': mode, 'eu': eu, 'commitment': entry['commitment']}
    if old_id == entry['id']:
        return Result('switch', summary='Already on {}; nothing to change.'.format(old_id), data=dict(base, changed=False),
                      next=[{'command': 'reaxctl verify', 'why': 'Check the running miner.'}])
    available, why = models.availability(release, entry)
    if not available:
        return Result('switch', 'blocked', why, exit_code=12, data=base, next=[{'command': 'reaxctl models', 'why': 'List the models you can mine now.'}])
    host, all_gpus = cmd_models.host_view(args, release)
    index = args.gpu if args.gpu is not None else config.get('gpu_index')
    gpus, gpu_error = selected_gpus(all_gpus, index)
    verdict = models.fit(release, entry, host, gpus)
    reasons = ([gpu_error] if gpu_error else []) + verdict['reasons']
    if reasons:  # refuse before anything is stopped
        return Result('switch', 'blocked', reasons[0], exit_code=10, data=dict(base, fit=verdict, would_block=reasons),
                      next=[{'command': 'reaxctl models', 'why': 'See which models fit this machine.'}])
    old_pool = io.pool_of(release, old_id)
    old_commitment = old_pool['commitment'] if old_pool else 'reax-s1/1:{}:{}'.format(old_id, config.get('manifest_id'))
    verification_id, verification_what = verification_step(eu)
    steps = [_step('fetch', 'planned', 'Download weights {}@{} and the runtime image; the old model keeps running meanwhile and its weights stay.'.format(entry['weights']['repo'], str(entry['weights']['revision'])[:8])),
             _step('stop', 'planned', 'Stop the old runtime and miner.'),
             _step('runtime', 'planned', 'Start the new runtime sidecar and wait until it is healthy.'),
             _step(verification_id, 'planned', verification_what + ' If it fails, the old setup is restored.'),
             _step('miner', 'planned', 'Start the miner; it publishes {} itself.'.format(entry['commitment']))]
    warnings = ['Switching moves this hotkey to the pot of {}: its score there starts from zero and {} stops earning for it.'.format(entry['id'], old_id)]
    if args.dry_run:
        return Result('switch', summary='Dry run: switch from {} to {}; nothing was changed.'.format(old_id, entry['id']), warnings=warnings,
                      data=dict(base, dry_run=True, commitment_before=old_commitment, steps=steps, text_lines=_lines(steps), purge_old=bool(args.purge_old)),
                      next=[{'command': 'reaxctl switch --model ' + entry['id'], 'why': 'Do it for real.'}])
    done = []
    snap = snapshot(home)
    was_active = unit_active(args)

    def back(result, step_id, text, installed_new):
        """Restore the old setup after a failure and return the failure Result."""
        log('switch: {} failed; restoring the previous setup ({})'.format(step_id, old_id))
        restore(home, snap)
        restart_failed = False
        if installed_new:  # containers were stopped/replaced: bring the old pair back
            cmd_runtime.compose(home, 'down', timeout=300)
            restart_failed = bool(cmd_runtime.compose(home, 'up', '-d', timeout=600)[0])
        if was_active:
            unit_do(args, 'start')
        result.command = 'switch'
        extra = dict(base, rolled_back=True, rollback_dir=str(snap), restart_failed=restart_failed, failed_step=step_id)
        result.data = dict(result.data, **extra)
        result.data['steps'] = done + [_step(step_id, 'failed', text)]
        result.data['text_lines'] = _lines(result.data['steps'])
        result.warnings = result.warnings + ['The previous setup ({}) was restored from {}.'.format(old_id, snap)] + (['It did not restart: run reaxctl start and inspect reaxctl logs.'] if restart_failed else [])
        return result

    # fetch: install writes the new compose/env/config and downloads; the old containers keep running until we stop them
    log('switch {} -> {}: downloading weights ({} GB) and the runtime image; the old model keeps running'.format(old_id, entry['id'], entry['weights']['size_gb']))
    installed = io.install(_ns(args, pool=entry['id'], mode=mode, confirm_mainnet=True, accept_manifest_change=True, cc_fix=False, gpu=index))
    if not installed.ok:
        return back(installed, 'fetch', installed.summary, installed_new=False)
    done.append(_step('fetch', 'ok', 'new weights and image ready'))
    config = io.read_config(home)
    config['eu'] = eu
    io.write_config(home, config)
    if was_active:
        unit_do(args, 'stop')
    log('switch {} -> {}: stopping the old miner and starting the new runtime'.format(old_id, entry['id']))
    rc, _, err = cmd_runtime.compose(home, 'down', timeout=300)
    if rc:
        return back(Result('switch', 'error', 'Stopping the old miner failed.', data={'detail': io.detail(err)}, exit_code=13), 'stop', 'stopping failed', installed_new=True)
    done.append(_step('stop', 'ok', 'old runtime and miner stopped'))
    failure = start_runtime(home, entry, args.wait_minutes, done)
    if failure:
        return back(failure, 'runtime', failure.summary, installed_new=True)
    done.append(_step('runtime', 'ok', 'new runtime is healthy'))
    log('switch {} -> {}: running the {}'.format(old_id, entry['id'], verification_id))
    result, outcome = verify_runtime(args, home, config, mode, eu)
    if outcome == 'failed':
        log('switch: the check failed; restoring {}'.format(old_id))
        return back(result, verification_id, result.summary, installed_new=True)
    done.append(_step(verification_id, outcome, result.summary if outcome == 'ok' else 'not available yet; ' + result.summary))
    if outcome == 'skipped':
        warnings.append(result.summary)
    rc, _, err = cmd_runtime.compose(home, 'up', '-d', timeout=600)
    if rc:
        return back(Result('switch', 'error', 'Starting the miner failed.', data={'detail': io.detail(err)}, exit_code=13), 'miner', 'miner start failed', installed_new=True)
    done.append(_step('miner', 'ok', 'miner started'))
    if was_active:
        unit_do(args, 'start')
    published = wait_status(home, config, 'commitment_published', args.wait_registered)
    if not published:
        warnings.append('The commitment was not seen in the miner log yet. Check it with: ' + commit_show_command(home))
    io.mark(home, 'switch', {'from': old_id, 'to': entry['id'], 'release_id': release['release_id']})
    purged = purge_old(home, old_id) if args.purge_old else False
    kept = (home / 'weights' / old_id).exists()
    done.append(_step('weights', 'ok', 'old weights removed (--purge-old)' if purged else ('old weights kept in {}'.format(home / 'weights' / old_id) if kept else 'no old weights on disk')))
    return Result('switch', summary='Switched from {} to {}.'.format(old_id, entry['id']), warnings=warnings,
                  data=dict(base, changed=True, commitment_before=old_commitment, commitment_published=published, check_commitment=commit_show_command(home),
                            old_weights_kept=kept and not purged, rollback_dir=str(snap), steps=done, text_lines=_lines(done) + ['Commitment now: ' + entry['commitment']]),
                  next=[{'command': 'reaxctl verify', 'why': 'Check health, commitment and validator rounds for the new model.'}])


def commit_show_command(home):
    return 'docker compose -f {} run --rm --no-deps --entrypoint reax-commit miner show'.format(home / 'compose.yml')


def register(sub):
    for name, handler, helptext in (('up', up, 'set up this machine to mine one model (one command, idempotent)'),
                                    ('switch', switch, 'move the installed miner to another model, with rollback')):
        p = sub.add_parser(name, help=helptext)
        add_common(p)
        p.add_argument('--model', required=True, help='model id from: reaxctl models')
        p.add_argument('--gpu', type=int, default=None, metavar='N', help='use GPU number N of a multi-GPU machine')
        p.add_argument('--dry-run', action='store_true', help='show the plan and the machine check; change nothing')
        p.add_argument('--wait-minutes', type=int, default=20, help='how long to wait for the runtime sidecar to become healthy (default 20)')
        p.add_argument('--wait-registered', type=int, default=60, help='seconds to look for registration / commitment evidence in the miner log (default 60)')
        if name == 'up':
            p.add_argument('--eu', action='store_true', help='EU contract operator: answer test instead of the bit-identity self-check')
            p.add_argument('--confirm-mainnet', action='store_true', help='a person approved setting up a mainnet miner')
            p.add_argument('--no-service', action='store_true', help='do not install the systemd user service')
        else:
            p.add_argument('--purge-old', action='store_true', help='delete the weights of the old model after a successful switch')
        p.set_defaults(func=handler)
