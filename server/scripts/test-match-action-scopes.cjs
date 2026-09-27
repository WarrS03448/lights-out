// Run: node --test scripts/test-match-action-scopes.cjs. No external services.
const test = require('node:test');
const assert = require('node:assert/strict');
const live = require('../live.cjs');
const identity = require('../player-identity.cjs');
const A = 'aaaaaaaaaaaaaaaa', B = 'bbbbbbbbbbbbbbbb';
const ids = ['76561198000000001', '76561198000000002'];

function fixture(t, options={}) {
  let read = async req => Buffer.from(JSON.stringify(req.body || {}));
  const events = [];
  const service = live.create({whoami: async token => ({steam_id: token, auth_method: 'steam'}),
    bearer: req => req.token, sendJson: (res, status, body) => Object.assign(res, {status, body}),
    badRequest() {}, readBody: req => read(req), upstashCmd: null, ...options});
  const I = service._internals;
  for (const id of ids) {
    I.clients.set(id, {steamId:id, gameSteamId:id, token:id,
      res:{write: event => events.push(event), end() {}}});
    I.bySteam.set(id, new Set([id]));
  }
  const match = identity.freezeMatch({id:B, state:'found', created:Date.now(),
    players:ids.map(steam_id => ({steam_id, accepted:false, connected:false})),
    teams:{1:[ids[0]], 2:[ids[1]]}, host:ids[0], left:[], map:'Airsoft'});
  I.matches.set(B, match); ids.forEach(id => I.inMatch.set(id, B));
  // A scoped client: a Windows hub is answered as 2d25405 did (test-legacy-wire.cjs).
  const request = async (action, body={}) => {
    const res = {};
    await service.route({token:ids[0], body, headers:{'x-hub-platform':'linux'}}, res, 'POST', '/api/match/' + action);
    return res;
  };
  t.after(() => service.shutdown());
  return {service, I, match, events, request, read: fn => {read = fn;}};
}

for (const action of ['accept','leave','coin','choose','side','ban','chat','connecting','connected']
    .flatMap(action => [action, 'scoped/' + action])) {
  test(`${action} rejects a request belonging to the previous match`, async t => {
    const f = fixture(t);
    const before = structuredClone(f.match);
    const res = await f.request(action, {expected_match_id:A, text:'private', side:'heads', map:'Airsoft'});
    assert.equal(res.status, 409);
    assert.equal(res.body.stale_action, true);
    assert.deepEqual(f.match, before);
    assert.equal(f.I.inMatch.get(ids[0]), B);
    assert.deepEqual(f.events, []);
  });
}

test('scoped routes require an explicit match and are in the authenticated allowlist', async t => {
  const f = fixture(t);
  for (const action of ['accept','leave','coin','choose','side','ban','chat','connecting','connected']) {
    assert.equal(live.owns('/api/match/scoped/' + action), true);
    const res = await f.request('scoped/' + action);
    assert.equal(res.status, 409);
    assert.equal(res.body.stale_action, true);
  }
  assert.equal(f.match.players[0].accepted, false);
  assert.equal((await f.request('scoped/accept', {expected_match_id:B})).status, 200);
  assert.equal(f.match.players[0].accepted, true);
});

for (const body of [{expected_match_id:null}, {expected_match_id:16}, {expected_match_id:''},
  {expected_match_id:B, expected_stage:null}, {expected_stage:'coin', expected_ban_count:0},
  {expected_match_id:B, expected_stage:'coin', expected_ban_count:'0'},
  {expected_match_id:B, expected_ban_count:0}]) {
  test(`malformed expectation rejects without accepting: ${JSON.stringify(body)}`, async t => {
    const f = fixture(t), res = await f.request('accept', body);
    assert.equal(res.status, 409);
    assert.equal(res.body.stale_action, true);
    assert.equal(f.match.players[0].accepted, false);
  });
}

