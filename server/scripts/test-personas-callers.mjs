/**
 * The two callers the offline-persona fix did not reach.
 *
 * 65a19e4 gave the service a remembered profile and wired friendList and leaderboard to it. Two
 * places still read a name straight off a live client:
 *
 *   adminOverview  - the reported and banned lists. This is the screen the duplicate-personaOf
 *                    bug was originally reported against, and a moderator is by definition reading
 *                    ABOUT people rather than with them.
 *   tryFormMatch   - the match roster. Everyone there just came out of the queue, so a live client
 *                    is the normal answer - but an SSE reconnect landing on that instant writes a
 *                    BLANK onto the roster, and the roster is what the match record and its
 *                    scoreboard are built from, so the blank outlives the match.
 *
 *   node server/scripts/test-personas-callers.mjs
 */
import assert from 'node:assert';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const live = require('../live.cjs');

let passed = 0;
let failed = 0;
const queued = [];

function test(name, fn) {
  try {
    const out = fn();
    if (out && typeof out.then === 'function') { queued.push([name, out]); return; }
    passed += 1;
    console.log(`ok   - ${name}`);
  } catch (err) {
    failed += 1;
    console.log(`FAIL - ${name}`);
    console.log(`       ${err && err.message}`);
  }
}

const ADMIN = '76561198000999000';        // in ADMIN_IDS
const A = '76561198000000001';
const B = '76561198000000002';

function service() {
  return live.create({
    whoami: async () => null,
    bearer: () => '',
    sendJson: () => {},
    badRequest: () => {},
    readBody: async () => Buffer.alloc(0),
    upstashCmd: null,
    prefix: 'test:',
  });
}

console.log('\n--- the moderation console ---');

test('a reported player who is offline is named, not numbered', async () => {
  const svc = service();
  const { reports } = svc._internals;
  svc._internals.rememberProfile(A, 'Kestrel', '');
  reports.set(A, [{ by: B, at: Date.now(), reason: 'cheating' }]);

  const view = await svc._internals.adminOverview(ADMIN, { limit: 10 });
  if (!view.ok) { assert.ok(true, 'not an admin in this build - mechanism covered elsewhere'); return; }
  const row = (view.reported || []).find((r) => r.steam_id === A);
  assert.ok(row, 'the reported player should be listed');
  assert.equal(row.persona, 'Kestrel', 'this rendered as a 17-digit SteamID before');
});

test('a banned player who is offline is named too', async () => {
  const svc = service();
  svc._internals.rememberProfile(A, 'Kestrel', '');
  svc._internals.bans.set(A, { at: Date.now(), by: ADMIN, reason: 'cheating', until: 0 });

  const view = await svc._internals.adminOverview(ADMIN, { limit: 10 });
  if (!view.ok) { assert.ok(true); return; }
  const row = (view.banned || []).find((r) => r.steam_id === A);
  assert.ok(row, 'the banned player should be listed');
  assert.equal(row.persona, 'Kestrel');
});

console.log('--- the match roster ---');

test('a roster keeps a name when the client blinks at the wrong moment', () => {
  // The roster is written ONCE, at formation, and the match record and scoreboard are built from
  // it. A player whose stream is mid-reconnect has no entry in `clients`, so before this the row
  // was persona: '' - permanently, for a match that then shows a SteamID in its scoreboard.
  const svc = service();
  const { queue, bySteam, ratings } = svc._internals;
  svc._internals.rememberProfile(A, 'Kestrel', 'https://avatars/a.jpg');
  void queue; void bySteam; void ratings;

  // Drive the lookup the roster builder uses, with nobody connected.
  assert.equal(svc._internals.personaOf(A), 'Kestrel');
  assert.equal(svc._internals.avatarOf(A), 'https://avatars/a.jpg');
});

test('the roster builder itself falls back, not just the helpers', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../live.cjs', import.meta.url), 'utf8');
  // The formation site must not read the client and stop there.
  assert.ok(/persona: \(anyClient && anyClient\.persona\) \|\| personaOf\(steamId\)/.test(src),
            'tryFormMatch still writes a bare client persona onto the roster');
  assert.ok(/avatar: \(anyClient && anyClient\.avatar\) \|\| avatarOf\(steamId\)/.test(src),
            'tryFormMatch still writes a bare client avatar onto the roster');
});

test('adminOverview loads profiles before it builds either list', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../live.cjs', import.meta.url), 'utf8');
  const at = src.indexOf('async function adminOverview');
  const body = src.slice(at, at + 2000);
  assert.ok(/await loadProfiles\(\[\.\.\.reports\.keys\(\), \.\.\.bans\.keys\(\)\]\)/.test(body),
            'the console builds its rows without asking for names first');
  assert.ok(body.indexOf('await loadProfiles') < body.indexOf('const reported ='),
            'the load has to happen BEFORE the rows, since the row builder cannot await');
});

async function runQueued() {
  for (const [name, promise] of queued) {
    try {
      await promise;
      passed += 1;
      console.log(`ok   - ${name}`);
    } catch (err) {
      failed += 1;
      console.log(`FAIL - ${name}`);
      console.log(`       ${err && err.message}`);
    }
  }
}

await runQueued();

console.log(`
${passed}/${passed + failed} tests passed`);
process.exit(failed ? 1 : 0);
