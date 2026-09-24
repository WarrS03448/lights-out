'use strict';
// Run: node --test scripts/test-recovery.cjs (ACCOUNT_TEST_PYTHON supplies fakeredis[lua]).
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const path=require('node:path');
const crypto=require('node:crypto');
let recovery={};try{recovery=require('../recovery.cjs');}catch(e){if(e.code!=='MODULE_NOT_FOUND')throw e;}
let bridge,store;
before(async()=>{
  bridge=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')],{stdio:['pipe','pipe','pipe']});
  let n=0;const pending=new Map();
  readline.createInterface({input:bridge.stdout}).on('line',line=>{const r=JSON.parse(line),p=pending.get(r.id);pending.delete(r.id);if(p)r.error?p.reject(Error(r.error)):p.resolve(r.result);});
  bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('test store stopped'));pending.clear();});
  store=command=>new Promise((resolve,reject)=>{const id=++n;pending.set(id,{resolve,reject});bridge.stdin.write(JSON.stringify({id,command})+'\n');});
  assert.equal(await store(['PING']),'PONG');
});
after(()=>bridge?.kill());
const ids=['76561198000000001','76561198000000002','76561198000000003','76561198000000004'];
const tokens=ids.map((_,i)=>String(i+1).repeat(64));
const digest=t=>crypto.createHash('sha256').update(t).digest('hex');
const now=1750000000000;
const keys=['t:authority','t:live','t:receipt','t:recovery'];
const checkpoint=()=>({v:1,round:3,limit:7,scores:[2,1],objective:1,rows:ids.map((id,i)=>({id,team:i%2,k:2,d:1,sp:3}))});
async function fixture(){
  assert.equal(typeof recovery.execute,'function','recovery state machine must be implemented');
  await store(['FLUSHALL']);
  const m={id:'0123456789abcdef',mode:'BB5',state:'live',host:ids[0],host_epoch:0,map:'Rome',start_ready_verified:true,
    expected_score_limit:7,expected_max_rounds:12,agreedScoreLimit:7,players:ids.map(player_id=>({player_id,game_steam_id:player_id})),left:[],
    game_bindings:Object.fromEntries(ids.map(id=>[id,id])),teams:{1:[ids[0],ids[2]],2:[ids[1],ids[3]]},
    ingame:{__map:ids.map((id,i)=>[id,i%2])},score:{1:2,2:1},reportToken:tokens[0],
    migration_capabilities:Object.fromEntries(ids.map((id,i)=>[id,tokens[i]])),migration_digests:Object.fromEntries(ids.map((id,i)=>[id,digest(tokens[i])]))};
  await store(['SET',keys[1],JSON.stringify(m)]);
  await store(['SET',keys[0],JSON.stringify({host:ids[0],epoch:0,digest:digest(tokens[0]),last_seen:now-61000,candidate:''})]);
  return m;
}
const call=(operation,who=0,extra={})=>recovery.execute(store,keys,{operation,player:ids[who],token:tokens[who],epoch:0,
  session:'chm-0123456789abcdef',now,ttl:86400,...extra});
async function eligible(){await fixture();assert.equal((await call('checkpoint',0,{checkpoint:checkpoint()})).ok,true);
  for(const who of [1,2])assert.equal((await call('closed',who)).ok,true);}

for(const limit of [5,7])test(`BB1 recovery accepts only its frozen first-to-${limit} checkpoint`,async()=>{
 const m=await fixture();m.mode='BB1';m.map='Paintball';m.players=m.players.slice(0,2);
 m.teams={1:[ids[0]],2:[ids[1]]};m.assigned_teams=structuredClone(m.teams);
 m.game_bindings=Object.fromEntries(ids.slice(0,2).map(id=>[id,id]));
 m.expected_score_limit=limit;m.expected_max_rounds=limit*2-1;m.agreedScoreLimit=limit;
 await store(['SET',keys[1],JSON.stringify(m)]);
 const cp={...checkpoint(),limit,rows:checkpoint().rows.slice(0,2)};
 assert.equal((await call('checkpoint',0,{checkpoint:{...cp,limit:limit===5?7:5}})).ok,false);
 assert.equal((await call('checkpoint',0,{checkpoint:cp})).ok,true);
 assert.equal(JSON.parse(await store(['GET',keys[3]])).checkpoint.data.limit,limit);
});
async function prepareRestore(m) {
  const who=ids.indexOf(m.host),args={epoch:m.host_epoch,session:m.session_key,token:m.reportToken,checkpoint:checkpoint()};
  assert.equal((await call('opened',who,args)).ok,true);
  for(const peer of m.players.map(p=>ids.indexOf(p.player_id)))assert.equal((await call('pulse',peer,{epoch:m.host_epoch,session:m.session_key,
    token:m.migration_capabilities[ids[peer]],sequence:1})).ok,true);
  assert.equal((await call('seal',who,args)).ok,true);
  assert.equal((await call('prepared',who,args)).ok,true);
}

