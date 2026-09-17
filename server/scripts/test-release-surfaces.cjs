// Run: node --test scripts/test-release-surfaces.cjs (isolated, no external services).
const test = require('node:test');
const assert = require('node:assert/strict');
for (const key of ['UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN', 'STEAM_API_KEY']) delete process.env[key];
const { createServer } = require('../server.cjs');

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
