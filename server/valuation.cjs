/**
 * The valuation system (Sam, 2026-09-15): what a played match is actually worth to each of the
 * ten people in it.
 *
 * PURE, like rating.cjs and matchmaker.cjs. Handed a match record and whatever the gamemode
 * managed to report, it answers with a Glicko score and a weight per player. `live.cjs` owns
 * the storage, the intake and the settling; nothing in here knows what a socket is.
 *
 * ================================================================================= THE SHAPE
 *
 * Glicko-2 takes two things per player: a SCORE `s` in [0,1] and a WEIGHT in (0,1] saying how
 * much this game is allowed to teach us. Everything below is a way of computing those two
 * numbers honestly. Nothing bolts points on afterwards - the whole valuation goes in through
 * Glicko's own front door, which is what keeps the maths coherent and the bounds provable.
 *
 *   s       = the outcome, nudged inside a band by how the player performed RELATIVE TO WHAT
 *             THEIR RATING PREDICTED. A win is never worth less than 1 - PERF_BAND; a loss is
 *             never worth more than PERF_BAND. So the outcome always dominates, and no amount
 *             of statistics turns a loss into a gain.
 *   weight  = how much the match is worth as EVIDENCE: how fair we expected it to be, how
 *             decisive it was, whether it was played clean, and how much of it this player was
 *             actually present for.
 *
 * ========================================================================== WHY RELATIVE, AND
 *                                                                           WHY AGAINST EXPECTATION
 *
 * Two decisions do most of the work here, and both exist to stop the thing that kills
 * performance-rated ladders: players optimising the statistic instead of the win.
 *
 * 1. EVERY METRIC IS RELATIVE AND PER-ROUND. Never "23 kills" - always "how did your kills per
 *    round compare with the other nine people in this match". Padding is impossible when the
 *    yardstick is the lobby you are standing in, and a long match is worth no more than a short
 *    one.
 *
 * 2. PERFORMANCE IS SCORED AGAINST EXPECTATION, not against zero. A 1900-rated player who tops
 *    the scoreboard has done exactly what their rating predicted and gets NO adjustment: the
 *    rating already knew. The adjustment only fires when someone lands somewhere their rating
 *    did not predict, which is precisely the case where the rating is wrong and wants moving.
 *    This is also what stops the system paying twice for the same skill.
 *
 * And one that protects the mode itself: `roundWinShare` is a first-class component, weighted
 * as heavily as fragging. A player who refuses to take the site to protect their K/D scores
 * badly on it. The metric that could teach people to play selfishly is balanced by one that
 * cannot be won any way except by winning rounds.
 *
 * ============================================================================ MISSING DATA IS
 *                                                                              THE NORMAL CASE
 *
 * The gamemode reports over `SendAttributionEvent`, which is the host's word, arrives in pieces
 * (one player per timer fire - see bb5_graphs.rule_stats), and may not arrive at all: the graph
 * is written but has never been cooked or played. So EVERY input here is optional and every one
 * has a defined absence:
 * with nothing reported at all, `valuation()` returns exactly the outcome-only result the
 * ladder had before any of this existed. Degrading to "we only know who won" is the designed
 * behaviour, not a failure path.
 */
const rating = require('./rating.cjs');

// ------------------------------------------------------------------ how far performance moves
// The most the outcome's score can be nudged. At 0.25 a winner is worth between 0.75 and 1.00
// and a loser between 0.00 and 0.25 - so the worst winner in the match is still scored higher
// than the best loser, by construction and at every rating. That gap is the invariant that
// makes "you cannot lose rating for winning" true rather than hoped for.
const PERF_BAND = Math.min(0.45, Math.max(0, Number(process.env.COMP_PERF_BAND) === 0
  ? 0 : Number(process.env.COMP_PERF_BAND) || 0.25));

// Performance is measured partly against your own four team-mates and partly against all ten.
// Within-team is the primary question ("were you carrying or carried?") because it is the one
// the outcome has NOT already answered; the lobby-wide term is what notices a whole team being
// dragged by one player. They must sum to 1.
const TEAM_WEIGHT = Math.min(1, Math.max(0, Number(process.env.COMP_PERF_TEAM_WEIGHT) || 0.65));
const LOBBY_WEIGHT = 1 - TEAM_WEIGHT;

