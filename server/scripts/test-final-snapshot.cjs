const assert = require('node:assert/strict');
const { test } = require('node:test');
const live = require('../live.cjs');

function fixture(t, count = 4) {
  let fail = false, commits = 0, lastPlan;
  const receipts = new Map();
  const store = async args => {
    if (store.before) await store.before(args);
    if (args[0] === 'GET') return receipts.get(args[1]) || null;
    if (args[0] === 'SMEMBERS') return [];
    if (args[0] === 'HGET') return null;
    if (args[0] === 'SET') { receipts.set(args[1], args[2]); return 'OK'; }
    if (args[0] === 'EVAL') {
      if (args[1].includes('rank-snapshot-v1')) { receipts.set(args[4], args.at(-2)); return ['saved']; }
      if (args[1].includes('local incoming = cjson.decode')) return 0;
      commits++;
      if (fail) throw Error('offline');
      const plan = JSON.parse(args.at(-1));
      lastPlan = {...plan, receipt: JSON.parse(plan.receipt_json)};
      const saved = receipts.get(args[3]) || plan.receipt_json;
      receipts.set(args[3], saved);
      return saved;
    }
    return 'OK';
  };
  const L = live.create({ upstashCmd: store });
  const ids = Array.from({ length: count }, (_, i) => String(76561198000000001n + BigInt(i)));
  const teams = { 1: ids.filter((_, i) => i % 2 === 0), 2: ids.filter((_, i) => i % 2) };
  const match = { id: '0123456789abcdef', state: 'live', host: ids[0], created: Date.now(),
    players: ids.map(steam_id => ({ steam_id, connected: true, accepted: true })),
    teams, assigned_teams: structuredClone(teams), start_ready_verified: true, expected_score_limit: 7, expected_max_rounds: 12,
    combat_end: { epoch: 'test-epoch', seq: 1, complete: false },
    left: [], timer: null, deadline: 0, map: 'Rome', score: { 1: 7, 2: 2 } };
  L._internals.matches.set(match.id, match);
  for (const id of ids) L._internals.inMatch.set(id, match.id);
  t.after(() => { clearTimeout(match.timer); clearTimeout(match.collectTimer); });
  const fields = { match_id: match.id, meta: '9;7;0;7;1;2', combat_end: 'test-epoch;1',
    rows: ids.map((id, i) => `${id}|k=3;d=2;sp=10;t=${i%2};s=${i%2 ? 2 : 7};a=false`).join(',') + ',' };
  return { L, match, fields, fail: value => { fail = value; }, commits: () => commits, plan: () => lastPlan, store };
}

test('final receipt contains effective analytical rules and a durable export reference', async t => {
  const {L,match,fields,plan}=fixture(t);
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,true);
  assert.ok(plan().receipt.analytics_context?.rules?.rating?.START_RATING);
  assert.ok(plan().analytics_outbox);
});

test('non-protocol or escape-inflated combat batches are rejected before state changes',async t=>{
  for(const bad of ['\u0000','\ud800','\\'.repeat(5000)]){
    const {L,match}=fixture(t);
    assert.equal((await L.combatBatch(match.host,match.id,bad)).ok,false);
    assert.equal(match.combatState,undefined);
  }
});

test('authoritative final totals replace an earlier last-round death sample',async t=>{
  const {L,match,fields,plan}=fixture(t);
  match.stats={series:{[match.host]:{8:{deaths:0}}}};
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,true);
  assert.equal(plan().receipt.inputs.stats.round_index_base,0);
  assert.equal(plan().receipt.inputs.stats.series[match.host][8].deaths,2);
});

