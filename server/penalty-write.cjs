'use strict';
const { randomBytes } = require('node:crypto');
const RETENTION_MS = 14 * 86400000;
const MAX_JSON_BYTES = 4096;
const nonnegative = n => typeof n === 'number' && Number.isFinite(n) && n >= 0;

function validRecord(record, legacy = false) {
  return record && typeof record === 'object' && !Array.isArray(record)
    && ((!legacy && record.last === undefined) || (Number.isSafeInteger(record.last) && record.last >= 0))
    && Number.isSafeInteger(record.until) && record.until >= 0
    && (record.count === undefined || (Number.isSafeInteger(record.count) && record.count >= 0))
    && (record.elo === undefined || nonnegative(record.elo))
    && (!legacy || (record.reason === 'no_show' && record.until >= record.last
      && Number.isSafeInteger(record.count) && record.count > 0 && nonnegative(record.elo)));
}

/** Capture once before queueing; retry THIS operation, never regenerate its ID/TTL.
 * The high-water key is one permanent bounded record per player, not an unbounded
 * receipt log. Older writes cannot resurrect a served penalty after its TTL.
 * Equal-time ties favor durable combat; two legacy no-shows can advance only by
 * increasing count without decreasing duration or cumulative cost. Independent
 * same-time no-shows calculated from the same stale count cannot be distinguished
 * safely and conservatively retain the first durable record.
 */
function prepare(record, now = Date.now()) {
  if (!validRecord(record, true) || !Number.isSafeInteger(now) || now < 0) throw new Error('Invalid legacy penalty');
  const json = JSON.stringify(record);
  if (Buffer.byteLength(json, 'utf8') > MAX_JSON_BYTES) throw new Error('Penalty record too large');
  const expiresAt = Math.max(now + 60000, record.until + RETENTION_MS);
  if (!Number.isSafeInteger(expiresAt)) throw new Error('Invalid penalty expiry');
  return Object.freeze({ operationId: randomBytes(16).toString('hex'), json, last: record.last, expiresAt });
}

const WRITE = `-- penalty-write-v1
local function validType(key)
  local t = redis.call('TYPE', key).ok
  return t == 'none' or t == 'string'
end
local function number(n)
  return type(n) == 'number' and n == n and n >= 0 and n <= 9007199254740991
end
local function integer(n) return number(n) and n == math.floor(n) end
local function decode(raw)
  local ok, value = pcall(cjson.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  return value
end
local function validRecord(r)
  return r and (r.last == nil or integer(r.last)) and integer(r['until'])
    and (r.count == nil or integer(r.count)) and (r.elo == nil or number(r.elo))
end
if not validType(KEYS[1]) or not validType(KEYS[2]) then return {'invalid-type'} end
local p = decode(ARGV[1])
if not p or type(p.json) ~= 'string' or #p.json > 4096 or not integer(p.last)
  or not integer(p.expiresAt) or type(p.operationId) ~= 'string'
  or #p.operationId ~= 32 or string.find(p.operationId, '[^a-f0-9]') then return {'invalid-payload'} end
local incoming = decode(p.json)
if not validRecord(incoming) or incoming.last ~= p.last or incoming.reason ~= 'no_show'
  or incoming['until'] < incoming.last or not integer(incoming.count) or incoming.count < 1
  or not number(incoming.elo) then return {'invalid-payload'} end
local currentJson = redis.call('GET', KEYS[1])
local current = currentJson and decode(currentJson) or nil
if currentJson and not validRecord(current) then return {'invalid-current'} end
local guardJson = redis.call('GET', KEYS[2])
local guard = guardJson and decode(guardJson) or nil
local guarded = guard and type(guard.json) == 'string' and decode(guard.json) or nil
if guardJson and (not guard or type(guard.operationId) ~= 'string' or not validRecord(guarded)) then return {'invalid-current'} end
if guard and guard.operationId == p.operationId then
  if guard.json ~= p.json then return {'invalid-operation'} end
  return {'replayed', currentJson or ''}
end
if currentJson == p.json then return {'replayed', currentJson} end
local function mayFollow(prior)
  if not prior then return true end
  local previousLast = prior.last or 0
  if incoming.last > previousLast then return true end
  if incoming.last < previousLast then return false end
  return incoming.reason == 'no_show' and prior.reason == 'no_show'
    and incoming.count > (prior.count or 0) and incoming['until'] >= prior['until']
    and incoming.elo >= (prior.elo or 0)
end
if not mayFollow(current) or not mayFollow(guarded) then return {'stale', currentJson or ''} end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if p.expiresAt <= now then return {'stale', currentJson or ''} end
local ttl = math.ceil((p.expiresAt - now) / 1000)
local nextGuard = cjson.encode({operationId=p.operationId, json=p.json})
-- All type/shape/ordering/expiry checks and JSON work precede the first write.
redis.call('SET', KEYS[2], nextGuard)
redis.call('SET', KEYS[1], p.json, 'EX', ttl)
return {'written', p.json}
`;

async function write(store, penaltyKey, operation) {
  if (typeof penaltyKey !== 'string' || !penaltyKey || !operation || typeof operation.json !== 'string'
    || Buffer.byteLength(operation.json, 'utf8') > MAX_JSON_BYTES
    || !/^[a-f0-9]{32}$/.test(operation.operationId || '')
    || !Number.isSafeInteger(operation.last) || operation.last < 0
    || !Number.isSafeInteger(operation.expiresAt) || operation.expiresAt < 0) throw new Error('Invalid penalty operation');
  const proposed = JSON.parse(operation.json);
  if (!validRecord(proposed, true) || proposed.last !== operation.last) throw new Error('Invalid penalty operation');
  const result = await store(['EVAL', WRITE, '2', penaltyKey, `${penaltyKey}:write`, JSON.stringify(operation)], { strict: true });
  if (!Array.isArray(result) || result.length !== 2 || typeof result[1] !== 'string'
    || !['written', 'replayed', 'stale'].includes(result[0])) throw new Error('Penalty write not committed');
  const record = result[1] ? JSON.parse(result[1]) : null;
  if (record !== null && !validRecord(record)) throw new Error('Invalid saved penalty');
  if (result[0] === 'written' && record === null) throw new Error('Missing committed penalty');
  return { status: result[0], record };
}

module.exports = { RETENTION_MS, MAX_JSON_BYTES, prepare, write, WRITE };
