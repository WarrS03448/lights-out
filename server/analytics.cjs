'use strict';
const crypto=require('node:crypto');
const metrics=require('./analytics-metrics.cjs');
const storage=require('./analytics-store.cjs');
const combatStorage=require('./combat-storage.cjs');
const {DAY,projectReceipt}=metrics;
const CLIENT=/^(app|session|ui|request|connection|operation|update|launch|telemetry|auth)\.[a-z0-9_.]{1,50}$/;
const CLIENT_TYPES=new Set('app.action app.error app.fallback session.start session.end session.previous_unclean ui.action ui.screen request.outcome connection.start connection.state connection.connected connection.disconnected operation.start operation.outcome operation.complete operation.failed update.start update.downloaded update.launched update.failed launch.request launch.outcome telemetry.gap auth.signout auth.sign_out'.split(' '));
const KEYS=new Set('action status duration_ms code screen reason connected count gap error_class phase method route bytes attempt quality queue_players wait_seconds host target_id match_size team round winner score1 score2 delay_ms before after mmr rr prefix persisted category batch_count rejected duplicates'.split(' '));
const SID={test:require('./player-identity.cjs').validPlayer};
function cleanEvent(raw,context={},now=Date.now()){
  if(!raw||typeof raw!=='object'||Array.isArray(raw)||typeof raw.id!=='string'||!/^[A-Za-z0-9_.:-]{1,96}$/.test(raw.id))return null;
  if(typeof raw.type!=='string'||!(/^[a-z][a-z0-9_.]{1,63}$/).test(raw.type)||(context.source==='client'&&(!CLIENT.test(raw.type)||!CLIENT_TYPES.has(raw.type))))return null;
  if(context.source==='client'&&[raw.id,raw.session_id].some(v=>typeof v==='string'&&/bearer|token|password|secret|sk[-_]|eyJ/i.test(v)))return null;
  const at=raw.at;
  if(typeof at!=='number'||!Number.isFinite(at)||at>now+300000||at<now-30*DAY)return null;
  const data={};
  for(const[k,v]of Object.entries(raw.data||{})){
    if(!KEYS.has(k))continue;
    if(typeof v==='number'&&Number.isFinite(v)&&Math.abs(v)<=1e12)data[k]=v;
    else if(typeof v==='boolean')data[k]=v;
    else if(typeof v==='string'&&/^[A-Za-z0-9_. -]{1,96}$/.test(v)&&!/(?:bearer|token|password|secret|sk[-_]|eyJ)/i.test(v)&&
      (context.source!=='client'||/^[a-z][a-z0-9_.-]{0,95}$/.test(v)||k==='error_class'&&['TimeoutError','ConnectionError','OSError','RuntimeError','ValueError'].includes(v)))data[k]=v;
  }
  const out={schema:1,id:raw.id,type:raw.type,at,received_at:now,source:context.source||'server',
    severity:['info','warn','error'].includes(raw.severity)?raw.severity:'info',actor_id:SID.test(context.actor_id)?context.actor_id:null,
    session_id:typeof raw.session_id==='string'&&/^[A-Za-z0-9-]{1,64}$/.test(raw.session_id)?raw.session_id:null,
    match_id:context.source!=='client'&&/^[A-Za-z0-9_.:-]{1,80}$/.test(raw.match_id||'')?raw.match_id:null,
    version:typeof raw.version==='string'&&(context.source==='client'?/^\d{1,3}\.\d{1,3}\.\d{1,3}$/:/^[A-Za-z0-9_.-]{1,64}$/).test(raw.version)?raw.version:null,
    request_id:typeof context.request_id==='string'&&/^[a-f0-9-]{36}$/.test(context.request_id)?context.request_id:null,data};
  return out;
}
function filters(raw={},now=Date.now(),permanent=false){
  const parse=(v,fallback)=>{if(v===undefined||v==='')return fallback;const n=typeof v==='number'?v:Date.parse(v);if(!Number.isFinite(n))throw Error('Invalid date range');return n;};
  // Daily aggregates and drill-downs use the same inclusive UTC days.
  const to=Math.min(now,parse(raw.to,now)+(/^\d{4}-\d\d-\d\d$/.test(String(raw.to))?DAY-1:0));
  const from=Math.floor(parse(raw.from,to-6*DAY)/DAY)*DAY;
  if(from>to||from<(permanent?0:now-1096*DAY))throw Error(permanent?'Invalid date range':'Date range must be within three years');
  const size=raw.size==='all'?'all':Number(raw.size||10);
  if(size!=='all'&&(!Number.isInteger(size)||size<1||size>64))throw Error('Invalid match size');
  return {from,to,size,map:String(raw.map||'').slice(0,96),version:String(raw.version||'').slice(0,96),rules_id:String(raw.rules_id||'').slice(0,64),region:String(raw.region||'').slice(0,40),mode:String(raw.mode||'').slice(0,40),
    q:String(raw.q||'').trim().slice(0,96),category:String(raw.category||'').slice(0,32),severity:String(raw.severity||'').slice(0,8),
    actor_id:String(raw.actor_id||'').slice(0,36),match_id:String(raw.match_id||'').slice(0,80),
    offset:Math.min(1e7,Math.max(0,Math.floor(Number(raw.offset)||0))),limit:Math.min(200,Math.max(1,Math.floor(Number(raw.limit)||50)))};
}
function csvCell(value){let s=value==null?'':String(value);if(/^[\s]*[=+@-]/.test(s)||/^[\t\r\n]/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';}
function create({store,prefix='hub:',now=Date.now,resetAt=Number(process.env.HUB_GAME_DATA_RESET_AT||0)}={}){
  if(!Number.isSafeInteger(resetAt)||resetAt<0)throw Error('Invalid game data reset timestamp');
  const db=storage.create({store,prefix,now}),pending=[],terminals=new Map(),health={dropped:0,write_failures:0,rejected:0,last_error:null};
  let flushing=null,working=null,stopped=false,outboxCursor='0';
  function terminal(receipt){if(terminals.size>=100){health.dropped++;return;}terminals.set(receipt.matchId,receipt);void saveTerminals();}
  let saving=null;
  async function saveTerminals(){
    if(saving)return saving;
    saving=(async()=>{for(const[id,r]of terminals){
      if(store){const key=db.base+'terminal:'+id;const packed=await combatStorage.pack(store,r,key,{ttl:31622400});await db.call(['EVAL',`local t=redis.call('TYPE',KEYS[1]).ok;local q=redis.call('TYPE',KEYS[2]).ok;if (t~='none' and t~='string') or (q~='none' and q~='set') then return redis.error_reply('analytics key type') end;redis.call('SET',KEYS[1],ARGV[1],'EX',31536000,'NX');redis.call('SADD',KEYS[2],KEYS[1]);return 1`,'2',key,db.base+'outbox',JSON.stringify(packed)]);}
      else await project(r);
      terminals.delete(id);
    }})().catch(()=>{health.write_failures++;health.last_error='Terminal evidence is waiting for storage';}).finally(()=>{saving=null;});return saving;
  }
  function emit(type,data={},context={}){
    const e=cleanEvent({id:crypto.randomUUID(),type,at:now(),severity:context.severity||'info',match_id:context.match_id,version:context.version||process.env.RAILWAY_GIT_COMMIT_SHA,data},context,now());
    if(!e)return false;
    if(pending.length>=1000){health.dropped++;return false;}
    pending.push(e);return true;
  }
  async function ingest(raw,context){
    if(!Array.isArray(raw)||!raw.length||raw.length>40)throw Error('Expected 1 to 40 events');
    const events=raw.map(e=>cleanEvent(e,context,now()));
    if(events.some(e=>!e)){health.rejected++;throw Error('Invalid telemetry event');}
    // Acknowledge old desktop outboxes so they drain without reviving pre-release diagnostics.
    const current=events.filter(e=>e.at>=resetAt),expired=events.filter(e=>e.at<resetAt);
    const accepted=current.length?await db.writeEvents(current):[];
    return [...accepted,...expired.map(e=>e.id)];
  }
  async function flush(){
    if(flushing)return flushing;
    flushing=(async()=>{
      const chunk=pending.slice(0,40);if(!chunk.length)return;
      try {await db.writeEvents(chunk);pending.splice(0,chunk.length);health.diagnostic_error=null;}
      catch{health.write_failures++;health.diagnostic_error='Diagnostic storage unavailable';}
    })().finally(()=>{flushing=null;});return flushing;
  }
  // The retry identity is the immutable receipt, independent of later UI projections.
  // Reducer/schema changes require an explicit aggregate migration, not replay into v1 totals.
  async function project(receipt,owner=prefix+'settlement:'+(receipt.matchId||receipt.match_id)){
    receipt=await combatStorage.unpack(store,receipt,owner);
    return db.project(projectReceipt(receipt),metrics.hash(receipt));
  }
  async function query(kind,raw={}){
    if(!['events','matches','audits'].includes(kind))throw Error('Unknown analytics collection');
    const f=filters(raw,now(),kind==='audits'),rows=[];let offset=f.offset,exhausted=false,scanned=0;
    while(rows.length<f.limit&&!exhausted&&scanned<2000){
      const batch=await db.page(kind,{...f,offset,scan:Math.min(200,2000-scanned)});
      exhausted=batch.exhausted;
      for(let i=0;i<batch.rows.length;i++){
        const row=batch.rows[i];offset++;scanned++;
        if(!row)continue;
        let ok;
        if(kind==='audits')ok=(!f.q||[row.actor_id,row.target_id,row.action,row.id].join(' ').includes(f.q));
        else if(kind==='matches')ok=metrics.matches(row,f)&&(!f.actor_id||row.players.some(p=>(p.player_id||p.steam_id)===f.actor_id))&&(!f.match_id||row.id===f.match_id)&&(!f.q||[row.id,row.map,...row.players.flatMap(p=>[p.steam_id,p.persona])].join(' ').toLowerCase().includes(f.q.toLowerCase()));
        else ok=(!f.actor_id||row.actor_id===f.actor_id)&&(!f.match_id||row.match_id===f.match_id)&&(!f.category||row.type.startsWith(f.category+'.'))&&(!f.severity||(f.severity==='problems'?['warn','error'].includes(row.severity):row.severity===f.severity))&&(!f.version||row.version===f.version)&&(!f.q||JSON.stringify(row).toLowerCase().includes(f.q.toLowerCase()));
        if(ok)rows.push(row);
        if(rows.length>=f.limit){exhausted=exhausted&&i===batch.rows.length-1;break;}
      }
    }
    return {ok:true,rows,filters:f,next:exhausted?null:offset,scanned,complete:exhausted,persisted:db.persisted,retention:db.retention};
  }
  async function detail(id){
    if(!/^[A-Za-z0-9_.:-]{1,80}$/.test(id))throw Error('Invalid match ID');
    let row=await db.detail(id);
    if(!row&&store){const key=prefix+'settlement:'+id,receipt=await db.call(['GET',key]);if(receipt)row=projectReceipt(await combatStorage.unpack(store,JSON.parse(receipt),key));}
    return row;
  }
  async function combat(id,offset=0){
    if(!/^[A-Za-z0-9_.:-]{1,80}$/.test(id))throw Error('Invalid match ID');
    const start=Math.max(0,Math.floor(Number(offset)||0));
    if(!store)return {rows:[],next:null,available:false};
    const raw=await db.call(['GET',prefix+'settlement:'+id]);
    const events=raw?(await combatStorage.unpack(store,JSON.parse(raw),prefix+'settlement:'+id))?.inputs?.combatState?.events||[]:[];
    return {rows:metrics.evidence(events.slice(start,start+100)),total:events.length,next:start+100<events.length?start+100:null,available:Boolean(raw)};
  }
  async function summary(raw={}){
    const f=filters(raw,now()),span=Math.floor(f.to/DAY)*DAY+DAY-f.from;
    const previous={...f,to:f.from-1,from:f.from-span};
    const all=await db.buckets(Math.max(now()-1095*DAY,previous.from),f.to);
    const current=metrics.aggregate(all.filter(b=>Date.parse(b.day)>=f.from),f);
    return {ok:true,...current,previous:metrics.aggregate(all.filter(b=>Date.parse(b.day)<f.from),previous).totals,
      filters:f,persisted:db.persisted,retention:db.retention,previous_complete:previous.from>=now()-1095*DAY,
      options:{maps:[...new Set(all.map(b=>b.cohort.map))].sort(),versions:[...new Set(all.map(b=>b.cohort.version))].sort()},
      granularity:'UTC day',coverage_note:'Only retained canonical evidence is included. Unknown statistics are excluded from metric denominators. Small/test matches are excluded unless selected.'};
  }
  async function maintenance(){
    if(!store||working||stopped)return working;
    working=(async()=>{
      let failed=false;
      async function consume(key){try{const raw=await db.call(['GET',key]);if(!raw)throw Error('Missing receipt');await project(JSON.parse(raw),key);await db.call(['SREM',db.base+'outbox',key]);}catch{failed=true;health.write_failures++;health.last_error='Some match evidence is waiting for storage or repair';}}
      await saveTerminals();
      await db.call(['ZREMRANGEBYSCORE',db.base+'events','-inf',String(now()-db.retention.events*DAY)]);
      await db.call(['ZREMRANGEBYSCORE',db.base+'matches','-inf',String(now()-db.retention.matches*DAY)]);
      const out=await db.call(['SSCAN',db.base+'outbox',outboxCursor,'COUNT','4']);
      outboxCursor=String(out[0]);
      for(const key of (out?.[1]||[])){
        if(!String(key).startsWith(prefix+'settlement:')&&!String(key).startsWith(db.base+'terminal:'))continue;
        await consume(key);
      }
      const done=await db.call(['GET',db.base+'backfill_done:v1']);
      if(!done){
        const cursor=await db.call(['GET',db.base+'backfill_cursor'])||'0';
        const result=await db.call(['SCAN',String(cursor),'MATCH',prefix+'settlement:*','COUNT','10']);
        for(const key of result?.[1]||[]){if(String(key).includes(':combat:v1:'))continue;await db.call(['SADD',db.base+'outbox',key]);await consume(key);}
        await db.call(['SET',db.base+'backfill_cursor',String(result[0])]);
        if(String(result[0])==='0')await db.call(['SET',db.base+'backfill_done:v1',String(now())]);
      }
      if(!failed)health.last_error=null;
    })().catch(()=>{health.write_failures++;health.last_error='Storage unavailable or a projection needs repair';}).finally(()=>{working=null;});return working;
  }
  async function status(){return {...await db.status(),...health,last_error:health.last_error||health.diagnostic_error||null,pending:pending.length,pending_terminal:terminals.size,retention:db.retention};}
  async function reliability(raw={}){
    const f=filters(raw,now()),buckets=await db.buckets(f.from,f.to,'reliability'),groups=new Map();
    for(const b of buckets){if(f.version&&b.version!==f.version)continue;const key=b.kind+'|'+b.source+'|'+b.version;const row=groups.get(key)||{kind:b.kind,source:b.source,version:b.version,events:0,errors:0,warnings:0,duration_sum:0,duration_count:0};for(const[k,v]of Object.entries(b.metrics))row[k]=(row[k]||0)+v;groups.set(key,row);}
    return {ok:true,rows:[...groups.values()].sort((a,b)=>b.errors-a.errors||b.events-a.events),filters:f,persisted:db.persisted};
  }
  const timer=setInterval(()=>{void flush();},2000);timer.unref?.();
  const worker=setInterval(()=>{void maintenance();},15000);worker.unref?.();
  async function close(){stopped=true;clearInterval(timer);clearInterval(worker);await saveTerminals();if(working)await working;for(let i=0;i<25&&pending.length;i++){const before=pending.length;await flush();if(pending.length===before)break;}}
  return {emit,ingest,flush,project,terminal,query,detail,combat,summary,reliability,status,maintenance,close,db};
}
module.exports={create,cleanEvent,projectReceipt,filters,csvCell,ruleSnapshot:metrics.ruleSnapshot};
