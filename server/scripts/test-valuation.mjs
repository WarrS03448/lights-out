/**
 * The valuation system and the visible ladder: Riot's four factors, our two numbers.
 *
 * Pure modules only - no ports, no sockets. `node server/scripts/test-valuation.mjs`.
 *
 * The invariants at the top of this file are the ones that must never break, because each of
 * them is a promise to a player: a win never costs rank, a loss never pays, and a match nobody
 * reported statistics for is worth exactly what it was worth before any of this existed.
 */
import assert from 'node:assert';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const V = require('../valuation.cjs');
const P = require('../progress.cjs');
const R = require('../rating.cjs');

let passed = 0;
let failed = 0;
function test(name, fn) {
  try { fn(); passed += 1; console.log(`ok   - ${name}`); }
  catch (err) { failed += 1; console.log(`FAIL - ${name}`); console.log(`       ${err && err.message}`); }
}

const side = (p) => Array.from({ length: 5 }, (_, i) => `${p}${i}`);
const A = side('a');
const B = side('b');
const flat = (extra = {}) => {
  const r = {};
  for (const id of [...A, ...B]) r[id] = { rating: 1500, rd: 60, matches: 30, progress: 1050 };
  return { ...r, ...extra };
};
const matchOf = (score = { 1: 7, 2: 3 }, extra = {}) =>
  ({ teams: { 1: A, 2: B }, left: [], score, score_limit: 7, mm: { quality: 1 }, ...extra });

const ROSTER = [
  { steamId: 'a0', kills: 24, deaths: 8, roundsPlayed: 10, roundsWon: 7, clutches: 2 },
  { steamId: 'a1', kills: 12, deaths: 10, roundsPlayed: 10, roundsWon: 7, clutches: 0 },
  { steamId: 'a2', kills: 10, deaths: 11, roundsPlayed: 10, roundsWon: 7, clutches: 0 },
  { steamId: 'a3', kills: 9, deaths: 12, roundsPlayed: 10, roundsWon: 7, clutches: 0 },
  { steamId: 'a4', kills: 3, deaths: 14, roundsPlayed: 10, roundsWon: 7, clutches: 0 },
  { steamId: 'b0', kills: 18, deaths: 10, roundsPlayed: 10, roundsWon: 3, clutches: 1 },
  { steamId: 'b1', kills: 11, deaths: 12, roundsPlayed: 10, roundsWon: 3, clutches: 0 },
  { steamId: 'b2', kills: 10, deaths: 13, roundsPlayed: 10, roundsWon: 3, clutches: 0 },
  { steamId: 'b3', kills: 8, deaths: 13, roundsPlayed: 10, roundsWon: 3, clutches: 0 },
  { steamId: 'b4', kills: 5, deaths: 14, roundsPlayed: 10, roundsWon: 3, clutches: 0 },
];
const STATS = { rounds: 10, limit: 7, players: ROSTER };

console.log('\n--- the invariants ---');

test('a winner is ALWAYS scored above a loser, whatever the statistics say', () => {
  // The band is what makes this true by construction: winners live in [1-BAND, 1] and losers
  // in [0, BAND]. Without it a player could out-perform their way past the result.
  const rows = V.valuation(matchOf(), STATS, flat(), 1);
  const worstWinner = Math.min(...rows.filter((r) => r.won).map((r) => r.score));
  const bestLoser = Math.max(...rows.filter((r) => !r.won).map((r) => r.score));
  assert.ok(worstWinner > bestLoser, `${worstWinner} must beat ${bestLoser}`);
});

test('winning never costs MMR and losing never gains it', () => {
  const rows = V.valuation(matchOf(), STATS, flat(), 1);
  for (const row of rows) {
    const before = { rating: 1500, rd: 60, matches: 30 };
    const after = R.update(before, { rating: 1500, rd: 60 }, row.score, row.weight);
    const delta = after.rating - before.rating;
    if (row.won) assert.ok(delta > 0, `${row.steamId} won and moved ${delta}`);
    else assert.ok(delta < 0, `${row.steamId} lost and moved ${delta}`);
  }
});

test('with nothing reported the result is the bare outcome - no silent haircut', () => {
  const rows = V.valuation(matchOf(), {}, flat(), 1);
  assert.equal(rows.every((r) => r.breakdown.measured === false), true);
  assert.equal(rows.find((r) => r.won).score, 1);
  assert.equal(rows.find((r) => !r.won).score, 0);
});

test('an unreported player is not a player who went 0-0', () => {
  // 0 kills and 0 deaths is a real and rather flattering line - perfect survival. Defaulting a
  // missing row to it made an empty report look like ten flawless players.
  const partial = { rounds: 10, limit: 7, players: ROSTER.slice(0, 3) };
  const rows = V.valuation(matchOf(), partial, flat(), 1);
  const missing = rows.find((r) => r.steamId === 'a4');
  assert.ok(Number.isNaN(missing.breakdown.kills), 'kills must be NaN, not 0');
  assert.equal(missing.breakdown.excess, 0, 'and they must not be judged');
});

console.log('\n--- performance is measured against EXPECTATION ---');

