// Local UI fixture: node tests/test_password_recovery_browser.cjs (Playwright in NODE_PATH).
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.i18n import STRINGS; from hub.webui.snapshot import state_snapshot,strings_for; p,s=_web_panel(); print(json.dumps({"state":state_snapshot(s,p),"languages":{l:strings_for(l) for l in STRINGS}}))'],{cwd:root,encoding:'utf8'}));
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
 const page=await browser.newPage(),calls=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 const s=fixture.state;s.view='competitive';s.update=null;s.gamemode_update=null;s.auth.signed_in=false;s.auth.phase='signed_out';s.comp.phase='idle';s.comp.error='';
 await page.route('http://hub.test/**',r=>{const u=new URL(r.request().url());if(u.pathname==='/state')return r.fulfill({json:s});if(u.pathname==='/events')return r.fulfill({json:[]});if(u.pathname.startsWith('/verb/')){calls.push(r.request().postDataJSON());return r.fulfill({json:{ok:true}});}const file=path.join(root,'hub/webui/static',u.pathname==='/'?'index.html':u.pathname);return fs.existsSync(file)?r.fulfill({path:file}):r.fulfill({status:404,body:''});});
 const submit=()=>Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/account_action')),page.locator('.account-form button[type=submit]').click()]);
 for(const [lang,strings] of Object.entries(fixture.languages))for(const width of [800,1280]){
  s.lang=lang;s.strings=strings;s.auth.account_step='';s.auth.account_busy=false;s.comp.error='';
  await page.setViewportSize({width,height:900});await page.goto('http://hub.test/');
  await page.locator('.account-choices button').nth(1).click();
  await page.locator('.account-forgot').click();
  assert.equal(await page.locator('.account-form input[type=password]').count(),0,'No password before email verification');
  await page.locator('.account-form input[name=email]').fill('fixture@example.test');await submit();
  assert.equal(calls.at(-1)[0],'forgot-password');
  s.auth.account_step='recovery_code';await page.evaluate(s=>__hub.onState(s),s);
  assert.equal(await page.locator('.account-form input[type=password]').count(),0);
  // Choosing Lights Out again cancels recovery instead of leaving an orphaned code form.
  if(lang==='en'&&width===800){
   await Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/account_action')),page.locator('.account-choices button').nth(1).click()]);
   assert.equal(calls.at(-1)[0],'cancel');assert.equal(await page.locator('.account-form input[name=password]').count(),1);
   s.auth.account_step='';await page.evaluate(s=>__hub.onState(s),s);await page.locator('.account-forgot').click();
   await page.locator('.account-form input[name=email]').fill('fixture@example.test');await submit();
   s.auth.account_step='recovery_code';await page.evaluate(s=>__hub.onState(s),s);
  }
  await page.locator('.account-form input[name=code]').fill('123456');await submit();
  assert.equal(calls.at(-1)[0],'forgot-password/verify');assert.equal(calls.at(-1)[1].code,'123456');
  assert.equal(await page.locator('.account-form input[name=code]').inputValue(),'');
  s.comp.error=strings.account_recovery_invalid;await page.evaluate(s=>__hub.onState(s),s);
  assert.equal(await page.locator('.account-form [role=alert]').count(),1);
  s.auth.account_step='reset_password';s.comp.error='';await page.evaluate(s=>__hub.onState(s),s);
  await page.locator('.account-form input[name=password]').fill('new-password');
  await page.locator('.account-form input[name=confirm_password]').fill('different');
  const count=calls.length;await page.locator('.account-form button[type=submit]').click();assert.equal(calls.length,count);
  await page.locator('.account-form input[name=confirm_password]').fill('new-password');
  s.unrelatedTick=(s.unrelatedTick||0)+1;await page.evaluate(s=>__hub.onState(s),s);
  assert.equal(await page.locator('.account-form input[name=password]').inputValue(),'new-password');
  assert.equal(await page.locator('.account-form input[name=password]').getAttribute('autocomplete'),'new-password');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,lang+' fits');
  if(lang==='en'&&width===800){fs.mkdirSync(path.join(root,'work'),{recursive:true});await page.screenshot({path:path.join(root,'work/recovery-password.png')});}
  await submit();assert.equal(calls.at(-1)[0],'reset-password');assert.equal(calls.at(-1)[1].password,'new-password');
  assert.equal(await page.locator('.account-form input[name=password]').inputValue(),'');
  s.auth.account_step='login';s.comp.error=strings.account_password_reset;await page.evaluate(s=>__hub.onState(s),s);
  assert.equal(await page.locator('.account-forgot').count(),1);
  await page.locator('.account-forgot').click();assert.equal(await page.locator('.account-form input[type=password]').count(),0);
 }
 assert.deepEqual(errors,[]);console.log('Recovery UI passed: seven languages, 800/1280 widths, email/code/password separation, mismatch, retry, snapshot preservation and secret clearing.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
