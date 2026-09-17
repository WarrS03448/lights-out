const { test } = require('node:test');
const assert = require('node:assert/strict');
const live = require('../live.cjs');
const rating = require('../rating.cjs');
const reportServer = require('../server.cjs');
const H = '76561198000000001', F = '76561198000000002';
const tick = () => new Promise(resolve => setImmediate(resolve));

test('a failed manual-rank audit leaves the visible cache and career mirror unchanged',async()=>{
  const db=database(),s=service(db);
  try{
    await s._internals.ready;s._internals.ADMIN_IDS.add(H);
    await db(['SET','test:rating:'+F,JSON.stringify({...rating.defaultRating(),matches:30,progress:125})]);
    const before=await s._internals.loadRating(F);await tick();
    const career=JSON.stringify(s._internals.careerOf(F));
    db.fault=([op,script])=>{if(op==='EVAL'&&script.includes('rank-write-v1'))throw Error('Invalid audit index');};
    await assert.rejects(s.setRank(H,{steam_id:F,progress:300}),/Invalid audit/);
    assert.deepEqual(s._internals.ratingOf(F),before);
    assert.equal(JSON.stringify(s._internals.careerOf(F)),career);
    assert.equal(JSON.parse(db.strings.get('test:rating:'+F)).progress,125);
  }finally{db.fault=null;await s.shutdown();}
});