test('a strong player who plays to their rating is not rewarded for it', () => {
  const asPeer = V.valuation(matchOf(), STATS, flat(), 1).find((r) => r.steamId === 'a0');
  const asStar = V.valuation(matchOf(), STATS,
    flat({ a0: { rating: 2100, rd: 50, matches: 60, progress: 1600 } }), 1)
    .find((r) => r.steamId === 'a0');
  assert.ok(asPeer.breakdown.excess > 0.2, 'topping the board as a peer is over-performance');
  assert.ok(asStar.breakdown.excess < asPeer.breakdown.excess,
            'the same game from a 2100 player is expected, so it is worth less');
});

test('a player we have not measured is barely judged at all', () => {
  const sure = V.valuation(matchOf(), STATS, flat(), 1).find((r) => r.steamId === 'a4');
  const unsure = V.valuation(matchOf(), STATS,
    flat({ a4: { rating: 1500, rd: 350, matches: 0, progress: 0 } }), 1)
    .find((r) => r.steamId === 'a4');
  assert.ok(Math.abs(unsure.breakdown.expected - 0.5) <= Math.abs(sure.breakdown.expected - 0.5),
            'high RD must pull the expectation towards the middle');
});

test('refusing to take the site is not a way to protect your rating', () => {
  // Two players with identical fragging, one whose team wins rounds when they are in and one
  // whose does not. roundWinShare is weighted as heavily as kills precisely so the second is
  // not the better score.
  const rows = [
    { steamId: 'a0', kills: 15, deaths: 10, roundsPlayed: 10, roundsWon: 9 },
    { steamId: 'a1', kills: 15, deaths: 10, roundsPlayed: 10, roundsWon: 2 },
    { steamId: 'a2', kills: 10, deaths: 12, roundsPlayed: 10, roundsWon: 5 },
    { steamId: 'a3', kills: 10, deaths: 12, roundsPlayed: 10, roundsWon: 5 },
    { steamId: 'a4', kills: 10, deaths: 12, roundsPlayed: 10, roundsWon: 5 },
    ...ROSTER.slice(5),
  ];
  const out = V.valuation(matchOf(), { rounds: 10, limit: 7, players: rows }, flat(), 1);
  const helper = out.find((r) => r.steamId === 'a0');
  const hoarder = out.find((r) => r.steamId === 'a1');
  assert.ok(helper.score > hoarder.score,
            'winning rounds must beat the identical K/D that did not');
});

test('padding is bounded: percentile is by rank, not by magnitude', () => {
  const modest = ROSTER.map((r) => (r.steamId === 'a0' ? { ...r, kills: 25 } : r));
  const absurd = ROSTER.map((r) => (r.steamId === 'a0' ? { ...r, kills: 250 } : r));
  const a = V.valuation(matchOf(), { rounds: 10, limit: 7, players: modest }, flat(), 1)
    .find((r) => r.steamId === 'a0');
  const b = V.valuation(matchOf(), { rounds: 10, limit: 7, players: absurd }, flat(), 1)
    .find((r) => r.steamId === 'a0');
  assert.equal(a.score, b.score, 'ten times the kills must not be worth ten times the credit');
});

test('the pak payload alone is enough to separate players', () => {
  // EXACTLY the four fields bb5_graphs.rule_stats sends: kills, deaths, roundsPlayed, score.
  // roundsWon and clutches need per-round accumulation the gamemode cannot do yet, so this is
  // the real shape of a live match today and it has to work on its own.
  const players = [
    ['a0', 24, 8, 10, 4800], ['a1', 12, 10, 10, 2600], ['a2', 10, 11, 10, 2200],
    ['a3', 9, 12, 10, 2000], ['a4', 3, 14, 10, 900],
    ['b0', 18, 10, 10, 3800], ['b1', 11, 12, 10, 2400], ['b2', 10, 13, 10, 2100],
    ['b3', 8, 13, 10, 1800], ['b4', 5, 14, 10, 1200],
  ].map(([steamId, kills, deaths, roundsPlayed, score]) =>
    ({ steamId, kills, deaths, roundsPlayed, score }));
  const rows = V.valuation(matchOf(), { rounds: 10, limit: 7, players }, flat(), 1);
  assert.ok(rows.every((r) => r.breakdown.measured), 'all ten must be measured');
  const carry = rows.find((r) => r.steamId === 'a0');
  const passenger = rows.find((r) => r.steamId === 'a4');
  assert.ok(carry.score > passenger.score, 'the carry must outscore the passenger');
  assert.ok(carry.breakdown.excess > 0 && passenger.breakdown.excess < 0);
  const worstWinner = Math.min(...rows.filter((r) => r.won).map((r) => r.score));
  const bestLoser = Math.max(...rows.filter((r) => !r.won).map((r) => r.score));
  assert.ok(worstWinner > bestLoser, 'and the invariant still holds on this payload');
});

