/**
 * The matchmaking and rating tests.
 *
 * SEPARATE FROM test.mjs on purpose. That suite stands up real HTTP servers and real SSE
 * streams and takes minutes, because that is what testing a live service costs. This one
 * tests the two PURE modules - rating.cjs and matchmaker.cjs - and so runs in milliseconds
 * with no ports, no sockets and no sleeping.
 *
 * That split is the whole reason those modules are pure. The arithmetic that decides every
 * player's rank should not be hard to test just because the service it runs inside is.
 *
 *   node server/scripts/test-matchmaking.mjs
 */
import assert from 'node:assert';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
// Legacy arithmetic tests isolate rating policy; test-network.mjs exercises enforced networks.
process.env.NODE_ENV = 'test';
process.env.COMP_NETWORK_TEST_BYPASS = '1';
const rating = require('../rating.cjs');
const mm = require('../matchmaker.cjs');

let passed = 0;
let failed = 0;

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`ok   - ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`FAIL - ${name}`);
    console.log(`       ${err && err.message}`);
  }
}

// Helpers. `ago` is how many seconds this unit has been queueing.
const player = (r, rd = 60, matches = 20) => ({ rating: r, rd, matches });
const solo = (key, r, { rd = 60, ago = 5, matches = 20 } = {}) =>
  ({ key, joined: Date.now() - ago * 1000, ratings: [player(r, rd, matches)] });
const party = (key, rs, { ago = 5, rd = 60 } = {}) =>
  ({ key, joined: Date.now() - ago * 1000, ratings: rs.map((r) => player(r, rd)) });
const mean = (xs) => xs.reduce((a, b) => a + b, 0) / xs.length;
const teamValues = (match, n) => match.teams[n].flatMap((u) => u.values);

// ==================================================================== rating.cjs
console.log('\n--- ratings ---');

test('an unknown player is 1500 with everything to learn', () => {
  const r = rating.defaultRating();
  assert.equal(r.rating, rating.START_RATING);
  assert.equal(r.rd, rating.START_RD);
  assert.equal(r.matches, 0);
});

test('a missing or null RD falls back to UNKNOWN, never to the floor', () => {
  // The bug this exists for: Number(null) is 0, which clamps to MIN_RD - so a half-written
  // record would read as a player we had measured forty times, and the matchmaker would
  // hand them a tight match on a rating nobody ever verified.
  assert.equal(rating.normalise({ rating: 1600 }).rd, rating.START_RD);
  assert.equal(rating.normalise({ rating: 1600, rd: null }).rd, rating.START_RD);
  assert.equal(rating.normalise({ rating: 1600, rd: '' }).rd, rating.START_RD);
  assert.equal(rating.normalise({ rating: 1600, rd: 70 }).rd, 70);
});

test('garbage in is a default record, not a crash', () => {
  for (const junk of [null, undefined, 'nope', 42, [], { rating: {} }]) {
    const r = rating.normalise(junk);
    assert.ok(Number.isFinite(r.rating) && Number.isFinite(r.rd));
  }
});

test('placements place: five matches move a new player further than fifty move a settled one', () => {
  let fresh = rating.defaultRating();
  const start = fresh.rating;
  for (let i = 0; i < 5; i += 1) fresh = rating.update(fresh, { rating: 1500, rd: 100 }, 1);
  const placementMove = fresh.rating - start;

  let settled = fresh;
  const settledStart = settled.rating;
  for (let i = 0; i < 5; i += 1) settled = rating.update(settled, { rating: 1500, rd: 100 }, 1);
  const settledMove = settled.rating - settledStart;

  assert.ok(placementMove > 300, `five placement wins should move a lot, moved ${placementMove}`);
  assert.ok(placementMove > settledMove * 3,
            `placements (${placementMove}) must dwarf the same five later (${settledMove})`);
});

test('confidence settles at an equilibrium, and the ladder never freezes', () => {
  let r = rating.defaultRating();
  let last = r.rd;
  for (let i = 0; i < 60; i += 1) {
    r = rating.update(r, { rating: 1500, rd: 60 }, i % 2);
    assert.ok(r.rd <= last + 1, 'RD must not wander back up inside a run of matches');
    last = r.rd;
  }
  // NOT the floor. Glicko-2's step 6 re-inflates RD by the volatility every match, so it
  // converges to a balance point rather than decaying to zero - which is the property that
  // actually keeps a settled player moving. Measured at ~68 with our constants; asserted as a
  // band so tuning TAU does not fail the suite for no reason.
  assert.ok(r.rd >= rating.MIN_RD, 'and never below the guard rail');
  assert.ok(r.rd > 50 && r.rd < 110, `RD should settle in a usable band, got ${r.rd}`);

  let stable = r;
  for (let i = 0; i < 20; i += 1) stable = rating.update(stable, { rating: 1500, rd: 60 }, i % 2);
  assert.ok(Math.abs(stable.rd - r.rd) <= 5, 'and stay there');

  const before = stable.rating;
  const move = rating.update(stable, { rating: 1500, rd: 60 }, 1).rating - before;
  assert.ok(move >= 5, `a fully-measured player must still move a visible amount, moved ${move}`);
});

test('a level is withheld until placements are done', () => {
  const placing = { rating: 1800, rd: 200, matches: rating.PLACEMENT_MATCHES - 1 };
  const placed = { rating: 1800, rd: 200, matches: rating.PLACEMENT_MATCHES };
  assert.equal(rating.levelOf(placing), null);
  assert.ok(rating.levelOf(placed) >= 1);
  const pub = rating.publicRating(placing);
  assert.equal(pub.rating, null, 'the raw number is withheld too while placing');
  assert.equal(pub.placing, true);
  assert.equal(pub.placements_left, 1);
});

test('the level ladder is monotonic and 1500 sits mid-table', () => {
  let last = 0;
  for (let r = 500; r <= 2600; r += 25) {
    const level = rating.levelOf({ rating: r, matches: 20 });
    assert.ok(level >= last, `level went backwards at ${r}`);
    last = level;
  }
  const mid = rating.levelOf({ rating: rating.START_RATING, matches: 20 });
  assert.ok(mid >= 4 && mid <= 6, `a new player should land mid-ladder, got ${mid}`);
});

test('beating a stronger team is worth more than beating a weaker one', () => {
  const me = { rating: 1500, rd: 60, matches: 30 };
  const vsStrong = rating.update(me, { rating: 1900, rd: 60 }, 1).rating - me.rating;
  const vsWeak = rating.update(me, { rating: 1100, rd: 60 }, 1).rating - me.rating;
  assert.ok(vsStrong > vsWeak, `upset ${vsStrong} should beat expected win ${vsWeak}`);
});

test('a discounted match teaches us less', () => {
  const me = { rating: 1500, rd: 60, matches: 30 };
  const full = rating.update(me, { rating: 1700, rd: 60 }, 1, 1).rating - me.rating;
  const light = rating.update(me, { rating: 1700, rd: 60 }, 1, 0.4).rating - me.rating;
  assert.ok(light > 0 && light < full, `weighted ${light} must be a smaller move than ${full}`);
});

test('a penalty costs points but never confidence, matches or placements', () => {
  const before = { rating: 1500, rd: 300, vol: 0.06, matches: 2, wins: 1, losses: 1, updated: 1 };
  const after = rating.penalise(before, 25);
  assert.equal(after.rating, 1475);
  assert.equal(after.rd, before.rd, 'a no-show teaches us nothing about how good they are');
  assert.equal(after.matches, before.matches, 'and must not count towards placements');
});

test('nobody falls below the floor, however many penalties they take', () => {
  let r = { rating: rating.RATING_FLOOR + 10, rd: 60, matches: 30 };
  for (let i = 0; i < 20; i += 1) r = rating.penalise(r, 25);
  assert.equal(r.rating, rating.RATING_FLOOR);
});

test('uncertainty flattens a prediction towards a coin toss', () => {
  const known = rating.winProbability({ rating: 1700, rd: 50 }, { rating: 1500, rd: 50 });
  const unsure = rating.winProbability({ rating: 1700, rd: 330 }, { rating: 1500, rd: 330 });
  assert.ok(known > unsure, 'the same gap should predict harder when we are sure of it');
  assert.ok(Math.abs(rating.winProbability({ rating: 1500, rd: 60 }, { rating: 1500, rd: 60 }) - 0.5) < 1e-9);
});

test('arrows, not numbers', () => {
  assert.equal(rating.arrowsFor(0), 0);
  assert.equal(rating.arrowsFor(5), 1);
  assert.equal(rating.arrowsFor(-5), -1);
  assert.equal(rating.arrowsFor(rating.ARROW_MEDIUM + 1), 3);
  assert.equal(rating.arrowsFor(-(rating.ARROW_MEDIUM + 1)), -3);
  for (const d of [-999, -31, -16, -1, 0, 1, 16, 31, 999]) {
    assert.ok(Math.abs(rating.arrowsFor(d)) <= 3);
  }
});

// ==================================================================== matchmaker.cjs
console.log('\n--- matchmaking ---');

test('tolerance opens with time and closes on nobody', () => {
  const now = Date.now();
  const at = (ago) => mm.toleranceFor(mm.decorate(solo('x', 1500, { ago })), now);
  assert.ok(at(0) < at(10), 'waiting must buy tolerance');
  assert.ok(at(10) < at(30));
  assert.equal(at(mm.TOL_OPEN_SECONDS + 1), Infinity,
               'past the open window a unit takes whatever there is');
});

test('a player we have not measured accepts a wider match than one we have', () => {
  const now = Date.now();
  const known = mm.toleranceFor(mm.decorate(solo('k', 1500, { rd: rating.MIN_RD, ago: 0 })), now);
  const newbie = mm.toleranceFor(mm.decorate(solo('n', 1500, { rd: 350, ago: 0 })), now);
  assert.ok(newbie > known, 'uncertainty should buy tolerance, not cost it');
});

test('the split is CHOSEN, and beats cutting the queue in half', () => {
  const ladder = [1200, 1250, 1400, 1450, 1500, 1550, 1700, 1750, 1900, 2000];
  const match = mm.findMatch(ladder.map((r, i) => solo(`s${i}`, r)), { matchSize: 10, teamSize: 5 });
  assert.ok(match, 'ten people should produce a match');
  const sliced = Math.abs(mean(ladder.slice(0, 5)) - mean(ladder.slice(5)));
  assert.ok(match.delta < 20, `a chosen split should be near-even, was ${match.delta}`);
  assert.ok(match.delta < sliced / 5, `chosen ${match.delta} vs sliced-in-half ${sliced}`);
});

test('neither team is left carrying the whole spread', () => {
  const ladder = [1200, 1250, 1400, 1450, 1500, 1550, 1700, 1750, 1900, 2000];
  const match = mm.findMatch(ladder.map((r, i) => solo(`s${i}`, r)), { matchSize: 10, teamSize: 5 });
  const sd = (xs) => Math.sqrt(mean(xs.map((x) => (x - mean(xs)) ** 2)));
  const a = sd(teamValues(match, 1));
  const b = sd(teamValues(match, 2));
  // Scoring the average of the two spreads buys a very flat team paired with a very lumpy
  // one; scoring the worst does not. This is that, asserted.
  assert.ok(Math.max(a, b) / Math.min(a, b) < 2,
            `one team should not be twice as lumpy as the other (${a.toFixed(0)} vs ${b.toFixed(0)})`);
});

test('a bad queue is held at first and let through once people have waited', () => {
  const split = [900, 920, 940, 960, 980, 2200, 2220, 2240, 2260, 2280];
  const fresh = split.map((r, i) => solo(`f${i}`, r, { ago: 0 }));
  assert.equal(mm.findMatch(fresh, { matchSize: 10, teamSize: 5 }), null,
               'nobody has waited: hold out for a better match');
  const patient = split.map((r, i) => solo(`p${i}`, r, { ago: mm.TOL_OPEN_SECONDS + 5 }));
  assert.ok(mm.findMatch(patient, { matchSize: 10, teamSize: 5 }),
            'once they have waited, any game beats no game');
});

test('the longest waiter is never skipped', () => {
  const odd = [solo('WAITED', 1000, { ago: 80 })];
  [1500, 1510, 1520, 1530, 1540, 1550, 1560, 1570, 1580].forEach((r, i) => {
    odd.push(solo(`n${i}`, r, { ago: 1 }));
  });
  const match = mm.findMatch(odd, { matchSize: 10, teamSize: 5 });
  assert.ok(match, 'a match should form');
  const ids = [...match.teams[1], ...match.teams[2]].map((u) => u.key);
  assert.ok(ids.includes('WAITED'), 'the anchor must be in the match it unblocked');
});

test('a party is never split across the two teams', () => {
  const units = [party('P', [1500, 1500, 1500]),
                 ...[1500, 1500, 1500, 1500, 1500, 1500, 1500].map((r, i) => solo(`s${i}`, r))];
  const match = mm.findMatch(units, { matchSize: 10, teamSize: 5 });
  assert.ok(match);
  const onOne = match.teams[1].some((u) => u.key === 'P');
  const onTwo = match.teams[2].some((u) => u.key === 'P');
  assert.ok(onOne !== onTwo, 'the party must be wholly on one side');
});

test('a party searches on its AVERAGE, not on its members', () => {
  // Sam, 2026-09-15. 1200/1500/1800 goes shopping as three players of 1500.
  const p = mm.decorate(party('P', [1200, 1500, 1800]));
  assert.equal(new Set(p.values).size, 1, 'every member carries the same number');
  assert.equal(p.values[0], 1500 + mm.premiumFor(3));
  // And the team average is untouched by the flattening - that is what makes it free.
  const raw = mean([1200, 1500, 1800]) + mm.premiumFor(3);
  assert.equal(p.mean, raw);
});

test('a wide party is not punished for being wide', () => {
  // The spread term must not make teams holding a mixed-ability party score worse, or C11
  // ("friends can always play together") stops being true in the arithmetic.
  const wide = mm.findMatch([party('W', [1100, 1500, 1900]),
                             ...[1500, 1500, 1500, 1500, 1500, 1500, 1500].map((r, i) => solo(`a${i}`, r))],
                            { matchSize: 10, teamSize: 5 });
  const tight = mm.findMatch([party('T', [1500, 1500, 1500]),
                              ...[1500, 1500, 1500, 1500, 1500, 1500, 1500].map((r, i) => solo(`b${i}`, r))],
                             { matchSize: 10, teamSize: 5 });
  assert.ok(wide && tight);
  assert.equal(wide.delta, tight.delta, 'the wide party must matchmake exactly like the tight one');
});

test('a stack is worth more than its numbers say', () => {
  assert.ok(mm.premiumFor(5) > mm.premiumFor(2), 'a five-stack outranks a duo');
  assert.equal(mm.premiumFor(1), 0, 'a solo pays nothing');
  const stack = mm.decorate(party('P5', [1500, 1500, 1500, 1500, 1500]));
  assert.ok(stack.mean > 1500, 'the premium must actually reach the search value');
});

test('party shapes that cannot fill two teams do not form a match', () => {
  // A duo cannot make two teams of one. Answering null is correct; quietly splitting it
  // would break C10, and quietly forming a 1v1 out of two teammates would be worse.
  assert.equal(mm.findMatch([party('D', [1500, 1500])], { matchSize: 2, teamSize: 1 }), null);
  assert.equal(mm.bestSplit([mm.decorate(party('D', [1500, 1500]))], 1), null);
});

test('two solos form instantly at match size two (the one-person test rig)', () => {
  const match = mm.findMatch([solo('a', 1500, { rd: 350, ago: 0 }), solo('b', 1500, { rd: 350, ago: 0 })],
                             { matchSize: 2, teamSize: 1 });
  assert.ok(match, 'COMP_MATCH_SIZE=2 must still match immediately or the wire suite stalls');
  assert.equal(match.delta, 0);
});

test('a one-player match is legal and balanced by definition', () => {
  const match = mm.findMatch([solo('a', 1500)], { matchSize: 1, teamSize: 1 });
  assert.ok(match, 'COMP_MATCH_SIZE=1 is how the whole flow is exercised by one person');
  assert.equal(match.teams[2].length, 0);
});

test('three-player testing waits for all three, then assigns everyone once in a 1v2', () => {
  const queue = ['a', 'b', 'c'].map(key => solo(key, 1500));
  assert.equal(mm.findMatch(queue.slice(0, 2), { matchSize: 3 }), null);
  const match = mm.findMatch(queue, { matchSize: 3 });
  assert.ok(match, 'three testers must be able to form a match');
  assert.equal(teamValues(match, 1).length, 1);
  assert.equal(teamValues(match, 2).length, 2);
  assert.deepEqual(match.units.map(u => u.key).sort(), ['a', 'b', 'c']);
});

test('a three-player test keeps a duo together and rejects a three-stack', () => {
  const match = mm.findMatch([solo('a', 1500), party('duo', [1500, 1500])], { matchSize: 3 });
  assert.ok(match);
  assert.deepEqual(match.teams[1].map(u => u.key), ['a']);
  assert.deepEqual(match.teams[2].map(u => u.key), ['duo']);
  assert.equal(mm.findMatch([party('trio', [1500, 1500, 1500])], { matchSize: 3 }), null);
});

test('5v5 still waits for ten players and creates two teams of five', () => {
  const queue = Array.from({ length: 10 }, (_, i) => solo(String(i), 1500));
  assert.equal(mm.findMatch(queue.slice(0, 9), { matchSize: 10 }), null);
  const match = mm.findMatch(queue, { matchSize: 10 });
  assert.ok(match);
  assert.equal(teamValues(match, 1).length, 5);
  assert.equal(teamValues(match, 2).length, 5);
});

test('not enough people is null, not a short match', () => {
  assert.equal(mm.findMatch([solo('a', 1500)], { matchSize: 10, teamSize: 5 }), null);
  assert.equal(mm.findMatch([], { matchSize: 10, teamSize: 5 }), null);
});

test('quality falls as the match gets less fair', () => {
  const even = mm.findMatch([1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500, 1500]
    .map((r, i) => solo(`e${i}`, r)), { matchSize: 10, teamSize: 5 });
  const lopsided = mm.findMatch([900, 920, 940, 960, 980, 2200, 2220, 2240, 2260, 2280]
    .map((r, i) => solo(`l${i}`, r, { ago: mm.TOL_OPEN_SECONDS + 5 })), { matchSize: 10, teamSize: 5 });
  assert.equal(even.quality, 1);
  assert.ok(lopsided.quality < 0.5, `a match formed out of desperation should know it, got ${lopsided.quality}`);
});

test('the search is bounded on a big queue', () => {
  const many = [];
  for (let i = 0; i < 60; i += 1) many.push(solo(`m${i}`, 1200 + (i * 13) % 800, { ago: 3 }));
  const started = Date.now();
  const match = mm.findMatch(many, { matchSize: 10, teamSize: 5 });
  const took = Date.now() - started;
  assert.ok(match, 'sixty people should certainly produce a match');
  assert.ok(took < 500, `match formation must not become a compute problem, took ${took}ms`);
});

test('it is deterministic: the same queue and clock give the same answer', () => {
  const now = Date.now();
  const build = () => [1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 2100]
    .map((r, i) => ({ key: `d${i}`, joined: now - 5000, ratings: [player(r)] }));
  const a = mm.findMatch(build(), { matchSize: 10, teamSize: 5, now });
  const b = mm.findMatch(build(), { matchSize: 10, teamSize: 5, now });
  assert.deepEqual(a.teams[1].map((u) => u.key), b.teams[1].map((u) => u.key));
});

console.log(`\n${passed}/${passed + failed} tests passed`);
process.exit(failed ? 1 : 0);