test('replacement readiness grants a fresh five minutes and seals late returns before any pulse can revive them',async()=>{
  await eligible();await call('claim',1);let m=JSON.parse(await store(['GET',keys[1]]));
  const auth={epoch:1,session:m.session_key,token:m.reportToken};
  const ready=now+45000,end=ready+300000;
  assert.equal((await call('opened',1,{...auth,now:ready})).ok,true);
  assert.equal((await call('opened',1,{...auth,now:ready+30000})).ok,true);
  m=JSON.parse(await store(['GET',keys[1]]));assert.equal(m.recovery.rejoin_until,end);
  assert.ok(JSON.parse(await store(['GET',keys[0]])).restore_until>end);
  for(const peer of [1,2])assert.equal((await call('pulse',peer,{...auth,token:m.migration_capabilities[ids[peer]],sequence:1,now:end-1000})).ok,true);
  assert.equal((await call('seal',1,{...auth,now:end-1})).ok,false);
  assert.equal((await call('pulse',0,{...auth,token:m.migration_capabilities[ids[0]],sequence:1,now:end})).ok,false);
  assert.equal((await call('seal',1,{...auth,now:end})).ok,true);
  m=JSON.parse(await store(['GET',keys[1]]));
  assert.deepEqual(m.recovery.roster.admitted,[ids[1],ids[2]]);
  assert.deepEqual(m.recovery.roster.excluded,[ids[0],ids[3]]);
  const penalties=Object.fromEntries([0,3].map(i=>[ids[i],{match_id:m.id,player_id:ids[i],reason:'reconnect_timeout',seconds:300}]));
  assert.equal((await call('adjudicated',1,{...auth,now:end,penalties})).ok,true);
  m=JSON.parse(await store(['GET',keys[1]]));
  assert.deepEqual(m.players.map(p=>p.player_id),[ids[1],ids[2]]);
  assert.equal(m.left.length,2);assert.equal(m.left[0].disconnect_confirmed,true);
  const partial={...checkpoint(),rows:checkpoint().rows.filter(r=>[ids[1],ids[2]].includes(r.id))};
  assert.equal((await call('prepared',1,{...auth,checkpoint:partial,now:end+1})).ok,true);
  assert.equal((await call('restored',1,{...auth,checkpoint:partial,now:end+2})).ok,true);
  assert.equal((await call('pulse',0,{...auth,token:tokens[0],sequence:2,now:end+3})).ok,false);
  const next={...partial,round:4,scores:[3,1]};
  assert.equal((await call('checkpoint',1,{...auth,checkpoint:next,now:end+4})).ok,true);
  const cp=JSON.parse(await store(['GET',keys[3]])).checkpoint.data;
  assert.equal(cp.round,4);assert.equal(cp.rows.length,4,'departed boundary stats survive future recovery');
  const changedLeft={...cp,round:5,scores:[4,1],rows:cp.rows.map(row=>row.id===ids[0]?{...row,k:row.k+1}:row)};
  assert.equal((await call('checkpoint',1,{...auth,checkpoint:changedLeft,now:end+5})).ok,false,
    'a disconnected player cannot gain stats in a later checkpoint');
  const later=end+61005,peerToken=m.migration_capabilities[ids[2]];
  await call('closed',2,{...auth,token:peerToken,now:later});
  assert.equal((await call('claim',2,{...auth,token:peerToken,now:later})).ok,true,
    'the reduced roster can recover again from a later completed round');
  m=JSON.parse(await store(['GET',keys[1]]));
  const second={epoch:2,session:m.session_key,token:m.reportToken,now:later};
  await call('opened',2,second);
  for(const peer of [1,2])await call('pulse',peer,{...second,token:m.migration_capabilities[ids[peer]],sequence:1});
  await call('seal',2,second);
  assert.equal((await call('prepared',2,{...second,checkpoint:next})).ok,true);
  assert.equal((await call('restored',2,{...second,checkpoint:next})).ok,true);
  assert.deepEqual(JSON.parse(await store(['GET',keys[1]])).players.map(p=>p.player_id),[ids[1],ids[2]]);
});

async function cutoffService(t,{mode='BB5',returned=[1,2],fault=()=>false}={}) {
  const originalClock=Date.now,oldPaused=process.env.COMP_NO_SHOW_PENALTIES_PAUSED;
  let clock=now;Date.now=()=>clock;process.env.COMP_NO_SHOW_PENALTIES_PAUSED='1';
  t.after(()=>{Date.now=originalClock;if(oldPaused===undefined)delete process.env.COMP_NO_SHOW_PENALTIES_PAUSED;else process.env.COMP_NO_SHOW_PENALTIES_PAUSED=oldPaused;});
  const m=await fixture();m.mode=mode;m.deadline=now+3600000;
  const participants=mode==='BB1'?ids.slice(0,2):ids;
  m.players=m.players.filter(p=>participants.includes(p.player_id));
  m.teams={1:participants.filter((_,i)=>i%2===0),2:participants.filter((_,i)=>i%2===1)};
  m.assigned_teams=structuredClone(m.teams);
  m.game_bindings=Object.fromEntries(participants.map(id=>[id,id]));
  const basePrefix='cutoff:'+mode+':',prefix=require('../ranked-modes.cjs').rankedPrefix(basePrefix,mode);
  const durable=async(args,opts)=>{if(fault(args))throw Error('injected storage outage');return store(args,opts);};
  const L=require('../live.cjs').create({upstashCmd:durable,prefix:basePrefix,modeId:mode});await L._internals.ready;t.after(()=>L.shutdown());
  L._internals.matches.set(m.id,m);participants.forEach(id=>L._internals.inMatch.set(id,m.id));await L._internals.flushMatches();
  const ak=prefix+'live:authority:'+m.id,a=JSON.parse(await store(['GET',ak]));
  a.last_seen=now-61000;await store(['SET',ak,JSON.stringify(a)]);
  const scope={match_id:m.id,epoch:0,session:'chm-'+m.id};
  const cp={...checkpoint(),rows:checkpoint().rows.filter(row=>participants.includes(row.id))};
  assert.equal((await L.recoveryReport(tokens[0],{...scope,operation:'checkpoint',user_id:ids[0],checkpoint:cp})).ok,true);
  for(const i of mode==='BB1'?[1]:[1,2])await L.recoveryAction(ids[i],{...scope,operation:'closed'});
  assert.equal((await L.recoveryAction(ids[1],{...scope,operation:'claim'})).ok,true);
  clock+=45000;assert.equal((await L.gameReportedIn(ids[1],'ch_lobby_read')).ok,true);
  const rs={match_id:m.id,epoch:1,session:m.session_key};clock=m.recovery.rejoin_until-1000;
  for(const i of returned)assert.equal((await L.recoveryReport(m.migration_capabilities[ids[i]],{...rs,operation:'pulse',sequence:1,user_id:ids[i]})).ok,true);
  clock+=1000;
  return {L,m,rs,prefix,basePrefix,durable,setClock:value=>{clock=value;}};
}

