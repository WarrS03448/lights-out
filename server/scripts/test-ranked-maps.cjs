'use strict';
// node --test server/scripts/test-ranked-maps.cjs
const {test}=require('node:test'),assert=require('node:assert/strict');
process.env.NODE_ENV='test';process.env.COMP_NETWORK_TEST_BYPASS='1';
const live=require('../live.cjs');
const A='76561198000000001', B='76561198000000002';
function setup(t,extra={}) {
 const svc=live.create({modeId:'BB1',prefix:'ranked-maps-test:',...extra});
 t.after(()=>svc.shutdown());const I=svc._internals;
 I.enqueue([A],'',Date.now());I.enqueue([B],'',Date.now());const m=I.tryFormMatch();
 for(const p of m.players)I.acceptMatch(p.player_id);
 I.flipCoin(m.lobby.coin_captain,{side:'heads'});I.expireStageTurn(m.id);
 return {svc,I,m};
}
for(const choice of ['side','ban'])for(const winner of [1,2])test(`three-map 1v1: team ${winner} takes ${choice}, correct player bans last`,t=>{
 const {I,m}=setup(t),L=m.lobby;L.toss_winner=winner;
 assert.deepEqual(L.pool,['Paintball','Airsoft','BombHouse']);assert.equal(L.stage,'choice');
 const loser=winner===1?2:1;
 assert.equal(I.chooseAdvantage(L.captains[loser],{kind:choice}).ok,false);
 assert.equal(I.chooseAdvantage(L.captains[winner],{kind:choice}).ok,true);
 assert.equal(L.side_picker,choice==='side'?winner:loser);
 assert.equal(I.pickSide(L.captains[L.side_picker],{side:'attack'}).ok,true);
 assert.equal(L.stage,'veto');
 assert.equal(I.banMap(L.captains[L.ban_turn],{map:'Hospital'}).ok,false);
 const first=L.ban_turn;assert.equal(I.banMap(L.captains[first],{map:'Paintball'}).ok,true);
 assert.equal(L.ban_turn,L.ban_advantage);assert.notEqual(L.ban_turn,first);
 assert.equal(I.banMap(L.captains[first],{map:'Airsoft'}).ok,false);
 assert.equal(I.banMap(L.captains[L.ban_turn],{map:'Airsoft'}).ok,true);
 assert.equal(L.stage,'ready');assert.equal(L.map,'BombHouse');
 assert.equal(I.beginConnect(A,{host:A,map:'Paintball'}).ok,true);
 assert.equal(m.map,'BombHouse','the server veto overrides the requested map');
 assert.equal(m.expected_score_limit,5);assert.equal(m.expected_max_rounds,9);
});
test('three-map lobby survives a restart and automatically completes timed out choices',t=>{
 const {I,m}=setup(t);const raw=I.serialiseMatch(m);clearTimeout(m.timer);clearTimeout(m.lobby.stage_timer);
 const restored=I.reviveMatch(JSON.parse(JSON.stringify(raw)));I.matches.set(m.id,restored);
 for(let n=0;n<4;n++)I.expireStageTurn(m.id);
 assert.equal(restored.lobby.stage,'ready');assert.equal(restored.lobby.bans.length,2);
 assert.equal(restored.lobby.pool.filter(map=>!restored.lobby.bans.some(b=>b.map===map))[0],restored.lobby.map);
});