// What goes into a player's impact, and how much each is worth. Deliberately flat-ish: any one
// of these being dominant is an invitation to farm it.
//
//   rounds   the share of rounds their side won while they were on the server. The anti-selfish
//            term, and the only one that cannot be improved by playing for yourself.
//   kills    kills per round played.
//   survival deaths per round played, inverted. Dying less is worth the same as killing more,
//            which is what stops "trade my life for a frag" being free.
//   score    the game's OWN per-player score, the figure its scoreboard sorts on
//            (BodycamGameState::SortPlayersByScore). What feeds it in Bodybomb is NOT verified -
//            it may be little more than kills - so it is weighted modestly and never alone. It
//            earns its place because it is the one reported number that MIGHT already include
//            objective work, and it costs one field to send.
//   clutch   rounds won from a man disadvantage, per round played. Rare and heavily capped;
//            it is a bonus for the thing everyone remembers, not a pillar.
//
// WHAT THE PAK ACTUALLY SENDS TODAY is kills, deaths, roundsPlayed and score. `rounds` and
// `clutch` need per-round accumulation the gamemode cannot do yet, so their weight is
// redistributed across the rest. The cost of that is real and worth stating plainly: `rounds` is
// the anti-selfish term, and until it arrives `score` is the only component that might reward
// objective play at all. See docs/valuation.md.
const COMPONENTS = {
  rounds: Math.max(0, Number(process.env.COMP_W_ROUNDS) || 0.30),
  kills: Math.max(0, Number(process.env.COMP_W_KILLS) || 0.25),
  survival: Math.max(0, Number(process.env.COMP_W_SURVIVAL) || 0.25),
  score: Math.max(0, Number(process.env.COMP_W_SCORE) || 0.12),
  clutch: Math.max(0, Number(process.env.COMP_W_CLUTCH) || 0.08),
};

// Validated combat is a small, fixed share of impact, never redistributed upward
// when legacy metrics are sparse. Missing/incomparable combat takes the exact
// legacy path. These components cannot expand PERF_BAND or change outcome bounds.
const COMBAT_COMPONENTS = Object.freeze({ damage: 0.10, assists: 0.05 });

// ------------------------------------------------------------------------------- the weight
// Floors, so a match that got played is always worth something. A weight of 0 would mean "this
// game taught us nothing at all", and a game ten people spent forty minutes on is never that.
const WEIGHT_FLOOR = Math.min(0.9, Math.max(0.05, Number(process.env.COMP_WEIGHT_FLOOR) || 0.35));

// DECISIVENESS. A 7-0 says more about who is better than a 7-6 does, so it is worth more as
// evidence. Runs from a coin-toss scoreline to a whitewash.
const DECISIVE_MIN = Math.min(1, Math.max(0.1, Number(process.env.COMP_DECISIVE_MIN) || 0.75));

// INTEGRITY. What a match loses for not having been a clean ten-player game: somebody walked
// out, somebody never turned up, it ended miles short of the score limit. Multiplicative and
// capped below, so three small problems do not zero a match out.
const INTEGRITY_LEAVER = Math.min(1, Math.max(0.1, Number(process.env.COMP_INT_LEAVER) || 0.6));
const INTEGRITY_SHORT = Math.min(1, Math.max(0.1, Number(process.env.COMP_INT_SHORT) || 0.7));
const INTEGRITY_FLOOR = Math.min(1, Math.max(0.1, Number(process.env.COMP_INT_FLOOR) || 0.4));

// PRESENCE. Rounds you were actually there for, as a share of the match. Someone who played
// four of twelve rounds moves less in BOTH directions - which is also what makes C9 ("the short
// team loses less if it loses") real arithmetic instead of a promise in a document.
const PRESENCE_FLOOR = Math.min(1, Math.max(0.05, Number(process.env.COMP_PRESENCE_FLOOR) || 0.25));

// How sure we have to be about a player's rating before their performance is judged against it.
// An unmeasured player (RD 350) has no meaningful prediction to beat, so their expectation is
// pulled towards the middle of the lobby and the adjustment mostly stands down - placements are
// for finding out where they belong, not for grading them against a guess.
const EXPECT_CONFIDENCE_RD = Math.max(50, Number(process.env.COMP_EXPECT_RD) || 120);

const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const mean = (xs) => (xs.length ? sum(xs) / xs.length : 0);
const num = (v, fallback = 0) => {
  if (v === null || v === undefined || v === '') return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
};

/**
 * Where `value` sits among `all`, as 0..1, by rank rather than by magnitude.
 *
 * RANK, not z-score, and on purpose: one outlier cannot drag everybody else's number, and a
 * player cannot improve their own percentile by inflating a statistic far past everyone else -
 * being first is being first whether it is by one kill or by thirty. That is a farming cap
 * built into the shape of the measurement.
 *
 * Ties share the mid-rank, so five identical players all score 0.5 and nobody is adjusted.
 */
function percentile(value, all) {
  const others = all.filter((x) => Number.isFinite(x));
  if (others.length < 2) return 0.5;
  let below = 0;
  let equal = 0;
  for (const x of others) {
    if (x < value) below += 1;
    else if (x === value) equal += 1;
  }
  return (below + (equal - 1) / 2) / (others.length - 1 || 1);
}

/**
 * The per-round numbers for one player, from whatever was reported.
 *
 * `roundsPlayed` is the denominator for everything, and it is the player's OWN - a substitute
 * who played four rounds is measured across four rounds, not twelve.
 */
