// Ranked writes use Redis-side compare-and-set. A match receipt is permanent:
// retrying an uncertain HTTP response must never award that match a second time.
const VERSION = 'team-mmr-rr-v2';
const combatStorage = require('./combat-storage.cjs');
const TYPES = `
local function validType(key, wanted)
  local t = redis.call('TYPE', key).ok
  return t == 'none' or t == wanted
end
`;
const WRITE = `-- rank-write-v1
${TYPES}
if not validType(KEYS[1], 'string') or not validType(KEYS[2], 'zset') then return {'invalid-type'} end
if KEYS[3] and not validType(KEYS[3], 'string') then return {'invalid-type'} end
if KEYS[4] and (not validType(KEYS[4], 'string') or not validType(KEYS[5], 'zset')) then return {'invalid-type'} end
local row = cjson.decode(ARGV[1])[1]
if KEYS[4] and (type(row.audit)~='string' or type(row.audit_at)~='number') then return {'invalid-audit'} end
local audit = row.audit and cjson.decode(row.audit) or nil
local current = redis.call('GET', KEYS[1])
if KEYS[3] and redis.call('GET', KEYS[3]) then return {'written', current} end
if current == row.json then
  local auditJson=nil
  if KEYS[4] then audit.before=cjson.decode(current);audit.after=cjson.decode(current);audit.noop=true;auditJson=cjson.encode(audit) end
  if KEYS[3] then redis.call('SET', KEYS[3], row.json) end
  if KEYS[4] then redis.call('SET',KEYS[4],auditJson);redis.call('ZADD',KEYS[5],row.audit_at,KEYS[4]) end
  return {'written', current}
end
local revision = 0
if current then revision = cjson.decode(current).revision or 0 end
if revision ~= row.expected then return {'conflict'} end
redis.call('SET', KEYS[1], row.json)
if row.placing then redis.call('ZREM', KEYS[2], row.id)
else redis.call('ZADD', KEYS[2], row.progress, row.id) end
if KEYS[3] then redis.call('SET', KEYS[3], row.json) end
if KEYS[4] then redis.call('SET',KEYS[4],row.audit);redis.call('ZADD',KEYS[5],row.audit_at,KEYS[4]) end
return {'written', row.json}
`;
const COMMIT = `-- rank-settlement-v1
${TYPES}
if not validType(KEYS[1], 'string') then return {'invalid-type'} end
local receipt = redis.call('GET', KEYS[1])
if receipt then return {'replayed', receipt} end
if not validType(KEYS[2], 'zset') or not validType(KEYS[3], 'string') or not validType(KEYS[4], 'set') then return {'invalid-type'} end
local rows = cjson.decode(ARGV[3])
local live = redis.call('GET', KEYS[3])
if live and (cjson.decode(live).void_pending or cjson.decode(live).terminal) then return {'void-decision'} end
local queueIndex = #rows + 5
local authorityKey=KEYS[queueIndex+1]
if authorityKey then
  local raw=redis.call('GET',authorityKey)
  local expected=cjson.decode(ARGV[2])
  if raw then
    local a=cjson.decode(raw)
    if a.closed or a.epoch~=(expected.host_epoch or 0) or a.host~=expected.host then return {'authority'} end
  elseif (expected.host_epoch or 0)>0 then return {'authority'} end
end
if KEYS[queueIndex] and not validType(KEYS[queueIndex], 'set') then return {'invalid-type'} end
for i, row in ipairs(rows) do
  if not validType(KEYS[4+i], 'string') then return {'invalid-type'} end
  local current = redis.call('GET', KEYS[4+i])
  local revision = 0
  if current then revision = cjson.decode(current).revision or 0 end
  if revision ~= row.expected then return {'conflict', row.id} end
end
-- All validation precedes the first write: Redis scripts do not roll back a
-- runtime error, so wrong types must not leave a half-applied result.
for i, row in ipairs(rows) do
  redis.call('SET', KEYS[4+i], row.json)
  if row.placing then redis.call('ZREM', KEYS[2], row.id)
  else redis.call('ZADD', KEYS[2], row.progress, row.id) end
end
redis.call('SET', KEYS[1], ARGV[2])
if KEYS[queueIndex] then redis.call('SADD', KEYS[queueIndex], KEYS[1]) end
redis.call('DEL', KEYS[3])
redis.call('SREM', KEYS[4], ARGV[1])
return {'committed', ARGV[2]}
`;
const SNAPSHOT = `-- rank-snapshot-v1
${TYPES}
if not validType(KEYS[1], 'string') or not validType(KEYS[2], 'string') or not validType(KEYS[3], 'set') then return {'invalid-type'} end
local receipt = redis.call('GET', KEYS[1])
if receipt then
  redis.call('DEL', KEYS[2])
  redis.call('SREM', KEYS[3], ARGV[1])
  return {'settled', receipt}
end
local previous = redis.call('GET', KEYS[2])
local incoming = cjson.decode(ARGV[2])
local initialAuthority=nil
if KEYS[4] then
  if not validType(KEYS[4],'string') then return {'invalid-type'} end
  local authority=redis.call('GET',KEYS[4])
  if authority then
    local a=cjson.decode(authority)
    if a.closed or a.epoch~=(incoming.host_epoch or 0) or a.host~=incoming.host then return {'authority'} end
  elseif (incoming.host_epoch or 0)>0 then return {'authority'}
  elseif incoming.migration_digests then
    initialAuthority=cjson.encode({host=incoming.host,epoch=0,digest=incoming.migration_digests[incoming.host],candidate='',last_seen=0})
  end
end
if previous then
  local saved = cjson.decode(previous)
  local incoming = cjson.decode(ARGV[2])
  if saved.void_pending then return {'pending', previous} end
  if saved.terminal then return {'pending', previous} end
  if saved.collecting and (not incoming.collecting or
      saved.collecting.winner ~= incoming.collecting.winner or
      saved.collecting.limit ~= incoming.collecting.limit or
      saved.collecting.score['1'] ~= incoming.collecting.score['1'] or
      saved.collecting.score['2'] ~= incoming.collecting.score['2']) then
    return {'pending', previous}
  end
end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
if initialAuthority then redis.call('SET',KEYS[4],initialAuthority,'EX',ARGV[3]) end
redis.call('SADD', KEYS[3], ARGV[1])
return {'saved'}
`;