// Storage faults are injected at the network boundary. The Lua itself is also
// executed by test-settlement-lua.py, rather than relying solely on this fixture.
function database() {
  const strings = new Map(), sets = new Map(), boards = new Map(), lists = new Map();
  const cmd = async ([op, key, ...args], options) => {
    if (cmd.fault) await cmd.fault([op, key, ...args], options);
    switch (op) {
      case 'GET': return strings.get(key) ?? null;
      case 'MGET': return [key,...args].map(k=>strings.get(k)??null);
      case 'LRANGE': return (lists.get(key)||[]).slice(Number(args[0]),Number(args[1])+1);
      case 'SET': strings.set(key, args[0]); return 'OK';
      case 'DEL': strings.delete(key); return 1;
      case 'SADD': if (!sets.has(key)) sets.set(key, new Set()); sets.get(key).add(args[0]); return 1;
      case 'SREM': sets.get(key)?.delete(args[0]); return 1;
      case 'SMEMBERS': return [...(sets.get(key) || [])];
      case 'ZADD': if (!boards.has(key)) boards.set(key, new Map()); boards.get(key).set(args[1], Number(args[0])); return 1;
      case 'ZREM': return Number(boards.get(key)?.delete(args[0]) || 0);
      case 'ZREVRANGE': return [...(boards.get(key) || [])].sort((a,b) => b[1]-a[1] || b[0].localeCompare(a[0])).slice(Number(args[0]),Number(args[1])+1).map(x=>x[0]);
      case 'ZREVRANK': { const ids = await cmd(['ZREVRANGE',key,0,9999]); const n=ids.indexOf(args[0]); return n<0?null:n; }
      case 'ZCOUNT': return [...(boards.get(key)?.values() || [])].filter(x=>x>Number(String(args[0]).replace('(',''))).length;
      case 'EVAL': {
        const count = Number(args[0]), keys = args.slice(1, count+1), argv = args.slice(count+1);
        if (key.includes('penalty-write-v1')) {
          const operation = JSON.parse(argv[0]);
          strings.set(keys[0], operation.json);
          return ['written', operation.json];
        }
        if (key.includes('rank-snapshot-v1')) {
          const receipt = strings.get(keys[0]);
          if (receipt) { strings.delete(keys[1]); sets.get(keys[2])?.delete(argv[0]); return ['settled',receipt]; }
          const previous=strings.get(keys[1]);
          if(previous){const saved=JSON.parse(previous),incoming=JSON.parse(argv[1]);
            if(saved.collecting&&(!incoming.collecting||['winner','limit'].some(k=>saved.collecting[k]!==incoming.collecting[k])||[1,2].some(k=>saved.collecting.score[k]!==incoming.collecting.score[k])))return ['pending',previous];}
          strings.set(keys[1],argv[1]); if(!sets.has(keys[2]))sets.set(keys[2],new Set());sets.get(keys[2]).add(argv[0]);
          return ['saved'];
        }
        if (key.includes('rank-write-v1')) {
          const [row] = JSON.parse(argv[0]);
          const current = strings.get(keys[0]);
          if (keys[2]&&strings.has(keys[2])) return ['written',current];
          if (current === row.json) {if(keys[2])strings.set(keys[2],row.json);return ['written',current];}
          if ((JSON.parse(current || '{}').revision || 0) !== row.expected) return ['conflict'];
          strings.set(keys[0], row.json);
          await cmd([row.placing?'ZREM':'ZADD',keys[1],...(row.placing?[row.id]:[row.progress,row.id])]);
          if(keys[2])strings.set(keys[2],row.json);
          if(cmd.loseWriteReply){cmd.loseWriteReply=false;throw Error('write reply lost');}
          return ['written',row.json];
        }
        if (key.includes('rank-settlement-v1')) {
          const old = strings.get(keys[0]); if (old) return ['replayed',old];
          const rows = JSON.parse(argv[2]);
          for (let i=0;i<rows.length;i++) if ((JSON.parse(strings.get(keys[4+i])||'{}').revision||0)!==rows[i].expected) return ['conflict',rows[i].id];
          for (let i=0;i<rows.length;i++) { const r=rows[i]; strings.set(keys[4+i],r.json); if(!boards.has(keys[1]))boards.set(keys[1],new Map()); if(r.placing)boards.get(keys[1]).delete(r.id);else boards.get(keys[1]).set(r.id,r.progress); }
          strings.set(keys[0],argv[1]); strings.delete(keys[2]); sets.get(keys[3])?.delete(argv[0]);
          if(cmd.loseReply){cmd.loseReply=false;throw Error('reply lost after commit');}
          if(cmd.afterCommit) await cmd.afterCommit();
          return ['committed',argv[1]];
        }
        throw Error('Unexpected Lua script');
      }
      default: return null;
    }
  };
  return Object.assign(cmd,{strings,sets,boards,lists});
}
function service(store, extra={}) {
  return live.create({collectSeconds:0,whoami:async()=>({steam_id:H}),bearer:()=>'',sendJson(res,status,body){Object.assign(res,{status,body});},badRequest(){},readBody:async()=>Buffer.alloc(0),upstashCmd:store,prefix:'test:',...extra});
}
function match() {
  return {id:'match-1',players:[H,F].map(steam_id=>({steam_id,connected:true})),state:'live',created:Date.now(),deadline:Date.now()+600000,expiry:'live',host:H,left:[],teams:{1:[H],2:[F]},mm:{quality:1,ratings:{1:{rating:1800,rd:60},2:{rating:1800,rd:60}}}};
}
async function seed(db) {
  const m=match(); await db(['SET','test:live:match:'+m.id,JSON.stringify(m)]); await db(['SADD','test:live:matches',m.id]);
  for(const id of [H,F])await db(['SET','test:rating:'+id,JSON.stringify({...rating.defaultRating(),rating:1800,rd:60,matches:20,wins:10,losses:10,progress:1800,revision:0})]);
  return m;
}
async function finish(s) {
  s.gameReportedScore(H,{match:'match-1',scores:'0|0:7|1:3',limit:7});
  await tick();
  const m=s._internals.matches.get('match-1');
  if(m?.settling)await m.settling;
  await tick();
}

