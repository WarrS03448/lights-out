// Run: node --test scripts/test-release-surfaces.cjs (isolated, no external services).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
for (const key of ['UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN', 'STEAM_API_KEY']) delete process.env[key];
const { createServer } = require('../server.cjs');

async function downloadServer(t, hub) {
  const read = fs.readFileSync;
  const cataloguePath = path.resolve(__dirname, '../public/catalogue.json');
  t.mock.method(fs, 'readFileSync', (filename, ...args) =>
    typeof filename === 'string' && path.resolve(filename) === cataloguePath
      ? JSON.stringify({hub}) : read(filename, ...args));
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  return `http://127.0.0.1:${server.address().port}`;
}

test('website installer download stays on the reached hostname despite the catalogue app host', async t => {
  const base = await downloadServer(t, {download_url:'https://play.lightsoutranked.com/hub/Fixture.exe?download=1'});
  for (const method of ['GET', 'HEAD']) {
    const response = await fetch(base+'/hub/download', {method, redirect:'manual', headers:{
      host:'lightsout.up.railway.app', 'x-lightsout-request-host':'lightsoutranked.com',
      'x-forwarded-host':'attacker.example', 'x-forwarded-proto':'http',
    }});
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('location'), '/hub/Fixture.exe?download=1');
    assert.equal(response.headers.get('cache-control'), 'no-store');
    assert.equal((await response.arrayBuffer()).byteLength, 0);
  }
});

test('website still prefers a published zip on the same host', async t => {
  const base = await downloadServer(t, {download_url:'https://play.lightsoutranked.com/hub/Fixture.exe',
    zip_url:'https://lightsout.up.railway.app/hub/Fixture.zip'});
  const response = await fetch(base+'/hub/download', {redirect:'manual'});
  assert.equal(response.status,302);
  assert.equal(response.headers.get('location'),'/hub/Fixture.zip');
});

test('external and private capability download URLs retain their destination', async t => {
  for (const target of ['https://downloads.example.test/hub/Fixture.exe',
    'https://private.example.test/private/capability/hub/Fixture.exe',
    'https://play.lightsoutranked.com/private/capability/hub/Fixture.exe']) {
    await t.test(target, async sub => {
      const base = await downloadServer(sub, {download_url:target});
      const response = await fetch(base+'/hub/download', {redirect:'manual'});
      assert.equal(response.status,302);
      assert.equal(response.headers.get('location'),target);
    });
  }
});

test('retired diagnostics and permit routes are unavailable anonymously', async () => {
  const server = createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    for (const route of ['/probe', '/probe.html', '/api/probe/log', '/api/probe/clear',
      '/api/probe/join', '/api/probe/team', '/api/probe/wait']) {
      for (const method of ['GET', 'POST', 'HEAD']) {
        assert.equal((await fetch(base + route, { method })).status, 404, `${method} ${route}`);
      }
    }
    assert.equal((await fetch(base + '/api/probe', { method: 'POST', body: '{}' })).status, 200);
  } finally { await new Promise(resolve => server.close(resolve)); }
});

test('strict versions are the release default and diagnostics exclude private headers', () => {
  const { GATE_MODE } = require('../live.cjs');
  assert.equal(GATE_MODE, 'strict');
  const { describeHeaders } = require('../server.cjs')._internals;
  assert.deepEqual(describeHeaders({authorization:'Bearer private', cookie:'session=private',
    'x-forwarded-for':'203.0.113.1', 'x-real-ip':'203.0.113.2', 'content-type':'application/json'}),
  {authorization:'[redacted]', 'content-type':'application/json'});
});

test('test identities cannot authenticate in production or an unspecified environment', async () => {
  const { create } = require('../auth.cjs');
  const auth = create({ upstashCmd: async () => null, prefix: 'release-test:' });
  process.env.HUB_TEST_TOKENS = 'fixture=76561198000000001';
  try {
    for (const env of ['production', '']) {
      process.env.NODE_ENV = env;
      assert.equal(await auth.whoami('fixture'), null);
    }
    process.env.NODE_ENV = 'test';
    assert.equal((await auth.whoami('fixture')).steam_id, '76561198000000001');
  } finally { delete process.env.HUB_TEST_TOKENS; delete process.env.NODE_ENV; }
});
