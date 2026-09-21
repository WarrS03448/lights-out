// UTF-8. Run against tools/dev/lobby_preview.py with Playwright on NODE_PATH.
const assert=require('node:assert/strict');
const {execFileSync}=require('node:child_process');
const {chromium}=require('playwright');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page=await browser.newPage(),errors=[],calls=[];
    page.on('pageerror',e=>errors.push(e.message));
    const url=process.argv[2]||'http://127.0.0.1:8859';
    const snapshot=await (await page.request.get(url+'/state')).json();
    const data=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
      "import sys,json;sys.path.insert(0,'tests');from test_screen_postmatch import _session,_card;from hub.i18n import STRINGS;s=_session();s.register_match_complete=lambda source:None;s.on_live_event({'type':'match_result','match_id':s.match_id,'voided':True,'void_reason':'vote'});print(json.dumps({'languages':STRINGS,'postmatch':_card(s)}))"],{encoding:'utf8'}));
    snapshot.view='competitive';snapshot.comp.phase='live';
    snapshot.comp.live={...snapshot.comp.connect,teams:snapshot.comp.lobby.teams,can_vote:true,vote:null,can_finish:false};
    await page.route('**/state',route=>route.fulfill({json:snapshot}));
    await page.route('**/verb/*',route=>{calls.push({path:new URL(route.request().url()).pathname,body:route.request().postDataJSON()});return route.fulfill({json:{ok:true}});});
    await page.goto(url);
    for(const [lang,strings] of Object.entries(data.languages)) {
      snapshot.strings=strings;snapshot.lang=lang;
      for(const width of [800,1200]){
        await page.setViewportSize({width,height:760});
        await page.evaluate(s=>window.__hub.onState(s),snapshot);
        const button=page.locator('button.void-vote-button');await button.waitFor();
        assert.equal(await button.textContent(),strings.comp_report);
        const bounds=await button.boundingBox();
        assert(bounds.width>100&&bounds.height>30&&bounds.x>=0&&bounds.x+bounds.width<=width+1,JSON.stringify({lang,width,bounds}));
        assert.equal(await button.evaluate(n=>parseFloat(getComputedStyle(n).minHeight)>=44),true);
        assert.equal(await button.evaluate(n=>n.scrollWidth<=n.clientWidth+1),true);
      }
    }
    snapshot.lang='en';snapshot.strings=data.languages.en;
    await page.evaluate(s=>window.__hub.onState(s),snapshot);
    const button=page.getByRole('button',{name:'Cheater in the game? Click to vote to void match. 7/10 Votes Required',exact:true});
    await page.screenshot({path:'work-void-vote-button.png'});
    await button.focus();await page.keyboard.press('Enter');
    await page.waitForFunction(()=>true);
    assert(calls.some(c=>c.path==='/verb/start_vote'));
    snapshot.comp.live.vote={yes:6,no:0,needed:7,voted:false};
    await page.evaluate(s=>window.__hub.onState(s),snapshot);
    await page.getByRole('button',{name:'Yes',exact:true}).click();
    assert(calls.some(c=>c.path==='/verb/cast_vote'&&c.body[0]===true));
    snapshot.comp.live.vote.voted=true;
    await page.evaluate(s=>window.__hub.onState(s),snapshot);
    assert.equal(await page.getByRole('button',{name:'Yes',exact:true}).count(),0);
    snapshot.postmatch=data.postmatch;
    await page.evaluate(s=>window.__hub.onState(s),snapshot);
    await page.locator('.pm-verdict.void').waitFor();
    assert.equal(await page.locator('.pm-headline').textContent(),'Match voided');
    assert.equal(await page.locator('.pm-headline').evaluate(n=>getComputedStyle(n).textTransform),'uppercase');
    assert.equal(await page.locator('.pm-verdict.win,.pm-verdict.loss').count(),0);
    await page.screenshot({path:'work-voided-result.png'});
    snapshot.comp.phase='idle';await page.evaluate(s=>window.__hub.onState(s),snapshot);
    assert.equal(await page.locator('.pm-verdict.void').count(),1);
    assert.deepEqual(errors,[]);
    console.log('Void button: seven languages, two widths, keyboard/click votes; MATCH VOIDED overlay stays open.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
