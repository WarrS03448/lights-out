'use strict';
// Host authority is separate from the periodically saved game snapshot. A stale
// worker must never restore an old host or settle a result after a handoff.
const {createHash}=require('node:crypto');
const digest=token=>createHash('sha256').update(token).digest('hex');
const SCRIPT=`-- host-migration-v1
local authority=redis.call('GET',KEYS[1])
local raw=redis.call('GET',KEYS[2])
if not authority or not raw or redis.call('GET',KEYS[3]) then return {'closed'} end
local a=cjson.decode(authority)
if a.closed or a.phase=='restoring' then return {'closed'} end
local m=cjson.decode(raw)
local op,who,token,epoch,now,candidate=ARGV[1],ARGV[2],ARGV[3],tonumber(ARGV[4]),tonumber(ARGV[5]),ARGV[6]
if m.state~='live' or not m.start_ready_verified or m.final_snapshot then return {'closed'} end
if not m.migration_digests or m.migration_digests[who]~=token then return {'credential'} end
local member=false
local successor=false
for _,p in ipairs(m.players) do
  if p.player_id==who then member=true end
  if p.player_id==candidate then successor=true end
end
if not member then return {'member'} end
if op=='endorse' then
  if a.host~=who or a.epoch~=epoch or a.digest~=token then return {'authority'} end
  if candidate~='' and (not successor or candidate==who) then return {'candidate'} end
  a.candidate=candidate
  a.last_seen=now
  redis.call('SET',KEYS[1],cjson.encode(a),'EX',ARGV[7])
  return {'endorsed'}
end
if op~='activate' then return {'operation'} end
if a.host==who and a.epoch==epoch and a.digest==token then return {'replayed',raw} end
if epoch~=a.epoch+1 or a.candidate~=who then return {'candidate'} end
if now-a.last_seen<10000 or now-a.last_seen>300000 then return {'lease'} end
if (m.host_epoch or 0)~=a.epoch or m.host~=a.host then return {'snapshot'} end
if raw~=ARGV[8] or authority~=ARGV[10] then return {'conflict'} end
local saved=ARGV[9]
local nextAuthority=cjson.encode({host=who,epoch=epoch,digest=token,candidate='',last_seen=now,roster_revision=m.roster_revision or 0})
redis.call('SET',KEYS[2],saved,'EX',ARGV[7])
redis.call('SET',KEYS[1],nextAuthority,'EX',ARGV[7])
return {'activated',saved}
`;
async function transition(store,keys,{operation,player,token,epoch,candidate='',now=Date.now(),ttl=86400}) {
  for(let attempt=0;attempt<4;attempt++) {
  let raw='',proposal='',authorityRaw='';
  if(operation==='activate') {
    raw=await store(['GET',keys[1]],{strict:true});
    if(!raw)return {ok:false,error:'Match is no longer live.'};
    const storage=require('./combat-storage.cjs');
    const m=await storage.unpack(store,JSON.parse(raw),keys[1]);
    authorityRaw=await store(['GET',keys[0]],{strict:true})||'';
    const authority=JSON.parse(authorityRaw||'null');
    if(!authority)return {ok:false,error:'Host authority is unavailable.'};
    const previous=m.host;
    m.host=player;m.host_epoch=epoch;m.reportToken=m.migration_capabilities?.[player];m.legacyReportAuth=false;
    m.reconnect ||= {};
    m.reconnect[previous] ||= {since:authority.last_seen,deadline:authority.last_seen+300000};
    (m.host_migrations ||= []).push({epoch,previous,host:player,at:now});
    m.combat_segments ||= [];
    if(m.combatState) {m.combatState.coverage.broken=true;m.combat_segments.push(m.combatState);}
    delete m.combatState;delete m.combat_end;m.combat_migrated=true;
    proposal=JSON.stringify(await storage.pack(store,m,keys[1],{ttl:ttl+86400}));
  }
  const reply=await store(['EVAL',SCRIPT,String(keys.length),...keys,operation,player,digest(token),String(epoch),String(now),candidate,String(ttl),raw,proposal,authorityRaw],{strict:true});
  if(reply?.[0]==='conflict')continue;
  if(!Array.isArray(reply)||!['endorsed','activated','replayed'].includes(reply[0]))
    return {ok:false,error:'Host handoff is not ready.'};
  return {ok:true,changed:reply[0]==='activated',snapshot:reply[1]?await require('./combat-storage.cjs').unpack(store,JSON.parse(reply[1]),keys[1]):null};
  }
  return {ok:false,error:'Host handoff is busy.'};
}
const CLOSE=`-- host-close-v1
local raw=redis.call('GET',KEYS[1])
if redis.call('GET',KEYS[2]) then return 0 end
if not raw then return 0 end
local a=cjson.decode(raw)
if a.host~=ARGV[1] or a.epoch~=tonumber(ARGV[2]) then return 0 end
if tonumber(ARGV[4])>0 and (a.last_seen==0 or tonumber(ARGV[3])-a.last_seen<tonumber(ARGV[4])) then return 0 end
a.closed=true
redis.call('SET',KEYS[1],cjson.encode(a),'EX',ARGV[5])
return 1
`;
const FORGET=`-- host-forget-v1
local raw=redis.call('GET',KEYS[3])
if raw and not cjson.decode(raw).closed and not redis.call('GET',KEYS[4]) then return 0 end
redis.call('DEL',KEYS[1]);redis.call('SREM',KEYS[2],ARGV[1]);return 1
`;
module.exports={digest,SCRIPT,CLOSE,FORGET,transition};
