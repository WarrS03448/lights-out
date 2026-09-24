"""Execute generated transport graphs; the Unreal build separately checks actual pins."""
from test_start_gate_graph import Graph
import bb5_graphs as B
try:
    import recovery_graphs as R
except ModuleNotFoundError:
    R = None
try:
    import recovery_restore_graphs as restore
except ModuleNotFoundError:
    restore = None


class RecoveryGraph(Graph):
    def __init__(self, body, token="", key="chm-0123456789abcdef-r1111111111111111"):
        super().__init__(body, [], {"HostEpoch": 2}, key=key)
        self.token = token

    def value(self, pin):
        node = self.nodes[pin.split(".")[0]]
        fn = node.get("func")
        if fn == "GetGameInstance":
            return {"Session Name": self.key, "Search String": self.token}
        if fn in ("GetGameMode", "GetComponentByClass"):
            return self.rule
        if fn == 'GetRealTimeSeconds':return self.now
        if fn == "Left":
            return self.arg(node, "SourceString")[:int(self.arg(node, "Count"))]
        if fn == 'LessEqual_IntInt':return int(self.arg(node,'A'))<=int(self.arg(node,'B'))
        return super().value(pin)


def test_host_report_keeps_private_restore_configuration_out_of_the_authorization_header():
    g = B.G()
    B._report_send(g, "send", {})
    graph = RecoveryGraph(g.json(), "a" * 64 + "-r1-private-configuration")
    assert graph.arg(graph.nodes["send"], "BearerToken") == "a" * 64 + ".2"


def test_rotated_search_session_still_reports_to_the_permanent_match():
    g = B.G()
    key, _ = B._start_key(g, "test_")
    assert RecoveryGraph(g.json()).value(key) == "chm-0123456789abcdef"


def test_participant_pulse_uses_replicated_session_and_local_private_identity():
    assert R is not None, "participant pulse graph is not implemented"
    class Client(RecoveryGraph):
        def __init__(self, expected):
            super().__init__(R.pulse_logic(), "b" * 64)
            self.rule.update(Session=self.key, Epoch=4, Sequence=17)
            self.expected = expected
            self.sent = []
        def value(self, pin):
            node = self.nodes[pin.split('.')[0]]
            fn = node.get('func')
            if fn == 'GetGameInstance':
                return {'Session Name': self.expected, 'SessionToJoin (Client)': self.expected, 'Search String': self.token}
            if fn == 'HasAuthority': return False
            if fn == 'GetPlayerController': return {'PlayerState': {'id': '76561198000000002'}}
            return super().value(pin)
        def execute(self, name):
            node = self.nodes[name]
            if node.get('func') == 'SendAttributionEvent':
                self.sent.append({key:self.arg(node,key) for key in ('BearerToken','UserId','Storefront','Timestamp')})
            super().execute(name)
    good = Client('chm-0123456789abcdef-r1111111111111111')
    good.execute('pulse_send')
    assert good.sent == [{'BearerToken':'b'*64+'.4','UserId':'76561198000000002',
                          'Storefront':'chm-0123456789abcdef-r1111111111111111','Timestamp':'17'}]
    wrong = Client('chm-fedcba9876543210')
    wrong.execute('pulse_send')
    assert wrong.sent == []


