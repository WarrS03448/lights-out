"""Authored round checkpoint and participant connection graphs for make_blueprints.py."""
from ctf_graphs import G, P, SYS, MATH, STR, ARR, GS_LIB, ACTOR, CONTROLLER
import bb5_graphs as B

ACTOR_CLASS = B.BB5_DIR + '/BP_BB5Recovery.BP_BB5Recovery_C'
URL = 'https://play.lightsoutranked.com/api/match-report/recovery'


def gi(g, p):
    g.call(p+'gi', GS_LIB, 'GetGameInstance')
    g.cast(p+'cast', B.GI_CLASS, pure=True)
    g.link((p+'gi.ReturnValue', p+'cast.cast_object'))
    return p+'cast.cast_result'


def text(g, name, value):
    g.call(name, STR, 'Conv_IntToString'); g.link((value, name+'.InInt'))
    return name+'.ReturnValue'


def concat(g, name, values, separator):
    previous = values[0]
    for i, value in enumerate(values[1:]):
        key = name+str(i)
        g.call(key+'sep', STR, 'Concat_StrStr', {'B': separator})
        g.call(key, STR, 'Concat_StrStr')
        g.link((previous,key+'sep.A'),(key+'sep.ReturnValue',key+'.A'),(value,key+'.B'))
        previous = key+'.ReturnValue'
    return previous


def conjunction(g, p, values):
    previous = values[0]
    for i, value in enumerate(values[1:]):
        node=p+str(i);g.call(node,MATH,'BooleanAND')
        g.link((previous,node+'.A'),(value,node+'.B'));previous=node+'.ReturnValue'
    return previous


def events():
    g=G()
    g.custom('start','RecoveryStart',[P('RuleComponent','object',**{'class':B.RULE})])
    g.custom('tick','RecoveryTick')
    g.custom('pulse_send','SendPulse')
    g.custom('checkpoint_send','SendCheckpoint')
    g.custom('warmup','RecoveryWarmup')
    return g.json()


def on_rep():
    # HTTP is latent, so it belongs to an event, never a RepNotify function.
    g=G();g.entry();g.call('authority',ACTOR,'HasAuthority');g.branch('host')
    g.link(('authority.ReturnValue','host.condition'));g.chain('entry','host')
    g.call('send',None,'SendPulse');g.link(('host.else','send.exec'))
    return g.json()


def pulse_logic():
    g=G();g.existing('pulse_send','SendPulse')
    local=gi(g,'pulse_')
    for prop,name in (('Search String','private'),('Session Name','host_session'),('SessionToJoin (Client)','join_session')):
        g.get(name,prop,B.GI_CLASS);g.link((local,name+'.self'))
    g.call('authority',ACTOR,'HasAuthority')
    g.call('intended',MATH,'SelectString')
    g.link(('authority.ReturnValue','intended.bPickA'),('host_session.Session Name','intended.A'),('join_session.SessionToJoin (Client)','intended.B'))
    for name in ('Session','Epoch','Sequence'):g.get(name,name)
    g.call('same',STR,'EqualEqual_StrStr');g.link(('Session.Session','same.A'),('intended.ReturnValue','same.B'))
    g.call('ranked',STR,'StartsWith',{'InPrefix':'chm-'});g.link(('Session.Session','ranked.SourceString'))
    g.call('pc',GS_LIB,'GetPlayerController',{'PlayerIndex':'0'})
    g.get('ps','PlayerState',CONTROLLER);g.link(('pc.ReturnValue','ps.self'))
    g.call('valid_pc',SYS,'IsValid');g.link(('pc.ReturnValue','valid_pc.Object'))
    g.call('valid_ps',SYS,'IsValid');g.link(('ps.PlayerState','valid_ps.Object'))
    g.call('id',B.ONLINE,'RetrievePlatformIdAsStringFromPlayerState');g.link(('ps.PlayerState','id.PlayerState'))
    g.call('cap',STR,'Left',{'Count':'64'});g.link(('private.Search String','cap.SourceString'))
    g.call('cap_length',STR,'Len');g.link(('cap.ReturnValue','cap_length.S'))
    g.call('cap_valid',MATH,'EqualEqual_IntInt',{'B':'64'});g.link(('cap_length.ReturnValue','cap_valid.A'))
    guard=conjunction(g,'guard',['same.ReturnValue','ranked.ReturnValue','valid_pc.ReturnValue','valid_ps.ReturnValue','cap_valid.ReturnValue'])
    g.branch('allowed');g.link((guard,'allowed.condition'));g.chain('pulse_send','allowed')
    epoch=text(g,'epoch_text','Epoch.Epoch')
    bearer=concat(g,'bearer',['cap.ReturnValue',epoch],'.')
    sequence=text(g,'seq_text','Sequence.Sequence')
    g.call('send',B.HTTP_LIB,'SendAttributionEvent',{'URL':URL,'EventName':'ch_session_pulse',
        'FirstSessionTimestamp':'chrecovery-1','Platform':'','IsFirstGameOpen':'false'})
    g.link((bearer,'send.BearerToken'),('id.ReturnValue','send.UserId'),('Session.Session','send.Storefront'),(sequence,'send.Timestamp'))
    g.chain('allowed','send')
    return g.json()


