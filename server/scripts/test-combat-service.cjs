'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const live=require('../live.cjs');
const ids=Array.from({length:4},(_,i)=>`7656119800000000${i+1}`);
function setup(t,validated=true,store=null) {
  for(const [key,value] of Object.entries({COMP_COMBAT_VALIDATED_COLLECTORS:validated?'chcombat-1':'',COMP_TK_ENFORCE:'1',COMP_QUEUE_PENALTIES_PAUSED:'0'})) {
    const old=process.env[key];process.env[key]=value;t.after(()=>old===undefined?delete process.env[key]:process.env[key]=old);
  }
  const svc=live.create({upstashCmd:store});t.after(()=>svc.shutdown());const L=svc._internals;
  const match={id:'combat-test',host:ids[0],state:'live',created:Date.now(),players:ids.map(steam_id=>({steam_id,connected:true})),
    teams:{1:ids.slice(0,2),2:ids.slice(2)},ingame:new Map(ids.map((id,i)=>[id,i<2?0:1])),team_sort_verified:true,left:[]};
  L.matches.set(match.id,match);ids.forEach(id=>L.inMatch.set(id,match.id));
  L.ratings.set(ids[0],{...L.rating.normalise({}),matches:20,progress:1000});
  let seq=0;
  const send=(fields={})=>svc.gameReportedCombat(ids[0],{match:match.id,row:{v:1,seq:++seq,n:1,t:seq,phase:'Game.Phase.StartRound',...fields}},{authenticated:true});
  const coverage=()=>send({kind:'coverage',epoch:'test',observer:'chcombat-1',roster:4,bound:4,gaps:0,damage:1,shots:0,objectives:0,complete:0});
  const damage=(round=1)=>send({kind:'health',n:round,a:ids[0],b:ids[1],at:0,bt:0,old:100,new:0,max:100});
  return {svc,L,match,send,coverage,damage};
}
test('legacy kill reports cannot classify or punish intent from an early kill',async t=>{
  const {svc,L}=setup(t);
  const out=svc.teamKillReported(ids[0],{killer:ids[0],victim:ids[1],team:0,elapsed:1,round:1,alive0:2,alive1:0});
  assert.equal(out.malicious,false);assert.equal(out.enforced,false);assert.equal(L.penalties.size,0);
});

test('a replayed named kill cannot inflate the scoreboard or legacy rating feed after restore',async t=>{
  const {svc,L,match}=setup(t);
  const report={match:match.id,row:`k=${ids[0]};v=${ids[2]};n=1;t=22;a0=2;a1=1`};
  assert.equal(svc.gameReportedKill(ids[0],report).kills,1);
  L.matches.set(match.id,L.reviveMatch(JSON.parse(JSON.stringify(L.serialiseMatch(match)))));
  assert.equal(svc.gameReportedKill(ids[0],report).duplicate,true);
  assert.equal(L.matches.get(match.id).kills.length,1);
});
test('unvalidated collector remains visible but cannot affect penalties or ratings',async t=>{
  const {L,match,coverage,damage}=setup(t,false);await coverage();for(let n=1;n<=4;n++)await damage(n);
  assert.equal(L.penalties.size,0);const row=L.scoreboardOf(match).find(r=>r.steam_id===ids[0]);
  assert.equal(row.combat.friendlyDamage,400);assert.equal(row.combat.status,'partial');assert.equal(row.combat.ratingsEligible,false);
});
test('repeated independent confirmed harm is charged once and records team_kill',async t=>{
  const {L,match,coverage,damage,svc}=setup(t);await coverage();await damage(1);await damage(2);const out=await damage(3);
  assert.equal(out.enforced,true);assert.equal(L.penalties.get(ids[0]).reason,'team_kill');assert.equal(L.penalties.get(ids[0]).count,1);
  const before=L.ratingOf(ids[0]).progress;
  const event=match.combatState.events.at(-1);const {loss,relation,...wire}=event;
  await svc.gameReportedCombat(ids[0],{match:match.id,row:wire},{authenticated:true});
  assert.equal(L.penalties.get(ids[0]).count,1);assert.equal(L.ratingOf(ids[0]).progress,before);
});
test('unauthenticated, wrong-match, connecting and mismatched teams do not punish',async t=>{
  const {svc,match,L,coverage,damage}=setup(t);await coverage();
  assert.equal((await svc.gameReportedCombat(ids[0],{match:'other',row:''},{authenticated:true})).ok,false);
  assert.equal((await svc.gameReportedCombat(ids[0],{match:match.id,row:''})).ok,false);
  match.state='connecting';await damage(1);match.state='live';match.ingame.set(ids[1],1);await damage(2);
  assert.equal(L.penalties.size,0);
});
test('paused penalties retain observations without strikes or deductions',async t=>{
  const {L,coverage,damage}=setup(t);process.env.COMP_QUEUE_PENALTIES_PAUSED='1';await coverage();for(let n=1;n<=4;n++)await damage(n);
  assert.equal(L.penalties.size,0);assert.equal(L.ratingOf(ids[0]).progress,1000);
});

