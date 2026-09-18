// Run with Playwright in NODE_PATH and HUB_TEST_PYTHON set to the test interpreter.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
  'import json; from tests.test_hub import _web_panel; from hub.i18n import STRINGS; from hub.webui.snapshot import state_snapshot, strings_for; p,s=_web_panel(); print(json.dumps({"state":state_snapshot(s,p),"languages":{lang:strings_for(lang) for lang in STRINGS}}))'],{cwd:root,encoding:'utf8'}));
const state=fixture.state;
state.view='leaderboard';state.update=null;state.gamemode_update=null;
state.auth.signed_in=true;state.auth.steam_id='76561198000000001';
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage(),calls=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://hub.test/**',route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/state')return route.fulfill({json:state});
   if(url.pathname==='/events')return route.fulfill({json:[]});
   if(url.pathname.startsWith('/verb/')){calls.push(url.pathname.split('/').pop());return route.fulfill({json:{ok:true}});}
   const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  const refreshes=()=>calls.filter(c=>c==='leaderboard_refresh').length;
  await page.goto('http://hub.test/');await page.waitForSelector('.leaderboard');
  await page.waitForTimeout(150);assert.equal(refreshes(),1);
  state.auth.signed_in=false;await page.evaluate(s=>__hub.onState(s),state);await page.waitForTimeout(150);
  const signedout=refreshes();
  state.auth.signed_in=true;state.auth.steam_id='76561198000000002';
  await page.evaluate(s=>__hub.onState(s),state);await page.waitForTimeout(150);
  assert.equal(refreshes(),signedout+1,'new account refreshes its own standings');
  state.view='competitive';state.comp.error=state.strings.comp_launch_prepare_failed;
  for(const phase of ['connecting','live']){
   state.comp.phase=phase;
   await page.evaluate(s=>__hub.onState(s),state);
   assert.ok(await page.getByText(state.comp.error,{exact:true}).count(),phase+' renders preparation error');
  }
  // The joiner gets one game action throughout the connection flow. Exercise actual
  // clicks as well as labels so a reconnect cannot accidentally call first-launch.
  state.comp.error='';state.comp.host_test=false;
  assert.equal(fixture.languages.en.comp_reconnect,'Reconnect to game');
  for(const [lang,strings] of Object.entries(fixture.languages)){
   state.lang=lang;state.strings=strings;
   assert.ok(strings.comp_reconnect,lang+' has a reconnect translation');
   for(const isHost of [false,true]){
    for(const phase of ['connecting','live']){
     for(const connected of [false,true]){
      for(const ready of [false,true]){
       state.comp.phase=phase;
       state.comp.connect={is_host:isHost,i_connected:connected,host_ready:ready,left:180,join_left:300,total:10,done:2};
       state.comp.live={is_host:isHost,map:'Rome',host:{name:'Host'},can_finish:false};
       await page.evaluate(s=>__hub.onState(s),state);
       const firstLaunch=!isHost&&phase==='connecting'&&!connected;
       const buttons=page.locator(phase==='connecting'?'.connect-action button':'.live-action button');
       assert.equal(await buttons.count(),1,`${lang}: ${phase}, host=${isHost}, connected=${connected}, ready=${ready}`);
       const label=firstLaunch?strings.comp_launch:isHost?strings.comp_relaunch:strings.comp_reconnect;
       assert.equal(await buttons.textContent(),label);
       assert.equal(await buttons.isDisabled(),firstLaunch&&!ready);
       if(!(firstLaunch&&!ready)){
        const before=calls.length;
        await Promise.all([
         page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/verb/')),
         buttons.click()
        ]);
        assert.equal(calls.length,before+1,'one click sends exactly one action');
        assert.equal(calls.at(-1),firstLaunch?'launch_game':'relaunch_game');
       }
      }
     }
    }
   }
  }
  state.view='settings';state.settings.game.locked=true;
  await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.set-path-row button:enabled,.set-path-row input:enabled').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Release browser regressions passed: account refresh, visible connection errors, one game action across 112 role/phase/readiness/language states, locked game path.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
