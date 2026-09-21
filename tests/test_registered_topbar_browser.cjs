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
result = {}
for lang in i18n.CODES:
    i18n.set_language(lang)
    result[lang] = state_snapshot(session, panel)
print(json.dumps(result))
`], {cwd:root, encoding:'utf8', maxBuffer:8*1024*1024}));
let state = snapshots.en;
const mime = {'.js':'text/javascript', '.css':'text/css', '.html':'text/html', '.woff2':'font/woff2', '.svg':'image/svg+xml'};
const server = http.createServer((req,res) => {
  const url = new URL(req.url, 'http://localhost');
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
    await page.locator('#statregistered').waitFor();
    await page.evaluate(()=>document.fonts.ready);
    async function render(next) {state=structuredClone(next);await page.evaluate(s=>window.__hub.onState(s),state);}
    assert.equal(await page.locator('#statregistered').textContent(),'1234567 players registered');
    for(const total of [1291,0,null]) {
      await render({...snapshots.en,status:{...snapshots.en.status,players_registered:total}});
      assert.equal(await page.locator('#statregistered').textContent(),`${total===null?'—':total} players registered`);
    }
    await render({...snapshots.en,status:{...snapshots.en.status,connected:false}});
    assert.equal(await page.locator('#statregistered').textContent(),'');
    for(const [lang,snapshot] of Object.entries(snapshots)) {
      await render(snapshot);
      for(const width of [1400,1200,800]) {
        await page.setViewportSize({width,height:760});
        const bounds=await page.evaluate(()=>{
          const box=selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {x:r.x,right:r.right,y:r.y,bottom:r.bottom};};
          return {registered:box('#statregistered'),live:box('#statlive'),controls:box('.wincontrols'),bar:box('#topbar'),nav:[...document.querySelectorAll('.navitem')].map(e=>{const r=e.getBoundingClientRect();return {x:r.x,right:r.right,y:r.y,bottom:r.bottom};})};
        });
        assert(bounds.registered.x>=bounds.live.right,`${lang}/${width}: registered must follow Live Games`);
        assert(Math.abs(bounds.registered.y-bounds.live.y)<1,`${lang}/${width}: counts stay on the same row`);
        assert(bounds.registered.right<=bounds.controls.x,`${lang}/${width}: count overlaps window controls`);
        assert(bounds.controls.right<=width+1,`${lang}/${width}: window controls clipped`);
        for(const item of bounds.nav)assert(item.y>=bounds.bar.y && item.bottom<=bounds.bar.bottom+1,`${lang}/${width}: navigation clipped vertically`);
      }
    }
    await render(snapshots.en);await page.setViewportSize({width:1400,height:850});
    fs.mkdirSync(path.join(root,'build'),{recursive:true});
    await page.screenshot({path:path.join(root,'build/players-registered.png')});
    assert.deepEqual(errors,[]);
    console.log('Registered top bar: live updates, zero/unknown/offline, seven languages and three window sizes passed.');
  } finally {await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
