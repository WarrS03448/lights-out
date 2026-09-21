/**
 * The scoreboard path: the game reports its score, the server decides the winner, the ladder
 * moves (Sam, 2026-09-15 - "whichever team reaches the score limit first, wins").
 *
 * No HTTP and no sockets. `live.create()` is built with stub plumbing and the match is
 * assembled directly in `_internals`, which is the only honest way to test this right now:
 * getting a match to `live` through the front door needs ten accept POSTs, a whole coin
 * flip and veto, and a connect window, none of which this file is about.
 *
 * What it IS about is the decision - the team-id mapping, the score limit, and everything
 * that must be REFUSED, since a score report is the host's unauthenticated word.
 *
 *   node server/scripts/test-result.mjs
 */
import assert from 'node:assert';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const live = require('../live.cjs');
const rating = require('../rating.cjs');
const progress = require('../progress.cjs');
const valuation = require('../valuation.cjs');

let passed = 0;
let failed = 0;

/** Async tests, queued here and awaited at the end - see runQueued(). */
const queued = [];

function test(name, fn) {
  try {
    const out = fn();
    // An async test cannot be scored here: its assertions have not run yet, and a rejected
    // promise nobody awaits is a test that fails by printing "ok". Queue it instead.
    if (out && typeof out.then === 'function') {
      queued.push([name, out]);
      return;
    }
    passed += 1;
    console.log(`ok   - ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`FAIL - ${name}`);
    console.log(`       ${err && err.message}`);
  }
}

const HOST = '76561198000000001';
const FOE = '76561198000000002';

/** A service with nothing plugged into it, and no Upstash (the in-memory fallbacks are used).
 *
 * `collectSeconds` defaults to 0 here, so a decided match settles at once and the tests below can
 * assert on the result in the same tick. The collection window itself is driven explicitly, with
 * a service made with a real one - see 'the match is held open for the last stats'.
 */
function service(collectSeconds = 0) {
  return live.create({
    collectSeconds,
    whoami: async () => null,
    bearer: () => '',
    sendJson: () => {},
    badRequest: () => {},
    readBody: async () => Buffer.alloc(0),
    upstashCmd: null,
    prefix: 'test:',
  });
}

/**
 * A live 1v1 with `host` on our team `hostSide`. Small on purpose: the mapping this file
 * tests does not care how many players are on a side, and two ids keep the assertions legible.
 */
function liveMatch(svc, { hostSide = 1 } = {}) {
  const { matches, inMatch } = svc._internals;
  const id = 'match-under-test';
  const teams = hostSide === 1 ? { 1: [HOST], 2: [FOE] } : { 1: [FOE], 2: [HOST] };
  const match = {
    id,
    players: [{ steam_id: HOST, persona: 'host', accepted: true, connected: true },
              { steam_id: FOE, persona: 'foe', accepted: true, connected: true }],
    state: 'live', created: Date.now(), timer: null, deadline: 0,
    map: 'Rome', host: HOST, left: [], teams,
    mm: { teams, quality: 1, ratings: { 1: { rating: 1500, rd: 60 }, 2: { rating: 1500, rd: 60 } } },
  };
  matches.set(id, match);
  inMatch.set(HOST, id);
  inMatch.set(FOE, id);
  return match;
}

// These tests assemble extra roster seats before exercising native reports.
function addFixturePlayer(match, player) {
  match.players.push(player);
  delete match.game_bindings;
  require('../player-identity.cjs').freezeMatch(match);
}

console.log('\n--- the game reports its score ---');

test('reaching the score limit decides the match', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc.gameReportedScore(HOST, {
    match: 'match-under-test', scores: '0|0:7|1:3', limit: '7',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.finished, true);
  assert.equal(out.winner, 1, 'the host was on our team 1 and their in-game team won');
  assert.deepEqual(out.score, { 1: 7, 2: 3 });
});

test('the host anchor maps in-game team ids onto OUR teams', () => {
  // We never call SetTeamId, so in-game team 0 is not our team 1. The only thing tying the
  // two together is that the report says which in-game team the HOST is on, and we know which
  // of our sides the host is. Here the host is on our team 2 and is in-game team 1 - and
  // in-game team 1 is the one that reaches 7.
  const svc = service();
  liveMatch(svc, { hostSide: 2 });
  const out = svc.gameReportedScore(HOST, {
    match: 'match-under-test', scores: '1|0:2|1:7', limit: '7',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.winner, 2, 'our team 2 must win, not our team 1');
  assert.deepEqual(out.score, { 1: 2, 2: 7 });
});

test('a match below the limit keeps playing', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:6|1:5', limit: '7' });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.finished, false);
  assert.ok(svc._internals.matches.has('match-under-test'), 'and is still a match');
});

test('the limit comes off the game, not off our guess', () => {
  const svc = service();
  liveMatch(svc);
  // DA_BB5 ships ScoreLimit 7, but the pack can be re-cooked. 5-3 is a win at a limit of 5
  // and nothing at all at 7, so the reported limit has to be the one that counts.
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:5|1:3', limit: '5' });
  assert.equal(out.finished, true);
  assert.equal(out.winner, 1);
});

test('a missing limit falls back to the shipped one', () => {
  const svc = service();
  liveMatch(svc);
  const limit = svc._internals.DEFAULT_SCORE_LIMIT;
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: `0|0:${limit}|1:0`, limit: '' });
  assert.equal(out.limit, limit);
  assert.equal(out.finished, true);
});

test('the ratings actually move, and the winner gains', () => {
  const svc = service();
  liveMatch(svc);
  const before = { host: svc._internals.ratingOf(HOST).rating, foe: svc._internals.ratingOf(FOE).rating };
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:1', limit: '7' });
  const after = { host: svc._internals.ratingOf(HOST).rating, foe: svc._internals.ratingOf(FOE).rating };
  assert.ok(after.host > before.host, `the winner should gain (${before.host} -> ${after.host})`);
  assert.ok(after.foe < before.foe, `the loser should lose (${before.foe} -> ${after.foe})`);
  assert.equal(svc._internals.ratingOf(HOST).matches, 1, 'and it counts towards placements');
});

test('the match is over: nobody is held in it and it can be queued out of again', () => {
  const svc = service();
  liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:1', limit: '7' });
  assert.equal(svc._internals.matches.has('match-under-test'), false, 'the match is gone');
  assert.equal(svc._internals.inMatch.has(HOST), false, 'and so is the hold on the host');
  assert.equal(svc._internals.inMatch.has(FOE), false, 'and on everyone else');
});

test('a decided match cannot be decided a second time', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:1', limit: '7' });
  const gained = svc._internals.ratingOf(HOST).rating;
  // The pak reports on a loop, so the winning score is sent again before the match tears down.
  const again = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:1', limit: '7' });
  assert.equal(again.ok, false, 'the second report must be refused');
  assert.equal(svc._internals.ratingOf(HOST).rating, gained, 'and must not pay out twice');
  assert.ok(match.finished, 'the match remembers it was already decided');
});

console.log('\n--- what must be refused ---');

test('only the host may report', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc.gameReportedScore(FOE, { match: 'match-under-test', scores: '1|0:1|1:7', limit: '7' });
  assert.equal(out.ok, false);
  assert.match(out.error, /host/);
  assert.ok(svc._internals.matches.has('match-under-test'), 'and the match plays on');
});

test('a report for a different match is not applied to this one', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc.gameReportedScore(HOST, { match: 'some-other-match', scores: '0|0:7|1:0', limit: '7' });
  assert.equal(out.ok, false);
  assert.match(out.error, /wrong match/);
});

test('a stale report cannot un-win a match', () => {
  // Reports are independent HTTP calls on a timer; they can arrive out of order. Applying an
  // older one would walk the score backwards and, at the limit, could re-open a decided match.
  const svc = service();
  liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:6|1:2', limit: '7' });
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:3|1:1', limit: '7' });
  assert.equal(out.ok, false);
  assert.match(out.error, /stale/);
  assert.deepEqual(svc._internals.matches.get('match-under-test').score, { 1: 6, 2: 2 });
});

test('garbage in the score field decides nothing', () => {
  const svc = service();
  liveMatch(svc);
  for (const scores of ['', 'nonsense', '0|0:7', '0:7|1:3', '0|0:7|1:3|2:1',
                        '0|0:99999|1:3', '0|abc:7|1:3', '0|0:7|0:3',
                        '<script>alert(1)</script>']) {
    const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores, limit: '7' });
    assert.equal(out.ok, false, `"${scores}" should have been refused`);
  }
  assert.ok(svc._internals.matches.has('match-under-test'), 'and the match is untouched');
});

test('but surrounding whitespace is tolerated, not refused', () => {
  // The fields are packed by Blueprint string concatenation. Being strict about a stray space
  // would throw away a perfectly good scoreboard for no benefit; being strict about the SHAPE
  // inside it is what matters, and the test above is that.
  const svc = service();
  liveMatch(svc);
  const out = svc.gameReportedScore(HOST, { match: ' match-under-test ', scores: ' 0|0:7|1:3 ', limit: ' 7 ' });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.winner, 1);
});

test('a report about a match that is not live is refused', () => {
  const svc = service();
  const match = liveMatch(svc);
  match.state = 'ready';
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });
  assert.equal(out.ok, false);
  assert.match(out.error, /ready/);
});

test('a report from somebody in no match at all is refused', () => {
  const svc = service();
  const out = svc.gameReportedScore('76561198000000009', { scores: '0|0:7|1:0', limit: '7' });
  assert.equal(out.ok, false);
});

test('a report from something that is not a steamid is refused', () => {
  const svc = service();
  liveMatch(svc);
  for (const who of ['', 'abc', '123', null, undefined, '765611980000000011']) {
    assert.equal(svc.gameReportedScore(who, { scores: '0|0:7|1:0', limit: '7' }).ok, false);
  }
});

console.log('\n--- the record ---');

test('the history row gets the result written into it', () => {
  const svc = service();
  const match = liveMatch(svc);
  // Archived at go-live, exactly as reportConnected does it, with the score still unknown.
  svc._internals.archiveMatch(match, { outcome: 'played', reason: '' });
  const rowBefore = svc._internals.history.get(HOST)[0];
  assert.equal(rowBefore.won, null, 'go-live archives an honest null');

  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:4', limit: '7' });
  const rowAfter = svc._internals.history.get(HOST)[0];
  assert.equal(rowAfter.won, true);
  assert.equal(rowAfter.score, '7-4', 'and the score from this player\'s point of view');
  // Two fresh accounts, so this was a PLACEMENT match: RR is frozen at 0 by design, and the row
  // says so rather than leaving a list to print "0 RR" - or, as it used to, arrows drawn from the
  // matchmaking rating that moved while the visible one could not.
  assert.equal(rowAfter.placement, true);
  assert.equal(rowAfter.rr_delta, 0);
  assert.equal(rowAfter.delta, 0, 'the arrows are drawn from RR, and RR did not move');

  const loser = svc._internals.history.get(FOE)[0];
  assert.equal(loser.won, false);
  assert.equal(loser.score, '4-7', 'the loser sees their own score first');
  assert.equal(loser.placement, true);
});

/** Both players placed, on the same MMR, mid-division - so convergence is 1 and demotion
 *  protection cannot pin the loser. A fresh account would be placing, with RR frozen at 0. */
function placedPair(svc) {
  for (const id of [HOST, FOE]) {
    svc._internals.saveRating(id, { rating: 1500, rd: 60, matches: rating.PLACEMENT_MATCHES + 10,
                                    wins: 5, losses: 5, progress: 950 });
  }
}

test("a placed player's history row carries the RR the match moved (Sam, 2026-09-16)", () => {
  // "we are only gaining and losing 1-3 RR": the list printed `delta` - an arrow count, drawn from
  // the matchmaking rating - with "RR" after it.
  const svc = service();
  placedPair(svc);
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played', reason: '' });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:4', limit: '7' });

  const mine = svc._internals.history.get(HOST)[0];
  assert.equal(mine.placement, false);
  assert.equal(mine.rr_delta, svc._internals.ratingOf(HOST).progress - 950,
               'the number is exactly what the badge moved');
  assert.ok(mine.rr_delta > 3, `a win is worth more than an arrow count, paid ${mine.rr_delta}`);
  assert.equal(mine.delta, rating.arrowsFor(mine.rr_delta), '`delta` stays the arrows, drawn from RR');

  const theirs = svc._internals.history.get(FOE)[0];
  assert.equal(theirs.rr_delta, svc._internals.ratingOf(FOE).progress - 950);
  assert.ok(theirs.rr_delta < -3, `and a loss costs a real amount, cost ${theirs.rr_delta}`);

  const full = svc._internals.archived.get('match-under-test');
  assert.equal(full.players.find((p) => p.steam_id === HOST).rr_delta, mine.rr_delta,
               'the full record agrees with the row');
});

test('the full record learns the winner', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played', reason: '' });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:4', limit: '7' });
  const full = svc._internals.archived.get('match-under-test');
  assert.equal(full.won_team, 1);
  assert.deepEqual(full.score, { 1: 7, 2: 4 });
  assert.equal(full.players.find((p) => p.steam_id === HOST).won, true);
  assert.equal(full.players.find((p) => p.steam_id === FOE).won, false);
});

test('a match nobody archived still settles', () => {
  // patchResult must never be the thing that stops a result being applied.
  const svc = service();
  liveMatch(svc);
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:4', limit: '7' });
  assert.equal(out.finished, true);
  assert.ok(svc._internals.ratingOf(HOST).rating > rating.START_RATING);
});

console.log('');
console.log('--- per-player stats from the pak ---');

// EXACTLY what bb5_graphs.rule_stats builds: <steamId>|<kills>:<deaths>:<spawnCount>:<score>,
// one player per call, with user_id the HOST (the reporter) and never the subject. The third
// field is SpawnCount, NOT rounds - `roundsPlayed` comes from `rounds` (GetCurrentRound).
const row = (id, k, d, r, sc) => `${id}|${k}:${d}:${r}:${sc}`;

test('one call, one player - the shape the pak actually sends', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test', rows: row(HOST, 14, 6, 10, 3200), rounds: '10',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.players, 1);
  // Unreported fields are ABSENT, not present-and-undefined: the parser only sets what the
  // report actually carried, which is what lets the valuation tell "no opinion" from "zero".
  assert.deepEqual(match.stats.players[0],
    { steamId: HOST, kills: 14, deaths: 6, spawnCount: 10, score: 3200 });
  assert.equal('roundsWon' in match.stats.players[0], false);
  assert.equal(match.stats.rounds, 10);
});

test('a sweep builds the roster one player at a time', () => {
  // The cursor walks PlayerArray, so the roster arrives across several calls and several
  // passes. Later reports are running totals and must REPLACE the earlier ones, not stack.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: row(HOST, 3, 2, 3, 700), rounds: '3' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: row(FOE, 2, 3, 3, 500), rounds: '3' });
  assert.equal(match.stats.players.length, 2, 'both players known after one sweep');
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: row(HOST, 9, 5, 8, 2100), rounds: '8' });
  assert.equal(match.stats.players.length, 2, 'the second pass must not duplicate anybody');
  assert.equal(match.stats.players.find((p) => p.steamId === HOST).kills, 9, 'newest wins');
  assert.equal(match.stats.rounds, 8);
});

test('the packed-roster shape works too, if the field turns out to be long enough', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test',
    rows: [row(HOST, 14, 6, 10, 3200), row(FOE, 8, 11, 10, 1900)].join(','),
    rounds: '10',
  });
  assert.equal(out.players, 2);
  assert.equal(match.stats.players.length, 2);
});

test('a steamId that was not in the match is dropped, not recorded', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test',
    rows: [row(HOST, 1, 1, 1, 10), row('76561198000000999', 99, 0, 12, 99999)].join(','),
    rounds: '1',
  });
  assert.equal(out.players, 1, 'only the real roster line is taken');
  assert.equal(match.stats.players.length, 1);
  assert.equal(match.stats.players[0].steamId, HOST);
});

test('only the host may report stats, and only about a live match', () => {
  const svc = service();
  const match = liveMatch(svc);
  assert.equal(svc.gameReportedStats(FOE, { rows: row(FOE, 5, 5, 5, 100) }).ok, false);
  assert.equal(svc.gameReportedStats(HOST, { match: 'somebody-else', rows: row(HOST, 5, 5, 5, 100) }).ok, false);
  match.state = 'ready';
  assert.equal(svc.gameReportedStats(HOST, { rows: row(HOST, 5, 5, 5, 100) }).ok, false);
});

test('a malformed sweep changes nothing', () => {
  const svc = service();
  const match = liveMatch(svc);
  for (const rows of ['', 'nonsense', '123|1:2:3:4', HOST + '|x:y', HOST, HOST + '|']) {
    assert.equal(svc.gameReportedStats(HOST, { match: 'match-under-test', rows }).ok, false, rows);
  }
  assert.equal((match.stats && match.stats.players.length) || 0, 0);
});

test('the stats reach settleMatch and separate the players', () => {
  const svc = service();
  liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: row(HOST, 20, 4, 10, 5000), rounds: '10' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: row(FOE, 2, 15, 10, 600), rounds: '10' });
  const settled = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });
  assert.equal(settled.finished, true);
  assert.ok(svc._internals.ratingOf(HOST).rating > 1500, 'the winner who also carried gained');
  const placed = svc._internals.ratingOf(HOST);
  assert.equal(placed.placementMeasured, 1, 'settlement saves placement performance evidence');
  assert.ok(placed.placementImpact > 0.5, 'the saved evidence reflects above-average impact');
});

console.log('');
console.log('--- the anchor, as it actually behaves in game ---');

// MEASURED on Sam's first hosted launch, 2026-09-15. These are the real payloads, in order.
test('the opening reports name team -1, because the game has not assigned one yet', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '-1|0:0|1:0', limit: '7' });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'team anchor not ready', 'not an error about corruption - the match simply has not started');
  assert.equal(out.anchor, -1);
  assert.equal(match.score, undefined, 'and nothing is recorded from it');
});

test('once the game assigns a team, the anchor maps and is remembered', () => {
  const svc = service();
  const match = liveMatch(svc);            // HOST is on our team 1
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '-1|0:0|1:0', limit: '7' });
  const ok = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:2|1:1', limit: '7' });
  assert.equal(ok.ok, true, ok.error);
  assert.deepEqual(ok.score, { 1: 2, 2: 1 }, 'in-game team 0 is our team 1');
  assert.deepEqual(match.team_map, { 0: 1, 1: 2 });
});

test('a later report whose anchor flickers back to -1 still lands', () => {
  // A host who is respawning or spectating can read -1 again mid-match. The mapping is already
  // known by then, so the report must not be thrown away.
  const svc = service();
  liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:2|1:1', limit: '7' });
  const later = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '-1|0:5|1:3', limit: '7' });
  assert.equal(later.ok, true, later.error);
  assert.deepEqual(later.score, { 1: 5, 2: 3 }, 'mapped from the remembered anchor');
});

test('the anchor maps the other way round just as well', () => {
  const svc = service();
  liveMatch(svc, { hostSide: 2 });         // HOST is on our team 2, in-game team 1
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '1|0:3|1:7', limit: '7' });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.winner, 2, 'in-game team 1 is our team 2, and it reached the limit');
  assert.deepEqual(out.score, { 1: 3, 2: 7 });
});

test('an in-game team the mapping does not know is refused, not guessed', () => {
  const svc = service();
  liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:2|1:1', limit: '7' });
  const odd = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '-1|4:9|5:0', limit: '7' });
  assert.equal(odd.ok, false);
  assert.match(odd.error, /unmapped/);
});

console.log('');
console.log('--- the payloads the game really sends ---');

test('the first live payload parses - it has a NEGATIVE score', () => {
  // Verbatim from the probe log, 2026-09-15 10:52:27. The first pattern accepted only digits
  // for the score, so this matched nothing and every row was binned as 'no usable rows'.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test', rows: HOST + '|0:0:1:-1', rounds: '0',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(match.stats.players[0].kills, 0);
  assert.equal(match.stats.players[0].spawnCount, 1);
  // rounds was '0', so there is no authoritative count yet and none is invented.
  assert.equal('roundsPlayed' in match.stats.players[0], false);
});

test('a negative score is a sentinel and is not recorded as a score', () => {
  // GetPlayerScore answers -1 until the player has properly spawned, then 0. Keeping -1 would
  // rank them last on spr purely for being early.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|0:0:1:-1', rounds: '0' });
  assert.equal(match.stats.players[0].score, undefined, 'dropped, not stored as -1');
  // ...and the real 0 that follows IS kept.
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|0:0:3:0', rounds: '0' });
  assert.equal(match.stats.players[0].score, 0, 'zero is a real score');
  assert.equal(match.stats.players[0].spawnCount, 3);
});

test('a sentinel score leaves the player unjudged on spr, not bottom of it', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|9:2:5:-1', rounds: '5' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: FOE + '|1:8:5:400', rounds: '5' });
  const V = require('../valuation.cjs');
  const rows = V.valuation(match, match.stats, { [HOST]: { rating: 1500, rd: 60, matches: 30 },
                                                 [FOE]: { rating: 1500, rd: 60, matches: 30 } }, 1);
  const me = rows.find((r) => r.steamId === HOST);
  assert.ok(Number.isNaN(me.breakdown.spr), 'no score reported means no opinion, not a bad one');
  assert.ok(me.breakdown.excess > 0, 'and 9-2 still reads as the better game');
});

test('a minus sign is still refused everywhere it is not a sentinel', () => {
  const svc = service();
  const match = liveMatch(svc);
  for (const bad of [HOST + '|-1:0:1:0', HOST + '|0:-1:1:0', HOST + '|0:0:-1:0']) {
    assert.equal(svc.gameReportedStats(HOST, { match: 'match-under-test', rows: bad }).ok, false, bad);
  }
  assert.equal((match.stats && match.stats.players.length) || 0, 0);
});


test('SpawnCount is not rounds played - the live match measured the gap', () => {
  // Verbatim from the 2026-09-15 bot match: the two numbers arrived in the SAME payloads.
  //   ts=1 (round 1) -> spawnCount 3      ts=2 (round 2) -> spawnCount 4
  // Reading the third field as rounds made a 2-round match look like a 4-round one, which is
  // the denominator for kpr and, through presence, the weight of the whole match.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|0:0:4:2', rounds: '2' });
  assert.equal(match.stats.players[0].spawnCount, 4, 'kept, as the diagnostic it is');
  assert.equal('roundsPlayed' in match.stats.players[0], false, 'never per-player; it goes stale');
  assert.equal(match.stats.rounds, 2, 'the match carries the one authoritative count');
});

test('a stale row cannot shrink the round count the valuation uses', () => {
  // The sweep visits one player every 3 s, so the host's last row was still round 1 when the
  // 2026-09-15 match settled on round 2. The count must come from the match, never the row.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|0:0:3:1', rounds: '1' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: FOE + '|1:0:4:2', rounds: '2' });
  assert.equal(match.stats.rounds, 2);
  const host = match.stats.players.find((p) => p.steamId === HOST);
  assert.equal('roundsPlayed' in host, false, 'his row is a round behind and says nothing about it');
  const m = valuation.valuation(match, match.stats, {}, 1).find((r) => r.steamId === HOST);
  assert.equal(m.breakdown.roundsPlayed, 2, 'measured across the match, not his stale row');
});


console.log('');
console.log('--- the collection window ---');

test('a decided match is held open for the last stats, not settled at once', () => {
  // MEASURED 2026-09-15: the real end (OnMatchEnded -> ch_exit_armed, 20:41:36.5) was 11.7 s
  // before the score report that told us (20:41:48.2), and the sweep visits one player every
  // 3 s. Settling on that report settles on stats up to half a minute stale.
  const svc = service(30);
  const match = liveMatch(svc);
  const out = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });
  assert.equal(out.finished, true, 'decided immediately - the winner never changes again');
  assert.equal(out.collecting, true, 'but not settled');
  assert.equal(match.finished, undefined, 'no result written yet');
  assert.ok(svc._internals.matches.has('match-under-test'), 'still open, so stats still land');
});

test('the window closes early once every player has reported since the end', () => {
  const svc = service(30);
  const match = liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });
  assert.equal(svc._internals.collectionComplete(match), false, 'nobody has reported yet');
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|9:3:7:900', rounds: '7' });
  assert.equal(svc._internals.collectionComplete(match), false, 'one of two');
  const out = svc.gameReportedStats(HOST, { match: 'match-under-test', rows: FOE + '|3:9:7:300', rounds: '7' });
  assert.equal(out.settled, true, 'the roster is covered, so it settles without waiting out the clock');
  assert.ok(match.finished, 'and the result is written');
});

test('a stats report that arrives after the end is USED, not refused', () => {
  // This is the whole point. These reports used to hit 'no match' because the match had already
  // been deleted - and they are the freshest numbers of the entire match.
  const svc = service(30);
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|1:1:1:10', rounds: '1' });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });
  const out = svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|9:3:7:900', rounds: '7' });
  assert.equal(out.ok, true, out.error);
  const row = match.stats.players.find((p) => p.steamId === HOST);
  assert.equal(row.kills, 9, 'the final numbers replaced the mid-match ones');
  assert.equal(match.stats.rounds, 7);
});

test('the result cannot change once the match is decided', () => {
  // The game keeps reporting the final scoreline while it sits on the end screen.
  const svc = service(30);
  const match = liveMatch(svc);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });
  const again = svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:1|1:1', limit: '7' });
  assert.equal(again.ok, true, 'acknowledged');
  assert.equal(again.winner, 1, 'and still the same winner');
  assert.deepEqual(match.score, { 1: 7, 2: 0 }, 'the scoreline was not re-applied');
});

test('beginCollection is idempotent - OnMatchEnded fires more than once', () => {
  // Seen twice in the 2026-09-15 match, 46 s apart.
  const svc = service(30);
  const match = liveMatch(svc);
  svc._internals.beginCollection(match, 1, { 1: 7, 2: 0 }, 7);
  const first = match.collecting.since;
  svc._internals.beginCollection(match, 2, { 1: 0, 2: 7 }, 7);
  assert.equal(match.collecting.since, first, 'the first call won');
  assert.equal(match.collecting.winner, 1, 'and the second could not flip the winner');
});

console.log('');
console.log('--- the per-round series (layer 1b) ---');

test('every sample is kept, keyed by the round it was stamped with', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|1:0:3:10', rounds: '1' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|4:1:4:40', rounds: '2' });
  assert.deepEqual(Object.keys(match.stats.series[HOST]).sort(), ['1', '2']);
  assert.equal(match.stats.series[HOST][1].kills, 1);
  assert.equal(match.stats.series[HOST][2].kills, 4);
  // and the running totals the valuation reads are untouched by any of this
  assert.equal(match.stats.players[0].kills, 4, 'last write still wins for the totals');
});

test('within a round the last sample wins', () => {
  // The sweep visits a player about six times in a 180 s round; the latest is the one closest to
  // the end of it, which is what a per-round delta has to be measured from.
  const svc = service();
  const match = liveMatch(svc);
  for (const k of [1, 2, 3]) {
    svc.gameReportedStats(HOST, { match: 'match-under-test', rows: `${HOST}|${k}:0:3:0`, rounds: '2' });
  }
  assert.equal(Object.keys(match.stats.series[HOST]).length, 1, 'one entry for round 2');
  assert.equal(match.stats.series[HOST][2].kills, 3, 'the latest sample');
});

test('a LATE report is filed under its own round, not the match high-water mark', () => {
  // stats.rounds is the match's furthest round; a report stamped 1 arriving during round 3 must
  // not be filed under 3, or it corrupts two rounds at once.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|9:0:3:90', rounds: '3' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|2:0:3:20', rounds: '1' });
  assert.equal(match.stats.rounds, 3, 'the high-water mark does not go backwards');
  assert.equal(match.stats.series[HOST][1].kills, 2, 'filed under round 1');
  assert.equal(match.stats.series[HOST][3].kills, 9, 'and round 3 is untouched');
});

test('round 0 is kept - it is the baseline every delta counts from', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|0:0:1:0', rounds: '0' });
  assert.ok(match.stats.series[HOST][0], 'warm-up sample kept');
  assert.equal(match.stats.rounds, undefined, 'but it is not a round the match has reached');
});

test('deltas say what happened IN each round', () => {
  const svc = service();
  const match = liveMatch(svc);
  const send = (k, d, sc, r) =>
    svc.gameReportedStats(HOST, { match: 'match-under-test', rows: `${HOST}|${k}:${d}:9:${sc}`, rounds: String(r) });
  send(0, 0, 0, 0);
  send(3, 1, 30, 1);
  send(4, 3, 45, 2);
  const per = valuation.roundDeltas(match.stats)[HOST];
  assert.deepEqual(per.map((e) => e.round), [1, 2], 'warm-up is dropped');
  assert.deepEqual(per[0], { round: 1, from: 0, kills: 3, deaths: 1 });
  assert.deepEqual(per[1], { round: 2, from: 1, kills: 1, deaths: 2 },
    'one kill and two deaths in round 2 - totals alone could never say that');
});

test('a missing round is skipped, not treated as zero', () => {
  // The sweep can genuinely miss a player for a whole short round. The delta then spans the gap
  // and says so, rather than handing round 3 everything rounds 2 and 3 earned.
  const svc = service();
  const match = liveMatch(svc);
  const send = (k, r) =>
    svc.gameReportedStats(HOST, { match: 'match-under-test', rows: `${HOST}|${k}:0:9:0`, rounds: String(r) });
  send(0, 1);
  send(6, 3);
  const per = valuation.roundDeltas(match.stats)[HOST];
  assert.deepEqual(per.map((e) => e.round), [1, 3], 'round 2 was never sampled and is not invented');
  assert.deepEqual(per[1], { round: 3, from: 1, kills: 6, deaths: 0 });
  assert.equal(per[1].from, 1, 'the gap is visible, so nobody can mistake this for one round');
});

test('an unreported field yields no delta rather than a zero one', () => {
  const svc = service();
  const match = liveMatch(svc);
  // score is the -1 sentinel in the first sample, so it is never recorded and cannot be differenced
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|0:0:1:-1', rounds: '1' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|2:0:2:50', rounds: '2' });
  const per = valuation.roundDeltas(match.stats, ['kills', 'teamScore'])[HOST];
  assert.equal(per[1].kills, 2);
  assert.equal('teamScore' in per[1], false, 'no baseline for it, so no delta for it');
});

test('warm-up is not a baseline - the counters are cleared at match start', () => {
  // Sam, from his own time in the game: "once the game starts, the kills and death are kept until
  // the game ends". So round 0 belongs to a counter that is about to be wiped, and the first real
  // round is measured against ZERO - what the clear leaves behind - not against warm-up.
  const svc = service();
  const match = liveMatch(svc);
  const send = (k, r) =>
    svc.gameReportedStats(HOST, { match: 'match-under-test', rows: `${HOST}|k=${k};d=0`, rounds: String(r) });
  send(2, 0);        // warm-up, discarded at match start
  send(-1, 1);
  send(1, 2);
  const per = valuation.roundDeltas(match.stats, ['kills'])[HOST];
  assert.deepEqual(per.map((e) => e.round), [1, 2], 'warm-up is dropped, not differenced');
  assert.equal(per[0].kills, -1, 'measured against zero, not against the warm-up total');
  assert.equal(per[1].kills, 2, 'and +2 in round 2 - which is what Sam described');
});

test('a negative ROUND is real work, not a counter restart', () => {
  // Kill is a NET score: two team kills and no enemies is a genuinely -2 round. Reading a fall as
  // a restart would report the new total instead of the change, turning -2 into -1 and flagging
  // it as an artefact.
  const svc = service();
  const match = liveMatch(svc);
  const send = (k, r) =>
    svc.gameReportedStats(HOST, { match: 'match-under-test', rows: `${HOST}|k=${k};d=1`, rounds: String(r) });
  send(1, 1);
  send(-1, 2);
  const per = valuation.roundDeltas(match.stats, ['kills'])[HOST];
  assert.equal(per[1].kills, -2, 'the change, not the total');
  assert.equal('reset' in per[1], false, 'and not mistaken for an artefact');
});

test('a count that genuinely cannot fall is still guarded', () => {
  const svc = service();
  const match = liveMatch(svc);
  match.stats = { series: { [HOST]: { 1: { deaths: 4 }, 2: { deaths: 1 } } } };
  const per = valuation.roundDeltas(match.stats, ['deaths'])[HOST];
  assert.equal(per[1].deaths, 1);
  assert.equal(per[1].reset, true, 'deaths cannot go backwards, so that IS a restart');
});

console.log('');
console.log('--- the keyed row ---');

test('a keyed row carries everything the positional one could, and more', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test', rounds: '9',
    rows: HOST + '|k=14;d=6;t=0;s=3200;sp=10;a=1;b=0;l=0;p=34;pt=ab12',
  });
  assert.equal(out.ok, true, out.error);
  const row = match.stats.players[0];
  assert.equal(row.kills, 14);
  assert.equal(row.deaths, 6);
  assert.equal(row.teamId, 0);
  assert.equal(row.teamScore, 3200, 'the game score is the TEAM score - see the Layer 1 match');
  assert.equal(row.spawnCount, 10);
  assert.equal(row.alive, true);
  assert.equal(row.lateJoin, false);
  assert.equal(row.ping, 34);
  assert.equal(row.partyId, 'ab12');
});

test('an UNKNOWN key is ignored, so the pak can get ahead of the server', () => {
  // The whole point of keying. A pak change is a cook, a repack and a reinstall; it must not have
  // to land in the same breath as a server deploy.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test', rows: HOST + '|k=5;zz=99;hs=3;d=2', rounds: '4',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(match.stats.players[0].kills, 5);
  assert.equal(match.stats.players[0].deaths, 2);
  assert.equal('zz' in match.stats.players[0], false);
});

test('order does not matter, which is the bug positional kept having', () => {
  const svc = service();
  const a = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|d=2;k=5', rounds: '4' });
  const svc2 = service();
  const b = liveMatch(svc2);
  svc2.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=5;d=2', rounds: '4' });
  assert.deepEqual(a.stats.players[0], b.stats.players[0]);
});

test('a missing key is absent, never zero', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=5', rounds: '4' });
  assert.equal(match.stats.players[0].kills, 5);
  assert.equal('deaths' in match.stats.players[0], false,
    'not reported is not zero - nought deaths is a flattering scoreline to invent');
});

test('the -1 sentinels stay sentinels', () => {
  // Both were measured live: GetPlayerScore is -1 until the player spawns, TeamID is -1 for the
  // first ~30 s of the match world.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=0;d=0;s=-1;t=-1', rounds: '0' });
  assert.equal('score' in match.stats.players[0], false, 'not stored as -1');
  assert.equal('teamId' in match.stats.players[0], false);
  assert.equal(match.stats.players[0].kills, 0, 'a real zero IS stored');
});

test('a count that cannot be negative and is drops the whole row', () => {
  // `k` is NOT one of those any more - a team kill decrements it, measured. `d` still is.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=3;d=-1', rounds: '4' });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'no usable rows');
  assert.equal((match.stats && match.stats.players || []).length, 0);
});

test('a row that says it is a bot never reaches a ladder', () => {
  // The pak skips bots at the source now, but an older pak does not, and b=1 is a much better
  // signal than the blank SteamID we had been inferring it from.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=9;d=0;b=1', rounds: '4' });
  assert.equal(out.ok, false, 'dropped');
  assert.equal((match.stats && match.stats.players || []).length, 0);
});

test('keyed and positional rows can arrive in the same match', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|3:1:4:40', rounds: '2' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: FOE + '|k=1;d=3;t=1', rounds: '2' });
  const host = match.stats.players.find((p) => p.steamId === HOST);
  const foe = match.stats.players.find((p) => p.steamId === FOE);
  assert.equal(host.kills, 3);
  assert.equal(foe.kills, 1);
  assert.equal(foe.teamId, 1);
});

test('the per-round series works the same for keyed rows', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=1;d=0', rounds: '1' });
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=4;d=2', rounds: '2' });
  const per = valuation.roundDeltas(match.stats, ['kills', 'deaths'])[HOST];
  assert.deepEqual(per.map((e) => e.round), [1, 2]);
  assert.deepEqual(per[1], { round: 2, from: 1, kills: 3, deaths: 2 });
});

console.log('');
console.log('--- the round snapshot ---');

test('a round is recorded with what it was', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row: 'n=3;w=1;sec=47;a0=0;a1=2',
  });
  assert.equal(out.ok, true, out.error);
  assert.deepEqual(match.rounds[3],
    { round: 3, winTeam: 1, seconds: 47, alive0: 0, alive1: 2, source: 'delegate' });
});

test('a repeated round is idempotent, not a duplicate', () => {
  const svc = service();
  const match = liveMatch(svc);
  const send = () => svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row: 'n=1;w=2;sec=30;a0=3;a1=0' });
  send(); send();
  assert.equal(Object.keys(match.rounds).length, 1);
});

test('a -1 winner is the game saying nobody, not team -1', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.gameReportedRound(HOST, { match: 'match-under-test', row: 'n=1;w=-1;sec=12' });
  assert.equal('winTeam' in match.rounds[1], false, 'dropped, like every other -1 sentinel here');
  assert.equal(match.rounds[1].seconds, 12, 'the rest of the row still lands');
});

test('an unknown key is ignored here too', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.gameReportedRound(HOST, { match: 'match-under-test', row: 'n=2;w=0;bomb=1;zz=9' });
  assert.deepEqual(match.rounds[2], { round: 2, winTeam: 0, source: 'delegate' });
});

test('a row with no round number is refused rather than filed somewhere', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedRound(HOST, { match: 'match-under-test', row: 'w=1;sec=20' });
  assert.equal(out.ok, false);
  assert.equal(match.rounds, undefined);
});

test('only the host may report a round', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc._internals.gameReportedRound(FOE, { match: 'match-under-test', row: 'n=1;w=1' });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'not the host');
});

test('a round from a DIFFERENT match is refused', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc._internals.gameReportedRound(HOST, { match: 'some-other-match', row: 'n=1;w=1' });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'wrong match id');
});

console.log('');
console.log('--- what the first Layer 1 match taught us ---');

test('kills are a NET score and CAN be negative - a team kill decrements them', () => {
  // Verbatim, 2026-09-15 23:19:53. Sam killed one enemy and two team-mates in round 1 and `Kill`
  // read -1. Treating it as a count that cannot be negative dropped every sample of that round.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, {
    match: 'match-under-test', rounds: '1',
    rows: HOST + '|k=-1;d=0;sp=2;s=1;t=0;a=true',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(match.stats.players[0].kills, -1, 'kept, not binned as corrupt');
});

test('deaths are still a count, and a negative one is still corrupt', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=1;d=-2', rounds: '1' });
  assert.equal(out.ok, false, 'nothing measured says deaths can go backwards');
  assert.equal((match.stats && match.stats.players || []).length, 0);
});

test('the game score is the TEAM score, and never lands in a per-player field', () => {
  // It tracked the team's round wins exactly: 0 at 0:0, 1 at 0:1, 2 at 0:2. The same number for
  // all five players on a side, so it discriminates nobody - and feeding it to a performance
  // component would count the match outcome a second time.
  const svc = service();
  const match = liveMatch(svc);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=1;d=0;s=2', rounds: '2' });
  const row = match.stats.players[0];
  assert.equal(row.teamScore, 2);
  assert.equal('score' in row, false, 'spr must get nothing until a real per-player score exists');
});

test('the whole round-1 payload replays end to end', () => {
  // Every stat row from the 23:19 match, verbatim, in order.
  const svc = service();
  const match = liveMatch(svc);
  const rows = [
    ['k=0;d=0;sp=1;s=-1;t=-1;a=true', '0'],
    ['k=1;d=0;sp=1;s=-1;t=-1;a=true', '0'],
    ['k=0;d=0;sp=2;s=0;t=0;a=true', '0'],
    ['k=-1;d=0;sp=2;s=1;t=0;a=true', '1'],
    ['k=-1;d=0;sp=3;s=1;t=0;a=true', '1'],
    ['k=1;d=0;sp=3;s=2;t=0;a=true', '2'],
  ];
  for (const [row, round] of rows) {
    const out = svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|' + row, rounds: round });
    assert.equal(out.ok, true, `${row}: ${out.error}`);
  }
  assert.deepEqual(Object.keys(match.stats.series[HOST]).sort(), ['0', '1', '2']);
  assert.equal(match.stats.series[HOST][1].kills, -1, 'round 1 survives, which it did not before');
  assert.equal(match.stats.series[HOST][2].kills, 1);
  assert.equal(match.stats.series[HOST][0].teamId, 0, 'the -1 anchor resolved inside round 0');
});

test('a round snapshot from the real match parses', () => {
  // Verbatim, 23:19:47. This is OnRoundEnded proving it fires at all.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row: 'n=0;w=0;sec=14;a0=1;a1=0',
  });
  assert.equal(out.ok, true, out.error);
  assert.deepEqual(match.rounds[0],
    { round: 0, winTeam: 0, seconds: 14, alive0: 1, alive1: 0, source: 'delegate' });
});

console.log('');
console.log('--- the counter is blind; the feed is not ---');

test("Sam's case: a team kill cancelled by an enemy kill leaves the counter at zero", () => {
  // "lets say a teammate kills a teammate to get -1 kill count but then kills an enemy to bring it
  // back to 0... it would have ignored the teamkill". The counter is netted and cannot show it.
  // The feed records two events, and two events cannot cancel.
  const svc = service();
  const match = liveMatch(svc);
  match.team_map = { 0: 1, 1: 2 };
  const mate = '76561198000000003';
  addFixturePlayer(match, { steam_id: mate, accepted: true, connected: true });
  match.teams[1].push(mate);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: `${HOST}|k=0;d=0`, rounds: '1' });
  svc._internals.gameReportedKill(HOST, { match: 'match-under-test', row: `k=${HOST};v=${mate};n=1` });
  svc._internals.gameReportedKill(HOST, { match: 'match-under-test', row: `k=${HOST};v=${FOE};n=1` });
  assert.equal(match.stats.players[0].kills, 0, 'the counter nets to nothing');
  const tally = valuation.killCounts(match)[HOST];
  assert.deepEqual({ ...tally }, { enemyKills: 1, teamKills: 1, deathsByKill: 0 },
    'while the feed keeps them apart');
});

test('the net counter is never used as a kill metric', () => {
  // Sam: "net score/kills is a useless metric". With no feed the kill component is NOT MEASURED,
  // and the component table drops it and redistributes its weight - better than a number known to
  // mislead.
  const noFeed = valuation.playerMetrics({ steamId: 'x', kills: 7, deaths: 2 }, { rounds: 10 });
  assert.ok(Number.isNaN(noFeed.kpr), 'no feed, no kill metric - the net score is not a substitute');
  const withFeed = valuation.playerMetrics({ steamId: 'x', enemyKills: 6, deaths: 2 }, { rounds: 10 });
  assert.equal(withFeed.kpr, 0.6, 'enemy kills per round, from the feed');
});

console.log('');
console.log('--- the kill feed ---');

test('a kill names both players, and OUR roster decides whose side they were on', () => {
  const svc = service();
  const match = liveMatch(svc);            // HOST on team 1, FOE on team 2
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=${FOE};n=2;t=41;a0=3;a1=1`,
  });
  assert.equal(out.ok, true, out.error);
  assert.deepEqual(match.kills[0], {
    killer: HOST, victim: FOE, round: 2, at: 41, alive0: 3, alive1: 1,
    victimTeam: 2, killerTeam: 1, teamKill: false, suicide: false,
  });
});

