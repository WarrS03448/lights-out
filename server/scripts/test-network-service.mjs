import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
process.env.COMP_MATCH_SIZE = '2';
delete process.env.COMP_NETWORK_TEST_BYPASS;
delete process.env.COMP_TURN_KEY_ID;
delete process.env.COMP_TURN_API_TOKEN;
const live = require('../live.cjs');
const A = '76561198000000001', B = '76561198000000002', C = '76561198000000003';
const relayResponse = {ok:true,transport:'webrtc-relay-v1',iceServers:[{
  urls:['turn:relay.example:3478'],username:'temporary',credential:'temporary-secret',
}],expires_at:700000};
const base = {
  whoami: async token => [A,B,C].includes(token) ? {steam_id:token,persona:token} : null,
  bearer:req => req.token,
  sendJson:(res,status,body) => {res.status = status; res.body = body;},
  badRequest:res => {res.status=400;},
  readBody:async req => Buffer.from(JSON.stringify(req.body)),
};
const svc = live.create({...base,
  relayIssuer:{issue:async id => ({...relayResponse,account:id})}, prefix:'network-test:',
});
const missingRelay = live.create({...base, prefix:'network-test-missing:'});
const I = svc._internals;
async function request(id,path,body,method='POST', service=svc) {
  const res = {};
  await service.route({token:id,body,headers:{},url:path},res,method,path.split('?')[0],new URL(path,'http://localhost'));
  return res;
}
const marker = id => id.slice(-1).repeat(32);
const profile = (id,region='NA',cross_region=false) => ({region,cross_region,location:marker(id),
  transport:'webrtc-relay-v1',age_seconds:0});
const offerSdp = ['v=0','o=- 1 2 IN IP4 0.0.0.0','s=-','c=IN IP4 0.0.0.0','t=0 0',
  'm=application 9 UDP/DTLS/SCTP webrtc-datachannel','a=sctp-port:5000'].join('\r\n')+'\r\n';
const signal = (revision,peer,peer_revision,attempt,type='offer',data=offerSdp) =>
  ({revision,peer,peer_revision,attempt,type,data});
const report = (revision,peer,peerRevision,attempt,ping=30,samples=5,age_seconds=0) => ({revision,
  peers:[{steam_id:peer,revision:peerRevision,ping,transport:'webrtc-relay-v1',attempt,samples,age_seconds}]});
const deliveredA = [], deliveredB = [];
function setOnline(id,clientId,delivered) {
  I.clients.set(clientId,{steamId:id,res:{write:text=>delivered.push(JSON.parse(text.slice(6)))} });
  I.bySteam.set(id,new Set([clientId]));
}

