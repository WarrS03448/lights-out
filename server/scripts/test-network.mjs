import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
const require = createRequire(import.meta.url);
delete process.env.COMP_NETWORK_TEST_BYPASS;
const net = require('../network.cjs');
const mm = require('../matchmaker.cjs');
const now = 1000000;
const limits = JSON.parse(execFileSync(process.execPath,['-e',
  `const n=require(${JSON.stringify(require.resolve('../network.cjs'))}); console.log(JSON.stringify([n.MAX_PING,n.TARGET_PING]));`],
  {env:{...process.env,COMP_MAX_PING_MS:'500',COMP_TARGET_PING_MS:'200'},encoding:'utf8'}));
assert.deepEqual(limits,[120,80],'environment cannot silently open approved limits');
function player(id, region = 'NA', cross_region = false) {
  return { id, region, cross_region, transport:'webrtc-relay-v1',
    location: Buffer.from(id).toString('hex').padEnd(32,'0').slice(0,32),
    revision: id, sampled: now, pings: {} };
}
function link(a, b, ms, reverse = ms) {
  a.pings[b.id] = { ms, at: now, localRevision: a.revision, remoteRevision: b.revision };
  b.pings[a.id] = { ms: reverse, at: now, localRevision: b.revision, remoteRevision: a.revision };
}
const a = player('a'), b = player('b'), c = player('c'), d = player('d');
link(a,b,10); link(a,c,10); link(a,d,100);
link(b,c,70); link(b,d,70); link(c,d,70);
assert.equal(net.selectHost([a,b,c,d], now).host, 'a', 'lowest average beats better worst ping');
assert.equal(net.selectHost([a,b,c,d], now).average, 40);
assert.equal(net.selectHost([a,b], now + 61000), null, 'stale reports never pass');
assert.equal(net.selectHost([a,player('x')], now), null, 'unknown is not zero');
link(a,b,10,121);
assert.equal(net.selectHost([a,b], now), null, 'both directions must pass');
link(a,b,120);
assert.ok(net.selectHost([a,b], now), 'ceiling inclusive');
b.region = 'EU';
assert.equal(net.selectHost([a,b], now), null);
a.cross_region = true; b.cross_region = true;
assert.ok(net.selectHost([a,b], now));
b.cross_region = false;
assert.equal(net.selectHost([a,b], now), null, 'unanimous opt-in required');
const u = (p, joined) => ({key:p.id, joined, ratings:[{rating:1500,rd:60}], network:[p]});
const testPlayers = ['test-a','test-b','test-c'].map(id => player(id));
link(testPlayers[0],testPlayers[1],30);
link(testPlayers[0],testPlayers[2],40);
link(testPlayers[1],testPlayers[2],50);
const threeTesters = testPlayers.map((p,i) => u(p,i+1));
assert.equal(mm.findMatch(threeTesters.slice(0,2),{now,matchSize:3}),null);
const threeMatch = mm.findMatch(threeTesters,{now,matchSize:3});
assert.ok(threeMatch,'three network-ready testers form a match');
assert.equal(threeMatch.teams[1].reduce((n,t)=>n+t.size,0),1);
assert.equal(threeMatch.teams[2].reduce((n,t)=>n+t.size,0),2);
const stranded = player('stranded','NA');
const e = player('e','EU'), f = player('f','EU'); link(e,f,40);
const match = mm.findMatch([u(stranded,1),u(e,2),u(f,3)], {now,matchSize:2,teamSize:1});
assert.deepEqual(match.units.map(x=>x.key).sort(), ['e','f'], 'another region bypasses stranded anchor');
assert.equal(match.network.host, 'e');
stranded.region='EU';
assert.deepEqual(mm.findMatch([u(stranded,1),u(e,2),u(f,3)],{now,matchSize:2,teamSize:1}).units.map(x=>x.key).sort(),
  ['e','f'],'unmeasured anchor cannot starve measured neighbors in same region');
stranded.cross_region=true; e.cross_region=true; f.cross_region=true; stranded.region='NA'; f.region='NA';
assert.deepEqual(mm.findMatch([u(stranded,1),u(e,2),u(f,3)],{now,matchSize:2,teamSize:1}).units.map(x=>x.key).sort(),
  ['e','f'],'unmeasured anchor cannot starve opted-in cross-region pool');
stranded.cross_region=false; e.cross_region=false; f.cross_region=false; f.region='EU';
assert.equal(mm.findMatch([u(stranded,1),u(e,2)], {now,matchSize:2,teamSize:1}), null,
  'time never opens region boundary');
