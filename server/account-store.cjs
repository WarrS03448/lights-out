'use strict';
const {createHmac} = require('node:crypto');
const {digest} = require('./account-security.cjs');
const ownership = require('./account-ownership.cjs');
const game = require('./account-game.cjs');

// Scripts preflight key types and decode records before writing: Redis Lua is
// atomic, but does NOT roll back a script that errors after its first mutation.
const TYPES = `
for _,key in ipairs(KEYS) do
 local t=redis.call('TYPE',key).ok
 if t~='none' and t~='string' then return redis.error_reply('account key type') end
end
`;
const WITH_SESSION_SET = position => `
for i,key in ipairs(KEYS) do
 local t=redis.call('TYPE',key).ok
 local expected=i==${position} and 'set' or 'string'
 if t~='none' and t~=expected then return redis.error_reply('account key type') end
end
`;
const CREATE = TYPES + `
local pending=redis.call('GET',KEYS[1])
if not pending or pending~=ARGV[1] then return 0 end
if redis.call('EXISTS',KEYS[2])==1 or redis.call('EXISTS',KEYS[3])==1 then return 0 end
redis.call('SET',KEYS[2],ARGV[2])
redis.call('SET',KEYS[3],ARGV[3])
redis.call('DEL',KEYS[1])
return 1`;
const LOGIN_START = TYPES + `
local raw=redis.call('GET',KEYS[1])
if not raw then return 0 end
local account=cjson.decode(raw)
if account.password_hash~=ARGV[1] or account.version~=tonumber(ARGV[2]) then return 0 end
redis.call('SET',KEYS[2],ARGV[3],'EX',ARGV[4])
return 1`;
const LOGIN_FINISH = WITH_SESSION_SET(4) + `
local raw=redis.call('GET',KEYS[1])
local user=redis.call('GET',KEYS[2])
if not raw or not user then return 0 end
local challenge=cjson.decode(raw)
local account=cjson.decode(user)
if challenge.account_id~=account.id or challenge.version~=account.version or challenge.expires_at<=tonumber(ARGV[4]) then
 redis.call('DEL',KEYS[1]);return 0
end
if challenge.code_hash~=ARGV[1] then
 challenge.attempts=challenge.attempts+1
 if challenge.attempts>=5 then redis.call('DEL',KEYS[1])
 else redis.call('SET',KEYS[1],cjson.encode(challenge),'KEEPTTL') end
 return 0
end
local session=cjson.decode(ARGV[2])
if session.account_id~=account.id or session.version~=account.version or session.remember_me~=challenge.remember_me then return 0 end
if challenge.remember_me and redis.call('SCARD',KEYS[4])>=20 then return -2 end
if challenge.remember_me then
 redis.call('SET',KEYS[3],ARGV[2])
 redis.call('SADD',KEYS[4],ARGV[5])
else redis.call('SET',KEYS[3],ARGV[2],'EX',ARGV[3]) end
redis.call('DEL',KEYS[1])
return 1`;
const PASSWORD = WITH_SESSION_SET(2) + `
local raw=redis.call('GET',KEYS[1])
if not raw then return 0 end
local account=cjson.decode(raw)
if account.version~=tonumber(ARGV[1]) or account.password_hash~=ARGV[2] then return 0 end
if #KEYS==3 and redis.call('GET',KEYS[3])~=ARGV[5] then return 0 end
local sessions=redis.call('SMEMBERS',KEYS[2])
for _,token in ipairs(sessions) do
 if #token~=64 or not string.match(token,'^[a-f0-9]+$') then return redis.error_reply('invalid account session index') end
end
account.password_hash=ARGV[3]
account.version=account.version+1
account.updated_at=tonumber(ARGV[4])
local updated=cjson.encode(account)
redis.call('SET',KEYS[1],updated)
for _,token in ipairs(sessions) do redis.call('DEL',ARGV[6]..token) end
redis.call('DEL',KEYS[2])
if #KEYS==3 then redis.call('DEL',KEYS[3]) end
return 1`;
const LOGOUT = WITH_SESSION_SET(2) + `
redis.call('DEL',KEYS[1])
redis.call('SREM',KEYS[2],ARGV[1])
return 1`;
const RATE = TYPES + `
local limits=cjson.decode(ARGV[1])
for i,key in ipairs(KEYS) do
 local count=tonumber(redis.call('GET',key) or '0')
 if not count then return redis.error_reply('account rate type') end
 if count>=limits[i][1] then return math.max(1,redis.call('TTL',key)) end
end
for i,key in ipairs(KEYS) do
 local count=redis.call('INCR',key)
 if count==1 then redis.call('EXPIRE',key,limits[i][2]) end
end
return 0`;
const CONFIG = TYPES + `
local current=redis.call('GET',KEYS[1])
if current and current~=ARGV[1] then return 0 end
if not current then redis.call('SET',KEYS[1],ARGV[1]) end
return 1`;

