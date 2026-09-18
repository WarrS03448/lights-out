"""Settings account linking verifies both owners without adopting the temporary email identity."""
from types import SimpleNamespace
from hub.account_link import AccountLink
from hub.auth import AuthError

STEAM = '76561198000000001'
EMAIL_TOKEN = 'lo_' + 'e' * 43
PROOF = 's' * 43


def harness():
    pending, calls, revoked, opened = [], [], [], []
    session = SimpleNamespace(me={'steam_id': STEAM, 'player_id': STEAM,
        'game_steam_id': STEAM, 'name': 'Steam Player'}, token='original-steam',
        parent_token='original-steam', phase='idle', _account_epoch=1,
        locked_in=lambda: False, _changed=lambda: None)
    session.panel = SimpleNamespace(post=lambda fn: fn(), _closed=False)
    def signout():
        calls.append(('signout',)); session.me = None; session.token = ''; session.phase = 'signed_out'
    session.sign_out = signout
    session.finish_account_link = signout
    def request(action, payload, token=None):
        calls.append((action, payload, token))
        return {
            'login': {'challenge': 'login-challenge'},
            'login/verify': {'token': EMAIL_TOKEN, 'account': {'id': 'email-id', 'email': 'fixture@example.test', 'player_id': 'email-id'}},
            'link-steam': {'challenge': 'link-challenge', 'steam_id': STEAM},
            'link-steam/verify': {'status': 'sign_in_required', 'account': {'player_id': STEAM}},
        }[action]
    flow=AccountLink(session, request=request, start=lambda: {'code':'browser-code','url':'https://example.test/steam'},
        wait=lambda code, should_stop: {'token':PROOF,'steam_id':STEAM,'game_steam_id':STEAM},
        open_browser=opened.append, spawn=pending.append, revoke=revoked.append)
    session.account_link=flow
    def run():
        while pending: pending.pop(0)()
    return session,flow,calls,revoked,opened,run,pending


def advance_to_confirm(flow, run):
    flow.action('start')
    flow.action('login', {'email':'fixture@example.test','password':'secret-first'})
    run()
    flow.action('login_code', {'code':'123456'})
    run()
    flow.action('steam')
    run()
    flow.action('password', {'password':'secret-second'})
    run()


def test_success_preserves_current_identity_until_atomic_confirmation_and_then_signs_out():
    s,f,calls,revoked,opened,run,_=harness()
    advance_to_confirm(f,run)
    assert s.token=='original-steam' and s.me['steam_id']==STEAM
    assert f.view()['stage']=='confirm'
    assert calls[0]==('login',{'email':'fixture@example.test','password':'secret-first','remember_me':False},None)
    assert calls[-1]==('link-steam',{'password':'secret-second','steam_token':PROOF},EMAIL_TOKEN)
    serialized=repr(f.view())
    for private in [EMAIL_TOKEN,PROOF,'secret-first','secret-second','123456','login-challenge','link-challenge']:
        assert private not in serialized
    f.action('confirm',{'code':'654321'});run()
    assert calls[-2]==('link-steam/verify',{'challenge':'link-challenge','code':'654321','steam_token':PROOF},EMAIL_TOKEN)
    assert calls[-1]==('signout',)
    assert f.view()['stage']=='done' and s.me is None
    assert EMAIL_TOKEN in revoked and PROOF in revoked
    assert 'original-steam' not in revoked


def test_cancel_reclaims_late_email_session_without_touching_steam_session():
    s,f,calls,revoked,opened,run,pending=harness()
    f.action('start');f.action('login',{'email':'fixture@example.test','password':'secret'});run()
    f.action('login_code',{'code':'123456'})
    f.action('cancel');run()
    assert EMAIL_TOKEN in revoked and s.token=='original-steam'
    assert f.view()['stage']==''


def test_wrong_steam_proof_never_sends_a_link_challenge():
    s,f,calls,revoked,opened,run,_=harness()
    f.wait=lambda code, should_stop: {'token':PROOF,'steam_id':'76561198000000002'}
    f.action('start');f.action('login',{'email':'fixture@example.test','password':'secret'});run()
    f.action('login_code',{'code':'123456'});run();f.action('steam');run()
    assert f.view()['error']=='wrong_steam'
    assert f.view()['stage']=='steam'
    assert PROOF in revoked
    assert not any(c[0]=='link-steam' for c in calls)


def test_busy_is_single_flight_and_account_change_invalidates_flow():
    s,f,calls,revoked,opened,run,_=harness()
    f.action('start');f.action('login',{'email':'fixture@example.test','password':'secret'})
    f.action('login',{'email':'other@example.test','password':'other'});run()
    assert len(calls)==1
    f.action('login_code',{'code':'123456'});s.token='another-account';run()
    assert EMAIL_TOKEN in revoked and f.view()['stage']==''
    assert s.token=='another-account'


def test_conflicting_progress_is_shown_without_merging_or_signing_out():
    s,f,calls,revoked,opened,run,_=harness();advance_to_confirm(f,run)
    def reject(*args,**kw): raise AuthError('safe',code='progress_conflict',status=409)
    f.request=reject;f.action('confirm',{'code':'654321'});run()
    assert f.view()['error']=='progress_conflict'
    assert s.token=='original-steam' and f.view()['stage']=='confirm'


def test_linking_cannot_start_during_match_or_from_email_signin():
    s,f,calls,revoked,opened,run,_=harness()
    s.phase='live';f.action('start');assert f.view()['stage']==''
    s.phase='idle';s.parent_token=EMAIL_TOKEN;f.action('start');assert f.view()['stage']==''


def test_new_game_activity_cancels_pending_link_and_reclaims_credentials():
    s,f,calls,revoked,opened,run,_=harness();advance_to_confirm(f,run)
    s.phase='found';assert f.view()['stage']=='';run()
    assert set(revoked)=={EMAIL_TOKEN,PROOF}
    assert s.token=='original-steam'


def test_lost_final_response_detaches_old_authority_and_reports_uncertain_outcome():
    s,f,calls,revoked,opened,run,_=harness();advance_to_confirm(f,run)
    def timeout(*args,**kw): raise AuthError("Account verification failed.")
    f.request=timeout;f.action('confirm',{'code':'654321'});run()
    assert f.view()['stage']=='uncertain'
    assert s.me is None and s.token==''
    assert EMAIL_TOKEN in revoked and PROOF in revoked


def test_confirmed_link_does_not_depend_on_normal_signout_accepting_it():
    s,f,calls,revoked,opened,run,_=harness();advance_to_confirm(f,run)
    s.sign_out=lambda: None
    f.action('confirm',{'code':'654321'});run()
    assert s.me is None and s.token==''
    assert f.view()['stage']=='done'


def test_live_link_completion_clears_memory_even_when_state_storage_fails(monkeypatch):
    from tests.test_hub import _web_panel
    from hub import auth
    panel, session = _web_panel()
    session.parent_token = 'original-steam'
    panel.app.state['auth'] = {'token': 'original-steam'}
    def disk_error(*args, **kwargs):
        raise OSError('fixture disk unavailable')
    panel.save_auth = disk_error
    monkeypatch.setattr(auth, 'queue_revoke', disk_error)
    monkeypatch.setattr(auth, 'revoke_session', lambda *args, **kwargs: None)
    session.finish_account_link()
    assert session.token == session.parent_token == ''
    assert session.me is None and session.phase == 'signed_out'
    assert panel.app.state['auth'] is None