def test_boundary_snapshot_is_coherent_and_uses_team_ids_instead_of_array_order():
    assert R is not None and hasattr(R, 'snapshot'), "round snapshot graph is not implemented"
    class Capture(RecoveryGraph):
        def __init__(self):
            super().__init__(R.snapshot())
            self.rule['SnapshotIds']=[]
            self.state = {'CurrentRound':3, 'ObjectiveTeam':1, 'Teams':[
                {'TeamID':1,'TeamScore':1}, {'TeamID':0,'TeamScore':2}], 'PlayerArray':[
                {'id':'76561198000000001','TeamID':0,'Kill':-1,'Death':2,'SpawnCount':3,'owner':{}},
                {'id':'76561198000000002','TeamID':1,'Kill':2,'Death':1,'SpawnCount':3,'owner':{}}]}
        def value(self, pin):
            node, output = pin.split('.', 1)
            spec = self.nodes[node]
            if spec.get('func') == 'GetGameState': return self.state
            if spec.get('func') == 'GetScoreLimit': return 7
            if spec['type'] == 'break': return self.arg(spec, 'in')[output]
            return super().value(pin)
    graph = Capture()
    graph.run()
    assert graph.rule['Meta'] == '3;7;2;1;1'
    assert graph.rule['Rows'] == '76561198000000001:0:-1:2:3;76561198000000002:1:2:1:3'
    for prop,bad in [('TeamID',-1),('Kill',1000),('Death',-1),('SpawnCount',1000),('owner',None)]:
        incomplete=Capture();incomplete.state['PlayerArray'][1][prop]=bad;incomplete.run()
        assert incomplete.rule['Rows']=='76561198000000001:0:-1:2:3'
        assert incomplete.rule['SnapshotIds']==['76561198000000001']


def test_seed_epoch_is_match_bound_and_never_rewinds_native_migration():
    assert restore is not None, 'cold recovery initialisation is not implemented'
    g=B.G();g.entry();restore.initialise(g,'entry.then')
    envelope='a'*64+'-r1-0123456789abcdef-2-'+'b'*64+'-3-7-2-1-1-2-76561198000000001-0-999-2-3-76561198000000002-1-1002-1-3-z'
    class Seed(RecoveryGraph):
        def value(self,pin):
            node=self.nodes[pin.split('.')[0]];fn=node.get('func')
            if fn=='ParseIntoArray':return self.arg(node,'SourceString').split(self.arg(node,'Delimiter'))
            if fn=='Array_Get':return self.arg(node,'TargetArray')[int(self.arg(node,'Index'))]
            if fn=='Conv_StringToInt':return int(self.arg(node,'InString'))
            if fn=='Max':return max(int(self.arg(node,'A')),int(self.arg(node,'B')))
            if fn=='Multiply_IntInt':return int(self.arg(node,'A'))*int(self.arg(node,'B'))
            return super().value(pin)
        def execute(self,name):
            node=self.nodes[name]
            if node['type']=='set' and 'class' in node:
                self.arg(node,'self')[node['var']]=self.arg(node,node['var']);self.emit(name);return
            super().execute(name)
    for current_epoch,key,want_epoch,want_stage in [(0,'chm-0123456789abcdef',2,0),(3,'chm-0123456789abcdef',3,-1),(0,'chm-fedcba9876543210',0,-1)]:
        graph=Seed(g.json(),envelope,key=key)
        rule={'HostEpoch':current_epoch,'RecoveryApplied':False}
        graph.rule={'Rule':rule}
        graph.run()
        assert rule['HostEpoch']==want_epoch
        assert int(graph.rule['Stage'])==want_stage


