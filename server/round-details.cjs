'use strict';
const combat = require('./combat.cjs');
const identity = require('./player-identity.cjs');
const number = v => typeof v === 'number' && Number.isFinite(v);
const roundNumber = n => Number.isInteger(n) && n > 0 && n <= 99;
const nativeRound = n => Number.isInteger(n) && n >= 0 && n < 99;

/** Display-only projection. Never edits evidence, settlement or rating inputs. */
function roundDetails(match) {
  const roster = [...new Map([...(match.players || []), ...(match.left || [])]
    .filter(p => identity.playerOf(p)).map(p => [identity.playerOf(p), p])).values()];
  const teamOf = id => [1, 2].find(n => (match.teams?.[n] || []).includes(id)) || 0;
  const series = match.stats?.series || {};
  const feed = Array.isArray(match.kills) ? match.kills : [];
  // Current collectors gate warmup with CompetitiveStarted; native round 0 is
  // the FIRST played round. Translate only in this display projection.
  const events = (match.combatState?.events || []).filter(e => !e.ignored && nativeRound(e.n));
  const reports = { ...(match.round_reports || {}) };
  // Legacy records mixed score steps and numbered snapshots in the same container.
  for (const row of Object.values(match.rounds || {})) {
    if (row && nativeRound(row.round) && !reports[row.round]) reports[row.round] = row;
  }
  const winners = {}, scores = {};
  for (const [native, side] of Object.entries(match.round_results || {})) {
    if (nativeRound(Number(native))) winners[Number(native) + 1] = side;
  }
  for (const [native, gameTeam] of Object.entries(match.round_wins || {})) {
    const n = Number(native) + 1;
    const side = match.team_map?.[gameTeam];
    if (!winners[n] && (side === 1 || side === 2)) winners[n] = side;
  }
  for (const [native, report] of Object.entries(reports)) {
    const n = Number(native) + 1;
    const side = report.source === 'delegate' ? match.team_map?.[report.winTeam] : null;
    if (!winners[n] && (side === 1 || side === 2)) winners[n] = side;
  }
  let previous = [0, 0];
  for (const row of Array.isArray(match.rounds) ? match.rounds : []) {
    if (!row || ![row[1], row[2]].every(v => number(v) && v >= 0)) continue;
    const score = [row[1], row[2]], start = previous[0] + previous[1], end = score[0] + score[1];
    if (!Number.isInteger(end) || end > 99 || score.some((v, i) => v < previous[i])) continue;
    const gains = score.map((v, i) => v - previous[i]);
    if ((gains[0] > 0) !== (gains[1] > 0)) {
      const side = gains[0] ? 1 : 2;
      for (let n = start + 1; n <= end; n++) {
        winners[n] ??= side;
        scores[n] = previous.slice(); scores[n][side - 1] += n - start;
      }
    }
    previous = score;
  }
  const scoreTotal = number(match.score?.[1]) && number(match.score?.[2]) ? match.score[1] + match.score[2] : 0;
  const candidates = [number(match.stats?.rounds) ? match.stats.rounds + 1 : 0, ...Object.keys(winners).map(Number),
    ...Object.keys(reports).map(n => Number(n) + 1), ...events.map(e => e.n + 1), ...feed.map(e => e?.round + 1)];
  for (const samples of Object.values(series)) candidates.push(...Object.keys(samples || {}).map(n => Number(n) + 1));
  const recordedTotal = Math.max(0, ...[match.rounds_played, scoreTotal].filter(roundNumber));
  const total = recordedTotal || Math.max(0, ...candidates.filter(roundNumber));
  let completeScore = true;
  const running = [0, 0], output = [];
  for (let n = 1; n <= total; n++) {
    const won = [1, 2].includes(winners[n]) ? winners[n] : null;
    if (won) running[won - 1]++; else completeScore = false;
    const native = n - 1, report = reports[native];
    const seconds = report?.source === 'delegate' && number(report.seconds) && report.seconds >= 0 ? report.seconds : null;
    const roundFeed = feed.filter(k => k?.round === native && k.killer && k.victim && !k.inferred);
    const state = match.combatState ? { ...match.combatState, events: events.filter(e => e.n === native) } : null;
    const scoreboard = roster.map(p => {
      const id = identity.playerOf(p), game = identity.gameOf(p);
      const samples = series[id] || {}, current = samples[native];
      const prior = n === 1 ? { kills: 0, deaths: 0 } : samples[native - 1];
      const delta = key => current && prior && number(current[key]) && number(prior[key])
        && (key === 'kills' || current[key] >= prior[key]) ? current[key] - prior[key] : null;
      const kills = delta('kills'), deaths = delta('deaths');
      return { player_id: id, ...(game ? {steam_id:game,game_steam_id:game} : {}), team: teamOf(id), reported: kills !== null || deaths !== null,
        kills, deaths,
        team_kills: roundFeed.length ? roundFeed.filter(k => k.teamKill && k.killer === id).length : null,
        ...(state ? { combat: identity.combatSummary(match, combat.summary(state, game, 1)) } : {}) };
    });
    output.push({ n, won, score: n === scoreTotal ? [match.score[1], match.score[2]]
      : completeScore ? running.slice() : scores[n] || null, seconds, scoreboard });
  }
  return output;
}
module.exports = { roundDetails };
