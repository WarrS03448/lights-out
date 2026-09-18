// Run: node --test scripts/test-account-matchflow.cjs. No game or external service.
'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const live=require('../live.cjs');

const PLAYERS=['a1111111-1111-4111-8111-111111111111','76561198000000002',
  'c3333333-3333-4333-8333-333333333333','76561198999999994'];
const GAME=['76561198000000001','76561198000000002','76561198000000003','76561198000000004'];
function fixture(t,hostIndex=0,options={}) {
  const L=live.create(options);
  const teams={1:[PLAYERS[0],PLAYERS[2]],2:[PLAYERS[1],PLAYERS[3]]};
  const match={id:'0123456789abcdef',state:'connecting',host:PLAYERS[hostIndex],
    players:PLAYERS.map((player_id,i)=>({player_id,game_steam_id:GAME[i],steam_id:GAME[i],accepted:true,connected:false})),
    teams,assigned_teams:structuredClone(teams),left:[],timer:null,deadline:0,map:'Rome',
    expected_score_limit:7,expected_max_rounds:12};
  L._internals.matches.set(match.id,match);
  for(const id of PLAYERS)L._internals.inMatch.set(id,match.id);
  t.after(()=>{for(const name of ['timer','collectTimer','stage_timer'])clearTimeout(match[name]);});
  const rows=GAME.map((steam_id,i)=>({steam_id,team:i%2,active:1}));
  return {L,match,rows,host:GAME[hostIndex]};
}

test('either login type can host: native host/join gates and team/kick decisions use actual game identities',t=>{
  for(const hostIndex of [0,1]) {
    const {L,match,host}=fixture(t,hostIndex);
    assert.equal(L.grantHostPermit(host),true);
    assert.equal(L.takeHostPermit(host),true);
    assert.equal(L.teamRuling(host,GAME[2],'side-one').yes,true);
    assert.equal(L.teamRuling(host,GAME[3],'side-two').yes,true);
    assert.equal(L.teamRuling(host,'76561198000000999','stranger').yes,true);
    assert.equal(L.teamRuling(host,PLAYERS[3],'stranger').yes,true,'canonical legacy ID is not the active game identity');
    assert.equal(L.teamRuling(host,PLAYERS[0],'member').ok,false,'UUIDs are never accepted as native Steam IDs');
    assert.equal(L.gameReportedIn(host,'ch_lobby_read').ok,true);
    assert.equal(match.players[hostIndex].connected,true);
    assert.equal(L.takeHostPermit(host),false);
    assert.equal(L.takeJoinPermit(GAME[(hostIndex+1)%4]),true);
  }
});

test('mixed roster settles and restores progress under the persistent player IDs',async t=>{
  const saved=new Map();let receipt;
  const store=async args=>{
    if(args[0]==='SMEMBERS')return [];
    if(args[0]==='GET')return saved.get(args[1])||null;
    if(args[0]==='HGET')return null;
    if(args[0]==='SET'){saved.set(args[1],args[2]);return 'OK';}
    if(args[0]==='EVAL'){
      if(args[1].includes('rank-snapshot-v1'))return ['saved'];
      if(args[1].includes('local incoming = cjson.decode'))return 0;
      const plan=JSON.parse(args.at(-1));receipt=JSON.parse(plan.receipt_json);
      saved.set(args[3],plan.receipt_json);return plan.receipt_json;
    }
    return 'OK';
  };
  const {L,match,rows,host}=fixture(t,0,{upstashCmd:store});
  await L._internals.ready;
  const reportToken=L._internals.connectPayload(match,PLAYERS[0]).report_token;
  assert.equal(L.startReady(host,match.id,rows).ok,true);
  match.created=Date.now()-60000;match.combat_end={epoch:'mixed',seq:1,complete:false};
  const fields={match_id:match.id,meta:'9;7;0;7;1;2',combat_end:'mixed;1',
    rows:GAME.map((id,i)=>`${id}|k=3;d=2;sp=10;t=${i%2};s=${i%2?2:7};a=false`).join(',')};
  assert.equal((await L.finalSnapshot(host,fields)).ok,true);
  assert.deepEqual(new Set(Object.keys(receipt.ratings)),new Set(PLAYERS));
  assert.equal(receipt.publicMatch.host_game_steam_id,host);
  const projected=require('../analytics-metrics.cjs').projectReceipt(receipt);
  assert.deepEqual(new Set(projected.players.map(p=>p.player_id)),new Set(PLAYERS));
  assert.equal(projected.host,PLAYERS[0]);
  for(const player of PLAYERS)assert.ok(await L._internals.readMatch(match.id,player));
  assert.equal(await L._internals.readMatch(match.id,GAME[0]),null);
  assert.equal((await L.finalSnapshot(host,fields)).ok,true,'native retry after settlement uses the frozen receipt');
  assert.equal(JSON.stringify(receipt.publicMatch).includes(reportToken),false);
  const recovered=live.create({upstashCmd:store});await recovered._internals.ready;
  const server=require('../server.cjs').createServer({liveService:recovered});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));await recovered.shutdown();});
  const url='http://127.0.0.1:'+server.address().port+'/api/match-report/final';
  const body={event_name:'ch_final_snapshot',storefront:'chm-'+match.id,first_session_timestamp:'chfinal-1',
    platform:fields.rows,timestamp:fields.meta,ip:fields.combat_end};
  const post=token=>fetch(url,{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer '+token},body:JSON.stringify(body)});
  const retried=await post(reportToken);
  assert.equal(retried.status,200);assert.equal((await retried.json()).data_collected,true);
  assert.equal((await post('f'.repeat(64))).status,401);
});

