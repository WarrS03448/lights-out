'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const modulePath = require('node:path').join(__dirname, '../combat.cjs');
// A missing implementation is an assertion failure during the initial red run.
const combat = fs.existsSync(modulePath) ? require(modulePath) : {};
const A = '76561198000000001', B = '76561198000000002', C = '76561198000000003';
const NOW = 1800000000000;
function context(extra = {}) { return {matchId:'match-1',hostId:A,authenticated:true,validated:true,roster:{[A]:0,[B]:0,[C]:1},phase:'combat',now:NOW,...extra}; }
function row(extra = {}) { return {v:1,seq:1,kind:'health',n:1,t:20,phase:'combat',a:A,b:B,at:0,bt:0,old:100,new:0,max:100,...extra}; }
function wire(event) { return Object.entries(event).map(([k,v]) => `${k}=${v}`).join(';'); }
function ingest(state, extra = {}, ctx = {}) { return combat.ingest(state,row(extra),context(ctx)); }
function coverage(seq, complete = 0, extra = {}) { return row({seq,kind:'coverage',t:seq,epoch:'epoch-1',observer:'test-v1',roster:3,bound:3,gaps:0,damage:1,shots:0,objectives:0,complete,...extra}); }
function history(extra = {}) { return {id:'i1',actorId:A,matchId:'m1',round:1,startedAt:NOW,endedAt:NOW,gameTime:20,damage:50,victimIds:[B],validated:true,sanctionEligible:true,...extra}; }

test('player damage separates counterparties and directions using accepted health loss', () => {
  const s=combat.createState();combat.ingest(s,coverage(1),context());
  assert.equal(ingest(s,{seq:2,b:C,bt:1,new:70}).accepted,true);
  assert.equal(ingest(s,{seq:3,new:80}).accepted,true);
  assert.equal(ingest(s,{seq:4,a:C,at:1,b:A,bt:0,new:60}).accepted,true);
  ingest(s,{seq:5,kind:'hit',a:C,at:1,b:A,bt:0,weapon:'/Game/Bodycam_Player_C'});
  ingest(s,{seq:6,phase:'warmup',b:C,bt:1,old:70,new:0},{phase:'warmup'});
  const stats=combat.summary(s,A,1).playerStats;
  assert.deepEqual(stats,[{steam_id:B,damageDealt:20,damageTaken:null},{steam_id:C,damageDealt:30,damageTaken:40}]);
  assert.equal(combat.summary(s,C,1).playerStats.find(r=>r.steam_id===A).damageTaken,30);
});

test('player breakdown keeps unknown source and self damage separate without inventing zero coverage', () => {
  const s=combat.createState();combat.ingest(s,coverage(1),context());
  ingest(s,{seq:2,b:A,bt:0,new:90});
  const unknown=row({seq:3,b:A,bt:0,old:90,new:70});delete unknown.a;delete unknown.at;
  combat.ingest(s,unknown,context());
  const stats=combat.summary(s,A,1).playerStats;
  assert.deepEqual(stats.find(r=>r.steam_id===A),{steam_id:A,damageDealt:null,damageTaken:10});
  assert.deepEqual(stats.find(r=>r.steam_id===''),{steam_id:'',damageDealt:null,damageTaken:20});
  assert.deepEqual(stats.find(r=>r.steam_id===B),{steam_id:B,damageDealt:null,damageTaken:null});
  const complete=combat.createState();combat.ingest(complete,coverage(1),context());combat.ingest(complete,coverage(2,1),context());
  assert.deepEqual(combat.summary(complete,A,1).playerStats,[{steam_id:B,damageDealt:0,damageTaken:0},{steam_id:C,damageDealt:0,damageTaken:0}]);
  assert.deepEqual(combat.summary(null,A,1).playerStats,[]);
});

test('unresolved damage attribution disables rating comparison but not later direct evidence', () => {
  const s=combat.createState();combat.ingest(s,coverage(1),context());
  const unknown=row({seq:2,b:C,bt:1});delete unknown.a;delete unknown.at;
  combat.ingest(s,unknown,context());
  const direct=ingest(s,{seq:3,n:2});assert.equal(direct.incident.sanctionEligible,true);
  combat.ingest(s,coverage(4,1),context());
  for(const id of [A,B,C])assert.equal(combat.summary(s,id,2).ratingsEligible,false);
  assert.equal(combat.summary(s,C,2).damageTaken,100);
});

