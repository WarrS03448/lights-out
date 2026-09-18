// Run with Playwright available on NODE_PATH: node server/scripts/test-analytics-browser.cjs
const http=require('node:http'),assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require('playwright');
const analytics=require('../analytics.cjs').create(),sid='76561198000000001';
const admin=require('../admin.cjs').create({analytics,upstashCmd:async()=>({steam_id:sid}),live:()=>({isAdmin:()=>true})});
const server=http.createServer(async(req,res)=>{const u=new URL(req.url,'http://local');try{if(!await admin.route(req,res,req.method,u.pathname,u)){res.writeHead(404);res.end();}}catch(e){res.writeHead(500);res.end(e.message);}});
(async()=>{
  for(let i=0;i<65;i++){
    const at=Date.now()-(i%7)*86400000,ids=Array.from({length:10},(_,n)=>(76561198000000001n+BigInt(n)).toString());
    const board=ids.map((steam_id,n)=>({steam_id,kills:10+n,deaths:5,team_kills:0,reported:true,combat:{enemyDamage:1000,coverage:{damage:true}}}));
    const players=ids.map((steam_id,n)=>({steam_id,persona:n?'Player '+(n+1):'<img src=x onerror="alert(1)">',team:n<5?1:2}));
    await analytics.project({matchId:'fixture-'+i,at,winner:1,score:{1:7,2:4},analytics_context:{hub:'2.3.83',predicted_win:.5,rules:{test:true}},
      inputs:{teams:{1:ids.slice(0,5),2:ids.slice(5)},mm:{quality:.9,waited:42,spread:100,parties:[ids.slice(0,2)]}},
      publicMatch:{id:'fixture-'+i,map:i%2?'Hospital':'Rome',created:at-600000,ended:at,players,sides:{1:'attack',2:'defend'},round_details:[{n:1,won:1,score:[1,0],scoreboard:board}]},board,
      rows:ids.map((steamId,n)=>({steamId,won:n<5,before:{rating:1500,rd:80,progress:100,matches:30},after:{rating:n<5?1512:1488,rd:78,progress:n<5?123:78,matches:31},delta:n<5?12:-12,rr:{delta:n<5?23:-22,factors:{base:23,convergence:1}},valuation:{weight:1,breakdown:{measured:true,presence:1,integrity:1}}}))});
  }
  await analytics.ingest([{id:'browser-event',type:'request.outcome',at:Date.now(),version:'2.3.83',severity:'error',data:{action:'api.queue',duration_ms:324,status:503}}],{actor_id:sid,source:'client'});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));const url='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try{
    const context=await browser.newContext();await context.addCookies([{name:'hubadmin',value:'fixture',url}]);
    const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
    fs.mkdirSync('work/analytics-browser',{recursive:true});
    for(const width of [1440,390]){
      await page.setViewportSize({width,height:1000});await page.goto(url+'/admin/analytics');await page.locator('.card').first().waitFor();
      assert.equal(await page.locator('.card').filter({hasText:'Completed matches'}).locator('.value').innerText(),'65');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:`work/analytics-browser/overview-${width}.png`,fullPage:true});
      for(const view of ['balance','ratings','reliability','audits','events','matches']){
        await page.locator(`[data-view="${view}"]`).first().click();await page.waitForFunction(()=>document.querySelector('#notice').textContent!=='Loading…');
        assert.equal(await page.locator('#notice.error').count(),0);
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'overflow '+view+' '+width);
        if(view==='reliability')assert.match(await page.locator('#content').innerText(),/≤ 500 ms/);
      }
      await page.locator('[name=map]').fill('Rome');await page.getByRole('button',{name:'Apply filters',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#notice').textContent!=='Loading…');
      assert.equal(await page.locator('#content tbody tr').count(),33);
      await page.locator('[name=size]').selectOption('2');await page.getByRole('button',{name:'Apply filters',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#notice').textContent!=='Loading…');
      assert.match(await page.locator('#content').innerText(),/No evidence/);
      await page.locator('[name=size]').selectOption('10');await page.locator('[name=map]').fill('');await page.getByRole('button',{name:'Apply filters',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#notice').textContent!=='Loading…');
      await page.locator('[data-match]').first().click();await page.locator('#detail-body table').first().waitFor();
      assert.equal(await page.locator('#detail-body img').count(),0);
      await page.getByText(/Round 1 · Team 1 won/).click();await page.screenshot({path:`work/analytics-browser/match-${width}.png`});
      assert.match(await page.locator('#detail-body').innerText(),/reported/i);
      await page.locator('#close-detail').click();await page.locator('#next').click();await page.waitForFunction(()=>document.querySelector('#notice').textContent!=='Loading…');
      assert.equal(await page.locator('#content tbody tr').count(),15);
      const csv=await context.request.get(url+'/admin/analytics/export?kind=matches&format=csv&size=all');assert.equal(csv.status(),200);assert.match(await csv.text(),/fixture-/);
    }
    assert.deepEqual(errors,[]);console.log('Analytics browser: seven views, two widths, filters, pagination, round detail, escaping and export passed.');
  }finally{await browser.close();await analytics.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;server.close();analytics.close();});
