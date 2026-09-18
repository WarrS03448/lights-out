'use strict';
const {test,afterEach}=require('node:test');
const assert=require('node:assert/strict');
const {fromEnvironment}=require('../account-mail.cjs');
const nodemailer=require('nodemailer');
const ENV={HUB_SMTP_HOST:'smtp.example.test',HUB_SMTP_PORT:'465',HUB_SMTP_USER:'test-user',
  HUB_SMTP_PASSWORD:'test-password',HUB_SMTP_FROM:'Lights Out <accounts@example.test>'};
const originalFetch=global.fetch;
afterEach(()=>{global.fetch=originalFetch;});
const env={HUB_MAIL_TRANSPORT:'resend-https',HUB_SMTP_HOST:'smtp.resend.com',HUB_SMTP_USER:'resend',
  HUB_SMTP_PASSWORD:'fixture-private-key',HUB_SMTP_FROM:'Lights Out <accounts@example.test>'};
const message={to:'member@example.test',kind:'verify',token:'fixture-verification-code'};

test('Resend HTTPS uses the configured restricted key and existing account message',async()=>{
  let sent;
  global.fetch=async(url,options)=>{sent={url,options};return {ok:true,json:async()=>({id:'49a3999c-0ce1-4ea6-ab68-afcd6dc2e794'})};};
  await fromEnvironment(env)(message);
  assert.equal(sent.url,'https://api.resend.com/emails');
  assert.equal(sent.options.method,'POST');
  assert.equal(sent.options.redirect,'error');
  assert.equal(sent.options.headers.authorization,'Bearer fixture-private-key');
  assert.ok(sent.options.signal instanceof AbortSignal);
  const body=JSON.parse(sent.options.body);
  assert.equal(body.from,env.HUB_SMTP_FROM);assert.deepEqual(body.to,[message.to]);
  assert.equal(body.subject,'Create your Lights Out account');assert.ok(body.text.includes(message.token));
  assert.equal(Object.hasOwn(body,'headers'),false);
});
test('HTTPS account mail rejects provider failures and incomplete receipts without leaking details',async()=>{
  for(const response of [{ok:false,json:async()=>({message:'fixture-private-key'})},
    {ok:true,json:async()=>({})},{ok:true,json:async()=>{throw Error('fixture-private-key');}}]) {
    global.fetch=async()=>response;
    await assert.rejects(fromEnvironment(env)(message),{message:'Account email was not accepted'});
  }
  global.fetch=async()=>{throw Error('fixture-private-key');};
  await assert.rejects(fromEnvironment(env)(message),{message:'Account email was not accepted'});
});
test('HTTPS request timeout is bounded inside the desktop request deadline',async()=>{
  const original=AbortSignal.timeout;let duration;
  AbortSignal.timeout=ms=>{duration=ms;return original(ms);};
  try {
    global.fetch=async()=>({ok:true,json:async()=>({id:'receipt'})});
    await fromEnvironment(env)(message);assert.equal(duration,10000);
  } finally {AbortSignal.timeout=original;}
});
test('Resend selection cannot send another SMTP provider credential to Resend',()=>{
  for(const bad of [{HUB_SMTP_HOST:'smtp.other.test'},{HUB_SMTP_USER:'other'},
    {HUB_SMTP_PASSWORD:''},{HUB_MAIL_TRANSPORT:'unknown'},{HUB_SMTP_FROM:'bad\r\nheader'}])
    assert.equal(fromEnvironment({...env,...bad}),null);
  assert.equal(typeof fromEnvironment({...env,HUB_MAIL_TRANSPORT:'smtp'}),'function');
});

test('SMTP delivery requires TLS, keeps credentials out of messages and sends codes only when actionable',async t=>{
  let settings;const messages=[];
  t.mock.method(nodemailer,'createTransport',options=>{
    settings=options;return {sendMail:async message=>{messages.push(message);return {accepted:[message.to]};}};
  });
  const send=fromEnvironment(ENV);
  assert.equal(settings.secure,true);assert.equal(settings.requireTLS,true);
  assert.equal(settings.tls.rejectUnauthorized,true);assert.equal(settings.logger,false);
  assert.equal(settings.debug,false);assert.equal(settings.disableFileAccess,true);assert.equal(settings.disableUrlAccess,true);
  for(const kind of ['verify','reset','login']) {
    await send({kind,to:'player@example.test',token:'secret-test-code',actionable:true});
    assert.ok(messages.at(-1).text.includes('secret-test-code'));
    if(kind==='verify') assert.match(messages.at(-1).text,/in the Lights Out app or on the Lights Out website/);
    await send({kind,to:'player@example.test',token:'secret-test-code',actionable:false});
    assert.ok(!messages.at(-1).text.includes('secret-test-code'));
  }
  assert.ok(!JSON.stringify(messages).includes('test-password'));
  fromEnvironment({...ENV,HUB_SMTP_PORT:'587'});
  assert.equal(settings.secure,false);assert.equal(settings.requireTLS,true);
});

test('missing/insecure SMTP settings cannot enable email and rejected recipients fail delivery',async t=>{
  for(const altered of [{HUB_SMTP_HOST:''},{HUB_SMTP_USER:''},{HUB_SMTP_PASSWORD:''},
    {HUB_SMTP_PORT:'25'},{HUB_SMTP_FROM:'sender@example.test\r\nBcc:other@example.test'}])
    assert.equal(fromEnvironment({...ENV,...altered}),null);
  t.mock.method(nodemailer,'createTransport',()=>({sendMail:async()=>({accepted:[],rejected:['player@example.test']})}));
  await assert.rejects(fromEnvironment(ENV)({kind:'verify',to:'player@example.test',token:'code'}),/not accepted/);
});

test('ownership emails name the exact action and Steam account and explain retained data',async t=>{
  const messages=[];
  t.mock.method(nodemailer,'createTransport',()=>({sendMail:async message=>{messages.push(message);return {accepted:[message.to]};}}));
  const send=fromEnvironment(ENV);
  for(const kind of ['link-steam','disconnect-steam']) {
    await send({kind,to:'owner@example.test',steam_id:'76561198000000001',token:'123456'});
    assert.match(messages.at(-1).text,/76561198000000001/);
    assert.match(messages.at(-1).text,/owner@example.test/);
    assert.match(messages.at(-1).text,/123456/);
    assert.match(messages.at(-1).text,/5 minutes/);
  }
  assert.match(messages.at(-1).text,/progress and account data will stay with your Lights Out account/);
  assert.match(messages.at(-1).subject,/disconnect/);
});
