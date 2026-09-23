'use strict';
const {test}=require('node:test');const assert=require('node:assert/strict');
process.env.NODE_ENV='test';process.env.COMP_NETWORK_TEST_BYPASS='1';
const ranked=require('../ranked-service.cjs');
const A='76561198000000001',B='76561198000000002';
function make(){return ranked.create({prefix:'dual-test:',whoami:async()=>({steam_id:A}),bearer:()=> 'token',
  sendJson:(res,status,body)=>Object.assign(res,{status,body}),badRequest(){},readBody:async()=>Buffer.from('{}')});}
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
