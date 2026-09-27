'use strict';
// node --test scripts/test-platform-gate.cjs; synthetic streams, no external services.
//
// The per-platform hub gate. A native Linux build names itself with `x-hub-platform: linux` and
// is held to requiredVersions().linux, which server.cjs reads from the catalogue's top-level
// `linux` entry; every other build, and a Linux build while that entry is absent, is held to
// requiredVersions().hub exactly as before. The gamemode (pak) requirement is shared by both.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {EventEmitter} = require('node:events');
delete process.env.COMP_NETWORK_TEST_BYPASS;
delete process.env.COMP_VERSION_GATE;
// The last test runs the real server.cjs in process: never against real storage or Steam.
for (const key of ['UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN', 'STEAM_API_KEY', 'COMP_GAME_RULES_OVERRIDE'])
  delete process.env[key];
process.env.COMP_MATCH_SIZE = '10';
const live = require('../live.cjs');

const A = '76561198000000001';
const B = '76561198000000002';
const MODE = '1.0.27';

async function fixture(t, required) {
  const credentials = new Map([[A, A], [B, B]]);
  const need = {current:required};
  const streams = [];
  const service = live.create({
    prefix:'platform-gate-test:',
    whoami:async token => credentials.has(token) ? {steam_id:credentials.get(token), auth_method:'steam'} : null,
    bearer:req => req.token,
    sendJson:(res, status, body) => Object.assign(res, {status, body}),
    badRequest() {}, readBody:async req => Buffer.from(JSON.stringify(req.body ?? {})),
    upstashCmd:null, requiredVersions:() => need.current,
  });
  t.after(async () => { for (const s of streams) s.close(); await service.shutdown(); });
  await service._internals.ready;
  const I = service._internals;
  // `client` is what a hub stamps on every request: {hub, mode, platform}. platform undefined
  // means the header is absent, which is what every Windows hub sends today.
  const headers = client => {
    const h = {'x-ranked-mode':'BB5', 'x-hub-version':client.hub, 'x-mode-version':client.mode ?? MODE};
    if (client.platform !== undefined) h['x-hub-platform'] = client.platform;
    return h;
  };
  function measured(id) {
    I.networkRegistry.profile(id, {region:'NA', cross_region:false, location:'1'.repeat(32),
      transport:'webrtc-relay-v1', age_seconds:0}, Date.now());
  }
  async function post(token, client, path, body = {}) {
    const res = {setHeader() {}};
    await service.route({token, body, headers:headers(client)}, res, 'POST', path, new URL('http://fixture' + path));
    return res;
  }
  async function stream(token, client) {
    const req = Object.assign(new EventEmitter(), {token, headers:headers(client), socket:{setTimeout() {}}});
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
  async function join(token, client) {
    for (const id of [A, B]) measured(id);
    return post(token, client, '/api/queue/join');
  }
  async function party(leader, member) {
    await stream(A, leader);
    await stream(B, member);
    const created = await post(A, leader, '/api/party/create');
    assert.equal(created.status, 200);
    const joined = await post(B, member, '/api/party/join', {code:created.body.code});
    assert.equal(joined.status, 200);
    assert.deepEqual(I.parties.get(created.body.code).members, [A, B]);
  }
  return {I, service, need, post, stream, join, party};
}

const WIN = (hub, mode = MODE) => ({hub, mode});
const LINUX = (hub, mode = MODE) => ({hub, mode, platform:'linux'});

test('hello announces the platform gate beside the three action scopes', async t => {
  const f = await fixture(t, {hub:'3.0.3', mode:MODE});
  const s = await f.stream(A, LINUX('3.0.3'));
  const hello = s.events.find(e => e.type === 'hello');
  assert.deepEqual(hello.capabilities, {match_action_scopes_v1:true, party_action_scopes_v1:true,
    queue_action_scopes_v1:true, hub_platform_gate_v1:true});
  // ...to the Linux build only: a Windows hub's hello is 2d25405's, with no capabilities at all.
  const windows = await f.stream(B, WIN('3.0.3'));
  assert.equal('capabilities' in windows.events.find(e => e.type === 'hello'), false);
});

test('Windows is unchanged whatever the Linux entry says', async t => {
  for (const linux of [undefined, '', '3.0.1', '3.0.3', '3.0.9', '3.0.3.1']) {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, ...(linux === undefined ? {} : {linux})});
    const behind = await f.join(A, WIN('3.0.2'));
    assert.equal(behind.status, 426, `Windows 3.0.2 against linux=${linux}`);
    assert.equal(behind.body.what, 'hub');
    assert.equal(behind.body.need_hub, '3.0.3');
    assert.equal(behind.body.have_hub, '3.0.2');
    assert.equal((await f.join(A, WIN('3.0.3'))).status, 200, `Windows 3.0.3 against linux=${linux}`);
  }
});