test('maximum retained combat evidence commits below the storage request limit and replays losslessly', async t => {
  const {L,match,fields,store}=fixture(t,10);
  const combat=require('../combat.cjs').createState();
  combat.rejected=Array.from({length:20000},(_,i)=>({hash:String(i),reason:'invalid',raw:require('node:crypto').randomBytes(600).toString('base64')}));
  match.combatState=combat;
  let largest=0;
  store.before=async args=>{
    largest=Math.max(largest,Buffer.byteLength(JSON.stringify(args)));
    if(largest>10*1024*1024)throw Error('storage request exceeds 10 MiB');
  };
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,true,`${largest} byte request`);
  const raw=await store(['GET',`hub:settlement:${match.id}`]);
  const receipt=await require('../combat-storage.cjs').unpack(store,JSON.parse(raw),`hub:settlement:${match.id}`);
  assert.deepEqual(receipt.inputs.combatState,combat);
  const reboot=live.create({upstashCmd:store});
  assert.equal((await reboot.completion(match.host,match.id)).close_allowed,true);
  t.diagnostic(`largest request: ${largest} bytes`);
});

test('complete final batch is saved once and can be read after server recreation', async t => {
  const { L, match, fields, commits, store } = fixture(t);
  const r = await L.finalSnapshot(match.host, fields);
  assert.equal(r.ok, true); assert.equal(r.data_collected, true);
  assert.equal(L._internals.matches.has(match.id), false);
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
  assert.equal(commits(), 1);
  const reboot = live.create({ upstashCmd: store });
  const receipt = await reboot.completion(match.host, match.id);
  assert.equal(receipt.close_allowed, true);
  assert.equal(receipt.result.scoreboard.length, 4);
  assert.equal(receipt.result.round_details.length, 9);
  const detail = await reboot._internals.readMatch(match.id, match.host);
  assert.deepEqual(detail.round_details, receipt.result.round_details);
  assert.equal((await reboot.completion('76561198999999999', match.id)).ok, false);
});

test('ten-player twelve-round results fit storage and retain each round after replay', async t => {
  for (const draw of [false, true]) {
    const { L, match, fields, store } = fixture(t, 10);
    const ids = match.players.map(p => p.steam_id);
    match.score = draw ? { 1: 6, 2: 6 } : { 1: 7, 2: 5 };
    fields.meta = `12;7;0;${match.score[1]};1;${match.score[2]}`;
    fields.rows = ids.map((id, i) => `${id}|k=3;d=2;sp=10;t=${i%2};s=${match.score[i%2+1]};a=false`).join(',') + ',';
    match.combatState = require('../combat.cjs').createState();
    match.combatState.roster = Object.fromEntries(ids.map((id, i) => [id, i%2]));
    Object.assign(match.combatState.coverage, { started: true, closed: true, damage: true });
    match.combatState.events = Array.from({ length: 12 }, (_, n) => ({
      kind: 'health', n, a: ids[0], b: ids[1], loss: 35, relation: 'enemy', old: 100, new: 65,
    }));
    const emitted = [];
    L._internals.clients.set('round-test', { steamId: match.host, res: { write: data => {
      emitted.push(JSON.parse(data.slice(6)));
    } } });
    L._internals.bySteam.set(match.host, new Set(['round-test']));
    let bytes = 0;
    store.before = async args => {
      if (args[0] === 'EVAL' && args[1].includes('local p = cjson.decode(ARGV[1])')) {
        bytes = Buffer.byteLength(JSON.stringify(args));
        if (bytes > 10 * 1024 * 1024) throw Error('storage request exceeds 10 MiB');
      }
    };
    assert.equal((await L.finalSnapshot(match.host, fields)).ok, true, `${bytes} byte result must commit`);
    // Leave room for the match's raw evidence instead of repeating round boards per recipient.
    assert.ok(bytes < 4 * 1024 * 1024, `${bytes} byte result should retain storage headroom`);
    t.diagnostic(`${draw ? 'draw' : 'win'} result request: ${bytes} bytes`);
    const event = emitted.find(e => e.type === 'match_result');
    assert.equal(event.round_details.length, 12);
    const reboot = live.create({ upstashCmd: store });
    const detail = await reboot._internals.readMatch(match.id, match.host);
    for (const id of ids) {
      const replay = await reboot.completion(id, match.id);
      assert.equal(replay.close_allowed, true);
      assert.deepEqual(replay.result.round_details, event.round_details);
      assert.deepEqual(replay.result.round_details, detail.round_details);
      assert.equal(replay.result.round_details[11].scoreboard[0].combat.enemyDamage, 35);
    }
  }
});

