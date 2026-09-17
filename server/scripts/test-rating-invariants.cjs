// Rating fixtures do not provide relay samples; network behavior has dedicated tests.
process.env.NODE_ENV = 'test';
process.env.COMP_NETWORK_TEST_BYPASS = '1';
const test = require('node:test');
const assert = require('node:assert/strict');
const R = require('../rating.cjs');
const P = require('../progress.cjs');
const V = require('../valuation.cjs');
const M = require('../matchmaker.cjs');
const record = (rating = 1500, extra = {}) => ({ ...R.defaultRating(), rating, rd: 60, matches: 30, ...extra });

test('mirrored outcomes have symmetric base RR and zero drift at equilibrium', () => {
  // Default band geometry: MMR 2450 corresponds to total progress 2500.
  const me = record(2450, { progress: 2500 });
  for (const roundDiff of [1 / 7, 0.25, 4 / 7, 1]) {
    const win = P.award(me, me, { won: true, roundDiff });
    const loss = P.award(me, me, { won: false, roundDiff });
    assert.equal(win.delta + loss.delta, 0);
    assert.equal(win.factors.base, -loss.factors.base);
  }
});

test('continuous convergence follows divisions and remains meaningful above Spectre', () => {
  for (const mmr of [910, 1540, 2170, 2380]) {
    const below = P.convergence(record(mmr - 0.001), 1500);
    const above = P.convergence(record(mmr + 0.001), 1500);
    assert.ok(Math.abs(above - below) < 0.00001, `MMR cliff at ${mmr}`);
  }
  assert.ok(P.convergence(record(1800), 1499.999) - P.convergence(record(1800), 1500.001) < 0.00001);
  assert.ok(P.convergence(record(2400), 2400) > P.convergence(record(2400), 2600));
  assert.ok(P.convergence(record(3000), 3000) > P.convergence(record(2500), 3000));
});

test('1000 alternating matches do not reward volume at fixed top skill', () => {
  let me = record(2450, { progress: 2500 });
  for (let i = 0; i < 1000; i++) {
    const rr = P.award(me, me, { won: i % 2 === 0, roundDiff: 1 / 7 });
    me = { ...me, progress: rr.progress, demoteArmed: rr.demoteArmed };
  }
  assert.ok(Math.abs(me.progress - 2500) <= 25, `ended at ${me.progress}`);
});

test('balanced mixed-skill teams have symmetric outcome evidence for each member', () => {
  const team = { rating: 1500, rd: 60 };
  for (const mmr of [1000, 1500, 2000]) {
    const me = record(mmr);
    const win = R.update(me, team, 1, 1, team);
    const loss = R.update(me, team, 0, 1, team);
    assert.equal(win.rating - mmr, mmr - loss.rating);
    assert.equal(win.rating - mmr, R.update(record(1500), team, 1, 1, team).rating - 1500);
  }
  assert.ok(R.update(record(1500, { rd: 300 }), team, 1, 1, team).rating - 1500 > 50);
});

test('own-team strength and party premium affect outcome expectation; solo API stays usable', () => {
  const me = record(1800), opponent = { rating: 1500, rd: 60 };
  const balanced = R.update(me, opponent, 1, 1, opponent).rating;
  const favored = R.update(me, opponent, 1, 1, { rating: 1700, rd: 60 }).rating;
  assert.ok(balanced > favored);
  assert.equal(R.update(record(1500), opponent, 1).rating,
    R.update(record(1500), opponent, 1, 1, opponent).rating);
});

test('party packaging preserves player-weighted team uncertainty', () => {
  const units = [{ key: 'trio', joined: 1, ratings: Array.from({ length: 3 }, () => record(1500, { rd: 350 })) },
    ...Array.from({ length: 7 }, (_, i) => ({ key: `s${i}`, joined: 1, ratings: [record(1500, { rd: 45 })] }))];
  const formed = M.findMatch(units, { matchSize: 10, teamSize: 5, now: 1000000 });
  for (const side of [1, 2]) {
    const records = formed.teams[side].flatMap(u => u.ratings);
    assert.ok(Math.abs(formed.ratings[side].rd - R.teamAggregate(records).rd) < 1e-9);
  }
});

