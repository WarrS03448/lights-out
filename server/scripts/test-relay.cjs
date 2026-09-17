const assert = require('node:assert/strict');
const test = require('node:test');

function relay() {
  return require('../relay.cjs');
}

const validIce = () => ({
  iceServers: [{
    urls: [
      'stun:stun.cloudflare.com:3478',
      'turn:turn.cloudflare.com:3478?transport=udp',
      'turns:turn.cloudflare.com:5349?transport=tcp',
    ],
    username: 'ephemeral-user',
    credential: 'ephemeral-secret',
  }],
});

test('missing TURN configuration is unavailable without calling the provider', async () => {
  let calls = 0;
  const issuer = relay().createCredentialIssuer({
    keyId: '', apiToken: '', fetch: async () => { calls += 1; }, now: () => 1000,
  });
  await assert.rejects(() => issuer.issue('account-a'), (err) => err.status === 503);
  assert.equal(calls, 0);
});

test('issues only relay ICE URLs with the fixed provider request', async () => {
  const calls = [];
  const issuer = relay().createCredentialIssuer({
    keyId: 'key/id', apiToken: 'provider-token', now: () => 10_000,
    fetch: async (url, init) => {
      calls.push({ url, init });
      return { ok: true, json: async () => validIce() };
    },
  });
  const result = await issuer.issue('account-a');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url,
    'https://rtc.live.cloudflare.com/v1/turn/keys/key%2Fid/credentials/generate-ice-servers');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers.Authorization, 'Bearer provider-token');
  assert.deepEqual(JSON.parse(calls[0].init.body), { ttl: 600 });
  assert.deepEqual(result, {
    ok: true,
    transport: 'webrtc-relay-v1',
    iceServers: [{
      urls: [
        'turn:turn.cloudflare.com:3478?transport=udp',
        'turns:turn.cloudflare.com:5349?transport=tcp',
      ],
      username: 'ephemeral-user',
      credential: 'ephemeral-secret',
    }],
    expires_at: 610_000,
  });
});

test('coalesces issuance and refreshes inside the 120 second expiry margin', async () => {
  let now = 5_000;
  let calls = 0;
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const issuer = relay().createCredentialIssuer({
    keyId: 'key', apiToken: 'token', now: () => now,
    fetch: async () => {
      calls += 1;
      if (calls === 1) await pending;
      return { ok: true, json: async () => validIce() };
    },
  });
  const one = issuer.issue('account-a');
  const two = issuer.issue('account-a');
  release();
  const [a, b] = await Promise.all([one, two]);
  assert.equal(calls, 1);
  assert.deepEqual(a, b);
  now = a.expires_at - 120_001;
  assert.deepEqual(await issuer.issue('account-a'), a);
  assert.equal(calls, 1);
  now = a.expires_at - 120_000;
  const refreshed = await issuer.issue('account-a');
  assert.equal(calls, 2);
  assert.equal(refreshed.expires_at, now + 600_000);
});

test('provider failures and malformed or non-relay responses become bounded 503 errors', async () => {
  for (const response of [
    { ok: false, status: 401, json: async () => ({ error: 'permanent provider secret' }) },
    { ok: true, json: async () => ({ iceServers: [{ urls: ['stun:only.example'] }] }) },
    { ok: true, json: async () => ({ iceServers: 'wrong' }) },
  ]) {
    const issuer = relay().createCredentialIssuer({
      keyId: 'key', apiToken: 'token', now: () => 0, fetch: async () => response,
    });
    await assert.rejects(() => issuer.issue('account-a'), (err) => {
      assert.equal(err.status, 503);
      assert.equal(String(err.message).includes('secret'), false);
      assert.ok(String(err.message).length < 100);
      return true;
    });
  }
});

test('bounds provider latency and response/config sizes', async () => {
  let sawAbort = false;
  const hanging = relay().createCredentialIssuer({
    keyId:'key',apiToken:'token',now:()=>0,providerTimeoutMs:5,
    fetch:async (_url,init) => new Promise((_resolve,reject) => {
      init.signal.addEventListener('abort',()=>{ sawAbort=true; reject(new Error('aborted')); },{once:true});
    }),
  });
  await assert.rejects(() => hanging.issue('a'), (err) => err.status === 503);
  assert.equal(sawAbort,true);

  for (const response of [
    {ok:true,text:async()=> 'x'.repeat(70_000),json:async()=>validIce()},
    {ok:true,text:async()=>JSON.stringify({iceServers:[{
      urls:['turn:relay.example'],username:'u'.repeat(600),credential:'credential',
    }]}),json:async()=>({iceServers:[{
      urls:['turn:relay.example'],username:'u'.repeat(600),credential:'credential',
    }]})},
    {ok:true,text:async()=>JSON.stringify({iceServers:[{
      urls:['turn:'+'x'.repeat(3000)],username:'user',credential:'credential',
    }]}),json:async()=>({iceServers:[{
      urls:['turn:'+'x'.repeat(3000)],username:'user',credential:'credential',
    }]})},
  ]) {
    const issuer = relay().createCredentialIssuer({
      keyId:'key',apiToken:'token',now:()=>0,fetch:async()=>response,
    });
    await assert.rejects(() => issuer.issue('a'), (err) => err.status === 503);
  }
});