test('a persistent weapon causer cannot merge separate attacks for the whole round', () => {
  const s=combat.createState();
  ingest(s,{seq:1,t:20,new:70,causer:'rifle-1'});
  ingest(s,{seq:2,t:40,old:70,new:40,causer:'rifle-1'});
  ingest(s,{seq:3,t:60,old:40,new:10,causer:'rifle-1'});
  assert.equal(s.incidents.length,3);
  assert.equal(new Set(s.incidents.map(i=>i.id)).size,3);
});

test('exports the bounded evidence reducer contract', () => {
  for (const name of ['createState','parseRow','ingest','summary','evaluate']) assert.equal(typeof combat[name], 'function', name);
});
test('strict parser rejects malformed, oversized, duplicate and non-finite fields without fabricating zero', () => {
  assert.equal(combat.parseRow(wire(row())).new, 0);
  assert.equal(combat.parseRow(wire(row())).weapon, undefined);
  for (const bad of ['v=1;v=1',wire(row({seq:NaN})),wire(row({old:Infinity})),wire(row({n:-1})),wire(row({old:101})),wire(row({a:'123'})),wire(row())+';new=2',wire(row())+';unknown=1','x'.repeat(4097)]) assert.equal(combat.parseRow(bad),null,bad.slice(0,80));
  assert.equal(combat.parseRow(wire(row({distance:'NaN'}))),null);
});

test('lethal overkill preserves raw evidence but credits only remaining health', () => {
  let s=combat.createState();combat.ingest(s,coverage(1),context());
  const lethal=row({seq:2,old:30,new:-5});
  assert.equal(combat.parseRow(wire(lethal))?.new,-5);
  const result=combat.ingest(s,wire(lethal),context());
  assert.equal(result.accepted,true);
  assert.equal(result.incident.damage,30);
  assert.deepEqual(result.incident.lethalVictimIds,[B]);
  assert.equal(result.incident.evidence.lastHealth.new,-5);
  assert.equal(result.incident.sanctionEligible,true);
  s=JSON.parse(JSON.stringify(s));
  assert.equal(combat.ingest(s,wire(lethal),context()).duplicate,true);
  assert.equal(ingest(s,{seq:3,old:-5,new:-20,t:21}).accepted,true);
  assert.equal(ingest(s,{seq:4,old:-20,new:-20,t:22}).accepted,true);
  assert.equal(combat.summary(s,A,1).friendlyDamage,30);
  assert.equal(s.incidents.length,1);
  assert.equal(s.coverage.broken,false);
  assert.equal(ingest(s,{seq:2,old:30,new:-6}).reason,'sequence_conflict');
});

