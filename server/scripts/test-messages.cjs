// Run with ACCOUNT_TEST_PYTHON pointing to Python with fakeredis[lua].
'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{spawn}=require('node:child_process'),readline=require('node:readline');
const file=path.join(__dirname,'../messages.cjs');
test('persistent private messaging service exists',()=>assert(fs.existsSync(file)));
const api=fs.existsSync(file)?require(file):{};
function redis(t){const child=spawn(process.env.ACCOUNT_TEST_PYTHON||'python',['-u',path.join(__dirname,'account-redis-fixture.py')]);let seq=0;const waiting=new Map();readline.createInterface({input:child.stdout}).on('line',line=>{const r=JSON.parse(line),p=waiting.get(r.id);waiting.delete(r.id);if(p)r.error?p.reject(Error(r.error)):p.resolve(r.result);});child.on('exit',()=>{for(const p of waiting.values())p.reject(Error('Fixture exited'));});t.after(()=>child.kill());return args=>new Promise((resolve,reject)=>{const id=++seq;waiting.set(id,{resolve,reject});child.stdin.write(JSON.stringify({id,command:args})+'\n');});}
const a='76561198000000001',b='76561198000000002',c='a1111111-1111-4111-8111-111111111111';
test('only mutual friends send; retries, unread receipts and offline history survive restart',async t=>{
 const store=redis(t),svc=api.create({store}),body={target:b,text:'Hello <script>friend</script>',client_id:require('node:crypto').randomUUID()};
 assert.equal((await svc.send(a,body)).ok,false);await store(['SADD','hub:friends:'+a,b]);assert.equal((await svc.send(a,body)).ok,false);await store(['SADD','hub:friends:'+b,a]);
 assert.equal((await svc.send(a,body)).ok,true);assert.equal((await svc.send(a,body)).ok,true);
 const rebuilt=api.create({store});assert.equal((await rebuilt.inbox(b)).unread,1);
 const thread=await rebuilt.thread(b,a);assert.equal(thread.messages.length,1);assert.equal(thread.messages[0].text,body.text);
 await rebuilt.markRead(b,a,thread.messages[0].seq);await rebuilt.markRead(b,a,thread.messages[0].seq);assert.equal((await rebuilt.inbox(b)).unread,0);
 await rebuilt.block(b,a);assert.equal((await svc.send(a,{...body,client_id:require('node:crypto').randomUUID()})).ok,false);
});
test('official admin contact is separate, private and can reach an offline account',async t=>{
 const store=redis(t),svc=api.create({store}),body={target:c,text:'Your event ticket has a response.',client_id:require('node:crypto').randomUUID()};
 assert.equal((await svc.send(a,body)).ok,false);assert.equal((await svc.send(a,body,{admin:true})).ok,true);
 const inbox=await svc.inbox(c);assert.equal(inbox.threads[0].official,true);assert.equal(inbox.unread,1);
 const thread=await svc.thread(c,'admin');assert.equal(thread.messages[0].sender,'admin');assert(!JSON.stringify(thread).includes(a));
 assert.equal((await svc.send(b,{...body,target:'admin'})).ok,false,'players cannot create unsolicited admin threads');
 assert.equal((await svc.send(c,{...body,target:'admin',client_id:require('node:crypto').randomUUID(),text:'Thanks, here is my follow-up.'})).ok,true);
 assert.equal((await svc.adminInbox()).unread,1);
 assert.equal((await svc.adminThread(c)).messages.length,2);
 assert.equal((await svc.thread(b,'admin')).messages.length,0,'another player cannot read this official conversation');
 await svc.adminRead(c,2);assert.equal((await svc.adminInbox()).unread,0);
});

test('official conversations have a reserved place even at the friend inbox limit',async t=>{
 const store=redis(t),svc=api.create({store});
 for(let i=0;i<100;i++)await store(['HSET','hub:message:inbox:'+b,'friend:old:'+i,JSON.stringify({thread:'friend:old:'+i,target:a,last_at:i})]);
 assert.equal((await svc.send(a,{target:b,text:'Official contact',client_id:require('node:crypto').randomUUID()},{admin:true})).ok,true);
 assert.equal((await svc.inbox(b)).threads.length,101);
});

test('admin inbox, conversation and read receipts require an active allowlisted session',async()=>{
 let allowed=true,calls=0;
 const router=require('../admin.cjs').create({upstashCmd:async()=>({steam_id:a}),live:()=>({isAdmin:()=>allowed,adminMessages:async()=>{calls++;return {ok:true,threads:[]};}})});
 async function request(path,cookie){const res={writeHead(status){this.status=status;},setHeader(){},end(body){this.body=body;}};await router.route({headers:{cookie:cookie?'hubadmin=test':''}},res,'GET',path,new URL('http://localhost'+path));return res;}
 for(const endpoint of ['/admin/messages/data','/admin/messages/thread?target='+b]){const pathname=endpoint.split('?')[0];assert.equal((await request(pathname,false)).status,401);}
 assert.equal(calls,0);assert.equal((await request('/admin/messages/data',true)).status,200);allowed=false;assert.equal((await request('/admin/messages/data',true)).status,401);assert.equal(calls,1);
});

