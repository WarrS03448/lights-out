"""Cold recovery graphs. Run through make_blueprints.py; no native phase writes."""
from ctf_graphs import G, P, SYS, MATH, STR, ARR, GS_LIB, ACTOR
import bb5_graphs as B
import recovery_graphs as R

REQUEST=B.BB5_DIR+'/BP_BB5RestoreRequest.BP_BB5RestoreRequest_C'
COUNTDOWN='/Script/Bodycam.CountdownComponent'


def part(g, name, index, integer=False, parts='init_parts.Parts'):
    g.call(name, ARR, 'Array_Get', {'Index': str(index)} if isinstance(index,int) else None, array=True)
    g.link((parts, name+'.TargetArray'))
    if not isinstance(index,int):g.link((index,name+'.Index'))
    value=name+'.Item'
    if integer:
        g.call(name+'_int', STR, 'Conv_StringToInt');g.link((value,name+'_int.InString'))
        value=name+'_int.ReturnValue'
    return value


def initialise(g, after):
    """Private seed must belong to this match and may never rewind a native epoch."""
    g.set('init_off','Stage',defaults={'Stage':'-1'});g.link((after,'init_off.exec'))
    local=R.gi(g,'init_')
    g.get('init_config','Search String',B.GI_CLASS);g.link((local,'init_config.self'))
    g.call('init_parse',STR,'ParseIntoArray',{'Delimiter':'-','CullEmptyStrings':'false'})
    g.link(('init_config.Search String','init_parse.SourceString'))
    g.set('init_save','Parts');g.link(('init_parse.ReturnValue','init_save.Parts'));g.chain('init_off','init_save')
    g.get('init_parts','Parts');g.call('init_count',ARR,'Array_Length',array=True);g.link(('init_parts.Parts','init_count.TargetArray'))
    g.call('init_enough',MATH,'GreaterEqual_IntInt',{'B':'6'});g.link(('init_count.ReturnValue','init_enough.A'))
    g.branch('init_shape');g.link(('init_enough.ReturnValue','init_shape.condition'));g.chain('init_save','init_shape')
    version=part(g,'init_version',1);match=part(g,'init_match',2);epoch=part(g,'init_epoch',3,True)
    g.call('init_version_ok',STR,'EqualEqual_StrStr',{'B':'r1'});g.link((version,'init_version_ok.A'))
    g.call('init_key',STR,'Concat_StrStr',{'A':'chm-'});g.link((match,'init_key.B'))
    key,_=B._start_key(g,'init_session_')
    g.call('init_match_ok',STR,'EqualEqual_StrStr');g.link((key,'init_match_ok.A'),('init_key.ReturnValue','init_match_ok.B'))
    valid=R.conjunction(g,'init_valid',['init_version_ok.ReturnValue','init_match_ok.ReturnValue'])
    g.branch('init_bound');g.link((valid,'init_bound.condition'));g.chain('init_shape','init_bound')
    g.get('init_rule','Rule');g.get('init_current','HostEpoch',B.RULE);g.link(('init_rule.Rule','init_current.self'))
    g.call('init_max',MATH,'Max');g.link(('init_current.HostEpoch','init_max.A'),(epoch,'init_max.B'))
    g.set('init_set_epoch','HostEpoch',B.RULE);g.link(('init_rule.Rule','init_set_epoch.self'),('init_max.ReturnValue','init_set_epoch.HostEpoch'));g.chain('init_bound','init_set_epoch')
    # A no-checkpoint envelope still carries the current epoch to rejoining clients.
    g.call('init_has_rows',MATH,'GreaterEqual_IntInt',{'B':'22'});g.link(('init_count.ReturnValue','init_has_rows.A'))
    g.branch('init_checkpoint');g.link(('init_has_rows.ReturnValue','init_checkpoint.condition'));g.chain('init_set_epoch','init_checkpoint')
    digest=part(g,'init_hash',4);count=part(g,'init_rows',10,True)
    g.call('init_hash_len',STR,'Len');g.link((digest,'init_hash_len.S'))
    g.call('init_hash_ok',MATH,'EqualEqual_IntInt',{'B':'64'});g.link(('init_hash_len.ReturnValue','init_hash_ok.A'))
    g.call('init_row_size',MATH,'Multiply_IntInt',{'B':'5'});g.link((count,'init_row_size.A'))
    g.call('init_size',MATH,'Add_IntInt',{'B':'12'});g.link(('init_row_size.ReturnValue','init_size.A'))
    g.call('init_size_ok',MATH,'EqualEqual_IntInt');g.link(('init_size.ReturnValue','init_size_ok.A'),('init_count.ReturnValue','init_size_ok.B'))
    g.call('init_same',MATH,'EqualEqual_IntInt');g.link(('init_current.HostEpoch','init_same.A'),(epoch,'init_same.B'))
    g.get('init_applied','RecoveryApplied',B.RULE);g.link(('init_rule.Rule','init_applied.self'))
    g.call('init_unapplied',MATH,'Not_PreBool');g.link(('init_applied.RecoveryApplied','init_unapplied.A'))
    ready=R.conjunction(g,'init_ready',['init_hash_ok.ReturnValue','init_size_ok.ReturnValue','init_same.ReturnValue','init_unapplied.ReturnValue'])
    g.branch('init_restore');g.link((ready,'init_restore.condition'));g.chain('init_checkpoint','init_restore')
    g.set('init_stage','Stage',defaults={'Stage':'0'});g.chain('init_restore','init_stage')
    g.set('init_seed_epoch','SeedEpoch');g.link((epoch,'init_seed_epoch.SeedEpoch'));g.chain('init_stage','init_seed_epoch')
    g.set('init_pending','RecoveryPending',B.RULE,defaults={'RecoveryPending':'true'});g.link(('init_rule.Rule','init_pending.self'));g.chain('init_seed_epoch','init_pending')
    full,_=B._start_key(g,'init_full_',full=True)
    g.set('init_seed_session','SeedSession');g.link((full,'init_seed_session.SeedSession'));g.chain('init_pending','init_seed_session')
    g.call('init_clock',GS_LIB,'GetRealTimeSeconds');g.call('init_seconds',MATH,'FTrunc');g.link(('init_clock.ReturnValue','init_seconds.A'))
    # Fallback for a companion/service outage: outlast 3m setup + 5m return +
    # 3m verification, then tear down the unverified session without a result.
    g.call('init_expiry',MATH,'Add_IntInt',{'B':'720'});g.link(('init_seconds.ReturnValue','init_expiry.A'))
    g.set('init_deadline','RestoreDeadline');g.link(('init_expiry.ReturnValue','init_deadline.RestoreDeadline'));g.chain('init_seed_session','init_deadline')
    g.seq('init_done',1)
    for source in ('init_shape.else','init_bound.else','init_checkpoint.else','init_restore.else','init_deadline.then'):
        g.link((source,'init_done.exec'))
    return 'init_done.then_0'