test('every demoted division earns its own stop at zero during a losing streak', () => {
  let me = record(1500, { progress: 1005 });
  const visited = [];
  for (let i = 0; i < 35; i++) {
    const rr = P.award(me, me, { won: false, roundDiff: 4 / 7, demoteArmed: me.demoteArmed });
    if (rr.divisionChanged < 0) assert.equal(rr.demoteArmed, false);
    if (rr.progress % 100 === 0) visited.push(rr.progress);
    me = { ...me, progress: rr.progress, demoteArmed: rr.demoteArmed };
  }
  for (const floor of [1000, 900, 800]) assert.ok(visited.includes(floor), `skipped ${floor}`);
});

test('a stale demotion flag after a penalty cannot skip a new division boundary', () => {
  const me = record(1500, { progress: 905, demoteArmed: true });
  const rr = P.award(me, me, { won: false, roundDiff: 1, demoteArmed: true });
  assert.equal(rr.progress, 900);
  const win = P.award({ ...me, progress: 900 }, me, { won: true, demoteArmed: true });
  assert.equal(win.demoteArmed, false);
});

test('one MMR point changes expected performance continuously', () => {
  const tied = [record(1600), record(1600), record(1600)];
  const tiny = [record(1600), record(1599), record(1599)];
  assert.equal(V.expectation(tied, 0), 0.5);
  assert.ok(V.expectation(tiny, 0) > 0.5 && V.expectation(tiny, 0) < 0.505);
  const strong = V.expectation([record(2000), record(1500)], 0);
  assert.ok(strong > 0.8);
  assert.ok(V.expectation([record(2000, { rd: 350 }), record(1500)], 0) < strong);
});

const match = { teams: { 1: ['a', 'b', 'c'], 2: ['x'] }, players: ['a', 'b', 'c', 'x'].map(steam_id => ({ steam_id })),
  score: { 1: 3, 2: 0 }, score_limit: 3, team_map: { 0: 1, 1: 2 },
  rounds: { 1: { winTeam: 0 }, 2: { winTeam: 0 }, 3: { winTeam: 0 } } };
const ratings = { a: record(2000), b: record(), c: record(), x: record() };

test('peer reports cannot manufacture evidence for an unreported strong player', () => {
  const stats = { rounds: 3, limit: 3, players: [{ steamId: 'b', deaths: 0 }, { steamId: 'c', deaths: 3 }] };
  const result = V.valuation(match, stats, ratings, 1).find(r => r.steamId === 'a');
  assert.equal(result.breakdown.measured, false);
  assert.equal(result.breakdown.excess, 0);
  assert.equal(result.score, 1);
  const after = R.update(record(2000, { matches: 0 }), { rating: 1500, rd: 60 }, result.score);
  P.recordPlacement({ matches: 0 }, after, result.breakdown);
  assert.equal(after.placementMeasured, 0);
});

test('partial metric coverage cannot compare different metrics or peer populations', () => {
  assert.deepEqual(V.impact([{ kpr: 4 }, { kpr: 1 }, { survival: 1 }, { survival: 0 }]), [null, null, null, null]);
});

test('unknown or intermittent death series never invents survived team wins', () => {
  for (const series of [{}, { a: { 1: { deaths: 0 }, 3: { deaths: 0 } } },
    { a: { 1: { deaths: null }, 2: { deaths: 0 }, 3: { deaths: 0 } } },
    { a: { 1: { deaths: 1 }, 2: { deaths: 0 }, 3: { deaths: 0 } } }]) {
    const ctx = V.roundContext(match, { series }, () => 1);
    assert.equal(ctx.a?.roundsWon, undefined);
    assert.equal(ctx.b?.roundsWon, undefined);
  }
  const ctx = V.roundContext(match, { series: { a: { 1: { deaths: 0 }, 2: { deaths: 1 }, 3: { deaths: 1 } } } }, () => 1);
  assert.equal(ctx.a.roundsWon, 2);
  assert.equal(ctx.a.roundsCounted, 3);
});

test('a partial kill feed is evidence of events, not complete performance totals', () => {
  const m = { ...match, kills: [
    { killer: 'a', victim: 'b', killerTeam: 1, victimTeam: 2, round: 1 },
    { killer: 'a', victim: 'c', killerTeam: 1, victimTeam: 2, round: 2 },
    { killer: 'b', victim: 'x', killerTeam: 1, victimTeam: 2, round: 3 },
  ] };
  const rows = V.valuation(m, { rounds: 3, limit: 3 }, ratings, 1);
  assert.ok(rows.every(r => r.breakdown.measured === false));
  assert.equal(V.killCounts(m).a.enemyKills, 2, 'events remain available for display');
});