for (const raw of ['{', '[]', 'null', 'true']) {
  test(`invalid body cannot downgrade a guarded request: ${raw}`, async t => {
    const f = fixture(t); f.read(async () => Buffer.from(raw));
    const res = await f.request('accept');
    assert.equal(res.status, 409);
    assert.equal(f.match.players[0].accepted, false);
  });
}

for (const body of [{}, {expected_match_id:B}]) {
  test(`accept preserves valid ${body.expected_match_id ? 'scoped' : 'legacy'} behavior`, async t => {
    const f = fixture(t), res = await f.request('accept', body);
    assert.equal(res.status, 200);
    assert.equal(f.match.players[0].accepted, true);
  });
}

test('body arriving after membership changes is validated against the new membership', {timeout:5000}, async t => {
  const f = fixture(t);
  let ready, release;
  const entered = new Promise(resolve => {ready = resolve;});
  f.I.inMatch.set(ids[0], A);
  f.read(() => {ready(); return new Promise(resolve => {release = resolve;});});
  const response = f.request('accept', {expected_match_id:A});
  await entered;
  f.I.inMatch.set(ids[0], B);
  release(Buffer.from(JSON.stringify({expected_match_id:A})));
  const res = await response;
  assert.equal(res.status, 409);
  assert.equal(res.body.stale_action, true);
  assert.equal(f.match.players[0].accepted, false);
});

test('a veto from an earlier turn rejects even when the same captain is up again', async t => {
  const f = fixture(t);
  f.match.state = 'ready';
  f.match.lobby = {stage:'veto', bans:[{team:1,map:'Airsoft'}, {team:2,map:'Hospital'}], ban_turn:1};
  const res = await f.request('ban', {expected_match_id:B, expected_stage:'veto',
    expected_ban_count:0, map:'Worn House'});
  assert.equal(res.status, 409);
  assert.equal(res.body.stale_action, true);
  assert.equal(f.match.lobby.bans.length, 2);
});

test('a decision from an earlier lobby stage rejects before mutation', async t => {
  const f = fixture(t);
  f.match.state = 'ready'; f.match.lobby = {stage:'side', bans:[]};
  const res = await f.request('choose', {expected_match_id:B, expected_stage:'choice',
    expected_ban_count:0, kind:'side'});
  assert.equal(res.status, 409);
  assert.equal(res.body.stale_action, true);
});

test('scoped routes retain authentication and account admission', async t => {
  const unauthenticated = fixture(t, {whoami: async () => null});
  assert.equal((await unauthenticated.request('scoped/accept', {expected_match_id:B})).status, 401);
  assert.equal(unauthenticated.match.players[0].accepted, false);
  const refused = fixture(t, {admitGameplay: async () => false});
  assert.equal((await refused.request('scoped/accept', {expected_match_id:B})).status, 401);
  assert.equal(refused.match.players[0].accepted, false);
  const banned = fixture(t);
  banned.I.bans.set(ids[0], {until:Date.now()+60000, reason:'fixture'});
  assert.equal((await banned.request('scoped/accept', {expected_match_id:B})).status, 403);
  assert.equal(banned.match.players[0].accepted, false);
});

test('scoped action rejects across ladders even when adapter selects the new active engine', async t => {
  const ranked = require('../ranked-service.cjs').create({
    whoami:async token => ({steam_id:token, auth_method:'steam'}), bearer:req=>req.token,
    sendJson:(res,status,body)=>Object.assign(res,{status,body}),
    readBody:async req=>Buffer.from(JSON.stringify(req.body))});
  t.after(() => ranked.shutdown()); await ranked._internals.ready;
  const current = identity.freezeMatch({id:B, state:'found', players:ids.map(steam_id=>({steam_id,accepted:false}))});
  const I = ranked.forMode('BB1')._internals;
  I.matches.set(B,current); I.inMatch.set(ids[0],B);
  const res = {};
  await ranked.route({token:ids[0], headers:{'x-ranked-mode':'BB5'}, body:{expected_match_id:A}},
    res,'POST','/api/match/scoped/accept');
  assert.equal(res.status,409); assert.equal(res.body.stale_action,true);
  assert.equal(current.players[0].accepted,false);
  assert.equal(I.inMatch.get(ids[0]),B);
});

