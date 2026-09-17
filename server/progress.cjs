/**
 * The VISIBLE ladder - Valorant's RR, in our shape (Sam, 2026-09-15: "i have an extensive
 * amount of time in valorant so if is documentation on which factors and stats play into how
 * mmr/elo is attributed for valorant, lets mimic their logic to the best of our ability").
 *
 * PURE, like rating.cjs, matchmaker.cjs and valuation.cjs.
 *
 * ================================================================== WHY THERE ARE TWO NUMBERS
 *
 * Valorant runs two: a hidden MMR that decides matchmaking and payouts, and a visible Rank
 * Rating that chases it. We had one. This file is the second.
 *
 *   rating.cjs   MMR   - Glicko-2, hidden, what the matchmaker sorts on, what a result moves
 *   progress.cjs RR    - visible, what the player watches, what the level is drawn from
 *
 * Splitting them is what makes the rest of Riot's model expressible at all: "convergence" is
 * *by definition* the gap between the two, and there is nothing to converge with one number.
 *
 * ==================================================== THE FOUR FACTORS RIOT ACTUALLY DOCUMENTS
 *
 * Riot states four things move RR, and all four are here:
 *
 *   1. WIN / LOSS            the dominant term - BASE_WIN / BASE_LOSS below.
 *   2. ROUND DIFFERENTIAL    a 13-3 pays more than a 13-11. RR_ROUND_DIFF.
 *   3. PERFORMANCE BONUS     individual play, and ONLY BELOW THE TOP RANKS. Riot: the bonus
 *                            applies Iron through Ascendant; Immortal and Radiant reward wins
 *                            and round difference only. PERF_FREE_LEVEL is our Ascendant line.
 *   4. CONVERGENCE           RR is multiplied by how far MMR sits from the visible rank, and
 *                            Riot describes it as strongest once they are more than ~3 tiers
 *                            apart. CONVERGE_PER_LEVEL, capped by CONVERGE_MAX.
 *
 * Riot also mentions match difficulty as an input. Ours is in MMR, where Glicko already prices
 * the opponent, and it reaches RR through convergence. The `weight` award() takes is NOT that: it
 * is valuation.cjs's `rrWeight`, how much of the match this player was actually in - see award().
 *
 * Two hard rules of Riot's, both enforced below and both tested:
 *
 *   * YOU CANNOT GAIN RR ON A LOSS. Whatever the performance bonus says, a loss is a loss.
 *   * YOU CAN GAIN MMR ON A LOSS. Strong individual play still moves the hidden rating up.
 *     That one is not implemented here - it falls out of valuation.cjs handing Glicko a score
 *     of up to PERF_BAND for a good loser, which beats a heavy underdog's expected score. It
 *     is noted here because the two rules only make sense as a pair.
 *
 * WHERE WE DELIBERATELY DIVERGE, and why:
 *
 *   * Valorant shows RR at every rank. Sam's rule (docs/competitive.md) is that the ranks below
 *     the top show ARROWS and never numbers, with a figure appearing only at the counting band.
 *     So the number exists at every rank here and the hub simply does not draw it lower down.
 *     Both rules are kept; only the display differs.
 *   * Valorant restricts party composition at high ranks. C11 says we never will.
 *   * Valorant soft-resets each Act. We have no season yet, so nothing resets (see rating.cjs).
 */
const rating = require('./rating.cjs');
const ladder = require('./ladder.cjs');

// ---------------------------------------------------------------- the ladder: ranks and divisions
// Sam, 2026-09-15: "ranks have 3 subranks that you incrementally move through every 100 BDR".
// Valorant's shape exactly - Iron 1/2/3, Bronze 1/2/3, and so on.
//
// THE NAMES LIVE IN ladder.cjs, and this module is what turns them into a rank a player has
// climbed to. There used to be a second set here - Static/Witness/Responder/Operator/Enforcer/
// Nightwatch/Ghostframe/Blackout, six tiered ranks with an apex and an unawardable top-500 rank -
// and a player could be shown a badge from one ladder and a tier label from the other on the same
// panel. One ladder, one file.
//
// Eight named ranks of three divisions each, then ONE capstone:
//   * RANK_NAMES    ranks 1..8, each with DIVISIONS divisions of RR_PER_DIVISION RR
//   * RANK_TOP      rank 9. No divisions, and no amount of RR awards it - see THE TOP OF THE
//                   LADDER below. Valorant's Radiant.
const RANK_NAMES = ladder.NAMES;
const RANK_TOP = ladder.TOP;
const DIVISIONS = ladder.DIVISIONS;
const RR_PER_DIVISION = Math.max(10, Number(process.env.COMP_RR_PER_DIVISION) || 100);

