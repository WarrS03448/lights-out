/**
 * The matchmaker (Sam, 2026-09-15): who plays with whom, and when we stop waiting for better.
 *
 * PURE and DETERMINISTIC, like rating.cjs: it is handed a snapshot of the queue and a clock
 * reading and it answers with a match or with null. No timers, no sockets, no randomness -
 * which is what makes "would these ten people have been matched?" a thing a test can ask.
 * `live.cjs` owns the queue itself, the tick that calls this, and shuffling the teams it
 * returns so the captains are not always the same people.
 *
 * WHAT IT REPLACED. Until now match formation was `queue.splice(0, MATCH_SIZE)` - the first
 * ten in join order - and the teams were `ids.slice(0, 5)` against `ids.slice(5)`, which is
 * to say the first five to press Find Match played the next five. No rating was read, because
 * there was no rating. Three things are different now:
 *
 *   1. WIDENING TOLERANCE. Every queue unit carries a rating gap it is willing to accept, and
 *      that gap grows the longer it waits. A match forms when the gap it would create is
 *      inside what EVERY unit in it will accept. This is the one mechanism that trades queue
 *      time for balance per-player instead of globally, and it is why a small population does
 *      not have to choose between long queues and lopsided games - it gets whichever of the
 *      two the individual player has earned.
 *   2. THE SPLIT IS CHOSEN, NOT SLICED. Ten players can be cut into two fives 126 ways. We
 *      look at all of them and take the most even. This is free: it costs microseconds, costs
 *      nobody a second of queue time, and it is the single biggest balance win available at
 *      our size, because at ten players online the ten in the match are not a choice - the
 *      teams are.
 *   3. PARTIES ARE THE UNIT. The queue holds parties, not players, and a party is never split
 *      across the two teams (C10). A stack also pays a PREMIUM (below): five friends on voice
 *      are worth more than five strangers with the same ratings, and pretending otherwise is
 *      how a community ladder loses its solo players.
 *
 * Network eligibility is checked before rating balance, through network.cjs. Regions and
 * the host RTT ceiling never widen with queue time. Each regional pool has its own anchor;
 * cross-region candidates require every member's explicit opt-in.
 */
const rating = require('./rating.cjs');
const network = require('./network.cjs');

// ---------------------------------------------------------------- the widening window
// The gap - in points of team-average rating - a freshly queued unit is willing to accept.
// Tight on purpose: with anybody at all in the queue we want the GOOD match first.
const TOL_START = Math.max(0, Number(process.env.COMP_TOL_START) || 50);

// ...and how fast that gap opens up, per second of waiting. 15/s means a minute of queue buys
// 900 points of tolerance, which is most of the ladder: the shape is "be picky for the first
// twenty seconds, be realistic after a minute".
const TOL_PER_SECOND = Math.max(0, Number(process.env.COMP_TOL_PER_SECOND) || 15);

// The hard floor under queue time. Past this, a unit accepts ANY match it can be put in. At a
// hundred players this is never reached; at eleven players online it is the difference between
// a game and an empty queue, and an unbalanced game beats no game. Sam's brief exactly: the
// queue times must not be long.
const TOL_OPEN_SECONDS = Math.max(10, Number(process.env.COMP_TOL_OPEN_SECONDS) || 90);

// Uncertainty buys tolerance too (technique 3 paying for technique 1). A player we have never
// measured has no business insisting on a tight match - we do not know what a tight match FOR
// THEM would even be - and widening their window is what gets them through placements fast.
// Factored off the RD floor, so a fully-measured player gets exactly zero from this.
const TOL_RD_FACTOR = Math.max(0, Number(process.env.COMP_TOL_RD_FACTOR) || 0.5);

// ---------------------------------------------------------------- the stack premium
// What a party is worth ABOVE the sum of its members, in rating points per member, by party
// size. Five people on voice comms who queued together coordinate in a way five strangers do
// not, and that is real skill the ratings cannot see - it shows up as the stack winning more
// than its numbers say it should. Valorant and FACEIT both price it; we do too.
//
// Matchmaking only. It is NEVER stored and never shown: it changes who you are matched against,
// not what you are worth. Index is party size, so index 0 is unused.
const PARTY_PREMIUM = (process.env.COMP_PARTY_PREMIUM || '0,0,10,25,40,50')
  .split(',').map((s) => Math.max(0, Number(s.trim()) || 0));

