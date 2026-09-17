const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const file = path.resolve(__dirname,'../hub/webui/static/network.js');
assert.ok(fs.existsSync(file), 'The relay measurement engine must exist');
const { Samples, safeCandidate, safeSdp, relayPair, RelayEngine } = require(file);
let now = 100;
const s = new Samples(() => now);
assert.equal(s.echo('unknown'),false);
for (let i=0;i<7;i++) {
  s.sent('token-'+i); now += 20+i;
  assert.equal(s.echo('token-'+i),true);
  assert.equal(s.echo('token-'+i),false,'duplicate echoes cannot count twice');
}
assert.equal(s.read().samples,5,'two warmup echoes are discarded');
assert.equal(s.read().ping,24);
now += 16000;
assert.equal(s.read(),null,'expired samples cannot be reported as zero or fresh');
assert.equal(safeCandidate({candidate:'candidate:1 1 udp 1 10.0.0.5 4000 typ host'}),null);
const relay=safeCandidate({candidate:'candidate:1 1 udp 1 192.0.2.2 4000 typ relay raddr 10.0.0.5 rport 99',sdpMid:'0',sdpMLineIndex:0});
assert.ok(relay.candidate.includes('raddr 0.0.0.0 rport 0'));
assert.ok(!safeSdp('v=0\r\nc=IN IP4 10.0.0.5\r\n').includes('10.0.0.5'));
const stats = new Map([
  ['t',{type:'transport',selectedCandidatePairId:'p'}],
  ['p',{type:'candidate-pair',localCandidateId:'l',remoteCandidateId:'r',state:'succeeded'}],
  ['l',{type:'local-candidate',candidateType:'relay'}],
  ['r',{type:'remote-candidate',candidateType:'relay'}],
]);
assert.equal(relayPair(stats),true);
stats.get('r').candidateType='host'; assert.equal(relayPair(stats),false);
async function retiredAttemptCannotCloseReplacement() {
  let fail;
  const engine = new RelayEngine(() => new Promise((resolve, reject) => { fail = reject; }), {now:()=>100});
  const old = {id:'2', revision:'r', generation:'g', attempt:'a'.repeat(32), outbox:Promise.resolve(), pc:{close(){}}};
  engine.peers.set('2',old);
  const pending = engine.post(old,'offer','description');
  await Promise.resolve();
  engine.closePeer('2');
  const replacement = {id:'2',closed:false,pc:{close(){}}};
  engine.peers.set('2',replacement);
  fail(new Error('Old request failed after glare replaced its connection'));
  await pending;
  assert.equal(engine.peers.get('2'),replacement,'a retired attempt must not close its replacement');
  engine.reset();
}
async function failedPeerAndQueueExitCleanUp() {
  let clock=0;
  class FakePeer {
    constructor(config) {this.config=config;this.closed=false;}
    close() {this.closed=true;}
  }
  const engine=new RelayEngine(async()=>({ok:true}),{PC:FakePeer,now:()=>clock,random:()=> 'a'.repeat(32)});
  const state={generation:'g',revision:'r',steam_id:'1',queued:true,iceServers:[{urls:['turn:relay.example']}],
    expires_at:Date.now()+60000,peers:[{steam_id:'2',revision:'s'}],signals:[]};
  engine.state=state;
  const stalled=engine.make(state.peers[0]);
  assert.equal(stalled.pc.config.iceTransportPolicy,'relay');
  clock=12001;
  await engine.tick(stalled);
  assert.equal(stalled.pc.closed,true,'a peer that never connects must retire');
  assert.equal(engine.peers.size,0);
  const current=engine.make(state.peers[0]);
  await engine.update({...state,queued:false});
  assert.equal(current.pc.closed,true,'leaving queue must close the connection');
  assert.equal(engine.peers.size,0);
  engine.reset();
}
async function retryMetadataTracksOnlyCurrentPeers() {
  let clock=100;
  class FakePeer {close(){}}
  const engine=new RelayEngine(async()=>({ok:true}),{PC:FakePeer,now:()=>clock,random:()=> 'a'.repeat(32)});
  const current={steam_id:'2',revision:'s'};
  const state={generation:'g',revision:'r',steam_id:'1',queued:true,iceServers:[{urls:['turn:relay.example']}],
    expires_at:Date.now()+60000,peers:[current],signals:[]};
  engine.state=state;
  engine.retry.set('2',clock+1000);
  engine.retry.set('expired',clock);
  engine.retry.set('absent',clock+1000);
  await engine.update(state);
  assert.deepEqual([...engine.retry.keys()],['2'],'only current peers with future deadlines keep retry metadata');
  await engine.update({...state,queued:false,peers:[]});
  assert.equal(engine.retry.size,0,'leaving queue clears all encountered-peer retry metadata');
  engine.reset();
}
Promise.all([retiredAttemptCannotCloseReplacement(),failedPeerAndQueueExitCleanUp(),retryMetadataTracksOnlyCurrentPeers()]).then(() => {
  console.log('Relay browser timing, privacy, route and attempt-lifecycle checks passed');
}).catch(error => { console.error(error); process.exitCode=1; });
