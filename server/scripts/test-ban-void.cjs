// Local real-Lua fixtures; ACCOUNT_TEST_PYTHON selects Python with fakeredis[lua].
'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process'),readline=require('node:readline'),path=require('node:path');
const admin='76561198999999999';process.env.COMP_ADMIN_STEAM_IDS=admin;
const live=require('../live.cjs');
async function fixture(t){
 const bridge=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')],{stdio:['pipe','pipe','pipe']});
 let seq=0,blockVoid=false,blockBan=false;const pending=new Map(),instances=[];
 readline.createInterface({input:bridge.stdout}).on('line',line=>{const row=JSON.parse(line),p=pending.get(row.id);pending.delete(row.id);if(p)row.error?p.reject(Error(row.error)):p.resolve(row.result);});
 bridge.on('exit',()=>{for(const p of pending.values())p.reject(Error('store stopped'));});
 const raw=args=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});bridge.stdin.write(JSON.stringify({id,command:args})+'\n');});
 const store=async args=>{if(args[0]==='EVAL'&&((blockVoid&&args[1].includes('local p = cjson.decode(ARGV[1])'))||(blockBan&&args[1].includes('account-ban-change-v1'))))throw Error('storage unavailable');return raw(args);};
 const create=async()=>{const L=live.create({upstashCmd:store});instances.push(L);await L._internals.ready;return L;};
 const L=await create(),ids=Array.from({length:10},(_,i)=>i===0?'a1111111-1111-4111-8111-111111111111':String(76561198000000001n+BigInt(i)));
 const match={id:'0123456789abcdef',state:'live',host:ids[0],map:'Rome',created:Date.now(),
  players:ids.map((player_id,i)=>({player_id,game_steam_id:String(76561198000000001n+BigInt(i)),accepted:true,connected:true})),
  teams:{1:ids.slice(0,5),2:ids.slice(5)},left:[],start_ready_verified:true,reportToken:'ab'.repeat(32),deadline:Date.now()+60000};
 match.assigned_teams=structuredClone(match.teams);L._internals.matches.set(match.id,match);for(const id of ids)L._internals.inMatch.set(id,match.id);
 t.after(async()=>{blockVoid=false;blockBan=false;for(const item of instances)await item.shutdown();bridge.kill();});
 return {L,ids,match,store,raw,create,blockVoid:value=>blockVoid=value,blockBan:value=>blockBan=value};
}
for(const category of ['ordinary'])for(const targetIndex of [0,7])test(category+' ban of '+(targetIndex?'nonhost':'UUID host')+' voids once without rank writes',async t=>{
 const f=await fixture(t),rankKey='hub:rating:'+f.ids[3],before=JSON.stringify({rating:1234,progress:87,revision:4});
 await f.raw(['SET',rankKey,before]);
 const result=await f.L.banAccount(admin,{player_id:f.ids[targetIndex],reason:'Reviewed violation',...(category==='cheating'?{category,operation_id:require('node:crypto').randomUUID()}:{} )});
 assert.equal(result.ok,true);assert.equal(f.L._internals.matches.has(f.match.id),false);
 for(const id of f.ids){const ended=await f.L.completion(id,f.match.id);assert.equal(ended.close_allowed,true);assert.equal(ended.result.voided,true);assert.equal(ended.result.rr_delta,0);assert.equal(ended.result.void_reason,'ban');assert.equal(f.L._internals.inMatch.has(id),false);}
 assert.equal(await f.raw(['GET',rankKey]),before);assert.equal((await f.raw(['KEYS','hub:rating:*'])).length,1);
 const replay=await f.L.finalSnapshot(f.match.players[0].game_steam_id,{match_id:f.match.id});assert.equal(replay.ok,true,JSON.stringify({replay,host:f.match.host}));
 const reboot=await f.create();assert.equal(reboot._internals.matches.size,0);assert.equal((await reboot.completion(f.ids[2],f.match.id)).result.voided,true);
 for(const id of f.ids)assert.equal((await reboot._internals.readHistory(id)).filter(r=>r.id===f.match.id).length,1);
});
test('ban and pending void persist together when result storage fails, then recover after restart',async t=>{
 const f=await fixture(t);f.blockVoid(true);
 assert.equal((await f.L.banAccount(admin,{player_id:f.ids[4],reason:'Reviewed violation'})).ok,true);
 assert(await f.raw(['GET','hub:ban:'+f.ids[4]]));
 const saved=JSON.parse(await f.raw(['GET','hub:live:match:'+f.match.id]));assert(saved.void_pending);assert.equal(saved.void_reason,'ban');
 assert.equal((await f.L.completion(f.ids[3],f.match.id)).close_allowed,false);
 await f.L.shutdown();f.blockVoid(false);const reboot=await f.create();await reboot._internals.flushMatches();
 assert.equal((await reboot.completion(f.ids[3],f.match.id)).result.voided,true);
});
test('failed ban writes leave a live match intact',async t=>{
 const f=await fixture(t);f.blockBan(true);
 await assert.rejects(f.L.banAccount(admin,{player_id:f.ids[4],reason:'Reviewed violation'}),/storage unavailable/);
 assert.equal(f.L._internals.matches.has(f.match.id),true);assert.equal(Boolean(f.match.void_pending),false);assert.equal(await f.raw(['GET','hub:ban:'+f.ids[4]]),null);
});
test('an already decided score is not overwritten by a later ban',async t=>{
 const f=await fixture(t);f.match.collecting={winner:1,score:{1:7,2:2},limit:7,since:Date.now(),deadline:Date.now()+60000};
 await f.L._internals.flushMatches();await f.L.banAccount(admin,{player_id:f.ids[4],reason:'Reviewed violation'});
 assert.equal(Boolean(f.match.void_pending),false);assert.equal(f.match.collecting.winner,1);
});
test('banning a native game identity also voids its UUID-owned match',async t=>{
 const f=await fixture(t);await f.L.banAccount(admin,{player_id:f.match.players[0].game_steam_id,reason:'Reviewed violation'});
 assert.equal((await f.L.completion(f.ids[3],f.match.id)).result.voided,true);
});
test('without a persistent store, an ordinary ban releases every player with a void result',async t=>{
 const L=live.create({});t.after(()=>L.shutdown());const ids=['76561198000000001','76561198000000002'];
 const match={id:'0123456789abcdef',state:'live',host:ids[0],map:'Rome',created:Date.now(),players:ids.map(player_id=>({player_id,game_steam_id:player_id})),teams:{1:[ids[0]],2:[ids[1]]},left:[]};
 L._internals.matches.set(match.id,match);for(const id of ids)L._internals.inMatch.set(id,match.id);
 await L.banAccount(admin,{player_id:ids[0],reason:'Reviewed violation'});
 assert.equal(L._internals.matches.size,0);assert.equal(L._internals.inMatch.size,0);
 assert.equal((await L.completion(ids[1],match.id)).result.voided,true);
});
test('the saved ban void supersedes a stale worker collecting a score',async t=>{
 const f=await fixture(t);f.blockVoid(true);
 await f.L.banAccount(admin,{player_id:f.ids[4],reason:'Reviewed violation'});
 f.match.collecting={winner:1,score:{1:7,2:2},limit:7,since:Date.now(),deadline:Date.now()+60000};
 f.blockVoid(false);await f.L._internals.flushMatches();
 assert.equal((await f.L.completion(f.ids[3],f.match.id)).result?.voided,true);
});
