'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {roundDetails} = require('../round-details.cjs');
const combat = require('../combat.cjs');
const A = '76561198000000001', B = '76561198000000002';
function match() {
  const combatState = combat.createState();
  combatState.roster = {[A]:0,[B]:1};
  Object.assign(combatState.coverage,{started:true,closed:true,damage:true});
  combatState.events = [
    {kind:'health',n:0,ignored:true,a:A,b:B,loss:900,relation:'enemy',old:1000,new:100},
    {kind:'health',n:0,a:A,b:B,loss:35,relation:'enemy',old:100,new:65},
    {kind:'health',n:1,a:B,b:A,loss:80,relation:'enemy',old:100,new:20}
  ];
  return {players:[{steam_id:A},{steam_id:B}],teams:{1:[A],2:[B]},score:{1:1,2:1},
    team_map:{0:1,1:2},round_wins:{0:0,1:1},combatState,
    stats:{rounds:2,series:{[A]:{0:{kills:1,deaths:0},1:{kills:0,deaths:1}}}}};
}
test('round damage, counterparties and signed K/D are scoped without changing match evidence',()=>{
  const m=match(),before=JSON.stringify(m), rounds=roundDetails(m);
  assert.deepEqual(rounds.map(r=>r.n),[1,2]);
  assert.deepEqual(rounds.map(r=>r.won),[1,2]);
  assert.deepEqual(rounds[1].score,[1,1]);
  assert.equal(rounds[0].scoreboard[0].kills,1);
  assert.equal(rounds[1].scoreboard[0].kills,-1);
  assert.equal(rounds[0].scoreboard[0].combat.enemyDamage,35);
  assert.equal(rounds[1].scoreboard[0].combat.enemyDamage,0);
  assert.equal(rounds[1].scoreboard[0].combat.damageTaken,80);
  assert.equal(rounds[0].scoreboard[0].combat.playerStats[0].damageDealt,35);
  assert.equal(rounds[1].scoreboard[0].combat.adr,0);
  assert.equal(JSON.stringify(m),before);
});
test('missing adjacent samples and decreasing death counters stay unknown',()=>{
  const m=match();delete m.stats.series[A][0];
  let rounds=roundDetails(m);
  assert.equal(rounds[1].scoreboard[0].kills,null);
  assert.equal(rounds[1].scoreboard[0].deaths,null);
  m.stats.series[A][0]={kills:1,deaths:2};
  rounds=roundDetails(m);
  assert.equal(rounds[1].scoreboard[0].kills,-1);
  assert.equal(rounds[1].scoreboard[0].deaths,null);
});

test('Lights Out round and archived combat counterparties retain canonical ownership',async t=>{
  const raw=match(), player='a1111111-1111-4111-8111-111111111111';
  raw.players[1]={player_id:player,game_steam_id:B,steam_id:B};raw.teams[2]=[player];
  const before=JSON.stringify(raw);
  const peer=roundDetails(raw)[0].scoreboard[0].combat.playerStats[0];
  assert.equal(peer.player_id,player);assert.equal(peer.game_steam_id,B);
  assert.equal(JSON.stringify(raw),before);
  const board=[{steam_id:A,player_id:A,combat:{enemyDamage:35}}];
  const publicMatch={players:raw.players,teams:raw.teams,score:raw.score,scoreboard:board,rounds_played:2};
  const receipt={inputs:raw,publicMatch,score:raw.score,rows:[],board};
  const svc=require('../live.cjs').create({upstashCmd:async args=>
    args[0]==='GET'&&args[1]==='hub:settlement:old' ? JSON.stringify(receipt) : args[0]==='SMEMBERS' ? [] : null});
  t.after(()=>svc.shutdown());
  const detail=await svc._internals.readMatch('old',A);
  assert.equal(detail.scoreboard[0].combat.playerStats[0].player_id,player);
  assert.equal(detail.scoreboard[0].combat.playerStats[0].game_steam_id,B);
});
test('partial capture cannot manufacture complete round data or anonymous team kills',()=>{
  const m=match();m.combatState.coverage.broken=true;
  m.kills=[{round:0,killer:A,victim:B,teamKill:true},{round:1,killer:A,teamKill:true,inferred:true}];
  const rounds=roundDetails(m);
  assert.equal(rounds[1].scoreboard[0].combat.status,'partial');
  assert.equal(rounds[0].scoreboard[0].team_kills,1);
  assert.equal(rounds[1].scoreboard[0].team_kills,null);
});
test('unknown winners and batched score reports do not invent a winning order',()=>{
  const m=match();delete m.round_wins;delete m.team_map;
  m.rounds=[{won:1,steps:2,1:2,2:1},{won:2,steps:1,1:2,2:1}];
  m.score={1:2,2:1};
  const rounds=roundDetails(m);
  assert.equal(rounds.length,3);
  assert(rounds.every(r=>r.won===null));
  assert.equal(rounds[0].score,null);
  assert.deepEqual(rounds[2].score,[2,1]);
});
test('next-round heartbeat cannot add a round beyond the recorded final total',()=>{
  const m=match();m.round_reports={3:{round:3,source:'heartbeat',seconds:0}};
  m.combatState.events.push({kind:'coverage',n:3});
  assert.equal(roundDetails(m).length,2);
});
test('frozen hub-side outcomes survive a later game-team mapping change',()=>{
  const m=match();m.round_results={0:1,1:2};m.team_map={0:2,1:1};
  assert.deepEqual(roundDetails(m).map(r=>r.won),[1,2]);
});
test('delegate winner and duration survive an objective round with both teams alive',async t=>{
  const svc=require('../live.cjs').create({collectSeconds:0});t.after(()=>svc.shutdown());
  const m=match();delete m.round_results;delete m.round_wins;
  Object.assign(m,{id:'objective-round',state:'live',host:A,created:Date.now(),left:[]});
  svc._internals.matches.set(m.id,m);svc._internals.inMatch.set(A,m.id);
  svc._internals.gameReportedRound(A,{match:m.id,row:'n=0;w=1;sec=64;a0=1;a1=1',source:'delegate'});
  // The legacy timeline can be replaced later; the display evidence must remain.
  m.rounds=[];m.team_map={0:2,1:1};
  const round=roundDetails(m)[0];
  assert.equal(round.won,2);assert.equal(round.seconds,64);
});
test('old unmarked receipts cannot reinterpret warmup as round-one stats',async t=>{
  const raw=match(),publicMatch={players:raw.players,teams:raw.teams,score:raw.score,scoreboard:[],rounds_played:2};
  const receipt={inputs:raw,publicMatch,score:raw.score,rows:[],board:[]};
  const before=JSON.stringify(receipt);
  const svc=require('../live.cjs').create({upstashCmd:async args=>
    args[0]==='GET'&&args[1]==='hub:settlement:old' ? JSON.stringify(receipt) : args[0]==='SMEMBERS' ? [] : null});
  t.after(()=>svc.shutdown());
  let detail=await svc._internals.readMatch('old',A);
  assert.equal(detail.round_details.length,2);
  assert.equal(detail.round_details[0].scoreboard[0].kills,null);
  assert.equal(detail.round_details[0].scoreboard[0].combat,undefined);
  assert.equal(JSON.stringify(receipt),before);
  receipt.inputs.round_index_base=0;
  detail=await svc._internals.readMatch('old',A);
  assert.equal(detail.round_details[0].scoreboard[0].kills,1);
  assert.equal(detail.round_details[0].scoreboard[0].combat.enemyDamage,35);
  assert.equal(await svc._internals.readMatch('old','76561198999999999'),null);
});