test('Linux behind the Linux entry gets 426 naming the Linux version', async t => {
  const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.3.1'});
  const res = await f.join(A, LINUX('3.0.3'));
  assert.equal(res.status, 426);
  assert.equal(res.body.outdated, true);
  assert.equal(res.body.what, 'hub');
  assert.equal(res.body.need_hub, '3.0.3.1');
  assert.equal(res.body.have_hub, '3.0.3');
  assert.equal(res.body.error, 'Update before you queue.');
  assert.equal(f.I.queueOf.has(A), false);
});

test('Linux at the Linux entry queues even while behind the Windows hub version', async t => {
  const f = await fixture(t, {hub:'3.0.4', mode:MODE, linux:'3.0.3'});
  const res = await f.join(A, LINUX('3.0.3'));
  assert.equal(res.status, 200);
  assert.equal(f.I.queueOf.has(A), true);
  assert.equal(f.I.versionProblem(A), null);
});

test('with no Linux entry the hub version applies to Linux, which is lockstep', async t => {
  for (const linux of [undefined, '', null, 3, {version:'3.0.1'}]) {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, ...(linux === undefined ? {} : {linux})});
    const behind = await f.join(A, LINUX('3.0.2'));
    assert.equal(behind.status, 426, `Linux 3.0.2 with linux=${JSON.stringify(linux)}`);
    assert.equal(behind.body.need_hub, '3.0.3');
    assert.equal((await f.join(A, LINUX('3.0.3'))).status, 200);
  }
});

test('any other platform value, or none, gets the Windows rule', async t => {
  for (const platform of [undefined, '', 'Linux', 'LINUX', ' linux', 'linux ', 'linux, linux', 'linux-beta', 'windows', 'win32']) {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
    const client = {hub:'3.0.2', mode:MODE, platform};
    const res = await f.join(A, client);
    assert.equal(res.status, 426, `platform ${JSON.stringify(platform)}`);
    assert.equal(res.body.need_hub, '3.0.3');
  }
});

test('the gamemode requirement stays shared by both platforms', async t => {
  const f = await fixture(t, {hub:'3.0.3', mode:'1.0.28', linux:'3.0.2'});
  const linux = await f.join(A, LINUX('3.0.2', '1.0.27'));
  assert.equal(linux.status, 426);
  assert.equal(linux.body.what, 'mode');
  assert.equal(linux.body.need_mode, '1.0.28');
  const missing = await f.join(A, {hub:'3.0.2', mode:'', platform:'linux'});
  assert.equal(missing.status, 426);
  assert.equal(missing.body.missing_mode, true);
  assert.equal((await f.join(A, LINUX('3.0.2', '1.0.28'))).status, 200);
});

test('a mixed party judges each member by their own platform', async t => {
  await t.test('Windows leader, Linux member at the Linux entry: queues', async t => {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
    await f.party(WIN('3.0.3'), LINUX('3.0.2'));
    const res = await f.join(A, WIN('3.0.3'));
    assert.equal(res.status, 200);
    assert.equal(res.body.party, 2);
  });
  await t.test('Windows leader, Linux member behind the Linux entry: refused, naming the member', async t => {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
    await f.party(WIN('3.0.3'), LINUX('3.0.1'));
    const res = await f.join(A, WIN('3.0.3'));
    assert.equal(res.status, 426);
    assert.equal(res.body.who, B);
    assert.equal(res.body.need_hub, '3.0.2');
    assert.equal(res.body.error, 'Somebody in your party is not on the current version.');
  });
  await t.test('Linux leader at the Linux entry, Windows member at the same number: the Windows member is behind', async t => {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
    await f.party(LINUX('3.0.2'), WIN('3.0.2'));
    const res = await f.join(A, LINUX('3.0.2'));
    assert.equal(res.status, 426);
    assert.equal(res.body.who, B);
    assert.equal(res.body.need_hub, '3.0.3');
  });
  await t.test('Linux leader behind the Linux entry: the leader is told first', async t => {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
    await f.party(LINUX('3.0.1'), WIN('3.0.3'));
    const res = await f.join(A, LINUX('3.0.1'));
    assert.equal(res.status, 426);
    assert.equal(res.body.who, undefined);
    assert.equal(res.body.need_hub, '3.0.2');
    assert.equal(res.body.error, 'Update before you queue.');
  });
  await t.test('both at their own current version: queues', async t => {
    const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
    await f.party(LINUX('3.0.2'), WIN('3.0.3'));
    assert.equal((await f.join(A, LINUX('3.0.2'))).status, 200);
  });
});

