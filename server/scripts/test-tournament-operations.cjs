// Run: ACCOUNT_TEST_PYTHON=<python with fakeredis[lua]> node --test server/scripts/test-tournament-operations.cjs
'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),{spawn}=require('node:child_process'),readline=require('node:readline'),path=require('node:path');
const api=require('../tournament.cjs'),start=api.EVENT.start_at,end=api.EVENT.end_at;
test('outages extend the deadline by union duration and never double count overlapping incidents',()=>{
 const event=api.effectiveEvent({outages:[{start:start+1000,end:start+11000},{start:start+5000,end:start+16000}]});
 assert.equal(event.end_at,end+15000);assert.equal(event.dispute_deadline,event.end_at+86400000);
});
test('five matches are required for prize eligibility and disqualified entrants retain their evidence',()=>{
 const reg={player_id:'76561198000000001',game_steam_id:'76561198000000001',registered_at:start-1};
 const entries=Array.from({length:5},(_,i)=>({id:'m'+i,started:start,ended:start+1000+i,rows:[{...reg,delta:20,won:true}]}));
 assert.equal(api.standings([reg],entries.slice(0,4))[0].eligible,false);
 assert.equal(api.standings([reg],entries)[0].eligible,true);
 const row=api.standings([reg],entries,api.EVENT,{[reg.player_id]:{reason:'win trading'}})[0];assert.equal(row.eligible,false);assert.equal(row.history.length,5);
});
function fixture(t){
 const child=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')]);let seq=0;const pending=new Map();
 readline.createInterface({input:child.stdout}).on('line',line=>{const r=JSON.parse(line),p=pending.get(r.id);pending.delete(r.id);if(p)r.error?p.reject(Error(r.error)):p.resolve(r.result);});
 child.on('exit',()=>{for(const p of pending.values())p.reject(Error('Redis test bridge stopped'));pending.clear();});t.after(()=>child.kill());
 return args=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});child.stdin.write(JSON.stringify({id,command:args})+'\n');});
}
test('the BB1 reformat preserves registrations and support while isolating legacy BB5 scores',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',id='76561198000000001',opponent='76561198000000002';
 const registration={player_id:id,game_steam_id:id,persona:'Existing player',registered_at:start-1000,rules_version:3};
 await store(['HSET',base+'registrations',id,JSON.stringify(registration)]);
 await store(['HSET',base+'identities',id,id]);
 const ops=require('../tournament-ops.cjs').initial();ops.tickets.push({id:'old-ticket',player_id:id,category:'other',message:'Saved support request',status:'open',replies:[]});
 await store(['SET',base+'operations',JSON.stringify(ops)]);
 await store(['HSET',base+'matches','same-id',JSON.stringify({id:'same-id',started:start,ended:start+1000,rows:[{...registration,delta:999,won:true}]})]);
 await store(['SADD',base+'reverted','same-id']);
 const svc=api.create({store,now:()=>start+2000});
 assert.equal((await svc.register(registration)).registered_at,registration.registered_at);
 const before=await svc.view(id);assert.equal(before.you.net_rr,0);assert.equal(before.tickets[0].id,'old-ticket');
 const receipt={mode:'BB1',matchId:'same-id',data_collected:true,publicMatch:{id:'same-id',mode:'BB1',map:'Paintball',size:2,outcome:'played',started:start,ended:start+1000,players:[id,opponent].map((p,i)=>({player_id:p,game_steam_id:p,team:i+1}))},rows:[id,opponent].map((p,i)=>({steamId:p,won:i===0,rr:{delta:i===0?20:-20}}))};
 await svc.project(receipt);await svc.project(receipt);
 const after=await svc.view(id);assert.equal(after.you.net_rr,20);assert.equal(after.history.length,1);assert.equal(after.registered_at,registration.registered_at);
 assert.equal(await store(['HLEN',base+'BB1:matches']),1);
 for(const change of [r=>{r.mode='BB5';r.publicMatch.mode='BB5';},r=>r.publicMatch.mode='BB5',r=>{delete r.mode;delete r.publicMatch.mode;}]){
  const wrong=structuredClone(receipt);wrong.matchId='wrong';wrong.publicMatch.id='wrong';change(wrong);await svc.project(wrong);
 }
 assert.equal(await store(['HLEN',base+'BB1:matches']),1,'other modes never enter this ledger');
});
test('confirmation waits for BB1 backfill, terminal evidence and correction jobs',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',ranked='hub:ranked:BB1:',id='76561198000000001';
 const reg={player_id:id,game_steam_id:id,registered_at:start-1};
 await store(['HSET',base+'registrations',id,JSON.stringify(reg)]);
 for(let i=0;i<5;i++)await store(['HSET',base+'BB1:matches','m'+i,JSON.stringify({id:'m'+i,started:start,ended:start+1000+i,rows:[{...reg,delta:20,won:true}]})]);
 await store(['SET','hub:analytics:backfill_done:tournament-launch-2026-v1','1']);
 const svc=api.create({store,now:()=>end+86400001}),confirm=()=>svc.adminAction(id,{action:'confirm'});
 assert.equal((await confirm()).error,'evidence_pending');
 await store(['SET','hub:analytics:backfill_done:'+api.BACKFILL_VERSION+'-BB1','1']);
 await store(['SADD',ranked+'live:matches','pending']);await store(['SET',ranked+'live:match:pending',JSON.stringify({final_ended_at:end-1})]);
 assert.equal((await confirm()).error,'evidence_pending');
 await store(['DEL',ranked+'live:matches']);await store(['SADD',ranked+'cheater:jobs','pending']);
 assert.equal((await confirm()).error,'evidence_pending');
 await store(['DEL',ranked+'cheater:jobs']);
 assert.equal((await confirm()).ok,true);
});
test('support, audited exclusion, confirmation and payout tracking persist and fail closed',async t=>{
 const store=fixture(t),id='76561198000000001',base='hub:tournament:launch-2026:';let clock=start+1000;
 const service=api.create({store,now:()=>clock});
 await store(['HSET',base+'registrations',id,JSON.stringify({player_id:id,game_steam_id:id,persona:'Sam',registered_at:start-1})]);
 const ticket=await service.support(id,{match_id:'missing-match',category:'missing_rr',message:'My finished match is missing.'});assert.equal(ticket.ok,true);
 assert.equal((await service.view(id)).tickets.length,1);
 clock=end+86400001;
 assert.equal((await service.adminAction(id,{action:'confirm'})).ok,false,'open dispute blocks confirmation');
 assert.equal((await service.adminAction(id,{action:'resolve',ticket_id:ticket.ticket_id,reason:'Checked the saved receipt.'})).ok,true);
 assert.equal((await service.adminAction(id,{action:'disqualify',player_id:id,reason:'Verified win trading.'})).ok,true);
 const rebuilt=api.create({store,now:()=>clock});assert.equal((await rebuilt.view(id)).disqualified,true);
 assert.equal((await service.adminAction(id,{action:'restore',player_id:id,reason:'Appeal accepted.'})).ok,true);
 const ops=await service.adminView();assert(ops.audit.length>=3);
 assert.equal((await service.adminAction(id,{action:'payout',player_id:id,method:'Bank transfer',status:'paid'})).ok,false);
});

