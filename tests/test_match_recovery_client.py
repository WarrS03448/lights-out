"""Recovery must not confuse failed process discovery with a closed game."""
from types import SimpleNamespace
import importlib

import pytest

try:
    recovery = importlib.import_module("hub.match_recovery")
except ModuleNotFoundError:
    recovery = None


@pytest.mark.parametrize("returncode,stdout,want", [
    (0, '"System","4","Services","0","1,024 K"\n', "closed"),
    (0, '"Bodycam-Win64-Shipping.exe","1234","Console","1","1,024 K"\n', "running"),
    (1, "", "unknown"), (0, "", "unknown"), (0, "Access denied", "unknown"),
])
def test_process_observation_fails_closed(monkeypatch, returncode, stdout, want):
    assert recovery is not None, "recovery client is not implemented"
    monkeypatch.setattr(recovery, "WINDOWS", True)
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout))
    assert recovery.game_observation() == want


def test_recovery_response_cannot_update_another_match_or_account():
    assert recovery is not None, "recovery client is not implemented"
    session = SimpleNamespace(match_id="0123456789abcdef", host_epoch=1, session_key="chm-0123456789abcdef-r1111111111111111",
                              _account_epoch=7)
    scope = recovery.scope(session)
    assert recovery.current(session, scope)
    for attr, value in [("match_id", "fedcba9876543210"), ("host_epoch", 2), ("_account_epoch", 8), ("session_key", "other")]:
        before = getattr(session, attr)
        setattr(session, attr, value)
        assert not recovery.current(session, scope)
        setattr(session, attr, before)


def test_rejoin_requires_recovery_world_readiness_even_if_legacy_host_flag_is_true():
    s=SimpleNamespace(recovery={"phase":"restoring","world_ready":False},host_ready=True,_i_am_host=lambda:False)
    assert not recovery.launch_allowed(s)
    s.recovery["world_ready"]=True
    assert recovery.launch_allowed(s)
    s.recovery["world_ready"]=False;s._i_am_host=lambda:True
    assert recovery.launch_allowed(s)


def test_async_closed_game_check_cannot_launch_after_account_switch(monkeypatch):
    pending=[];launched=[]
    s=SimpleNamespace(match_id="0123456789abcdef",host_epoch=1,session_key="chm-0123456789abcdef-r1111111111111111",
        _account_epoch=7,recovery={"phase":"restoring","world_ready":True},_i_am_host=lambda:False,
        _action=lambda work,reply:pending.append((work,reply)),_relaunch_closed_game=lambda:launched.append(True),_changed=lambda:None)
    monkeypatch.setattr(recovery,"game_observation",lambda:"closed")
    recovery.relaunch(s)
    work,reply=pending.pop();status,body=work();s._account_epoch+=1;reply(status,body)
    assert launched==[]


def test_same_generation_status_cannot_regress_world_readiness_or_completed_restore():
    from hub.competitive import LiveSession
    s=object.__new__(LiveSession)
    s.match_id="0123456789abcdef";s.session_key="chm-"+s.match_id+"-r1111111111111111";s.host_epoch=1
    s.recovery={"phase":"playing","world_ready":True};s.host={"steam_id":"host"};s.players=[]
    s._i_am_host=lambda:False;s.host_ready=True
    LiveSession._update_match_authority(s,{"host":"host","host_epoch":1,"session_key":s.session_key,
        "recovery":{"phase":"restoring","world_ready":False}})
    assert s.recovery["phase"]=="playing" and s.host_ready
    s.recovery={"phase":"restoring","world_ready":True}
    LiveSession._update_match_authority(s,{"host":"host","host_epoch":1,"session_key":s.session_key,
        "recovery":{"phase":"restoring","world_ready":False}})
    assert s.recovery["world_ready"] and s.host_ready


def test_completed_restore_cannot_be_replayed_by_cold_host_relaunch():
    s=SimpleNamespace(recovery={"phase":"playing","world_ready":True},_i_am_host=lambda:True)
    assert not recovery.launch_allowed(s)
    s._i_am_host=lambda:False
    assert recovery.launch_allowed(s), "participants can still rejoin the live successor"


def test_completed_restore_does_not_seed_another_world(monkeypatch):
    from hub.competitive import LiveSession, lobbypak_mod
    calls=[]
    monkeypatch.setattr(lobbypak_mod,'_retarget_module',lambda:SimpleNamespace(
        recovery_config=SimpleNamespace(encode=lambda *args:calls.append(args))))
    s=object.__new__(LiveSession);s.host_epoch=1;s.match_id='0123456789abcdef'
    s.recovery={'phase':'playing','checkpoint':{'round':3},'hash':'b'*64}
    s._report_configuration('a'*64)
    assert calls[-1][3] is None
    s.recovery['phase']='restoring';s._report_configuration('a'*64)
    assert calls[-1][3]=={'round':3}


