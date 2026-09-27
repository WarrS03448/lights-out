'use strict';
// Run: node --test scripts/test-recovery-http.cjs
const {test}=require('node:test'),assert=require('node:assert/strict'),server=require('../server.cjs');
test('game recovery route authenticates versioned reports, awaits storage and returns no private state',async t=>{
  const seen=[],token='ab'.repeat(32),match='0123456789abcdef';let failure=false;
  const service={_internals:{ready:Promise.resolve(),ensureRecovery:async()=>0},
    recoveryReport:async(cap,fields)=>{seen.push({cap,fields});if(failure)throw Error('offline');
      return {ok:true,snapshot:{reportToken:'secret'},match:{migration_token:'secret'},recovery:{private:'secret'}};}};
  const listener=server.createServer({liveService:service});await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>listener.close(resolve)));
  const post=(body,auth=token+'.2')=>fetch(`http://127.0.0.1:${listener.address().port}/api/match-report/recovery`,{
    method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+auth},body:JSON.stringify(body)});
  const body={event_name:'ch_session_pulse',first_session_timestamp:'chrecovery-1',user_id:'76561198000000001',
    storefront:'chm-'+match+'-r1111111111111111',timestamp:'17',now:1,operation:'claim'};
  assert.equal((await post(body,token)).status,401);
  assert.equal((await post({...body,first_session_timestamp:'other'})).status,409);
  assert.equal((await post({...body,timestamp:'0'})).status,409);
  const reply=await post(body);assert.equal(reply.status,200);assert.deepEqual(await reply.json(),{ok:true});
  assert.deepEqual(seen,[{cap:token,fields:{operation:'pulse',match_id:match,epoch:2,session:body.storefront,user_id:body.user_id,sequence:17}}]);
  failure=true;assert.equal((await post(body)).status,503);
});

// Live 2026-09-27: the hub's route was built and unit-tested but never listed in NEEDS_AUTH, so
// server.cjs answered 404 before live.cjs saw it and no cold host could ever claim a recovery.
test('the hub recovery route reaches the live router instead of a 404',async t=>{
  const routed=[];
  const service={_internals:{ready:Promise.resolve(),ensureRecovery:async()=>0},
    route:async(req,res,method,pathname)=>{routed.push([method,pathname]);
      res.writeHead(200,{'content-type':'application/json'});res.end('{"ok":true}');return true;}};
  const listener=server.createServer({liveService:service});await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>listener.close(resolve)));
  const reply=await fetch(`http://127.0.0.1:${listener.address().port}/api/match/recovery`,{method:'POST',
    headers:{'content-type':'application/json',authorization:'Bearer x'},body:JSON.stringify({match_id:'0123456789abcdef',operation:'status'})});
  assert.equal(reply.status,200);
  assert.deepEqual(routed,[['POST','/api/match/recovery']]);
});

test('every path live.cjs handles is one server.cjs hands to it',()=>{
  const live=require('../live.cjs'),source=require('node:fs').readFileSync(require.resolve('../live.cjs'),'utf8');
  const handled=[...new Set([...source.matchAll(/pathname *=== *'(\/api\/[^']+)'/g)].map(m=>m[1]))];
  assert.ok(handled.includes('/api/match/recovery'),'the scan must see the route it guards');
  assert.deepEqual(handled.filter(path=>!live.owns(path)),[],'a handled route missing from NEEDS_AUTH answers 404');
});
