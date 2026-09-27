"""A launch the hub skips tells the service why.  Run: python -m pytest tests/test_launch_skip_telemetry.py

Every early return between the connect window and game_mod.launch_game used to be silent to the
service: when a host's game never opened (2026-09-27) the only trace was the player's own log. Each
skip now sends launch.outcome/skipped with a reason code. Scratch state and stubbed game operations
only: nothing launches, nothing touches a game folder, nothing is sent over the network.
"""
import json
import re
import pytest
from test_screen_matchflow import _panel, P2  # sets HUB_STATE_DIR before the hub is imported
from hub import competitive as C, lobbypak, telemetry

HOST = '76561198000000042'
MATCH = 'a' * 16


@pytest.fixture
def sent(monkeypatch):
    """Every telemetry event the hub emits, plus a guard that Steam is never really asked."""
    rows = []
    monkeypatch.setattr(telemetry, 'emit', lambda kind, **fields: rows.append((kind, fields)) or True)
    monkeypatch.setattr(C.game_mod, 'launch_game', lambda: pytest.fail('the real launch_game was reached'))
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: False)
    monkeypatch.setattr(C.MockSession, '_take_game_ownership', lambda self: None)
    return rows


def skips(rows):
    return [fields for kind, fields in rows if kind == 'launch.outcome' and fields.get('status') == 'skipped']


def launches(monkeypatch):
    calls = []
    monkeypatch.setattr(C.game_mod, 'launch_game', lambda: calls.append(True) or True)
    return calls


def stub_prepare(monkeypatch, level='', why=None, raises=None):
    calls = []
    def prepare(*args, **kwargs):
        calls.append(kwargs.get('role', 'host'))
        if raises:
            raise raises
        if not level and kwargs.get('why') is not None:
            kwargs['why'].update(why or {})
        return level
    monkeypatch.setattr(C.lobbypak_mod, 'prepare', prepare)
    return calls


def session(host=True, match_id=MATCH, game='scratch-game', map_name='Rome'):
    panel, s = _panel()
    panel.app.game_dir = game
    s.phase = 'connecting'
    s.match_id = match_id
    s.map = map_name
    s.host = dict(s.me) if host else {'steam_id': HOST}
    return s


def connecting(s, **extra):
    """The service's match_connecting for this session's match, naming the session's host."""
    mine = s.me['steam_id']
    host = s.host['steam_id']
    event = {'match_id': s.match_id, 'host': host, 'map': s.map, 'connect_seconds': 120,
             'players': [{'steam_id': mine}, {'steam_id': HOST if host == mine else host}]}
    event.update(extra)
    s._on_connecting(event)


# ---------------------------------------------------------------- lobbypak.prepare says why
@pytest.fixture
def pak(monkeypatch, tmp_path):
    """A scratch game dir and the pak steps stubbed; `removed` proves the stale-pak rule still holds."""
    removed = []
    monkeypatch.setattr(lobbypak.game_mod, 'game_running', lambda: False)
    monkeypatch.setattr(lobbypak, 'remove', lambda game: removed.append(game) or True)
    monkeypatch.setattr(lobbypak, '_complain', lambda message, log=None: None)
    return str(tmp_path), removed


def test_prepare_fills_nothing_on_success(monkeypatch, pak):
    game, removed = pak
    monkeypatch.setattr(lobbypak, 'resolve', lambda *a: ('/Game/Maps/BB5_Rome', 'BB5_Rome'))
    monkeypatch.setattr(lobbypak, 'variant', lambda *a, **k: 'cut.pak')
    monkeypatch.setattr(lobbypak, 'install', lambda *a: 'installed')
    why = {}
    assert lobbypak.prepare(game, 'BB5', 'Rome', why=why) == 'BB5_Rome'
    assert why == {} and removed == []
    assert lobbypak.prepare(game, 'BB5', 'Rome') == 'BB5_Rome', 'why stays optional'


def test_prepare_reports_a_running_game(monkeypatch, pak):
    game, removed = pak
    monkeypatch.setattr(lobbypak.game_mod, 'game_running', lambda: True)
    why = {}
    assert lobbypak.prepare(game, 'BB5', 'Rome', why=why) == ''
    assert why == {'reason': 'game_running'} and removed == [game]


