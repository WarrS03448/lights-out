/**
 * The rank service (task 8, part one): Glicko-2 ratings, placements, and the level ladder.
 *
 * PURE. No I/O, no timers, no Upstash, no knowledge of a match or a queue - so every number
 * the ladder is built on can be tested on its own, and the matchmaker can ask it questions
 * without dragging the live service in. `live.cjs` owns the storage and the wiring.
 *
 * WHY GLICKO-2 AND NOT PLAIN ELO (Sam, 2026-09-15). Flat-K Elo needs volume to converge, and
 * volume is the one thing a community ladder does not have. Glicko-2 carries a second number
 * per player - the rating DEVIATION, how sure we are - and that buys us two things a small
 * population cannot do without:
 *
 *   1. A new player is placed in ~5 matches instead of ~30, because a high RD means a big
 *      first move. That IS the placement system (technique 4); there is no separate "high K
 *      for your first five" hack layered on top.
 *   2. The matchmaker can ASK how sure we are. A player we do not know yet accepts a wider
 *      match than one we have measured forty times, which is the honest thing to do and also
 *      shortens their queue. See matchmaker.cjs `toleranceFor`.
 *
 * The game ships its own maths (`BodycamRankingManagerLibrary::CalculateTeamEloGain`, see
 * docs/autojoin.md). We do not use it: we cannot read its constants, cannot tune it, and it
 * has no concept of uncertainty. Ours is the one the ladder is built on.
 *
 * Idle uncertainty is handled by ageUncertainty(record, now), an ephemeral view of a stored
 * record. It increases RD only; inactivity never reduces skill or visible progress.
 * Visible RR is owned by progress.cjs, and leaderboard seats by live.cjs. There is no season reset.
 */

// The scale everything is stored and displayed on. 1500 is Glickman's own starting point and
// there is no reason to move it; the level bands below are what decide where 1500 SITS.
// How many rungs the ladder has. The NAMES are not this module's business (see below); the
// COUNT is, because a rating has to map onto the same scale the visible ladder is numbered on.
const LADDER = require('./ladder.cjs');

const START_RATING = Math.max(100, Number(process.env.COMP_RATING_START) || 1500);
// 350 is "we know nothing", and it is what makes placements work: the first match of a new
// player's life can move them several hundred points, which is the whole point.
const START_RD = Math.max(30, Number(process.env.COMP_RATING_START_RD) || 350);
const START_VOL = Math.max(0.01, Number(process.env.COMP_RATING_START_VOL) || 0.06);

// Glickman's system constant: how much a rating is allowed to lurch. Smaller is steadier.
// 0.3-1.2 is the paper's range; 0.5 is the middle and what most implementations ship.
const TAU = Math.min(1.2, Math.max(0.2, Number(process.env.COMP_RATING_TAU) || 0.5));

// RD FLOOR, and a BACKSTOP rather than the main event - measured, not assumed (2026-09-15).
//
// The worry it was written for is real in plain Glicko: drive RD to 20 and a player stops
// moving, so the ladder freezes at the top exactly where it most needs to keep sorting people.
// Glicko-2 already defends against that on its own, though. Step 6 re-inflates the deviation by
// the volatility every match (phi* = sqrt(phi^2 + sigma^2)), so RD does not decay towards zero -
// it settles where what a match teaches us balances what the volatility hands back. With these
// constants that equilibrium is ~68, comfortably ABOVE this floor, and a settled player still
// moves ~16 points a match. The test suite asserts both, and found this by failing.
//
// So MIN_RD binds only if TAU or the starting volatility are ever tuned down far enough to
// matter. It stays because that tuning is a policy dial and this is the guard rail under it.
// MAX_RD is the other end: nobody is less known than a brand-new account.
const MIN_RD = Math.max(20, Number(process.env.COMP_RATING_MIN_RD) || 45);
const MAX_RD = Math.max(MIN_RD, Number(process.env.COMP_RATING_MAX_RD) || START_RD);
// Conservative time policy: one idle week adds 30^2 to RD variance. A settled RD of 60
// becomes ~67 after a week, ~84 after a month. Always capped at new-player uncertainty.
const IDLE_RD_PER_WEEK = 30;
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

// Nobody goes below this, whatever they do. A negative rating is not a message anyone needs.
const RATING_FLOOR = Math.max(0, Number(process.env.COMP_RATING_FLOOR) || 100);