for(const mode of ['BB1','BB5'])test(mode+' recovery empty-team forfeit survives restart and saves result/penalties once',async t=>{
  let fail=true;
  const f=await cutoffService(t,{mode,returned:mode==='BB1'?[1]:[1,3],
    fault:args=>fail&&args[0]==='EVAL'&&args[1].startsWith('\nlocal old = redis.call')});
  await f.L._internals.checkHostTimeouts();
  assert.equal(f.m.terminal.recovery,true);assert.equal(f.m.terminal.winner,2);
  const receiptKey=f.prefix+'settlement:'+f.m.id;
  assert.equal(await store(['GET',receiptKey]),null,'outage leaves durable pending decision');
  await f.L.shutdown();fail=false;
  const reboot=require('../live.cjs').create({upstashCmd:f.durable,prefix:f.basePrefix,modeId:mode});
  await reboot._internals.ready;t.after(()=>reboot.shutdown());
  await reboot._internals.flushMatches();
  const receipt=JSON.parse(await store(['GET',receiptKey]));
  assert.equal(receipt.winner,2);assert.equal(receipt.terminal.recovery,true);
  for(const id of receipt.participants){
    assert.equal(receipt.history[id].recovery_forfeit,true);
    assert.equal(receipt.events[id].terminal.recovery,true);
    assert.equal(receipt.events[id].players.length,receipt.participants.length);
  }
  assert.equal(receipt.data_collected,true);assert.notEqual(receipt.voided,true);
  const pk=f.prefix+'penalty:'+ids[0],penalty=await store(['GET',pk]);
  assert.equal(JSON.parse(penalty).count,1);
  const rating=await store(['GET',f.prefix+'rating:'+ids[0]]);
  await reboot._internals.flushMatches();
  assert.equal(await store(['GET',pk]),penalty);assert.equal(await store(['GET',f.prefix+'rating:'+ids[0]]),rating);
  assert.equal(reboot._internals.matches.has(f.m.id),false);
});

test('a partially saved absence decision stays closed and retries its charges once',async t=>{
  let block=true;
  const f=await cutoffService(t,{fault:args=>block&&args[0]==='EVAL'&&args[1].includes('-- reconnect-penalty-v1')&&args[3].endsWith(ids[3])});
  await assert.rejects(f.L._internals.checkHostTimeouts(),/injected storage outage/);
  assert.deepEqual(f.m.recovery.roster.excluded,[ids[0],ids[3]]);
  assert.equal(f.m.recovery.roster.done,false);
  const first=await store(['GET',f.prefix+'reconnect:penalty:'+f.m.id+':'+ids[0]]);
  assert.equal(JSON.parse(first).count,1);
  assert.equal(f.L.takeJoinPermit(ids[3]),false);
  assert.equal(f.L.teamRuling(ids[1],ids[3],'stranger').yes,true);
  await f.L.shutdown();block=false;
  const reboot=require('../live.cjs').create({upstashCmd:f.durable,prefix:f.prefix});await reboot._internals.ready;t.after(()=>reboot.shutdown());
  await reboot._internals.checkHostTimeouts();
  const restored=reboot._internals.matches.get(f.m.id);
  assert.equal(restored.recovery.roster.done,true);
  assert.deepEqual(restored.players.map(p=>p.player_id),[ids[1],ids[2]]);
  assert.equal(await store(['GET',f.prefix+'reconnect:penalty:'+f.m.id+':'+ids[0]]),first);
});

test('another service worker releases a removed player after adopting the sealed roster',async t=>{
  const f=await cutoffService(t);
  const follower=require('../live.cjs').create({upstashCmd:f.durable,prefix:f.basePrefix});await follower._internals.ready;t.after(()=>follower.shutdown());
  assert.equal(follower._internals.inMatch.get(ids[0]),f.m.id);
  await f.L._internals.checkHostTimeouts();
  assert.equal((await follower.recoveryAction(ids[0],{...f.rs,operation:'status'})).ok,false);
  assert.equal(follower._internals.inMatch.has(ids[0]),false,'old worker cannot keep the excluded player trapped in a live match');
  assert.equal(follower.takeJoinPermit(ids[0]),false);
});

