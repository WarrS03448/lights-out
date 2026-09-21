// UTF-8. Run: node --test scripts/test-void-vote.cjs. No game or external services.
'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const live=require('../live.cjs');

function fixture(t, count=10) {
  const saved=new Map(), events=new Map(); let offline=false, plan, pendingSnapshot;
  const store=async args=>{
    if(offline)throw Error('offline');
    if(args[0]==='SMEMBERS')return [];
    if(args[0]==='GET')return saved.get(args[1])||null;
    if(args[0]==='HGET')return null;
    if(args[0]==='SET'){saved.set(args[1],args[2]);return 'OK';}
    if(args[0]==='EVAL'){
      if(args[1].includes('rank-snapshot-v1')){
        if(pendingSnapshot)return ['pending',JSON.stringify(pendingSnapshot)];
        saved.set(args[4],args.at(-2));return ['saved'];
      }
      if(args[1].includes('local incoming = cjson.decode'))return 0;
      plan=JSON.parse(args.at(-1));
      const receipt=saved.get(args[3])||plan.receipt_json;
      saved.set(args[3],receipt);return receipt;
    }
    return 'OK';
  };
  let actor;
  const L=live.create({upstashCmd:store,whoami:async()=>actor,bearer:()=> 'token',
    readBody:async req=>Buffer.from(JSON.stringify(req.body||{})),
    sendJson:(res,status,body)=>Object.assign(res,{status,body})});
  const ids=Array.from({length:count},(_,i)=>i===0?'a1111111-1111-4111-8111-111111111111':String(76561198000000001n+BigInt(i)));
  const games=Array.from({length:count},(_,i)=>String(76561198000000001n+BigInt(i)));
  const teams={1:ids.slice(0,5),2:ids.slice(5)};
  const match={id:'0123456789abcdef',state:'live',host:ids[0],created:Date.now(),map:'Rome',
    players:ids.map((player_id,i)=>({player_id,game_steam_id:games[i],accepted:true,connected:true})),
    teams,assigned_teams:structuredClone(teams),left:[],start_ready_verified:true,
    reportToken:'ab'.repeat(32),timer:null,deadline:Date.now()+60000,score:{1:2,2:1}};
  L._internals.matches.set(match.id,match);
  for(const id of ids){
    L._internals.inMatch.set(id,match.id);events.set(id,[]);
    L._internals.clients.set(id,{steamId:id,res:{write:data=>events.get(id).push(JSON.parse(data.slice(6))),end(){}}});
    L._internals.bySteam.set(id,new Set([id]));
  }
  async function post(index,body={}) {
    actor=index===null?null:{player_id:ids[index]||'76561198999999999',game_steam_id:games[index]||'76561198999999999',auth_method:'steam'};
    const res={}; await L.route({headers:{},body},res,'POST','/api/match/void-vote');return res;
  }
  t.after(async()=>{offline=false;await L.shutdown();});
  return {L,match,ids,games,events,saved,store,post,plan:()=>plan,offline:value=>offline=value,
    savedDecision:value=>pendingSnapshot=value};
}

test('only seven distinct authenticated match participants can void, with no rating writes',async t=>{
  const f=fixture(t);await f.L._internals.ready;
  const body={match_id:f.match.id,yes:true};
  assert.equal((await f.post(null,body)).status,401);
  assert.equal((await f.post(12,body)).status,409);
  assert.equal((await f.post(0,{...body,match_id:'fedcba9876543210'})).status,409);
  assert.equal((await f.post(0,{...body,yes:'true'})).status,409);
  for(let i=0;i<6;i++) {
    assert.equal((await f.post(i,{...body,player_id:f.ids[9],yes_count:10})).status,200);
    assert.equal((await f.post(i,body)).body.vote.yes,i+1,'a retry is one vote');
    assert.equal(f.L._internals.matches.has(f.match.id),true);
    assert.equal(f.events.get(f.ids[0]).some(e=>e.type==='match_result'),false);
  }
  assert.equal((await f.post(0,{...body,yes:false})).body.vote.yes,6,'first ballot cannot be replaced');
  const seventh=await f.post(6,body);
  assert.equal(seventh.status,200);assert.equal(seventh.body.voided,true);
  assert.equal(f.L._internals.matches.has(f.match.id),false);
  assert.equal(f.plan().rank_checks.length,0);assert.equal(f.plan().board.length,0);assert.equal(f.plan().hashes.length,0);
  const analytics=require('../analytics-metrics.cjs').projectReceipt(JSON.parse(f.plan().receipt_json));
  assert.equal(analytics.outcome,'voided');assert.equal(analytics.completed,false);
  const metrics=require('../analytics-metrics.cjs').contribution(analytics);
  assert.equal(metrics.voided,1);assert.equal(metrics.cancelled,0);assert.equal(metrics.completed,0);
  for(const id of f.ids){
    assert.equal(f.L._internals.inMatch.has(id),false);
    const result=f.events.get(id).find(e=>e.type==='match_result');
    assert.equal(result.voided,true);assert.equal(result.rr_delta,0);
    const receipt=await f.L.completion(id,f.match.id);
    assert.equal(receipt.close_allowed,true);assert.equal(receipt.result.voided,true);
  }
  const reboot=live.create({upstashCmd:f.store});t.after(()=>reboot.shutdown());
  assert.equal((await reboot.completion(f.ids[9],f.match.id)).result.voided,true);
  assert.equal((await reboot.completion('76561198999999999',f.match.id)).close_allowed,false);
});