// PLACEMENTS (technique 4). Five matches before a level is shown. The rating is real from
// match one - it has to be, or the matchmaker has nothing to work with - but a number we are
// this unsure of should not be worn as a badge, and "3 of 5 placement matches" is a much
// better thing to show a new player than a level that will move three bands next game.
const PLACEMENT_MATCHES = Math.max(0, Number(process.env.COMP_PLACEMENT_MATCHES) === 0
  ? 0 : Number(process.env.COMP_PLACEMENT_MATCHES) || 5);

// Levels 1-10 never show numbers, only arrows (Sam): small/medium/large = 1/2/3. These are the
// point boundaries between those three words. The hub draws the arrows; this decides how many.
const ARROW_SMALL = Math.max(1, Number(process.env.COMP_ARROW_SMALL) || 15);
const ARROW_MEDIUM = Math.max(ARROW_SMALL + 1, Number(process.env.COMP_ARROW_MEDIUM) || 30);

const GLICKO_SCALE = 173.7178;

/** A rating record for somebody we have never seen. Shape-identical to a stored one. */
function defaultRating() {
  return { rating: START_RATING, rd: START_RD, vol: START_VOL,
           matches: 0, wins: 0, losses: 0, updated: 0, revision: 0,
           // The VISIBLE ladder - Valorant's RR, owned entirely by progress.cjs. It is carried
           // on this record so one stored object round-trips both numbers and there is only
           // ever one thing to save. Nothing in THIS file reads or moves it.
           progress: 0, demoteArmed: false, placementMeasured: 0, placementImpact: 0 };
}

/** Anything off the wire or out of Upstash, made safe. A corrupt record must cost one
 *  player their history, never the matchmaker its arithmetic. */
