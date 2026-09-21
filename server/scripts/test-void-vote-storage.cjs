// UTF-8. Run with ACCOUNT_TEST_PYTHON pointing to Python with fakeredis[lua].
'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const path=require('node:path');
const live=require('../live.cjs');

test('real Lua preserves votes across restart, commits one void and protects ranks from late reports',async t=>{
  const bridge=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')],{stdio:['pipe','pipe','pipe']});
  t.after(()=>bridge.kill());let seq=0;
  const pending=new Map();
  readline.createInterface({input:bridge.stdout}).on('line',line=>{
    const row=JSON.parse(line),p=pending.get(row.id);pending.delete(row.id);
    if(p)row.error?(t.diagnostic(row.error),p.reject(Error(row.error))):p.resolve(row.result);
  });
  bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('test store stopped'));pending.clear();});
  const rawStore=args=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});bridge.stdin.write(JSON.stringify({id,command:args})+'\n');});
  let blockResult=false,blockedCommand;
  const store=async args=>{
    if(blockResult&&args[0]==='EVAL'&&args[1].includes('local p = cjson.decode(ARGV[1])')){
      blockedCommand=args;throw Error('temporary result storage failure');
    }
    return rawStore(args);
  };
  const ids=Array.from({length:10},(_,i)=>String(76561198000000001n+BigInt(i)));
  const options={upstashCmd:store,whoami:async token=>({player_id:token,game_steam_id:token}),bearer:req=>req.token,
    readBody:async req=>Buffer.from(JSON.stringify(req.body)),sendJson:(res,status,body)=>Object.assign(res,{status,body})};
  let L=live.create(options);await L._internals.ready;
  const match={id:'0123456789abcdef',state:'live',host:ids[0],map:'Rome',created:Date.now(),
    players:ids.map(steam_id=>({steam_id,accepted:true,connected:true})),left:[],
    teams:{1:ids.slice(0,5),2:ids.slice(5)},start_ready_verified:true,
    reportToken:'ab'.repeat(32),deadline:Date.now()+60000};
  L._internals.matches.set(match.id,match);
  for(const id of ids)L._internals.inMatch.set(id,match.id);
  const post=async (i,yes=true)=>{const res={};await L.route({headers:{},token:ids[i],body:{match_id:match.id,yes}},res,'POST','/api/match/void-vote');return res;};
  for(let i=0;i<6;i++)assert.equal((await post(i)).status,200);
  assert.equal((await L.completion(ids[0],match.id)).close_allowed,false);
  await L.shutdown();L=live.create(options);await L._internals.ready;
  t.after(()=>L.shutdown());
  assert.equal((await post(0)).body.vote.yes,6);
  const rankKey='hub:rating:'+ids[0],rankBefore=JSON.stringify({rating:1280,progress:240,matches:20,wins:11,losses:9,revision:4});
  await store(['SET',rankKey,rankBefore]);
  const before=await store(['KEYS','hub:rating:*']);
  const staleSnapshot=await store(['GET','hub:live:match:'+match.id]);
  blockResult=true;
  const replies=await Promise.all([post(6),post(6),post(7),post(8)]);
  assert.ok(replies.every(r=>r.status===503),JSON.stringify(replies));
  assert.equal((await L.completion(ids[9],match.id)).close_allowed,false);
  const settledKey=L._internals.settlementKey(match.id);
  const kept=await require('../settlement.cjs').snapshot(store,
    [settledKey,'hub:live:match:'+match.id,'hub:live:matches'],match.id,staleSnapshot,86400);
  assert.ok(kept?.pendingMatch?.void_pending,'a stale worker cannot erase a saved seven-vote decision');
  const scored=blockedCommand.slice(),plan=JSON.parse(scored.at(-1));
  const competing=JSON.parse(plan.receipt_json);competing.voided=false;
  plan.receipt_json=JSON.stringify(competing);scored[scored.length-1]=JSON.stringify(plan);
  await assert.rejects(rawStore(scored),/void decision/,'a stale score commit cannot beat a saved void');
  blockResult=false;
  await L._internals.flushMatches();
  const receipt=await L.completion(ids[9],match.id);
  assert.equal(receipt.close_allowed,true);assert.equal(receipt.result.voided,true);
  assert.deepEqual(await store(['KEYS','hub:rating:*']),before);
  assert.equal(await store(['GET',rankKey]),rankBefore);
  assert.equal(L._internals.matches.has(match.id),false);
  assert.equal((await L.finalSnapshot(ids[0],{match_id:match.id})).ok,true,'a delayed host report replays the void');
  for(const id of ids){
    const rows=await L._internals.readHistory(id);
    assert.equal(rows.filter(row=>row.id===match.id).length,1);
    assert.equal(rows[0].outcome,'voided');assert.equal(rows[0].rr_delta,0);
  }
  const reboot=live.create(options);await reboot._internals.ready;t.after(()=>reboot.shutdown());
  assert.equal(reboot._internals.matches.size,0);
  assert.equal((await reboot.completion(ids[2],match.id)).result.voided,true);
});
