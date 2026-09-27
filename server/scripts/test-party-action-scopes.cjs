// Run: node --test scripts/test-party-action-scopes.cjs. No external services.
const {test} = require('node:test'), assert = require('node:assert/strict');
const live = require('../live.cjs');
const ids = ['76561198000000001','76561198000000002','76561198000000003'];

function fixture(t, options={}) {
  const events = [];
  const service = live.create({whoami:async token=>({steam_id:token,auth_method:'steam'}),
    bearer:req=>req.token, sendJson:(res,status,body)=>Object.assign(res,{status,body}),
    readBody:async req=>Buffer.from(JSON.stringify(req.body||{})), ...options});
  const I=service._internals;
  for (const id of ids) {
    I.clients.set(id,{steamId:id,gameSteamId:id,token:id,res:{write:data=>events.push([id,data]),end(){}}});
    I.bySteam.set(id,new Set([id]));
  }
  const request=async (action,body={},id=ids[0],scoped=true)=>{
    const res={};
    await service.route({token:id,headers:{},body},res,'POST','/api/party/'+(scoped?'scoped/':'')+action);
    return res;
  };
  t.after(()=>service.shutdown());
  return {I,service,events,request,context:id=>I.partyContext(id||ids[0])};
}

test('resending the sole invitation keeps the replacement in the actual inbox',async t=>{
  const f=fixture(t);
  await f.request('create',{},ids[0],false);
  f.I.friends.set(ids[0],new Set([ids[1]]));
  assert.equal((await f.I.inviteToParty(ids[0],{target:ids[1]})).ok,true);
  const first=f.I.partyInvites.get(ids[1]).get(ids[0]);
  const oldTimer=first.timer._onTimeout;
  assert.equal((await f.I.inviteToParty(ids[0],{target:ids[1]})).ok,true);
  const next=f.I.partyInvites.get(ids[1])?.get(ids[0]);
  assert(next,'replacement must not be written to a detached Map');
  assert.notEqual(next.invite_id,first.invite_id);
  oldTimer();
  assert.equal(f.I.partyInvites.get(ids[1]).get(ids[0]),next,'retired timer cannot remove replacement');
});

for (const action of ['create','join','leave','refresh-code','invite','invite/accept','invite/decline']) {
  test(`${action} rejects stale membership without touching party, queue or grace`,async t=>{
    const f=fixture(t), old=f.context();
    const created=await f.request('create',{expected_party_context:old});
    assert.equal(created.status,200);
    const code=created.body.code, party=f.I.parties.get(code);
    const grace=setTimeout(()=>{},60000); f.I.partyGrace.set(ids[0],grace);
    const unit={members:[ids[0]],partyCode:code}; f.I.queue.push(unit); f.I.queueOf.set(ids[0],unit);
    const before=f.events.length;
    const result=await f.request(action,{expected_party_context:old, code, target:ids[1],from:ids[1],expected_invite_id:'1'.repeat(32)});
    assert.equal(result.status,409); assert.equal(result.body.stale_action,true);
    assert.equal(f.I.partyOf.get(ids[0]),code); assert.equal(f.I.parties.get(code),party);
    assert.equal(f.I.queueOf.get(ids[0]),unit); assert.equal(f.I.partyGrace.get(ids[0]),grace);
    assert.equal(f.events.length,before);
  });
}

test('same party code after leave and rejoin has a different membership context',async t=>{
  const f=fixture(t);
  const created=await f.request('create',{},ids[0],false), code=created.body.code;
  await f.request('join',{code},ids[1],false);
  const old=f.context(ids[1]);
  await f.request('leave',{expected_party_context:old},ids[1]);
  await f.request('join',{code,expected_party_context:f.context(ids[1])},ids[1]);
  assert.notEqual(f.context(ids[1]),old);
  assert.equal((await f.request('leave',{expected_party_context:old},ids[1])).status,409);
  assert.equal(f.I.partyOf.get(ids[1]),code);
});

for (const answer of ['accept','decline']) test(`${answer} cannot consume replacement invitation from same sender`,async t=>{
  const f=fixture(t); await f.request('create',{},ids[0],false);
  f.I.friends.set(ids[0],new Set([ids[1]]));
  await f.I.inviteToParty(ids[0],{target:ids[1]});
  const old=f.I.partyInvites.get(ids[1]).get(ids[0]).invite_id;
  await f.I.inviteToParty(ids[0],{target:ids[1]});
  const current=f.I.partyInvites.get(ids[1]).get(ids[0]);
  const res=await f.request('invite/'+answer,{from:ids[0],expected_party_context:f.context(ids[1]),
    expected_invite_id:old},ids[1]);
  assert.equal(res.status,409); assert.equal(res.body.stale_action,true);
  assert.equal(f.I.partyInvites.get(ids[1]).get(ids[0]),current);
  assert.equal(f.I.partyOf.has(ids[1]),false);
});

test('normal scoped create, invite and accept carry successor identities',async t=>{
  const f=fixture(t), solo=f.context();
  const created=await f.request('create',{expected_party_context:solo});
  assert.equal(created.status,200); assert.notEqual(created.body.party_context,solo);
  f.I.friends.set(ids[0],new Set([ids[1]]));
  assert.equal((await f.request('invite',{target:ids[1],expected_party_context:created.body.party_context})).status,200);
  const offered=f.I.invitePayload(ids[1]).invites[0];
  assert.match(offered.invite_id,/^[a-f0-9]{32}$/);
  const joined=await f.request('invite/accept',{from:ids[0],expected_party_context:f.context(ids[1]),
    expected_invite_id:offered.invite_id},ids[1]);
  assert.equal(joined.status,200); assert.equal(joined.body.party_context,f.context(ids[1]));
  assert.equal(f.I.partyOf.get(ids[1]),created.body.code);
  const sent=f.events.filter(([id,data])=>id===ids[1]&&data.includes('party_update')).at(-1)[1];
  assert.equal(JSON.parse(sent.slice(6)).party_context,joined.body.party_context);
});