test('confirmation freezes results, retries are idempotent, and late evidence blocks payout atomically',async t=>{
 const raw=fixture(t),base='hub:tournament:launch-2026:',id='76561198000000001';let inject=false;
 const regs=Array.from({length:5},(_,i)=>({player_id:String(76561198000000001n+BigInt(i)),game_steam_id:String(76561198000000001n+BigInt(i)),registered_at:start-1,persona:'Player '+i}));
 for(const r of regs)await raw(['HSET',base+'registrations',r.player_id,JSON.stringify(r)]);
 for(let i=0;i<5;i++)await raw(['HSET',base+'BB1:matches','m'+i,JSON.stringify({id:'m'+i,started:start,ended:start+1000+i,rows:regs.map((r,j)=>({...r,delta:100-j,won:true}))})]);
 await raw(['SET','hub:analytics:backfill_done:tournament-launch-2026-bb1-v3-BB1','1']);
 const store=async args=>{if(inject&&args[0]==='EVAL'&&String(args[1]).startsWith('-- tournament-operations')){inject=false;await raw(['HSET',base+'BB1:matches','late',JSON.stringify({id:'late',started:start,ended:start+9000,rows:regs.map((r,j)=>({...r,delta:j===4?1000:-1000,won:j===4}))})]);}return raw(args);};
 const svc=api.create({store,now:()=>end+86400001}),confirm={action:'confirm',operation_id:require('node:crypto').randomUUID()};
 assert.equal((await svc.adminAction(id,confirm)).ok,true);assert.equal((await svc.adminAction(id,confirm)).replayed,true);
 const fixed=await svc.view(id);assert.equal(fixed.you.net_rr,500);assert.equal(fixed.results_provisional,false);
 assert.equal((await svc.adminAction(id,{action:'payout',player_id:id,method:'PayPal',status:'details_received'})).ok,true);
 inject=true;const paid=await svc.adminAction(id,{action:'payout',player_id:id,method:'PayPal',status:'paid'});assert.equal(paid.ok,false);assert.equal(paid.error,'state_changed');
 const changed=await svc.view(id);assert.equal(changed.review_required,true);assert.equal(changed.you.net_rr,500);assert.equal(changed.leaders[0].persona,'Player 0');assert.equal(changed.payout.status,'details_received');
});

