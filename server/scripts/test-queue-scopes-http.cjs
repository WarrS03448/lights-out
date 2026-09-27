'use strict';
// node --test scripts/test-queue-scopes-http.cjs; synthetic streams, no external services.
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
delete process.env.COMP_NETWORK_TEST_BYPASS;
process.env.COMP_MATCH_SIZE = '10';
const live = require('../live.cjs');

async function fixture(t, dual = false) {
  const A = '76561198000000001';
  const B = '76561198000000002';
  const credentials = new Map([[A,A],[B,B],['replacement-b',B]]), revoked = new Set();
  let held = null;
  const streams = [];
  const options = {
    prefix:'strict-queue-test:',
    whoami:async token => {
      if (held && token === A && held.skip-- === 0) {
        const gate = held;
        held = null;
        gate.enter();
        await gate.wait;
      }
      return credentials.has(token) && !revoked.has(token)
        ? {steam_id:credentials.get(token), auth_method:'steam'} : null;
    },
    bearer:req => req.token,
    sendJson:(res,status,body) => Object.assign(res,{status,body}),
    badRequest() {}, readBody:async req => req.raw ?? Buffer.from(JSON.stringify(req.body ?? {})),
    upstashCmd:null, requiredVersions:() => ({hub:'2.3.85', mode:'1.0.27'}),
  };
  const service = dual ? require('../ranked-service.cjs').create(options) : live.create(options);
  t.after(async () => { for (const stream of streams) stream.close(); await service.shutdown(); });
  await service._internals.ready;
  const internals = mode => dual ? service.forMode(mode)._internals : service._internals;
  const headers = (mode = 'BB5', extra = {}) => ({'x-ranked-mode':mode,
    'x-hub-version':'2.3.85', 'x-mode-version':'1.0.27',
    'x-bb5-version':'1.0.27', 'x-bb1-version':'1.0.27', ...extra});
  async function post(path, body = {}, mode = 'BB5', extra = {}, token = A, raw) {
    if (path.endsWith('/join') && credentials.has(token)) internals(mode).networkRegistry.profile(credentials.get(token), {
      region:'NA', cross_region:false, location:'1'.repeat(32),
      transport:'webrtc-relay-v1', age_seconds:0,
    }, Date.now());
    const res = {setHeader() {}};
    await service.route({token, body, raw, headers:headers(mode, extra)}, res, 'POST', path,
      new URL('http://fixture' + path));
    return res;
  }
  async function stream(token=A) {
    const req = Object.assign(new EventEmitter(), {token, headers:headers(), socket:{setTimeout() {}}});
    const events = [];
    const res = {
      headersSent:false, writableEnded:false, destroyed:false,
      setTimeout() {}, setHeader() {}, writeHead() { this.headersSent = true; },
      write(chunk) {
        if (typeof chunk === 'string' && chunk.startsWith('data: ')) events.push(JSON.parse(chunk.slice(6)));
        return true;
      },
      end() { if (!this.writableEnded) { this.writableEnded = true; req.emit('close'); } },
    };
    await service.route(req, res, 'GET', '/api/live', new URL('http://fixture/api/live'));
    await new Promise(setImmediate);
    const result = {events, close:() => res.end()};
    streams.push(result);
    for (const mode of dual ? ['BB5','BB1'] : ['BB5']) {
      const hello = events.find(e => e.type === 'hello' && (e.mode || 'BB5') === mode);
      assert(hello, `Missing ${mode} hello`);
      assert.equal(hello.capabilities.queue_action_scopes_v1, true);
    }
    return result;
  }
  function scope(stream, mode = 'BB5') {
    const event = stream.events.findLast(e => (e.mode || 'BB5') === mode && typeof e.queue_actor === 'string');
    assert(event?.queue_actor, `No current ${mode} actor`);
    return {queue_actor:event.queue_actor, queue_context:event.queue_context, queue_unit:event.queue_unit || ''};
  }
  const joinBody = (stream, attempt, mode = 'BB5') => ({...scope(stream, mode), queue_attempt:attempt, queue_unit:''});
  function pauseAdmission() {
    assert.equal(held, null);
    let enter, release;
    const entered = new Promise(resolve => { enter = resolve; });
    const wait = new Promise(resolve => { release = resolve; });
    held = {skip:1, enter, wait};
    return {entered, release};
  }
  return {A, B, revoked, service, internals, post, stream, scope, joinBody, pauseAdmission};
}