try {
  assert.ok(live.owns('/api/network/relay'));
  assert.ok(live.owns('/api/network/signal'));
  assert.equal((await request('invalid','/api/network/relay',null,'GET')).status,401);
  const relay = await request(A,'/api/network/relay',null,'GET');
  assert.equal(relay.status,200);
  assert.equal(relay.body.transport,'webrtc-relay-v1');
  assert.equal(relay.body.account,A);
  assert.equal((await request(A,'/api/network/relay',null,'GET',missingRelay)).status,503);

  assert.equal((await request(A,'/api/network/peers',{},'POST')).status,405);
  assert.equal((await request(A,'/api/queue/join')).body.network_unready,true);
  setOnline(A,'client-a',deliveredA);
  setOnline(B,'client-b',deliveredB);
  I.bySteam.set(C,new Set());
  const a = await request(A,'/api/network/profile',profile(A));
  let b = await request(B,'/api/network/profile',profile(B));
  await request(C,'/api/network/profile',profile(C,'EU'));
  assert.equal(a.status,200);
  assert.equal(a.body.steam_id,A);
  assert.equal(a.body.transport,'webrtc-relay-v1');
  const notQueued = await request(A,'/api/network/peers',null,'GET');
  assert.equal(notQueued.body.queued,false);
  assert.deepEqual(notQueued.body.peers,[]);
  const offQueueAttempt = '0'.repeat(32);
  assert.equal((await request(A,'/api/network/signal',
    signal(a.body.revision,B,b.body.revision,offQueueAttempt))).status,409);
  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    offQueueAttempt))).status,409,'off-queue reports are rejected');

  assert.equal((await request(A,'/api/queue/join')).status,200);
  assert.equal((await request(B,'/api/queue/join')).status,200);
  await request(C,'/api/queue/join');
  assert.ok(I.queueOf.has(C));
  await request(C,'/api/network/profile',{unavailable:true});
  await request(C,'/api/network/profile',profile(C,'NA'));
  assert.equal(I.queueOf.has(C),false,'unavailable profile cancels its queue unit');

  I.bySteam.delete(A);
  assert.equal((await request(A,'/api/network/signal',
    signal(a.body.revision,B,b.body.revision,'5'.repeat(32)))).status,409,
    'offline requester cannot signal from a surviving authenticated request');
  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    '5'.repeat(32)))).status,409,
    'offline requester cannot report from a surviving authenticated request');
  I.bySteam.set(A,new Set(['client-a']));
  const peers = await request(A,'/api/network/peers',null,'GET');
  assert.equal(peers.body.queued,true);
  assert.deepEqual(peers.body.peers.map(p=>p.steam_id),[B]);
  assert.equal(peers.body.peers[0].revision,b.body.revision);
  assert.equal(peers.body.peers[0].location,marker(B));

  const staleAttempt = '4'.repeat(32);
  assert.equal((await request(A,'/api/network/signal',
    signal(a.body.revision,B,b.body.revision,staleAttempt))).status,200);
  const oldBRevision = b.body.revision;
  b = await request(B,'/api/network/profile',{...profile(B),location:'f'.repeat(32)});
  assert.equal(I.queueOf.has(B),false,'new app session leaves the old signaling queue');
  assert.equal((await request(B,'/api/network/signal',
    signal(oldBRevision,A,a.body.revision,staleAttempt,'answer'))).status,409,
    'stale session cannot answer an old attempt');
  assert.equal((await request(B,'/api/queue/join')).status,200);

  const attempt = '1'.repeat(32);
  assert.equal((await request(B,'/api/network/signal',
    signal(b.body.revision,A,a.body.revision,'2'.repeat(32),'answer'))).status,409,
    'answer cannot mint an attempt');
  assert.equal((await request(A,'/api/network/signal',
    signal(a.body.revision,B,b.body.revision,attempt,'candidate',{
      candidate:'candidate:1 1 udp 1 10.0.0.7 5000 typ host',sdpMid:'0',sdpMLineIndex:0,
    }))).status,400,'host candidate is rejected');

  deliveredB.length=0;
  const offered = await request(A,'/api/network/signal',signal(a.body.revision,B,b.body.revision,attempt));
  assert.equal(offered.status,200);
  assert.deepEqual(deliveredB.pop(),{type:'network_signal',from:A,revision:a.body.revision,
    target_revision:b.body.revision,attempt,signal:{type:'offer',data:offerSdp}});
  assert.equal((await request(B,'/api/network/signal',
    signal(b.body.revision,A,a.body.revision,attempt,'answer'))).status,200);

  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    '3'.repeat(32)))).status,400,'unissued report attempt is rejected');
  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    attempt,30,4))).status,400,'incomplete sample evidence is rejected');
  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    attempt,30,5,16))).status,400,'stale sample evidence is rejected');
  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    attempt,0))).status,400,'fake zero is rejected');

  assert.equal((await request(A,'/api/network/pings',report(a.body.revision,B,b.body.revision,
    attempt,30,5,2))).status,200);
  assert.equal(I.matches.size,0,'one direction is insufficient');
  assert.equal((await request(B,'/api/network/pings',report(b.body.revision,A,a.body.revision,
    attempt,40,6,1))).status,200);
  assert.equal(I.matches.size,1);
  const match = [...I.matches.values()][0];
  assert.equal(match.network.host,A);
  assert.equal(match.network.average,40);
  assert.equal(I.queue.length,0);
  assert.equal((await request(A,'/api/network/profile',profile(A,'EU'))).status,409,'region locked in match');
  await request(A,'/api/network/profile',{unavailable:true});
  assert.equal((await request(A,'/api/network/profile',profile(A,'EU'))).status,409,
    'unavailable profile cannot erase locked region preference');
  I.acceptMatch(A); I.acceptMatch(B);
  const payload = I.lobbyPayload(match);
  assert.equal(payload.host,A);
  assert.equal(payload.network.average,40);
  match.lobby.stage='ready'; match.lobby.map='Rome';
  process.env.COMP_PREFER_HOST=B;
  const connect = I.beginConnect(B,{host:B,map:'Rome'});
  assert.equal(connect.ok,true);
  assert.equal(match.host,A,'client nomination does not override measured selection');
  const saved = I.serialiseMatch(match);
  assert.equal(I.reviveMatch(saved).network.host,A,'host survives redeploy');
  I.networkRegistry.preferences.clear();
  I.networkRegistry.profiles.clear();
  I.matches.set(match.id,I.reviveMatch(saved));
  assert.equal((await request(A,'/api/network/profile',profile(A,'EU'))).status,409,
    'persisted formation preference protects a restored match');
  I.matches.set(match.id,match);
  match.players.find(p=>p.steam_id===A).connected=true;
  await request(A,'/api/match/leave');
  assert.equal(I.matches.has(match.id),false,'connected host leaving cancels immediately');
  const lobbyMatch = I.reviveMatch(saved);
  lobbyMatch.id='host-leaves-lobby'; lobbyMatch.state='ready';
  I.matches.set(lobbyMatch.id,lobbyMatch);
  for (const p of lobbyMatch.players) { I.removeFromQueue(p.steam_id); I.inMatch.set(p.steam_id,lobbyMatch.id); }
  await request(A,'/api/match/leave');
  assert.equal(I.matches.has(lobbyMatch.id),false,'lobby host leaving cancels immediately');
  console.log('Network relay authentication, signaling, evidence and host handoff tests passed');
} finally {
  await svc.shutdown();
  await missingRelay.shutdown();
}