test('support replies survive delivery failures, retain one conversation message, and replay once',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',id='76561198000000001';let fail=true;const delivered=new Set();
 await store(['HSET',base+'registrations',id,JSON.stringify({player_id:id,game_steam_id:id,registered_at:start-1})]);
 const svc=api.create({store,now:()=>start+10000,notifySupport:async(actor,body)=>{if(fail)throw Error('Offline');delivered.add(body.client_id);return {ok:true};}});
 const ticket=await svc.support(id,{category:'outage',message:'A match broke during warmup.'});
 const request={action:'reply',ticket_id:ticket.ticket_id,reason:'We are reviewing your match.',operation_id:require('node:crypto').randomUUID()};
 assert.equal((await svc.adminAction(id,request)).ok,true);assert.equal((await svc.adminAction(id,request)).replayed,true);
 assert.equal((await svc.view(id)).tickets[0].replies.length,1);assert.equal(delivered.size,0);fail=false;
 await svc.adminView();await svc.adminView();assert.equal(delivered.size,1);assert((await svc.view(id)).tickets[0].replies[0].notified);
 assert.equal((await svc.view(id)).tickets[0].replies[0].by,undefined,'private admin attribution must not reach players');
 assert.equal((await svc.adminAction(id,{...request,reason:'A different message.'})).error,'operation_conflict');
});

test('a smaller qualified field can confirm and award the published places',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',admin='76561198000000099';
 const regs=Array.from({length:2},(_,i)=>({player_id:String(76561198000000001n+BigInt(i)),game_steam_id:String(76561198000000001n+BigInt(i)),registered_at:start-1}));
 for(const r of regs)await store(['HSET',base+'registrations',r.player_id,JSON.stringify(r)]);
 for(let i=0;i<5;i++)await store(['HSET',base+'BB1:matches','m'+i,JSON.stringify({id:'m'+i,started:start,ended:start+1000+i,rows:regs.map((r,j)=>({...r,delta:100-j,won:true}))})]);
 await store(['SET','hub:analytics:backfill_done:tournament-launch-2026-bb1-v3-BB1','1']);
 const svc=api.create({store,now:()=>end+86400001});
 assert.equal((await svc.adminAction(admin,{action:'confirm'})).ok,true);
 assert.deepEqual((await svc.view(regs[0].player_id)).winners.map(w=>w.prize_usd),[100,35]);
});