test('missing, malformed, duplicate and wrong-team final rows cannot complete', async t => {
  for (const modify of [f => f.rows = f.rows.split(',').slice(0, 3).join(','),
    f => f.rows = f.rows.replace(';d=2', ''), f => f.rows += f.rows.split(',')[0],
    f => f.rows = f.rows.replace(';t=0', ';t=1'), f => f.meta = '2;7;0;2;1;1']) {
    const { L, match, fields, commits } = fixture(t); modify(fields);
    assert.equal((await L.finalSnapshot(match.host, fields)).ok, false);
    assert.equal(commits(), 0); assert.equal(match.state, 'live');
  }
});

test('one through ten remaining players can finish; confirmed leavers retain their data and history', async t => {
  for(let remaining=1;remaining<=10;remaining++) {
    const {L,match,fields,plan}=fixture(t,10);
    const original=match.players.slice(), gone=original.slice(remaining);
    match.stats={players:gone.map(p=>({steamId:p.player_id,kills:2,deaths:1,teamId:match.assigned_teams[1].includes(p.player_id)?0:1}))};
    match.left=gone.map(p=>({...p,at:Date.now(),left_state:'live',disconnect_confirmed:true,reason:'reconnect_timeout'}));
    match.players=original.slice(0,remaining);
    gone.forEach(p=>L._internals.inMatch.delete(p.player_id));
    fields.rows=fields.rows.split(',').slice(0,remaining).join(',');
    assert.equal((await L.finalSnapshot(match.host,fields)).ok,true,`remaining=${remaining}`);
    assert.equal(plan().receipt.participants.length,10);
    assert.equal(Object.keys(plan().receipt.history).length,10);
    for(const p of gone) {
      const row=plan().receipt.full.final_stats.find(r=>r.steamId===p.player_id);
      assert.equal(row.kills,2); assert.equal(row.deaths,1); assert.equal(row.stats_complete,false);
    }
  }
});

test('a confirmed disconnect in the last round does not hold results for the unused reconnect window',async t=>{
  const {L,match,fields,plan}=fixture(t);
  const id=match.players.at(-1).player_id, since=Date.now();
  match.reconnect={[id]:{since,deadline:since+300000}};
  fields.rows=fields.rows.split(',').slice(0,3).join(',');
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,true);
  const row=plan().receipt.full.final_stats.find(r=>r.steamId===id);
  assert.equal(row.stats_complete,false);
  assert.equal(row.kills,undefined,'unknown totals must not be invented');
  assert.equal(plan().receipt.participants.length,4);
});

test('a missing final row cannot itself establish a disconnect',async t=>{
  const {L,match,fields}=fixture(t);
  const gone=match.players.pop(); match.left=[{...gone,left_state:'live'}];
  fields.rows=fields.rows.split(',').slice(0,3).join(',');
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,false);
});

test('failed presence persistence cannot authorize missing final data',async t=>{
  const {L,match,fields,store}=fixture(t);
  store.before=async args=>{if(args[0]==='EVAL')throw Error('offline');};
  const rows=match.players.slice(0,3).map(p=>({steam_id:p.game_steam_id,active:1}));
  assert.equal((await L.matchPresence(match.host,match.id,rows)).ok,false);
  assert.equal(match.reconnect,undefined);
  store.before=null;
  fields.rows=fields.rows.split(',').slice(0,3).join(',');
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,false);
});

