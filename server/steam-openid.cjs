'use strict';
const crypto = require('node:crypto');
const STEAM_OPENID = 'https://steamcommunity.com/openid/login';
const CLAIMED_ID_RE = /^https:\/\/steamcommunity\.com\/openid\/id\/(\d{17})$/;
const MAX_AGE_MS = 5 * 60 * 1000;
const FUTURE_SKEW_MS = 60 * 1000;
const NONCE_TTL_SECONDS = 6 * 60;

// Local verification complements (never replaces) Steam's check_authentication.
function validate(params, returnTo, now=Date.now()) {
  if(!returnTo)return null;
  const seen=new Set();
  for(const [name] of params) {
    if(seen.has(name))return null;
    seen.add(name);
  }
  const get=name=>params.get('openid.'+name);
  if(get('ns')!=='http://specs.openid.net/auth/2.0'||get('mode')!=='id_res'||
     get('op_endpoint')!==STEAM_OPENID||get('return_to')!==returnTo)return null;
  // The caller's code/state must also be the one in the signed callback.
  const expected=new URL(returnTo);
  for(const [name,value] of expected.searchParams)if(params.get(name)!==value)return null;
  const match=CLAIMED_ID_RE.exec(get('claimed_id')||'');
  if(!match||get('identity')!==get('claimed_id')||!get('sig')||!get('assoc_handle'))return null;
  const signed=(get('signed')||'').split(',');
  if(new Set(signed).size!==signed.length)return null;
  for(const field of ['op_endpoint','return_to','response_nonce','assoc_handle','claimed_id','identity']) {
    if(!signed.includes(field))return null;
  }
  const nonce=get('response_nonce')||'';
  if(nonce.length>255||!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z[\x21-\x7e]*$/.test(nonce))return null;
  const timestamp=Date.parse(nonce.slice(0,20));
  if(!Number.isFinite(timestamp)||new Date(timestamp).toISOString().slice(0,19)+'Z'!==nonce.slice(0,20)||
     now-timestamp>MAX_AGE_MS||timestamp-now>FUTURE_SKEW_MS)return null;
  return {steamId:match[1],nonce:crypto.createHash('sha256').update(nonce).digest('hex')};
}

const memory=new Map();
const CLAIM = `
local raw=redis.call('GET',KEYS[1])
if not raw then return 0 end
local record=cjson.decode(raw)
if record.status~='pending' or record.return_to~=ARGV[1] then return 0 end
if redis.call('EXISTS',KEYS[2])~=0 then return 0 end
local ttl=redis.call('TTL',KEYS[1])
if ttl<=0 then return 0 end
redis.call('SET',KEYS[2],'1','EX',ARGV[2])
record.status='verified'
redis.call('SET',KEYS[1],cjson.encode(record),'EX',ttl)
return 1`;
const TAKE_READY = `
local raw=redis.call('GET',KEYS[1])
if not raw then return nil end
if cjson.decode(raw).status~='ready' then return nil end
redis.call('DEL',KEYS[1])
return raw`;

function makeStore(upstashCmd, prefix) {
  // Only an explicitly unconfigured development server can use memory. A Redis
  // miss, NX conflict or outage must never create a second authorization store.
  const allowMemory=process.env.NODE_ENV!=='production' &&
    !process.env.UPSTASH_REDIS_REST_URL && !process.env.UPSTASH_REDIS_REST_TOKEN;
  const command=args=>upstashCmd ? upstashCmd(args,{strict:true,timeout:10000}) : Promise.resolve(null);
  const key=name=>prefix+name;
  function local(k) {
    const hit=allowMemory&&memory.get(k);
    if(hit&&hit.expires>Date.now())return hit;
    memory.delete(k);return null;
  }
  function parse(raw) {return raw ? (typeof raw==='string'?JSON.parse(raw):raw) : null;}
  return {
    async set(name,value,ttl) {
      const raw=JSON.stringify(value),k=key(name);
      const result=await command(['SET',k,raw,'EX',String(ttl)]);
      if(result==='OK'||result===true){memory.delete(k);return;}
      if(result===null&&allowMemory){memory.set(k,{raw,expires:Date.now()+ttl*1000});return;}
      throw Error('Sign-in storage unavailable');
    },
    async get(name) {
      const k=key(name),hit=local(k);
      if(hit)return parse(hit.raw);
      return parse(await command(['GET',k]));
    },
    async del(name) {await command(['DEL',key(name)]);memory.delete(key(name));},
    async claim(name,returnTo,nonce) {
      const k=key(name),nk=key('nonce:'+nonce),hit=local(k);
      if(hit) {
        const record=parse(hit.raw);
        if(record.status!=='pending'||record.return_to!==returnTo||local(nk))return false;
        memory.set(nk,{raw:'1',expires:Date.now()+NONCE_TTL_SECONDS*1000});
        hit.raw=JSON.stringify({...record,status:'verified'});return true;
      }
      return await command(['EVAL',CLAIM,'2',k,nk,returnTo,String(NONCE_TTL_SECONDS)])===1;
    },
    async takeReady(name) {
      const k=key(name),hit=local(k);
      if(hit) {
        const record=parse(hit.raw);
        if(record.status!=='ready')return null;
        memory.delete(k);return record;
      }
      return parse(await command(['EVAL',TAKE_READY,'1',k]));
    },
  };
}
module.exports={STEAM_OPENID,CLAIMED_ID_RE,validate,makeStore};