def logic():
    import recovery_restore_graphs as restore
    g=G();g.existing('start','RecoveryStart')
    g.set('rule_set','Rule');g.link(('start.RuleComponent','rule_set.Rule'));g.chain('start','rule_set')
    g.selfnode('self')
    g.call('timer',SYS,'K2_SetTimer',{'FunctionName':'RecoveryTick','Time':'1.0','bLooping':'true'})
    g.link(('self.self','timer.Object'))
    initialised=restore.initialise(g,'rule_set.then')
    bound=restore.bind(g,initialised);g.link((bound,'timer.exec'))
    g.existing('tick','RecoveryTick')
    g.call('authority',ACTOR,'HasAuthority');g.branch('host');g.link(('authority.ReturnValue','host.condition'));g.chain('tick','host')
    session,ranked=B._start_key(g,'session_',full=True)
    g.branch('ranked');g.link((ranked,'ranked.condition'));g.chain('host','ranked')
    g.set('session_set','Session');g.link((session,'session_set.Session'));g.chain('ranked','session_set')
    g.get('rule','Rule');g.get('epoch','HostEpoch',B.RULE);g.link(('rule.Rule','epoch.self'))
    g.set('epoch_set','Epoch');g.link(('epoch.HostEpoch','epoch_set.Epoch'));g.chain('session_set','epoch_set')
    g.seq('actions',3);g.chain('epoch_set','actions')
    capture_tick(g, 'actions.then_1')
    restore.tick(g,'actions.then_2')
    # Inspect boundaries every second, but send independent liveness only every five.
    g.get('ticks','Ticks');g.call('tick_next',MATH,'Add_IntInt',{'B':'1'});g.link(('ticks.Ticks','tick_next.A'))
    g.set('tick_save','Ticks');g.link(('tick_next.ReturnValue','tick_save.Ticks'),('actions.then_0','tick_save.exec'))
    g.call('mod',MATH,'Percent_IntInt',{'B':'5'});g.link(('ticks.Ticks','mod.A'))
    g.call('due',MATH,'EqualEqual_IntInt',{'B':'0'});g.link(('mod.ReturnValue','due.A'))
    g.branch('report_due');g.link(('due.ReturnValue','report_due.condition'));g.chain('tick_save','report_due')
    g.get('seq','Sequence');g.call('next',MATH,'Add_IntInt',{'B':'1'});g.link(('seq.Sequence','next.A'))
    g.set('seq_set','Sequence');g.link(('next.ReturnValue','seq_set.Sequence'));g.chain('report_due','seq_set')
    g.call('notify',None,'SendPulse');g.chain('seq_set','notify')
    g.call('send_checkpoint',None,'SendCheckpoint');g.chain('notify','send_checkpoint')
    return g.json()