def apply_snapshot():
    """Apply only the saved fields; never overwrite the engine's phase or live actors."""
    g=G();g.entry();g.result();g.get('parts','Parts');g.get('rule','Rule')
    g.call('gs',GS_LIB,'GetGameState');g.cast('state',B.BC_GS,pure=True);g.link(('gs.ReturnValue','state.cast_object'))
    values={i:part(g,'part'+str(i),i,True,'parts.Parts') for i in (5,7,8,9)}
    previous='entry'
    for prop,value in (('CurrentRound',values[5]),('ObjectiveTeam',values[9])):
        g.set(prop,prop,B.BC_GS);g.link(('state.cast_result',prop+'.self'),(value,prop+'.'+prop));g.chain(previous,prop);previous=prop
    g.call('other',MATH,'Subtract_IntInt',{'A':'1'});g.link((values[9],'other.B'))
    g.set('counter','CounterObjectiveTeam',B.BC_GS);g.link(('state.cast_result','counter.self'),('other.ReturnValue','counter.CounterObjectiveTeam'));g.chain(previous,'counter')
    g.get('teams','Teams',B.BC_GS);g.link(('state.cast_result','teams.self'))
    g.foreach('team');g.link(('teams.Teams','team.Array'));g.chain('counter','team')
    g.brk('td',B.V_TEAMDATA);g.link(('team.Array Element','td.in'))
    for i in (0,1):
        g.call('is'+str(i),MATH,'EqualEqual_IntInt',{'B':str(i)});g.link(('td.TeamID','is'+str(i)+'.A'))
    g.call('binary',MATH,'BooleanOR');g.link(('is0.ReturnValue','binary.A'),('is1.ReturnValue','binary.B'))
    g.branch('team_ok');g.link(('binary.ReturnValue','team_ok.condition'),('team.LoopBody','team_ok.exec'))
    g.call('score',MATH,'SelectInt');g.link(('is0.ReturnValue','score.bPickA'),(values[7],'score.A'),(values[8],'score.B'))
    g.n('data','make',struct=B.V_TEAMDATA);g.link(('td.TeamID','data.TeamID'),('score.ReturnValue','data.TeamScore'),('td.PlayerCount','data.PlayerCount'))
    g.call('put',ARR,'Array_Set',{'bSizeToFit':'false'},array=True)
    g.link(('teams.Teams','put.TargetArray'),('team.Array Index','put.Index'),('data.out','put.Item'));g.chain('team_ok','put')
    g.foreach('row');g.link(('parts.Parts','row.Array'),('team.Completed','row.exec'))
    g.call('offset',MATH,'Subtract_IntInt',{'B':'11'});g.link(('row.Array Index','offset.A'))
    g.call('positive',MATH,'GreaterEqual_IntInt',{'B':'0'});g.link(('offset.ReturnValue','positive.A'))
    g.call('mod',MATH,'Percent_IntInt',{'B':'5'});g.link(('offset.ReturnValue','mod.A'))
    g.call('first',MATH,'EqualEqual_IntInt',{'B':'0'});g.link(('mod.ReturnValue','first.A'))
    g.call('length',ARR,'Array_Length',array=True);g.link(('parts.Parts','length.TargetArray'))
    g.call('last',MATH,'Subtract_IntInt',{'B':'1'});g.link(('length.ReturnValue','last.A'))
    g.call('inside',MATH,'Less_IntInt');g.link(('row.Array Index','inside.A'),('last.ReturnValue','inside.B'))
    ready=R.conjunction(g,'row_guard',['positive.ReturnValue','first.ReturnValue','inside.ReturnValue'])
    g.branch('row_ok');g.link((ready,'row_ok.condition'),('row.LoopBody','row_ok.exec'))
    g.get('players','PlayerArray',B.GAMESTATE);g.link(('gs.ReturnValue','players.self'))
    g.foreach('player');g.link(('players.PlayerArray','player.Array'));g.chain('row_ok','player')
    g.call('id',B.ONLINE,'RetrievePlatformIdAsStringFromPlayerState');g.link(('player.Array Element','id.PlayerState'))
    g.call('same',STR,'EqualEqual_StrStr');g.link(('id.ReturnValue','same.A'),('row.Array Element','same.B'))
    g.branch('same_player');g.link(('same.ReturnValue','same_player.condition'),('player.LoopBody','same_player.exec'))
    g.cast('ps',B.BC_PS,pure=True);g.link(('player.Array Element','ps.cast_object'))
    previous='same_player'
    for i,prop in enumerate(('TeamID','Kill','Death','SpawnCount'),1):
        g.call(prop+'_index',MATH,'Add_IntInt',{'B':str(i)});g.link(('row.Array Index',prop+'_index.A'))
        value=part(g,prop+'_value',prop+'_index.ReturnValue',True,'parts.Parts')
        if prop=='Kill':
            g.call('kills_signed',MATH,'Subtract_IntInt',{'B':'1000'});g.link((value,'kills_signed.A'));value='kills_signed.ReturnValue'
        g.set(prop,prop,B.BC_PS);g.link(('ps.cast_result',prop+'.self'),(value,prop+'.'+prop));g.chain(previous,prop);previous=prop
    g.call('recount',B.RULE,'RecountTeams');g.link(('rule.Rule','recount.self'),('row.Completed','recount.exec'));g.chain('recount','result')
    return g.json()


