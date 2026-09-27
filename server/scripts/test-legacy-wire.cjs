'use strict';
// node --test scripts/test-legacy-wire.cjs; synthetic streams, no external services.
//
// A WINDOWS HUB CANNOT TELL THE SCOPES ARE THERE. The 2026-09-27 deploy of the action scopes
// stalled the only two matches that reached the connect window and the cause was never found, so
// they came back behind scopedClient (live.cjs): a client that does not send
// `x-hub-platform: linux` (or `x-hub-action-scopes: 1`) is answered exactly as 2d25405 answered
// it. These tests pin that wire, the scoped client's side of the same switch, and the two
// [connect] log lines that are the evidence for next time.
const test = require('node:test');
const assert = require('node:assert/strict');
const util = require('node:util');
const {EventEmitter} = require('node:events');
delete process.env.COMP_NETWORK_TEST_BYPASS;
process.env.COMP_MATCH_SIZE = '10';
process.env.COMP_CONNECT_SILENT_SECONDS = '1';   // the floor, so a silent host is logged in a second
const live = require('../live.cjs');
const identity = require('../player-identity.cjs');

const [A, B, C] = ['76561198000000001', '76561198000000002', '76561198000000003'];
const WINDOWS = {};                                // a 3.0.x hub sends no platform header
const LINUX = {'x-hub-platform':'linux'};
const SCOPE_KEYS = ['queue_actor', 'queue_context', 'queue_unit', 'queue_attempt', 'party_context', 'capabilities'];
const MATCH = 'abcdef0123456789';

async function fixture(t, {dual = false, store = null} = {}) {
  let held = null;
  const streams = [];
  const options = {
    prefix:'legacy-wire-test:',
    whoami:async token => {
      if (held && token === held.token && held.skip-- === 0) {
        const gate = held;
        held = null;
        gate.enter();
        await gate.wait;
      }
      return [A, B, C].includes(token) ? {steam_id:token, auth_method:'steam'} : null;
    },
    bearer:req => req.token,
    sendJson:(res, status, body) => Object.assign(res, {status, body}),
    badRequest() {},
    // server.cjs readBody: over the limit rejects (err.tooLarge). Every read is counted, because
    // "2d25405 never read this body" is part of the contract.
    readBody:async (req, limit) => {
      req.reads = (req.reads || 0) + 1;
      req.onRead?.();
      const raw = Buffer.from(req.raw ?? JSON.stringify(req.body ?? {}));
      if (limit && raw.length > limit) throw Object.assign(Error('Request body too large.'), {tooLarge:true});
      return raw;
    },
    upstashCmd:store,
  };
  const service = dual ? require('../ranked-service.cjs').create(options) : live.create(options);
  t.after(async () => { for (const s of streams) s.close(); await service.shutdown(); });
  await service._internals.ready;
  const internals = (mode = 'BB5') => dual ? service.forMode(mode)._internals : service._internals;
  const measure = (id, location = '1'.repeat(32)) => internals().networkRegistry.profile(id, {
    region:'NA', cross_region:false, location, transport:'webrtc-relay-v1', age_seconds:0}, Date.now());
  async function post(token, client, path, body = {}, {mode = 'BB5', raw, onRead} = {}) {
    const req = {token, body, raw, onRead, headers:{'x-ranked-mode':mode, ...client}};
    const res = {setHeader() {}};
    await service.route(req, res, 'POST', path, new URL('http://fixture' + path));
    return Object.assign(res, {reads:req.reads || 0});
  }
  async function get(token, client, path, mode = 'BB5') {
    const res = {setHeader() {}};
    await service.route({token, headers:{'x-ranked-mode':mode, ...client}}, res, 'GET', path.split('?')[0],
      new URL('http://fixture' + path));
    return res;
  }
  async function stream(token, client) {
    const req = Object.assign(new EventEmitter(), {token, headers:{'x-ranked-mode':'BB5', ...client},
      socket:{setTimeout() {}}});
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
    const result = {events, close:() => res.end(), since:() => { const n = events.length; return () => events.slice(n); }};
    streams.push(result);
    return result;
  }
  // Holds `token`'s whoami number `skip` (0-based) until released: the route's own check is 0.
  function pause(token, skip) {
    let enter, release;
    const entered = new Promise(resolve => { enter = resolve; });
    const wait = new Promise(resolve => { release = resolve; });
    held = {token, skip, enter, wait};
    return {entered, release};
  }
  return {service, internals, measure, post, get, stream, pause};
}