function playerMetrics(row, match) {
  const roundsTotal = Math.max(1, num(match.rounds, 0) || num(match.roundsTotal, 0) || 1);
  const played = clamp(num(row.roundsPlayed, roundsTotal), 0, roundsTotal) || roundsTotal;
  // NOT REPORTED IS NOT ZERO. A row we never received defaults to 0 kills and 0 deaths, which
  // is a real and rather flattering scoreline (nought deaths is perfect survival). Defaulting
  // it that way made an empty report look like ten players who all went 0-0, and the whole
  // valuation fired on data nobody had sent. Absent metrics are NaN, which every component
  // below already reads as "no opinion".
  const has = (key) => row[key] !== null && row[key] !== undefined && row[key] !== ''
    && Number.isFinite(Number(row[key]));
  // ENEMY KILLS ONLY, and only from the feed. `row.kills` is the game's NET score and is
  // deliberately not consulted: it conflates a team kill with a missing enemy kill, so a player
  // who went ten-and-ten reads the same as one who never fired. With no feed this is NaN, the
  // component is dropped and its weight redistributed - no number beats a misleading one.
  const kills = has('enemyKills') ? Math.max(0, num(row.enemyKills)) : NaN;
  const deaths = has('deaths') ? Math.max(0, num(row.deaths)) : NaN;
  const roundsWonWhilePresent = has('roundsWon') ? clamp(num(row.roundsWon), 0, played) : NaN;
  return {
    steamId: row.steamId,
    roundsPlayed: played,
    presence: clamp(played / roundsTotal, 0, 1),
    kills,
    teamKills: has('teamKills') ? Math.max(0, num(row.teamKills)) : NaN,
    deaths,
    // Per round, always. An absolute count would make a twelve-round match worth twice a
    // six-round one for the same standard of play.
    kpr: Number.isFinite(kills) ? kills / played : NaN,
    // Inverted here rather than at the point of use, so every component points the same way:
    // bigger is better, for all four of them.
    survival: Number.isFinite(deaths) ? 1 - clamp(deaths / played, 0, 1) : NaN,
    // NaN when the round history never arrived, which `component` reads as "no opinion".
    roundWinShare: Number.isFinite(roundsWonWhilePresent) ? roundsWonWhilePresent / played : NaN,
    // Per round like every other component: an absolute score would make a long match worth
    // more than a short one for the same standard of play.
    spr: has('score') ? num(row.score) / played : NaN,
    clutch: has('clutches') ? num(row.clutches) / played : NaN,
  };
}

/**
 * One component, as a percentile within a group - or null when nobody in the group reported it.
 * A component nobody has is not a component everybody scores zero on; it is one that does not
 * exist for this match, and its weight is redistributed across the others.
 */
function componentPercentiles(rows, key) {
  const values = rows.map((r) => r[key]);
  const known = values.filter((v) => Number.isFinite(v));
  // Compare the same metric over the same population used by expectation(). Sparse samples
  // cannot stand in for the missing players, nor can each player choose a different metric.
  if (known.length < 2 || known.length !== rows.length) return null;
  // A component every player scores IDENTICALLY on separates nobody. Ranking on it would hand
  // them all 0.5 while still consuming its share of the weight, quietly diluting the components
  // that DO discriminate. Treated as absent instead, so its weight is redistributed. This is the
  // normal case for any team-wide figure compared within one team.
  if (known.every((v) => v === known[0])) return null;
  return rows.map((r) => percentile(r[key], values));
}

/**
 * Blend the components into one impact percentile per row, renormalising over whichever
 * components actually arrived.
 */
function impact(rows) {
  const parts = [];
  let total = 0;
  for (const [key, weight] of Object.entries(COMPONENTS)) {
    if (!weight) continue;
    const metricKey = { rounds: 'roundWinShare', kills: 'kpr', score: 'spr' }[key] || key;
    const ps = componentPercentiles(rows, metricKey);
    if (!ps) continue;
    parts.push({ ps, weight });
    total += weight;
  }
  if (!total) return rows.map(() => null);       // nothing was reported: no opinion at all
  return rows.map((_, i) => sum(parts.map((p) => p.ps[i] * p.weight)) / total);
}

function combatImpact(rows, enabled) {
  const legacy = impact(rows);
  const percentiles = enabled ? {
    damage: componentPercentiles(rows, 'damagePerRound'),
    assists: componentPercentiles(rows, 'assistsPerRound'),
  } : {};
  const used = Object.entries(COMBAT_COMPONENTS).filter(([key]) => percentiles[key]);
  const share = sum(used.map(([, weight]) => weight));
  const values = !share ? legacy : legacy.map((value, i) =>
    (value ?? 0.5) * (1 - share) + sum(used.map(([key, weight]) => percentiles[key][i] * weight)));
  return { values, percentiles };
}