def test_restore_writes_saved_fields_by_steam_identity_and_team_id():
    assert restore is not None and hasattr(restore,'apply_snapshot'), 'native restore graph is missing'
    class Apply(RecoveryGraph):
        def __init__(self):
            super().__init__(restore.apply_snapshot())
            self.rule={'Parts': ('a'*64+'-r1-0123456789abcdef-2-'+'b'*64+
                '-3-7-2-1-1-2-76561198000000001-0-999-2-3-76561198000000002-1-1002-1-3-z').split('-'), 'Rule':{}}
            self.state={'CurrentRound':0,'ObjectiveTeam':0,'CounterObjectiveTeam':1,
                'Teams':[{'TeamID':1,'TeamScore':0,'PlayerCount':1},{'TeamID':0,'TeamScore':0,'PlayerCount':1}],
                'PlayerArray':[{'id':'76561198000000002'},{'id':'76561198999999999','Kill':11},{'id':'76561198000000001'}]}
        def value(self,pin):
            name,out=pin.split('.',1);n=self.nodes[name];fn=n.get('func')
            if fn=='GetGameState':return self.state
            if fn=='Array_Get':return self.arg(n,'TargetArray')[int(self.arg(n,'Index'))]
            if fn=='Conv_StringToInt':return int(self.arg(n,'InString'))
            if fn=='Subtract_IntInt':return int(self.arg(n,'A'))-int(self.arg(n,'B'))
            if fn=='Percent_IntInt':return int(self.arg(n,'A'))%int(self.arg(n,'B'))
            if fn=='SelectInt':return int(self.arg(n,'A') if self.arg(n,'bPickA') else self.arg(n,'B'))
            if n['type']=='break':return self.arg(n,'in')[out]
            if n['type']=='make':return {p:self.arg(n,p) for p in ('TeamID','TeamScore','PlayerCount')}
            return super().value(pin)
        def execute(self,name):
            n=self.nodes[name]
            if n['type']=='set' and 'class' in n:
                self.arg(n,'self')[n['var']]=self.arg(n,n['var']);self.emit(name);return
            if n['type']=='foreach':
                for i,v in enumerate(self.arg(n,'Array')):
                    self.values[name+'.Array Index']=i;self.values[name+'.Array Element']=v;self.emit(name,'LoopBody')
                self.emit(name,'Completed');return
            if n.get('func')=='Array_Set':self.arg(n,'TargetArray')[int(self.arg(n,'Index'))]=self.arg(n,'Item')
            super().execute(name)
    g=Apply();g.run()
    assert g.state['CurrentRound']==3 and g.state['ObjectiveTeam']==1 and g.state['CounterObjectiveTeam']==0
    assert [t['TeamScore'] for t in g.state['Teams']]==[1,2]
    assert g.state['PlayerArray']==[
        {'id':'76561198000000002','TeamID':1,'Kill':2,'Death':1,'SpawnCount':3},
        {'id':'76561198999999999','Kill':11},
        {'id':'76561198000000001','TeamID':0,'Kill':-1,'Death':2,'SpawnCount':3}]


def test_incomplete_warmup_capture_is_retried_until_the_saved_roster_and_scores_are_ready():
    g=B.G();g.entry();g.get('rule','Rule');g.get('epoch','HostEpoch',B.RULE);g.link(('rule.Rule','epoch.self'))
    R.capture_tick(g,'entry.then')
    class Boundary(RecoveryGraph):
        def value(self,pin):
            n=self.nodes[pin.split('.')[0]];fn=n.get('func')
            if fn=='GetGameState':return {'CurrentRound':3,'ObjectiveTeam':1}
            if fn in ('GetCurrentPhase','GetTagName','Conv_NameToString'):return 'Game.Phase.RoundWarmup'
            if fn=='NotEqual_IntInt':return int(self.arg(n,'A'))!=int(self.arg(n,'B'))
            if fn=='GetScoreLimit':return 7
            if fn=='ParseIntoArray':return self.arg(n,'SourceString').split(self.arg(n,'Delimiter'))
            return super().value(pin)
        def execute(self,name):
            n=self.nodes[name]
            if n['type']=='set' and n['var'] in ('HasCapture','SnapshotRosterValid'):
                self.rule[n['var']]=self.arg(n,n['var'])=='true';self.emit(name);return
            super().execute(name)
    graph=Boundary(g.json());ids=['76561198000000001','76561198000000002']
    graph.rule={'Rule':{'HostEpoch':2,'CompetitiveStarted':True,'RecoveryPending':False,'StartApprovedHumans':ids},
        'HasCapture':False,'CapturedRound':0,'Score0':-1,'Score1':1,'Rows':';'.join(ids),'SnapshotIds':ids}
    graph.run();assert graph.rule['HasCapture'] is False
    graph.rule.update(Score0=2,Rows=ids[0],SnapshotIds=ids[:1]);graph.run();assert graph.rule['HasCapture'] is False
    graph.rule.update(Rows=ids[0]+';76561198999999999',SnapshotIds=[ids[0],'76561198999999999']);graph.run();assert graph.rule['HasCapture'] is False
    graph.rule.update(Rows=';'.join(ids),SnapshotIds=ids);graph.run()
    assert graph.rule['HasCapture'] is True and graph.rule['CapturedRound']==3


