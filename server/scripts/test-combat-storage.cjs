const test=require('node:test'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const blobs=require('../combat-storage.cjs');

test('large incompressible combat evidence survives bounded writes, retry and hydration',async()=>{
  const data=new Map(),calls=[];
  const store=async cmd=>{
    assert.ok(Buffer.byteLength(JSON.stringify(cmd))<10*1024*1024);
    calls.push(cmd);
    if(cmd[0]==='SET'){data.set(cmd[1],cmd[2]);return 'OK';}
    return data.get(cmd[1])??null;
  };
  const receipt={matchId:'large',inputs:{combatState:{events:[],rejected:[{raw:crypto.randomBytes(9*1024*1024).toString('base64')}]}}};
  const packed=await blobs.pack(store,receipt,'hub:settlement:large');
  assert.ok(packed.inputs.combatState.__combat_blob);
  assert.deepEqual(await blobs.unpack(store,packed,'hub:settlement:large'),receipt);
  assert.deepEqual(await blobs.pack(store,receipt,'hub:settlement:large'),packed);
  assert.ok(calls.filter(c=>c[0]==='SET').every(c=>c.length===3),'final evidence never expires');
  const first=data.keys().next().value;
  data.delete(first);
  await assert.rejects(blobs.unpack(store,packed,'hub:settlement:large'),/Missing/);
});

test('failed chunks never produce a publishable manifest and live chunks have a TTL',async()=>{
  const calls=[];
  const receipt={combatState:{events:[{raw:'x'.repeat(300000)}]}};
  await assert.rejects(blobs.pack(async()=>null,receipt,'live'),/not saved/);
  const packed=await blobs.pack(async c=>{calls.push(c);return 'OK';},receipt,'live',{ttl:172800});
  assert.ok(packed.combatState.__combat_blob);
  assert.deepEqual(calls[0].slice(-2),['EX','172800']);
  await assert.rejects(blobs.unpack(async()=> 'a'.repeat(20),packed,'live'),/Truncated/);
});
