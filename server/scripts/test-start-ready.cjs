const assert = require('node:assert/strict');
const { test } = require('node:test');
const live = require('../live.cjs');

function fixture(t, count = 4, modeId = 'BB5', L = live.create({modeId})) {
  const ids = Array.from({ length: count }, (_, i) => String(76561198000000001n + BigInt(i)));
  const teams = { 1: ids.filter((_, i) => i % 2 === 0), 2: ids.filter((_, i) => i % 2) };
  const match = { id: '0123456789abcdef', state: 'connecting', host: ids[0],
    players: ids.map(steam_id => ({ steam_id, connected: true, accepted: true })),
    teams, assigned_teams: structuredClone(teams), left: [], timer: null, deadline: 0,
    map: modeId === 'BB1' ? 'Paintball' : 'Rome' };
  L._internals.matches.set(match.id, match);
  for (const id of ids) L._internals.inMatch.set(id, match.id);
  t.after(() => clearTimeout(match.timer));
  const rows = ids.map((steam_id, i) => ({ steam_id, team: i % 2, active: 1 }));
  return { L, match, rows };
}

test('authenticated two-player BB1 snapshot starts the match through the shared ranked service', async t => {
  const service = require('../ranked-service.cjs').create({});
  await service._internals.ready;
  t.after(() => service.shutdown());
  const {L, match, rows} = fixture(t, 2, 'BB1', service.forMode('BB1'));
  const token = L._internals.connectPayload(match, match.host).report_token;
  const listener = require('../server.cjs').createServer({liveService: service});
  await new Promise(resolve => listener.listen(0, '127.0.0.1', resolve));
  t.after(async () => { listener.closeAllConnections(); await new Promise(resolve => listener.close(resolve)); });
  const post = async (snapshot, bearer = token) => {
    const response = await fetch(`http://127.0.0.1:${listener.address().port}/api/match-report/start-ready`, {
      method: 'POST', headers: {'content-type': 'application/json', authorization: `Bearer ${bearer}`},
      body: JSON.stringify({event_name: 'ch_start_ready', storefront: 'chm-' + match.id,
        first_session_timestamp: 'chstart-1', user_id: match.host,
        platform: snapshot.length + '|' + snapshot.map(r => `${r.steam_id}:${r.team}:${r.active};`).join('')})
    });
    return {status: response.status, body: await response.json()};
  };
  assert.equal((await post(rows, 'invalid')).status, 401);
  for (const bad of [rows.slice(0, 1), [rows[0], rows[0]], [...rows, {...rows[0]}],
    rows.map(r => ({...r, team: 0})), rows.map((r, i) => i ? {...r, active: 0} : r)]) {
    assert.equal((await post(bad)).status, 409);
    assert.equal(match.state, 'connecting');
    assert.notEqual(match.start_ready_verified, true);
  }
  const started = await post(rows);
  assert.equal(started.status, 200, JSON.stringify(started.body));
  assert.equal(match.state, 'live');
  assert.equal(match.start_ready_verified, true);
  assert.equal(match.team_sort_verified, true);
  assert(match.players.every(p => p.connected));
  assert.equal((await post(rows)).status, 200, 'a lost approval response can be retried');
});

test('BB1 rejects frozen assignments that do not contain one player per side', t => {
  for (const side of [1, 2]) {
    const {L, match, rows} = fixture(t, 2, 'BB1');
    t.after(() => L.shutdown());
    match.assigned_teams = {1: [], 2: []};
    match.assigned_teams[side] = rows.map(r => r.steam_id);
    assert.deepEqual(L.startReady(match.host, match.id, rows), {ok: false, error: 'invalid duel assignment'});
    assert.equal(match.state, 'connecting');
  }
});

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