function create({upstashCmd,prefix,authPrefix=prefix,secret}) {
  const base=prefix+'accounts:';
  const key=(kind,id)=>base+kind+':'+id;
  const privateId=value=>createHmac('sha256',secret).update(value).digest('hex');
  let checked;
  async function call(args) {
    // A different HMAC key must never silently create a second email namespace.
    if(!checked) {
      checked=upstashCmd(['EVAL',CONFIG,'1',key('config','secret-v1'),privateId('lights-out-accounts-v1')],
        {strict:true,timeout:5000}).then(result=>{
          if(result!==1)throw Error('Account configuration does not match durable storage');
        }).catch(error=>{checked=null;throw error;});
    }
    await checked;
    return upstashCmd(args,{strict:true,timeout:5000});
  }
  async function get(kind,id) {
    const value=await call(['GET',key(kind,id)]);
    return value===null ? null : JSON.parse(value);
  }
  async function set(kind,id,value,ttl) {
    const result=await call(['SET',key(kind,id),JSON.stringify(value),'EX',String(ttl)]);
    if(result!=='OK')throw Error('Account storage unavailable');
  }
  async function evalScript(script,keys,args) {
    const result=await call(['EVAL',script,String(keys.length),...keys,...args.map(String)]);
    if(!Number.isInteger(result))throw Error('Invalid account storage response');
    return result;
  }
  async function accountBy(index,value) {
    const id=await call(['GET',key(index,value)]);
    // Keep the same lookup work for present and absent emails.
    return get('user',id||'missing');
  }
  return {get,set,
    byEmail:email=>accountBy('email',privateId(email)),
    bySteam:steamId=>accountBy('steam',steamId),
    async remove(kind,id) {const result=await call(['DEL',key(kind,id)]);if(!Number.isInteger(result))throw Error('Account storage unavailable');},
    createAccount:(code,pending,account)=>evalScript(CREATE,
      [key('verify',digest(code)),key('user',account.id),key('email',privateId(account.email))],
      [JSON.stringify(pending),JSON.stringify(account),account.id]),
    issueLoginChallenge:(account,token,challenge,ttl)=>evalScript(LOGIN_START,
      [key('user',account.id),key('login',digest(token))],
      [account.password_hash,account.version,JSON.stringify(challenge),ttl]),
    completeLogin:(challenge,account,codeHash,token,session,ttl)=>evalScript(LOGIN_FINISH,
      [key('login',digest(challenge)),key('user',account.id),key('session',digest(token)),key('sessions',account.id)],
      [codeHash,JSON.stringify(session),ttl,Date.now(),digest(token)]),
    revokeSession:(token,accountId)=>evalScript(LOGOUT,
      [key('session',digest(token)),key('sessions',accountId)],[digest(token)]),
    changePassword:(account,hash,at,reset)=>evalScript(PASSWORD,
      [key('user',account.id),key('sessions',account.id),...(reset?[key('reset',digest(reset.token))]:[])],
      [account.version,account.password_hash,hash,at,reset?JSON.stringify(reset.record):'',key('session','')]),
    issueOwnership:(account,parentToken,challenge,state,ttl)=>evalScript(ownership.START,
      [key('user',account.id),key('session',digest(parentToken)),key('ownership',digest(challenge))],
      [account.version,account.password_hash,JSON.stringify(state),Date.now(),ttl]),
    issueGame:(account,parentToken,challenge,state,ttl)=>evalScript(ownership.START,
      [key('user',account.id),key('session',digest(parentToken)),key('game-challenge',digest(challenge))],
      [account.version,account.password_hash,JSON.stringify(state),Date.now(),ttl]),
    finishGame:(account,parentToken,challenge,identity,token,child)=>evalScript(game.FINISH,
      [key('user',account.id),key('game-challenge',digest(challenge)),key('session',digest(parentToken)),key('game-session',digest(token))],
      [Date.now(),identity,digest(parentToken),JSON.stringify(child)]),
    finishOwnership:(account,parentToken,input,pending,codeHash,newSteamProfile)=>evalScript(ownership.FINISH,
      [key('user',account.id),key('ownership',digest(input.challenge)),key('session',digest(parentToken)),
        key('steam-identity',pending.steam_id),key('steam',pending.steam_id),key('sessions',account.id),
        authPrefix+'auth:token:'+(input.steam_token||''),key('ownership-audit',digest(input.challenge))],
      [Date.now(),input.kind,digest(parentToken),pending.steam_id,codeHash,
        digest(input.steam_token||''),input.confirmation||'',newSteamProfile,key('session','')]),
    rate:entries=>evalScript(RATE,entries.map(e=>key('rate',privateId(e[0]))),[JSON.stringify(entries.map(e=>e.slice(1)))]),
  };
}
module.exports={create};
