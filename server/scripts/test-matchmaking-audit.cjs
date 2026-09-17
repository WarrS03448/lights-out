const test = require('node:test');
const assert = require('node:assert/strict');
delete process.env.COMP_NETWORK_TEST_BYPASS;
const mm = require('../matchmaker.cjs');
const net = require('../network.cjs');
const live = require('../live.cjs');

test('dense production relay pools keep synchronous matchmaking bounded',()=>{
  const now=Date.now();
  const ps=Array.from({length:50},(_,i)=>({id:String(i),region:'NA',cross_region:false,transport:net.TRANSPORT,
    location:i.toString(16).padStart(32,'0'),revision:String(i),sampled:now,pings:{}}));
  for(const p of ps)for(const q of ps)if(p!==q)p.pings[q.id]={at:now,ms:20,localRevision:p.revision,remoteRevision:q.revision};
  const units=ps.map((p,i)=>({key:p.id,members:[p.id],joined:now-120000+i,ratings:[{rating:1500,rd:100}],network:[p]}));
  const start=performance.now();
  assert.ok(mm.findMatch(units,{now,matchSize:10,teamSize:5}));
  assert.ok(performance.now()-start<2000,'a single search must not block the two-second match tick');
});

test('interleaved feasible host groups cannot starve at the candidate cap',()=>{
  const now=Date.now(),names=['A','H1','H2',...Array.from({length:8},(_,i)=>['P'+i,'Q'+i]).flat()];
  const ps=Object.fromEntries(names.map((id,i)=>[id,{id,region:'NA',cross_region:false,
    transport:net.TRANSPORT,location:i.toString(16).padStart(32,'0'),revision:id,sampled:now,pings:{}}]));
  function edge(a,b){for(const [x,y] of [[a,b],[b,a]]) ps[x].pings[y]={at:now,ms:20,localRevision:x,remoteRevision:y};}
  edge('A','H1');edge('A','H2');
  for(let i=0;i<8;i++){edge('H1','P'+i);edge('H2','Q'+i);}
  const units=names.map((id,i)=>({key:id,members:[id],joined:now-120000+i,
    ratings:[{rating:1500,rd:100}],network:[ps[id]]}));
  const match=mm.findMatch(units,{now,matchSize:10,teamSize:5});
  assert.ok(match);
  assert.equal(match.units.length,10);
  assert.ok(match.units.some(u=>u.key==='A'));
});

test('cancelled host permit is revoked and a pending launch survives snapshot restore',async()=>{
  const ids=Array.from({length:10},(_,i)=>String(76561198000000001n+BigInt(i)));
  const service=live.create({upstashCmd:null,expectedRules:()=>({score_limit:7,max_rounds:13})});
  let restored;
  try {
    const I=service._internals;
    const m={id:'permit',state:'ready',created:Date.now(),network:{host:ids[0]},
      players:ids.map(steam_id=>({steam_id,persona:steam_id,accepted:true,connected:false})),
      lobby:{stage:'ready',map:'Rome',teams:{1:ids.slice(0,5),2:ids.slice(5)},sides:{1:'attack',2:'defend'},bans:[]}};
    I.matches.set(m.id,m);for(const id of ids)I.inMatch.set(id,m.id);
    I.beginConnect(ids[0],{});
    assert.equal(service.takeHostPermit(ids[0]),true);
    const snapshot=JSON.stringify(I.serialiseMatch(m));
    I.closeMatch(m,'stalled',[]);
    assert.equal(service.takeHostPermit(ids[0]),false);
    restored=live.create({prefix:'audit:',upstashCmd:async cmd=>cmd[0]==='SMEMBERS'
      ? (cmd[1]==='audit:live:matches'?['permit']:[]) :cmd[0]==='GET'&&cmd[1]==='audit:live:match:permit'
        ?snapshot:cmd[0]==='EVAL'?['saved']:null});
    await restored._internals.ready;
    assert.equal(restored.takeHostPermit(ids[0]),true);
  } finally {await service.shutdown();if(restored)await restored.shutdown();}
});