test('all players tied at third receive the full prize, confirmation, badges and payout tracking',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',admin='76561198000000099';
 const regs=Array.from({length:8},(_,i)=>({player_id:String(76561198000000001n+BigInt(i)),game_steam_id:String(76561198000000001n+BigInt(i)),registered_at:start-1,persona:'Player '+i}));
 const gains=[100,90,80,80,80,70,60,50];
 for(const r of regs)await store(['HSET',base+'registrations',r.player_id,JSON.stringify(r)]);
 for(let i=0;i<5;i++)await store(['HSET',base+'BB1:matches','m'+i,JSON.stringify({id:'m'+i,started:start,ended:start+1000+i,rows:regs.map((r,j)=>({...r,delta:gains[j],won:true}))})]);
 await store(['SET','hub:analytics:backfill_done:tournament-launch-2026-bb1-v3-BB1','1']);
 const svc=api.create({store,now:()=>end+86400001});
 const preview=await svc.view(regs[4].player_id);
 assert.deepEqual(preview.winners.map(w=>w.rank),[1,2,3,3,3]);
 assert.deepEqual(preview.winners.map(w=>w.prize_usd),[100,35,15,15,15]);
 assert.equal(preview.winners.reduce((sum,w)=>sum+w.prize_usd,0),180);
 assert.equal((await svc.adminAction(admin,{action:'confirm'})).ok,true);
 // Read through a fresh service to verify the durable confirmation includes every shared place.
 const restored=api.create({store,now:()=>end+86400001});
 for(const r of regs.slice(2,5)){
   const confirmed=await restored.view(r.player_id);
   assert.equal(confirmed.results_provisional,false);assert.deepEqual(confirmed.winners,preview.winners.map(w=>({...w,is_you:w.persona===r.persona})));
   assert(confirmed.badges.some(b=>b.type==='winner'&&b.rank===3));assert.equal(confirmed.payout.status,'awaiting_details');
   assert.equal((await restored.adminAction(admin,{action:'payout',player_id:r.player_id,method:'PayPal',status:'details_received'})).ok,true);
   assert.equal((await restored.adminAction(admin,{action:'payout',player_id:r.player_id,method:'PayPal',status:'paid'})).ok,true);
 }
 assert.equal((await restored.view(regs[7].player_id)).payout,null);
 assert.equal((await restored.adminAction(admin,{action:'payout',player_id:regs[7].player_id,method:'PayPal',status:'details_received'})).ok,false);
});

test('outage extensions recalculate previously excluded matches and finalization waits for evidence',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',id='76561198000000001',svc=api.create({store,now:()=>end+9000});
 await store(['HSET',base+'registrations',id,JSON.stringify({player_id:id,game_steam_id:id,registered_at:start-1})]);
 await store(['HSET',base+'BB1:matches','extended',JSON.stringify({id:'extended',started:end-1000,ended:end+5000,rows:[{player_id:id,game_steam_id:id,delta:25,won:true}]})]);
 assert.equal((await svc.view(id)).you.net_rr,0);
 assert.equal((await svc.adminAction(id,{action:'outage',start:start+10000,end:start+20000,reason:'Match service outage.'})).ok,true);
 const view=await svc.view(id);assert.equal(view.event.end_at,end+10000);assert.equal(view.phase,'live');assert.equal(view.you.net_rr,25);
 assert.equal((await svc.adminAction(id,{action:'confirm'})).error,'review_window_open');
});

test('cheater corrections and pending correction jobs block stale winner confirmation and payments',async t=>{
 const raw=fixture(t),base='hub:tournament:launch-2026:',id='76561198000000001';let inject=false;
 const regs=Array.from({length:5},(_,i)=>({player_id:String(76561198000000001n+BigInt(i)),game_steam_id:String(76561198000000001n+BigInt(i)),registered_at:start-1,persona:'Player '+i}));
 for(const r of regs)await raw(['HSET',base+'registrations',r.player_id,JSON.stringify(r)]);
 for(let i=0;i<6;i++)await raw(['HSET',base+'BB1:matches','m'+i,JSON.stringify({id:'m'+i,started:start,ended:start+1000+i,rows:regs.map((r,j)=>({...r,delta:100-j,won:true}))})]);
 await raw(['SET','hub:analytics:backfill_done:tournament-launch-2026-bb1-v3-BB1','1']);
 const store=async args=>{if(inject&&args[0]==='EVAL'&&String(args[1]).startsWith('-- tournament-operations')){inject=false;await raw(['SADD',base+'BB1:reverted','m0']);}return raw(args);};
 const svc=api.create({store,now:()=>end+86400001});
 await raw(['SADD','hub:ranked:BB1:cheater:jobs','unfinished-job']);
 assert.equal((await svc.adminAction(id,{action:'confirm'})).ok,false);
 await raw(['DEL','hub:ranked:BB1:cheater:jobs']);
 assert.equal((await svc.adminAction(id,{action:'confirm'})).ok,true);
 assert.equal((await svc.adminAction(id,{action:'payout',player_id:id,method:'PayPal',status:'details_received'})).ok,true);
 inject=true;assert.equal((await svc.adminAction(id,{action:'payout',player_id:id,method:'PayPal',status:'paid'})).ok,false);
 const view=await svc.view(id);assert.equal(view.review_required,true);assert.equal(view.results_provisional,true);
 assert.equal(view.payout.status,'details_received');assert.equal(view.you.net_rr,600,'confirmed snapshot is retained for explicit review');
});

