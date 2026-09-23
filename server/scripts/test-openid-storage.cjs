'use strict';
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const readline=require('node:readline');
const {spawn}=require('node:child_process');
const {makeStore}=require('../steam-openid.cjs');
let bridge,command;
before(async()=>{
  bridge=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')]);
  const pending=new Map();let sequence=0,errors='';
  bridge.stderr.on('data',chunk=>{errors+=chunk;});
  readline.createInterface({input:bridge.stdout}).on('line',line=>{
    const response=JSON.parse(line),p=pending.get(response.id);pending.delete(response.id);
    if(p)response.error?p.reject(Error(response.error)):p.resolve(response.result);
  });
  bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('Redis fixture exited: '+errors));pending.clear();});
  command=(args,options)=>new Promise((resolve,reject)=>{
    if(options)assert.equal(options.strict,true);
    const id=++sequence;pending.set(id,{resolve,reject});bridge.stdin.write(JSON.stringify({id,command:args})+'\n');
  });
  assert.equal(await command(['PING']),'PONG');
});
after(()=>bridge?.stdin.end());
test('Redis atomically claims one callback across replicas and prevents nonce reuse',async()=>{
  const a=makeStore(command,'openid:redis:'),b=makeStore(command,'openid:redis:');
  const pending={status:'pending',return_to:'https://hub.test/return?code=one'};
  await a.set('one',pending,300);
  const claimed=await Promise.all([a.claim('one',pending.return_to,'nonce'),b.claim('one',pending.return_to,'nonce')]);
  assert.deepEqual(claimed.sort(),[false,true]);
  await b.set('two',{...pending,return_to:'https://hub.test/return?code=two'},300);
  assert.equal(await b.claim('two','https://hub.test/return?code=two','nonce'),false);
  assert.equal((await b.get('two')).status,'pending');
  assert.equal(await b.claim('two','https://other.test/','fresh'),false);
  assert.equal(await b.claim('two','https://hub.test/return?code=two','fresh'),true);
});
test('Redis delivers a ready token once across replicas and preserves pending polls',async()=>{
  const a=makeStore(command,'openid:poll:'),b=makeStore(command,'openid:poll:');
  await a.set('one',{status:'pending',return_to:'https://hub.test/'},300);
  assert.equal(await b.takeReady('one'),null);
  assert.equal((await a.get('one')).status,'pending');
  await a.set('one',{status:'ready',token:'synthetic-token'},300);
  const results=await Promise.all([a.takeReady('one'),b.takeReady('one')]);
  assert.equal(results.filter(r=>r?.token==='synthetic-token').length,1);
  assert.equal(await a.get('one'),null);
});
test('missing production storage and strict storage failures cannot mint local records',async()=>{
  const original=process.env.NODE_ENV;process.env.NODE_ENV='production';
  try {
    const absent=makeStore(async()=>null,'openid:absent:');
    await assert.rejects(absent.set('one',{status:'pending'},300),/unavailable/);
    assert.equal(await absent.get('one'),null);
    assert.equal(await absent.claim('one','https://hub.test/','nonce'),false);
    const failed=makeStore(async()=>{throw Error('storage unavailable');},'openid:failed:');
    await assert.rejects(failed.set('one',{},300),/unavailable/);
    await assert.rejects(failed.claim('one','https://hub.test/','nonce'),/unavailable/);
  } finally {if(original===undefined)delete process.env.NODE_ENV;else process.env.NODE_ENV=original;}
});