// Every named rank has divisions; only the capstone above them does not.
const TIERED_RANKS = ladder.TIERED;

// ============================================================ THE TOP OF THE LADDER
//
// Sam, 2026-09-16: "lets do the numbers start counting from Spectre 1 ... until 99 RR. then at
// 100 RR they become spectre 2 ... Then once a user hits 200 RR+ they become Spectre 3. Then
// once a user hits 300 RR, they become eligible to become reaper if their RR is in the top 150
// highest RR values on the leaderboard."
//
// That is Valorant's Immortal/Radiant rule with our numbers, and it makes the LAST tiered rank
// behave unlike every rank below it. Two things change up there:
//
//   1. THE FIGURE STOPS RESETTING. Below the counting band every division is its own 0-99 RR
//      bucket. From Spectre 1 the figure counts from 0 and keeps going: Spectre 2 at 100,
//      Spectre 3 at 200, and Spectre 3 has NO CEILING - a player sitting on 940 RR is still
//      Spectre 3. This is the old BDR idea (docs/competitive-ideas.md) arriving at the rank it
//      was always meant for, and it is why the board can order the top of the ladder at all:
//      with per-division buckets everyone up there is tied on "99".
//
//   2. THE CAPSTONE IS NOT EARNED, IT IS SEATED. REAPER_RR only makes a player ELIGIBLE. The
//      badge itself belongs to the REAPER_SLOTS highest RR totals on the leaderboard, so it can
//      be taken off somebody by a stranger having a good night, and no function in this pure
//      module can decide it. live.cjs owns the cut and passes it in - see publicProgress.
//
// Where the counting band opens: the floor of the LAST tiered rank. 7 ranks x 3 divisions x
// 100 = 2100 RR by default.
const COUNT_RANK = TIERED_RANKS;                    // 8: Spectre, the counting band
const COUNT_AT = (TIERED_RANKS - 1) * DIVISIONS * RR_PER_DIVISION;

// The counted RR that makes a player eligible for the capstone, and the number of seats behind
// it. 300 RR (= progress 2400) and 150 seats by default; Valorant's are 550 and 500.
const REAPER_RR = Math.max(0, Number(process.env.COMP_REAPER_RR) || 300);
const REAPER_SLOTS = Math.max(1, Number(process.env.COMP_REAPER_SLOTS) || 150);
const REAPER_AT = COUNT_AT + REAPER_RR;

// Kept under its old name because callers and tests speak in these terms: the progress at which
// the capstone becomes POSSIBLE. It used to be the progress at which it was automatic.
const APEX_AT = REAPER_AT;
const APEX_RANK = TIERED_RANKS + 1;                 // 9: Reaper. Display only; see above.

// Kept as an alias because award() and the callers speak in these terms. One division IS one
// step of the ladder, so this is the unit everything below counts in.
const RR_PER_LEVEL = RR_PER_DIVISION;

// ------------------------------------------------------------------------------- the payouts
// A typical Valorant win is around +20 RR and a typical loss around -20, before everything
// else pulls on them. These are the same shape.
const BASE_WIN = Math.max(1, Number(process.env.COMP_RR_WIN) || 20);
const BASE_LOSS = Math.max(1, Number(process.env.COMP_RR_LOSS) || 20);

// FACTOR 2: round differential. A whitewash is worth this much more than a coin-toss
// scoreline, and a narrow loss costs this much less than a blowout.
const RR_ROUND_DIFF = Math.max(0, Number(process.env.COMP_RR_ROUND_DIFF) || 6);