test('an expired missing player must be adjudicated before an incomplete final can save',async t=>{
  const {L,match,fields}=fixture(t);
  const id=match.players.at(-1).player_id,since=Date.now()-300001;
  match.reconnect={[id]:{since,deadline:since+300000}};
  fields.rows=fields.rows.split(',').slice(0,3).join(',');
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,false);
  assert.equal(match.final_snapshot,undefined);
});

test('optional departed ghost rows cannot change the effective final fingerprint on retry',async t=>{
  const {L,match,fields,fail,plan}=fixture(t);
  const p=match.players.pop();
  match.left=[{...p,left_state:'live',disconnect_confirmed:true,last_stats:{steamId:p.player_id,kills:2,deaths:1}}];
  L._internals.inMatch.delete(p.player_id);
  fail(true);assert.equal((await L.finalSnapshot(match.host,fields)).ok,false);
  fields.rows=fields.rows.split(',').slice(0,3).join(',');
  fail(false);assert.equal((await L.finalSnapshot(match.host,fields)).ok,true);
  assert.equal(plan().receipt.full.final_stats.find(r=>r.steamId===p.player_id).kills,2);
});

test('a last-round disconnect with a lingering PlayerState stays incomplete across final retries',async t=>{
  const {L,match,fields,fail,plan}=fixture(t);
  const id=match.players.at(-1).player_id,since=Date.now();
  match.reconnect={[id]:{since,deadline:since+300000}};
  match.stats={players:[{steamId:id,kills:1,deaths:1}]};
  fail(true);assert.equal((await L.finalSnapshot(match.host,fields)).ok,false);
  fields.rows=fields.rows.split(',').slice(0,3).join(',');
  fail(false);assert.equal((await L.finalSnapshot(match.host,fields)).ok,true);
  const row=plan().receipt.full.final_stats.find(r=>r.steamId===id);
  assert.equal(row.kills,1);assert.equal(row.disconnected,true);assert.equal(row.stats_complete,false);
  assert.equal(plan().receipt.inputs.stats.series[id],undefined);
});

test('a lingering PlayerState cannot bypass an expired reconnect penalty',async t=>{
  const {L,match,fields}=fixture(t);
  const id=match.players.at(-1).player_id,since=Date.now()-300001;
  match.reconnect={[id]:{since,deadline:since+300000}};
  assert.equal((await L.finalSnapshot(match.host,fields)).ok,false);
  assert.equal(match.final_snapshot,undefined);
});

test('storage failure never releases players or updates ratings; retry commits once', async t => {
  const { L, match, fields, fail } = fixture(t); fail(true);
  const before = structuredClone(L._internals.ratingOf(match.host));
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, false);
  assert.equal(L._internals.inMatch.get(match.host), match.id);
  assert.deepEqual(L._internals.ratingOf(match.host), before);
  assert.equal((await L.completion(match.host, match.id)).close_allowed, false);
  fail(false);
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
});

test('timer or ordinary stat rows never complete a strict match', t => {
  const { L, match } = fixture(t);
  L._internals.beginCollection(match, 1, match.score, 7);
  match.stats.seenAt = Object.fromEntries(match.players.map(p => [p.steam_id, Date.now() + 100]));
  assert.equal(L._internals.collectionComplete(match), false);
  assert.ok(!match.collectTimer);
  assert.doesNotThrow(() => JSON.stringify(L._internals.serialiseMatch(match)));
});

test('frozen game rules and full assigned roster are required', async t => {
  for (const change of [
    ({ fields }) => { fields.meta = '1;1;0;1;1;0'; },
    ({ match }) => { match.players.pop(); },
    ({ L, match }) => { L._internals.inMatch.delete(match.players[1].steam_id); },
  ]) {
    const f = fixture(t); change(f);
    assert.equal((await f.L.finalSnapshot(f.match.host, f.fields)).ok, false);
    assert.equal(f.commits(), 0);
  }
});

