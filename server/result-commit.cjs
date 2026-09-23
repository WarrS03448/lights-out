// One Redis transaction publishes the receipt, final records and absolute ratings together.
// Preflight all types and JSON before writing: Lua runtime errors do not roll back mutations.
const combatStorage = require('./combat-storage.cjs');
const SCRIPT = `
local old = redis.call('GET', KEYS[1])
if old then return old end
local p = cjson.decode(ARGV[1])
for i, expected in ipairs(p.types) do
  local actual = redis.call('TYPE', KEYS[i]).ok
  if actual ~= 'none' and actual ~= expected then return redis.error_reply('result key type mismatch') end
end
local live = redis.call('GET', KEYS[p.live])
if live and cjson.decode(live).void_pending and cjson.decode(p.receipt_json).voided ~= true then
  return redis.error_reply('saved void decision takes precedence')
end
if live and cjson.decode(live).terminal and cjson.decode(p.receipt_json).voided ~= true then
  local decided=cjson.decode(live).terminal
  local result=cjson.decode(p.receipt_json).terminal
  if not result or result.reason~=decided.reason or result.loser~=decided.loser or result.at~=decided.at then
    return redis.error_reply('saved duel decision takes precedence')
  end
end
if p.authority then
  local raw=redis.call('GET',KEYS[p.authority])
  if raw then
    local a=cjson.decode(raw)
    if a.closed or a.epoch~=p.host_epoch or a.host~=p.host then return redis.error_reply('result authority changed') end
  elseif p.host_epoch>0 then return redis.error_reply('result authority missing') end
end
for _, r in ipairs(p.rank_checks or {}) do
  local current = redis.call('GET', KEYS[r.index])
  local revision = 0
  if current then revision = tonumber(cjson.decode(current).revision) or 0 end
  if revision ~= r.expected then return redis.error_reply('result rank conflict') end
end
for _, check in ipairs(p.string_checks or {}) do
  if redis.call('GET',KEYS[check.index]) ~= check.expected then return redis.error_reply('result state conflict') end
end
local histories = {}
for i, h in ipairs(p.histories) do
  local kept = {}
  for _, raw in ipairs(redis.call('LRANGE', KEYS[h.index], 0, p.keep - 1)) do
    local ok, value = pcall(cjson.decode, raw)
    if not ok or type(value) ~= 'table' or value.id ~= p.id then table.insert(kept, raw) end
  end
  histories[i] = kept
end
for _, w in ipairs(p.writes) do
  if w.ttl then redis.call('SET', KEYS[w.index], w.value, 'EX', w.ttl)
  else redis.call('SET', KEYS[w.index], w.value) end
end
for _, b in ipairs(p.board) do
  if b.value == false then redis.call('ZREM', KEYS[b.index], b.member)
  else redis.call('ZADD', KEYS[b.index], b.value, b.member) end
end
for _, h in ipairs(p.hashes or {}) do
  redis.call('HSET', KEYS[h.index], h.field, h.value)
end
for i, h in ipairs(p.histories) do
  redis.call('DEL', KEYS[h.index])
  redis.call('RPUSH', KEYS[h.index], h.value)
  for j, raw in ipairs(histories[i]) do
    if j < p.keep then redis.call('RPUSH', KEYS[h.index], raw) end
  end
  redis.call('EXPIRE', KEYS[h.index], p.history_ttl)
end
redis.call('SET', KEYS[1], p.receipt_json)
if p.analytics_outbox then redis.call('SADD', KEYS[p.analytics_outbox], KEYS[1]) end
redis.call('DEL', KEYS[p.live])
redis.call('SREM', KEYS[p.live_index], p.id)
return p.receipt_json
`;

async function commit(store, keys, plan) {
  if (!store) throw Error('durable result store unavailable');
  const receipt_json = JSON.stringify(await combatStorage.pack(store, plan.receipt, keys[0]));
  const wire = {...plan, receipt_json, writes: [...plan.writes]};
  delete wire.receipt;
  if (plan.result_index) wire.writes.push({index: plan.result_index, value: receipt_json, ttl: plan.ttl});
  const raw = await store(['EVAL', SCRIPT, String(keys.length), ...keys, JSON.stringify(wire)], { strict: true });
  if (typeof raw !== 'string' || !raw) throw Error('result store did not acknowledge commit');
  const saved = await combatStorage.unpack(store, JSON.parse(raw), keys[0]);
  if (saved.match_id !== plan.id || saved.data_collected !== true || !saved.events) throw Error('invalid result receipt');
  return saved;
}
// A delayed profile/directory write from the previous process cannot erase a final result.
const CAREER_SCRIPT = `
local incoming = cjson.decode(ARGV[2])
local raw = redis.call('HGET', KEYS[1], ARGV[1])
if raw then
  local current = cjson.decode(raw)
  if (tonumber(current.result_revision) or 0) > (tonumber(incoming.result_revision) or 0) then return 0 end
end
return redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
`;
module.exports = { commit, SCRIPT, CAREER_SCRIPT };