// ---------------------------------------------------------------- choosing a split
// How much an UNEVEN team counts against a split next to an unfair one. A 5v5 whose averages
// match but where one team is two smurfs and three beginners is a worse game than the numbers
// say: the beginners get run over by the other team's mid-tier players while their own carries
// are in a different fight. Weighted well below the average gap - fairness between the teams is
// still the thing being optimised - but enough to break ties towards the flatter roster.
//
// It scores the WORST team, not the average of the two. Averaging them buys a very flat team
// with a very lumpy one, which is precisely the roster this term exists to avoid; taking the
// max means a split is only as good as the team having the worse time.
const SPREAD_WEIGHT = Math.max(0, Number(process.env.COMP_SPREAD_WEIGHT) || 0.35);

// A tie-break, in points per second of mean queue wait. Two candidate matches that are equally
// balanced should go to the people who have been waiting, and without this the search would
// pick whichever it happened to enumerate first.
const WAIT_BONUS = Math.max(0, Number(process.env.COMP_WAIT_BONUS) || 0.5);

// The gap at which a match counts as worthless as evidence. Feeds `quality`, which is what a
// later result should weight the rating update by: a match we knowingly formed lopsided because
// somebody had waited ninety seconds should not move the ladder as much as a fair one.
const QUALITY_SCALE = Math.max(1, Number(process.env.COMP_QUALITY_SCALE) || 300);

// The search below is exhaustive within a cap. Ten units is 2^10 splits and a few hundred
// combinations - nothing - but the queue is not bounded and a hundred units is. The cap is what
// stops a busy Saturday turning match formation into a compute problem; hitting it costs a
// slightly worse match on that tick, not a failure.
const MAX_CANDIDATES = Math.max(20, Number(process.env.COMP_MAX_CANDIDATES) || 400);

const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const mean = (xs) => (xs.length ? sum(xs) / xs.length : 0);

function stdev(xs) {
  if (xs.length < 2) return 0;
  const m = mean(xs);
  return Math.sqrt(mean(xs.map((x) => (x - m) ** 2)));
}

/** The per-member premium a party of this size carries into matchmaking. */
function premiumFor(size) {
  const n = Math.max(0, Math.floor(Number(size) || 0));
  return PARTY_PREMIUM[Math.min(n, PARTY_PREMIUM.length - 1)] || 0;
}

/**
 * A queue unit, with everything the search needs precomputed.
 *
 * In:  { key, joined, ratings: [ {rating, rd}, ... ] }   - one entry per member
 * Out: that, plus `size`, `values` (what each member is worth TO THE SEARCH), `total`,
 *      `mean` and `rd` (the RMS - see rating.teamAggregate for why not the average).
 *
 * A PARTY SEARCHES ON ITS AVERAGE (Sam, 2026-09-15). Every member of a party is handed the
 * party's mean rating rather than their own, so a party of 1200/1500/1800 goes looking for a
 * match as three players of 1500. Two consequences, both wanted:
 *
 *   * The team average is UNCHANGED - the sum is the same either way - so this costs the
 *     balance calculation nothing. It is purely about what the party is shopping as.
 *   * The party's internal spread stops counting against it. Without this the SPREAD_WEIGHT
 *     term below would quietly punish exactly the wide parties C11 exists to protect ("no cap
 *     on party skill spread ... friends can always play together"), by making every team
 *     holding one score worse and so making that party queue longer. Flattening here is what
 *     keeps C11 true in the arithmetic and not just in the docs.
 *
 * The premium rides on top. Individual ratings are untouched by all of this - they are what
 * the result settles against; this is only how the queue shops.
 */
function decorate(unit) {
  const rows = (unit.ratings || []).map(rating.normalise);
  const premium = premiumFor(rows.length);
  const base = rows.length > 1 ? mean(rows.map((r) => r.rating)) : null;
  const values = rows.map((r) => (base === null ? r.rating : base) + premium);
  return {
    ...unit,
    size: rows.length,
    premium,
    values,
    total: sum(values),
    mean: mean(values),
    rd: rows.length ? Math.sqrt(mean(rows.map((r) => r.rd * r.rd))) : rating.START_RD,
  };
}

