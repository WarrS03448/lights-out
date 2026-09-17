'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const filename=path.join(__dirname,'../penalty-write.cjs');
const lib=fs.existsSync(filename)?require(filename):{};
const NOW=1800000000000;
const record=()=>({last:NOW,until:NOW+60000,reason:'no_show',count:1,elo:25});

test('exposes immutable preparation and strict guarded writes',()=>{
  assert.equal(typeof lib.prepare,'function');assert.equal(typeof lib.write,'function');assert.equal(typeof lib.WRITE,'string');
});
test('preparation snapshots mutable callers and fixes expiration across retries',()=>{
  const r=record(),op=lib.prepare(r,NOW);r.count=99;
  assert.ok(Object.isFrozen(op));assert.equal(JSON.parse(op.json).count,1);
  assert.equal(op.expiresAt,NOW+60000+14*86400000);assert.match(op.operationId,/^[a-f0-9]{32}$/);
  assert.notEqual(lib.prepare(record(),NOW).operationId,op.operationId);
});
test('invalid/non-finite penalty payloads fail before contacting storage',async()=>{
  for(const changed of [{last:NaN},{until:Infinity},{until:NOW-1},{count:-1},{count:1.5},{elo:null},{reason:'team_kill'}])
    assert.throws(()=>lib.prepare({...record(),...changed},NOW));
  let called=false;await assert.rejects(lib.write(async()=>{called=true;},'penalty:p',{json:'invalid'}));assert.equal(called,false);
});
test('writer returns current durable state for stale and replayed operations and uses strict persistence',async()=>{
  const newer={...record(),last:NOW+1,reason:'team_kill',count:2};
  for(const status of ['written','replayed','stale']) {
    let command;const store=async(cmd,options)=>{assert.equal(options.strict,true);command=cmd;return [status,JSON.stringify(newer)];};
    const op=lib.prepare(record(),NOW),out=await lib.write(store,'hub:penalty:p',op);
    assert.deepEqual(out,{status,record:newer});assert.equal(command[0],'EVAL');assert.equal(command[2],'2');
    assert.deepEqual(command.slice(3,5),['hub:penalty:p','hub:penalty:p:write']);assert.deepEqual(JSON.parse(command[5]),op);
  }
  assert.deepEqual(await lib.write(async()=>['replayed',''],'p',lib.prepare(record(),NOW)),{status:'replayed',record:null});
  const legacy={until:NOW+60000,count:2,elo:50};
  assert.deepEqual(await lib.write(async()=>['stale',JSON.stringify(legacy)],'p',lib.prepare(record(),NOW)),{status:'stale',record:legacy});
});
test('storage, invalid return and malformed durable records remain retryable failures',async()=>{
  const op=lib.prepare(record(),NOW);
  await assert.rejects(lib.write(async()=>{throw new Error('offline');},'p',op),/offline/);
  for(const out of [null,['invalid-type'],['replayed'],['stale',null],['written','invalid'],['written','[]'],['written','{"last":null}']])
    await assert.rejects(lib.write(async()=>out,'p',op));
});
