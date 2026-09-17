// Run with Playwright in NODE_PATH and HUB_TEST_PYTHON set to the test interpreter.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const state=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
  'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; p,s=_web_panel(); print(json.dumps(state_snapshot(s,p)))'],{cwd:root,encoding:'utf8'}));
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
  state.view='competitive';state.comp.error='Could not prepare the match. Close Bodycam, then select Open game to retry.';
  for(const phase of ['connecting','live']){
   state.comp.phase=phase;
   await page.evaluate(s=>__hub.onState(s),state);
   assert.ok(await page.getByText(state.comp.error,{exact:true}).count(),phase+' renders preparation error');
  }
  state.view='settings';state.settings.game.locked=true;
  await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.set-path-row button:enabled,.set-path-row input:enabled').count(),0);
  assert.deepEqual(errors,[]);
  console.log('Release browser regressions passed: account refresh, visible connection errors, locked game path.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
