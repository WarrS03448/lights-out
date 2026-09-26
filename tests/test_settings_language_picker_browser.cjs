// NODE_PATH must contain Playwright; HUB_TEST_PYTHON selects the project interpreter.
// Sam, 2026-09-25: the Settings language dropdown "disappears after a second due to the refresh".
// Every changed snapshot redrew the screen, and a <select> taken out of the page closes its list.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const state=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; p,s=_web_panel(); print(json.dumps(state_snapshot(s,p)))'],{cwd:root,encoding:'utf8'}));
state.view='settings';state.update=null;state.gamemode_update=null;
state.status=Object.assign(state.status||{},{connected:true,players_registered:100,tournament_registered:10});
state.settings.sound=Object.assign(state.settings.sound||{},{enabled:true,volume:40});
const verbs=[];
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1200,height:760}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://hub.test/**',route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/state')return route.fulfill({json:state});
   if(url.pathname==='/events')return route.fulfill({json:[]});
   if(url.pathname.startsWith('/window/'))return route.fulfill({json:{maximized:false}});
   if(url.pathname.startsWith('/verb/')){
    verbs.push({verb:url.pathname.slice(6),args:JSON.parse(route.request().postData()||'[]')});
    return route.fulfill({json:{ok:true}});
   }
   const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');await page.waitForSelector('#settings-language');
  const picker=page.locator('#settings-language');
  await picker.focus();await picker.press('Alt+ArrowDown');
  await page.evaluate(()=>{window.langPicker=document.getElementById('settings-language');});
  assert.equal(await page.evaluate(()=>langPicker.matches(':open')),true,'the language list opens');
  for(let n=0;n<8;n++){
   state.status.players_registered+=1;state.status.tournament_registered+=1;
   if(n===4){state.settings.sound.volume=55;}
   await page.evaluate(s=>__hub.onState(s),state);
   assert.equal(await page.evaluate(()=>langPicker.isConnected&&langPicker===document.getElementById('settings-language')&&
     document.activeElement===langPicker&&langPicker.matches(':open')),true,'snapshot '+n+' closed the language list');
  }
  assert.equal(await page.locator('#set-vol-pct').textContent(),'55%','the rest of Settings stays current with the list open');
  assert.equal(await page.locator('#statregistered').textContent().then(t=>t.includes('108')),true,'the top bar stays current');
  const next=await page.evaluate(()=>langPicker.options[langPicker.selectedIndex+1].value);
  await page.keyboard.press('ArrowDown');await page.keyboard.press('Enter');await page.waitForTimeout(100);
  assert.deepEqual(verbs.filter(v=>v.verb==='set_language').map(v=>v.args[0]),[next],'the list still picks a language after the refreshes');
  // Once the player has moved on, a snapshot redraws Settings from the state as before.
  await page.evaluate(()=>document.activeElement.blur());
  state.settings.language.current=next;state.status.players_registered+=1;
  await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await picker.inputValue(),next);
  assert.deepEqual(errors,[]);
  console.log('Settings language list stays open through changed snapshots and still picks a language.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
