'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const V=require('../valuation.cjs');
const ids=['a','b','c','d'];
const match={teams:{1:['a','b'],2:['c','d']},left:[],score:{1:7,2:3},score_limit:7,mm:{quality:1}};
const ratings=Object.fromEntries(ids.map(id=>[id,{rating:1500,rd:60,matches:30}]));
const complete=(damage,assists)=>({version:1,status:'complete',coverage:{damage:true,shots:false,objectives:false},ratingsEligible:true,enemyDamage:damage,assists});
function stats(combat=true) { return {rounds:10,limit:7,players:ids.map((steamId,i)=>({steamId,roundsPlayed:10,deaths:[1,9,2,8][i],score:[100,10,90,20][i],...(combat?{combat:complete([100,900,200,800][i],[1,9,2,8][i])}:{})}))}; }
const value=s=>V.valuation(match,s,ratings,1);

test('combat rating rollout can remain off independently of validated damage collection',t=>{
  const old=process.env.COMP_COMBAT_RATINGS_ENABLED;
  process.env.COMP_COMBAT_RATINGS_ENABLED='0';
  t.after(()=>old===undefined?delete process.env.COMP_COMBAT_RATINGS_ENABLED:process.env.COMP_COMBAT_RATINGS_ENABLED=old);
  assert.deepEqual(value(stats()),value(stats(false)));
});

test('complete comparable combat modestly rewards relative enemy damage and assists inside the existing band',()=>{
  const baseline=value(stats(false)),actual=value(stats());
  const before=baseline.find(r=>r.steamId==='b'),after=actual.find(r=>r.steamId==='b');
  assert.ok(after.breakdown.actual>before.breakdown.actual);
  assert.ok(Math.abs(after.breakdown.actual-before.breakdown.actual)<=0.15+Number.EPSILON);
  for(const r of actual) assert.ok(r.won?r.score>=1-V.PERF_BAND&&r.score<=1:r.score>=0&&r.score<=V.PERF_BAND);
  assert.equal(after.breakdown.combat.damagePerRound,90);assert.equal(after.breakdown.combat.assistsPerRound,0.9);
  assert.equal(after.breakdown.combat.components.damage.weight,0.10);assert.equal(after.breakdown.combat.components.assists.weight,0.05);
  assert.equal(after.breakdown.combat.components.damage.teamPercentile,1);
});
test('any missing, partial, provisional, malformed or incomparable roster summary exactly preserves the legacy result',()=>{
  for(const broken of [undefined,{status:'partial'},{ratingsEligible:false},{coverage:{damage:false}},
    {enemyDamage:null},{enemyDamage:Infinity},{enemyDamage:'900'},{enemyDamage:-1},{assists:null},{assists:NaN},{assists:-1},{version:2}]) {
    const s=stats(); s.players[3].combat=broken===undefined?undefined:{...s.players[3].combat,...broken};
    assert.deepEqual(value(s),value(stats(false)),JSON.stringify(broken));
  }
});
test('one incomplete opposing player disables combat influence even for a completely observed team',()=>{
  const s=stats();s.players[3].combat.status='partial';
  assert.deepEqual(value(s),value(stats(false)));
});
test('friendly damage, headshots, accuracy, shots and weapon output never earn valuation credit',()=>{
  const s=stats(),base=value(s);
  for(const [i,p] of s.players.entries()) Object.assign(p.combat,{friendlyDamage:i*100000,damageTaken:i*100,headshots:i*1000,shots:i*50000,hits:i*50000,accuracy:i*33,weaponStats:[{weapon:'rifle',friendlyDamage:10000}]});
  assert.deepEqual(value(s),base);
});
test('per-round combat performance is invariant when match length and counts scale together',()=>{
  const a=stats(),b=stats();
  for(const p of b.players) {p.roundsPlayed*=2;p.deaths*=2;p.score*=2;p.combat.enemyDamage*=2;p.combat.assists*=2;}
  b.rounds=20;
  const first=value(a),second=value(b);
  first.forEach((r,i)=>{assert.equal(r.score,second[i].score);assert.equal(r.breakdown.actual,second[i].breakdown.actual);assert.equal(r.breakdown.combat.damagePerRound,second[i].breakdown.combat.damagePerRound);});
});
test('complete zero/tied combat data neither dilutes legacy components nor invents measured performance',()=>{
  const s=stats();for(const p of s.players)p.combat=complete(0,0);
  const expected=value(stats(false)),actual=value(s);
  actual.forEach((r,i)=>{assert.equal(r.score,expected[i].score);assert.equal(r.breakdown.actual,expected[i].breakdown.actual);assert.equal(r.breakdown.combat.components.damage.teamPercentile,null);});
  const noLegacy={rounds:10,players:ids.map(steamId=>({steamId,combat:complete(0,0)}))};
  for(const r of value(noLegacy)) {assert.equal(r.breakdown.measured,false);assert.equal(r.score,r.won?1:0);}
});
test('combat-only evidence retains the modest fixed contribution cap and serializable audit breakdown',()=>{
  const s=stats();s.players=s.players.map(p=>({steamId:p.steamId,roundsPlayed:10,combat:p.combat}));
  for(const r of value(s)) {
    assert.ok(r.breakdown.actual>=0.425&&r.breakdown.actual<=0.575);
    assert.equal(JSON.parse(JSON.stringify(r)).breakdown.combat.eligible,true);
  }
});
