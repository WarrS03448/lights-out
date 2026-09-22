"""Connection failures must recover without replaying actions or crossing accounts."""
import io
import http.client
import urllib.error
from types import SimpleNamespace

import pytest

from hub import auth, catalogue, competitive as C, live
from test_screen_matchflow import _panel

PLAY = "play.lightsoutranked.com"
ROOT = "lightsoutranked.com"


class Socket:
    def __init__(self, body, status=200, headers="", fail_send=False):
        self.writes = []
        self.timeout = None
        self.fail_send = fail_send
        self.data = (f"HTTP/1.1 {status} Fixture\r\nContent-Length: {len(body)}\r\n"
                     f"{headers}\r\n").encode() + body

    def sendall(self, data):
        self.writes.append(data)
        if self.fail_send:
            raise TimeoutError("response lost after a possible send")

    def makefile(self, *args):
        return io.BytesIO(self.data)

    def settimeout(self, timeout): self.timeout = timeout
    def close(self): pass


@pytest.fixture
def network(monkeypatch):
    """Only replace the external socket; exercise real urllib and app consumers."""
    connections, sockets = [], []
    config = dict(down={PLAY}, body=b'{"ok":true}', status=200, headers="", fail_send=False)
    monkeypatch.setattr(auth, 'API_BASE', 'https://' + PLAY)
    monkeypatch.setattr(live, 'API_BASE', 'https://' + PLAY)
    monkeypatch.setattr('urllib.request.getproxies', lambda: {})
    # Each test starts a new process-equivalent route preference.
    import sys
    transport = sys.modules.get('hub.transport')
    if transport:
        monkeypatch.setattr(transport, '_preferred', PLAY)

    def connect(connection):
        connections.append((connection.host, connection.timeout))
        if connection.host in config['down']:
            raise TimeoutError('secure connection unavailable')
        sock = Socket(config['body'], config['status'], config['headers'], config['fail_send'])
        sockets.append((connection.host, sock))
        connection.sock = sock
    monkeypatch.setattr(http.client.HTTPSConnection, 'connect', connect)
    monkeypatch.setattr(http.client.HTTPConnection, 'connect', connect)
    return config, connections, sockets


def test_saved_login_uses_backup_after_tls_timeout(network):
    config, connections, sockets = network
    assert auth._request('/api/auth/me', token='fixture-token') == (200, {'ok': True})
    assert [host for host, _ in connections] == [PLAY, ROOT]
    wire = b''.join(sockets[0][1].writes)
    assert b'Host: lightsoutranked.com\r\n' in wire
    assert b'Authorization: Bearer fixture-token\r\n' in wire


def test_post_switches_only_before_any_request_bytes_are_sent(network):
    _, connections, sockets = network
    client = live.LiveClient('fixture-token')
    status, body = client._post_request('/api/messages/send', {'text': 'once'})
    assert status == 200 and body['ok']
    assert [host for host, _ in connections] == [PLAY, ROOT]
    assert len(sockets) == 1
    wire = b''.join(sockets[0][1].writes)
    assert wire.count(b'POST /api/messages/send ') == 1 and b'"text": "once"' in wire


def test_post_is_not_replayed_after_ambiguous_send_failure(network):
    config, connections, _ = network
    config.update(down=set(), fail_send=True)
    assert live.LiveClient('fixture-token')._post_request('/api/party/create')[0] == 0
    assert [host for host, _ in connections] == [PLAY]


@pytest.mark.parametrize('status', [401, 403, 429, 503])
def test_http_rejection_does_not_trigger_route_retry(network, status):
    config, connections, _ = network
    config.update(down=set(), status=status)
    assert auth._request('/api/auth/me', token='fixture-token')[0] == status
    assert [host for host, _ in connections] == [PLAY]


def test_healthy_backup_is_reused_and_can_fail_back(network):
    config, connections, _ = network
    assert auth._request('/api/auth/me')[0] == 200
    assert live.stats() == {'ok': True}
    assert [host for host, _ in connections] == [PLAY, ROOT, ROOT]
    config['down'] = {ROOT}
    assert auth._request('/api/auth/me')[0] == 200
    assert [host for host, _ in connections][-2:] == [ROOT, PLAY]


def test_both_routes_down_stops_after_one_attempt_each(network):
    config, connections, _ = network
    config['down'] = {PLAY, ROOT}
    with pytest.raises(auth.AuthError): auth._request('/api/auth/me')
    assert [host for host, _ in connections] == [PLAY, ROOT]
    assert all(0 < timeout <= 8 for _, timeout in connections)


