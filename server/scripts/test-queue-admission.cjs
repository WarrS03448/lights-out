// Run: node --test scripts/test-queue-admission.cjs. No external services.
const test = require('node:test');
const assert = require('node:assert/strict');
delete process.env.COMP_NETWORK_TEST_BYPASS;
process.env.COMP_MATCH_SIZE = '10';
const live = require('../live.cjs');

function fixture(authMethod='steam') {
  const revoked=new Set();
  const ids = Array.from({length:19}, (_,i) => String(76561198000000001n + BigInt(i)));
  const now = Date.now();
  const service = live.create({whoami:async token => revoked.has(token)?null:({steam_id:token,auth_method:authMethod}), bearer:r=>r.token,
    sendJson:(r,status,body)=>Object.assign(r,{status,body}), badRequest(){},
    readBody:async r=>Buffer.from(JSON.stringify(r.body||{})), upstashCmd:null,
    requiredVersions:()=>({hub:'2.3.85',mode:'1.0.27'})});
  const I = service._internals;
  for (const id of ids) {
    I.networkRegistry.profile(id,{region:'NA',cross_region:false,location:id.slice(-2).repeat(16),
      transport:'webrtc-relay-v1',age_seconds:0},now);
    I.clients.set(id,{steamId:id,gameSteamId:id,token:id,res:{write(){},end(){}}});
    I.bySteam.set(id,new Set([id]));
  }
  for (const a of ids) for (const b of ids) if (a!==b) {
    const p=I.networkRegistry.player(a),q=I.networkRegistry.player(b);
    p.pings[b]={at:now,ms:20,localRevision:p.revision,remoteRevision:q.revision};
  }
  const request=async(id,path,body={})=>{
    const res={};
    await service.route({token:id,body,headers:{'x-hub-version':'2.3.85','x-mode-version':'1.0.27'}},res,'POST',path);
    return res;
  };
  return {service,I,ids,now,request,revoked};
}

test('party queue admission rechecks revoked member credentials before heartbeat',async()=>{
  const {service,I,ids,request,revoked}=fixture();
  try {
    const party=await request(ids[0],'/api/party/create');
    await request(ids[1],'/api/party/join',{code:party.body.code});
    revoked.add(ids[1]);
    const response=await request(ids[0],'/api/queue/join');
    assert.equal(response.status,409);
    assert.equal(I.queueOf.has(ids[0]),false);assert.equal(I.inMatch.has(ids[1]),false);
  } finally {await service.shutdown();}
});

test('a Lights Out login with an admin game ID cannot inherit Steam administrator permissions',async()=>{
  const {service,I,ids,request}=fixture('lightsout');
  try {
    I.ADMIN_IDS.add(ids[0]);
    const response=await request(ids[0],'/api/admin/ban',{steam_id:ids[1],reason:'must not apply'});
    assert.equal(response.status,403);
    assert.equal(I.bans.has(ids[1]),false);
  }finally{await service.shutdown();}
});

test('a declined party member never leaves a partial party in the queue',async()=>{
  const {service,I,ids,request}=fixture();
  try {
    const party=await request(ids[0],'/api/party/create');
    await request(ids[1],'/api/party/join',{code:party.body.code});
    const players=ids.slice(0,10).map(steam_id=>({steam_id,accepted:true,connected:false}));
    const match={id:'partial-party',state:'found',players,created:Date.now(),teams:{1:ids.slice(0,5),2:ids.slice(5,10)}};
    I.matches.set(match.id,match);for(const p of players)I.inMatch.set(p.steam_id,match.id);
    I.closeMatch(match,'declined',[ids[1]]);
    assert.ok(!I.queueOf.has(ids[0]));assert.ok(!I.queueOf.has(ids[1]));
    const response=await request(ids[0],'/api/queue/join');
    assert.equal(response.status,200);
    assert.ok(I.inMatch.has(ids[0]) || I.queueOf.get(ids[0])===I.queueOf.get(ids[1]));
  }finally{await service.shutdown();}
});

test('simultaneous final-seat joins cannot enqueue an already matched player',async()=>{
  const {service,I,ids,now,request}=fixture();
  try {
    for (const id of ids.slice(0,9)) I.enqueue([id],'',now);
    await Promise.all([request(ids[9],'/api/queue/join'),request(ids[9],'/api/queue/join')]);
    assert.ok(I.inMatch.has(ids[9]));
    assert.ok(!I.queueOf.has(ids[9]));
    for (const id of ids.slice(10)) I.enqueue([id],'',now);
    assert.equal(I.tryFormMatch(),null);
  } finally { await service.shutdown(); }
});

test('changing party membership invalidates the prior queued roster',async()=>{
  const {service,I,ids,now,request}=fixture();
  try {
    await request(ids[0],'/api/queue/join');
    const party=await request(ids[1],'/api/party/create');
    await request(ids[0],'/api/party/join',{code:party.body.code});
    assert.ok(!I.queueOf.has(ids[0]),'party change cancels the old solo search');
    await request(ids[1],'/api/queue/join');
    assert.equal(I.queuedPlayers(),new Set(I.queue.flatMap(u=>u.members)).size);
    for (const id of ids.slice(2,9)) I.enqueue([id],'',now);
    assert.equal(I.tryFormMatch(),null,'nine unique players cannot create a ten-player match');
  } finally { await service.shutdown(); }
});
