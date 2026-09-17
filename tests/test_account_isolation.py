"""Account changes invalidate private caches and asynchronous work."""
import time
from types import SimpleNamespace

from hub import competitive as C
from test_screen_matchflow import _panel


def test_switching_accounts_discards_private_data_and_penalties(monkeypatch):
    _, s = _panel()
    monkeypatch.setattr(s, '_disconnect', lambda: None)
    monkeypatch.setattr(s, '_connect', lambda: None)
    s.history = [{'id': 'old-account-match'}]
    s.friends = ({'steam_id':'private-friend'},)
    s.friend_code = 'PRIVATE'
    s.postmatch = {'id':'private-result'}
    s.penalty_until = time.time() + 3600
    s.adopt_account({'steam_id':'76561198000000002','token':'other'})
    assert s.history is None and not s.friends and not s.friend_code
    assert s.postmatch is None and s.penalty_until == 0


def test_old_http_results_cannot_modify_new_account(monkeypatch):
    panel, s = _panel()
    workers, deliveries, results = [], [], []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', deliveries.append)
    monkeypatch.setattr(s, '_disconnect', lambda: None)
    monkeypatch.setattr(s, '_connect', lambda: None)
    C.LiveSession._action(s, lambda: (200, {'private':'old'}), lambda status,body: results.append(body))
    workers.pop()()
    s.adopt_account({'steam_id':'76561198000000002','token':'other'})
    for callback in deliveries: callback()
    assert results == []


def test_invited_marker_clears_when_player_joins(monkeypatch):
    _, s = _panel()
    s.party = {'code':'ABCDE','leader_id':'me','members':[]}
    s.invite_sent = ('friend',)
    s.on_live_event({'type':'party_update','code':'ABCDE','leader_id':'me',
                     'members':[{'steam_id':'friend'}]})
    assert not s.invite_sent


def test_old_queue_tick_and_retry_cannot_run_for_new_account(monkeypatch):
    _, s = _panel()
    timers = []
    monkeypatch.setattr(s, '_later', lambda ms, fn: timers.append(fn))
    s.phase = 'queued'
    s._start_queue_tick()
    old_tick = timers.pop()
    s._clear_account_state()
    s.phase = 'queued'
    s._start_queue_tick()
    new_tick = timers.pop()
    old_tick()
    new_tick()
    assert s.queue_seconds == 1 and len(timers) == 1


def test_previous_invite_timer_cannot_clear_new_invitation(monkeypatch):
    _, s = _panel()
    s.party = {'code': 'ABCDE'}
    timers = []
    monkeypatch.setattr(s, '_later', lambda ms, fn: timers.append(fn))
    s.invite_sent = ('friend',)
    s._invite_result(200, {'expires_in': 120}, 'friend')
    s._invite_result(200, {'expires_in': 120}, 'friend')
    timers[0]()
    assert s.invite_sent == ('friend',)
    timers[1]()
    assert not s.invite_sent
