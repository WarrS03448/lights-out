"""Account changes invalidate private caches and asynchronous work."""
import time
import pytest
from types import SimpleNamespace

from hub import competitive as C
from test_screen_matchflow import _panel

def test_lights_out_adoption_waits_for_proof_and_saves_only_parent(monkeypatch):
    from hub import game_identity
    panel, s = _panel()
    workers, saved = [], []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(s, '_disconnect', lambda: None)
    monkeypatch.setattr(s, '_connect', lambda: None)
    monkeypatch.setattr(s, '_game_dir', lambda: 'game')
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(panel, 'save_auth', saved.append)
    player='a1111111-1111-4111-8111-111111111111'
    parent='lo_'+'a'*43
    child='lg_'+'b'*43
    monkeypatch.setattr(game_identity, 'mint_session', lambda *a,**kw: {
        'token':child,'player_id':player,'game_steam_id':'76561198000000001','expires_in':43200})
    s.adopt_account({'token':parent,'account':{'player_id':player,'display_name':'Player'},'remember_me':True},save=True)
    assert not s.token
    workers.pop()()
    assert s.token==child and s.parent_token==parent
    assert s.me['player_id']==player and s.me['steam_id']==player
    assert s.me['game_steam_id']=='76561198000000001'
    assert saved[-1]['token']==parent

def test_uuid_player_is_never_stamped_as_native_host_id():
    from hub.player_identity import normalize, native_id
    p='a1111111-1111-4111-8111-111111111111'
    row=normalize({'player_id':p,'game_steam_id':'76561198000000001'})
    assert row['steam_id']==p
    assert native_id(row)=='76561198000000001'
    assert native_id({'player_id':p,'steam_id':p})==''


def test_switching_to_steam_ignores_pending_email_login(monkeypatch):
    panel, s = _panel()
    workers = []
    s.me = None; s.phase = 'signed_out'
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(C.auth_mod, 'account_request', lambda *args: {'challenge':'old-challenge'})
    s.account_action('login', {'email':'fixture@example.test','password':'fixture-only'})
    email_worker = workers.pop()
    s.sign_in()
    email_worker()
    assert s.phase == 'signing_in' and not s.account_busy
    assert not s.account_step and not s._login_challenge


def test_cancelled_game_proof_cannot_sign_in(monkeypatch):
    from hub import game_identity
    panel, s = _panel()
    workers = []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(s, '_disconnect', lambda: None)
    monkeypatch.setattr(s, '_game_dir', lambda: 'game')
    player='a1111111-1111-4111-8111-111111111111'
    monkeypatch.setattr(game_identity, 'mint_session', lambda *a,**kw: {
        'token':'lg_'+'b'*43,'player_id':player,'game_steam_id':'76561198000000001'})
    s.adopt_account({'token':'lo_'+'a'*43,'player_id':player})
    proof_worker = workers.pop()
    s.cancel_sign_in()
    proof_worker()
    assert s.me is None and not s.token and not s.parent_token
    assert s.phase == 'signed_out'


@pytest.mark.parametrize('restore_first',[True,False])
def test_explicit_email_login_supersedes_saved_restore(monkeypatch,restore_first):
    panel,s=_panel();workers=[];delivered=[];adopted=[]
    s.me=None;s.phase='signed_out'
    monkeypatch.setattr(C.threading,'Thread',lambda target,**kw:SimpleNamespace(start=lambda:workers.append(target)))
    monkeypatch.setattr(panel,'post',delivered.append)
    monkeypatch.setattr(s,'adopt_account',adopted.append)
    monkeypatch.setattr(C.auth_mod,'me',lambda token:{'steam_id':'76561198000000001'})
    monkeypatch.setattr(C.auth_mod,'account_request',lambda *args:{'challenge':'new-login'})
    s.restore_account({'token':'old-saved-token'})
    restore_worker=workers.pop()
    s.account_action('login',{'email':'fixture@example.test','password':'fixture-password'})
    login_worker=workers.pop()
    for worker in ([restore_worker,login_worker] if restore_first else [login_worker,restore_worker]):
        worker()
        while delivered:delivered.pop(0)()
    assert not adopted and s.account_step=='login_code'