test('settling a recovered match before reconnect preserves saved rank history',async()=>{
  const db=database();await seed(db);const s=service(db);
  try {await s._internals.ready;await finish(s);const r=JSON.parse(db.strings.get('test:rating:'+H));assert.equal(r.matches,21);assert.ok(r.progress>1800);}
  finally {await s.shutdown();}
});
test('final collection is serializable and resumes the remaining collection window',async()=>{
  const db=database();await seed(db);const s=service(db,{collectSeconds:30});
  try {await s._internals.ready;s.gameReportedScore(H,{match:'match-1',scores:'0|0:7|1:3',limit:7});const m=s._internals.matches.get('match-1');assert.doesNotThrow(()=>JSON.stringify(s._internals.serialiseMatch(m)));await s._internals.flushMatches();const raw=JSON.parse(db.strings.get('test:live:match:match-1'));assert.equal(raw.collecting.winner,1);assert.ok(raw.collecting.deadline>Date.now());}
  finally {await s.shutdown();}
  const next=service(db,{collectSeconds:90});
  try {await next._internals.ready;const m=next._internals.matches.get('match-1');assert.equal(m.collecting.winner,1);assert.ok(m.collectTimer);assert.ok(m.collecting.deadline-Date.now()<31000,'restore keeps original deadline instead of starting another full window');}
  finally {await next.shutdown();}
});
test('a failed commit keeps the decided match retryable without publishing changed rank',async()=>{
  const db=database();await seed(db);const s=service(db);
  try {await s._internals.ready;db.fault=([op])=>{if(op==='EVAL')throw Error('store unavailable');};await finish(s);assert.equal(JSON.parse(db.strings.get('test:rating:'+H)).matches,20);assert.ok(s._internals.matches.has('match-1'));assert.equal(s._internals.ratingOf(H).matches,20);db.fault=null;await s._internals.finishMatch(s._internals.matches.get('match-1'),1,{1:7,2:3},7);assert.equal(JSON.parse(db.strings.get('test:rating:'+H)).matches,21);}
  finally {await s.shutdown();}
});
test('lost commit response and a stale restored snapshot do not pay a result twice',async()=>{
  const db=database();const original=await seed(db);const s=service(db);
  try {await s._internals.ready;db.loseReply=true;await finish(s);assert.equal(JSON.parse(db.strings.get('test:rating:'+H)).matches,21);}
  finally {await s.shutdown();}
  await db(['SET','test:live:match:match-1',JSON.stringify(original)]);await db(['SADD','test:live:matches','match-1']);const next=service(db);
  try {await next._internals.ready;await finish(next);assert.equal(JSON.parse(db.strings.get('test:rating:'+H)).matches,21);assert.equal(next._internals.inMatch.has(H),false);}
  finally {await next.shutdown();}
});
test('penalty reads fail closed and retry after a transient error',async()=>{
  const db=database();const s=service(db);let reads=0;
  try {await s._internals.ready;db.fault=([op,key],options)=>{if(op==='GET'&&key==='test:penalty:'+H){reads++;if(reads===1)throw Error('offline');assert.equal(options?.strict,true);}};await db(['SET','test:penalty:'+H,JSON.stringify({until:Date.now()+60000,reason:'abandon',count:1})]);let res={};await s.route({headers:{},url:'/api/queue/join'},res,'POST','/api/queue/join');assert.equal(res.status,503);res={};await s.route({headers:{},url:'/api/queue/join'},res,'POST','/api/queue/join');assert.equal(res.status,403);assert.equal(reads,2);assert.equal(s._internals.queue.length,0);}
  finally {await s.shutdown();}
});
test('off-page leaderboard ties use the actual seat position',async()=>{
  const db=database();const s=service(db);const id=H;
  try {await s._internals.ready;for(let n=1;n<=200;n++){const who=String(BigInt(H)+BigInt(n-1));await db(['SET','test:rating:'+who,JSON.stringify({...rating.defaultRating(),matches:20,progress:2400})]);await db(['ZADD','test:leaderboard:rr',2400,who]);}const board=await s.leaderboard(id,50);assert.equal(board.you.rank,200);assert.equal(board.you.top,false);}
  finally {await s.shutdown();}
});

test('report credentials are host-only, stable across restore, and revoked at completion',async()=>{
  const db=database();await seed(db);const s=service(db);
  try {await s._internals.ready;const m=s._internals.matches.get('match-1');const host=s._internals.connectPayload(m,H);const other=s._internals.connectPayload(m,F);assert.match(host.report_token,/^[a-f0-9]{64}$/);assert.equal(other.report_token,undefined);assert.equal(s.authoriseReport('probe'),null);assert.equal(s.authoriseReport(host.report_token).steamId,H);await s._internals.flushMatches();const next=service(db);try{await next._internals.ready;assert.equal(next.authoriseReport(host.report_token).matchId,m.id);await finish(next);assert.equal(next.authoriseReport(host.report_token),null);}finally{await next.shutdown();}}
  finally {await s.shutdown();}
});

