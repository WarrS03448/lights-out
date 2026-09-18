'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const server=require('../server.cjs');
test('presence accepts only authenticated match-bound explicit human snapshots',async t=>{
  const match='0123456789abcdef',host='76561198000000001',token='ab'.repeat(32),seen=[];
  const service={_internals:{ready:Promise.resolve(),ensureRecovery:async()=>0},
    authoriseReport:value=>value===token?{steamId:host,matchId:match}:null,
    matchPresence:async(...args)=>{seen.push(args);return {ok:true};}};
  const listener=server.createServer({liveService:service});
  await new Promise(resolve=>listener.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>listener.close(resolve)));
  const post=(body,bearer=token)=>fetch(`http://127.0.0.1:${listener.address().port}/api/match-report/start-ready`,{
    method:'POST',headers:{'content-type':'application/json',authorization:`Bearer ${bearer}`},body:JSON.stringify(body)});
  const good={event_name:'ch_match_presence',first_session_timestamp:'chpresence-1',storefront:'chm-'+match,platform:`1|${host};`,user_id:'76561198999999999'};
  assert.equal((await post(good,'invalid')).status,401);
  for(const change of [{platform:'0|'},{platform:`2|${host};`},{platform:`2|${host};${host};`},
    {platform:'1|;'},{storefront:'chm-fedcba9876543210'},{first_session_timestamp:'chstart-1'}])
    assert.equal((await post({...good,...change})).status,409);
  assert.equal(seen.length,0);
  assert.equal((await post(good)).status,200);
  assert.deepEqual(seen,[[host,match,[{steam_id:host,active:1}]]]);
});
