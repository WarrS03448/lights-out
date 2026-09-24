"""Execute the new-arrival dispatcher graph; engine objects, timers and HTTP are boundaries."""
import json
import unittest

from test_team_sort_graph import RequestHarness, SweepHarness, bg


def player(index=1, steam_id=None, path=None):
    return {'id': steam_id if steam_id is not None else str(76561198000000000 + index),
            'path': path or f'/World/PlayerState_{index}', 'TeamID': -1}


class ArrivalHarness(SweepHarness):
    def __init__(self, players, key='chm-test', token='a' * 64):
        RequestHarness.__init__(self)
        g = bg.G(); bg.gm_arrival_guard(g)
        graph = json.loads(g.json())
        self.nodes = {n['id']: n for n in graph['nodes']}
        self.inputs = {b: a for a, b in graph['links']}
        self.outputs = {a: b for a, b in graph['links']}
        self.rule = {'GuardSeen': '', 'GuardNext': '', 'GuardDispatched': False}
        self.state = {'BB5BombRule': self.rule}
        self.gi = {'Session Name': key, bg.REPORT_TOKEN_PROP: token}
        self.players, self.values, self.requests = players, {}, []
        self.time, self.spawn_failures, self.spawn_attempts = 0, 0, 0
        self.execute('t_arrival')

    def value(self, pin):
        if pin in self.values: return self.values[pin]
        n = self.nodes[pin.split('.')[0]]
        if n['type'] == 'call':
            f = n['func']
            if f == 'GetGameInstance': return self.gi
            if f == 'Len': return len(self.arg(n, 'S'))
            if f == 'GreaterEqual_IntInt': return int(self.arg(n, 'A')) >= int(self.arg(n, 'B'))
            if f == 'Left': return self.arg(n, 'SourceString')[:int(self.arg(n, 'Count'))]
            if f == 'IsNumeric': return self.arg(n, 'SourceString').isdigit()
            if f == 'GetPathName': return self.arg(n, 'Object')['path']
            if f == 'Contains': return self.arg(n, 'Substring') in self.arg(n, 'SearchIn')
            if f == 'StartsWith': return self.arg(n, 'SourceString').startswith(self.arg(n, 'InPrefix'))
        return super().value(pin)

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'set' and n['var'] == 'GuardDispatched':
            self.arg(n, 'self')['GuardDispatched'] = self.arg(n, 'GuardDispatched') == 'true'
            self.emit(node); return
        if n['type'] == 'foreach':
            for subject in list(self.arg(n, 'Array')):
                self.values[node + '.Array Element'] = subject
                self.emit(node, 'LoopBody')
            self.emit(node, 'Completed'); return
        if n['type'] == 'spawn':
            self.spawn_attempts += 1
            if self.spawn_failures:
                self.spawn_failures -= 1
                self.values[node + '.ReturnValue'] = None
                self.emit(node); return
        if n['type'] == 'call' and n['func'] == 'BeginRequest':
            request = self.arg(n, 'self')
            request.player, request.players, request.time = self.arg(n, 'Player'), self.players, self.time
            request.begin(fast=self.arg(n, 'FastOnly') == 'true')
            self.requests.append(request)
            self.emit(node); return
        super().execute(node)

    def advance(self, until):
        while self.next_tick <= until:
            self.time = self.next_tick
            self.execute('ag_tick')
            self.next_tick = round(self.next_tick + self.period, 6)
        self.time = until


class ArrivalGuardTests(unittest.TestCase):
    def test_seventeen_characters_of_non_numeric_identity_are_not_cached_or_sent(self):
        h = ArrivalHarness([player(1, steam_id='x' * 17)])
        h.advance(.2)
        self.assertEqual(h.requests, []); self.assertEqual(h.rule['GuardSeen'], '')
        h.players[0]['id'] = '76561198000000001'; h.advance(.3)
        self.assertEqual(len(h.requests), 1)

    def test_newcomer_gets_direct_confirmation_without_waiting_for_regular_sweep(self):
        h = ArrivalHarness([player()]); h.advance(.11)
        intruder = player(2); h.players.append(intruder); h.advance(.21)
        self.assertEqual(len(h.requests), 2)
        request = h.requests[-1]
        self.assertLess(request.time, 1)
        self.assertEqual([x['Platform'] for x in request.sent], ['stranger'])
        request.time = .35; request.reply('stranger', True)
        self.assertEqual(request.kicks, [intruder['id']])

    def test_initial_roster_is_spread_over_one_second_then_known_players_are_quiet(self):
        h = ArrivalHarness([player(i) for i in range(1, 11)])
        h.advance(1.01)
        self.assertEqual(len(h.requests), 10)
        self.assertEqual(len({r.time for r in h.requests}), 10)
        h.advance(10)
        self.assertEqual(len(h.requests), 10)
        self.assertLess(len(h.rule['GuardSeen']), 1000)

    def test_waits_for_steam_identity_and_ranked_reporting_context(self):
        h = ArrivalHarness([player(1, steam_id='')], key='Private game', token='')
        h.advance(1)
        self.assertEqual(h.requests, []); self.assertEqual(h.rule['GuardSeen'], '')
        h.gi['Session Name'] = 'chm-test'; h.advance(1.1)
        self.assertEqual(h.requests, [])
        h.gi[bg.REPORT_TOKEN_PROP] = 'a' * 64; h.advance(1.2)
        self.assertEqual(h.requests, [])
        h.players[0]['id'] = '76561198000000001'; h.advance(1.3)
        self.assertEqual(len(h.requests), 1)

    def test_reconnect_identity_change_and_departure_do_not_reuse_attempt_cache(self):
        h = ArrivalHarness([player()]); h.advance(.1)
        h.players[:] = [player(path='/World/PlayerState_reconnect')]; h.advance(.2)
        h.players[0]['id'] = '76561198000000002'; h.advance(.3)
        self.assertEqual(len(h.requests), 3)
        self.assertNotIn('/World/PlayerState_1|', h.rule['GuardSeen'])
        saved = h.players.pop(); h.advance(.4)
        self.assertEqual(h.rule['GuardSeen'], '')
        h.players.append(saved); h.advance(.5)
        self.assertEqual(len(h.requests), 4)

    def test_failed_actor_creation_is_retried_without_being_cached(self):
        h = ArrivalHarness([player()]); h.spawn_failures = 1; h.advance(.1)
        self.assertEqual(h.requests, []); self.assertEqual(h.rule['GuardSeen'], '')
        h.advance(.2)
        self.assertEqual(len(h.requests), 1); self.assertEqual(h.spawn_attempts, 2)


if __name__ == '__main__':
    unittest.main()
