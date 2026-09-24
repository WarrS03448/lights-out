'use strict';
// Run with Playwright on NODE_PATH: node server/scripts/test-admin-queue-browser.cjs
process.env.NODE_ENV='test';process.env.COMP_NETWORK_TEST_BYPASS='1';
const http=require('node:http'),assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require('playwright');
const ADMIN='76561198000000001',PLAYER='12345678-1234-1234-1234-123456789abc';
let signedIn=true;
const inventory={snapshot:()=>({available:true,stale:false,updated_at:Date.now(),rows:[
  {player_id:PLAYER,account_id:PLAYER,persona:'Queue Player',account_type:'Lights Out',account_created:Date.now(),linked_steam_id:'',steam_login_id:''},
]})};
const live=require('../ranked-service.cjs').create({whoami:async()=>null,bearer:()=>'',sendJson:()=>{},badRequest:()=>{},
  readBody:async()=>Buffer.alloc(0),prefix:'queue-browser:',accountDirectory:inventory});
live._internals.ADMIN_IDS.add(ADMIN);
const admin=require('../admin.cjs').create({upstashCmd:async()=>signedIn?{steam_id:ADMIN}:null,live:mode=>mode?live.forMode(mode):live});
const server=http.createServer(async(req,res)=>{try{const u=new URL(req.url,'http://local');
  if(!await admin.route(req,res,req.method,u.pathname,u)){res.writeHead(404);res.end();}
}catch(e){res.writeHead(500);res.end(e.message);}});
(async()=>{
  await live._internals.ensureRecovery();
  const duel=live.forMode('BB1')._internals,team=live.forMode('BB5')._internals;
  team.bySteam.set(PLAYER,new Set(['fixture-connection']));
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const url='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try{
    const context=await browser.newContext();await context.addCookies([{name:'hubadmin',value:'fixture',url}]);
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    fs.mkdirSync('work/admin-queue-browser',{recursive:true});
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});
      assert.ok(duel.enqueue([PLAYER],'',Date.now()));
      await page.goto(url+'/admin/players?player_id='+PLAYER);
      await page.locator('#rows .c-status').waitFor();
      assert.equal(await page.locator('#rows .c-status').textContent(),'queued · Bodybomb 1v1');
      await page.locator('#columns').click();await page.locator('#q').focus();
      await page.locator('#q').evaluate(el=>el.setSelectionRange(2,7));
      await page.evaluate(()=>{window.queueModeControl=document.querySelector('#ranked-mode');});
      const scroll=await page.locator('.table-scroll').evaluate((el,width)=>{
        el.scrollLeft=width<500?document.querySelector('#rows .c-status').offsetLeft:0;
        return el.scrollLeft;
      },width);
      // These are different responses, not repeated reads of an unchanged fixture.
      for(const [engine,label]of [[null,'online'],[team,'queued · Bodybomb 5v5'],[duel,'queued · Bodybomb 1v1']]){
        duel.removeFromQueue(PLAYER);team.removeFromQueue(PLAYER);
        if(engine)assert.ok(engine.enqueue([PLAYER],'',Date.now()));
        const refreshed=page.waitForResponse(r=>r.url().includes('/admin/players/data?'));
        await page.locator('#refresh').evaluate(el=>el.click());
        const snapshot=await (await refreshed).json();
        assert.equal(snapshot.rows[0].status,label.split(' · ')[0],JSON.stringify({width,label,snapshot}));
        await page.waitForFunction(want=>document.querySelector('#rows .c-status')?.textContent===want,label,{timeout:5000});
        assert.deepEqual(await page.locator('#q').evaluate(el=>[document.activeElement===el,el.selectionStart,el.selectionEnd]),[true,2,7]);
        assert.equal(await page.locator('#picker').isVisible(),true);
        assert.equal(await page.evaluate(()=>window.queueModeControl===document.querySelector('#ranked-mode')),true);
        assert.equal(await page.locator('#q').inputValue(),PLAYER);
        assert.equal(await page.locator('.table-scroll').evaluate(el=>el.scrollLeft),scroll);
      }
      await page.locator('#ranked-mode').focus();
      const response=page.waitForResponse(r=>r.url().includes('/admin/players/data?mode=BB1'));
      await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');await response;
      assert.equal(await page.locator('#ranked-mode').inputValue(),'BB1');
      assert.equal(await page.locator('#rows .c-status').textContent(),'queued · Bodybomb 1v1');
      await page.keyboard.press('Escape');await page.locator('#columns').click();
      await page.screenshot({path:`work/admin-queue-browser/queued-1v1-${width}.png`,fullPage:true});
      duel.removeFromQueue(PLAYER);await page.locator('#refresh').click();
      await page.waitForFunction(()=>document.querySelector('#rows .c-status')?.textContent==='online');
    }
    const anonymous=await fetch(url+'/admin/players/data');
    assert.match(anonymous.headers.get('content-type'),/text\/html/);
    assert.doesNotMatch(await anonymous.text(),/Queue Player/);
    await context.request.get(url+'/admin/logout',{maxRedirects:0});
    signedIn=false;
    const signedOut=await context.request.get(url+'/admin/players/data');
    assert.match(signedOut.headers()['content-type'],/text\/html/);
    assert.doesNotMatch(await signedOut.text(),/Queue Player/);
    await page.goto(url+'/admin/players');assert.equal(await page.locator('#rows').count(),0);
    assert.deepEqual(errors,[]);
    console.log('Admin queue browser: both ladders, queue cancellation, desktop/mobile, changed refreshes, retained search/caret/picker/mode control, keyboard and sign-out guards passed.');
  }finally{await browser.close();await live.shutdown();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();live.shutdown();});
