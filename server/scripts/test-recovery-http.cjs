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
