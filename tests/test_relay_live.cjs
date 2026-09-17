// Opt-in real relay test: run under `railway run ... node tests/test_relay_live.cjs`.
// Never prints provider secrets or temporary ICE credentials. Two independent browser contexts.
const assert=require('node:assert/strict');
const http=require('node:http');
const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const {createCredentialIssuer,validateSignal}=require('../server/relay.cjs');
(async()=>{
  const issuer=createCredentialIssuer();
  const configs=await Promise.all([issuer.issue('relay-smoke-a'),issuer.issue('relay-smoke-b')]);
  console.log('Cloudflare credentials accepted; temporary relay configuration received.');
  const ids=['76561198000000001','76561198000000002'];
  const states=ids.map((id,i)=>({...configs[i],generation:String(i+1).repeat(32),steam_id:id,
    revision:String(i+3).repeat(24),queued:true,signals:[],peers:[]}));
  states.forEach((s,i)=>s.peers=[{steam_id:ids[1-i],revision:states[1-i].revision}]);
  const measurements=[[],[]], failures=[];let seq=0,nativeRoutes=null;
  const script=fs.readFileSync(path.resolve(__dirname,'../hub/webui/static/network.js'));
  const server=http.createServer(async(req,res)=>{
    const url=new URL(req.url,'http://localhost');
    const cookie=/peer=([01])/.exec(req.headers.cookie || '');
    const index=cookie?Number(cookie[1]):Number(url.searchParams.get('peer') || 0);
    const send=(status,body)=>{res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(body));};
    if(url.pathname==='/smoke-status'){send(200,{ready:measurements.every(rows=>rows.length)});return;}
    if(url.pathname==='/smoke-result'){
      let raw='';for await(const chunk of req)raw+=chunk;
      nativeRoutes=JSON.parse(raw);send(200,{ok:true});return;
    }
    if(url.pathname==='/network.js'){res.writeHead(200,{'Content-Type':'application/javascript'});res.end(script);return;}
    if(url.pathname!=='/network'){
      res.writeHead(200,{'Content-Type':'text/html','Set-Cookie':`peer=${index}; SameSite=Strict`});
      res.end('<!doctype html><title>Relay test</title><script>const Native=window.RTCPeerConnection;window.testConnections=[];window.RTCPeerConnection=class extends Native{constructor(options){super(options);window.testConnections.push(this);}};</script><script src="/network.js"></script>');return;
    }
    if(req.method==='GET'){send(200,states[index]);return;}
    let raw='';for await(const chunk of req)raw+=chunk;
    try{
      const body=JSON.parse(raw),state=states[index];
      assert.equal(body.generation,state.generation);
      if(body.type==='ready')state.signals=state.signals.filter(e=>e.seq>(body.ack || 0));
      else if(body.type==='signal'){
        assert.equal(body.peer,ids[1-index]);
        assert.equal(body.peer_revision,states[1-index].revision);
        const data=validateSignal(body.signal_type,body.data);
        states[1-index].signals.push({type:'network_signal',seq:++seq,from:ids[index],revision:state.revision,
          target_revision:states[1-index].revision,attempt:body.attempt,signal:{type:body.signal_type,data}});
      }else if(body.type==='measurement'){
        assert.ok(Number.isFinite(body.ping)&&body.ping>=0&&body.samples>=5&&body.age_seconds<=15);
        measurements[index].push(body);
      }else if(body.type==='error')failures.push(String(body.error).slice(0,200));
      send(200,{ok:true});
    }catch(error){failures.push(error.message);send(400,{ok:false});}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  if(process.argv.includes('--webview2')){
    const port=server.address().port;
    const child=spawn(process.env.HUB_TEST_PYTHON || 'python',[path.join(__dirname,'relay_webview_smoke.py'),
      `http://127.0.0.1:${port}/?peer=0`,`http://localhost:${port}/?peer=1`],{windowsHide:true,stdio:['ignore','pipe','pipe']});
    let diagnostic='';child.stdout.on('data',x=>diagnostic+=x);child.stderr.on('data',x=>diagnostic+=x);
    const timeout=setTimeout(()=>child.kill(),65000);
    const code=await new Promise(resolve=>child.on('exit',resolve));clearTimeout(timeout);
    await new Promise(resolve=>server.close(resolve));
    assert.equal(code,0,'WebView2 runtime failed: '+diagnostic.slice(-1500));
    assert.ok(measurements.every(rows=>rows.length));
    assert.ok(nativeRoutes && nativeRoutes.every(rows=>rows.length&&rows.every(r=>r.policy==='relay'&&r.local==='relay'&&r.remote==='relay')));
    assert.deepEqual(failures,[]);
    console.log(JSON.stringify({webview2RelayPassed:true,measurements:measurements.map(rows=>({ping:Math.round(rows[0].ping),samples:rows[0].samples})),routes:nativeRoutes}));
    return;
  }
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  try{
    const contexts=await Promise.all([browser.newContext(),browser.newContext()]);
    const pages=await Promise.all(contexts.map(c=>c.newPage()));
    await Promise.all(pages.map((page,i)=>page.goto(`http://127.0.0.1:${server.address().port}/?peer=${i}`)));
    const deadline=Date.now()+45000;
    while(Date.now()<deadline && !measurements.every(rows=>rows.length))await new Promise(resolve=>setTimeout(resolve,500));
    assert.ok(measurements.every(rows=>rows.length),'Both peers must report usable real relay RTTs; signaling failures: '+failures.join(', '));
    const routes=await Promise.all(pages.map(page=>page.evaluate(async()=>{
      const result=[];
      for(const pc of window.testConnections){
        if(pc.connectionState!=='connected')continue;
        const stats=await pc.getStats();
        for(const row of stats.values())if(row.type==='transport'&&row.selectedCandidatePairId){
          const pair=stats.get(row.selectedCandidatePairId);
          result.push({policy:pc.getConfiguration().iceTransportPolicy,
            local:stats.get(pair.localCandidateId)?.candidateType,remote:stats.get(pair.remoteCandidateId)?.candidateType});
        }
      }return result;
    })));
    assert.ok(routes.every(rows=>rows.length&&rows.every(r=>r.policy==='relay'&&r.local==='relay'&&r.remote==='relay')));
    assert.deepEqual(failures,[]);
    console.log(JSON.stringify({realRelayPassed:true,measurements:measurements.map(rows=>({ping:Math.round(rows[0].ping),samples:rows[0].samples})),routes}));
  }finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error('Relay smoke test failed:',error.message);process.exitCode=1;});
