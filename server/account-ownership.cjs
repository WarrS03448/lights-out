'use strict';

// This confirmation is also a protocol version: a client cannot silently accept
// different ownership consequences using an older confirmation screen.
const DISCONNECT_CONFIRMATION=Object.freeze({
  id:'disconnect-steam-v1',required:true,
  message:'Your progress and account data will stay with your Lights Out account. Steam sign-in will no longer give access to this data.',
});

const VALIDATE=`
local function invalid() error('invalid ownership state') end
local function id(value)
 if type(value)~='string' then return false end
 if #value==17 and string.match(value,'^%d+$') then return true end
 local a,b,c,d,e=string.match(value,'^([a-f0-9]+)%-([a-f0-9]+)%-([a-f0-9]+)%-([a-f0-9]+)%-([a-f0-9]+)$')
 return a and #a==8 and #b==4 and #c==4 and #d==4 and #e==12
end
local function validateLedger(ledger,owner)
 if type(ledger)~='table' or not id(ledger.player_id) or not id(ledger.legacy_owner_player_id) or
  type(ledger.generation)~='number' or ledger.generation<1 or ledger.generation>=9007199254740991 or
  ledger.generation~=math.floor(ledger.generation) or type(ledger.used)~='boolean' then invalid() end
 if ledger.state=='linked' then
  if not id(ledger.account_id) or owner~=ledger.account_id then invalid() end
 elseif ledger.state=='disconnected' then
  if ledger.account_id~=cjson.null or owner then invalid() end
 else invalid() end
end
local function validateAccount(account)
 if type(account)~='table' or not id(account.id) or type(account.password_hash)~='string' or
  type(account.version)~='number' or account.version<1 or account.version>=9007199254740991 or
  account.version~=math.floor(account.version) then invalid() end
 if account.player_id~=nil and not id(account.player_id) then invalid() end
 if account.profile_used~=nil and type(account.profile_used)~='boolean' then invalid() end
end
`;

// Read the ledger/index/account as one invariant, including a one-time upgrade
// of the earlier (never publicly enabled) optional-link format. No authentication
// fallback is permitted when just one part of the ownership record is missing.
const READ=VALIDATE+`
local ledgerRaw=redis.call('GET',KEYS[1])
local owner=redis.call('GET',KEYS[2])
if not ledgerRaw and not owner then return false end
local account=nil
if owner then
 if not id(owner) then invalid() end
 local raw=redis.call('GET',ARGV[1]..owner)
 if not raw then invalid() end
 account=cjson.decode(raw)
 validateAccount(account)
 if account.id~=owner or account.steam_id~=ARGV[2] then invalid() end
end
if not ledgerRaw then
 if not account or account.player_id~=nil or account.profile_used~=nil then invalid() end
 account.player_id=ARGV[2]
 account.profile_used=true
 local ledger={player_id=ARGV[2],legacy_owner_player_id=ARGV[2],account_id=owner,
  generation=1,state='linked',used=true,updated_at=tonumber(ARGV[3])}
 local updated=cjson.encode(account)
 local encoded=cjson.encode(ledger)
 redis.call('SET',ARGV[1]..owner,updated)
 redis.call('SET',KEYS[1],encoded)
 return encoded
end
local ledger=cjson.decode(ledgerRaw)
validateLedger(ledger,owner)
if ledger.legacy_owner_player_id~=ARGV[2] then invalid() end
if account and account.player_id~=ledger.player_id then invalid() end
return ledgerRaw`;

async function readSteam({upstashCmd,prefix},steamId) {
  const base=prefix+'accounts:';
  const raw=await upstashCmd(['EVAL',READ,'2',base+'steam-identity:'+steamId,base+'steam:'+steamId,
    base+'user:',steamId,String(Date.now())],{strict:true,timeout:5000});
  return raw===null?null:JSON.parse(raw);
}

const START = `
for _,key in ipairs(KEYS) do
 local t=redis.call('TYPE',key).ok
 if t~='none' and t~='string' then return redis.error_reply('ownership key type') end
end
local raw=redis.call('GET',KEYS[1])
local parentRaw=redis.call('GET',KEYS[2])
if not raw or not parentRaw then return 0 end
local account=cjson.decode(raw)
local parent=cjson.decode(parentRaw)
local action=cjson.decode(ARGV[3])
local now=tonumber(ARGV[4])
if account.version~=tonumber(ARGV[1]) or account.password_hash~=ARGV[2] then return 0 end
if parent.account_id~=account.id or parent.version~=account.version then return 0 end
if not (parent.remember_me==true and parent.expires_at==cjson.null) and
 (type(parent.expires_at)~='number' or parent.expires_at<=now) then return 0 end
if action.account_id~=account.id or action.version~=account.version then return 0 end
redis.call('SET',KEYS[3],ARGV[3],'EX',ARGV[5])
return 1`;