test('malicious harm deducts RR and blocks queueing while no-show penalties stay paused',async t=>{
  const {L,coverage,damage}=setup(t);
  const old=process.env.COMP_NO_SHOW_PENALTIES_PAUSED;
  process.env.COMP_NO_SHOW_PENALTIES_PAUSED='1';
  t.after(()=>old===undefined?delete process.env.COMP_NO_SHOW_PENALTIES_PAUSED:process.env.COMP_NO_SHOW_PENALTIES_PAUSED=old);
  const before={...L.ratingOf(ids[0])};
  await coverage();await damage(1);await damage(2);const result=await damage(3);
  assert.equal(result.enforced,true);
  assert.equal(L.ratingOf(ids[0]).progress,985);
  for(const field of ['rating','rd','vol','matches','wins','losses','updated'])
    assert.equal(L.ratingOf(ids[0])[field],before[field]);
  assert.equal((await L.loadPenalty(ids[0])).reason,'team_kill');
  assert.equal(L.applyNoShow(ids[2]),null);
  assert.equal(L.penalties.has(ids[2]),false);
});

test('repeated lethal overkill is sanctioned once without charging excess damage',async t=>{
  const {L,coverage,send,match,svc}=setup(t);await coverage();
  for(let n=1;n<=3;n++) await send({kind:'health',n,a:ids[0],b:ids[1],at:0,bt:0,old:100,new:-35,max:100});
  assert.equal(L.penalties.get(ids[0])?.count,1);
  assert.equal(L.ratingOf(ids[0]).progress,985);
  assert.equal(L.scoreboardOf(match).find(r=>r.steam_id===ids[0]).combat.friendlyDamage,300);
  const {loss,relation,...wire}=match.combatState.events.at(-1);
  await svc.gameReportedCombat(ids[0],{match:match.id,row:wire},{authenticated:true});
  assert.equal(L.penalties.get(ids[0]).count,1);
  assert.equal(L.ratingOf(ids[0]).progress,985);
});

test('opening coverage survives connecting and cannot itself punish',async t=>{
  const {match,coverage,damage,L}=setup(t);match.state='connecting';
  assert.equal((await coverage()).accepted,true);assert.equal(L.penalties.size,0);
  match.state='live';await damage();assert.equal(match.combatState.coverage.broken,false);
});

test('final stats wait for the combat closing marker within the existing deadline',async t=>{
  const {match,coverage,L}=setup(t);await coverage();
  match.collecting={since:Date.now()-1000,score:{1:2,2:1}};
  match.stats={seenAt:Object.fromEntries(ids.map(id=>[id,Date.now()])),seenRounds:Object.fromEntries(ids.map(id=>[id,3]))};
  assert.equal(L.collectionComplete(match),false);
  match.combatState.coverage.closed=true;assert.equal(L.collectionComplete(match),true);
  match.collecting=null;
});