/**
 * What this player's RATING predicted about where they would finish in the group.
 *
 * Pulled towards 0.5 by how unsure we are: a player with RD 350 has no prediction worth
 * beating, so their expectation is "middle of the pack" and their adjustment all but stands
 * down. That is the correct behaviour during placements - we are finding out where they
 * belong, not marking them against a number we invented five minutes ago.
 */
function expectation(ratings, index) {
  const me = rating.normalise(ratings[index]);
  const peers = ratings.filter((_, i) => i !== index);
  // Mean pairwise expectation is continuous in the MMR gap and in both uncertainties.
  // A one-point lead is a tiny advantage, not a jump from median to first place.
  const raw = peers.length ? mean(peers.map(peer => rating.winProbability(me, rating.normalise(peer)))) : 0.5;
  const rd = me.rd;
  const confidence = clamp(EXPECT_CONFIDENCE_RD / Math.max(rd, 1), 0, 1);
  return 0.5 + (raw - 0.5) * confidence;
}

/**
 * How decisive the scoreline was, as a multiplier on the match's evidential weight.
 *
 * `7-0` is a whitewash and worth full weight; `7-6` is close to a coin toss and worth
 * DECISIVE_MIN. Measured as the loser's share of the winner's score, so it does not care what
 * the score limit happens to be.
 */
function decisiveness(score, limit) {
  const a = Math.max(0, num(score && score[1], 0));
  const b = Math.max(0, num(score && score[2], 0));
  const top = Math.max(a, b, num(limit, 0), 1);
  const gap = Math.abs(a - b) / top;
  return DECISIVE_MIN + (1 - DECISIVE_MIN) * clamp(gap, 0, 1);
}

/**
 * What the match loses for not having been a clean, complete, ten-player game.
 *
 * Every one of these is something the SERVER knows on its own (tier 3) - no trust in the host
 * required - which is why integrity is the part of the weight that cannot be manufactured by a
 * tampered report.
 */
function integrity(match, stats) {
  let w = 1;
  const left = (match.left || []).length;
  if (left) w *= Math.pow(INTEGRITY_LEAVER, Math.min(3, left));
  // Ended a long way short of the limit: a match that stopped rather than finished.
  const limit = Math.max(1, num(stats.limit, 0) || num(match.score_limit, 0) || 1);
  const best = Math.max(num(match.score && match.score[1], 0), num(match.score && match.score[2], 0));
  if (best < limit) w *= INTEGRITY_SHORT;
  return clamp(w, INTEGRITY_FLOOR, 1);
}

/**
 * THE WHOLE VALUATION. Returns one row per player, each carrying the Glicko score and weight
 * `rating.update` should be called with, the `rrWeight` `progress.award` should be called with,
 * and the full breakdown that produced them.
 *
 * `match`  the server's own record: teams, left[], score, mm{} (tier 3 - always available)
 * `stats`  whatever the gamemode reported: { rounds, limit, players: [{steamId, kills,
 *          deaths, roundsPlayed, roundsWon, clutches}] } (tier 1 - frequently absent)
 * `ratings` steamId -> rating record, as they stood when the match FORMED
 * `winner` 1 or 2
 *
 * The breakdown is returned rather than logged because it is the match detail card, the
 * anomaly-detection input, and the only way anybody will ever be able to answer "why did I
 * only get one arrow for that".
 */
/**
 * REAL kills and deaths, out of the kill feed.
 *
 * The stat row's `Kill` is a NET score - a team kill decrements it - and Sam's verdict on that as a
 * metric is the right one: it is useless. It cannot tell a player who went ten-and-ten from one who
 * never fired, and a team kill followed by an enemy kill leaves no trace at all. Nothing here uses
 * it any more.
 *
 * The feed says what actually happened. An enemy kill and a team kill are separate events and are
 * counted separately, so neither can hide the other, and a team kill is never quietly netted off
 * somebody's score - if it is to cost them something, that has to be a decision made in the open.
 */
function killCounts(match) {
  const out = {};
  const row = (id) => (out[id] = out[id] || { enemyKills: 0, teamKills: 0, deathsByKill: 0 });
  for (const k of (match && match.kills) || []) {
    if (k.killer && k.victimTeam) {
      if (k.teamKill) row(k.killer).teamKills += 1;
      else if (!k.suicide) row(k.killer).enemyKills += 1;
    }
    if (k.victim) row(k.victim).deathsByKill += 1;
  }
  return out;
}