test('only restored pre-credential matches accept bounded legacy reports',async()=>{
  const db=database();const old=await seed(db);old.state='connecting';
  await db(['SET','test:live:match:'+old.id,JSON.stringify(old)]);
  const s=service(db);
  try {
    await s._internals.ready;
    const restored=s._internals.matches.get(old.id);
    assert.equal(restored.legacyReportAuth,true);
    assert.equal(s.authoriseLegacyReport(H,'ch_lobby_read','').matchId,old.id);
    assert.equal(s.authoriseLegacyReport(H,'ch_bb5_score',old.id).matchId,old.id);
    assert.equal(s.authoriseLegacyReport(F,'ch_bb5_score',old.id),null,'only the restored host reports');
    assert.equal(s.authoriseLegacyReport(H,'ch_combat_v1',old.id),null,'combat is never legacy-authorised');
    restored.players.find(p=>p.steam_id===H).connected=false;
    const legacy=(body)=>reportServer._internals.applyRestoredLegacyReport(
      {body:JSON.stringify({user_id:H,...body})},s);
    assert.equal(legacy({event_name:'ch_lobby_read'}),true);
    assert.equal(restored.players.find(p=>p.steam_id===H).connected,true,
      'restored connecting arrival reaches the real service');
    assert.equal(legacy({event_name:'ch_team_verified',storefront:H,platform:'0:2:0:0:0'}),true);
    assert.equal(legacy({event_name:'ch_team_verified',storefront:F,platform:'1:2:1:1:1'}),true);
    assert.equal(restored.ingame.get(H),0,'restored connecting team rows reach the real service');
    s._internals.connectPayload(restored,H);
    assert.match(restored.reportToken,/^[a-f0-9]{64}$/);
    assert.ok(s.authoriseLegacyReport(H,'ch_bb5_stats',old.id),'lazy token creation does not revoke compatibility');
    await s._internals.flushMatches();
  } finally {await s.shutdown();}
  const again=service(db);
  try {
    await again._internals.ready;
    assert.ok(again.authoriseLegacyReport(H,'ch_bb5_state','ch-test-4821'),'compatibility survives another restore');
    const restored=again._internals.matches.get(old.id);restored.state='live';
    const legacy=(body)=>reportServer._internals.applyRestoredLegacyReport(
      {body:JSON.stringify({user_id:H,storefront:old.id,...body})},again);
    assert.equal(legacy({event_name:'ch_bb5_stats',platform:`${H}|2:1:2:1:0`,timestamp:'2'}),true);
    assert.equal(restored.stats.players.find(p=>p.steamId===H).kills,2,
      'restored live stats reach the real service');
    assert.equal(legacy({event_name:'ch_bb5_round',platform:'n=0;w=-1;sec=20;a0=1;a1=0'}),true);
    assert.ok(restored.rounds.length,'restored live rounds reach the real service');
    assert.equal(legacy({event_name:'ch_bb5_score',platform:'0|0:6|1:3',timestamp:'6'}),true);
    if(restored.settling)await restored.settling;await tick();
    assert.equal(again._internals.matches.has(old.id),false,'restored live score completes the match');
    assert.equal(again.authoriseLegacyReport(H,'ch_bb5_score',old.id),null,'permission ends with the match');
  } finally {await again.shutdown();}
});

test('credentialed snapshots and new matches never gain legacy report authority',async()=>{
  const db=database();const current=await seed(db);current.reportToken='ab'.repeat(32);
  await db(['SET','test:live:match:'+current.id,JSON.stringify(current)]);
  const s=service(db);
  try {await s._internals.ready;assert.equal(s.authoriseLegacyReport(H,'ch_bb5_score',current.id),null);}
  finally {await s.shutdown();}
});