/** How long this unit has been queueing, in seconds. */
function waitedSeconds(unit, now) {
  return Math.max(0, ((Number(now) || Date.now()) - (Number(unit.joined) || 0)) / 1000);
}

/**
 * The team-average gap this unit will accept right now. Infinity once it has waited out
 * TOL_OPEN_SECONDS - at which point it takes whatever there is, which is the whole point.
 */
function toleranceFor(unit, now) {
  const waited = waitedSeconds(unit, now);
  if (waited >= TOL_OPEN_SECONDS) return Infinity;
  const unsure = Math.max(0, (Number(unit.rd) || rating.START_RD) - rating.MIN_RD);
  return TOL_START + TOL_PER_SECOND * waited + TOL_RD_FACTOR * unsure;
}

/**
 * The best way to cut these units into two teams of `teamSize`, or null when there is none.
 *
 * Exhaustive over subsets, which is the honest way to say "126 possibilities, look at all of
 * them": with at most ten units that is 1024 masks and finishes in microseconds. A party is a
 * single unit, so it lands whole on one side or the other and C10 holds by construction -
 * which is also why this can legitimately return NULL: a duo cannot fill a team of one, and
 * five 2-stacks cannot make two teams of five. The caller must try a different set.
 *
 * Score: the gap between the team averages, plus a weighted nudge towards teams that are flat
 * inside themselves. Lower is better.
 */
function bestSplit(units, teamSize, otherTeamSize = teamSize) {
  const list = units.map((u) => (u.values ? u : decorate(u)));
  const n = list.length;
  if (!n || n > 20) return null;
  const want = Math.max(1, Math.floor(Number(teamSize) || 1));
  const wantOther = Math.max(0, Math.floor(Number(otherTeamSize) || 0));

  let best = null;
  const total = 1 << n;
  for (let mask = 0; mask < total; mask += 1) {
    const a = [];
    const b = [];
    let sizeA = 0;
    for (let i = 0; i < n; i += 1) {
      if (mask & (1 << i)) { a.push(list[i]); sizeA += list[i].size; } else { b.push(list[i]); }
    }
    if (sizeA !== want) continue;
    const sizeB = sum(b.map((u) => u.size));
    // The one-sided case is real: COMP_MATCH_SIZE=1 is how the whole flow is tested by one
    // person, and it makes a "team" of one against nobody. Balanced by definition.
    if (sizeB !== wantOther && sizeB !== 0) continue;

    const valuesA = a.flatMap((u) => u.values);
    const valuesB = b.flatMap((u) => u.values);
    const meanA = mean(valuesA);
    const meanB = valuesB.length ? mean(valuesB) : meanA;
    const delta = Math.abs(meanA - meanB);
    const spread = valuesB.length ? Math.max(stdev(valuesA), stdev(valuesB)) : stdev(valuesA);
    const score = delta + SPREAD_WEIGHT * spread;
    if (!best || score < best.score) {
      best = { a, b, meanA, meanB, delta, spread, score };
    }
  }
  return best;
}

/**
 * Try to build one match out of the queue. Returns null when nothing forms yet.
 *
 * `units` are undecorated queue entries; `now` is the clock the tolerances are measured
 * against, passed in so a test can fast-forward without sleeping.
 *
 * THE ANCHOR is the fairness guarantee. The longest-waiting unit is REQUIRED to be in any
 * match this returns, so the search can never keep assembling nice tidy matches out of
 * whoever just arrived while somebody sits at the top of the queue being skipped for being
 * an awkward rating. Everything else is chosen around them, nearest rating first.
 */
