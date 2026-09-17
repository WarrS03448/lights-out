// UTF-8. Run: node tests/test_profile_history_browser.cjs (Playwright required).
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const fixture=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python',['-c',
 'import json; from tests.test_hub import _web_panel; from tests.test_screen_history import sample_rows,_record,_open; from hub.webui.snapshot import state_snapshot; from hub.i18n import STRINGS; from hub.webui.screens.profile import PROFILE_STRINGS as ps; from hub.webui.screens.history import strings_for as hs; p,s=_web_panel(); s.history=sample_rows(); print(json.dumps(dict(snapshot=state_snapshot(s,p),detail=_open(_record()),languages={k:dict(shared=STRINGS[k],profile=ps[k],history=hs(k)) for k in STRINGS})))'
],{cwd:root,encoding:'utf8'}));
const s=fixture.snapshot;s.view='profile';s.update=null;s.gamemode_update=null;
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{channel:'msedge'})});
 try{
 const page=await browser.newPage({viewport:{width:1200,height:760}});
 const calls=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://hub.test/**',route=>{
 const url=new URL(route.request().url());
 if(url.pathname==='/state')return route.fulfill({json:s});
 if(url.pathname==='/events')return route.fulfill({json:[]});
 if(url.pathname.startsWith('/verb/')){
 const verb=url.pathname.split('/').pop(), args=JSON.parse(route.request().postData()||'[]');calls.push([verb,...args]);
 if(verb==='set_view')s.view=args[0];
 if(verb==='open_match'){s.history.open_id=args[0];s.history.open=fixture.detail;}
 if(verb==='close_match'){s.history.open_id='';s.history.open=null;}
 s.history.seq++;
 return route.fulfill({json:{ok:true}});
 }
 const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
 return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
 });
 await page.goto('http://hub.test/');await page.waitForSelector('.profile');
 assert.equal(await page.locator('#nav [data-view="history"]').count(),1,'History belongs in the top header');
 assert.equal(await page.locator('#nav [data-view="friends"]').count(),0,'Friends has its own persistent dock');
 assert.equal(await page.locator('#app').getByRole('tab',{name:'Friends',exact:true}).count(),0,'Friends is not a Profile tab');
 assert.equal(await page.locator('.profile-tabs').getByRole('tab',{name:'Match history',exact:true}).count(),0,'History is not duplicated inside Profile');
 assert.equal(await page.locator('.ladder').count(),0,'Rank ladder removed from Profile');
 for(const [index,action] of ['click','Enter',' '].entries()){
 const row=page.locator('.profile-recent .ui-listrow').nth(index);
 assert.equal(await row.getAttribute('role'),'button','Recent matches can open their game');
 if(action==='click')await row.click();else await row.press(action);
 await page.waitForSelector('#app > .history');
 await page.waitForSelector('.md-overlay');
 assert.equal(s.history.open_id,['m1','m2','m3'][index],'Open the selected recent match');
 assert.equal(await page.locator('#nav .active').getAttribute('data-view'),'history');
 await page.getByRole('button',{name:'Close',exact:true}).click();
 await page.waitForSelector('.md-overlay',{state:'detached'});
 await page.locator('#nav [data-view="profile"]').click();
 await page.waitForSelector('.profile');
 }
 await page.locator('#nav [data-view="history"]').click();
 await page.waitForSelector('#app > .history');
 assert.equal(await page.locator('#nav .active').getAttribute('data-view'),'history');
 const total=await page.locator('.hist-row').count();assert(total>1);
 await page.getByRole('tab',{name:'Cancelled',exact:true}).click();
 const cancelled=await page.locator('.hist-row').count();assert(cancelled<total);
 s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
 assert.equal(await page.getByRole('tab',{name:'Cancelled',exact:true}).getAttribute('aria-selected'),'true');
 await page.getByRole('tab',{name:'All',exact:true}).click();assert.equal(await page.locator('.hist-row').count(),total);
 await page.getByRole('tab',{name:'Played',exact:true}).click();assert((await page.locator('.hist-row').count())<total);
 await page.getByRole('tab',{name:'All',exact:true}).click();
 await page.getByRole('button',{name:'Refresh',exact:true}).click();
 assert(calls.some(c=>c[0]==='refresh_history'));
 for(const action of ['click','Enter',' ']){
 const row=page.locator('.hist-open').first();if(action==='click')await row.click();else await row.press(action);
 await page.waitForSelector('.md-overlay');
 assert.equal(await page.locator('.md-overlay').count(),1);assert.equal(await page.locator('.md-overlay .md-player.sb').count(),2);
 if(action==='click'){
 await page.locator('.md-overlay .tm-report').click();
 assert(calls.some(c=>c[0]==='open_report' && c[1]==='22222222222222222'));
 await page.getByRole('button',{name:'Close',exact:true}).click();
 }else if(action==='Enter')await page.locator('.md-overlay .ui-modal').press('Escape');
 else await page.locator('.md-overlay').click({position:{x:2,y:2}});
 await page.waitForSelector('.md-overlay',{state:'detached'});
 }
 // The result belongs to the viewer's team, regardless of the server's team numbering.
 for(const [team,score,expected] of [
 [2,{'1':7,'2':3},[3,7]], [1,{'1':7,'2':5},[7,5]],
 [2,{'1':5,'2':7},[7,5]], [1,{'1':3,'2':7},[3,7]],
 [2,{'1':7,'2':0},[0,7]]
 ]){
 const detail=JSON.parse(JSON.stringify(fixture.detail));
 detail.score=score;
 detail.players.forEach(p=>{p.is_me=p.team===team;});
 detail.scoreboard.forEach(p=>{p.is_me=p.team===team;});
 s.history.open_id=detail.id;s.history.open=detail;s.history.seq++;
 await page.evaluate(s=>__hub.onState(s),s);
 assert.deepEqual((await page.locator('.md-score').textContent()).match(/\d+/g).map(Number),expected,'viewer score comes first for team '+team);
 assert.deepEqual(detail.score,score,'display keeps the archived team scores unchanged');
 for(const [width,height] of [[800,560],[1200,760]]){
 await page.setViewportSize({width,height});await page.evaluate(()=>HubUI.applyScale());
 const centered=await page.locator('.md-score').evaluate(n=>{const score=n.getBoundingClientRect(),head=n.parentElement.getBoundingClientRect();return Math.abs((score.left+score.right-head.left-head.right)/2)<1;});
 assert(centered,'match score is centered at '+width+'px');
 }
 }
 // Old records can retain team membership without a per-player scoreboard.
 s.history.open.players=[];s.history.open.scoreboard=[];s.history.open.has_scoreboard=false;
 s.history.open.teams={'1':['opponent'],'2':[s.auth.steam_id]};
 s.history.open.score={'1':5,'2':7};s.history.open.map='A deliberately long map name that must not push the score off center';s.history.seq++;
 await page.evaluate(s=>__hub.onState(s),s);
 assert.deepEqual((await page.locator('.md-score').textContent()).match(/\d+/g).map(Number),[7,5]);
 assert(await page.locator('.md-score').evaluate(n=>{const r=n.getBoundingClientRect(),h=n.parentElement.getBoundingClientRect(),map=n.previousElementSibling.getBoundingClientRect();return Math.abs((r.left+r.right-h.left-h.right)/2)<1&&map.right<=r.left;}));
 if(process.env.HUB_SCORE_SCREENSHOT)await page.screenshot({path:process.env.HUB_SCORE_SCREENSHOT});
 s.history.open.score=null;s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
 assert.equal(await page.locator('.md-score').count(),0,'missing results do not invent a score');
 await page.getByRole('button',{name:'Close',exact:true}).click();await page.waitForSelector('.md-overlay',{state:'detached'});
 for(const [lang,strings] of Object.entries(fixture.languages)){
 s.strings=strings.shared;s.profile.strings=strings.profile;s.history.strings=strings.history;
 for(const [width,height] of [[800,560],[1200,760]]){
 await page.setViewportSize({width,height});s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
 const fits=await page.evaluate(()=>{
 const app=document.querySelector('#app').getBoundingClientRect();
 return [...document.querySelectorAll('.profile-tabs,.hist-head,.hist-row')].every(n=>{const r=n.getBoundingClientRect();return r.left>=app.left && r.right<=app.right+1}) && document.querySelector('.hist-scroll').getBoundingClientRect().bottom<=app.bottom+1;
 });assert(fits,lang+' '+width+' history fits');
 }
 }
 s.strings=fixture.languages.en.shared;s.profile.strings=fixture.languages.en.profile;s.history.strings=fixture.languages.en.history;
 s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
 await page.locator('#nav [data-view="profile"]').click();await page.waitForSelector('.profile');
 await page.getByRole('button',{name:'View all',exact:true}).click();await page.waitForSelector('#app > .history');
 assert.equal(s.view,'history');
 if(process.env.HUB_SCREENSHOT)await page.screenshot({path:process.env.HUB_SCREENSHOT});
 s.friends.signed_in=true;s.friends.code='ABCDE-FGHJK';s.friends.code_hidden=true;s.friends.code_masked='•••••-•••••';
 s.friends.list=[{steam_id:'mate',persona:'Mate',online:true,can_invite:true},{steam_id:'away',persona:'Away',online:false,can_invite:false}];
 s.friends.incoming=[{steam_id:'request',persona:'Request'}];s.friends.outgoing=[{steam_id:'sent',persona:'Sent'}];
 await page.locator('#nav [data-view="profile"]').click();await page.waitForSelector('.profile');
 await page.locator('#friends-toggle').click();await page.waitForSelector('#friends-panel');
 assert.equal(await page.locator('#nav .active').getAttribute('data-view'),'profile');
 for(const label of ['Show','New code','Accept','Decline','Cancel'])await page.getByRole('button',{name:label,exact:true}).first().click();
 for(const label of ['Invite','Remove']){
 await page.locator('.fr-section-friend .fr-row').first().click({button:'right'});
 await page.getByRole('menuitem',{name:label,exact:true}).click();
 }
 await page.locator('#fr-code-input').fill(' QWERT-YUIOP ');await page.locator('#fr-code-input').press('Enter');
 await page.waitForTimeout(100);
 for(const expected of [['friend_code_toggle'],['friend_code_new'],['friend_accept','request'],['friend_decline','request'],['friend_cancel','sent'],['friend_invite','mate'],['friend_remove','mate'],['friend_add','QWERT-YUIOP']])assert(calls.some(c=>JSON.stringify(c)===JSON.stringify(expected)),JSON.stringify(expected));
 await page.locator('.fr-section-friend .fr-row').last().click({button:'right'});
 assert(await page.getByRole('menuitem',{name:'Invite',exact:true}).isDisabled());
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('.fr-code').textContent(),'•••••-•••••');
 await page.getByRole('button',{name:'Minimize friends',exact:true}).click();await page.waitForSelector('.profile');
 assert.equal(await page.locator('.ladder').count(),0);

 assert.deepEqual(errors,[]);
 console.log('Profile history: navigation, all filters, redraw persistence, refresh, mouse/keyboard details, reporting, close controls, 7 languages at 2 sizes.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
