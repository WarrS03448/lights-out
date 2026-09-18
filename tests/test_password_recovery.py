"""Recovery verifies email before accepting a password and never signs in implicitly."""
import json
from types import SimpleNamespace

import pytest
from hub import competitive as C
from hub.webui.snapshot import state_snapshot
from test_screen_matchflow import _panel


@pytest.fixture
def flow(monkeypatch):
    panel, session = _panel()
    session.me = None
    session.phase = 'signed_out'
    workers, calls = [], []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(session, '_changed', lambda: None)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: False)
    monkeypatch.setattr(session, 'adopt_account', lambda *a, **kw: pytest.fail('Recovery signed in'))
    replies = {'forgot-password': {'challenge': 'c' * 43},
               'forgot-password/verify': {'reset_token': 'r' * 43}, 'reset-password': {'ok': True}}
    def request(action, payload):
        calls.append((action, dict(payload)))
        return replies[action]
    monkeypatch.setattr(C.auth_mod, 'account_request', request)
    return panel, session, workers, calls


def test_recovery_requires_verified_code_and_keeps_grant_out_of_snapshot(flow):
    panel, s, workers, calls = flow
    s.account_action('reset-password', {'password': 'new-password', 'token': 'injected'})
    assert not workers
    s.account_action('forgot-password', {'email': 'fixture@example.test'})
    assert workers, 'Forgot password must send the recovery request'
    workers.pop(0)()
    assert s.account_step == 'recovery_code'
    s.account_action('forgot-password/verify', {'code': '123456', 'challenge': 'injected'})
    workers.pop(0)()
    assert calls[-1] == ('forgot-password/verify', {'challenge': 'c' * 43, 'code': '123456'})
    assert s.account_step == 'reset_password'
    snapshot = json.dumps(state_snapshot(s, panel))
    assert 'r' * 43 not in snapshot and 'c' * 43 not in snapshot and '123456' not in snapshot
    s.account_action('reset-password', {'password': 'new-password', 'token': 'injected'})
    workers.pop()()  # Snapshot may queue an unrelated desktop background probe.
    assert calls[-1] == ('reset-password', {'token': 'r' * 43, 'password': 'new-password'})
    assert s.account_step == 'login' and s.error
    assert not s._reset_token and not s._recovery_challenge


@pytest.mark.parametrize('stage', ['forgot-password', 'forgot-password/verify', 'reset-password'])
def test_cancelled_recovery_response_cannot_reopen_flow(flow, stage):
    _, s, workers, _ = flow
    for action, fields in [('forgot-password', {'email': 'fixture@example.test'}),
                           ('forgot-password/verify', {'code': '123456'}),
                           ('reset-password', {'password': 'new-password'})]:
        s.account_action(action, fields)
        assert workers
        if action == stage:
            s.cancel_sign_in()
            workers.pop(0)()
            assert not s.account_step and not s.account_busy
            assert not s._reset_token and not s._recovery_challenge
            break
        workers.pop(0)()


def test_expired_grant_returns_to_recovery_request(flow, monkeypatch):
    _, s, workers, _ = flow
    s.account_action('forgot-password', {'email': 'fixture@example.test'}); workers.pop(0)()
    s.account_action('forgot-password/verify', {'code': '123456'}); workers.pop(0)()
    def expired(*args):
        raise C.auth_mod.AuthError('expired', code='invalid_code', status=400)
    monkeypatch.setattr(C.auth_mod, 'account_request', expired)
    s.account_action('reset-password', {'password': 'new-password'}); workers.pop(0)()
    assert s.account_step == 'forgot_password' and not s._reset_token
    assert s.error


def test_failed_resend_keeps_original_code_usable(flow, monkeypatch):
    _, s, workers, calls = flow
    s.account_action('forgot-password', {'email': 'fixture@example.test'}); workers.pop(0)()
    original = C.auth_mod.account_request
    def limited(*args):
        raise C.auth_mod.AuthError('limited', code='rate_limited', status=429)
    monkeypatch.setattr(C.auth_mod, 'account_request', limited)
    s.account_action('forgot-password', {'email': 'fixture@example.test'}); workers.pop(0)()
    monkeypatch.setattr(C.auth_mod, 'account_request', original)
    s.account_action('forgot-password/verify', {'code': '123456'})
    assert workers, 'A failed resend must leave the existing email code usable'
    workers.pop(0)()
    assert s.account_step == 'reset_password'