// FACTOR 3: the performance bonus, and the rank at which it switches off. Riot's line is drawn
// between Ascendant and Immortal; ours is drawn at the COUNTING BAND - the same structural
// place, since that is exactly where our ladder stops being tiers and starts being a
// leaderboard. Below it the bonus fades in linearly rather than snapping off, so nobody loses a
// chunk of their payout for crossing one level boundary.
//
// It used to be drawn one rank higher, at the capstone. That was the same line when the capstone
// was reachable by RR alone; now that it is a seat rather than a rank, drawing it there would
// have left Spectre - our Immortal - paying a performance bonus Riot's model switches off.
const RR_PERF_MAX = Math.max(0, Number(process.env.COMP_RR_PERF) || 8);
const PERF_FREE_LEVEL = Math.max(2, Number(process.env.COMP_RR_PERF_FREE_LEVEL) || COUNT_RANK);

// FACTOR 4: convergence. RR is multiplied by how far MMR sits from the visible rank, in levels.
// Riot describes it as strongest past about three tiers of gap, so at 0.16 per level a 4-level
// gap is ~1.64x - which is where a double rank-up starts falling out of the arithmetic on its
// own rather than being a special case.
const CONVERGE_PER_LEVEL = Math.max(0, Number(process.env.COMP_RR_CONVERGE) || 0.16);
const CONVERGE_MAX = Math.max(1, Number(process.env.COMP_RR_CONVERGE_MAX) || 1.8);
const CONVERGE_MIN = Math.min(1, Math.max(0.1, Number(process.env.COMP_RR_CONVERGE_MIN) || 0.5));

// DEMOTION PROTECTION. Hitting 0 RR does not drop a level; you have to lose again while
// already sitting at 0. Valorant has this and it matters more than it sounds: without it a
// player on a level boundary bounces between two levels on alternate games.
const DEMOTE_NEEDS_SECOND_LOSS = String(process.env.COMP_RR_DEMOTE_GRACE || '1') !== '0';

// Nobody falls out of the bottom of the ladder.
const FLOOR = 0;

// ============================================================ WHERE A NEW PLAYER STARTS
//
// Sam, 2026-09-16, after placing Operator 2 himself: "i dont think players who are brand new to
// the game should ever start there", then "starting in operator is fine but never shadow", "a new
// player starting in operator should definitely be someone whos considered very good", and "lets
// have most people start around soldier or veteran".
//
// Placement used to seed exactly the rank MMR said. Five matches is a thin read, and MMR's bands
// put 1540 - forty points over the 1500 every account starts on - into Operator, so a 3-2
// placement landed there. A fresh placement is now seeded:
//
//   * PLACEMENT_RANK_OFFSET ranks BELOW what MMR says (1), and
//   * never above PLACEMENT_MAX_RANK (5 - Operator, on the default ladder).
//
// Operator also requires an undefeated run with measured performance in every placement,
// averaging at least 0.65 on valuation's combined team/lobby impact percentile.
// Winning all five alone is insufficient; without this evidence the cap is Veteran.
//
// It is a slower start, not a lower ceiling. Convergence measures the gap against MMR, so a player
// seeded below their MMR rank gains more and loses less until the badge has caught up.
const PLACEMENT_RANK_OFFSET = Math.max(0, Math.floor(process.env.COMP_PLACEMENT_RANK_OFFSET === undefined
  ? 1 : Number(process.env.COMP_PLACEMENT_RANK_OFFSET) || 0));
const PLACEMENT_MAX_RANK = Math.min(TIERED_RANKS,
  Math.max(1, Math.floor(Number(process.env.COMP_PLACEMENT_MAX_RANK) || 5)));
