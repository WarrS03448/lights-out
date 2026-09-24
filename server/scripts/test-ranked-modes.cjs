'use strict';
// node --test server/scripts/test-ranked-modes.cjs
const {test}=require('node:test');
const assert=require('node:assert/strict');
process.env.NODE_ENV='test';
process.env.COMP_NETWORK_TEST_BYPASS='1';
const live=require('../live.cjs');
const A='76561198000000001', B='76561198000000002';
function service(modeId, records=new Map()) {
  return live.create({modeId,prefix:'ranked-test:',whoami:async()=>({steam_id:A}),bearer:()=> 'token',
    sendJson:(res,status,body)=>Object.assign(res,{status,body}),badRequest(){},readBody:async()=>Buffer.from('{}'),
    upstashCmd:async([op,key])=>op==='GET'?records.get(key)||null:op==='SMEMBERS'?[]:null});
}
test('BB1 forms two solo opponents while default BB5 retains ten-player capacity',async t=>{
  const one=service('BB1'), five=service();t.after(()=>{one.shutdown();five.shutdown();});
  assert.equal(one._internals.MATCH_SIZE,2);
  assert.equal(one._internals.MAX_PARTY,1);
  assert.equal(five._internals.MATCH_SIZE,10);
  assert.equal(one._internals.enqueue([A,B],'party',Date.now()),null);
  one._internals.enqueue([A],'',Date.now());one._internals.enqueue([B],'',Date.now());
  const match=one._internals.tryFormMatch();
  assert.ok(match);assert.equal(match.mode,'BB1');assert.equal(match.players.length,2);
  assert.equal(match.expected_score_limit,5);assert.equal(match.expected_max_rounds,9);
  assert.deepEqual(require('../ranked-modes.cjs').rulesOf('BB5'),{
    score_limit:7,max_rounds:13,team_switch_interval:6,round_seconds:180,time_limit:180});
});
test('mode rank reads retain existing BB5 record and use independent BB1 placements',async t=>{
  const records=new Map([['ranked-test:rating:'+A,JSON.stringify({matches:8,wins:5,losses:3,progress:950})]]);
  const one=service('BB1',records),five=service(undefined,records);t.after(()=>{one.shutdown();five.shutdown();});
  assert.equal((await five._internals.loadRating(A)).matches,8);
  assert.equal((await one._internals.loadRating(A)).matches,0);
  assert.notEqual(one._internals.boardKey(),five._internals.boardKey());
});

for(const legacy of [false,true])test(`BB1 ${legacy?'legacy':'new'} lobby retains its rules through restore and connect`,t=>{
 const svc=service('BB1');t.after(()=>svc.shutdown());const I=svc._internals;
 I.enqueue([A],'',Date.now());I.enqueue([B],'',Date.now());const original=I.tryFormMatch();
 const raw=I.serialiseMatch(original);
 if(legacy){delete raw.expected_score_limit;delete raw.expected_max_rounds;}
 const match=I.reviveMatch(JSON.parse(JSON.stringify(raw)));I.matches.set(match.id,match);clearTimeout(original.timer);
 for(const p of match.players)I.acceptMatch(p.player_id);
 I.flipCoin(match.lobby.coin_captain,{side:'heads'});I.expireStageTurn(match.id);
 for(let n=0;n<4;n++)I.expireStageTurn(match.id);
 assert.equal(I.beginConnect(A,{host:A,map:'Paintball'}).ok,true);
 assert.equal(match.expected_score_limit,legacy?7:5);assert.equal(match.expected_max_rounds,legacy?13:9);
 assert.equal(match.agreedScoreLimit,legacy?7:5);
});
test('unknown explicit mode fails instead of entering default ladder',()=>{
  let svc;
  try {assert.throws(()=>{svc=service('unknown');},/mode/i);}finally{svc?.shutdown();}
});

test('BB1 freezes the served rules including an operator test override',t=>{
 const svc=live.create({modeId:'BB1',expectedRules:()=>({score_limit:3,max_rounds:5})});
 t.after(()=>svc.shutdown());const I=svc._internals;
 I.enqueue([A],'',Date.now());I.enqueue([B],'',Date.now());const match=I.tryFormMatch();
 assert.equal(match.expected_score_limit,3);assert.equal(match.expected_max_rounds,5);
});
test('persisted one-map BB1 lobby still picks a side directly without map bans',t=>{
  const svc=service('BB1');t.after(()=>svc.shutdown());const I=svc._internals;
  I.enqueue([A],'',Date.now());I.enqueue([B],'',Date.now());const match=I.tryFormMatch();
  assert.ok(match,'two-player mode forms a match');
  for(const p of match.players)I.acceptMatch(p.player_id);
  match.lobby.pool=['Paintball']; // Lobby saved before the three-map rollout.
  assert.equal(match.lobby.stage,'coin');
  assert.deepEqual(match.lobby.pool,['Paintball']);
  I.flipCoin(match.lobby.coin_captain,{side:'heads'});
  I.expireStageTurn(match.id);
  assert.equal(match.lobby.stage,'side');assert.equal(match.lobby.side_picker,match.lobby.toss_winner);
  const winner=match.lobby.captains[match.lobby.toss_winner];
  assert.equal(I.chooseAdvantage?.(winner,{kind:'ban'})?.ok,false);
  assert.equal(I.pickSide(winner,{side:'defend'}).ok,true);
  assert.equal(match.lobby.stage,'ready');assert.equal(match.map,'Paintball');
  assert.equal(I.banMap(winner,{map:'Paintball'}).ok,false);
});
