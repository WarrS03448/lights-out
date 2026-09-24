'use strict';
// A match/player receipt makes retries (including a lost storage reply) charge once.
const COMMIT = `-- reconnect-penalty-v1
local function istype(k,t)
  local actual=redis.call('TYPE',k).ok
  return actual=='none' or actual==t
end
for i=1,4 do if not istype(KEYS[i],'string') then return {'invalid-type'} end end
if not istype(KEYS[5],'zset') then return {'invalid-type'} end
if not istype(KEYS[6],'string') then return {'invalid-type'} end
local saved=redis.call('GET',KEYS[1])
if saved then return {'replayed',saved} end
if redis.call('EXISTS',KEYS[6])==1 then return {'match-finished'} end
local p=cjson.decode(ARGV[1])
local authority=redis.call('GET',KEYS[7])
if authority then
  local a=cjson.decode(authority)
  if a.closed or a.epoch~=(p.host_epoch or 0) or a.host~=p.host then return {'authority'} end
  if (a.roster_revision or 0)~=(p.roster_revision or 0) then return {'authority'} end
elseif (p.host_epoch or 0)>0 then return {'authority'} end
local receipt=cjson.decode(p.receiptJson)
local rank=cjson.decode(p.rankJson)
local penalty=cjson.decode(p.penaltyJson)
local guard=cjson.decode(p.guardJson)
if type(receipt.player_id)~='string' or receipt.reason~='reconnect_timeout'
 or type(rank.revision)~='number' or rank.revision~=p.expectedRank+1
 or type(penalty['until'])~='number' or type(guard.operationId)~='string'
 or guard.json~=p.penaltyJson or type(p.expectedPenalty)~='string'
 or type(p.progress)~='number' or p.progress~=p.progress or math.abs(p.progress)==math.huge
 or type(p.placing)~='boolean' or type(p.ttl)~='number' or p.ttl<86400
 or p.ttl~=math.floor(p.ttl) then return {'invalid-payload'} end
local old=redis.call('GET',KEYS[2])
local rev=old and (cjson.decode(old).revision or 0) or 0
if rev~=p.expectedRank or (redis.call('GET',KEYS[3]) or '')~=p.expectedPenalty then return {'conflict'} end
-- The receipt, cooldown, RR and leaderboard move together or not at all.
redis.call('SET',KEYS[2],p.rankJson)
redis.call('SET',KEYS[3],p.penaltyJson,'EX',p.ttl)
redis.call('SET',KEYS[4],p.guardJson)
if p.placing then redis.call('ZREM',KEYS[5],receipt.player_id)
else redis.call('ZADD',KEYS[5],p.progress,receipt.player_id) end
redis.call('SET',KEYS[1],p.receiptJson,'EX',p.ttl)
return {'committed',p.receiptJson}
`;
async function commit(store, prefix, match, player, payload) {
  const keys=[`${prefix}reconnect:penalty:${match}:${player}`,`${prefix}rating:${player}`,
    `${prefix}penalty:${player}`,`${prefix}penalty:${player}:write`,`${prefix}leaderboard:rr`,`${prefix}settlement:${match}`,`${prefix}live:authority:${match}`];
  const out=await store(['EVAL',COMMIT,String(keys.length),...keys,JSON.stringify(payload)],{strict:true});
  if(!Array.isArray(out)||!['committed','replayed'].includes(out[0])) {
    const e=Error('Reconnect penalty not committed'); e.conflict=out?.[0]==='conflict'; throw e;
  }
  return {replayed:out[0]==='replayed',receipt:JSON.parse(out[1])};
}
module.exports={COMMIT,commit};