test('concession during restoration cannot create a result that blocks both restore and expiry',async t=>{
  const f=await cutoffService(t,{mode:'BB1',returned:[1]});
  const result=await f.L.concedeMatch({player_id:ids[1],game_steam_id:ids[1]},{match_id:f.m.id});
  assert.equal(result.ok,false);
  assert.equal(f.m.terminal,undefined,'a recovery must not acquire an uncommittable ordinary terminal');
  const snapshot=JSON.parse(await store(['GET',f.prefix+'live:match:'+f.m.id]));
  assert(snapshot);assert.equal(snapshot.terminal,undefined);
});

test('public recovery health distinguishes stale session evidence from permission to claim',async()=>{
  await fixture();await call('checkpoint',0,{checkpoint:checkpoint()});
  let status=(await call('status',1)).recovery;
  assert.equal(status.session_stale,true);assert.equal(status.can_claim,false);
  assert.equal(status.server_now,now);
  await call('pulse',3,{sequence:1});status=(await call('status',1)).recovery;
  assert.equal(status.session_stale,false,'a healthy participant suppresses cold-recovery instructions');
});

test('a failed replacement host cannot turn a return deadline into penalties for everyone',async()=>{
  await eligible();await call('claim',1);const m=JSON.parse(await store(['GET',keys[1]]));
  const auth={epoch:1,session:m.session_key,token:m.reportToken};
  await call('opened',1,auth);
  const result=await call('seal',1,{...auth,now:now+300000});
  assert.equal(result.ok,false);
  assert.equal(JSON.parse(await store(['GET',keys[1]])).recovery.roster,undefined);
});

test('an empty-side forfeit identifies the losing side even if a missing teammate appears first',async()=>{
  await eligible();await call('claim',2);let m=JSON.parse(await store(['GET',keys[1]]));
  const auth={epoch:1,session:m.session_key,token:m.reportToken};
  await call('opened',2,auth);
  await call('pulse',2,{...auth,sequence:1,now:now+299999});
  await call('seal',2,{...auth,now:now+300000});
  const penalties=Object.fromEntries([0,1,3].map(i=>[ids[i],{match_id:m.id,player_id:ids[i],reason:'reconnect_timeout'}]));
  assert.equal((await call('adjudicated',2,{...auth,now:now+300000,penalties})).ok,true);
  m=JSON.parse(await store(['GET',keys[1]]));assert.equal(m.terminal.winner,1);
  assert(m.teams[2].includes(m.terminal.loser),'representative forfeiting player belongs to the empty side');
});

test('checkpoint rejects foreign, partial, non-boundary and conflicting data, and survives independent live snapshots',async()=>{
  await fixture();const c=checkpoint();
  assert.equal((await call('checkpoint',1,{checkpoint:c})).ok,false);
  for(const bad of [{...c,rows:c.rows.slice(1)},{...c,rows:[c.rows[0],c.rows[0],...c.rows.slice(2)]},
    {...c,round:2},{...c,limit:8},{...c,objective:2},{...c,rows:c.rows.map(r=>({...r,k:1.1}))}])
    assert.equal((await call('checkpoint',0,{checkpoint:bad})).ok,false);
  assert.equal((await call('checkpoint',0,{checkpoint:c})).ok,true);
  const saved=await store(['GET',keys[3]]);
  assert.equal((await call('checkpoint',0,{checkpoint:{...c,rows:[...c.rows].reverse()}})).ok,true,'canonical retry');
  assert.equal((await call('checkpoint',0,{checkpoint:{...c,scores:[1,2]}})).ok,false);
  assert.equal(await store(['GET',keys[3]]),saved,'retries do not advance liveness or replace the checkpoint');
  const live=JSON.parse(await store(['GET',keys[1]]));live.score={1:3,2:1};await store(['SET',keys[1],JSON.stringify(live)]);
  assert.equal(JSON.parse(await store(['GET',keys[3]])).checkpoint.data.round,3);
});

test('ordinary departures retain completed statistics through later partial checkpoints and side swaps',async()=>{
  const m=await fixture();await call('checkpoint',0,{checkpoint:checkpoint()});
  m.left=[{...m.players.pop(),disconnect_confirmed:true,last_stats:{kills:99}}];
  await store(['SET',keys[1],JSON.stringify(m)]);
  const next={...checkpoint(),round:4,scores:[2,2],rows:checkpoint().rows.slice(0,3).map(r=>({...r,team:1-r.team}))};
  assert.equal((await call('checkpoint',0,{checkpoint:next})).ok,true);
  const saved=JSON.parse(await store(['GET',keys[3]])).checkpoint.data;
  assert.deepEqual(saved.rows.find(r=>r.id===ids[3]),{...checkpoint().rows[3],team:0});
  const changed={...saved,round:5,scores:[3,2],rows:saved.rows.map(r=>r.id===ids[3]?{...r,k:99}:r)};
  assert.equal((await call('checkpoint',0,{checkpoint:changed})).ok,false,'interrupted/departed stats cannot leak into a completed boundary');
});

