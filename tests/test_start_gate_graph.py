"""Execute the generated start predicate and roster graph without launching Unreal or Steam."""
import json
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mirror/Bodycam/Scripts'))
import bb5_graphs as bg


class Graph:
    def __init__(self, body, players, rule, key='chm-test', lobby=0, now=10):
        self.nodes = {n['id']: n for n in json.loads(body)['nodes']}
        self.inputs = {b: a for a, b in json.loads(body)['links']}
        self.outputs = {a: b for a, b in json.loads(body)['links']}
        self.players, self.rule, self.key, self.lobby, self.now = players, rule, key, lobby, now
        self.values = {}
        self.result = None

    def arg(self, n, name):
        pin = n['id'] + '.' + name
        return self.value(self.inputs[pin]) if pin in self.inputs else n.get('defaults', {}).get(name)

    def value(self, pin):
        if pin in self.values:
            return self.values[pin]
        node, output = pin.split('.', 1)
        n = self.nodes[node]
        if n['type'] == 'cast': return self.arg(n, 'cast_object')
        if n['type'] == 'get':
            obj = self.arg(n, 'self') if 'class' in n else self.rule
            if n['var'] == 'BB5BombRule': return self.rule
            return (obj or {}).get(n['var'], [] if n['var'] in ('StartApprovedHumans', 'StartCurrentHumans') else 0 if n['var'] == 'StartDeadline' else '')
        if n['type'] == 'call':
            f = n['func']; a, b = self.arg(n, 'A'), self.arg(n, 'B')
            if f == 'GetGameState': return {'PlayerArray': self.players}
            if f == 'StartRoster': return ''  # default output of an unexecuted impure call
            if f == 'StartHumansPresent': return False
            if f == 'GetGameInstance': return {'Session Name': self.key}
            if f == 'GetCurrentLobbyInfo': return {}
            if f == 'GetCurrentLobbyMembers': return self.lobby
            if f == 'Array_Length': return len(self.arg(n, 'TargetArray'))
            if f == 'Array_Contains': return self.arg(n, 'ItemToFind') in self.arg(n, 'TargetArray')
            if f == 'EqualEqual_IntInt': return int(a) == int(b)
            if f == 'NotEqual_StrStr': return a != b
            if f == 'Len': return len(self.arg(n, 'S'))
            if f == 'IsNumeric': return self.arg(n, 'SourceString').isnumeric()
            if f == 'Add_IntInt': return int(a) + int(b)
            if f == 'GetGameTimeInSeconds': return self.now
            if f == 'FTrunc': return int(float(a))
            if f == 'GreaterEqual_IntInt': return int(a) >= int(b)
            if f == 'Less_IntInt': return int(a) < int(b)
            if f == 'BooleanAND': return bool(a and b)
            if f == 'BooleanOR': return bool(a or b)
            if f == 'Not_PreBool': return not a
            if f == 'EqualEqual_StrStr': return a == b
            if f == 'StartsWith': return self.arg(n, 'SourceString').startswith(self.arg(n, 'InPrefix'))
            if f == 'SelectBool': return bool(a if self.arg(n, 'bPickA') else b)
            if f == 'SelectString': return a if self.arg(n, 'bPickA') else b
            if f == 'IsValid': return self.arg(n, 'Object') is not None
            if f == 'GetOwner': return self.arg(n, 'self').get('owner')
            if f == 'K2_GetPawn': return (self.arg(n, 'self') or {}).get('pawn')
            if f == 'RetrievePlatformIdAsStringFromPlayerState': return self.arg(n, 'PlayerState')['id']
            if f == 'GetPlayerTeamID': return self.arg(n, 'PlayerState')['team']
            if f == 'Conv_IntToString': return str(self.arg(n, 'InInt'))
            if f == 'Concat_StrStr': return str(a) + str(b)
        raise AssertionError(('unhandled value', pin, n))

    def emit(self, node, pin='then'):
        target = self.outputs.get(node + '.' + pin)
        if target: self.execute(target.split('.')[0])

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'branch':
            self.emit(node, 'then' if self.arg(n, 'condition') else 'else'); return
        if n['type'] == 'foreach':
            for player in self.arg(n, 'Array'):
                self.values[node + '.Array Element'] = player
                self.emit(node, 'LoopBody')
            self.emit(node, 'Completed'); return
        if n['type'] == 'set':
            self.rule[n['var']] = self.arg(n, n['var'])
        if n['type'] == 'result':
            self.result = self.arg(n, 'ReturnValue'); return
        if n['type'] == 'call':
            f = n['func']
            if f in ('StartRoster', 'PresenceRoster', 'StartHumansPresent', 'CaptureStartHumans'):
                self.values[node + '.ReturnValue'] = Graph(bg.RULE_FN_BODIES[f](), self.players,
                                                           self.rule, self.key, self.lobby, self.now).run()
            if f == 'Array_Clear': self.arg(n, 'TargetArray').clear()
            if f == 'Array_AddUnique':
                arr, item = self.arg(n, 'TargetArray'), self.arg(n, 'NewItem')
                if item not in arr: arr.append(item)
        self.emit(node)

    def run(self):
        self.execute('entry')
        if self.result is None and 'result' in self.nodes: self.result = self.arg(self.nodes['result'], 'ReturnValue')
        return self.result


