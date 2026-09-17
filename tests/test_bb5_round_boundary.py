"""Execute the authored round-boundary graphs without starting the game."""
import json

from test_start_gate_graph import Graph, roster, bg
import combat_graphs as cg


def test_release_arrival_reports_do_not_change_lobby_permissions():
    graph = json.loads(bg.gm_logic())
    nodes = graph['nodes']
    assert not any(n.get('func') == 'UpdateLobby' for n in nodes)
    assert not any('no-such-route' in str(n) for n in nodes)
    assert not any('ch-test-4821' in str(n) for n in nodes)
    for event in ('ch_lobby_read', 'ch_lobby_write'):
        assert any(n.get('defaults', {}).get('EventName') == event for n in nodes)
    assert ['t_write.then', 'x_bind.exec'] in graph['links']


class RoundGraph(Graph):
    def __init__(self, body, players, rule, objective=0):
        super().__init__(body, players, rule)
        self.objective = objective
        self.calls = []

    def value(self, pin):
        if pin in self.values: return self.values[pin]
        node, output = pin.split('.', 1)
        n = self.nodes[node]
        if n['type'] == 'cast' and output == 'cast_ok': return self.arg(n, 'cast_object') is not None
        if n['type'] == 'call':
            f = n['func']; a, b = self.arg(n, 'A'), self.arg(n, 'B')
            if f == 'GetObjectiveTeam': return self.objective
            if f in ('EqualEqual_IntInt', 'EqualEqual_NameName'): return str(a) == str(b)
            if f == 'Subtract_IntInt': return int(a) - int(b)
            if f == 'SelectInt': return int(a if self.arg(n, 'bPickA') else b)
        return super().value(pin)

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'set':
            obj = self.arg(n, 'self') if 'class' in n else self.rule
            value = self.arg(n, n['var'])
            if value in ('true', 'false'): value = value == 'true'
            if n['var'] in ('Kill', 'Death', 'CombatGaps'): value = int(value)
            obj[n['var']] = value
            self.emit(node); return
        if n['type'] == 'cast' and not n.get('pure'):
            self.emit(node, 'then' if self.arg(n, 'cast_object') is not None else 'cast_failed'); return
        if n['type'] == 'result':
            self.result = self.arg(n, 'Side'); return
        if n['type'] == 'call' and n['func'] in ('CombatEmit', 'SendAttributionEvent'):
            self.calls.append(n['func']); return
        super().execute(node)


def test_first_round_clears_warmup_kills_and_deaths_once_for_the_whole_roster():
    players = roster()
    for p in players: p.update(Kill=3, Death=2, SpawnCount=4, Score=7)
    rule = {'CompetitiveStarted': False}
    graph = RoundGraph(bg.rule_fn_BeginCompetitive(), players, rule)
    graph.execute('entry')
    assert rule['CompetitiveStarted'] is True
    assert all(p['Kill'] == p['Death'] == 0 for p in players)
    assert all(p['SpawnCount'] == 4 and p['Score'] == 7 for p in players)
    players[0].update(Kill=2, Death=1)
    graph.execute('entry')
    assert (players[0]['Kill'], players[0]['Death']) == (2, 1)


def test_teams_exchange_bases_when_the_objective_role_changes_and_swap_back():
    for initial in (0, 1):
        rule = {'bLatched': True, 'LatchTeam': 0, 'LatchSide': 1, 'LatchObjectiveTeam': initial}
        for objective, expected in ((initial, (1, 2)), (1-initial, (2, 1)), (initial, (1, 2))):
            for team in (0, 1):
                graph = RoundGraph(bg.rule_fn_SideForPlayer(), [], rule, objective)
                graph.values['entry.Player'] = {'PlayerState': {'TeamID': team}}
                graph.execute('entry')
                assert graph.result == expected[team]