for(const absent of [[3],[1,3]])test('ordinary presence expiry keeps recovery usable: missing '+absent.join(','),async t=>{
  const originalClock=Date.now,oldPaused=process.env.COMP_NO_SHOW_PENALTIES_PAUSED;
  let clock=now;Date.now=()=>clock;process.env.COMP_NO_SHOW_PENALTIES_PAUSED='1';
  t.after(()=>{Date.now=originalClock;if(oldPaused===undefined)delete process.env.COMP_NO_SHOW_PENALTIES_PAUSED;else process.env.COMP_NO_SHOW_PENALTIES_PAUSED=oldPaused;});
  const m=await fixture();m.assigned_teams=structuredClone(m.teams);m.deadline=now+3600000;
  let blockResult=false;
  const durable=async(args,opts)=>{if(blockResult&&args[0]==='EVAL'&&args[1].startsWith('\nlocal old = redis.call'))throw Error('result unavailable');return store(args,opts);};
  const prefix='ordinary:',L=require('../live.cjs').create({upstashCmd:durable,prefix});await L._internals.ready;t.after(()=>L.shutdown());
  L._internals.matches.set(m.id,m);ids.forEach(id=>L._internals.inMatch.set(id,m.id));await L._internals.flushMatches();
  const follower=require('../live.cjs').create({upstashCmd:store,prefix});await follower._internals.ready;t.after(()=>follower.shutdown());
  const stale=follower._internals.matches.get(m.id);assert(stale);
  const fields={match_id:m.id,epoch:0,session:'chm-'+m.id,operation:'checkpoint',user_id:ids[0]};
  assert.equal((await L.recoveryReport(tokens[0],{...fields,checkpoint:checkpoint()})).ok,true);
  const present=ids.filter((_,i)=>!absent.includes(i));
  const rows=present.map(steam_id=>({steam_id,active:1}));
  assert.equal((await L.matchPresence(ids[0],m.id,rows)).error,'waiting for reconnect');
  await follower._internals.flushMatches();
  const waiting=JSON.parse(await store(['GET',prefix+'live:match:'+m.id]));
  assert.equal(waiting.reconnect[ids[absent[0]]].deadline,now+300000,'a stale worker cannot reset the reconnect window');
  clock+=300000;
  blockResult=absent.length>1;
  const outcome=await L.matchPresence(ids[0],m.id,rows);
  if(absent.length===1){
    assert.equal(outcome.ok,true);assert.equal(m.left.length,1);
    stale.ordinary_revision_probe=true;await follower._internals.flushMatches();
    const durable=JSON.parse(await store(['GET',prefix+'live:match:'+m.id]));
    assert.equal(durable.players.length,3,'an older worker cannot resurrect a confirmed leaver');
    assert.equal(durable.left.length,1);
    await follower.recoveryAction(ids[0],{match_id:m.id,epoch:0,session:'chm-'+m.id,operation:'status'});
    assert.equal(stale.players.length,3,'same-host worker adopts membership version');
    assert.equal(follower._internals.inMatch.has(ids[3]),false,'departed membership is released on every worker');
    const cp={...checkpoint(),round:4,scores:[3,1],rows:checkpoint().rows.filter(r=>present.includes(r.id))};
    assert.equal((await L.recoveryReport(tokens[0],{...fields,checkpoint:cp})).ok,true);
    assert.equal(JSON.parse(await store(['GET',prefix+'live:recovery:'+m.id])).checkpoint.data.round,4);
  }else{
    assert.equal(outcome.ok,false,'presence cannot approve an empty-team round');
    assert.equal(await store(['GET',prefix+'settlement:'+m.id]),null,'result outage leaves the durable forfeit pending');
    await follower.recoveryAction(ids[1],{match_id:m.id,epoch:0,session:'chm-'+m.id,operation:'status'});
    assert.equal(follower._internals.inMatch.has(ids[1]),false,'other workers release a departed BB5 player while the result is pending');
    blockResult=false;await L._internals.flushMatches();
    const receipt=JSON.parse(await store(['GET',prefix+'settlement:'+m.id]));
    assert.equal(receipt.winner,1);assert.equal(receipt.terminal.absence,true);
  }
});

test('a team emptied before recovery cannot bypass forfeit when every remaining player returns',async()=>{
  const m=await fixture();await call('checkpoint',0,{checkpoint:checkpoint()});
  m.left=m.players.filter(p=>[ids[1],ids[3]].includes(p.player_id)).map(p=>({...p,disconnect_confirmed:true}));
  m.players=m.players.filter(p=>!m.left.some(l=>l.player_id===p.player_id));
  await store(['SET',keys[1],JSON.stringify(m)]);
  await call('closed',2);assert.equal((await call('claim',2)).ok,true);
  const claimed=JSON.parse(await store(['GET',keys[1]])),auth={epoch:1,session:claimed.session_key,token:claimed.reportToken};
  await call('opened',2,auth);
  for(const i of [0,2])await call('pulse',i,{...auth,token:claimed.migration_capabilities[ids[i]],sequence:1});
  await call('seal',2,auth);
  const subset={...checkpoint(),rows:checkpoint().rows.filter(r=>[ids[0],ids[2]].includes(r.id))};
  assert.equal((await call('prepared',2,{...auth,checkpoint:subset})).ok,false,'one-sided round cannot be released');
  assert.equal((await call('adjudicated',2,{...auth,penalties:{}})).ok,true);
  const final=JSON.parse(await store(['GET',keys[1]]));
  assert.equal(final.terminal.winner,1);assert.equal(final.terminal.recovery,true);
  assert(m.teams[2].includes(final.terminal.loser));
});

