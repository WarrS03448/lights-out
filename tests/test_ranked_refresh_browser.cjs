// NODE_PATH contains Playwright; HUB_TEST_PYTHON selects the project interpreter.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot,strings_for; from hub.i18n import CODES; p,s=_web_panel(); print(json.dumps(dict(state=state_snapshot(s,p),languages={lang:strings_for(lang) for lang in CODES})))'],{cwd:root,encoding:'utf8'}));
const state=fixture.state;
state.view='competitive';state.update=null;state.gamemode_update=null;
state.auth.signed_in=true;state.auth.player_id='00000000-0000-4000-8000-000000000010';
state.comp.phase='idle';state.comp.installed=true;state.comp.mode_id='BB1';state.comp.mode_selectable=true;
state.settings.network={region:'NA',cross_region:false,status:'ready',locked:false,options:[{code:'NA',name:'North America'},{code:'EU',name:'Europe'}]};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1050,height:720}}),errors=[],verbs=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://hub.test/**',route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/state')return route.fulfill({json:state});
   if(url.pathname==='/events')return route.fulfill({json:[]});
   if(url.pathname.startsWith('/verb/')){verbs.push(url.pathname);return route.fulfill({json:{ok:true}});}
   const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');await page.waitForSelector('#comp-network-region');
  state.comp.installed=false;state.comp.mode_listed=true;
  for(const [lang,strings] of Object.entries(fixture.languages)){
   state.lang=lang;state.strings=strings;
   for(const mode of ['BB1','BB5']){
    state.comp.mode_id=mode;
    for(let n=0;n<3;n++){
     state.comp.queue_seconds++;await page.evaluate(s=>__hub.onState(s),state);
     const expected=mode==='BB1'?'Bodybomb 1v1':'Bodybomb 5v5',other=mode==='BB1'?'Bodybomb 5v5':'Bodybomb 1v1';
     for(const selector of ['.hero-action .btn-find','.hero-action .hero-ready']){
      const text=await page.locator(selector).textContent();assert(text.includes(expected),lang+' '+mode+' '+text);assert(!text.includes(other));
     }
    }
   }
  }
  state.comp.installed=true;state.comp.phase='queued';state.comp.mode_selectable=false;
  for(const [lang,strings] of Object.entries(fixture.languages)){
   state.lang=lang;state.strings=strings;
   for(const mode of ['BB1','BB5']){
    state.comp.mode_id=mode;
    for(let n=0;n<3;n++){
     state.comp.queue.seconds=10+n;state.comp.queue.size=3+n;
     await page.evaluate(s=>__hub.onState(s),state);
     const text=await page.locator('.search-meta').textContent();
     assert(text.includes(mode==='BB1'?'Bodybomb 1v1':'Bodybomb 5v5'),lang+' '+mode+' '+text);
     assert(text.includes(mode==='BB1'?'2':'10'),lang+' '+mode+' '+text);
     assert(!text.includes(mode==='BB1'?'5v5':'1v1'),lang+' '+mode+' '+text);
     assert.equal(await page.locator('.search-head .timer').textContent(),'0:'+(10+n));
    }
   }
  }
  state.comp.phase='idle';state.comp.mode_selectable=true;
  state.lang='en';state.strings=fixture.languages.en;state.comp.installed=true;state.comp.mode_id='BB1';
  await page.evaluate(s=>__hub.onState(s),state);
  for(const width of [800,1050,1440]){
   await page.setViewportSize({width,height:720});
   const picker=page.locator('#comp-network-region');await picker.focus();await picker.press('Alt+ArrowDown');
   await page.evaluate(()=>{window.originalPicker=document.getElementById('comp-network-region');window.originalBar=document.querySelector('.ranked-mode-bar');});
   for(let n=0;n<8;n++){
    state.comp.queue_seconds=n+width;state.comp.error='Changed status '+n;state.settings.network.status=n%2?'measuring':'ready';
    await page.evaluate(s=>__hub.onState(s),state);
   }
   assert.equal(await page.evaluate(()=>originalPicker.isConnected&&document.activeElement===originalPicker&&originalBar.isConnected),true);
   assert.ok((await page.locator('.hero-action').textContent()).includes('Changed status 7'));
   await picker.press('Escape');await picker.selectOption('EU');
   const button=page.locator('[data-mode="BB5"]');await button.focus();
   state.comp.queue_seconds++;await page.evaluate(s=>__hub.onState(s),state);
   assert.equal(await button.evaluate(n=>document.activeElement===n),true);
   await button.click();
  }
  await page.waitForTimeout(100);
  assert(verbs.some(v=>v.includes('settings_set_matchmaking_region')));assert(verbs.some(v=>v.includes('select_ranked_mode')));
  const join=page.locator('#join-code-input');
  if(await join.count()){
   await join.fill('PRIVATE');await page.evaluate(()=>window.originalJoin=document.getElementById('join-code-input'));
   state.comp.queue_seconds++;await page.evaluate(s=>__hub.onState(s),state);
   assert.equal(await page.evaluate(()=>originalJoin.isConnected&&document.activeElement===originalJoin),true);
   state.auth.player_id='00000000-0000-4000-8000-000000000011';await page.evaluate(s=>__hub.onState(s),state);
   assert.equal(await join.inputValue(),'');
  }
  for(const view of ['profile','history']){
   state.view=view;state.profile.signed_in=true;state.history.asked=true;
   state.profile.ranked_ranks={BB5:{rank_name:'Soldier',division:1,rr:30},BB1:{rank_name:'Rookie',division:2,rr:40}};
   state.profile.history_mode=state.history.mode='all';
   await page.evaluate(s=>__hub.onState(s),state);
   const tabs=page.locator('.ranked-history-modes');await tabs.locator('button').last().focus();
   await page.evaluate(()=>window.originalTabs=document.querySelector('.ranked-history-modes'));
   for(let n=0;n<6;n++){
    state.profile.ranked_ranks.BB1.rr++;state.profile.stats.played=n;state.history.loading=n%2===0;state.comp.queue_seconds++;
    await page.evaluate(s=>__hub.onState(s),state);
   }
   assert.equal(await page.evaluate(()=>originalTabs.isConnected&&originalTabs.contains(document.activeElement)),true,view+' retains focused mode controls during relevant updates');
   await tabs.locator('button').last().click();
  }
  state.history.open_id='m1';state.history.open_loading=true;
  await page.evaluate(s=>__hub.onState(s),state);
  await page.evaluate(()=>window.originalDetail=document.querySelector('body > .ui-overlay'));
  for(let n=0;n<5;n++){state.comp.queue_seconds++;await page.evaluate(s=>__hub.onState(s),state);}
  assert.equal(await page.evaluate(()=>originalDetail.isConnected),true,'Unrelated updates keep history detail attached');
  state.history.open_id='';state.history.open_loading=false;await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('body > .ui-overlay').count(),0);
  state.view='competitive';state.comp.phase='idle';state.comp.installed=true;state.comp.mode_id='BB1';
  state.comp.ranked_ban={reason:'test'};state.comp.mode_strings={banned:'Ranked restriction',solo_only:'Solo players only'};
  state.party.size=1;state.party.in_party=true;state.comp.can_find=false;await page.evaluate(s=>__hub.onState(s),state);
  assert.ok((await page.locator('.hero-action').textContent()).includes('Solo players only'));
  assert.ok((await page.locator('.hero-action').textContent()).includes('Ranked restriction'));
  assert.equal(await page.locator('.hero-action .btn-find').isDisabled(),true);
  state.comp.ranked_ban=null;state.party.in_party=false;state.comp.can_find=true;
  await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.hero-action .btn-find').isEnabled(),true);
  assert.equal((await page.locator('.hero-action').textContent()).includes('Solo players only'),false);
  state.auth={signed_in:false,phase:'signed_out',account_step:'login',account_busy:false};state.comp.error='';
  await page.evaluate(s=>__hub.onState(s),state);
  const email=page.locator('.account-form input[name="email"]');await email.fill('draft@example.test');
  await page.evaluate(()=>{window.originalEmail=document.querySelector('.account-form input[name="email"]');});
  state.auth.account_busy=true;await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.account-form button[type="submit"]').isDisabled(),true);
  assert.equal(await page.evaluate(()=>originalEmail.isConnected&&document.activeElement===originalEmail),true);
  state.auth.account_busy=false;state.comp.error='Sign-in service unavailable';await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.account-form button[type="submit"]').isEnabled(),true);
  assert.equal(await email.inputValue(),'draft@example.test');
  assert.equal(await page.locator('.account-form [role="alert"]').textContent(),'Sign-in service unavailable');
  await page.locator('.account-form input[name="password"]').fill('test-password');
  await page.locator('.account-form button[type="submit"]').click();
  assert.equal(await page.locator('.account-form input[name="password"]').inputValue(),'');
  assert.deepEqual(errors,[]);console.log('Ranked controls survive changed snapshots; account form state and eligibility explanations stay current.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
