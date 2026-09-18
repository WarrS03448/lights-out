'use strict';
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const path=require('node:path');
const migration=require('../migration.cjs');
const live=require('../live.cjs');
let bridge,store;
before(async()=>{
  bridge=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')],{stdio:['pipe','pipe','pipe']});
  let n=0;const pending=new Map();
  readline.createInterface({input:bridge.stdout}).on('line',line=>{
    const r=JSON.parse(line),p=pending.get(r.id);pending.delete(r.id);
    if(p)r.error?p.reject(Error(r.error)):p.resolve(r.result);
  });
  bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('test store stopped'));pending.clear();});
  store=command=>new Promise((resolve,reject)=>{const id=++n;pending.set(id,{resolve,reject});bridge.stdin.write(JSON.stringify({id,command})+'\n');});
  assert.equal(await store(['PING']),'PONG');
});
after(()=>bridge?.kill());

async function fixture(t,serviceStore=store) {
  await store(['FLUSHALL']);
  const L=live.create({upstashCmd:serviceStore}); await L._internals.ready;
  const ids=['76561198000000001','76561198000000002','76561198000000003','76561198000000004'];
  const teams={1:[ids[0],ids[2]],2:[ids[1],ids[3]]};
  const m={id:'0123456789abcdef',state:'live',host:ids[0],players:ids.map(player_id=>({player_id,game_steam_id:player_id,connected:true})),
    teams,assigned_teams:structuredClone(teams),left:[],start_ready_verified:true,expected_score_limit:7,expected_max_rounds:12,
    deadline:Date.now()+3600000,created:Date.now(),map:'Rome',score:{1:2,2:1}};
  L._internals.matches.set(m.id,m);ids.forEach(id=>L._internals.inMatch.set(id,m.id));
  const payloads=ids.map(id=>L._internals.connectPayload(m,id));
  await L._internals.flushMatches();
  const keys=[`hub:live:authority:${m.id}`,L._internals.liveMatchKey(m.id),L._internals.settlementKey(m.id)];
  t.after(async()=>{await L.shutdown();clearTimeout(m.timer);clearTimeout(m.collectTimer);});
  return {L,m,ids,keys,payloads};
}

test('only the endorsed participant can activate after the host lease expires; retries and stale workers are fenced',async t=>{
  const {L,m,ids,keys,payloads}=await fixture(t);
  assert.equal(payloads[1].report_token,undefined);
  assert.equal(L.authoriseReport(payloads[1].migration_token),null);
  const old=JSON.stringify(L._internals.serialiseMatch(m));
  const call=(who,operation,epoch,candidate='',now=Date.now())=>migration.transition(store,keys,
    {player:ids[who],token:payloads[who].migration_token,operation,epoch,candidate,now});
  const now=Date.now();
  assert.equal((await call(0,'endorse',0,ids[1],now)).ok,true);
  assert.equal((await call(2,'activate',1,'',now+11000)).ok,false);
  assert.equal((await call(1,'activate',1,'',now+9999)).ok,false);
  const [first,again]=await Promise.all([call(1,'activate',1,'',now+11000),call(1,'activate',1,'',now+11000)]);
  assert.equal(first.ok,true);assert.equal(again.ok,true);
  const saved=JSON.parse(await store(['GET',keys[1]]));
  assert.equal(saved.host,ids[1]);assert.equal(saved.host_epoch,1);assert.equal(saved.host_migrations.length,1);
  assert.equal(saved.reconnect[ids[0]].deadline,now+300000);
  assert.deepEqual(saved.assigned_teams,m.assigned_teams);assert.ok(Array.isArray(saved.left));
  await assert.rejects(require('../settlement.cjs').snapshot(store,[keys[2],keys[1],L._internals.liveIndexKey(),keys[0]],m.id,old,86400));
  assert.equal(await L.authoriseReportFresh(payloads[0].report_token),null);
  assert.equal((await L.authoriseReportFresh(payloads[1].migration_token)).playerId,ids[1]);
  assert.equal((await call(0,'endorse',0,ids[2],now+12000)).ok,false);
  assert.equal((await L.matchPresence(ids[1],m.id,ids.map(steam_id=>({steam_id,active:1})))).ok,true);
  assert.deepEqual(m.reconnect,{});
});

