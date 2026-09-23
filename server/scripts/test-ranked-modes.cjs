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
});
test('mode rank reads retain existing BB5 record and use independent BB1 placements',async t=>{
  const records=new Map([['ranked-test:rating:'+A,JSON.stringify({matches:8,wins:5,losses:3,progress:950})]]);
  const one=service('BB1',records),five=service(undefined,records);t.after(()=>{one.shutdown();five.shutdown();});
  assert.equal((await five._internals.loadRating(A)).matches,8);
  assert.equal((await one._internals.loadRating(A)).matches,0);
  assert.notEqual(one._internals.boardKey(),five._internals.boardKey());
});
test('unknown explicit mode fails instead of entering default ladder',()=>{
  let svc;
  try {assert.throws(()=>{svc=service('unknown');},/mode/i);}finally{svc?.shutdown();}
});
test('BB1 coin winner picks a side directly and cannot enter map bans',t=>{
  const svc=service('BB1');t.after(()=>svc.shutdown());const I=svc._internals;
  I.enqueue([A],'',Date.now());I.enqueue([B],'',Date.now());const match=I.tryFormMatch();
  assert.ok(match,'two-player mode forms a match');
  for(const p of match.players)I.acceptMatch(p.player_id);
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
