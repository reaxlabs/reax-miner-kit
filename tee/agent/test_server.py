import http.client
import json
import socket
import ssl
import threading

import pytest

from agent import server

HOTKEY = '5' * 48
WORKLOAD = {'serving_image': 'registry/test@sha256:' + 'a' * 64, 'agent_version': '1', 'runbook_version': '1'}


@pytest.fixture
def service():
    context, digest = server.tls_context('/dev/shm')
    agent = server.Agent(digest, WORKLOAD, b'', HOTKEY)
    calls = []
    def bundle(nonce, hotkey):
        calls.append((nonce, hotkey))
        return {'ok': True}
    agent.bundle = bundle
    httpd = server.AttestationServer(('127.0.0.1', 0), server.Handler)
    httpd.agent, httpd.context, httpd.allowed = agent, context, None
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, calls
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def request(httpd, hotkey=HOTKEY, source='127.0.0.1', body=None):
    context = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection('127.0.0.1', httpd.server_port, timeout=2,
                                       context=context, source_address=(source, 0))
    try:
        conn.request('POST', '/attest', body=body if body is not None else json.dumps({'hotkey': hotkey, 'nonce': 'ab' * 32}))
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_wrong_hotkey_does_not_collect_or_consume_limit(service):
    httpd, calls = service
    assert request(httpd, '6' * 48) == (403, {'error': 'hotkey'})
    assert not calls
    assert request(httpd)[0] == 200
    assert len(calls) == 1


def test_idle_tls_connection_does_not_block_other_clients(service):
    httpd, _ = service
    with socket.create_connection(httpd.server_address, timeout=2):
        assert request(httpd)[0] == 200


def test_limits_per_source_and_global(service):
    httpd, calls = service
    assert request(httpd)[0] == 200
    assert request(httpd)[0] == 429
    for i in range(2, 7):
        assert request(httpd, source=f'127.0.0.{i}')[0] == 200
    assert request(httpd, source='127.0.0.7')[0] == 429
    assert len(calls) == 6


def test_allowlist_blocks_before_parsing(service):
    httpd, calls = service
    httpd.allowed = (server.ipaddress.ip_network('127.0.0.2/32'),)
    assert request(httpd, body='not json') == (403, {'error': 'forbidden'})
    assert request(httpd, source='127.0.0.2')[0] == 200
    assert len(calls) == 1


def test_missing_and_invalid_hotkey_refuse_start(monkeypatch):
    monkeypatch.delenv('SN92_MINER_HOTKEY', raising=False)
    with pytest.raises(ValueError, match='SN92_MINER_HOTKEY'):
        server.main()
    monkeypatch.setenv('SN92_MINER_HOTKEY', 'invalid')
    with pytest.raises(ValueError, match='SN92_MINER_HOTKEY'):
        server.main()


def test_invalid_allowlist_refuses_start(monkeypatch):
    monkeypatch.setenv('SN92_MINER_HOTKEY', HOTKEY)
    monkeypatch.setenv('SN92_VALIDATOR_ALLOW', '')
    with pytest.raises(ValueError, match='SN92_VALIDATOR_ALLOW'):
        server.main()


def test_invalid_body_does_not_consume_quota(service):
    httpd, calls = service
    assert request(httpd, body='{}')[0] == 400
    assert request(httpd)[0] == 200
    assert len(calls) == 1


def test_rate_limit_expires():
    agent = server.Agent(b't' * 32, WORKLOAD, b'', HOTKEY)
    assert agent.admit('127.0.0.1')
    assert not agent.admit('127.0.0.1')
    agent.recent = [(t - 10, ip) for t, ip in agent.recent]
    assert agent.admit('127.0.0.1')