function normalise(record) {
  const base = defaultRating();
  if (!record || typeof record !== 'object') return base;
  // null, undefined and '' all coerce to 0, and a zero RD would clamp to MIN_RD - which reads
  // as "we have measured this player forty times" for a record that is simply absent. So a
  // missing field falls back explicitly rather than through Number().
  const num = (v, fallback) => {
    if (v === null || v === undefined || v === '') return fallback;
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const rating = Math.max(RATING_FLOOR, num(record.rating, base.rating));
  return {
    rating,
    rd: Math.min(MAX_RD, Math.max(MIN_RD, num(record.rd, base.rd))),
    vol: Math.min(1, Math.max(0.001, num(record.vol, base.vol))),
    matches: Math.max(0, Math.floor(num(record.matches, 0))),
    wins: Math.max(0, Math.floor(num(record.wins, 0))),
    losses: Math.max(0, Math.floor(num(record.losses, 0))),
    updated: Math.max(0, Math.floor(num(record.updated, 0))),
    revision: Math.max(0, Math.floor(num(record.revision, 0))),
    progress: Math.max(0, Math.floor(num(record.progress, 0))),
    demoteArmed: Boolean(record.demoteArmed),
    placementMeasured: Math.min(PLACEMENT_MATCHES, Math.max(0, Math.floor(num(record.placementMeasured, 0)))),
    placementImpact: Math.min(PLACEMENT_MATCHES, Math.max(0, num(record.placementImpact, 0))),
  };
}

/** A clock-explicit, non-mutating view for search/update. Apply to the STORED record, never
 * repeatedly to a previously aged view: updated stays untouched until a real update is saved.
 * Legacy records without a known update time cannot establish an idle interval. */
function ageUncertainty(record, now) {
  const me = normalise(record);
  if (!Number.isFinite(now) || !me.updated || now <= me.updated) return me;
  const weeks = (now - me.updated) / WEEK_MS;
  return { ...me, rd: Math.min(MAX_RD, Math.sqrt(me.rd ** 2 + IDLE_RD_PER_WEEK ** 2 * weeks)) };
}

/** Still placing? Their rating is real; their LEVEL is not shown yet. */
function isPlacing(record) {
  return normalise(record).matches < PLACEMENT_MATCHES;
}

/**
 * The level a rating is worth, 1-10. `null` while the player is still placing - which is the
 * same null the hub already renders as "unknown" for a party member with no rank, so nothing
 * on the client has to learn a new shape.
 */
function levelOf(record) {
  const r = normalise(record);
  if (r.matches < PLACEMENT_MATCHES) return null;
  let level = 1;
  for (let i = 0; i < LEVEL_THRESHOLDS.length; i += 1) {
    if (r.rating >= LEVEL_THRESHOLDS[i]) level = i + 1;
  }
  return Math.max(1, Math.min(LEVEL_THRESHOLDS.length, level));
}

// ---------------------------------------------------------------- the VISIBLE rank
//
// Sam, 2026-09-15: "we want a system thats showed to a player and a system thats hidden to the
// player ... similar to valorant where there are ranks with 3 subranks in each rank, you start at
// each sub rank at 0 and getting to 100 moves you up sub ranks".
//
// TWO SYSTEMS, TWO STORED NUMBERS. Hidden MMR estimates skill; progress.cjs accumulates visible
// RR independently and uses these thresholds to interpolate its continuous convergence target.
// This module carries progress through updates but never derives or changes it.
//
// Every dial is an environment variable, for the same reason the level thresholds are.
//
// NO NAMES LIVE HERE. This module is the HIDDEN half - a Glicko-2 rating and the rung that rating
// deserves. What the rungs are CALLED, and what a player has actually climbed to, is
// progress.cjs's business, and the names are in ladder.cjs. It used to keep its own copy of both,
// which is how the repository ended up with two ladders and the hub drew a badge from one beside
// a tier label from the other.
//
// What is left is the geometry: where the bottom of the ladder sits in rating, and how much
// rating one division is worth. With the defaults (700, 70) a new player's 1500 lands in rank 4
// of 9 - mid-ladder, with as much room to fall as to climb, which is the same property
// START_RATING was chosen for.
const MMR_BASE = Number(process.env.COMP_RANK_BASE) || 700;
const MMR_SPAN = Math.max(1, Number(process.env.COMP_RANK_SPAN) || 70);

// WHERE MMR SAYS A PLAYER BELONGS, as a RANK index on the same 1..N scale progress.cjs numbers
// the visible ladder on, because convergence compares the two directly and would be meaningless
// if they counted in different units. `LEVEL_THRESHOLDS[i]` is the lowest rating that is rank i+1.
//
// ONE ENTRY PER RUNG, derived rather than typed out, and that is the whole point: it used to be
// its own list of seven, for a ladder that had seven reachable ranks, and it silently clamped
// everything above the seventh. With nine rungs that meant nobody's MMR could ever say "Nightmare or
// better" - convergence would pull a Reaper player DOWN, and progress.seedFor could not
// place anyone past rank 7.
//
// Still a policy dial: COMP_LEVEL_THRESHOLDS overrides it wholesale, and turning it moves nobody's
// MMR - only which rung it says they deserve.
const LEVEL_THRESHOLDS = (process.env.COMP_LEVEL_THRESHOLDS
  ? process.env.COMP_LEVEL_THRESHOLDS.split(',').map((s) => Number(s.trim()))
      .filter((n) => Number.isFinite(n))
  : Array.from({ length: LADDER.RANKS },
               (_, i) => MMR_BASE + i * LADDER.DIVISIONS * MMR_SPAN));

/**
 * The whole ladder, as data, so a client can EXPLAIN the system instead of describing a guess.
 *
 * Every number here is an environment dial (COMP_LEVEL_THRESHOLDS, COMP_PLACEMENT_MATCHES,
 * COMP_ARROW_*), which is exactly why the hub must be told rather than ship its own copy: the day
 * Sam retunes the bands, a hardcoded table in the UI becomes a lie that nobody notices, because
 * nothing fails - it just tells players the wrong thing about their own rank.
 */
function ladder() {
  return {
    levels: LEVEL_THRESHOLDS.map((min, i) => ({
      level: i + 1,
      min,
      // The top band has no ceiling; null rather than a big number, so the UI can say so.
      max: i + 1 < LEVEL_THRESHOLDS.length ? LEVEL_THRESHOLDS[i + 1] - 1 : null,
    })),
    start_rating: START_RATING,
    floor: RATING_FLOOR,
    placement_matches: PLACEMENT_MATCHES,
    arrow_small: ARROW_SMALL,
    arrow_medium: ARROW_MEDIUM,
    // NO `ranks` HERE. The visible ladder a player climbs is progress.ranks(), and live.cjs
    // merges it in before sending this to a client. `levels` above is the hidden banding, and
    // shipping both from one module is how they came to disagree.
  };
}

/** How many arrows a points change is worth, signed, -3..3. Zero means no arrow at all. */
function arrowsFor(delta) {
  const n = Math.abs(Number(delta) || 0);
  if (n < 1) return 0;
  const size = n <= ARROW_SMALL ? 1 : (n <= ARROW_MEDIUM ? 2 : 3);
  return delta > 0 ? size : -size;
}

// ---------------------------------------------------------------- Glicko-2 internals
const g = (phi) => 1 / Math.sqrt(1 + (3 * phi * phi) / (Math.PI * Math.PI));
const expected = (mu, muOpp, phiOpp) => 1 / (1 + Math.exp(-g(phiOpp) * (mu - muOpp)));

/**
 * One team, seen as a single opponent.
 *
 * The rating is the team's MEAN - which is exactly what docs/competitive.md C11 says the
 * matchmaker balances on, so the ladder and the matchmaker agree about what a team is worth.
 * The deviation is the ROOT MEAN SQUARE of the members', not the mean: uncertainty adds in
 * quadrature, and a team carrying one unplaced player is genuinely less predictable than the
 * plain average of its RDs suggests.
 */
function teamAggregate(records) {
  const rows = (records || []).map(normalise);
  if (!rows.length) return { rating: START_RATING, rd: START_RD };
  const rating = rows.reduce((sum, r) => sum + r.rating, 0) / rows.length;
  const rd = Math.sqrt(rows.reduce((sum, r) => sum + r.rd * r.rd, 0) / rows.length);
  return { rating, rd };
}

/**
 * The chance `a` beats `b`, both as `teamAggregate` shapes. Glicko's own E(), so it accounts
 * for how sure we are: two teams 200 points apart but barely measured are closer to a coin
 * toss than two teams 200 points apart that we have watched for a season.
 *
 * Used for display and for scaling what a match is worth - never for deciding a winner.
 */
function winProbability(a, b) {
  const ratingA = Number(a && a.rating) || START_RATING;
  const ratingB = Number(b && b.rating) || START_RATING;
  const rdA = Number(a && a.rd) || START_RD;
  const rdB = Number(b && b.rd) || START_RD;
  const muA = (ratingA - START_RATING) / GLICKO_SCALE;
  const muB = (ratingB - START_RATING) / GLICKO_SCALE;
  // Both deviations fold into the one phi the formula takes: being unsure about EITHER side
  // pulls the prediction towards a coin toss, which is what uncertainty actually means here.
  const phi = Math.sqrt((rdA * rdA + rdB * rdB) / 2) / GLICKO_SCALE;
  return expected(muA, muB, phi);
}

/**
 * Glickman's step 5: the new volatility, by the Illinois variant of regula falsi.
 * Straight out of the paper; the only liberty taken is the iteration cap, so a pathological
 * input can never spin a Railway replica instead of ending a match.
 */
function newVolatility(phi, sigma, delta, v) {
  const a = Math.log(sigma * sigma);
  const d2 = delta * delta;
  const phi2 = phi * phi;
  const f = (x) => {
    const ex = Math.exp(x);
    const denom = 2 * ((phi2 + v + ex) ** 2);
    return (ex * (d2 - phi2 - v - ex)) / denom - (x - a) / (TAU * TAU);
  };
  let A = a;
  let B;
  if (d2 > phi2 + v) {
    B = Math.log(d2 - phi2 - v);
  } else {
    let k = 1;
    while (f(a - k * TAU) < 0 && k < 100) k += 1;
    B = a - k * TAU;
  }
  let fA = f(A);
  let fB = f(B);
  let guard = 0;
  while (Math.abs(B - A) > 1e-6 && guard < 100) {
    const C = A + ((A - B) * fA) / (fB - fA);
    const fC = f(C);
    if (fC * fB <= 0) { A = B; fA = fB; } else { fA /= 2; }
    B = C; fB = fC;
    guard += 1;
  }
  const out = Math.exp(A / 2);
  return Number.isFinite(out) ? Math.min(1, Math.max(0.001, out)) : sigma;
}

/**
 * One player, one match, against one aggregate opponent. `score` is 1 for a win, 0 for a
 * loss (0.5 exists for a draw the mode cannot produce, and costs nothing to support).
 *
 * `weight` scales how much the match is allowed to teach us, 0-1. It is how a match we
 * KNOWINGLY formed unbalanced - because somebody had waited ninety seconds and a fair one was
 * not available - pays out less than a match we are proud of. Applied to v, which is the term
 * the paper uses for "how much information this game carried", so a light match widens RD less
 * as well as moving the rating less. That is the honest reading: we learned less.
 */
function update(record, opponent, score, weight = 1, team = null) {
  const me = normalise(record);
  const opp = { rating: Number(opponent && opponent.rating) || START_RATING,
                rd: Math.max(MIN_RD, Number(opponent && opponent.rd) || START_RD) };
  const w = Math.min(1, Math.max(0.05, Number(weight) || 1));
  const s = Math.min(1, Math.max(0, Number(score)));

  const mu = (me.rating - START_RATING) / GLICKO_SCALE;
  const phi = me.rd / GLICKO_SCALE;
  const muOpp = (opp.rating - START_RATING) / GLICKO_SCALE;
  // With teams, use the same symmetric uncertainty and strength prediction as matchmaking.
  // Personal phi still governs how much this particular player learns from that residual.
  // Without a team argument this remains the existing individual Glicko API.
  const own = team ? normalise(team) : null;
  const phiOpp = (own ? Math.sqrt((own.rd ** 2 + opp.rd ** 2) / 2) : opp.rd) / GLICKO_SCALE;

  const gOpp = g(phiOpp);
  const E = expected(own ? (own.rating - START_RATING) / GLICKO_SCALE : mu, muOpp, phiOpp);
  // v is the estimated variance of the rating from THIS game. Dividing by the weight is what
  // makes a discounted match carry less information (a bigger variance = a smaller move).
  const v = 1 / (gOpp * gOpp * E * (1 - E) * w);
  const deltaTerm = gOpp * (s - E) * w;
  const delta = v * deltaTerm;

  const vol = newVolatility(phi, me.vol, delta, v);
  const phiStar = Math.sqrt(phi * phi + vol * vol);
  const phiNew = 1 / Math.sqrt(1 / (phiStar * phiStar) + 1 / v);
  const muNew = mu + phiNew * phiNew * deltaTerm;

  const rating = Math.max(RATING_FLOOR, Math.round(START_RATING + GLICKO_SCALE * muNew));
  const rd = Math.min(MAX_RD, Math.max(MIN_RD, Math.round(GLICKO_SCALE * phiNew)));
  return {
    rating, rd, vol,
    matches: me.matches + 1,
    wins: me.wins + (s > 0.5 ? 1 : 0),
    losses: me.losses + (s < 0.5 ? 1 : 0),
    updated: Date.now(),
    revision: me.revision,
    // Carried through untouched. The visible ladder is progress.cjs's to move, and it needs
    // the post-match MMR to decide by how much - so it must survive this call, not be reset
    // by it. Dropping these two here silently zeroed everyone's rank on every result.
    progress: me.progress,
    demoteArmed: me.demoteArmed,
    placementMeasured: me.placementMeasured,
    placementImpact: me.placementImpact,
  };
}

/**
 * Take points off a rating without it counting as a match - the no-show debt, and whatever
 * the abandon ladder (C6-C9) ends up charging. It must NOT touch RD or the match count: the
 * player did not play, so we learned nothing about them, and it must not shorten anybody's
 * placements. `matches` staying put is what stops a griefer placing themselves by no-showing.
 */
function penalise(record, points) {
  const me = normalise(record);
  const cost = Math.max(0, Math.round(Number(points) || 0));
  if (!cost) return me;
  return { ...me, rating: Math.max(RATING_FLOOR, me.rating - cost), updated: Date.now() };
}

/** What the hub is told about a player. Never the raw rating while they are placing. */
function publicRating(record) {
  const r = normalise(record);
  const placing = r.matches < PLACEMENT_MATCHES;
  return {
    // WHICH RUNG THIS MMR DESERVES, not which one the player has climbed to. Those are different
    // questions and this module only answers the first; `progress.publicProgress` answers the
    // second, and it is the one a player is shown. Null while placing.
    level: levelOf(r),
    rating: placing ? null : r.rating,
    rd: r.rd,
    matches: r.matches,
    wins: r.wins,
    losses: r.losses,
    placing,
    placements_left: Math.max(0, PLACEMENT_MATCHES - r.matches),
  };
}

module.exports = {
  defaultRating, normalise, ageUncertainty, isPlacing, levelOf, arrowsFor, ladder,
  teamAggregate, winProbability, update, penalise, publicRating,
  START_RATING, START_RD, START_VOL, TAU, MIN_RD, MAX_RD, RATING_FLOOR, IDLE_RD_PER_WEEK,
  PLACEMENT_MATCHES, LEVEL_THRESHOLDS, ARROW_SMALL, ARROW_MEDIUM,
};