def test_game_proof_cancel_revokes_parent_exactly_once(monkeypatch):
    panel,s=_panel();workers=[];revoked=[];saved=[]
    monkeypatch.setattr(C.threading,'Thread',lambda target,**kw:SimpleNamespace(start=lambda:workers.append(target)))
    monkeypatch.setattr(C.auth_mod,'revoke_session',lambda token, **kw: revoked.append(token))
    monkeypatch.setattr(panel,'save_auth',saved.append)
    s.me=None;s.phase='signing_in';s.token='';s.parent_token='lo_'+'a'*43
    s.cancel_sign_in();s.cancel_sign_in()
    for worker in workers:worker()
    assert revoked==['lo_'+'a'*43]
    assert saved[-1] is None and not s.parent_token


def test_signout_waits_for_detached_game_cleanup(monkeypatch):
    panel,s=_panel();saved=[]
    s.phase='result';s.parent_token='lo_'+'a'*43;s.token='lg_'+'b'*43
    monkeypatch.setattr(C.match_cleanup,'pending_for',lambda player:True,raising=False)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: True)
    monkeypatch.setattr(panel,'save_auth',saved.append)
    s.sign_out()
    assert s.me and s.token and not saved


@pytest.mark.parametrize('failure', ['cleanup_read', 'revocation_write'])
def test_signout_storage_failure_leaves_current_session_available(monkeypatch, failure):
    panel, s = _panel()
    s.phase = 'idle'; s.parent_token = 'lo_' + 'a' * 43; s.token = 'lg_' + 'b' * 43
    original = s.me
    def cleanup(_):
        if failure == 'cleanup_read':
            raise PermissionError('fixture')
        return False
    monkeypatch.setattr(C.match_cleanup, 'pending_for', cleanup)
    def deny(_): raise PermissionError('fixture')
    monkeypatch.setattr(C.auth_mod, 'queue_revoke', deny)
    s.sign_out()
    assert s.me is original and s.token and s.parent_token and s.phase == 'idle'
    assert s.error and s.error != 'account_storage_failed'


@pytest.mark.parametrize('restore', [False, True])
def test_failed_proof_preserves_remembered_account_and_can_retry(monkeypatch, restore):
    from hub import game_identity
    panel, s = _panel(); workers = []; saved = []; revoked = []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(panel, 'save_auth', saved.append)
    monkeypatch.setattr(C.auth_mod, 'revoke_session', lambda token, **kw: revoked.append(token))
    monkeypatch.setattr(s, '_disconnect', lambda: None)
    monkeypatch.setattr(s, '_game_dir', lambda: 'game')
    monkeypatch.setattr(s, '_connect', lambda: None)
    def fail(*args, **kwargs):
        raise game_identity.GameIdentityError()
    monkeypatch.setattr(game_identity, 'mint_session', fail)
    parent = 'lo_' + 'a' * 43
    player = 'a1111111-1111-4111-8111-111111111111'
    account = {'token': parent, 'player_id': player, 'remember_me': True}
    if restore:
        saved.append(account)
        monkeypatch.setattr(C.auth_mod, 'me', lambda _: account)
        s.restore_account(account)
    else:
        s.adopt_account(account, save=True)
    while workers: workers.pop(0)()
    assert revoked == [] and saved[-1]['token'] == parent
    assert s.me is None and not s.token and s.parent_token == parent and s.phase == 'game_unavailable'
    monkeypatch.setattr(game_identity, 'mint_session', lambda *a, **kw: {
        'token': 'lg_' + 'b' * 43, 'player_id': player, 'game_steam_id': '76561198000000001'})
    s.account_action('game/retry')
    while workers: workers.pop(0)()
    assert s.phase == 'idle' and s.token.startswith('lg_') and s.parent_token == parent