test('missing observations are unknown; a healthy participant or recent native host blocks recovery',async()=>{
  await fixture();await call('checkpoint',0,{checkpoint:checkpoint()});
  assert.equal((await call('claim',1)).ok,false);
  await call('closed',1);assert.equal((await call('claim',1)).ok,false,'one claimant cannot unilaterally replace a 4-player session');
  await call('closed',2);await call('pulse',3,{sequence:1});
  assert.equal((await call('claim',1)).ok,false,'another client is still connected');
  assert.equal((await call('pulse',3,{sequence:1,now:now+61000})).ok,false,'duplicate sequence cannot renew presence');
  assert.equal((await call('closed',1,{now:now+61000})).ok,true);await call('closed',2,{now:now+61000});
  const a=JSON.parse(await store(['GET',keys[0]]));a.last_seen=now+60000;await store(['SET',keys[0],JSON.stringify(a)]);
  assert.equal((await call('claim',1,{now:now+61000})).ok,false,'native host renewed its lease');
});

test('concurrent claims choose exactly one successor and rotate credentials; old host and delayed writes are fenced',async()=>{
  await eligible();const results=await Promise.all([call('claim',1),call('claim',2)]);
  assert.equal(results.filter(r=>r.ok).length,1);
  const m=JSON.parse(await store(['GET',keys[1]])),a=JSON.parse(await store(['GET',keys[0]]));
  assert.equal(a.epoch,1);assert.equal(m.host_epoch,1);assert.equal(a.phase,'restoring');
  assert.match(m.session_key,/^chm-0123456789abcdef-r[a-f0-9]{16}$/);
  assert.equal(m.host,a.host);assert.notEqual(m.reportToken,tokens[ids.indexOf(m.host)]);
  assert.equal((await call('checkpoint',0,{checkpoint:checkpoint()})).ok,false);
  assert.equal((await call('pulse',0,{sequence:2})).ok,false);
  assert.equal((await call('claim',3)).ok,false);
});

test('exact restored boundary is required before reopening normal reports and grants only one acknowledgement',async()=>{
  await eligible();const claimed=await call('claim',1);assert.equal(claimed.ok,true);
  const m=JSON.parse(await store(['GET',keys[1]]));
  const args={epoch:1,session:m.session_key,token:m.migration_capabilities[ids[1]],checkpoint:checkpoint()};
  assert.equal((await call('restored',1,{...args,checkpoint:{...checkpoint(),scores:[0,0]}})).ok,false);
  assert.equal(JSON.parse(await store(['GET',keys[0]])).phase,'restoring');
  assert.equal((await call('restored',1,args)).ok,false,'host alone cannot unlock an unprepared world');
  await prepareRestore(m);
  assert.equal((await call('restored',1,args)).ok,true);
  assert.equal((await call('restored',1,args)).ok,true,'lost reply can be retried');
  assert.equal((await call('prepared',1,args)).ok,false,'a fresh world cannot replay a completed restore');
  assert.equal(JSON.parse(await store(['GET',keys[0]])).phase,'playing');
});

test('a retry rotates the preparation handshake and expired restoration closes despite fresh host health',async()=>{
  await eligible();await call('claim',1);let m=JSON.parse(await store(['GET',keys[1]]));
  await prepareRestore(m);
  const later=now+recovery.REJOIN_WINDOW+recovery.RESTORE_WINDOW+1;
  for(const who of [0,2])await call('closed',who,{epoch:1,session:m.session_key,token:m.migration_capabilities[ids[who]],now:later});
  assert.equal((await call('claim',2,{epoch:1,session:m.session_key,token:m.migration_capabilities[ids[2]],now:later})).ok,true);
  m=JSON.parse(await store(['GET',keys[1]]));
  assert.equal(JSON.parse(await store(['GET',keys[3]])).prepared,undefined);
  for(const who of [0,2])await call('pulse',who,{epoch:2,session:m.session_key,token:m.migration_capabilities[ids[who]],now:later,sequence:1});
  assert.equal((await call('restored',2,{epoch:2,session:m.session_key,token:m.reportToken,now:later,checkpoint:checkpoint()})).ok,false);
  const expire=(epoch,at)=>store(['EVAL',recovery.EXPIRE,'2',keys[0],keys[2],m.host,String(epoch),String(at),'86400']);
  assert.equal(await expire(1,later+recovery.RESTORE_WINDOW),0,'old epoch cannot close a successor');
  assert.equal(await expire(2,later),0,'deadline has not elapsed');
  const a=JSON.parse(await store(['GET',keys[0]]));a.last_seen=later+recovery.RESTORE_WINDOW;
  await store(['SET',keys[0],JSON.stringify(a)]);
  assert.equal(await expire(2,later+recovery.RESTORE_WINDOW),1);
  assert.equal((await call('status',2,{epoch:2,session:m.session_key,token:m.reportToken,now:later+recovery.RESTORE_WINDOW})).ok,false);
});

