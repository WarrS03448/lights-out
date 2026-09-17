'use strict';
const {DAY,hash,cohort,contribution}=require('./analytics-metrics.cjs');
const RETENTION={events:30,matches:365,aggregates:1095};
// Preflight all keys before writes. Each batch acknowledges duplicates too, so retries terminate.
const EVENTS=`-- analytics-events-v1
local rows=cjson.decode(ARGV[1])
for _,i in ipairs({2,3,4,5}) do local n=tonumber(ARGV[i]);if not n or n~=n or math.abs(n)>9007199254740991 or n%1~=0 then return redis.error_reply('invalid analytics integer') end end
if #rows ~= #KEYS-3 or not tonumber(ARGV[2]) or tonumber(ARGV[2])<=0 or not tonumber(ARGV[3]) or not tonumber(ARGV[4]) or not tonumber(ARGV[5]) then return redis.error_reply('invalid analytics batch') end
for i,r in ipairs(rows) do
 if type(r.id)~='string' or type(r.key)~='string' or r.key~=KEYS[i+3] or type(r.json)~='string' or type(r.at)~='number' or type(r.group)~='string' or type(r.metric)~='table' then return redis.error_reply('invalid analytics row') end
end
for i,k in ipairs(KEYS) do
 local typ=redis.call('TYPE',k).ok
 local wanted=i==1 and 'zset' or (i<=3 and 'hash' or 'string')
 if typ~='none' and typ~=wanted then return redis.error_reply('analytics key type') end
end
local accepted={}
local totals={}
local groups=redis.call('HLEN',KEYS[3])
for _,field in ipairs({'events','bytes'}) do local v=redis.call('HGET',KEYS[2],field);if v and (not tonumber(v) or tonumber(v)%1~=0 or math.abs(tonumber(v))>9000000000000000) then return redis.error_reply('invalid analytics counter') end end
for _,r in ipairs(rows) do
 if not totals[r.group] and redis.call('HEXISTS',KEYS[3],r.group)==0 then
  if groups>=512 then r.group='overflow';r.kind='telemetry.other';r.source='mixed';r.version='mixed' else groups=groups+1 end
 end
 if not totals[r.group] then local raw=redis.call('HGET',KEYS[3],r.group);totals[r.group]=raw and cjson.decode(raw) or {kind=r.kind,source=r.source,version=r.version,metrics={}} end
 for k,v in pairs(r.metric) do if type(v)~='number' or (totals[r.group].metrics[k] and type(totals[r.group].metrics[k])~='number') then return redis.error_reply('invalid analytics metric') end end
end
for i,r in ipairs(rows) do
 if redis.call('SET',KEYS[i+3],r.json,'EX',ARGV[2],'NX') then
  redis.call('ZADD',KEYS[1],r.at,r.key)
  redis.call('HINCRBY',KEYS[2],'events',1)
  redis.call('HINCRBY',KEYS[2],'bytes',string.len(r.json))
  for k,v in pairs(r.metric) do totals[r.group].metrics[k]=(totals[r.group].metrics[k] or 0)+v end
 end
 table.insert(accepted,r.id)
end
for field,value in pairs(totals) do redis.call('HSET',KEYS[3],field,cjson.encode(value)) end
redis.call('EXPIREAT',KEYS[3],ARGV[5])
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',ARGV[3])
redis.call('HSET',KEYS[2],'last_write',ARGV[4])
return accepted`;
const PROJECT=`-- analytics-project-v1
local types={'string','string','zset','hash','hash','string'}
if #KEYS~=6 then return redis.error_reply('invalid analytics keys') end
for i,k in ipairs(KEYS) do
 local typ=redis.call('TYPE',k).ok
 if typ~='none' and typ~=types[i] then return redis.error_reply('analytics key type') end
end
for _,i in ipairs({2,5,6,7,8,9}) do local n=tonumber(ARGV[i]);if not n or n~=n or math.abs(n)>9007199254740991 or n%1~=0 then return redis.error_reply('invalid analytics integer') end end
if tonumber(ARGV[6])<=0 or tonumber(ARGV[7])<=0 then return redis.error_reply('invalid analytics retention') end
if type(ARGV[10])~='string' or type(ARGV[11])~='string' then return redis.error_reply('invalid analytics identity') end
local existing=redis.call('GET',KEYS[1])
if existing then if existing==ARGV[10] then return 'duplicate' else return redis.error_reply('analytics projection conflict') end end
local bucket=cjson.decode(ARGV[4])
if type(bucket.metrics)~='table' then return redis.error_reply('invalid analytics metrics') end
for k,v in pairs(bucket.metrics) do if type(k)~='string' or type(v)~='number' then return redis.error_reply('invalid analytics metric') end end
local old=redis.call('HGET',KEYS[4],ARGV[3])
if old then
 local decoded=cjson.decode(old)
 for k,v in pairs(bucket.metrics) do decoded.metrics[k]=(decoded.metrics[k] or 0)+v end
 bucket=decoded
end
local encoded=cjson.encode(bucket)
local healthCount=redis.call('HGET',KEYS[5],'matches')
if healthCount and (not tonumber(healthCount) or tonumber(healthCount)%1~=0 or math.abs(tonumber(healthCount))>9000000000000000) then return redis.error_reply('invalid analytics counter') end
redis.call('SET',KEYS[1],ARGV[10],'EX',ARGV[7])
redis.call('SET',KEYS[2],ARGV[1],'EX',ARGV[6])
redis.call('SET',KEYS[6],ARGV[11],'EX',ARGV[6])
redis.call('ZADD',KEYS[3],ARGV[2],KEYS[6])
redis.call('ZREMRANGEBYSCORE',KEYS[3],'-inf',ARGV[8])
redis.call('HSET',KEYS[4],ARGV[3],encoded)
redis.call('EXPIREAT',KEYS[4],ARGV[5])
redis.call('HINCRBY',KEYS[5],'matches',1)
redis.call('HSET',KEYS[5],'last_projection',ARGV[9])
return 'stored'`;
function create({store,prefix='hub:',now=Date.now}={}){
  const base=prefix+'analytics:',memory={events:new Map(),matches:new Map(),audits:new Map(),buckets:new Map(),reliability:new Map()},health={events:0,matches:0,bytes:0};
  const call=async args=>{const value=await store(args,{strict:true});if(value===null&& !['GET','ZSCORE'].includes(args[0]))throw Error('Analytics storage unavailable');return value;};
  async function writeEvents(events){
    if(!events.length)return [];
    const day=new Date(now()).toISOString().slice(0,10);
    const rows=events.map(e=>{
      const metric={events:1,errors:e.severity==='error'?1:0,warnings:e.severity==='warn'?1:0,duration_sum:0,duration_count:0};
      if(typeof e.data.duration_ms==='number'){metric.duration_sum=e.data.duration_ms;metric.duration_count=1;metric['latency_'+([100,250,500,1000,3000,10000].find(n=>e.data.duration_ms<=n)||'over')]=1;}
      return {id:e.id,key:base+'event:'+hash([e.actor_id,e.session_id,e.id]),at:e.received_at,json:JSON.stringify(e),group:hash([e.type,e.source,e.version]),kind:e.type,source:e.source,version:e.version||'unknown',metric};
    });
    if(store)return call(['EVAL',EVENTS,String(rows.length+3),base+'events',base+'health',base+'reliability:'+day,...rows.map(r=>r.key),JSON.stringify(rows),String(RETENTION.events*86400),String(now()-RETENTION.events*DAY),String(now()),String(Math.ceil((Date.parse(day)+(RETENTION.aggregates+1)*DAY)/1000))]);
    for(let i=0;i<rows.length;i++){const r=rows[i];if(!memory.events.has(r.key)){memory.events.set(r.key,events[i]);health.events++;health.bytes+=r.json.length;const bucket=memory.reliability.get(day+r.group)||{day,kind:r.kind,source:r.source,version:r.version,metrics:{}};for(const[k,v]of Object.entries(r.metric))bucket.metrics[k]=(bucket.metrics[k]||0)+v;memory.reliability.set(day+r.group,bucket);}}
    while(memory.events.size>10000)memory.events.delete(memory.events.keys().next().value);
    return events.map(e=>e.id);
  }
  async function project(m,fingerprint=hash(m)){
    if(m.at<now()-RETENTION.aggregates*DAY)return 'expired';
    const c=cohort(m),bucket={day:m.day,cohort:c,metrics:contribution(m)},field=hash(c),key=base+'match:'+m.id;
    const brief={...m};for(const k of ['rules','rounds','round_details','timeline','kills','combat_coverage','stats_series'])delete brief[k];
    brief.players=m.players.map(p=>({steam_id:p.steam_id,persona:p.persona,team:p.team,mmr_delta:p.mmr_delta,rr_delta:p.rr_delta,coverage:p.coverage}));
    if(store)return call(['EVAL',PROJECT,'6',base+'projected:'+m.id,key,base+'matches',base+'day:'+m.day,base+'health',base+'summary:'+m.id,JSON.stringify(m),String(m.at),field,JSON.stringify(bucket),String(Math.ceil((Date.parse(m.day)+ (RETENTION.aggregates+1)*DAY)/1000)),String(Math.max(60,Math.ceil((m.at+RETENTION.matches*DAY-now())/1000))),String((RETENTION.aggregates+2)*86400),String(now()-RETENTION.matches*DAY),String(now()),fingerprint,JSON.stringify(brief)]);
    if(memory.matches.has(m.id))return 'duplicate';
    memory.matches.set(m.id,m);health.matches++;
    const bk=m.day+field,old=memory.buckets.get(bk);
    if(old){for(const[k,v]of Object.entries(bucket.metrics))old.metrics[k]=(old.metrics[k]||0)+v;}else memory.buckets.set(bk,bucket);
    while(memory.matches.size>1000)memory.matches.delete(memory.matches.keys().next().value);
    return 'stored';
  }
  async function page(kind,f){
    if(!store){const rows=[...memory[kind].values()].filter(v=>(kind==='events'?v.received_at:v.at)>=f.from&&(kind==='events'?v.received_at:v.at)<=f.to).sort((a,b)=>(b.received_at||b.at)-(a.received_at||a.at));return {rows:rows.slice(f.offset,f.offset+f.scan),exhausted:rows.length<=f.offset+f.scan};}
    const keys=await call(['ZREVRANGEBYSCORE',base+kind,String(f.to),String(f.from),'LIMIT',String(f.offset),String(f.scan)]);
    const values=keys.length?await call(['MGET',...keys]):[];
    return {rows:values.map(v=>{try{return JSON.parse(v);}catch{return null;}}),exhausted:keys.length<f.scan};
  }
  async function detail(id){if(!store)return memory.matches.get(id)||null;const raw=await call(['GET',base+'match:'+id]);return raw?JSON.parse(raw):null;}
  async function buckets(from,to,kind='day'){
    if(!store)return [...memory[kind==='day'?'buckets':'reliability'].values()].filter(b=>Date.parse(b.day)>=Math.floor(from/DAY)*DAY&&Date.parse(b.day)<=to);
    const days=[];for(let t=Math.floor(from/DAY)*DAY;t<=to;t+=DAY)days.push(new Date(t).toISOString().slice(0,10));
    const out=[];
    for(let i=0;i<days.length;i+=14){const batch=days.slice(i,i+14);const sets=await call(['EVAL',`local out={};for _,k in ipairs(KEYS) do table.insert(out,redis.call('HVALS',k)) end;return out`,String(batch.length),...batch.map(d=>base+kind+':'+d)]);sets.forEach((values,j)=>{for(const raw of values)out.push({day:batch[j],...JSON.parse(raw)});});}
    return out;
  }
  async function status(){
    if(!store)return {...health,persisted:false,outbox:0};
    const raw=await call(['HGETALL',base+'health']);const values=Array.isArray(raw)?Object.fromEntries(raw.reduce((a,v,i)=>{if(i%2===0)a.push([v,raw[i+1]]);return a;},[])):raw||{};
    return {...values,persisted:true,outbox:await call(['SCARD',base+'outbox'])};
  }
  return {writeEvents,project,page,detail,buckets,status,call,base,persisted:Boolean(store),retention:RETENTION};
}
module.exports={create,EVENTS,PROJECT,RETENTION};
