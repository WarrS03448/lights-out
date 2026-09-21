// Run with Playwright in NODE_PATH and HUB_TEST_PYTHON set to the project Python.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),http=require('node:http'),{execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..'),a='76561198000000001',b='76561198000000002';
const snapshots=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',`import json
from tests.test_screen_bugreport import _panel
from hub.webui.snapshot import state_snapshot
from hub import i18n
p,s=_panel();p.view='messages'
out={}
for lang in i18n.CODES:
 i18n.set_language(lang);out[lang]=state_snapshot(s,p)
print(json.dumps(out))`],{cwd:root,encoding:'utf8',maxBuffer:8000000}));
let state=snapshots.en;const calls=[],adminCalls=[],adminMessages=[{id:'first',seq:1,official:false,sender:b,at:Date.now(),text:'Hello <script>window.pwned=true</script>'}];let failSend=true;
const admin=require('../server/admin.cjs'),adminUI=admin.create({live:()=>({isAdmin:()=>true}),upstashCmd:async args=>args[0]==='GET'&&args[1]==='hub:adminsession:browser-fixture'?JSON.stringify({steam_id:a}):null});
const server=http.createServer((req,res)=>{const url=new URL(req.url,'http://localhost'),json=data=>{res.setHeader('content-type','application/json');res.end(JSON.stringify(data));};
 if(url.pathname==='/admin/messages'){req.headers.cookie=admin.COOKIE+'=browser-fixture';adminUI.route(req,res,req.method,url.pathname,url).catch(e=>{res.statusCode=500;res.end(e.message);});return;}
 if(url.pathname==='/admin/messages/data'){json({ok:true,unread:1,threads:[{target:b,persona:'Player <Two>',last_text:'Hello',unread:1}]});return;}
 if(url.pathname==='/admin/messages/thread'){json({ok:true,messages:adminMessages,next_before:null});return;}
 if(url.pathname==='/admin/messages/send'){let body='';req.on('data',s=>body+=s);req.on('end',()=>{const data=JSON.parse(body);adminCalls.push(data);if(!adminMessages.some(m=>m.id===data.client_id))adminMessages.push({id:data.client_id,seq:adminMessages.length+1,official:true,text:data.text,at:Date.now()});if(failSend){failSend=false;res.statusCode=503;json({ok:false,error:"Delivery not confirmed"});}else json({ok:true});});return;}
 if(url.pathname==='/admin/messages/read'){json({ok:true});return;}
 if(url.pathname.startsWith('/verb/')){let body='';req.on('data',s=>body+=s);req.on('end',()=>{calls.push({path:url.pathname,body:JSON.parse(body||'[]')});json({ok:true});});return;}
 if(url.pathname==='/state'||url.pathname==='/events'||url.pathname.startsWith('/window/')){json(url.pathname==='/state'?state:url.pathname==='/events'?{events:[],seq:0}:{maximized:false});return;}
 const base=path.join(root,'hub/webui/static'),file=path.resolve(base,url.pathname==='/'?'index.html':decodeURIComponent(url.pathname.slice(1)));if(!file.startsWith(base+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}res.setHeader('content-type',({'.html':'text/html','.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'})[path.extname(file)]||'application/octet-stream');fs.createReadStream(file).pipe(res);
});
(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
 try{const page=await browser.newPage({viewport:{width:1400,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));await page.clock.install();const url='http://127.0.0.1:'+server.address().port;await page.goto(url);await page.locator('.mail').waitFor();
 async function show(lang='en',identity=a,target='admin'){state=structuredClone(snapshots[lang]);state.friends.list=[{steam_id:b,persona:'My Friend'}];Object.assign(state.messages,{signed_in:true,identity,target,seq:0,data:{unread:2,threads:[{target:'admin',persona:'Lights Out Admin',official:true,unread:2,last_text:'Your ticket has a response.'}]},thread:{messages:[{seq:1,sender:'admin',official:true,text:'<script>bad()</script>',at:Date.now()}],blocked:false},error:''});await page.evaluate(s=>window.__hub.onState(s),state);}
 await show();assert.equal(await page.locator('#messages-unread').textContent(),'2');assert.equal(await page.locator('.mail-select option').count(),2);assert.equal(await page.locator('.mail-message script').count(),0);assert(await page.locator('.mail-compose').isVisible());
 // A native dropdown cannot stay open if a snapshot detaches its control.
 await page.locator('.mail-select').click();
 await page.evaluate(()=>{window.friendPicker=document.querySelector('.mail-select');});
 for(let i=0;i<12;i++){
   state.tournament.data={server_now:Date.now()+i*300};
   if(i===6)state.messages.data.unread=3;
   await page.evaluate(s=>window.__hub.onState(s),state);await page.clock.runFor(50);
   assert(await page.evaluate(()=>friendPicker.isConnected&&friendPicker===document.querySelector('.mail-select')&&document.activeElement===friendPicker),'refresh closed the friend picker');
 }
 assert.equal(await page.locator('#messages-unread').textContent(),'3','unread count must keep updating with the picker open');
 await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');await page.waitForTimeout(50);
 assert(calls.some(c=>c.path==='/verb/messages_open'&&c.body[0]===b),'the open picker must still select a friend after refresh');
 assert.equal(await page.locator('.mail-select').inputValue(),'','a friend can be selected again after visiting another conversation');
 await page.locator('.mail-select').click();
 state.friends.list.push({steam_id:'76561198000000003',persona:'New Friend'});
 await page.evaluate(s=>window.__hub.onState(s),state);
 assert(await page.evaluate(()=>friendPicker.isConnected&&document.activeElement===friendPicker));
 assert.equal(await page.locator('.mail-select option').count(),2,'do not modify an open native menu');
 await page.keyboard.press('Escape');await page.locator('.mail-compose textarea').focus();
 assert.equal(await page.locator('.mail-select option').count(),3,'apply the latest friends after the interaction');
 // Read acknowledgments still run after background visibility changes without rebuilding.
 await page.evaluate(()=>Object.defineProperty(document,'hidden',{configurable:true,value:true}));
 state.messages.thread.messages.push({seq:2,sender:'admin',official:true,text:'An unread message',at:Date.now()});
 await page.evaluate(s=>window.__hub.onState(s),state);await page.waitForTimeout(50);
 const readsBefore=calls.filter(c=>c.path==='/verb/messages_read').length;
 await page.evaluate(()=>{delete document.hidden;});state.tournament.data.server_now++;
 await page.evaluate(s=>window.__hub.onState(s),state);await page.waitForTimeout(50);
 assert.equal(calls.filter(c=>c.path==='/verb/messages_read').length,readsBefore+1,'read acknowledgment resumes when visible with unchanged message data');
 await page.clock.runFor(15100);state.tournament.data.server_now++;
 await page.evaluate(s=>window.__hub.onState(s),state);await page.waitForTimeout(50);
 assert(calls.filter(c=>c.path==='/verb/messages_read').length>readsBefore+1,'read acknowledgment retries even without new mail');
 await page.locator('.mail-compose textarea').fill('A private reply');await page.evaluate(s=>window.__hub.onState(s),state);assert.equal(await page.locator('.mail-compose textarea').inputValue(),'A private reply');
 await page.locator('.mail-compose button').click();await page.waitForTimeout(50);assert(calls.some(c=>c.path==='/verb/messages_send'&&c.body[0]==='A private reply'));
 await show('en',b);assert.equal(await page.locator('.mail-compose textarea').inputValue(),'','another account must not inherit the previous draft');
 await show('en',a,b);await page.locator('.mail-compose textarea').fill('Friend draft');await show('en',a,'admin');await page.locator('.mail-compose textarea').fill('  Admin draft  ');
 state.messages.seq=1;state.messages.sent={target:'admin',text:'Admin draft',id:'sent-admin'};await page.evaluate(s=>window.__hub.onState(s),state);assert.equal(await page.locator('.mail-compose textarea').inputValue(),'');
 await show('en',a,b);assert.equal(await page.locator('.mail-compose textarea').inputValue(),'Friend draft','sending in another conversation preserves this draft');
 for(const lang of Object.keys(snapshots)){await show(lang);for(const width of [1400,800]){await page.setViewportSize({width,height:900});assert(await page.locator('.mail-compose button').isVisible());assert.equal(await page.locator('.mail-compose button').textContent(),snapshots[lang].messages.strings.send);}}
 await page.goto(url+'/admin/messages?target='+b);await page.locator('.chat-bubble').waitFor();assert.equal(await page.locator('.chat-bubble script').count(),0);assert.equal(await page.locator('#admin-chat-title').textContent(),'Player <Two>');
 await page.locator('#admin-chat-text').fill('A saved admin reply');await page.locator('#admin-chat-send').click();await page.waitForFunction(()=>document.querySelector('#admin-chat-status').textContent.includes('Retrying'),null,{timeout:5000}).catch(async e=>{console.error({status:await page.locator('#admin-chat-status').textContent(),adminCalls,errors});throw e;});
 assert.equal(await page.locator('#admin-chat-text').inputValue(),'A saved admin reply');
 await page.locator('#admin-chat-target').fill('76561198000000003');await page.locator('#admin-chat-open').click();
 await page.locator('#admin-chat-target').fill(b);await page.locator('#admin-chat-open').click();
 assert.equal(await page.locator('#admin-chat-text').inputValue(),'A saved admin reply');
 await page.locator('#admin-chat-send').click();await page.waitForFunction(()=>document.querySelector('#admin-chat-status').textContent==='Message delivered.');
 assert.equal(adminCalls.length,2);assert.equal(adminCalls[0].client_id,adminCalls[1].client_id);assert.equal(adminMessages.length,2);assert.equal(await page.locator('.chat-bubble').count(),2);
 await page.reload();await page.locator('.chat-bubble').first().waitFor();assert.equal(await page.locator('.chat-bubble').count(),2);assert.equal(await page.locator('#admin-chat-older').isVisible(),false);assert.equal(await page.locator('#admin-chat-more').isVisible(),false);assert.deepEqual(errors,[]);fs.mkdirSync(path.join(root,'build'),{recursive:true});await page.setViewportSize({width:1400,height:1000});await page.evaluate(()=>scrollTo(0,0));await page.screenshot({path:path.join(root,'build/admin-conversation.png'),fullPage:true});console.log('Messaging browser: seven languages, account isolation, official replies, saved admin history and duplicate-safe retry passed.');
 }finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