async function snapshot(store, keys, id, json, ttl) {
  const packed = await combatStorage.pack(store, JSON.parse(json), keys[1], {ttl: Number(ttl) + 86400});
  json = JSON.stringify(packed);
  const out = await store(['EVAL', SNAPSHOT, String(keys.length), ...keys, id, json, String(ttl)], { strict: true });
  if (!Array.isArray(out) || !['saved', 'settled', 'pending'].includes(out[0])) throw new Error('Live match not saved');
  if (out[0] === 'pending') return { pendingMatch: await combatStorage.unpack(store, JSON.parse(out[1]), keys[1]) };
  return out[0] === 'settled' ? combatStorage.unpack(store, JSON.parse(out[1]), keys[0]) : null;
}

async function write(store, keys, row) {
  const out = await store(['EVAL', WRITE, String(keys.length), ...keys, JSON.stringify([row])], { strict: true });
  if (!Array.isArray(out) || out[0] !== 'written') {
    const error = new Error('Rank write not committed');
    error.conflict = Array.isArray(out) && out[0] === 'conflict';
    throw error;
  }
  return out[1] ? JSON.parse(out[1]) : null;
}
async function commit(store, keys, receipt, rows) {
  const packed = await combatStorage.pack(store, receipt, keys[0]);
  const out = await store(['EVAL', COMMIT, String(keys.length), ...keys,
    receipt.matchId, JSON.stringify(packed), JSON.stringify(rows)], { strict: true });
  if (!Array.isArray(out) || !['committed', 'replayed'].includes(out[0])) {
    const error = new Error('Match settlement not committed');
    error.conflict = Array.isArray(out) && out[0] === 'conflict';
    throw error;
  }
  return { replayed: out[0] === 'replayed', receipt: await combatStorage.unpack(store, JSON.parse(out[1]), keys[0]) };
}
module.exports = { VERSION, WRITE, COMMIT, SNAPSHOT, write, commit, snapshot };
