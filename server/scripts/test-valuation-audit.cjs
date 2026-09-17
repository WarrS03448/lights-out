const test=require('node:test'),assert=require('node:assert/strict');
const V=require('../valuation.cjs');
function fixture(){
  const ids=['a','b','x','y'];
  const stats={round_index_base:0,rounds:7,players:ids.map(steamId=>({steamId})),series:{}};
  for(const id of ids)stats.series[id]=Object.fromEntries(Array.from({length:7},(_,i)=>[i,{deaths:['b','y'].includes(id)?i+1:0}]));
  const match={teams:{1:['a','b'],2:['x','y']},players:ids.map(steam_id=>({steam_id})),score:{1:7,2:0},team_map:{0:1,1:2},
    round_reports:Object.fromEntries(Array.from({length:7},(_,i)=>[i,{winTeam:0,source:'delegate'}])),round_results:Object.fromEntries(Array.from({length:7},(_,i)=>[i,1]))};
  return {ids,stats,match,side:id=>['a','b'].includes(id)?1:2};
}
test('zero-based first round and frozen side outcomes measure every played round',()=>{
  const {stats,match,side}=fixture();
  const deltas=V.roundDeltas(stats);
  assert.equal(deltas.b.length,7);assert.equal(deltas.b[0].deaths,1);assert.equal(deltas.b[0].from,-1);
  const expected=V.roundContext(match,stats,side);
  assert.deepEqual(expected.a,{roundsWon:7,roundsCounted:7});
  assert.deepEqual(expected.b,{roundsWon:0,roundsCounted:7});
  match.team_map={0:2,1:1};assert.deepEqual(V.roundContext(match,stats,side),expected);
  delete match.team_map;assert.deepEqual(V.roundContext(match,stats,side),expected);
});
test('gaps and resets remain unknown while legacy warmup stays excluded',()=>{
  for(const change of [s=>delete s.series.b[0],s=>delete s.series.b[3],s=>s.series.b[3].deaths=0]){
    const {stats,match,side}=fixture();change(stats);
    assert.equal(V.roundContext(match,stats,side).b?.roundsWon,undefined);
  }
  const legacy={series:{b:{0:{deaths:99},1:{deaths:1},2:{deaths:2}}}};
  assert.deepEqual(V.roundDeltas(legacy).b.map(r=>r.deaths),[1,1]);
});
test('final native sample uses total minus one and ignores post-end samples',()=>{
  const {stats,match,ids}=fixture();
  const ratings=Object.fromEntries(ids.map(id=>[id,{rating:1500,rd:100,matches:30}]));
  const before=V.valuation(match,stats,ratings,1);
  assert.ok(before.every(r=>r.breakdown.measured));
  for(const id of ids)stats.series[id][7]={deaths:500};
  assert.deepEqual(V.valuation(match,stats,ratings,1),before);
});