def snapshot():
    """Read a synchronous round boundary, with team scores keyed by native TeamID."""
    g=G();g.entry();g.result()
    g.call('gs',GS_LIB,'GetGameState');g.cast('gsc',B.BC_GS,pure=True);g.link(('gs.ReturnValue','gsc.cast_object'))
    for name,prop in (('round','CurrentRound'),('objective','ObjectiveTeam'),('teams','Teams')):
        g.get(name,prop,B.BC_GS);g.link(('gsc.cast_result',name+'.self'))
    g.call('limit',B.BC_GS,'GetScoreLimit');g.link(('gsc.cast_result','limit.self'))
    g.set('clear_rows','Rows',defaults={'Rows':''})
    g.get('ids','SnapshotIds');g.call('clear_ids',ARR,'Array_Clear',array=True);g.link(('ids.SnapshotIds','clear_ids.TargetArray'))
    g.set('clear0','Score0',defaults={'Score0':'-1'});g.set('clear1','Score1',defaults={'Score1':'-1'})
    g.chain('entry','clear_rows','clear_ids','clear0','clear1')
    g.foreach('teams_each');g.link(('teams.Teams','teams_each.Array'));g.chain('clear1','teams_each')
    g.brk('td',B.V_TEAMDATA);g.link(('teams_each.Array Element','td.in'))
    g.call('is0',MATH,'EqualEqual_IntInt',{'B':'0'});g.link(('td.TeamID','is0.A'))
    g.call('is1',MATH,'EqualEqual_IntInt',{'B':'1'});g.link(('td.TeamID','is1.A'))
    g.branch('team0');g.link(('teams_each.LoopBody','team0.exec'),('is0.ReturnValue','team0.condition'))
    g.branch('team1');g.link(('team0.else','team1.exec'),('is1.ReturnValue','team1.condition'))
    for i in (0,1):
        g.set('score'+str(i),'Score'+str(i));g.link(('td.TeamScore','score'+str(i)+'.Score'+str(i)))
        g.chain('team'+str(i),'score'+str(i))
        g.get('s'+str(i),'Score'+str(i))
    fields=['round.CurrentRound','limit.ReturnValue','s0.Score0','s1.Score1','objective.ObjectiveTeam']
    meta=concat(g,'meta',[text(g,'number'+str(i),value) for i,value in enumerate(fields)],';')
    g.set('save_meta','Meta');g.link((meta,'save_meta.Meta'),('teams_each.Completed','save_meta.exec'))
    g.get('players','PlayerArray',B.GAMESTATE);g.link(('gs.ReturnValue','players.self'))
    g.foreach('players_each');g.link(('players.PlayerArray','players_each.Array'));g.chain('save_meta','players_each')
    g.cast('ps',B.BC_PS,pure=True);g.link(('players_each.Array Element','ps.cast_object'))
    g.call('id',B.ONLINE,'RetrievePlatformIdAsStringFromPlayerState');g.link(('players_each.Array Element','id.PlayerState'))
    g.call('id_length',STR,'Len');g.link(('id.ReturnValue','id_length.S'))
    g.call('human',MATH,'EqualEqual_IntInt',{'B':'17'});g.link(('id_length.ReturnValue','human.A'))
    g.branch('include');g.link(('human.ReturnValue','include.condition'),('players_each.LoopBody','include.exec'))
    g.call('add_id',ARR,'Array_AddUnique',array=True);g.link(('ids.SnapshotIds','add_id.TargetArray'),('id.ReturnValue','add_id.NewItem'))
    values=['id.ReturnValue']
    for i,prop in enumerate(('TeamID','Kill','Death','SpawnCount')):
        key='p'+str(i);g.get(key,prop,B.BC_PS);g.link(('ps.cast_result',key+'.self'))
        values.append(text(g,key+'text',key+'.'+prop))
    g.call('row_owner',ACTOR,'GetOwner');g.link(('players_each.Array Element','row_owner.self'))
    g.cast('row_controller',B.CONTROLLER,pure=True);g.link(('row_owner.ReturnValue','row_controller.cast_object'))
    g.call('row_connected',SYS,'IsValid');g.link(('row_controller.cast_result','row_connected.Object'))
    checks=[B._binary_team(g,'row_team','p0.TeamID'),'row_connected.ReturnValue']
    for i,prop,minimum,maximum in ((1,'Kill',-999,999),(2,'Death',0,99),(3,'SpawnCount',0,999)):
        for suffix,fn,bound in (('min','GreaterEqual_IntInt',minimum),('max','LessEqual_IntInt',maximum)):
            node='row_'+prop+suffix;g.call(node,MATH,fn,{'B':str(bound)});g.link(('p'+str(i)+'.'+prop,node+'.A'));checks.append(node+'.ReturnValue')
    valid=conjunction(g,'row_valid',checks)
    g.branch('row_ready');g.link((valid,'row_ready.condition'));g.chain('include','row_ready','add_id')
    row=concat(g,'row',values,':')
    g.get('rows','Rows');g.call('empty',STR,'EqualEqual_StrStr',{'B':''});g.link(('rows.Rows','empty.A'))
    g.call('separator',MATH,'SelectString',{'A':'','B':';'});g.link(('empty.ReturnValue','separator.bPickA'))
    g.call('append_sep',STR,'Concat_StrStr');g.link(('rows.Rows','append_sep.A'),('separator.ReturnValue','append_sep.B'))
    g.call('append_row',STR,'Concat_StrStr');g.link(('append_sep.ReturnValue','append_row.A'),(row,'append_row.B'))
    g.set('save_rows','Rows');g.link(('append_row.ReturnValue','save_rows.Rows'));g.chain('add_id','save_rows')
    g.link(('players_each.Completed','result.exec'))
    return g.json()