test('a failed save freezes final data and preserves richer accumulated metrics', async t => {
  const { L, match, fields, fail, plan } = fixture(t);
  match.stats = { players: [{ steamId: match.host, kills: 1, clutches: 2, ping: 34, roundsWon: 7 }] };
  fail(true);
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, false);
  fail(false);
  assert.equal((await L.finalSnapshot(match.host, { ...fields, rows: fields.rows.replace('k=3', 'k=4') })).ok, false);
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
  const row = plan().receipt.full.final_stats.find(p => p.steamId === match.host);
  assert.equal(row.kills, 3); assert.equal(row.clutches, 2); assert.equal(row.ping, 34);
  assert.equal(plan().receipt.careers[match.host].played, 1);
  assert.equal(plan().receipt.careers[match.host].clutches, 2);
  assert.equal(plan().hashes.length, 4);
});

test('max-round draw completes without changing ratings and simultaneous retries commit once', async t => {
  const { L, match, fields, commits, plan } = fixture(t);
  fields.meta = '12;7;0;6;1;6'; fields.rows = fields.rows.replace(/;s=\d+/g, ';s=6');
  match.score = { 1: 6, 2: 6 };
  const replies = await Promise.all([L.finalSnapshot(match.host, fields), L.finalSnapshot(match.host, fields)]);
  assert.ok(replies.every(r => r.ok)); assert.equal(commits(), 1);
  assert.equal(plan().receipt.full.draw, true);
  assert.equal(plan().receipt.events[match.host].rr_delta, 0);
  assert.equal(plan().board.length, 0);
});

test('partial combat batches must drain before final completion and replay without duplicating events', async t => {
  const { L, match, fields, plan } = fixture(t);
  delete match.combat_end;
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, false);
  assert.equal(match.final_snapshot, undefined);
  const rows = [
    'v=1;seq=1;n=0;t=0;phase=Game.Phase.Warmup;kind=coverage;epoch=test-epoch;observer=chcombat-1;roster=4;bound=0;gaps=0;complete=0;damage=1;shots=0;objectives=0',
    'v=1;seq=2;n=9;t=200;phase=Game.Phase.EndMatch;kind=coverage;terminal=1;epoch=test-epoch;observer=chcombat-1;roster=4;bound=0;gaps=0;complete=0;damage=1;shots=0;objectives=0',
  ].join('\n') + '\n';
  assert.equal((await L.combatBatch(match.host, match.id, rows)).ok, true);
  assert.equal((await L.combatBatch(match.host, match.id, rows)).ok, true);
  assert.equal(match.combatState.events.length, 2);
  const later = rows.split('\n')[0].replace('seq=1', 'seq=3');
  assert.equal((await L.combatBatch(match.host, match.id, later)).ok, false);
  assert.equal(match.combatState.events.length, 2);
  fields.combat_end = 'test-epoch;2';
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
  assert.equal(plan().receipt.inputs.combatState.events.length, 2);
  assert.equal(plan().receipt.full.combat_end.complete, false);
  assert.equal(plan().receipt.rows[0].after.revision, 1);
});

test('final data waits for the entire admitted combat batch to save', async t => {
  const { L, match, fields, store, commits } = fixture(t);
  delete match.combat_end;
  let release, entered;
  const writing = new Promise(resolve => { entered = resolve; });
  const blocked = new Promise(resolve => { release = resolve; });
  store.before = async args => { if (args[0] === 'EVAL' && args[1].includes('rank-snapshot-v1')) { entered(); await blocked; } };
  const row = 'v=1;seq=1;n=9;t=200;phase=Game.Phase.EndMatch;kind=coverage;terminal=1;epoch=test-epoch;observer=chcombat-1;roster=4;bound=0;gaps=0;complete=0;damage=1;shots=0;objectives=0';
  const batch = L.combatBatch(match.host, match.id, row);
  await writing;
  const finish = L.finalSnapshot(match.host, fields);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(commits(), 0); assert.equal(match.final_snapshot, undefined);
  release(); assert.equal((await batch).ok, true); assert.equal((await finish).ok, true);
});

