// NODE_PATH must contain Playwright; HUB_TEST_PYTHON selects the project interpreter.
// Sam, 2026-09-26: hovering the tabs, or Cancel while searching, "they seem to flicker". Every
// changed snapshot redrew them, and the new element faded from its resting look back into its
// hover look. A control under the pointer must hold its hover look through a redraw.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {execFileSync}=require('node:child_process'),{chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const base=JSON.parse(execFileSync(process.env.HUB_TEST_PYTHON||'python',['-c',
 'import json; from tests.test_hub import _web_panel; from hub.webui.snapshot import state_snapshot; p,s=_web_panel(); print(json.dumps(state_snapshot(s,p)))'],{cwd:root,encoding:'utf8'}));
function searching(){
 const s=structuredClone(base);s.view='competitive';s.update=null;s.gamemode_update=null;
 s.auth.signed_in=true;s.auth.player_id='00000000-0000-4000-8000-000000000010';
 s.status=Object.assign(s.status||{},{connected:true,players_registered:100,tournament_registered:10});
 s.comp.phase='queued';s.comp.installed=true;s.comp.mode_id='BB5';s.comp.mode_selectable=true;s.comp.queue={seconds:5};
 s.settings.network={region:'NA',cross_region:false,status:'ready',locked:true,options:[{code:'NA',name:'North America'}]};
 return s;
}
const cases=[
 ['a nav tab','.navitem[data-view="leaderboard"]',s=>s],
 ['Cancel while searching','.search-box .btn-ghost-light',s=>s],
 ['the update strip button','#updatebar [data-ub="apply"]',s=>{s.update={available:true,latest:'9.9.9',current:'3.0.1',status:'idle',forced:false,progress:0};return s;}],
];
let state;
(async()=>{
 const browser=await chromium.launch({headless:true,channel:'msedge'});
 try{
  for(const [label,sel,extra] of cases){
   const page=await browser.newPage({viewport:{width:1200,height:760}}),errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   state=extra(searching());
   await page.route('http://hub.test/**',route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/state')return route.fulfill({json:state});
    if(url.pathname==='/events')return route.fulfill({json:[]});
    if(url.pathname.startsWith('/window/'))return route.fulfill({json:{maximized:false}});
    if(url.pathname.startsWith('/verb/'))return route.fulfill({json:{ok:true}});
    const file=path.join(root,'hub/webui/static',url.pathname==='/'?'index.html':decodeURIComponent(url.pathname));
    return fs.existsSync(file)?route.fulfill({path:file}):route.fulfill({status:404,body:''});
   });
   await page.goto('http://hub.test/');await page.waitForSelector(sel);await page.waitForTimeout(400);
   const at=await page.evaluate(s=>{const r=document.querySelector(s).getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};},sel);
   await page.mouse.move(at.x,at.y);await page.waitForTimeout(600);
   for(let round=0;round<3;round++){
    state.status.players_registered+=1;state.comp.queue.seconds+=1;
    // One sample after each of the next 20 frames has been produced (rAF, then a task).
    const frames=await page.evaluate(({s,sel,x,y})=>new Promise(done=>{
     function look(){const hit=document.elementFromPoint(x,y),n=hit&&hit.closest(sel);if(!n)return 'gone';
      const c=getComputedStyle(n);return [n.matches(':hover'),c.color,c.backgroundColor,c.transform,c.boxShadow].join('|');}
     const settled=look(),out=[];
     window.__hub.onState(s);
     function frame(){out.push(look());if(out.length<20)requestAnimationFrame(()=>setTimeout(frame,0));else done({settled,out});}
     requestAnimationFrame(()=>setTimeout(frame,0));
    }),{s:state,sel,x:at.x,y:at.y});
    const off=frames.out.filter(v=>v!==frames.settled).length;
    assert.equal(off,0,label+' left its hover look for '+off+' of 20 frames after snapshot '+round+': '+frames.out.find(v=>v!==frames.settled));
   }
   assert.deepEqual(errors,[]);
   await page.close?.();
  }
  console.log('Hovered controls hold their hover look through changed snapshots.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