@pytest.mark.parametrize('cancel', [False, True])
def test_revocation_is_durable_before_background_worker_runs(monkeypatch, cancel):
    from hub import credentials, paths
    import json
    panel, s = _panel()
    parent = 'lo_' + 'a' * 43
    s.parent_token = parent; s.token = 'lg_' + 'b' * 43
    s.phase = 'signing_in' if cancel else 'idle'
    monkeypatch.setattr(C.threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(panel, 'save_auth', lambda _: False)
    if cancel: s.cancel_sign_in()
    else: s.sign_out()
    assert not s.token and not s.parent_token and s.me is None
    records = list((paths.state_dir() / 'pending-signouts').glob('*.json'))
    assert len(records) == 1
    assert credentials.open_sealed(json.loads(records[0].read_text(encoding='utf-8')))['token'] == parent
    monkeypatch.setattr(C.auth_mod, '_request', lambda *a, **kw: pytest.fail('Restored a pending logout'))
    assert C.auth_mod.me(parent) is None


def test_stale_cleanup_does_not_trap_signout_after_manual_game_close(monkeypatch):
    panel, s = _panel(); s.phase = 'result'; s.parent_token = 'lo_' + 'a' * 43
    monkeypatch.setattr(C.threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(C.match_cleanup, 'pending_for', lambda _: True)
    monkeypatch.setattr(C.game_mod, 'game_running', lambda: False)
    s.sign_out()
    assert s.me is None and not s.token and not s.parent_token


def test_cancel_retains_parent_when_revocation_cannot_be_saved(monkeypatch):
    panel, s = _panel()
    parent = 'lo_' + 'a' * 43
    s.parent_token = parent; s.phase = 'signing_in'
    monkeypatch.setattr(C.threading, 'Thread', lambda **kw: SimpleNamespace(start=lambda: None))
    monkeypatch.setattr(panel, 'save_auth', lambda _: False)
    def deny(_): raise PermissionError('fixture')
    monkeypatch.setattr(C.auth_mod, 'queue_revoke', deny)
    s.cancel_sign_in()
    assert s.parent_token == parent and s.phase == 'game_unavailable'
    assert s.error and s.error != 'account_storage_failed'


def test_signout_writes_revocation_once(monkeypatch):
    panel, s = _panel(); s.phase = 'idle'; s.parent_token = 'lo_' + 'a' * 43
    calls = []; workers = []; revoked = []
    def queue(token):
        calls.append(token)
        assert len(calls) == 1
        return 'durable-record'
    monkeypatch.setattr(C.auth_mod, 'queue_revoke', queue)
    monkeypatch.setattr(C.auth_mod, 'revoke_session', lambda token, **kw: revoked.append((token, kw['pending'])))
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    s.sign_out()
    while workers: workers.pop(0)()
    assert s.me is None and not s.token and not s.parent_token and s.phase == 'signed_out'
    assert len(calls) == 1 and revoked == [(calls[0], 'durable-record')]


def test_cleanup_probe_failure_cannot_revoke_session(monkeypatch):
    panel, s = _panel(); s.phase = 'result'; s.parent_token = 'lo_' + 'a' * 43
    monkeypatch.setattr(C.match_cleanup, 'pending_for', lambda _: True)
    def fail(): raise OSError('fixture')
    monkeypatch.setattr(C.game_mod, 'game_running', fail)
    monkeypatch.setattr(C.auth_mod, 'queue_revoke', lambda _: pytest.fail('Revoked with unknown game state'))
    s.sign_out()
    assert s.me and s.parent_token and s.phase == 'result'


def test_stale_email_code_response_revokes_issued_session(monkeypatch):
    panel, s = _panel(); workers = []; revoked = []
    s.me = None; s.phase = 'signed_out'
    parent = 'lo_' + 'a' * 43
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'post', lambda fn: fn())
    monkeypatch.setattr(C.auth_mod, 'account_request', lambda *args: {'token': parent})
    monkeypatch.setattr(C.auth_mod, 'revoke_session', lambda token, **kw: revoked.append(token))
    s.account_action('login/verify', {'code': '123456'})
    s.cancel_sign_in()
    while workers: workers.pop(0)()
    assert revoked == [parent] and s.me is None and s.phase == 'signed_out'


def test_failed_remember_does_not_connect_or_keep_credentials(monkeypatch):
    panel, s = _panel(); workers = []; revoked = []
    monkeypatch.setattr(C.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=lambda: workers.append(target)))
    monkeypatch.setattr(panel, 'save_auth', lambda _: False)
    monkeypatch.setattr(C.auth_mod, 'revoke_session', lambda token, **kw: revoked.append(token))
    monkeypatch.setattr(s, '_disconnect', lambda: None)
    monkeypatch.setattr(s, '_connect', lambda: pytest.fail('Connected before saving the remembered login'))
    parent = 'lo_' + 'a' * 43
    assert s.adopt_account({'token': 'lg_' + 'b' * 43, 'parent_token': parent,
        'player_id': 'a1111111-1111-4111-8111-111111111111', 'game_steam_id': '76561198000000001'}, save=True) is False
    while workers: workers.pop(0)()
    assert revoked == [parent] and s.me is None and not s.token and not s.parent_token
    assert s.phase == 'signed_out' and s.error != 'account_storage_failed'


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