const scopeFields = event => SCOPE_KEYS.filter(key => key in event);
function assertLegacyWire(events) {
  for (const event of events) assert.deepEqual(scopeFields(event), [], `${event.type} carries ${scopeFields(event)}`);
  for (const event of events.filter(e => e.type === 'party_invites'))
    for (const invite of event.invites) assert.equal('invite_id' in invite, false);
}

// ------------------------------------------------------------------ (a) the Windows stream

test('a Windows stream carries no scope field on any event and no solo party_update', async t => {
  const f = await fixture(t, {dual:true});
  const a = await f.stream(A, WINDOWS), b = await f.stream(B, WINDOWS), c = await f.stream(C, WINDOWS);
  for (const mode of ['BB5', 'BB1'])
    assert.ok(a.events.some(e => e.type === 'hello' && e.mode === mode), `${mode} hello`);
  assert.equal(a.events.some(e => e.type === 'party_update'), false, 'no party_update for a solo hub');

  // A party, an invite and a search: every event that used to be decorated.
  const created = await f.post(A, WINDOWS, '/api/party/create');
  assert.deepEqual(Object.keys(created.body), ['ok', 'code']);
  assert.deepEqual(Object.keys((await f.post(B, WINDOWS, '/api/party/join', {code:created.body.code})).body), ['ok', 'code']);
  f.internals().friends.set(A, new Set([C]));
  assert.equal((await f.post(A, WINDOWS, '/api/party/invite', {target:C})).status, 200);
  const inbox = c.events.findLast(e => e.type === 'party_invites');
  assert.deepEqual(Object.keys(inbox.invites[0]), ['from', 'code', 'size', 'max', 'expires_in']);
  for (const id of [A, B]) f.measure(id);
  assert.equal((await f.post(A, WINDOWS, '/api/queue/join')).status, 200);
  assert.equal((await f.post(A, WINDOWS, '/api/queue/leave')).status, 200);
  for (const s of [a, b]) {
    const kinds = new Set(s.events.map(e => e.type));
    for (const kind of ['hello', 'stats', 'party_update', 'queued', 'unqueued']) assert.ok(kinds.has(kind), kind);
  }
  for (const s of [a, b, c]) assertLegacyWire(s.events);
  // A party member still gets the roster replayed on reconnect, as before.
  const back = await f.stream(B, WINDOWS);
  assert.equal(back.events.filter(e => e.type === 'party_update').length, 1);
  assertLegacyWire(back.events);
});

// ------------------------------------------------------------------ (b) the scoped stream

test('a Linux stream still gets capabilities, the solo party context, queue scopes and invite ids', async t => {
  const f = await fixture(t, {dual:true});
  const a = await f.stream(A, LINUX), c = await f.stream(C, LINUX);
  for (const mode of ['BB5', 'BB1']) {
    const hello = a.events.find(e => e.type === 'hello' && e.mode === mode);
    assert.deepEqual(hello.capabilities, {match_action_scopes_v1:true, party_action_scopes_v1:true,
      queue_action_scopes_v1:true, hub_platform_gate_v1:true}, mode);
    assert.match(hello.queue_actor, /^[A-Za-z0-9_-]{16,128}$/, mode);
  }
  const solo = a.events.filter(e => e.type === 'party_update');
  assert.equal(solo.length, 1);
  assert.equal(solo[0].code, null);
  assert.match(solo[0].party_context, /^[a-f0-9]{32}$/);
  const created = await f.post(A, LINUX, '/api/party/scoped/create', {expected_party_context:solo[0].party_context});
  assert.equal(created.status, 200);
  assert.match(created.body.party_context, /^[a-f0-9]{32}$/);
  f.internals().friends.set(A, new Set([C]));
  assert.equal((await f.post(A, LINUX, '/api/party/scoped/invite',
    {target:C, expected_party_context:created.body.party_context})).status, 200);
  assert.match(c.events.findLast(e => e.type === 'party_invites').invites[0].invite_id, /^[a-f0-9]{32}$/);
});