@pytest.mark.parametrize('pak_file,levels,reason', [
    (False, {}, 'no_gamemode_pak'),
    (True, {}, 'no_levels'),
    (True, {'BB5_Hospital': '/Game/Maps/BB5_Hospital'}, 'no_level'),
])
def test_prepare_says_which_level_lookup_failed(monkeypatch, pak, pak_file, levels, reason):
    game, removed = pak
    if pak_file:
        with open(lobbypak.os.path.join(lobbypak.game_mod.mods_dir(game), lobbypak.version.PAK_NAME), 'wb'):
            pass
    monkeypatch.setattr(lobbypak, 'available_levels', lambda g: dict(levels))
    why = {}
    assert lobbypak.prepare(game, 'BB5', 'Rome', why=why) == ''
    assert why == {'reason': reason} and removed == [game]


@pytest.mark.parametrize('step,error', [('variant', FileNotFoundError), ('install', PermissionError)])
def test_prepare_names_the_failing_step_and_class_but_never_the_message(monkeypatch, pak, step, error):
    game, removed = pak
    secret = r'C:\Users\someone\Steam\Bodycam report-token-0123 76561198000000042'
    def fail(*a, **k):
        raise error(secret)
    monkeypatch.setattr(lobbypak, 'resolve', lambda *a: ('/Game/Maps/BB5_Rome', 'BB5_Rome'))
    monkeypatch.setattr(lobbypak, 'variant', fail if step == 'variant' else (lambda *a, **k: 'cut.pak'))
    monkeypatch.setattr(lobbypak, 'install', fail if step == 'install' else (lambda *a: 'installed'))
    why = {}
    assert lobbypak.prepare(game, 'BB5', 'Rome', why=why) == ''
    assert why == {'reason': 'pak_failed', 'code': step, 'error_class': error.__name__}
    assert removed == [game]


# ---------------------------------------------------------------- the session reports each skip
@pytest.mark.parametrize('why', [
    {'reason': 'game_running'},
    {'reason': 'no_gamemode_pak'},
    {'reason': 'no_level'},
    {'reason': 'pak_failed', 'code': 'install', 'error_class': 'PermissionError'},
])
def test_a_host_pak_that_cannot_be_prepared_reports_why_and_does_not_launch(monkeypatch, sent, why):
    opened = launches(monkeypatch)
    stub_prepare(monkeypatch, why=why)
    s = session(host=True)
    connecting(s)
    assert not opened and s.error
    assert skips(sent) == [{'severity': 'warn', 'action': 'host', 'status': 'skipped', 'phase': 'pak', **why}]


@pytest.mark.parametrize('host,game,map_name,match_id,reason', [
    (True, '', 'Rome', MATCH, 'no_game_dir'),
    (True, 'scratch-game', '', MATCH, 'no_map'),
    (False, '', 'Rome', MATCH, 'no_game_dir'),
    (False, 'scratch-game', '', MATCH, 'no_map'),
    (False, 'scratch-game', 'Rome', '', 'no_match_id'),   # only the joiner's pak needs the token
])
def test_missing_inputs_are_named(monkeypatch, sent, host, game, map_name, match_id, reason):
    prepared = stub_prepare(monkeypatch, level='BB5_Rome')
    s = session(host=host, game=game, map_name=map_name, match_id=match_id)
    assert s._prepare_launch() is False
    assert not prepared
    assert [(f['action'], f['phase'], f['reason']) for f in skips(sent)] == [
        ('host' if host else 'join', 'pak', reason)]


@pytest.mark.parametrize('host', [True, False])
def test_an_exception_while_preparing_sends_only_its_class(monkeypatch, sent, host):
    stub_prepare(monkeypatch, raises=TypeError("'NoneType' object is not subscriptable at C:/Users/x"))
    s = session(host=host)
    assert s._prepare_launch() is False
    assert skips(sent) == [{'severity': 'warn', 'action': 'host' if host else 'join', 'status': 'skipped',
                            'phase': 'pak', 'reason': 'prepare_error', 'error_class': 'TypeError'}]


