// UTF-8. node --test server/scripts/test-rating-load.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
// These rank-storage fixtures have no Steam probe; dedicated network suites enforce it.
process.env.NODE_ENV = 'test';
process.env.COMP_NETWORK_TEST_BYPASS = '1';
const live = require('../live.cjs');
const ID = '76561198000999000';
const saved = { matches: 8, wins: 5, losses: 3, progress: 950, updated: 10 };
function service(read) {
  return live.create({ whoami: async () => ({steam_id: ID}), bearer: () => 'token',
    sendJson(res, status, body) { Object.assign(res, {status, body}); },
    badRequest() {}, readBody: async () => Buffer.alloc(0), prefix: 'test:',
    upstashCmd: async ([op, key], options) => op === 'GET' && key === 'test:rating:' + ID
      ? read(options) : op === 'SMEMBERS' ? [] : null });
}

test('concurrent rating readers wait for the saved rank instead of returning a new player', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const svc = service(() => gate);
  try {
    const first = svc._internals.loadRating(ID);
    let early = false;
    const second = svc._internals.loadRating(ID).then(row => { early = true; return row; });
    await new Promise(resolve => setImmediate(resolve));
    const resolvedEarly = early;
    release(JSON.stringify(saved));
    const rows = await Promise.all([first, second]);
    assert.equal(resolvedEarly, false);
    assert.deepEqual(rows.map(row => row.matches), [8, 8]);
  } finally { svc.shutdown(); }
});

test('a failed rating read is rejected and retried, never cached as unranked', async () => {
  let broken = true;
  const svc = service(() => { if (broken) throw new Error('offline'); return JSON.stringify(saved); });
  try {
    await assert.rejects(svc._internals.loadRating(ID), /offline/);
    broken = false;
    assert.equal((await svc._internals.loadRating(ID)).matches, 8);
  } finally { svc.shutdown(); }
});

test('rating reads require storage errors to remain distinct from missing keys', async () => {
  const svc = service(options => {
    if (options && options.strict) throw new Error('storage unavailable');
    return null; // legacy best-effort adapter swallows this failure
  });
  try { await assert.rejects(svc._internals.loadRating(ID), /storage unavailable/); }
  finally { svc.shutdown(); }
});

test('a confirmed missing rating is a new account', async () => {
  const svc = service(() => null);
  try { assert.equal((await svc._internals.loadRating(ID)).matches, 0); }
  finally { svc.shutdown(); }
});

test('queueing is refused while rank storage is unavailable and works after recovery', async () => {
  let broken = true;
  const svc = service(() => { if (broken) throw new Error('offline'); return JSON.stringify(saved); });
  try {
    const req = {headers: {}, url: '/api/queue/join'};
    const events = [];
    svc._internals.clients.set('rank-test', {steamId: ID,
      res: {write: frame => events.push(frame), end() {}}});
    svc._internals.bySteam.set(ID, new Set(['rank-test']));
    const failed = {};
    await svc.route(req, failed, 'POST', '/api/queue/join');
    assert.equal(failed.status, 503);
    assert.equal(svc._internals.queue.length, 0);
    broken = false;
    const recovered = {};
    await svc.route(req, recovered, 'POST', '/api/queue/join');
    assert.equal(recovered.status, 200);
    assert.equal(svc._internals.ratingOf(ID).matches, 8);
    assert.equal(svc._internals.queue.length, 1);
    assert.ok(events.some(frame => frame.includes('"type":"rating"') && frame.includes('"matches":8')),
      'recovered rank must reach the already-open stream');
  } finally { svc.shutdown(); }
});

test('a malformed saved rating is retryable instead of silently resetting placements', async () => {
  let raw = '{broken';
  const svc = service(() => raw);
  try {
    await assert.rejects(svc._internals.loadRating(ID));
    raw = JSON.stringify(saved);
    assert.equal((await svc._internals.loadRating(ID)).matches, 8);
  } finally { svc.shutdown(); }
});

test('the leaderboard reports unavailable when its player rating cannot be read', async () => {
  const svc = service(() => { throw new Error('offline'); });
  try {
    const board = await svc.leaderboard(ID, 50);
    assert.equal(board.available, false);
    assert.deepEqual(board.rows, []);
    assert.equal(board.you, null);
  } finally { svc.shutdown(); }
});

test('an in-flight read cannot overwrite a newer in-process rank update', async () => {
  let release;
  const svc = service(() => new Promise(resolve => { release = resolve; }));
  try {
    const pending = svc._internals.loadRating(ID);
    svc._internals.saveRating(ID, {...saved, matches: 9, updated: 20});
    release(JSON.stringify(saved));
    assert.equal((await pending).matches, 9);
  } finally { svc.shutdown(); }
});

test('HTTP storage failures never masquerade as a missing rating, and recovery restores the board', async () => {
  const originalFetch = global.fetch;
  const keys = ['UPSTASH_REDIS_REST_URL', 'UPSTASH_REDIS_REST_TOKEN', 'HUB_TEST_TOKENS'];
  const oldEnv = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  process.env.UPSTASH_REDIS_REST_URL = 'https://rank-store.invalid';
  process.env.UPSTASH_REDIS_REST_TOKEN = 'test-only';
  process.env.HUB_TEST_TOKENS = 'rank-token=' + ID;
  let failure = 'http';
  global.fetch = async (url, options) => {
    if (url !== 'https://rank-store.invalid') return originalFetch(url, options);
    const [op, key] = JSON.parse(options.body);
    if (op === 'GET' && key.endsWith('rating:' + ID)) {
      if (failure === 'http') return new Response('', {status: 503});
      if (failure === 'redis') return Response.json({error: 'unavailable'});
      if (failure === 'network') throw new Error('offline');
      return Response.json({result: JSON.stringify(saved)});
    }
    return Response.json({result: op === 'SCAN' ? ['0', []] : op === 'SMEMBERS' ? [] : null});
  };
  const server = require('../server.cjs').createServer();
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const url = `http://127.0.0.1:${server.address().port}/api/leaderboard`;
    for (failure of ['http', 'redis', 'network', 'none']) {
      const response = await originalFetch(url, {headers: {authorization: 'Bearer rank-token'}});
      assert.equal(response.status, 200);
      const body = await response.json();
      assert.equal(body.available, failure === 'none', failure);
      if (failure === 'none') assert.equal(body.you.matches, 8);
    }
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    global.fetch = originalFetch;
    for (const key of keys) {
      if (oldEnv[key] === undefined) delete process.env[key];
      else process.env[key] = oldEnv[key];
    }
  }
});