const OPERATOR_RANK = 5;
const PLACEMENT_MIN_IMPACT = 0.65;

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const num = (v, fallback = 0) => {
  if (v === null || v === undefined || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
};

/**
 * THE COUNTED FIGURE: RR measured from the bottom of the counting band, uncapped - or null
 * below it, where RR is a 0-99 figure inside a division instead.
 *
 * This is the one number the top of the ladder is ordered by, and the reason Spectre 3 has no
 * ceiling: a band that stopped counting would tie every player in it on the same 99.
 */
function countOf(progress) {
  const p = Math.max(FLOOR, num(progress, 0));
  return p < COUNT_AT ? null : p - COUNT_AT;
}

/** Is this enough RR to be SEATED at the capstone, if the leaderboard has room? */
function reaperEligible(progress) {
  return Math.max(FLOOR, num(progress, 0)) >= REAPER_AT;
}

/**
 * WHICH RANK a progress figure is, 1-based, and it stops at TIERED_RANKS.
 *
 * NO AMOUNT OF RR RETURNS THE CAPSTONE. That is deliberate and it is the whole shape of the
 * change: the capstone is a seat on the leaderboard, not a band of the ladder, so a pure
 * function of one player's RR cannot know whether they hold it. Everything that PAYS - the
 * convergence gap, the performance taper, demotion protection - wants this answer anyway: it
 * asks what the player has climbed to, and they have climbed to Spectre.
 *
 * `publicProgress` is the only place the capstone appears, and only when its caller says so.
 */
function rankOf(progress) {
  const p = Math.max(FLOOR, num(progress, 0));
  return clamp(Math.floor(p / (DIVISIONS * RR_PER_DIVISION)) + 1, 1, TIERED_RANKS);
}

/**
 * The division within the rank, 1..DIVISIONS.
 *
 * Two different sums, because the counting band works differently: below it a division is a
 * 0-99 bucket of its own, and inside it the divisions are THRESHOLDS on one running figure -
 * Spectre 1 at 0, Spectre 2 at 100, Spectre 3 at 200 and never a Spectre 4.
 *
 * Never null. The capstone is the only rank without a division and it is not this function's to
 * hand out; publicProgress nulls it when live.cjs says the player is seated.
 */
function divisionOf(progress) {
  const p = Math.max(FLOOR, num(progress, 0));
  const counted = countOf(p);
  if (counted !== null) return clamp(Math.floor(counted / RR_PER_DIVISION) + 1, 1, DIVISIONS);
  return clamp(Math.floor((p % (DIVISIONS * RR_PER_DIVISION)) / RR_PER_DIVISION) + 1, 1, DIVISIONS);
}

/** The rank's name, for display. The capstone's name is not in RANK_NAMES - it sits above them. */
function rankName(progress) {
  return ladder.nameOf(rankOf(progress));
}

/**
 * THE VISIBLE LADDER, as data, so a client can EXPLAIN ranked instead of describing a guess.
 *
 * This is what `hello` ships as `ladder.ranks` and what the hub's rank badges are keyed by: a
 * rank's INDEX is its position in `names`, so a rename needs no client release (docs/ranks.md).
 * It comes from HERE and not from rating.cjs because this is the ladder a player climbs - the
 * one they see. rating.cjs describes the hidden MMR underneath it and has no names at all.
 */
function ranks() {
  return {
    names: RANK_NAMES.slice(),
    top: RANK_TOP || null,
    divisions: DIVISIONS,
    // one division's worth of RR, how many divisions sit below the capstone, and the RR the
    // capstone opens at - everything a client needs to draw the ladder without a table of its own
    rr_per_division: RR_PER_DIVISION,
    bands: TIERED_RANKS * DIVISIONS,
    capstone_at: APEX_AT,
    // THE COUNTING BAND, so a client can explain the top of the ladder without a table of its
    // own: which rank it is (1-based, so `names[counting_rank - 1]`), the RR total it opens at,
    // the counted RR the capstone needs, and how many seats there are behind that.
    counting_rank: COUNT_RANK,
    counting_at: COUNT_AT,
    top_at: REAPER_RR,
    top_slots: REAPER_SLOTS,
  };
}

/**
 * The integer the rest of the system calls a "level": the RANK index.
 *
 * Deliberately the rank and not the division. A rank is what a badge SHOWS: the art in
 * hub/webui/static/ranks.svg draws the rank in its plate and the division in its silhouette, so
 * Operator I and Operator III are one rank wearing two silhouettes, not two ranks.
 */
function levelOf(progress) {
  return rankOf(progress);
}

/**
 * BDR: the uncapped figure the top of the ladder counts in, or null below it.
 *
 * It is `countOf` under the name the match result card and the hub already use - the presence
 * of this field is what switches those surfaces from arrows to a number (docs/match-result.md).
 * It used to open one rank higher, at a capstone that could be reached with RR alone; it opens
 * at the counting band now, which is the rank the idea was written for.
 */
function bdrOf(progress) {
  return countOf(progress);
}

/**
 * THE FIGURE A PLAYER IS SHOWN. 0-99 through the division below the counting band, and the
 * running count from the bottom of it once they are there.
 *
 * Never null once placed, which is the difference from the old capstone: there is no rank where
 * RR stops existing and a second currency starts, only a rank where it stops resetting.
 */
function withinLevel(progress) {
  const p = Math.max(FLOOR, num(progress, 0));
  const counted = countOf(p);
  return counted === null ? p % RR_PER_DIVISION : counted;
}

/**
 * Which STEP of the ladder this is, counting divisions from the bottom. Demotion protection
 * works on this rather than on the rank: falling out of Operator III into Operator II is a demotion
 * the player feels, and protecting only the rank boundary would let them slide two divisions
 * in two games without ever being held.
 *
 * THE TOP STEP IS THE LAST DIVISION AND EVERYTHING ABOVE IT. Spectre 3 has no ceiling, so a
 * player falling from 940 RR to 350 has not been demoted and must not be pinned as if they
 * had - they are in the division they started in the whole way down.
 */
function stepOf(progress) {
  const p = Math.max(FLOOR, num(progress, 0));
  return Math.min(Math.floor(p / RR_PER_DIVISION), TIERED_RANKS * DIVISIONS - 1);
}

/**
 * The named rank used by placement seeding and payout explanations. Continuous convergence
 * uses targetProgress below, including divisions and the unbounded counting band.
 *
 * `rating.levelOf` returns a rank index on the same 1..APEX_RANK scale (rating.cjs owns the
 * MMR thresholds), so the two can be compared directly. `null` means still placing: no
 * prediction, so no pull in either direction.
 */
function levelForRating(record) {
  const level = rating.levelOf(record);
  // MMR does not promise a Reaper seat. The named target remains capped at Spectre;
  // its continuous RR target can keep growing inside that same rank.
  return level === null ? null : clamp(level, 1, TIERED_RANKS);
}

/** Accumulate only measured placement evidence, before the final rank is seeded. */
function recordPlacement(before, after, breakdown) {
  if (!rating.isPlacing(before)) return;
  const previous = rating.normalise(before);
  const measured = breakdown && breakdown.measured === true
    && Number.isFinite(breakdown.actual) && breakdown.actual >= 0 && breakdown.actual <= 1;
  after.placementMeasured = previous.placementMeasured + (measured ? 1 : 0);
  after.placementImpact = previous.placementImpact + (measured ? breakdown.actual : 0);
}

/**
 * The progress a freshly placed player starts on: the MIDDLE DIVISION of the rank their
 * placement earned, halfway through it - and that rank sits below what MMR says, under a cap.
 * See WHERE A NEW PLAYER STARTS above.
 *
 * Mid-rank rather than the floor of it, so a placed player is not one loss from a demotion
 * they have not had the chance to earn, and has somewhere to fall before the badge changes.
 *
 * A FIRST PLACEMENT ONLY. Sam, 2026-09-16: "in the future we can have it so players could start
 * higher on a season rank reset based on their mmr". Seasons do not exist yet; when they do, the
 * reset wants its own seed rather than this one.
 */
function seedFor(record) {
  const earned = levelForRating(record);
  if (earned === null) return 0;
  const r = rating.normalise(record);
  const strongRun = rating.PLACEMENT_MATCHES > 0
    && r.matches === rating.PLACEMENT_MATCHES
    && r.wins === rating.PLACEMENT_MATCHES && r.losses === 0
    && r.placementMeasured === rating.PLACEMENT_MATCHES
    && r.placementImpact / rating.PLACEMENT_MATCHES >= PLACEMENT_MIN_IMPACT;
  const cap = Math.min(PLACEMENT_MAX_RANK, strongRun ? OPERATOR_RANK : OPERATOR_RANK - 1);
  const rank = clamp(earned - PLACEMENT_RANK_OFFSET, 1, cap);
  // The cap can be configured lower, but a first placement never exceeds Operator.
  const middle = Math.floor(DIVISIONS / 2);
  return (rank - 1) * DIVISIONS * RR_PER_DIVISION
    + middle * RR_PER_DIVISION
    + Math.floor(RR_PER_DIVISION / 2);
}

/**
 * FACTOR 3's taper. Full performance bonus at the bottom of the ladder, nothing at
 * PERF_FREE_LEVEL and above - Riot's "Immortal and Radiant reward wins and round difference
 * only". Linear rather than a cliff so one level boundary never costs a visible chunk of RR.
 */
function perfScale(visibleLevel) {
  const level = clamp(num(visibleLevel, 1), 1, APEX_RANK);
  if (level >= PERF_FREE_LEVEL) return 0;
  return clamp((PERF_FREE_LEVEL - level) / (PERF_FREE_LEVEL - 1), 0, 1);
}

/**
 * FACTOR 4. How hard the visible rank is being pulled towards MMR.
 *
 * `> 1` means MMR is above the visible rank: wins pay more and losses cost less, so the player
 * climbs to where they belong quickly. `< 1` is the mirror. Riot's three scenarios exactly.
 */
function targetProgress(record) {
  if (rating.isPlacing(record)) return null;
  const mmr = rating.normalise(record).rating;
  const bands = rating.LEVEL_THRESHOLDS;
  let i = 0;
  while (i < bands.length - 2 && mmr >= bands[i + 1]) i += 1;
  const width = Math.max(1, (bands[i + 1] ?? bands[i] + 210) - bands[i]);
  // Interpolate between rank floors, then extrapolate the last interval forever. A high
  // MMR targets RR in Spectre; it does not promise a Reaper seat or cap that RR target.
  return Math.max(FLOOR, (i + (mmr - bands[i]) / width) * DIVISIONS * RR_PER_DIVISION);
}

function convergence(record, progress) {
  const target = targetProgress(record);
  if (target === null) return 1;
  const gap = (target - Math.max(FLOOR, num(progress))) / (DIVISIONS * RR_PER_DIVISION);
  return clamp(1 + CONVERGE_PER_LEVEL * gap, CONVERGE_MIN, CONVERGE_MAX);
}

/**
 * What one finished match does to the visible ladder.
 *
 * `before`    the player's rating record BEFORE the match (carries `progress`)
 * `after`     the record after rating.update - used only for convergence, so the pull is
 *             measured against the MMR the match just produced, not the stale one
 * `opts`      { won, roundDiff (0..1), excess (-1..1), weight (0..1), demoteArmed }
 *
 * `weight` IS valuation.cjs's `rrWeight` - integrity and presence - and NOT the Glicko `weight`
 * beside it. That one also carries the matchmaker's quality and the scoreline's decisiveness,
 * and it used to be passed here: on Sam's 1v1 test nights quality sat at 0, the weight at its
 * 0.35 floor, and every win paid +8 RR instead of ~+23 (2026-09-16).
 *
 * Returns everything, not just the number, because the detail card has to be able to say WHY.
 */
function award(before, after, opts = {}) {
  const me = rating.normalise(before);
  const start = Math.max(FLOOR, num(before && before.progress, 0));
  const won = Boolean(opts.won);
  const roundDiff = clamp(num(opts.roundDiff, 0), 0, 1);
  const excess = clamp(num(opts.excess, 0), -1, 1);
  const weight = clamp(num(opts.weight, 1), 0, 1);

  // Placements pay no RR at all. There is no visible rank to move yet, and moving one the
  // player cannot see is how you end up explaining a rank that arrived from nowhere.
  if (rating.isPlacing(me)) {
    const placed = !rating.isPlacing(after);
    return {
      progress: placed ? seedFor(after) : 0,
      delta: 0, placing: !placed, placed,
      level: placed ? levelOf(seedFor(after)) : null,
      factors: { base: 0, roundDiff: 0, perf: 0, convergence: 1, weight },
    };
  }

  const visible = levelOf(start);
  const conv = convergence(after, start);

  // FACTOR 1 and FACTOR 2 together: the outcome, widened or narrowed by the scoreline.
  // A win pays more the more decisive it was; a loss costs less the closer it was.
  const base = won
    ? BASE_WIN + RR_ROUND_DIFF * roundDiff
    : -(BASE_LOSS + RR_ROUND_DIFF * roundDiff);

  // FACTOR 3: individual play, faded out towards the top of the ladder.
  const perf = RR_PERF_MAX * excess * perfScale(visible);

  // FACTOR 4: convergence multiplies gains and divides losses when MMR is above the rank, and
  // the other way round when it is below. Dividing rather than multiplying the loss is what
  // makes "you gain more AND lose less" true, which is how Riot describes it.
  const scaled = won ? (base + perf) * conv : (base + perf) / conv;

  // Round magnitudes symmetrically too: Math.round(-x.5) otherwise favors the player.
  let delta = Math.sign(scaled) * Math.round(Math.abs(scaled * weight));

  // RIOT'S HARD RULE: a loss can never pay. The performance bonus can soften a loss to the
  // floor and no further.
  if (won) delta = Math.max(1, delta);
  else delta = Math.min(-1, delta);

  let next = Math.max(FLOOR, start + delta);

  // DEMOTION PROTECTION: dropping out of the bottom of a DIVISION needs a second loss once you
  // are already at 0 RR in it. `demoteArmed` is the flag the caller carries between matches.
  //
  // On the division and not the rank, deliberately. Sliding Veteran III -> Veteran II is a
  // demotion the player watches happen; protecting only the rank boundary would let somebody
  // fall two divisions in two games without ever being held once.
  const startStep = stepOf(start);
  let demoteArmed = Boolean(opts.demoteArmed);
  if (!won && DEMOTE_NEEDS_SECOND_LOSS && stepOf(next) < startStep) {
    if (!demoteArmed || start !== startStep * RR_PER_DIVISION) {
      next = startStep * RR_PER_DIVISION;       // pinned at 0 RR in the division they were in
      demoteArmed = true;
    } else {
      demoteArmed = false;                     // the new division earns its own protection
    }
  } else if (won || stepOf(next) > startStep) {
    demoteArmed = false;
  }

  return {
    progress: next,
    delta: next - start,
    placing: false,
    placed: false,
    level: levelOf(next),
    rank: rankOf(next),
    rankName: rankName(next),
    division: divisionOf(next),
    levelChanged: levelOf(next) - visible,
    divisionChanged: stepOf(next) - startStep,
    demoteArmed,
    factors: {
      base: Math.sign(base) * Math.round(Math.abs(base)),
      roundDiff: (won ? 1 : -1) * Math.round(RR_ROUND_DIFF * roundDiff),
      perf: Math.round(perf),
      convergence: Number(conv.toFixed(3)),
      weight: Number(weight.toFixed(3)),
      mmrLevel: levelForRating(after),
      visibleLevel: visible,
    },
  };
}

/**
 * WHAT A PENALTY COSTS, and it comes off the ladder the player can SEE.
 *
 * It used to come off MMR (`rating.penalise`), which is the one number we have promised never to
 * show anybody - so the punishment was invisible on the day it landed and then leaked out over
 * the following matches as convergence dragged the visible rank down after it. A player could
 * not have told you it had happened, let alone why. Sam, 2026-09-16: "lets swap it so a penalty
 * doesnt cost any hidden rating and instead costs RR."
 *
 * So a penalty is now exactly what the Penalties screen says it is: RR off the total, today.
 *
 * THREE RULES, all of them deliberate:
 *
 *   * IT DOES NOT TOUCH MMR. The player did not play, so the matchmaker learned nothing about
 *     how good they are and must not be told otherwise (the same reasoning that made no-shows
 *     skip RD and placements in the first place).
 *   * DEMOTION PROTECTION DOES NOT APPLY. That flag exists so a bad match cannot drop you out
 *     of a division on its own; a penalty is not a match, and holding somebody at 0 RR after
 *     they abandoned nine other people would make the last rung of every division free.
 *   * A PLACING PLAYER LOSES NO RR, because they have none yet - there is no visible rank to
 *     take it off. The queue ban still applies in full, and it is the whole of the penalty
 *     until they have placed.
 *
 * Returns the same shape the ban does: what it cost and what is left, so the caller can say so.
 */
function penalise(record, points) {
  const me = rating.normalise(record);
  const start = Math.max(FLOOR, num(record && record.progress, 0));
  const cost = Math.max(0, Math.round(num(points, 0)));
  const placing = rating.isPlacing(me);
  if (!cost || placing) {
    return { progress: start, delta: 0, placing, level: placing ? null : levelOf(start) };
  }
  const next = Math.max(FLOOR, start - cost);
  return {
    progress: next,
    delta: next - start,             // negative, and never more than they had
    placing: false,
    level: levelOf(next),
    rank: rankOf(next),
    rankName: rankName(next),
    division: divisionOf(next),
    levelChanged: levelOf(next) - levelOf(start),
    divisionChanged: stepOf(next) - stepOf(start),
  };
}

/**
 * What the hub is told about the visible ladder. Mirrors rating.publicRating's shape.
 *
 * `opts.top` IS THE CAPSTONE, AND IT COMES FROM OUTSIDE. This module cannot work out whether a
 * player is one of the REAPER_SLOTS highest RR totals in the world - that is a leaderboard
 * question, and live.cjs answers it (`isReaper`) before calling this. Eligibility is still
 * checked here, so a caller that gets the cut wrong can seat nobody who has not earned the RR.
 *
 * `top_eligible` goes out either way: a player on 300+ RR who is not seated is being kept out
 * by other people's scores, and a UI that cannot say so can only look broken to them.
 */
function publicProgress(record, opts = {}) {
  const me = rating.normalise(record);
  if (rating.isPlacing(me)) {
    return { level: null, rank: null, rank_name: '', division: null, rr: null, bdr: null,
             counting: false, top: false, top_eligible: false,
             placing: true,
             placements_left: Math.max(0, rating.PLACEMENT_MATCHES - me.matches) };
  }
  const p = Math.max(FLOOR, num(record && record.progress, 0));
  const eligible = reaperEligible(p);
  const seated = Boolean(opts && opts.top) && eligible && Boolean(RANK_TOP);
  return {
    level: seated ? APEX_RANK : levelOf(p),
    rank: seated ? APEX_RANK : rankOf(p),
    rank_name: seated ? RANK_TOP : rankName(p),
    division: seated ? null : divisionOf(p),   // the capstone is the one rank without one
    rr: withinLevel(p),        // 0-99 in the division, or the running count at the top
    bdr: bdrOf(p),             // the same running count, null below the counting band
    // Whether `rr` is a running count with no ceiling. A client draws a progress bar when this
    // is false and a bare figure when it is true - a bar for Spectre 3 could only ever be full.
    counting: countOf(p) !== null,
    top: seated,
    top_eligible: eligible,
    placing: false,
    placements_left: 0,
  };
}

module.exports = {
  award, penalise, levelOf, bdrOf, withinLevel, levelForRating, seedFor, perfScale, convergence,
  publicProgress,
  rankOf, divisionOf, rankName, stepOf, ranks, countOf, reaperEligible,
  RANK_NAMES, RANK_TOP, DIVISIONS, RR_PER_DIVISION, RR_PER_LEVEL,
  TIERED_RANKS, APEX_AT, APEX_RANK,
  COUNT_RANK, COUNT_AT, REAPER_RR, REAPER_AT, REAPER_SLOTS,
  PLACEMENT_RANK_OFFSET, PLACEMENT_MAX_RANK, recordPlacement,
  BASE_WIN, BASE_LOSS, RR_ROUND_DIFF, RR_PERF_MAX, PERF_FREE_LEVEL,
  CONVERGE_PER_LEVEL, CONVERGE_MAX, CONVERGE_MIN, DEMOTE_NEEDS_SECOND_LOSS,
};
