// Run: node --test server/scripts/test-penalty-pause.cjs
const assert = require('node:assert/strict');
const { test } = require('node:test');
const live = require('../live.cjs');
const id = '76561198000000001';
function fixture(t, paused) {
  const old = process.env.COMP_QUEUE_PENALTIES_PAUSED;
  process.env.COMP_QUEUE_PENALTIES_PAUSED = paused ? '1' : '0';
  t.after(() => { if (old === undefined) delete process.env.COMP_QUEUE_PENALTIES_PAUSED;
    else process.env.COMP_QUEUE_PENALTIES_PAUSED = old; });
  return live.create({})._internals;
}
test('pause suppresses new bans, strike counts and RR deductions', t => {
  const L = fixture(t, true);
  assert.equal(L.applyNoShow(id), null);
  assert.equal(L.penalties.size, 0);
  assert.equal(L.ratings.size, 0);
});
test('existing bans are bypassed without deleting records and resume with enforcement', async t => {
  const L = fixture(t, true);
  const record = { until: Date.now() + 60000, count: 2, elo: 50, last: Date.now() };
  L.penalties.set(id, record);
  assert.equal(await L.loadPenalty(id), null);
  assert.deepEqual(L.penalties.get(id), record);
  process.env.COMP_QUEUE_PENALTIES_PAUSED = '0';
  assert.deepEqual(await L.loadPenalty(id), record);
  assert.equal(L.applyNoShow(id).count, 3);
});
test('no-show timeout still releases the match without charging anyone', t => {
  const L = fixture(t, true);
  const match = { id: 'pause-test', state: 'connecting', host: id,
    players: [{ steam_id: id, accepted: true, connected: false }],
    teams: { 1: [id], 2: [] }, left: [], timer: null, deadline: 0, map: 'Rome' };
  L.matches.set(match.id, match);
  L.inMatch.set(id, match.id);
  L.expireConnect(match.id);
  assert.equal(L.matches.has(match.id), false);
  assert.equal(L.inMatch.has(id), false);
  assert.equal(L.penalties.size, 0);
});
test('pause also overrides team-kill penalty enforcement', t => {
  const L = fixture(t, true);
  const old = process.env.COMP_TK_ENFORCE;
  process.env.COMP_TK_ENFORCE = '1';
  t.after(() => { if (old === undefined) delete process.env.COMP_TK_ENFORCE;
    else process.env.COMP_TK_ENFORCE = old; });
  assert.equal(L.tkEnforcing(), false);
  process.env.COMP_QUEUE_PENALTIES_PAUSED = '0';
  assert.equal(L.tkEnforcing(), true);
});

test('no-show-only pause preserves team-kill bans and suppresses no-show charges', async t => {
  const L = fixture(t, false);
  const old = process.env.COMP_NO_SHOW_PENALTIES_PAUSED;
  process.env.COMP_NO_SHOW_PENALTIES_PAUSED = '1';
  t.after(() => old === undefined ? delete process.env.COMP_NO_SHOW_PENALTIES_PAUSED
    : process.env.COMP_NO_SHOW_PENALTIES_PAUSED = old);
  assert.equal(L.applyNoShow(id), null);
  assert.equal(L.penalties.size, 0);
  const record = { until: Date.now() + 60000, last: Date.now(), count: 1, elo: 15 };
  for (const reason of [undefined, 'no_show']) {
    L.penalties.set(id, { ...record, reason });
    assert.equal(await L.loadPenalty(id), null);
  }
  const teamkill = { ...record, reason: 'team_kill' };
  L.penalties.set(id, teamkill);
  assert.deepEqual(await L.loadPenalty(id), teamkill);
  process.env.COMP_NO_SHOW_PENALTIES_PAUSED = '0';
  L.penalties.set(id, { ...record, reason: 'no_show' });
  assert.equal((await L.loadPenalty(id)).reason, 'no_show');
});
