"""Run: python -m pytest tests/test_ranked_modes.py"""
import urllib.request
from tests.test_screen_bugreport import _panel
from hub.live import LiveClient


def test_one_person_party_blocks_duel_button_and_direct_find_action():
    from hub.webui.screens.competitive import comp_snapshot
    from hub.ranked_modes import strings
    panel, session = _panel()
    session.ranked_mode = 'BB1'
    session.gamemode_installed = lambda: True
    session.update_needed = lambda: None
    session.party = {'code': 'ABCDEF', 'leader_id': session.me['steam_id'], 'members': [session.me]}
    assert not comp_snapshot(session, panel)['can_find']
    session.find_match()
    assert session.phase == 'idle' and session.error == strings()['solo_only']
    session.party = None
    assert comp_snapshot(session, panel)['can_find']


def test_selected_mode_stamps_requests_and_both_installed_versions():
    client = LiveClient('token', versions=lambda: {'BB5': '1.0.29', 'BB1': '1.0.0'})
    client.ranked_mode = 'BB1'
    req = urllib.request.Request('http://localhost/api/queue/join')
    client._stamp(req)
    assert req.get_header('X-ranked-mode') == 'BB1'
    assert req.get_header('X-bb5-version') == '1.0.29'
    assert req.get_header('X-bb1-version') == '1.0.0'


def test_both_ranks_are_cached_without_overwriting_selected_ladder():
    _, session = _panel()
    session.on_live_event({'type': 'rating', 'mode': 'BB5', 'rank_name': 'Five', 'rr': 11})
    session.on_live_event({'type': 'rating', 'mode': 'BB1', 'rank_name': 'One', 'rr': 22})
    assert session.me['rr'] == 11
    assert session.ranked_ranks['BB1']['rr'] == 22
    session.select_ranked_mode('BB1')
    assert session.ranked_mode == 'BB1' and session.me['rr'] == 22
    session.phase = 'queued'
    session.select_ranked_mode('BB5')
    assert session.ranked_mode == 'BB1'
    session._clear_account_state()
    assert session.ranked_ranks == {} and session.ranked_mode == 'BB5'


def test_a_penalty_in_one_mode_does_not_block_the_other():
    _, session = _panel()
    session.on_live_event({'type': 'penalty', 'mode': 'BB5', 'seconds': 100, 'count': 2})
    session.select_ranked_mode('BB1')
    assert session.banned_left() == 0
    session.select_ranked_mode('BB5')
    assert 90 <= session.banned_left() <= 100


def test_refund_messages_are_localized_without_changing_stored_message_or_steam_name():
    from hub import i18n
    from hub.webui.screens.messages import snapshot
    panel, session = _panel()
    original = i18n.get_language()
    try:
        for mode in ('BB5', 'BB1'):
            context = {'type': 'rr_refund', 'mode': mode, 'amount': 24, 'cheaters': ['Steam <Player>']}
            session.messages_thread = {'messages': [{'text': 'Original', 'context': context}]}
            for lang in i18n.CODES:
                i18n.set_language(lang)
                text = snapshot(session, panel)['messages']['thread']['messages'][0]['text']
                assert '24' in text and 'Steam <Player>' in text and ('1v1' if mode == 'BB1' else '5v5') in text
                assert session.messages_thread['messages'][0]['text'] == 'Original'
    finally:
        i18n.set_language(original)
