import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import contextlib
import copy
import io
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from release_fixtures import live_private_release
from reaxlib import backends, cmd_install as install, cmd_selfcheck, descriptor
from reaxlib.cli import main

BUNDLED = json.loads(descriptor.BUNDLED.read_text())
POOLS = {p['id']: p for p in BUNDLED['pools']}
REPORT = {'pass': True, 'items': 640, 'answered': 640, 'failed': 0, 'failures': [], 'distance_median': 0.07, 'distance_p99': 0.4,
          'distance_max': 0.8, 'items_over_tau_item': 0, 'round_scores': 1.0, 'round_strikes': 0.0, 'decisions_per_s': 40.2,
          'seconds': 15.9, 'model': 's1-pro', 'manifest_id': 'm1'}


def token(tmp, name, mode=0o600):
    path = pathlib.Path(tmp) / name
    path.write_text('t' * 64)
    path.chmod(mode)
    return str(path)


class PlanTest(unittest.TestCase):
    def test_two_gpus_get_distinct_loopback_ports(self):
        self.assertEqual(backends.plan(POOLS['s1-pro'], '0,1'),
                         [{'gpu': '0', 'bind': '127.0.0.1', 'port': 8102}, {'gpu': '1', 'bind': '127.0.0.1', 'port': 8112}])
        self.assertEqual([b['port'] for b in backends.plan(POOLS['s1-fast'], '0,1,2,3')], [8101, 8111, 8121, 8131])

    def test_url_list_matches_miner_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = backends.plan(POOLS['s1-fast'], '0,1')
            self.assertEqual(backends.urls(plain), 'http://127.0.0.1:8101,http://127.0.0.1:8111')
            own = backends.plan(POOLS['s1-fast'], '0,1', '10.0.0.2', token(tmp, 'a') + ',' + token(tmp, 'b'))
            self.assertEqual(backends.urls(own), 'http://10.0.0.2:8101|/run/backend-token-0,http://10.0.0.2:8111|/run/backend-token-1')

    def test_refusals(self):
        bad = [(POOLS['s1-lite'], '0,1', {}), (POOLS['s1-cpu'], '0,1', {}), (POOLS['s1-pro'], '0,0', {}), (POOLS['s1-pro'], '0,x', {}),
               (POOLS['s1-pro'], '', {}), (POOLS['s1-pro'], '0,1', {'bind': '0.0.0.0'}), (POOLS['s1-pro'], '0,1', {'bind': '8.8.8.8'}),
               (POOLS['s1-pro'], '0,1', {'bind': 'miner.example.com'}), (POOLS['s1-pro'], '0,1', {'token_files': '/nonexistent/a,/nonexistent/b'}),
               (POOLS['s1-pro'], ','.join(map(str, range(17))), {})]
        for pool, gpus, kw in bad:
            with self.subTest(pool=pool['id'], gpus=gpus, kw=kw), self.assertRaises(ValueError):
                backends.plan(pool, gpus, kw.get('bind', '127.0.0.1'), kw.get('token_files'))

    def test_token_file_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, loose = token(tmp, 'a'), token(tmp, 'loose', 0o644)
            with self.assertRaises(ValueError):
                backends.plan(POOLS['s1-pro'], '0,1', token_files=a + ',' + loose)  # group/other readable
            with self.assertRaises(ValueError):
                backends.plan(POOLS['s1-pro'], '0,1', token_files=a)  # one file per GPU
            with self.assertRaises(ValueError):
                backends.plan(POOLS['s1-pro'], '0,1', token_files=a + ',' + a)  # distinct files
            link = pathlib.Path(tmp) / 'link'
            link.symlink_to(a)
            with self.assertRaises(ValueError):
                backends.plan(POOLS['s1-pro'], '0,1', token_files=a + ',' + str(link))

    def test_release_gate(self):
        self.assertFalse(backends.release_ok('1.2.0rc3'))
        self.assertFalse(backends.release_ok('1.2.0rc7'))
        self.assertTrue(backends.release_ok('1.2.0rc8'))
        self.assertTrue(backends.release_ok('1.2.0rc10'))
        self.assertTrue(backends.release_ok('1.2.0'))
        self.assertFalse(backends.release_ok('1.1.0rc2'))

    def test_private_hosts(self):
        for host in ('127.0.0.1', '::1', '10.1.2.3', '172.16.0.1', '172.31.255.1', '192.168.1.5', 'fd00::5'):
            self.assertTrue(backends.private_host(host), host)
        for host in ('0.0.0.0', '8.8.8.8', '172.32.0.1', '169.254.1.1', 'localhost', ''):
            self.assertFalse(backends.private_host(host), host)


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name)
        patcher = patch.dict(_os.environ, REAX_WALLET_DIR='')
        patcher.start()
        self.addCleanup(patcher.stop)

    def render(self, pool, **extra):
        install.render(self.home, copy.deepcopy(BUNDLED), dict({'pool': pool, 'network': 'finney', 'netuid': 92}, **extra))
        return (self.home / 'compose.yml').read_text(), (self.home / 'miner.env').read_text()

    def test_single_backend_is_unchanged(self):
        compose, env = self.render('s1-pro')
        self.assertIn('REAX_S1_PRO_RUNTIME_URL=http://127.0.0.1:8102', env)
        self.assertNotIn('RUNTIME_URLS', env)
        self.assertIn('    network_mode: host', compose)
        self.assertEqual(compose.count('network_mode: host'), 2)
        self.assertIn('count: 1', compose)
        self.assertNotIn('device_ids', compose)
        self.assertNotIn('ports:', compose)
        self.assertNotIn('runtime-1', compose)
        self.assertIn('RUNE_HOST: 127.0.0.1', compose)
        self.assertIn('      runtime:\n        condition: service_healthy', compose)

    def test_two_gpus_s1_pro(self):
        compose, env = self.render('s1-pro', backends=backends.plan(POOLS['s1-pro'], '0,1'))
        self.assertIn('REAX_S1_PRO_RUNTIME_URLS=http://127.0.0.1:8102,http://127.0.0.1:8112', env)
        self.assertNotIn('REAX_S1_PRO_RUNTIME_URL=', env)
        self.assertIn('  runtime:\n', compose)
        self.assertIn('  runtime-1:\n', compose)
        self.assertIn('device_ids: ["0"]', compose)
        self.assertIn('device_ids: ["1"]', compose)
        self.assertNotIn('count: 1', compose)
        self.assertIn('- "127.0.0.1:8102:8102"', compose)
        self.assertIn('- "127.0.0.1:8112:8102"', compose)
        self.assertEqual(compose.count('network_mode: host'), 1)  # the miner only
        self.assertEqual(compose.count('RUNE_HOST: 0.0.0.0'), 2)
        self.assertIn('      runtime:\n        condition: service_healthy', compose)
        self.assertIn('      runtime-1:\n        condition: service_healthy', compose)
        self.assertEqual(compose.count('RUNE_DETERMINISTIC'), 2)
        self.assertEqual(compose.count('/weights/s1-pro:/model:ro'), 2)

    def test_two_gpus_s1_fast_with_own_tokens_on_private_address(self):
        a, b = token(self.tmp.name, 'a'), token(self.tmp.name, 'b')
        plan = backends.plan(POOLS['s1-fast'], '2,3', '192.168.1.10', a + ',' + b)
        compose, env = self.render('s1-fast', backends=plan)
        self.assertIn('REAX_S1_FAST_RUNTIME_URLS=http://192.168.1.10:8101|/run/backend-token-0,http://192.168.1.10:8111|/run/backend-token-1', env)
        self.assertIn('REAX_RUNTIME_TOKEN_FILE=/run/runtime-token', env)
        self.assertIn('- "192.168.1.10:8111:8101"', compose)
        self.assertIn('RUNTIME_PORT: "8101"', compose)
        self.assertIn('device_ids: ["3"]', compose)
        self.assertIn('"{}:/run/runtime-token:ro"'.format(a), compose)  # backend 0's sidecar
        self.assertIn('"{}:/run/runtime-token:ro"'.format(b), compose)
        self.assertIn('"{}:/run/backend-token-1:ro"'.format(b), compose)  # the miner
        self.assertNotIn(str(self.home / 'secrets/runtime-token') + ':/run/runtime-token:ro', compose.split('  runtime-1:')[0].split('  runtime:')[1])

    def test_miner_port_cannot_collide_with_backend_port(self):
        with self.assertRaises(ValueError):
            self.render('s1-pro', backends=backends.plan(POOLS['s1-pro'], '0,1'), miner_port=8112)

    def test_refused_for_gguf_pools(self):
        with self.assertRaises(ValueError):
            self.render('s1-lite', backends=[{'gpu': '0', 'bind': '127.0.0.1', 'port': 8103}])


