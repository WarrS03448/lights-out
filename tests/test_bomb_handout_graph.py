"""Run the Bodybomb bomb hand-out graphs against a model of the game's BP_InventoryComponent.

The model follows the decoded game Blueprint (2026-09-26), not our own graph:

* SpawnSpecialItem on an inventory that is not initialized only QUEUES the class; BroadcastInitialization
  later spawns each queued class with bShouldEquip = TRUE, whatever the caller passed.
* After BroadcastInitialization, InternalInitializeInventory swaps to the Primary weapon only when no slot
  is equipped yet.
* DropItem empties the slot and drops the item. It never changes CurrentEquippedItemSlot.

Leaving CurrentEquippedItemSlot on an empty Special slot is what broke every 1v1 bomb carrier: the next
bomb pickup waits for a drop that cannot happen, with Weapon.State.Dropping blocking the gun.
"""
import json

from test_start_gate_graph import Graph, bg

GUN, SPECIAL, PRIMARY = 'gun', 'Special', 'Primary'


class Inventory:
    def __init__(self, initialized=False):
        self.initialized = initialized
        self.slots = {PRIMARY: GUN, SPECIAL: None}
        self.current = PRIMARY if initialized else None
        self.pending = []
        self.spawns = []
        self.drops = []

    def spawn_special(self, cls, equip):
        self.spawns.append(equip)
        if not self.initialized:
            self.pending.append(cls)   # the caller's bShouldEquip is not kept
            return
        self.slots[SPECIAL] = {'class': cls}
        if equip:
            self.current = SPECIAL

    def initialize(self):
        self.initialized = True
        for cls in self.pending:
            self.spawn_special(cls, True)
        self.pending = []
        if self.current is None:
            self.current = PRIMARY

    def drop(self, slot):
        item = self.slots.get(slot)
        if item is None:
            return False
        self.slots[slot] = None
        self.drops.append(item)
        return True

    def stale(self):
        return self.current is not None and self.slots.get(self.current) is None


class BombGraph(Graph):
    def __init__(self, body, rule, inventory):
        super().__init__(body, [], rule)
        self.inv = inventory
        self.timers = []

    def value(self, pin):
        if pin in self.values: return self.values[pin]
        node, output = pin.split('.', 1)
        n = self.nodes[node]
        if n['type'] == 'get':
            return self.rule.get(n['var'])
        if n['type'] == 'call':
            f = n['func']
            if f == 'GetPawn': return (self.arg(n, 'self') or {}).get('pawn')
            if f == 'GetComponentByClass': return (self.arg(n, 'self') or {}).get('inventory')
            if f == 'Greater_IntInt': return int(self.arg(n, 'A')) > int(self.arg(n, 'B'))
            if f == 'EqualEqual_ObjectObject': return self.arg(n, 'A') is self.arg(n, 'B')
        return super().value(pin)

    def execute(self, node):
        n = self.nodes[node]
        if n['type'] == 'set':
            value = self.arg(n, n['var'])
            if value in ('true', 'false'): value = value == 'true'
            if n['var'] == 'DropTries': value = int(value)
            self.rule[n['var']] = value
            self.emit(node); return
        if n['type'] == 'cast' and not n.get('pure'):
            self.emit(node, 'then' if self.arg(n, 'cast_object') is not None else 'cast_failed'); return
        if n['type'] == 'call':
            f, inv = n['func'], self.arg(n, 'self')
            if f in ('K2_SetTimer', 'K2_ClearTimer'):
                self.timers.append((f, n['defaults']['FunctionName']))
            elif f == 'SpawnSpecialItem':
                inv.spawn_special(self.arg(n, 'ItemClass'), self.arg(n, 'bShouldEquip') == 'true')
            elif f == 'IsInitialized':
                self.values[node + '.bIsInitialized'] = inv.initialized
            elif f == 'GetItemForSlot':
                item = inv.slots.get(SPECIAL if 'Special' in self.arg(n, 'ItemSlot') else None)
                self.values.update({node + '.Item': item, node + '.IsValid': item is not None})
            elif f == 'GetCurrentEquippedItem':
                item = inv.slots.get(inv.current)
                self.values.update({node + '.Item': item, node + '.IsValid': item is not None})
            elif f == 'DropItem':
                self.values[node + '.WasDropped'] = inv.drop(SPECIAL)
            else:
                raise AssertionError(('unmodelled call', node, f))
            self.emit(node); return
        super().execute(node)


def assign(inventory):
    rule = {'DropTries': 7, 'bBombSpawned': True, 'CurrentBomb': 'last round'}
    graph = BombGraph(bg.rule_assign(), rule, inventory)
    graph.values['entry.Player'] = {'pawn': {'inventory': inventory}}
    graph.run()
    return rule, graph


def tick(rule, inventory):
    graph = BombGraph(bg.rule_logic(), rule, inventory)
    graph.execute('drop')
    return graph.timers


def test_the_released_hand_out_leaves_the_carrier_on_an_empty_current_slot():
    # What BB1 1.0.4 / BB5 1.0.30 do: SpawnSpecialItem(false) at assignment, DropItem on the next tick.
    inv = Inventory()
    inv.spawn_special('Bombe', False)
    inv.initialize()
    assert inv.current == SPECIAL           # the queue equipped it anyway, so the Primary swap was skipped
    assert inv.drop(SPECIAL)
    assert inv.stale()