def test_custom_api_does_not_fall_back_to_production(network, monkeypatch):
    config, connections, _ = network
    config['down'] = {'staging.example.test'}
    monkeypatch.setattr(auth, 'API_BASE', 'https://staging.example.test')
    with pytest.raises(auth.AuthError): auth._request('/api/auth/me')
    assert [host for host, _ in connections] == ['staging.example.test']


@pytest.mark.parametrize('target', ['https://untrusted.example.test/steal', 'http://lightsoutranked.com/steal'])
def test_owned_route_redirect_cannot_leak_credentials(network, target):
    config, connections, _ = network
    config.update(down=set(), status=302, headers=f'Location: {target}\r\n')
    assert auth._request('/api/auth/me', token='fixture-token')[0] == 302
    assert [host for host, _ in connections] == [PLAY]


def test_download_falls_back_and_keeps_hash_verification(network, tmp_path):
    import hashlib
    config, connections, _ = network
    config['body'] = b'fixture download bytes'
    output = tmp_path / 'download'
    catalogue._download('https://' + PLAY + '/packs/fixture.zip', output,
                        hashlib.sha256(config['body']).hexdigest())
    assert output.read_bytes() == config['body']
    assert [host for host, _ in connections] == [PLAY, ROOT]
    with pytest.raises(RuntimeError, match='checksum'):
        catalogue._download('https://' + PLAY + '/packs/fixture.zip', output, '0' * 64)
    assert not output.exists()


def test_live_stream_falls_back_and_delivers_events(network):
    config, connections, sockets = network
    config['body'] = b': ping\n\ndata: {"type":"stats","online":7}\n\n'
    events = []
    client = live.LiveClient('fixture-token', on_event=events.append)
    client._read_stream()
    assert events == [{'type': 'stats', 'online': 7}]
    assert [host for host, _ in connections] == [PLAY, ROOT]
    assert sockets[-1][1].timeout == 60


def test_me_distinguishes_offline_from_rejected_token(monkeypatch):
    def offline(*args, **kwargs): raise auth.AuthUnavailable('temporary')
    monkeypatch.setattr(auth, '_request', offline)
    with pytest.raises(auth.AuthUnavailable): auth.me('saved-fixture')
    monkeypatch.setattr(auth, '_request', lambda *a, **kw: (401, {'ok': False}))
    assert auth.me('saved-fixture') is None


@pytest.fixture
def restore(monkeypatch):
    panel, session = _panel()
    session.me = None; session.token = ''; session.phase = 'signed_out'
    workers, timers, adopted = [], [], []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(session, '_later', lambda ms, fn: timers.append((ms, fn)))
    monkeypatch.setattr(session, 'adopt_account', adopted.append)
    return session, workers, timers, adopted


def test_saved_login_recovers_after_outage_without_restart(restore, monkeypatch):
    session, workers, timers, adopted = restore
    def offline(_): raise auth.AuthUnavailable('temporary')
    monkeypatch.setattr(auth, 'me', offline)
    session.restore_account({'token': 'saved-fixture', 'remember_me': True})
    workers.pop(0)()
    assert len(timers) == 1 and not adopted
    delay, retry = timers.pop(0)
    assert 1000 <= delay <= 30000
    monkeypatch.setattr(auth, 'me', lambda _: {'ok': True, 'steam_id': 'fixture-player'})
    retry(); workers.pop(0)()
    assert adopted == [{'token': 'saved-fixture', 'remember_me': True, 'ok': True, 'steam_id': 'fixture-player'}]
    assert not timers and session.phase == 'idle'


@pytest.mark.parametrize('cancel', ['sign_in', 'cancel_sign_in', 'sign_out'])
def test_cancelled_restore_never_retries_or_adopts(restore, monkeypatch, cancel):
    session, workers, timers, adopted = restore
    def offline(_): raise auth.AuthUnavailable('temporary')
    monkeypatch.setattr(auth, 'me', offline)
    session.restore_account({'token': 'saved-fixture'})
    workers.pop(0)()
    _, retry = timers.pop(0)
    getattr(session, cancel)()
    workers.clear()  # explicit sign-in/sign-out may start its own work
    monkeypatch.setattr(auth, 'me', lambda _: pytest.fail('stale saved-login request'))
    retry()
    assert not workers and not timers and not adopted


def test_invalid_saved_token_does_not_retry(restore, monkeypatch):
    session, workers, timers, adopted = restore
    monkeypatch.setattr(auth, 'me', lambda _: None)
    session.restore_account({'token': 'expired-fixture'})
    workers.pop(0)()
    assert not timers and not adopted


@pytest.mark.parametrize('status,body', [(503, {}), (429, {}), (200, []), (200, {'ok': False})])
def test_unavailable_or_malformed_me_does_not_discard_saved_login(monkeypatch, status, body):
    monkeypatch.setattr(auth, '_request', lambda *a, **kw: (status, body))
    with pytest.raises(auth.AuthUnavailable): auth.me('saved-fixture')