test('a TEAM KILL is a lookup, not an inference - and the host cannot hide it', () => {
  // The server assigned the teams when it formed the match, so this needs no TeamID from the game
  // at all. A host that wanted to conceal its team killing could report any TeamID it liked; it
  // cannot rewrite who we put on which side.
  const svc = service();
  const { matches } = svc._internals;
  const match = liveMatch(svc);
  const mate = '76561198000000003';
  addFixturePlayer(match, { steam_id: mate, persona: 'mate', accepted: true, connected: true });
  match.teams[1].push(mate);
  svc._internals.inMatch.set(mate, match.id);
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=${mate};n=1;t=12`,
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.kill.teamKill, true);
  assert.equal(matches.get('match-under-test').kills[0].teamKill, true);
});

test('this is exactly what the kill COUNTER cannot see', () => {
  // Sam's case: a team kill then an enemy kill nets to zero and the counter shows nothing. Two
  // events cannot cancel.
  const svc = service();
  const match = liveMatch(svc);
  const mate = '76561198000000003';
  addFixturePlayer(match, { steam_id: mate, accepted: true, connected: true });
  match.teams[1].push(mate);
  const kill = (k, v) => svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${k};v=${v};n=1` });
  kill(HOST, mate);      // team kill
  kill(HOST, FOE);       // and an enemy, cancelling it in the counter
  assert.equal(match.kills.length, 2);
  assert.equal(match.kills.filter((x) => x.teamKill).length, 1,
    'still exactly one team kill, however the arithmetic nets out');
});

