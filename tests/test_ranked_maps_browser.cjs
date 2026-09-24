// NODE_PATH provides Playwright; HUB_TEST_PYTHON selects the test environment.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..'),python=process.env.HUB_TEST_PYTHON||'python';
const data=JSON.parse(execFileSync(python,['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; p,s=_web_panel(); print(json.dumps(dict(state=state_snapshot(s,p))))'],{cwd:root,encoding:'utf8'}));
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  const page=await browser.newPage({viewport:{width:1050,height:720}}),errors=[],verbs=[];
  page.on('pageerror',e=>errors.push(e.message));
  const state=data.state;state.view='competitive';state.update=null;state.gamemode_update=null;
  state.auth.signed_in=true;state.comp.mode_id='BB1';
  state.comp.phase='lobby';state.comp.match_id='private-map-test';state.comp.installed=true;
  state.comp.lobby={stage:'veto',my_turn:true,ban_turn:1,teams:[],captains:[],stage_seconds:25,
   veto:{pool:['Paintball','Airsoft','BombHouse'],bans:[]}};
  await page.route('http://hub.test/**',route=>{
   const u=new URL(route.request().url());
   if(u.pathname==='/state')return route.fulfill({json:state});
   if(u.pathname==='/events')return route.fulfill({json:[]});
   if(u.pathname.startsWith('/verb/')){verbs.push(u.pathname);return route.fulfill({json:{ok:true}});}
   const f=path.join(root,'hub/webui/static',u.pathname==='/'?'index.html':decodeURIComponent(u.pathname));
   return fs.existsSync(f)?route.fulfill({path:f}):route.fulfill({status:404,body:''});
  });
  await page.goto('http://hub.test/');await page.waitForSelector('button.veto-map');
  assert.equal(await page.locator('button.veto-map').count(),3);
  assert.equal(await page.locator('.ranked-mode-choice').count(),2);
  assert.equal(await page.locator('.ranked-mode-choice[data-mode=BB1]').textContent(),'1v1 Bodybomb');
  const airsoft=page.locator('button.veto-map').filter({hasText:'Airsoft'});
  await airsoft.focus();
  for(let n=0;n<8;n++){state.comp.lobby.stage_seconds=24-n;state.comp.queue_seconds=n;await page.evaluate(s=>__hub.onState(s),state);}
  await airsoft.click();assert(verbs.includes('/verb/ban_map'));
  state.comp.lobby.veto.bans=[{map:'Airsoft',team:1}];state.comp.lobby.my_turn=false;
  await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.veto-map.banned').count(),1);
  state.comp.lobby.stage='ready';state.comp.lobby.map='BombHouse';state.comp.lobby.veto.bans.push({map:'Paintball',team:2});
  await page.evaluate(s=>__hub.onState(s),state);
  assert.equal(await page.locator('.veto-map.final').textContent(),'BombHouse');
  assert.deepEqual(errors,[]);
  console.log('Public BB1 veto controls survive changed snapshots and complete two bans.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
