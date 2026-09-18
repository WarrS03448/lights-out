'use strict';
const {test, before, after, beforeEach} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const readline = require('node:readline');
const {spawn} = require('node:child_process');
const authModule = require('../auth.cjs');
const PASSWORD = 'Six123';
const NEW_PASSWORD = 'Another long unique passphrase!';
const EMAIL = 'player@example.test';
const STEAM = '76561198000000001';
const ORIGIN = 'https://accounts.example.test';
let bridge, store, auth, server, base, outbox, storeDown, mailDown, prefix;

function sendJson(res, status, body, headers = {}) {
  res.writeHead(status, {'content-type':'application/json', 'cache-control':'no-store', ...headers});
  res.end(JSON.stringify(body));
}
function makeAuth(options = {}) {
  return authModule.create({upstashCmd: store, prefix, accountPrefix:options.accountPrefix, privateGameplay:options.privateGameplay, sendJson,
    badRequest: (res, error) => sendJson(res,400,{error}),
    accounts: {enabled:true,ownership:true, origin:ORIGIN, secret:'s'.repeat(64),
      sendMail: async message => { if(mailDown) throw Error('smtp password must stay private'); outbox.push(message); },
      ...options}});
}
async function request(route, body, token, extra = {}) {
  const headers = {'content-type':'application/json', ...extra.headers};
  if(token) headers.authorization = 'Bearer ' + token;
  const response = await fetch(base + (route.startsWith('/') ? route : '/api/auth/account/' + route), {
    method: body === undefined ? 'GET' : 'POST', headers,
    ...(body === undefined ? {} : {body: JSON.stringify(body)}), ...extra,
    headers,
  });
  return {status: response.status, headers: response.headers, body: await response.json()};
}
async function register(email = EMAIL, password = PASSWORD) {
  assert.equal((await request('register',{email})).status,202);
  const token = outbox.findLast(m=>m.kind==='verify' && m.to===email.toLowerCase().trim()).token;
  const verified = await request('verify',{token,password,display_name:'Player One'});
  assert.equal(verified.status,201,JSON.stringify(verified.body));
  return verified.body.account;
}
async function login(email = EMAIL, password = PASSWORD, remember_me = false) {
  const started=await request('login',{email,password,remember_me});
  assert.equal(started.status,202,JSON.stringify(started.body));
  assert.equal(started.body.token,undefined);
  const code=outbox.findLast(m=>m.kind==='login'&&m.to===email.trim().toLowerCase()).token;
  const result = await request('login/verify',{challenge:started.body.challenge,code});
  assert.equal(result.status,200,JSON.stringify(result.body));
  return result.body.token;
}
async function steamSession(token='steam-proof', steamId=STEAM) {
  const raw=await store(['GET',prefix+'accounts:steam-identity:'+steamId]);
  const generation=raw?JSON.parse(raw).generation:0;
  await store(['SET',prefix+'auth:token:'+token,JSON.stringify({steam_id:steamId,created:Date.now(),steam_generation:generation}),'EX','300']);
  return token;
}
async function startLink(token,steamToken='steam-proof') {
  const started=await request('link-steam',{password:PASSWORD,steam_token:steamToken},token);
  assert.equal(started.status,202,JSON.stringify(started.body));
  return {challenge:started.body.challenge,code:outbox.findLast(m=>m.kind==='link-steam').token,steam_token:steamToken};
}
async function link(token,steamToken='steam-proof') {
  return request('link-steam/verify',await startLink(token,steamToken),token);
}
before(async()=>{
  bridge = spawn(process.env.ACCOUNT_TEST_PYTHON || 'python', ['-u',path.join(__dirname,'account-redis-fixture.py')],{stdio:['pipe','pipe','pipe']});
  const pending = new Map(); let sequence=0, errors='';
  bridge.stderr.on('data',chunk=>{errors+=chunk;});
  readline.createInterface({input:bridge.stdout}).on('line',line=>{
    const response=JSON.parse(line), callbacks=pending.get(response.id); pending.delete(response.id);
    if(callbacks) response.error ? callbacks.reject(Error(response.error)) : callbacks.resolve(response.result);
  });
  bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('Redis test bridge exited: '+errors));pending.clear();});
  store=async(command, options)=>{
    if(storeDown) throw Error('redis credentials must stay private');
    return new Promise((resolve,reject)=>{
      const id=++sequence; pending.set(id,{resolve,reject}); bridge.stdin.write(JSON.stringify({id,command})+'\n');
    });
  };
  assert.equal(await store(['PING']),'PONG');
  server=http.createServer(async(req,res)=>{
    try {
      const url=new URL(req.url,ORIGIN);
      if(!await auth.route(req,res,req.method,url.pathname,url))sendJson(res,404,{error:'Not found'});
    } catch {sendJson(res,500,{error:'Unexpected test server error'});}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  base='http://127.0.0.1:'+server.address().port;
});
beforeEach(async()=>{
  storeDown=false;mailDown=false;outbox=[];prefix='accounts-test:';
  await store(['FLUSHDB']);auth=makeAuth();
});
after(async()=>{
  if(server){server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
  bridge?.stdin.end();
});

test('a verified email creates an independent account that can sign in without Steam',async()=>{
  const account=await register(' Player@Example.Test ');
  assert.match(account.id,/^[a-f0-9-]{36}$/);assert.equal(account.email,EMAIL);
  assert.equal(account.steam_id,null);assert.equal(account.display_name,'Player One');
  const token=await login('PLAYER@example.test');assert.match(token,/^lo_[A-Za-z0-9_-]{43}$/);
  const me=await request('me',undefined,token);
  assert.equal(me.status,200);assert.equal(me.body.account.id,account.id);
  assert.equal((await request('/api/auth/me',undefined,token)).body.account_id,account.id);
  assert.equal(me.headers.get('cache-control'),'no-store');
  assert.equal(await auth.whoami(token),null,'unverified game identity must not enter legacy Steam roster');
  auth=makeAuth();assert.equal((await request('me',undefined,token)).body.account.id,account.id);
});

test('registration cannot pre-claim an email or return a verification secret to its caller',async()=>{
  const started=await request('register',{email:EMAIL,password:'attacker password'});
  assert.equal(started.status,202);assert.ok(!JSON.stringify(started.body).includes(outbox[0].token));
  assert.equal((await request('login',{email:EMAIL,password:PASSWORD})).status,401);
  const token=outbox[0].token;
  assert.equal((await request('verify',{token,password:PASSWORD,display_name:'Owner'})).status,201);
  assert.equal((await request('verify',{token,password:NEW_PASSWORD,display_name:'Attacker'})).status,400);
  assert.equal((await request('login',{email:EMAIL,password:'attacker password'})).status,401);
  await login();
});

test('duplicate email verification is atomic and never overwrites the winning account',async()=>{
  await request('register',{email:EMAIL});await request('register',{email:EMAIL});
  const results=await Promise.all(outbox.map(m=>request('verify',{token:m.token,password:PASSWORD,display_name:'Owner'})));
  assert.deepEqual(results.map(r=>r.status).sort(),[201,400]);
  const duplicate=await request('register',{email:EMAIL});
  assert.equal(duplicate.status,202);
  assert.equal((await request('verify',{token:outbox.at(-1).token,password:NEW_PASSWORD,display_name:'Takeover'})).status,400);
  await login();
});

test('passwords and bearer/code secrets never appear in stored values or API summaries',async()=>{
  await register();const token=await login();
  const keys=await store(['KEYS',prefix+'*']);
  const values=[];for(const key of keys)if(await store(['TYPE',key])==='string')values.push(await store(['GET',key]));
  const stored=JSON.stringify({keys,values});
  for(const secret of [PASSWORD,token,outbox[0].token])assert.ok(!stored.includes(secret));
  assert.ok(stored.includes('scrypt$'));
  assert.ok(!JSON.stringify((await request('me',undefined,token)).body).includes('scrypt$'));
});

test('login failure is generic for unknown email or wrong password and logout revokes a session',async()=>{
  await register();
  const bad=await request('login',{email:EMAIL,password:NEW_PASSWORD});
  const absent=await request('login',{email:'absent@example.test',password:NEW_PASSWORD});
  assert.equal(bad.status,401);assert.deepEqual(absent.body,bad.body);
  const first=await login(),second=await login();assert.notEqual(first,second);
  assert.equal((await request('/api/auth/signout',{},first)).status,200);
  assert.equal((await request('me',undefined,first)).status,401);
  assert.equal((await request('me',undefined,second)).status,200);
});

test('reset codes are single use and changing credentials revokes every old account session',async()=>{
  await register();const first=await login(),second=await login();
  const unknown=await request('forgot-password',{email:'nobody@example.test'});
  const known=await request('forgot-password',{email:EMAIL});
  assert.equal(known.status,202);assert.deepEqual(known.body,unknown.body);
  const code=outbox.findLast(m=>m.kind==='reset').token;
  const reset=await request('reset-password',{token:code,password:NEW_PASSWORD});
  assert.equal(reset.status,200);
  for(const token of [first,second])assert.equal((await request('me',undefined,token)).status,401);
  assert.equal((await request('reset-password',{token:code,password:PASSWORD})).status,400);
  assert.equal((await request('login',{email:EMAIL,password:PASSWORD})).status,401);
  await login(EMAIL,NEW_PASSWORD);
});

test('password change checks old password, revokes sessions and invalidates outstanding reset codes',async()=>{
  await register();const token=await login();await request('forgot-password',{email:EMAIL});
  const resetToken=outbox.findLast(m=>m.kind==='reset').token;
  assert.equal((await request('change-password',{current_password:'wrong',password:NEW_PASSWORD},token)).status,401);
  assert.equal((await request('change-password',{current_password:PASSWORD,password:NEW_PASSWORD},token)).status,200);
  assert.equal((await request('me',undefined,token)).status,401);
  assert.equal((await request('reset-password',{token:resetToken,password:PASSWORD})).status,400);
  await login(EMAIL,NEW_PASSWORD);
});

test('optional linking requires both proofs and allows either login to reach the same account',async()=>{
  const account=await register();let token=await login();
  const steamToken=await steamSession('verified-steam-session');
  assert.equal((await request('link-steam',{password:PASSWORD,steam_id:STEAM},token)).status,401);
  assert.equal((await request('link-steam',{password:'wrong',steam_token:steamToken},token)).status,401);
  assert.equal((await request('link-steam',{password:PASSWORD,steam_token:token},token)).status,401);
  assert.equal((await link(token,steamToken)).status,200);
  assert.equal((await request('me',undefined,token)).status,401,'linking revokes pre-link account sessions');
  assert.equal(await auth.whoami(steamToken),null,'old Steam tokens cannot inherit new authority');
  token=await login();await steamSession(steamToken);
  assert.equal(await auth.whoami(token),null,'linked Lights Out login still requires active game proof');
  assert.equal((await auth.whoami(steamToken)).steam_id,STEAM);
  const steamMe=await request('me',undefined,steamToken);
  assert.equal(steamMe.status,200);assert.equal(steamMe.body.account.id,account.id);
  assert.equal((await request('/api/auth/me',undefined,steamToken)).body.account_id,account.id);
  const other=await register('other@example.test');const otherToken=await login('other@example.test');
  assert.notEqual(account.id,other.id);
  assert.equal((await link(otherToken,steamToken)).status,409);
  assert.equal((await request('me',undefined,otherToken)).body.account.steam_id,null);
});

test('disabled accounts and storage/delivery failures fail closed without exposing secrets',async()=>{
  auth=makeAuth({enabled:false});assert.equal((await request('register',{email:EMAIL})).status,503);
  auth=makeAuth();storeDown=true;
  let result=await request('register',{email:EMAIL});assert.equal(result.status,503);
  assert.ok(!JSON.stringify(result.body).includes('redis credentials'));
  storeDown=false;mailDown=true;result=await request('register',{email:EMAIL});
  assert.equal(result.status,503);assert.ok(!JSON.stringify(result.body).includes('smtp password'));
});

test('rejects malformed input, short passwords, cross-origin writes and non-JSON requests',async()=>{
  for(const email of ['bad','x\r\ny@example.test','a@b','a'.repeat(300)+'@example.test'])
    assert.equal((await request('register',{email})).status,400);
  assert.equal((await request('register',{email:EMAIL},null,{headers:{origin:'https://evil.test'}})).status,403);
  assert.equal((await request('register',{email:EMAIL},null,{headers:{'content-type':'text/plain'}})).status,415);
  await request('register',{email:EMAIL});const code=outbox[0].token;
  for(const password of ['short','p'.repeat(129)])
    assert.equal((await request('verify',{token:code,password,display_name:'P'})).status,400);
  assert.equal((await request('verify',{token:code,password:PASSWORD,display_name:'P'})).status,201);
  assert.equal((await request('login',{email:EMAIL,password:'x'.repeat(20000)})).status,413);
});

test('email delivery is rate limited without collecting an IP address',async()=>{
  for(let n=0;n<3;n++)assert.equal((await request('register',{email:EMAIL})).status,202);
  const blocked=await request('register',{email:EMAIL});assert.equal(blocked.status,429);
  assert.ok(Number(blocked.headers.get('retry-after'))>0);
  const keys=await store(['KEYS',prefix+'*']);assert.ok(keys.every(k=>!k.includes('127.0.0.1')&&!k.includes(EMAIL)));
});

test('expired verification, reset and session records cannot be reused',async()=>{
  await request('register',{email:EMAIL});const expiredVerify=outbox[0].token;
  for(const key of await store(['KEYS',prefix+'accounts:verify:*']))await store(['EXPIRE',key,'0']);
  assert.equal((await request('verify',{token:expiredVerify,password:PASSWORD,display_name:'P'})).status,400);
  await register();const session=await login();
  for(const key of await store(['KEYS',prefix+'accounts:session:*']))await store(['EXPIRE',key,'0']);
  assert.equal((await request('me',undefined,session)).status,401);
  await request('forgot-password',{email:EMAIL});const reset=outbox.findLast(m=>m.kind==='reset').token;
  for(const key of await store(['KEYS',prefix+'accounts:reset:*']))await store(['EXPIRE',key,'0']);
  assert.equal((await request('reset-password',{token:reset,password:NEW_PASSWORD})).status,400);
  await login();
});

test('concurrent reset codes have one winner and cannot restore stale credentials',async()=>{
  await register();await request('forgot-password',{email:EMAIL});await request('forgot-password',{email:EMAIL});
  const codes=outbox.filter(m=>m.kind==='reset').map(m=>m.token);
  const results=await Promise.all(codes.map(token=>request('reset-password',{token,password:NEW_PASSWORD})));
  assert.deepEqual(results.map(r=>r.status).sort(),[200,400]);
  await login(EMAIL,NEW_PASSWORD);
});

test('a password check completed before a reset cannot create a session afterwards',async()=>{
  await register();
  const key=(await store(['KEYS',prefix+'accounts:user:*']))[0];
  const stale=JSON.parse(await store(['GET',key]));
  const token=await login();await request('change-password',{current_password:PASSWORD,password:NEW_PASSWORD},token);
  const db=require('../account-store.cjs').create({upstashCmd:store,prefix,secret:'s'.repeat(64)});
  const result=await db.issueLoginChallenge(stale,'a'.repeat(43),{account_id:stale.id,version:1,expires_at:Date.now()+60000},60);
  assert.equal(result,0);assert.equal((await request('me',undefined,'lo_'+'a'.repeat(43))).status,401);
});

test('two accounts racing to link one Steam identity cannot both acquire it',async()=>{
  await register();await register('two@example.test');
  const first=await login(),second=await login('two@example.test');
  await steamSession();
  const proofs=await Promise.all([first,second].map(token=>startLink(token)));
  const results=await Promise.all([first,second].map((token,i)=>request('link-steam/verify',proofs[i],token)));
  assert.equal(results.filter(r=>r.status===200).length,1);
  assert.ok(results.every(r=>[200,401,409].includes(r.status)));
  const accounts=await Promise.all((await store(['KEYS',prefix+'accounts:user:*'])).map(async key=>JSON.parse(await store(['GET',key]))));
  assert.equal(accounts.filter(a=>a.steam_id===STEAM).length,1);
});

test('corrupt storage cannot leave an account partially linked',async()=>{
  await register();const token=await login();
  await steamSession();
  const proof=await startLink(token);
  await store(['LPUSH',prefix+'accounts:steam:'+STEAM,'bad-type']);
  assert.equal((await request('link-steam/verify',proof,token)).status,503);
  assert.equal((await request('me',undefined,token)).body.account.steam_id,null);
});

test('disconnect warns, requires its own email proof and preserves the Steam-first profile permanently',async()=>{
  await register();let token=await login();await steamSession();
  const linked=await link(token);assert.equal(linked.status,200);
  assert.equal(linked.body.account.player_id,STEAM);
  token=await login();const oldSteam=await steamSession('after-link');
  const historyKey=prefix+'comp:history:'+STEAM;
  await store(['SET',historyKey,JSON.stringify([{match_id:'prior-match',rank:1234}])]);
  const preview=await request('disconnect-steam', {password:PASSWORD},token);
  assert.equal(preview.status,202);
  assert.equal(preview.body.confirmation.required,true);
  assert.match(preview.body.confirmation.message,/stay with your Lights Out account/);
  const input={challenge:preview.body.challenge,code:outbox.findLast(m=>m.kind==='disconnect-steam').token};
  assert.equal((await request('disconnect-steam/verify',input,token)).status,400,'warning must be acknowledged');
  input.confirmation=preview.body.confirmation.id;
  const result=await request('disconnect-steam/verify',input,token);
  assert.equal(result.status,200);assert.equal(result.body.account.player_id,STEAM);
  assert.equal(result.body.account.steam_id,null);
  assert.equal(await store(['GET',historyKey]),JSON.stringify([{match_id:'prior-match',rank:1234}]));
  assert.equal(await auth.whoami(oldSteam),null);
  assert.equal((await request('/api/auth/me',undefined,oldSteam)).status,401);
  assert.equal((await request('me',undefined,token)).status,401);
  token=await login();assert.equal((await request('me',undefined,token)).body.account.player_id,STEAM);
  await steamSession('new-steam');
  const freshSteam=await request('/api/auth/me',undefined,'new-steam');
  assert.equal(freshSteam.status,200);
  assert.notEqual(freshSteam.body.player_id,STEAM,'a later Steam login cannot reclaim old progress');
  assert.notEqual(freshSteam.body.account_id,linked.body.account.id);
  auth=makeAuth({enabled:false});
  assert.equal((await request('/api/auth/me',undefined,oldSteam)).status,401,'disabling registration must not bypass revocation');
});

test('ownership codes are bound to action, exact parent session, Steam proof and account version',async()=>{
  await register();const first=await login(),other=await login();await steamSession();
  const proof=await startLink(first);
  assert.equal((await request('link-steam/verify',proof,other)).status,401);
  assert.equal((await request('disconnect-steam/verify',{...proof,confirmation:'disconnect-steam-v1'},first)).status,401);
  await steamSession('different-steam-proof','76561198000000002');
  assert.equal((await request('link-steam/verify',{...proof,steam_token:'different-steam-proof'},first)).status,401);
  assert.equal((await request('logout',{},first)).status,200);
  assert.equal((await request('link-steam/verify',proof,first)).status,401);
  assert.equal((await request('me',undefined,other)).body.account.steam_id,null);
});

test('ownership confirmation is one-use and cannot be brute-forced or started with a stale Steam login',async()=>{
  await register();let token=await login();await steamSession();
  const old=JSON.parse(await store(['GET',prefix+'auth:token:steam-proof']));old.created=Date.now()-601000;
  await store(['SET',prefix+'auth:token:steam-proof',JSON.stringify(old)]);
  assert.equal((await request('link-steam',{password:PASSWORD,steam_token:'steam-proof'},token)).status,401);
  await steamSession();const proof=await startLink(token);
  for(let n=0;n<5;n++)assert.equal((await request('link-steam/verify',{...proof,code:proof.code==='000000'?'999999':'000000'},token)).status,401);
  assert.equal((await request('link-steam/verify',proof,token)).status,401);
  const next=await startLink(token);
  const results=await Promise.all([1,2].map(()=>request('link-steam/verify',next,token)));
  assert.equal(results.filter(r=>r.status===200).length,1);
  token=await login();assert.ok([401,429].includes((await request('link-steam/verify',next,token)).status));
});

test('missing/stale ownership indexes cannot transfer progress or expose a disconnected account',async()=>{
  await register();let token=await login();await steamSession();assert.equal((await link(token)).status,200);
  token=await login();await steamSession();
  const account=(await request('me',undefined,token)).body.account;
  const index=prefix+'accounts:steam:'+STEAM;
  await store(['DEL',index]);
  assert.equal((await request('me',undefined,'steam-proof')).status,503);
  assert.equal((await request('/api/auth/me',undefined,'steam-proof')).status,503);
  assert.equal(await auth.whoami('steam-proof'),null);
  await store(['SET',index,account.id]);
  const started=(await request('disconnect-steam',{password:PASSWORD},token)).body;
  assert.equal((await request('disconnect-steam/verify',{challenge:started.challenge,
    code:outbox.findLast(m=>m.kind==='disconnect-steam').token,confirmation:started.confirmation.id},token)).status,200);
  await steamSession('fresh-after-disconnect');
  await store(['SET',index,account.id]);
  const exposed=await request('/api/auth/me',undefined,'fresh-after-disconnect');
  assert.equal(exposed.status,503);assert.ok(!JSON.stringify(exposed.body).includes(EMAIL));
  assert.equal((await request('me',undefined,'fresh-after-disconnect')).status,503);
});

test('pre-ledger optional links adopt their existing Steam profile once and can then disconnect',async()=>{
  const created=await register();let token=await login();
  const key=prefix+'accounts:user:'+created.id;
  const legacy=JSON.parse(await store(['GET',key]));delete legacy.player_id;delete legacy.profile_used;legacy.steam_id=STEAM;
  await store(['SET',key,JSON.stringify(legacy)]);await store(['SET',prefix+'accounts:steam:'+STEAM,created.id]);
  await steamSession('legacy-steam');
  assert.equal((await request('/api/auth/me',undefined,'legacy-steam')).status,401,'backfill does not upgrade an old Steam token');
  token=await login();
  const preview=await request('disconnect-steam',{password:PASSWORD},token);assert.equal(preview.status,202);
  const confirmed=await request('disconnect-steam/verify',{challenge:preview.body.challenge,
    code:outbox.findLast(m=>m.kind==='disconnect-steam').token,confirmation:preview.body.confirmation.id},token);
  assert.equal(confirmed.status,200);assert.equal(confirmed.body.account.player_id,STEAM);
});

test('malformed ledgers fail closed without normalizing ownership or consuming a valid action',async()=>{
  await register();let token=await login();await steamSession();assert.equal((await link(token)).status,200);
  token=await login();
  const preview=(await request('disconnect-steam',{password:PASSWORD},token)).body;
  const input={challenge:preview.challenge,code:outbox.findLast(m=>m.kind==='disconnect-steam').token,confirmation:preview.confirmation.id};
  const key=prefix+'accounts:steam-identity:'+STEAM;
  const original=await store(['GET',key]);
  for(const change of [{generation:0},{generation:'1'},{used:'yes'},{player_id:null},{state:'unknown'},
    {account_id:'00000000-0000-4000-8000-000000000000'}]) {
    const bad=JSON.stringify({...JSON.parse(original),...change});
    await store(['SET',key,bad]);
    assert.equal((await request('disconnect-steam/verify',input,token)).status,503);
    assert.equal(await store(['GET',key]),bad);
    assert.equal((await request('me',undefined,token)).body.account.steam_id,STEAM);
  }
  await store(['SET',key,original]);
  assert.equal((await request('disconnect-steam/verify',input,token)).status,200);
});

test('account sign-in alone does not enable unfinished ownership or gameplay integration',async()=>{
  auth=makeAuth({ownership:false,gameplay:false});await register();const token=await login();
  for(const action of ['link-steam','link-steam/verify','disconnect-steam','disconnect-steam/verify']) {
    const result=await request(action,{},token);assert.equal(result.status,503);assert.equal(result.body.code,'ownership_unavailable');
  }
  const result=await request('game/challenge',{},token);assert.equal(result.status,503);assert.equal(result.body.code,'gameplay_unavailable');
});

function withGameplay(options={}) {
  auth=makeAuth({gameplay:true,steamTicket:{key:'server-test-key',fetchImpl:async url=>{
    const input=new URL(url);
    assert.equal(input.searchParams.get('appid'),'2406770');
    assert.match(input.searchParams.get('identity'),/^[A-Za-z0-9]{24}$/);
    return Response.json({response:input.searchParams.get('ticket')==='ab'.repeat(128)?
      {params:{result:'OK',steamid:STEAM}}:{error:{errorcode:3}}});
  }},...options});
}
async function gameProof(token) {
  const started=await request('game/challenge',{},token);
  assert.equal(started.status,202,JSON.stringify(started.body));
  assert.match(started.body.identity,/^[A-Za-z0-9]{24}$/);
  const verified=await request('game/verify',{challenge:started.body.challenge,ticket:'ab'.repeat(128)},token);
  assert.equal(verified.status,200,JSON.stringify(verified.body));
  return verified.body;
}
test('game proof binds real Steam identity to the Lights Out player without linking or moving progress',async()=>{
  withGameplay();const account=await register();const token=await login();
  const verified=await gameProof(token);
  assert.match(verified.token,/^lg_[A-Za-z0-9_-]{43}$/);
  assert.equal(verified.player_id,account.id);assert.equal(verified.game_steam_id,STEAM);
  const me=await request('me',undefined,token);
  assert.equal(me.body.account.player_id,account.id);assert.equal(me.body.account.steam_id,null);
  assert.equal(await store(['GET',prefix+'accounts:steam:'+STEAM]),null);
  assert.equal(await store(['GET',prefix+'accounts:steam-identity:'+STEAM]),null);
  assert.equal((await request('me',undefined,verified.token)).status,401,'game proof cannot manage credentials');
  const keys=await store(['KEYS',prefix+'accounts:game*']);
  const stored=[];for(const key of keys)stored.push(await store(['GET',key]));
  assert.ok(!JSON.stringify(stored).includes(verified.token));assert.ok(!JSON.stringify(stored).includes('ab'.repeat(128)));
  assert.equal((await request('game/me',undefined,verified.token)).body.player_id,account.id);
  const gameplay=await auth.whoami(verified.token);
  assert.equal(gameplay?.player_id,account.id);
  assert.equal(gameplay?.game_steam_id,STEAM);
  assert.equal(gameplay?.auth_method,'lightsout');
  assert.equal(await auth.whoami(token),null,'a parent login alone is not a game identity');
  await request('logout',{},token);
  assert.equal((await request('game/me',undefined,verified.token)).status,401,'child credentials cannot outlive parent logout');
  assert.equal(await auth.whoami(verified.token),null);
});

test('game challenges are single-use, session-bound and invalidated by logout/password changes',async()=>{
  withGameplay();await register();const first=await login(),second=await login();
  const pending=(await request('game/challenge',{},first)).body;
  const input={challenge:pending.challenge,ticket:'ab'.repeat(128)};
  assert.equal((await request('game/verify',input,second)).status,401);
  assert.equal((await request('game/verify',{...input,ticket:'cd'.repeat(128)},first)).status,401);
  const results=await Promise.all([1,2].map(()=>request('game/verify',input,first)));
  assert.equal(results.filter(r=>r.status===200).length,1);
  const pending2=(await request('game/challenge',{},first)).body;
  await request('logout',{},first);
  assert.equal((await request('game/verify',{challenge:pending2.challenge,ticket:'ab'.repeat(128)},first)).status,401);
  const pending3=(await request('game/challenge',{},second)).body;
  const child=await gameProof(second);
  await request('change-password',{current_password:PASSWORD,password:NEW_PASSWORD},second);
  assert.equal((await request('game/verify',{challenge:pending3.challenge,ticket:'ab'.repeat(128)},second)).status,401);
  assert.equal((await request('game/me',undefined,child.token)).status,401);
});

test('temporary game proof is not progress and cannot block later adoption of a Steam-first profile',async()=>{
  withGameplay();await register();const token=await login();const child=await gameProof(token);await steamSession();
  const result=await link(token);
  assert.equal(result.status,200,JSON.stringify(result.body));
  assert.equal(result.body.account.player_id,STEAM);
  assert.equal((await request('game/me',undefined,child.token)).status,401,'profile association revokes prior child credentials');
});

test('lasting account credentials survive a private gameplay namespace change',async()=>{
  auth=makeAuth({accountPrefix:'lasting:',ownership:false});
  const account=await register();
  const token=await login(EMAIL,PASSWORD,true);
  assert.ok(await store(['GET','lasting:accounts:user:'+account.id]));
  assert.equal(await store(['GET',prefix+'accounts:user:'+account.id]),null);
  prefix='different-gameplay:';
  auth=makeAuth({accountPrefix:'lasting:',ownership:false});
  assert.equal((await request('me',undefined,token)).body.account.id,account.id);
  assert.equal((await request('me',undefined,await login())).body.account.id,account.id);
});

test('private email restriction prevents mail and account access for other addresses',async()=>{
  auth=makeAuth({allowEmail:email=>email===EMAIL});
  assert.equal((await request('register',{email:'outsider@example.test'})).status,202);
  assert.equal(outbox.length,0);
  await register();const token=await login();
  auth=makeAuth({allowEmail:()=>false});
  assert.equal((await request('me',undefined,token)).status,401);
  assert.equal((await request('login',{email:EMAIL,password:PASSWORD})).status,401);
});

test('private Steam login cannot select a linked account outside the invited email',async()=>{
  await register();const token=await login();await steamSession();assert.equal((await link(token)).status,200);
  await steamSession();
  auth=makeAuth({privateGameplay:true,allowEmail:()=>false});
  assert.equal((await request('/api/auth/me',undefined,'steam-proof')).status,401);
  assert.equal(await auth.whoami('steam-proof'),null);
  assert.equal(await auth.admitGameplay('steam-proof',{player_id:STEAM,game_steam_id:STEAM}),false);
});

test('private game admission preserves unused canonical ownership and rejects uninvited game identities',async()=>{
  withGameplay({privateGameplay:true,allowGame:id=>id===STEAM});const account=await register();const token=await login();
  const child=await gameProof(token),actor=await auth.whoami(child.token);
  assert.equal(await auth.admitGameplay(child.token,actor),true);
  assert.equal(JSON.parse(await store(['GET',prefix+'accounts:user:'+account.id])).profile_used,false);
  assert.equal(await store(['GET',prefix+'accounts:steam-identity:'+STEAM]),null);
  withGameplay({privateGameplay:true,allowGame:()=>false});
  assert.equal(await auth.whoami(child.token),null);assert.equal(await auth.admitGameplay(child.token,actor),false);
  const challenge=(await request('game/challenge',{},token)).body.challenge;
  assert.equal((await request('game/verify',{challenge,ticket:'ab'.repeat(128)},token)).status,403);
});

test('admitted gameplay permanently owns progress while proof alone remains unused',async()=>{
  withGameplay();const account=await register();const parent=await login();const child=await gameProof(parent);
  const key=prefix+'accounts:user:'+account.id;
  assert.equal(JSON.parse(await store(['GET',key])).profile_used,false);
  const actor=await auth.whoami(child.token);
  assert.equal(await auth.admitGameplay(child.token,actor),true);
  assert.equal(JSON.parse(await store(['GET',key])).profile_used,true);
  await steamSession();
  assert.equal((await link(parent)).body.code,'progress_conflict');
  await request('logout',{},parent);
  assert.equal(await auth.admitGameplay(child.token,actor),false);
});

test('fresh Steam progress after disconnect is marked durably and cannot take Lights Out progress',async()=>{
  withGameplay();await register();let parent=await login();await steamSession();await link(parent);
  parent=await login();
  const started=await request('disconnect-steam',{password:PASSWORD},parent);
  assert.equal((await request('disconnect-steam/verify',{challenge:started.body.challenge,
    code:outbox.findLast(m=>m.kind==='disconnect-steam').token,confirmation:'disconnect-steam-v1'},parent)).status,200);
  await steamSession();
  const actor=await auth.whoami('steam-proof');
  assert.notEqual(actor.player_id,STEAM);
  assert.equal(await auth.admitGameplay('steam-proof',actor),true);
  assert.equal(JSON.parse(await store(['GET',prefix+'accounts:steam-identity:'+STEAM])).used,true);
  parent=await login();assert.equal((await link(parent)).body.code,'progress_conflict');
});

test('malformed child credentials never return a game identity',async()=>{
  withGameplay();await register();const token=await login();const child=await gameProof(token);
  const key=(await store(['KEYS',prefix+'accounts:game-session:*']))[0];
  const original=await store(['GET',key]);
  for(const change of [{game_steam_id:'somebody-else'},{game_steam_id:76561198000000001},
    {version:'1'},{parent_hash:'bad'},{player_id:'bad'},{account_id:'bad'},
    {issued_at:Date.now()+30000},{expires_at:Date.now()+13*3600000}]) {
    await store(['SET',key,JSON.stringify({...JSON.parse(original),...change})]);
    assert.equal((await request('game/me',undefined,child.token)).status,401,JSON.stringify(change));
  }
  await store(['SET',key,original]);assert.equal((await request('game/me',undefined,child.token)).status,200);
});

test('password login limits survive router recreation and cannot be bypassed by email capitalization',async()=>{
  await register();
  for(let n=0;n<10;n++)assert.equal((await request('login',{email:EMAIL,password:'wrong'})).status,401);
  auth=makeAuth();assert.equal((await request('login',{email:EMAIL.toUpperCase(),password:PASSWORD})).status,429);
});

test('unsafe deployment configurations refuse account creation',async()=>{
  for(const options of [{durable:false},{secret:''},{origin:'http://accounts.example.test'},
    {origin:'https://accounts.example.test/path'},{sendMail:null}]) {
    auth=makeAuth(options);assert.equal((await request('register',{email:EMAIL})).status,503);
  }
});

test('account HTTP methods, malformed JSON and chunked oversize bodies fail cleanly',async()=>{
  assert.equal((await request('register')).status,405);
  assert.equal((await request('me',{})).status,405);
  assert.equal((await request('register',{},null,{body:'{'})).status,400);
  const result=await new Promise((resolve,reject)=>{
    const req=http.request(base+'/api/auth/account/register',{method:'POST',headers:{'content-type':'application/json'}},res=>{
      let body='';res.on('data',c=>body+=c);res.on('end',()=>resolve({status:res.statusCode,body}));
    });
    req.on('error',reject);req.write('{"email":"');req.write('a'.repeat(9000));req.end('"}');
  });
  assert.equal(result.status,413);assert.ok(JSON.parse(result.body).error);
});

test('mail failure never reveals whether an email exists through a different status',async()=>{
  await register();mailDown=true;
  for(const action of ['register','forgot-password']) {
    const known=await request(action,{email:EMAIL});
    const absent=await request(action,{email:'absent@example.test'});
    assert.equal(known.status,503);assert.equal(absent.status,503);
    assert.deepEqual(known.body,absent.body);
  }
});

test('a changed account secret fails closed instead of hiding accounts or creating duplicates',async()=>{
  await register();const token=await login();auth=makeAuth({secret:'different-secret-'.repeat(4)});
  assert.equal((await request('login',{email:EMAIL,password:PASSWORD})).status,503);
  assert.equal((await request('register',{email:EMAIL})).status,503);
  assert.equal((await request('me',undefined,token)).status,503);
  auth=makeAuth();assert.equal((await request('me',undefined,token)).status,200);
});

test('corrupt ownership indexes fail closed instead of bypassing account authorization',async()=>{
  await store(['SET',prefix+'auth:token:steam-proof',JSON.stringify({steam_id:STEAM}),'EX','300']);
  await store(['LPUSH',prefix+'accounts:steam:'+STEAM,'bad-type']);
  const me=await request('/api/auth/me',undefined,'steam-proof');
  assert.equal(me.status,503);assert.equal(me.body.steam_id,undefined);
  assert.equal(me.body.account_id,undefined);
});

test('password alone never issues a session and the emailed login code is single-use',async()=>{
  await register();const started=await request('login',{email:EMAIL,password:PASSWORD});
  assert.equal(started.status,202);assert.equal(started.body.status,'verification_required');
  assert.equal(started.body.token,undefined);assert.match(started.body.challenge,/^[A-Za-z0-9_-]{43}$/);
  const code=outbox.findLast(m=>m.kind==='login').token;assert.match(code,/^\d{6}$/);
  assert.equal((await request('me',undefined,started.body.challenge)).status,401);
  assert.equal((await request('login/verify',{challenge:started.body.challenge,code:'000000'===code?'111111':'000000'})).status,401);
  const verified=await request('login/verify',{challenge:started.body.challenge,code});
  assert.equal(verified.status,200);assert.equal(verified.body.remember_me,false);assert.equal(verified.body.expires_in,43200);
  assert.equal((await request('login/verify',{challenge:started.body.challenge,code})).status,401);
  assert.equal((await request('me',undefined,verified.body.token)).status,200);
});

test('five wrong login codes exhaust the challenge, and expired/reset-invalidated challenges fail',async()=>{
  await register();let started=await request('login',{email:EMAIL,password:PASSWORD});
  let code=outbox.findLast(m=>m.kind==='login').token;
  for(let i=0;i<5;i++)assert.equal((await request('login/verify',{challenge:started.body.challenge,code:code==='000000'?'111111':'000000'})).status,401);
  assert.equal((await request('login/verify',{challenge:started.body.challenge,code})).status,401);
  started=await request('login',{email:EMAIL,password:PASSWORD});code=outbox.findLast(m=>m.kind==='login').token;
  for(const key of await store(['KEYS',prefix+'accounts:login:*']))await store(['EXPIRE',key,'0']);
  assert.equal((await request('login/verify',{challenge:started.body.challenge,code})).status,401);
  started=await request('login',{email:EMAIL,password:PASSWORD});code=outbox.findLast(m=>m.kind==='login').token;
  await request('forgot-password',{email:EMAIL});const reset=outbox.findLast(m=>m.kind==='reset').token;
  assert.equal((await request('reset-password',{token:reset,password:NEW_PASSWORD})).status,200);
  assert.equal((await request('login/verify',{challenge:started.body.challenge,code})).status,401);
});

test('remembered sessions have no time expiry, sign out per device, and password changes revoke all',async()=>{
  await register();const first=await login(EMAIL,PASSWORD,true),second=await login(EMAIL,PASSWORD,true);
  const sessionKeys=await store(['KEYS',prefix+'accounts:session:*']);
  for(const key of sessionKeys) {
    assert.equal(await store(['TTL',key]),-1);
    const record=JSON.parse(await store(['GET',key]));assert.equal(record.remember_me,true);assert.equal(record.expires_at,null);
  }
  assert.equal((await request('logout',{},first)).status,200);
  assert.equal((await request('me',undefined,first)).status,401);
  assert.equal((await request('me',undefined,second)).status,200);
  assert.equal((await request('change-password',{current_password:PASSWORD,password:NEW_PASSWORD},second)).status,200);
  assert.equal((await request('me',undefined,second)).status,401);
  assert.equal((await store(['KEYS',prefix+'accounts:session:*'])).length,0);
});

test('a code cannot change the remember choice or authenticate a different challenge',async()=>{
  await register();const started=await request('login',{email:EMAIL,password:PASSWORD,remember_me:false});
  const code=outbox.findLast(m=>m.kind==='login').token;
  assert.equal((await request('login/verify',{challenge:'x'.repeat(43),code})).status,401);
  const results=await Promise.all([1,2].map(()=>request('login/verify',{challenge:started.body.challenge,code,remember_me:true})));
  assert.deepEqual(results.map(r=>r.status).sort(),[200,401]);
  const verified=results.find(r=>r.status===200);assert.equal(verified.body.remember_me,false);
  const keys=await store(['KEYS',prefix+'accounts:session:*']);assert.equal(keys.length,1);assert.ok(await store(['TTL',keys[0]])>0);
});

test('pending login codes are not stored in plaintext and a delivery failure never issues a session',async()=>{
  await register();let started=await request('login',{email:EMAIL,password:PASSWORD});
  const code=outbox.findLast(m=>m.kind==='login').token;
  const keys=await store(['KEYS',prefix+'accounts:login:*']);assert.equal(keys.length,1);
  const raw=await store(['GET',keys[0]]),record=JSON.parse(raw);
  assert.ok(!raw.includes(started.body.challenge));assert.ok(!Object.values(record).includes(code));
  assert.match(record.code_hash,/^[a-f0-9]{64}$/);assert.equal((await store(['KEYS',prefix+'accounts:session:*'])).length,0);
  mailDown=true;started=await request('login',{email:EMAIL,password:PASSWORD,remember_me:true});
  assert.equal(started.status,503);assert.equal(started.body.token,undefined);
  assert.equal((await store(['KEYS',prefix+'accounts:login:*'])).length,1);
  assert.equal((await store(['KEYS',prefix+'accounts:session:*'])).length,0);
});

test('remembered-session capacity is bounded and password recovery deletes all remembered sessions',async()=>{
  await register();
  const key=(await store(['KEYS',prefix+'accounts:user:*']))[0],account=JSON.parse(await store(['GET',key]));
  const db=require('../account-store.cjs').create({upstashCmd:store,prefix,secret:'s'.repeat(64)});
  const security=require('../account-security.cjs');const sessions=[];
  for(let index=0;index<21;index++) {
    const challenge=security.randomToken(),token='lo_'+security.randomToken();
    const hash=security.codeDigest('s'.repeat(64),challenge,'123456');
    await db.issueLoginChallenge(account,challenge,{account_id:account.id,version:1,remember_me:true,code_hash:hash,
      attempts:0,expires_at:Date.now()+600000},600);
    const result=await db.completeLogin(challenge,account,hash,token,{account_id:account.id,version:1,
      remember_me:true,expires_at:null},43200);
    assert.equal(result,index<20?1:-2);if(result===1)sessions.push(token);
  }
  assert.equal((await store(['KEYS',prefix+'accounts:session:*'])).length,20);
  const started=await request('login',{email:EMAIL,password:PASSWORD,remember_me:true});
  const loginCode=outbox.findLast(m=>m.kind==='login').token;
  assert.equal((await request('login/verify',{challenge:started.body.challenge,code:loginCode})).status,409);
  assert.equal((await request('logout',{},sessions[0])).status,200);
  const retried=await request('login/verify',{challenge:started.body.challenge,code:loginCode});
  assert.equal(retried.status,200);assert.equal(retried.body.remember_me,true);
  assert.equal((await store(['KEYS',prefix+'accounts:session:*'])).length,20);
  await request('forgot-password',{email:EMAIL});const code=outbox.findLast(m=>m.kind==='reset').token;
  assert.equal((await request('reset-password',{token:code,password:NEW_PASSWORD})).status,200);
  assert.equal((await store(['KEYS',prefix+'accounts:session:*'])).length,0);
  assert.equal((await request('me',undefined,sessions[0])).status,401);
});