test('a world kill has no killer and blames nobody', () => {
  // Fall damage, the bomb, any world kill: the victim has no instigator.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=;v=${FOE};n=3;t=8`,
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(match.kills[0].world, true);
  assert.equal(match.kills[0].teamKill, false);
  assert.equal('killer' in match.kills[0], false);
});

test('a suicide is not a team kill', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.gameReportedKill(HOST, { match: 'match-under-test', row: `k=${HOST};v=${HOST};n=1` });
  assert.equal(match.kills[0].suicide, true);
  assert.equal(match.kills[0].teamKill, false, 'you cannot team-kill yourself');
});

test('a player the server never put in this match is refused', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=76561198000000099;n=1`,
  });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'victim not on the roster');
  assert.equal(match.kills, undefined);
});

test('a row naming NOBODY is refused', () => {
  // An unnamed victim alone is no longer fatal - bots have no SteamID, so in a bot match no victim
  // can ever be named, and refusing those threw away every kill that proved this works. A row with
  // neither end named is still nothing to record.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedKill(HOST, { match: 'match-under-test', row: 'n=1;t=5' });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'neither killer nor victim');
  assert.equal(match.kills, undefined);
});

test('only the host may report a kill', () => {
  const svc = service();
  liveMatch(svc);
  const out = svc._internals.gameReportedKill(FOE, { match: 'match-under-test', row: `k=${FOE};v=${HOST}` });
  assert.equal(out.ok, false);
  assert.equal(out.error, 'not the host');
});

test('the heartbeat fills a round the delegate never reported', () => {
  // The measured risk: OnRoundEnded fired once in a three-round match. Correctness must not
  // depend on it, so the same payload also arrives from a plain looping timer.
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row: 'n=2;w=1;sec=30;a0=0;a1=3', source: 'heartbeat',
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(match.rounds[2].winTeam, 1);
  assert.equal(match.rounds[2].source, 'heartbeat');
});

test('a heartbeat never overwrites what the delegate reported', () => {
  // The delegate row was taken AT the round's end; a later sample cannot improve on it.
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.gameReportedRound(HOST, { match: 'match-under-test', row: 'n=1;w=1;sec=45;a0=0;a1=2' });
  const out = svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row: 'n=1;w=1;sec=3;a0=5;a1=5', source: 'heartbeat' });
  assert.equal(out.kept, 'delegate');
  assert.equal(match.rounds[1].seconds, 45, 'the exact row survived');
});

test('a later heartbeat DOES replace an earlier one', () => {
  // Within a round the heartbeat overwrites itself, so what is kept is the last sample taken
  // during it - the closest thing to the round's end state.
  const svc = service();
  const match = liveMatch(svc);
  const beat = (row) => svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row, source: 'heartbeat' });
  beat('n=1;w=-1;sec=5;a0=5;a1=5');
  beat('n=1;w=0;sec=48;a0=3;a1=0');
  assert.equal(match.rounds[1].seconds, 48);
  assert.equal(match.rounds[1].winTeam, 0, 'and the winner, once the game knew it');
});

test('a mid-round heartbeat cannot invent a winner', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row: 'n=1;w=-1;sec=5;a0=5;a1=5', source: 'heartbeat' });
  assert.equal('winTeam' in match.rounds[1], false, '-1 is the game saying it does not know yet');
});

test('the alive counts name the victim side when the victim cannot be named', () => {
  // In a bot match no victim has a SteamID, so none can ever be named - and the alive counts still
  // say which side lost somebody. With our own roster saying whose side the KILLER is on, that is
  // all a team kill needs.
  const svc = service();
  const match = liveMatch(svc);
  match.team_map = { 0: 1, 1: 2 };          // game team 0 is our side 1, which is HOST's
  const beat = (row) => svc._internals.gameReportedRound(HOST, {
    match: 'match-under-test', row, source: 'heartbeat' });
  const kill = (row) => svc._internals.gameReportedKill(HOST, { match: 'match-under-test', row });
  beat('n=1;w=-1;sec=0;a0=5;a1=5');         // the round-start baseline
  kill(`k=${HOST};v=;n=1;t=1;a0=4;a1=5`);   // team 0 lost one, and HOST is on team 0
  kill(`k=${HOST};v=;n=1;t=9;a0=4;a1=4`);   // team 1 lost one
  assert.equal(match.kills[0].teamKill, true, 'a team kill, inferred from the count');
  assert.equal(match.kills[0].inferred, true, 'and said to be an inference, not two named accounts');
  assert.equal(match.kills[1].teamKill, false);
});

test("the round's FIRST kill needs the heartbeat, and that is why it exists", () => {
  // Replaying the real match without a round-start baseline missed exactly one kill per round -
  // the first - because there was no earlier kill to difference against.
  const svc = service();
  const match = liveMatch(svc);
  match.team_map = { 0: 1, 1: 2 };
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=;n=1;t=1;a0=4;a1=5` });
  assert.equal(out.ok, true, out.error);
  assert.equal(match.kills[0].teamKill, false, 'nothing to compare with, so nothing is claimed');
  assert.equal('victimTeam' in match.kills[0], false);
});

