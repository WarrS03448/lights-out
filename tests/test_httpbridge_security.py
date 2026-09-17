"""Exercise the real local bridge without an app or game process."""
import http.client
from types import SimpleNamespace

import pytest

from hub.webui import httpbridge


@pytest.fixture
def bridge():
    calls = []
    panel = SimpleNamespace(last_payload='{}', post=lambda fn: fn(),
                            session=SimpleNamespace(cancel_queue=lambda: calls.append('cancel')),
                            chrome=SimpleNamespace(close=lambda: calls.append('close'),
                                                   is_maximized=lambda: False))
    server, _ = httpbridge.start(panel)

    def request(path, method='GET', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        try:
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()
    yield request, calls, 'http://127.0.0.1:%d' % server.server_port
    server.shutdown()
    server.server_close()


def test_all_local_routes_reject_rebinding_and_foreign_origins(bridge):
    request, calls, origin = bridge
    for path in ('/state', '/index.html'):
        assert request(path, headers={'Host': 'attacker.example'})[0] == 403
    for path in ('/verb/cancel_search', '/window/close'):
        for bad in ('https://attacker.example', 'null', ''):
            assert request(path, 'POST', '[]', {'Origin': bad, 'Content-Type': 'application/json'})[0] == 403
    assert calls == []
    assert request('/window/close', 'POST', headers={'Origin': origin})[0] == 200
    assert calls == ['close']


def test_malformed_oversized_or_form_verbs_never_dispatch(bridge):
    request, calls, origin = bridge
    for body, ctype in (('{', 'application/json'), ('{}', 'application/json'),
                         ('[]', 'text/plain'), ('x' * 65537, 'application/json')):
        assert request('/verb/cancel_search', 'POST', body,
                       {'Origin': origin, 'Content-Type': ctype})[0] == 400
    assert calls == []
    assert request('/verb/cancel_search', 'POST', '[]',
                   {'Origin': origin, 'Content-Type': 'application/json'})[0] == 200
    assert calls == ['cancel']
