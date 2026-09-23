// Run with Playwright in NODE_PATH and HUB_TEST_PYTHON pointing to the project Python.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const snapshots = JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON || 'python', ['-c', `
import json
from tests.test_screen_topbar import _panel
from hub import i18n
from hub.webui.snapshot import state_snapshot
panel, session = _panel()
session.on_live_event({'type':'stats','online':1284,'queued':137,'live_matches':42,'players_registered':1234567})
session._tournament_result(200, {'ok': True, 'entrant_count': 1234})
result = {}
for lang in i18n.CODES:
    i18n.set_language(lang)
    result[lang] = state_snapshot(session, panel)
print(json.dumps(result))
`], {cwd:root, encoding:'utf8', maxBuffer:8*1024*1024}));
// Deliver a changed snapshot after all screen/overlay scripts have loaded, so
// startup fetch timing cannot leave the message control out of the layout test.
let state = {...snapshots.en,status:{...snapshots.en.status,players_registered:0}};
const mime = {'.js':'text/javascript', '.css':'text/css', '.html':'text/html', '.woff2':'font/woff2', '.svg':'image/svg+xml'};
const server = http.createServer((req,res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/verb/')) {
    res.writeHead(200, {'content-type':'application/json'});
    res.end('{}'); return;
  }
  if (url.pathname.startsWith('/window/')) {
    res.writeHead(200, {'content-type':'application/json'});
    res.end(JSON.stringify({maximized:false})); return;
  }
  if (url.pathname === '/state' || url.pathname === '/events') {
    res.writeHead(200, {'content-type':'application/json'});
    res.end(JSON.stringify(url.pathname === '/state' ? state : {events:[],seq:0})); return;
  }
  const name = url.pathname === '/' ? 'index.html' : decodeURIComponent(url.pathname.slice(1));
  const base = path.join(root,'hub/webui/static');
  const file = path.resolve(base,name);
  if (!file.startsWith(base + path.sep) || !fs.existsSync(file)) {res.writeHead(404);res.end();return;}
  res.writeHead(200, {'content-type':mime[path.extname(file)] || 'application/octet-stream'});
  fs.createReadStream(file).pipe(res);
});
(async()=>{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({headless:true,executablePath:process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  try {
    const page=await browser.newPage({viewport:{width:1400,height:850}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:'+server.address().port);
    await page.locator('#stattournament').waitFor();
    await page.evaluate(()=>document.fonts.ready);
    async function render(next) {state=structuredClone(next);await page.evaluate(s=>window.__hub.onState(s),state);}
    await render(snapshots.en);
    await page.locator('#messages-toggle').waitFor({state:'visible'});
    assert.equal(await page.locator('#statqueued, #statlive').count(),0);
    assert.equal(await page.locator('#statregistered').textContent(),'1234567 registered in Lights Out');
    assert.equal(await page.locator('#stattournament').textContent(),'1234 registered for tournament');
    for(const [lang,snapshot] of Object.entries(snapshots)) {
      await render(snapshot);
      assert.equal(await page.locator('#statregistered').textContent(),snapshot.strings.topbar_registered.replace('{n}','1234567'));
      assert.equal(await page.locator('#stattournament').textContent(),snapshot.strings.topbar_tournament.replace('{n}','1234'));
      for(const [width,height] of [[1600,850],[1401,760],[1400,760],[1200,760],[1135,760],[893,560],[815,560],[805,560],[800,560]]) {
        await page.setViewportSize({width,height});
        await page.evaluate(async()=>{await document.fonts.ready;await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);});
        const bounds=await page.evaluate(()=>{
          const box=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {x:r.x,right:r.right,y:r.y,bottom:r.bottom};};
          return {registered:box('#statregistered'),tournament:box('#stattournament'),mail:box('#messages-toggle'),controls:box('.wincontrols'),bar:box('#topbar'),nav:[...document.querySelectorAll('.navitem')].map(e=>{const r=e.getBoundingClientRect();return {x:r.x,right:r.right,y:r.y,bottom:r.bottom};})};
        });
        assert(Math.abs(bounds.registered.y-bounds.tournament.y)<1,`${lang}/${width}: registration totals stack instead of staying side by side`);
        assert(bounds.registered.right<=bounds.tournament.x,`${lang}/${width}: counts overlap each other`);
        assert(bounds.nav.at(-1).right<=bounds.mail.x,`${lang}/${width}: navigation overlaps messages`);
        assert(bounds.mail.right<=Math.min(bounds.registered.x,bounds.tournament.x),`${lang}/${width}: messages overlap counts`);
        assert(bounds.registered.right<=bounds.controls.x,`${lang}/${width}: Lights Out count overlaps controls`);
        assert(bounds.tournament.right<=bounds.controls.x,`${lang}/${width}: counts overlap controls`);
        assert(bounds.registered.x>=0,`${lang}/${width}: registration count clipped`);
        assert(bounds.controls.right<=width+1,`${lang}/${width}: window controls clipped at ${bounds.controls.right}px`);
        assert(bounds.nav.every(item=>Math.abs(item.y-bounds.nav[0].y)<1),`${lang}/${width}: Tournament or another navigation item wrapped onto a second row`);
        for(const item of bounds.nav)assert(item.y>=bounds.bar.y && item.bottom<=bounds.bar.bottom+1,`${lang}/${width}: navigation clipped vertically`);
      }
    }
    await render(snapshots.en);await page.setViewportSize({width:800,height:560});
    await page.evaluate(()=>{window.registeredLabel=document.querySelector('#statregistered');window.tournamentLabel=document.querySelector('#stattournament');});
    for(let i=0;i<12;i++) {
      const next=structuredClone(snapshots.en);
      next.status.players_registered=200+i;next.status.tournament_registered=20+i;
      await render(next);
      assert.equal(await page.locator('#statregistered').textContent(),`${200+i} registered in Lights Out`);
      assert.equal(await page.locator('#stattournament').textContent(),`${20+i} registered for tournament`);
      assert(await page.evaluate(()=>registeredLabel===document.querySelector('#statregistered')&&tournamentLabel===document.querySelector('#stattournament')),'refresh replaced registration labels');
      assert(await page.evaluate(()=>{const r=document.querySelector('#statregistered').getBoundingClientRect();const t=document.querySelector('#stattournament').getBoundingClientRect();return Math.abs(r.y-t.y)<1 && r.right<=t.x;}), 'live refresh stacked or overlapped registration totals');
      assert(await page.evaluate(()=>{const items=[...document.querySelectorAll('.navitem')];return items.every(item=>Math.abs(item.getBoundingClientRect().y-items[0].getBoundingClientRect().y)<1);}), 'live refresh wrapped navigation');
    }
    const tournamentClick=page.waitForRequest(req=>req.url().endsWith('/verb/set_view'));
    await page.locator('.navitem[data-view="tournament"]').click();
    assert.deepEqual((await tournamentClick).postDataJSON(),['tournament']);
    const minimizeClick=page.waitForRequest(req=>req.url().endsWith('/window/minimize'));
    await page.locator('[data-win="minimize"]').click();
    await minimizeClick;
    for(const [registered,tournament] of [[0,0],[null,12],[51,null],[null,null]]) {
      const next=structuredClone(snapshots.en);
      next.status.players_registered=registered;next.status.tournament_registered=tournament;
      await render(next);
      assert.equal(await page.locator('#statregistered').textContent(),`${registered ?? '—'} registered in Lights Out`);
      assert.equal(await page.locator('#stattournament').textContent(),`${tournament ?? '—'} registered for tournament`);
    }
    const disconnected=structuredClone(snapshots.en);disconnected.status.connected=false;
    await render(disconnected);
    assert.equal(await page.locator('#serverstatus').textContent(),'Reconnecting…');
    assert.equal(await page.locator('#stattournament').textContent(),'');
    await render({...snapshots.en,status:{...snapshots.en.status,players_registered:51,tournament_registered:12}});
    fs.mkdirSync(path.join(root,'build'),{recursive:true});
    await page.screenshot({path:path.join(root,'build/client-topbar-minimum.png')});
    await page.setViewportSize({width:893,height:560});
    await page.screenshot({path:path.join(root,'build/client-topbar.png')});
    assert.deepEqual(errors,[]);
    console.log('Top bar: single-row navigation and side-by-side totals, accessible Tournament/window buttons, repeated updates, zero/unknown/offline states, seven languages and nine window sizes passed.');
  } finally {await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