test('stream admission fixes ownership before writing a profile and ownership changes close only affected streams',async t=>{
  const {EventEmitter}=require('node:events');let claimed=false;
  const actor={player_id:PLAYERS[0],game_steam_id:GAME[0],account_id:PLAYERS[0],auth_method:'lightsout'};
  const L=live.create({whoami:async()=>actor,bearer:()=> 'child',sendJson:(r,s,b)=>Object.assign(r,{status:s,body:b}),
    admitGameplay:async()=>{claimed=true;return true;}});
  const req=new EventEmitter();req.headers={};req.socket={setTimeout(){}};
  const res={setTimeout(){},writeHead(){assert.equal(claimed,true);},write(){},end(){this.closed=true;req.emit('close');}};
  await L.route(req,res,'GET','/api/live');
  assert.ok(L._internals.clients.size);
  const other={res:{write(){},end(){this.closed=true;}},steamId:PLAYERS[2],accountId:PLAYERS[2]};
  L._internals.clients.set('unrelated',other);
  L.ownershipChanged({ids:[PLAYERS[0]],accountId:PLAYERS[0],steamId:GAME[0]});
  assert.equal(res.closed,true);assert.equal(other.res.closed,undefined);
  t.after(()=>L.shutdown());
});

test('ownership cannot change during a match and its exclusive gate cannot be overtaken',async t=>{
  const {L}=fixture(t);
  await assert.rejects(L.prepareOwnership([PLAYERS[0]]),/Finish your match/);
  const gate=require('../account-activity.cjs').create(),order=[];
  let release;
  const first=gate.read(()=>new Promise(resolve=>{release=resolve;order.push('reader');}));
  await new Promise(resolve=>setImmediate(resolve));
  const writer=gate.write(()=>{assert.equal(gate.writing,true);order.push('writer');});
  assert.equal(gate.blocksFormation,true,'a queued ownership writer also holds background formation');
  const second=gate.read(()=>order.push('second'));
  release();await Promise.all([first,writer,second]);
  assert.deepEqual(order,['reader','writer','second']);
});

test('mixed login roster must be complete, active, unique and correctly sorted before starting',t=>{
  for(const alter of [r=>r.pop(),r=>r[0].active=0,r=>r[0].team=1,
    r=>r[1]={...r[0]},r=>r[3].steam_id=PLAYERS[3]]) {
    const {L,match,rows,host}=fixture(t);alter(rows);
    assert.equal(L.startReady(host,match.id,rows).ok,false);assert.equal(match.state,'connecting');
  }
  const {L,match,rows,host}=fixture(t);
  for(const row of rows)L.gameReportedTeam(host,{subject:row.steam_id,team:row.team,verified:true});
  assert.equal(match.state,'connecting');
  assert.equal(L.startReady(host,match.id,rows).ok,true);
  assert.equal(match.state,'live');assert.deepEqual([...match.ingame.keys()],PLAYERS);
  assert.equal(L.startReady(host,match.id,rows).ok,true);
});