test('a restored match settles normal ranked progress and retains the returning original host',async t=>{
  const {L,m,ids,keys,payloads}=await fixture(t);
  for(const id of ids)await store(['SET',`hub:rating:${id}`,JSON.stringify(require('../rating.cjs').normalise({matches:20,progress:500}))]);
  const now=Date.now();
  assert.equal((await migration.transition(store,keys,{operation:'endorse',player:ids[0],token:payloads[0].migration_token,epoch:0,candidate:ids[1],now:now-11000})).ok,true);
  assert.equal((await L.migrationReport(payloads[1].migration_token,{match_id:m.id,operation:'activate',epoch:1,candidate:''})).ok,true);
  assert.equal(m.host,ids[1]);
  assert.equal((await L.matchPresence(ids[1],m.id,ids.map(steam_id=>({steam_id,active:1})))).ok,true);
  m.combat_end={epoch:'restored',seq:1,complete:false};
  m.score={1:7,2:2};
  const auth=await L.authoriseReportFresh(payloads[1].migration_token);
  const fields={match_id:m.id,meta:'9;7;0;7;1;2',combat_end:'restored;1',
    rows:ids.map((id,i)=>`${id}|k=3;d=2;sp=9;t=${i%2};s=${i%2?2:7};a=false`).join(',')};
  assert.equal((await L.withReportAuthority(auth,()=>L.finalSnapshot(ids[1],fields))).ok,true);
  const oldHost=await L.completion(ids[0],m.id);
  assert.equal(oldHost.data_collected,true);
  assert.equal(oldHost.result.won,true);
  assert.ok(oldHost.result.rr_delta>0,'restoration must not disable ranked RR');
  assert.equal(JSON.parse(await store(['GET',`hub:rating:${ids[0]}`])).matches,21);
});

test('stale deletion and conduct evidence cannot cross the handoff fence',async t=>{
  const {L,m,ids,keys,payloads}=await fixture(t),now=Date.now();
  await migration.transition(store,keys,{operation:'endorse',player:ids[0],token:payloads[0].migration_token,epoch:0,candidate:ids[1],now:now-11000});
  await migration.transition(store,keys,{operation:'activate',player:ids[1],token:payloads[1].migration_token,epoch:1,now});
  assert.equal(await store(['EVAL',migration.CLOSE,'2',keys[0],keys[2],ids[0],'0',String(now),'0','86400']),0);
  assert.equal(await store(['EVAL',migration.FORGET,'4',keys[1],L._internals.liveIndexKey(),keys[0],keys[2],m.id]),0);
  assert.ok(await store(['GET',keys[1]]));
  await assert.rejects(require('../combat-ledger.cjs').commit(store,'hub:',ids[2],{
    authority:{match:m.id,host:ids[0],epoch:0},expected:0,history:{revision:1,incidents:[]}}));
  assert.equal(await store(['GET',`hub:combat:history:${ids[2]}`]),null);
  const originalAuth={matchId:m.id,playerId:ids[0],steamId:ids[0],epoch:0};
  let mutated=false;
  await assert.rejects(L.withReportAuthority(originalAuth,()=>{mutated=true;}));
  assert.equal(mutated,false);
});

test('an unavailable host without a successor releases the match after five minutes',async t=>{
  const {L,m,ids,keys,payloads}=await fixture(t);
  await migration.transition(store,keys,{operation:'endorse',player:ids[0],token:payloads[0].migration_token,epoch:0,candidate:'',now:Date.now()-300001});
  m.collecting={winner:1,score:{1:7,2:2},since:Date.now()-300001};
  await L._internals.checkHostTimeouts();
  assert.equal(L._internals.matches.has(m.id),false);
  for(const id of ids)assert.equal(L._internals.inMatch.has(id),false);
  assert.equal(JSON.parse(await store(['GET',keys[0]])).closed,true);
});

test('an old queued timeout cannot close the newly promoted host',async t=>{
  let release,entered;
  const waiting=new Promise(r=>{entered=r;});
  const gate=new Promise(r=>{release=r;});
  const gatedStore=async(cmd,...args)=>{
    if(cmd[0]==='EVAL'&&cmd[1]===migration.SCRIPT&&cmd[6]==='activate') {entered();await gate;}
    return store(cmd,...args);
  };
  const {L,m,ids,keys,payloads}=await fixture(t,gatedStore);
  await migration.transition(store,keys,{operation:'endorse',player:ids[0],token:payloads[0].migration_token,epoch:0,candidate:ids[1],now:Date.now()-11000});
  const activation=L.migrationReport(payloads[1].migration_token,{match_id:m.id,operation:'activate',epoch:1,candidate:''});
  await waiting;
  const closure=L._internals.closeMatch(m,'stalled',[]);
  release();
  assert.equal((await activation).ok,true);
  await closure;
  assert.equal(L._internals.matches.get(m.id),m);
  assert.equal(m.host,ids[1]);
  assert.equal(JSON.parse(await store(['GET',keys[0]])).closed,undefined);
});
