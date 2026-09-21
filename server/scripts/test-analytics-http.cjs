process.env.NODE_ENV = 'test';
// Run: node --test server/scripts/test-analytics-http.cjs
const test=require('node:test'),assert=require('node:assert/strict'),http=require('node:http');
const sid='76561198000000001';
process.env.HUB_TEST_TOKENS='analytics-test='+sid;
const analytics=require('../analytics.cjs').create();
const server=require('../server.cjs').createServer({analyticsService:analytics});
let url;
test.before(async()=>{await new Promise(r=>server.listen(0,'127.0.0.1',r));url='http://127.0.0.1:'+server.address().port;});
test.after(async()=>{await analytics.close();await new Promise(r=>server.close(r));});
test('telemetry requires identity and rejects malformed batches',async()=>{
  assert.equal((await fetch(url+'/api/telemetry',{method:'POST',body:'{}'})).status,401);
  assert.equal((await fetch(url+'/api/telemetry',{method:'POST',headers:{authorization:'Bearer analytics-test'},body:'{}'})).status,400);
});
test('accepted client event uses authenticated identity and survives replay',async()=>{
  const body=JSON.stringify({events:[{id:'http-1',type:'app.action',at:Date.now(),actor_id:'bad',data:{action:'accept',password:'secret'}}]});
  for(let i=0;i<2;i++)assert.equal((await fetch(url+'/api/telemetry',{method:'POST',headers:{authorization:'Bearer analytics-test'},body})).status,200);
  const result=await analytics.query('events',{category:'app'});
  assert.equal(result.rows.length,1);assert.equal(result.rows[0].actor_id,sid);
});
test('analytics admin endpoints recheck sign-in and render a useful page',async()=>{
  let allowed=true;
  const admin=require('../admin.cjs').create({analytics,upstashCmd:async()=>({steam_id:sid}),live:()=>({isAdmin:()=>allowed})});
  const app=http.createServer(async(req,res)=>{try{const u=new URL(req.url,'http://local');if(!await admin.route(req,res,req.method,u.pathname,u)){res.writeHead(404);res.end();}}catch{res.writeHead(500);res.end();}});
  await new Promise(r=>app.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+app.address().port;
  try{
    assert.equal((await fetch(base+'/admin/analytics/data')).status,401);
    const options={headers:{cookie:'hubadmin=valid'}};
    const page=await fetch(base+'/admin/analytics',options);assert.equal(page.status,200);assert.match(await page.text(),/Match explorer/);
    const data=await fetch(base+'/admin/analytics/data',options);assert.equal(data.status,200);assert.equal((await data.json()).ok,true);
    allowed=false;
    for(const route of ['data','match?id=one','export','audits','events','fairplay'])assert.equal((await fetch(base+'/admin/analytics/'+route,options)).status,401);
  }finally{await new Promise(r=>app.close(r));}
});

test('authenticated ingestion is rate limited without acknowledging rejected batches',async()=>{
  let response;
  for(let i=0;i<31;i++)response=await fetch(url+'/api/telemetry',{method:'POST',headers:{authorization:'Bearer analytics-test'},body:JSON.stringify({events:[{id:'rate-'+i,type:'app.action',at:Date.now()}]})});
  assert.equal(response.status,429);assert.equal(response.headers.get('retry-after'),'60');assert.equal((await response.json()).accepted,undefined);
});