test('friend acceptance is durable before messaging and blocking clears requests and forbids new ones',async t=>{
 const store=redis(t),svc=api.create({store}),live=require('../live.cjs').create({upstashCmd:store});t.after(()=>live.shutdown());await live._internals.ready;
 assert.equal((await live.requestFriend(a,{target:b})).ok,true);assert.equal((await live.acceptFriend(b,{target:a})).ok,true);
 assert.equal((await svc.send(a,{target:b,text:'Immediately after acceptance',client_id:require('node:crypto').randomUUID()})).ok,true);
 await store(['SADD','hub:friendreq:out:'+a,b]);await store(['SADD','hub:friendreq:in:'+b,a]);
 assert.equal((await svc.block(b,a)).ok,true);assert.equal(await store(['SISMEMBER','hub:friends:'+a,b]),0);assert.equal(await store(['SCARD','hub:friendreq:in:'+b]),0);
 assert.equal((await live.requestFriend(a,{target:b})).ok,false);assert.equal((await live.acceptFriend(b,{target:a})).ok,false);
 assert.equal((await svc.block(a,c)).error,'unknown_conversation');assert.equal(await store(['SCARD','hub:message:blocks:'+a]),0);
 for(let i=0;i<500;i++)await store(['SADD','hub:message:blocks:'+b,String(76561197000000000n+BigInt(i))]);
 await store(['SADD','hub:friends:'+b,c]);assert.equal((await svc.block(b,c)).error,'block_limit');
});

test('admin history keeps moderator attribution private and inbox reads are paginated',async t=>{
 const store=redis(t),svc=api.create({store});await svc.send(a,{target:b,text:'Admin audit message',client_id:require('node:crypto').randomUUID()},{admin:true});
 assert.equal((await svc.adminThread(b)).messages[0].admin_actor,a);assert(!('admin_actor' in (await svc.thread(b,'admin')).messages[0]));
 for(let i=0;i<101;i++){const target=String(76561197000000000n+BigInt(i)),thread='official:'+target;await store(['HSET','hub:message:inbox:admin',thread,JSON.stringify({thread,target,last_at:i})]);await store(['ZADD','hub:message:admin-index',String(i),thread]);}
 const first=await svc.adminInbox();assert.equal(first.threads.length,100);assert.equal(first.next_cursor,100);assert.equal((await svc.adminInbox(100)).threads.length,2);
});

test('banned players retain event appeals and official support without gameplay or friend messaging',async t=>{
 const store=redis(t),svc=api.create({store});
 await store(['SET','hub:ban:'+b,JSON.stringify({until:0,reason:'Review ban'})]);
 await store(['SADD','hub:friends:'+a,b]);await store(['SADD','hub:friends:'+b,a]);
 await svc.send(a,{target:b,text:'Friend history',client_id:require('node:crypto').randomUUID()});
 await svc.send(a,{target:b,text:'Official review',client_id:require('node:crypto').randomUUID()},{admin:true});
 const live=require('../live.cjs').create({upstashCmd:store,whoami:async()=>({steam_id:b}),bearer:()=>'',sendJson:(res,status,body)=>{res.status=status;res.body=body;},readBody:async req=>Buffer.from(JSON.stringify(req.body||{})),tournament:{view:async()=>({ok:true}),support:async()=>({ok:true,ticket_id:'appeal'})}});
 t.after(()=>live.shutdown());await live._internals.ready;
 async function request(path,method='GET',body={}){const res={setHeader(){}};await live.route({headers:{},body},res,method,path.split('?')[0],new URL('http://localhost'+path));return res;}
 assert.equal((await request('/api/tournament')).status,200);
 assert.equal((await request('/api/tournament/support','POST',{category:'appeal'})).status,200);
 assert.equal((await request('/api/tournament/register','POST')).status,403);
 assert.equal((await request('/api/queue/join','POST')).status,403);
 const inbox=await request('/api/messages');assert.equal(inbox.status,200);assert.equal(inbox.body.threads.length,1);assert.equal(inbox.body.threads[0].target,'admin');assert.equal(inbox.body.unread,1);
 assert.equal((await request('/api/messages/thread?target='+a)).status,403);
 assert.equal((await request('/api/messages/thread?target=admin')).status,200);
 assert.equal((await request('/api/messages/send','POST',{target:a,text:'Blocked',client_id:require('node:crypto').randomUUID()})).status,403);
 assert.equal((await request('/api/messages/send','POST',{target:'admin',text:'My appeal',client_id:require('node:crypto').randomUUID()})).status,200);
 assert.equal((await request('/api/messages/read','POST',{target:'admin',through_seq:2})).status,200);
 assert.equal((await request('/api/messages/block','POST',{target:a})).status,403);
});