def capture_tick(g, after):
    g.call('capture_gs',GS_LIB,'GetGameState');g.cast('capture_state',B.BC_GS,pure=True)
    g.link(('capture_gs.ReturnValue','capture_state.cast_object'))
    g.call('capture_phase',B.BC_GS,'GetCurrentPhase');g.link(('capture_state.cast_result','capture_phase.self'))
    g.call('capture_tag','/Script/GameplayTags.BlueprintGameplayTagLibrary','GetTagName');g.link(('capture_phase.ReturnValue','capture_tag.GameplayTag'))
    g.call('capture_text',STR,'Conv_NameToString');g.link(('capture_tag.ReturnValue','capture_text.InName'))
    g.call('capture_warm',STR,'EqualEqual_StrStr',{'B':'Game.Phase.RoundWarmup'});g.link(('capture_text.ReturnValue','capture_warm.A'))
    g.get('capture_round','CurrentRound',B.BC_GS);g.link(('capture_state.cast_result','capture_round.self'))
    g.get('capture_previous','CapturedRound');g.get('capture_have','HasCapture')
    g.call('capture_changed',MATH,'NotEqual_IntInt');g.link(('capture_round.CurrentRound','capture_changed.A'),('capture_previous.CapturedRound','capture_changed.B'))
    g.call('capture_first',MATH,'Not_PreBool');g.link(('capture_have.HasCapture','capture_first.A'))
    g.call('capture_new',MATH,'BooleanOR');g.link(('capture_changed.ReturnValue','capture_new.A'),('capture_first.ReturnValue','capture_new.B'))
    g.get('capture_started','CompetitiveStarted',B.RULE);g.link(('rule.Rule','capture_started.self'))
    g.get('capture_pending','RecoveryPending',B.RULE);g.link(('rule.Rule','capture_pending.self'))
    g.call('capture_restored',MATH,'Not_PreBool');g.link(('capture_pending.RecoveryPending','capture_restored.A'))
    ready=conjunction(g,'capture_ok',['capture_warm.ReturnValue','capture_new.ReturnValue','capture_started.CompetitiveStarted','capture_restored.ReturnValue'])
    g.branch('capture_guard');g.link((ready,'capture_guard.condition'),(after,'capture_guard.exec'))
    g.call('capture',None,'BuildSnapshot');g.chain('capture_guard','capture')
    # Teams/PlayerArray can still be arriving. Do not freeze a rejected partial read.
    checks=[]
    for i in (0,1):
        g.get('capture_s'+str(i),'Score'+str(i))
        g.call('capture_nonnegative'+str(i),MATH,'GreaterEqual_IntInt',{'B':'0'})
        g.link(('capture_s'+str(i)+'.Score'+str(i),'capture_nonnegative'+str(i)+'.A'));checks.append('capture_nonnegative'+str(i)+'.ReturnValue')
    g.call('capture_sum',MATH,'Add_IntInt');g.link(('capture_s0.Score0','capture_sum.A'),('capture_s1.Score1','capture_sum.B'))
    g.call('capture_boundary',MATH,'EqualEqual_IntInt');g.link(('capture_sum.ReturnValue','capture_boundary.A'),('capture_round.CurrentRound','capture_boundary.B'));checks.append('capture_boundary.ReturnValue')
    g.get('capture_rows','Rows');g.call('capture_parse',STR,'ParseIntoArray',{'Delimiter':';','CullEmptyStrings':'true'});g.link(('capture_rows.Rows','capture_parse.SourceString'))
    g.call('capture_count',ARR,'Array_Length',array=True);g.link(('capture_parse.ReturnValue','capture_count.TargetArray'))
    g.get('capture_expected','StartApprovedHumans',B.RULE);g.link(('rule.Rule','capture_expected.self'))
    g.call('capture_expected_count',ARR,'Array_Length',array=True);g.link(('capture_expected.StartApprovedHumans','capture_expected_count.TargetArray'))
    g.call('capture_full',MATH,'EqualEqual_IntInt');g.link(('capture_count.ReturnValue','capture_full.A'),('capture_expected_count.ReturnValue','capture_full.B'));checks.append('capture_full.ReturnValue')
    g.call('capture_two',MATH,'GreaterEqual_IntInt',{'B':'2'});g.link(('capture_count.ReturnValue','capture_two.A'));checks.append('capture_two.ReturnValue')
    g.get('capture_objective','ObjectiveTeam',B.BC_GS);g.link(('capture_state.cast_result','capture_objective.self'))
    objective=B._binary_team(g,'capture_objective_valid','capture_objective.ObjectiveTeam');checks.append(objective)
    g.call('capture_limit',B.BC_GS,'GetScoreLimit');g.link(('capture_state.cast_result','capture_limit.self'))
    for i in (0,1):
        g.call('capture_unfinished'+str(i),MATH,'Less_IntInt');g.link(('capture_s'+str(i)+'.Score'+str(i),'capture_unfinished'+str(i)+'.A'),('capture_limit.ReturnValue','capture_unfinished'+str(i)+'.B'));checks.append('capture_unfinished'+str(i)+'.ReturnValue')
    g.set('capture_roster_reset','SnapshotRosterValid',defaults={'SnapshotRosterValid':'true'});g.chain('capture','capture_roster_reset')
    g.foreach('capture_each');g.link(('capture_expected.StartApprovedHumans','capture_each.Array'));g.chain('capture_roster_reset','capture_each')
    g.get('capture_ids','SnapshotIds');g.call('capture_contains',ARR,'Array_Contains',array=True);g.link(('capture_ids.SnapshotIds','capture_contains.TargetArray'),('capture_each.Array Element','capture_contains.ItemToFind'))
    g.branch('capture_member');g.link(('capture_contains.ReturnValue','capture_member.condition'),('capture_each.LoopBody','capture_member.exec'))
    g.set('capture_roster_bad','SnapshotRosterValid',defaults={'SnapshotRosterValid':'false'});g.link(('capture_member.else','capture_roster_bad.exec'))
    g.get('capture_roster','SnapshotRosterValid');checks.append('capture_roster.SnapshotRosterValid')
    complete=conjunction(g,'capture_complete',checks)
    g.branch('capture_valid');g.link((complete,'capture_valid.condition'),('capture_each.Completed','capture_valid.exec'))
    g.set('capture_save_round','CapturedRound');g.link(('capture_round.CurrentRound','capture_save_round.CapturedRound'));g.chain('capture_valid','capture_save_round')
    g.set('capture_save_epoch','CapturedEpoch');g.link(('epoch.HostEpoch','capture_save_epoch.CapturedEpoch'));g.chain('capture_save_round','capture_save_epoch')
    g.set('capture_done','HasCapture',defaults={'HasCapture':'true'});g.chain('capture_save_epoch','capture_done')


