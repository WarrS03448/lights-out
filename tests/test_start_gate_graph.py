"""Execute the generated start predicate and roster graph without launching Unreal or Steam."""
import json
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
            return (obj or {}).get(n['var'], 0 if n['var'] == 'StartDeadline' else '')
        if n['type'] == 'call':
            f = n['func']; a, b = self.arg(n, 'A'), self.arg(n, 'B')
            if f == 'GetGameState': return {'PlayerArray': self.players}
            if f == 'StartRoster': return ''  # default output of an unexecuted impure call
            if f == 'GetGameInstance': return {'Session Name': self.key}
            if f == 'GetCurrentLobbyInfo': return {}
            if f == 'GetCurrentLobbyMembers': return self.lobby
            if f == 'Array_Length': return len(self.arg(n, 'TargetArray'))
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
        if n['type'] == 'call' and n['func'] == 'StartRoster':
            self.values[node + '.ReturnValue'] = Graph(bg.rule_fn_StartRoster(), self.players,
                                                       self.rule, self.key, self.lobby, self.now).run()
        self.emit(node)

    def run(self):
        self.execute('entry')
        if self.result is None: self.result = self.arg(self.nodes['result'], 'ReturnValue')
        return self.result


def roster(n=4):
    return [{'id': str(76561198000000001 + i), 'team': i % 2, 'owner': {'pawn': {}}} for i in range(n)]


def stamp(players):
    return str(len(players)) + '|' + ''.join(f"{p['id']}:{p['team']}:{int(p['owner'] is not None)};" for p in players)


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
