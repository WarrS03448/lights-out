'use strict';
// node --test scripts/test-scope-guards.cjs; synthetic streams, no external services.
//
// Guards that test-party-action-scopes.cjs and test-queue-scopes-http.cjs leave unpinned. Each
// test names the live.cjs line it holds; delete that line and the test fails. Kept in its own file
// so the ported scope suites stay byte-identical on main and on the Linux branch.
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
delete process.env.COMP_NETWORK_TEST_BYPASS;
process.env.COMP_MATCH_SIZE = '10';
const live = require('../live.cjs');

const ids = ['76561198000000001', '76561198000000002', '76561198000000003'];
const PREFIX = 'scope-guard-test:';

// ------------------------------------------------------------------ party contexts

function partyFixture(t) {
  const events = [];
  const service = live.create({whoami:async token => ({steam_id:token, auth_method:'steam'}),
    bearer:req => req.token, sendJson:(res, status, body) => Object.assign(res, {status, body}),
    readBody:async req => Buffer.from(JSON.stringify(req.body || {}))});
  const I = service._internals;
  for (const id of ids) {
    I.clients.set(id, {steamId:id, gameSteamId:id, token:id, res:{write:data => events.push([id, data]), end() {}}});
    I.bySteam.set(id, new Set([id]));
  }
  const request = async (action, body = {}, id = ids[0], scoped = true) => {
    const res = {};
    await service.route({token:id, headers:{}, body}, res, 'POST', '/api/party/' + (scoped ? 'scoped/' : '') + action);
    return res;
  };
  t.after(() => service.shutdown());
  return {I, events, request, context:id => I.partyContext(id || ids[0])};
}

// joinPartyByCode: rotatePartyContext(steamId) after partyOf.set(steamId, code).
test('a solo action prepared before joining by invite cannot move the player afterwards', async t => {
  const f = partyFixture(t);
  const solo = f.context(ids[1]);                        // B is solo when both clicks are made
  const p = await f.request('create', {}, ids[0], false);  // A's party P
  const q = await f.request('create', {}, ids[2], false);  // C's party Q
  f.I.friends.set(ids[0], new Set([ids[1]]));
  assert.equal((await f.I.inviteToParty(ids[0], {target:ids[1]})).ok, true);
  const invite = f.I.partyInvites.get(ids[1]).get(ids[0]).invite_id;
  const accepted = await f.request('invite/accept',
    {from:ids[0], expected_party_context:solo, expected_invite_id:invite}, ids[1]);
  assert.equal(accepted.status, 200);
  assert.notEqual(accepted.body.party_context, solo);
  assert.equal(f.I.partyOf.get(ids[1]), p.body.code);
  // B's second click, a join by code built from the same solo state, lands late.
  const late = await f.request('join', {code:q.body.code, expected_party_context:solo}, ids[1]);
  assert.equal(late.status, 409);
  assert.equal(late.body.stale_action, true);
  assert.equal(f.I.partyOf.get(ids[1]), p.body.code, 'B stays in the party they just joined');
  assert.deepEqual(f.I.parties.get(q.body.code).members, [ids[2]]);
});

// handlePartyRefresh: rotatePartyContext(id) for every member of the re-keyed party.
test('refresh-code retires every member context captured before it', async t => {
  const f = partyFixture(t);
  const created = await f.request('create', {}, ids[0], false);
  assert.equal((await f.request('join', {code:created.body.code}, ids[1], false)).status, 200);
  const leader = f.context(ids[0]), member = f.context(ids[1]);
  const refreshed = await f.request('refresh-code', {expected_party_context:leader});
  assert.equal(refreshed.status, 200);
  assert.notEqual(refreshed.body.party_context, leader);
  const code = refreshed.body.code;
  // A duplicate click built before the first answer arrived must not re-key the party again.
  const duplicate = await f.request('refresh-code', {expected_party_context:leader});
  assert.equal(duplicate.status, 409);
  assert.equal(duplicate.body.stale_action, true);
  assert.equal(f.I.partyOf.get(ids[0]), code);
  assert.equal(f.I.parties.has(code), true);
  // The member's context rotates too: a leave they prepared before the refresh is refused.
  const leave = await f.request('leave', {expected_party_context:member}, ids[1]);
  assert.equal(leave.status, 409);
  assert.equal(leave.body.stale_action, true);
  assert.deepEqual(f.I.parties.get(code).members, [ids[0], ids[1]]);
});