test('the game score counts, and counts per round', () => {
  const base = ROSTER.map((r) => ({ ...r, score: 1000 }));
  const lifted = base.map((r) => (r.steamId === 'a3' ? { ...r, score: 9000 } : r));
  const before = V.valuation(matchOf(), { rounds: 10, limit: 7, players: base }, flat(), 1)
    .find((r) => r.steamId === 'a3');
  const after = V.valuation(matchOf(), { rounds: 10, limit: 7, players: lifted }, flat(), 1)
    .find((r) => r.steamId === 'a3');
  assert.ok(after.score > before.score, 'a much better game score must be worth something');
  // ...and halving the rounds they played doubles their score PER ROUND, so it must not be
  // possible to gain by simply being present longer.
  const short = lifted.map((r) => (r.steamId === 'a3' ? { ...r, roundsPlayed: 5 } : r));
  const perRound = V.valuation(matchOf(), { rounds: 10, limit: 7, players: short }, flat(), 1)
    .find((r) => r.steamId === 'a3');
  assert.ok(perRound.breakdown.spr > after.breakdown.spr, 'score is measured per round');
});

test('a component nobody can be separated on stops taking weight', () => {
  // Every player with an identical value ranks everybody at 0.5, which separates nobody while
  // still consuming its share. Treated as absent instead, so the components that DO
  // discriminate are not quietly diluted. A team-wide figure inside one team is exactly this.
  const varied = ROSTER.map((r) => ({ ...r, score: r.kills * 200 }));
  const flatScore = ROSTER.map((r) => ({ ...r, score: 2000 }));
  const withVaried = V.valuation(matchOf(), { rounds: 10, limit: 7, players: varied }, flat(), 1);
  const withFlat = V.valuation(matchOf(), { rounds: 10, limit: 7, players: flatScore }, flat(), 1);
  const noScore = V.valuation(matchOf(), { rounds: 10, limit: 7, players: ROSTER }, flat(), 1);
  const pick = (rows) => rows.find((r) => r.steamId === 'a0').score;
  assert.equal(pick(withFlat), pick(noScore),
               'a flat component must behave exactly as if it were never reported');
  assert.ok(pick(withVaried) !== pick(noScore), 'but a varying one must actually count');
});

console.log('\n--- the weight: how much a match is worth as evidence ---');

test('a blowout is stronger evidence than a coin toss', () => {
  const close = V.valuation(matchOf({ 1: 7, 2: 6 }), STATS, flat(), 1)[0];
  const rout = V.valuation(matchOf({ 1: 7, 2: 0 }), STATS, flat(), 1)[0];
  assert.ok(rout.weight > close.weight, `${rout.weight} should beat ${close.weight}`);
});

test('a match somebody walked out of counts for less', () => {
  const clean = V.valuation(matchOf(), STATS, flat(), 1)[0];
  const abandoned = V.valuation(matchOf({ 1: 7, 2: 3 }, { left: [{ steam_id: 'b4' }] }), STATS, flat(), 1)[0];
  assert.ok(abandoned.weight < clean.weight);
});

test('someone who played four rounds of twelve moves less, both ways', () => {
  const sub = ROSTER.map((r) => (r.steamId === 'a4' ? { ...r, roundsPlayed: 4, roundsWon: 3 } : r));
  const rows = V.valuation(matchOf(), { rounds: 10, limit: 7, players: sub }, flat(), 1);
  const part = rows.find((r) => r.steamId === 'a4');
  const full = rows.find((r) => r.steamId === 'a3');
  assert.ok(part.weight < full.weight, 'presence must scale the weight');
  assert.ok(part.weight >= V.WEIGHT_FLOOR, 'but never to nothing');
});

test('the weight never reaches zero, however bad the match was', () => {
  const awful = matchOf({ 1: 7, 2: 6 }, { left: [{ a: 1 }, { b: 2 }, { c: 3 }], mm: { quality: 0 } });
  for (const row of V.valuation(awful, STATS, flat(), 1)) {
    assert.ok(row.weight >= V.WEIGHT_FLOOR && row.weight <= 1, `weight ${row.weight}`);
  }
});

console.log('\n--- rrWeight: how much of the VISIBLE payout a player gets ---');

test('RR is not docked for the pairing: a quality-0 match pays full RR (Sam, 2026-09-16)', () => {
  // The test-night shape: a 1v1 whose MMRs had drifted apart, so the matchmaker scored the pairing
  // 0 and the evidence weight sat on its floor - and RR was multiplied by it, so a win paid +8.
  const complete = { ...STATS, rounds: 13, players: ROSTER.map(row => ({ ...row, roundsPlayed: 13 })) };
  const rows = V.valuation(matchOf({ 1: 7, 2: 6 }, { mm: { quality: 0 } }), complete, flat(), 1);
  for (const row of rows) {
    assert.equal(row.weight, V.WEIGHT_FLOOR, 'the matchmaking rating still learns less from it');
    assert.equal(row.rrWeight, 1, `but the visible payout is whole (${row.rrWeight})`);
  }
});

test('RR is not docked for the scoreline either - round differential already pays for that', () => {
  const complete = rounds => ({ ...STATS, rounds, players: ROSTER.map(row => ({ ...row, roundsPlayed: rounds })) });
  const close = V.valuation(matchOf({ 1: 7, 2: 6 }), complete(13), flat(), 1)[0];
  const rout = V.valuation(matchOf({ 1: 7, 2: 0 }), complete(7), flat(), 1)[0];
  assert.ok(rout.weight > close.weight, 'decisiveness is evidence for MMR');
  assert.equal(close.rrWeight, rout.rrWeight, 'and counted once for RR, in award()');
});