function findMatchPool(units, options = {}) {
  const now = Number(options.now) || Date.now();
  const matchSize = Math.max(1, Math.floor(Number(options.matchSize) || 10));
  const teamSize = Math.max(1, Math.floor(Number(options.teamSize) || Math.floor(matchSize / 2)));

  const list = (units || []).map(decorate).filter((u) => u.size > 0 && u.size <= matchSize);
  if (sum(list.map((u) => u.size)) < matchSize) return null;

  // Longest wait first: index 0 is the anchor.
  list.sort((x, y) => (x.joined || 0) - (y.joined || 0));
  const anchor = list[0];
  const required = [anchor, ...list.slice(1).filter(u => (options.requiredKeys || []).includes(u.key))];
  const need = matchSize - sum(required.map(u=>u.size));
  if (need < 0) return null;

  // Nearest rating to the anchor first, so the first complete candidates the search sees are
  // the good ones and the cap below costs us as little as possible when it bites.
  const rest = list.filter(u=>!required.includes(u))
    .sort((x, y) => Math.abs(x.mean - anchor.mean) - Math.abs(y.mean - anchor.mean));

  let best = null;
  let seen = 0;
  const budget = Math.max(1, Math.min(MAX_CANDIDATES, options.budget || MAX_CANDIDATES));

  const consider = (chosen) => {
    seen += 1;
    const connection = network.enforced()
      ? network.selectHost(chosen.flatMap(u => u.network || []), now) : null;
    if (network.enforced() && !connection) return;
    // Odd-sized test matches put the extra player on side two. Production
    // still requires five per side, and parties remain indivisible.
    const split = bestSplit(chosen, teamSize, matchSize - teamSize);
    if (!split) return;                       // party shapes cannot fill two teams: not this set
    // EVERY unit must accept the gap, not just the impatient one. The binding constraint is
    // therefore the newest arrival's window, and it opens on its own within TOL_OPEN_SECONDS -
    // so this cannot starve anybody, it can only make them wait for their own tolerance.
    let tolerance = Infinity;
    for (const u of chosen) tolerance = Math.min(tolerance, toleranceFor(u, now));
    if (split.delta > tolerance) return;

    const waited = mean(chosen.map((u) => waitedSeconds(u, now)));
    const score = split.score - WAIT_BONUS * waited + (options.repeatCost ? options.repeatCost(chosen,now) : 0);
    const candidate = { chosen, split, tolerance, score, waited, network:connection };
    if (!best || compareMatches(candidate,best) < 0) {
      best = candidate;
    }
  };

  // Depth-first over `rest`, taking units until the match is exactly full. `remaining` prunes
  // the branches that can no longer reach matchSize at all.
  const walk = (from, picked, filled) => {
    if (seen >= budget) return;
    if (filled === need) { consider([...required, ...picked]); return; }
    let remaining = 0;
    for (let i = from; i < rest.length; i += 1) remaining += rest[i].size;
    if (filled + remaining < need) return;
    for (let i = from; i < rest.length; i += 1) {
      if (seen >= budget) return;
      if (filled + rest[i].size > need) continue;       // too big for the hole left
      picked.push(rest[i]);
      walk(i + 1, picked, filled + rest[i].size);
      picked.pop();
    }
  };
  if (options.witness) consider(options.witness.map(key=>list.find(u=>u.key===key)));
  walk(0, [], 0);

  if (!best) return null;
  const { chosen, split, tolerance, waited } = best;
  const teamRd = (units) => Math.sqrt(sum(units.map(u => u.rd ** 2 * u.size)) / sum(units.map(u => u.size)));
  return {
    units: chosen,
    network: best.network,
    score: best.score,
    teams: { 1: split.a, 2: split.b },
    delta: split.delta,
    spread: split.spread,
    tolerance,
    waited,
    // 1.0 is a match we would defend; 0 is one we formed because somebody had waited long
    // enough that any game beat no game. Stored on the match so a later result can weight the
    // rating update by how much the game was actually evidence of anything.
    quality: Math.max(0, 1 - Math.min(1, split.delta / QUALITY_SCALE)),
    // What each side is worth, for the record and for the win-probability line.
    ratings: {
      1: { rating: split.meanA, rd: teamRd(split.a) },
      2: split.b.length
        ? { rating: split.meanB, rd: teamRd(split.b) }
        : { rating: split.meanA, rd: teamRd(split.a) },
    },
  };
}

// Network rules never widen with time. Separate anchors let EU form while NA lacks a game.
function compareMatches(a,b) {
  if (a.network && b.network) {
    const cross = Number(a.network.cross_region) - Number(b.network.cross_region);
    if (cross) return cross;
    const target = Number(a.network.worst > network.TARGET_PING) - Number(b.network.worst > network.TARGET_PING);
    if (target) return target;
  }
  return a.score - b.score || ((a.network && b.network) ? a.network.average - b.network.average : 0);
}