test('a later request re-stamps the platform with its version', async t => {
  const f = await fixture(t, {hub:'3.0.3', mode:MODE, linux:'3.0.2'});
  await f.stream(A, LINUX('3.0.2'));
  assert.equal(f.I.versions.get(A).platform, 'linux');
  assert.equal(f.I.versionProblem(A), null);
  // A request with no version headers says nothing and leaves the record alone.
  f.I.noteVersions({headers:{'x-hub-platform':'windows'}}, A);
  assert.equal(f.I.versions.get(A).platform, 'linux');
  f.I.noteVersions({headers:{'x-hub-version':'3.0.2', 'x-mode-version':MODE}}, A);
  assert.equal(f.I.versions.get(A).platform, 'windows');
  assert.equal(f.I.versionProblem(A).need_hub, '3.0.3');
});

test('a bug report records the platform beside the hub version, and the console shows it', async t => {
  const f = await fixture(t, {hub:'3.0.3', mode:MODE});
  assert.equal((await f.post(A, LINUX('3.0.3'), '/api/bug', {text:'from linux'})).status, 200);
  assert.equal((await f.post(B, WIN('3.0.3'), '/api/bug', {text:'from windows'})).status, 200);
  const rows = await f.I.bugReports(10);
  assert.deepEqual(rows.map(r => [r.text, r.hub, r.platform]),
    [['from windows', '3.0.3', 'windows'], ['from linux', '3.0.3', 'linux']]);
  // The admin console tags each report with it, beside the hub version.
  f.I.ADMIN_IDS.add(A);
  t.after(() => f.I.ADMIN_IDS.delete(A));
  const admin = require('../admin.cjs').create({upstashCmd:async () => ({steam_id:A}), live:() => f.service});
  const res = {headers:{}, body:'', setHeader(k, v) { this.headers[k] = v; },
    writeHead(status, headers) { this.status = status; Object.assign(this.headers, headers || {}); },
    end(body) { this.body = body ? body.toString('utf8') : ''; }};
  await admin.route({method:'GET', url:'/admin', headers:{cookie:'hubadmin=fixture', host:'fixture'}},
    res, 'GET', '/admin', new URL('http://fixture/admin'));
  assert.equal(res.status, 200);
  const shown = res.body.split('<div class="bug">').slice(1).map(row =>
    [...row.matchAll(/<span class="tag">([^<]*)<\/span>/g)].map(m => m[1]).filter(tag => !/^\d{4}-/.test(tag)));
  assert.deepEqual(shown, [['hub 3.0.3', 'windows', 'mode ' + MODE], ['hub 3.0.3', 'linux', 'mode ' + MODE]]);
});

