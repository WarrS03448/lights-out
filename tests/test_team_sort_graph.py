"""Contracts at the asynchronous Blueprint boundary; run with unittest discovery."""
import json
import pathlib
import sys
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from io import StringIO

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'mirror/Bodycam/Scripts'))
import bb5_graphs as bg
from tools import teamwatch


class RequestHarness:
    """Execute the generated request's branch/data wiring with mocked engine/HTTP boundaries.

    Responses are delivered separately so roster churn, loss, and delay are deterministic.
    This does not substitute for Unreal compilation or replication testing.
    """
    def __init__(self, kick_enabled=True):
        graph = json.loads(bg.team_request_logic(kick_enabled=kick_enabled))
        self.nodes = {n['id']: n for n in graph['nodes']}
        self.inputs = {b: a for a, b in graph['links']}
        self.outputs = {a: b for a, b in graph['links']}
        self.state = {'Phase': 0}
        self.time = 8.1
        self.player = {'id': '76561198000000003', 'TeamID': -1}
        self.players = [self.player]
        self.events = {}
        self.sent = []
        self.kicks = []
        self.lobby_kicks = []
        self.controllers = {}
        self.kick_controllers = []
        self.kick_result = True
        self.authority = True
        self.values = {}
        self.timers = []

    def arg(self, n, pin):
        key = n['id'] + '.' + pin
        if key in self.inputs:
            return self.value(self.inputs[key])
        return n.get('defaults', {}).get(pin)

    def value(self, pin):
        if pin in self.values:
            return self.values[pin]
        node, output = pin.split('.', 1)
        n = self.nodes[node]
        if n['type'] == 'existing':
            return self.events.get(pin)
        if n['type'] == 'self': return self.state
        if n['type'] == 'cast':
            obj = self.arg(n, 'cast_object')
            return obj if obj and obj.get('bodycam', True) else None
        if n['type'] == 'get':
            obj = self.arg(n, 'self') if 'class' in n else self.state
            return obj[n['var']] if obj is not None else None
        if n['type'] == 'call':
            f = n['func']
            a, b = self.arg(n, 'A'), self.arg(n, 'B')
            if f == 'GetGameState': return {'PlayerArray': self.players}
            if f == 'GetGameTimeInSeconds': return self.time
            if f == 'FTrunc': return int(float(a))
            if f == 'Add_IntInt': return int(a) + int(b)
            if f == 'Less_IntInt': return int(a) < int(b)
            if f == 'EqualEqual_IntInt': return int(a) == int(b)
            if f == 'EqualEqual_StrStr': return a == b
            if f == 'BooleanAND': return bool(a and b)
            if f == 'Not_PreBool': return not self.arg(n, 'A')
            if f == 'EqualEqual_ObjectObject': return a is b
            if f == 'HasAuthority': return self.authority
            if f == 'GetPlayerController':
                player = self.arg(n, 'self')
                return self.controllers.setdefault(id(player), {'PlayerState': player, 'local': False})
            if f == 'IsLocalController': return self.arg(n, 'self')['local']
            if f == 'Conv_BoolToString': return 'true' if self.arg(n, 'InBool') else 'false'
            if f == 'FindTextInLocalizationTable': return False if output == 'ReturnValue' else ''
            if f == 'MakeLiteralText': return self.arg(n, 'Value')
            if f == 'SelectText': return a if self.arg(n, 'bPickA') else b
            if f == 'IsValid': return self.arg(n, 'Object') is not None
            if f == 'Array_Contains': return any(p is self.arg(n, 'ItemToFind') for p in self.arg(n, 'TargetArray'))
            if f == 'RetrievePlatformIdAsStringFromPlayerState': return self.arg(n, 'PlayerState')['id']
            if f == 'RetrievePlatformIdFromPlayerState': return self.arg(n, 'PlayerState')['id']
            if f == 'GetPlayerTeamID': return self.arg(n, 'PlayerState')['TeamID']
            if f == 'Conv_IntToString': return str(self.arg(n, 'InInt'))
            if f == 'Concat_StrStr': return str(a) + str(b)
        raise AssertionError(('unhandled pure node', pin, n))

    def emit(self, node, pin='then'):
        destination = self.outputs.get(node + '.' + pin)
        if destination:
            self.execute(destination.split('.')[0])

    def execute(self, node):
        n = self.nodes[node]
        kind = n['type']
        if kind == 'branch':
            self.emit(node, 'then' if self.arg(n, 'condition') else 'else')
            return
        if kind == 'sequence':
            for i in range(n['count']): self.emit(node, 'then_' + str(i))
            return
        if kind == 'cast':
            obj = self.value(node + '.cast_result')
            self.emit(node, 'then' if obj is not None else 'cast_failed')
            return
        if kind == 'set':
            obj = self.arg(n, 'self') if 'class' in n else self.state
            value = self.arg(n, n['var'])
            if n['var'] in ('TeamID', 'Phase', 'Deadline'): value = int(value)
            obj[n['var']] = value
        elif kind == 'call':
            if n['func'] == 'SendAttributionEvent':
                self.sent.append({p: self.arg(n, p) for p in ('EventName', 'Platform', 'Storefront', 'UserId')})
                return  # latent, no completion path is assumed
            if n['func'] == 'KickPlayerInLobby': self.lobby_kicks.append(self.arg(n, 'TargetPlayer'))
            elif n['func'] == 'Server_KickPlayer':
                controller = self.arg(n, 'self')
                self.kick_controllers.append(controller)
                self.kicks.append(controller['PlayerState']['id'])  # command, not simulated removal
            elif n['func'] == 'K2_SetTimer':
                self.timers.append((self.arg(n, 'FunctionName'), float(self.arg(n, 'Time'))))
            elif n['func'] == 'KickPlayer':
                controller = self.arg(n, 'KickedPlayer')
                self.kick_controllers.append(controller)
                if self.kick_result: self.kicks.append(controller['PlayerState']['id'])
                self.values[node + '.ReturnValue'] = self.kick_result
            elif n['func'] not in ('RecountTeams', 'SetLifeSpan'): raise AssertionError(n)
        self.emit(node)

    def begin(self, fast=False):
        self.events.update({'begin.Player': self.player, 'begin.Host': '76561198000000001',
                            'begin.RuleComponent': {}, 'begin.FastOnly': fast})
        self.execute('begin')

    def reply(self, name, success):
        self.events[name + '_answer.bSuccess'] = success
        self.execute(name + '_answer')