function findMatch(units, options = {}) {
  if (!network.enforced()) return findMatchPool(units,options);
  const now = Number(options.now) || Date.now();
  const eligible = (units || []).filter(u => Array.isArray(u.network)
    && u.network.length === (u.ratings || []).length && u.network.length
    && u.network.every(p => network.ready(p,now)) && network.compatible(u.network));
  const pools = network.REGIONS.map(region => eligible.filter(u => u.network.every(p => p.region === region)));
  pools.push(eligible.filter(u => u.network.every(p => p.cross_region === true)));
  let best = null;
  const groups = pools.flatMap(pool=>networkFeasiblePools(pool,options,now)).slice(0,MAX_CANDIDATES);
  const budget = Math.max(1,Math.floor(MAX_CANDIDATES / Math.max(1,groups.length)));
  for (const star of groups) {
      const candidate = findMatchPool(star.units,{...options,requiredKeys:star.requiredKeys,witness:star.witness,budget});
      if (candidate && (!best || compareMatches(candidate,best) < 0)) best = candidate;
  }
  return best;
}

// A fresh local marker alone is not enough to anchor a pool: there must be a host that can
// actually accommodate that unit in two whole teams. DP checks party sizes before the more
// expensive rating search. This stops one player with no peer estimates blocking everyone.
function networkFeasiblePools(pool, options, now) {
  const matchSize = Math.max(1,Math.floor(Number(options.matchSize) || 10));
  const sizeA = Math.max(1,Math.floor(Number(options.teamSize) || Math.floor(matchSize/2)));
  const sizeB = matchSize - sizeA;
  const stars = [];
  for (const hostUnit of pool) for (const host of hostUnit.network) {
    const connected = pool.filter(u => u.network.every(p => p.id === host.id || (() => {
      const ping = network.pairPing(host,p,now);
      return ping !== null && ping <= network.MAX_PING;
    })()));
    if (!connected.includes(hostUnit) || connected.reduce((n,u)=>n+u.network.length,0) < matchSize) continue;
    stars.push({hostUnit,connected});
  }
  const skipped = new Set();
  for (const anchor of pool.slice().sort((a,b)=>(a.joined || 0)-(b.joined || 0))) {
    const validStars = [];
    const signatures = new Set();
    for (const star of stars.slice().sort((a,b)=>Number(b.hostUnit===anchor)-Number(a.hostUnit===anchor))) {
      const {hostUnit}=star,connected=star.connected.filter(u=>!skipped.has(u));
      if (!connected.includes(anchor) || !connected.includes(hostUnit)) continue;
      const signature=connected.map(u=>u.key).sort().join('|');
      if(signatures.has(signature))continue;
      let states = new Map([['0,0',[]]]);
      for (const unit of connected) {
        const mandatory = unit === anchor || unit === hostUnit;
        const next = mandatory ? new Map() : new Map(states);
        for (const [state,chosen] of states) {
          const [a,b] = state.split(',').map(Number), size = unit.network.length;
          if (a+size <= sizeA && !next.has(`${a+size},${b}`)) next.set(`${a+size},${b}`,[...chosen,unit.key]);
          if (b+size <= sizeB && !next.has(`${a},${b+size}`)) next.set(`${a},${b+size}`,[...chosen,unit.key]);
        }
        states = next;
      }
      const witness=states.get(`${sizeA},${sizeB}`);
      if(witness){signatures.add(signature);validStars.push({hostUnit,connected,witness});}
    }
    if (validStars.length) {
      // Keep each host's feasible group intact so invalid mixtures cannot exhaust
      // the bounded candidate search before a playable match is considered.
      return validStars.map(({hostUnit,connected,witness}) => ({
        units:connected,requiredKeys:[anchor.key,hostUnit.key],witness}));
    }
    skipped.add(anchor);
  }
  return [];
}

module.exports = {
  findMatch, bestSplit, toleranceFor, waitedSeconds, decorate, premiumFor,
  TOL_START, TOL_PER_SECOND, TOL_OPEN_SECONDS, TOL_RD_FACTOR,
  PARTY_PREMIUM, SPREAD_WEIGHT, WAIT_BONUS, QUALITY_SCALE, MAX_CANDIDATES,
};
