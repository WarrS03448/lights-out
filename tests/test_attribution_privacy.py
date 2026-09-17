"""Inspect generated game graphs: no connection address enters attribution calls."""
import importlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mirror' / 'Bodycam' / 'Scripts'))


@pytest.mark.parametrize('module_name,function_name', [
    ('lobby_graphs', 'chjoin_logic'),
    ('lobby_graphs', 'chlobby_logic'),
    ('lobby_graphs', 'chpeek_logic'),
    ('lobby_graphs', 'chtjoin_logic'),
    ('bb5_graphs', 'combat_transport'),
    ('bb5_graphs', 'gm_logic'),
    ('bb5_graphs', 'start_request_logic'),
    ('bb5_graphs', 'team_request_logic'),
])
def test_generated_attribution_never_sends_a_connection_address(module_name, function_name):
    graph = json.loads(getattr(importlib.import_module(module_name), function_name)())
    for node in graph['nodes']:
        if node.get('func') != 'SendAttributionEvent':
            continue
        # An unconnected FString input has an empty default in the native call.
        assert node.get('defaults', {}).get('IP', '') == '', node['id']
        inputs = [source for source, target in graph['links'] if target == node['id'] + '.IP']
        if (module_name, function_name, node['id']) == ('bb5_graphs', 'gm_logic', 'final_send'):
            # Native field reused only for the frozen combat-completion value.
            assert inputs == ['frozen_FinalCombat.FinalCombat']
        else:
            assert not inputs, node['id']
