'use strict';
const crypto=require('node:crypto'),identity=require('./player-identity.cjs');
const SEND=`-- private-message-send-v1
local types={'zset','string','hash','hash','zset','string','string','set','set','set','set','zset','hash','zset'}
for i,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;if t~='none' and t~=types[i] then return redis.error_reply('message key type') end end
local old=redis.call('GET',KEYS[6]);if old then local prior=cjson.decode(old);if prior.hash~=ARGV[8] then return {'conflict'} end;return {'sent',prior.message} end
local official=ARGV[6]~='0'
if ARGV[6]=='2' and redis.call('HEXISTS',KEYS[3],cjson.decode(ARGV[1]).thread)==0 then return {'admin_contact_required'} end
if not official and (redis.call('SISMEMBER',KEYS[8],ARGV[5])~=1 or redis.call('SISMEMBER',KEYS[9],ARGV[4])~=1) then return {'friends_only'} end
if not official and (redis.call('SISMEMBER',KEYS[10],ARGV[5])==1 or redis.call('SISMEMBER',KEYS[11],ARGV[4])==1) then return {'blocked'} end
if tonumber(redis.call('GET',KEYS[7]) or '0')>=30 then return {'rate_limited'} end
local row=cjson.decode(ARGV[1]);local sent=cjson.decode(ARGV[2]);local received=cjson.decode(ARGV[3])
if not official and ((redis.call('HEXISTS',KEYS[3],row.thread)==0 and redis.call('HLEN',KEYS[3])-redis.call('HEXISTS',KEYS[3],'official:'..ARGV[4])>=100) or (redis.call('HEXISTS',KEYS[4],row.thread)==0 and redis.call('HLEN',KEYS[4])-redis.call('HEXISTS',KEYS[4],'official:'..ARGV[5])>=100)) then return {'inbox_full'} end
local seq=tonumber(redis.call('GET',KEYS[2]) or '0')+1
if not seq or seq>9007199254740000 then return redis.error_reply('message sequence') end
row.seq=seq;local encoded=cjson.encode(row)
redis.call('SET',KEYS[2],seq);redis.call('ZADD',KEYS[1],seq,encoded)
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',seq-500)
redis.call('ZREMRANGEBYSCORE',KEYS[5],'-inf',seq-500);redis.call('ZREMRANGEBYSCORE',KEYS[12],'-inf',seq-500)
redis.call('ZADD',KEYS[5],seq,row.id)
redis.call('HSET',KEYS[3],row.thread,cjson.encode(sent))
redis.call('HSET',KEYS[4],row.thread,cjson.encode(received))
if official then redis.call('ZADD',KEYS[14],ARGV[7],row.thread) end
redis.call('INCR',KEYS[7]);if redis.call('TTL',KEYS[7])<0 then redis.call('EXPIRE',KEYS[7],60) end
redis.call('SET',KEYS[6],cjson.encode({hash=ARGV[8],message=encoded}),'EX',604800)
return {'sent',encoded}`;
const INBOX=`-- private-message-inbox-v1
local rows=redis.call('HVALS',KEYS[1]);if #rows>tonumber(ARGV[2]) then return redis.error_reply('message inbox capacity') end
local out={};for _,raw in ipairs(rows) do local row=cjson.decode(raw);row.unread=redis.call('ZCARD',ARGV[1]..row.thread);table.insert(out,cjson.encode(row)) end;return out`;
const ADMIN_INBOX=`-- admin-message-inbox-v1
local ids=redis.call('ZREVRANGE',KEYS[2],ARGV[2],tonumber(ARGV[2])+99);local out={}
for _,id in ipairs(ids) do local raw=redis.call('HGET',KEYS[1],id);if raw then local row=cjson.decode(raw);row.unread=redis.call('ZCARD',ARGV[1]..row.thread);table.insert(out,cjson.encode(row)) end end
return {out,redis.call('ZCARD',KEYS[2])}`;
const BLOCK=`-- private-message-block-v1
for i,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;local wanted=i==8 and 'hash' or 'set';if t~='none' and t~=wanted then return redis.error_reply('message relationship type') end end
if redis.call('SISMEMBER',KEYS[1],ARGV[1])==1 then return 'blocked' end
if redis.call('SCARD',KEYS[1])>=500 then return 'block_limit' end
if redis.call('SISMEMBER',KEYS[2],ARGV[1])==0 and redis.call('SISMEMBER',KEYS[4],ARGV[1])==0 and redis.call('SISMEMBER',KEYS[5],ARGV[1])==0 and redis.call('HEXISTS',KEYS[8],ARGV[3])==0 then return 'unknown_conversation' end
redis.call('SADD',KEYS[1],ARGV[1]);for _,i in ipairs({2,4,5}) do redis.call('SREM',KEYS[i],ARGV[1]) end;for _,i in ipairs({3,6,7}) do redis.call('SREM',KEYS[i],ARGV[2]) end;return 'blocked'`;
function create({store,prefix='hub:',now=Date.now,nudge=()=>{},nameOf=()=>''}={}){
 const base=prefix+'message:';
 async function call(args){if(!store)throw Error('Message storage unavailable');return store(args,{strict:true,timeout:5000});}
 const threadId=(me,target)=>target==='admin'?'official:'+me:'friend:'+ [me,target].sort().join(':');
 const clean=m=>({id:m.id,seq:m.seq,at:m.at,sender:m.sender,text:m.text,official:m.official,context:m.context||null});
 async function send(sender,body,{admin=false,context=null}={}){
   const target=String(body?.target||''),text=String(body?.text||'').trim(),id=String(body?.client_id||'');
   const reply=!admin&&target==='admin';
   if(!identity.validPlayer(sender)||!(reply||identity.validPlayer(target))||!admin&&target===sender||text.length<1||text.length>1000||!/^[0-9a-f-]{36}$/.test(id))return {ok:false,error:'invalid_message'};
   const thread=admin?threadId(target,'admin'):threadId(sender,target),at=now();
   const filtered=require('./censor.cjs').censor(text);
   const message={id,thread,at,sender:admin?'admin':sender,text:filtered,official:admin,...(admin?{admin_actor:sender,context}: {})};
   const outgoing={thread,target,persona:reply?'Lights Out Admin':String(await nameOf(target)||target).slice(0,80),last_at:at,last_text:filtered.slice(0,120),official:reply};
   const incoming={thread,target:admin?'admin':sender,persona:admin?'Lights Out Admin':String(await nameOf(sender)||sender).slice(0,80),last_at:at,last_text:filtered.slice(0,120),official:admin};
   const result=await call(['EVAL',SEND,'14',base+'thread:'+thread,base+'seq:'+thread,base+'inbox:'+(admin?'admin':sender),base+'inbox:'+target,base+'unread:'+target+':'+thread,base+'dedupe:'+sender+':'+id,base+'rate:'+sender,prefix+'friends:'+sender,prefix+'friends:'+target,base+'blocks:'+sender,base+'blocks:'+target,base+'unread:'+(admin?'admin':sender)+':'+thread,base+'inbox:admin',base+'admin-index',JSON.stringify(message),JSON.stringify(outgoing),JSON.stringify(incoming),sender,target,admin?'1':reply?'2':'0',String(at),crypto.createHash('sha256').update(JSON.stringify({target,text,admin,context})).digest('hex')]);
   if(result?.[0]!=='sent')return {ok:false,error:result?.[0]||'unavailable'};
   nudge(target);if(!admin)nudge(sender);return {ok:true,message:clean(JSON.parse(result[1]))};
 }
 async function readInbox(me,limit){
   const rows=await call(['EVAL',INBOX,'1',base+'inbox:'+me,base+'unread:'+me+':',String(limit)]);
   if(!Array.isArray(rows))throw Error('Invalid inbox');
   const threads=rows.map(JSON.parse).sort((a,b)=>b.last_at-a.last_at);
   return {ok:true,threads,unread:threads.reduce((sum,t)=>sum+t.unread,0)};
 }
 async function inbox(me){if(!identity.validPlayer(me))return {ok:false,error:'identity_required'};return readInbox(me,101);}
 async function adminInbox(cursor=0){const offset=Number(cursor||0);if(!Number.isSafeInteger(offset)||offset<0)return {ok:false,error:'invalid_cursor'};const result=await call(['EVAL',ADMIN_INBOX,'2',base+'inbox:admin',base+'admin-index',base+'unread:admin:',String(offset)]);const threads=result[0].map(JSON.parse);return {ok:true,threads,total:result[1],unread:threads.reduce((sum,t)=>sum+t.unread,0),next_cursor:offset+100<result[1]?offset+100:null};}
 async function adminThread(target,before){return thread(target,'admin',before,true);}
 async function adminRead(target,through){if(!identity.validPlayer(target)||!Number.isSafeInteger(through)||through<0)return {ok:false,error:'invalid_receipt'};await call(['ZREMRANGEBYSCORE',base+'unread:admin:'+threadId(target,'admin'),'-inf',String(through)]);return {ok:true};}
 async function thread(me,target,before,adminView=false){
   if(!identity.validPlayer(me)||target!=='admin'&&!identity.validPlayer(target)||me===target)return {ok:false,error:'invalid_recipient'};
   const boundary=before===undefined||before===null||before===''?'+inf':String(Math.max(0,Math.floor(Number(before))-1));
   if(boundary!=='+inf'&&!/^\d+$/.test(boundary))return {ok:false,error:'invalid_cursor'};
   const rows=await call(['ZREVRANGEBYSCORE',base+'thread:'+threadId(me,target),boundary,'-inf','LIMIT','0','50']);
   if(!Array.isArray(rows))throw Error('Invalid conversation');
   const messages=rows.map(JSON.parse).map(m=>({...clean(m),...(adminView&&m.admin_actor?{admin_actor:m.admin_actor}: {})})).reverse();
   const blocked=target==='admin'?false:!!await call(['SISMEMBER',base+'blocks:'+me,target]);
   return {ok:true,target,official:target==='admin',messages,blocked,next_before:messages.length===50?messages[0].seq:null};
 }
 async function markRead(me,target,through){
   if(!identity.validPlayer(me)||target!=='admin'&&!identity.validPlayer(target)||!Number.isSafeInteger(through)||through<0)return {ok:false,error:'invalid_receipt'};
   await call(['ZREMRANGEBYSCORE',base+'unread:'+me+':'+threadId(me,target),'-inf',String(through)]);return {ok:true};
 }
 async function block(me,target,blocked=true){
   if(!identity.validPlayer(me)||!identity.validPlayer(target)||me===target)return {ok:false,error:'invalid_recipient'};
   if(blocked){const result=await call(['EVAL',BLOCK,'8',base+'blocks:'+me,prefix+'friends:'+me,prefix+'friends:'+target,prefix+'friendreq:in:'+me,prefix+'friendreq:out:'+me,prefix+'friendreq:in:'+target,prefix+'friendreq:out:'+target,base+'inbox:'+me,target,me,threadId(me,target)]);if(result!=='blocked')return {ok:false,error:result};}
   else await call(['SREM',base+'blocks:'+me,target]);return {ok:true};
 }
 return {send,inbox,thread,markRead,block,adminInbox,adminThread,adminRead};
}
module.exports={create,SEND,INBOX,BLOCK};
