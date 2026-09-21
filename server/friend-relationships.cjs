// Atomic durable relationships used by the inbox permission check.
'use strict';
const CHANGE=`-- friend-relationship-v1
for _,k in ipairs(KEYS) do local t=redis.call('TYPE',k).ok;if t~='none' and t~='set' then return redis.error_reply('friend key type') end end
local me,target,action=ARGV[1],ARGV[2],ARGV[3]
if action=='request' or action=='accept' then
 if redis.call('SISMEMBER',KEYS[7],target)==1 or redis.call('SISMEMBER',KEYS[8],me)==1 then return {'error','Messaging is blocked between these accounts.'} end
 if redis.call('SISMEMBER',KEYS[1],target)==1 and redis.call('SISMEMBER',KEYS[2],me)==1 then return {'friends'} end
 if redis.call('SCARD',KEYS[1])>=tonumber(ARGV[4]) or redis.call('SCARD',KEYS[2])>=tonumber(ARGV[4]) then return {'error','The friends list is full.'} end
 if action=='accept' or redis.call('SISMEMBER',KEYS[3],target)==1 then
  if redis.call('SISMEMBER',KEYS[3],target)~=1 or redis.call('SISMEMBER',KEYS[6],me)~=1 then return {'error','No request from them.'} end
  redis.call('SADD',KEYS[1],target);redis.call('SADD',KEYS[2],me)
  for i=3,4 do redis.call('SREM',KEYS[i],target) end;for i=5,6 do redis.call('SREM',KEYS[i],me) end
  return {'accepted'}
 end
 if redis.call('SISMEMBER',KEYS[4],target)==1 then return {'pending'} end
 if redis.call('SCARD',KEYS[4])>=tonumber(ARGV[5]) or redis.call('SCARD',KEYS[5])>=tonumber(ARGV[5]) then return {'error','Too many pending requests.'} end
 redis.call('SADD',KEYS[4],target);redis.call('SADD',KEYS[5],me);return {'sent'}
end
if action=='decline' then redis.call('SREM',KEYS[3],target);redis.call('SREM',KEYS[6],me);return {'declined'} end
if action=='cancel' then redis.call('SREM',KEYS[4],target);redis.call('SREM',KEYS[5],me);return {'cancelled'} end
if action=='remove' then redis.call('SREM',KEYS[1],target);redis.call('SREM',KEYS[2],me);return {'removed'} end
return {'error','Invalid relationship action.'}`;
module.exports={CHANGE};