def test_return_cutoff_blocks_launch_until_admission_is_confirmed(monkeypatch):
    monkeypatch.setattr(recovery.time,'time',lambda:1000)
    s=SimpleNamespace(recovery={'phase':'restoring','world_ready':True,'rejoin_until':1000000,
        'can_rejoin':True},_i_am_host=lambda:False)
    assert not recovery.launch_allowed(s), 'the local countdown cannot offer a late launch'
    s.recovery['roster']={'admitted':['me'],'excluded':[],'done':True}
    assert recovery.launch_allowed(s), 'a sealed admitted player can still reconnect'
    s.recovery['can_rejoin']=False
    assert not recovery.launch_allowed(s), 'a sealed excluded player cannot relaunch'


def test_older_recovery_health_cannot_replace_sealed_roster_status(monkeypatch):
    pending=[]
    s=SimpleNamespace(phase='live',client=object(),match_id='0123456789abcdef',host_epoch=1,
        session_key='chm-0123456789abcdef-r1111111111111111',_account_epoch=1,
        recovery={'phase':'restoring','revision':2},recovery_health={'revision':2,'connected':['me']},
        _action=lambda work,reply:pending.append(reply),_changed=lambda:None,_i_am_host=lambda:False)
    recovery.poll(s)
    s.recovery={'phase':'restoring','revision':4,'roster':{'done':True}}
    s.recovery_health={'revision':4,'connected':['me','peer']}
    pending.pop()(200,{'recovery':{'revision':2,'connected':[]},'observation':'closed'})
    assert s.recovery_health=={'revision':4,'connected':['me','peer']}
    assert not s._recovery_busy


def test_stale_live_replay_cannot_restore_reconnect_clock():
    from hub.competitive import LiveSession
    s=object.__new__(LiveSession)
    s.match_id='0123456789abcdef';s.host_epoch=1;s.session_key='chm-'+s.match_id+'-r1111111111111111'
    s.recovery={'phase':'restoring','revision':4};s.reconnect_waiting=[]
    s.on_live_event({'type':'match_live','match_id':s.match_id,'host_epoch':1,'session_key':s.session_key,
        'recovery':{'phase':'restoring','revision':2},'reconnect_waiting':[{'player_id':'me','deadline':42}]})
    assert s.reconnect_waiting==[]


def test_running_stranded_game_gets_conditional_guidance_but_cannot_claim():
    s=SimpleNamespace(recovery={},recovery_health={'session_stale':True,'checkpoint_round':3,
        'observation':'running','can_claim':False},_i_am_host=lambda:False)
    status=recovery.public_status(s)
    assert status['visible'] and status['state']=='close_game'
    assert not status['can_claim']
    s.recovery_health['session_stale']=False
    assert not recovery.public_status(s)['visible']


def test_completed_recovery_keeps_participant_reconnect_control():
    s=SimpleNamespace(recovery={'phase':'playing','can_rejoin':True},
        recovery_health={'checkpoint_round':3,'observation':'closed'},_i_am_host=lambda:False)
    assert recovery.public_status(s)['can_launch']
    s._i_am_host=lambda:True
    assert not recovery.public_status(s)['can_launch']


