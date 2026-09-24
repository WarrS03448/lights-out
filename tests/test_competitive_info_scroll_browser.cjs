// NODE_PATH supplies Playwright; HUB_TEST_PYTHON selects the project interpreter.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot,strings_for; from hub.i18n import CODES; p,s=_web_panel(); print(json.dumps(dict(state=state_snapshot(s,p),languages={lang:strings_for(lang) for lang in CODES})))'],{cwd:root,encoding:'utf8'}));
const s=fixture.state;s.view='competitive';s.update=null;s.gamemode_update=null;s.comp.installed=true;
s.comp.ladder={ranks:require('../server/progress.cjs').ranks(),placement_matches:5};
s.comp.penalties={rungs:[300,900,3600,7200,86400],decay_seconds:604800,connect_seconds:300,rr:25};
s.auth.rank={rank:3,division:1,top:false};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://hub.test/**',route=>{
   const u=new URL(route.request().url());
   if(u.pathname==='/state')return route.fulfill({json:s});
   if(u.pathname==='/events')return route.fulfill({json:[]});
   if(u.pathname.startsWith('/verb/')){
    const key=u.pathname.endsWith('toggle_rank_info')?'rank_info_open':u.pathname.endsWith('toggle_penalties')?'penalties_open':null;
    if(key){s.comp[key]=!s.comp[key];s.comp[key==='rank_info_open'?'penalties_open':'rank_info_open']=false;}
    return route.fulfill({json:{ok:true}});
   }
   const file=path.join(root,'hub/webui/static',u.pathname==='/'?'index.html':decodeURIComponent(u.pathname));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');await page.waitForSelector('.hero-tools');
  const push=()=>page.evaluate(v=>__hub.onState(v),s);
  for(const [lang,strings] of Object.entries(fixture.languages))for(const width of [800,1050,1440])for(const mode of ['BB5','BB1'])for(const kind of ['rank','penalties']){
   s.lang=lang;s.strings=strings;s.comp.mode_id=mode;s.comp.rank_info_open=false;s.comp.penalties_open=false;
   await page.setViewportSize({width,height:560});await push();
   await page.locator('.hero-tools button').nth(kind==='rank'?1:0).click();
   const dialog=page.locator('.rank-overlay .ui-modal');await dialog.waitFor();
   await page.waitForFunction(()=>document.activeElement===document.querySelector('.rank-overlay .ui-modal'));
   const label=[lang,width,mode,kind].join('/');
   const bounds=await dialog.boundingBox();await page.mouse.move(bounds.x+bounds.width/2,bounds.y+bounds.height/2);
   if(await dialog.evaluate(n=>n.scrollHeight>n.clientHeight)){
    await page.mouse.wheel(0,200);await page.waitForFunction(()=>document.querySelector('.rank-overlay .ui-modal').scrollTop>0);
   }
   await dialog.press('PageDown');
   // Chromium animates keyboard scrolling. Finish that user gesture before measuring refreshes.
   await page.evaluate(()=>{window.settledTop=-1;window.settledFrames=0;});
   await page.waitForFunction(()=>{const top=document.querySelector('.rank-overlay .ui-modal').scrollTop;
    settledFrames=top===settledTop?settledFrames+1:0;settledTop=top;return settledFrames>=10;},null,{polling:'raf'});
   await page.evaluate(()=>{const n=document.querySelector('.rank-overlay .ui-modal');
    window.savedDialog=n;window.savedOverlay=n.parentNode;window.savedClose=n.querySelector('.ui-modal-x');
    savedClose.focus({preventScroll:true});n.scrollTop=Math.min(170,(n.scrollHeight-n.clientHeight)/2);window.savedTop=n.scrollTop;});
   for(let n=0;n<6;n++){s.comp.queue_seconds=(s.comp.queue_seconds||0)+1;s.comp.error='Changed status '+n;await push();}
   assert.equal(await dialog.evaluate(n=>n.scrollTop),await page.evaluate(()=>savedTop),label+' scroll survives live refresh');
   assert(await page.evaluate(()=>savedDialog.isConnected&&savedOverlay.isConnected&&document.activeElement===savedClose),label+' same dialog and focus');
   const before=await dialog.textContent();
   if(kind==='rank'){s.comp.ladder.placement_matches++;s.auth.rank.division=s.auth.rank.division===1?2:1;}
   else{s.comp.penalties.rr++;s.comp.penalties.rungs[0]+=60;}
   await push();
   assert.notEqual(await dialog.textContent(),before,label+' relevant data remains current');
   assert.equal(await dialog.evaluate(n=>n.scrollTop),await page.evaluate(()=>savedTop),label+' relevant update preserves scroll');
   assert(await page.evaluate(()=>savedDialog.isConnected&&savedClose===document.activeElement),label+' relevant update preserves nodes/focus');
   s.comp.queue_seconds++;await push();
   assert(await page.evaluate(()=>savedOverlay.isConnected&&savedDialog.scrollTop===savedTop&&savedClose===document.activeElement),label+' later refresh preserves updated dialog');
   assert.equal(await page.locator('body > .rank-overlay').count(),1);
   assert(await dialog.evaluate(n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth+1&&r.bottom<=innerHeight+1&&n.scrollWidth<=n.clientWidth+1;}),label+' fits');
   assert.equal(await dialog.evaluate(n=>getComputedStyle(n).borderRadius),'0px');
   if(width===800)await page.keyboard.press('Escape');
   else if(width===1050)await page.locator('.rank-overlay .ui-modal-x').click();
   else await page.locator('.rank-overlay').click({position:{x:3,y:3}});
   await page.waitForFunction(()=>!document.querySelector('.rank-overlay'));
  }
  // Scope transitions must not carry a prior account/mode/language's reading position.
  s.comp.rank_info_open=true;await push();
  for(const change of [()=>s.comp.mode_id=s.comp.mode_id==='BB1'?'BB5':'BB1',()=>{s.lang='de';s.strings=fixture.languages.de;},()=>s.auth.player_id='00000000-0000-4000-8000-000000000019']){
   await page.evaluate(()=>{window.oldDialog=document.querySelector('.rank-overlay .ui-modal');oldDialog.scrollTop=150;});change();await push();
   assert(await page.evaluate(()=>!oldDialog.isConnected&&document.querySelector('.rank-overlay .ui-modal').scrollTop===0));
  }
  s.auth.signed_in=false;s.comp.rank_info_open=false;s.comp.penalties_open=false;await push();assert.equal(await page.locator('.rank-overlay').count(),0);
  s.auth.signed_in=true;s.comp.penalties_open=true;await push();s.view='profile';await push();assert.equal(await page.locator('.rank-overlay').count(),0);
  assert.deepEqual(errors,[]);console.log('Rank/penalties dialogs: wheel and keyboard, 7 languages, 3 sizes, both modes, changed data/scroll/focus, close methods and account/mode/language/navigation scopes passed.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