def checkpoint_logic():
    g=G();g.existing('checkpoint_send','SendCheckpoint')
    g.get('captured','HasCapture');g.branch('has_capture');g.link(('captured.HasCapture','has_capture.condition'));g.chain('checkpoint_send','has_capture')
    for name in ('Meta','Rows','Session','CapturedEpoch','Rule'):g.get(name,name)
    g.get('epoch','HostEpoch',B.RULE);g.link(('Rule.Rule','epoch.self'))
    g.call('same_epoch',MATH,'EqualEqual_IntInt');g.link(('CapturedEpoch.CapturedEpoch','same_epoch.A'),('epoch.HostEpoch','same_epoch.B'))
    g.branch('current');g.link(('same_epoch.ReturnValue','current.condition'));g.chain('has_capture','current')
    g.call('id',B.ONLINE,'GetPlatformUserNetId')
    B._report_send(g,'send',{'URL':URL,'EventName':'ch_recovery_checkpoint','FirstSessionTimestamp':'chrecovery-1','IsFirstGameOpen':'false'})
    g.link(('Meta.Meta','send.Timestamp'),('Rows.Rows','send.Platform'),('Session.Session','send.Storefront'),('id.ReturnValue','send.UserId'))
    g.chain('current','send')
    return g.json()


def spawn(g):
    g.call('recovery_transform',MATH,'MakeTransform');g.spawn('recovery_actor',ACTOR_CLASS)
    g.link(('recovery_transform.ReturnValue','recovery_actor.SpawnTransform'))
    g.get('recovery_rule','BB5BombRule')
    g.call('recovery_start',ACTOR_CLASS,'RecoveryStart')
    g.link(('recovery_actor.ReturnValue','recovery_start.self'),('recovery_rule.BB5BombRule','recovery_start.RuleComponent'))
    g.chain('recovery_actor','recovery_start')
