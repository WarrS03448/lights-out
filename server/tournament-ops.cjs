'use strict';
const crypto=require('node:crypto');
const initial=()=>({revision:0,exclusions:{},tickets:[],outages:[],payouts:{},audit:[],announcement:'',finalized:null,confirmations:[]});
const CAS=`-- tournament-operations-v1
local types={'string','hash','set','string','set','set','set','list','hash'}
for i,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;if t~='none' and t~=types[i] then return redis.error_reply('event operations key type') end end
if (redis.call('GET',KEYS[1]) or '')~=ARGV[1] then return 'retry' end
if ARGV[3]~='' then
 if KEYS[6] and redis.call('SCARD',KEYS[6])~=tonumber(ARGV[7]) then return 'retry' end
 if KEYS[7] and redis.call('SCARD',KEYS[7])>0 then return 'evidence_pending' end
 if redis.call('HLEN',KEYS[2])~=tonumber(ARGV[3]) then return 'retry' end
 if redis.call('SCARD',KEYS[3])>0 or not redis.call('GET',KEYS[4]) then return 'evidence_pending' end
 if redis.call('SCARD',KEYS[5])>1000 then return 'evidence_pending' end
 for _,id in ipairs(redis.call('SMEMBERS',KEYS[5])) do
  local raw=redis.call('GET',ARGV[4]..id)
  if raw then local match=cjson.decode(raw);local ended=tonumber(match.final_ended_at or (match.collecting and match.collecting.since));if ended and ended>=tonumber(ARGV[5]) and ended<tonumber(ARGV[6]) then return 'evidence_pending' end end
 end
end
local additions=cjson.decode(ARGV[8]);local entries={}
for _,raw in ipairs(additions) do local entry=cjson.decode(raw);if entry.operation_id and (not entry.actor or not entry.fingerprint or not entry.result) then return redis.error_reply('invalid operation receipt') end;table.insert(entries,entry) end
for i,entry in ipairs(entries) do
 redis.call('RPUSH',KEYS[8],additions[i])
 if entry.operation_id then redis.call('HSET',KEYS[9],entry.actor..':'..entry.operation_id,additions[i]) end
end
redis.call('SET',KEYS[1],ARGV[2]);return 'saved'`;
function create({call,base,prefix,ledger=base,rankedPrefix=prefix,backfillVersion,now}){
 function resultOf(entry,operation){return entry?entry.fingerprint===operation.hash?{ok:true,...entry.result,replayed:true}:{ok:false,error:'operation_conflict'}:null;}
 async function prior(state,actor,operation){if(!operation)return null;const raw=await call(['HGET',base+'operation-receipts',actor+':'+operation.id]);return resultOf(raw?JSON.parse(raw):state.audit.find(a=>a.actor===actor&&a.operation_id===operation.id),operation);}
 async function replay(actor,operation){if(!operation)return null;const raw=await call(['GET',base+'operations']);return prior(raw?JSON.parse(raw):initial(),actor,operation);}
 async function modify(actor,action,fn,guard,operation){
  for(let attempt=0;attempt<5;attempt++){
   const raw=await call(['GET',base+'operations'])||'',state=raw?JSON.parse(raw):initial();
   const previous=await prior(state,actor,operation);if(previous)return previous;
   const result=await fn(state);if(result?.ok===false)return result;if(result?.noop)return {ok:true,...result};
   state.revision++;state.audit.push({id:crypto.randomUUID(),at:now(),actor,action,player_id:result?.player_id||null,reason:result?.reason||'',details:result?.details||null,...(operation?{operation_id:operation.id,fingerprint:operation.hash,result}: {})});
   const additions=(state.audit_archived?state.audit.slice(-1):state.audit).map(a=>JSON.stringify(a));state.audit=state.audit.slice(-100);state.audit_archived=true;
   const saved=await call(['EVAL',CAS,'9',base+'operations',ledger+'matches',prefix+'analytics:outbox',prefix+'analytics:backfill_done:'+backfillVersion,rankedPrefix+'live:matches',ledger+'reverted',rankedPrefix+'cheater:jobs',base+'audit',base+'operation-receipts',raw,JSON.stringify(state),guard?String(guard.count):'',rankedPrefix+'live:match:',String(guard?.start||0),String(guard?.end||0),String(guard?.reverted||0),JSON.stringify(additions)]);
   if(saved==='saved')return {ok:true,...result};if(saved!=='retry')return {ok:false,error:saved};
   if(guard)return {ok:false,error:'state_changed'};
  }
  return {ok:false,error:'state_changed'};
 }
 return {modify,replay};
}
module.exports={create,initial,CAS};