class SweepHarness(RequestHarness):
    """Run the generated dispatcher; only engine actor creation and its timer are simulated."""
    def __init__(self, players):
        super().__init__()
        g = bg.G()
        bg.gm_teamset(g)
        graph = json.loads(g.json())
        self.nodes = {n['id']: n for n in graph['nodes']}
        self.inputs = {b: a for a, b in graph['links']}
        self.outputs = {a: b for a, b in graph['links']}
        self.players = players
        self.state = {'BB5BombRule': {}}
        self.values = {}
        self.requests = []
        self.time = 0
        self.execute('t_team')

    def value(self, pin):
        if pin in self.values:
            return self.values[pin]
        n = self.nodes[pin.split('.')[0]]
        if n['type'] == 'cast':
            return self.arg(n, 'cast_object')
        if n['type'] == 'self':
            return self
        if n['type'] == 'call':
            f = n['func']
            a, b = self.arg(n, 'A'), self.arg(n, 'B')
            if f == 'Array_Length': return len(self.arg(n, 'TargetArray'))
            if f == 'Array_Get': return self.arg(n, 'TargetArray')[int(self.arg(n, 'Index'))]
            if f == 'Greater_IntInt': return int(a) > int(b)
            if f == 'Less_IntInt': return int(a) < int(b)
            if f == 'Max': return max(int(a), int(b))
            if f == 'Percent_IntInt': return int(a) % int(b)
            if f == 'GetPlayerState': return self.players[int(self.arg(n, 'PlayerStateIndex'))]
            if f == 'MakeTransform': return {}
        return super().value(pin)

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'spawn':
            self.values[node + '.ReturnValue'] = RequestHarness()
            self.emit(node)
            return
        if n['type'] == 'call' and n['func'] == 'K2_SetTimer':
            self.period = float(self.arg(n, 'Time'))
            self.once_per_frame = self.arg(n, 'bMaxOncePerFrame') == 'true'
            self.next_tick = self.period
            return
        if n['type'] == 'call' and n['func'] == 'BeginRequest':
            request = self.arg(n, 'self')
            request.player = self.arg(n, 'Player')
            request.players = self.players
            request.time = self.time
            request.begin()
            self.requests.append(request)
            return
        super().execute(node)

    def advance(self, until):
        while self.next_tick <= until:
            self.time = self.next_tick
            self.execute('ev_team')
            self.next_tick += self.period
        self.time = until

    def stalled_frame(self, until):
        # UE looping timers normally catch up all elapsed expirations in the current frame.
        self.time = until
        due = 0
        while self.next_tick <= until:
            due += 1
            self.next_tick += self.period
        for _ in range(min(due, 1) if self.once_per_frame else due):
            self.execute('ev_team')


