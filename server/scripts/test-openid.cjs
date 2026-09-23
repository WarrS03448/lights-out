'use strict';
// Run: node --test server/scripts/test-openid.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const authModule = require('../auth.cjs');
const adminModule = require('../admin.cjs');
const {assertion} = require('./openid-fixture.cjs');
const ORIGIN = 'https://hub.test';
const ID = '76561198000000001';

function services(options={}) {
  let checks = 0;
  const prefix='openid-test:'+Math.random()+':';
  const verify=async()=>{checks++;return options.verified!==false;};
  const auth=authModule.create({prefix,upstashCmd:async()=>null,verify,
    sendJson:(res,status,data)=>{res.writeHead(status,{});res.end(JSON.stringify(data));},
    badRequest:res=>{res.writeHead(400,{});res.end();}});
  const admin=adminModule.create({prefix,upstashCmd:async()=>null,verifyWithSteam:verify,live:()=>({isAdmin:id=>id===ID})});
  async function call(url, cookie='', headers={}) {
    url=new URL(url,ORIGIN);
    const response={status:0,headers:{},body:''};
    const res={writeHead:(status,headers)=>Object.assign(response,{status,headers}),end:body=>{response.body=String(body||'');}};
    const req={headers:{host:'hub.test',cookie,...headers}};
    if(!await auth.route(req,res,'GET',url.pathname,url))await admin.route(req,res,'GET',url.pathname,url);
    return response;
  }
  async function player() {
    const start=JSON.parse((await call('/api/auth/start')).body);
    const redirect=await call(start.url);
    const returnTo=new URL(redirect.headers.location).searchParams.get('openid.return_to');
    return {code:start.code,url:assertion(returnTo)};
  }
  async function administrator() {
    const redirect=await call('/admin/login');
    const returnTo=new URL(redirect.headers.location).searchParams.get('openid.return_to');
    return {url:assertion(returnTo),cookie:String(redirect.headers['set-cookie']||'').split(';')[0]};
  }
  return {call,player,administrator,checks:()=>checks};
}

for(const kind of ['player','administrator']) {
  test(kind+': Steam rejection cannot create a session',async()=>{
    const s=services({verified:false}),login=await s[kind]();
    const response=await s.call(login.url,login.cookie);
    assert.equal(response.status,400);
    assert.equal(response.headers['set-cookie'],undefined);
    assert.equal(s.checks(),1);
    if(kind==='player')assert.equal(JSON.parse((await s.call('/api/auth/poll?code='+login.code)).body).status,'pending');
  });
  test(kind+': rejects a genuine assertion issued to a different website',async()=>{
    const s=services(),login=await s[kind]();
    login.url.searchParams.set('openid.return_to','https://other.example/return');
    const response=await s.call(login.url,login.cookie);
    assert.equal(response.status,400);
    assert.equal(s.checks(),0);
  });
  test(kind+': rejects incomplete, ambiguous, stale or misbound assertions',async()=>{
    for(const mutate of [
      q=>q.delete('openid.return_to'), q=>q.set('openid.mode','cancel'),
      q=>q.set('openid.ns','wrong'), q=>q.set('openid.op_endpoint','https://other.example/openid'),
      q=>q.set('openid.identity','https://steamcommunity.com/openid/id/76561198000000002'),
      q=>q.set('openid.signed','claimed_id'),q=>q.delete('openid.sig'),
      q=>q.append('openid.claimed_id',q.get('openid.claimed_id')),
      q=>q.set('openid.response_nonce','2020-01-01T00:00:00Zold'),
      q=>q.set('openid.response_nonce','2099-01-01T00:00:00Zfuture'),
      q=>q.append(kind==='player'?'code':'state','duplicate'),
    ]) {
      const s=services(),login=await s[kind]();mutate(login.url.searchParams);
      assert.equal((await s.call(login.url,login.cookie)).status,400);
      assert.equal(s.checks(),0);
    }
  });
  test(kind+': a valid login completes only once even with concurrent callbacks',async()=>{
    const s=services(),login=await s[kind]();
    const responses=await Promise.all([s.call(login.url,login.cookie),s.call(login.url,login.cookie)]);
    assert.equal(responses.filter(r=>r.status===(kind==='player'?200:302)).length,1);
    assert.equal((await s.call(login.url,login.cookie)).status,400);
    if(kind==='player') {
      const polls=await Promise.all([s.call('/api/auth/poll?code='+login.code),s.call('/api/auth/poll?code='+login.code)]);
      assert.equal(polls.filter(r=>JSON.parse(r.body).token).length,1);
    }
  });
  test(kind+': fresh attempt rejects a used nonce and callbacks on a different host',async()=>{
    const s=services(),first=await s[kind](),second=await s[kind]();
    assert.equal((await s.call(first.url,first.cookie,{host:'alias.test'})).status,400);
    assert.equal((await s.call(first.url,first.cookie)).status,kind==='player'?200:302);
    second.url.searchParams.set('openid.response_nonce',first.url.searchParams.get('openid.response_nonce'));
    assert.equal((await s.call(second.url,second.cookie)).status,400);
  });
}
test('administrator: callback must return to the browser that started the login',async()=>{
  const s=services(),login=await s.administrator();
  assert.match(login.cookie,/^hubadmin_login=[a-f0-9]{64}$/);
  assert.equal((await s.call(login.url)).status,400);
  assert.equal((await s.call(login.url,'hubadmin_login='+'b'.repeat(64))).status,400);
  assert.equal((await s.call(login.url,login.cookie)).status,302);
});
test('player: a pending poll does not consume the sign-in attempt',async()=>{
  const s=services(),login=await s.player();
  assert.equal(JSON.parse((await s.call('/api/auth/poll?code='+login.code)).body).status,'pending');
  assert.equal((await s.call(login.url)).status,200);
});