def test_account_change_invalidates_inflight_restore(restore, monkeypatch):
    session, workers, _, adopted = restore
    replies = []
    monkeypatch.setattr(session.panel, 'post', replies.append)
    monkeypatch.setattr(auth, 'me', lambda _: {'ok': True, 'steam_id': 'old-player'})
    session.restore_account({'token': 'old-fixture'})
    workers.pop(0)()
    session._clear_account_state()
    replies.pop(0)()
    assert not adopted


def test_closed_panel_cannot_restore_or_schedule_more_work(restore, monkeypatch):
    session, workers, timers, adopted = restore
    replies = []
    monkeypatch.setattr(session.panel, 'post', replies.append)
    monkeypatch.setattr(auth, 'me', lambda _: {'ok': True, 'steam_id': 'old-player'})
    session.restore_account({'token': 'saved-fixture'})
    workers.pop(0)()
    session.panel._closed = True
    replies.pop(0)()
    assert not adopted and not timers


def test_restore_retries_at_bounded_rate_and_rechecks_revocation(restore, monkeypatch):
    session, workers, timers, adopted = restore
    def offline(*args, **kwargs): raise auth.AuthUnavailable('temporary')
    monkeypatch.setattr(auth, '_request', offline)
    session.restore_account({'token': 'saved-fixture'})
    delays = []
    for _ in range(9):
        assert len(workers) == 1
        workers.pop(0)()
        assert len(timers) == 1
        delay, retry = timers.pop(0)
        delays.append(delay)
        retry()
    assert delays == sorted(delays) and max(delays) <= 30000 and min(delays) >= 1000
    monkeypatch.setattr(auth, 'revocation_pending', lambda _: True)
    workers.pop(0)()
    assert not workers and not timers and not adopted


def test_post_redirect_cannot_cause_action_replay(network):
    config, connections, _ = network
    config.update(down=set(), status=307, headers=f'Location: https://{ROOT}/api/party/create\r\n')
    assert live.LiveClient('fixture-token')._post_request('/api/party/create')[0] == 307
    assert [host for host, _ in connections] == [PLAY]


def test_preferred_host_does_not_override_custom_port(network, monkeypatch):
    config, connections, _ = network
    config['down'] = {PLAY}
    monkeypatch.setattr(auth, 'API_BASE', 'https://' + PLAY + ':444')
    with pytest.raises(auth.AuthError): auth._request('/api/auth/me')
    assert [host for host, _ in connections] == [PLAY]


@pytest.mark.parametrize('target', ['https://untrusted.example.test/steal', 'http://staging.example.test/steal'])
def test_custom_endpoint_redirect_cannot_leak_credentials(network, monkeypatch, target):
    config, connections, _ = network
    config.update(down=set(), status=302, headers=f'Location: {target}\r\n')
    monkeypatch.setattr(auth, 'API_BASE', 'https://staging.example.test')
    assert auth._request('/api/auth/me', token='fixture-token')[0] == 302
    assert [host for host, _ in connections] == ['staging.example.test']


def test_live_transport_uses_verified_tls_context(network, monkeypatch):
    import ssl
    base_connect = http.client.HTTPSConnection.connect
    def verify_context(connection):
        assert connection._context.verify_mode == ssl.CERT_REQUIRED
        assert connection._context.check_hostname is True
        return base_connect(connection)
    monkeypatch.setattr(http.client.HTTPSConnection, 'connect', verify_context)
    assert auth._request('/api/auth/me')[0] == 200


def test_application_auth_error_does_not_retry_forever(restore, monkeypatch):
    session, workers, timers, adopted = restore
    def terminal(_): raise auth.AuthError('Account unavailable', code='account_disabled')
    monkeypatch.setattr(auth, 'me', terminal)
    session.restore_account({'token': 'fixture'})
    workers.pop(0)()
    assert not timers and not adopted


def test_default_telemetry_transport_follows_reachable_route(network, tmp_path):
    from hub.telemetry import _Telemetry
    import urllib.request
    _, connections, _ = network
    sender = _Telemetry(outbox_path=tmp_path/'events.json', background=False)
    request = urllib.request.Request('https://' + PLAY + '/api/telemetry', data=b'{}', method='POST')
    with sender._open_url(request, timeout=10) as response:
        assert response.status == 200
    assert [host for host, _ in connections] == [PLAY, ROOT]


def test_read_request_send_ambiguity_is_not_replayed(network):
    config, connections, _ = network
    config.update(down=set(), fail_send=True)
    with pytest.raises(auth.AuthError): auth._request('/api/auth/me')
    assert [host for host, _ in connections] == [PLAY]
