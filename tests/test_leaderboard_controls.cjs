// Run with: node --test tests/test_leaderboard_controls.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const sandbox = { module: { exports: {} }, window: { HubUI: { registerScreen() {} } } };
vm.runInNewContext(fs.readFileSync('hub/webui/static/screens/leaderboard.js', 'utf8'), sandbox);
const selectRows = sandbox.module.exports;
const ladder = { names: ['Rookie', 'Operator', 'Spectre'], divisions: 3, top: 'Reaper' };
const rows = [
  { steam_id: '1', name: 'Zulu', rank: 1, rank_name: 'Reaper', top: true, rr: 250, matches: 100, win_rate: '90%' },
  { steam_id: '2', name: 'Alpha', rank: 2, rank_name: 'Spectre', division: 3, rr: 200, matches: 20, win_rate: '100%' },
  { steam_id: '3', name: 'Beta', rank: 10, rank_name: 'Operator', division: 2, rr: 9, matches: 3, win_rate: '9%' },
  { steam_id: '4', name: 'beta two', rank: null, rank_name: null, rr: null, matches: 0, win_rate: '-' }
];
function ids(options = {}, source = rows) {
  return Array.from(selectRows(source, options, ladder), r => r.steam_id);
}
test('search matches names case-insensitively and Steam IDs, trimming spaces', () => {
  assert.deepEqual(ids({ query: ' BETA ' }), ['3', '4']);
  assert.deepEqual(ids({ query: '2' }), ['2']);
});
test('rank, placement and search filters combine', () => {
  assert.deepEqual(ids({ query: 'beta', tier: 'Operator', status: 'ranked' }), ['3']);
  assert.deepEqual(ids({ status: 'placing' }), ['4']);
  assert.deepEqual(ids({ tier: 'Reaper' }), ['1']);
  assert.deepEqual(ids({ tier: 'Rookie' }), []);
});
for (const [column, high, low] of [
  ['rank', ['1', '2', '3', '4'], ['3', '2', '1', '4']],
  ['player', ['1', '4', '3', '2'], ['2', '3', '4', '1']],
  ['tier', ['1', '2', '3', '4'], ['3', '2', '1', '4']],
  ['rr', ['1', '2', '3', '4'], ['3', '2', '1', '4']],
  ['matches', ['1', '2', '3', '4'], ['4', '3', '2', '1']],
  ['winrate', ['2', '1', '3', '4'], ['3', '1', '2', '4']]
]) {
  test(`${column}: highest/lowest compare values, with missing values last`, () => {
    assert.deepEqual(ids({ sort: column, direction: 'highest' }), high);
    assert.deepEqual(ids({ sort: column, direction: 'lowest' }), low);
  });
}
test('None restores source order, sorting never mutates source', () => {
  const before = JSON.stringify(rows);
  ids({ sort: 'matches', direction: 'lowest' });
  assert.deepEqual(ids({ sort: 'matches', direction: 'none' }), ['1', '2', '3', '4']);
  assert.equal(JSON.stringify(rows), before);
});
test('tier sorting respects division order and ties retain server order', () => {
  const source = [
    { steam_id: 'a', rank_name: 'Operator', division: 1, rr: 5 },
    { steam_id: 'b', rank_name: 'Operator', division: 3, rr: 5 },
    { steam_id: 'c', rank_name: 'Rookie', division: 3, rr: 5 }
  ];
  assert.deepEqual(ids({ sort: 'tier', direction: 'highest' }, source), ['b', 'a', 'c']);
  assert.deepEqual(ids({ sort: 'rr', direction: 'highest' }, source), ['a', 'b', 'c']);
});
