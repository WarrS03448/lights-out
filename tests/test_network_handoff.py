from types import SimpleNamespace
from hub.competitive import LiveSession


def test_connect_uses_server_host_even_when_another_player_has_lower_display_ping():
    session = LiveSession.__new__(LiveSession)
    session.phase = 'lobby'
    session.players = [{'steam_id': 'a', 'ping': 1}, {'steam_id': 'b', 'ping': 40}]
    session.host = session.players[1]
    session.map = 'Rome'
    session.teams = {1: [session.players[0]], 2: [session.players[1]]}
    session.sides = {}
    session.bans = []
    calls = []
    session.client = SimpleNamespace(start_connect=lambda *args: calls.append(args))
    session._action = lambda action, callback: action()
    session._begin_connect()
    assert calls[0][1] == 'b'


def test_network_preferences_read_current_app_state():
    session = LiveSession.__new__(LiveSession)
    session.panel = SimpleNamespace(app=SimpleNamespace(game_dir='game', state={
        'matchmaking_region': 'EU', 'matchmaking_cross_region': True}))
    assert session._network_config() == {'game_dir': 'game', 'region': 'EU', 'cross_region': True}


def test_network_payload_uses_average_for_host_and_peer_rtt_for_roster():
    session = LiveSession.__new__(LiveSession)
    session.players = [{'steam_id':'a'}, {'steam_id':'b'}]
    session._apply_network({'network': {'host':'b', 'average':45.5, 'pings':{'a':91,'b':0}}})
    assert session.host['steam_id'] == 'b'
    assert session.host['ping'] == 46
    assert session.host['ping_estimated'] is True
    assert session.players[0]['ping'] == 91
    assert session.players[1]['ping'] == 0


def test_stale_queued_event_cannot_resurrect_cancelled_search():
    session = LiveSession.__new__(LiveSession)
    session.phase = 'idle'
    session._cancel_queue_pending = True
    session._changed = lambda: None
    session.on_live_event({'type':'queued','position':1,'size':1})
    assert session.phase == 'idle'
    session.on_live_event({'type':'unqueued'})
    assert session._cancel_queue_pending is False  # a later leader's deliberate search is allowed


def test_join_generation_captured_before_action_thread_runs():
    session = LiveSession.__new__(LiveSession)
    session._queue_epoch = 0
    session.phase = 'checking'
    calls = []
    session.client = SimpleNamespace(prepare_queue_join=lambda: calls.append('captured') or (lambda: None))
    session._action = lambda action, callback: calls.append('scheduled')
    session._join_attempt(0)
    assert calls == ['captured','scheduled']


def _delayed_queue_actions(phase):
    from hub.live import LiveClient
    session = LiveSession.__new__(LiveSession)
    session._queue_epoch = 0
    session.phase = phase
    session.client = LiveClient('test-token')
    session._changed = lambda: None
    session.reset_match = lambda: None
    posts, scheduled = [], []
    session.client._post = lambda path, body=None: (posts.append(path) or (200, {'ok': True}))
    session._action = lambda action, callback=None: scheduled.append(action)
    return session, posts, scheduled


def test_cancel_after_reconnect_cannot_join_after_leave():
    session, posts, scheduled = _delayed_queue_actions('queued')
    session.on_live_event({'type': 'hello'})
    session.cancel_queue()
    scheduled[1]()  # leave wins scheduling; reconnect join has not started yet
    scheduled[0]()
    assert posts == ['/api/queue/leave']
    session.on_live_event({'type': 'queued', 'position': 1, 'size': 1})
    assert session.phase == 'idle'


def test_watchdog_cleanup_invalidates_delayed_join_and_stale_queue_event():
    session, posts, scheduled = _delayed_queue_actions('checking')
    session._join_attempt(0)
    session.leave_everything()
    session.phase = 'idle'  # watchdog resets its UI after requesting cleanup
    scheduled[1]()
    scheduled[0]()
    assert posts == ['/api/queue/leave']
    session.on_live_event({'type': 'queued', 'position': 1, 'size': 1})
    assert session.phase == 'idle'