test('only an exact platform value or the opt-in header makes a stream scoped', async t => {
  for (const [headers, scoped] of [[{'x-hub-action-scopes':'1'}, true], [{'x-hub-platform':'Linux'}, false],
    [{'x-hub-platform':'linux '}, false], [{'x-hub-platform':'windows'}, false],
    [{'x-hub-action-scopes':'true'}, false]]) {
    const f = await fixture(t);
    const s = await f.stream(A, headers);
    const hello = s.events.find(e => e.type === 'hello');
    assert.equal('capabilities' in hello, scoped, JSON.stringify(headers));
    assert.equal(s.events.some(e => e.type === 'party_update'), scoped, JSON.stringify(headers));
  }
});

// ------------------------------------------------------------------ (c) the old body fallbacks

test('Windows party routes read and answer as 2d25405 did; Linux ones stay strict', async t => {
  const f = await fixture(t);
  await f.stream(A, WINDOWS); await f.stream(B, WINDOWS);
  // create, leave and refresh-code never read a body, so an unreadable one changes nothing.
  const created = await f.post(A, WINDOWS, '/api/party/create', undefined, {raw:'{'});
  assert.equal(created.status, 200);
  assert.equal(created.reads, 0);
  assert.deepEqual(created.body, {ok:true, code:created.body.code});
  const code = created.body.code;
  // join reads 1024 bytes and treats anything unreadable as {} - no code, so no party.
  const padded = JSON.stringify({code, pad:'x'.repeat(1100)});
  assert.deepEqual((await f.post(B, WINDOWS, '/api/party/join', undefined, {raw:padded})).body,
    {ok:false, error:'No party with that code.'});
  assert.equal((await f.post(B, WINDOWS, '/api/party/join', undefined, {raw:'['})).status, 404);
  assert.deepEqual((await f.post(B, WINDOWS, '/api/party/join', {code})).body, {ok:true, code});
  const refreshed = await f.post(A, WINDOWS, '/api/party/refresh-code', undefined, {raw:'null'});
  assert.equal(refreshed.reads, 0);
  assert.deepEqual(Object.keys(refreshed.body), ['ok', 'code']);
  const left = await f.post(B, WINDOWS, '/api/party/leave', undefined, {raw:'[]'});
  assert.equal(left.reads, 0);
  assert.deepEqual(left.body, {ok:true, was_in_party:true});
  assert.deepEqual((await f.post(A, WINDOWS, '/api/party/invite', undefined, {raw:'null'})).body,
    {ok:false, error:'not a steamid64'});
  // Invite accept answers without party_context.
  f.internals().friends.set(A, new Set([B]));
  assert.equal((await f.post(A, WINDOWS, '/api/party/invite', {target:B})).status, 200);
  assert.deepEqual(Object.keys((await f.post(B, WINDOWS, '/api/party/invite/accept', {from:A})).body),
    ['ok', 'joined', 'code']);
  // The same unreadable body from a Linux client on the same route is refused (A21 is intact).
  const f2 = await fixture(t);
  const strict = await f2.post(C, LINUX, '/api/party/create', undefined, {raw:'{'});
  assert.equal(strict.status, 409);
  assert.equal(strict.body.stale_action, true);
  assert.equal(f2.internals().partyOf.has(C), false);
});

test('a Windows join clears its own grace timer before it reads the body, as 2d25405 did', async t => {
  const f = await fixture(t);
  await f.stream(A, WINDOWS);
  const b = await f.stream(B, WINDOWS);
  const code = (await f.post(A, WINDOWS, '/api/party/create')).body.code;
  assert.equal((await f.post(B, WINDOWS, '/api/party/join', {code})).status, 200);
  b.close();                                             // B's last stream: the grace timer is armed
  assert.equal(f.internals().partyGrace.has(B), true);
  let armedAtRead = null;
  const joined = await f.post(B, WINDOWS, '/api/party/join', {code},
    {onRead:() => { armedAtRead = f.internals().partyGrace.has(B); }});
  assert.equal(joined.status, 200);
  assert.equal(armedAtRead, false, 'cleared before the read, so it cannot fire during it');
});

