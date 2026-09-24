// NODE_PATH contains Playwright; HUB_TEST_PYTHON selects the project interpreter.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot,strings_for; from hub.i18n import CODES; from hub.webui.screens.postmatch import snapshot; p,s=_web_panel(); s.phase="live"; s.match_id="0123456789abcdef"; s.map="Rome"; s.players=[s._player_from(dict(steam_id=str(76561198000000001+i),persona="Player "+str(i+1))) for i in range(10)]; s.teams={1:s.players[:5],2:s.players[5:]}; s.host=s.players[1]; s.recovery=dict(phase="restoring"); s.recovery_health=dict(connected=[x["steam_id"] for x in s.players[:3]]); state=state_snapshot(s,p); s._on_result(dict(won=True, score=[2,1], terminal=dict(recovery=True,reason="reconnect_timeout"))); print(json.dumps(dict(state=state,postmatch=snapshot(s,p),languages={lang:strings_for(lang) for lang in CODES})))'],{cwd:root,encoding:'utf8'}));
const s=fixture.state;s.view='competitive';s.update=null;s.gamemode_update=null;s.comp.installed=true;
s.comp.live.recovery={visible:true,restoring:false,can_claim:true,round:3,world_ready:false,busy:false};
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try {
  const page=await browser.newPage({viewport:{width:1050,height:720}}),errors=[],verbs=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://hub.test/**',route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/state')return route.fulfill({json:s});
   if(url.pathname==='/events')return route.fulfill({json:[]});
   if(url.pathname.startsWith('/verb/')){verbs.push(url.pathname);return route.fulfill({json:{ok:true}});}
   const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');await page.waitForSelector('.live-action button');
  const push=()=>page.evaluate(state=>__hub.onState(state),s);
  for(const [lang,strings] of Object.entries(fixture.languages)) {
   s.lang=lang;s.strings=strings;
   for(const width of [800,1050,1440]) {
    await page.setViewportSize({width,height:720});await push();
    const button=page.locator('.live-action button').first();await button.focus();
    await page.evaluate(()=>window.originalRecovery=document.querySelector('.live-action button'));
    for(let n=0;n<6;n++){s.comp.queue_seconds++;s.comp.live.host.ping=30+n;s.comp.error='Changed '+n;await push();}
    assert(await page.evaluate(()=>originalRecovery.isConnected&&document.activeElement===originalRecovery),lang+' focus survives changed snapshots');
    assert.equal(await button.textContent(),strings.comp_recovery_host);
    assert.equal(await button.evaluate(n=>getComputedStyle(n).borderRadius),'0px');
    assert(await button.evaluate(n=>n.getBoundingClientRect().right<=innerWidth+1),lang+' fits viewport');
   }
  }
  await page.locator('.live-action button').first().press('Enter');
  s.comp.live.recovery.restoring=true;s.comp.live.recovery.can_claim=false;s.comp.live.is_host=false;await push();
  assert.equal(await page.locator('.live-action button').count(),0,'joiners wait for successor lobby');
  s.comp.live.recovery.world_ready=true;await push();await page.locator('.live-action button').click();
  Object.assign(s.comp.live.recovery,{rejoin_until:Date.now()+298000,returned:3,expected:10,roster_sealed:false});
  for(const [lang,strings] of Object.entries(fixture.languages)) {
   s.lang=lang;s.strings=strings;await page.setViewportSize({width:800,height:720});await push();
   const clock=page.locator('.recovery-clock');
   assert.match(await clock.textContent(),/4:\d\d/);assert.match(await clock.textContent(),/3\/10/);
   assert.equal(await page.getByText(strings.comp_recovery_return_rule,{exact:true}).count(),1,lang+' deadline consequence is shown');
   assert(await clock.evaluate(n=>n.getBoundingClientRect().right<=innerWidth+1),lang+' countdown fits');
  }
  const before=await page.locator('.recovery-clock').textContent();await page.waitForTimeout(1200);
  assert.notEqual(await page.locator('.recovery-clock').textContent(),before,'clock advances without waiting for server snapshots');
  s.comp.live.recovery.rejoin_until=Date.now()+700;await push();await page.waitForTimeout(1200);
  assert.equal(await page.locator('#match-recovery-relaunch_game').isDisabled(),true,'deadline disables an existing launch control without another snapshot');
  assert.equal(await page.locator('.recovery-clock').textContent(),s.strings.comp_recovery_return_closed);
  await push();assert.equal(await page.locator('#match-recovery-relaunch_game:not(:disabled)').count(),0,'expired return window cannot offer a new launch');
  s.comp.live.recovery.roster_sealed=true;await push();
  assert.equal(await page.locator('.recovery-clock').count(),0,'sealed roster removes return clock');
  s.comp.live.recovery.can_rejoin=false;await push();
  assert.equal(await page.locator('#match-recovery-relaunch_game').count(),0,'excluded player cannot launch after sealing');
  // Every state uses the same components and survives genuinely changed snapshots.
  for(const [state,key] of Object.entries({creating:'comp_recovery_creating',verifying:'comp_recovery_verifying',
      close_game:'comp_recovery_close_hint',resumed:'comp_recovery_resumed',unavailable:'comp_recovery_unavailable'})) {
    Object.assign(s.comp.live.recovery,{state,can_launch:state==='resumed',restoring:state==='creating'||state==='verifying',
      can_rejoin:true,rejoin_until:null,roster_sealed:state==='verifying',can_claim:false});
    for(const [lang,strings] of Object.entries(fixture.languages)){
      s.lang=lang;s.strings=strings;await push();
      assert.equal(await page.getByText(strings[key],{exact:true}).count(),1,lang+' '+state);
    }
  }
  Object.assign(s.comp.live.recovery,{state:'resumed',can_launch:true,busy:false,restoring:false});await push();
  const reconnect=page.locator('#match-recovery-relaunch_game');await reconnect.focus();
  await page.evaluate(()=>window.reconnectNode=document.activeElement);
  s.comp.live.recovery.busy=true;await push();assert(await reconnect.isDisabled());
  s.comp.live.recovery.busy=false;s.comp.queue_seconds++;await push();
  assert(await page.evaluate(()=>reconnectNode===document.querySelector('#match-recovery-relaunch_game')));
  assert.equal(await reconnect.textContent(),s.strings.comp_reconnect,'successful recovery retains ordinary participant reconnect');
  // Service time, not a fast or manually changed PC wall clock, decides the countdown.
  await page.evaluate(()=>{window.realDateNow=Date.now;Date.now=()=>9999999999999;});
  Object.assign(s.comp.live.recovery,{state:'returning',restoring:true,rejoin_until:400000,server_now:100000,
    roster_sealed:false,returned:3,expected:10});await push();
  assert.match(await page.locator('.recovery-clock').textContent(),/[45]:[0-5]\d/);
  assert(!(await reconnect.isDisabled()));
  await page.evaluate(()=>{Date.now=window.realDateNow;});
  s.lang='en';s.strings=fixture.languages.en;s.comp.error='';await push();
  assert.equal(await page.getByText(s.strings.comp_recovery_returned,{exact:true}).count(),3);
  assert.equal(await page.getByText(s.strings.comp_recovery_waiting,{exact:true}).count(),7);
  assert(!(await page.locator('body').innerText()).includes('\u2014'),'client copy contains no em dash');
  fs.mkdirSync(path.join(root,'work/recovery-candidate/ui-audit'),{recursive:true});
  await page.screenshot({path:path.join(root,'work/recovery-candidate/ui-audit/return-window.png')});
  // An unrelated refresh must preserve the result dialog, focus and expanded details.
  s.postmatch=fixture.postmatch.postmatch;await push();await page.waitForSelector('.pm-overlay');
  await page.evaluate(()=>{window.originalResult=document.querySelector('.pm-overlay');window.resultFocus=originalResult.querySelector('button');resultFocus.focus();});
  s.comp.queue_seconds++;s.comp.error='Changed while reading result';await push();
  assert(await page.evaluate(()=>originalResult.isConnected&&document.activeElement===resultFocus),'result dialog survives changed snapshots');
  assert.equal(await page.getByText(s.postmatch.strings.recovery_forfeit,{exact:true}).count(),1);
  await page.screenshot({path:path.join(root,'work/recovery-candidate/ui-audit/recovery-forfeit.png')});
  s.postmatch={open:false};await push();assert.equal(await page.locator('.pm-overlay').count(),0);
  await page.waitForTimeout(50);
  assert(verbs.includes('/verb/claim_recovery'));assert(verbs.includes('/verb/relaunch_game'));
  s.auth.player_id='00000000-0000-4000-8000-000000000099';s.comp.phase='idle';s.comp.live.recovery={};await push();
  assert.equal(await page.locator('.live-action').count(),0,'account transition removes private match controls');
  assert.deepEqual(errors,[]);console.log('Recovery controls: seven languages, changed snapshots, keyboard/pointer, readiness, account scope.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