/**
 * ROUND WIN SHARE and CLUTCHES, the two components that have sat in the table unfed since this
 * model was written (Sam, 2026-09-15: "lets get the clutch and round win share wired").
 *
 * ROUND WIN SHARE IS NOT "what fraction of rounds did your team win". Everybody on a side plays
 * every round in BB5 - one life each, nobody joins mid-match - so that number is IDENTICAL for all
 * five of them and discriminates nobody. It would be `spr` all over again: the match outcome,
 * counted a second time, wearing a different name.
 *
 * What IS per-player is whether you were still standing when your team won it. Dying first in a
 * round your team goes on to win is a real contribution and a smaller one than closing it out, and
 * that difference is exactly what the anti-selfish term is supposed to see. So:
 *
 *     roundWinShare = rounds your team won AND you did not die in  /  rounds counted
 *
 * Whether you died in a round comes from the per-round series - the deaths delta - not from the
 * kill feed, because a victim is only NAMED when they have a SteamID, and the sweep names
 * everybody it reports on.
 *
 * CLUTCHES are kills taken while you were the last one standing on your side. The kill feed
 * carries the alive counts at the moment of each kill, so a 1vN is identifiable exactly rather
 * than guessed at from a survival heuristic. Normalised per round, like everything else here.
 *
 * Both return NaN where they cannot be measured - no rounds known, no team mapping, no feed - and
 * the component table then drops them and redistributes their weight, which is the same rule the
 * rest of this file follows.
 */
function roundContext(match, stats, teamOfPlayer) {
  const out = {};
  const rounds = (match && match.rounds) || {};
  const map = (match && match.team_map) || null;
  const perRound = roundDeltas(stats || {}, ['deaths']);

  // Which of OUR sides won each round. Without the mapping the round data says nothing about us.
  const wonBy = {};
  if (map) {
    for (const [n, row] of Object.entries(rounds)) {
      if (row && Number.isFinite(row.winTeam) && map[row.winTeam]) wonBy[Number(n)] = map[row.winTeam];
    }
    for (const [n, row] of Object.entries(match?.round_reports || {})) {
      if (row?.source === 'delegate' && Number.isInteger(row.winTeam) && map[row.winTeam]) wonBy[Number(n)] = map[row.winTeam];
    }
  }
  for (const [n,side] of Object.entries(match?.round_results || {})) if (side === 1 || side === 2) wonBy[Number(n)] = side;
  if(stats?.round_index_base === 0) {
    const total = Math.max(0,num(stats.rounds),num(stats.roundsTotal),num(match?.score?.[1])+num(match?.score?.[2]));
    for(const n of Object.keys(wonBy)) if(Number(n)<0 || (total && Number(n)>=total)) delete wonBy[n];
  }
  const played = Object.keys(wonBy).map(Number).sort((a, b) => a - b);

  const deathEvidence = {};      // steamId -> round -> known consecutive delta
  for (const [steamId, rows] of Object.entries(perRound)) {
    deathEvidence[steamId] = new Map(rows.filter(r => r.from === r.round - 1
      && !r.reset && Number.isFinite(r.deaths) && r.deaths >= 0).map(r => [r.round, r.deaths]));
  }

  // Clutch kills: an enemy killed while the killer's own side was down to one.
  const clutch = {};
  for (const k of (match && match.kills) || []) {
    if (!k.killer || k.teamKill || k.suicide || !k.killerTeam) continue;
    const gameTeamOfKiller = map
      ? Object.keys(map).find((g) => map[g] === k.killerTeam) : null;
    if (gameTeamOfKiller === null || gameTeamOfKiller === undefined) continue;
    const mine = Number(gameTeamOfKiller) === 0 ? k.alive0 : k.alive1;
    if (Number.isFinite(mine) && mine === 1) clutch[k.killer] = (clutch[k.killer] || 0) + 1;
  }

  const everyone = new Set([
    ...Object.keys(perRound), ...Object.keys(clutch),
    ...((match && match.players) || []).map((p) => p.player_id || p.steam_id),
  ]);
  for (const steamId of everyone) {
    const side = teamOfPlayer(steamId);
    const row = {};
    const evidence = deathEvidence[steamId];
    if (side && played.length && evidence && played.every(n => evidence.has(n))) {
      let credited = 0;
      for (const n of played) if (wonBy[n] === side && evidence.get(n) === 0) credited += 1;
      // COUNTS, not fractions. playerMetrics already divides roundsWon and clutches by the rounds
      // played, and doing it here as well would divide twice - so this fills the fields it already
      // reads rather than inventing new ones beside them.
      row.roundsWon = credited;
      row.roundsCounted = played.length;
    }
    if (played.length && Number.isFinite(clutch[steamId])) {
      row.clutches = clutch[steamId];
    }
    if (Object.keys(row).length) out[steamId] = row;
  }
  return out;
}

