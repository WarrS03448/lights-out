'use strict';
// Run with Playwright on NODE_PATH: node server/scripts/test-account-console-browser.cjs
const http=require('node:http'),assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require('playwright');
const ADMIN='76561198000000001',OLD='76561198000000002',GAME='76561198000000003';
const PLAYER='12345678-1234-1234-1234-123456789abc',ACCOUNT='87654321-4321-4321-4321-cba987654321';
const at=Date.now(),name='<img src=x onerror="alert(1)">';
let available=true;
const inventory={snapshot:()=>({available,stale:!available,updated_at:at,reassigned:[OLD],rows:[
  {player_id:OLD,account_id:ACCOUNT,persona:name,account_type:'Lights Out',account_created:at,linked_steam_id:'',steam_login_id:''},
  {player_id:PLAYER,account_id:'',persona:'New Steam profile',account_type:'Steam',account_created:0,linked_steam_id:'',steam_login_id:OLD},
]})};
const analytics=require('../analytics.cjs').create();
const live=require('../live.cjs').create({whoami:async()=>null,bearer:()=>'',sendJson:()=>{},badRequest:()=>{},
  readBody:async()=>Buffer.alloc(0),prefix:'browser:',accountDirectory:inventory});
live._internals.ADMIN_IDS.add(ADMIN);
live._internals.noteSeen(GAME,'Steam Player');
live._internals.noteSeen(PLAYER,'New Steam profile',{game_steam_id:OLD,auth_method:'steam'});
const admin=require('../admin.cjs').create({analytics,upstashCmd:async()=>({steam_id:ADMIN}),live:()=>live});
const server=http.createServer(async(req,res)=>{try{const u=new URL(req.url,'http://local');
  if(!await admin.route(req,res,req.method,u.pathname,u)){res.writeHead(404);res.end();}
}catch(e){res.writeHead(500);res.end(e.message);}});
(async()=>{
  for(const [player,id] of [[OLD,'old-profile-match'],[PLAYER,'new-profile-match']]) {
    await analytics.project({matchId:id,at,winner:1,score:{1:7,2:0},inputs:{teams:{1:[player],2:[]}},
      publicMatch:{id,ended:at,map:'Rome',players:[{player_id:player,game_steam_id:OLD,persona:name}]},
      rows:[{steamId:player,won:true,before:{},after:{}}]});
  }
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const url='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const context=await browser.newContext();await context.addCookies([{name:'hubadmin',value:'fixture',url}]);
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    fs.mkdirSync('work/account-console-browser',{recursive:true});
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:1000});
      await page.goto(url+'/admin');
      assert.deepEqual(await page.locator('#account-stats b').allTextContents(),['3','1','0','2']);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overview overflow');
      await page.screenshot({path:`work/account-console-browser/overview-${width}.png`,fullPage:true});
      await page.goto(url+'/admin/players?player_id='+OLD);
      await page.waitForFunction(()=>document.querySelector('#count').textContent.includes('1 of 3'));
      assert.equal(await page.locator('#rows tr').count(),1);
      assert.equal(await page.locator('#q').inputValue(),OLD);
      assert.equal(await page.locator('#rows img').count(),0);
      assert.match(await page.locator('#rows').innerText(),/Lights Out/);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'players overflow');
      await page.screenshot({path:`work/account-console-browser/players-${width}.png`,fullPage:true});
      await page.locator('#q').fill(ACCOUNT);
      await page.waitForFunction(()=>document.querySelector('#count').textContent.includes('1 of 3'));
      await page.locator('#rows a').first().click();
      await page.waitForFunction(()=>document.querySelector('#notice').textContent!=='Loading…');
      assert.match(await page.locator('#content').innerText(),/old-profile-match/);
      assert.doesNotMatch(await page.locator('#content').innerText(),/new-profile-match/);
      await page.locator('[data-match="old-profile-match"]').click();
      await page.locator('#detail-body table').first().waitFor();
      assert.match(await page.locator('#detail-body').innerText(),/Game Steam:/);
      assert.equal(await page.locator('#detail-body img').count(),0);
      await page.locator('#detail-body a').first().click();
      await page.waitForFunction(()=>document.querySelector('#count').textContent.includes('1 of 3'));
      await page.goto(url+'/admin/analytics');
      await page.locator('#account-population').waitFor();
      assert.deepEqual(await page.locator('#account-population .value').allTextContents(),['3','1','0','2','1']);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'analytics overflow');
      await page.screenshot({path:`work/account-console-browser/analytics-${width}.png`,fullPage:true});
    }
    available=false;await page.goto(url+'/admin/analytics');await page.locator('#account-population').waitFor();
    assert.equal(await page.locator('#account-population .value').first().innerText(),'—');
    assert.match(await page.locator('#account-population').innerText(),/unavailable/);
    const denied=await fetch(url+'/admin/analytics/data');assert.equal(denied.status,401);
    const data=await context.request.get(url+'/admin/analytics/data');
    assert.doesNotMatch(await data.text(),/password_hash|email|session_token/);
    assert.deepEqual(errors,[]);
    console.log('Account console browser: all three pages, desktop/mobile, exact profile links, UUID/account search, escaped names and unavailable state passed.');
  }finally{await browser.close();await analytics.close();await live.shutdown();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();analytics.close();live.shutdown();});