const party = {key:'mixed',joined:1,ratings:[{rating:1500},{rating:1500}],network:[a,b]};
assert.equal(mm.findMatch([party],{now,matchSize:2,teamSize:1}),null,'party never split');
const g = player('g'), h = player('h'), i = player('i');
link(g,h,90); link(g,i,70); link(h,i,70);
const preference = mm.findMatch([u(g,1),u(h,2),u(i,3)],{now,matchSize:2,teamSize:1});
assert.deepEqual(preference.units.map(x=>x.key).sort(),['g','i'],'prefer target RTT when choosing a roster');
g.cross_region=true; h.cross_region=true;
const j = player('j','EU',true); link(g,j,1); link(h,j,1);
const localFirst = mm.findMatch([u(g,1),u(h,2),u(j,3)],{now,matchSize:2,teamSize:1});
assert.equal(localFirst.network.cross_region,false,'local game takes priority even for opted-in players');
assert.equal(net.selectHost([{...g,sampled:now-120001},h],now),null,'old profiles fail independently of reports');
const mixedPlayers = [player('m1','NA',true),player('m2','EU',true),player('m3','EU',true),player('m4','EU',true)];
for (let x=0;x<4;x++) for(let y=x+1;y<4;y++) link(mixedPlayers[x],mixedPlayers[y],50);
const mixedUnit = {key:'mixed-good',joined:1,ratings:[{rating:1500},{rating:1500}],network:mixedPlayers.slice(0,2)};
const mixedMatch = mm.findMatch([mixedUnit,u(mixedPlayers[2],2),u(mixedPlayers[3],3)],{now,matchSize:4,teamSize:2});
assert.ok(mixedMatch);
assert.ok(Object.values(mixedMatch.teams).some(team=>team.length===1 && team[0].key==='mixed-good'));
mixedPlayers[3].cross_region=false;
assert.equal(mm.findMatch([mixedUnit,u(mixedPlayers[2],2),u(mixedPlayers[3],3)],{now,matchSize:4,teamSize:2}),null);
const registry = new net.Registry();
assert.throws(()=>registry.profile('a',{region:'?',location:'marker',age_seconds:0},now));
const markerA = 'a'.repeat(32), markerB = 'b'.repeat(32);
const relayProfile = (location, age_seconds=0) => ({region:'NA',location,age_seconds,
  transport:'webrtc-relay-v1'});
assert.throws(()=>registry.profile('a',relayProfile('legacy-steam-marker'),now));
assert.throws(()=>registry.profile('a',relayProfile(markerA,-1),now));
assert.throws(()=>registry.profile('a',relayProfile(markerA,121),now));
assert.throws(()=>registry.profile('a',{...relayProfile(markerA),transport:'steam'},now));
const ra = registry.profile('a',relayProfile(markerA),now);
const rb = registry.profile('b',relayProfile(markerB),now);
const attemptAB = '1'.repeat(32);
registry.authorizeAttempt('a','b',ra.revision,rb.revision,attemptAB,now);
assert.throws(()=>registry.report('a',{revision:ra.revision,peers:[{steam_id:'b',revision:rb.revision,
  ping:30,transport:'webrtc-relay-v1',attempt:attemptAB,samples:5,age_seconds:2}]},now),
  /authorized/,'an unanswered offer is not measurement evidence');
registry.attempt(attemptAB,now).answered=true;
registry.report('a',{revision:ra.revision,peers:[{steam_id:'b',revision:rb.revision,ping:30,
  transport:'webrtc-relay-v1',attempt:attemptAB,samples:5,age_seconds:2}]},now);
registry.report('b',{revision:rb.revision,peers:[{steam_id:'a',revision:ra.revision,ping:40,
  transport:'webrtc-relay-v1',attempt:attemptAB,samples:6,age_seconds:1}]},now);
assert.equal(net.selectHost([registry.player('a'),registry.player('b')],now).average,40);
assert.equal(registry.player('a').pings.b.at,now-2000,'measurement age is preserved');
registry.profile('b',relayProfile('c'.repeat(32)),now+1);
assert.equal(net.selectHost([registry.player('a'),registry.player('b')],now+1),null);
assert.throws(()=>registry.report('a',{revision:ra.revision,peers:[{steam_id:'b',revision:rb.revision,
  ping:null,transport:'webrtc-relay-v1',attempt:attemptAB,samples:5,age_seconds:0}]},now));
assert.throws(()=>registry.report('a',{revision:ra.revision,peers:[{steam_id:'b',revision:rb.revision,
  ping:10,transport:'webrtc-relay-v1',attempt:'3'.repeat(32),samples:5,age_seconds:0}]},now));
assert.throws(()=>registry.report('a',{revision:ra.revision,peers:[{steam_id:'b',revision:rb.revision,
  ping:10,transport:'webrtc-relay-v1',attempt:attemptAB,samples:4,age_seconds:0}]},now));
assert.equal(registry.attempt(attemptAB,now + net.ATTEMPT_TTL),null,'attempt expires at its deadline');

const atomic = new net.Registry();
const aa = atomic.profile('a',relayProfile('1'.repeat(32)),now);
const ab = atomic.profile('b',relayProfile('2'.repeat(32)),now);
const ac = atomic.profile('c',relayProfile('3'.repeat(32)),now);
const validAttempt = '4'.repeat(32), invalidAttempt = '5'.repeat(32);
atomic.authorizeAttempt('a','b',aa.revision,ab.revision,validAttempt,now).answered=true;
atomic.authorizeAttempt('a','c',aa.revision,ac.revision,invalidAttempt,now).answered=true;
assert.throws(()=>atomic.report('a',{revision:aa.revision,peers:[
  {steam_id:'b',revision:ab.revision,ping:30,transport:'webrtc-relay-v1',
    attempt:validAttempt,samples:5,age_seconds:0},
  {steam_id:'c',revision:ac.revision,ping:40,transport:'webrtc-relay-v1',
    attempt:'6'.repeat(32),samples:5,age_seconds:0},
]},now),/authorized/);
assert.deepEqual(atomic.player('a').pings,{},'mixed-validity report commits no partial rows');
console.log('Network selection and profile validation tests passed');