class TeamSweepGraphTests(unittest.TestCase):
    def test_game_hitch_does_not_spawn_duplicate_requests_in_one_frame(self):
        players = [{'id': str(76561198000000001 + i), 'TeamID': -1} for i in range(10)]
        h = SweepHarness(players)
        h.stalled_frame(3.2)
        self.assertEqual(len(h.requests), 1)
        h.advance(4)
        self.assertEqual(len(h.requests), 2)
        self.assertNotEqual(h.requests[0].state['SubjectId'], h.requests[1].state['SubjectId'])

    def test_empty_slots_and_a_shrinking_roster_do_not_index_missing_players(self):
        h = SweepHarness([])
        h.advance(10)
        self.assertEqual(h.requests, [])
        h.players.extend({'id': str(76561198000000001 + i), 'TeamID': -1} for i in range(10))
        h.advance(11)
        h.players[:] = h.players[:1]
        h.requests.clear()
        h.advance(16)
        self.assertEqual([r.state['SubjectId'] for r in h.requests],
                         ['76561198000000001', '76561198000000001'])

    def test_full_roster_is_checked_within_ten_seconds_without_a_request_burst(self):
        for size, deadline in ((1, 4), (2, 4), (10, 10)):
            with self.subTest(size=size):
                players = [{'id': str(76561198000000001 + i), 'TeamID': -1} for i in range(size)]
                h = SweepHarness(players)
                for cycle in (0, 1):
                    h.requests.clear()
                    for second in range(1, deadline + 1):
                        before = len(h.requests)
                        h.advance(cycle * deadline + second)
                        self.assertLessEqual(len(h.requests) - before, 1)
                    self.assertEqual(sorted(r.state['SubjectId'] for r in h.requests),
                                     [p['id'] for p in players])

    def test_newcomer_is_checked_next_sweep_and_each_request_keeps_its_target(self):
        players = [{'id': '76561198000000001', 'TeamID': -1}]
        h = SweepHarness(players)
        h.advance(4)
        newcomer = {'id': '76561198000000099', 'TeamID': -1}
        players.append(newcomer)
        h.advance(5)
        self.assertEqual(len(h.requests), 2)
        self.assertIsNot(h.requests[0], h.requests[1])
        players.reverse()
        for request in reversed(h.requests):
            request.time = 5.1  # both requests are still inside their actual response deadlines
            stranger = request.state['SubjectId'] == newcomer['id']
            request.reply('one', not stranger)
            if stranger:
                request.reply('two', False)
                request.reply('stranger', True)
        by_id = {r.state['SubjectId']: r for r in h.requests}
        self.assertEqual(by_id[newcomer['id']].kicks, [newcomer['id']])
        self.assertEqual(by_id['76561198000000001'].kicks, [])


