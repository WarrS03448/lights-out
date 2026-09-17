"""Execute the shipped batch graph's queue/retry decisions without an Unreal process."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mirror/Bodycam/Scripts'))
import combat_transport_graphs as transport


class BatchVM:
    def __init__(self):
        graph = json.loads(transport.manager_logic())
        self.nodes = {n['id']: n for n in graph['nodes']}
        self.inputs = {b: a for a, b in graph['links']}
        self.outputs = {a: b for a, b in graph['links']}
        self.state = {'CombatQueue': [], 'CombatBatchRows': [], 'CombatBatch': '',
                      'CombatBatchCount': 0, 'CombatAttempt': 0, 'CombatSentAt': 0, 'CombatInFlight': False}
        self.values, self.sent, self.now = {}, [], 0

    def arg(self, node, pin):
        key = node['id'] + '.' + pin
        if key in self.inputs: return self.value(self.inputs[key])
        value = node.get('defaults', {}).get(pin)
        if value in ('true', 'false'): return value == 'true'
        if isinstance(value, str) and value.replace('.', '', 1).isdigit(): return float(value)
        return value

    def value(self, key):
        if key in self.values: return self.values[key]
        name, _ = key.split('.', 1); n = self.nodes[name]
        if n['type'] == 'get': return self.state[n['var']]
        if n['type'] == 'self': return self.state
        f = n.get('func'); a, b = self.arg(n, 'A'), self.arg(n, 'B')
        if f == 'GetTimeSeconds': return self.now
        if f == 'MakeTransform': return {}
        if f == 'Subtract_DoubleDouble': return a-b
        if f == 'GreaterEqual_DoubleDouble': return a >= b
        if f == 'Greater_IntInt': return a > b
        if f == 'Less_IntInt': return a < b
        if f == 'LessEqual_IntInt': return a <= b
        if f == 'EqualEqual_IntInt': return a == b
        if f == 'Add_IntInt': return a+b
        if f == 'Not_PreBool': return not a
        if f == 'BooleanAND': return bool(a and b)
        if f == 'BooleanOR': return bool(a or b)
        if f == 'Concat_StrStr': return a+b
        if f == 'Len': return len(self.arg(n, 'S'))
        raise AssertionError(('unhandled', key, n))

    def emit(self, node, pin='then'):
        target = self.outputs.get(node + '.' + pin)
        if target: self.execute(target.split('.')[0])

    def execute(self, name):
        n = self.nodes[name]; kind = n['type']
        if kind == 'branch':
            self.emit(name, 'then' if self.arg(n, 'condition') else 'else'); return
        if kind == 'foreach':
            for i, value in enumerate(list(self.arg(n, 'Array'))):
                self.values[name + '.Array Index'] = i; self.values[name + '.Array Element'] = value
                self.emit(name, 'LoopBody')
            self.emit(name, 'Completed'); return
        if kind == 'set': self.state[n['var']] = self.arg(n, n['var'])
        if kind == 'spawn': self.values[name + '.ReturnValue'] = {}
        if kind == 'call':
            f = n['func']
            if f == 'Array_Add': self.arg(n, 'TargetArray').append(self.arg(n, 'NewItem'))
            elif f == 'Array_Remove': self.arg(n, 'TargetArray').pop(int(self.arg(n, 'IndexToRemove')))
            elif f == 'Array_Clear': self.arg(n, 'TargetArray').clear()
            elif f == 'StartRequest': self.sent.append((self.arg(n, 'RequestAttempt'), self.arg(n, 'Payload')))
            else: raise AssertionError(('unexpected exec', n))
        self.emit(name)

    def reply(self, attempt, success):
        self.values.update({'reply.Attempt': attempt, 'reply.Success': success}); self.execute('reply')


def test_frozen_prefix_survives_failure_timeout_and_late_callbacks():
    vm = BatchVM(); vm.state['CombatQueue'] = ['a', 'b']; vm.execute('flush')
    first = vm.sent[-1]; assert first == (1, 'a\nb\n')
    vm.state['CombatQueue'].append('later')
    vm.now = 9; vm.execute('flush'); assert len(vm.sent) == 1
    vm.now = 10; vm.execute('flush'); assert vm.sent[-1] == (2, first[1])
    vm.reply(1, True); assert vm.state['CombatQueue'] == ['a', 'b', 'later']
    vm.reply(2, False); assert vm.state['CombatQueue'] == ['a', 'b', 'later']
    vm.execute('flush'); vm.reply(3, True)
    assert vm.state['CombatQueue'] == ['later']
    vm.execute('flush'); assert vm.sent[-1] == (4, 'later\n')
    vm.reply(3, True); assert vm.state['CombatQueue'] == ['later']
    vm.reply(4, True); assert vm.state['CombatQueue'] == []


def test_batch_has_a_bounded_contiguous_prefix():
    vm = BatchVM(); vm.state['CombatQueue'] = ['x'*800, 'y'*800, 'z'*800, 'short']
    vm.execute('flush'); assert vm.sent[-1][1] == 'x'*800 + '\n' + 'y'*800 + '\n'
    assert vm.state['CombatBatchCount'] == 2
    vm.reply(1, True); assert vm.state['CombatQueue'] == ['z'*800, 'short']
    vm.state['CombatQueue'] = ['row']*20; vm.execute('flush'); assert vm.state['CombatBatchCount'] == 16