test('a service-verified complete feed can measure zero-kill players without inventing deaths', () => {
  const m = { ...match, kill_feed_complete: true, kills: [
    { killer: 'a', victim: 'x', killerTeam: 1, victimTeam: 2, round: 1 },
    { killer: 'a', victim: 'x', killerTeam: 1, victimTeam: 2, round: 2 },
    { killer: 'b', victim: 'x', killerTeam: 1, victimTeam: 2, round: 3 },
  ] };
  const rows = V.valuation(m, { rounds: 3, limit: 3 }, ratings, 1);
  assert.ok(rows.every(r => r.breakdown.measured));
  const c = rows.find(r => r.steamId === 'c');
  assert.equal(c.breakdown.kills, 0);
  assert.ok(Number.isNaN(c.breakdown.survival));
});

test('partial round history does not substitute for full-match survival coverage', () => {
  const m = { ...match, rounds: { 1: { winTeam: 0 } } };
  const stats = { rounds: 3, series: { a: { 1: { deaths: 0 } }, b: { 1: { deaths: 1 } }, c: { 1: { deaths: 0 } } } };
  const rows = V.valuation(m, stats, ratings, 1);
  assert.ok(rows.every(r => r.breakdown.measured === false));
});

test('score-only reports use the known match length when the report omits rounds', () => {
  const rows = V.valuation(match, { players: [
    { steamId: 'a', score: 300 }, { steamId: 'b', score: 200 }, { steamId: 'c', score: 100 }, { steamId: 'x', score: 0 },
  ] }, ratings, 1);
  assert.equal(rows[0].breakdown.spr, 100);
  assert.equal(rows[0].breakdown.roundsPlayed, 3);
});

test('idle uncertainty grows with explicit elapsed time, bounded and without skill or RR decay', () => {
  assert.equal(typeof R.ageUncertainty, 'function');
  const now = 2000000000000;
  const me = record(1800, { progress: 1600, updated: now - 7 * 86400000, revision: 8 });
  const copy = { ...me };
  const aged = R.ageUncertainty(me, now);
  assert.ok(aged.rd > me.rd);
  assert.deepEqual({ ...aged, rd: me.rd }, me);
  assert.deepEqual(me, copy);
  assert.deepEqual(R.ageUncertainty(me, me.updated), me);
  assert.deepEqual(R.ageUncertainty(me, me.updated - 1), me);
  assert.deepEqual(R.ageUncertainty(me, NaN), me);
  assert.equal(R.ageUncertainty({ ...me, updated: 0 }, now).rd, 60);
  assert.equal(R.ageUncertainty(me, now + 1000 * 365 * 86400000).rd, R.MAX_RD);
  assert.deepEqual(R.ageUncertainty(me, now), aged, 'same stored record and clock are deterministic');
});

test('rating normalization and update preserve the storage revision', () => {
  assert.equal(R.defaultRating().revision, 0);
  assert.equal(R.normalise(record(1500, { revision: 12 })).revision, 12);
  assert.equal(R.normalise({ revision: -2 }).revision, 0);
  assert.equal(R.update(record(1500, { revision: 12 }), { rating: 1500, rd: 60 }, 1).revision, 12);
});