// Preflight everything before writing. Lua execution is atomic but Redis does
// not roll back writes if a later command errors.
const FINISH = VALIDATE+`
for i,key in ipairs(KEYS) do
 local t=redis.call('TYPE',key).ok
 local expected=i==6 and 'set' or 'string'
 if t~='none' and t~=expected then return redis.error_reply('ownership key type') end
end
local raw=redis.call('GET',KEYS[1])
local pendingRaw=redis.call('GET',KEYS[2])
local parentRaw=redis.call('GET',KEYS[3])
if not raw or not pendingRaw or not parentRaw then return 0 end
local account=cjson.decode(raw)
validateAccount(account)
local pending=cjson.decode(pendingRaw)
local parent=cjson.decode(parentRaw)
local ledgerRaw=redis.call('GET',KEYS[4])
local ledger=ledgerRaw and cjson.decode(ledgerRaw) or nil
local owner=redis.call('GET',KEYS[5])
if ledger then
 validateLedger(ledger,owner)
 if ledger.legacy_owner_player_id~=ARGV[4] then invalid() end
elseif owner then invalid() end
if ledger and owner==account.id and (account.player_id~=ledger.player_id or account.steam_id~=ARGV[4]) then invalid() end
local now=tonumber(ARGV[1])
if pending.kind~=ARGV[2] or pending.parent_hash~=ARGV[3] or pending.account_id~=account.id or
 pending.version~=account.version or pending.expires_at<=now or pending.steam_id~=ARGV[4] then return 0 end
if parent.account_id~=account.id or parent.version~=account.version then return 0 end
if not (parent.remember_me==true and parent.expires_at==cjson.null) and
 (type(parent.expires_at)~='number' or parent.expires_at<=now) then return 0 end
local generation=ledger and ledger.generation or 0
if generation~=pending.steam_generation then return -1 end
if pending.code_hash~=ARGV[5] then
 pending.attempts=pending.attempts+1
 if pending.attempts>=5 then redis.call('DEL',KEYS[2])
 else redis.call('SET',KEYS[2],cjson.encode(pending),'KEEPTTL') end
 return 0
end
local profile=account.player_id or account.id
if pending.kind=='link-steam' then
 local proofRaw=redis.call('GET',KEYS[7])
 if not proofRaw or pending.steam_hash~=ARGV[6] then return 0 end
 local proof=cjson.decode(proofRaw)
 if proof.steam_id~=pending.steam_id or (proof.steam_generation or 0)~=generation or
  type(proof.created)~='number' or proof.created>now or proof.created<now-300000 then return 0 end
 if owner and owner~=account.id then return -1 end
 if account.steam_id and account.steam_id~=cjson.null and account.steam_id~=pending.steam_id then return -1 end
 local steamProfile=ledger and ledger.player_id or pending.steam_id
 local steamUsed=not ledger or ledger.used==true
 local accountUsed=account.profile_used==true or profile~=account.id
 if profile~=steamProfile then
  if accountUsed and steamUsed then return -2 end
  if steamUsed then profile=steamProfile end
 end
 ledger=ledger or {legacy_owner_player_id=pending.steam_id}
 ledger.player_id=profile
 ledger.account_id=account.id
 ledger.state='linked'
 ledger.used=accountUsed or steamUsed
 account.steam_id=pending.steam_id
elseif pending.kind=='disconnect-steam' then
 if pending.confirmation~='disconnect-steam-v1' or ARGV[7]~=pending.confirmation then return 0 end
 if not ledger or owner~=account.id or ledger.account_id~=account.id or account.steam_id~=pending.steam_id then return -1 end
 if ledger.player_id~=profile then return -1 end
 -- Never delete the historical ownership record. A later Steam login receives
 -- a fresh empty profile, even if account registration has been disabled.
 ledger.player_id=ARGV[8]
 ledger.account_id=cjson.null
 ledger.state='disconnected'
 ledger.used=false
 account.steam_id=cjson.null
else return 0 end
local sessions=redis.call('SMEMBERS',KEYS[6])
for _,token in ipairs(sessions) do
 if #token~=64 or not string.match(token,'^[a-f0-9]+$') then return redis.error_reply('invalid account session index') end
end
account.player_id=profile
account.version=account.version+1
account.updated_at=now
ledger.generation=generation+1
ledger.updated_at=now
local updated=cjson.encode(account)
local newLedger=cjson.encode(ledger)
local audit=cjson.encode({action=pending.kind,account_id=account.id,player_id=profile,
 steam_id=pending.steam_id,generation=ledger.generation,at=now,confirmation=pending.confirmation})
redis.call('SET',KEYS[1],updated)
redis.call('SET',KEYS[4],newLedger)
if pending.kind=='link-steam' then redis.call('SET',KEYS[5],account.id)
else redis.call('DEL',KEYS[5]) end
redis.call('SET',KEYS[8],audit)
redis.call('DEL',KEYS[2])
for _,token in ipairs(sessions) do redis.call('DEL',ARGV[9]..token) end
redis.call('DEL',KEYS[6])
return 1`;

