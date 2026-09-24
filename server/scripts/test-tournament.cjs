// Run: node --test server/scripts/test-tournament.cjs
'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const path=require('node:path').join(__dirname,'../tournament.cjs');
test('tournament service exists',()=>assert.ok(fs.existsSync(path),'Missing tournament service'));
const api=fs.existsSync(path)?require(path):{};
const start=Date.parse('2026-09-26T16:00:00Z'),end=Date.parse('2026-09-28T16:00:00Z');
const ids=Array.from({length:10},(_,i)=>String(76561198000000001n+BigInt(i)));
function squadReceipt(id='m1',at=start+1000,delta=25){return {matchId:id,data_collected:true,winner:1,publicMatch:{id,started:start,ended:at,outcome:'played',size:10,players:ids.map((p,i)=>({player_id:p,game_steam_id:p,persona:'Player '+i,team:i<5?1:2}))},rows:ids.map((p,i)=>({steamId:p,won:i<5,rr:{delta:i===0?delta:-10}}))};}
function receipt(...args){const r=squadReceipt(...args);r.mode='BB1';r.publicMatch.mode='BB1';r.publicMatch.map='Paintball';r.publicMatch.size=2;r.publicMatch.players=[r.publicMatch.players[0],r.publicMatch.players[5]];r.rows=[r.rows[0],r.rows[5]];return r;}
const reg=(at=start-1000)=>({player_id:ids[0],game_steam_id:ids[0],persona:'Sam',registered_at:at});
test('schedule automatically enters live and ends at exact Central boundaries',()=>{
 assert.equal(api.phase(start-1),'scheduled');assert.equal(api.phase(start),'live');assert.equal(api.phase(end-1),'live');assert.equal(api.phase(end),'ended');
 assert.deepEqual(api.EVENT.prizes,[100,35,15]);assert.equal(api.EVENT.prize_pool,150);assert.equal(api.EVENT.prizes.reduce((sum,amount)=>sum+amount,0),api.EVENT.prize_pool);assert.deepEqual(api.EVENT.payout_methods,['Zelle','Venmo','PayPal']);
});
test('only full completed ranked receipts within the interval qualify',()=>{
 assert.ok(api.matchEntry(receipt()));
 for(const mutate of [r=>r.publicMatch.started--,r=>r.publicMatch.ended=end,r=>r.publicMatch.size=10,r=>r.publicMatch.outcome='voided',r=>r.voided=true,r=>delete r.data_collected,r=>r.publicMatch.players.pop(),r=>r.rows[0].rr.delta=null]){const r=receipt();mutate(r);assert.equal(api.matchEntry(r),null);}
});
test('net RR includes losses, ignores placement seed and late registration has no retroactive credit',()=>{
 const a=receipt('a',start+2000,30),b=receipt('b',start+4000,-10),c=receipt('c',start+6000,0);b.publicMatch.started=start+2000;b.rows[0].won=false;c.publicMatch.started=start+4000;c.rows[0].rr.placed=true;c.rows[0].after={progress:9000};
 const entries=[a,b,c].map(api.matchEntry);
 const [row]=api.standings([reg()],entries);assert.equal(row.net_rr,20);assert.equal(row.gained_rr,30);assert.equal(row.lost_rr,10);assert.equal(row.matches,2);assert.equal(row.history[2].reason,'placement');
 const [late]=api.standings([reg(start+1000)],entries);assert.equal(late.net_rr,-10);assert.equal(late.matches,1);
 assert.equal(api.standings([{...reg(),game_steam_id:ids[1]}],entries)[0].matches,0);
});
test('receipt order does not affect results; ties use wins then first reaching final total',()=>{
 const a=receipt('a',start+2000,20),b=receipt('b',start+3000,-5),c=receipt('c',start+4000,5);b.rows[0].won=false;
 const entries=[a,b,c].map(api.matchEntry),rows=api.standings([reg()],entries.reverse());assert.equal(rows[0].reached_at,start+2000);assert.equal(rows[0].wins,2);
 const r=receipt('tie');r.rows[1].rr.delta=25;r.rows[1].won=true;
 const tied=api.standings([reg(),{...reg(),player_id:ids[5],game_steam_id:ids[5]}],[api.matchEntry(r)],{...api.EVENT,minimum_matches:1});assert.equal(tied[0].rank,1);assert.equal(tied[1].rank,1);assert.equal(tied[0].tied,true);
});
module.exports={receipt,reg,ids,start,end};
const duel=receipt;
test('launch reformat accepts only verified two-player Paintball receipts with consistent mode identity',()=>{
 assert.ok(api.matchEntry(duel()));assert.equal(api.matchEntry(squadReceipt()),null);
 for(const change of [r=>r.mode='BB5',r=>r.publicMatch.mode='BB5',r=>{delete r.mode;delete r.publicMatch.mode;},r=>r.publicMatch.map='Rome',r=>r.publicMatch.players[1].team=1,r=>r.publicMatch.players[1].game_steam_id=ids[0],r=>r.rows[1].steamId=ids[0],r=>r.publicMatch.id='different']){
  const r=duel();change(r);assert.equal(api.matchEntry(r),null);
 }
});
test('durable outbox does not acknowledge a receipt before tournament projection succeeds',async()=>{
 const analytics=require('../analytics.cjs');let attempts=0;
 const svc=analytics.create({projectTournament:async()=>{attempts++;throw Error('Event storage down');}});
 try {await assert.rejects(svc.project(receipt()),/Event storage down/);assert.equal(attempts,1);}finally{await svc.close();}
});
test('tournament API routes are owned by the authenticated competitive router',()=>{
 const live=require('../live.cjs');assert.equal(live.owns('/api/tournament'),true);assert.equal(live.owns('/api/tournament/register'),true);
});
test('event storage failures never present an empty successful board',async()=>{
 const svc=api.create();await assert.rejects(svc.view(ids[0]),/unavailable/);await assert.rejects(svc.register(reg()),/unavailable/);
});
test('public standings hide identities and keep own row outside top ten',async()=>{
 const regs=Array.from({length:12},(_,i)=>({...reg(),player_id:String(76561198000000001n+BigInt(i)),game_steam_id:String(76561198000000001n+BigInt(i))}));
 const entries=Array.from({length:5},(_,j)=>({id:'test'+j,started:start,ended:start+1000+j,rows:regs.map((r,i)=>({...r,delta:100-i,won:true}))}));
 const svc=api.create({now:()=>start+2000,store:async()=>[regs.map(r=>JSON.stringify(r)),entries.map(r=>JSON.stringify(r)),'',[]]});
 const view=await svc.view(regs[11].player_id);assert.equal(view.leaders.length,10);assert.equal(view.you.rank,12);assert.equal(view.you.net_rr,445);
 assert(!JSON.stringify(view).includes(regs[0].player_id));assert(!JSON.stringify(view).includes('game_steam_id'));
});
test('ended event awards full shared-place USD prizes after both tiebreakers',async()=>{
 const regs=ids.map((id,i)=>({...reg(),player_id:id,game_steam_id:id,persona:'Player '+i}));
 let clock=end-1;
 const entry={id:'prize',started:start,ended:start+1000,rows:regs.map(r=>({...r,won:true}))};entry.rows.forEach((r,i)=>{r.delta=100-i;});
 const svc=api.create({now:()=>clock,store:async()=>[regs.map(r=>JSON.stringify(r)),Array.from({length:5},(_,i)=>JSON.stringify({...entry,id:'test'+i})),'',[]]});
 assert.deepEqual((await svc.view(ids[0])).winners,[]);clock=end;
 const data=await svc.view(ids[0]);assert.equal(data.winners.length,3);assert.deepEqual(data.winners.map(r=>r.prize_usd),[100,35,15]);assert.equal(data.results_provisional,true);
 entry.rows[1].delta=100;
 const tied=api.create({now:()=>end,store:async()=>[regs.map(r=>JSON.stringify(r)),Array.from({length:5},(_,i)=>JSON.stringify({...entry,id:'test'+i})),'',[]]});
 const shared=(await tied.view(ids[0])).winners;
 assert.deepEqual(shared.map(r=>r.rank),[1,1,3]);
 assert.deepEqual(shared.map(r=>r.prize_usd),[100,100,15]);
});
test('live route ignores supplied identity and requires an authenticated account',async t=>{
 let actor=null,registered=null;
 const L=require('../live.cjs').create({whoami:async()=>actor,bearer:()=> 'token',sendJson:(res,status,body)=>Object.assign(res,{status,body}),tournament:{register:async account=>{registered=account;return {ok:true};},view:async()=>({ok:true})}});
 t.after(()=>L.shutdown());await L._internals.ready;
 async function post(){const res={setHeader(){}};await L.route({headers:{},body:{player_id:ids[2]}},res,'POST','/api/tournament/register');return res;}
 assert.equal((await post()).status,401);actor={...reg(),persona:'Sam'};assert.equal((await post()).status,200);assert.equal(registered.player_id,ids[0]);
});
test('admin event data requires a current allowlisted session',async()=>{
 let allowed=true;const admin=require('../admin.cjs').create({upstashCmd:async()=>({steam_id:ids[0]}),live:()=>({isAdmin:()=>allowed}),tournament:{adminView:async()=>({ok:true,rows:[]})}});
 async function request(cookie){const res={headers:{},writeHead(status,headers){this.status=status;this.headers=headers;},setHeader(){},end(body){this.body=body;}};await admin.route({headers:{cookie:cookie?'hubadmin=test':''}},res,'GET','/admin/events/data',new URL('http://localhost/admin/events/data'));return res;}
 assert.equal((await request(false)).status,401);assert.equal((await request(true)).status,200);allowed=false;assert.equal((await request(true)).status,401);
});

