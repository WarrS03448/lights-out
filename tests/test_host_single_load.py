"""Execute the generated host flow; engine/network boundaries are controlled here."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mirror/Bodycam/Scripts'))
import lobby_graphs as lg

RANGE = '/Game/Map/LobbyHost/LobbyHost'


class Host:
    def __init__(self, standalone=True, server=True, level='LobbyHost', state=0):
        graph = json.loads(lg.chlobby_logic())
        self.nodes = {n['id']: n for n in graph['nodes']}
        self.inputs = {b: a for a, b in graph['links']}
        self.outputs = {a: b for a, b in graph['links']}
        self.gi = {'Selected Level Name': lg.HOST_MAP_SHORT, lg.LAP_PROP: state}
        self.standalone, self.server, self.level = standalone, server, level
        self.values, self.requests, self.travels, self.parents = {}, [], [], []

    def arg(self, n, name):
        pin = n['id'] + '.' + name
        return self.value(self.inputs[pin]) if pin in self.inputs else n.get('defaults', {}).get(name)

    def value(self, pin):
        if pin in self.values: return self.values[pin]
        node, output = pin.split('.', 1)
        n = self.nodes[node]
        if n['type'] == 'cast': return self.arg(n, 'cast_object')
        if n['type'] == 'get': return self.arg(n, 'self').get(n['var'], 0)
        f = n.get('func'); a, b = self.arg(n, 'A'), self.arg(n, 'B')
        if f == 'GetGameInstance': return self.gi
        if f == 'MakeLiteralName': return self.arg(n, 'Value')
        if f == 'Conv_NameToString': return self.arg(n, 'InName')
        if f == 'IsStandalone': return self.standalone
        if f == 'IsServer': return self.server
        if f == 'Not_PreBool': return not a
        if f == 'BooleanAND': return bool(a and b)
        if f == 'EqualEqual_IntInt': return int(a) == int(b)
        if f == 'EqualEqual_StrStr': return a == b
        if f == 'IsValid': return self.arg(n, 'Object') is not None
        raise AssertionError(('unsupported value', pin, n))

    def emit(self, node, pin='then'):
        target = self.outputs.get(node + '.' + pin)
        if target: self.execute(target.split('.')[0])

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'branch':
            self.emit(node, 'then' if self.arg(n, 'condition') else 'else'); return
        if n['type'] == 'sequence':
            for i in range(n['count']): self.emit(node, 'then_' + str(i))
            return
        if n['type'] == 'set': self.arg(n, 'self')[n['var']] = self.arg(n, n['var'])
        if n['type'] == 'callparent': self.parents.append(dict(self.gi))
        if n.get('func') == 'GetCurrentLevelName': self.values[node + '.ReturnValue'] = self.level
        if n.get('func') == 'SendAttributionEvent':
            if self.arg(n, 'URL') == lg.PROBE_SLOW_URL:
                self.requests.append((dict(self.gi), self.arg(n, 'BearerToken')))
            return  # no automatic response: timeout must be safe
        if n.get('func') == 'OpenLevel': self.travels.append(self.arg(n, 'LevelName'))
        self.emit(node)

    def event(self, name, success=True):
        node = next(n['id'] for n in self.nodes.values() if n.get('name') == name)
        self.values[node + '.bSuccess'] = success
        self.execute(node)


def test_first_boot_leaves_native_hosting_in_charge_and_cannot_load_match():
    h = Host()
    h.event('ReceiveBeginPlay')
    assert h.parents[0]['Selected Level Name'] == RANGE
    assert h.parents[0]['Session Name'] == lg.JOIN_TOKEN
    assert h.parents[0]['Search String'] == lg.REPORT_TOKEN
    assert not h.requests and not h.travels


def test_native_listen_range_requests_once_and_loads_match_once():
    h = Host(standalone=False)
    h.event('ReceiveBeginPlay')
    assert not h.parents, 'The native listen range must not restart stock hosting'
    assert len(h.requests) == 1
    assert int(h.requests[0][0][lg.LAP_PROP]) == 1
    assert h.requests[0][1] == lg.REPORT_TOKEN
    assert not h.travels
    h.event('OnHostDelay')
    h.event('OnHostDelay')
    assert h.travels == [lg.HOST_MAP]
    assert int(h.gi[lg.LAP_PROP]) == 2
    h.event('ReceiveBeginPlay')  # returning to the range must not rearm
    assert len(h.requests) == 1 and len(h.travels) == 1
    assert all(p['Selected Level Name'] == RANGE for p in h.parents)


def test_denied_or_missing_reply_never_loads_and_initial_native_parent_runs_once():
    h = Host()
    h.event('ReceiveBeginPlay')
    h.standalone = False  # native hosting-success opened the range as listen
    h.event('ReceiveBeginPlay')
    assert len(h.parents) == 1 and not h.travels
    h.event('OnHostDelay', success=False)
    assert not h.travels


def test_native_range_completion_and_return_cannot_schedule_another_host_callback():
    h = Host()
    h.event('ReceiveBeginPlay')
    assert len(h.parents) == 1
    # Each stock parent call schedules another native hosting-success OpenLevel.
    # This models the real first run: repeatedly calling parent in the listen
    # range made a new range world every three seconds, even after match travel.
    h.standalone = False
    h.event('ReceiveBeginPlay')
    assert len(h.parents) == 1
    h.event('OnHostDelay')
    assert h.travels == [lg.HOST_MAP]
    h.event('ReceiveBeginPlay')
    h.standalone = True
    h.event('ReceiveBeginPlay')
    assert len(h.parents) == 1 and len(h.requests) == 1 and len(h.travels) == 1


def test_client_wrong_map_and_late_reply_cannot_load():
    for kwargs in ({'server': False}, {'level': 'BB5_Hospital'}, {'state': 2}):
        h = Host(standalone=False, **kwargs)
        h.event('ReceiveBeginPlay')
        h.event('OnHostDelay')
        assert not h.requests and not h.travels
    h = Host(standalone=False)
    h.event('ReceiveBeginPlay')
    h.level = 'BB5_Hospital'
    h.event('OnHostDelay')
    assert not h.travels
