const assert = require('node:assert/strict');
const { test } = require('node:test');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const vm = require('node:vm');
const live = require('../live.cjs');
const H = '76561198000000001', M = '76561198000000002', F = '76561198000000003';

function fixture(t) {
  const L = live.create({ whoami: async () => null, bearer: () => '', sendJson: () => {},
    badRequest: () => {}, readBody: async () => Buffer.alloc(0), upstashCmd: async () => null, prefix: 'test' });
  const match = { id: 'team-sort', state: 'connecting', host: H,
    players: [H, M, F].map(steam_id => ({ steam_id, connected: true, accepted: true })),
    teams: { 1: [H, M], 2: [F] }, left: [], timer: null, deadline: 0, map: 'Rome' };
  L._internals.matches.set(match.id, match);
  for (const p of match.players) L._internals.inMatch.set(p.steam_id, match.id);
  t.after(() => clearTimeout(match.timer));
  return { L, match };
}

test('both sides require a positive ruling; refusals authorize neither', t => {
  const { L, match } = fixture(t);
  for (const [id, one, two] of [[H, true, false], [F, false, true]]) {
    assert.equal(L.teamRuling(H, id, 'side-one').yes, one);
    assert.equal(L.teamRuling(H, id, 'side-two').yes, two);
  }
  match.teams = null;
  assert.equal(L.teamRuling(H, H, 'side-one').ok, false);
  assert.equal(L.teamRuling(H, H, 'side-two').ok, false);
});

test('all-connected waits even with no reports or only unassigned reports', t => {
  const { L, match } = fixture(t);
  L.gameReportedTeam(H, { subject: H, team: -1 });
  const result = L._internals.goLiveIfReady(match);
  assert.equal(result.started, false);
  assert.equal(result.gated, true);
  assert.equal(match.timer, null, 'no deadline can authorize an early start');
});

test('verified reports use the writer mapping, never a reversed pre-write mapping', t => {
  const { L, match } = fixture(t);
  for (const [subject, team] of [[H, 1], [M, 1], [F, 0]]) {
    L.gameReportedTeam(H, { subject, team, verified: true });
  }
  assert.equal(match.state, 'connecting');
  for (const [subject, team] of [[H, 0], [M, 0], [F, 1]]) {
    L.gameReportedTeam(H, { subject, team, verified: true });
  }
  assert.equal(L.teamsAgree(match).agree, true);
  assert.equal(match.state, 'connecting', 'only a complete current host snapshot starts');
});

test('first verified report cannot inherit stale reports from an older pak', t => {
  const { L, match } = fixture(t);
  match.ingame = new Map([[H, 0], [M, 0], [F, 1]]);
  L.gameReportedTeam(H, { subject: H, team: 0, verified: true });
  assert.equal(match.state, 'connecting');
  assert.deepEqual(L.teamsAgree(match).missing, [M, F]);
});

test('the probe accepts post-write verification but not legacy before-write telemetry', () => {
  const source = fs.readFileSync(require.resolve('../server.cjs'), 'utf8');
  const start = source.indexOf('const TEAM_EVENT =');
  const end = source.indexOf('// TEAM KILLS.', start);
  const calls = [];
  const ctx = { live: () => ({ gameReportedTeam: (...args) => { calls.push(args); return { ok: false }; } }), console };
  vm.createContext(ctx);
  vm.runInContext(source.slice(start, end), ctx);
  const send = event_name => ctx.noteMatchTeams({ body: JSON.stringify({ event_name, user_id: H,
    storefront: M, platform: '0:3:0:0:0' }) });
  send('ch_team_write');
  assert.equal(calls.length, 0);
  send('ch_team_verified');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].team, '0');
  assert.equal(calls[0][1].verified, true);
  ctx.noteMatchTeams({ body: JSON.stringify({ event_name: 'ch_team_verified', user_id: H,
    storefront: M, platform: '0:3:0:0:1' }) });
  assert.equal(calls[1][1].team, -1, 'a library/property disagreement invalidates the old observation');
});

test('zero cannot disable the strict in-game gate', () => {
  const script = `const L=require(${JSON.stringify(require.resolve('../live.cjs'))}).create({});
    const m={id:'zero',state:'connecting',host:'h',players:[{steam_id:'h',connected:true},{steam_id:'m',connected:true}],
      teams:{1:['h'],2:['m']},ingame:new Map([['h',0]])};
    const r=L._internals.goLiveIfReady(m); clearTimeout(m.timer);
    if(r.started || !r.gated) process.exit(1);`;
  execFileSync(process.execPath, ['-e', script], { env: { ...process.env, COMP_TEAMS_GATE_SECONDS: '0' } });
});