def test_return_deadline_uses_service_time_and_monotonic_progress(monkeypatch):
    clock=[10.0]
    monkeypatch.setattr(recovery.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(recovery.time,'time',lambda:9999999999)
    s=SimpleNamespace(match_id='0123456789abcdef',host_epoch=1,session_key='chm-test',_account_epoch=1,
        recovery={'phase':'restoring','world_ready':True,'rejoin_until':400000},_i_am_host=lambda:False)
    recovery.note_clock(s,100000)
    assert recovery.launch_allowed(s)
    clock[0]+=299
    recovery.note_clock(s,100000)  # A delayed reply cannot extend the window.
    assert recovery.server_now(s)==399000 and recovery.launch_allowed(s)
    clock[0]+=1
    assert not recovery.launch_allowed(s)


def test_background_health_check_does_not_swallow_host_claim(monkeypatch):
    pending=[]
    s=SimpleNamespace(phase='live',client=object(),match_id='0123456789abcdef',host_epoch=1,
        session_key='chm-test',_account_epoch=1,recovery={},recovery_health={},
        _action=lambda work,reply:pending.append((work,reply)),_changed=lambda:None,_i_am_host=lambda:False)
    recovery.poll(s)
    assert not recovery.public_status(s)['busy']
    recovery.poll(s,claim=True)
    assert len(pending)==2 and recovery.public_status(s)['busy']


def test_same_live_recovery_replay_refreshes_roster_without_resetting_chat():
    from tests.test_hub import _web_panel
    p,s=_web_panel();s.phase='live';s.match_id='0123456789abcdef';s.host_epoch=1;s.session_key='chm-'+s.match_id
    s.players=[s._player_from({'steam_id':str(76561198000000001+i),'persona':'Player '+str(i)}) for i in range(4)]
    ids=[row['steam_id'] for row in s.players]
    assert len(ids)>2
    s.recovery={'phase':'restoring','revision':2}
    s.chat_team=[{'text':'keep this'}]
    event={'host':ids[0],'host_epoch':1,'session_key':s.session_key,
        'recovery':{'phase':'restoring','revision':3},'players':[
            {'player_id':row['steam_id'],'name':row['name']} for row in s.players[:-1]],
        'teams':{'1':ids[::2],'2':ids[1::2]},'sides':{'1':'attack','2':'defend'}}
    old_name=s.players[0]['name']
    s._update_match_authority(event)
    assert [p['steam_id'] for p in s.players]==ids[:-1]
    assert s.players[0]['name']==old_name
    assert all(ids[-1] not in [p['steam_id'] for p in team] for team in s.teams.values())
    assert s.chat_team==[{'text':'keep this'}]


def test_recovery_forfeit_is_explained_in_result_card_and_history():
    from tests.test_hub import _web_panel
    from hub.webui.screens.competitive import result_snapshot
    from hub.webui.screens.postmatch import _card
    from hub.webui.screens.history import _detail
    p,s=_web_panel();s.match_id='0123456789abcdef';s.phase='live'
    terminal={'recovery':True,'reason':'reconnect_timeout'}
    s._on_result({'type':'match_result','match_id':s.match_id,'won':True,'score':[2,1],'terminal':terminal})
    assert result_snapshot(s)['recovery_forfeit']
    assert _card(s._postmatch_record())['recovery_forfeit']
    assert _detail({'id':s.match_id,'terminal':terminal},'')['recovery_forfeit']


def test_recovery_cancellation_and_exclusion_do_not_claim_no_penalty():
    from tests.test_hub import _web_panel
    from hub.i18n import t
    p,s=_web_panel();s.match_id='0123456789abcdef';s.phase='live'
    s._on_cancelled({'match_id':s.match_id,'reason':'recovery_expired'})
    assert s.error==t('comp_recovery_failed')
    s.phase='live'
    s.on_live_event({'type':'match_over','match_id':s.match_id,'recovery_excluded':True})
    assert s.error==t('comp_recovery_excluded')


def test_old_completion_cannot_interrupt_new_accept_or_reopen_dismissed_result():
    from tests.test_hub import _web_panel
    p,s=_web_panel();old='0123456789abcdef';new='123456789abcdef0'
    s.match_id=old;s.phase='result';s.leave_result()
    s.on_live_event({'type':'match_result','match_id':old,'won':True})
    assert s.phase=='idle'
    s.on_live_event({'type':'match_found','match_id':new,'players':[]})
    assert s.phase=='found' and s.match_id==new
    s.on_live_event({'type':'match_over','match_id':old})
    assert s.phase=='found'


def test_delayed_normal_roster_snapshot_cannot_resurrect_a_departed_member():
    s=SimpleNamespace(match_id='0'*16,session_key='chm-'+'0'*16,host_epoch=1,
        recovery={'phase':'playing','revision':6},roster_revision=2)
    assert recovery.stale_event(s,{'match_id':s.match_id,'session_key':s.session_key,'host_epoch':1,
        'recovery':dict(s.recovery),'roster_revision':1})

@pytest.mark.parametrize("phase,host_ready", [("live",False),("live",True),("connecting",True)])
def test_cold_host_cannot_relaunch_without_new_authority(phase,host_ready):
    from hub.competitive import LiveSession
    launched=[]
    s=object.__new__(LiveSession)
    s.phase=phase;s.host_ready=host_ready;s.recovery={};s.client=None;s.match_id="0123456789abcdef"
    s._i_am_host=lambda:True;s._changed=lambda:None
    s._relaunch_closed_game=lambda:launched.append(True)
    LiveSession.relaunch_game(s)
    assert launched==[]

def test_first_host_arrival_can_still_retry_launch():
    from hub.competitive import LiveSession
    launched=[]
    s=object.__new__(LiveSession)
    s.phase="connecting";s.host_ready=False;s.recovery={};s._i_am_host=lambda:True
    s._relaunch_closed_game=lambda:launched.append(True)
    LiveSession.relaunch_game(s)
    assert launched==[True]
