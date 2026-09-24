'use strict';
// Friendly-fire evidence is durable across matches. Its revision and any resulting
// penalty, RR change and receipt commit together; an uncertain HTTP reply is safe to retry.
const KEEP_MS = 30 * 86400000;
const KEEP_INCIDENTS = 256;
const crypto = require('node:crypto');

function merge(previous, incoming, now) {
  const state = previous && typeof previous === 'object' ? previous : {};
  const byId = new Map();
  for (const row of state.incidents || []) {
    if (row && typeof row.id === 'string' && Number.isFinite(row.startedAt ?? row.at) && (row.startedAt ?? row.at) >= now - KEEP_MS)
      byId.set(row.id, { ...row });
  }
  for (const row of incoming || []) {
    if (!row || typeof row.id !== 'string' || !Number.isFinite(row.startedAt ?? row.at) || (row.startedAt ?? row.at) < now - KEEP_MS) continue;
    const old = byId.get(row.id);
    let merged = { ...row };
    if (old) {
      const incomingSeqs = new Set(row.eventSeqs || []), savedSeqs = new Set(old.eventSeqs || []);
      const extendsSaved = [...savedSeqs].every(seq => incomingSeqs.has(seq));
      const stale = [...incomingSeqs].every(seq => savedSeqs.has(seq));
      if (savedSeqs.size && stale && !extendsSaved) merged = { ...old };
      else if (savedSeqs.size && !extendsSaved) merged = { ...old, sanctionEligible: false };
      if (old.validated === false || row.validated === false) merged.validated = false;
      if (old.sanctionEligible === false || row.sanctionEligible === false) merged.sanctionEligible = false;
      if (old.sanctionId) { merged.sanctionId = old.sanctionId; merged.sanctionedAt = old.sanctionedAt; }
    }
    byId.set(row.id, merged);
  }
  return { ...state, version: 1, revision: (Number(state.revision) || 0) + 1,
    incidents: [...byId.values()].sort((a, b) => (a.startedAt ?? a.at) - (b.startedAt ?? b.at) || a.id.localeCompare(b.id)).slice(-KEEP_INCIDENTS) };
}

function consume(history, decision, now) {
  const ids = new Set(decision.incidentIds || []);
  return { ...history, incidents: history.incidents.map(row => ids.has(row.id)
    ? { ...row, sanctionId: decision.decisionId, sanctionedAt: now } : { ...row }) };
}

function decode(raw, label) {
  if (raw === null || raw === undefined) return null;
  const out = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (!out || typeof out !== 'object' || Array.isArray(out)) throw new Error(`Invalid ${label}`);
  return out;
}

async function load(store, prefix, id) {
  const raw = await store(['GET', `${prefix}combat:history:${id}`], { strict: true });
  const history = decode(raw, 'combat history') || { version: 1, revision: 0, incidents: [] };
  if (!Array.isArray(history.incidents) || !Number.isSafeInteger(history.revision) || history.revision < 0)
    throw new Error('Invalid combat history');
  return history;
}

const COMMIT = `-- combat-ledger-v1
local function validType(key, wanted)
  local t = redis.call('TYPE', key).ok
  return t == 'none' or t == wanted
end
if not validType(KEYS[1], 'string') then return {'invalid-type'} end
local p = cjson.decode(ARGV[1])
if type(p.historyJson) ~= 'string' or type(p.expected) ~= 'number' then return {'invalid-payload'} end
local proposed = cjson.decode(p.historyJson)
if proposed.revision ~= p.expected + 1 then return {'invalid-payload'} end
if p.receipt then
  if type(p.rankJson) ~= 'string' or type(p.penaltyJson) ~= 'string' or type(p.receiptJson) ~= 'string'
    or type(p.player) ~= 'string' or type(p.progress) ~= 'number' or p.progress ~= p.progress
    or math.abs(p.progress) == math.huge or type(p.expectedRank) ~= 'number'
    or type(p.expectedPenalty) ~= 'string' then return {'invalid-payload'} end
  local nextRank = cjson.decode(p.rankJson)
  cjson.decode(p.penaltyJson)
  cjson.decode(p.receiptJson)
  if nextRank.revision ~= p.expectedRank + 1 then return {'invalid-payload'} end
  if not validType(KEYS[5], 'string') then return {'invalid-type'} end
  local receipt = redis.call('GET', KEYS[5])
  if receipt then return {'replayed', redis.call('GET', KEYS[1]) or '{}', receipt} end
end
local current = redis.call('GET', KEYS[1])
if p.authority then
  local raw=redis.call('GET',KEYS[6])
  if redis.call('GET',KEYS[7]) then return {'authority'} end
  if raw then
    local a=cjson.decode(raw)
    if a.closed or a.phase=='restoring' or a.epoch~=p.authority.epoch or a.host~=p.authority.host then return {'authority'} end
  elseif p.authority.epoch>0 then return {'authority'} end
end
local revision = 0
if current then revision = cjson.decode(current).revision or 0 end
if revision ~= p.expected then return {'conflict'} end
if p.receipt then
  if not validType(KEYS[2], 'string') or not validType(KEYS[3], 'string') or not validType(KEYS[4], 'zset') then return {'invalid-type'} end
  local rank = redis.call('GET', KEYS[2])
  local rankRevision = 0
  if rank then rankRevision = cjson.decode(rank).revision or 0 end
  if rankRevision ~= p.expectedRank then return {'conflict'} end
  local penalty = redis.call('GET', KEYS[3]) or ''
  if penalty ~= p.expectedPenalty then return {'conflict'} end
end
-- All reads, decoding and type/revision checks precede any mutation.
if p.receipt then
  redis.call('SET', KEYS[2], p.rankJson)
  redis.call('SET', KEYS[3], p.penaltyJson)
  if p.placing then redis.call('ZREM', KEYS[4], p.player)
  else redis.call('ZADD', KEYS[4], p.progress, p.player) end
  redis.call('SET', KEYS[5], p.receiptJson)
end
redis.call('SET', KEYS[1], p.historyJson)
return {'committed', p.historyJson, p.receiptJson or ''}
`;

async function commit(store, prefix, id, payload) {
  const receiptId = payload.receipt && payload.receipt.decisionId;
  // Hash the decision, never interpolate caller/evidence-controlled delimiters into a key.
  const suffix = crypto.createHash('sha256').update(String(receiptId || 'none')).digest('hex');
  const keys = [`${prefix}combat:history:${id}`, `${prefix}rating:${id}`, `${prefix}penalty:${id}`,
    `${prefix}leaderboard:rr`, `${prefix}combat:sanction:${id}:${suffix}`];
  if(payload.authority)keys.push(`${prefix}live:authority:${payload.authority.match}`,`${prefix}settlement:${payload.authority.match}`);
  const encoded = { ...payload, player: id, historyJson: JSON.stringify(payload.history),
    ...(payload.receipt ? { receiptJson: JSON.stringify(payload.receipt) } : {}) };
  const out = await store(['EVAL', COMMIT, String(keys.length), ...keys, JSON.stringify(encoded)], { strict: true });
  if (!Array.isArray(out) || !['committed', 'replayed'].includes(out[0])) {
    const error = new Error('Combat evidence not committed');
    error.conflict = Array.isArray(out) && out[0] === 'conflict';
    throw error;
  }
  return { replayed: out[0] === 'replayed', history: decode(out[1], 'combat history'),
    receipt: out[2] ? decode(out[2], 'combat receipt') : null };
}

module.exports = { KEEP_MS, KEEP_INCIDENTS, COMMIT, merge, consume, load, commit };
