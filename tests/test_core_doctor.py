import os as _os; _os.environ.setdefault("REAX_OFFLINE", "1")  # tests never touch the network
import argparse
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from reaxlib import descriptor, sysprobe
from reaxlib.cmd_doctor import diagnose, plan
from release_fixtures import registered_pending_release

FIXTURES = Path(__file__).parent / 'fixtures'


class DoctorTests(unittest.TestCase):
    def test_fixture_hosts(self):
        for path in sorted(FIXTURES.glob('core_*.json')):
            with self.subTest(host=path.name), tempfile.TemporaryDirectory() as tmp:
                fixture = json.loads(path.read_text())
                args = argparse.Namespace(home=tmp, mode=None, pool=None, probe_ip=False)
                calls = []
                def probe(cmd, timeout):
                    calls.append(cmd)
                    return fixture['commands'].get(' '.join(cmd), {'returncode': 127, 'stdout': '', 'stderr': 'not found'})
                with patch('urllib.request.urlopen', side_effect=AssertionError('offline doctor used network')):
                    result = diagnose(args, probe=probe, host=fixture['host'])
                self.assertEqual(result.status, fixture['expect']['status'])
                self.assertEqual([pool['id'] for pool in result.data['eligible_pools'] if pool['ok']], fixture['expect']['eligible'])
                self.assertEqual(result.data['os'], fixture['host']['os'])
                self.assertFalse(result.data['real_mining_possible'])
                self.assertTrue(all(blocker['fix'] for blocker in result.data['blockers']))
                if 'permission' in path.name:
                    blocker = next(item for item in result.data['blockers'] if item['id'] == 'docker_daemon')
                    self.assertIn('log out', blocker['fix'])
                if '4090' in path.name:
                    self.assertFalse(any('tolerance' in item for item in result.warnings))  # a real 24 GB card is not warned about

    def test_docker_info_with_null_runtimes_does_not_crash(self):
        fixture = json.loads((FIXTURES / 'core_4090.json').read_text())
        commands = dict(fixture['commands'])
        commands['docker info --format {{json .}}'] = {'returncode': 1, 'stdout': '{"Runtimes":null,"ServerErrors":["permission denied"]}', 'stderr': 'permission denied'}
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(home=tmp, mode=None, pool=None, probe_ip=False)
            result = diagnose(args, probe=lambda cmd, timeout: commands.get(' '.join(cmd), {'returncode': 127, 'stdout': '', 'stderr': ''}), host=fixture['host'])
        self.assertIn('docker_daemon', [b['id'] for b in result.data['blockers']])

    def test_unverifiable_clock_is_a_warning_not_a_blocker(self):
        fixture = json.loads((FIXTURES / 'core_4090.json').read_text())
        commands = {k: v for k, v in fixture['commands'].items() if not k.startswith(('timedatectl', 'chronyc'))}
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(home=tmp, mode=None, pool=None, probe_ip=False)
            result = diagnose(args, probe=lambda cmd, timeout: commands.get(' '.join(cmd), {'returncode': 127, 'stdout': '', 'stderr': ''}), host=fixture['host'])
        self.assertNotIn('ntp', [b['id'] for b in result.data['blockers']])
        self.assertTrue(any('Clock synchronization could not be checked' in w for w in result.warnings))

    def test_driver_floor(self):
        fixture = json.loads((FIXTURES / 'core_4090.json').read_text())
        fixture['commands']['nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits']['stdout'] = 'RTX 4090, 24564, 579.99'
        result = diagnose(argparse.Namespace(home='/nonexistent', probe_ip=False), probe=lambda cmd, timeout: fixture['commands'].get(' '.join(cmd), {'returncode': 1, 'stdout': '', 'stderr': ''}), host=fixture['host'])
        self.assertFalse(any(item['ok'] for item in result.data['eligible_pools']))
        self.assertIn('driver', [item['id'] for item in result.data['blockers']])

    def test_plan_launching_soon(self):
        with tempfile.TemporaryDirectory() as tmp, patch('reaxlib.cmd_doctor.descriptor.load', return_value=registered_pending_release()), patch('reaxlib.cmd_doctor.diagnose') as fake:
            fake.return_value.data = {'rehearsal_possible': True}
            result = plan(argparse.Namespace(home=tmp, mode='localnet', pool=None))
            self.assertEqual(result.data['mode'], 'localnet')
            self.assertFalse(any(item['id'] == 'coldkey' for item in result.data['steps']))
            self.assertTrue(all(item.get('blocked') == 'launching_soon' for item in result.data['mainnet_steps'][2:]))
            self.assertTrue(all({'who', 'cost', 'reversible', 'command'} <= set(item) for item in result.data['steps']))

    def test_missing_command_never_crashes(self):
        self.assertNotEqual(sysprobe.run(['/definitely/missing/reax-tool'])['returncode'], 0)

    def test_no_rehearsal_or_gpu_is_blocked(self):
        fixture = json.loads((FIXTURES / 'core_no_gpu.json').read_text())
        fixture['host']['python_version'] = '3.8.10'
        result = diagnose(argparse.Namespace(home='/nonexistent', probe_ip=False), probe=lambda cmd, timeout: {'returncode': 127, 'stdout': '', 'stderr': 'missing'}, host=fixture['host'])
        self.assertEqual(result.code(), 10)
        self.assertFalse(result.data['rehearsal_possible'])

    def test_native_windows_and_wsl_detection(self):
        with tempfile.TemporaryDirectory() as tmp, patch('reaxlib.sysprobe.sys.platform', 'win32'), patch('pathlib.Path.read_text', side_effect=OSError('no proc')):
            self.assertEqual(sysprobe.facts(tmp)['os'], 'windows-native')
        with tempfile.TemporaryDirectory() as tmp, patch('reaxlib.sysprobe.sys.platform', 'linux'), patch('pathlib.Path.read_text', return_value='Linux Microsoft WSL2'):
            self.assertEqual(sysprobe.facts(tmp)['os'], 'wsl2')

    def test_fast_pool_mining_ready_even_if_pro_disk_missing(self):
        fixture = json.loads((FIXTURES / 'core_4090.json').read_text())
        fixture['host']['disk_free_gb'] = 30
        release = descriptor.load('/nonexistent')
        release['status'].update(live=True, netuid=123)
        release['images']['public'] = True
        result = diagnose(argparse.Namespace(home='/nonexistent', probe_ip=False), probe=lambda cmd, timeout: fixture['commands'].get(' '.join(cmd), {'returncode': 1, 'stdout': '', 'stderr': ''}), host=fixture['host'], release=release)
        self.assertTrue(result.data['real_mining_possible'])
