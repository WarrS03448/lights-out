'use strict';
// THE WINDOWS WIRE, COMPARED WITH A REFERENCE SERVER. One scripted day for four Windows hubs -
// a party, invites (one re-sent while pending), searches around network profile changes (one
// posted from the 1v1 tab), reconnects, and a 1v1 through
// accept, the lobby and the connect window to the host's arrival - is run against a reference
// server and against this one. Every event on every Windows stream and every answer has to
// match, key order included, once random ids are numbered by first appearance.
//
// Not part of npm test: it needs a reference checkout, which has to be what production runs NOW
// (the reference moves with main; it was 2d25405b until main reached d80ac059, hub 3.0.6).
// Before redeploying the action scopes:
//   git worktree add --detach ../../ref d80ac059
//   node scripts/wire-parity.cjs ../../ref/server             # malformed bodies as well
//   node scripts/wire-parity.cjs ../../ref/server --hub-exact # only what a 3.0.x hub sends
//   node scripts/wire-parity.cjs ../../ref/server --linux     # a Linux stream alongside
// Exit code 1, with the difference, when they disagree.
process.env.NODE_ENV = 'test';
process.env.COMP_NETWORK_TEST_BYPASS = '1';
process.env.COMP_MATCH_SIZE = '10';
const assert = require('node:assert/strict');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const [refArg, newArg] = process.argv.slice(2).filter(a => !a.startsWith('--'));
if (!refArg) { console.error('usage: node scripts/wire-parity.cjs <reference server dir> [server dir] [--hub-exact] [--linux]'); process.exit(2); }
const refDir = path.resolve(refArg), newDir = path.resolve(newArg || path.join(__dirname, '..'));
const withLinux = process.argv.includes('--linux');
// --hub-exact: only bodies a 3.0.x hub sends (hub/live.py _post: no body, or a JSON object).
const hubExact = process.argv.includes('--hub-exact');

const [A, B, C, D, E] = ['76561198000000001', '76561198000000002', '76561198000000003',
  '76561198000000004', '76561198000000005'];
const T = 1790000000000;
const WIN = {'x-hub-version':'3.0.6', 'x-bb5-version':'1.0.31', 'x-bb1-version':'1.0.5'};
const LINUX = {...WIN, 'x-hub-platform':'linux'};

function normaliser() {
  const map = new Map();
  const counters = {};
  const tag = (kind, value) => {
    const key = kind + ':' + value;
    if (!map.has(key)) { counters[kind] = (counters[kind] || 0) + 1; map.set(key, `<${kind}${counters[kind]}>`); }
    return map.get(key);
  };
  const walk = v => {
    if (typeof v === 'string') {
      if (/^[0-9a-f]{16}$/.test(v)) return tag('id16', v);
      if (/^chm-[0-9a-f]{16}$/.test(v)) return tag('session', v);
      if (/^[0-9a-f]{24,128}$/.test(v)) return tag('hex', v);
      if (/^[A-Z2-9]{4}-[A-Z2-9]{2}$/.test(v)) return tag('code', v);
      if (/^\d{4}-\d\d-\d\dT/.test(v)) return '<iso>';
      return v;
    }
    if (Array.isArray(v)) return v.map(walk);
    if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x)]));
    return v;
  };
  return walk;
}

