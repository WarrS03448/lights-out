// Save a ban and its active-match void decision in the same Redis transaction.
'use strict';
const PREPARE_VOID=`
local function prepareVoid(liveKey, receiptKey, at)
 if not liveKey then return nil end
 if redis.call('GET',receiptKey) then return nil end
 local raw=redis.call('GET',liveKey);if not raw then return nil end
 local match=cjson.decode(raw)
 if match.state~='live' or type(match.collecting)=='table' or type(match.finished)=='table' or type(match.final_snapshot)=='string' or type(match.void_pending)=='number' then return nil end
 match.void_pending=at;match.void_reason='ban';match.final_snapshot='{"voided":true}'
 return cjson.encode(match)
end
`;
const SET_BAN=`-- account-ban-change-v1
${PREPARE_VOID}
for _,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;if t~='none' and t~='string' then return redis.error_reply('ban key type') end end
local old=redis.call('GET',KEYS[2]);local action=cjson.decode(ARGV[1])
if old and cjson.decode(old).at>=action.at then return 'superseded' end
local pending=nil
if ARGV[2]~='' then cjson.decode(ARGV[2]);pending=prepareVoid(KEYS[3],KEYS[4],action.at) end
if ARGV[2]=='' then redis.call('DEL',KEYS[1]) else redis.call('SET',KEYS[1],ARGV[2]) end
if pending then redis.call('SET',KEYS[3],pending,'KEEPTTL') end
redis.call('SET',KEYS[2],ARGV[1]);return 'saved'`;
module.exports={PREPARE_VOID,SET_BAN};