test('negative lethal health gives one assist and cannot bypass context or numeric checks', () => {
  const s=combat.createState();combat.ingest(s,coverage(1),context());
  ingest(s,{seq:2,b:C,bt:1,new:70});
  ingest(s,{seq:3,a:B,b:C,bt:1,old:70,new:-50,t:21});
  ingest(s,{seq:4,a:B,b:C,bt:1,old:-50,new:-60,t:22});
  assert.equal(combat.summary(s,A,1).enemyDamage,30);
  assert.equal(combat.summary(s,B,1).enemyDamage,70);
  assert.equal(combat.summary(s,A,1).assists,1);
  for(const fields of [{t:-1},{seq:-1},{max:-100},{distance:-1},{kind:'hit',new:-5},{new:-1e20}])
    assert.equal(combat.parseRow(wire(row(fields))),null);
  for(const fields of [{phase:'warmup'},{bt:1},{a:A,b:A}]) {
    const state=combat.createState();combat.ingest(state,coverage(1),context());
    ingest(state,{seq:2,new:-5,...fields},{phase:fields.phase||'combat'});
    assert.equal(state.incidents.length,0);
  }
});
test('untrusted and unvalidated collection cannot authorize damage or sanctions', () => {
  for (const ctx of [{authenticated:false},{validated:false}]) {
    const s=combat.createState(); const r=ingest(s,{},ctx);
    assert.equal(r.accepted,false); assert.equal(s.incidents.length,0);
    assert.equal(combat.summary(s,A,1).enemyDamage,null);
  }
});
test('duplicate lethal sequence survives serialization without adding damage or incidents', () => {
  let s=combat.createState(); assert.equal(ingest(s).accepted,true);
  s=JSON.parse(JSON.stringify(s)); const before=JSON.stringify(s);
  assert.equal(ingest(s).duplicate,true); assert.equal(JSON.stringify(s),before);
  assert.equal(s.incidents.length,1); assert.equal(combat.summary(s,A,1).friendlyDamage,100);
  assert.equal(ingest(s,{new:50}).reason,'sequence_conflict');
});
test('new old sequence is rejected and a gap permanently defeats complete coverage', () => {
  const s=combat.createState(); combat.ingest(s,coverage(1),context());
  ingest(s,{seq:3,b:C,bt:1}); assert.equal(ingest(s,{seq:2}).reason,'old_sequence');
  combat.ingest(s,coverage(4,1),context());
  assert.equal(combat.summary(s,A,1).ratingsEligible,false);
});
test('warmup, phase disagreement and team disagreement cannot add sanctionable incidents', () => {
  for (const [r,c] of [[{phase:'warmup'},{phase:'warmup'}],[{phase:'combat'},{phase:'warmup'}],[{bt:1},{}]]) {
    const s=combat.createState(); ingest(s,r,c); assert.equal(s.incidents.length,0); assert.equal(combat.summary(s,A,1).friendlyDamage,null);
  }
});
test('environmental source keeps damage taken but cannot become a guilty actor', () => {
  const s=combat.createState(); const e=row(); delete e.a; delete e.at;
  assert.equal(combat.ingest(s,e,context()).accepted,true); assert.equal(s.incidents.length,0);
  assert.equal(combat.summary(s,B,1).damageTaken,100);
});
test('coverage distinguishes unavailable, observed partial and complete real zero', () => {
  const s=combat.createState(); assert.equal(combat.summary(s,A,1).status,'unavailable');
  combat.ingest(s,coverage(1),context()); combat.ingest(s,coverage(2,1),context());
  const summary=combat.summary(s,A,2);
  assert.equal(summary.status,'complete'); assert.equal(summary.enemyDamage,0); assert.equal(summary.friendlyDamage,0);
  assert.equal(summary.adr,0); assert.equal(summary.shots,null); assert.equal(summary.accuracy,null); assert.equal(summary.ratingsEligible,true);
  assert.deepEqual(summary.coverage,{damage:true,shots:false,objectives:false});
  const partial=combat.createState(); ingest(partial,{b:C,bt:1,new:65});
  assert.equal(combat.summary(partial,A,1).status,'partial'); assert.equal(combat.summary(partial,A,1).enemyDamage,35);
});
test('closing claims alone, missing bindings, epoch changes or roster changes cannot complete coverage', () => {
  for (const mode of ['late','bindings','epoch','roster']) {
    const s=combat.createState();
    if (mode==='late') ingest(s,{seq:1});
    else combat.ingest(s,coverage(1,0,mode==='bindings'?{bound:2}:{}),context());
    combat.ingest(s,coverage(2,1,mode==='epoch'?{epoch:'epoch-2'}:{}),context(mode==='roster'?{roster:{[A]:0,[B]:0}}:{}));
    assert.equal(combat.summary(s,A,1).ratingsEligible,false,mode);
  }
});
test('same explosion victims and uninterrupted multi-hit burst count as one incident', () => {
  const s=combat.createState(); ingest(s,{attack:'grenade-1',causer:'grenade-1'});
  ingest(s,{seq:2,b:A,attack:'grenade-1',causer:'grenade-1',t:20.1});
  assert.equal(s.incidents.length,1); assert.equal(combat.evaluate(s.incidents,NOW).classification,'warning');
  const b=combat.createState(); ingest(b,{new:80}); ingest(b,{seq:2,old:80,new:60,t:20.5});
  assert.equal(b.incidents.length,1); assert.equal(combat.summary(b,A,1).friendlyDamage,40);
});
test('health credit cannot exceed actual health per victim round or replay identical transitions under new sequences', () => {
  const s=combat.createState(); ingest(s,{b:C,bt:1,old:100,new:50}); ingest(s,{seq:2,b:C,bt:1,old:100,new:50,t:21});
  assert.equal(combat.summary(s,A,1).enemyDamage,50);
  ingest(s,{seq:3,b:C,bt:1,old:50,new:0,t:22}); assert.equal(combat.summary(s,A,1).enemyDamage,100);
});
test('optional hit context associates in either delivery order without double counting damage', () => {
  for (const order of ['hit-first','health-first']) {
    const s=combat.createState(); const health=row({b:C,bt:1,attack:'a1',weapon:'rifle'}); const hit=row({kind:'hit',b:C,bt:1,attack:'a1',weapon:'rifle',bone:'head'});
    const events=order==='hit-first'?[hit,health]:[health,hit]; events.forEach((e,i)=>combat.ingest(s,{...e,seq:i+1},context()));
    const sum=combat.summary(s,A,1); assert.equal(sum.enemyDamage,100); assert.equal(sum.headshots,1); assert.equal(sum.hits,1); assert.equal(sum.weaponStats[0].enemyDamage,100);
  }
});
test('shots require an independently validated shot signal and accuracy is absent otherwise', () => {
  const s=combat.createState(); assert.equal(ingest(s,{kind:'shot'}).accepted,false);
  assert.equal(ingest(s,{kind:'shot'},{validatedShot:true}).accepted,true);
  assert.equal(combat.summary(s,A,1).shots,1); assert.equal(combat.summary(s,A,1).accuracy,null);
});
test('single accidental friendly kill and uncorroborated repetition never imply malicious intent', () => {
  assert.equal(combat.evaluate([history({damage:100})],NOW).classification,'warning');
  const h=[0,1,2].map(i=>history({id:`i${i}`,gameTime:20+i*20,startedAt:NOW+i*20000,endedAt:NOW+i*20000}));
  assert.equal(combat.evaluate(h,NOW+60000).classification,'warning');
});
test('repeated substantial nonlethal targeting across rounds is malicious with deterministic evidence IDs', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,round:i,damage:60,startedAt:NOW+i*1000,endedAt:NOW+i*1000}));
  const d=combat.evaluate(h,NOW+10000); assert.equal(d.classification,'malicious'); assert.equal(d.ruleVersion,1);
  assert.ok(d.reasons.includes('persistent_targeting')); assert.equal(d.decisionId,combat.evaluate([...h].reverse(),NOW+10000).decisionId);
  assert.equal(combat.evaluate([...h,...h],NOW+10000).decisionId,d.decisionId);
});
test('post-ack continuation corroborates independent incidents; missing ack cannot immunize large repeated harm', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,damage:60,gameTime:20+i*20,startedAt:NOW+i*20000,endedAt:NOW+i*20000,warningAckAt:NOW+10000}));
  assert.equal(combat.evaluate(h,NOW+100000).classification,'malicious');
  const large=[1,2,3,4].map(i=>history({id:`i${i}`,matchId:`m${i}`,damage:100,startedAt:NOW+i*1000,endedAt:NOW+i*1000,victimIds:[i%2?B:C]}));
  assert.equal(combat.evaluate(large,NOW+10000).classification,'malicious');
});
test('future, expired, unvalidated and decayed histories do not sustain automatic sanctions', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,round:i}));
  assert.equal(combat.evaluate(h,NOW+7*86400000).classification,'warning');
  assert.equal(combat.evaluate(h,NOW+31*86400000).classification,'insufficient');
  assert.equal(combat.evaluate(h,NOW-1000).classification,'insufficient');
  assert.equal(combat.evaluate(h.map(e=>({...e,validated:false})),NOW).classification,'insufficient');
});
test('mixed actors, same attack IDs and closely spaced attacks cannot manufacture repeated decisions', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,actorId:i===2?B:A,round:i})); assert.notEqual(combat.evaluate(h,NOW).classification,'malicious');
  const fast=[1,2,3,4].map(i=>history({id:`i${i}`,gameTime:i,damage:100,warningAckAt:NOW-1000})); assert.notEqual(combat.evaluate(fast,NOW).classification,'malicious');
});
test('serializable restored reducers deterministically reproduce statistics and incident decisions', () => {
  const s=combat.createState(); combat.ingest(s,coverage(1),context()); ingest(s,{seq:2,b:C,bt:1,new:60});
  const restored=JSON.parse(JSON.stringify(s));
  for (const target of [s,restored]) { ingest(target,{seq:3,b:C,bt:1,old:60,new:0,t:21}); combat.ingest(target,coverage(4,1),context()); }
  assert.deepEqual(restored,s); assert.deepEqual(combat.summary(restored,A,1),combat.summary(s,A,1));
});
test('consumed sanction evidence cannot authorize another sanction when one incident is appended', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,round:i,damage:60,sanctionId:'receipt-1'}));
  assert.equal(combat.evaluate([...h,history({id:'new',round:4})],NOW).classification,'warning');
  assert.equal(combat.evaluate(h.map(i=>({...i,sanctionId:undefined,sanctioned:true})),NOW).classification,'insufficient');
});
test('threshold boundaries require substantial individual harm and 150 current damage', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,round:i}));
  assert.equal(combat.evaluate(h,NOW).classification,'malicious');
  assert.equal(combat.evaluate(h.map((i,n)=>({...i,damage:n===0?49:50})),NOW).classification,'warning');
  assert.equal(combat.evaluate(h.map((i,n)=>({...i,damage:n===0?29:100})),NOW).classification,'warning');
});
test('one grenade harming two teammates stays one incident even with multiple lethal victims', () => {
  const s=combat.createState(),ctx={roster:{[A]:0,[B]:0,[C]:0}};
  ingest(s,{attack:'grenade'},ctx); ingest(s,{seq:2,b:C,bt:0,attack:'grenade'},ctx);
  assert.equal(s.incidents.length,1); assert.equal(s.incidents[0].victimIds.length,2);
  assert.equal(combat.evaluate(s.incidents,NOW).classification,'warning');
});
test('known accuracy is percent and one shot hitting multiple enemies cannot exceed 100 percent', () => {
  const s=combat.createState(); combat.ingest(s,coverage(1,0,{shots:1}),context());
  ingest(s,{seq:2,kind:'shot',attack:'s1',weapon:'rifle'},{validatedShot:true});
  ingest(s,{seq:3,kind:'shot',attack:'s2',weapon:'rifle'},{validatedShot:true});
  ingest(s,{seq:4,b:C,bt:1,attack:'s1',weapon:'rifle'});
  combat.ingest(s,coverage(5,1,{shots:1}),context());
  assert.equal(combat.summary(s,A,1).accuracy,50);
});
test('assists credit only substantial recent enemy damage before a teammate lethal health event', () => {
  const s=combat.createState(); ingest(s,{b:C,bt:1,new:75});
  ingest(s,{seq:2,a:B,at:0,b:C,bt:1,old:75,new:0,t:25});
  assert.equal(combat.summary(s,A,1).assists,1);
  const late=combat.createState(); ingest(late,{b:C,bt:1,new:75});
  ingest(late,{seq:2,a:B,at:0,b:C,bt:1,old:75,new:0,t:31});
  assert.equal(combat.summary(late,A,1).assists,null);
});
test('unvalidated data after a completed segment degrades coverage and object delimiter injection is rejected', () => {
  const s=combat.createState(); combat.ingest(s,coverage(1),context()); combat.ingest(s,coverage(2,1),context());
  ingest(s,{seq:3},{validated:false}); assert.equal(combat.summary(s,A,1).ratingsEligible,false);
  const other=combat.createState(); assert.equal(ingest(other,{weapon:'rifle;bone=head'}).accepted,false);
});
test('known body-hit zero differs from absent bone context, and one ambiguous nearby context cannot label two losses', () => {
  const body=combat.createState(); ingest(body,{b:C,bt:1,bone:'spine'});
  assert.equal(combat.summary(body,A,1).headshots,0);
  const unknown=combat.createState(); ingest(unknown,{b:C,bt:1,bone:'unknown'});
  assert.equal(combat.summary(unknown,A,1).headshots,null);
  const burst=combat.createState(); ingest(burst,{b:C,bt:1,new:90});
  ingest(burst,{seq:2,b:C,bt:1,old:90,new:80,t:20.1});
  ingest(burst,{seq:3,kind:'hit',b:C,bt:1,bone:'head',t:20.05});
  assert.equal(combat.summary(burst,A,1).headshots,null);
});
test('repeated real shot identifiers are one shot and per-weapon hit counts group pellets consistently', () => {
  const s=combat.createState();
  ingest(s,{kind:'shot',attack:'s1',weapon:'rifle'},{validatedShot:true});
  ingest(s,{seq:2,kind:'shot',attack:'s1',weapon:'rifle'},{validatedShot:true});
  ingest(s,{seq:3,b:C,bt:1,new:90,bone:'head',attack:'s1',weapon:'rifle'});
  ingest(s,{seq:4,b:C,bt:1,old:90,new:80,bone:'head',attack:'s1',weapon:'rifle'});
  const sum=combat.summary(s,A,1);assert.equal(sum.shots,1);assert.equal(sum.hits,1);assert.equal(sum.headshots,1);
  assert.equal(sum.weaponStats[0].hits,1);assert.equal(sum.weaponStats[0].headshots,1);
});
test('matching warmup observations are excluded without breaking later complete combat coverage', () => {
  const s=combat.createState(); combat.ingest(s,coverage(1),context());
  ingest(s,{seq:2,phase:'Game.Phase.RoundWarmup'},{phase:'Game.Phase.RoundWarmup'});
  ingest(s,{seq:3,b:C,bt:1,phase:'Game.Phase.StartRound'},{phase:'Game.Phase.StartRound'});
  combat.ingest(s,coverage(4,1),context());
  assert.equal(s.incidents.length,0);assert.equal(combat.summary(s,A,1).enemyDamage,100);
  assert.equal(combat.summary(s,A,1).ratingsEligible,true);
});
test('same-round health replenishment cannot claim complete life-aware damage coverage', () => {
  const s=combat.createState(); combat.ingest(s,coverage(1),context());
  ingest(s,{seq:2,b:C,bt:1,new:50});ingest(s,{seq:3,b:C,bt:1,old:50,new:100});
  ingest(s,{seq:4,b:C,bt:1,old:100,new:0});combat.ingest(s,coverage(5,1),context());
  assert.equal(combat.summary(s,A,1).enemyDamage,100);assert.equal(combat.summary(s,A,1).ratingsEligible,false);
});
test('explicit observation-only authenticated collection displays provisional totals without sanctions or rating eligibility', () => {
  const s=combat.createState(),ctx={validated:false,observationOnly:true};
  for(let n=1;n<=3;n++) assert.equal(ingest(s,{seq:n,n},ctx).accepted,true);
  const sum=combat.summary(s,A,3);assert.equal(sum.status,'partial');assert.equal(sum.friendlyDamage,300);
  assert.equal(sum.ratingsEligible,false);assert.equal(combat.evaluate(s.incidents,NOW).classification,'insufficient');
  assert.ok(s.incidents.every(i=>i.validated===false&&i.sanctionEligible===false));
  const unauthenticated=combat.createState();assert.equal(ingest(unauthenticated,{}, {...ctx,authenticated:false}).accepted,false);
});
test('durable incidents retain bounded first/last actual health context for independent review', () => {
  const s=combat.createState();ingest(s,{new:80,weapon:'rifle',causer:'gun-1',bone:'spine'});
  ingest(s,{seq:2,old:80,new:50,t:20.5,weapon:'rifle',causer:'gun-1',bone:'head'});
  const restored=JSON.parse(JSON.stringify(s.incidents[0]));
  assert.equal(restored.evidence.phase,'combat');assert.equal(restored.evidence.firstHealth.seq,1);
  assert.equal(restored.evidence.firstHealth.old,100);assert.equal(restored.evidence.firstHealth.new,80);
  assert.equal(restored.evidence.lastHealth.seq,2);assert.equal(restored.evidence.lastHealth.new,50);
  assert.equal(restored.evidence.lastHealth.at,0);assert.equal(restored.evidence.lastHealth.bt,0);
  assert.equal(restored.evidence.firstHealth.weapon,'rifle');assert.equal(restored.evidence.lastHealth.causer,'gun-1');
});
test('gapped burst evidence cannot manufacture later independently sanctionable incidents', () => {
  const s=combat.createState();ingest(s,{new:40});
  ingest(s,{seq:3,n:2,new:40});ingest(s,{seq:4,n:3,new:40});
  assert.equal(s.incidents[0].sanctionEligible,true);assert.equal(s.incidents[1].sanctionEligible,false);
  assert.equal(s.incidents[2].sanctionEligible,false);assert.equal(combat.evaluate(s.incidents,NOW).classification,'warning');
});
test('a conflicting health transition retains partial observed loss but cannot strengthen a sanction incident', () => {
  const s=combat.createState();ingest(s,{new:60});ingest(s,{seq:2,old:100,new:0,t:21});
  assert.equal(combat.summary(s,A,1).friendlyDamage,100);assert.equal(s.incidents.length,1);
  assert.equal(s.incidents[0].sanctionEligible,false);assert.equal(combat.evaluate(s.incidents,NOW).classification,'insufficient');
});
test('expired copied acknowledgements cannot corroborate new independent harm', () => {
  const h=[1,2,3].map(i=>history({id:`i${i}`,damage:60,gameTime:i*20,warningAckAt:NOW-31*86400000}));
  assert.equal(combat.evaluate(h,NOW).classification,'warning');
  assert.equal(combat.evaluate(h.map(i=>({...i,warningAckAt:NOW-86400000})),NOW).classification,'malicious');
});
