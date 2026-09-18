"""Execute terminal restoration paths without a game process."""
import json
import pytest
from test_start_gate_graph import Graph
import migration_graphs as migration


class RestoredMatch(Graph):
    def __init__(self, phase, pending):
        g=migration.G(); migration.logic(g)
        super().__init__(g.json(), [], {'FinalPending':pending, 'CombatManager':{}})
        self.phase=phase
        self.closed=False

    def value(self, pin):
        node=self.nodes[pin.split('.')[0]]
        if node.get('func')=='GetCurrentPhase':return self.phase
        if node.get('func')=='GetTagName':return self.arg(node,'GameplayTag')
        if node.get('func')=='Conv_NameToString':return self.arg(node,'InName')
        return super().value(pin)

    def execute(self, node):
        if self.nodes[node].get('func')=='CombatClose':self.closed=True
        super().execute(node)


@pytest.mark.parametrize('phase,pending,expected',[
    ('Game.Phase.EndMatch',False,True),
    ('Game.Phase.RoundWarmup',True,True),
    ('Game.Phase.StartRound',False,False),
])
def test_restored_terminal_world_closes_the_fresh_collector_without_a_new_round(phase,pending,expected):
    graph=RestoredMatch(phase,pending)
    graph.execute('migration_finish')
    assert graph.closed is expected
    assert bool(graph.rule['FinalPending']) is expected


def test_terminal_recovery_retries_only_after_migration_authorization():
    g=migration.G();migration.logic(g)
    body=json.loads(g.json())
    links=dict(body['links'])
    assert links['migration_retry_advertise.then']=='migration_retry_publish.exec'
    assert links['migration_retry_publish.then']=='migration_retry_finish.exec'
    assert 'migration_retry_advertise.else' not in links
    inputs={b:a for a,b in body['links']}
    assert inputs['migration_publish_ready.A']=='migration_ready.ReturnValue'
    assert inputs['migration_publish_ready.B']=='migration_has_epoch.ReturnValue'