def test_a_host_pak_that_raises_mid_connect_is_reported_and_the_window_still_opens(monkeypatch, sent):
    """The whole match_connecting handler, not just _prepare_launch. Before the session had its own
    _log_exception, the except arm in _prepare_host_pak raised AttributeError out of it: no remove(),
    no error line, no countdown, and no report."""
    panel, s = _panel()                       # phase idle: this event opens the connect window
    panel.app.game_dir = 'scratch-game'
    s.match_id, s.map, s.host = MATCH, 'Rome', dict(s.me)
    stub_prepare(monkeypatch, raises=TypeError("'NoneType' object is not subscriptable at C:/Users/x"))
    removed = []
    monkeypatch.setattr(C.lobbypak_mod, 'remove', lambda game: removed.append(game) or True)
    opened = launches(monkeypatch)
    log = C.paths.log_file()
    before = log.read_text(encoding='utf-8') if log.exists() else ''
    connecting(s)
    assert not opened and removed == ['scratch-game'] and not s.host_level
    assert s.phase == 'connecting' and s.error == C.t('comp_launch_prepare_failed')
    assert s._tick_connect in panel.scheduler.pending
    assert skips(sent) == [{'severity': 'warn', 'action': 'host', 'status': 'skipped',
                            'phase': 'pak', 'reason': 'prepare_error', 'error_class': 'TypeError'}]
    assert '[competitive/prepare_host_pak]' in log.read_text(encoding='utf-8')[len(before):]


def test_the_joiner_button_says_why_it_did_nothing(monkeypatch, sent):
    opened = launches(monkeypatch)
    prepared = stub_prepare(monkeypatch, level='BB5_Rome')
    s = session(host=False)
    s.host_ready = False
    s.launch_game()
    s.phase = 'live'
    s.launch_game()
    assert not prepared and not opened
    assert [(f['action'], f['phase'], f['reason']) for f in skips(sent)] == [
        ('join', 'button', 'not_host_ready'), ('join', 'button', 'not_connecting')]


def test_a_game_already_running_at_launch_is_reported(monkeypatch, sent):
    opened = launches(monkeypatch)
    s = session(host=True)
    s.host_pak_done = True        # the pak went in before the player opened the game themselves
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: True)
    connecting(s)
    assert not opened and s.game_was_open
    assert [(f['action'], f['phase'], f['reason']) for f in skips(sent)] == [('host', 'launch', 'game_running')]


def test_a_launch_that_raises_sends_its_class(monkeypatch, sent):
    stub_prepare(monkeypatch, level='BB5_Rome')
    def boom():
        raise OSError(r'C:\Program Files (x86)\Steam\steam.exe')
    monkeypatch.setattr(C.game_mod, 'launch_game', boom)
    s = session(host=True)
    connecting(s)
    assert skips(sent) == [{'severity': 'warn', 'action': 'host', 'status': 'skipped', 'phase': 'launch',
                            'reason': 'launch_error', 'error_class': 'OSError'}]


def test_a_guard_set_without_our_own_launch_is_reported(monkeypatch, sent):
    """_recover_running_game keys the once-per-match guard without launching anything."""
    opened = launches(monkeypatch)
    stub_prepare(monkeypatch, level='BB5_Rome')
    s = session(host=True)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: True)
    s._recover_running_game()
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: False)
    connecting(s)
    assert not opened
    assert skips(sent) == [{'severity': 'info', 'action': 'host', 'status': 'skipped', 'phase': 'launch',
                            'reason': 'already_launched_for_match'}]


def test_relaunch_into_a_running_game_is_reported(monkeypatch, sent):
    opened = launches(monkeypatch)
    s = session(host=False)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: True)
    s._relaunch_closed_game()
    assert not opened
    assert [(f['action'], f['phase'], f['reason']) for f in skips(sent)] == [('join', 'relaunch', 'game_running')]


def test_replays_report_a_skip_once_per_match(monkeypatch, sent):
    launches(monkeypatch)
    stub_prepare(monkeypatch, why={'reason': 'no_level'})
    s = session(host=True)
    for _ in range(4):
        connecting(s)
    assert len(skips(sent)) == 1
    s.match_id = 'b' * 16
    connecting(s)
    assert len(skips(sent)) == 2, 'the next match reports its own skip'