// inviteToParty(me, body, scoped). A Windows caller's invite is 2d25405's in full: checked before
// the friends read only, re-sent with dropInvite, pointing at a code and not a party object.
test("a Windows inviter's resend is 2d25405's: the sole invite vanishes and its accept expires", async t => {
  const f = await fixture(t);
  await f.stream(A, WINDOWS);
  const c = await f.stream(C, WINDOWS);
  assert.equal((await f.post(A, WINDOWS, '/api/party/create')).status, 200);
  f.internals().friends.set(A, new Set([C]));
  const sent = await f.post(A, WINDOWS, '/api/party/invite', {target:C});
  assert.deepEqual(sent.body, {ok:true, sent:true, expires_in:f.internals().PARTY_INVITE_SECONDS});
  assert.equal(c.events.findLast(e => e.type === 'party_invites').invites.length, 1);
  const toC = c.since();
  assert.deepEqual((await f.post(A, WINDOWS, '/api/party/invite', {target:C})).body, sent.body);
  assert.deepEqual(toC().map(e => [e.type, (e.invites || []).length]), [['party_invites', 0], ['party_invite', 0]]);
  const accepted = await f.post(C, WINDOWS, '/api/party/invite/accept', {from:A});
  assert.equal(accepted.status, 409);
  assert.deepEqual(accepted.body, {ok:false, error:'That invite has expired.'});
  assert.equal(f.internals().partyOf.has(C), false);
  assertLegacyWire(c.events);
});

test("a Linux inviter's resend keeps the invite, and its accept seats the invitee", async t => {
  const f = await fixture(t);
  const a = await f.stream(A, LINUX), c = await f.stream(C, LINUX);
  const solo = a.events.find(e => e.type === 'party_update').party_context;
  const created = await f.post(A, LINUX, '/api/party/scoped/create', {expected_party_context:solo});
  f.internals().friends.set(A, new Set([C]));
  const invite = {target:C, expected_party_context:created.body.party_context};
  assert.equal((await f.post(A, LINUX, '/api/party/scoped/invite', invite)).status, 200);
  assert.equal((await f.post(A, LINUX, '/api/party/scoped/invite', invite)).status, 200);
  const rows = c.events.findLast(e => e.type === 'party_invites').invites;
  assert.equal(rows.length, 1);
  const context = c.events.findLast(e => e.type === 'party_update').party_context;
  const accepted = await f.post(C, LINUX, '/api/party/scoped/invite/accept',
    {from:A, expected_party_context:context, expected_invite_id:rows[0].invite_id});
  assert.equal(accepted.status, 200);
  assert.equal(f.internals().partyOf.get(C), created.body.code);
});

test('a Windows invite is not checked again after the friends read', async t => {
  // The friends read is the one await in inviteToParty. Hold it while the party re-keys.
  let hold = null;
  const store = async ([command, key]) => {
    if (command === 'SMEMBERS' && key === 'legacy-wire-test:friends:' + A) {
      if (hold) await hold.wait;
      return [C];
    }
    return command === 'SMEMBERS' ? [] : null;
  };
  for (const [client, route] of [[WINDOWS, '/api/party/invite'], [LINUX, '/api/party/scoped/invite']]) {
    const f = await fixture(t, {store});
    const a = await f.stream(A, client), c = await f.stream(C, client);
    const solo = a.events.find(e => e.type === 'party_update')?.party_context;
    const created = await f.post(A, client, route.replace('invite', 'create'), {expected_party_context:solo});
    const first = created.body.code;
    let release;
    hold = {wait:new Promise(resolve => { release = resolve; })};
    const inviting = f.post(A, client, route, {target:C, expected_party_context:created.body.party_context});
    await new Promise(setImmediate);
    const refreshed = await f.post(A, client, route.replace('invite', 'refresh-code'),
      {expected_party_context:created.body.party_context});
    assert.equal(refreshed.status, 200);
    hold = null;
    const toC = c.since();
    release();
    const answer = await inviting;
    if (client === WINDOWS) {
      // 2d25405: sent, for the code it was asked about. The refresh retired that code, so the
      // inbox drops the row on the way out and only the toast names it.
      assert.equal(answer.status, 200);
      assert.deepEqual(answer.body, {ok:true, sent:true, expires_in:f.internals().PARTY_INVITE_SECONDS});
      assert.deepEqual(toC().map(e => [e.type, e.invites?.length ?? e.code]),
        [['party_invites', 0], ['party_invite', first]]);
      assertLegacyWire(c.events);
    } else {
      assert.equal(answer.status, 409);
      assert.equal(answer.body.stale_action, true);
      assert.deepEqual(toC(), []);
    }
  }
});