def test_assignment_hands_nothing_out_and_restarts_the_poll():
    inv = Inventory()
    rule, graph = assign(inv)
    assert inv.spawns == [] and inv.pending == []
    assert graph.result is None
    assert rule['DropTries'] == 0 and rule['bBombSpawned'] is False and rule['CurrentBomb'] is None
    assert graph.timers == [('K2_ClearTimer', 'PlaceBomb'), ('K2_SetTimer', 'DropBombWhenReady')]


def test_a_carrier_whose_inventory_is_still_loading_keeps_their_weapon():
    inv = Inventory()
    rule, _ = assign(inv)
    for _ in range(3):
        assert tick(rule, inv) == []
    assert inv.spawns == [] and inv.pending == []
    inv.initialize()
    assert tick(rule, inv) == []                       # hand-out tick: spawned, not dropped
    assert inv.spawns == [False] and inv.slots[SPECIAL] is not None and inv.current == PRIMARY
    bomb = inv.slots[SPECIAL]
    assert tick(rule, inv) == [('K2_ClearTimer', 'DropBombWhenReady'), ('K2_SetTimer', 'PlaceBomb')]
    assert inv.drops == [bomb] and rule['CurrentBomb'] is bomb
    assert inv.current == PRIMARY and not inv.stale()


def test_a_ready_inventory_is_handed_the_bomb_unequipped_and_dropped_a_tick_later():
    inv = Inventory(initialized=True)
    rule, _ = assign(inv)
    assert tick(rule, inv) == []
    assert inv.spawns == [False] and inv.drops == []
    tick(rule, inv)
    assert len(inv.drops) == 1 and inv.current == PRIMARY and not inv.stale()


def test_a_bomb_in_the_carriers_hands_is_left_there():
    inv = Inventory(initialized=True)
    rule, _ = assign(inv)
    tick(rule, inv)
    inv.current = SPECIAL                              # anything that equips it before the drop tick
    assert tick(rule, inv) == [('K2_ClearTimer', 'DropBombWhenReady')]
    assert inv.drops == [] and inv.slots[SPECIAL] is not None and not inv.stale()


def test_an_inventory_that_never_loads_gets_the_stock_hand_out_once():
    inv = Inventory()
    rule, _ = assign(inv)
    for _ in range(bg.DROP_TRIES):
        assert tick(rule, inv) == []
    assert tick(rule, inv) == [('K2_ClearTimer', 'DropBombWhenReady')]
    assert inv.spawns == [True] and inv.pending == ['/Game/BodycamWeapons/Core/Blueprint/Bombe.Bombe_C']
    inv.initialize()
    assert inv.current == SPECIAL and not inv.stale()  # carried in hand, as in the stock mode, never yanked
    assert inv.drops == []


def test_a_carrier_without_a_pawn_is_never_handed_anything():
    inv = Inventory(initialized=True)
    rule, _ = assign(inv)
    rule['Attacker'] = {'pawn': None}
    for _ in range(bg.DROP_TRIES + 1):
        tick(rule, inv)
    assert inv.spawns == [] and inv.drops == []


def test_no_path_drops_the_special_slot_without_first_checking_the_carriers_hands():
    body = json.loads(bg.rule_logic())
    feeds = {b: a for a, b in body['links']}
    nodes = {n['id']: n for n in body['nodes']}
    drops = [n['id'] for n in nodes.values() if n.get('func') == 'DropItem']
    assert drops == ['dropit']
    assert feeds['dropit.exec'] == 'br_inhand.else'
    assert nodes['br_inhand']['type'] == 'branch' and feeds['br_inhand.condition'] == 'inhand.ReturnValue'
    assert {feeds['inhand.A'], feeds['inhand.B']} == {'held.Item', 'slot.Item'}
    spawns = {n['id']: n['defaults']['bShouldEquip'] for n in nodes.values() if n.get('func') == 'SpawnSpecialItem'}
    assert spawns == {'spawnnow': 'false', 'latespawn': 'true'}
    assert feeds['spawnnow.exec'] == 'br_init.then' and feeds['br_init.condition'] == 'isinit.bIsInitialized'
    assert not any(n.get('func') == 'SpawnSpecialItem' for n in json.loads(bg.rule_assign())['nodes'])


def test_the_stand_in_matches_the_game_signatures_we_call():
    # Same names, parameter order and kinds as the game's BP_InventoryComponent (decoded 2026-09-26).
    expected = {'IsInitialized': ([], [('bIsInitialized', 'bool')]),
                'GetCurrentEquippedItem': ([], [('Item', 'object'), ('IsValid', 'bool')]),
                'DropItem': ([('ItemSlot', 'struct')], [('WasDropped', 'bool')]),
                'GetItemForSlot': ([('ItemSlot', 'struct')], [('Item', 'object'), ('IsValid', 'bool'), ('ItemIndex', 'int')])}
    assert set(bg.INVENTORY_FUNCTIONS) == set(expected)
    for name, (ins, outs) in expected.items():
        nodes = {n['type']: n for n in json.loads(bg.inventory_signature(name))['nodes']}
        assert [(p['name'], p['category']) for p in nodes['entry']['params']] == ins
        assert [(p['name'], p['category']) for p in nodes['result']['params']] == outs
