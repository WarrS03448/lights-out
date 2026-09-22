'use strict';
const {test,before,after} = require('node:test');
const assert = require('node:assert/strict');
process.env.NODE_ENV='test';
process.env.HUB_ACCOUNTS_ENABLED='0';
delete process.env.UPSTASH_REDIS_REST_URL;
delete process.env.UPSTASH_REDIS_REST_TOKEN;
delete process.env.HUB_TEST_TOKENS;
delete process.env.HUB_ACCOUNT_ORIGIN;
const analytics=require('../analytics.cjs').create();
const server=require('../server.cjs').createServer({analyticsService:analytics});
let base;
before(async()=>{await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));base='http://127.0.0.1:'+server.address().port;});
after(async()=>{await analytics.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));});

test('production HTTP router reaches every account endpoint and keeps them disabled by default',async()=>{
  for(const action of ['register','verify','login','login/verify','logout','forgot-password','forgot-password/verify','reset-password','change-password',
    'link-steam','link-steam/verify','disconnect-steam','disconnect-steam/verify','game/challenge','game/verify']) {
    const response=await fetch(base+'/api/auth/account/'+action,{method:'POST',headers:{'content-type':'application/json'},body:'{}'});
    assert.equal(response.status,503,action);assert.equal((await response.json()).code,'accounts_unavailable');
    assert.equal(response.headers.get('cache-control'),'no-store');
  }
  assert.equal((await fetch(base+'/api/auth/account/me')).status,503);
  assert.equal((await fetch(base+'/api/auth/account/game/me')).status,503);
});

test('legacy Steam auth still starts and no unverified account can reach the competitive queue',async()=>{
  const start=await fetch(base+'/api/auth/start');assert.equal(start.status,200);
  const link=await start.json();assert.ok(link.url.includes('/auth/steam/start?code='));
  const redirect=await fetch(base+'/auth/steam/start?code='+link.code,{redirect:'manual'});
  assert.equal(redirect.status,302);assert.equal(new URL(redirect.headers.get('location')).hostname,'steamcommunity.com');
  const response=await fetch(base+'/api/queue/join',{method:'POST',headers:{authorization:'Bearer lo_'+'a'.repeat(43)}});
  assert.equal(response.status,401);
});

test('account page and its scripts are reachable with private form security headers',async()=>{
  for(const route of ['/account','/account.html']) {
    const response=await fetch(base+route);assert.equal(response.status,200);
    assert.equal(response.headers.get('cache-control'),'no-store');
    assert.equal(response.headers.get('referrer-policy'),'no-referrer');
    assert.equal(response.headers.get('x-content-type-options'),'nosniff');
    assert.match(response.headers.get('content-security-policy'),/frame-ancestors 'none'/);
    const html=await response.text();assert.match(html,/id="email-form"/);assert.match(html,/id="verify-form"/);
    for(const script of html.matchAll(/<script src="([^"]+)"/g)) {
      const asset=await fetch(base+script[1]);assert.equal(asset.status,200);
      assert.match(asset.headers.get('content-type'),/^text\/javascript/);
      assert.ok((await asset.text()).length>100);
    }
    const head=await fetch(base+route,{method:'HEAD'});assert.equal(head.status,200);
    assert.equal(head.headers.get('content-length'),String(Buffer.byteLength(html)));
    assert.equal(await head.text(),'');
  }
  for(const route of ['/','/ranked','/how','/faq','/about','/privacy','/terms','/cookies','/notices'])
    assert.match(await (await fetch(base+route)).text(),/href="\/account"/);
});

test('website aliases redirect to the configured account origin before credentials are entered',async()=>{
  process.env.HUB_ACCOUNT_ORIGIN='https://accounts.example.test';
  try {
    const response=await fetch(base+'/account?irrelevant=discarded',{redirect:'manual'});
    assert.equal(response.status,302);assert.equal(response.headers.get('location'),'https://accounts.example.test/account');
    assert.equal(response.headers.get('cache-control'),'no-store');
  } finally {delete process.env.HUB_ACCOUNT_ORIGIN;}
});

test('canonical account page behind Bunny does not redirect to itself; aliases still redirect',async()=>{
  process.env.HUB_ACCOUNT_ORIGIN='https://lightsoutranked.com';
  try {
    for (const route of ['/account','/account.html']) {
      const response=await fetch(base+route,{redirect:'manual',headers:{
        host:'lightsout.up.railway.app','x-lightsout-request-host':'lightsoutranked.com'}});
      assert.equal(response.status,200);
      assert.equal(response.headers.get('cache-control'),'no-store');
      assert.match(await response.text(),/id="email-form"/);
    }
    for (const forwarded of ['www.lightsoutranked.com','play.lightsoutranked.com','attacker.example','lightsoutranked.com, attacker.example']) {
      const response=await fetch(base+'/account',{redirect:'manual',headers:{
        host:'lightsout.up.railway.app','x-lightsout-request-host':forwarded}});
      assert.equal(response.status,302);
      assert.equal(response.headers.get('location'),'https://lightsoutranked.com/account');
    }
  } finally {delete process.env.HUB_ACCOUNT_ORIGIN;}
});