test("a Windows invite names a code; a code minted again seats its invitee as 2d25405's did", async t => {
  for (const [client, prefix, joined] of [[WINDOWS, '/api/party/', true], [LINUX, '/api/party/scoped/', false]]) {
    const f = await fixture(t);
    const a = await f.stream(A, client), b = await f.stream(B, client), c = await f.stream(C, client);
    const context = s => s.events.findLast(e => e.type === 'party_update')?.party_context;
    const code = (await f.post(A, client, prefix + 'create', {expected_party_context:context(a)})).body.code;
    f.internals().friends.set(A, new Set([C]));
    assert.equal((await f.post(A, client, prefix + 'invite', {target:C, expected_party_context:context(a)})).status, 200);
    const row = c.events.findLast(e => e.type === 'party_invites').invites[0];
    assert.equal((await f.post(A, client, prefix + 'leave', {expected_party_context:context(a)})).status, 200);
    // B's new party is given the dissolved party's code, as freshPartyCode may.
    const I = f.internals();
    const other = (await f.post(B, client, prefix + 'create', {expected_party_context:context(b)})).body.code;
    const party = I.parties.get(other);
    I.parties.delete(other); party.code = code; I.parties.set(code, party); I.partyOf.set(B, code);
    const answer = await f.post(C, client, prefix + 'invite/accept',
      {from:A, expected_party_context:context(c), expected_invite_id:row.invite_id});
    assert.equal(answer.body.ok, joined, JSON.stringify(answer.body));
    assert.equal(I.partyOf.get(C) === code, joined);
  }
});

function matchFixture(f, state) {
  const match = identity.freezeMatch({id:MATCH, state, created:Date.now(),
    players:[A, B].map(steam_id => ({steam_id, accepted:state !== 'found', connected:false})),
    teams:{1:[A], 2:[B]}, host:A, left:[], map:'Airsoft'});
  f.internals().matches.set(MATCH, match);
  for (const id of [A, B]) f.internals().inMatch.set(id, MATCH);
  return match;
}

test('Windows match routes read bodies as 2d25405 did; Linux ones stay strict', async t => {
  // accept, connected and leave never read a body.
  for (const [path, state, check] of [
    ['/api/match/accept', 'found', (f, m) => m.players[0].accepted === true],
    ['/api/match/connected', 'connecting', (f, m) => m.players[0].connected === true],
    ['/api/match/leave', 'found', f => !f.internals().inMatch.has(A)]]) {
    const f = await fixture(t), match = matchFixture(f, state);
    const res = await f.post(A, WINDOWS, path, undefined, {raw:'{'});
    assert.equal(res.status, 200, path);
    assert.equal(res.reads, 0, path);
    assert.ok(check(f, match), path);
    const g = await fixture(t), other = matchFixture(g, state);
    const strict = await g.post(A, LINUX, path, undefined, {raw:'{'});
    assert.equal(strict.status, 409, path);
    assert.equal(strict.body.stale_action, true, path);
    assert.equal(check(g, other), false, path);
  }
  // connecting keeps whatever JSON arrived, and an unreadable body is {}.
  for (const [raw, error] of [['{', 'The host has to be a player in this match.'], ['null', 'Send the map and the host.']]) {
    const f = await fixture(t);
    matchFixture(f, 'ready');
    const res = await f.post(A, WINDOWS, '/api/match/connecting', undefined, {raw});
    assert.deepEqual(res.body, {ok:false, error}, raw);
  }
  // The lobby verbs keep readJsonBody: a non-object is {}, and the verb answers on its own terms.
  for (const path of ['/api/match/coin', '/api/match/choose', '/api/match/side', '/api/match/ban', '/api/match/chat']) {
    const f = await fixture(t);
    matchFixture(f, 'found');
    const res = await f.post(A, WINDOWS, path, undefined, {raw:'[]'});
    assert.equal(res.status, 409, path);
    assert.equal(res.body.stale_action, undefined, path);
    const g = await fixture(t);
    matchFixture(g, 'found');
    assert.equal((await g.post(A, LINUX, path, undefined, {raw:'[]'})).body.stale_action, true, path);
  }
});