test('the ranked router carries the platform to both ladders', async t => {
  const service = require('../ranked-service.cjs').create({
    prefix:'platform-gate-dual-test:',
    whoami:async token => token === A ? {steam_id:A, auth_method:'steam'} : null,
    bearer:req => req.token,
    sendJson:(res, status, body) => Object.assign(res, {status, body}),
    badRequest() {}, readBody:async req => Buffer.from(JSON.stringify(req.body ?? {})),
    upstashCmd:null, requiredVersions:() => ({hub:'3.0.3', mode:MODE, linux:'3.0.2'}),
  });
  t.after(() => service.shutdown());
  await service._internals.ready;
  async function call(path, ladder, platform) {
    const headers = {'x-ranked-mode':ladder, 'x-hub-version':'3.0.2', 'x-bb5-version':MODE, 'x-bb1-version':MODE};
    if (platform !== undefined) headers['x-hub-platform'] = platform;
    const res = {setHeader() {}};
    await service.route({token:A, body:{}, headers}, res, 'POST', path, new URL('http://fixture' + path));
    return res;
  }
  for (const ladder of ['BB5', 'BB1']) {
    const I = service.forMode(ladder)._internals;
    I.networkRegistry.profile(A, {region:'NA', cross_region:false, location:'1'.repeat(32),
      transport:'webrtc-relay-v1', age_seconds:0}, Date.now());
    const windows = await call('/api/queue/join', ladder);
    assert.equal(windows.status, 426, ladder);
    assert.equal(windows.body.need_hub, '3.0.3');
    const linux = await call('/api/queue/join', ladder, 'linux');
    assert.equal(linux.status, 200, ladder);
    assert.equal(I.versions.get(A).platform, 'linux');
    assert.equal((await call('/api/queue/leave', ladder, 'linux')).status, 200);
  }
});

// hello announces hub_platform_gate_v1, and the Linux client then gates Find match on the
// catalogue's `linux` entry. That is only true if the real server.cjs hands live.cjs that entry,
// so this drives the real server over HTTP with the catalogue swapped in memory.
test('the served catalogue holds a Linux hub to its linux entry, and to hub.version without one', async t => {
  const saved = {NODE_ENV:process.env.NODE_ENV, HUB_TEST_TOKENS:process.env.HUB_TEST_TOKENS};
  process.env.NODE_ENV = 'test';
  process.env.HUB_TEST_TOKENS = 'tok-a=' + A;
  t.after(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  const cataloguePath = path.resolve(__dirname, '../public/catalogue.json');
  const read = fs.readFileSync;
  let catalogue = null;
  t.mock.method(fs, 'readFileSync', (file, ...args) =>
    typeof file === 'string' && path.resolve(file) === cataloguePath ? JSON.stringify(catalogue) : read(file, ...args));
  const {createServer} = require('../server.cjs');
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  // The headers hub/live.py _stamp sends, for one ladder and one build.
  async function join(ladder, hub, pack, platform) {
    const headers = {authorization:'Bearer tok-a', 'content-type':'application/json', 'x-ranked-mode':ladder,
      'x-hub-version':hub, 'x-mode-version':pack, ['x-' + ladder.toLowerCase() + '-version']:pack};
    if (platform) headers['x-hub-platform'] = platform;
    const res = await fetch(base + '/api/queue/join', {method:'POST', headers, body:'{}'});
    return {status:res.status, body:await res.json()};
  }
  // requiredVersions caches each ladder's answer for ten seconds, so each catalogue is read
  // through its own ladder: BB5 while it has a Linux entry, BB1 after the entry is gone.
  catalogue = {hub:{version:'3.0.4'}, linux:{version:'3.0.3.1'},
               gamemodes:[{id:'BB5', version:'1.0.31'}, {id:'BB1', version:'1.0.5'}]};
  const behind = await join('BB5', '3.0.3', '1.0.31', 'linux');
  assert.equal(behind.status, 426);
  assert.equal(behind.body.what, 'hub');
  assert.equal(behind.body.need_hub, '3.0.3.1');
  // Past the version gate, and stopped at the next check: this player has no relay measurements.
  const current = await join('BB5', '3.0.3.1', '1.0.31', 'linux');
  assert.equal(current.status, 409);
  assert.equal(current.body.network_unready, true);
  const windows = await join('BB5', '3.0.3.1', '1.0.31');
  assert.equal(windows.status, 426);
  assert.equal(windows.body.need_hub, '3.0.4');

  catalogue = {hub:{version:'3.0.4'}, gamemodes:[{id:'BB5', version:'1.0.31'}, {id:'BB1', version:'1.0.6'}]};
  const lockstep = await join('BB1', '3.0.3.1', '1.0.6', 'linux');
  assert.equal(lockstep.status, 426);
  assert.equal(lockstep.body.what, 'hub');
  assert.equal(lockstep.body.need_hub, '3.0.4');
  assert.equal(lockstep.body.need_mode, '1.0.6', 'the catalogue without a Linux entry was the one read');
});
