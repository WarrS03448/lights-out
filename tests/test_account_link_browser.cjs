// Local browser fixtures only; no accounts, mail or external authentication.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.i18n import STRINGS; from hub.webui.snapshot import state_snapshot,strings_for; from hub.webui.screens.settings import _strings; p,s=_web_panel(); print(json.dumps({"state":state_snapshot(s,p),"languages":{l:{"core":strings_for(l),"settings":_strings(l)} for l in STRINGS}}))'],{cwd:root,encoding:'utf8'}));
(async()=>{const browser=await chromium.launch({headless:true,channel:'msedge'});try{
 const page=await browser.newPage(),calls=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 const s=fixture.state;s.view='settings';s.update=null;s.gamemode_update=null;s.auth.signed_in=true;
 s.settings.account={signed_in:true,persona:'Steam Player',steam_id_masked:'7656119…0001',locked:false,linked:false,login_method:'steam',link:{stage:'',busy:false,error:'',can_connect:true,email:'fixture@example.test',steam_id:'76561198000000001'}};
 await page.route('http://hub.test/**',r=>{const u=new URL(r.request().url());if(u.pathname==='/state')return r.fulfill({json:s});if(u.pathname==='/events')return r.fulfill({json:[]});if(u.pathname.startsWith('/verb/')){calls.push({name:u.pathname.split('/').pop(),body:r.request().postDataJSON()});return r.fulfill({json:{ok:true}});}const file=path.join(root,'hub/webui/static',u.pathname==='/'?'index.html':u.pathname);return fs.existsSync(file)?r.fulfill({path:file}):r.fulfill({status:404,body:''});});
 await page.goto('http://hub.test/');await page.waitForSelector('.settings');
 for(const [lang,copy] of Object.entries(fixture.languages)){
  s.lang=lang;s.strings=copy.core;s.settings.strings=copy.settings;
  for(const width of [800,1280]){
   await page.setViewportSize({width,height:900});s.settings.account.link.stage='';await page.evaluate(s=>__hub.onState(s),s);
   assert.equal(await page.locator('.set-account-link').count(),1,lang+' Connect belongs under account');
   await Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/settings_account_link')),page.locator('.set-account-link').click()]);assert.equal(calls.at(-1).body[0],'start');
   for(const stage of ['login','login_code','steam','password','confirm','done','forgot_password','recovery_code','reset_password']){
    s.settings.account.link.stage=stage;await page.evaluate(s=>__hub.onState(s),s);const modal=page.locator('.account-link-modal');await modal.waitFor();
    const box=await modal.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1,lang+' '+stage+' fits');
    assert.ok((await modal.textContent()).includes(['forgot_password','recovery_code','reset_password'].includes(stage)?copy.core.account_reset_title:copy.settings.link_title));
    if(stage==='login'||stage==='password'){
     await Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/settings_account_link')),modal.locator('.settings-account-forgot').click()]);
     assert.equal(calls.at(-1).body[0],'recover');
    }
    if(stage==='forgot_password'||stage==='recovery_code')assert.equal(await modal.locator('input[type=password]').count(),0);
    if(stage==='reset_password'){
     await modal.locator('input[name=password]').fill('new-password');await modal.locator('input[name=confirm_password]').fill('wrong');
     const before=calls.length;await modal.locator('button[type=submit]').click();assert.equal(calls.length,before);
     await modal.locator('input[name=confirm_password]').fill('new-password');
     await Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/settings_account_link')),modal.locator('button[type=submit]').click()]);
     assert.equal(calls.at(-1).body[0],'reset-password');assert.equal(calls.at(-1).body[1].password,'new-password');
     assert.equal(await modal.locator('input[name=password]').inputValue(),'');
    }
    if(stage==='login'){
     await modal.locator('input[name=email]').fill('fixture@example.test');await modal.locator('input[name=password]').fill('secret123');
     s.unrelatedTick=(s.unrelatedTick||0)+1;await page.evaluate(s=>__hub.onState(s),s);assert.equal(await page.locator('.account-link-modal input[name=password]').inputValue(),'secret123');
     await Promise.all([page.waitForResponse(r=>r.url().endsWith('/verb/settings_account_link')),page.locator('.account-link-modal button[type=submit]').click()]);
     assert.equal(calls.at(-1).body[0],'login');assert.equal(calls.at(-1).body[1].password,'secret123');
     assert.equal(await page.locator('.account-link-modal input[name=password]').inputValue(),'');
    }
   }
  }
 }
 s.settings.account.link.stage='confirm';s.settings.account.link.error='progress_conflict';s.settings.account.link.busy=false;await page.evaluate(s=>__hub.onState(s),s);assert.equal(await page.locator('.account-link-modal [role=alert]').count(),1);
 s.settings.account.link.stage='';s.settings.account.link.error='';s.settings.account.linked=true;s.settings.account.email='fixture@example.test';await page.evaluate(s=>__hub.onState(s),s);assert.equal(await page.locator('.set-account-link').count(),0);
 assert.deepEqual(errors,[]);console.log('Settings account linking browser checks passed: all7languages,800/1280widths,6steps,request payload,refresh-safe fields,secret clearing,conflict andconnected states.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