test('settlement and closed authority prevent recovery; storage failures cannot acknowledge a save',async()=>{
  await eligible();await store(['SET',keys[2],'{}']);assert.equal((await call('claim',1)).ok,false);
  await eligible();const a=JSON.parse(await store(['GET',keys[0]]));a.closed=true;await store(['SET',keys[0],JSON.stringify(a)]);
  assert.equal((await call('claim',1)).ok,false);
  await fixture();await assert.rejects(recovery.execute(async()=>{throw Error('offline');},keys,{operation:'checkpoint',player:ids[0],token:tokens[0],epoch:0,session:'chm-0123456789abcdef',checkpoint:checkpoint(),now}),/offline/);
  assert.equal(await store(['GET',keys[3]]),null);
});

test('recovery fences settlement and periodic snapshots until verified, including same-epoch stale workers',async()=>{
  await eligible();await call('claim',1);
  const m=JSON.parse(await store(['GET',keys[1]]));
  const settlement=require('../settlement.cjs');
  await assert.rejects(settlement.commit(store,[keys[2],'t:board',keys[1],'t:index','t:outbox',keys[0]],
    {matchId:m.id,host:m.host,host_epoch:1},[]),'cannot award a result during restore');
  const stale=JSON.stringify(m);
  await prepareRestore(m);
  await call('restored',1,{epoch:1,session:m.session_key,token:m.reportToken,checkpoint:checkpoint()});
  await assert.rejects(settlement.snapshot(store,[keys[2],keys[1],'t:index',keys[0]],m.id,stale,86400),
    'a queued same-epoch snapshot cannot reset a completed restore');
});

test('live service authenticates independent participants and updates host payload and launch permission on claim',async t=>{
  const m=await fixture();m.assigned_teams=structuredClone(m.teams);m.deadline=Date.now()+3600000;
  const L=require('../live.cjs').create({upstashCmd:store,prefix:'integration:'});await L._internals.ready;
  t.after(()=>L.shutdown());
  assert.equal(typeof L.recoveryReport,'function','game recovery reports must be integrated');
  assert.equal(typeof L.recoveryAction,'function','companion recovery actions must be integrated');
  L._internals.matches.set(m.id,m);ids.forEach(id=>L._internals.inMatch.set(id,m.id));
  await L._internals.flushMatches();
  const authKey=`integration:live:authority:${m.id}`;
  const a=JSON.parse(await store(['GET',authKey]));a.last_seen=Date.now()-61000;await store(['SET',authKey,JSON.stringify(a)]);
  const scope={match_id:m.id,epoch:0,session:`chm-${m.id}`};
  assert.equal((await L.recoveryReport(tokens[0],{...scope,operation:'checkpoint',user_id:ids[0],checkpoint:checkpoint()})).ok,true);
  assert.equal((await L.recoveryReport(tokens[1],{...scope,operation:'pulse',user_id:ids[2],sequence:1})).ok,false,'token cannot impersonate another player');
  for(const who of [1,2])assert.equal((await L.recoveryAction(ids[who],{...scope,operation:'closed'})).ok,true);
  assert.equal((await L.recoveryAction('76561198000000099',{...scope,operation:'claim'})).ok,false);
  const result=await L.recoveryAction(ids[1],{...scope,operation:'claim'});assert.equal(result.ok,true);
  assert.equal(m.host,ids[1]);assert.equal(m.host_epoch,1);assert.equal(L.takeHostPermit(ids[1]),true);
  assert.equal(result.match.session_key,m.session_key);assert.equal(result.match.recovery.checkpoint.round,3);
  assert.equal(await L.authoriseReportFresh(tokens[0]),null);
  const auth=await L.authoriseReportFresh(m.reportToken);let mutated=false;
  await assert.rejects(L.withReportAuthority(auth,()=>{mutated=true;}));assert.equal(mutated,false);
  assert.equal((await L.matchPresence(ids[1],m.id,ids.map(steam_id=>({steam_id,active:1})))).ok,false,'no reconnect penalties during restore');
  const restoreScope={match_id:m.id,epoch:1,session:m.session_key,user_id:ids[1],checkpoint:checkpoint()};
  assert.equal((await L.gameReportedIn(ids[1],'ch_lobby_read')).ok,true);
  for(const who of [0,1,2,3])assert.equal((await L.recoveryReport(m.migration_capabilities[ids[who]],
    {...restoreScope,user_id:ids[who],operation:'pulse',sequence:1})).ok,true);
  assert.equal((await L.recoveryReport(m.reportToken,{...restoreScope,operation:'prepared'})).ok,true);
  assert.equal((await L.recoveryReport(m.reportToken,{...restoreScope,operation:'restored'})).ok,true);
  m.recovery.phase='restoring'; // stale same-epoch local state after another worker's commit
  assert.equal((await L.recoveryReport(m.reportToken,{...restoreScope,operation:'restored'})).ok,true);
  assert.equal(m.recovery.phase,'playing','authority refresh hydrates a phase change without an epoch change');
});