// invitePayload: the (invite.party && invite.party !== party) filter.
test('an invite to a dissolved party is not listed when a new party reuses its code', async t => {
  const f = partyFixture(t);
  const created = await f.request('create', {}, ids[0], false), code = created.body.code;
  f.I.friends.set(ids[0], new Set([ids[1]]));
  assert.equal((await f.I.inviteToParty(ids[0], {target:ids[1]})).ok, true);
  assert.equal(f.I.invitePayload(ids[1]).invites.length, 1);
  assert.equal((await f.request('leave', {}, ids[0], false)).status, 200);  // P dissolves
  assert.equal(f.I.parties.has(code), false);
  assert.equal(f.I.partyInvites.get(ids[1])?.has(ids[0]), true, 'the row is still stored');
  // Codes are random, so stand in for a reuse by giving C's party the same code (ABA).
  f.I.parties.set(code, {code, leaderId:ids[2], members:[ids[2]], created:Date.now()});
  f.I.partyOf.set(ids[2], code);
  assert.deepEqual(f.I.invitePayload(ids[1]).invites, []);
  assert.equal(f.I.partyInvites.has(ids[1]), false);
});

// ------------------------------------------------------------------ queue admission

async function queueFixture(t, {dual = false, store = null} = {}) {
  const A = ids[0];
  let held = null;
  const streams = [];
  const options = {
    prefix:PREFIX,
    whoami:async token => {
      if (held && token === A && held.skip-- === 0) {
        const gate = held;
        held = null;
        gate.enter();
        await gate.wait;
      }
      return token === A ? {steam_id:A, auth_method:'steam'} : null;
    },
    bearer:req => req.token,
    sendJson:(res, status, body) => Object.assign(res, {status, body}),
    badRequest() {}, readBody:async req => Buffer.from(JSON.stringify(req.body ?? {})),
    upstashCmd:store, requiredVersions:() => ({hub:'2.3.85', mode:'1.0.27'}),
  };
  const service = dual ? require('../ranked-service.cjs').create(options) : live.create(options);
  t.after(async () => { for (const s of streams) s.close(); await service.shutdown(); });
  await service._internals.ready;
  const internals = mode => dual ? service.forMode(mode)._internals : service._internals;
  const headers = (mode = 'BB5', extra = {}) => ({'x-ranked-mode':mode,
    'x-hub-version':'2.3.85', 'x-mode-version':'1.0.27',
    'x-bb5-version':'1.0.27', 'x-bb1-version':'1.0.27', ...extra});
  const measure = (mode, location) => internals(mode).networkRegistry.profile(A, {
    region:'NA', cross_region:false, location, transport:'webrtc-relay-v1', age_seconds:0}, Date.now());
  async function post(path, body = {}, mode = 'BB5', extra = {}) {
    if (path.endsWith('/join')) measure(mode, '1'.repeat(32));
    const res = {setHeader() {}};
    await service.route({token:A, body, headers:headers(mode, extra)}, res, 'POST', path,
      new URL('http://fixture' + path));
    return res;
  }
  async function stream() {
    const req = Object.assign(new EventEmitter(), {token:A, headers:headers(), socket:{setTimeout() {}}});
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
    return result;
  }
  function joinBody(s, attempt) {
    const event = s.events.findLast(e => (e.mode || 'BB5') === 'BB5' && typeof e.queue_actor === 'string');
    assert(event?.queue_actor, 'No current BB5 actor');
    return {queue_actor:event.queue_actor, queue_context:event.queue_context, queue_attempt:attempt, queue_unit:''};
  }
  // Holds the join inside its own credential recheck, after every storage read.
  function pauseAdmission() {
    assert.equal(held, null);
    let enter, release;
    const entered = new Promise(resolve => { enter = resolve; });
    const wait = new Promise(resolve => { release = resolve; });
    held = {skip:1, enter, wait};
    return {entered, release};
  }
  return {A, internals, measure, post, stream, joinBody, pauseAdmission};
}