function valuation(match, stats, ratings, winner) {
  const report = stats && typeof stats === 'object' ? stats : {};
  const teams = match.teams || (match.mm && match.mm.teams) || {};
  const series = report.series && typeof report.series === 'object' ? report.series : null;
  let roundsTotal = Math.max(0, num(report.rounds), num(report.roundsTotal),
    num(match.score && match.score[1]) + num(match.score && match.score[2]));
  if(!roundsTotal || report.round_index_base !== 0) for (const samples of Object.values(series || {})) {
    for (const round of Object.keys(samples || {})) roundsTotal = Math.max(roundsTotal, num(round) + (report.round_index_base === 0 ? 1 : 0));
  }
  const metricContext = { ...report, rounds: roundsTotal };
  const byId = new Map((Array.isArray(report.players) ? report.players : [])
    .filter((p) => p && p.steamId)
    .map((p) => [String(p.steamId), { ...p }]));

  // A last-known summary is not necessarily a final summary. When sampling history exists,
  // verify EACH cumulative metric against the final round, not merely the player's latest
  // report: a round-1 zero-death sample says nothing about survival through rounds 2..10.
  // Use the final sample's value so stale summary copies cannot override newer evidence.
  // Direct callers without a series can still supply explicit complete summary fixtures.
  if (series) {
    for (const steamId of new Set([...byId.keys(), ...Object.keys(series)])) {
      const row = byId.get(steamId) || { steamId };
      const final = roundsTotal > 0 && series[steamId] && series[steamId][roundsTotal - (report.round_index_base === 0 ? 1 : 0)];
      for (const key of ['enemyKills', 'teamKills', 'deaths', 'score', 'roundsWon', 'clutches', 'roundsPlayed']) {
        const value = num(final?.[key], NaN);
        if (Number.isFinite(value)) row[key] = value;
        else delete row[key];
      }
      byId.set(steamId, row);
    }
  }

  // The feed overrides the counter wherever it has an opinion. Where there is no feed the kill
  // metric is simply NOT MEASURED - which the component table already handles by dropping it and
  // redistributing its weight. That is better than a number we know to be misleading.
  const counted = killCounts(match);
  // Event delivery has no completeness guarantee today. Counts are useful for the detail
  // card but cannot measure whole-match performance unless the service has verified coverage.
  const feedIds = match.kill_feed_complete === true
    ? new Set([...(teams[1] || []), ...(teams[2] || [])]) : new Set();
  for (const steamId of feedIds) {
    const tally = counted[steamId] || { enemyKills: 0, teamKills: 0 };
    const row = byId.get(steamId) || { steamId };
    row.enemyKills = tally.enemyKills;
    row.teamKills = tally.teamKills;
    byId.set(steamId, row);
  }


  const sides = { 1: [...(teams[1] || teams['1'] || [])], 2: [...(teams[2] || teams['2'] || [])] };
  const everyone = [...sides[1], ...sides[2]];
  if (!everyone.length) return [];

  // ROUND WIN SHARE and CLUTCHES, derived from the round store and the kill feed. Both were in the
  // component table from the start - 0.30 and 0.08 of the performance score - and until now
  // NOTHING could fill them. A report that already carries them keeps its own values; these only
  // fill what is absent.
  const sideOf = (steamId) => (sides[1].includes(steamId) ? 1 : (sides[2].includes(steamId) ? 2 : 0));
  for (const [steamId, ctx] of Object.entries(roundContext(match, report, sideOf))) {
    const row = byId.get(steamId) || { steamId };
    if (Number.isFinite(ctx.roundsWon) && ctx.roundsCounted === roundsTotal
      && !Number.isFinite(num(row.roundsWon, NaN))) {
      row.roundsWon = ctx.roundsWon;
    }
    if (match.kill_feed_complete === true && Number.isFinite(ctx.clutches)
      && !Number.isFinite(num(row.clutches, NaN))) {
      row.clutches = ctx.clutches;
    }
    byId.set(steamId, row);
  }

  // Coverage must be comparable across the ENTIRE authoritative roster, including
  // the other team. A numeric-looking string or an absent assist is not a zero.
  // Validating the collector for conduct must not silently roll out new match-rating inputs.
  const combatEligible = process.env.COMP_COMBAT_RATINGS_ENABLED !== '0'
    && roundsTotal > 0 && everyone.every(steamId => {
    const c = byId.get(steamId)?.combat;
    return c?.version === 1 && c.status === 'complete' && c.ratingsEligible === true
      && c.coverage?.damage === true && Number.isFinite(c.enemyDamage) && c.enemyDamage >= 0
      && Number.isFinite(c.assists) && c.assists >= 0;
  });
  const metricsFor = (steamId) => {
    const row = byId.get(steamId) || { steamId };
    const metrics = playerMetrics(row, metricContext);
    if (combatEligible) {
      metrics.damagePerRound = row.combat.enemyDamage / metrics.roundsPlayed;
      metrics.assistsPerRound = row.combat.assists / metrics.roundsPlayed;
    }
    return metrics;
  };
  const lobbyRows = everyone.map(metricsFor);
  const lobbyCombat = combatImpact(lobbyRows, combatEligible);
  const lobbyImpact = lobbyCombat.values;
  const lobbyRatings = everyone.map((id) => ratings[id]);

  // Per-side, so "were you carrying or being carried" is asked among the people who share your
  // result - the question the outcome has not already answered.
  const sideImpact = {};
  const sideCombat = {};
  const sideIndex = {};
  for (const n of [1, 2]) {
    const rows = sides[n].map(metricsFor);
    sideCombat[n] = combatImpact(rows, combatEligible);
    sideImpact[n] = sideCombat[n].values;
    sideIndex[n] = new Map(sides[n].map((id, i) => [id, i]));
  }

  const decisive = decisiveness(match.score || {}, report.limit || match.score_limit);
  const integrityW = integrity(match, report);

  const out = [];
  for (const n of [1, 2]) {
    const won = n === winner;
    for (const steamId of sides[n]) {
      const i = everyone.indexOf(steamId);
      const j = sideIndex[n].get(steamId);
      const m = lobbyRows[i];

      const inLobby = lobbyImpact[i];
      const inSide = sideImpact[n][j];
      const sideWeight = inSide === null ? 0 : TEAM_WEIGHT;
      const lobbyWeight = inLobby === null ? 0 : LOBBY_WEIGHT;
      const measuredWeight = sideWeight + lobbyWeight;
      // Only enabled groups with shared metric coverage provide evidence for this player.
      const measured = measuredWeight > 0;
      const actual = measuredWeight
        ? (sideWeight * (inSide ?? 0) + lobbyWeight * (inLobby ?? 0)) / measuredWeight : 0.5;
      const expected = measuredWeight
        ? (sideWeight * expectation(sides[n].map((id) => ratings[id]), j)
          + lobbyWeight * expectation(lobbyRatings, i)) / measuredWeight : 0.5;

      // -1 .. +1. Zero when a player finished exactly where their rating said they would,
      // which is the common case and correctly produces no adjustment at all.
      const excess = measured ? clamp(actual - expected, -1, 1) : 0;

      // THE SCORE. A winner lives in [1 - PERF_BAND, 1] and a loser in [0, PERF_BAND], so the
      // worst winner still outscores the best loser and the outcome can never be reversed by
      // performance. `excess` moves the player inside their own half and nowhere else.
      //
      // WITH NOTHING MEASURED THE SCORE IS THE BARE OUTCOME, 1 or 0 - not the middle of the
      // band. Sitting an unmeasured player at the centre of their half would quietly discount
      // every match played before the stats pak ships, which is a silent, permanent haircut on
      // the entire early ladder. Absence of evidence has to cost nothing.
      const inside = clamp(0.5 + excess / 2, 0, 1);
      const score = measured
        ? (won ? 1 - PERF_BAND * (1 - inside) : PERF_BAND * inside)
        : (won ? 1 : 0);

      const weight = clamp(
        (match.mm && Number.isFinite(Number(match.mm.quality)) ? clamp(Number(match.mm.quality), 0, 1) : 1)
          * decisive * integrityW * Math.max(PRESENCE_FLOOR, m.presence),
        WEIGHT_FLOOR, 1,
      );

      // HOW MUCH OF THE VISIBLE PAYOUT THIS PLAYER GETS - which is not `weight`.
      //
      // `weight` is evidence: how much this match may teach the HIDDEN rating. Two of its terms do
      // not belong on the ladder a player watches. `mm.quality` is the matchmaker's opinion of a
      // pairing nobody in it chose, and one we never show (NEVER PRICE A MATCH, live.cjs).
      // `decisive` is the scoreline, which RR already pays through its own round-differential
      // term. Scaling RR by `weight` docked everybody for the pairing and counted the scoreline
      // twice: on Sam's 1v1 test nights quality sat at 0, the weight at its 0.35 floor, and a win
      // paid +8 RR where the ladder is built to pay ~+23 (2026-09-16).
      //
      // What stays is what IS about this player's own match: whether it was played out clean
      // (`integrity` - leavers, which is C9) and how much of it they were there for (`presence`).
      const rrWeight = clamp(integrityW * Math.max(PRESENCE_FLOOR, m.presence), 0, 1);

      out.push({
        steamId,
        team: n,
        won,
        score,
        weight,
        rrWeight,
        // Everything below is for the detail card and for anomaly detection. None of it is
        // shown to a player before the match ends (docs/matchmaking.md, M2).
        breakdown: {
          measured,
          coverage: Object.fromEntries(['kpr', 'survival', 'spr', 'roundWinShare', 'clutch']
            .map(key => [key, Number.isFinite(m[key])])),
          actual, expected, excess,
          impactInTeam: inSide, impactInLobby: inLobby,
          decisive, integrity: integrityW,
          presence: m.presence, roundsPlayed: m.roundsPlayed,
          kills: m.kills, teamKills: m.teamKills, deaths: m.deaths,
          kpr: m.kpr, survival: m.survival, spr: m.spr,
          roundWinShare: m.roundWinShare, clutch: m.clutch,
          quality: match.mm && match.mm.quality,
          ...(combatEligible ? { combat: {
            version: 1, eligible: true,
            enemyDamage: byId.get(steamId).combat.enemyDamage,
            assists: byId.get(steamId).combat.assists,
            damagePerRound: m.damagePerRound, assistsPerRound: m.assistsPerRound,
            components: Object.fromEntries(Object.entries(COMBAT_COMPONENTS).map(([key, componentWeight]) => [key, {
              weight: componentWeight,
              teamPercentile: sideCombat[n].percentiles[key]?.[j] ?? null,
              lobbyPercentile: lobbyCombat.percentiles[key]?.[i] ?? null,
            }])),
          } } : {}),
        },
      });
    }
  }
  return out;
}

