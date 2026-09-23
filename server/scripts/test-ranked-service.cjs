'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
process.env.NODE_ENV='test';process.env.COMP_NETWORK_TEST_BYPASS='1';
const ranked=require('../ranked-service.cjs');
const A='76561198000000001',B='76561198000000002';
function make(options={}){return ranked.create({prefix:'dual-test:',whoami:async token=>({steam_id:token}),bearer:req=>req.token||A,
  sendJson:(res,status,body)=>Object.assign(res,{status,body}),badRequest(){},readBody:async req=>Buffer.from(JSON.stringify(req.body||{})),...options});}
async function request(s,id,path,mode='BB1',body={}){
 const res={};await s.route({token:id,body,headers:{'x-ranked-mode':mode}},res,'POST',path,new URL('http://test'+path));return res;
}

test('one-person party blocks BB1 through the API until the player leaves it',async t=>{
 const s=make();t.after(()=>s.shutdown());
 assert.equal((await request(s,A,'/api/party/create')).status,200);
 const denied=await request(s,A,'/api/queue/join');assert.equal(denied.status,409);assert.equal(denied.body.solo_only,true);
 assert.equal(s.forMode('BB1')._internals.queueOf.has(A),false);
 await request(s,A,'/api/party/leave');assert.equal((await request(s,A,'/api/queue/join')).status,200);
});

test('queued BB1 player cannot create, join or accept an invite into a party',async t=>{
 const s=make();t.after(()=>s.shutdown());const I=s.forMode('BB5')._internals;
 const party=await request(s,B,'/api/party/create');assert.equal((await request(s,A,'/api/queue/join')).status,200);
 assert.equal((await request(s,A,'/api/party/create')).status,409);
 assert.equal((await request(s,A,'/api/party/join','BB1',{code:party.body.code})).status,409);
 I.partyInvites.set(A,new Map([[B,{code:party.body.code,expires:Date.now()+60000}]]));
 assert.equal(I.acceptPartyInvite(A,{from:B}).ok,false);
 assert.equal(I.partyOf.has(A),false);assert(s.forMode('BB1')._internals.queueOf.has(A));
 await request(s,A,'/api/queue/leave');assert.equal(I.acceptPartyInvite(A,{from:B}).ok,true);
});

test('creating a party while BB1 admission waits cannot race into the queue',async t=>{
 let checks=0,release,entered;const waiting=new Promise(resolve=>entered=resolve),hold=new Promise(resolve=>release=resolve);
 const s=make({whoami:async token=>{if(++checks===2){entered();await hold;}return {steam_id:token};}});t.after(()=>s.shutdown());
 const joining=request(s,A,'/api/queue/join');await waiting;
 assert.equal((await request(s,A,'/api/party/create')).status,200);release();
 assert.equal((await joining).status,409);assert.equal(s.forMode('BB1')._internals.queueOf.has(A),false);
});
test('one account cannot queue both ladders and can switch after cancelling',async t=>{
  const s=make();t.after(()=>s.shutdown());await s._internals.ensureRecovery();
  const a=s.forMode('BB5')._internals,b=s.forMode('BB1')._internals;
  assert.ok(a.enqueue([A],'',Date.now()));assert.equal(b.enqueue([A],'',Date.now()),null);
  a.removeFromQueue(A);assert.ok(b.enqueue([A],'',Date.now()));assert.equal(a.enqueue([A],'',Date.now()),null);
});
test('BB1 entry does not silently split a two-person BB5 party',async t=>{
  const s=make();t.after(()=>s.shutdown());await s._internals.ensureRecovery();
  const I=s.forMode('BB5')._internals;
  I.parties.set('SQUAD',{code:'SQUAD',members:[A,B],leaderId:A});I.partyOf.set(A,'SQUAD');I.partyOf.set(B,'SQUAD');
  assert.equal(s.forMode('BB1')._internals.enqueue([A],'',Date.now()),null);
  assert.deepEqual(I.parties.get('SQUAD').members,[A,B]);
});
test('invalid mode is rejected by request router',async t=>{
  const s=make();t.after(()=>s.shutdown());const res={};
  await s.route({headers:{'x-ranked-mode':'oops'}},res,'GET','/api/leaderboard',new URL('http://test/api/leaderboard'));
  assert.equal(res.status,400);assert.equal(res.body.ok,false);
});
test('a mode ban cannot close the shared connection or block the other ladder',async t=>{
  const {EventEmitter}=require('node:events'),s=make();t.after(()=>s.shutdown());
  await s._internals.ensureRecovery();s.forMode('BB5')._internals.bans.set(A,{reason:'Cheating',until:0});
  const req=new EventEmitter();req.headers={'x-ranked-mode':'BB1'};req.socket={setTimeout(){}};
  const frames=[];const res={headersSent:false,setTimeout(){},setHeader(){},writeHead(){this.headersSent=true;},
    write(s){frames.push(s);},end(){this.writableEnded=true;}};
  await s.route(req,res,'GET','/api/live',new URL('http://test/api/live'));
  t.after(()=>req.emit('close'));
  assert.equal(res.writableEnded,undefined);
  const events=frames.filter(s=>s.startsWith('data: ')).map(s=>JSON.parse(s.slice(6)));
  assert.ok(events.some(e=>e.type==='hello'&&e.mode==='BB1'));
  assert.ok(events.some(e=>e.type==='ranked_ban'&&e.mode==='BB5'));
});
