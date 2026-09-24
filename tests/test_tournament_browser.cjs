// Run with Playwright in NODE_PATH and HUB_TEST_PYTHON set to the project Python.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const snapshots=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',`
import json
from tests.test_screen_bugreport import _panel
from hub.webui.snapshot import state_snapshot
from hub import i18n
panel,s=_panel(); panel.view='tournament'
result={}
for lang in i18n.CODES:
 i18n.set_language(lang); result[lang]=state_snapshot(s,panel)
print(json.dumps(result))
`],{cwd:root,encoding:'utf8',maxBuffer:8*1024*1024}));
let state=snapshots.en;const verbs=[],payloads=[];
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://localhost');
 if(url.pathname.startsWith('/verb/')){verbs.push(url.pathname);let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{payloads.push({verb:url.pathname,args:JSON.parse(body||'[]')});res.setHeader('content-type','application/json');res.end('{}');});return;}
 if(['/state','/events'].includes(url.pathname)||url.pathname.startsWith('/window/')){res.setHeader('content-type','application/json');res.end(JSON.stringify(url.pathname==='/state'?state:url.pathname==='/events'?{events:[],seq:0}:{maximized:false}));return;}
 const base=path.join(root,'hub/webui/static'),file=path.resolve(base,url.pathname==='/'?'index.html':decodeURIComponent(url.pathname.slice(1)));
 if(!file.startsWith(base+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
 res.setHeader('content-type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res);
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{const page=await browser.newPage({viewport:{width:1400,height:950}}),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.clock.install();await page.goto('http://127.0.0.1:'+server.address().port);
 await page.locator('.tournament').waitFor({timeout:5000});
 const event=require('../server/tournament.cjs').EVENT;
 async function show(data,lang='en'){state=structuredClone(snapshots[lang]);state.tournament.data={ok:true,event,server_now:event.start_at-2000,leaders:[],...data};await page.evaluate(s=>window.__hub.onState(s),state);}
 await show({});
 assert.equal(await page.locator('#tournament-format-notice').textContent(),snapshots.en.tournament.strings.format_notice);
 assert.equal(await page.locator('#tournament-format-notice').evaluate(e=>getComputedStyle(e).color),'rgb(255, 79, 88)');
 assert.equal(await page.locator('.tournament-pool').textContent(),'$150 USD');
 assert.deepEqual(await page.locator('.tournament-prize strong').allTextContents(),['$100 USD','$35 USD','$15 USD']);
 // The bridge advances server_now on every snapshot, even between standings polls.
 for(const width of [1400,800])for(const lang of Object.keys(snapshots))for(const at of [event.start_at-60000,event.start_at+60000,event.end_at+60000]){
   await page.setViewportSize({width,height:950});await show({server_now:at},lang);
   assert.equal(await page.locator('#tournament-format-notice').textContent(),snapshots[lang].tournament.strings.format_notice);
   await page.evaluate(()=>document.fonts.ready);await page.clock.runFor(100);
   await page.locator('.tournament').evaluate(n=>{n.scrollTop=500;});await page.clock.runFor(50);
   const readingPosition=await page.locator('.tournament').evaluate(n=>n.scrollTop);assert(readingPosition>0,'unable to scroll fixture at '+width+'/'+lang+'/'+at+': '+JSON.stringify(await page.locator('.tournament').evaluate(n=>({top:n.scrollTop,height:n.clientHeight,content:n.scrollHeight}))));
   for(let i=0;i<5;i++){
     state.tournament.data.server_now+=300;await page.evaluate(s=>window.__hub.onState(s),state);await page.clock.runFor(50);
     const position=await page.locator('.tournament').evaluate(n=>n.scrollTop);
     assert(Math.abs(position-readingPosition)<=1,'snapshot refresh moved the tournament from '+readingPosition+' to '+position+' at '+width+'/'+lang+'/'+at);
   }
 }
 await page.setViewportSize({width:1400,height:950});
 await show({});
 assert.equal(await page.locator('#tournament-payout-agreement').textContent(),'By playing in this tournament, you agree to receive any winnings through Zelle, Venmo, or PayPal.');
 assert.equal(await page.locator('[data-view="bugreport"] + [data-view="tournament"]').count(),1);
 const nav=await page.locator('[data-view="tournament"]').evaluate(e=>({color:getComputedStyle(e).color,weight:getComputedStyle(e).fontWeight}));assert.equal(nav.color,'rgb(255, 79, 88)');assert(+nav.weight>=700);
 await page.clock.runFor(2500);
 assert.equal(await page.locator('#tournament-phase').textContent(),'LIVE');assert.equal(await page.locator('#tournament-clock-label').textContent(),'Time remaining');
 assert(verbs.includes('/verb/tournament_refresh'));
 await show({server_now:event.end_at-1000,registered_at:event.start_at-10,leaders:[{rank:1,persona:'<script>bad</script>',net_rr:120,wins:4,matches:5}],you:{rank:15,net_rr:20,gained_rr:30,lost_rr:10,wins:2,matches:4}});
 assert.equal(await page.locator('#tournament-leaders tbody tr').count(),1);assert((await page.locator('#tournament-you').textContent()).includes('15'));assert.equal(await page.locator('#tournament-leaders script').count(),0);
 await page.clock.runFor(1500);assert.equal(await page.locator('#tournament-phase').textContent(),'Event ended');assert.equal(await page.locator('#tournament-register').isVisible(),false);
 const tiedWinners=[100,35,15,15].map((prize_usd,i)=>({rank:Math.min(i+1,3),persona:'Winner '+i,net_rr:100-Math.min(i,2),prize_usd}));
 for(const lang of Object.keys(snapshots)){
   await show({server_now:event.end_at,winners:tiedWinners,results_provisional:false},lang);
   assert.equal(await page.locator('.tournament-winner').count(),4);
   assert.deepEqual(await page.locator('.tournament-winner-prize').allTextContents(),['$100 USD','$35 USD','$15 USD','$15 USD']);
   assert.equal(await page.locator('.tournament-rules').first().textContent(),snapshots[lang].tournament.strings.rules);
 }
 await show({registered_at:event.start_at-10,history:[{match_id:'receipt',ended:event.start_at+1,delta:25,counted:true}]});
 await page.locator('#tournament-category').selectOption('outage');
 await page.locator('.tournament-support-form input').fill('private-match-reference');
 await page.locator('.tournament-support-form textarea').fill('My private support draft');
 await page.locator('#tournament-category').click();
 await page.evaluate(()=>{window.supportPicker=document.querySelector('#tournament-category');window.supportForm=document.querySelector('.tournament-support-form');});
 for(let i=0;i<12;i++){
   state.tournament.data.server_now+=300;
   state.status.connected=true;state.status.players_registered=200+i;state.status.tournament_registered=20+i;
   if(i===6){state.tournament.data.tickets=[{status:'open',at:event.start_at,message:'A newly received support update',replies:[]}];state.tournament.loading=true;}
   if(i===8)state.tournament.loading=false;
   await page.evaluate(s=>window.__hub.onState(s),state);await page.clock.runFor(50);
   assert(await page.evaluate(()=>supportPicker.isConnected&&supportPicker.matches(':open')&&document.activeElement===supportPicker&&supportForm===document.querySelector('.tournament-support-form')),'snapshot refresh closed the support dropdown');
   assert.equal(await page.locator('#stattournament').textContent(),`${20+i} registered for tournament`);
 }
 assert((await page.locator('.tournament-ticket').textContent()).includes('A newly received support update'),'support data still updates while the dropdown is open');
 await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');
 assert.equal(await page.locator('#tournament-category').inputValue(),'other','keyboard selection still works after refresh');
 await page.locator('#tournament-category').selectOption('outage');
 await page.locator('.tournament details summary').click();
 state.tournament.data.server_now++;await page.evaluate(s=>window.__hub.onState(s),state);
 assert.equal(await page.locator('#tournament-category').inputValue(),'outage','refresh must preserve issue type');
 assert.equal(await page.locator('.tournament-support-form textarea').inputValue(),'My private support draft');
 assert.equal(await page.locator('.tournament details').evaluate(e=>e.open),true,'refresh must preserve the open match ledger');
 const sent=page.waitForResponse(r=>r.url().endsWith('/verb/tournament_support'));
 await page.locator('.tournament-support-form button').click();await sent;
 assert.deepEqual(payloads.find(p=>p.verb==='/verb/tournament_support').args,['outage','private-match-reference','My private support draft'],'the retained form submits its current fields');
 state.tournament.ticket_seq++;await page.evaluate(s=>window.__hub.onState(s),state);
 assert.equal(await page.locator('.tournament-support-form textarea').inputValue(),'','a confirmed ticket clears its submitted draft');
 assert.equal(await page.locator('.tournament-support-form input').inputValue(),'');
 await page.locator('.tournament-support-form textarea').fill('Private account draft');
 await page.locator('.tournament-support-form input').fill('Private account reference');
 state.tournament.identity='76561198000000999';state.tournament.data.server_now++;await page.evaluate(s=>window.__hub.onState(s),state);
 assert.equal(await page.locator('.tournament-support-form textarea').inputValue(),'','account switch cannot inherit a private draft');
 assert.equal(await page.locator('.tournament-support-form input').inputValue(),'');
 for(const lang of Object.keys(snapshots)){await show({server_now:event.start_at-50000},lang);for(const width of [1400,800]){await page.setViewportSize({width,height:850});assert.equal(await page.locator('#tournament-register').textContent(),snapshots[lang].tournament.strings.register);assert(await page.locator('#tournament-register').isVisible());}}
 await show({server_now:event.start_at-50000});await page.setViewportSize({width:1400,height:950});fs.mkdirSync(path.join(root,'build'),{recursive:true});await page.screenshot({path:path.join(root,'build/tournament-scheduled.png')});
 await page.locator('#tournament-register').click();assert(verbs.includes('/verb/tournament_register'));
 assert.deepEqual(errors,[]);console.log('Tournament browser: countdown boundaries, navigation, registration, languages and escaped names passed.');
 }finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
