"""Windows credential protection, without contacting a service."""
import json
import os
import pytest
from hub import credentials


@pytest.mark.skipif(os.name != "nt", reason="Windows CurrentUser DPAPI")
def test_current_user_round_trip_and_tamper_rejection():
    original = {"token": "lo_" + "a" * 43, "player_id": "a1111111-1111-4111-8111-111111111111"}
    saved = credentials.seal(original)
    assert original["token"] not in json.dumps(saved)
    assert credentials.open_sealed(saved) == original
    with pytest.raises(credentials.CredentialError):
        credentials.open_sealed({**saved, "protected": "broken"})


@pytest.mark.skipif(os.name != "nt", reason="Windows CurrentUser DPAPI")
def test_state_migrates_plaintext_once_and_never_restores_a_broken_seal(tmp_path):
    from hub import state
    path = tmp_path / "state.json"
    token = "lo_" + "b" * 43
    path.write_text(json.dumps({"auth": {"token": token, "player_id": "a1111111-1111-4111-8111-111111111111"}}), encoding="utf-8")
    assert state.load(path)["auth"]["token"] == token
    assert token not in path.read_text(encoding="utf-8")
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["auth"]["protected"] = "broken"
    path.write_text(json.dumps(saved), encoding="utf-8")
    assert not state.load(path).get("auth")


def test_failed_credential_migration_does_not_crash_startup(tmp_path, monkeypatch):
    from hub import state
    path=tmp_path/'state.json'
    path.write_text(json.dumps({'auth':{'token':'old-token','steam_id':'76561198000000001'}}),encoding='utf-8')
    monkeypatch.setattr(credentials,'seal',lambda value:{'protected':'fixture'})
    monkeypatch.setattr(credentials,'open_sealed',lambda value:{'token':'old-token','steam_id':'76561198000000001'})
    def deny(*args):raise PermissionError('fixture')
    monkeypatch.setattr(state,'_write_state',deny)
    loaded=state.load(path)
    assert loaded['auth'] is None
    assert loaded.keys()==state.default_state().keys()


@pytest.mark.skipif(os.name != 'nt', reason='Windows CurrentUser DPAPI')
def test_offline_signout_is_encrypted_and_retried_without_restoring_login(monkeypatch):
    from hub import auth, paths, state, telemetry
    token = 'lo_' + 'z' * 43
    monkeypatch.setattr(auth, '_request', lambda *a, **kw: (503, {}))
    monkeypatch.setattr(telemetry, 'identify', lambda *_: pytest.fail('Revoking a stale login changed telemetry'))
    assert auth.revoke_session(token) is False
    records = list((paths.state_dir() / 'pending-signouts').glob('*.json'))
    assert len(records) == 1
    assert token not in records[0].read_text(encoding='utf-8')
    assert credentials.open_sealed(json.loads(records[0].read_text(encoding='utf-8'))) == {'token': token}
    assert state.load().get('auth') is None
    calls = []
    def success(path, **kwargs):
        calls.append((path, kwargs['token']))
        return 200, {}
    monkeypatch.setattr(auth, '_request', success)
    auth.retry_signouts()
    assert calls == [('/api/auth/signout', token)]
    assert not list((paths.state_dir() / 'pending-signouts').glob('*.json'))


@pytest.mark.parametrize('classic', [False, True])
def test_failed_save_preserves_previous_in_memory_state(monkeypatch, classic):
    from types import SimpleNamespace
    from hub import competitive, state
    from hub.webui.panel import WebPanel
    previous = {'token': 'previous'}
    panel = SimpleNamespace(app=SimpleNamespace(state={'auth': previous}))
    def deny(*_):
        raise PermissionError('fixture')
    monkeypatch.setattr(state, 'update_fields', deny)
    panel_type = competitive.CompetitivePanel if classic else WebPanel
    assert panel_type.save_auth(panel, {'token': 'new'}) is False
    assert panel.app.state['auth'] is previous