test('the team map is learned from the host row, not only from a score report', () => {
  // The score report is on a 15 s timer, so team_map did not exist for most of the first round -
  // which cost four of six team kills on the real match. The host's own stat row settles it.
  const svc = service();
  const match = liveMatch(svc);        // HOST is on our side 1
  assert.equal(match.team_map, undefined);
  svc.gameReportedStats(HOST, { match: 'match-under-test', rows: HOST + '|k=0;d=0;t=0', rounds: '1' });
  assert.deepEqual(match.team_map, { 0: 1, 1: 2 }, 'two teams, so naming one names the other');
});

test('a named victim always beats the inference', () => {
  const svc = service();
  const match = liveMatch(svc);
  match.team_map = { 0: 1, 1: 2 };
  svc._internals.gameReportedRound(HOST, { match: 'match-under-test', row: 'n=1;w=-1;a0=5;a1=5', source: 'heartbeat' });
  // the counts say team 0 lost somebody, but the victim is NAMED and is on the other side
  svc._internals.gameReportedKill(HOST, { match: 'match-under-test', row: `k=${HOST};v=${FOE};n=1;a0=4;a1=5` });
  assert.equal(match.kills[0].teamKill, false, 'two named accounts settle it');
  assert.equal('inferred' in match.kills[0], false);
});

