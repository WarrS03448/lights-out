// Run: node --test server/scripts/test-analytics.cjs
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const file = require('node:path').join(__dirname, '../analytics.cjs');
test('analytics provides persisted capture and queries', () => assert.ok(fs.existsSync(file), 'analytics capture module is missing'));
const api = fs.existsSync(file) ? require(file) : {};
const sid = '76561198000000001';
const fixture = () => ({matchId:'match-one',at:Date.now(),winner:1,score:{1:7,2:4},version:'team-mmr-rr-v2',
  inputs:{teams:{1:[sid],2:['76561198000000002']},mm:{quality:0.8,ratings:{1:{rating:1500,rd:80},2:{rating:1500,rd:80}}},stats:{},players:[{steam_id:sid}]},
  publicMatch:{id:'match-one',map:'Hospital',created:Date.now()-600000,ended:Date.now(),outcome:'played',players:[{steam_id:sid,persona:'A',team:1}]},
  board:[{steam_id:sid,kills:8,deaths:null,reported:true}],rows:[{steamId:sid,won:true,before:{rating:1500,rd:80,progress:1000},after:{rating:1512,rd:76,progress:1023},delta:12,rr:{delta:23,factors:{base:23,convergence:1}},valuation:{score:1,weight:1,breakdown:{measured:false,coverage:{}}}}]});

test('release cutoff acknowledges old desktop outboxes without restoring their diagnostics', async () => {
  const now=Date.now(),cutover=now-1000;
  const service=api.create({now:()=>now,resetAt:cutover});
  const context={actor_id:sid,source:'client'};
  const old={id:'before-release',type:'app.action',at:cutover-1,data:{action:'queue'}};
  const fresh={...old,id:'after-release',at:cutover};
  assert.deepEqual((await service.ingest([old,fresh],context)).sort(),[old.id,fresh.id].sort());
  assert.deepEqual(await service.ingest([old],context),[old.id]);
  const rows=(await service.query('events',{})).rows;
  assert.deepEqual(rows.map(row=>row.id),[fresh.id]);
});

test('malformed analytics cutoff cannot silently disable release isolation', () => {
  for(const resetAt of [NaN,Infinity,-1,1.5,9007199254740992]) {
    assert.throws(()=>api.create({resetAt}));
  }
});
test('client identity is authenticated and unknown payload fields cannot retain secrets', () => {
  const ev=api.cleanEvent({id:'client-1',type:'app.action',at:Date.now(),actor_id:'attacker',data:{action:'accept',status:200,token:'secret',text:'private chat',path:'C:/Users/test'}},{actor_id:sid,source:'client'});
  assert.equal(ev.actor_id,sid); assert.deepEqual(ev.data,{action:'accept',status:200});
  assert.equal(JSON.stringify(ev).includes('secret'),false);
});
test('malformed, future and unbounded client input is rejected', () => {
  assert.equal(api.cleanEvent({id:'x',type:'app.action',at:Infinity}, {source:'client'}),null);
  assert.equal(api.cleanEvent({id:'x',type:'app.action',at:Date.now()+900000}, {source:'client'}),null);
  assert.equal(api.cleanEvent({id:'x',type:'admin.rank',at:Date.now()}, {source:'client'}),null);
});

