const {test} = require('node:test');
const assert = require('node:assert/strict');
const live = require('../live.cjs');
const reconnect = require('../reconnect.cjs');

function fixture(t) {
  const L = live.create({});
  const ids = Array.from({length:4}, (_,i)=>String(76561198000000001n+BigInt(i)));
  const teams = {1:[ids[0],ids[2]],2:[ids[1],ids[3]]};
  const match = {id:'0123456789abcdef',state:'live',host:ids[0],start_ready_verified:true,
    players:ids.map(steam_id=>({steam_id,connected:true})),teams,assigned_teams:structuredClone(teams),left:[]};
  L._internals.matches.set(match.id,match);
  ids.forEach(id=>L._internals.inMatch.set(id,match.id));
  t.after(()=>clearTimeout(match.timer));
  const rows = ids.map(steam_id=>({steam_id,active:1}));
  return {L,ids,match,rows};
}

test('presence needs a confirmed match and assigned connected host; deaths are irrelevant',t=>{
  const {match,ids}=fixture(t);
  assert.deepEqual(reconnect.observe(match,ids,100).missing,[]);
  for(const value of [[],ids.slice(1),[...ids,ids[0]],[ids[0],'76561198999999999']])
    assert.equal(reconnect.observe(match,value,100).ok,false);
  match.start_ready_verified=false;
  assert.equal(reconnect.observe(match,ids,100).ok,false);
});

test('return clears the window; each new departure has its own five minutes',t=>{
  const {match,ids}=fixture(t);
  match.reconnect=reconnect.observe(match,ids.slice(0,3),100).windows;
  assert.equal(match.reconnect[ids[3]].deadline,300100);
  assert.deepEqual(reconnect.observe(match,ids.slice(0,3),300099).expired,[]);
  assert.deepEqual(reconnect.observe(match,ids.slice(0,3),300100).expired,[ids[3]]);
  match.reconnect=reconnect.observe(match,ids,150000).windows;
  assert.deepEqual(match.reconnect,{});
  assert.equal(reconnect.observe(match,ids.slice(0,3),200000).windows[ids[3]].deadline,500000);
});

test('a return at or after the deadline cannot erase the abandonment penalty',t=>{
  const {match,ids}=fixture(t);
  match.reconnect={[ids[3]]:{since:100,deadline:300100}};
  assert.deepEqual(reconnect.observe(match,ids,300099).expired,[]);
  assert.deepEqual(reconnect.observe(match,ids,300100).expired,[ids[3]]);
  assert.deepEqual(reconnect.observe(match,ids,300101).expired,[ids[3]]);
});

test('host presence opens a persisted window, clears it on return, and expires only a confirmed absence',async t=>{
  const {L,match,rows,ids}=fixture(t);
  assert.equal((await L.matchPresence(ids[1],match.id,rows)).ok,false);
  assert.equal((await L.matchPresence(ids[0],match.id,rows.slice(0,3))).ok,false);
  assert.equal(match.reconnect[ids[3]].deadline-match.reconnect[ids[3]].since,300000);
  assert.equal(match.players.length,4);
  assert.equal((await L.matchPresence(ids[0],match.id,rows)).ok,true);
  assert.deepEqual(match.reconnect,{});
  const since=Date.now()-300001;
  match.reconnect={[ids[3]]:{since,deadline:since+300000}};
  assert.equal((await L.matchPresence(ids[0],match.id,rows.slice(0,3))).ok,true);
  assert.equal(match.players.length,3);
  assert.equal(match.left[0].disconnect_confirmed,true);
  assert.equal(match.left[0].player_id,ids[3]);
  assert.equal(match.assigned_teams[2].length,2);
  assert.equal(L.teamRuling(ids[0],ids[3],'stranger').yes,true);
  assert.equal((await L.matchPresence(ids[0],match.id,rows.slice(0,3))).ok,true);
  assert.equal(match.left.length,1);
});
