'use strict';

const assert = require('node:assert/strict');
const server = require('../server.cjs');

const I = server._internals || {};
assert.equal(typeof I.dispatchRankedReport, 'function', 'ranked report dispatcher must be exported');
assert.equal(typeof I.describeHeaders, 'function', 'header redaction must be testable');
assert.equal(I.effectiveRankedRules().score_limit, 7,
             'ranked capability must snapshot the same base rule the hub builds');
assert.deepEqual(I.effectiveRankedRules(), {
  score_limit: 7, max_rounds: 13, team_switch_interval: 6, team_size: 5, max_players: 10,
}, 'official ranked matches use the published 5v5 rules and a deciding round');
const fs = require('node:fs');
const readFile = fs.readFileSync;
try {
  fs.readFileSync = function(filename, ...args) {
    if (String(filename).includes('manifest.json')) throw Error('source manifests are absent on Railway');
    return readFile.call(this, filename, ...args);
  };
  assert.equal(I.effectiveRankedRules().score_limit, 7,
               'the deployed catalogue must supply rules without source manifests');
} finally { fs.readFileSync = readFile; }
const savedOverride = process.env.COMP_GAME_RULES_OVERRIDE;
process.env.COMP_GAME_RULES_OVERRIDE = '{"BB5":{"score_limit":5}}';
assert.equal(I.effectiveRankedRules().score_limit, 5,
             'served rule override must replace the base manifest rule');
if (savedOverride === undefined) delete process.env.COMP_GAME_RULES_OVERRIDE;
else process.env.COMP_GAME_RULES_OVERRIDE = savedOverride;

const host = '76561198000000001';
const match = '0123456789abcdef';
const auth = { steamId: host, matchId: match, scoreLimit: 7 };
const calls = [];
const live = {
  gameReportedStats: (...args) => (calls.push(['stats', ...args]), { ok: true }),
  gameReportedScore: (...args) => (calls.push(['score', ...args]), { ok: true }),
  gameReportedTeam: (...args) => (calls.push(['team', ...args]), { ok: true }),
  teamKillReported: (...args) => (calls.push(['teamkill', ...args]), { ok: true }),
};

let result = I.dispatchRankedReport({
  event_name: 'ch_bb5_stats', user_id: '76561198999999999', storefront: 'ch-test-4821',
  platform: '76561198000000002|3:2:4:2:0', timestamp: '4',
}, auth, live);
assert.equal(result.status, 200);
assert.deepEqual(calls.pop(), ['stats', host, {
  match, rows: '76561198000000002|3:2:4:2:0', rounds: '4',
}]);

result = I.dispatchRankedReport({
  event_name: 'ch_bb5_score', user_id: '76561198999999999', storefront: match,
  platform: '0|0:7|1:5', timestamp: '999',
}, auth, live);
assert.equal(result.status, 200);
assert.deepEqual(calls.pop(), ['score', host, { match, scores: '0|0:7|1:5', limit: '7' }]);

result = I.dispatchRankedReport({
  event_name: 'ch_bb5_score', storefront: 'fedcba9876543210', platform: '0|0:7|1:5',
}, auth, live);
assert.equal(result.status, 409);
assert.equal(calls.length, 0);

result = I.dispatchRankedReport({
  event_name: 'ch_team_verified', storefront: '76561198000000002', platform: '0:1:1:1:1',
}, auth, live);
assert.equal(result.status, 200);
assert.deepEqual(calls.pop(), ['team', host, {
  subject: '76561198000000002', team: '1', verified: true,
}]);

result = I.dispatchRankedReport({
  event_name: 'ch_team_kill', storefront: '76561198000000002',
  timestamp: '76561198000000003', platform: '1:30:2:1:0',
}, auth, live);
assert.equal(result.status, 200);
assert.deepEqual(calls.pop(), ['teamkill', host, {
  killer: '76561198000000002', victim: '76561198000000003',
  team: '1', elapsed: '30', round: '2', alive0: '1', alive1: '0',
}]);

assert.equal(I.describeHeaders({ authorization: 'Bearer super-secret', accept: '*/*' }).authorization,
             '[redacted]');

