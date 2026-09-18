"""Contract tests for authored passive collector graphs (engine compile is separate)."""
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'mirror/Bodycam/Scripts'))
import combat_graphs as cg


class CombatGraphs(unittest.TestCase):
    def graph(self, fn):
        value = json.loads(fn())
        ids = {n['id'] for n in value['nodes']}
        self.assertEqual(len(ids), len(value['nodes']))
        for a, b in value['links']:
            self.assertIn(a.split('.')[0], ids)
            self.assertIn(b.split('.')[0], ids)
        return value

    def test_every_graph_is_well_formed(self):
        for fn in cg.GRAPH_EXPORTS:
            with self.subTest(fn=fn.__name__): self.graph(fn)

    def test_health_is_only_damage_source_and_effect_is_context(self):
        graph = self.graph(cg.observer_logic)
        nodes = {n['id']: n for n in graph['nodes']}
        self.assertEqual(nodes['health_bind']['delegate'], 'OnHealthChanged')
        self.assertEqual(nodes['effect']['type'], 'async')
        self.assertEqual(nodes['effect']['func'], 'WaitGameplayEffectAppliedToActor')
        self.assertIn(['effect.AsyncAction', 'retain.CombatAction'], graph['links'])
        self.assertFalse(any(a == 'effect.Source' for a, b in graph['links']))
        self.assertIn(['health.OldValue', 'loss.A'], graph['links'])
        self.assertIn(['health.NewValue', 'loss.B'], graph['links'])
        self.assertIn(['loss.ReturnValue', 'loss_only.condition'], graph['links'])
        self.assertEqual(nodes['context']['func'], 'GetEffectContext')
        self.assertEqual(nodes['source']['func'], 'EffectContextGetOriginalInstigatorActor')

    def test_lifecycle_and_loss_are_explicit(self):
        observer = self.graph(cg.observer_logic)
        nodes = observer['nodes']
        self.assertTrue(any(n.get('func') == 'EndAction' for n in nodes))
        self.assertTrue(any(n['type'] == 'removedelegate' for n in nodes))
        self.assertTrue(any(n.get('func') == 'K2_DestroyActor' for n in nodes))
        by_id = {n['id']: n for n in nodes}
        self.assertEqual(by_id['sameasc']['func'], 'EqualEqual_ObjectObject')
        self.assertEqual(by_id['samehealth']['func'], 'EqualEqual_ObjectObject')
        gm = self.graph(cg.gm_logic)
        self.assertTrue(any(n.get('var') == 'PlayerArray' for n in gm['nodes']))
        self.assertTrue(any(n.get('func') == 'Array_Length' for n in gm['nodes']))
        self.assertIn('complete=0', json.dumps(gm))
        self.assertIn('shots=0', json.dumps(gm))
        self.assertIn('objectives=0', json.dumps(gm))

    def test_delegate_has_native_float32_and_context_exec(self):
        declarations = self.graph(cg.observer_events)
        health = next(n for n in declarations['nodes'] if n['id'] == 'health')
        self.assertEqual([p['category'] for p in health['params']], ['object', 'float32', 'float32', 'object'])
        links = self.graph(cg.observer_logic)['links']
        self.assertIn(['effect.OnApplied', 'context.exec'], links)

    def test_closure_and_unknown_fields_do_not_fabricate_evidence(self):
        graph = self.graph(cg.gm_logic)
        nodes = {n['id']: n for n in graph['nodes']}
        self.assertEqual(nodes['full']['func'], 'BooleanAND')
        self.assertEqual(nodes['completeok']['func'], 'BooleanAND')
        self.assertIn(['finalcoverage.then', 'close.exec'], graph['links'])
        self.assertIn(['acceptopen.else', 'sequence' + 'set.exec'], graph['links'])
        self.assertIn('terminal=1', json.dumps(graph))
        self.assertEqual(nodes['roundbind']['delegate'], 'OnRoundStarted')
        self.assertIn(['live_event.OutputDelegate', 'roundbind.Delegate'], graph['links'])
        self.assertIn(['boundary.CombatBoundary', 'islive.A'], graph['links'])
        self.assertIn(['stopreconcile.then', 'endgs.exec'], graph['links'])
        observer = self.graph(cg.observer_logic)
        self.assertTrue(any(n.get('func') == 'SelectString' for n in observer['nodes']))
        self.assertFalse(any(n.get('func') == 'GetObjectName' for n in observer['nodes']))

    def test_auth_transport_and_game_mode_have_no_collector_state(self):
        import bb5_graphs as bg
        import combat_transport_graphs as transport
        graph = self.graph(transport.request_logic)
        nodes = {n['id']: n for n in graph['nodes']}
        self.assertEqual(nodes['http']['defaults']['EventName'], 'ch_combat_batch')
        self.assertTrue(nodes['http']['defaults']['URL'].endswith('/api/match-report/batch'))
        self.assertNotIn('BearerToken', nodes['http']['defaults'])
        self.assertIn(['http_auth_token.Search String', 'http_auth_prefix.A'], graph['links'])
        self.assertEqual(nodes['http_auth_prefix']['defaults']['B'], '.')
        self.assertIn(['http_auth_epoch.HostEpoch', 'http_auth_epoch_s.InInt'], graph['links'])
        self.assertIn(['http_auth_epoch_s.ReturnValue', 'http_auth_bearer.B'], graph['links'])
        self.assertIn(['http_auth_bearer.ReturnValue', 'http.BearerToken'], graph['links'])
        self.assertIn(['delegate.OutputDelegate', 'http.OnResponse'], graph['links'])
        gm = self.graph(bg.gm_logic)
        self.assertTrue(any(n.get('class') == cg.GM and n['type'] == 'spawn' for n in gm['nodes']))
        self.assertFalse(any(n.get('var', '').startswith('Combat') and not n.get('class') for n in gm['nodes']))

    def test_observer_only(self):
        forbidden = {'ApplyDamage', 'ApplyPointDamage', 'ApplyGameplayEffectToSelf',
                     'Revive', 'InitHealth', 'Equip', 'ConsumeBullet', 'Fire'}
        for fn in cg.GRAPH_EXPORTS:
            for n in self.graph(fn)['nodes']:
                self.assertNotIn(n.get('func'), forbidden)


if __name__ == '__main__': unittest.main()
