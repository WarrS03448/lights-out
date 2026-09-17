// UTF-8. node server/scripts/test-report-restart.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const live = require('../live.cjs');
const ADMIN = '76561198000999000', FRIEND = '76561198000999001';
const OTHER = '76561198000000001';
function fixture() {
  const records = new Map([
    ['test:reports:' + ADMIN, JSON.stringify([{at: 100, by: FRIEND, reason: 'cheating', match_id: 'saved'}])],
    ['test:match:saved', JSON.stringify({id: 'saved', players: [{steam_id: ADMIN}, {steam_id: FRIEND}]})],
  ]);
  let scans = 0;
  const service = live.create({whoami: async () => null, bearer: () => '',
    sendJson() {}, badRequest() {}, readBody: async () => Buffer.alloc(0), prefix: 'test:',
    upstashCmd: async ([op, key, value]) => {
      if (op === 'GET') return records.get(key) || null;
      if (op === 'SET') { records.set(key, value); return 'OK'; }
      if (op === 'SCAN') {
        scans++;
        return key === '0' ? ['7', []] : ['0', ['test:reports:' + ADMIN]];
      }
      return null;
    }});
  return {service, scans: () => scans};
}
test('admin Overview restores stored reports after a restart, including later SCAN pages', async () => {
  const {service, scans} = fixture();
  service._internals.ADMIN_IDS.add(ADMIN);
  try {
    assert.equal((await service.adminOverview(OTHER)).ok, false);
    assert.equal(scans(), 0, 'non-admin must not trigger a database scan');
    const view = await service.adminOverview(ADMIN);
    assert.equal(view.reported.length, 1);
    assert.equal(view.reported[0].steam_id, ADMIN);
    assert.equal(view.reported[0].total, 1);
    assert.deepEqual(view.reported[0].by_reason, {cheating: 1});
    await service.adminOverview(ADMIN);
    assert.equal(scans(), 2, 'only the first overview scans persisted keys');
  } finally { service._internals.ADMIN_IDS.delete(ADMIN); service.shutdown(); }
});
test('history reports use persisted membership after restart and still reject outsiders', async () => {
  const {service} = fixture();
  try {
    assert.equal((await service.reportPlayer(ADMIN, {target: FRIEND, reason: 'afk', match_id: 'saved'})).recorded, true);
    assert.equal((await service.reportPlayer(OTHER, {target: FRIEND, reason: 'afk', match_id: 'saved'})).ok, false);
    assert.equal((await service.reportPlayer(ADMIN, {target: OTHER, reason: 'afk', match_id: 'saved'})).ok, false);
    const duplicate = await service.reportPlayer(FRIEND, {target: ADMIN, reason: 'afk', match_id: 'saved'});
    assert.equal(duplicate.already, true);
    assert.equal((await service.reportsFor(ADMIN)).total, 1);
  } finally { service.shutdown(); }
});
test('custom explanation is stored and exposed to admins, with a bounded length', async () => {
  const {service} = fixture();
  service._internals.ADMIN_IDS.add(ADMIN);
  try {
    const body = {target: FRIEND, reason: 'other', match_id: 'saved'};
    assert.equal((await service.reportPlayer(ADMIN, {...body, note: '   '})).ok, false);
    assert.equal((await service.reportPlayer(ADMIN, {...body, note: 'x'.repeat(1001)})).ok, false);
    assert.equal((await service.reportPlayer(ADMIN, {...body, note: '  <b>Custom explanation</b>  '})).recorded, true);
    assert.equal((await service.reportsFor(FRIEND)).reports[0].note, '<b>Custom explanation</b>');
    const row = (await service.adminOverview(ADMIN)).reported.find(p => p.steam_id === FRIEND);
    assert.equal(row.reports[0].note, '<b>Custom explanation</b>');
  } finally { service._internals.ADMIN_IDS.delete(ADMIN); service.shutdown(); }
});
test('admin renders custom explanations as text, never HTML', async () => {
  const admin = require('../admin.cjs').create({prefix: '',
    upstashCmd: async () => ({steam_id: ADMIN}),
    live: () => ({isAdmin: () => true, adminOverview: async () => ({ok: true,
      reported: [{steam_id: FRIEND, total: 1, reporters: 1, by_reason: {other: 1},
        reports: [{at: 100, by: ADMIN, note: '<script>alert(1)</script>'}]}]})})});
  let html = '';
  await admin.route({headers: {host: 'hub.test', cookie: 'hubadmin=' + 'a'.repeat(64)}},
    {writeHead() {}, end(body) {html = String(body);}}, 'GET', '/admin', new URL('https://hub.test/admin'));
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(!html.includes('<script>alert(1)</script>'));
});
