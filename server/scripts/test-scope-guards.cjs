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

// server.cjs readBody: a body over the limit rejects (err.tooLarge) instead of arriving cut short.
async function fixtureBody(req, limit) {
  const raw = Buffer.from(req.raw ?? JSON.stringify(req.body ?? {}));
  if (limit && raw.length > limit) throw Object.assign(Error('Request body too large.'), {tooLarge:true});
  return raw;
}

function partyFixture(t) {
  const events = [];
  const service = live.create({whoami:async token => ({steam_id:token, auth_method:'steam'}),
    bearer:req => req.token, sendJson:(res, status, body) => Object.assign(res, {status, body}),
    readBody:fixtureBody});
  const I = service._internals;
  for (const id of ids) {
    I.clients.set(id, {steamId:id, gameSteamId:id, token:id, scoped:true, res:{write:data => events.push([id, data]), end() {}}});
    I.bySteam.set(id, new Set([id]));
  }
  // `raw`, when given, is the request body exactly as sent, in place of JSON.stringify(body).
  const request = async (action, body = {}, id = ids[0], scoped = true, raw = undefined) => {
    const res = {};
    await service.route({token:id, headers:{'x-hub-platform':'linux'}, body, raw}, res, 'POST', '/api/party/' + (scoped ? 'scoped/' : '') + action);
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

// readPartyActionBody: a body that is not a JSON object, or is over 4096 bytes, reads as
// {expected_party_context:null}. No context matches null, so even a legacy route refuses it from a
// scoped client rather than running it as an unscoped create or leave. (A Windows hub keeps
// 2d25405's reading, an unreadable body is {}: test-legacy-wire.cjs.)
const UNREADABLE = [['cut-off JSON', '{'], ['an array', '[]'], ['null', 'null'], ['a bare value', 'true'],
  ['a body over 4096 bytes', JSON.stringify({code:'', pad:'x'.repeat(4096)})]];
for (const [what, raw] of UNREADABLE) {
  test(`a legacy party create or leave with ${what} is refused as stale and changes nothing`, async t => {
    const f = partyFixture(t);
    const create = await f.request('create', undefined, ids[0], false, raw);
    assert.equal(create.status, 409);
    assert.equal(create.body.stale_action, true);
    assert.equal(f.I.partyOf.has(ids[0]), false);
    assert.equal(f.I.parties.size, 0);
    const created = await f.request('create', {}, ids[0], false), code = created.body.code;
    assert.equal((await f.request('join', {code}, ids[1], false)).status, 200);
    const leave = await f.request('leave', undefined, ids[1], false, raw);
    assert.equal(leave.status, 409);
    assert.equal(leave.body.stale_action, true);
    assert.deepEqual(f.I.parties.get(code).members, [ids[0], ids[1]]);
  });
}

test('a legacy party create with an empty body or {} still runs unscoped', async t => {
  for (const raw of ['', '{}']) {
    const f = partyFixture(t);
    const create = await f.request('create', undefined, ids[0], false, raw);
    assert.equal(create.status, 200);
    assert.equal(f.I.partyOf.get(ids[0]), create.body.code);
  }
});

// ------------------------------------------------------------------ queue admission

// Every player's token is their SteamID. Requests and streams are A's unless a token is passed.
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
      return ids.includes(token) ? {steam_id:token, auth_method:'steam'} : null;
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
  // A Linux client on every request and on the stream (hub/live.py _stamp): the scopes are theirs.
  const headers = (mode = 'BB5', extra = {}) => ({'x-ranked-mode':mode, 'x-hub-platform':'linux',
    'x-hub-version':'2.3.85', 'x-mode-version':'1.0.27',
    'x-bb5-version':'1.0.27', 'x-bb1-version':'1.0.27', ...extra});
  const measure = (mode, location) => internals(mode).networkRegistry.profile(A, {
    region:'NA', cross_region:false, location, transport:'webrtc-relay-v1', age_seconds:0}, Date.now());
  async function post(path, body = {}, mode = 'BB5', extra = {}, token = A) {
    if (path.endsWith('/join')) measure(mode, '1'.repeat(32));
    const res = {setHeader() {}};
    await service.route({token, body, headers:headers(mode, extra)}, res, 'POST', path,
      new URL('http://fixture' + path));
    return res;
  }
  async function stream(token = A) {
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

// networkQueueChanged: `if (queued || pending)`. A scoped admission still in flight when the
// profile goes unavailable is told why, with network_unready and the error text, rather than
// only getting the generic 409 its fenced join returns. That holds whichever ladder the profile
// was posted through.
for (const mode of ['BB5', 'BB1']) {
  test(`a scoped join in flight is told the profile went unavailable through ${mode}`, {timeout:5000}, async t => {
    const f = await queueFixture(t, {dual:true}), s = await f.stream();
    const gate = f.pauseAdmission(), joining = f.post('/api/queue/scoped/join', f.joinBody(s, 1));
    await gate.entered;
    try { assert.equal((await f.post('/api/network/profile', {unavailable:true}, mode)).status, 200); }
    finally { gate.release(); }
    const joined = await joining;
    assert.equal(joined.status, 409);
    assert.equal(f.internals('BB5').queueOf.has(f.A), false);
    const told = s.events.filter(e => e.type === 'unqueued');
    assert.equal(told.length, 1);
    assert.equal(told[0].mode, 'BB5');
    assert.equal(told[0].network_unready, true);
    assert.equal(told[0].error, 'Connection measurements are unavailable. Check Settings before searching again.');
  });
}

// handleNetwork, /api/network/profile: (inMatch.has(id) || competitionGuard?.inMatch?.(id)).
// Both ladders share one registry, so a region change posted through BB5 would rotate the profile
// and its revision underneath a running BB1 match.
test('a region change through one ladder is refused while the player is in a match on the other', {timeout:5000}, async t => {
  const f = await queueFixture(t, {dual:true});
  const registry = f.internals('BB5').networkRegistry;
  assert.equal(registry, f.internals('BB1').networkRegistry, 'one registry for both ladders');
  f.measure('BB5', '1'.repeat(32));
  const before = registry.player(f.A).revision;
  f.internals('BB1').inMatch.set(f.A, 'duel-match');
  const change = {region:'EU', cross_region:false, location:'1'.repeat(32), transport:'webrtc-relay-v1', age_seconds:0};
  const refused = await f.post('/api/network/profile', change, 'BB5');
  assert.equal(refused.status, 409);
  assert.equal(refused.body.error, 'Finish your match before changing regions.');
  assert.equal(registry.player(f.A).revision, before);
  assert.deepEqual(registry.preferences.get(f.A), {region:'NA', cross_region:false});
  // With the match over, the same change goes through: the refusal was the match's.
  f.internals('BB1').inMatch.delete(f.A);
  const accepted = await f.post('/api/network/profile', change, 'BB5');
  assert.equal(accepted.status, 200);
  assert.equal(accepted.body.region, 'EU');
  assert.notEqual(registry.player(f.A).revision, before);
});

// The scoped queue routes: all four fields must be present. The ticket logic treats queue_unit
// as optional, so without this check a join body that leaves it out would be admitted.
for (const missing of ['queue_actor', 'queue_context', 'queue_attempt', 'queue_unit']) {
  test(`a scoped join or leave without ${missing} is refused as invalid`, {timeout:5000}, async t => {
    const f = await queueFixture(t), s = await f.stream(), body = f.joinBody(s, 1);
    const partial = {...body};
    delete partial[missing];
    const refused = await f.post('/api/queue/scoped/join', partial);
    assert.equal(refused.status, 400);
    assert.equal(refused.body.code, 'queue_scope_invalid');
    assert.equal(f.internals('BB5').queueOf.has(f.A), false);
    // The refusal used nothing up: the complete body for the same attempt still joins.
    assert.equal((await f.post('/api/queue/scoped/join', body)).status, 200);
    const unit = s.events.findLast(e => e.type === 'queued')?.queue_unit;
    assert.match(unit, /^[A-Za-z0-9_-]{16,128}$/);
    const leave = {...body, queue_unit:unit};
    delete leave[missing];
    const kept = await f.post('/api/queue/scoped/leave', leave);
    assert.equal(kept.status, 400);
    assert.equal(kept.body.code, 'queue_scope_invalid');
    assert.equal(f.internals('BB5').queueOf.has(f.A), true);
  });
}

// drop: queueScopes.close(...). Without it the scope row of a player whose last stream closed is
// never collected: one row for every player who ever connected, until the next restart.
test('closing the last stream collects the player queue-scope row on both ladders', async t => {
  const f = await queueFixture(t, {dual:true}), s = await f.stream();
  for (const mode of ['BB5', 'BB1'])
    assert.equal(f.internals(mode).queueScopes.players.get(f.A)?.actor?.player, f.A, mode);
  s.close();
  for (const mode of ['BB5', 'BB1']) {
    assert.equal(f.internals(mode).bySteam.has(f.A), false, mode);
    assert.equal(f.internals(mode).queueScopes.players.has(f.A), false, mode);
  }
});

// ------------------------------------------------------------------ the connect-time party_update

// handleStream: send(clientId, partyPayload(partyOf.get(account.player_id))) on every connect,
// solo or not. For a solo player it is the only event that carries a party_context, and the
// Linux client sends the context it holds as expected_party_context on every party action
// (hub/live.py prepare_party_action). Without it a solo player's create, join by code and invite
// accept are all refused as stale.
const partyUpdates = s => s.events.filter(e => e.type === 'party_update');

test('a solo stream is handed its party context on connect, and a scoped create accepts it', async t => {
  const f = await queueFixture(t), s = await f.stream();
  const [update, ...more] = partyUpdates(s);
  assert.deepEqual(more, []);
  assert.equal(update?.code, null);
  assert.match(update.party_context, /^[a-f0-9]{32}$/);
  const created = await f.post('/api/party/scoped/create', {expected_party_context:update.party_context});
  assert.equal(created.status, 200);
  assert.equal(f.internals('BB5').partyOf.get(f.A), created.body.code);
});

test('a party member who reconnects inside the grace window is handed the roster again', async t => {
  const f = await queueFixture(t), B = ids[1];
  const a = await f.stream(), b = await f.stream(B);
  const created = await f.post('/api/party/scoped/create', {expected_party_context:partyUpdates(a)[0].party_context});
  assert.equal(created.status, 200);
  const code = created.body.code;
  const joined = await f.post('/api/party/scoped/join', {code, expected_party_context:partyUpdates(b)[0].party_context},
    'BB5', {}, B);
  assert.equal(joined.status, 200);
  b.close();
  assert.equal(f.internals('BB5').partyGrace.has(B), true);
  const back = await f.stream(B);
  const [update, ...more] = partyUpdates(back);
  assert.deepEqual(more, []);
  assert.equal(update.code, code);
  assert.deepEqual(update.members.map(m => m.player_id), [f.A, B]);
  assert.equal(f.internals('BB5').partyGrace.has(B), false);
  // The context it carries is the current one: a scoped leave built from it is accepted.
  const left = await f.post('/api/party/scoped/leave', {expected_party_context:update.party_context}, 'BB5', {}, B);
  assert.equal(left.status, 200);
  assert.deepEqual(f.internals('BB5').parties.get(code).members, [f.A]);
});
