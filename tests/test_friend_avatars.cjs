const { test } = require('node:test');
const assert = require('node:assert/strict');
const live = require('../server/live.cjs');
const ME = '76561198000000010';
const FRIEND = '76561198000000011';
const AVATAR = 'https://avatars.steamstatic.com/example.jpg';

test('offline friends and requests retain their persisted profile picture after restart', async () => {
  const svc = live.create({
    whoami: async () => null, bearer: () => '', sendJson() {}, badRequest() {},
    readBody: async () => Buffer.alloc(0), prefix: 'avatar-test:',
    upstashCmd: async ([op, key]) => {
      if (op === 'GET' && key.includes('profile:')) return JSON.stringify({ persona: 'Player', avatar: AVATAR });
      if (op === 'SMEMBERS') return [FRIEND];
      return null;
    },
  });
  const result = await svc.friendList(ME);
  for (const kind of ['friends', 'incoming', 'outgoing']) {
    assert.equal(result[kind][0].online, false);
    assert.equal(result[kind][0].avatar, AVATAR, kind);
  }
});