// A temporary ticket is not profile use. Claim ownership only when the live
// service admits an operation that can create progress or other player data.
const CHECK_GAME=VALIDATE+`
local accountRaw=redis.call('GET',KEYS[1])
local childRaw=redis.call('GET',KEYS[2])
local parentRaw=redis.call('GET',KEYS[3])
if not accountRaw or childRaw~=ARGV[1] or not parentRaw then return 0 end
local account=cjson.decode(accountRaw)
local child=cjson.decode(childRaw)
local parent=cjson.decode(parentRaw)
local now=tonumber(ARGV[2])
validateAccount(account)
if account.id~=child.account_id or account.version~=child.version or (account.player_id or account.id)~=child.player_id or child.expires_at<=now then return 0 end
if parent.account_id~=account.id or parent.version~=account.version then return 0 end
if not (parent.remember_me==true and parent.expires_at==cjson.null) and
 (type(parent.expires_at)~='number' or parent.expires_at<=now) then return 0 end
`;
const MARK_GAME=CHECK_GAME+`
local ledger=nil
if account.steam_id and account.steam_id~=cjson.null then
 if account.steam_id~=ARGV[3] then return 0 end
 local raw=redis.call('GET',KEYS[4])
 local owner=redis.call('GET',KEYS[5])
 if not raw then invalid() end
 ledger=cjson.decode(raw)
 validateLedger(ledger,owner)
 if ledger.legacy_owner_player_id~=account.steam_id or ledger.account_id~=account.id or ledger.player_id~=child.player_id then invalid() end
 ledger.used=true
elseif ARGV[3]~='' then return 0 end
account.profile_used=true
local updated=cjson.encode(account)
local marked=ledger and cjson.encode(ledger) or nil
redis.call('SET',KEYS[1],updated)
if marked then redis.call('SET',KEYS[4],marked) end
return 1`;

const MARK_STEAM=VALIDATE+`
local raw=redis.call('GET',KEYS[1])
if not raw then return 0 end
local token=cjson.decode(raw)
if token.steam_id~=ARGV[1] or (token.steam_generation or 0)~=tonumber(ARGV[3]) then return 0 end
local ledgerRaw=redis.call('GET',KEYS[2])
local owner=redis.call('GET',KEYS[3])
if not ledgerRaw then
 if owner then invalid() end
 return (tonumber(ARGV[3])==0 and ARGV[2]==ARGV[1]) and 1 or 0
end
local ledger=cjson.decode(ledgerRaw)
validateLedger(ledger,owner)
if ledger.legacy_owner_player_id~=ARGV[1] or ledger.player_id~=ARGV[2] or ledger.generation~=tonumber(ARGV[3]) then return 0 end
local updated=nil
if owner then
 local accountRaw=redis.call('GET',ARGV[4]..owner)
 if not accountRaw then invalid() end
 local account=cjson.decode(accountRaw)
 validateAccount(account)
 if account.id~=owner or account.steam_id~=ARGV[1] or account.player_id~=ledger.player_id then invalid() end
 account.profile_used=true
 updated=cjson.encode(account)
end
ledger.used=true
local marked=cjson.encode(ledger)
if updated then redis.call('SET',ARGV[4]..owner,updated) end
redis.call('SET',KEYS[2],marked)
return 1`;

async function markSteamUsed({upstashCmd,prefix,authPrefix=prefix},token,identity) {
  const base=prefix+'accounts:';
  return await upstashCmd(['EVAL',MARK_STEAM,'3',authPrefix+'auth:token:'+token,
    base+'steam-identity:'+identity.steam_id,base+'steam:'+identity.steam_id,
    identity.steam_id,identity.player_id,String(identity.steam_generation||0),base+'user:'],{strict:true,timeout:5000})===1;
}
module.exports={START,FINISH,DISCONNECT_CONFIRMATION,readSteam,MARK_GAME,CHECK_GAME:CHECK_GAME+'return 1',markSteamUsed};
