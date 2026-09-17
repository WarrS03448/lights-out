'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const ledger = require('../combat-ledger.cjs');

test('merging a replay or expanded explosion preserves sanction ownership and one incident', () => {
  const old = { revision: 1, incidents: [{ id: 'm:a', matchId: 'm', at: 1000, damage: 100, sanctionId: 's' }] };
  const next = ledger.merge(old, [{ id: 'm:a', matchId: 'm', at: 1000, damage: 200 }], 2000);
  assert.equal(next.incidents.length, 1);
  assert.equal(next.incidents[0].damage, 200);
  assert.equal(next.incidents[0].sanctionId, 's');
  assert.equal(old.incidents[0].damage, 100);
});

test('expired friendly fire evidence is removed while recent incidents survive', () => {
  const now = 90 * 86400000;
  const next = ledger.merge({ revision: 0, incidents: [
    { id: 'old', at: 1 }, { id: 'new', at: now - 60000 },
  ] }, [], now);
  assert.deepEqual(next.incidents.map(x => x.id), ['new']);
});

test('a restored stale snapshot cannot revalidate or shrink durable incident evidence', () => {
  const old = {revision:1,incidents:[{id:'i',startedAt:1000,endedAt:2000,damage:90,
    validated:true,sanctionEligible:false,eventSeqs:[1,2,3],victimIds:['a','b']}]};
  const stale = {id:'i',startedAt:1000,endedAt:1000,damage:30,validated:true,
    sanctionEligible:true,eventSeqs:[1],victimIds:['a']};
  const next=ledger.merge(old,[stale],3000);
  assert.equal(next.incidents[0].sanctionEligible,false);
  assert.equal(next.incidents[0].damage,90);
  assert.deepEqual(next.incidents[0].eventSeqs,[1,2,3]);
});

test('receipt marks only decision evidence as used and preserves an exact reason', () => {
  const state = { revision: 3, incidents: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] };
  const out = ledger.consume(state, { decisionId: 'd', incidentIds: ['a', 'b'], ruleVersion: 1 }, 9000);
  assert.equal(out.incidents[0].sanctionId, 'd');
  assert.equal(out.incidents[1].sanctionId, 'd');
  assert.equal(out.incidents[2].sanctionId, undefined);
  assert.equal(state.incidents[0].sanctionId, undefined);
});

test('strict persistence errors are surfaced for retry instead of granting a penalty', async () => {
  let calls = 0;
  const store = async (_cmd, options) => { assert.equal(options.strict, true); calls++; throw new Error('offline'); };
  await assert.rejects(ledger.load(store, 'x:', 'p'), /offline/);
  assert.equal(calls, 1);
});

test('CAS conflicts are retryable and do not look like committed sanctions', async () => {
  const store = async () => ['conflict'];
  await assert.rejects(ledger.commit(store, 'x:', 'p', { expected: 1, history: { revision: 2 } }), e => e.conflict === true);
});

test('an uncertain acknowledgement replay returns the saved receipt', async () => {
  const receipt = { decisionId: 'd', reason: 'team_kill', rr: 15 };
  const store = async () => ['replayed', JSON.stringify({ revision: 2, incidents: [] }), JSON.stringify(receipt)];
  const out = await ledger.commit(store, 'x:', 'p', { expected: 1, history: { revision: 2 }, receipt });
  assert.equal(out.replayed, true);
  assert.deepEqual(out.receipt, receipt);
});

test('public-key targeting is fixed by server arguments and never evidence fields', async () => {
  let command;
  const store = async cmd => { command = cmd; return ['committed', JSON.stringify({ revision: 1 }), '']; };
  await ledger.commit(store, 'hub:', '76561198000000001', { expected: 0, history: { revision: 1 } });
  assert.equal(command[0], 'EVAL');
  assert.equal(command[2], '5');
  assert.equal(command[3], 'hub:combat:history:76561198000000001');
  assert.equal(command[4], 'hub:rating:76561198000000001');
  assert.equal(command[5], 'hub:penalty:76561198000000001');
});