test('strict cancellation overtakes awaiting eligibility', {timeout:5000}, async t => {
  const f = await fixture(t), stream = await f.stream(), body = f.joinBody(stream,1);
  const gate = f.pauseAdmission(), joining = f.post('/api/queue/scoped/join',body);
  await gate.entered;
  try { assert.equal((await f.post('/api/queue/scoped/leave',body)).status,200); }
  finally { gate.release(); }
  assert.equal((await joining).status,409);
  assert.equal(f.internals('BB5').queueOf.has(f.A),false);
  assert.equal(f.internals('BB5').inMatch.has(f.A),false);
  assert.equal(stream.events.some(e => e.type === 'queued'),false);
  assert.equal((await f.post('/api/queue/scoped/join',body)).status,409);
});
test('Find after a locally retired Cancel reconciles only the existing unit', {timeout:5000}, async t => {
  const f = await fixture(t), stream = await f.stream(), first = f.joinBody(stream,1);
  assert.equal((await f.post('/api/queue/scoped/join',first)).status,200);
  const unit = f.internals('BB5').queueOf.get(f.A), stamp = JSON.stringify(unit);
  const nonce = f.scope(stream).queue_unit, second = f.joinBody(stream,2);
  const result = await f.post('/api/queue/scoped/join',second);
  assert.equal(result.status,200);
  assert.equal(result.body.already_queued,true);
  assert.equal(result.body.queue_unit,nonce);
  assert.equal(stream.events.findLast(e => e.type === 'queued').queue_unit,nonce);
  assert.equal(f.internals('BB5').queue.length,1);
  assert.equal(JSON.stringify(unit),stamp);
  // Attempt 2 never becomes the unobserved cancellation owner.
  assert.equal((await f.post('/api/queue/scoped/leave',second)).status,409);
  assert.equal((await f.post('/api/queue/scoped/join',first)).status,200);
  assert.equal((await f.post('/api/queue/scoped/leave',{...second,queue_unit:nonce})).status,200);
  assert.equal(f.internals('BB5').queue.length,0);
});
test('old Cancel reaching the server first defeats the later stale Find', {timeout:5000}, async t => {
  const f = await fixture(t), stream = await f.stream(), first = f.joinBody(stream,1);
  assert.equal((await f.post('/api/queue/scoped/join',first)).status,200);
  const second = f.joinBody(stream,2);
  assert.equal((await f.post('/api/queue/scoped/leave',first)).status,200);
  assert.equal((await f.post('/api/queue/scoped/join',second)).status,409);
  assert.equal(f.internals('BB5').queue.length,0);
});
test('reconciliation refuses malformed, stale and inconsistent party identities', {timeout:5000}, async t => {
  const f = await fixture(t), stream = await f.stream();
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(stream,1))).status,200);
  const second = f.joinBody(stream,2), unit = f.internals('BB5').queueOf.get(f.A);
  for (const altered of [{queue_actor:'x'.repeat(32)}, {queue_context:'x'.repeat(32)},
    {queue_attempt:0}, {queue_unit:'x'.repeat(32)}]) {
    const result = await f.post('/api/queue/scoped/join',{...second,...altered});
    assert.notEqual(result.status,200);
    assert.equal(result.body.queue_unit,undefined);
  }
  unit.code = 'inconsistent';
  assert.equal((await f.post('/api/queue/scoped/join',second)).status,409);
  unit.code = '';
  unit.members.push(f.B);
  assert.equal((await f.post('/api/queue/scoped/join',second)).status,409);
  unit.members.pop();
  assert.equal(f.internals('BB5').queueOf.get(f.A),unit);
});
test('same-token replacement keeps authority private and old close cannot drop queue', {timeout:5000}, async t => {
  const f = await fixture(t), old = await f.stream(), oldBody = f.joinBody(old,1), mark = old.events.length;
  const current = await f.stream(), fresh = f.scope(current);
  assert.notEqual(oldBody.queue_actor,fresh.queue_actor);
  f.service.broadcastStats();
  assert.equal(old.events.slice(mark).some(e => e.queue_actor === fresh.queue_actor),false);
  const rejection = await f.post('/api/queue/scoped/join',oldBody);
  assert.equal(rejection.status,409);
  assert.equal(JSON.stringify(rejection.body).includes(fresh.queue_actor),false);
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(current,1))).status,200);
  const unit = f.internals('BB5').queueOf.get(f.A);
  assert(unit);
  old.close();
  assert.equal(f.internals('BB5').queueOf.get(f.A),unit);
  assert.equal((await f.post('/api/queue/scoped/leave',oldBody)).status,409);
  assert.equal(f.internals('BB5').queueOf.get(f.A),unit);
});
test('stream replacement invalidates awaiting admission', {timeout:5000}, async t => {
  const f = await fixture(t), old = await f.stream(), gate = f.pauseAdmission();
  const joining = f.post('/api/queue/scoped/join',f.joinBody(old,1));
  await gate.entered;
  let current;
  try { current = await f.stream(); } finally { gate.release(); }
  assert.equal((await joining).status,409);
  assert.equal(f.internals('BB5').queueOf.has(f.A),false);
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(current,1))).status,200);
});
test('multiplexed modes isolate actors and only one simultaneous admission wins', {timeout:5000}, async t => {
  const f = await fixture(t,true), stream = await f.stream();
  const bb5 = f.joinBody(stream,1,'BB5'), bb1 = f.joinBody(stream,1,'BB1');
  assert.notEqual(bb5.queue_actor,bb1.queue_actor);
  assert.equal((await f.post('/api/queue/scoped/join',bb5,'BB1')).status,409);
  const result = await Promise.all([f.post('/api/queue/scoped/join',bb5,'BB5'),f.post('/api/queue/scoped/join',bb1,'BB1')]);
  assert.deepEqual(result.map(r => r.status).sort(),[200,409]);
  const occupied = ['BB5','BB1'].filter(mode => f.internals(mode).queueOf.has(f.A) || f.internals(mode).inMatch.has(f.A));
  assert.equal(occupied.length,1);
  const winner = occupied[0], other = winner === 'BB5' ? 'BB1' : 'BB5';
  const unit = f.internals(winner).queueOf.get(f.A), leave = {...f.scope(stream,winner),queue_attempt:1};
  assert(unit);
  assert.equal((await f.post('/api/queue/scoped/leave',leave,other)).status,409);
  assert.equal(f.internals(winner).queueOf.get(f.A),unit);
  assert.equal((await f.post('/api/queue/scoped/leave',leave,winner)).status,200);
  assert.equal(f.internals(winner).queueOf.has(f.A),false);
});
test('duplicate cancellation cannot unqueue a newer legacy search', {timeout:5000}, async t => {
  const f = await fixture(t), stream = await f.stream();
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(stream,1))).status,200);
  const cancellation = {...f.scope(stream),queue_attempt:1};
  assert.equal((await f.post('/api/queue/scoped/leave',cancellation)).status,200);
  assert.equal((await f.post('/api/queue/join')).status,200);
  const unit = f.internals('BB5').queueOf.get(f.A), count = stream.events.filter(e => e.type === 'unqueued').length;
  assert.equal((await f.post('/api/queue/scoped/leave',cancellation)).status,200);
  assert.equal(f.internals('BB5').queueOf.get(f.A),unit);
  assert.equal(stream.events.filter(e => e.type === 'unqueued').length,count);
});
test('strict endpoints retain authentication, version gates and bounded body validation', {timeout:5000}, async t => {
  const f = await fixture(t), stream = await f.stream();
  for (const action of ['join','leave']) {
    const path = '/api/queue/scoped/' + action;
    assert.equal(live.owns(path),true);
    assert.equal((await f.post(path)).status,400);
    assert.equal((await f.post(path,f.joinBody(stream,1),'BB5',{},'invalid')).status,401);
    for (const raw of ['{','[]','null','true','1','"text"',' '.repeat(1025)])
      assert.equal((await f.post(path,{},'BB5',{},f.A,Buffer.from(raw))).status,400);
  }
  assert.equal(live.owns('/api/queue/scoped/unknown'),false);
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(stream,1),'BB5',{'x-hub-version':'0.0.1'})).status,426);
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(stream,2))).status,200);
  assert.equal((await f.post('/api/queue/scoped/leave',{...f.scope(stream),queue_attempt:2},'BB5',{'x-hub-version':'0.0.1'})).status,200);
});

