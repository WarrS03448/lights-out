// Run: node --test server/scripts/test-moderation-adapters.cjs
'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
test('public review adapters report unavailable instead of a clean score',()=>{
 const review=require('../fair-play.cjs').inspect({});
 assert.equal(review.available,false);assert.deepEqual(review.alerts,[]);assert.equal(review.enforcement,'none');
 const score=require('../suspicion.cjs').score({player:'76561198000000001'});
 assert.equal(score.available,false);assert.equal(score.status,null);assert.equal(score.score,null);assert.deepEqual(score.contributions,[]);
});
test('hosted correction adapter refuses mutations and leaves ordinary projections available',async()=>{
 const service=require('../cheater-restitution.cjs').create();
 await assert.rejects(service.begin(),/unavailable/);await assert.rejects(service.correct(),/unavailable/);await assert.rejects(service.statuses(),/unavailable/);
 assert.equal(await service.project({}),null);await service.run();
 assert.deepEqual(await service.annotate([{id:'match-one',delta:25}]),[{id:'match-one',delta:25}]);
});
test('public correction annotations expose only public result fields',async()=>{
 const service=require('../cheater-restitution.cjs').create({store:async()=>JSON.stringify({at:100,cheaters:['76561198000000001'],private_audit:'must not leak'})});
 const [row]=await service.annotate([{id:'match-one',delta:25}]);
 assert.equal(row.cheater_reverted,true);assert.equal(row.delta,0);assert.equal(row.rr_delta,0);assert.equal(row.private_audit,undefined);
});