def roster(n=4):
    return [{'id': str(76561198000000001 + i), 'team': i % 2, 'owner': {'pawn': {}}} for i in range(n)]


def stamp(players):
    return str(len(players)) + '|' + ''.join(f"{p['id']}:{p['team']}:{int((p['owner'] or {}).get('pawn') is not None)};" for p in players)


def test_ranked_game_waits_for_authorization_even_when_steam_lobby_looks_full():
    for fn in (bg.gm_min_players, bg.gm_player_full):
        assert Graph(fn(), roster(3), {}, lobby=3).run() is False


def test_exact_authorized_roster_passes_and_any_departure_team_change_or_expiry_closes_gate():
    for fn in (bg.gm_min_players, bg.gm_player_full):
        players = roster()
        approved = {'StartApprovedRoster': stamp(players), 'StartApprovedMatch': 'chm-test', 'StartDeadline': 12}
        assert Graph(fn(), players, dict(approved), lobby=4).run() is True
        assert Graph(fn(), players[:-1], dict(approved), lobby=3).run() is False
        changed = [dict(p) for p in players]; changed[0]['team'] = 1
        assert Graph(fn(), changed, dict(approved), lobby=4).run() is False
        changed = [dict(p) for p in players]; changed[0]['owner'] = {'pawn': None}
        assert Graph(fn(), changed, dict(approved), lobby=4).run() is False
        changed = [dict(p) for p in players]; changed[0]['owner'] = None
        assert Graph(fn(), changed, dict(approved), lobby=4).run() is False
        assert Graph(fn(), players, dict(approved), key='chm-next', lobby=4).run() is False
        assert Graph(fn(), players, dict(approved), lobby=4, now=12).run() is False


def test_non_competitive_bodybomb_keeps_its_existing_waiting_rule():
    assert Graph(bg.gm_min_players(), roster(2), {}, key='Private game', lobby=2).run() is True
    assert Graph(bg.gm_min_players(), roster(1), {}, key='Private game', lobby=1).run() is False


