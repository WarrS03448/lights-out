'use strict';

// A game proof can create only a child gameplay credential. It cannot touch
// login indexes, Steam ownership, another player profile, or account passwords.
const FINISH=`
for _,key in ipairs(KEYS) do
 local t=redis.call('TYPE',key).ok
 if t~='none' and t~='string' then return redis.error_reply('game credential key type') end
end
local accountRaw=redis.call('GET',KEYS[1])
local pendingRaw=redis.call('GET',KEYS[2])
local parentRaw=redis.call('GET',KEYS[3])
if not accountRaw or not pendingRaw or not parentRaw then return 0 end
local account=cjson.decode(accountRaw)
local pending=cjson.decode(pendingRaw)
local parent=cjson.decode(parentRaw)
local child=cjson.decode(ARGV[4])
local now=tonumber(ARGV[1])
if pending.kind~='game' or pending.account_id~=account.id or pending.version~=account.version or
 pending.identity~=ARGV[2] or pending.parent_hash~=ARGV[3] or pending.expires_at<=now then return 0 end
if parent.account_id~=account.id or parent.version~=account.version then return 0 end
local remembered=parent.remember_me==true and parent.expires_at==cjson.null
if not remembered and (type(parent.expires_at)~='number' or parent.expires_at<=now) then return 0 end
if child.account_id~=account.id or child.version~=account.version or child.parent_hash~=pending.parent_hash or
 child.player_id~=(account.player_id or account.id) or child.expires_at<=now or child.expires_at>now+43200000 then return 0 end
if not remembered and child.expires_at>parent.expires_at then return 0 end
if type(child.game_steam_id)~='string' or #child.game_steam_id~=17 or not string.match(child.game_steam_id,'^%d+$') then return 0 end
redis.call('SET',KEYS[4],ARGV[4],'PX',math.max(1,math.floor(child.expires_at-now)))
redis.call('DEL',KEYS[2])
return 1`;

module.exports={FINISH};
