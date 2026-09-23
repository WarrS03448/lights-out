// NODE_PATH must contain Playwright; HUB_TEST_PYTHON selects the project interpreter.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const state=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; p,s=_web_panel(); print(json.dumps(state_snapshot(s,p)))'],{cwd:root,encoding:'utf8'}));
state.view='leaderboard';state.update=null;state.gamemode_update=null;
state.auth.signed_in=true;state.auth.steam_id='';state.auth.player_id='00000000-0000-4000-8000-000000000010';
state.leaderboard.available=true;
state.leaderboard.rows=Array.from({length:70},(_,i)=>({steam_id:String(i+1),name:'Player '+i,rank:i+1,rank_name:'Iron',matches:10,rr:100-i}));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1050,height:720}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('http://hub.test/**',route=>{
   const url=new URL(route.request().url());
   if(url.pathname==='/state')return route.fulfill({json:state});
   if(url.pathname==='/events')return route.fulfill({json:[]});
   if(url.pathname.startsWith('/verb/'))return route.fulfill({json:{ok:true}});
   const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
   return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');await page.waitForSelector('.leaderboard');
  for(const id of ['lb-tier-filter','lb-status-filter',...['rank','player','tier','rr','matches','winrate'].map(c=>'lb-sort-'+c)]){
   const picker=page.locator('#'+id);await picker.focus();await picker.press('Alt+ArrowDown');
   await page.evaluate(id=>window.originalPicker=document.getElementById(id),id);
   for(let n=0;n<5;n++){
    state.comp.queue_seconds=(state.comp.queue_seconds||0)+1;
    state.leaderboard.rows[0].rr+=1;
    await page.evaluate(s=>__hub.onState(s),state);
   }
   assert.equal(await page.evaluate(()=>originalPicker.isConnected&&document.activeElement===originalPicker),true,id+' stays attached and focused through changed snapshots');
   assert.ok((await page.locator('.lb-row').first().textContent()).includes(String(state.leaderboard.rows[0].rr)+' RR'),'rows stay current with picker open');
   await picker.press('Escape');
  }
  const search=page.locator('#lb-player-search');await search.fill('Player');
  await page.evaluate(()=>{const n=document.getElementById('lb-player-search');n.type='text';n.setSelectionRange(2,4);window.originalSearch=n;const v=document.querySelector('.lb-table-viewport');v.scrollTop=250;v.scrollLeft=90;window.originalScroll=[v.scrollTop,v.scrollLeft];});
  state.comp.queue_seconds++;await page.evaluate(s=>__hub.onState(s),state);
  assert.deepEqual(await page.evaluate(()=>[originalSearch.isConnected,document.activeElement===originalSearch,originalSearch.selectionStart,originalSearch.selectionEnd]),[true,true,2,4]);
  assert.deepEqual(await page.evaluate(()=>{const v=document.querySelector('.lb-table-viewport');return [v.scrollTop,v.scrollLeft];}),await page.evaluate(()=>originalScroll));
  state.auth.player_id='00000000-0000-4000-8000-000000000011';await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await search.inputValue(),'','account switch clears previous search');
  assert.deepEqual(errors,[]);
  console.log('Leaderboard native controls survive changed timer, ranking and account snapshots.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
