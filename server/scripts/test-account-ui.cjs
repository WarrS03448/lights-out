// Run: node --test scripts/test-account-ui.cjs (no network, email or real credentials).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.join(__dirname, '../public/assets');

function page(fetch) {
  const nodes = new Map();
  for (const id of ['language','email-form','verify-form','complete','status','email','code',
    'display-name','password','confirm-password','have-code','restart']) {
    nodes.set(id, {value:'', hidden:['verify-form','complete'].includes(id), textContent:'',
      listeners:{}, fieldset:{disabled:true}, focus(){}, setAttribute(){},
      addEventListener(event, fn){this.listeners[event]=fn;}, querySelector(){return this.fieldset;},
      reset(){for (const key of (id==='email-form'?['email']:['code','display-name','password','confirm-password'])) nodes.get(key).value='';}});
  }
  const document = {documentElement:{}, getElementById:id=>nodes.get(id), querySelectorAll:()=>[]};
  const window = {addEventListener(){}};
  const context = vm.createContext({window,document,navigator:{language:'en'},fetch,AbortController,setTimeout,clearTimeout});
  vm.runInContext(fs.readFileSync(path.join(assets,'account-i18n.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(assets,'account.js'),'utf8'),context);
  return {nodes,strings:window.LightsOutAccountStrings,
    submit:id=>nodes.get(id).listeners.submit({preventDefault(){}}),
    click:id=>nodes.get(id).listeners.click()};
}
const response=(status, body={ok:true})=>({status,ok:status>=200&&status<300,json:async()=>body});

test('website creation verifies email first, sends exact fields, and clears secrets on success',async()=>{
  const calls=[];
  const p=page(async(url,options)=>{calls.push({url,options});return response(url.endsWith('/register')?202:201);});
  p.nodes.get('email').value='fixture@example.test';
  await p.submit('email-form');
  assert.equal(p.nodes.get('verify-form').hidden,false);
  assert.deepEqual(JSON.parse(calls[0].options.body),{email:'fixture@example.test'});
  p.nodes.get('code').value='  '+ 'aB_'.repeat(14)+'X'+'  ';
  p.nodes.get('password').value=p.nodes.get('confirm-password').value='A long fixture password';
  p.nodes.get('display-name').value='Player';
  await p.submit('verify-form');
  assert.deepEqual(JSON.parse(calls[1].options.body),{token:'aB_'.repeat(14)+'X',password:'A long fixture password',display_name:'Player'});
  for (const call of calls) {
    assert.ok(call.url.startsWith('/api/auth/account/'));
    assert.equal(call.options.credentials,'omit');assert.equal(call.options.cache,'no-store');
    assert.equal(call.options.referrerPolicy,'no-referrer');assert.equal(call.options.method,'POST');
  }
  assert.equal(p.nodes.get('complete').hidden,false);
  for (const field of ['password','confirm-password','code','email']) assert.equal(p.nodes.get(field).value,'');
});

test('mail failure, wrong origin, throttling, non-JSON proxy failure and wrong success status never advance',async()=>{
  for (const result of [response(503,{ok:false}),response(403,{ok:false}),response(429,{ok:false}),
    response(200),{status:503,ok:false,json:async()=>{throw Error('proxy');}},Error('offline')]) {
    const p=page(async()=>{if(result instanceof Error)throw result;return result;});
    await p.submit('email-form');
    assert.equal(p.nodes.get('email-form').hidden,false);
    assert.equal(p.nodes.get('verify-form').hidden,true);
    assert.equal(p.nodes.get('email-form').fieldset.disabled,false);
    assert.notEqual(p.nodes.get('status').textContent,p.strings.en.sent);
  }
});

test('double submit sends once; wrong code remains retryable; lost creation response is uncertain',async()=>{
  let finish, calls=0;
  const p=page(()=>{calls++;return new Promise(resolve=>{finish=resolve;});});
  const pending=p.submit('email-form');await p.submit('email-form');
  assert.equal(calls,1);finish(response(202));await pending;
  for(const offline of [false,true]) {
    const q=page(async()=>{if(offline)throw Error('offline');return response(400,{ok:false,code:'invalid_code'});});
    q.click('have-code');q.nodes.get('code').value='a'.repeat(43);
    q.nodes.get('password').value=q.nodes.get('confirm-password').value='Another test password';
    await q.submit('verify-form');
    assert.equal(q.nodes.get('verify-form').hidden,false);
    assert.equal(q.nodes.get('code').value,'a'.repeat(43));
    assert.equal(q.nodes.get('password').value,'');
    assert.equal(q.nodes.get('status').textContent,offline?q.strings.en.uncertain:q.strings.en.badCode);
  }
});

test('password confirmation and Unicode length prevent a mistyped account without submitting',async()=>{
  const p=page(()=>assert.fail('No valid password to submit'));
  p.nodes.get('password').value='A long fixture password';p.nodes.get('confirm-password').value='different';
  await p.submit('verify-form');assert.equal(p.nodes.get('status').textContent,p.strings.en.mismatch);
  p.nodes.get('password').value=p.nodes.get('confirm-password').value='😀'.repeat(5);
  await p.submit('verify-form');assert.equal(p.nodes.get('status').textContent,p.strings.en.badPassword);
});

test('six-character password is accepted by the account page',async()=>{
  const calls=[];const p=page(async(url,options)=>{calls.push(JSON.parse(options.body));return response(201);});
  p.nodes.get('code').value='aB_'.repeat(14)+'X';p.nodes.get('display-name').value='Player';
  p.nodes.get('password').value=p.nodes.get('confirm-password').value='Six123';
  await p.submit('verify-form');assert.equal(calls.length,1);assert.equal(calls[0].password,'Six123');
});

test('every account-page message has all seven translations',()=>{
  const {strings}=page(()=>assert.fail('No request expected'));
  assert.deepEqual(Object.keys(strings).sort(),['de','en','es','fr','pt','ru','zh']);
  const keys=Object.keys(strings.en).sort();
  for(const [culture,copy] of Object.entries(strings)) {
    assert.deepEqual(Object.keys(copy).sort(),keys,culture);
    for(const value of Object.values(copy))assert.ok(value.length>0);
  }
});
