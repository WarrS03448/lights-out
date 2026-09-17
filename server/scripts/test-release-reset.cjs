// Run: node --test server/scripts/test-release-reset.cjs
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const reset = require('../../tools/release/reset-ranked.cjs');

test('a configured cutoff must be an exact positive millisecond timestamp', () => {
  for(const value of ['oops','2026-09-17T18:00:00Z','0','-1','1.5','', '9007199254740992']) {
    assert.throws(()=>reset.cutoverTimestamp(value),value);
  }
  assert.equal(reset.cutoverTimestamp('1789670000000'),1789670000000);
});

test('the release reset selects game data and preserves identity, moderation and audits', () => {
  for (const key of ['rating:1','rating-operation:a','leaderboard','leaderboard:rr','history:1',
    'match:a','chat:a','live:matches','live:match:a','settlement:a','result:1','probe',
    'analytics:match:a','analytics:summary:a',
    'analytics:matches','analytics:day:2026-09-17','analytics:projected:a','analytics:terminal:a',
    'analytics:outbox','analytics:backfill_cursor','analytics:backfill_done:v1',
    'analytics:event:a','analytics:events','analytics:reliability:2026-09-17','analytics:health']) {
    assert.equal(reset.isResetKey('hub:'+key,'hub:'),true,key);
  }
  for (const key of ['other:rating:1','hub:auth:token','hub:friends:1','hub:roster','hub:ban:1',
    'hub:penalty:1','hub:reports:1','hub:analytics:audit:a','hub:analytics:audits',
    'hub:adminprefs:1','hub:comp:gamemode-build','hub:events:install:BB5','hub:bugs',
    'hub:combat:history:1','hub:combat:sanction:a']) {
    assert.equal(reset.isResetKey(key,'hub:'),false,key);
  }
  assert.throws(()=>reset.isResetKey('rating:1',''));
});

test('reset retains account identity and report counters while clearing every career field', () => {
  const before={steam_id:'76561198000000001',persona:'Player',reports:2,first_seen:1,
    kills:9,rounds:14,progress:100,last_map:'Rome',mmr:2100,played:7,wins:4};
  const after=reset.resetRoster(before,123456);
  assert.equal(after.persona,'Player');assert.equal(after.reports,2);assert.equal(after.first_seen,1);
  for(const k of ['kills','rounds','progress','played','wins','rated','losses','last_match']) assert.equal(after[k],0,k);
  assert.equal(after.last_map,'');assert.equal(after.mmr,1500);assert.equal(after.result_revision,123456);
  assert.equal(before.kills,9);
});

test('preflight refuses malformed roster data before any write', async () => {
  const writes=[];
  const store=async cmd=>{
    if(cmd[0]==='SCAN')return ['0',['hub:rating:1']];
    if(cmd[0]==='HGETALL')return ['76561198000000001','not json'];
    writes.push(cmd);throw Error('unexpected write');
  };
  await assert.rejects(reset.plan(store,'hub:',123456));
  assert.deepEqual(writes,[]);
});