def warmup(g,p):
    g.call(p+'gs',GS_LIB,'GetGameState');g.cast(p+'state',B.BC_GS,pure=True);g.link((p+'gs.ReturnValue',p+'state.cast_object'))
    g.call(p+'phase',B.BC_GS,'GetCurrentPhase');g.link((p+'state.cast_result',p+'phase.self'))
    g.call(p+'tag','/Script/GameplayTags.BlueprintGameplayTagLibrary','GetTagName');g.link((p+'phase.ReturnValue',p+'tag.GameplayTag'))
    g.call(p+'text',STR,'Conv_NameToString');g.link((p+'tag.ReturnValue',p+'text.InName'))
    g.call(p+'warm',STR,'EqualEqual_StrStr',{'B':'Game.Phase.RoundWarmup'});g.link((p+'text.ReturnValue',p+'warm.A'))
    return p+'warm.ReturnValue'


def countdown(g,p,state,seconds):
    g.get(p+'timer','WarmupCountdown',B.BC_GS);g.link((state,p+'timer.self'))
    g.call(p+'hold',COUNTDOWN,'StartCountdown',{'duration':str(seconds)});g.link((p+'timer.WarmupCountdown',p+'hold.self'))
    return p+'hold'


def timeout_guard(g,after):
    g.seq('timeout_actions',2);g.link((after,'timeout_actions.exec'))
    for name in ('Stage','SeedEpoch','SeedSession','RestoreDeadline'):g.get('timeout_'+name,name)
    g.call('timeout_authority',ACTOR,'HasAuthority')
    key,_=B._start_key(g,'timeout_key_',full=True)
    g.call('timeout_session',STR,'EqualEqual_StrStr');g.link((key,'timeout_session.A'),('timeout_SeedSession.SeedSession','timeout_session.B'))
    g.call('timeout_epoch',MATH,'EqualEqual_IntInt');g.link(('epoch.HostEpoch','timeout_epoch.A'),('timeout_SeedEpoch.SeedEpoch','timeout_epoch.B'))
    g.call('timeout_active',MATH,'GreaterEqual_IntInt',{'B':'0'});g.link(('timeout_Stage.Stage','timeout_active.A'))
    g.call('timeout_pending',MATH,'Less_IntInt',{'B':'3'});g.link(('timeout_Stage.Stage','timeout_pending.A'))
    g.call('timeout_clock',GS_LIB,'GetRealTimeSeconds');g.call('timeout_seconds',MATH,'FTrunc');g.link(('timeout_clock.ReturnValue','timeout_seconds.A'))
    g.call('timeout_due',MATH,'GreaterEqual_IntInt');g.link(('timeout_seconds.ReturnValue','timeout_due.A'),('timeout_RestoreDeadline.RestoreDeadline','timeout_due.B'))
    g.call('timeout_armed',MATH,'GreaterEqual_IntInt',{'B':'1'});g.link(('timeout_RestoreDeadline.RestoreDeadline','timeout_armed.A'))
    allowed=R.conjunction(g,'timeout_valid',[f'timeout_{n}.ReturnValue' for n in ('authority','session','epoch','active','pending','due','armed')])
    g.branch('timeout_guard');g.link((allowed,'timeout_guard.condition'),('timeout_actions.then_0','timeout_guard.exec'))
    g.set('timeout_latch','Stage',defaults={'Stage':'-2'});g.chain('timeout_guard','timeout_latch')
    g.call('timeout_gm',GS_LIB,'GetGameMode');g.cast('timeout_mode',B.GM_BB5,pure=True);g.link(('timeout_gm.ReturnValue','timeout_mode.cast_object'))
    g.call('timeout_exit',B.GM_BB5,'MatchExit');g.link(('timeout_mode.cast_result','timeout_exit.self'));g.chain('timeout_latch','timeout_exit')
    return 'timeout_actions.then_1'