test('RR still pays less for a match somebody walked out of, and for rounds not played (C9)', () => {
  const clean = V.valuation(matchOf(), STATS, flat(), 1)[0];
  const abandoned = V.valuation(matchOf({ 1: 7, 2: 3 }, { left: [{ steam_id: 'b4' }] }), STATS, flat(), 1)[0];
  assert.ok(abandoned.rrWeight < clean.rrWeight, 'integrity scales the visible payout');

  const sub = ROSTER.map((r) => (r.steamId === 'a4' ? { ...r, roundsPlayed: 4, roundsWon: 3 } : r));
  const rows = V.valuation(matchOf(), { rounds: 10, limit: 7, players: sub }, flat(), 1);
  const part = rows.find((r) => r.steamId === 'a4');
  const full = rows.find((r) => r.steamId === 'a3');
  assert.ok(part.rrWeight < full.rrWeight, 'presence scales it');
  assert.ok(part.rrWeight > 0 && part.rrWeight <= 1);
});

console.log('\n--- the visible ladder (Valorant RR) ---');

test('three divisions per rank, 100 RR each', () => {
  // Sam, 2026-09-15: "ranks have 3 subranks that you incrementally move through every 100 BDR".
  // The names are docs/ranks.md's, and the SAME list rating.cjs keeps - there is one ladder.
  assert.equal(P.DIVISIONS, 3);
  assert.equal(P.RR_PER_DIVISION, 100);
  const at = (rr) => `${P.rankName(rr)} ${'I'.repeat(P.divisionOf(rr) || 0)}`.trim();
  assert.equal(at(0), 'Rookie I');
  assert.equal(at(100), 'Rookie II');
  assert.equal(at(299), 'Rookie III');
  assert.equal(at(300), 'Private I', 'a new rank begins every three divisions');
  assert.equal(at(900), 'Veteran I');
  assert.equal(at(1799), 'Shadow III');
  assert.equal(at(2399), 'Spectre III', 'and the last named rank has divisions like the rest');
  assert.equal(P.withinLevel(250), 50, 'RR counts within the division');
});

test('every named rank has divisions, and the capstone sits above all of them', () => {
  // The ladder used to spend its last two names on an apex rank and a top-500 rank that nothing
  // could ever award. Now all eight names are playable ranks of three divisions, and the one
  // rank without divisions is the capstone above them.
  assert.equal(P.RANK_NAMES.length, 8);
  assert.equal(P.TIERED_RANKS, P.RANK_NAMES.length, 'no name is reserved any more');
  assert.equal(P.APEX_RANK, P.RANK_NAMES.length + 1);
  assert.equal(P.APEX_AT, 8 * 3 * 100);
  for (const name of P.RANK_NAMES) {
    const rr = (P.RANK_NAMES.indexOf(name) * P.DIVISIONS) * P.RR_PER_DIVISION;
    assert.equal(P.rankName(rr), name);
    assert.equal(P.divisionOf(rr), 1, `${name} must start at its own division 1`);
  }
});

test('the last rank counts RR instead of resetting it, Valorant Immortal', () => {
  // Sam, 2026-09-16: "spectre 1 ... until 99 RR. then at 100 RR they become spectre 2 ... Then
  // once a user hits 200 RR+ they become Spectre 3."
  const band = P.COUNT_AT;
  assert.equal(band, 7 * 3 * 100, 'the counting band is the floor of the LAST named rank');
  assert.equal(P.COUNT_RANK, P.TIERED_RANKS);
  const div = (rr) => P.divisionOf(band + rr);
  assert.equal(div(0), 1);
  assert.equal(div(99), 1);
  assert.equal(div(100), 2, '100 RR is division 2');
  assert.equal(div(199), 2);
  assert.equal(div(200), 3, '200 RR is division 3');
  assert.equal(div(4000), 3, '...and there is no division 4, however high it goes');
  // THE FIGURE STOPS RESETTING. Below the band it is 0-99 inside a division; inside it, it is
  // one running count - which is the only reason the board can order the top of the ladder.
  assert.equal(P.withinLevel(band - 1), 99, 'the rank below still counts within its division');
  assert.equal(P.withinLevel(band + 250), 250, 'and the band counts straight through');
  assert.equal(P.withinLevel(band + 4000), 4000, 'uncapped');
  assert.equal(P.bdrOf(band + 250), 250, 'BDR is that same running count, under its old name');
  assert.equal(P.bdrOf(band - 1), null, 'and it does not exist below the band');
});