class InstallTest(unittest.TestCase):
    def live(self, release_id):
        d = copy.deepcopy(BUNDLED)
        d['release_id'] = release_id
        d['status'].update(live=True, launch_status='live', netuid=92, testnet_netuid=41)
        d['images']['public'] = True
        d['images']['digests'] = {k: 'sha256:' + format(i + 1, 'x') * 64 for i, k in enumerate(d['images']['digests'])}
        return d

    def run_install(self, release_id, pool='s1-pro', **kw):
        calls = []

        def runner(cmd, **_):
            calls.append(cmd)
            return 0, '', ''
        with tempfile.TemporaryDirectory() as tmp, patch.object(install, 'descriptor', return_value=self.live(release_id)), \
                patch.object(install, 'runner', side_effect=runner), patch.object(install, 'prepare_tools', return_value=None), \
                patch.object(install.sys, 'version_info', (3, 11, 4)), patch.dict(_os.environ, REAX_WALLET_DIR=''):
            home = pathlib.Path(tmp) / 'h'
            args = argparse.Namespace(home=str(home), mode='testnet', pool=pool, confirm_mainnet=True, accept_manifest_change=False, **kw)
            result = install.install(args)
            files = {n: (home / n).read_text() for n in ('compose.yml', 'miner.env') if (home / n).exists()}
            return result, calls, install.read_config(home) if home.exists() else {}, files

    def test_old_release_is_refused_before_any_work(self):
        result, calls, config, _ = self.run_install('1.2.0rc3', gpus='0,1')
        self.assertEqual(result.code(), 12)
        self.assertIn('1.2.0rc8', result.summary)
        self.assertEqual(calls, [])
        self.assertEqual(config, {})

    def test_two_gpus_on_rc8(self):
        result, calls, config, files = self.run_install('1.2.0rc8', gpus='0,1')
        self.assertTrue(result.ok, result.summary)
        self.assertEqual([b['gpu'] for b in config['backends']], ['0', '1'])
        self.assertIn('REAX_S1_PRO_RUNTIME_URLS=http://127.0.0.1:8102,http://127.0.0.1:8112', files['miner.env'])
        self.assertIn('runtime-1', files['compose.yml'])

    def test_invalid_gpu_list_is_an_error(self):
        result, calls, _, _ = self.run_install('1.2.0rc8', gpus='0,0')
        self.assertEqual(result.code(), 2)
        self.assertEqual(calls, [])

    def test_gguf_pool_is_refused(self):
        for pool in ('s1-lite', 's1-cpu'):
            result, calls, _, _ = self.run_install('1.2.0rc8', pool=pool, gpus='0,1')
            self.assertEqual(result.code(), 2, pool)
            self.assertIn('s1-fast and s1-pro', result.summary)

    def test_without_gpus_nothing_changes(self):
        result, _, config, files = self.run_install('1.2.0rc3')
        self.assertTrue(result.ok, result.summary)
        self.assertNotIn('backends', config)
        self.assertIn('REAX_S1_PRO_RUNTIME_URL=http://127.0.0.1:8102', files['miner.env'])


