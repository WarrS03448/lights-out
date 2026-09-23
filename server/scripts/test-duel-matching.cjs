'use strict';
process.env.NODE_ENV='test';process.env.COMP_NETWORK_TEST_BYPASS='1';
const {test}=require('node:test'),assert=require('node:assert/strict');
const mm=require('../matchmaker.cjs'),duel=require('../duel-matching.cjs');
test('repeat preference chooses a fresh comparable opponent but never blocks a small queue',()=>{
 const now=Date.now();const unit=id=>({key:id,members:[id],ratings:[{rating:1500}],joined:now-1000,recent:[]});
 const a=unit('a'),b=unit('b'),c=unit('c');a.joined=now-2000;
 a.recent=[{ended:now-10000,outcome:'played',opponents:['b'],won:false}];
 const options={now,matchSize:2,teamSize:1,repeatCost:duel.cost};
 assert.deepEqual(mm.findMatch([a,b,c],options).units.map(u=>u.key).sort(),['a','c']);
 assert(mm.findMatch([a,b],options));
 assert.equal(duel.cost([{...a,joined:now-301000},{...b,joined:now-301000}],now),0);
});
