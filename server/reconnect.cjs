'use strict';
// Presence is Controller/connection presence, never pawn or alive status.
const GRACE_MS = 5 * 60 * 1000;

function observe(match, present, now = Date.now()) {
  if (!match || match.state !== 'live' || !match.start_ready_verified || match.final_snapshot)
    return {ok:false, error:'no confirmed live match'};
  const required = match.players.map(p => p.player_id);
  if (!Array.isArray(present) || !present.length || present.length > 10 ||
      new Set(present).size !== present.length || !present.includes(match.host) ||
      present.some(id => !required.includes(id))) return {ok:false, error:'invalid presence roster'};
  const missing = required.filter(id => !present.includes(id) ||
    (match.reconnect?.[id]?.deadline <= now));
  const windows = {};
  for (const id of missing) {
    const old = match.reconnect?.[id];
    windows[id] = old && Number.isSafeInteger(old.since) && old.deadline === old.since + GRACE_MS
      ? {...old} : {since:now, deadline:now + GRACE_MS};
  }
  return {ok:true, windows, expired:missing.filter(id => windows[id].deadline <= now), missing};
}

function confirmedLeft(match, id) {
  return (match.left || []).find(p => p.player_id === id && p.disconnect_confirmed === true && p.left_state === 'live');
}

module.exports = {GRACE_MS, observe, confirmedLeft};