// handleQueueJoin: retryable = status >= 500, then release(ticket) or abort(ticket) in finally.
// The Linux client resends the same attempt on a 503 (competitive.py RETRY_STATUSES).
// The ban key is also read by the route's account check before the join starts, so that read is
// let through and the join's own read is the one that fails.
for (const [read, skip, error] of [['penalty', 0, 'Could not verify queue eligibility. Please try again.'],
                                   ['ban', 1, 'Could not load your rank. Please try again.']]) {
  test(`a 503 from the ${read} read leaves the same scoped attempt retryable`, {timeout:5000}, async t => {
    let failing = null;
    const store = async ([command, key]) => {
      if (command === 'GET' && failing && key === failing.key && failing.skip-- === 0) {
        failing = null;
        throw new Error('storage unavailable');
      }
      return command === 'SMEMBERS' ? [] : null;
    };
    const f = await queueFixture(t, {store}), s = await f.stream(), body = f.joinBody(s, 1);
    failing = {key:PREFIX + read + ':' + f.A, skip};
    const first = await f.post('/api/queue/scoped/join', body);
    assert.equal(failing, null, 'the failing read was reached');
    assert.equal(first.status, 503);
    assert.equal(first.body.error, error);
    assert.equal(f.internals('BB5').queueOf.has(f.A), false);
    const retry = await f.post('/api/queue/scoped/join', body);
    assert.equal(retry.status, 200);
    assert.equal(f.internals('BB5').queueOf.has(f.A), true);
  });
}

test('a refusal below 500 retires the attempt, so the identical body cannot revive it', {timeout:5000}, async t => {
  const f = await queueFixture(t), s = await f.stream(), body = f.joinBody(s, 1);
  const refused = await f.post('/api/queue/scoped/join', body, 'BB5', {'x-hub-version':'0.0.1'});
  assert.equal(refused.status, 426);
  const again = await f.post('/api/queue/scoped/join', body);
  assert.equal(again.status, 409);
  assert.equal(again.body.code, 'queue_attempt_retired');
  assert.equal(f.internals('BB5').queueOf.has(f.A), false);
  // A new attempt from the same stream is the way back in.
  assert.equal((await f.post('/api/queue/scoped/join', f.joinBody(s, 2))).status, 200);
});

// networkQueueChanged: a legacy (Windows) join in flight is left alone, exactly as before scoped
// queues existed. Its own synchronous checks read the new profile before it enqueues.
for (const mode of ['BB5', 'BB1']) for (const unavailable of [false, true]) {
  test(`a legacy join in flight survives a profile change through ${mode} (${unavailable ? 'unavailable' : 'changed'})`,
    {timeout:5000}, async t => {
      const f = await queueFixture(t, {dual:true}), s = await f.stream();
      const gate = f.pauseAdmission(), joining = f.post('/api/queue/join');
      await gate.entered;
      try {
        const update = unavailable ? {unavailable:true} : {region:'NA', cross_region:false,
          location:'2'.repeat(32), transport:'webrtc-relay-v1', age_seconds:0};
        assert.equal((await f.post('/api/network/profile', update, mode)).status, 200);
        if (unavailable) f.measure(mode, '3'.repeat(32));
      } finally { gate.release(); }
      const joined = await joining;
      assert.equal(joined.status, 200);
      assert.equal(f.internals('BB5').queueOf.has(f.A), true);
      assert.equal(s.events.some(e => e.type === 'unqueued'), false);
    });
}

test('a legacy join still refuses a member whose profile went unavailable mid-join', {timeout:5000}, async t => {
  const f = await queueFixture(t, {dual:true}), s = await f.stream();
  const gate = f.pauseAdmission(), joining = f.post('/api/queue/join');
  await gate.entered;
  try { assert.equal((await f.post('/api/network/profile', {unavailable:true})).status, 200); }
  finally { gate.release(); }
  const joined = await joining;
  assert.equal(joined.status, 409);
  assert.equal(joined.body.network_unready, true);
  assert.equal(f.internals('BB5').queueOf.has(f.A), false);
  assert.equal(s.events.some(e => e.type === 'queued'), false);
});

// competitionGuard.networkChanged: the registry is shared, so a queued legacy search leaves the
// queue on a profile change whichever ladder the hub posted it through.
test('a queued legacy 1v1 search is dropped by a profile change posted through BB5', {timeout:5000}, async t => {
  const f = await queueFixture(t, {dual:true}), s = await f.stream();
  assert.equal((await f.post('/api/queue/join', {}, 'BB1')).status, 200);
  assert.equal(f.internals('BB1').queueOf.has(f.A), true);
  assert.equal((await f.post('/api/network/profile', {region:'NA', cross_region:false,
    location:'2'.repeat(32), transport:'webrtc-relay-v1', age_seconds:0}, 'BB5')).status, 200);
  assert.equal(f.internals('BB1').queueOf.has(f.A), false);
  const unqueued = s.events.filter(e => e.type === 'unqueued');
  assert.deepEqual(unqueued.map(e => e.mode), ['BB1']);
});
