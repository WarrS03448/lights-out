const assert = require('node:assert/strict');
const { test } = require('node:test');
const live = require('../live.cjs');

function fixture(t, count = 4) {
  const L = live.create({});
  const ids = Array.from({ length: count }, (_, i) => String(76561198000000001n + BigInt(i)));
  const teams = { 1: ids.filter((_, i) => i % 2 === 0), 2: ids.filter((_, i) => i % 2) };
  const match = { id: '0123456789abcdef', state: 'connecting', host: ids[0],
    players: ids.map(steam_id => ({ steam_id, connected: true, accepted: true })),
    teams, assigned_teams: structuredClone(teams), left: [], timer: null, deadline: 0, map: 'Rome' };
  L._internals.matches.set(match.id, match);
  for (const id of ids) L._internals.inMatch.set(id, match.id);
  t.after(() => clearTimeout(match.timer));
  const rows = ids.map((steam_id, i) => ({ steam_id, team: i % 2, active: 1 }));
  return { L, match, rows };
}

test('only an exact complete host snapshot releases 3, 4, and 10 player games', t => {
  for (const size of [3, 4, 10]) {
    const { L, match, rows } = fixture(t, size);
    assert.equal(L.startReady(match.host, match.id, rows.slice(0, -1)).ok, false);
    assert.equal(match.state, 'connecting');
    assert.equal(L.startReady(match.host, match.id, rows).ok, true);
    assert.equal(match.state, 'live');
    assert.equal(L.startReady(match.host, match.id, rows).ok, true, 'retry after lost reply');
  }
});

test('wrong identities, teams, inactive players and duplicate rows fail closed', t => {
  for (const change of [r => r[0].team = 1, r => r[0].active = 0,
    r => r[0].steam_id = '76561198999999999', r => r[1] = { ...r[0] },
    r => r.push({ ...r[0] })]) {
    const { L, match, rows } = fixture(t); change(rows);
    assert.equal(L.startReady(match.host, match.id, rows).ok, false);
    assert.equal(match.state, 'connecting');
  }
});

test('leaving cannot shrink the frozen assigned roster; stale and non-host requests fail', t => {
  const { L, match, rows } = fixture(t);
  assert.equal(L.startReady(rows[1].steam_id, match.id, rows).ok, false);
  assert.equal(L.startReady(match.host, 'old', rows).ok, false);
  match.players.pop(); match.teams[2].pop();
  assert.equal(L.startReady(match.host, match.id, rows.slice(0, -1)).ok, false);
  assert.equal(L.startReady(match.host, match.id, rows).ok, false);
  match.state = 'cancelled';
  assert.equal(L.startReady(match.host, match.id, rows).ok, false);
});

test('incremental reports cannot start a match or corrupt an approved assignment', t => {
  const { L, match, rows } = fixture(t);
  for (const row of rows) L.gameReportedTeam(match.host, { subject: row.steam_id, team: row.team, verified: true });
  assert.equal(match.state, 'connecting');
  assert.equal(L.startReady(match.host, match.id, rows).ok, true);
  L.gameReportedTeam(match.host, { subject: rows[0].steam_id, team: 1, verified: true });
  assert.equal(match.ingame.get(rows[0].steam_id), 0);
});