def test_started_match_does_not_reopen_arrival_gate_for_round_respawns():
    for fn in (bg.gm_min_players, bg.gm_player_full):
        for human_count in (1, 4, 10):
            players = roster(human_count)
            if human_count == 1:
                players += [{'id': '', 'team': i % 2, 'owner': {'pawn': {}}} for i in range(9)]
            approved = {'StartApprovedRoster': stamp(players), 'StartApprovedMatch': 'chm-test',
                        'StartDeadline': 12, 'CompetitiveStarted': True,
                        'StartApprovedHumans': [p['id'] for p in players if p['id']], 'StartCurrentHumans': []}
            # Native destroys dead pawns between rounds. This is not a new match arrival.
            for p in players:
                p['owner']['pawn'] = None
            assert Graph(fn(), players, dict(approved), lobby=human_count, now=100).run() is True
            assert Graph(fn(), list(reversed(players)), copy.deepcopy(approved), now=100).run() is True
            # Bot removal/replacement does not remove an assigned human.
            assert Graph(fn(), players[:human_count], copy.deepcopy(approved), now=100).run() is True
            assert Graph(fn(), players[1:], copy.deepcopy(approved), now=100).run() is False
            changed = copy.deepcopy(players); changed[0]['owner'] = None
            assert Graph(fn(), changed, copy.deepcopy(approved), now=100).run() is False
            changed = copy.deepcopy(players); changed[0]['id'] = '76561198999999999'
            assert Graph(fn(), changed, copy.deepcopy(approved), now=100).run() is False


def test_round_start_latch_never_opens_an_unapproved_or_different_match():
    for fn in (bg.gm_min_players, bg.gm_player_full):
        for approved_key in ('', 'chm-old'):
            rule = {'CompetitiveStarted': True, 'StartApprovedMatch': approved_key, 'StartDeadline': 0}
            assert Graph(fn(), roster(10), rule, now=100).run() is False
        assert Graph(fn(), roster(10), None, now=100).run() is False
        rule = {'CompetitiveStarted': False, 'StartApprovedMatch': 'chm-test', 'StartDeadline': 0}
        assert Graph(fn(), roster(10), rule, now=100).run() is False


def test_capture_remembers_only_connected_humans_and_outsiders_do_not_freeze_them():
    players = roster()
    rule = {'StartApprovedHumans': [], 'StartCurrentHumans': []}
    Graph(bg.rule_fn_CaptureStartHumans(), players + [{'id': '', 'team': 0, 'owner': {'pawn': None}}], rule).run()
    assert rule['StartApprovedHumans'] == [p['id'] for p in players]
    rule.update(CompetitiveStarted=True, StartApprovedMatch='chm-test')
    stranger = {'id': '76561198999999999', 'team': 0, 'owner': {'pawn': {}}}
    for fn in (bg.gm_min_players, bg.gm_player_full):
        assert Graph(fn(), players + [stranger], copy.deepcopy(rule), now=100).run() is True
        assert Graph(fn(), players[:-1] + [stranger], copy.deepcopy(rule), now=100).run() is False


def test_a_departure_stays_closed_until_server_clears_its_window():
    players=roster()
    rule={'StartApprovedHumans':[p['id'] for p in players], 'StartCurrentHumans':[],
          'CompetitiveStarted':True,'StartApprovedMatch':'chm-test'}
    assert Graph(bg.gm_min_players(),players[:-1],rule).run() is False
    assert Graph(bg.gm_min_players(),players,rule).run() is False
    rule['PresenceDirty']=False
    assert Graph(bg.gm_min_players(),players,rule).run() is True


def test_presence_wire_ignores_death_and_bot_sentinels_but_excludes_disconnected_controllers():
    players=roster(2)
    players[0]['owner']['pawn']=None
    bots=[{'id':sid,'team':0,'owner':{'pawn':None}} for sid in ('','BOT_0','0','invalid_platform')]
    rule={'PresenceCount':0,'PresenceScratch':'','StartApprovedHumans':[]}
    Graph(bg.rule_fn_CaptureStartHumans(),players+bots,rule).run()
    assert rule['StartApprovedHumans']==[p['id'] for p in players]
    assert Graph(bg.rule_fn_PresenceRoster(),players+bots,rule).run()==f"2|{players[0]['id']};{players[1]['id']};"
    players[1]['owner']=None
    assert Graph(bg.rule_fn_PresenceRoster(),players+bots,rule).run()==f"1|{players[0]['id']};"
