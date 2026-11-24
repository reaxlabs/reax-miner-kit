import base64
import json
import sys
import types
from pathlib import Path

import pytest

from agent import evidence, server

NONCE = 'ab' * 32


def binary(tmp_path, payload):
    path = tmp_path / 'nvattest'
    path.write_text('#!/usr/bin/python3\nimport json, sys\nassert sys.argv[1:] == ' + repr(['--format', 'json', 'collect-evidence', '--nonce', NONCE]) + '\nprint(' + repr(json.dumps(payload)) + ')\n')
    path.chmod(0o700)
    return str(path)


def payload(nonce=NONCE, code=0):
    return {'result_code': code, 'evidences': [{'arch': 'BLACKWELL', 'certificate': 'Yw==', 'evidence': 'ZQ==', 'nonce': nonce}]}


def test_nvattest_success(tmp_path):
    assert evidence.gpu_evidence(NONCE, binary(tmp_path, payload())) == payload()['evidences']


def test_nvattest_nonce_mismatch(tmp_path):
    with pytest.raises(ValueError, match='nonce mismatch'):
        evidence.gpu_evidence(NONCE, binary(tmp_path, payload(nonce='00'*32)))


def test_nvattest_result_code(tmp_path):
    with pytest.raises(ValueError, match='nvattest failed'):
        evidence.gpu_evidence(NONCE, binary(tmp_path, payload(code=1)))


def test_missing_cli_uses_sdk(tmp_path, monkeypatch):
    calls = []
    class Client:
        def set_nonce(self, nonce): calls.append(('nonce', nonce))
        def add_verifier(self, *args): calls.append(('verifier', args))
        def get_evidence(self): return ['sdk result']
    sdk = types.SimpleNamespace(Attestation=Client, Devices=types.SimpleNamespace(GPU='gpu'), Environment=types.SimpleNamespace(REMOTE='remote'))
    monkeypatch.setitem(sys.modules, 'nv_attestation_sdk', types.SimpleNamespace(attestation=sdk))
    assert evidence.gpu_evidence(NONCE, str(tmp_path/'missing')) == ['sdk result']
    assert calls == [('nonce', NONCE), ('verifier', ('gpu', 'remote', '', ''))]


def test_generation_after_privlevel_and_single_outblob_read(tmp_path, monkeypatch):
    mkdir, write_bytes, write_text, read_bytes = Path.mkdir, Path.write_bytes, Path.write_text, Path.read_bytes
    events = []
    def create(path, *args, **kwargs):
        mkdir(path, *args, **kwargs)
        if path.name.startswith('sn92-'):
            for name, value in {'provider': b'sev_guest', 'generation': b'0', 'privlevel': b'0', 'outblob': b'report', 'auxblob': b''}.items():
                write_bytes(path / name, value)
    def bump(path):
        generation = path.parent / 'generation'
        write_bytes(generation, str(int(generation.read_text()) + 1).encode())
        events.append(path.name)
    def wb(path, value):
        result = write_bytes(path, value)
        if path.name == 'inblob': bump(path)
        return result
    def wt(path, value, *args, **kwargs):
        result = write_text(path, value, *args, **kwargs)
        if path.name == 'privlevel': bump(path)
        return result
    def rb(path):
        if path.name == 'outblob': events.append('read-outblob')
        return read_bytes(path)
    monkeypatch.setattr(Path, 'mkdir', create)
    monkeypatch.setattr(Path, 'write_bytes', wb)
    monkeypatch.setattr(Path, 'write_text', wt)
    monkeypatch.setattr(Path, 'read_bytes', rb)
    assert evidence.tsm_report(b'x'*64, tmp_path)['outblob'] == b'report'
    assert events == ['privlevel', 'inblob', 'read-outblob']


def test_empty_auxblob_omits_vcek(monkeypatch):
    monkeypatch.setattr(server.nvidia, 'device_identity', lambda _: 'ab'*32)
    gpu = {'arch': 'BLACKWELL', 'certificate': base64.b64encode(b'cert').decode(), 'evidence': base64.b64encode(b'evidence').decode()}
    workload = {'serving_image': 'registry/test@sha256:' + 'a'*64, 'agent_version': '1', 'runbook_version': '1'}
    agent = server.Agent(b't'*32, workload, b'', '5'*48, gpu_collector=lambda _: [gpu], cpu_collector=lambda *_: {'provider': 'sev_guest', 'outblob': b'report', 'auxblob': b''})
    cpu = agent.bundle(NONCE, '5'*48)['cpu']
    assert cpu == {'type': 'snp', 'report': base64.b64encode(b'report').decode()}
