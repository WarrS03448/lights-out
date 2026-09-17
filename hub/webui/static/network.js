/* Private app-to-app RTT measurement. Signaling uses Railway; timed packets use TURN. */
(function (root) {
  'use strict';
  class Samples {
    constructor(now) { this.now=now; this.pending=new Map(); this.values=[]; this.warmup=2; }
    sent(id) {
      const now=this.now();
      for (const [key,at] of this.pending) if (now-at>3000) this.pending.delete(key);
      if (this.pending.size<16) this.pending.set(id,now);
    }
    echo(id) {
      if (!this.pending.has(id)) return false;
      const now=this.now(), elapsed=now-this.pending.get(id); this.pending.delete(id);
      if (elapsed<0 || elapsed>3000) return false;
      if (this.warmup) { this.warmup--; return true; }
      this.values.push({ms:elapsed,at:now}); this.values=this.values.slice(-10); return true;
    }
    read() {
      const now=this.now(); this.values=this.values.filter(v=>now-v.at<=15000);
      if (this.values.length<5) return null;
      const rows=this.values.map(v=>v.ms).sort((a,b)=>a-b), mid=Math.floor(rows.length/2);
      return {ping:rows.length%2 ? rows[mid] : (rows[mid-1]+rows[mid])/2,
        samples:rows.length,age_seconds:(now-this.values[0].at)/1000};
    }
  }
  function safeCandidate(value) {
    if (!value || typeof value.candidate!=='string' || value.candidate.length>2048
        || !/\btyp relay(?:\s|$)/.test(value.candidate)) return null;
    return {candidate:value.candidate.replace(/\braddr\s+\S+/g,'raddr 0.0.0.0').replace(/\brport\s+\d+/g,'rport 0'),
      sdpMid:value.sdpMid==null?null:String(value.sdpMid),sdpMLineIndex:value.sdpMLineIndex==null?null:value.sdpMLineIndex};
  }
  function safeSdp(sdp) {
    return String(sdp).split(/\r?\n/).filter(line=>!line.startsWith('a=candidate:') || /\btyp relay(?:\s|$)/.test(line))
      .map(line=>line.startsWith('c=IN IP') ? 'c=IN IP4 0.0.0.0' : line.startsWith('o=') ?
        line.replace(/IN IP[46] \S+$/,'IN IP4 0.0.0.0') :
        line.replace(/\braddr\s+\S+/g,'raddr 0.0.0.0').replace(/\brport\s+\d+/g,'rport 0')).join('\r\n');
  }
  function relayPair(stats) {
    let selected;
    for (const row of stats.values()) if (row.type==='transport' && row.selectedCandidatePairId) selected=stats.get(row.selectedCandidatePairId);
    if (!selected) for (const row of stats.values()) if (row.type==='candidate-pair' && row.nominated && row.state==='succeeded') selected=row;
    return !!(selected && stats.get(selected.localCandidateId)?.candidateType==='relay'
      && stats.get(selected.remoteCandidateId)?.candidateType==='relay');
  }
  class RelayEngine {
    constructor(send, options={}) {
      this.send=send; this.PC=options.PC || root.RTCPeerConnection;
      this.now=options.now || (()=>performance.now());
      this.random=options.random || (()=>crypto.randomUUID().replace(/-/g,''));
      this.peers=new Map(); this.retry=new Map(); this.state=null; this.ack=0;
    }
    closePeer(id, delay=0) {
      const entry=this.peers.get(id);
      if (entry) { entry.closed=true; clearInterval(entry.timer); entry.pc.close(); this.peers.delete(id); }
      this.retry.set(id,this.now()+delay);
    }
    retire(entry, delay=0) {
      // Async work from an old attempt must never close its replacement.
      if (this.peers.get(entry.id)===entry) this.closePeer(entry.id,delay);
    }
    reset() { for (const id of this.peers.keys()) this.closePeer(id); this.retry.clear(); this.ack=0; this.state=null; }
    async post(entry, signalType, data) {
      const body={generation:entry.generation,type:'signal',peer:entry.id,peer_revision:entry.revision,
        attempt:entry.attempt,signal_type:signalType,data};
      entry.outbox=entry.outbox.then(async()=>{
        if (entry.closed) return;
        const result=await this.send(body);
        if (!result || !result.ok) throw new Error('Signaling rejected');
      }).catch(()=>this.retire(entry,5000));
      return entry.outbox;
    }
    async describe(entry, type) {
      const description=type==='offer'?await entry.pc.createOffer():await entry.pc.createAnswer();
      await entry.pc.setLocalDescription(description);
      await this.post(entry,type,safeSdp(entry.pc.localDescription.sdp));
      entry.described=true;
      for (const candidate of entry.localCandidates.splice(0)) await this.post(entry,'candidate',candidate);
    }
    attachChannel(entry, channel) {
      if (entry.channel) { channel.close(); return; }
      entry.channel=channel;
      channel.onmessage=event=>{
        if (entry.closed || typeof event.data!=='string' || event.data.length>128) return;
        let packet; try {packet=JSON.parse(event.data);} catch {return;}
        if (!packet || typeof packet.id!=='string' || !/^[a-f0-9]{32}$/.test(packet.id)) return;
        if (packet.t==='p' && channel.readyState==='open') channel.send(JSON.stringify({t:'e',id:packet.id}));
        else if (packet.t==='e') entry.samples.echo(packet.id);
      };
      channel.onerror=()=>this.retire(entry,5000);
      channel.onclose=()=>this.retire(entry,5000);
    }
    make(peer, attempt) {
      if (this.peers.size>=32) return null;
      const pc=new this.PC({iceServers:this.state.iceServers,iceTransportPolicy:'relay',bundlePolicy:'max-bundle'});
      const entry={id:peer.steam_id,revision:peer.revision,attempt:attempt || this.random(),pc,
        generation:this.state.generation,born:this.now(),closed:false,described:false,localCandidates:[],remoteCandidates:[],
        outbox:Promise.resolve(),samples:new Samples(this.now),lastReport:0,reporting:false};
      this.peers.set(entry.id,entry);
      pc.onicecandidate=event=>{
        const candidate=safeCandidate(event.candidate);
        if (!candidate || entry.closed) return;
        if (!entry.described) { if(entry.localCandidates.length<24) entry.localCandidates.push(candidate); }
        else void this.post(entry,'candidate',candidate);
      };
      pc.ondatachannel=event=>this.attachChannel(entry,event.channel);
      pc.onconnectionstatechange=()=>{ if (['failed','closed'].includes(pc.connectionState) && !entry.closed) this.retire(entry,5000); };
      entry.timer=setInterval(()=>void this.tick(entry),500);
      return entry;
    }
    async tick(entry) {
      if (entry.closed) return;
      if (this.now()-entry.born>30000 || (this.now()-entry.born>12000 && entry.channel?.readyState!=='open')) {
        this.retire(entry,3000); return;
      }
      if (entry.channel?.readyState!=='open') return;
      const id=this.random(); entry.samples.sent(id);
      try {entry.channel.send(JSON.stringify({t:'p',id}));} catch {this.retire(entry,5000);return;}
      const values=entry.samples.read();
      if (!values || entry.reporting || this.now()-entry.lastReport<4000) return;
      entry.reporting=true;
      try {
        if (!relayPair(await entry.pc.getStats())) { this.retire(entry,10000); return; }
        if (entry.closed) return;
        const fresh=entry.samples.read(); if(!fresh) return;
        await this.send({generation:entry.generation,type:'measurement',peer:entry.id,peer_revision:entry.revision,
          attempt:entry.attempt,...fresh});
        entry.lastReport=this.now();
      } catch {this.retire(entry,5000);} finally {entry.reporting=false;}
    }
    async signal(event) {
      if (event.target_revision!==this.state.revision || !this.state.queued) return;
      const peer=this.state.peers.find(p=>p.steam_id===event.from && p.revision===event.revision);
      if (!peer || !/^[a-f0-9]{32}$/.test(event.attempt || '')) return;
      const signal=event.signal || {};
      let entry=this.peers.get(peer.steam_id);
      if (entry && entry.attempt!==event.attempt) {
        if (signal.type!=='offer' || this.state.steam_id<peer.steam_id) return;
        this.closePeer(peer.steam_id); entry=null;
      }
      if (!entry) {
        if (signal.type!=='offer') return;
        entry=this.make(peer,event.attempt); if (!entry) return;
      }
      if (signal.type==='candidate') {
        const candidate=safeCandidate(signal.data); if (!candidate) return;
        if (entry.pc.remoteDescription) await entry.pc.addIceCandidate(candidate);
        else if (entry.remoteCandidates.length<24) entry.remoteCandidates.push(candidate);
      } else if (signal.type==='offer' || signal.type==='answer') {
        if (typeof signal.data!=='string' || signal.data.length>16384 || /m=(audio|video)\b/.test(signal.data)) return;
        await entry.pc.setRemoteDescription({type:signal.type,sdp:safeSdp(signal.data)});
        for (const candidate of entry.remoteCandidates.splice(0)) await entry.pc.addIceCandidate(candidate);
        if (signal.type==='offer') await this.describe(entry,'answer');
      }
    }
    async update(state) {
      if (!state || !state.generation) {this.reset();return;}
      if (!this.state || this.state.generation!==state.generation || this.state.revision!==state.revision) this.reset();
      this.state=state;
      if (!this.PC) {await this.send({generation:state.generation,type:'error',error:'WebRTC requires the current desktop web interface.'});return;}
      const known=new Set((state.peers || []).map(peer=>peer.steam_id));
      if (!state.queued || !state.revision || !state.iceServers?.length || state.expires_at<=Date.now()) {
        for (const id of this.peers.keys()) this.closePeer(id);
        if (!state.queued) this.retry.clear();
        else for (const [id,deadline] of this.retry) if (!known.has(id) || deadline<=this.now()) this.retry.delete(id);
      } else {
        for (const [id,entry] of this.peers) if (!state.peers.some(p=>p.steam_id===id && p.revision===entry.revision)) this.closePeer(id);
        for (const [id,deadline] of this.retry) if (!known.has(id) || deadline<=this.now()) this.retry.delete(id);
        for (const event of state.signals || []) {
          if (event.seq<=this.ack) continue;
          try {await this.signal(event);} catch {this.closePeer(event.from,5000);}
          this.ack=event.seq;
        }
        for (const peer of state.peers.slice(0,32)) {
          if (this.peers.has(peer.steam_id) || (this.retry.get(peer.steam_id)||0)>this.now()) continue;
          const entry=this.make(peer); if (!entry) break;
          this.attachChannel(entry,entry.pc.createDataChannel('lightsout-ping',{ordered:false,maxRetransmits:0}));
          try {await this.describe(entry,'offer');} catch {this.retire(entry,5000);}
        }
      }
      await this.send({generation:state.generation,type:'ready',supported:true,ack:this.ack});
    }
  }
  const exported={Samples,safeCandidate,safeSdp,relayPair,RelayEngine};
  if (typeof module!=='undefined' && module.exports) {module.exports=exported;return;}
  root.HubRelay=exported;
  const send=async body=>{
    const response=await fetch('/network',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    return response.ok?response.json():{ok:false};
  };
  const engine=new RelayEngine(send);
  let stopped=false;
  // A slow offer/answer HTTP exchange must not make the local browser look dead.
  const heartbeat=setInterval(()=>{
    if (engine.state && engine.PC) void send({generation:engine.state.generation,type:'ready',supported:true,ack:engine.ack}).catch(()=>{});
  },2000);
  async function poll() {
    if (stopped) return;
    try {const response=await fetch('/network'); if(response.ok) await engine.update(await response.json());}
    catch {engine.reset();}
    if (!stopped) setTimeout(poll,500);
  }
  root.addEventListener('beforeunload',()=>{stopped=true;clearInterval(heartbeat);engine.reset();});
  void poll();
})(typeof window==='undefined'?globalThis:window);
