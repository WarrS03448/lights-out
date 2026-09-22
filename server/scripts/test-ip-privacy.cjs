'use strict';
// Run: node --test server/scripts/test-ip-privacy.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
process.env.PROBE_SLOW_SECONDS = '1';
for (const key of ['UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN', 'STEAM_API_KEY']) delete process.env[key];
const { createServer } = require('../server.cjs');
const host = '76561198000000001', token = 'a'.repeat(64);

test('diagnostic storage excludes network identifiers and unstructured request bodies', async t => {
  const writes = [];
  const storage = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const command = JSON.parse(Buffer.concat(chunks));
    if (command[0] === 'LPUSH') writes.push(JSON.parse(command[2]));
    res.end(JSON.stringify({result: null}));
  });
  await new Promise(r => storage.listen(0, '127.0.0.1', r));
  process.env.UPSTASH_REDIS_REST_URL = `http://127.0.0.1:${storage.address().port}`;
  process.env.UPSTASH_REDIS_REST_TOKEN = 'local-test-only';
  const matchId = '0123456789abcdef';
  const service = {_internals:{ready:Promise.resolve(), ensureRecovery:async()=>{}},
    authoriseReport: value => value === token ? {steamId:host, matchId} : null,
    gameReportedStats: () => ({ok:true}), teamRuling: () => ({ok:true,yes:true,team:1})};
  const server = createServer({liveService:service, analyticsService: {emit() {}}});
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(async () => {
    delete process.env.UPSTASH_REDIS_REST_URL; delete process.env.UPSTASH_REDIS_REST_TOKEN;
    server.closeAllConnections(); storage.closeAllConnections();
    await Promise.all([new Promise(r => server.close(r)), new Promise(r => storage.close(r))]);
  });
  const base = `http://127.0.0.1:${server.address().port}`;
  const headers = {'content-type':'application/json', 'x-forwarded-for':'203.0.113.10',
    'x-real-ip':'2001:db8::1', forwarded:'for=203.0.113.10',
    'cf-connecting-ip':'203.0.113.10', 'cdn-real-ip':'203.0.113.10', 'x-client-ip':'2001:db8::1', 'user-agent':'client at 203.0.113.10'};
  const response = await fetch(base + '/api/probe?ip=203.0.113.10', {method:'POST', headers,
    body:JSON.stringify({event_name:'ch_lobby_alive', user_id:host, ip:'203.0.113.10',
      client_ip:'2001:db8::1', extra:{remoteAddress:'203.0.113.10'}})});
  assert.equal(response.status, 200);
  const saved = writes.at(-1);
  assert.doesNotMatch(JSON.stringify(saved), /203\.0\.113\.10|2001:db8::1/);
  assert.equal(JSON.parse(saved.body).event_name, 'ch_lobby_alive');
  assert.equal(JSON.parse(saved.body).user_id, host);
  await fetch(base + '/api/probe', {method:'POST', body:'client ip 203.0.113.10'});
  assert.doesNotMatch(JSON.stringify(writes.at(-1)), /203\.0\.113\.10/);
  for (const route of ['/api/match-report', '/api/match-report/team']) {
    const before = writes.length;
    const result = await fetch(base + route + '?ip=203.0.113.10', {method:'POST',
      headers:{...headers, authorization:'Bearer '+token},
      body:JSON.stringify({event_name:'ch_bb5_stats', storefront:matchId, platform:'row',
        ip:'203.0.113.10', client_ip:'2001:db8::1'})});
    assert.equal(result.status,200,route);
    assert.equal(writes.length,before+1,route+' must exercise stored diagnostics');
    const entry = writes.at(-1);
    assert.equal(entry.url,route);
    assert.equal(entry.headers.authorization,'[redacted]');
    assert.doesNotMatch(JSON.stringify(entry), /203\.0\.113\.10|2001:db8::1/);
    assert.ok(!JSON.stringify(entry).includes(token));
  }
});

test('host travel uses its match credential without reading any network address', async t => {
  const launched = [];
  let permit = true;
  const service = {_internals:{ready:Promise.resolve(), ensureRecovery:async()=>{}},
    authoriseReport: value => value === token ? {steamId:host, matchId:'0123456789abcdef'} : null,
    takeHostPermit: id => permit && id === host, noteHostLaunching: id => launched.push(id)};
  const server = createServer({liveService:service, analyticsService:{emit(){}}});
  server.prependListener('request', req => {
    Object.defineProperty(req.socket, 'remoteAddress', {get(){throw Error('IP address was read');}, configurable:true});
    for (const name of ['x-forwarded-for','x-real-ip','cf-connecting-ip','forwarded','cdn-real-ip','x-client-ip'])
      Object.defineProperty(req.headers, name, {get(){throw Error('IP header was read');}, configurable:true});
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  t.after(async()=>{server.closeAllConnections(); await new Promise(r=>server.close(r));});
  const base = `http://127.0.0.1:${server.address().port}`;
  const post = credential => fetch(base+'/api/probe/slow', {method:'POST',
    headers:{authorization:'Bearer '+credential}, body:JSON.stringify({event_name:'ch_host_wait'}),
    signal:AbortSignal.timeout(1800)});
  const response = await post(token);
  assert.equal(response.status,200);
  assert.deepEqual(launched,[host]);
  for (const credential of ['chlobby', 'b'.repeat(64), token]) {
    if (credential === token) permit = false;
    await assert.rejects(post(credential), /timeout/i, 'denial must never fire the legacy travel delegate');
  }
  assert.deepEqual(launched,[host]);
});