test('native stats and kills are credited to Lights Out player IDs, never to the runtime Steam account',t=>{
  const {L,match,rows,host}=fixture(t);
  assert.equal(L.startReady(host,match.id,rows).ok,true);
  assert.equal(L.gameReportedStats(host,{match:match.id,rounds:1,
    rows:GAME.map((id,i)=>`${id}|k=${i+1};d=0;t=${i%2};a=true`).join(',')}).ok,true);
  assert.deepEqual(match.stats.players.map(p=>p.steamId),PLAYERS);
  const kill=L.gameReportedKill(host,{match:match.id,row:`k=${GAME[0]};v=${GAME[1]};n=1;t=20;a0=2;a1=1`});
  assert.equal(kill.ok,true);assert.equal(kill.kill.killer,PLAYERS[0]);assert.equal(kill.kill.victim,PLAYERS[1]);
  const cap=L._internals.connectPayload(match,PLAYERS[0]);
  assert.equal(cap.host,PLAYERS[0]);assert.equal(cap.host_game_steam_id,host);
  const proof=L.authoriseReport(cap.report_token);assert.equal(proof.steamId,host);assert.equal(proof.playerId,PLAYERS[0]);
});

test('restored game bindings are immutable and inconsistent snapshots cannot be admitted',t=>{
  const {L,match}=fixture(t), identity=require('../player-identity.cjs');
  const snapshot=JSON.parse(JSON.stringify(L._internals.serialiseMatch(match)));
  const restored=L._internals.reviveMatch(snapshot);
  assert.equal(Object.isFrozen(restored.game_bindings),true);
  assert.equal(identity.playerFor(restored,GAME[0]),PLAYERS[0]);
  const damaged=structuredClone(snapshot);
  damaged.game_bindings[PLAYERS[0]]='76561198000000999';
  assert.throws(()=>new identity.MatchMap().set(damaged.id,damaged),/Invalid saved game identity/);
  assert.throws(()=>{restored.game_bindings[PLAYERS[0]]='76561198000000999';},TypeError);
});

test('two persistent accounts cannot queue the same verified game identity in one party',async t=>{
  const actors=Object.fromEntries([PLAYERS[0],PLAYERS[2]].map(player_id=>[player_id,
    {player_id,game_steam_id:GAME[0],auth_method:'lightsout'}]));
  const L=live.create({whoami:async token=>actors[token],bearer:req=>req.token,
    sendJson:(res,status,body)=>Object.assign(res,{status,body}),
    readBody:async req=>Buffer.from(JSON.stringify(req.body || {}))});
  t.after(()=>L.shutdown());
  const request=async(token,path,body)=>{const res={};await L.route({token,body,headers:{}},res,'POST',path);return res;};
  const party=await request(PLAYERS[0],'/api/party/create');
  assert.equal(party.status,200);
  assert.equal((await request(PLAYERS[2],'/api/party/join',{code:party.body.code})).status,200);
  const queued=await request(PLAYERS[0],'/api/queue/join');
  assert.equal(queued.status,409);assert.match(queued.body.error,/different verified game account/);
  assert.equal(L._internals.queue.length,0);
});

test('leaving a continuing match revokes the native one-shot join permit',async t=>{
  const account={player_id:PLAYERS[2],game_steam_id:GAME[2],auth_method:'lightsout'};
  const {L,match,host}=fixture(t,0,{whoami:async()=>account,bearer:()=> 'child',
    sendJson:(res,status,body)=>Object.assign(res,{status,body})});
  L.gameReportedIn(host,'ch_lobby_read');
  match.players[2].connected=true;
  const res={};
  await L.route({headers:{}},res,'POST','/api/match/leave');
  assert.equal(res.status,200);assert.equal(match.players.length,3);
  assert.equal(L.takeJoinPermit(GAME[2]),false);
  const next={id:'1123456789abcdef',host:PLAYERS[2],state:'connecting',
    players:[{player_id:PLAYERS[2],game_steam_id:GAME[2],steam_id:GAME[2]}],left:[],timer:null};
  delete next.game_bindings;
  L._internals.matches.set(next.id,next);L._internals.inMatch.set(PLAYERS[2],next.id);
  t.after(()=>clearTimeout(next.timer));
  assert.equal(L.grantHostPermit(GAME[2]),true,'old left-player binding does not shadow the new match');
  assert.equal(L.takeHostPermit(GAME[2]),true);
});