test('the capstone is a SEAT on the leaderboard, not a band of the ladder', () => {
  // Sam, 2026-09-16: "once a user hits 300 RR, they become eligible to become reaper if their
  // RR is in the top 150 highest RR values on the leaderboard." So RR alone can never award it.
  assert.equal(P.REAPER_RR, 300);
  assert.equal(P.REAPER_SLOTS, 150);
  assert.equal(P.REAPER_AT, P.COUNT_AT + P.REAPER_RR);
  assert.equal(P.rankOf(P.REAPER_AT), P.TIERED_RANKS, 'no amount of RR is the capstone');
  assert.equal(P.rankOf(P.REAPER_AT + 99999), P.TIERED_RANKS, 'not even a lot of it');
  assert.equal(P.rankName(P.REAPER_AT + 99999), 'Spectre');
  assert.ok(!P.reaperEligible(P.REAPER_AT - 1));
  assert.ok(P.reaperEligible(P.REAPER_AT));

  // The only place it appears, and only because the caller - live.cjs, which owns the board -
  // said so. Eligibility is re-checked here so a wrong cut cannot seat an unqualified player.
  const rec = { rating: 2300, rd: 60, matches: 99, progress: P.REAPER_AT + 40 };
  const seated = P.publicProgress(rec, { top: true });
  assert.equal(seated.rank, P.APEX_RANK);
  assert.equal(seated.rank_name, P.RANK_TOP);
  assert.equal(seated.division, null, 'the capstone is the one rank with no division');
  assert.equal(seated.rr, 340, 'and it keeps counting in the same figure the band does');
  assert.equal(seated.top, true);

  const unseated = P.publicProgress(rec, { top: false });
  assert.equal(unseated.rank, P.TIERED_RANKS, 'the same record, outside the top 150');
  assert.equal(unseated.division, 3);
  assert.equal(unseated.top_eligible, true, 'but told it is eligible, or the UI cannot explain');

  const short = P.publicProgress({ ...rec, progress: P.REAPER_AT - 1 }, { top: true });
  assert.equal(short.top, false, 'a cut that names somebody short of the RR seats nobody');
});

test('MMR cannot promise the capstone, and placements cannot seed into it', () => {
  // The rank is 150 seats, not a rating band: an MMR that could say "Reaper" would leave every
  // seated player with a permanent convergence pull towards a rank the ladder cannot award.
  assert.equal(P.levelForRating({ rating: 5000, rd: 60, matches: 99 }), P.TIERED_RANKS,
               'the best MMR in the world says the top NAMED rank');
  assert.equal(P.seedFor({ rating: 5000, rd: 60, matches: 5, wins: 5, losses: 0,
                           placementMeasured: 5, placementImpact: 4 }),
               (P.PLACEMENT_MAX_RANK - 1) * P.DIVISIONS * P.RR_PER_DIVISION + 100 + 50,
               'and a perfect placement run lands in the middle division of the placement cap');
  assert.ok(P.seedFor({ rating: 5000, rd: 60, matches: 99 }) < P.REAPER_AT,
            'short of eligibility - the top of the ladder has to be climbed');
});

test('Operator placement requires all wins and strong measured performance across the run', () => {
  const strong = { rating: 2200, rd: 150, matches: 5, wins: 5, losses: 0,
                   placementMeasured: 5, placementImpact: 3.25 };
  const rank = (record) => P.rankOf(P.seedFor(record));
  assert.equal(rank(strong), 5, '5-0 at 65% impact can earn Operator');
  assert.equal(rank({ ...strong, wins: 4, losses: 1 }), 4, '4-1 cannot earn Operator even at high MMR');
  assert.equal(rank({ ...strong, placementImpact: 3.249 }), 4, 'performance below the threshold stays Veteran');
  assert.equal(rank({ ...strong, placementImpact: 2.5 }), 4, 'being carried to 5-0 is insufficient');
  assert.equal(rank({ ...strong, placementMeasured: 4 }), 4, 'missing performance evidence cannot unlock Operator');
  assert.equal(rank({ rating: 2200, matches: 5, wins: 5 }), 4, 'legacy records without evidence are conservative');
  assert.equal(rank({ ...strong, rating: 1500 }), 3, 'eligibility never awards a rank MMR did not earn');
});

test('placement performance survives rating persistence and updates, without counting later matches', () => {
  let record = R.defaultRating();
  for (let i = 0; i < 5; i++) {
    const next = R.update(record, { rating: 1800, rd: 60 }, 0.95, 1);
    P.recordPlacement(record, next, { measured: true, actual: 0.7 });
    record = R.normalise(JSON.parse(JSON.stringify(next)));
  }
  assert.equal(record.placementMeasured, 5);
  assert.ok(Math.abs(record.placementImpact - 3.5) < 1e-9);
  const next = R.update(record, { rating: 1800, rd: 60 }, 1, 1);
  P.recordPlacement(record, next, { measured: true, actual: 1 });
  assert.equal(next.placementMeasured, 5);
  assert.equal(next.placementImpact, record.placementImpact);
  const missing = R.update(R.defaultRating(), { rating: 1800, rd: 60 }, 1, 1);
  P.recordPlacement(R.defaultRating(), missing, { measured: false, actual: 0.5 });
  assert.equal(missing.placementMeasured, 0);
});

test('a new player starts below what MMR says, and never above the cap (Sam, 2026-09-16)', () => {
  // "when i placed, i placed operator 2 ... i dont think players who are brand new to the game
  // should ever start there", then "starting in operator is fine but never shadow".
  for (let mmr = 700; mmr <= 3000; mmr += 10) {
    const record = { rating: mmr, rd: 150, matches: R.PLACEMENT_MATCHES };
    const seeded = P.rankOf(P.seedFor(record));
    assert.ok(seeded <= P.PLACEMENT_MAX_RANK, `MMR ${mmr} seeded rank ${seeded}, over the cap`);
    assert.ok(seeded <= P.levelForRating(record), `MMR ${mmr} seeded above what it earned`);
    assert.equal(P.divisionOf(P.seedFor(record)), 2, 'still the middle division, halfway through');
  }
  // A 3-2 placement lands a little over the start rating - about +60 - and that is not "someone
  // whos considered very good". Sam's own went there, and it opened at the cap: Operator 2.
  const threeTwo = { rating: R.START_RATING + 60, rd: 150, matches: R.PLACEMENT_MATCHES };
  assert.ok(P.rankOf(P.seedFor(threeTwo)) < P.PLACEMENT_MAX_RANK,
            `a 3-2 placement opened at rank ${P.rankOf(P.seedFor(threeTwo))}`);
});