// ------------------------------------------------------------------ the network profile

const MOVED = {region:'NA', cross_region:false, location:'2'.repeat(32), transport:'webrtc-relay-v1', age_seconds:0};

test('a profile change tells a Windows hub nothing unless its search really ended', async t => {
  const f = await fixture(t, {dual:true});
  const a = await f.stream(A, WINDOWS), c = await f.stream(C, LINUX);
  f.measure(A); f.measure(C);
  // In a 1v1 match and not searching: 2d25405 sent nothing at all, and nothing is sent.
  f.internals('BB1').inMatch.set(A, MATCH);
  let after = a.since(), scoped = c.since();
  assert.equal((await f.post(A, WINDOWS, '/api/network/profile', MOVED)).status, 200);
  assert.equal((await f.post(A, WINDOWS, '/api/network/profile', {unavailable:true}, {mode:'BB1'})).status, 200);
  assert.deepEqual(after(), []);
  // The scoped stream is still handed the stats its rotated queue_context rides on.
  assert.ok(scoped().some(e => e.type === 'stats'));
  f.internals('BB1').inMatch.delete(A);
  // Searching 1v1: a profile posted through BB5 is BB5's business, as it was in 2d25405, so the
  // 1v1 search goes on and nothing is sent...
  f.measure(A);
  assert.equal((await f.post(A, WINDOWS, '/api/queue/join', {}, {mode:'BB1'})).status, 200);
  after = a.since();
  assert.equal((await f.post(A, WINDOWS, '/api/network/profile', {...MOVED, location:'3'.repeat(32)})).status, 200);
  assert.equal((await f.post(A, WINDOWS, '/api/network/profile', {unavailable:true})).status, 200);
  assert.equal(f.internals('BB1').queueOf.has(A), true);
  assert.deepEqual(after(), []);
  // ...and the same post through BB1 ends it, with one unqueued and the stats.
  f.measure(A, '3'.repeat(32));
  assert.equal((await f.post(A, WINDOWS, '/api/network/profile', {...MOVED, location:'4'.repeat(32)},
    {mode:'BB1'})).status, 200);
  assert.equal(f.internals('BB1').queueOf.has(A), false);
  assert.deepEqual(after().map(e => [e.type, e.mode]), [['unqueued', 'BB1'], ['stats', 'BB1']]);
  assertLegacyWire(a.events);
});