test('wire protocol binds session, version, decimal fields and participant identity without accepting caller time',()=>{
  assert.equal(typeof recovery.parseReport,'function','bounded game transport must be implemented');
  const good={event_name:'ch_recovery_checkpoint',first_session_timestamp:'chrecovery-1',storefront:'chm-0123456789abcdef',
    user_id:ids[0],timestamp:'3;7;2;1;1',platform:ids.map((id,i)=>`${id}:${i%2}:2:1:3`).join(';'),now:now+100000000};
  const parsed=recovery.parseReport(good,0);
  assert.deepEqual(parsed.checkpoint,checkpoint());assert.equal(parsed.now,undefined);
  for(const change of [{timestamp:'3x;7;2;1;1'},{timestamp:'3;7;2;1'},{platform:good.platform+';'},{user_id:'x'},
    {first_session_timestamp:'chrecovery-2'},{storefront:good.storefront+'-invalid'}])
    assert.equal(recovery.parseReport({...good,...change},0),null);
});

test('claim removes interrupted-round accounting while retaining completed history',async()=>{
  await eligible();const m=JSON.parse(await store(['GET',keys[1]]));
  m.rounds=[{won:1,steps:2,1:2,2:0},{won:2,steps:1,1:2,2:1},{won:1,steps:1,1:3,2:1}];
  m.kills=[{round:2,killer:ids[0],victim:ids[1]},{round:3,killer:ids[1],victim:ids[0]}];
  m.lastAlive={round:3,a0:0,a1:1};m.game_scores={0:3,1:1};
  m.round_reports={2:{round:2},3:{round:3}};
  await store(['SET',keys[1],JSON.stringify(m)]);
  assert.equal((await call('claim',1)).ok,true);
  const saved=JSON.parse(await store(['GET',keys[1]]));
  assert.equal(saved.rounds.length,2);assert.equal(saved.kills.length,1);assert.equal(saved.kills[0].round,2);
  assert.equal(saved.lastAlive,undefined);assert.equal(saved.game_scores,undefined);
  assert.deepEqual(saved.round_reports,{2:{round:2}});assert.deepEqual(saved.score,{'1':2,'2':1});
});

test('old reconnect time does not shorten the fresh replacement return window',async()=>{
  await eligible();let m=JSON.parse(await store(['GET',keys[1]]));
  m.reconnect={[ids[3]]:{since:now-280000,deadline:now+20000}};
  await store(['SET',keys[1],JSON.stringify(m)]);await call('claim',1);
  m=JSON.parse(await store(['GET',keys[1]]));assert.deepEqual(m.reconnect,{});
  await prepareRestore(m);
  assert.equal((await call('restored',1,{epoch:1,session:m.session_key,token:m.reportToken,checkpoint:checkpoint(),now:now+10000})).ok,true);
  m=JSON.parse(await store(['GET',keys[1]]));
  assert.deepEqual(m.reconnect,{});assert.equal(m.recovery.rejoin_until,now+300000);
});

test('service deadline removes and penalizes absent players once even when ordinary no-show penalties are paused',async t=>{
  const originalClock=Date.now,oldPaused=process.env.COMP_NO_SHOW_PENALTIES_PAUSED;
  let clock=now;Date.now=()=>clock;process.env.COMP_NO_SHOW_PENALTIES_PAUSED='1';
  t.after(()=>{Date.now=originalClock;if(oldPaused===undefined)delete process.env.COMP_NO_SHOW_PENALTIES_PAUSED;else process.env.COMP_NO_SHOW_PENALTIES_PAUSED=oldPaused;});
  const m=await fixture();m.assigned_teams=structuredClone(m.teams);m.deadline=now+3600000;
  const L=require('../live.cjs').create({upstashCmd:store,prefix:'absence:'});await L._internals.ready;t.after(()=>L.shutdown());
  L._internals.matches.set(m.id,m);ids.forEach(id=>L._internals.inMatch.set(id,m.id));await L._internals.flushMatches();
  const ak=`absence:live:authority:${m.id}`;const a=JSON.parse(await store(['GET',ak]));a.last_seen=now-61000;await store(['SET',ak,JSON.stringify(a)]);
  const scope={match_id:m.id,epoch:0,session:'chm-'+m.id};
  await L.recoveryReport(tokens[0],{...scope,operation:'checkpoint',user_id:ids[0],checkpoint:checkpoint()});
  for(const i of [1,2])await L.recoveryAction(ids[i],{...scope,operation:'closed'});
  assert.equal((await L.recoveryAction(ids[1],{...scope,operation:'claim'})).ok,true);
  clock+=45000;assert.equal((await L.gameReportedIn(ids[1],'ch_lobby_read')).ok,true);
  const rs={match_id:m.id,epoch:1,session:m.session_key};
  clock=m.recovery.rejoin_until-1000;
  for(const i of [1,2])await L.recoveryReport(m.migration_capabilities[ids[i]],{...rs,operation:'pulse',sequence:1,user_id:ids[i]});
  clock+=1000;await L._internals.checkHostTimeouts();
  assert.deepEqual(m.players.map(p=>p.player_id),[ids[1],ids[2]]);
  assert.equal(L.takeJoinPermit(ids[0]),false);
  assert.equal(L.teamRuling(ids[1],ids[0],'stranger').yes,true);
  const pk=`absence:reconnect:penalty:${m.id}:${ids[0]}`;
  const receipt=await store(['GET',pk]);assert.equal(JSON.parse(receipt).count,1);
  assert.equal((await L.recoveryAction(ids[0],{...rs,operation:'status'})).ok,false);
  clock+=11000;await L._internals.checkHostTimeouts();
  assert.equal(await store(['GET',pk]),receipt);
});
