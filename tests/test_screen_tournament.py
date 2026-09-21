"""Run: python -m pytest tests/test_screen_tournament.py"""
from tests.test_screen_bugreport import _panel
from hub import i18n
from hub.webui.screens import SCREEN_SNAPSHOTS, SCREEN_VERBS

def test_screen_and_registration_verbs_are_bundled():
    assert 'tournament' in SCREEN_SNAPSHOTS
    assert 'tournament_register' in SCREEN_VERBS
    assert 'tournament_refresh' in SCREEN_VERBS

def test_all_languages_have_complete_event_copy():
    from hub.webui.screens.tournament import STRINGS
    assert set(STRINGS) == set(i18n.CODES)
    for values in STRINGS.values():
        assert set(values) == set(STRINGS['en'])
        assert all(values.values())
    assert STRINGS['en']['register'] == 'Register for Tournament'

def test_registration_uses_existing_authenticated_client_and_refreshes():
    panel, session = _panel()
    calls = []
    session.client.register_tournament = lambda: (calls.append('register') or (200, {'ok': True, 'registered_at': 123}))
    session.client.tournament = lambda: (calls.append('refresh') or (200, {'ok': True, 'server_now': 1000, 'registered_at': 123, 'leaders': []}))
    session.register_tournament()
    assert calls == ['register', 'refresh']
    assert session.tournament_data['registered_at'] == 123
    session._clear_account_state()
    assert session.tournament_data is None
    assert not session.tournament_loading

def test_failed_refresh_retains_last_known_standings():
    panel, session = _panel()
    session.tournament_data = {'ok': True, 'server_now': 1, 'you': {'net_rr': 20}}
    session.client.tournament = lambda: (503, {'ok': False, 'error': 'unavailable'})
    session.refresh_tournament()
    assert session.tournament_data['you']['net_rr'] == 20
    assert session.tournament_error == 'unavailable'