def tick(g,after):
    after=timeout_guard(g,after)
    g.get('restore_stage','Stage');g.get('restore_seed','SeedEpoch')
    g.call('restore_current',MATH,'EqualEqual_IntInt');g.link(('restore_seed.SeedEpoch','restore_current.A'),('epoch.HostEpoch','restore_current.B'))
    g.call('restore_active',MATH,'GreaterEqual_IntInt',{'B':'0'});g.link(('restore_stage.Stage','restore_active.A'))
    g.call('restore_pending',MATH,'Less_IntInt',{'B':'3'});g.link(('restore_stage.Stage','restore_pending.A'))
    ready=R.conjunction(g,'restore_allowed',['restore_current.ReturnValue','restore_active.ReturnValue','restore_pending.ReturnValue'])
    g.branch('restore_guard');g.link((ready,'restore_guard.condition'),(after,'restore_guard.exec'))
    is_warm=warmup(g,'restore_')
    # Keep the native countdown held while asynchronous teardown runs. Never
    # release a round just because recovery has timed out.
    g.call('timeout_terminal',MATH,'EqualEqual_IntInt',{'B':'-2'});g.link(('restore_stage.Stage','timeout_terminal.A'))
    terminal=R.conjunction(g,'timeout_hold_valid',['timeout_terminal.ReturnValue','timeout_epoch.ReturnValue','timeout_session.ReturnValue',is_warm])
    g.branch('timeout_hold_guard');g.link((terminal,'timeout_hold_guard.condition'),('restore_guard.else','timeout_hold_guard.exec'))
    terminal_hold=countdown(g,'timeout_hold_','restore_state.cast_result',300);g.chain('timeout_hold_guard',terminal_hold)
    g.seq('restore_actions',2);g.chain('restore_guard','restore_actions')
    g.branch('restore_warm_guard');g.link((is_warm,'restore_warm_guard.condition'),('restore_actions.then_0','restore_warm_guard.exec'))
    hold=countdown(g,'restore_','restore_state.cast_result',300);g.chain('restore_warm_guard',hold)
    # Delegate and poll fallback both run after the native warmup initialization.
    g.call('restore_prepared',MATH,'EqualEqual_IntInt',{'B':'1'});g.link(('restore_stage.Stage','restore_prepared.A'))
    g.branch('restore_enter');g.link(('restore_prepared.ReturnValue','restore_enter.condition'));g.chain(hold,'restore_enter')
    g.set('restore_stage2','Stage',defaults={'Stage':'2'});g.chain('restore_enter','restore_stage2')
    for i in (0,2):
        g.call('restore_is'+str(i),MATH,'EqualEqual_IntInt',{'B':str(i)});g.link(('restore_stage.Stage','restore_is'+str(i)+'.A'))
    second=R.conjunction(g,'restore_second',['restore_is2.ReturnValue',is_warm])
    g.call('restore_apply_now',MATH,'BooleanOR');g.link(('restore_is0.ReturnValue','restore_apply_now.A'),(second,'restore_apply_now.B'))
    g.branch('restore_apply');g.link(('restore_apply_now.ReturnValue','restore_apply.condition'),('restore_actions.then_1','restore_apply.exec'))
    g.call('restore_values',None,'ApplySnapshot');g.chain('restore_apply','restore_values')
    g.call('restore_capture',None,'BuildSnapshot');g.chain('restore_values','restore_capture')
    g.call('restore_transform',MATH,'MakeTransform');g.spawn('restore_request',REQUEST)
    g.link(('restore_transform.ReturnValue','restore_request.SpawnTransform'));g.chain('restore_capture','restore_request')
    g.selfnode('restore_self');g.call('restore_begin',REQUEST,'BeginRestore')
    g.link(('restore_request.ReturnValue','restore_begin.self'),('restore_self.self','restore_begin.Manager'),('restore_stage.Stage','restore_begin.Stage'))
    g.chain('restore_request','restore_begin')