test('undeliverable old support replies cannot starve newer conversations',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',actor='76561198000000099',delivered=[];
 const ops=require('../tournament-ops.cjs').initial();
 ops.tickets=[{id:'ticket',player_id:'76561198000000001',replies:Array.from({length:11},(_,i)=>({id:'reply-'+i,by:actor,message:'Reply '+i,notified:null}))}];
 await store(['SET',base+'operations',JSON.stringify(ops)]);
 const svc=api.create({store,notifySupport:async(by,body)=>{if(body.client_id!=='reply-10')throw Error('Recipient unavailable');delivered.push(body.client_id);return {ok:true};}});
 await svc.flushNotifications();await svc.flushNotifications();
 assert.deepEqual(delivered,['reply-10']);
});

test('event activity remains writable beyond 2000 entries with durable retry receipts',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',actor='76561198000000099',ops=require('../tournament-ops.cjs'),state=ops.initial();
 const old={id:require('node:crypto').randomUUID(),hash:'old-hash'};
 state.audit=Array.from({length:2000},(_,i)=>({id:'a'+i,actor,action:'old',at:i,...(i===0?{operation_id:old.id,fingerprint:old.hash,result:{ticket_id:'original'}}:{})}));
 await store(['SET',base+'operations',JSON.stringify(state)]);
 const service=ops.create({call:store,base,prefix:'hub:',now:()=>start});
 assert.equal((await service.modify(actor,'new',s=>{s.announcement='New';return {};})).ok,true);
 assert.equal(await store(['LLEN',base+'audit']),2001);
 assert(JSON.parse(await store(['GET',base+'operations'])).audit.length<=100);
 assert.equal((await service.replay(actor,old)).ticket_id,'original');
 assert.equal((await service.modify(actor,'new',()=>{throw Error('must not repeat');},null,old)).replayed,true);
 assert.equal((await service.replay(actor,{...old,hash:'changed'})).error,'operation_conflict');
 await service.modify(actor,'next',()=>({}));assert.equal(await store(['LLEN',base+'audit']),2002);
 await store(['DEL',base+'operation-receipts']);await store(['SET',base+'operation-receipts','bad type']);
 const before=await store(['GET',base+'operations']);await assert.rejects(service.modify(actor,'fail',()=>({})),/type/);
 assert.equal(await store(['GET',base+'operations']),before);assert.equal(await store(['LLEN',base+'audit']),2002);
});

test('live leaders appear after the first eligible match while cash prizes still require five',async t=>{
 const store=fixture(t),base='hub:tournament:launch-2026:',id='76561198000000001';
 await store(['HSET',base+'registrations',id,JSON.stringify({player_id:id,game_steam_id:id,persona:'First player',registered_at:start-1})]);
 await store(['HSET',base+'BB1:matches','m0',JSON.stringify({id:'m0',started:start,ended:start+1000,rows:[{player_id:id,game_steam_id:id,delta:25,won:true}]})]);
 const svc=api.create({store,now:()=>start+2000}),view=await svc.view(id);
 assert.equal(view.leaders.length,1);assert.equal(view.leaders[0].rank,1);assert.equal(view.leaders[0].eligible,false);assert.equal(view.matches_needed,4);assert.equal(view.you.rank,null);assert.equal(view.winners.length,0);
});