for (const mode of ['BB5','BB1']) for (const unavailable of [false,true]) {
  test(`network profile invalidation through ${mode} fences awaiting BB5 admission (${unavailable})`, {timeout:5000}, async t => {
    const f = await fixture(t,true), stream = await f.stream(), body = f.joinBody(stream,1);
    const gate = f.pauseAdmission(), joining = f.post('/api/queue/scoped/join',body);
    await gate.entered;
    try {
      const update = unavailable ? {unavailable:true} : {region:'NA',cross_region:false,
        location:'2'.repeat(32),transport:'webrtc-relay-v1',age_seconds:0};
      assert.equal((await f.post('/api/network/profile',update,mode)).status,200);
      // A ready profile by final admission does not restore the retired intent.
      f.internals(mode).networkRegistry.profile(f.A,{region:'NA',cross_region:false,
        location:'3'.repeat(32),transport:'webrtc-relay-v1',age_seconds:0},Date.now());
    } finally { gate.release(); }
    assert.equal((await joining).status,409);
    assert.equal(f.internals('BB5').queueOf.has(f.A),false);
    assert.equal(stream.events.some(e => e.type === 'queued'),false);
    assert.notEqual(f.scope(stream).queue_context,body.queue_context);
  });
}

test('older valid member credential cannot satisfy revoked current queue actor', {timeout:5000}, async t => {
  const f = await fixture(t), leader = await f.stream();
  await f.stream(f.B);
  await f.stream('replacement-b');
  const party = await f.post('/api/party/create');
  assert.equal((await f.post('/api/party/join',{code:party.body.code},'BB5',{},f.B)).status,200);
  f.revoked.add('replacement-b');
  assert.equal((await f.post('/api/queue/scoped/join',f.joinBody(leader,1))).status,409);
  assert.equal(f.internals('BB5').queueOf.has(f.A),false);
  assert.equal(f.internals('BB5').queueOf.has(f.B),false);
  assert.equal(leader.events.some(e => e.type === 'queued'),false);
});