class FakeDocker:
    def __init__(self, run, health=('healthy', 'healthy')):
        self.run, self.health, self.calls = run, health, []

    def __call__(self, cmd, timeout=120, **kwargs):
        self.calls.append(cmd)
        if cmd[:2] == ['docker', 'compose'] and 'ps' in cmd:
            names = cmd[cmd.index('json') + 1:]
            return 0, json.dumps([{'Service': n, 'State': 'running', 'Health': h} for n, h in zip(names, self.health)]), ''
        if cmd[:2] == ['docker', 'compose'] and 'config' in cmd:
            return 0, json.dumps({'services': {'miner': {'image': 'ghcr.io/reaxlabs/reax-miner@sha256:' + 'a' * 64}}}), ''
        if cmd[:2] == ['docker', 'run']:
            return self.run
        return 0, '', ''


class SelfcheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = pathlib.Path(self.tmp.name) / 'home'
        self.home.mkdir()
        self.own = token(self.tmp.name, 'own')
        self.urls = 'http://127.0.0.1:8102|/run/backend-token-0,http://127.0.0.1:8112|/run/backend-token-1'
        install.write_config(self.home, {'mode': 'testnet', 'pool': 's1-pro', 'miner_port': 8091, 'backends': [
            {'gpu': '0', 'bind': '127.0.0.1', 'port': 8102, 'token_file': self.own},
            {'gpu': '1', 'bind': '127.0.0.1', 'port': 8112, 'token_file': self.own}]})
        (self.home / 'compose.yml').touch()
        (self.home / 'miner.env').write_text('REAX_MODEL=s1-pro\nREAX_RUNTIME_TOKEN_FILE=/run/runtime-token\nREAX_S1_PRO_RUNTIME_URLS=' + self.urls + '\n')
        (self.home / 'secrets').mkdir()
        (self.home / 'secrets/runtime-token').write_text('t' * 64)
        release = live_private_release()
        release['status']['testnet_netuid'] = 41
        patcher = patch.object(install, 'descriptor', return_value=release)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, fake):
        out = io.StringIO()
        with patch.object(install, 'runner', fake), contextlib.redirect_stdout(out):
            rc = main(['selfcheck', '--home', str(self.home), '--json'])
        return rc, json.loads(out.getvalue())

    def multi(self, second_pass=True):
        second = dict(REPORT, **{'pass': second_pass, 'distance_median': 2.5, 'round_scores': 0.3})
        return {'model': 's1-pro', 'pass': second_pass, 'backends': [dict(REPORT, url='http://127.0.0.1:8102'), dict(second, url='http://127.0.0.1:8112')]}

    def test_one_verdict_per_backend_on_pass(self):
        fake = FakeDocker((0, json.dumps(self.multi()), ''))
        rc, r = self.call(fake)
        self.assertEqual(rc, 0, r)
        self.assertIn('all 2 backends', r['summary'])
        self.assertEqual([b['url'] for b in r['data']['backends']], ['http://127.0.0.1:8102', 'http://127.0.0.1:8112'])
        run = next(c for c in fake.calls if c[:2] == ['docker', 'run'])
        self.assertIn('REAX_S1_PRO_RUNTIME_URLS=' + self.urls, run)
        self.assertIn(self.own + ':/run/backend-token-0:ro', run)
        self.assertIn(self.own + ':/run/backend-token-1:ro', run)
        ps = next(c for c in fake.calls if 'ps' in c)
        self.assertEqual(ps[-2:], ['runtime', 'runtime-1'])

    def test_one_failing_backend_fails_and_is_named(self):
        rc, r = self.call(FakeDocker((1, json.dumps(self.multi(False)), '')))
        self.assertEqual(rc, 20)
        self.assertIn('http://127.0.0.1:8102: PASS', r['summary'])
        self.assertIn('http://127.0.0.1:8112: FAIL', r['summary'])
        self.assertIn('Do not register', r['summary'])

    def test_backend_that_could_not_answer_shows_its_error(self):
        report = self.multi(False)
        report['backends'][1] = {'url': 'http://127.0.0.1:8112', 'pass': False, 'error': 'backend not reachable'}
        rc, r = self.call(FakeDocker((1, json.dumps(report), '')))
        self.assertEqual(rc, 20)
        self.assertIn('FAIL backend not reachable', r['summary'])

    def test_every_sidecar_must_be_healthy(self):
        fake = FakeDocker((0, json.dumps(self.multi()), ''), health=('healthy', 'starting'))
        rc, r = self.call(fake)
        self.assertEqual(rc, 10)
        self.assertEqual(r['data']['runtime'], 'starting')
        self.assertFalse(any(c[:2] == ['docker', 'run'] for c in fake.calls))

    def test_public_url_in_the_list_is_refused(self):
        (self.home / 'miner.env').write_text('REAX_MODEL=s1-pro\nREAX_RUNTIME_TOKEN_FILE=/run/runtime-token\nREAX_S1_PRO_RUNTIME_URLS=http://127.0.0.1:8102,http://8.8.8.8:8112\n')
        rc, r = self.call(FakeDocker((0, json.dumps(self.multi()), '')))
        self.assertEqual(rc, 10)
        self.assertIn('private network', r['summary'])

    def test_local_urls(self):
        self.assertTrue(cmd_selfcheck.local_urls('REAX_S1_PRO_RUNTIME_URLS', 'http://10.0.0.2:8102|/run/x,http://127.0.0.1:8112'))
        self.assertFalse(cmd_selfcheck.local_urls('REAX_S1_PRO_RUNTIME_URLS', 'http://example.com:8102'))
        self.assertFalse(cmd_selfcheck.local_urls('REAX_S1_PRO_RUNTIME_URL', 'http://10.0.0.2:8102'))  # the single URL stays loopback only
        self.assertTrue(cmd_selfcheck.local_urls('REAX_S1_PRO_RUNTIME_URL', 'http://127.0.0.1:8102'))


if __name__ == '__main__':
    unittest.main()