test('a disabled lobby comparison cannot create measured evidence', () => {
  const { spawnSync } = require('node:child_process');
  const result = spawnSync(process.execPath, ['-e', `
    const V = require('./server/valuation.cjs');
    const match = {teams:{1:['a','b'],2:['x','y']},score:{1:3,2:0},score_limit:3};
    const players = [{steamId:'a',deaths:0},{steamId:'b',deaths:0},{steamId:'x',deaths:1},{steamId:'y',deaths:2}];
    console.log(JSON.stringify(V.valuation(match,{rounds:3,players}, {}, 1)[0]));
  `], { cwd: require('node:path').join(__dirname, '../..'), env: { ...process.env, COMP_PERF_TEAM_WEIGHT: '1' }, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const row = JSON.parse(result.stdout);
  assert.equal(row.breakdown.measured, false);
  assert.equal(row.score, 1);
});

test('an early player summary cannot imply perfect survival across unreported rounds', () => {
  const m = { teams: { 1: ['a', 'b'], 2: ['x', 'y'] }, score: { 1: 7, 2: 3 }, score_limit: 7 };
  const stats = { rounds: 10, players: [
    { steamId: 'a', deaths: 0 }, { steamId: 'b', deaths: 5 },
    { steamId: 'x', deaths: 6 }, { steamId: 'y', deaths: 7 },
  ], series: { a: { 1: { deaths: 0 } }, b: { 10: { deaths: 5 } }, x: { 10: { deaths: 6 } }, y: { 10: { deaths: 7 } } } };
  const row = V.valuation(m, stats, {}, 1).find(r => r.steamId === 'a');
  assert.equal(row.breakdown.measured, false);
  assert.equal(row.breakdown.coverage.survival, false);
  assert.ok(Number.isNaN(row.breakdown.survival));
  assert.equal(row.score, 1);
});

test('final sample coverage is per metric and uses final values instead of stale summaries', () => {
  const m = { teams: { 1: ['a', 'b'], 2: ['x', 'y'] }, score: { 1: 7, 2: 3 }, score_limit: 7 };
  const stats = { rounds: 10, players: [
    { steamId: 'a', deaths: 0, score: 900 }, { steamId: 'b', deaths: 5, score: 200 },
    { steamId: 'x', deaths: 6, score: 100 }, { steamId: 'y', deaths: 7, score: 50 },
  ], series: { a: { 1: { deaths: 0, score: 900 }, 10: { score: 300 } },
    b: { 10: { deaths: 5, score: 200 } }, x: { 10: { deaths: 6, score: 100 } }, y: { 10: { deaths: 7, score: 50 } } } };
  const row = V.valuation(m, stats, {}, 1).find(r => r.steamId === 'a');
  assert.equal(row.breakdown.coverage.survival, false);
  assert.equal(row.breakdown.coverage.spr, true);
  assert.equal(row.breakdown.spr, 30);
  assert.equal(row.breakdown.measured, true, 'shared final score data still provides evidence');
  assert.equal(stats.players[0].score, 900, 'valuation does not modify the input summary');
});

test('an early report round total cannot make partial history cover a finished match', () => {
  const m = { teams: { 1: ['a', 'b'], 2: ['x', 'y'] }, score: { 1: 7, 2: 3 }, score_limit: 7,
    players: ['a', 'b', 'x', 'y'].map(steam_id => ({ steam_id })),
    team_map: { 0: 1, 1: 2 }, rounds: { 1: { winTeam: 0 } } };
  const stats = { rounds: 1, players: ['a', 'b', 'x', 'y'].map((steamId, deaths) => ({ steamId, deaths })),
    series: { a: { 1: { deaths: 0 } }, b: { 1: { deaths: 1 } }, x: { 1: { deaths: 1 } }, y: { 1: { deaths: 1 } } } };
  const rows = V.valuation(m, stats, {}, 1);
  assert.ok(rows.every(r => r.breakdown.measured === false));
  assert.ok(rows.every(r => r.breakdown.roundsPlayed === 10));
  assert.ok(rows.every(r => Number.isNaN(r.breakdown.roundWinShare)));
});

test('a player absent from available series has no verified final cumulative metrics', () => {
  const m = { teams: { 1: ['a', 'b'], 2: [] }, score: { 1: 7, 2: 3 }, score_limit: 7 };
  const stats = { rounds: 10, players: [{ steamId: 'a', deaths: 0 }, { steamId: 'b', deaths: 5 }],
    series: { b: { 10: { deaths: 5 } } } };
  const row = V.valuation(m, stats, {}, 1)[0];
  assert.equal(row.breakdown.measured, false);
  assert.equal(row.breakdown.coverage.survival, false);
});

test('empty sampling history without a completed round cannot invent zero counters', () => {
  const m = { teams: { 1: ['a', 'b'], 2: [] }, score: { 1: 0, 2: 0 }, score_limit: 7 };
  const stats = { rounds: 0, players: [{ steamId: 'a', deaths: 0 }, { steamId: 'b', deaths: 1 }], series: {} };
  const row = V.valuation(m, stats, {}, 1)[0];
  assert.equal(row.breakdown.coverage.survival, false);
});
