'use strict';
const {validSteam} = require('./player-identity.cjs');

// Permanent evidence of Steam SIGN-IN. A game's Steam identity and a cached
// public Steam profile are not evidence that somebody signed in to Lights Out.
function create({store, prefix='hub:', authPrefix=prefix, rosterPrefix=prefix}) {
  const key=prefix+'accounts:registered-steam', ready=key+':v1-ready';
  const recorded=new Set();
  const call=args=>store(args,{strict:true,timeout:5000});
  async function add(ids) {
    const unique=[...new Set(ids)];
    if(unique.some(id=>!validSteam(id)))throw Error('Invalid registration identity');
    for(let offset=0;offset<unique.length;offset+=100) {
      const result=await call(['SADD',key,...unique.slice(offset,offset+100)]);
      if(!Number.isSafeInteger(result)||result<0)throw Error('Registration storage unavailable');
    }
  }
  async function record(id) {
    if(recorded.has(id))return false;
    await add([id]);recorded.add(id);return true;
  }
  async function scan(command, consume) {
    let cursor='0';
    do {
      const page=await call(command(cursor));
      if(!Array.isArray(page)||page.length!==2||!/^\d+$/.test(String(page[0])))throw Error('Invalid registration inventory');
      cursor=String(page[0]);await consume(page[1]);
    } while(cursor!=='0');
  }
  function pairs(raw) {
    if(Array.isArray(raw)) {
      if(raw.length%2)throw Error('Invalid registration inventory');
      return Array.from({length:raw.length/2},(_,i)=>[raw[i*2],raw[i*2+1]]);
    }
    if(raw&&typeof raw==='object')return Object.entries(raw);
    throw Error('Invalid registration inventory');
  }
  async function load(ledgers) {
    if(await call(['GET',ready])!=='1') {
      // One-time, idempotent recovery from the old persistent directory and
      // still-retained sign-in sessions. Never copy session keys or secrets.
      await scan(cursor=>['HSCAN',rosterPrefix+'roster',cursor,'COUNT','100'],async raw=>{
        const ids=[];
        for(const [id,value] of pairs(raw)) {
          const row=typeof value==='string'?JSON.parse(value):value;
          if(!row||typeof row!=='object')throw Error('Invalid registration inventory');
          if(validSteam(id)&&Number.isFinite(row.first_seen)&&row.first_seen>0&&
              (!row.auth_method||row.auth_method==='steam'))ids.push(id);
        }
        await add(ids);
      });
      const tokenPrefix=authPrefix+'auth:token:';
      await scan(cursor=>['SCAN',cursor,'MATCH',tokenPrefix+'*','COUNT','100'],async keys=>{
        if(!Array.isArray(keys)||keys.some(k=>typeof k!=='string'||!k.startsWith(tokenPrefix)))throw Error('Invalid registration inventory');
        keys=[...new Set(keys)];
        for(let offset=0;offset<keys.length;offset+=100) {
          const batch=keys.slice(offset,offset+100), rows=await call(['MGET',...batch]);
          if(!Array.isArray(rows)||rows.length!==batch.length)throw Error('Invalid registration inventory');
          const ids=[];
          for(const raw of rows) {
            if(raw===null)continue;
            const row=typeof raw==='string'?JSON.parse(raw):raw;
            if(!validSteam(row?.steam_id))throw Error('Invalid registration inventory');
            ids.push(row.steam_id);
          }
          await add(ids);
        }
      });
      // Both linked and disconnected ownership ledgers require Steam proof.
      await add([...ledgers.keys()]);
      if(await call(['SET',ready,'1'])!=='OK')throw Error('Registration storage unavailable');
    }
    const ids=new Set();
    await scan(cursor=>['SSCAN',key,cursor,'COUNT','100'],async members=>{
      if(!Array.isArray(members)||members.some(id=>!validSteam(id)))throw Error('Invalid registration inventory');
      for(const id of members)ids.add(id);
    });
    return ids;
  }
  return {record,load};
}
module.exports={create};
