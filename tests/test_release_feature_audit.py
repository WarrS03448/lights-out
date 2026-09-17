"""Release regressions use only scratch files and simulated game operations."""
from types import SimpleNamespace
import pytest
from hub import competitive as C, state, activity
from hub.webui.screens import gamemodes, settings
from test_screen_matchflow import _panel


@pytest.mark.parametrize('ui_first', [False, True])
def test_pack_and_ui_state_writes_preserve_each_others_fields(tmp_path, ui_first):
    path = tmp_path / 'state.json'
    state.save({**state.default_state(), 'auth': {'token': 'old', 'steam_id': 'old'}}, path)
    writes = [{'installed': {'BB5': {'version': '1.0.27'}}, 'pak_sha256': 'new', 'game_dir': 'game'},
              {'auth': None, 'language': 'fr', 'ui_click_volume': 72}]
    if ui_first:
        writes.reverse()
    for fields in writes:
        state.update_fields(fields, path)
    saved = state.load(path)
    assert saved['auth'] is None and saved['language'] == 'fr' and saved['ui_click_volume'] == 72
    assert saved['installed']['BB5']['version'] == '1.0.27' and saved['pak_sha256'] == 'new'


@pytest.mark.parametrize('phase', ['checking', 'queued', 'found', 'lobby', 'connecting', 'live'])
def test_game_file_commands_refuse_during_every_match_phase(monkeypatch, phase):
    panel, session = _panel()
    session.phase = phase
    workers = []
    monkeypatch.setattr(gamemodes, 'run_async', workers.append)
    gamemodes._begin(panel, 'uninstall', 'BB5', set())
    settings._apply_game_path(panel, 'replacement')
    assert not workers and panel.settings_game_error


def test_queue_and_party_entry_refuse_during_file_job():
    panel, session = _panel()
    panel._gamemodes_job = {'busy': True}
    session.find_match()
    session.join_party('ABCDE')
    session.accept_party_invite('friend')
    assert session.phase == 'idle' and session.error and session.party_error and session.invite_error
    session.party = {'code': 'ABCDE', 'leader_id': 'someone-else', 'members': []}
    panel._gamemodes_job = None
    assert activity.files_locked(panel)


@pytest.mark.parametrize('invite', [False, True])
def test_pending_party_membership_blocks_files_until_the_roster_arrives(monkeypatch, invite):
    panel, session = _panel()
    callbacks = []
    monkeypatch.setattr(session, '_action', lambda fn, callback: callbacks.append(callback))
    session.client = SimpleNamespace(join_party=lambda code: None, accept_party_invite=lambda who: None)
    if invite:
        session.accept_party_invite('friend')
    else:
        session.join_party('ABCDEF')
    assert activity.files_locked(panel)
    callbacks[0](200, {'ok': True})
    assert activity.files_locked(panel), 'success can arrive before the roster event'
    session.on_live_event({'type':'party_update','code':'ABCDEF','leader_id':'someone-else','members':[]})
    assert not session._party_transition and activity.files_locked(panel)


def test_party_transition_is_serialized_and_old_roster_cannot_unlock_files(monkeypatch):
    panel, session = _panel()
    session.party = {'code': 'OLDOLD', 'leader_id': session.me['steam_id'], 'members': []}
    callbacks = []
    monkeypatch.setattr(session, '_action', lambda fn, callback: callbacks.append(callback))
    session.client = SimpleNamespace(join_party=lambda code: None, accept_party_invite=lambda who: None)
    session.join_party('NEWNEW')
    session.join_party('SECOND')
    session.accept_party_invite('someone')
    assert len(callbacks) == 1
    session.on_live_event({'type': 'party_update', 'code': 'OLDOLD', 'leader_id': session.me['steam_id'], 'members': []})
    assert activity.files_locked(panel)
    session.on_live_event({'type': 'party_update', 'code': None})
    assert activity.files_locked(panel)
    callbacks[0](404, {'error': 'expired'})
    assert not activity.files_locked(panel)


@pytest.mark.parametrize('host', [True, False])
def test_failed_handoff_does_not_launch_and_cold_retry_prepares_again(monkeypatch, host):
    panel, session = _panel()
    panel.app.game_dir = 'scratch-game'
    session.phase = 'connecting'
    session.match_id = 'a' * 16
    session.map = 'Rome'
    session.host = session.me if host else {'steam_id': '76561198000000042'}
    session.host_ready = True
    attempts, launches = [], []
    def prepare(*args, **kwargs):
        attempts.append(kwargs.get('role', 'host'))
        return '' if len(attempts) == 1 else 'map-level'
    monkeypatch.setattr(C.lobbypak_mod, 'prepare', prepare)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: False)
    monkeypatch.setattr(C.game_mod, 'launch_game', lambda: launches.append(True) or True)
    monkeypatch.setattr(session, '_take_game_ownership', lambda: None)
    session.relaunch_game()
    assert not launches and session.error and not session.host_pak_done and not session.pak_done
    session.relaunch_game()
    assert launches == [True] and len(attempts) == 2
    assert session.host_pak_done if host else session.pak_done


def test_joiner_cleanup_uses_install_path_and_retries_when_locked(monkeypatch):
    panel, session = _panel()
    session.pak_done = True
    session._lobby_pak_dir = 'original'
    panel.app.game_dir = 'changed'
    calls = []
    monkeypatch.setattr(C.lobbypak_mod, 'remove', lambda path: calls.append(path) or len(calls) > 1)
    monkeypatch.setattr(C.game_mod, 'game_running_cached', lambda *args: False)
    monkeypatch.setattr(C.MockSession, '_watch_tick', lambda self: None)
    session.reset_match()
    assert session._pending_lobby_cleanup == {'original'}
    session._watch_tick()
    assert calls == ['original', 'original'] and not session._pending_lobby_cleanup


def test_connect_replay_restores_teams_sides_and_host_readiness():
    _, session = _panel()
    mine = session.me['steam_id']
    host = '76561198000000042'
    session._on_connecting({'match_id': 'abc', 'host': host, 'stamped': True, 'connected': [host],
                            'players': [{'steam_id': mine}, {'steam_id': host}],
                            'teams': {'1': [host], '2': [mine]}, 'sides': {'1': 'defend', '2': 'attack'},
                            'connect_seconds': 123})
    assert session.host_ready and session.join_left == 123 and session.my_team() == 2
    assert session.sides == {1: 'defend', 2: 'attack'}
