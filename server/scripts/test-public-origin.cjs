'use strict';
// Run: node --test server/scripts/test-public-origin.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const authModule = require('../auth.cjs');
const adminModule = require('../admin.cjs');

function environment(t, values) {
  const names = ['HUB_PUBLIC_ORIGIN','HUB_ACCOUNT_ORIGIN','HUB_RECORDING','HUB_RECORDING_ORIGIN','NODE_ENV','HUB_ACCOUNTS_ENABLED'];
  const saved = Object.fromEntries(names.map(name=>[name,process.env[name]]));
  for(const name of names) delete process.env[name];
  Object.assign(process.env,{NODE_ENV:'test',HUB_ACCOUNTS_ENABLED:'0'},values);
  t.after(()=>{for(const name of names) {
    if(saved[name]===undefined) delete process.env[name]; else process.env[name]=saved[name];
  }});
}
function services() {
  const records=new Map();
  const upstashCmd=async([op,key,value])=>{
    if(op==='SET'){records.set(key,value);return 'OK';}
    if(op==='GET')return records.get(key)||null;
    throw Error('Unexpected command');
  };
  const auth = authModule.create({upstashCmd,prefix:'origin-test:'+Math.random()+':',
    sendJson:(res,status,data)=>{res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify(data));},
    badRequest:res=>{res.writeHead(400);res.end();}});
  const admin = adminModule.create({upstashCmd,live:()=>({})});
  return {auth,admin};
}
async function listen(t) {
  const {auth,admin}=services();
  const server=http.createServer(async(req,res)=>{
    const url=new URL(req.url,'http://local');
    if(await auth.route(req,res,req.method,url.pathname,url)) return;
    if(await admin.route(req,res,req.method,url.pathname,url)) return;
    res.writeHead(404);res.end();
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));});
  return 'http://127.0.0.1:'+server.address().port;
}
async function checkCallbacks(t, expected) {
  const base=await listen(t);
  const headers={host:'lightsout.up.railway.app','x-lightsout-request-host':new URL(expected).host,
    'x-forwarded-host':'attacker.example, lightsoutranked.com','x-forwarded-proto':'http'};
  const start=await(await fetch(base+'/api/auth/start',{headers})).json();
  assert.equal(start.url,expected+'/auth/steam/start?code='+start.code);
  for(const [path,callback] of [['/auth/steam/start?code='+start.code,'/auth/steam/return?code='+start.code],['/admin/login','/admin/return']]) {
    const response=await fetch(base+path,{headers,redirect:'manual'});
    assert.equal(response.status,302);
    const target=new URL(response.headers.get('location'));
    assert.equal(target.origin,'https://steamcommunity.com');
    assert.equal(target.searchParams.get('openid.realm'),expected);
    const returnTo=new URL(target.searchParams.get('openid.return_to'));
    if(path==='/admin/login') {
      assert.equal(returnTo.origin+returnTo.pathname,expected+callback);
      const state=returnTo.searchParams.get('state');
      assert.match(state,/^[a-f0-9]{64}$/);
      assert.equal(response.headers.get('set-cookie'),`hubadmin_login=${state}; Path=/admin; HttpOnly; Secure; SameSite=Lax; Max-Age=600`);
    } else assert.equal(returnTo.href,expected+callback);
    assert.equal(response.headers.get('cache-control'),'no-store');
  }
}
test('explicit public callback origin survives proxy Host and forged forwarding headers',async t=>{
  environment(t,{HUB_PUBLIC_ORIGIN:'https://lightsoutranked.com'});
  await checkCallbacks(t,'https://lightsoutranked.com');
});
test('production callback default remains on the owned website',async t=>{
  environment(t,{NODE_ENV:'production'});
  await checkCallbacks(t,'https://lightsoutranked.com');
});
test('an admin login bookmark on an alias redirects before setting browser state',async t=>{
  environment(t,{HUB_PUBLIC_ORIGIN:'https://lightsoutranked.com'});
  const base=await listen(t);
  const alias=await fetch(base+'/admin/login',{redirect:'manual',headers:{host:'lightsout.up.railway.app',
    'x-lightsout-request-host':'play.lightsoutranked.com'}});
  assert.equal(alias.status,302);
  assert.equal(alias.headers.get('location'),'https://lightsoutranked.com/admin/login');
  assert.equal(alias.headers.get('set-cookie'),null);
  const canonical=await fetch(base+'/admin/login',{redirect:'manual',headers:{host:'lightsout.up.railway.app',
    'x-lightsout-request-host':'lightsoutranked.com'}});
  assert.equal(new URL(canonical.headers.get('location')).origin,'https://steamcommunity.com');
  assert.match(canonical.headers.get('set-cookie'),/^hubadmin_login=/);
});
test('existing canonical account origin also directs Steam callbacks',async t=>{
  environment(t,{HUB_ACCOUNT_ORIGIN:'https://accounts.example.test'});
  await checkCallbacks(t,'https://accounts.example.test');
});
test('private recording callbacks cannot inherit the public origin',async t=>{
  environment(t,{HUB_RECORDING:'1',HUB_RECORDING_ORIGIN:'https://lorecord1.up.railway.app',HUB_PUBLIC_ORIGIN:'https://lightsoutranked.com'});
  await checkCallbacks(t,'https://lorecord1.up.railway.app');
});
test('invalid explicit callback configuration fails before serving sign-in links',t=>{
  environment(t,{});
  for(const origin of ['http://lightsoutranked.com','https://user:pass@lightsoutranked.com','https://lightsoutranked.com/','https://lightsoutranked.com/path','https://lightsoutranked.com?x=1','https://lightsoutranked.com#x','not a url','']) {
    process.env.HUB_PUBLIC_ORIGIN=origin;
    assert.throws(services,/origin/i,JSON.stringify(origin));
  }
});
test('private mode refuses a missing private callback origin',t=>{
  environment(t,{HUB_RECORDING:'1',HUB_PUBLIC_ORIGIN:'https://lightsoutranked.com'});
  assert.throws(services,/origin/i);
});


test('private catalogue keeps all owned proxy URLs behind its download boundary', () => {
  const key = 'a'.repeat(48);
  const origin = 'https://private-fixture.up.railway.app';
  const profile = require('../recording.cjs').config({ HUB_RECORDING: '1',
    HUB_STORE_PREFIX: 'recording:fixture:', COMP_PRIVATE_STEAM_IDS: '76561198000000001,76561198000000002',
    HUB_RECORDING_DOWNLOAD_KEY: key, HUB_RECORDING_ORIGIN: origin });
  for (const host of ['lightsout.up.railway.app', 'play.lightsoutranked.com', 'lightsoutranked.com', 'www.lightsoutranked.com']) {
    const result = JSON.parse(profile.catalogue(JSON.stringify({url: 'https://' + host + '/packs/test.zip'})));
    assert.equal(result.url, origin + '/private/' + key + '/packs/test.zip');
  }
});
