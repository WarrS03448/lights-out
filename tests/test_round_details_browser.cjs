// UTF-8. Run with bundled Playwright in NODE_PATH and HUB_TEST_PYTHON set.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const fixture = JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python', ['-c', `
import json
from tests.test_hub import _web_panel
from tests.test_screen_history import _record
from tests.test_round_details import round_record
from hub.webui.snapshot import state_snapshot
from hub.webui.screens import history as H, postmatch as P
from hub import i18n
p,s=_web_panel()
rec=_record(round_details=[round_record()])
post=dict(match_id='m1',map='Rome',won=True,winner=1,my_team=1,score=[7,3],rounds_played=10,
          teams={str(n):[dict(steam_id=x['steam_id'],name=x['persona'],is_me=n==1) for x in rec['players'] if x['team']==n] for n in [1,2]},
          scoreboard=rec['scoreboard'],round_details=rec['round_details'])
print(json.dumps(dict(state=state_snapshot(s,p),history=H._detail(rec,'1'*17),post=P._card(post),
 languages={k:dict(history=H.strings_for(k),postmatch=P.strings_for(k)) for k in i18n.CODES})))
`], {cwd:root,encoding:'utf8'}));
(async () => {
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try {
  const page=await browser.newPage({viewport:{width:1200,height:760}}), errors=[], calls=[];
  page.on('pageerror', e=>errors.push(e.message));
  const s=fixture.state;s.view='history';s.update=null;s.gamemode_update=null;
  s.history.asked=true;s.history.rows=[];
  s.history.open_id='m1';s.history.open=fixture.history;
  await page.route('http://hub.test/**',route=>{
   const u=new URL(route.request().url());
   if(u.pathname==='/state')return route.fulfill({json:s});
   if(u.pathname==='/events')return route.fulfill({json:[]});
   if(u.pathname.startsWith('/verb/')){calls.push(u.pathname);return route.fulfill({json:{ok:true}});}
   const f=path.join(root,'hub/webui/static',u.pathname==='/'?'index.html':decodeURIComponent(u.pathname));
   return fs.existsSync(f)?route.fulfill({path:f}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');
  for(const prefix of ['md','pm']) {
   if(prefix==='pm') {s.history.open_id='';s.history.open=null;s.postmatch={open:true,card:fixture.post,strings:fixture.languages.en.postmatch};await page.evaluate(s=>__hub.onState(s),s);}
   const overlay=page.locator('.'+prefix+'-overlay');
   await overlay.waitFor();
   assert.equal(await overlay.locator('.round-choice').count(),11,'every round can be selected');
   await overlay.getByRole('button',{name:'Round 2',exact:true}).press('Enter');
   assert.match(await overlay.locator('.round-summary').textContent(),/Team 2 won/);
   const me=overlay.locator('.'+prefix+'-player.sb').filter({hasText:'me'}).first();
   assert.equal(await me.locator('.'+prefix+'-n').first().textContent(),'-1');
   await me.locator('.combat-toggle').click();
   assert.equal(await overlay.locator('.combat-panel:visible [data-stat="damage"] .combat-value').textContent(),'35');
   assert.match(await overlay.locator('.combat-panel:visible .combat-player-damage').textContent(),/them/);
   assert(!await overlay.locator('.combat-panel:visible .combat-player-damage').textContent().then(t=>t.includes('900')));
   s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
   assert.equal(await overlay.getByRole('button',{name:'Round 2',exact:true}).getAttribute('aria-pressed'),'true');
   assert.equal(await overlay.locator('.combat-panel:visible [data-stat="damage"] .combat-value').textContent(),'35','expanded round detail survives background refresh');
   await overlay.getByRole('button',{name:'Round 1',exact:true}).press(' ');
   assert.match(await overlay.locator('.round-summary').textContent(),/not recorded/);
   assert.equal(await overlay.locator('.'+prefix+'-player.sb .'+prefix+'-n').first().textContent(),'—');
   await overlay.getByRole('button',{name:'All rounds',exact:true}).click();
   assert.equal(await overlay.locator('.'+prefix+'-player.sb .'+prefix+'-n').first().textContent(),'14');
   for(const [lang,labels] of Object.entries(fixture.languages)) {
    s.history.strings=labels.history;s.postmatch.strings=labels.postmatch;
    for(const [width,height] of [[800,560],[1200,760]]) {
     await page.setViewportSize({width,height});s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
     await overlay.locator('[data-round="2"]').click();
     assert(await overlay.locator('.round-controls').evaluate(n=>n.scrollWidth<=n.clientWidth+1),lang+' controls fit');
     assert.match(await overlay.locator('.round-title').textContent(),/2/);
    }
   }
   s.history.strings=fixture.languages.en.history;s.postmatch.strings=fixture.languages.en.postmatch;
   if(process.env.HUB_ROUND_SCREENSHOTS) {
    s.history.seq++;await page.evaluate(s=>__hub.onState(s),s);
    await overlay.locator('[data-round="2"]').click();
    const toggle=overlay.locator('.combat-toggle').first();
    if(await toggle.getAttribute('aria-expanded')!=='true')await toggle.click();
    await overlay.locator('.ui-modal').evaluate(n=>n.scrollTop=0);
    await page.screenshot({path:path.join(process.env.HUB_ROUND_SCREENSHOTS,prefix+'-round-detail.png')});
   }
  }
  assert(!calls.some(x=>/close_postmatch|close_match/.test(x)),'selecting rounds never dismisses result');
  assert.deepEqual(errors,[]);
  console.log('Round drill-down: both views, round stats and player damage, keyboard, redraw persistence, all-round restore, 7 languages × 2 sizes.');
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