// handleNetwork: everyLadder = scopedClient(req) && competitionGuard.networkChanged. A hub posts
// its profile through the ladder its tab is on (hub/live.py _stamp), and a party member may have
// the 1v1 tab open while the leader searches BB5 (competitive.py ignores the BB5 'queued' there).
test('a party member posting through the 1v1 tab ends the BB5 search only when scoped', async t => {
  const f = await fixture(t, {dual:true});
  const a = await f.stream(A, WINDOWS), b = await f.stream(B, WINDOWS), c = await f.stream(C, LINUX);
  const code = (await f.post(A, WINDOWS, '/api/party/create')).body.code;
  assert.equal((await f.post(B, WINDOWS, '/api/party/join', {code})).status, 200);
  for (const id of [A, B, C]) f.measure(id);
  assert.equal((await f.post(A, WINDOWS, '/api/queue/join')).status, 200);
  const toA = a.since(), toB = b.since();
  assert.equal((await f.post(B, WINDOWS, '/api/network/profile', MOVED, {mode:'BB1'})).status, 200);
  assert.equal((await f.post(B, WINDOWS, '/api/network/profile', {unavailable:true}, {mode:'BB1'})).status, 200);
  assert.equal(f.internals('BB5').queueOf.has(A), true, 'the BB5 search goes on, as in 2d25405');
  assert.deepEqual(toA(), []);
  assert.deepEqual(toB(), []);
  // A Linux member's post does reach the other ladder. The Windows leader is told what a
  // same-ladder change told it in 2d25405: one unqueued and the stats, nothing scoped.
  f.measure(B);
  assert.equal((await f.post(A, WINDOWS, '/api/party/leave')).status, 200);
  const next = (await f.post(B, WINDOWS, '/api/party/create')).body.code;
  const context = c.events.findLast(e => e.type === 'party_update').party_context;
  assert.equal((await f.post(C, LINUX, '/api/party/scoped/join', {code:next, expected_party_context:context})).status, 200);
  f.measure(C);
  assert.equal((await f.post(B, WINDOWS, '/api/queue/join')).status, 200);
  const toLeader = b.since();
  assert.equal((await f.post(C, LINUX, '/api/network/profile', {...MOVED, location:'5'.repeat(32)},
    {mode:'BB1'})).status, 200);
  assert.equal(f.internals('BB5').queueOf.has(B), false);
  assert.deepEqual(toLeader().map(e => [e.type, e.mode]), [['unqueued', 'BB5'], ['stats', 'BB5']]);
  for (const s of [a, b]) assertLegacyWire(s.events);
});

test('a region change through the other ladder is refused mid-match only for a scoped caller', async t => {
  const f = await fixture(t, {dual:true});
  for (const id of [A, C]) { f.measure(id); f.internals('BB1').inMatch.set(id, MATCH); }
  const moved = {...MOVED, region:'EU'};
  const windows = await f.post(A, WINDOWS, '/api/network/profile', moved);
  assert.equal(windows.status, 200, 'what 2d25405 answered');
  assert.equal(windows.body.region, 'EU');
  const linux = await f.post(C, LINUX, '/api/network/profile', moved);
  assert.equal(linux.status, 409);
  assert.equal(linux.body.error, 'Finish your match before changing regions.');
});

test('a Linux leader cancelling an admission in flight tells a Windows member nothing', {timeout:5000}, async t => {
  const f = await fixture(t);
  const a = await f.stream(A, LINUX), b = await f.stream(B, WINDOWS);
  const context = a.events.find(e => e.type === 'party_update').party_context;
  const code = (await f.post(A, LINUX, '/api/party/scoped/create', {expected_party_context:context})).body.code;
  assert.equal((await f.post(B, WINDOWS, '/api/party/join', {code})).status, 200);
  for (const id of [A, B]) f.measure(id);
  const scope = a.events.findLast(e => typeof e.queue_actor === 'string' && e.queue_actor);
  const body = {queue_actor:scope.queue_actor, queue_context:scope.queue_context, queue_attempt:1, queue_unit:''};
  const gate = f.pause(A, 1);                       // inside the admission's credential recheck
  const joining = f.post(A, LINUX, '/api/queue/scoped/join', body);
  await gate.entered;
  const toA = a.since(), toB = b.since();
  try { assert.equal((await f.post(A, LINUX, '/api/queue/scoped/leave', body)).status, 200); }
  finally { gate.release(); }
  assert.equal((await joining).status, 409);
  assert.equal(toA().filter(e => e.type === 'unqueued').length, 1);
  assert.equal(toB().some(e => e.type === 'unqueued' || e.type === 'queued'), false);
  assertLegacyWire(b.events);
});

// ------------------------------------------------------------------ (d) connect diagnostics

function captureLog(t) {
  const lines = [];
  t.mock.method(console, 'log', (...args) => { lines.push(util.format(...args)); });
  return () => lines.filter(line => line.startsWith('[connect]'));
}
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const open = f => f.post(B, WINDOWS, '/api/match/connecting', {map:'Airsoft', host:A});