class TeamRequestGraphTests(unittest.TestCase):
    def test_shipped_graph_uses_direct_controller_rpc_without_id_conversion_or_session_destroy(self):
        graph = json.loads(bg.team_request_logic())
        functions = {n.get('func') for n in graph['nodes']}
        self.assertIn('Server_KickPlayer', functions)
        self.assertTrue(functions.isdisjoint({'KickPlayer', 'KickPlayerInLobby', 'RetrievePlatformIdFromPlayerState'}))

    def test_diagnostic_confirmation_reports_once_without_removing_player(self):
        h = RequestHarness(kick_enabled=False); h.begin(fast=True)
        h.reply('stranger', True); h.reply('stranger', True)
        reports = [r for r in h.sent if r['EventName'] == 'ch_team_outsider_detected']
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['Storefront'], h.player['id'])
        self.assertEqual(reports[0]['Platform'], 'kick-disabled-diagnostic')
        self.assertEqual(h.kick_controllers, [])
        self.assertEqual(h.lobby_kicks, [])
        self.assertEqual(h.player['TeamID'], -1)

    def test_diagnostic_refusal_or_stale_player_cannot_report_confirmed_outsider(self):
        for mode in ('refusal', 'late', 'identity', 'departure'):
            with self.subTest(mode=mode):
                h = RequestHarness(kick_enabled=False); h.begin(fast=True)
                if mode == 'late': h.time = 12
                if mode == 'identity': h.player['id'] = '76561198000000099'
                if mode == 'departure': h.players.clear()
                h.reply('stranger', mode != 'refusal')
                self.assertFalse(any(r['EventName'] == 'ch_team_outsider_detected' for r in h.sent))

    def test_confirmed_outsider_uses_captured_remote_match_controller(self):
        h = RequestHarness(); h.begin(fast=True)
        h.players.insert(0, {'id': 'host', 'TeamID': 0})
        h.reply('stranger', True)
        self.assertEqual(len(h.kick_controllers), 1)
        self.assertIs(h.kick_controllers[0]['PlayerState'], h.player)
        self.assertEqual(h.lobby_kicks, [])
        self.assertEqual(h.sent[-1]['EventName'], 'ch_team_kick_sent')
        self.assertEqual(h.sent[-1]['Platform'], 'controller-rpc')
        self.assertIn(('VerifyKick', 1.0), h.timers)

    def test_kick_requires_authority_and_matching_remote_controller(self):
        for mode in ('no-controller', 'local-host', 'wrong-state', 'not-authority', 'wrong-class'):
            with self.subTest(mode=mode):
                h = RequestHarness(); h.begin(fast=True)
                pc = {'PlayerState': h.player, 'local': False}
                if mode == 'no-controller': pc = None
                if mode == 'local-host': pc['local'] = True
                if mode == 'wrong-state': pc['PlayerState'] = {'id': 'different'}
                if mode == 'not-authority': h.authority = False
                if mode == 'wrong-class': pc['bodycam'] = False
                h.controllers[id(h.player)] = pc
                h.reply('stranger', True)
                self.assertEqual(h.kick_controllers, [])
                self.assertEqual(h.lobby_kicks, [])

    def test_post_kick_check_reports_observation_not_rpc_success(self):
        for departed in (False, True):
            with self.subTest(departed=departed):
                h = RequestHarness(); h.begin(fast=True); h.reply('stranger', True)
                self.assertFalse(any(r['EventName'] == 'ch_team_kick_check' for r in h.sent))
                if departed: h.players.clear()
                h.execute('kick_check'); h.execute('kick_check')
                reports = [r for r in h.sent if r['EventName'] == 'ch_team_kick_check']
                self.assertEqual(len(reports), 1)
                self.assertEqual(reports[0]['Platform'], 'absent' if departed else 'present')
                self.assertEqual(reports[0]['Storefront'], h.player['id'])
                self.assertEqual(len(h.kick_controllers), 1)

    def test_no_post_kick_check_is_authorized_without_sending_command(self):
        h = RequestHarness(); h.begin(fast=True); h.reply('stranger', False)
        h.execute('kick_check')
        self.assertEqual(h.timers, [])
        self.assertFalse(any(r['EventName'] == 'ch_team_kick_check' for r in h.sent))

    def test_fast_arrival_asks_stranger_directly_and_kicks_only_on_confirmation(self):
        h = RequestHarness(); h.begin(fast=True)
        self.assertEqual([r['Platform'] for r in h.sent], ['stranger'])
        h.reply('stranger', True)
        self.assertEqual(h.kicks, [h.player['id']])
        h.reply('stranger', True)
        self.assertEqual(h.kicks, [h.player['id']])

    def test_fast_arrival_refusal_late_reply_and_identity_change_cannot_kick(self):
        for mode in ('refusal', 'late', 'identity', 'departure'):
            with self.subTest(mode=mode):
                h = RequestHarness(); h.begin(fast=True)
                if mode == 'late': h.time = 12
                if mode == 'identity': h.player['id'] = '76561198000000099'
                if mode == 'departure': h.players.clear()
                h.reply('stranger', mode != 'refusal')
                self.assertEqual(h.kicks, [])
                self.assertEqual(h.player['TeamID'], -1)

    def graph(self):
        return json.loads(bg.team_request_logic(kick_enabled=True))

    def test_callbacks_use_captured_player_not_clock_cursor(self):
        graph = self.graph()
        nodes = graph['nodes']
        self.assertFalse(any(n.get('func') == 'Array_Get' for n in nodes))
        self.assertTrue(any(n.get('var') == 'Subject' and n['type'] == 'get' for n in nodes))
        self.assertTrue(any(n.get('func') == 'Array_Contains' for n in nodes))
        self.assertTrue(any(n.get('func') == 'SetLifeSpan' for n in nodes))

    def test_only_positive_answers_reach_writes_or_kicks(self):
        graph = self.graph()
        links = graph['links']
        for name in ('one', 'two', 'stranger'):
            self.assertIn([name + '_answer.bSuccess', name + '_yes.condition'], links)
        self.assertIn(['one_yes.then', 'one_set.exec'], links)
        self.assertIn(['two_yes.then', 'two_set.exec'], links)
        self.assertIn(['stranger_yes.then', 'kick_pc_guard.exec'], links)
        self.assertNotIn(['one_yes.else', 'one_set.exec'], links)
        self.assertNotIn(['two_yes.else', 'two_set.exec'], links)

    def test_post_write_verification_is_ordered_after_recount(self):
        graph = self.graph()
        links = graph['links']
        for name in ('one', 'two'):
            self.assertIn([name + '_set.then', name + '_recount.exec'], links)
            self.assertIn([name + '_recount.then', name + '_report.exec'], links)
        reports = [n for n in graph['nodes'] if n.get('defaults', {}).get('EventName') == 'ch_team_verified']
        self.assertEqual(len(reports), 2)

    def test_gamemode_only_dispatches_requests(self):
        graph = json.loads(bg.gm_logic())
        self.assertTrue(any(n['type'] == 'spawn' and n.get('class') == bg.TEAM_REQUEST for n in graph['nodes']))
        self.assertFalse(any(n['type'] == 'set' and n.get('var') == 'TeamID' for n in graph['nodes']))

    def test_join_cannot_redirect_a_reply(self):
        h = RequestHarness(); h.begin()
        newcomer = {'id': '76561198000000099', 'TeamID': -1}
        h.players.insert(0, newcomer)
        h.reply('one', True)
        self.assertEqual(h.player['TeamID'], 0)
        self.assertEqual(newcomer['TeamID'], -1)
        self.assertEqual(h.sent[-1]['Storefront'], h.player['id'])
        self.assertEqual(h.sent[-1]['Platform'].split(':')[3:], ['0', '0'])

    def test_late_reply_cannot_write_or_kick(self):
        for action in ('one', 'stranger'):
            h = RequestHarness(); h.begin()
            if action == 'stranger':
                h.reply('one', False); h.reply('two', False)
            h.time = 12.2
            h.reply(action, True)
            self.assertEqual(h.player['TeamID'], -1)
            self.assertEqual(h.kicks, [])

    def test_departed_player_and_reconnect_reject_old_reply(self):
        h = RequestHarness(); h.begin()
        replacement = dict(h.player)
        h.players[:] = [replacement]
        h.reply('one', True)
        self.assertEqual(replacement['TeamID'], -1)
        self.assertEqual(h.player['TeamID'], -1)

    def test_failures_never_choose_team_two_or_kick(self):
        h = RequestHarness(); h.begin()
        for name in ('one', 'two', 'stranger'): h.reply(name, False)
        self.assertEqual(h.player['TeamID'], -1)
        self.assertEqual(h.kicks, [])

    def test_team_two_requires_its_own_positive_reply_and_consumes_it(self):
        h = RequestHarness(); h.begin(); h.reply('one', False); h.reply('two', True)
        self.assertEqual(h.player['TeamID'], 1)
        self.assertEqual(h.sent[-1]['EventName'], 'ch_team_verified')
        count = len(h.sent)
        h.reply('one', True); h.reply('two', True)
        self.assertEqual(len(h.sent), count)
        self.assertEqual(h.player['TeamID'], 1)

    def test_confirmed_stranger_kicks_only_captured_identity(self):
        h = RequestHarness(); h.begin(); h.reply('one', False); h.reply('two', False)
        h.players.insert(0, {'id': 'host', 'TeamID': 0})
        h.reply('stranger', True)
        self.assertEqual(h.kicks, [h.player['id']])

    def test_watcher_distinguishes_post_write_checks_from_persistence(self):
        entry = {'at': '2026-09-17T02:00:00Z', 'body': json.dumps({
            'event_name': 'ch_team_verified', 'platform': '-1:0:1:1:1',
            'storefront': '76561198000000003', 'first_session_timestamp': 'chteam-4'})}
        with patch.object(teamwatch, 'entries', return_value=[entry]):
            rows = teamwatch.rows()
        self.assertEqual(len(rows), 1)
        output = StringIO()
        with redirect_stdout(output): teamwatch.report(rows)
        self.assertIn('APPLIED', output.getvalue())
        self.assertNotIn('HELD', output.getvalue())


if __name__ == '__main__':
    unittest.main()