test('public tournament route and assets need no login, and every main website nav includes it',async()=>{
 delete process.env.UPSTASH_REDIS_REST_URL;delete process.env.UPSTASH_REDIS_REST_TOKEN;
 const server=require('../server.cjs').createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+server.address().port;
 try{for(const url of ['/tournament','/tournament.html','/assets/tournament.js','/assets/tournament.css'])assert.equal((await fetch(base+url)).status,200,url);
   assert.equal((await fetch(base+'/api/public/tournament')).status,503,'unavailable storage must not claim an empty successful leaderboard');
   const dir=require('node:path').join(__dirname,'../public');for(const name of fs.readdirSync(dir).filter(n=>n.endsWith('.html'))){const html=fs.readFileSync(require('node:path').join(dir,name),'utf8');if(html.includes('href="/leaderboards"'))assert(html.includes('href="/tournament"'),name);}
 }finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
});

test('a placement draw without RR flags is excluded for that player only',()=>{
 const r=receipt();r.draw=true;r.rows[0].before={matches:0};r.rows[0].rr={delta:0};
 const entry=api.matchEntry(r),registrations=[reg(),{...reg(),player_id:ids[5],game_steam_id:ids[5]}];
 const rows=api.standings(registrations,[entry]);
 assert.equal(rows.find(p=>p.player_id===ids[0]).matches,0);
 assert.equal(rows.find(p=>p.player_id===ids[0]).history[0].reason,'placement');
 assert.equal(rows.find(p=>p.player_id===ids[5]).matches,1);
});
