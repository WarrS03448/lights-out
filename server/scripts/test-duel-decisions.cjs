// Uses the same Redis/Lua fixture as account and settlement tests.
const {test}=require('node:test'),assert=require('node:assert/strict');
const live=require('../live.cjs'),identity=require('../player-identity.cjs');
function redis(t){
 const child=require('node:child_process').spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',require('node:path').join(__dirname,'account-redis-fixture.py')]);
 let seq=0;const pending=new Map();
 require('node:readline').createInterface({input:child.stdout}).on('line',line=>{const r=JSON.parse(line),p=pending.get(r.id);pending.delete(r.id);if(p)r.error?p.reject(Error(r.error)):p.resolve(r.result);});
 child.on('exit',()=>{for(const p of pending.values())p.reject(Error('Fixture closed'));});t.after(()=>child.kill());
 return args=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});child.stdin.write(JSON.stringify({id,command:args})+'\n');});
}
async function setup(t){
 const raw=redis(t);let fail=false;
 const store=async args=>{if(fail&&args[0]==='EVAL'&&args[1].includes('local old = redis.call'))throw Error('receipt offline');return raw(args);};
 const L=live.create({upstashCmd:store,modeId:'BB1'});await L._internals.ready;t.after(()=>L.shutdown());
 const ids=['76561198000000001','76561198000000002'],teams={1:[ids[0]],2:[ids[1]]};
 const match=identity.freezeMatch({mode:'BB1',id:'abcdef0123456789',state:'live',host:ids[0],created:Date.now(),live_at:Date.now(),
  players:ids.map(steam_id=>({steam_id,connected:true,accepted:true})),teams,assigned_teams:structuredClone(teams),
  start_ready_verified:true,left:[],map:'Paintball',expiry:'live',deadline:Date.now()-1000});
 L._internals.matches.set(match.id,match);ids.forEach(id=>L._internals.inMatch.set(id,match.id));
 await L._internals.flushMatches();
 return {L,match,ids,store,fail:x=>fail=x,account:i=>({player_id:ids[i],game_steam_id:ids[i]})};
}
test('pending concession survives overdue clocks, opposite concession, restart and late native final',async t=>{
 const f=await setup(t);f.fail(true);
 assert.equal((await f.L.concedeMatch(f.account(0),{match_id:f.match.id})).unavailable,true);
 const terminal=structuredClone(f.match.terminal);
 f.L._internals.rearm(f.match);assert.equal(f.match.timer,null);
 f.L._internals.expireLive(f.match.id);f.L._internals.closeMatch(f.match,'stalled');
 assert(f.L._internals.matches.has(f.match.id));
 const opposite=await f.L.concedeMatch(f.account(1),{match_id:f.match.id});
 assert.equal(opposite.ok,false);assert.match(opposite.error,/already/);
 assert.deepEqual(f.match.terminal,terminal);
 await f.L.shutdown();f.fail(false);
 const reboot=live.create({upstashCmd:f.store,modeId:'BB1'});t.after(()=>reboot.shutdown());await reboot._internals.ready;
 assert(reboot._internals.matches.has(f.match.id));
 const done=await reboot.finalSnapshot(f.ids[0],{match_id:f.match.id});assert.equal(done.ok,true);
 const receipt=JSON.parse(await f.store(['GET','hub:ranked:BB1:settlement:'+f.match.id]));
 assert.equal(receipt.winner,2);assert.equal(receipt.score,null);assert.equal(receipt.history[f.ids[0]].score,null);
 const ranks=await f.store(['MGET',...f.ids.map(id=>'hub:ranked:BB1:rating:'+id)]);
 await reboot.concedeMatch(f.account(0),{match_id:f.match.id});await reboot._internals.flushMatches();
 assert.deepEqual(await f.store(['MGET',...f.ids.map(id=>'hub:ranked:BB1:rating:'+id)]),ranks);
 assert.equal(await f.store(['EXISTS','hub:rating:'+f.ids[0]]),0);
});
test('verified host service timeout saves a retryable void without changing either rank',async t=>{
 const f=await setup(t);f.match.migration_capabilities=true;
 await f.store(['SET','hub:ranked:BB1:live:authority:'+f.match.id,JSON.stringify({host:f.ids[0],epoch:0,last_seen:Date.now()-300001})]);
 // The authority key spelling follows migration.cjs via the live engine.
 f.fail(true);await f.L._internals.checkHostTimeouts();
 assert.equal(f.match.void_reason,'service_failure');assert(f.L._internals.matches.has(f.match.id));
 f.fail(false);await f.L._internals.flushMatches();
 const receipt=JSON.parse(await f.store(['GET','hub:ranked:BB1:settlement:'+f.match.id]));
 assert.equal(receipt.voided,true);assert.equal(receipt.full.void_reason,'service_failure');
 assert.deepEqual(await f.store(['MGET',...f.ids.map(id=>'hub:ranked:BB1:rating:'+id)]),[null,null]);
});

test('verified reconnect expiry saves ordinary loss and mode-only cooldown atomically',async t=>{
 const f=await setup(t),loser=f.ids[1];f.match.score={1:2,2:1};
 const since=Date.now()-300001;
 f.match.reconnect={[loser]:{since,deadline:since+300000}};
 f.fail(true);
 const presence=f.ids.map((steam_id,i)=>({steam_id,active:i===0?1:0}));
 const response=await f.L.matchPresence(f.ids[0],f.match.id,presence);
 assert.equal(response.ok,false);
 assert(f.match.terminal,JSON.stringify({response,state:f.match.state,host:f.match.host,players:f.match.players,reconnect:f.match.reconnect}));
 assert.equal(f.match.terminal.reason,'reconnect_timeout');
 assert.equal(await f.store(['GET','hub:ranked:BB1:penalty:'+loser]),null);
 f.fail(false);await f.L._internals.flushMatches();
 const receipt=JSON.parse(await f.store(['GET','hub:ranked:BB1:settlement:'+f.match.id]));
 assert.equal(receipt.terminal.reason,'reconnect_timeout');assert.equal(receipt.winner,1);
 assert.equal(receipt.penalties[loser].count,1);assert(receipt.penalties[loser].until>Date.now());
 assert.equal(await f.store(['GET','hub:penalty:'+loser]),null);
 assert.equal(receipt.ratings[loser].progress,receipt.rows.find(r=>r.steamId===loser).after.progress);
 const before=await f.store(['GET','hub:ranked:BB1:penalty:'+loser]);await f.L._internals.flushMatches();
 assert.equal(await f.store(['GET','hub:ranked:BB1:penalty:'+loser]),before);
});