test('shutdown drains admitted final work before flushing and rejects new reports', async t => {
  const { L, match, fields, store } = fixture(t);
  let release, entered;
  const writing = new Promise(resolve => { entered = resolve; });
  const blocked = new Promise(resolve => { release = resolve; });
  store.before = async args => {
    if (args[0] === 'GET' && args[1] === L._internals.settlementKey(match.id)) { entered(); await blocked; }
  };
  let finished = false;
  const final = L.finalSnapshot(match.host, fields);
  const shutdown = L.shutdown().then(() => { finished = true; });
  await writing;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, false);
  release();
  assert.equal((await final).ok, true);
  await shutdown;
  assert.equal(finished, true);
  const late = await L.finalSnapshot(match.host, fields);
  assert.equal(late.ok, false);
  assert.equal(late.close_allowed, false);
});

test('replaying an old completion receipt cannot replace newer cached career or history', async t => {
  const { L, match, fields } = fixture(t);
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
  const id = match.host, state = L._internals;
  const newer = { ...state.careers.get(id), played: 2, result_revision: Date.now() + 1000 };
  state.careers.set(id, newer);
  const previous = state.history.get(id);
  state.history.set(id, [{ id: 'fedcba9876543210', at: Date.now() + 1000 }, ...previous]);
  assert.equal((await L.finalSnapshot(id, fields)).ok, true);
  assert.deepEqual(state.careers.get(id), newer);
  assert.equal(state.history.get(id)[0].id, 'fedcba9876543210');
});

test('an invalid captured sample is saved as partial evidence without blocking later rows or completion', async t => {
  const { L, match, fields, plan, store } = fixture(t);
  delete match.combat_end;
  const coverage = seq => `v=1;seq=${seq};n=0;t=2;phase=Game.Phase.WaitingForPlayers;kind=coverage;epoch=test-epoch;observer=chcombat-1;roster=4;bound=4;gaps=0;complete=0;damage=1;shots=0;objectives=0`;
  const invalid = `v=1;seq=2;n=0;t=3;phase=Game.Phase.WaitingForPlayers;kind=health;epoch=test-epoch;b=${match.host};bt=0;old=20;new=-35;max=100`;
  const terminal = coverage(4).replace('kind=coverage;', 'kind=coverage;terminal=1;');
  const rows = [coverage(1), invalid, coverage(3), terminal].join('\n');
  assert.equal((await L.combatBatch(match.host, match.id, rows)).ok, true);
  assert.equal((await L.combatBatch(match.host, match.id, rows)).ok, true);
  const saved = JSON.parse(await store(['GET', L._internals.liveMatchKey(match.id)]));
  assert.equal(saved.combatState.rejected.length, 1);
  assert.equal(saved.combatState.rejected[0].raw, invalid);
  assert.equal(saved.combatState.coverage.broken, true);
  assert.deepEqual(saved.combatState.events.map(e => e.seq), [1, 3, 4]);
  assert.equal(saved.combat_end.seq, 4);
  assert.equal(saved.combat_end.complete, false);
  fields.combat_end = 'test-epoch;4';
  assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
  assert.equal(plan().receipt.inputs.combatState.rejected[0].raw, invalid);
});

test('an all-invalid batch is acknowledged only after its diagnostic is saved, including retry after failure', async t => {
  const { L, match, store } = fixture(t); delete match.combat_end;
  let fail = true;
  store.before = async args => { if (args[0] === 'EVAL' && args[1].includes('rank-snapshot-v1') && fail) throw Error('save unavailable'); };
  const invalid = 'v=1;seq=2;n=0;t=3;phase=Game.Phase.WaitingForPlayers;kind=health;old=10;new=0;max=100';
  await assert.rejects(L.combatBatch(match.host, match.id, invalid), /save unavailable/);
  fail = false;
  assert.equal((await L.combatBatch(match.host, match.id, invalid)).ok, true);
  const saved = JSON.parse(await store(['GET', L._internals.liveMatchKey(match.id)]));
  assert.equal(saved.combatState.rejected.length, 1);
  assert.equal(saved.combat_end, undefined);
});