def warmup_event():
    g=G();g.existing('warmup','RecoveryWarmup')
    g.get('stage','Stage');g.call('pending',MATH,'Less_IntInt',{'B':'3'});g.link(('stage.Stage','pending.A'))
    g.call('active',MATH,'GreaterEqual_IntInt',{'B':'0'});g.link(('stage.Stage','active.A'))
    g.get('rule','Rule');g.get('seed','SeedEpoch');g.get('epoch','HostEpoch',B.RULE);g.link(('rule.Rule','epoch.self'))
    g.call('same_epoch',MATH,'EqualEqual_IntInt');g.link(('seed.SeedEpoch','same_epoch.A'),('epoch.HostEpoch','same_epoch.B'))
    g.get('session','Session');key,_=B._start_key(g,'warm_key_',full=True)
    g.call('same_session',STR,'EqualEqual_StrStr');g.link((key,'same_session.A'),('session.Session','same_session.B'))
    g.call('authority',ACTOR,'HasAuthority')
    value=R.conjunction(g,'valid',['pending.ReturnValue','active.ReturnValue','same_epoch.ReturnValue','same_session.ReturnValue','authority.ReturnValue'])
    g.branch('guard');g.link((value,'guard.condition'));g.chain('warmup','guard')
    g.call('gs',GS_LIB,'GetGameState');g.cast('state',B.BC_GS,pure=True);g.link(('gs.ReturnValue','state.cast_object'))
    hold=countdown(g,'warm_','state.cast_result',300);g.chain('guard',hold)
    return g.json()