async function run(dir) {
  const realNow = Date.now;
  Date.now = () => T;
  const log = console.log;
  console.log = () => {};
  try {
    const options = {
      prefix:'parity:', upstashCmd:null,
      whoami:async token => [A, B, C, D, E].includes(token) ? {steam_id:token, auth_method:'steam'} : null,
      bearer:req => req.token,
      sendJson:(res, status, body) => Object.assign(res, {status, body: JSON.parse(JSON.stringify(body))}),
      badRequest() {},
      readBody:async req => Buffer.from(req.raw ?? JSON.stringify(req.body ?? {})),
    };
    const service = require(dir + '/ranked-service.cjs').create(options);
    await service._internals.ready;
    const I = mode => service.forMode(mode)._internals;
    const streams = {}, answers = [];
    const tick = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };
    async function stream(token, headers) {
      const req = Object.assign(new EventEmitter(), {token, headers:{...headers}, socket:{setTimeout() {}}});
      const events = streams[token] ||= [];
      const res = {headersSent:false, writableEnded:false, destroyed:false,
        setTimeout() {}, setHeader() {}, writeHead() { this.headersSent = true; },
        write(chunk) { if (typeof chunk === 'string' && chunk.startsWith('data: ')) events.push(JSON.parse(chunk.slice(6))); return true; },
        end() { if (!this.writableEnded) { this.writableEnded = true; req.emit('close'); } }};
      await service.route(req, res, 'GET', '/api/live', new URL('http://fixture/api/live'));
      await tick();
      return () => res.end();
    }
    async function call(label, token, method, path, body, mode, raw) {
      if (hubExact && raw !== undefined && raw !== '') { raw = ''; if (path.endsWith('/join')) body = {}; }
      if (hubExact && typeof body === 'string') { body = undefined; raw = ''; }
      const headers = {...(token === C && withLinux ? LINUX : WIN), ...(mode ? {'x-ranked-mode':mode} : {})};
      const res = {setHeader() {}};
      await service.route({token, body, raw, headers}, res, method, path.split('?')[0], new URL('http://fixture' + path));
      await tick();
      answers.push([label, res.status, res.body]);
      return res;
    }
    const profile = (id, location, mode='BB5') => call('profile ' + id, id, 'POST', '/api/network/profile',
      {region:'NA', cross_region:false, location, transport:'webrtc-relay-v1', age_seconds:0}, mode);

    const close = {};
    for (const id of [A, B, D, E]) close[id] = await stream(id, WIN);
    if (withLinux) close[C] = await stream(C, LINUX);

    // ---- BB5 party life
    const created = await call('create', D, 'POST', '/api/party/create', undefined, 'BB5', '{');
    const code = created.body.code;
    await call('join bad', E, 'POST', '/api/party/join', undefined, 'BB5', '[');
    await call('join', E, 'POST', '/api/party/join', {code}, 'BB5');
    await call('refresh', D, 'POST', '/api/party/refresh-code', {}, 'BB5');
    I('BB5').friends.set(D, new Set([A]));
    await call('invite', D, 'POST', '/api/party/invite', {target:A}, 'BB5');
    await call('decline', A, 'POST', '/api/party/invite/decline', {from:D}, 'BB5');
    await call('invite again', D, 'POST', '/api/party/invite', {target:A}, 'BB5');
    // Re-sent while pending: the pre-scopes server (2d25405, d80ac059) empties the sole invite's
    // inbox and the accept expires.
    await call('invite resent', D, 'POST', '/api/party/invite', {target:A}, 'BB5');
    await call('accept resent', A, 'POST', '/api/party/invite/accept', {from:D}, 'BB5');
    if (I('BB5').partyOf.has(A)) await call('leave (a server that kept it)', A, 'POST', '/api/party/leave', {}, 'BB5');
    await call('leave', E, 'POST', '/api/party/leave', 'null', 'BB5');
    const code2 = I('BB5').partyOf.get(D);
    await call('rejoin', E, 'POST', '/api/party/join', {code:code2}, 'BB5');
    close[E](); close[E] = await stream(E, WIN);                // reconnect inside grace: roster replay

    // ---- BB5 search, and the network profile around it
    await profile(D, '1'.repeat(32)); await profile(E, '1'.repeat(32));
    await call('queue', D, 'POST', '/api/queue/join', {}, 'BB5');
    await profile(E, '2'.repeat(32));                           // revision change: party unqueued
    await call('queue again', D, 'POST', '/api/queue/join', {}, 'BB5');
    await call('unavailable', E, 'POST', '/api/network/profile', {unavailable:true}, 'BB5');
    await profile(E, '3'.repeat(32));
    await call('queue third', D, 'POST', '/api/queue/join', {}, 'BB5');
    close[D](); close[D] = await stream(D, WIN);                // queued replay
    await call('queue leave', D, 'POST', '/api/queue/leave', {}, 'BB5');
    await profile(E, '4'.repeat(32));                           // not queued: nothing to say
    // A member with the 1v1 tab open posts through BB1 while the leader searches BB5.
    await call('queue fourth', D, 'POST', '/api/queue/join', {}, 'BB5');
    await profile(E, '5'.repeat(32), 'BB1');
    await call('unavailable from the 1v1 tab', E, 'POST', '/api/network/profile', {unavailable:true}, 'BB1');
    await profile(E, '6'.repeat(32), 'BB1');
    await call('queue leave again', D, 'POST', '/api/queue/leave', {}, 'BB5');

    // ---- the 1v1: queue, accept, lobby, connect window, arrival
    await profile(A, '1'.repeat(32), 'BB1'); await profile(B, '1'.repeat(32), 'BB1');
    await call('1v1 A', A, 'POST', '/api/queue/join', {}, 'BB1');
    await call('1v1 B', B, 'POST', '/api/queue/join', {}, 'BB1');
    const match = [...I('BB1').matches.values()][0];
    assert.ok(match, 'the 1v1 formed');
    await call('accept A', A, 'POST', '/api/match/accept', undefined, 'BB1', '{');
    await call('accept B', B, 'POST', '/api/match/accept', undefined, 'BB1', '');   // hub: no body
    if (match.state !== 'ready') {                               // only the control gets here
      await call('accept A again', A, 'POST', '/api/match/accept', undefined, 'BB1', '');
      answers.push(['state after accept', match.state]);
    }
    I('BB1').clearStageTurn(match);
    Object.assign(match.lobby, {stage:'ready', map:match.lobby.pool[0], sides:{1:'attack', 2:'defend'}, bans:[]});
    await call('chat', B, 'POST', '/api/match/chat', {text:'gl', channel:'all'}, 'BB1');
    await profile(A, '2'.repeat(32), 'BB5');                     // a revision change mid-match
    const host = match.lobby.teams[1][0], joiner = host === A ? B : A;
    await call('connecting', joiner, 'POST', '/api/match/connecting', {map:match.lobby.map, host}, 'BB1');
    await call('connecting again', host, 'POST', '/api/match/connecting', undefined, 'BB1', '{');
    await call('completion', host, 'GET', '/api/match/completion?id=' + match.id);
    answers.push(['permit', service.takeHostPermit(host)]);
    answers.push(['launching', service.noteHostLaunching(host)]);
    close[host](); close[host] = await stream(host, WIN);       // connecting replay
    await call('connected host', host, 'POST', '/api/match/connected', undefined, 'BB1', '');
    answers.push(['report-in', service.gameReportedIn(host, 'ch_lobby_read')]);
    await call('unavailable mid-match', host, 'POST', '/api/network/profile', {unavailable:true}, 'BB1');
    await call('connected joiner', joiner, 'POST', '/api/match/connected', 'null', 'BB1');
    await tick();
    for (const f of Object.values(close)) f();
    await service.shutdown();
    const walk = normaliser();
    const windows = Object.fromEntries([A, B, D, E].map(id => [id, streams[id]]));
    return walk({windows, answers});
  } finally {
    Date.now = realNow;
    console.log = log;
  }
}

(async () => {
  const ref = await run(refDir), next = await run(newDir);
  const counts = Object.fromEntries(Object.entries(ref.windows).map(([id, events]) => [id.slice(-1), events.length]));
  try {
    assert.deepEqual(next, ref);
    assert.equal(JSON.stringify(next), JSON.stringify(ref), 'same values, different key order');
    console.log('IDENTICAL: %d answers, events per Windows stream %j%s', ref.answers.length, counts,
      withLinux ? ' (with a Linux stream alongside)' : '');
  } catch (err) {
    console.log('DIFFERENT');
    console.log(err.message.slice(0, 6000));
    process.exitCode = 1;
  }
})();