test('a failed retry of a durable terminal never reopens combat admission', async t => {
  const { L, match, store } = fixture(t); delete match.combat_end;
  const row = 'v=1;seq=1;n=9;t=200;phase=Game.Phase.EndMatch;kind=coverage;terminal=1;epoch=test-epoch;observer=chcombat-1;roster=4;bound=4;gaps=0;complete=0;damage=1;shots=0;objectives=0';
  assert.equal((await L.combatBatch(match.host, match.id, row)).ok, true);
  const marker = structuredClone(match.combat_end);
  store.before = async args => { if (args[0] === 'EVAL' && args[1].includes('rank-snapshot-v1')) throw Error('offline'); };
  await assert.rejects(L.combatBatch(match.host, match.id, row), /offline/);
  assert.deepEqual(match.combat_end, marker);
  store.before = null;
  assert.equal((await L.combatBatch(match.host, match.id, row.replace('seq=1', 'seq=2'))).ok, false);
});

test('combined capture ceiling preserves bounded partial evidence and still drains the terminal', async t => {
  const combat = require('../combat.cjs');
  for (const legacyFull of [false, true]) {
    const { L, match, store, fields, plan } = fixture(t); delete match.combat_end;
    const coverage = seq => `v=1;seq=${seq};n=0;t=1;phase=Game.Phase.StartRound;kind=coverage;epoch=test-epoch;observer=chcombat-1;roster=4;bound=4;gaps=0;complete=0;damage=1;shots=0;objectives=0`;
    assert.equal((await L.combatBatch(match.host, match.id, coverage(1))).ok, true);
    const count = combat.POLICY.maxEvents - (legacyFull ? 0 : 1);
    match.combatState.events = Array.from({ length: count }, (_, i) => ({ ...combat.parseRow(coverage(1)), seq: i + 1 }));
    match.combatState.lastSeq = count;
    match.combatState.rejected = [{ hash: 'old-invalid', raw: 'old-invalid', reason: 'malformed' }];
    const finalSeq = count + 3;
    const terminal = coverage(finalSeq).replace('complete=0', 'terminal=1;complete=1');
    const body = ['malformed-overflow', coverage(1).replace('t=1;', 't=2;'), coverage(count + 2), terminal].join('\n');
    let fail = true;
    store.before = async args => { if (args[0] === 'EVAL' && args[1].includes('rank-snapshot-v1') && fail) throw Error('offline'); };
    await assert.rejects(L.combatBatch(match.host, match.id, body), /offline/);
    const overflow = structuredClone(match.combatState.overflow);
    fail = false;
    assert.equal((await L.combatBatch(match.host, match.id, body)).ok, true);
    assert.equal((await L.combatBatch(match.host, match.id, body)).ok, true);
    assert.deepEqual(match.combatState.overflow, overflow);
    assert.equal(match.combatState.rejected.length, 1);
    assert.equal(match.combatState.events.length, count + 1);
    assert.equal(match.combat_end.seq, finalSeq);
    assert.equal(match.combat_end.complete, false);
    const key = L._internals.liveMatchKey(match.id);
    const saved = await require('../combat-storage.cjs').unpack(store, JSON.parse(await store(['GET', key])), key);
    assert.equal(saved.combatState.overflow.raw, 'malformed-overflow');
    fields.combat_end = `test-epoch;${finalSeq}`;
    assert.equal((await L.finalSnapshot(match.host, fields)).ok, true);
    assert.equal(plan().receipt.full.combat_end.complete, false);
  }
});