test('bounds credential cache and rate-limits repeated issuance per account', async () => {
  let calls = 0;
  const issuer = relay().createCredentialIssuer({
    keyId: 'key', apiToken: 'token', now: () => 0, maxCacheEntries: 2,
    fetch: async () => { calls += 1; return { ok: true, json: async () => validIce() }; },
  });
  await issuer.issue('a');
  await issuer.issue('b');
  await issuer.issue('c');
  await issuer.issue('a');
  assert.equal(calls, 4, 'oldest cache entry is evicted');

  calls = 0;
  const limited = relay().createCredentialIssuer({
    keyId: 'key', apiToken: 'token', now: () => 0, maxIssuesPerMinute: 2,
    fetch: async () => {
      calls += 1;
      return { ok: false, status: 503, json: async () => ({}) };
    },
  });
  await assert.rejects(() => limited.issue('same'), (err) => err.status === 503);
  await assert.rejects(() => limited.issue('same'), (err) => err.status === 503);
  await assert.rejects(() => limited.issue('same'), (err) => err.status === 429);
  assert.equal(calls, 2);
});

test('hard-bounds pending and fresh-account issuance state', async () => {
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  const pending = relay().createCredentialIssuer({
    keyId:'key',apiToken:'token',now:()=>0,maxPending:2,
    fetch:async()=>{ await wait; return {ok:true,json:async()=>validIce()}; },
  });
  const a = pending.issue('a');
  const b = pending.issue('b');
  await assert.rejects(() => pending.issue('c'), (err) => err.status === 429);
  release();
  await Promise.all([a,b]);

  let calls = 0;
  const bounded = relay().createCredentialIssuer({
    keyId:'key',apiToken:'token',now:()=>0,maxIssuesPerMinute:1,maxRateEntries:2,
    fetch:async()=>{ calls += 1; return {ok:false,status:503,json:async()=>({})}; },
  });
  for (const id of ['a','b','c','a']) {
    await assert.rejects(() => bounded.issue(id), (err) => err.status === 503);
  }
  assert.equal(calls,4,'old rate entries are evicted at the hard cap');
});

test('bounds per-account signal traffic in a rolling window', () => {
  let now = 1000;
  const limiter = relay().createRateLimiter({limit:2,windowMs:1000,maxEntries:2,now:()=>now});
  limiter.take('a');
  limiter.take('a');
  assert.throws(() => limiter.take('a'), (err) => err.status === 429);
  now = 2001;
  limiter.take('a');
  limiter.take('b');
  limiter.take('c');
  assert.ok(limiter.size() <= 2);
});

test('rejects legacy markers, non-relay candidates, leaked addresses, and media SDP', () => {
  const { validateSignal } = relay();
  const relayCandidate = {
    candidate: 'candidate:1 1 udp 1 203.0.113.8 50000 typ relay raddr 0.0.0.0 rport 0',
    sdpMid: '0', sdpMLineIndex: 0,
  };
  assert.deepEqual(validateSignal('candidate', relayCandidate), relayCandidate);
  assert.throws(() => validateSignal('candidate', {
    ...relayCandidate, candidate: 'candidate:1 1 udp 1 10.0.0.4 50000 typ host',
  }));
  assert.throws(() => validateSignal('candidate', {
    ...relayCandidate,
    candidate: 'candidate:1 1 udp 1 203.0.113.8 50000 typ relay raddr 192.168.1.7 rport 51111',
  }));
  assert.throws(() => validateSignal('candidate', {
    ...relayCandidate, candidate: 'candidate:1 1 udp 1 203.0.113.8 50000 typ relay',
  }));
  const dataOnly = [
    'v=0',
    'o=- 1 2 IN IP4 0.0.0.0',
    's=-',
    'c=IN IP4 0.0.0.0',
    't=0 0',
    'm=application 9 UDP/DTLS/SCTP webrtc-datachannel',
    'a=sctp-port:5000',
  ].join('\r\n') + '\r\n';
  assert.equal(validateSignal('offer', dataOnly), dataOnly);
  assert.throws(() => validateSignal('offer', dataOnly.replace('m=application', 'm=audio')));
  assert.throws(() => validateSignal('answer', dataOnly.replace('c=IN IP4 0.0.0.0', 'c=IN IP4 192.168.1.7')));
  assert.throws(() => validateSignal('answer', dataOnly.replace('o=- 1 2 IN IP4 0.0.0.0',
    'o=- 1 2 IN IP4 192.168.1.7')));
  assert.throws(() => validateSignal('answer', dataOnly.replace('c=IN IP4 0.0.0.0',
    'c=IN IP4 0.0.0.0 extra')));
  assert.throws(() => validateSignal('answer', dataOnly +
    'a=remote-candidates:1 192.168.1.7 51111\r\n'));
  assert.throws(() => validateSignal('answer', dataOnly +
    'a=candidate:1 1 udp 1 10.0.0.7 5000 typ host\r\n'));
  assert.throws(() => validateSignal('offer', 'legacy-steam-marker'));
});
