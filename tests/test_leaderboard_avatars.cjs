// Run: node --test tests/test_leaderboard_avatars.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const live = require('../server/live.cjs');
const ID = '76561198000000011';
const AVATAR = 'https://avatars.steamstatic.com/example.jpg';

function service(profileOf) {
  return live.create({
    whoami: async () => null, bearer: () => '', sendJson() {}, badRequest() {},
    readBody: async () => Buffer.alloc(0), prefix: 'avatar-test:', profileOf,
    upstashCmd: async ([op, key]) => {
      if (op === 'ZREVRANGE') return [ID];
      if (op === 'GET' && key.includes('profile:')) return JSON.stringify({ persona: 'Player', avatar: '' });
      return null;
    },
  });
}

test('an offline saved name without a picture backfills Steam and exposes the avatar on both rows', async () => {
  let calls = 0;
  const svc = service(async () => { calls++; return { persona: 'Player', avatar: AVATAR }; });
  const board = await svc.leaderboard(ID, 10);
  assert.equal(board.rows[0].persona, 'Player');
  assert.equal(board.rows[0].avatar, AVATAR);
  assert.equal(board.you.avatar, AVATAR);
  await svc.leaderboard(ID, 10);
  assert.equal(calls, 1);
});

test('Steam failure keeps the known name and initials fallback without repeated lookups', async () => {
  let calls = 0;
  const svc = service(async () => { calls++; throw new Error('offline'); });
  const board = await svc.leaderboard(ID, 10);
  assert.equal(board.rows[0].persona, 'Player');
  assert.equal(board.rows[0].avatar, '');
  await svc.leaderboard(ID, 10);
  assert.equal(calls, 1);
});