test('opening a window logs the host and its streams; a host still silent is logged once', {timeout:5000}, async t => {
  const f = await fixture(t);
  await f.stream(A, WINDOWS);
  const logged = captureLog(t);
  matchFixture(f, 'ready');
  assert.equal((await open(f)).status, 200);
  assert.deepEqual(logged(), [`[connect] ${MATCH} opened host=${A} host_streams=1 players=2`]);
  const watch = f.internals().connectWatch.get(MATCH);
  assert.equal(watch.timer.hasRef(), false, 'the watch never holds the process open');
  await sleep(1200);
  assert.deepEqual(logged().slice(1), [`[connect] ${MATCH} host ${A} silent 1s host_streams=1`]);
  assert.equal(f.internals().connectWatch.size, 0);
  assert.equal(f.internals().matches.get(MATCH).state, 'connecting', 'logging only: nothing else moved');
});

test('a host with no open stream is named as such when the window opens', async t => {
  const f = await fixture(t);
  const logged = captureLog(t);
  matchFixture(f, 'ready');
  assert.equal((await open(f)).status, 200);
  assert.deepEqual(logged(), [`[connect] ${MATCH} opened host=${A} host_streams=0 players=2 - the host has no open stream`]);
});

test('any sign of the host launching ends the watch, and nobody else can end it', {timeout:5000}, async t => {
  const signs = {
    'travel permit ask':f => f.service.takeHostPermit(A),
    'host launching':f => f.service.noteHostLaunching(A),
    'cleanup worker completion poll':f => f.get(A, WINDOWS, '/api/match/completion?id=' + MATCH),
    'hub report-in':f => f.post(A, WINDOWS, '/api/match/connected'),
    'game report-in':f => f.service.gameReportedIn(A, 'ch_lobby_read'),
  };
  for (const [name, sign] of Object.entries(signs)) {
    const f = await fixture(t);
    matchFixture(f, 'ready');
    await open(f);
    await f.get(B, WINDOWS, '/api/match/completion?id=' + MATCH);   // not the host
    await f.post(B, WINDOWS, '/api/match/connected');
    // The host's cleanup worker for its PREVIOUS match is still polling: no sign of this launch.
    await f.get(A, WINDOWS, '/api/match/completion?id=0123456789abcdef');
    assert.equal(f.internals().connectWatch.has(MATCH), true, name);
    await sign(f);
    assert.equal(f.internals().connectWatch.has(MATCH), false, name);
  }
  const logged = captureLog(t);
  await sleep(1200);
  assert.deepEqual(logged(), [], 'no host that showed a sign is reported silent');
});

test('every exit from the window clears the watch, and none of it reaches the saved match', {timeout:5000}, async t => {
  const exits = {
    'the host leaves':f => f.post(A, WINDOWS, '/api/match/leave'),
    'the window expires':f => f.internals().expireConnect(MATCH),
    'the match goes live':f => f.internals().goLive(f.internals().matches.get(MATCH)),
    'shutdown':f => f.service.shutdown(),
  };
  for (const [name, exit] of Object.entries(exits)) {
    const f = await fixture(t);
    matchFixture(f, 'ready');
    await open(f);
    assert.equal(f.internals().connectWatch.size, 1, name);
    await exit(f);
    assert.equal(f.internals().connectWatch.size, 0, name);
  }
  // The watch is not on the match, so the saved copy is exactly the match; a restored match has
  // no watch, and rearming it or signalling for it throws nothing.
  const f = await fixture(t);
  const match = matchFixture(f, 'ready');
  await open(f);
  const I = f.internals(), saved = JSON.parse(JSON.stringify(I.serialiseMatch(match)));
  assert.deepEqual(Object.keys(saved).filter(key => /watch/i.test(key)), []);
  clearTimeout(match.timer);
  const restored = I.reviveMatch(saved);
  I.matches.set(MATCH, restored);
  assert.doesNotThrow(() => I.rearm(restored));
  clearTimeout(restored.timer);
  assert.doesNotThrow(() => f.service.takeHostPermit(A));
  assert.equal(I.connectWatch.size, 0, 'the sign still found the watch by match id');
});