async function httpRegressions() {
  const token = 'cd'.repeat(32);
  const mutations = [];
  const service = {
    _internals: { ready: Promise.resolve(), ensureRecovery: async () => 0 },
    authoriseReport: (value) => value === token ? auth : null,
    authoriseLegacyReport: (reporter, event, claimed) =>
      reporter === host && event === 'ch_bb5_score' && claimed === match ? auth : null,
    gameReportedStats: (...args) => (mutations.push(['stats', ...args]), { ok: true }),
    gameReportedScore: (...args) => (mutations.push(['score', ...args]), { ok: true }),
    gameReportedRound: (...args) => (mutations.push(['round', ...args]), { ok: true }),
    gameReportedCombat: async (...args) => { mutations.push(['combat', ...args]); if (args[1].row === 'fail') throw new Error('store unavailable'); return {ok:true}; },
    gameReportedKill: (...args) => (mutations.push(['kill', ...args]), { ok: true }),
    gameReportedIn: (...args) => (mutations.push(['arrival', ...args]), { ok: true }),
    gameReportedTeam: (...args) => (mutations.push(['team', ...args]), { ok: true }),
    teamKillReported: (...args) => (mutations.push(['teamkill', ...args]), { ok: true }),
    teamRuling: () => ({ ok: true, yes: true, team: 1 }),
  };
  const listener = server.createServer({ liveService: service });
  await new Promise((resolve) => listener.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${listener.address().port}`;
  const post = (path, body, bearer) => fetch(base + path, {
    method: 'POST', headers: {
      'content-type': 'application/json', ...(bearer ? { authorization: `Bearer ${bearer}` } : {}),
    }, body: JSON.stringify(body),
  });
  try {
    let response = await post('/api/match-report', { event_name: 'ch_bb5_stats' });
    assert.equal(response.status, 401);
    assert.equal(mutations.length, 0);

    response = await post('/api/probe', {
      event_name: 'ch_bb5_stats', user_id: host, storefront: match, platform: 'forged',
    });
    assert.equal(response.status, 200);
    assert.equal(mutations.length, 0, 'legacy unauthenticated probe must be telemetry only');

    let releaseRecovery;
    service._internals.ready = new Promise((resolve) => { releaseRecovery = resolve; });
    service._internals.ensureRecovery = async () => 1;
    const delayedLegacy = post('/api/probe', {
      event_name: 'ch_bb5_score', user_id: host, storefront: match,
      platform: '0|0:1|1:0', timestamp: '99',
    });
    const early = await Promise.race([
      delayedLegacy.then(() => 'responded'),
      new Promise((resolve) => setTimeout(() => resolve('waiting'), 20)),
    ]);
    assert.equal(early, 'waiting', 'first restored legacy report waits for recovery');
    assert.equal(mutations.length, 0);
    releaseRecovery();
    response = await delayedLegacy;
    assert.equal(response.status, 200);
    assert.deepEqual(mutations.pop(), ['score', host,
      { match, scores: '0|0:1|1:0', limit: '99' }],
      'restored legacy matches keep the score limit reported by their old pak');

    service._internals.ready = Promise.resolve();
    service._internals.ensureRecovery = async () => { throw new Error('store unavailable'); };
    response = await post('/api/probe', {
      event_name: 'ch_bb5_score', user_id: host, storefront: match,
      platform: '0|0:2|1:0', timestamp: '99',
    });
    assert.equal(response.status, 503, 'recovery failure asks the old reporter to retry');
    assert.equal(mutations.length, 0, 'failed recovery cannot acknowledge and drop the report');
    service._internals.ensureRecovery = async () => 1;

    response = await post('/api/match-report', {
      event_name: 'ch_bb5_stats', user_id: '76561198999999999', storefront: 'ch-test-4821',
      platform: 'row', timestamp: '2',
    }, token);
    assert.equal(response.status, 200);
    assert.deepEqual(mutations.pop(), ['stats', host, { match, rows: 'row', rounds: '2' }]);

    response = await post('/api/match-report', {
      event_name: 'ch_combat_v1', user_id: '76561198999999999', storefront: match, platform: 'row',
    }, token);
    assert.equal(response.status, 200);
    assert.deepEqual(mutations.pop(), ['combat',host,{match,row:'row'},{authenticated:true}]);
    response = await post('/api/match-report', {event_name:'ch_combat_v1',storefront:match,platform:'fail'},token);
    assert.equal(response.status,500,'failed durable evidence must never be acknowledged as success');
    mutations.pop();

    response = await post('/api/match-report', {
      event_name: 'ch_bb5_score', storefront: 'fedcba9876543210', platform: '0|0:7|1:2',
    }, token);
    assert.equal(response.status, 409);
    assert.equal(mutations.length, 0);
  } finally {
    await new Promise((resolve) => listener.close(resolve));
  }
}

httpRegressions().then(() => console.log('report auth tests passed'));
