'use strict';
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const readline=require('node:readline');
const path=require('node:path');
const lib=require('../abandon-penalty.cjs');
let bridge,store;
before(async()=>{
  bridge=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')],{stdio:['pipe','pipe','pipe']});
  let n=0;const pending=new Map();
  readline.createInterface({input:bridge.stdout}).on('line',line=>{
    const r=JSON.parse(line),p=pending.get(r.id);pending.delete(r.id);
    if(p)r.error?p.reject(Error(r.error)):p.resolve(r.result);
  });
  bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('test store stopped'));pending.clear();});
  store=command=>new Promise((resolve,reject)=>{const id=++n;pending.set(id,{resolve,reject});bridge.stdin.write(JSON.stringify({id,command})+'\n');});
  assert.equal(await store(['PING']),'PONG');
});
after(()=>bridge?.kill());
function payload() {
  const penaltyJson=JSON.stringify({until:123456,last:1000,count:1,elo:25,reason:'reconnect_timeout'});
  return {expectedRank:0,expectedPenalty:'',rankJson:JSON.stringify({revision:1,progress:75}),penaltyJson,
    guardJson:JSON.stringify({operationId:'test',json:penaltyJson}),placing:false,progress:75,ttl:14*86400,
    receiptJson:JSON.stringify({player_id:'p',reason:'reconnect_timeout',rr:25,rank:{revision:1,progress:75}})};
}
test('real Lua commits once after a lost reply and cannot overwrite newer progress',async()=>{
  const first=await lib.commit(store,'t:','m','p',payload());assert.equal(first.replayed,false);
  await store(['SET','t:rating:p',JSON.stringify({revision:2,progress:120})]);
  const again=await lib.commit(store,'t:','m','p',payload());assert.equal(again.replayed,true);
  assert.equal(JSON.parse(await store(['GET','t:rating:p'])).progress,120);
  assert.equal(JSON.parse(await store(['GET','t:penalty:p'])).count,1);
});
test('rank conflicts and invalid key types cause no partial writes',async()=>{
  await store(['SET','c:rating:p',JSON.stringify({revision:3,progress:100})]);
  await assert.rejects(lib.commit(store,'c:','m','p',payload()));
  assert.equal(await store(['GET','c:penalty:p']),null);
  await store(['SADD','bad:leaderboard:rr','x']);
  await assert.rejects(lib.commit(store,'bad:','m','p',payload()));
  assert.equal(await store(['GET','bad:rating:p']),null);
});
test('a durable result wins the race against a later reconnect penalty',async()=>{
  await store(['SET','finished:settlement:m','{"data_collected":true}']);
  await assert.rejects(lib.commit(store,'finished:','m','p',payload()));
  assert.equal(await store(['GET','finished:penalty:p']),null);
  assert.equal(await store(['GET','finished:rating:p']),null);
});

test('a cancelled host epoch cannot charge a pending reconnect penalty',async()=>{
  const prefix='closed:',host='76561198000000001';
  await store(['SET',prefix+'live:authority:m',JSON.stringify({host,epoch:0,last_seen:1})]);
  assert.equal(await store(['EVAL',require('../migration.cjs').CLOSE,'2',prefix+'live:authority:m',prefix+'settlement:m',host,'0','400000','0','86400']),1);
  await assert.rejects(lib.commit(store,prefix,'m','p',{...payload(),host,host_epoch:0}));
  for(const key of ['rating:p','penalty:p','reconnect:penalty:m:p'])assert.equal(await store(['GET',prefix+key]),null);
});