test('IP fields and literal addresses never reach client analytics storage', async () => {
  const service=api.create({});
  try {
    for (const [i,address] of ['203.0.113.10','2001:db8::1'].entries()) {
      await service.ingest([{id:'privacy-'+i,type:'request.outcome',at:Date.now(),
        ip:address,client_ip:address,headers:{'x-forwarded-for':address},
        data:{action:'queue',status:200,ip:address,client_ip:address,reason:address,error_class:address}}],
        {actor_id:sid,source:'client'});
    }
    const {rows}=await service.query('events',{});
    assert.equal(rows.length,2);
    for(const row of rows) assert.deepEqual(row.data,{action:'queue',status:200});
    assert.doesNotMatch(JSON.stringify(rows),/203\.0\.113\.10|2001:db8::1/);
  } finally { await service.close(); }
});
test('receipt projection preserves exact rating and unknown metrics without credentials', () => {
  const r=fixture();r.inputs.players[0].token='hidden';
  const p=api.projectReceipt(r);
  assert.equal(p.players[0].mmr_delta,12);assert.equal(p.players[0].rr_delta,23);
  assert.equal(p.players[0].stats.deaths,null);assert.equal(p.players[0].stats.damage,null);
  assert.equal(p.players[0].rating.rr.factors.base,23);
  assert.equal(JSON.stringify(p).includes('hidden'),false);
  assert.equal(p.match_size,2);assert.equal(p.test_match,true);
});
test('capture is idempotent, filterable and shows temporary storage explicitly', async () => {
  const service=api.create({now:()=>Date.now()});
  const ev={id:'retry',type:'app.action',at:Date.now(),data:{action:'queue',status:200}};
  await service.ingest([ev,ev],{actor_id:sid,source:'client'});
  await service.ingest([ev],{actor_id:sid,source:'client'});
  const page=await service.query('events',{});assert.equal(page.rows.length,1);
  assert.equal(page.persisted,false);
  await service.project(fixture()); await service.project(fixture());
  const matches=await service.query('matches',{size:'all'});assert.equal(matches.rows.length,1);
  const summary=await service.summary({size:'all'});assert.equal(summary.totals.matches,1);assert.equal(summary.totals.player_matches,2);
  const normal=await service.summary({});assert.equal(normal.totals.matches,0);
  await service.close();
});
test('queries validate ranges and CSV neutralizes formulas', () => {
  assert.throws(()=>api.filters({from:'broken'}),/date/i);
  assert.equal(api.csvCell('=1+1'),"\"'=1+1\"");
  assert.equal(api.csvCell('normal'),'"normal"');
});
module.exports={fixture};

test('real receipt cohorts keep configuration, round rows and denominators accurate',()=>{
  const r=fixture();r.publicMatch.sides={1:'attack',2:'defend'};r.publicMatch.round_details=[{n:1,scoreboard:r.board}];r.inputs.mm.parties=[];
  r.rows[0].before.matches=30;
  const m=api.projectReceipt(r),metrics=require('../analytics-metrics.cjs'),counts=metrics.contribution(m);
  assert.equal(m.players[0].starting_side,'attack');assert.equal(m.players[0].party_size,1);
  assert.equal(m.round_details[0].scoreboard[0].kills,8);
  assert.equal(counts.rr_count,1);assert.equal(counts.party_1_count,1);assert.equal(counts.attack_wins,1);
  const a=api.ruleSnapshot();process.env.RAILWAY_GIT_COMMIT_SHA='different-deployment';const b=api.ruleSnapshot();delete process.env.RAILWAY_GIT_COMMIT_SHA;
  assert.deepEqual(a,b);assert.ok(Array.isArray(a.valuation.SIGNED_METRICS));
});

test('cancelled matches do not contaminate completed-duration or outcome denominators',async()=>{
  const r=fixture();r.rows=[];r.publicMatch.outcome='cancelled';r.inputs.mm.parties=[];
  const m=api.projectReceipt(r),counts=require('../analytics-metrics.cjs').contribution(m);
  assert.equal(counts.completed,0);assert.equal(counts.cancelled,1);assert.equal(counts.duration_count,0);assert.equal(counts.party_1_count,0);
});

test('a corrupt receipt does not starve healthy work in the same backfill or outbox page',async()=>{
  const good=fixture(),saved=[],removed=[],values=new Map([['hub:settlement:bad','broken'],['hub:settlement:good',JSON.stringify(good)],['hub:analytics:backfill_done:v1','1']]);
  const store=async([op,key,...args])=>{
    if(op==='GET')return values.get(key)||null;
    if(op==='SSCAN')return ['0',['hub:settlement:bad','hub:settlement:good']];
    if(op==='EVAL'){saved.push(args);return 'stored';}
    if(op==='SREM'){removed.push(args[0]);return 1;}
    if(op==='ZREMRANGEBYSCORE')return 0;
    throw Error('Unexpected command '+op);
  };
  const service=api.create({store});try{await service.maintenance();assert.equal(saved.length,1);assert.deepEqual(removed,['hub:settlement:good']);}finally{await service.close();}
});

test('reliability retry totals retain event durations and severity',async()=>{
  const service=api.create();try{const ev={id:'latency',type:'request.outcome',at:Date.now(),severity:'error',version:'2.3.83',data:{duration_ms:320,status:503}};
    await service.ingest([ev],{source:'client',actor_id:sid});await service.ingest([ev],{source:'client',actor_id:sid});
    const d=await service.reliability();assert.equal(d.rows[0].events,1);assert.equal(d.rows[0].errors,1);assert.equal(d.rows[0].latency_500,1);
  }finally{await service.close();}
});