for (const context of [undefined,null,'',1,'z'.repeat(32)]) test(`scoped create requires valid party context: ${context}`,async t=>{
  const f=fixture(t),res=await f.request('create',{expected_party_context:context});
  assert.equal(res.status,409); assert.equal(f.I.partyOf.size,0);
});

for (const scoped of [true,false]) for (const change of ['leave','rejoin','rotate','full','target_joined']) test(`delayed friend read rechecks ${change} (${scoped?'scoped':'legacy'})`,async t=>{
  let release, entered;
  const paused=new Promise(resolve=>{entered=resolve;});
  const f=fixture(t,{upstashCmd:async ([cmd,key])=>{
    if(cmd==='SMEMBERS' && key.endsWith('friends:'+ids[0])) {
      entered(); return new Promise(resolve=>{release=()=>resolve([ids[1]]);});
    }
    return cmd==='SMEMBERS'?[]:null;
  }});
  const created=await f.request('create',{},ids[0],false), code=created.body.code;
  // Keep the same party alive for the leave/rejoin ABA case.
  await f.request('join',{code},ids[2],false);
  const pending=f.I.inviteToParty(ids[0],{target:ids[1],...(scoped?{expected_party_context:f.context()}:{})});
  await paused;
  if(change==='leave' || change==='rejoin') await f.request('leave',{},ids[0],false);
  if(change==='rejoin') await f.request('join',{code},ids[0],false);
  if(change==='rotate') await f.request('refresh-code',{},ids[0],false);
  if(change==='full') f.I.parties.get(code).members=Array.from({length:10},(_,i)=>i?String(76561198000000100n+BigInt(i)):ids[0]);
  if(change==='target_joined') await f.request('join',{code},ids[1],false);
  const before=f.events.length;
  release(); const result=await pending;
  assert.equal(f.I.partyInvites.has(ids[1]),false);
  assert.equal(f.events.length,before);
  assert.equal(result.ok,change==='target_joined');
});

for(const change of ['rotate','reuse']) test(`old invitation cannot follow party code ${change}`,async t=>{
  const f=fixture(t), created=await f.request('create',{},ids[0],false), code=created.body.code;
  f.I.friends.set(ids[0],new Set([ids[1]]));
  await f.I.inviteToParty(ids[0],{target:ids[1]});
  const id=f.I.partyInvites.get(ids[1]).get(ids[0]).invite_id;
  await f.request('refresh-code',{},ids[0],false);
  if(change==='reuse') f.I.parties.set(code,{code,leaderId:ids[2],members:[ids[2]]});
  const result=await f.request('invite/accept',{from:ids[0],expected_invite_id:id,
    expected_party_context:f.context(ids[1])},ids[1]);
  assert.equal(result.status,409);
  assert.equal(f.I.partyOf.has(ids[1]),false);
  assert.deepEqual(f.I.invitePayload(ids[1]).invites,[]);
});

test('HTTP-only solo contexts expire, stay bounded and cannot authorize stale work',async t=>{
  const f=fixture(t); f.I.bySteam.clear(); f.I.clients.clear();
  const result=await f.request('leave',{},ids[0],false), old=result.body.party_context;
  assert.equal(f.I.partyContexts.size,1);
  f.I.prunePartyContexts(Date.now()+300001);
  assert.equal(f.I.partyContexts.size,0);
  assert.equal((await f.request('create',{expected_party_context:old})).status,409);
  for(let i=0;i<10005;i++) f.context(String(76561198000000000n+BigInt(i)));
  assert.equal(f.I.partyContexts.size,10000);
});

test('connected and party-member contexts survive solo expiry',async t=>{
  const f=fixture(t), connected=f.context(ids[1]);
  await f.request('create',{},ids[0],false);
  const member=f.context(); f.I.bySteam.delete(ids[0]);
  f.I.prunePartyContexts(Date.now()+300001);
  assert.equal(f.context(),member); assert.equal(f.context(ids[1]),connected);
});

for(const action of ['invite/accept','invite/decline']) for(const id of [undefined,null,'',42,'z'.repeat(32)])
test(`${action} rejects missing or malformed invitation identity ${id}`,async t=>{
  const f=fixture(t); await f.request('create',{},ids[0],false);
  f.I.friends.set(ids[0],new Set([ids[1]])); await f.I.inviteToParty(ids[0],{target:ids[1]});
  const invite=f.I.partyInvites.get(ids[1]).get(ids[0]);
  const result=await f.request(action,{from:ids[0],expected_invite_id:id,
    expected_party_context:f.context(ids[1])},ids[1]);
  assert.equal(result.status,409); assert.equal(f.I.partyInvites.get(ids[1]).get(ids[0]),invite);
});

test('scoped routes retain authentication and reject invalid bodies without mutation',async t=>{
  const f=fixture(t,{whoami:async()=>null});
  assert.equal((await f.request('create',{expected_party_context:f.context()})).status,401);
  assert.equal(f.I.parties.size,0);
  for(const text of ['{','null','[]','"value"']) {
    const malformed=fixture(t,{readBody:async()=>Buffer.from(text)});
    assert.equal((await malformed.request('leave')).status,409);
    assert.equal(malformed.I.partyContexts.size,0);
  }
});
