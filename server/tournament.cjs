// Server-owned event rules and an immutable ledger projected from settlement receipts.
'use strict';
const identity=require('./player-identity.cjs');
const EVENT=Object.freeze({id:'launch-2026',title:'Lights Out 1v1 Launch Tournament',
 start_at:Date.parse('2026-09-26T16:00:00Z'),end_at:Date.parse('2026-09-28T16:00:00Z'),
 timezone:'America/Chicago',currency:'USD',prize_pool:150,prizes:[100,35,15],mode:'BB1',maps:Object.freeze(['Paintball','Airsoft','BombHouse']),
 payout_methods:['Zelle','Venmo','PayPal'],minimum_matches:5,dispute_deadline:Date.parse('2026-09-29T16:00:00Z'),payout_days:7,rules_version:5});
const BACKFILL_VERSION='tournament-launch-2026-bb1-v3';
const ledgerBase=prefix=>prefix+'tournament:'+EVENT.id+':'+EVENT.mode+':';
const eventMode=receipt=>{const m=receipt?.publicMatch||receipt?.full;return !!m&&(receipt.mode||m.mode)===EVENT.mode&&(!receipt.mode||receipt.mode===EVENT.mode)&&(!m.mode||m.mode===EVENT.mode);};
const currentFinal=final=>final?.rules_version===EVENT.rules_version&&final?.mode===EVENT.mode;
const phase=(at=Date.now(),event=EVENT)=>at<event.start_at?'scheduled':at<event.end_at?'live':'ended';
const compareScore=(a,b)=>b.net_rr-a.net_rr||b.wins-a.wins||a.reached_at-b.reached_at;
// Shared places each receive the full prize, including every tie at the last prize place.
const prizeWinners=rows=>rows.filter(p=>p.eligible&&p.rank>=1&&p.rank<=EVENT.prizes.length).map(p=>({...p,prize_usd:EVENT.prizes[p.rank-1]}));
function effectiveEvent(ops={}){
 let end=EVENT.end_at,lastEnd=EVENT.start_at,extension=0;
 for(const outage of [...(ops.outages||[])].sort((a,b)=>a.start-b.start)){
   const from=Math.max(EVENT.start_at,outage.start,lastEnd);
   if(outage.start>=end||outage.end<=from)continue;
   const duration=outage.end-from;extension+=duration;end+=duration;lastEnd=outage.end;
 }
 return {...EVENT,end_at:end,extension_ms:extension,dispute_deadline:end+86400000};
}
const REGISTER=`-- tournament-register-v1
for _,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;if t~='none' and t~='hash' then return redis.error_reply('event key type') end end
local row=cjson.decode(ARGV[1])
local old=redis.call('HGET',KEYS[1],row.player_id)
if old then return {'existing',old} end
local clock=redis.call('TIME');local at=tonumber(clock[1])*1000+math.floor(tonumber(clock[2])/1000)
if at>=tonumber(ARGV[2]) then return {'ended'} end
if redis.call('HEXISTS',KEYS[2],row.game_steam_id)==1 then return {'identity_conflict'} end
if redis.call('HLEN',KEYS[1])>=10000 then return {'capacity'} end
row.registered_at=at
local encoded=cjson.encode(row)
redis.call('HSET',KEYS[1],row.player_id,encoded)
redis.call('HSET',KEYS[2],row.game_steam_id,row.player_id)
return {'registered',encoded}`;
const PROJECT=`-- tournament-project-v1
local t=redis.call('TYPE',KEYS[1]).ok;if t~='none' and t~='hash' then return redis.error_reply('event key type') end
if KEYS[2] then local t=redis.call('TYPE',KEYS[2]).ok;if t~='none' and t~='set' then return redis.error_reply('event correction type') end end
local old=redis.call('HGET',KEYS[1],ARGV[1])
if old and old~=ARGV[2] then return redis.error_reply('event receipt conflict') end
if KEYS[3] and redis.call('EXISTS',KEYS[3])==1 then redis.call('SADD',KEYS[2],ARGV[1]) end
if old then return 'duplicate' end
if redis.call('HLEN',KEYS[1])>=10000 then return redis.error_reply('event ledger capacity') end
redis.call('HSET',KEYS[1],ARGV[1],ARGV[2]);return 'stored'`;
// One bounded Redis snapshot prevents rules, registration and evidence straddling a write.
const READ=`-- tournament-read-v1
for i,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;local wanted=i==3 and 'string' or (i==4 and 'set' or 'hash');if t~='none' and t~=wanted then return redis.error_reply('event key type') end;if i<3 and redis.call('HLEN',k)>10000 then return redis.error_reply('event capacity') end end
return {redis.call('HVALS',KEYS[1]),redis.call('HVALS',KEYS[2]),redis.call('GET',KEYS[3]) or '',KEYS[4] and redis.call('SMEMBERS',KEYS[4]) or {}}`;
function matchEntry(receipt,includeCandidates=false){
 includeCandidates=includeCandidates===true;
 const m=receipt?.publicMatch||receipt?.full,id=receipt?.matchId||receipt?.match_id;
 if(!eventMode(receipt)||receipt.voided||receipt.data_collected!==true||m.outcome!=='played'||m.size!==2||!EVENT.maps.includes(m.map)||
    !Number.isSafeInteger(m.started)||!Number.isSafeInteger(m.ended)||m.ended<EVENT.start_at||(!includeCandidates&&(m.started<EVENT.start_at||m.ended>=EVENT.end_at))||m.ended<m.started||
    typeof id!=='string'||!/^[A-Za-z0-9_.:-]{1,80}$/.test(id)||m.id!==id||m.players?.length!==2||receipt.rows?.length!==2)return null;
 const people=new Map(m.players.map(p=>[p.player_id,p]));
 if(people.size!==2||m.players.filter(p=>p.team===1).length!==1||m.players.filter(p=>p.team===2).length!==1)return null;
 const rows=[];
 for(const r of receipt.rows){
   const p=people.get(r.steamId),delta=r.rr?.delta;
   if(!p||!identity.validPlayer(r.steamId)||!identity.validSteam(p.game_steam_id)||!Number.isSafeInteger(delta))return null;
   rows.push({player_id:r.steamId,game_steam_id:p.game_steam_id,delta,placement:Boolean(r.rr.placing||r.rr.placed||(r.before&&require('./rating.cjs').isPlacing(r.before))),won:!receipt.draw&&r.won===true});
 }
 if(new Set(rows.map(r=>r.player_id)).size!==2||new Set(rows.map(r=>r.game_steam_id)).size!==2)return null;
 return {id,started:m.started,ended:m.ended,rows:rows.sort((a,b)=>a.player_id.localeCompare(b.player_id))};
}
function standings(registrations,matches,event=EVENT,exclusions={},reverted=new Set()){
 const ordered=[...matches].sort((a,b)=>a.ended-b.ended||a.id.localeCompare(b.id)),players=new Map();
 for(const r of registrations)players.set(r.player_id,{...r,net_rr:0,gained_rr:0,lost_rr:0,wins:0,matches:0,reached_at:null,history:[],disqualified:!!exclusions[r.player_id]});
 for(const m of ordered)for(const r of m.rows){
   const p=players.get(r.player_id);if(!p)continue;
   const reason=reverted.has(m.id)?'cheater_reverted':m.reason|| (r.placement?'placement':p.game_steam_id!==r.game_steam_id?'account_mismatch':m.started<p.registered_at?'before_registration':m.started<event.start_at?'before_start':m.ended>=event.end_at?'after_cutoff':null);
   if(reason){p.history.push({match_id:m.id,started:m.started,ended:m.ended,delta:r.delta,counted:false,reason,total:p.net_rr});continue;}
   p.net_rr+=r.delta;p.gained_rr+=Math.max(0,r.delta);p.lost_rr+=Math.max(0,-r.delta);p.wins+=r.won?1:0;p.matches++;
   p.history.push({match_id:m.id,started:m.started,ended:m.ended,delta:r.delta,won:r.won,counted:true,total:p.net_rr});
 }
 const rows=[...players.values()];
 for(const p of rows){p.reached_at=p.history.find(h=>h.counted&&h.total===p.net_rr)?.ended||null;p.eligible=p.matches>=event.minimum_matches&&!p.disqualified;}
 const cmp=compareScore;
 rows.sort((a,b)=>Number(b.eligible)-Number(a.eligible)||cmp(a,b)||a.player_id.localeCompare(b.player_id));
 const eligible=rows.filter(p=>p.eligible);
 eligible.forEach((r,i)=>{r.rank=i&&cmp(r,eligible[i-1])===0?eligible[i-1].rank:i+1;r.tied=Boolean(i&&cmp(r,eligible[i-1])===0||i+1<eligible.length&&cmp(r,eligible[i+1])===0);});
 return rows;
}
function create({store,prefix='hub:',now=Date.now,finalizationHealth,notifySupport}={}){
 const base=prefix+'tournament:'+EVENT.id+':',ledger=ledgerBase(prefix),rankedPrefix=require('./ranked-modes.cjs').rankedPrefix(prefix,EVENT.mode);let cache=null,pending=null;
 async function call(args){if(!store)throw Error('Event storage unavailable');return store(args,{strict:true,timeout:5000});}
 const operations=require('./tournament-ops.cjs').create({call,base,prefix,ledger,rankedPrefix,backfillVersion:BACKFILL_VERSION+'-'+EVENT.mode,now});
 async function modify(actor,action,fn,guard,operation){const result=await operations.modify(actor,action,fn,guard,operation);cache=null;return result;}
 let flushing=null,deliveryCursor='';
 async function flushNotifications(){
   if(!notifySupport)return;if(flushing)return flushing;
   flushing=(async()=>{const raw=await call(['GET',base+'operations']),ops=raw?JSON.parse(raw):{};
     const pending=(ops.tickets||[]).flatMap(ticket=>(ticket.replies||[]).filter(reply=>reply.id&&!reply.notified).map(reply=>({ticket,reply})));
     const offset=pending.findIndex(row=>row.reply.id===deliveryCursor)+1;
     for(let n=0;n<Math.min(10,pending.length);n++){const {ticket,reply}=pending[(offset+n)%pending.length];deliveryCursor=reply.id;
       try{const sent=await notifySupport(reply.by,{target:ticket.player_id,text:reply.message,client_id:reply.id},{event_id:EVENT.id,ticket_id:ticket.id});if(!sent?.ok)continue;
         await modify(reply.by,'support_delivery',current=>{const found=current.tickets.find(t=>t.id===ticket.id)?.replies.find(r=>r.id===reply.id);if(!found||found.notified)return {noop:true};found.notified=now();return {player_id:ticket.player_id,details:{ticket_id:ticket.id,reply_id:reply.id}};});
       }catch{/* The saved reply stays pending for the next poll or process restart. */}
     }
   })().finally(()=>{flushing=null;});return flushing;
 }
 async function register(account){
   if(!identity.validPlayer(account?.player_id)||!identity.validSteam(account?.game_steam_id))return {ok:false,error:'identity_required'};
   const row={player_id:account.player_id,game_steam_id:account.game_steam_id,persona:String(account.persona||'Player').slice(0,80),rules_version:EVENT.rules_version};
   const opsRaw=await call(['GET',base+'operations']),event=effectiveEvent(opsRaw?JSON.parse(opsRaw):{});
   const result=await call(['EVAL',REGISTER,'2',base+'registrations',base+'identities',JSON.stringify(row),String(event.end_at)]);
   if(!Array.isArray(result))throw Error('Invalid event registration response');
   if(!['registered','existing'].includes(result[0]))return {ok:false,error:result[0]};
   const saved=JSON.parse(result[1]);
   if(saved.game_steam_id!==account.game_steam_id)return {ok:false,error:'identity_conflict'};
   cache=null;return {ok:true,registered_at:saved.registered_at};
 }
 async function project(receipt){
   if(!eventMode(receipt))return;
   let entry=matchEntry(receipt,true);
   if(!entry){
     const m=receipt?.publicMatch||receipt?.full,id=receipt?.matchId||receipt?.match_id;
     if(!m||!Number.isSafeInteger(m.ended)||m.ended<EVENT.start_at||typeof id!=='string'||!/^[A-Za-z0-9_.:-]{1,80}$/.test(id)||!Array.isArray(m.players)||m.players.length>20)return;
     const rows=m.players.filter(p=>identity.validPlayer(p.player_id)&&identity.validSteam(p.game_steam_id)).map(p=>({player_id:p.player_id,game_steam_id:p.game_steam_id,delta:0,won:false}));
     if(!rows.length)return;
     entry={id,started:Number.isSafeInteger(m.started)?m.started:0,ended:m.ended,reason:receipt.voided?'voided':m.size!==2||!EVENT.maps.includes(m.map)?'not_ranked':'unverified',rows:rows.sort((a,b)=>a.player_id.localeCompare(b.player_id))};
   }
   const opsRaw=await call(['GET',base+'operations']),ops=opsRaw?JSON.parse(opsRaw):{};
   if(ops.finalized&&entry.ended>=ops.finalized.event_end)return;
   await call(['EVAL',PROJECT,'3',ledger+'matches',ledger+'reverted',rankedPrefix+'cheater:match:'+entry.id,entry.id,JSON.stringify(entry)]);cache=null;
 }
 async function read(){
   if(cache&&now()-cache.at<5000)return cache;
   if(pending)return pending;
   pending=(async()=>{const data=await call(['EVAL',READ,'4',base+'registrations',ledger+'matches',base+'operations',ledger+'reverted']);
     if(!Array.isArray(data)||data.length!==4||!Array.isArray(data[0])||!Array.isArray(data[1]))throw Error('Invalid event snapshot');
     const registrations=data[0].map(JSON.parse),matches=data[1].map(JSON.parse);
     const ops=data[2]?JSON.parse(data[2]):require('./tournament-ops.cjs').initial(),event=effectiveEvent(ops);
     const reverted=new Set(data[3]);cache={at:now(),registrations,matches,ops,event,reverted,rows:standings(registrations,matches,event,ops.exclusions,reverted)};return cache;
   })().finally(()=>{pending=null;});return pending;
 }
 const publicRow=(p,playerId)=>({rank:p.rank||null,persona:p.persona,net_rr:p.net_rr,gained_rr:p.gained_rr,lost_rr:p.lost_rr,wins:p.wins,matches:p.matches,tied:!!p.tied,eligible:!!p.eligible,is_you:p.player_id===playerId});
 async function view(playerId){
   if(playerId)await flushNotifications();
   const data=await read(),at=now(),state=phase(at,data.event),registration=data.registrations.find(r=>r.player_id===playerId),you=data.rows.find(r=>r.player_id===playerId);
   const eligible=data.rows.filter(p=>p.eligible),savedFinal=data.ops.finalized,final=currentFinal(savedFinal)?savedFinal:null,reviewRequired=!!savedFinal&&(!final||(final.ledger_count!==data.matches.length||(final.reverted_count||0)!==data.reverted.size));
   const winners=final?final.winners:prizeWinners(data.rows);
   const displayed=final?.standings||data.rows,displayYou=displayed.find(p=>p.player_id===playerId),mine=final?.winners.find(p=>p.player_id===playerId);
   const leaders=state==='live'?displayed.filter(p=>p.matches>0&&!p.disqualified).sort((a,b)=>compareScore(a,b)||a.player_id.localeCompare(b.player_id)).map(p=>({...p})):displayed.filter(p=>p.eligible);
   if(state==='live')leaders.forEach((p,i)=>{p.rank=i&&compareScore(p,leaders[i-1])===0?leaders[i-1].rank:i+1;});
   return {ok:true,event:data.event,server_now:at,phase:state,updated_at:data.at,registered_at:registration?.registered_at||null,entrant_count:data.registrations.length,
     you:displayYou?publicRow(displayYou,playerId):null,leaders:state==='scheduled'?[]:leaders.slice(0,10).map(p=>publicRow(p,playerId)),
     winners:state==='ended'?winners.map(p=>({...publicRow(p,playerId),prize_usd:p.prize_usd})):[],results_provisional:state==='ended'&&(!final||reviewRequired),
     confirmed_at:final?.at||null,review_required:reviewRequired,announcement:data.ops.announcement,outages:data.ops.outages.map(o=>({start:o.start,end:o.end})),
     history:you?.history||[],disqualified:!!you?.disqualified,exclusion_reason:data.ops.exclusions[playerId]?.reason||'',
     matches_needed:Math.max(0,EVENT.minimum_matches-(you?.matches||0)),gap_to_prize:you&&eligible.length>=EVENT.prizes.length?Math.max(0,eligible[EVENT.prizes.length-1].net_rr-you.net_rr):null,
     tickets:data.ops.tickets.filter(t=>t.player_id===playerId).map(t=>({id:t.id,category:t.category,match_id:t.match_id,message:t.message,at:t.at,status:t.status,resolved_at:t.resolved_at,replies:t.replies.map(r=>({id:r.id,at:r.at,message:r.message,notified:r.notified}))})),
     badges:you&&you.matches>0&&!you.disqualified?[{type:'participant',event_id:EVENT.id},...(mine&&!reviewRequired?[{type:'winner',event_id:EVENT.id,rank:mine.rank}]:[])]:[],
     payout:mine?data.ops.payouts[playerId]||{status:'awaiting_details'}:null};
 }
 async function adminView(){
   await flushNotifications();
   const data=await read(),ranked=new Map(data.rows.map(r=>[r.player_id,r]));
   const backlog=await call(['SCARD',prefix+'analytics:outbox']);
   return {ok:true,event:data.event,server_now:now(),phase:phase(now(),data.event),updated_at:data.at,entrant_count:data.registrations.length,match_count:data.matches.length,backlog,
    rows:data.registrations.map(r=>ranked.get(r.player_id)||{...r,rank:null,net_rr:0,wins:0,matches:0,history:[]}).sort((a,b)=>(a.rank||Infinity)-(b.rank||Infinity)||a.registered_at-b.registered_at),...data.ops,
    results_provisional:!currentFinal(data.ops.finalized)||data.ops.finalized.ledger_count!==data.matches.length||(data.ops.finalized.reverted_count||0)!==data.reverted.size};
 }
 async function support(playerId,body){
   const data=await read();if(!data.registrations.some(r=>r.player_id===playerId))return {ok:false,error:'registration_required'};
   const category=String(body?.category||''),match_id=String(body?.match_id||''),message=String(body?.message||'').trim();
   if(!['missing_rr','fair_play','appeal','outage','other'].includes(category)||match_id&&!/^[A-Za-z0-9_.:-]{1,80}$/.test(match_id)||message.length<5||message.length>1000)return {ok:false,error:'invalid_ticket'};
   return modify(playerId,'support',ops=>{
     if(now()>=effectiveEvent(ops).dispute_deadline)return {ok:false,error:'disputes_closed'};
     const duplicate=ops.tickets.find(t=>t.player_id===playerId&&t.category===category&&t.match_id===match_id&&t.message===message);if(duplicate)return {ticket_id:duplicate.id,noop:true};
     if(ops.tickets.length>=1000||ops.tickets.filter(t=>t.player_id===playerId).length>=5)return {ok:false,error:'ticket_limit'};
     const ticket={id:require('node:crypto').randomUUID(),player_id:playerId,category,match_id,message,at:now(),status:'open',replies:[]};ops.tickets.push(ticket);return {ticket_id:ticket.id};
   });
 }
 async function adminAction(actor,body){
   const action=String(body?.action||''),player_id=String(body?.player_id||''),reason=String(body?.reason||'').trim().slice(0,500);
   if(!['disqualify','restore','resolve','reply','confirm','reopen','payout','outage','announcement'].includes(action))return {ok:false,error:'invalid_action'};
   const operation=body.operation_id?{id:String(body.operation_id),hash:require('node:crypto').createHash('sha256').update(JSON.stringify(body)).digest('hex')}:null;
   if(operation&&!/^[0-9a-f-]{36}$/.test(operation.id))return {ok:false,error:'invalid_operation'};
   const previous=await operations.replay(actor,operation);if(previous)return previous;
   cache=null;const data=await read();
   if(action==='confirm'){
     if(finalizationHealth){const health=await finalizationHealth();if(!health.persisted||health.outbox||health.pending_terminal||health.last_error)return {ok:false,error:'evidence_pending'};}
     const winners=prizeWinners(data.rows);
     if(now()<data.event.dispute_deadline)return {ok:false,error:'review_window_open'};
     if(data.ops.tickets.some(t=>t.status==='open'))return {ok:false,error:'open_tickets'};
     if(!winners.length)return {ok:false,error:'not_enough_eligible_players'};
     if(data.ops.finalized)return {ok:false,error:'already_confirmed'};
     return modify(actor,action,ops=>{
       if(ops.revision!==data.ops.revision)return {ok:false,error:'state_changed'};
       ops.finalized={rules_version:EVENT.rules_version,mode:EVENT.mode,id:require('node:crypto').randomUUID(),at:now(),by:actor,event_end:data.event.end_at,ledger_count:data.matches.length,reverted_count:data.reverted.size,standings:data.rows.map(r=>({...r,history:undefined})),winners:winners.map(r=>({...r,history:undefined,prize_usd:EVENT.prizes[r.rank-1]}))};
       (ops.confirmations||=[]).push(ops.finalized);
       for(const winner of ops.finalized.winners){if(ops.payouts[winner.player_id]?.status==='paid')return {ok:false,error:'prior_payment_review_required'};ops.payouts[winner.player_id]={status:'awaiting_details',confirmation_id:ops.finalized.id};}return {};
     },{count:data.matches.length,reverted:data.reverted.size,start:EVENT.start_at,end:data.event.end_at},operation);
   }
   return modify(actor,action,ops=>{
     if(['disqualify','restore','outage'].includes(action)&&ops.finalized)return {ok:false,error:'reopen_required'};
     if(['disqualify','restore','resolve','reply','reopen','outage'].includes(action)&&reason.length<5)return {ok:false,error:'reason_required'};
     if(action==='disqualify'||action==='restore'){
       if(!data.registrations.some(r=>r.player_id===player_id))return {ok:false,error:'unknown_entrant'};
       if(action==='disqualify')ops.exclusions[player_id]={at:now(),by:actor,reason};else delete ops.exclusions[player_id];
     }else if(action==='resolve'||action==='reply'){
       const ticket=ops.tickets.find(t=>t.id===body.ticket_id);if(!ticket)return {ok:false,error:'unknown_ticket'};
       if(ticket.replies.length>=30)return {ok:false,error:'reply_limit'};
       ticket.replies.push({id:require('node:crypto').randomUUID(),at:now(),by:actor,message:reason,notified:null});if(action==='resolve'){ticket.status='resolved';ticket.resolved_at=now();}
     }else if(action==='reopen'){
       if(!ops.finalized)return {ok:false,error:'not_confirmed'};
       if(Object.values(ops.payouts).some(p=>p.status==='paid'))return {ok:false,error:'prior_payment_review_required'};
       for(const payout of Object.values(ops.payouts))if(payout.status!=='paid')payout.status='on_hold';ops.finalized=null;
     }else if(action==='announcement'){
       ops.announcement=reason;
     }else if(action==='outage'){
       const start=Number(body.start),end=Number(body.end);
       if(!Number.isSafeInteger(start)||!Number.isSafeInteger(end)||start>=end||end>now()||start>=effectiveEvent(ops).end_at||end<=EVENT.start_at||ops.outages.length>=100)return {ok:false,error:'invalid_outage'};
       ops.outages.push({start,end,reason,by:actor,at:now()});
     }else if(action==='payout'){
       if(!currentFinal(ops.finalized)||!ops.finalized?.winners.some(r=>r.player_id===player_id)||ops.finalized.ledger_count!==data.matches.length||(ops.finalized.reverted_count||0)!==data.reverted.size)return {ok:false,error:'winner_not_confirmed'};
       if(!EVENT.payout_methods.includes(body.method))return {ok:false,error:'invalid_payout_method'};
       const existing=ops.payouts[player_id]||{};
       if(body.status==='details_received'){
         if(existing.status==='paid')return {ok:false,error:'already_paid'};
         const received=existing.details_received_at||now();ops.payouts[player_id]={...existing,status:'details_received',method:body.method,details_received_at:received,due_at:received+7*86400000};
       }else if(body.status==='paid'&&existing.details_received_at){ops.payouts[player_id]={...existing,status:'paid',method:body.method,paid_at:existing.paid_at||now()};}
       else return {ok:false,error:'details_required'};
     }
     return {player_id:player_id||null,reason,details:{ticket_id:body.ticket_id||null,start:body.start||null,end:body.end||null,method:body.method||null,status:body.status||null,confirmation_id:ops.finalized?.id||null}};
   },action==='payout'?{count:data.matches.length,reverted:data.reverted.size,start:EVENT.start_at,end:data.event.end_at}:null,operation);
 }
 return {register,project,view,adminView,support,adminAction,flushNotifications};
}
module.exports={EVENT,BACKFILL_VERSION,ledgerBase,phase,effectiveEvent,matchEntry,standings,create,REGISTER,PROJECT,READ};
