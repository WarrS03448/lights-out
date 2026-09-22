'use strict';
// Private deployment boundary. Production defaults remain unchanged when disabled.
const crypto = require('node:crypto');
function config(env = process.env) {
  if (env.HUB_RECORDING !== '1') return null;
  const mode = env.HUB_RECORDING_MODE || 'recording';
  if (!['recording', 'host-test', 'account-test'].includes(mode)) throw Error('Unknown private client mode');
  const count = mode === 'recording' ? 2 : 1;
  if (!/^recording:[a-z0-9-]+:$/.test(env.HUB_STORE_PREFIX || '')) throw Error('Recording storage prefix must be recording:<id>:');
  if (mode === 'host-test' && !/^recording:hosttest[a-z0-9-]*:$/.test(env.HUB_STORE_PREFIX)) throw Error('Host test requires its own storage namespace');
  if (mode === 'account-test' && !/^recording:accounttest[a-z0-9-]*:$/.test(env.HUB_STORE_PREFIX)) throw Error('Account test requires its own storage namespace');
  const ids = String(env.COMP_PRIVATE_STEAM_IDS || '').split(',').map(s=>s.trim());
  if (ids.length !== count || new Set(ids).size !== count || ids.some(id=>!/^7656119\d{10}$/.test(id))) throw Error(`Private ${mode} requires exactly ${count} Steam IDs`);
  const key = env.HUB_RECORDING_DOWNLOAD_KEY || '';
  if (!/^[a-f0-9]{48,128}$/.test(key)) throw Error('Recording download key must have at least 192 bits');
  const origin = env.HUB_RECORDING_ORIGIN || '';
  if (!/^https:\/\/[a-z0-9-]+\.up\.railway\.app$/.test(origin) || origin === 'https://lightsout.up.railway.app') throw Error('Unsafe recording origin');
  const allowed = id => ids.includes(String(id));
  const prefix = `/private/${key}`;
  function publicPath(path) {
    const parts = /^\/private\/([a-f0-9]+)(\/.*)$/.exec(path);
    if (!parts || parts[1].length !== key.length || !crypto.timingSafeEqual(Buffer.from(parts[1]),Buffer.from(key))) return null;
    return parts[2];
  }
  function catalogue(raw) {
    const data = JSON.parse(raw);
    function rewrite(value) {
      if (typeof value === 'string' && /^https?:\/\//.test(value)) {
        const url = new URL(value);
        if (['lightsout.up.railway.app','play.lightsoutranked.com','lightsoutranked.com','www.lightsoutranked.com',new URL(origin).host].includes(url.host)) {
          const path = url.pathname.replace(/^\/private\/[a-f0-9]+/, '');
          return origin + prefix + path;
        }
      }
      if (Array.isArray(value)) return value.map(rewrite);
      if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,rewrite(v)]));
      return value;
    }
    return JSON.stringify(rewrite(data));
  }
  const email=String(env.HUB_PRIVATE_ACCOUNT_EMAIL||'');
  if(mode==='account-test' && (env.HUB_ACCOUNT_STORE_PREFIX!=='hub:' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || email!==email.toLowerCase() ||
    env.HUB_ACCOUNTS_ENABLED!=='1' || env.HUB_ACCOUNT_GAMEPLAY_ENABLED!=='1' || env.HUB_ACCOUNT_OWNERSHIP_ENABLED!=='0' ||
    env.HUB_ACCOUNT_ORIGIN!==origin || String(env.HUB_ACCOUNT_SECRET||'').length<32)) throw Error('Invalid lasting account test configuration');
  return { allowed, publicPath, catalogue, origin, mode, count, allowEmail: value=>value===email };
}
function validateDeployment(env=process.env) {
  const recording=config(env);
  if(!recording) throw Error('HUB_RECORDING=1 is required for the private server');
  for(const [key,value] of Object.entries({NODE_ENV:'production',COMP_MATCH_SIZE:String(recording.count),COMP_MAX_PARTY:'1',COMP_VERSION_GATE:'strict'})) {
    if(env[key]!==value) throw Error(`Recording requires ${key}=${value}`);
  }
  if(!/^https:\/\//.test(env.UPSTASH_REDIS_REST_URL||'') || !env.UPSTASH_REDIS_REST_TOKEN) throw Error('Recording requires persistent Upstash storage');
  if(env.HUB_TEST_TOKENS || env.COMP_NETWORK_TEST_BYPASS) throw Error('Test authentication/network bypass is forbidden on the recording server');
  if(recording.mode!=='recording' && env.COMP_NO_SHOW_PENALTIES_PAUSED!=='1') throw Error('Private solo tests require paused no-show penalties');
  if(recording.mode==='account-test'&&(!require('./account-mail.cjs').fromEnvironment(env)||!env.STEAM_WEB_API_KEY))
    throw Error('Account test requires email delivery and game identity verification');
  return recording;
}
function assertStoreCommand(args,prefix) {
  if(!Array.isArray(args) || !/^recording:[a-z0-9-]+:$/.test(prefix)) throw Error('Invalid recording storage command');
  const op=String(args[0]||'').toUpperCase();
  let keys;
  if(op==='EVAL' || op==='EVALSHA') {
    const count=Number(args[2]);
    if(!Number.isInteger(count)||count<1||args.length<3+count) throw Error('Recording scripts must declare namespaced keys');
    keys=args.slice(3,3+count);
  } else if(op==='SCAN') {
    const at=args.indexOf('MATCH');
    if(at<0) throw Error('Recording scans require a namespaced match');
    keys=[args[at+1]];
  } else if(['DEL','UNLINK','MGET','EXISTS'].includes(op)) {
    keys=args.slice(1);
  } else if(['GET','SET','EXPIRE','TTL','INCR','INCRBY','HGET','HGETALL','HSET','HDEL','HINCRBY',
              'LPUSH','LRANGE','LSET','LTRIM','LREM','LLEN','SADD','SCARD','SMEMBERS','SREM','SSCAN',
              'ZADD','ZSCORE','ZCARD','ZRANGE','ZREM','ZREMRANGEBYSCORE','ZREVRANGE','ZREVRANGEBYSCORE','ZREVRANK'].includes(op)) {
    keys=[args[1]];
  } else throw Error('Unsupported recording storage command');
  if(!keys.length || keys.some(key=>typeof key!=='string'||!key.startsWith(prefix))) throw Error('Recording storage command outside its namespace');
}
function assertAccountCommand(args,prefix) {
  // Separate runner: it can touch canonical credentials, never public game data.
  if(prefix!=='hub:accounts:') throw Error('Invalid lasting account namespace');
  const cloned=[...args];
  const op=String(args[0]).toUpperCase();
  const n=op==='EVAL'?Number(args[2]):1;
  if(!['GET','SET','DEL','EVAL'].includes(op)||!Number.isInteger(n)||n<1) throw Error('Invalid account command');
  const begin=op==='EVAL'?3:1,end=op==='DEL'?args.length:begin+n;
  for(let i=begin;i<end;i++) {
    if(typeof args[i]!=='string'||!args[i].startsWith(prefix))throw Error('Account command outside namespace');
    cloned[i]='recording:check:'+args[i].slice(prefix.length);
  }
  assertStoreCommand(cloned,'recording:check:');
}
module.exports = { config, validateDeployment, assertStoreCommand, assertAccountCommand };