/**
 * PER-ROUND NUMBERS OUT OF THE SWEEP (layer 1b, docs/round-context.md section 3).
 *
 * `stats.series` is {steamId: {round: row}} - the running totals as they stood at the end of each
 * round, which live.cjs now keeps instead of throwing away. Differencing consecutive rounds turns
 * those totals into what the player actually DID in each round, which is the unit every contextual
 * metric is built from.
 *
 * Nothing here guesses. A round with no sample is absent from the result, not zero - the sweep
 * visits one player every 3 s, so a short round can genuinely go unsampled for somebody, and a
 * silent zero there would read as "did nothing" when it means "was not looked at". Same rule the
 * rest of this file follows.
 *
 * The baseline is the LAST round at or below the one being asked about, not `r - 1`: rounds can be
 * missing, and a delta taken against a gap would hand one round everything two rounds earned.
 *
 * THE COUNTERS ARE CUMULATIVE ACROSS THE MATCH, and cleared exactly once, at match start (Sam,
 * 2026-09-15, from his own time in the game: "once the game starts, the kills and death are kept
 * until the game ends"). The measurements agree: the host's Kill/Death stood at 2/1 and 0/1 during
 * warm-up and read 0/0 the moment round 1 began, then ran -1 in round 1 and +1 in round 2.
 *
 * SO ROUND 0 IS NOT A BASELINE. Warm-up totals belong to a counter that is about to be wiped, and
 * differencing round 1 against them produced -2 kills in round 1. The first real round is measured
 * against ZERO instead, which is what the clear actually leaves behind, and warm-up is dropped.
 *
 * AND A FALL IS NOT A RESET - not for kills. `Kill` is a NET score: a team kill decrements it
 * (measured, -1 after one enemy and two team-mates). A player who team-kills twice in a round has
 * a genuinely negative round, and reading that as a counter restart would report the new total
 * instead of the change - turning a -2 round into a -1 one and flagging it as an artefact. Only
 * fields that genuinely cannot fall are still guarded that way, which is why `signed` exists.
 *
 * Returns {steamId: [{round, kills, deaths, ...}]}, ascending, one entry per round that has both a
 * sample and something to compare it with.
 */