def test_a_connect_window_dropped_for_the_wrong_mode_is_reported(monkeypatch, sent):
    launches(monkeypatch)
    s = session(host=True)
    s.phase = 'lobby'
    s.ranked_mode = 'BB5'
    s.on_live_event({'type': 'match_connecting', 'mode': 'BB1', 'match_id': MATCH, 'host': s.me['steam_id']})
    assert s.phase == 'lobby'
    assert [(f['phase'], f['reason']) for f in skips(sent)] == [('event', 'mode_mismatch')]


def test_being_named_host_but_acting_as_joiner_is_reported(monkeypatch, sent):
    opened = launches(monkeypatch)
    prepared = stub_prepare(monkeypatch, level='BB5_Rome')
    s = session(host=False)
    s.players = [dict(P2)]
    s.host_epoch = 2              # a stale-epoch replay leaves the old host in place
    s._on_connecting({'match_id': MATCH, 'host': s.me['steam_id'], 'host_epoch': 1, 'map': 'Rome'})
    assert not s._i_am_host() and not prepared and not opened
    assert [(f['action'], f['phase'], f['reason']) for f in skips(sent)] == [('join', 'event', 'host_mismatch')]


# ---------------------------------------------------------------- a normal launch adds nothing
def test_a_normal_host_launch_and_its_replays_report_no_skip(monkeypatch, sent):
    opened = launches(monkeypatch)
    prepared = stub_prepare(monkeypatch, level='BB5_Rome')
    s = session(host=True)
    connecting(s)
    connecting(s, resumed=True)   # a stream that flapped mid-window
    assert opened == [True] and prepared == ['host']
    assert sent == []


def test_a_normal_joiner_launch_reports_no_skip(monkeypatch, sent):
    opened = launches(monkeypatch)
    prepared = stub_prepare(monkeypatch, level='BB5_Rome')
    s = session(host=False)
    connecting(s, stamped=True, connected=[HOST])
    assert s.host_ready and not opened, 'the joiner waits for the button'
    s.launch_game()
    assert opened == [True] and prepared == ['join']
    assert sent == []


def test_the_preview_session_never_reports(monkeypatch, sent):
    launches(monkeypatch)
    stub_prepare(monkeypatch, why={'reason': 'no_level'})
    s = session(host=True)
    s.live = False
    s._prepare_host_pak()
    assert sent == []


# ---------------------------------------------------------------- what actually goes on the wire
REASONS = ('game_running no_gamemode_pak no_levels no_level pak_failed prepare_error no_game_dir no_map '
           'no_match_id not_connecting already_launched_for_match already_launched launch_error '
           'not_host_ready is_host mode_mismatch host_mismatch unknown').split()


@pytest.mark.parametrize('reason', REASONS)
def test_every_reason_survives_both_privacy_filters(reason):
    """The hub drops any token matching bearer/token/password/secret, and the service keeps only
    lowercase codes from a client (server/analytics.cjs). A reason either filter drops arrives blank."""
    assert telemetry._safe_data({'reason': reason}) == {'reason': reason}
    assert re.fullmatch(r'[a-z][a-z0-9_.-]{0,95}', reason)
    assert not re.search(r'bearer|token|password|secret|sk[-_]|eyJ', reason, re.I)


def test_the_outbox_row_carries_codes_only(monkeypatch, tmp_path):
    """Through the real telemetry client: no match id, path, token or SteamID reaches the outbox."""
    client = telemetry._Telemetry(tmp_path / 'telemetry.json', background=False)
    client.start()
    monkeypatch.setattr(telemetry, 'emit', client.emit)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: False)
    opened = launches(monkeypatch)
    stub_prepare(monkeypatch, why={'reason': 'pak_failed', 'code': 'variant', 'error_class': 'FileNotFoundError'})
    s = session(host=True)
    s.report_token = 'f' * 64
    connecting(s, report_token='f' * 64)
    text = (tmp_path / 'telemetry.json').read_text(encoding='utf-8')
    row = [r for r in json.loads(text)['events'] if r['type'] == 'launch.outcome'][0]
    assert not opened and row['severity'] == 'warn'
    assert row['data'] == {'action': 'host', 'status': 'skipped', 'phase': 'pak', 'reason': 'pak_failed',
                           'code': 'variant', 'error_class': 'FileNotFoundError'}
    for private in (MATCH, 'f' * 64, s.me['steam_id'], 'scratch-game', 'Rome'):
        assert private not in text