test('rank writes are serialized and a failed write is retried before the next revision',async()=>{
  const db=database();const s=service(db);let release;const gate=new Promise(r=>{release=r;});let first=true;
  try {await s._internals.ready;db.fault=async([op,script])=>{if(op==='EVAL'&&script.includes('rank-write')&&first){first=false;await gate;throw Error('offline');}};s._internals.saveRating(H,{...rating.defaultRating(),rating:1400,matches:20});const one=s._internals.drainRatingWrites(H);s._internals.saveRating(H,{...rating.defaultRating(),rating:1600,matches:20});release();await assert.rejects(one,/offline/);db.fault=null;await s._internals.drainRatingWrites(H);const saved=JSON.parse(db.strings.get('test:rating:'+H));assert.equal(saved.rating,1600);assert.equal(saved.revision,2);}
  finally{await s.shutdown();}
});

test('a concurrent RR operation waits for settlement and preserves both changes',async()=>{
  const db=database();await seed(db);const s=service(db);
  let release, started;const gate=new Promise(r=>{release=r;});const entered=new Promise(r=>{started=r;});
  try {
    await s._internals.ready;db.afterCommit=async()=>{started();await gate;};
    const settled=s._internals.finishMatch(s._internals.matches.get('match-1'),1,{1:7,2:3},7);
    await entered;
    const penalty=s._internals.mutateRating(H,current=>({...current,progress:current.progress-50}));
    const awarded=JSON.parse(db.strings.get('test:rating:'+H));
    release();await settled;await penalty;
    const saved=JSON.parse(db.strings.get('test:rating:'+H));
    assert.equal(saved.matches,21);assert.equal(saved.progress,awarded.progress-50);
    assert.equal(s._internals.ratingOf(H).progress,saved.progress);
    await s._internals.drainRatingWrites(H);
  } finally {release();await s.shutdown();}
});

test('failed recovery reads retain the index and retry without admitting a queue',async()=>{
  const db=database();await seed(db);let offline=true;
  db.fault=([op,key],options)=>{if(op==='GET'&&key==='test:live:match:match-1'){assert.equal(options.strict,true);if(offline)throw Error('offline');}};
  const s=service(db);
  try {
    await s._internals.ready;assert.equal(db.sets.get('test:live:matches').has('match-1'),true);
    let res={};await s.route({headers:{},url:'/api/queue/join'},res,'POST','/api/queue/join');assert.equal(res.status,503);
    offline=false;await s._internals.ensureRecovery();assert.equal(s._internals.inMatch.get(H),'match-1');
    await finish(s);assert.equal(JSON.parse(db.strings.get('test:rating:'+H)).matches,21);
  } finally {offline=false;await s.shutdown();}
});

test('a stale container cannot resurrect a settled match on its next snapshot',async()=>{
  const db=database();await seed(db);const stale=service(db), current=service(db);
  try {
    await Promise.all([stale._internals.ready,current._internals.ready]);await finish(current);
    await stale._internals.flushMatches();await tick();
    assert.equal(db.strings.has('test:live:match:match-1'),false);
    assert.equal(stale._internals.inMatch.has(H),false);assert.equal(stale._internals.matches.size,0);
    assert.equal(stale._internals.ratingOf(H).matches,21);
  } finally {await stale.shutdown();await current.shutdown();}
});

test('reading ranks performs no independent leaderboard write',async()=>{
  const db=database();await seed(db);let writes=0;db.fault=([op])=>{if(op==='ZADD')writes++;};
  const s=service(db);try {await s._internals.ready;assert.equal(writes,0);}finally{await s.shutdown();}
});

test('out-of-order stats keep current totals and cannot count as final evidence',async()=>{
  const db=database();await seed(db);const s=service(db,{collectSeconds:30});
  try {
    await s._internals.ready;const m=s._internals.matches.get('match-1');
    assert.equal(s.gameReportedStats(H,{rounds:10,rows:H+'|8:4:10:100'}).ok,true);
    const before=structuredClone(m.stats.players);const seen={...m.stats.seenAt};
    s.gameReportedStats(H,{rounds:1,rows:H+'|1:0:1:10'});
    assert.deepEqual(m.stats.players,before);assert.deepEqual(m.stats.seenAt,seen);
    assert.ok(m.stats.series[H][1]);
  }finally{await s.shutdown();}
});