// Inject failures at the storage boundary; test-combat-ledger-lua.py executes
// the production transaction itself against Redis/Lua semantics separately.
function storage() {
  const values=new Map();
  const db=async cmd=>{
    const [op,key]=cmd;
    if(op==='GET')return values.get(key)??null;
    if(op==='SMEMBERS')return [];
    if(op==='EVAL'&&key.startsWith('-- penalty-write-v1')) {
      if(db.legacyGate) await db.legacyGate;
      const p=JSON.parse(cmd[5]);values.set(cmd[3],p.json);return ['written',p.json];
    }
    if(op==='EVAL'&&key.startsWith('-- rank-write-v1')) {
      const [row]=JSON.parse(cmd[3+Number(cmd[2])]);values.set(cmd[3],row.json);return ['written',row.json];
    }
    if(op==='EVAL'&&key.startsWith('-- combat-ledger-v1')) {
      const keys=cmd.slice(3,3+Number(cmd[2])),p=JSON.parse(cmd[3+Number(cmd[2])]);
      if(db.beforeCommit) await db.beforeCommit(p,keys);
      if(p.receipt&&values.has(keys[4])) return ['replayed',values.get(keys[0]),values.get(keys[4])];
      values.set(keys[0],p.historyJson);
      if(p.receipt) {values.set(keys[1],p.rankJson);values.set(keys[2],p.penaltyJson);values.set(keys[4],p.receiptJson);}
      if(p.receipt&&db.loseReply) {db.loseReply=false;throw new Error('reply lost');}
      return ['committed',p.historyJson,p.receiptJson||''];
    }
    if(op==='EVAL'&&key.startsWith('-- rank-snapshot-v1')) return ['saved'];
    return null;
  };
  db.values=values;return db;
}
test('lost commit reply retries without double charge and refreshes the durable penalty',async t=>{
  const db=storage(),{L,coverage,damage,svc,match}=setup(t,true,db);await L.ready;
  db.values.set('hub:rating:'+ids[0],JSON.stringify(L.ratingOf(ids[0])));
  await coverage();await damage(1);await damage(2);db.loseReply=true;
  await assert.rejects(damage(3),/reply lost/);
  const persisted=JSON.parse(db.values.get('hub:penalty:'+ids[0]));
  assert.equal(persisted.count,1);assert.equal(persisted.reason,'team_kill');
  const {loss,relation,...row}=match.combatState.events.at(-1);
  await svc.gameReportedCombat(ids[0],{match:match.id,row},{authenticated:true});
  assert.equal(JSON.parse(db.values.get('hub:penalty:'+ids[0])).count,1);
  assert.equal((await L.loadPenalty(ids[0])).count,1);
  assert.equal((await L.loadRating(ids[0])).progress,JSON.parse(db.values.get('hub:rating:'+ids[0])).progress);
});
test('historical receipt replay cannot replace a newer sanction cache',async t=>{
  const db=storage(),{L,coverage,damage}=setup(t,true,db);await L.ready;
  db.values.set('hub:rating:'+ids[0],JSON.stringify(L.ratingOf(ids[0])));
  await coverage();await damage(1);await damage(2);
  db.beforeCommit=async(p,keys)=>{
    if(!p.receipt)return;
    db.values.set(keys[4],p.receiptJson);db.values.set(keys[0],p.historyJson);
    db.values.set(keys[1],JSON.stringify({...JSON.parse(p.rankJson),revision:9,progress:900}));
    db.values.set(keys[2],JSON.stringify({...JSON.parse(p.penaltyJson),count:4,until:Date.now()+999999}));
  };
  await damage(3);assert.equal(L.penalties.get(ids[0]).count,4);assert.equal(L.ratingOf(ids[0]).progress,900);
});
test('queue penalty reads observe another instance and durable removal',async t=>{
  const db=storage(),{L}=setup(t,true,db);await L.ready;
  assert.equal(await L.loadPenalty(ids[0]),null);
  db.values.set('hub:penalty:'+ids[0],JSON.stringify({reason:'team_kill',until:Date.now()+60000,last:Date.now(),count:2}));
  assert.equal((await L.loadPenalty(ids[0])).count,2);
  db.values.delete('hub:penalty:'+ids[0]);assert.equal(await L.loadPenalty(ids[0]),null);
});

test('queued pre-warning attacks never gain intent evidence from later delivery',async t=>{
  const {L,match,coverage,send,svc}=setup(t);await coverage();
  // Three friendly victims, all on the same roster side; no persistent targeting
  // across rounds or heavy repeated-harm corroboration.
  match.teams={1:ids,2:[]};match.ingame=new Map(ids.map(id=>[id,0]));
  // Replace opening with this roster before recording any harm.
  match.combatState=null;
  await svc.gameReportedCombat(ids[0],{match:match.id,row:{v:1,seq:1,kind:'coverage',n:1,t:0,phase:'combat',
    epoch:'test',observer:'chcombat-1',roster:4,bound:4,gaps:0,damage:1,shots:0,objectives:0,complete:0}},{authenticated:true});
  for(let i=1;i<=3;i++) {
    const out=await svc.gameReportedCombat(ids[0],{match:match.id,row:{v:1,seq:i+1,kind:'health',n:1,t:i*20,phase:'combat',
      a:ids[0],b:ids[i],at:0,bt:0,old:100,new:40,max:100}},{authenticated:true});
    if(i===1) { const h=L.combatHistories.get(ids[0]);h.warning.ackAt=Date.now()-1; }
    assert.equal(out.enforced,false);
  }
  assert.equal(L.penalties.size,0);
});

test('a pending older penalty write finishes before the combat strike reads the ladder',async t=>{
  const db=storage(),{L,coverage,damage}=setup(t,true,db);await L.ready;
  db.values.set('hub:rating:'+ids[0],JSON.stringify(L.ratingOf(ids[0])));
  let release;db.legacyGate=new Promise(resolve=>{release=resolve;});
  L.applyNoShow(ids[0]);await coverage();await damage(1);await damage(2);
  let done=false;const result=damage(3).then(out=>{done=true;return out;});
  await new Promise(resolve=>setImmediate(resolve));assert.equal(done,false);
  release();assert.equal((await result).enforced,true);
  const penalty=JSON.parse(db.values.get('hub:penalty:'+ids[0]));
  assert.equal(penalty.reason,'team_kill');assert.equal(penalty.count,2);
});