test('a player seeded below their MMR climbs faster until the badge catches up', () => {
  // The start is lower; the ceiling is not. Convergence is measured against MMR, not the seed.
  const record = { rating: 1650, rd: 150, matches: R.PLACEMENT_MATCHES };
  const seeded = P.seedFor(record);
  if (P.PLACEMENT_RANK_OFFSET > 0) {
    assert.ok(P.rankOf(seeded) < P.levelForRating(record), 'seeded below what MMR says');
    assert.ok(P.convergence(record, seeded) > 1, 'so wins pay more and losses cost less');
  }
});

test('FACTOR 1: winning pays and losing costs', () => {
  const me = { rating: 1500, rd: 60, matches: 30, progress: 450 };
  const win = P.award(me, me, { won: true, roundDiff: 0.5, excess: 0, weight: 1 });
  const loss = P.award(me, me, { won: false, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.ok(win.delta > 0 && loss.delta < 0);
});

test('FACTOR 2: round differential moves the payout', () => {
  const me = { rating: 1500, rd: 60, matches: 30, progress: 450 };
  const rout = P.award(me, me, { won: true, roundDiff: 1, excess: 0, weight: 1 });
  const close = P.award(me, me, { won: true, roundDiff: 0, excess: 0, weight: 1 });
  assert.ok(rout.delta > close.delta, 'a 7-0 must pay more than a 7-6');
  const narrowLoss = P.award(me, me, { won: false, roundDiff: 0, excess: 0, weight: 1 });
  const heavyLoss = P.award(me, me, { won: false, roundDiff: 1, excess: 0, weight: 1 });
  assert.ok(narrowLoss.delta > heavyLoss.delta, 'a close loss must cost less than a whitewash');
});

test('FACTOR 3: the performance bonus fades out towards the top, as Riot describes', () => {
  const low = { rating: 1100, rd: 60, matches: 30, progress: 150 };            // Rookie II
  const high = { rating: 2300, rd: 60, matches: 30, progress: P.APEX_AT + 50 }; // counting band
  const lowGain = P.award(low, low, { won: true, roundDiff: 0.5, excess: 1, weight: 1 });
  const lowPlain = P.award(low, low, { won: true, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.ok(lowGain.delta > lowPlain.delta, 'the bonus must exist at the bottom of the ladder');
  const hiGain = P.award(high, high, { won: true, roundDiff: 0.5, excess: 1, weight: 1 });
  const hiPlain = P.award(high, high, { won: true, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.equal(hiGain.factors.perf, 0, 'and must be gone at the top');
  assert.equal(hiGain.delta, hiPlain.delta, 'wins and round difference only, up there');
});

test('FACTOR 4: convergence pays more and costs less when MMR is above the rank', () => {
  // A 2300-rated player (MMR says Spectre) still sitting down in Rookie III.
  const under = { rating: 2300, rd: 60, matches: 30, progress: 250 };
  // ...against one whose continuous MMR target is exactly Veteran II, 50 RR.
  // Default geometry: (1435 - 700) / 70 * 100 = 1050.
  const even = { rating: 1435, rd: 60, matches: 30, progress: 1050 };
  const underWin = P.award(under, under, { won: true, roundDiff: 0.5, excess: 0, weight: 1 });
  const evenWin = P.award(even, even, { won: true, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.ok(underWin.delta > evenWin.delta, 'an under-ranked player must climb faster');
  const underLoss = P.award(under, under, { won: false, roundDiff: 0.5, excess: 0, weight: 1 });
  const evenLoss = P.award(even, even, { won: false, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.ok(underLoss.delta > evenLoss.delta, 'and must lose less on the way');
  assert.ok(underWin.factors.convergence > 1 && evenWin.factors.convergence === 1);
});

test('and the mirror: an over-ranked player gains less and loses more', () => {
  // MMR says Private; they are sitting in Shadow II. Deliberately 50 RR INTO the division
  // rather than at 0, or demotion protection pins the loss and the delta under test is 0 -
  // which is the guard doing its job, not the convergence this test is about.
  const over = { rating: 1000, rd: 60, matches: 30, progress: 1650 };
  const win = P.award(over, over, { won: true, roundDiff: 0.5, excess: 0, weight: 1 });
  const loss = P.award(over, over, { won: false, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.ok(win.factors.convergence < 1, 'convergence must pull downwards');
  assert.ok(win.delta < 20, `an over-ranked win should pay under the base, paid ${win.delta}`);
  assert.ok(loss.delta < -20, `and an over-ranked loss should cost over it, cost ${loss.delta}`);
});

test("RIOT'S HARD RULE: a loss can never pay RR", () => {
  const me = { rating: 1100, rd: 60, matches: 30, progress: 150 };
  for (const roundDiff of [0, 0.25, 0.5, 1]) {
    for (const excess of [0, 0.5, 1]) {
      const out = P.award(me, me, { won: false, roundDiff, excess, weight: 1 });
      assert.ok(out.delta < 0, `loss paid ${out.delta} at rd=${roundDiff} excess=${excess}`);
    }
  }
});

test('...and a win always pays at least something', () => {
  const over = { rating: 900, rd: 60, matches: 30, progress: 1750 };
  const out = P.award(over, over, { won: true, roundDiff: 0, excess: -1, weight: 0.35 });
  assert.ok(out.delta >= 1, `win paid ${out.delta}`);
});

test('demotion needs a second loss at the bottom of a DIVISION', () => {
  // Veteran II with 5 RR. One loss should pin at 0 RR of Veteran II, not drop to Veteran I.
  const edge = { rating: 1500, rd: 60, matches: 30, progress: 1005 };
  const first = P.award(edge, edge, { won: false, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.equal(P.divisionOf(first.progress), 2, 'the first loss pins you, it does not demote');
  assert.equal(P.withinLevel(first.progress), 0, 'at 0 RR in the division you were in');
  assert.equal(first.demoteArmed, true);
  const armed = { ...edge, progress: first.progress };
  const second = P.award(armed, armed, { won: false, roundDiff: 0.5, excess: 0, weight: 1, demoteArmed: true });
  assert.equal(P.divisionOf(second.progress), 1, 'the second one drops a division');
  assert.equal(P.rankOf(second.progress), 4, 'still the same rank, though');
});

test('...and the same guard holds at a RANK boundary', () => {
  const edge = { rating: 1500, rd: 60, matches: 30, progress: 905 };  // Veteran I, 5 RR
  const first = P.award(edge, edge, { won: false, roundDiff: 0.5, excess: 0, weight: 1 });
  assert.equal(P.rankName(first.progress), 'Veteran', 'not demoted out of the rank yet');
  const armed = { ...edge, progress: first.progress };
  const second = P.award(armed, armed, { won: false, roundDiff: 0.5, excess: 0, weight: 1, demoteArmed: true });
  assert.equal(P.rankName(second.progress), 'Soldier', 'now it drops a rank');
  assert.equal(P.divisionOf(second.progress), 3, 'into the top division of the rank below');
});

test('nobody falls off the bottom of the ladder', () => {
  let me = { rating: 500, rd: 60, matches: 30, progress: 5 };
  for (let i = 0; i < 30; i += 1) {
    const out = P.award(me, me, { won: false, roundDiff: 1, excess: -1, weight: 1, demoteArmed: me.demoteArmed });
    me = { ...me, progress: out.progress, demoteArmed: out.demoteArmed };
  }
  assert.equal(me.progress, 0);
  assert.equal(P.levelOf(me.progress), 1);
});

test('placements pay no RR, then seed the rank from MMR', () => {
  const placing = { rating: 1500, rd: 200, matches: 2, progress: 0 };
  const stillPlacing = P.award(placing, { ...placing, matches: 3 }, { won: true, roundDiff: 1, excess: 1, weight: 1 });
  assert.equal(stillPlacing.delta, 0);
  assert.equal(stillPlacing.level, null, 'no visible rank during placements');

  const placed = { ...placing, matches: R.PLACEMENT_MATCHES, rating: 1750 };
  const out = P.award(placing, placed, { won: true, roundDiff: 1, excess: 1, weight: 1 });
  assert.equal(out.placed, true);
  assert.equal(out.progress, P.seedFor(placed), 'the rank is seeded from what MMR earned');
  assert.ok(out.level <= R.levelOf(placed), 'and never above it');
});

test('publicProgress withholds everything while placing', () => {
  const p = P.publicProgress({ rating: 1500, rd: 200, matches: 2, progress: 0 });
  assert.equal(p.level, null);
  assert.equal(p.rr, null);
  assert.equal(p.bdr, null);
  assert.equal(p.placing, true);
});

console.log('\n--- the two ladders together ---');

test('a settled player climbs the visible ladder towards their MMR over a run of wins', () => {
  let me = { rating: 1500, rd: 60, matches: 30, wins: 15, losses: 15, progress: 1050, vol: 0.06 };
  const opp = { rating: 1900, rd: 60 };
  const startLevel = P.levelOf(me.progress);
  for (let i = 0; i < 12; i += 1) {
    const after = R.update(me, opp, 1, 1);
    const rr = P.award(me, after, { won: true, roundDiff: 0.6, excess: 0.2, weight: 1, demoteArmed: me.demoteArmed });
    me = { ...after, progress: rr.progress, demoteArmed: rr.demoteArmed };
  }
  assert.ok(me.rating > 1500, 'MMR rose');
  assert.ok(P.levelOf(me.progress) > startLevel, 'and the visible rank followed');
});

test('MMR and RR never disagree about whether the match was won', () => {
  const rows = V.valuation(matchOf(), STATS, flat(), 1);
  for (const row of rows) {
    const was = { rating: 1500, rd: 60, matches: 30, progress: 1050 };
    const after = R.update(was, { rating: 1500, rd: 60 }, row.score, row.weight);
    const rr = P.award(was, after, {
      won: row.won, roundDiff: 0.57, excess: row.breakdown.excess, weight: row.weight,
    });
    const mmrUp = after.rating > was.rating;
    const rrUp = rr.delta > 0;
    assert.equal(mmrUp, rrUp, `${row.steamId}: MMR ${mmrUp ? 'up' : 'down'}, RR ${rrUp ? 'up' : 'down'}`);
  }
});

console.log('');
console.log('--- round win share and clutches ---');

test('round win share is NOT just the team win rate', () => {
  // The trap this metric has to avoid. Everyone on a side plays every round in BB5, so "rounds my
  // team won" is identical for all five of them and discriminates nobody - it would be spr again,
  // the outcome counted twice under a different name. What separates them is whether they were
  // still standing when the round was won.
  const A = 'a', B = 'b';
  const match = {
    teams: { 1: [A, B], 2: ['x'] }, players: [{ steam_id: A }, { steam_id: B }, { steam_id: 'x' }],
    team_map: { 0: 1, 1: 2 },
    rounds: { 1: { round: 1, winTeam: 0 }, 2: { round: 2, winTeam: 0 } },
  };
  const stats = { rounds: 2, series: {
    // A survived both rounds; B died in both. Same team, same two wins.
    [A]: { 1: { deaths: 0 }, 2: { deaths: 0 } },
    [B]: { 1: { deaths: 1 }, 2: { deaths: 2 } },
  } };
  const ctx = V.roundContext(match, stats, (id) => (id === 'x' ? 2 : 1));
  assert.equal(ctx[A].roundsWon, 2, 'alive at the end of both wins');
  assert.equal(ctx[B].roundsWon, 0, 'died in both, so neither is credited');
});

test('a round nobody won yet is not counted against anyone', () => {
  const match = {
    teams: { 1: ['a'], 2: ['x'] }, players: [{ steam_id: 'a' }], team_map: { 0: 1, 1: 2 },
    rounds: { 1: { round: 1, winTeam: 0 }, 2: { round: 2 } },   // round 2 has no winner
  };
  const ctx = V.roundContext(match, { series: { a: { 1: { deaths: 0 } } } }, () => 1);
  assert.equal(ctx.a.roundsCounted, 1, 'only the decided round counts');
  assert.equal(ctx.a.roundsWon, 1);
});

test('without the team mapping nothing is claimed', () => {
  // team_map turns the game's team ids into ours. With no mapping a round winner says nothing
  // about our sides, and a guess would be worse than a gap.
  const match = {
    teams: { 1: ['a'], 2: ['x'] }, players: [{ steam_id: 'a' }],
    rounds: { 1: { round: 1, winTeam: 0 } },
  };
  const ctx = V.roundContext(match, { series: { a: { 1: { deaths: 0 } } } }, () => 1);
  assert.equal(ctx.a && ctx.a.roundsWon, undefined);
});

test('a clutch is a kill taken while last alive on your side', () => {
  const match = {
    teams: { 1: ['a'], 2: ['x'] }, players: [{ steam_id: 'a' }], team_map: { 0: 1, 1: 2 },
    rounds: { 1: { round: 1, winTeam: 0 } },
    kills: [
      { killer: 'a', killerTeam: 1, victimTeam: 2, round: 1, alive0: 3, alive1: 2 },  // not alone
      { killer: 'a', killerTeam: 1, victimTeam: 2, round: 1, alive0: 1, alive1: 2 },  // 1v2
      { killer: 'a', killerTeam: 1, victimTeam: 2, round: 1, alive0: 1, alive1: 1 },  // 1v1
    ],
  };
  const ctx = V.roundContext(match, { series: { a: { 1: { deaths: 0 } } } }, () => 1);
  assert.equal(ctx.a.clutches, 2, 'the two taken while alone, not the first');
});

test('a team kill is never a clutch', () => {
  const match = {
    teams: { 1: ['a'], 2: ['x'] }, players: [{ steam_id: 'a' }], team_map: { 0: 1, 1: 2 },
    rounds: { 1: { round: 1, winTeam: 0 } },
    kills: [{ killer: 'a', killerTeam: 1, victimTeam: 1, teamKill: true, round: 1, alive0: 1, alive1: 3 }],
  };
  const ctx = V.roundContext(match, { series: { a: { 1: { deaths: 0 } } } }, () => 1);
  assert.equal(ctx.a.clutches, undefined);
});

test('the two components now MOVE the score', () => {
  // The point of the exercise: 0.30 and 0.08 of the performance weight were unreachable.
  const surv = V.playerMetrics(
    { steamId: 'a', enemyKills: 5, deaths: 0, roundsWon: 4, clutches: 2 }, { rounds: 4 });
  const died = V.playerMetrics(
    { steamId: 'b', enemyKills: 5, deaths: 4, roundsWon: 0, clutches: 0 }, { rounds: 4 });
  assert.equal(surv.roundWinShare, 1);
  assert.equal(died.roundWinShare, 0);
  assert.equal(surv.clutch, 0.5);
  assert.ok(Number.isFinite(died.clutch));
});

console.log(`\n${passed}/${passed + failed} tests passed`);
process.exit(failed ? 1 : 0);