test('late early-round rows cannot close final collection even on first sight of a player',async()=>{
  const db=database();await seed(db);const s=service(db,{collectSeconds:30});
  try {await s._internals.ready;s.gameReportedScore(H,{scores:'0|0:7|1:3',limit:7});
    const m=s._internals.matches.get('match-1');
    for(const id of [H,F])s.gameReportedStats(H,{rounds:1,rows:id+'|1:0:1:10'});
    assert.equal(s._internals.collectionComplete(m),false);assert.equal(m.finished,undefined);
  }finally{await s.shutdown();}
});

test('a failed RR deduction is rebased after another container settles the match',async()=>{
  const db=database();await seed(db);const a=service(db);let b;
  try {await a._internals.ready;
    db.fault=([op,script])=>{if(op==='EVAL'&&script.includes('rank-write-v1'))throw Error('offline');};
    await assert.rejects(a._internals.mutateRating(H,current=>({...current,progress:current.progress-50})),/offline/);
    db.fault=null;b=service(db);await b._internals.ready;await finish(b);
    const award=JSON.parse(db.strings.get('test:rating:'+H));
    await a._internals.drainRatingWrites(H);
    const saved=JSON.parse(db.strings.get('test:rating:'+H));assert.equal(saved.matches,21);assert.equal(saved.progress,award.progress-50);
  }finally{db.fault=null;await a.shutdown();if(b)await b.shutdown();}
});

test('an uncertain RR write is not charged twice after a later match update',async()=>{
  const db=database();await seed(db);const a=service(db);let b;
  try {await a._internals.ready;db.loseWriteReply=true;
    await assert.rejects(a._internals.mutateRating(H,current=>({...current,progress:current.progress-50})),/reply lost/);
    b=service(db);await b._internals.ready;await finish(b);
    const saved=db.strings.get('test:rating:'+H);await a._internals.drainRatingWrites(H);
    assert.equal(db.strings.get('test:rating:'+H),saved);assert.equal(a._internals.ratingOf(H).progress,JSON.parse(saved).progress);
  }finally{await a.shutdown();if(b)await b.shutdown();}
});

test('receipt restores visible results when optional archive publication was lost',async()=>{
  const db=database();await seed(db);const s=service(db);
  try {await s._internals.ready;await finish(s);
    db.lists.set('test:history:'+H,[JSON.stringify({id:'match-1',won:null,score:null})]);
    const next=service(db);
    try{await next._internals.ready;const full=await next._internals.readMatch('match-1',H);
      assert.equal(full.won_team,1);assert.equal(full.players.find(p=>p.steam_id===H).won,true);
      assert.equal((await next._internals.readHistory(H))[0].score,'7-3');
      assert.equal(await next._internals.readMatch('match-1','76561198000000999'),null);
      assert.equal(JSON.stringify(full).includes('reportToken'),false);
    }finally{await next.shutdown();}
  }finally{await s.shutdown();}
});

module.exports={database};

test('old receipts gain player damage detail without rewriting frozen totals or exposing raw evidence',async()=>{
  const combat=require('../combat.cjs');
  for(const asObject of [false,true]) {
    const db=database(),raw=combat.createState();raw.roster={[H]:0,[F]:1};
    raw.events=[{kind:'health',a:H,b:F,loss:35,relation:'enemy',n:1,t:2,seq:1}];
    const receipt={winner:1,score:{1:2,2:0},rows:[],
      publicMatch:{id:'match-1',players:[{steam_id:H,persona:'Host'},{steam_id:F,persona:'Friend'}]},
      board:[{steam_id:H,kills:2,combat:{status:'partial',enemyDamage:999,weaponStats:[]}}],
      inputs:{teams:{1:[H],2:[F]},combatState:raw}};
    const saved=structuredClone(receipt);db.strings.set('test:settlement:match-1',asObject?receipt:JSON.stringify(receipt));
    const s=service(db);
    try {
      await s._internals.ready;const full=await s._internals.readMatch('match-1',H);
      assert.deepEqual(full.scoreboard[0].combat.playerStats,[{steam_id:F,damageDealt:35,damageTaken:null}]);
      assert.equal(full.scoreboard[0].combat.enemyDamage,999);assert.equal(full.scoreboard[0].kills,2);
      assert.equal(JSON.stringify(full).includes('combatState'),false);assert.equal(JSON.stringify(full).includes('events'),false);
      assert.equal(await s._internals.readMatch('match-1','76561198000000999'),null);
      assert.deepEqual(asObject?db.strings.get('test:settlement:match-1'):JSON.parse(db.strings.get('test:settlement:match-1')),saved);
    } finally {await s.shutdown();}
  }
});