test('a team kill from the feed reaches the enforcement, verdict and all', () => {
  // Sam: "i still want to hold the punishments accordingly... but using the feed is the most
  // accurate way to get how many teamkills there were". The rules are unchanged; only the input is.
  const svc = service();
  const match = liveMatch(svc);
  const mate = '76561198000000003';
  addFixturePlayer(match, { steam_id: mate, accepted: true, connected: true });
  match.teams[1].push(mate);
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=${mate};n=2;t=40;a0=3;a1=3`,
  });
  assert.equal(out.ok, true, out.error);
  assert.equal(out.kill.teamKill, true);
  assert.ok(out.verdict && out.verdict.ok, 'it was ruled on');
  assert.ok(Array.isArray(match.teamkills) && match.teamkills.length === 1,
    'and recorded where the enforcement keeps them');
});

test('an INFERRED team kill is not punished - a count is not a person', () => {
  // The alive counts can tell us a team-mate died without telling us WHICH. That is enough to
  // rate a match and not enough to ban somebody.
  const svc = service();
  const match = liveMatch(svc);
  match.team_map = { 0: 1, 1: 2 };
  svc._internals.gameReportedRound(HOST, { match: 'match-under-test', row: 'n=1;a0=5;a1=5', source: 'heartbeat' });
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=;n=1;t=9;a0=4;a1=5` });
  assert.equal(out.kill.teamKill, true, 'still counted for the rating');
  assert.equal(out.kill.inferred, true);
  assert.equal(out.verdict, null, 'but nobody is punished on an inference');
  assert.equal(match.teamkills, undefined);
});