test('starting a vote does not fabricate ballots; small matches cannot reduce seven',async t=>{
  const f=fixture(t,2);await f.L._internals.ready;
  assert.equal((await f.post(0,{match_id:f.match.id})).body.vote.yes,0);
  for(let i=0;i<2;i++)assert.equal((await f.post(i,{match_id:f.match.id,yes:true})).status,200);
  assert.equal(f.L._internals.matches.has(f.match.id),true);
  assert.equal((await f.L.completion(f.ids[0],f.match.id)).close_allowed,false);
});

test('no votes, non-live matches and invalid game bindings never reach the threshold',async t=>{
  const f=fixture(t);await f.L._internals.ready;
  f.match.state='connecting';assert.equal((await f.post(0,{match_id:f.match.id,yes:true})).status,409);
  f.match.state='live';
  for(let i=0;i<4;i++)await f.post(i,{match_id:f.match.id,yes:false});
  for(let i=4;i<10;i++)await f.post(i,{match_id:f.match.id,yes:true});
  assert.equal(f.L._internals.matches.has(f.match.id),true);
  assert.equal((await f.post(0,{match_id:f.match.id})).body.vote.yes,6);
  f.match.game_bindings={...f.match.game_bindings,[f.ids[9]]:f.games[8]};
  assert.equal((await f.post(9,{match_id:f.match.id,yes:true})).status,409);
});

test('saved ballots survive restore and storage failure cannot authorize game closure',async t=>{
  const f=fixture(t);await f.L._internals.ready;
  for(let i=0;i<6;i++)await f.post(i,{match_id:f.match.id,yes:true});
  const restored=f.L._internals.reviveMatch(JSON.parse([...f.saved.values()].find(v=>v.includes('void_votes'))));
  f.L._internals.matches.set(f.match.id,restored);
  assert.equal((await f.post(0,{match_id:f.match.id,yes:true})).body.vote.yes,6);
  f.offline(true);
  const failed=await f.post(6,{match_id:f.match.id,yes:true});
  assert.equal(failed.status,503);
  assert.equal(f.events.get(f.ids[0]).some(e=>e.type==='match_result'),false);
  assert.equal((await f.L.completion(f.ids[0],f.match.id)).close_allowed,false);
  f.offline(false);
  await f.L._internals.flushMatches();
  assert.equal((await f.L.completion(f.ids[0],f.match.id)).result.voided,true);
});

test('a previously saved normal result decision wins over a stale seventh-vote request',async t=>{
  const f=fixture(t);await f.L._internals.ready;
  for(let i=0;i<6;i++)await f.post(i,{match_id:f.match.id,yes:true});
  const snapshot=f.L._internals.serialiseMatch(f.match);
  snapshot.collecting={winner:1,score:{1:7,2:2},limit:7,since:Date.now(),deadline:Date.now()+5000};
  f.savedDecision(snapshot);
  assert.equal((await f.post(6,{match_id:f.match.id,yes:true})).status,409);
  assert.equal(Boolean(f.match.void_pending),false);
  assert.equal(Boolean(f.match.final_snapshot),false);
  assert.equal(f.events.get(f.ids[0]).some(e=>e.type==='match_result'),false);
});