test('normal scoped coin and veto use the frozen stage and ban count', async t => {
  const f = fixture(t);
  f.match.state = 'ready';
  f.match.lobby = f.I.buildLobby(f.match);
  f.match.lobby.coin_captain = ids[0];
  assert.equal((await f.request('scoped/coin', {expected_match_id:B, expected_stage:'coin',
    expected_ban_count:0, side:'heads'})).status,200);
  assert.equal(f.match.lobby.stage,'flipping');
  f.I.clearStageTurn(f.match);
  f.match.lobby.stage = 'veto'; f.match.lobby.ban_turn = 1;
  const map = f.match.lobby.pool[0];
  assert.equal((await f.request('scoped/ban', {expected_match_id:B, expected_stage:'veto',
    expected_ban_count:0, map})).status,200);
  assert.equal(f.match.lobby.bans.length,1);
});

test('normal scoped live departure preserves the reconnect window and revokes only its permit', async t => {
  const f=fixture(t); f.match.state='live'; f.match.host=ids[1];
  f.service.grantJoinPermits(f.match);
  const res=await f.request('scoped/leave',{expected_match_id:B});
  assert.equal(res.status,200); assert.equal(res.body.reconnect,true);
  assert(res.body.deadline>Date.now());
  assert.equal(f.I.inMatch.get(ids[0]),B);
  assert.equal(f.service.takeJoinPermit(ids[0]),false);
  assert(f.events.some(event=>event.includes('match_reconnect')));
});

// Delay the actual snapshot boundary without external Redis. No other persistence API is
// needed by this fixture; unknown commands fail instead of silently faking a successful write.
function delayedStore() {
  let arrived, release, writes=0;
  const entered = new Promise(resolve => {arrived=resolve;});
  const store = async args => {
    if (['GET','HGET'].includes(args[0])) return null;
    if (['SMEMBERS','ZRANGE'].includes(args[0])) return [];
    if (args[0]==='EVAL' && args[1]===require('../settlement.cjs').SNAPSHOT) {
      writes++;
      if (writes===1) {arrived(); await new Promise(resolve => {release=resolve;});}
      return ['saved'];
    }
    throw Error('Unexpected storage command: '+args[0]);
  };
  return {store, entered, release:()=>release(), writes:()=>writes};
}

test('leave rechecks membership after persistence and after waiting behind another leave', {timeout:5000}, async t => {
  const storage=delayedStore(), f=fixture(t,{upstashCmd:storage.store});
  await f.I.ready;
  // Move the fixture to A before either leave starts; B is created while A saves.
  f.I.matches.delete(B); f.match.id=A; f.match.state='live'; f.match.host=ids[1];
  f.I.matches.set(A,f.match); ids.forEach(id=>f.I.inMatch.set(id,A));
  const first=f.request('scoped/leave',{expected_match_id:A});
  await storage.entered;
  const second=f.request('scoped/leave',{expected_match_id:A});
  await new Promise(setImmediate); // Both route body/auth continuations have run.
  const next=identity.freezeMatch({id:B,state:'live',host:ids[1],players:ids.map(steam_id=>({steam_id}))});
  f.I.matches.set(B,next); ids.forEach(id=>f.I.inMatch.set(id,B));
  f.service.grantJoinPermits(next);
  storage.release();
  for (const res of await Promise.all([first,second])) {
    assert.equal(res.status,409); assert.equal(res.body.stale_action,true);
  }
  assert.equal(storage.writes(),1,'queued stale departure must not start another save');
  assert.equal(f.I.inMatch.get(ids[0]),B);
  assert.equal(next.reconnect,undefined);
  assert.equal(f.service.takeJoinPermit(ids[0]),true,'new match permit must remain usable');
  assert.equal(f.events.some(event=>event.includes('match_reconnect')),false);
});