// Fields that are a NET score rather than a count, so a fall is real work and not a restart.
const SIGNED_METRICS = new Set(['kills']);

function roundDeltas(stats, keys = ['kills', 'deaths'], { warmupRound = stats?.round_index_base === 0 ? -1 : 0 } = {}) {
  const series = (stats && stats.series) || {};
  const out = {};
  for (const steamId of Object.keys(series)) {
    const rounds = Object.keys(series[steamId])
      .map(Number).filter((n) => Number.isFinite(n))
      .filter((n) => n > warmupRound)          // warm-up is not part of the match
      .sort((a, b) => a - b);
    const rows = [];
    // The clear at match start leaves zero behind, so the first real round is measured against it
    // rather than against warm-up numbers that no longer exist.
    let prev = { round: warmupRound, row: {}, zero: true };
    for (const r of rounds) {
      const now = series[steamId][r];
      {
        const entry = { round: r, from: prev.round };
        let any = false;
        for (const key of keys) {
          const a = prev.zero ? 0 : num(prev.row && prev.row[key], NaN);
          const b = num(now && now[key], NaN);
          if (!Number.isFinite(a) || !Number.isFinite(b)) continue;   // not reported is not zero
          if (b < a && !SIGNED_METRICS.has(key)) {
            entry[key] = b; entry.reset = true;    // a count that cannot fall, and did
          } else {
            entry[key] = b - a;                    // including a genuinely negative round
          }
          any = true;
        }
        if (any) rows.push(entry);
      }
      prev = { round: r, row: now };
    }
    if (rows.length) out[steamId] = rows;
  }
  return out;
}

module.exports = {
  valuation, playerMetrics, impact, percentile, expectation, decisiveness, integrity,
  roundDeltas, SIGNED_METRICS, killCounts, roundContext,
  PERF_BAND, TEAM_WEIGHT, LOBBY_WEIGHT, COMPONENTS, COMBAT_COMPONENTS,
  WEIGHT_FLOOR, DECISIVE_MIN, PRESENCE_FLOOR, INTEGRITY_FLOOR, EXPECT_CONFIDENCE_RD,
};