def bind(g,after):
    g.call('bind_gs',GS_LIB,'GetGameState');g.cast('bind_state',B.BC_GS,pure=True);g.link(('bind_gs.ReturnValue','bind_state.cast_object'))
    g.n('bind_warm','adddelegate',delegate='OnRoundWarmup',**{'class':B.BC_GS})
    g.n('bind_callback','createevent',func='RecoveryWarmup')
    g.link(('bind_state.cast_result','bind_warm.self'),('bind_callback.OutputDelegate','bind_warm.Delegate'),(after,'bind_warm.exec'))
    return 'bind_warm.then'


def request_events():
    g=G();g.custom('begin','BeginRestore',[P('Manager','object',**{'class':R.ACTOR_CLASS}),P('Stage','int')])
    g.custom('reply','OnRestoreReply',[P('bSuccess','bool')]);return g.json()


def request_logic():
    g=G();g.existing('begin','BeginRestore');g.get('manager','Manager')
    previous='begin'
    for name in ('Manager','Stage'):
        g.set('capture_'+name,name);g.link(('begin.'+name,'capture_'+name+'.'+name));g.chain(previous,'capture_'+name);previous='capture_'+name
    for name in ('Meta','Rows','Epoch','Session'):
        g.get('source_'+name,name,R.ACTOR_CLASS);g.link(('manager.Manager','source_'+name+'.self'))
        g.set('capture_'+name,name);g.link(('source_'+name+'.'+name,'capture_'+name+'.'+name));g.chain(previous,'capture_'+name);previous='capture_'+name
    g.call('clock',SYS,'GetGameTimeInSeconds');g.call('clock_int',MATH,'FTrunc');g.link(('clock.ReturnValue','clock_int.A'))
    g.call('expires',MATH,'Add_IntInt',{'B':'3'});g.link(('clock_int.ReturnValue','expires.A'))
    g.set('deadline_set','Deadline');g.link(('expires.ReturnValue','deadline_set.Deadline'));g.chain(previous,'deadline_set')
    g.call('lifespan',ACTOR,'SetLifeSpan',{'InLifespan':'4.0'});g.chain('deadline_set','lifespan')
    for name in ('Stage','Meta','Rows','Epoch','Session','Deadline'):g.get(name,name)
    g.call('first',MATH,'EqualEqual_IntInt',{'B':'0'});g.link(('Stage.Stage','first.A'))
    g.call('event',MATH,'SelectString',{'A':'ch_recovery_prepared','B':'ch_recovery_restored'});g.link(('first.ReturnValue','event.bPickA'))
    B._report_send(g,'send',{'URL':R.URL,'FirstSessionTimestamp':'chrecovery-1','IsFirstGameOpen':'false'})
    g.call('host',B.ONLINE,'GetPlatformUserNetId')
    for a,b in (('event.ReturnValue','EventName'),('Meta.Meta','Timestamp'),('Rows.Rows','Platform'),('Session.Session','Storefront'),('host.ReturnValue','UserId')):g.link((a,'send.'+b))
    g.n('callback','createevent',func='OnRestoreReply');g.link(('callback.OutputDelegate','send.OnResponse'));g.chain('lifespan','send')
    g.existing('reply','OnRestoreReply')
    g.get('rule','Rule',R.ACTOR_CLASS);g.link(('manager.Manager','rule.self'))
    g.get('current_epoch','HostEpoch',B.RULE);g.link(('rule.Rule','current_epoch.self'))
    g.get('current_stage','Stage',R.ACTOR_CLASS);g.link(('manager.Manager','current_stage.self'))
    for prop,a,b in (('epoch','current_epoch.HostEpoch','Epoch.Epoch'),('stage','current_stage.Stage','Stage.Stage')):
        g.call('same_'+prop,MATH,'EqualEqual_IntInt');g.link((a,'same_'+prop+'.A'),(b,'same_'+prop+'.B'))
    key,_=B._start_key(g,'current_',full=True)
    g.call('same_session',STR,'EqualEqual_StrStr');g.link((key,'same_session.A'),('Session.Session','same_session.B'))
    g.call('fresh',MATH,'Less_IntInt');g.link(('clock_int.ReturnValue','fresh.A'),('Deadline.Deadline','fresh.B'))
    g.call('valid',SYS,'IsValid');g.link(('manager.Manager','valid.Object'))
    g.get('consumed','Consumed');g.call('new_reply',MATH,'Not_PreBool');g.link(('consumed.Consumed','new_reply.A'))
    warm=warmup(g,'reply_')
    g.call('phase_valid',MATH,'BooleanOR');g.link(('first.ReturnValue','phase_valid.A'),(warm,'phase_valid.B'))
    okay=R.conjunction(g,'guard',['reply.bSuccess','valid.ReturnValue','same_epoch.ReturnValue','same_stage.ReturnValue','same_session.ReturnValue','fresh.ReturnValue','new_reply.ReturnValue','phase_valid.ReturnValue'])
    g.branch('accept');g.link((okay,'accept.condition'));g.chain('reply','accept')
    g.call('readback',R.ACTOR_CLASS,'BuildSnapshot');g.link(('manager.Manager','readback.self'));g.chain('accept','readback')
    for prop in ('Meta','Rows'):
        g.call('same_'+prop,STR,'EqualEqual_StrStr');g.link(('source_'+prop+'.'+prop,'same_'+prop+'.A'),(prop+'.'+prop,'same_'+prop+'.B'))
    unchanged=R.conjunction(g,'unchanged',['same_Meta.ReturnValue','same_Rows.ReturnValue'])
    g.branch('still_same');g.link((unchanged,'still_same.condition'));g.chain('readback','still_same')
    g.set('consume','Consumed',defaults={'Consumed':'true'});g.chain('still_same','consume')
    g.branch('which');g.link(('first.ReturnValue','which.condition'));g.chain('consume','which')
    g.set('prepared','Stage',R.ACTOR_CLASS,defaults={'Stage':'1'});g.link(('manager.Manager','prepared.self'));g.chain('which','prepared')
    g.set('started','CompetitiveStarted',B.RULE,defaults={'CompetitiveStarted':'true'});g.link(('rule.Rule','started.self'));g.chain('prepared','started')
    # The fresh world's waiting-room spawn may have latched the initial native roles.
    # Learn again on the first restored warmup spawn, after ObjectiveTeam was restored.
    g.set('unlatch','bLatched',B.RULE,defaults={'bLatched':'false'});g.link(('rule.Rule','unlatch.self'));g.chain('started','unlatch')
    base,_=B._start_key(g,'base_')
    g.set('approved','StartApprovedMatch',B.RULE);g.link(('rule.Rule','approved.self'),(base,'approved.StartApprovedMatch'));g.chain('unlatch','approved')
    # Only the just-verified snapshot can define the resumed roster. A broad
    # PlayerArray rescan could admit a late outsider awaiting its kick reply.
    g.get('verified_ids','SnapshotIds',R.ACTOR_CLASS);g.link(('manager.Manager','verified_ids.self'))
    g.set('humans','StartApprovedHumans',B.RULE)
    g.link(('rule.Rule','humans.self'),('verified_ids.SnapshotIds','humans.StartApprovedHumans'));g.chain('approved','humans')
    g.set('clean','PresenceDirty',B.RULE,defaults={'PresenceDirty':'false'});g.link(('rule.Rule','clean.self'));g.chain('humans','clean')
    g.call('gm',GS_LIB,'GetGameMode');g.cast('mode',B.BC_GM,pure=True);g.link(('gm.ReturnValue','mode.cast_object'))
    g.call('refresh',B.BC_GM,'RefreshWaitingForPlayer');g.link(('mode.cast_result','refresh.self'));g.chain('clean','refresh')
    g.set('complete','Stage',R.ACTOR_CLASS,defaults={'Stage':'3'});g.link(('manager.Manager','complete.self'),('which.else','complete.exec'))
    g.set('final_humans','StartApprovedHumans',B.RULE)
    g.link(('rule.Rule','final_humans.self'),('verified_ids.SnapshotIds','final_humans.StartApprovedHumans'));g.chain('complete','final_humans')
    g.set('final_clean','PresenceDirty',B.RULE,defaults={'PresenceDirty':'false'});g.link(('rule.Rule','final_clean.self'));g.chain('final_humans','final_clean')
    previous='final_clean'
    for prop,val in (('RecoveryApplied','true'),('RecoveryPending','false')):
        g.set(prop,prop,B.RULE,defaults={prop:val});g.link(('rule.Rule',prop+'.self'));g.chain(previous,prop);previous=prop
    g.call('gs',GS_LIB,'GetGameState');g.cast('state',B.BC_GS,pure=True);g.link(('gs.ReturnValue','state.cast_object'))
    release=countdown(g,'release_','state.cast_result',5);g.chain(previous,release)
    return g.json()