test('an enemy kill is never referred to the enforcement', () => {
  const svc = service();
  const match = liveMatch(svc);
  const out = svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=${FOE};n=1;t=20` });
  assert.equal(out.kill.teamKill, false);
  assert.equal(out.verdict, null);
  assert.equal(match.teamkills, undefined);
});

console.log('--- the scoreboard reaches the archive ---');

/** Archive the match the way go-live does, so `readMatch` has a record to patch. */
function archived(svc, match) {
  svc._internals.archiveMatch(match, { outcome: 'played' });
  return match;
}

test('a settled match archives a per-player board', async () => {
  const svc = service();
  const match = archived(svc, liveMatch(svc));
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '7',
    rows: `${HOST}|k=14;d=6;t=0;w=5;c=1;p=34,${FOE}|k=5;d=13;t=1;w=2` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const full = await svc._internals.readMatch('match-under-test', HOST);
  const board = full.scoreboard;
  assert.equal(board.length, 2, 'one row per player');
  const mine = board.find((r) => r.steam_id === HOST);
  assert.equal(mine.kills, 14);
  assert.equal(mine.deaths, 6);
  assert.equal(mine.rounds_won, 5);
  assert.equal(mine.clutches, 1);
  assert.equal(mine.ping, 34);
  assert.equal(mine.team, 1, 'OUR team, not the in-game one');
  assert.equal(mine.reported, true);
  assert.equal(full.rounds_played, 10, 'the final 7–3 score is ten displayed rounds');
});

test('a player the sweep never reached gets a row of nulls, not zeroes', async () => {
  const svc = service();
  const match = archived(svc, liveMatch(svc));
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '3',
    rows: `${HOST}|k=4;d=1` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });

  const full = await svc._internals.readMatch('match-under-test', HOST);
  const theirs = full.scoreboard.find((r) => r.steam_id === FOE);
  assert.equal(theirs.reported, false, 'the board still shows them');
  assert.equal(theirs.kills, null, 'null, because 0 would read as "they went 0-0"');
  assert.equal(theirs.deaths, null);
});

test('team kills on the board come from the feed, not from the netted counter', async () => {
  const svc = service();
  // Two team kills by the host, one enemy kill. The netted `Kill` counter would imply fewer.
  const mate = '76561198000000003';
  const match = liveMatch(svc, { hostSide: 1 });
  addFixturePlayer(match, { steam_id: mate, persona: 'mate', accepted: true, connected: true });
  match.teams[1].push(mate);
  svc._internals.inMatch.set(mate, match.id);
  archived(svc, match);
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '1',
    rows: `${HOST}|k=-1;d=0;t=0` });
  for (let i = 0; i < 2; i += 1) {
    svc._internals.gameReportedKill(HOST, {
      match: 'match-under-test', row: `k=${HOST};v=${mate};n=1;t=${10 + i}` });
  }
  svc._internals.gameReportedKill(HOST, {
    match: 'match-under-test', row: `k=${HOST};v=${FOE};n=1;t=20` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });

  const full = await svc._internals.readMatch('match-under-test', HOST);
  const mine = full.scoreboard.find((r) => r.steam_id === HOST);
  assert.equal(mine.team_kills, 2, 'exactly the two named team kills');
  assert.equal(mine.kills, -1, 'and the net counter is shown as the game reported it');
  const theirs = full.scoreboard.find((r) => r.steam_id === FOE);
  assert.equal(theirs.team_kills, 0, 'a real zero once a feed exists');
});

test('no feed at all means null team kills, never a zero we cannot back up', async () => {
  const svc = service();
  archived(svc, liveMatch(svc));
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '2',
    rows: `${HOST}|k=3;d=2` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:1', limit: '7' });
  const full = await svc._internals.readMatch('match-under-test', HOST);
  assert.equal(full.scoreboard.find((r) => r.steam_id === HOST).team_kills, null);
});

test('a match that ended before the gamemode said anything has no board', async () => {
  const svc = service();
  archived(svc, liveMatch(svc));
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:0', limit: '7' });
  const full = await svc._internals.readMatch('match-under-test', HOST);
  assert.deepEqual(full.scoreboard, [], 'empty, so the hub knows not to draw one');
});

test("the player's own K/D lands on their history row", async () => {
  const svc = service();
  archived(svc, liveMatch(svc));
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '7',
    rows: `${HOST}|k=14;d=6,${FOE}|k=5;d=13` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const rows = await svc._internals.readHistory(HOST);
  const row = rows.find((r) => r.id === 'match-under-test');
  assert.equal(row.kills, 14, 'so the list needs no detail fetch to show a K/D');
  assert.equal(row.deaths, 6);
  assert.equal(row.won, true);
  const theirs = (await svc._internals.readHistory(FOE)).find((r) => r.id === 'match-under-test');
  assert.equal(theirs.kills, 5, "and it is THEIR line, not the host's");
});

test('a stalled match keeps the board the sweep did see', async () => {
  const svc = service();
  const match = archived(svc, liveMatch(svc));
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '4',
    rows: `${HOST}|k=8;d=4,${FOE}|k=4;d=8` });
  // No score ever reaches the limit; the ceiling closes it instead.
  svc._internals.expireLive('match-under-test');

  const full = await svc._internals.readMatch('match-under-test', HOST);
  assert.equal(full.outcome, 'played', 'archiveMatch is idempotent - it is still the go-live row');
  assert.equal(full.scoreboard.length, 2, 'but its scoreboard is no longer the empty one');
  assert.equal(full.scoreboard.find((r) => r.steam_id === HOST).kills, 8);
});

console.log('--- the scoreboard rides with the result ---');

/** Attach a fake SSE client for `steamId` and return the events it receives. */
function listen(svc, steamId) {
  const { clients, bySteam } = svc._internals;
  const got = [];
  const clientId = `c-${steamId}`;
  clients.set(clientId, { steamId, res: { write: (chunk) => {
    // The wire format is an SSE data frame; parse it back, so a test asserts on the event a
    // hub would actually receive rather than on an object we handed the service ourselves.
    const line = String(chunk).replace(/^data: /, '').trim();
    try { got.push(JSON.parse(line)); } catch { /* heartbeat comment frames */ }
  } } });
  bySteam.set(steamId, new Set([clientId]));
  return got;
}

test('match_result carries the RR the match moved, at full size in a quality-0 pairing', () => {
  // Sam, 2026-09-16: "we are only gaining and losing 1-3 RR". Two bugs made that. The card printed
  // `arrows` with "RR" after it, and the RR underneath was scaled by the matchmaker's quality -
  // which a 1v1 whose MMRs have drifted apart scores at 0 - so a win really did pay about +8.
  const svc = service();
  placedPair(svc);
  const match = liveMatch(svc);
  match.mm.quality = 0;
  svc._internals.archiveMatch(match, { outcome: 'played' });
  const mine = listen(svc, HOST);
  const theirs = listen(svc, FOE);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:4', limit: '7' });

  const won = mine.find((e) => e.type === 'match_result');
  const lost = theirs.find((e) => e.type === 'match_result');
  assert.equal(won.you.rr_delta, svc._internals.ratingOf(HOST).progress - 950,
               'the number is exactly what the badge moved');
  assert.equal(won.rr_delta, won.you.rr_delta, 'flat as well as under `you`, like every other field');
  assert.equal(won.you.arrows, rating.arrowsFor(won.you.rr_delta), 'and the arrows agree with it');
  assert.equal(won.you.placed, false);
  assert.ok(won.you.rr_delta >= progress.BASE_WIN,
            `a win pays at least the base whatever the pairing, paid ${won.you.rr_delta}`);
  assert.ok(lost.you.rr_delta <= -(progress.BASE_LOSS - progress.RR_ROUND_DIFF),
            `and a loss costs at least the close-loss figure, cost ${lost.you.rr_delta}`);
});

test('the match that finishes placements says so, and pays no RR', () => {
  const svc = service();
  for (const id of [HOST, FOE]) {
    svc._internals.saveRating(id, { rating: 1500, rd: 200, matches: rating.PLACEMENT_MATCHES - 1,
                                    wins: 2, losses: 2, progress: 0 });
  }
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played' });
  const mine = listen(svc, HOST);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:4', limit: '7' });

  const result = mine.find((e) => e.type === 'match_result');
  assert.equal(result.you.placed, true, 'so a card can say where they landed');
  assert.equal(result.you.placing, false);
  assert.equal(result.you.rr_delta, 0, 'the seed is where they start, not something they won');
  assert.ok(result.you.rank_name, 'and the rank is in the same event');
  assert.equal(svc._internals.history.get(HOST)[0].placement, true);
});

test('match_result carries the per-player rows', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played' });
  const mine = listen(svc, HOST);
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '7',
    rows: `${HOST}|k=14;d=6,${FOE}|k=5;d=13` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const result = mine.find((e) => e.type === 'match_result');
  assert.ok(result, 'the result event should reach the player');
  assert.ok(Array.isArray(result.scoreboard), 'and carry a board');
  assert.equal(result.scoreboard.length, 2);
  const row = result.scoreboard.find((r) => r.steam_id === HOST);
  assert.equal(row.kills, 14);
  assert.equal(row.deaths, 6);
  assert.equal(row.team, 1, 'OUR team numbering, not the in-game one');
  assert.equal(row.reported, true);
});

test('both players get the SAME board, and it is not per-recipient', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played' });
  const mine = listen(svc, HOST);
  const theirs = listen(svc, FOE);
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '7',
    rows: `${HOST}|k=14;d=6,${FOE}|k=5;d=13` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const a = mine.find((e) => e.type === 'match_result').scoreboard;
  const b = theirs.find((e) => e.type === 'match_result').scoreboard;
  assert.deepEqual(a, b, 'is_me belongs to the client - ten boards would be ten near-identical copies');
  // And the RESULT itself still differs, which is the part that IS per-recipient.
  assert.equal(mine.find((e) => e.type === 'match_result').won, true);
  assert.equal(theirs.find((e) => e.type === 'match_result').won, false);
});

test('a match the gamemode said nothing about sends an empty board, not zeroes', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played' });
  const mine = listen(svc, HOST);
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const result = mine.find((e) => e.type === 'match_result');
  assert.deepEqual(result.scoreboard, [], 'the card falls back to its honest line');
});

test('the board names players, so a card can draw it with no roster of its own', () => {
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played' });
  svc._internals.rememberProfile(FOE, 'Kestrel', '');
  const mine = listen(svc, HOST);
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '7',
    rows: `${HOST}|k=14;d=6,${FOE}|k=5;d=13` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const board = mine.find((e) => e.type === 'match_result').scoreboard;
  // The ROSTER name wins, and should: personaOf checks the live match before the remembered
  // profile, so a player who changed their Steam name mid-match is still the person the other
  // nine watched. The point being tested is that the row is NAMED at all - a card built only
  // from this event has no roster of its own to join against.
  const row = board.find((r) => r.steam_id === FOE);
  assert.equal(row.persona, 'foe');
  assert.ok(row.persona, 'a row with no name would render as a 17-digit id on the card');
});

test('the board stays small enough to ride on one event', () => {
  // ~80 bytes a player is the budget this was designed against. A ten-player match must not
  // quietly become a multi-kilobyte push.
  const svc = service();
  const match = liveMatch(svc);
  svc._internals.archiveMatch(match, { outcome: 'played' });
  const mine = listen(svc, HOST);
  svc._internals.gameReportedStats(HOST, { match: 'match-under-test', rounds: '7',
    rows: `${HOST}|k=14;d=6,${FOE}|k=5;d=13` });
  svc.gameReportedScore(HOST, { match: 'match-under-test', scores: '0|0:7|1:3', limit: '7' });

  const board = mine.find((e) => e.type === 'match_result').scoreboard;
  const perPlayer = JSON.stringify(board).length / board.length;
  assert.ok(perPlayer < 160, `a row is ${Math.round(perPlayer)} bytes - the card does not need that much`);
});

/** Score every async test, in the order they were declared. */
async function runQueued() {
  for (const [name, promise] of queued) {
    try {
      await promise;
      passed += 1;
      console.log(`ok   - ${name}`);
    } catch (err) {
      failed += 1;
      console.log(`FAIL - ${name}`);
      console.log(`       ${err && err.message}`);
    }
  }
}

await runQueued();

console.log(`
${passed}/${passed + failed} tests passed`);
process.exit(failed ? 1 : 0);
