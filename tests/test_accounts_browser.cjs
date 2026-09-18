// Run with Playwright in NODE_PATH and HUB_TEST_PYTHON set. Uses local files and mocked account replies only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.i18n import STRINGS; from hub.webui.snapshot import state_snapshot, strings_for; p,s=_web_panel(); print(json.dumps({"state":state_snapshot(s,p),"languages":{l:strings_for(l) for l in STRINGS}}))'],{cwd:root,encoding:'utf8'}));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage(),errors=[],requests=[],verbs=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://account.test/**',route=>{
   const u=new URL(route.request().url());
   if(u.pathname.startsWith('/api/auth/account/')){requests.push({url:u.pathname,body:route.request().postDataJSON()});return route.fulfill({status:u.pathname.endsWith('/register')?202:201,json:{ok:true}});}
   const file=path.join(root,'server/public',u.pathname==='/account'?'account.html':u.pathname);
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  for(const lang of Object.keys(fixture.languages))for(const width of [390,1280]){
   await page.setViewportSize({width,height:900});await page.goto('http://account.test/account');await page.selectOption('#language',lang);
   assert.equal(await page.locator('html').getAttribute('lang'),lang);
   assert.equal(await page.locator('.account-policies a').count(),2);
   assert.ok((await page.locator('[data-copy=game]').textContent()).includes('Steam'));
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,lang+' mobile horizontal overflow');
   await page.fill('#email','fixture@example.test');await page.locator('#email-form button[type=submit]').click();await page.waitForSelector('#verify-form:visible');
   await page.fill('#code','a'.repeat(43));await page.fill('#display-name','Fixture');await page.fill('#password','abc123');await page.fill('#confirm-password','abc123');await page.locator('#verify-form button[type=submit]').click();await page.waitForSelector('#complete:visible');
   assert.equal(await page.inputValue('#password'),'');assert.equal(await page.inputValue('#code'),'');
  }
  const s=fixture.state;s.view='competitive';s.update=null;s.gamemode_update=null;s.auth.signed_in=false;s.auth.phase='signed_out';s.comp.phase='idle';s.comp.error='';
  await page.route('http://hub.test/**',route=>{
   const u=new URL(route.request().url());
   if(u.pathname==='/state')return route.fulfill({json:s});if(u.pathname==='/events')return route.fulfill({json:[]});
   if(u.pathname.startsWith('/verb/')){verbs.push({name:u.pathname.split('/').pop(),body:route.request().postData()});return route.fulfill({json:{ok:true}});}
   const file=path.join(root,'hub/webui/static',u.pathname==='/'?'index.html':u.pathname);
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.setViewportSize({width:1280,height:900});await page.goto('http://hub.test/');
  for(const [lang,strings] of Object.entries(fixture.languages)){
   s.lang=lang;s.strings=strings;await page.evaluate(s=>__hub.onState(s),s);await page.waitForSelector('.account-policies button');
   assert.equal(await page.locator('.account-disclosure').textContent(),strings.account_game_privacy);
   await Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/account_policy')),page.locator('.account-policies button').first().click()]);
   assert.equal(verbs.at(-1).name,'account_policy');
   const dims=await page.locator('.account-choices button').evaluateAll(nodes=>nodes.map(n=>({w:n.offsetWidth,h:n.offsetHeight})));
   assert.deepEqual(dims[0],dims[1],lang+' equal sign-in button dimensions');
  }
  assert.equal(requests.length,28);assert.deepEqual(errors,[]);
  console.log('Account browser checks passed: website creation across 7 languages at mobile/desktop widths; no live mail; app disclosure, policy action and equal sign-in buttons in all 7 languages.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
