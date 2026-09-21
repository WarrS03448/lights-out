"""Run: python -m pytest tests/test_screen_messages.py"""
from tests.test_screen_bugreport import _panel
from hub import i18n


def test_inbox_copy_is_complete_in_all_supported_languages():
    from hub.webui.screens.messages import STRINGS
    assert set(STRINGS) == set(i18n.CODES)
    assert all(set(v) == set(STRINGS['en']) and all(v.values()) for v in STRINGS.values())


def test_official_reply_retries_keep_the_same_id_and_signout_clears_messages():
    _, session = _panel()
    calls = []
    session.messages_target = 'admin'
    session.client.send_message = lambda *args: (calls.append(args) or (503, {}))
    session.send_private_message('A reply to admin')
    session.send_private_message('A reply to admin')
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][0] == 'admin'
    session.messages_data = {'threads': [{'last_text': 'Private'}]}
    session.messages_thread = {'messages': [{'text': 'Private'}]}
    session._clear_account_state()
    assert session.messages_data is None and session.messages_thread is None
    assert not session.messages_target and session.messages_pending is None


def test_old_conversation_response_cannot_replace_the_new_selection():
    _, session = _panel()
    callbacks = []
    session._action = lambda worker, callback: callbacks.append(callback)
    session.open_messages('76561198000000001')
    session.open_messages('76561198000000002')
    callbacks[0](200, {'ok': True, 'messages': [{'text': 'Old private conversation'}]})
    assert session.messages_thread is None
    callbacks[1](200, {'ok': True, 'messages': [{'text': 'Current'}]})
    assert session.messages_thread['messages'][0]['text'] == 'Current'


def test_refresh_preserves_loaded_history_and_moves_past_retention_boundary():
    _, session = _panel()
    session.messages_target = 'admin'
    session.messages_thread = {'messages': [{'seq': n, 'text': str(n)} for n in range(1, 101)], 'next_before': None}
    session.client.message_thread = lambda *args: (200, {'ok': True, 'messages': [{'seq': n, 'text': str(n)} for n in range(70, 120)], 'next_before': 70})
    session.open_messages('admin')
    assert len(session.messages_thread['messages']) == 119
    assert session.messages_thread['next_before'] is None
    session.client.message_thread = lambda *args: (200, {'ok': True, 'messages': [{'seq': n, 'text': str(n)} for n in range(701, 751)], 'next_before': 701})
    session.open_messages('admin')
    assert session.messages_thread['messages'][0]['seq'] == 701
    assert session.messages_thread['next_before'] == 701


def test_uncertain_send_ids_survive_sends_in_other_conversations():
    _, session = _panel()
    calls = []
    session.client.send_message = lambda *args: (calls.append(args) or (503, {}))
    session.messages_target = 'admin'
    session.send_private_message('Uncertain reply')
    session.messages_target = '76561198000000002'
    session.send_private_message('Another uncertain reply')
    session.messages_target = 'admin'
    session.send_private_message('Uncertain reply')
    assert calls[0] == calls[2]

def test_message_nudge_does_not_cancel_older_history_request():
    _, session = _panel()
    callbacks = []
    session.messages_target = 'admin'
    session.messages_thread = {'messages': [{'seq': 51, 'text': 'Current'}], 'next_before': 51}
    session._action = lambda worker, callback: callbacks.append(callback)
    session.client.messages = lambda: (200, {'ok': True, 'threads': []})
    session.open_messages('admin', before=51)
    session.on_live_event({'type': 'message_update'})
    callbacks[0](200, {'ok': True, 'messages': [{'seq': 50, 'text': 'Older'}], 'next_before': None})
    assert session.messages_thread['messages'][0]['seq'] == 50