def test_restore_reply_cannot_unlock_a_changed_epoch_stage_session_or_payload():
    class Reply(RecoveryGraph):
        def __init__(self,stage=0):
            super().__init__(restore.request_logic())
            self.native={'HostEpoch':2}
            self.manager={'Rule':self.native,'Stage':stage,'Epoch':2,'Session':self.key,'Rows':'saved-rows','Meta':'saved-meta',
                'SnapshotIds':['76561198000000001','76561198000000002']}
            self.rule={'Manager':self.manager,'Stage':stage,'Epoch':2,'Session':self.key,'Rows':'saved-rows','Meta':'saved-meta','Deadline':100,'Consumed':False}
            self.values['reply.bSuccess']=True;self.countdowns=[]
            self.phase='Game.Phase.RoundWarmup'
        def value(self,pin):
            if self.nodes[pin.split('.')[0]].get('func') in ('GetCurrentPhase','GetTagName','Conv_NameToString'):
                return self.phase
            return super().value(pin)
        def execute(self,name):
            n=self.nodes[name]
            if n['type']=='set' and 'class' in n:
                value=self.arg(n,n['var']);value={'true':True,'false':False}.get(value,value) if isinstance(value,str) else value
                self.arg(n,'self')[n['var']]=value;self.emit(name);return
            if n.get('func')=='CaptureStartHumans':
                self.native['StartApprovedHumans']=self.manager['SnapshotIds']+['76561198999999999']
                self.emit(name);return
            if n.get('func')=='StartCountdown':self.countdowns.append(self.arg(n,'duration'))
            super().execute(name)
    for stage in (0,2):
        good=Reply(stage);good.execute('reply');assert int(good.manager['Stage'])==stage+1
        assert good.native['StartApprovedHumans']==good.manager['SnapshotIds'], 'late outsider cannot expand the verified roster'
        if stage==2:assert good.native['RecoveryApplied'] is True and good.countdowns==['5']
        for mutation in ('epoch','stage','session','payload','deadline') + (('phase',) if stage==2 else ()):
            stale=Reply(stage)
            if mutation=='epoch':stale.native['HostEpoch']=3
            if mutation=='stage':stale.manager['Stage']=stage+1
            if mutation=='session':stale.key='chm-0123456789abcdef-r2222222222222222'
            if mutation=='payload':stale.manager['Rows']='changed-roster'
            if mutation=='deadline':stale.now=100
            if mutation=='phase':stale.phase='Game.Phase.RoundInProgress'
            before=dict(stale.native);stale.execute('reply')
            assert stale.native==before and stale.countdowns==[],mutation


def test_abandoned_restore_exits_once_without_releasing_a_round_and_is_generation_fenced():
    g=B.G();g.entry();g.get('rule','Rule');g.get('epoch','HostEpoch',B.RULE);g.link(('rule.Rule','epoch.self'))
    restore.timeout_guard(g,'entry.then')
    class Timeout(RecoveryGraph):
        def __init__(self):
            super().__init__(g.json());self.now=721;self.exits=0
            self.rule={'Rule':{'HostEpoch':2},'Stage':2,'SeedEpoch':2,'SeedSession':self.key,'RestoreDeadline':720}
        def value(self,pin):
            fn=self.nodes[pin.split('.')[0]].get('func')
            if fn=='GetRealTimeSeconds':return self.now
            if fn=='HasAuthority':return True
            return super().value(pin)
        def execute(self,name):
            if self.nodes[name]['type']=='sequence':
                self.emit(name,'then_0');self.emit(name,'then_1');return
            if self.nodes[name].get('func')=='MatchExit':self.exits+=1
            super().execute(name)
    good=Timeout();good.run();good.run()
    assert good.exits==1 and int(good.rule['Stage'])==-2
    for prop,value in [('SeedEpoch',1),('SeedSession','chm-other'),('Stage',3),('RestoreDeadline',800)]:
        stale=Timeout();stale.rule[prop]=value;stale.run();assert stale.exits==0