test('another container cannot overwrite a decided result while commit is unavailable',async()=>{
  const db=database();await seed(db);const stale=service(db),current=service(db);
  try{await Promise.all([stale._internals.ready,current._internals.ready]);
    db.fault=([op,script])=>{if(op==='EVAL'&&script.includes('rank-settlement-v1'))throw Error('offline');};
    await finish(current);const pending=JSON.parse(db.strings.get('test:live:match:match-1')).collecting;
    assert.equal(pending.winner,1);await stale._internals.flushMatches();
    assert.equal(stale._internals.matches.get('match-1').collecting.winner,1);
    assert.equal(JSON.parse(db.strings.get('test:live:match:match-1')).collecting.winner,1);
    db.fault=null;await stale._internals.finishMatch(stale._internals.matches.get('match-1'),1,{1:7,2:3},7);
    assert.equal(JSON.parse(db.strings.get('test:rating:'+H)).matches,21);
  }finally{db.fault=null;await stale.shutdown();await current.shutdown();}
});

test('authenticated score reports must satisfy the agreed first-to-N rules before mutation',async()=>{
  const s=service(null);try{const m=match();s._internals.matches.set(m.id,m);for(const p of m.players)s._internals.inMatch.set(p.steam_id,m.id);
    s._internals.connectPayload(m,H);m.agreedScoreLimit=7;
    for(const scores of ['0|0:999|1:998','0|0:8|1:7','0|0:7|1:7']){
      assert.equal(s.gameReportedScore(H,{scores,limit:999}).ok,false);assert.equal(m.score,undefined);assert.equal(m.rounds,undefined);
    }
    assert.equal(s.gameReportedScore(H,{scores:'0|0:7|1:3',limit:999}).finished,true);
  }finally{await s.shutdown();}
});

test('derived round wins use the agreed score limit even before any native score report',async()=>{
  const s=service(null);try{const m=match();s._internals.matches.set(m.id,m);for(const p of m.players)s._internals.inMatch.set(p.steam_id,m.id);
    s._internals.connectPayload(m,H);m.agreedScoreLimit=2;m.team_map={0:1,1:2};
    s.gameReportedRound(H,{row:'n=1;w=0;sec=30;a0=1;a1=0'});
    const out=s.gameReportedRound(H,{row:'n=2;w=0;sec=30;a0=1;a1=0'});
    assert.equal(out.scored.limit,2);assert.equal(out.scored.finished,true);assert.equal(m.finished.winner,1);
  }finally{await s.shutdown();}
});

test('authenticated dispatcher reaches real live round, heartbeat and kill handlers',async()=>{
  const dispatch=require('../server.cjs')._internals.dispatchRankedReport;
  const s=service(null);try{const m=match();s._internals.matches.set(m.id,m);for(const p of m.players)s._internals.inMatch.set(p.steam_id,m.id);
    s._internals.connectPayload(m,H);m.agreedScoreLimit=7;m.team_map={0:1,1:2};
    const auth={steamId:H,matchId:m.id,scoreLimit:7};
    const round=dispatch({event_name:'ch_bb5_round',platform:'n=1;w=0;sec=30;a0=1;a1=0'},auth,s);
    assert.equal(round.status,200);assert.equal(m.score[1],1);
    const state=dispatch({event_name:'ch_bb5_state',platform:'n=2;sec=10;a0=1;a1=1'},auth,s);
    assert.equal(state.status,200);assert.equal(m.lastAlive.round,2);
    const kill=dispatch({event_name:'ch_bb5_kill',platform:`k=${H};v=${F};n=2;t=12`},auth,s);
    assert.equal(kill.status,200);assert.equal(m.kills.length,1);assert.equal(m.kills[0].killer,H);
  }finally{await s.shutdown();}
});
