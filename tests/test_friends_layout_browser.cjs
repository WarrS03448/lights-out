// UTF-8. Run: node tests/test_friends_layout_browser.cjs (Playwright required).
// HUB_TEST_PYTHON selects the project Python; CHROME_PATH can select Chromium.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const staticRoot = path.join(root, 'hub/webui/static');
const snapshot = JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python', ['-c',
  'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; p,s=_web_panel(); print(json.dumps(state_snapshot(s,p)))'
], {cwd:root,encoding:'utf8'}));
snapshot.view = 'profile';
snapshot.update = null;
snapshot.gamemode_update = null;
snapshot.friends.signed_in = true;
snapshot.friends.code = 'ABCDE-FGHJK';
snapshot.friends.code_masked = '•••••-•••••';
snapshot.friends.code_hidden = true;
const players = Array.from({length:200}, (_, i) => ({steam_id:String(i + 1),persona:'Player ' + i, online: i % 2 === 0, can_invite: i % 2 === 0}));
players[1].avatar = 'https://avatars.steamstatic.com/offline.jpg';
snapshot.friends.list = players;
snapshot.friends.incoming = players.slice(0, 40);
snapshot.friends.outgoing = players.slice(40, 80);
(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH ? {executablePath:process.env.CHROME_PATH} : {channel:'msedge'})});
  try {
    const page=await browser.newPage();
    const errors=[];
    const calls=[];
    const windowCalls=[];
    let maximized=false;
    page.on('pageerror', e=>errors.push(e.message));
    await page.route('http://hub.test/**', async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/state') return route.fulfill({json:snapshot});
      if(url.pathname==='/events') return route.fulfill({json:[]});
      if(url.pathname.startsWith('/window/')) {
        const op=url.pathname.slice('/window/'.length);
        windowCalls.push(op);
        if(op==='maximize')maximized=!maximized;
        return route.fulfill({json:{maximized}});
      }
      if(url.pathname==='/avatar') return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32"><rect width="32" height="32" fill="red"/></svg>'});
      if(url.pathname.startsWith('/verb/')) {
        const verb=url.pathname.split('/').pop(),args=JSON.parse(route.request().postData()||'[]');
        calls.push([verb,...args]);
        if(verb==='set_view')snapshot.view=args[0];
        return route.fulfill({json:{ok:true}});
      }
      const file=path.join(staticRoot,url.pathname==='/' ? 'index.html' : decodeURIComponent(url.pathname));
      return fs.existsSync(file) ? route.fulfill({path:file}) : route.fulfill({status:404,body:''});
    });
    await page.goto('http://hub.test/');
    await page.waitForSelector('#friends-toggle');
    await page.waitForSelector('.wingrip-se');
    async function checkResize() {
      const {width,height}=page.viewportSize();
      const hits=await page.evaluate(()=>{
        const w=innerWidth,h=innerHeight;
        return [['n',w/2,2],['s',w/2,h-2],['e',w-2,h/2],['w',2,h/2],
          ['nw',2,2],['ne',w-2,2],['sw',2,h-2],['se',w-2,h-2]].map(([edge,x,y])=>({
            expected:edge,actual:document.elementFromPoint(x,y)?.closest('.wingrip')?.dataset.edge
          }));
      });
      assert(hits.every(hit=>hit.actual===hit.expected),'All window grips reachable: '+JSON.stringify({width,height,hits}));
      const hidden=await page.locator('#friends-panel').isHidden();
      const start=windowCalls.length;
      await page.mouse.move(width-2,height-2);
      const started=page.waitForResponse('**/window/resize/start/se');
      await page.mouse.down(); await started;
      const moved=page.waitForResponse('**/window/resize/move');
      await page.mouse.move(width-20,height-20,{steps:3}); await moved;
      const ended=page.waitForResponse('**/window/resize/end');
      await page.mouse.up(); await ended;
      assert(windowCalls.slice(start).includes('resize/start/se'));
      assert(windowCalls.slice(start).includes('resize/move'));
      assert(windowCalls.slice(start).includes('resize/end'));
      assert.equal(await page.locator('#friends-panel').isHidden(),hidden,'Resizing does not toggle Friends');
    }
    await checkResize();
    assert(await page.locator('#friends-panel').isHidden(),'Friends starts minimized');
    assert.equal(await page.locator('#app').getByRole('tab',{name:'Friends',exact:true}).count(),0);
    await page.locator('#friends-toggle').click();
    await page.waitForSelector('.fr-wrap');
    const offlineAvatar = page.locator('.fr-section-friend .fr-row').nth(1).locator('img');
    assert.equal(await offlineAvatar.count(), 1, 'offline friend displays their picture');
    await offlineAvatar.evaluate(img => img.decode());
    assert.equal(await offlineAvatar.evaluate(img => img.naturalWidth), 32);
    await offlineAvatar.dispatchEvent('error');
    assert.equal(await offlineAvatar.count(), 0, 'failed picture falls back to initials');
    assert.equal(await page.locator('.fr-section-friend .fr-avatar').nth(1).textContent(), 'P1');
    await page.evaluate(()=>document.fonts.ready);
    for (const [width,height] of [[800,560],[1200,560],[1140,700],[1200,760],[1920,1080]]) {
      await page.setViewportSize({width,height});
      await page.evaluate(()=>HubUI.applyScale());
      const measurements=await page.evaluate(()=>{
        const wrap=document.querySelector('.fr-wrap');
        const app=document.querySelector('#friends-panel').getBoundingClientRect();
        const bar=document.querySelector('#topbar').getBoundingClientRect();
        const fixed=[...document.querySelectorAll('.fr-section-head,.fr-addcard,.fr-codecard')];
        return {overflow:wrap.scrollHeight-wrap.clientHeight, horizontal:wrap.scrollWidth-wrap.clientWidth, belowTopbar:app.top>=bar.bottom,
          panels:fixed.map(n=>{const r=n.getBoundingClientRect();return {name:n.className,visible:r.top>=app.top && r.bottom<=app.bottom+1 && r.right<=app.right+1};})};
      });
      assert(measurements.overflow<=1 && measurements.horizontal<=1 && measurements.belowTopbar && measurements.panels.every(p=>p.visible),JSON.stringify({width,height,measurements}));
      await checkResize();
      await page.locator('#fr-minimize').click();
      await checkResize();
      await page.locator('#friends-toggle').click();
    }
    await page.locator('[data-win="maximize"]').click();
    await page.waitForSelector('html.win-maximized');
    assert.equal(await page.locator('.wingrip:visible').count(),0,'Maximized windows hide resize grips');
    await page.locator('[data-win="maximize"]').click();
    await page.waitForSelector('html:not(.win-maximized)');
    await checkResize();
    const input=page.locator('#fr-code-input');
    await input.fill('ABCDE-');
    snapshot.friends.seq++;
    await page.evaluate(s=>__hub.onState(s),snapshot);
    assert.equal(await input.inputValue(),'ABCDE-','draft survives friend updates');
    assert(await input.evaluate(n=>document.activeElement===n),'typing focus survives friend updates');
    for(const view of ['leaderboard','competitive','history','settings','gamemodes','bugreport','profile']) {
      await page.locator('#nav [data-view="'+view+'"]').click();
      await page.waitForFunction(view=>document.querySelector('#nav .active').dataset.view===view,view);
      assert(await page.locator('#friends-panel').isVisible(),'drawer stays open on '+view);
      assert.equal(await input.inputValue(),'ABCDE-');
    }
    await page.getByRole('button',{name:'Minimize friends',exact:true}).click();
    assert(await page.locator('#friends-panel').isHidden());
    await page.locator('#friends-toggle').click();
    assert.equal(await input.inputValue(),'ABCDE-','draft survives minimize');
    await input.fill(' QWERT-YUIOP ');await input.press('Enter');
    await page.waitForFunction(()=>document.querySelector('#fr-code-input').value==='');
    const friend=page.locator('.fr-section-friend .fr-row').first();
    await friend.click({button:'right'});
    assert(await page.getByRole('menuitem',{name:'Invite',exact:true}).isEnabled());
    await page.getByRole('menuitem',{name:'Invite',exact:true}).click();
    await friend.press('Shift+F10');
    await page.getByRole('menuitem',{name:'Remove',exact:true}).click();
    await page.locator('.fr-section-friend .fr-row').nth(1).click({button:'right'});
    assert(await page.getByRole('menuitem',{name:'Invite',exact:true}).isDisabled(),'offline invites stay disabled');
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('menu').count(),0);
    assert(await page.locator('#friends-panel').isVisible(),'Escape closes menu first');
    await friend.press('Enter');await page.locator('#nav [data-view="leaderboard"]').click();
    assert.equal(await page.getByRole('menu').count(),0,'outside click closes menu');
    await page.waitForFunction(()=>document.querySelector('#nav .active').dataset.view==='leaderboard');
    for(const expected of [['friend_add','QWERT-YUIOP'],['friend_invite','1'],['friend_remove','1']]) {
      assert(calls.some(call=>JSON.stringify(call)===JSON.stringify(expected)),JSON.stringify(expected));
    }
    await page.setViewportSize({width:1200,height:760});
    if(process.env.HUB_SCREENSHOT)await page.screenshot({path:process.env.HUB_SCREENSHOT});
    for(const kind of ['friend','incoming','outgoing']) {
      const list=page.locator('.fr-section-'+kind+' .fr-list');
      assert(await list.evaluate(n=>n.scrollHeight>n.clientHeight),kind+' must scroll');
      await list.evaluate(n=>{n.scrollTop=n.scrollHeight;n.dispatchEvent(new Event('scroll'))});
      const before=await list.evaluate(n=>n.scrollTop);
      snapshot.friends.seq++;
      await page.evaluate(s=>__hub.onState(s),snapshot);
      await page.waitForTimeout(60);
      assert.equal(await list.evaluate(n=>n.scrollTop),before,kind+' position survives snapshots');
    }
    snapshot.friends.list=[];snapshot.friends.incoming=[];snapshot.friends.outgoing=[];
    snapshot.friends.error='Could not send this request. Please try again.';
    snapshot.friends.invite_error='The party is currently full.';
    snapshot.friends.seq++;
    await page.setViewportSize({width:800,height:560});
    await page.evaluate(s=>__hub.onState(s),snapshot);
    assert(await page.locator('.fr-wrap').evaluate(n=>n.scrollHeight<=n.clientHeight+1),'empty/error state fits');
    assert.equal(await page.locator('.fr-empty').count(),1);
    snapshot.friends.signed_in=false;snapshot.friends.seq++;
    await page.evaluate(s=>__hub.onState(s),snapshot);
    assert(await page.locator('#friends-panel').isHidden(),'sign-out minimizes the drawer');
    await page.locator('#friends-toggle').click();
    assert.equal(await page.locator('#fr-code-input').count(),0,'signed-out drawer does not expose friend code actions');
    assert((await page.locator('#friends-panel').textContent()).includes('Sign in'));
    assert.deepEqual(errors,[]);
    console.log('Friends: minimized dock, 7 screens, typing/scroll persistence, right-click and keyboard actions, all resize grips and southeast dragging open/closed at 5 sizes, maximize/restore, empty/error/signed-out states.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