def test_bases_stay_put_for_six_rounds_then_follow_the_native_side_change():
    rule = {'bLatched': True, 'LatchTeam': 0, 'LatchSide': 2, 'LatchObjectiveTeam': 0}
    for completed_rounds in range(12):
        # Native ObjectiveRuleSet flips roles after each six completed rounds.
        objective = (completed_rounds // 6) % 2
        graph = RoundGraph(bg.rule_fn_SideForPlayer(), [], rule, objective)
        graph.values['entry.Player'] = {'PlayerState': {'TeamID': 0}}
        graph.execute('entry')
        assert graph.result == (2 if completed_rounds < 6 else 1)


def test_initial_spawn_mapping_waits_for_a_valid_native_role_and_latches_only_once():
    rule = {'bLatched': False}
    graph = RoundGraph(bg.rule_fn_LatchFrom(), [], rule, -1)
    graph.values.update({'entry.Player': {'PlayerState': {'TeamID': 1}}, 'entry.Start': {'PlayerStartTag': '2'}})
    graph.execute('entry')
    assert rule['bLatched'] is False
    graph.objective = 1
    graph.execute('entry')
    assert rule['bLatched'] is True
    assert (rule['LatchTeam'], rule['LatchSide'], rule['LatchObjectiveTeam']) == (1, 2, 1)
    graph.objective = 0
    graph.execute('entry')
    assert rule['LatchObjectiveTeam'] == 1


def test_unknown_team_or_objective_keeps_native_spawn_fallback():
    for objective, team, latched in ((-1, 0, True), (0, -1, True), (0, 2, True), (0, 0, False)):
        graph = RoundGraph(bg.rule_fn_SideForPlayer(), [], {
            'bLatched': latched, 'LatchTeam': 0, 'LatchSide': 1, 'LatchObjectiveTeam': 0}, objective)
        graph.values['entry.Player'] = {'PlayerState': {'TeamID': team}}
        graph.execute('entry')
        assert graph.result == 0


def test_warmup_never_activates_combat_but_first_round_does_even_with_partial_bindings():
    for full in (False, True):
        rule = {'CombatActive': False, 'CombatBoundary': False, 'CombatClosed': False, 'CombatGaps': 0}
        graph = RoundGraph(cg.gm_logic(), [], rule)
        graph.values['full.ReturnValue'] = full
        graph.values['islive.ReturnValue'] = False
        graph.execute('CombatCoverage')
        assert rule['CombatActive'] is False
        assert graph.calls == []
        rule['CombatBoundary'] = True
        graph.values['islive.ReturnValue'] = True
        graph.execute('CombatCoverage')
        assert rule['CombatActive'] is True
        assert graph.calls == ['CombatEmit']
        assert rule['CombatGaps'] == 0  # missing binds are counted by reconcile, not intentional startup


def test_stats_and_kills_wait_for_start_latch_and_reset_is_bound_to_native_round_start():
    body = json.loads(bg.gm_logic())
    nodes = {n['id']: n for n in body['nodes']}
    links = dict(body['links'])
    for event in ('ev_stat', 'kl_ev'):
        gate = links[event + '.then'].split('.')[0]
        assert nodes[gate]['type'] == 'branch'
        source = dict((b, a) for a, b in body['links'])[gate + '.condition'].split('.')[0]
        assert nodes[source]['var'] == 'CompetitiveStarted'
    binds = [n for n in nodes.values() if n.get('delegate') == 'OnRoundStarted']
    assert len(binds) == 1
    assert any(n.get('func') == 'BeginCompetitive' for n in nodes.values())
    for event in ('ev_stat', 'kl_ev'):
        graph = RoundGraph(bg.gm_logic(), roster(), {'CompetitiveStarted': False})
        graph.execute(event)
        assert graph.calls == []


def test_bomb_assignment_placement_and_ready_signal_use_the_inventory_bomb_not_a_world_search():
    assignment = json.loads(bg.rule_assign())
    logic = json.loads(bg.rule_logic())
    assert not any(n.get('func') == 'GetActorOfClass' for n in assignment['nodes'] + logic['nodes'])
    nodes = {n['id']: n for n in logic['nodes']}
    inputs = {b: a for a, b in logic['links']}
    for sink in ('move.self', 'ready.TheObjectiveActor'):
        source = inputs[sink].split('.')[0]
        assert nodes[source]['type'] == 'get' and nodes[source]['var'] == 'CurrentBomb'
    retained = [n for n in logic['nodes'] if n['type'] == 'set' and n['var'] == 'CurrentBomb']
    assert {inputs[n['id'] + '.CurrentBomb'] for n in retained} == {'slot.Item', 'pollslot.Item'}


class BombGraph(RoundGraph):
    def value(self, pin):
        if pin in self.values: return self.values[pin]
        node, _ = pin.split('.', 1); n = self.nodes[node]
        if n['type'] == 'call':
            if n['func'] == 'GetPawn': return (self.arg(n, 'self') or {}).get('pawn')
            if n['func'] == 'GetComponentByClass': return (self.arg(n, 'self') or {}).get('inventory')
            if n['func'] == 'Greater_IntInt': return int(self.arg(n, 'A')) > int(self.arg(n, 'B'))
            if n['func'] == 'Add_IntInt': return int(self.arg(n, 'A')) + int(self.arg(n, 'B'))
        return super().value(pin)

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'call':
            if n['func'] == 'GetItemForSlot':
                item = self.arg(n, 'self').get('slot')
                self.values[node + '.Item'] = item
                self.values[node + '.IsValid'] = item is not None
            elif n['func'] == 'DropItem':
                self.arg(n, 'self')['slot'] = None
                self.values[node + '.WasDropped'] = True
            elif n['func'] == 'SpawnSpecialItem':
                self.arg(n, 'self')['slot'] = {'id': 'new-bomb'}
            elif n['func'] == 'OnObjectiveReady':
                self.calls.append(self.arg(n, 'TheObjectiveActor'))
        super().execute(node)


def test_immediate_bomb_is_retained_before_it_can_leave_the_inventory():
    inventory = {'slot': None}
    player = {'pawn': {'inventory': inventory}}
    rule = {'CurrentBomb': {'id': 'previous-bomb'}}
    assignment = BombGraph(bg.rule_assign(), [], rule)
    assignment.values['entry.Player'] = player
    assignment.execute('entry')
    bomb = rule['CurrentBomb']
    assert bomb == {'id': 'new-bomb'}
    inventory['slot'] = None  # manual drop/death before the first timer tick
    lookup = BombGraph(bg.rule_logic(), [], rule)
    lookup.execute('poll')
    assert lookup.calls == [bomb]


def test_deferred_bomb_is_retained_before_drop_and_the_same_actor_becomes_the_objective():
    inventory = {'slot': None}
    rule = {'CurrentBomb': None, 'Attacker': {'pawn': {'inventory': inventory}}, 'DropTries': 0}
    graph = BombGraph(bg.rule_logic(), [], rule)
    graph.execute('poll')
    assert graph.calls == []
    bomb = {'id': 'current-bomb'}
    inventory['slot'] = bomb
    graph.execute('drop')
    assert inventory['slot'] is None
    assert rule['CurrentBomb'] is bomb
    graph.execute('poll')
    assert graph.calls == [bomb]


def test_late_inventory_creation_still_registers_the_bomb_after_drop_timeout():
    bomb = {'id': 'late-bomb'}
    rule = {'CurrentBomb': None, 'Attacker': {'pawn': {'inventory': {'slot': bomb}}}, 'DropTries': 100}
    graph = BombGraph(bg.rule_logic(), [], rule)
    graph.execute('poll')
    assert rule['CurrentBomb'] is bomb
    assert graph.calls == [bomb]
