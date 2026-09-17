// Lossless, immutable combat evidence blobs. Upload before publishing a referencing receipt.
const crypto = require('node:crypto');
const zlib = require('node:zlib');
const {promisify} = require('node:util');
const gzip = promisify(zlib.gzip), gunzip = promisify(zlib.gunzip);
const CHUNK = 512 * 1024, INLINE = 256 * 1024, MAX_RAW = 256 * 1024 * 1024;
const digest = data => crypto.createHash('sha256').update(data).digest('hex');
const keyOf = (owner,hash,i) => `${owner}:combat:v1:${hash}:${i}`;

async function encode(store,state,owner,ttl) {
  if (!state || typeof state !== 'object') return state;
  if(state.__combat_blob)state=await decode(store,state,owner);
  // Raw diagnostics may contain control characters whose JSON escaping expands sixfold.
  // Base64 these strings before serialization, retaining the exact original on hydration.
  const raw64=row=>typeof row?.raw==='string'?{...row,raw:Buffer.from(row.raw,'utf8').toString('base64')}:row;
  const encoded={...state};
  if(Array.isArray(state.rejected))encoded.rejected=state.rejected.map(raw64);
  if(state.overflow)encoded.overflow=raw64(state.overflow);
  const raw=Buffer.from(JSON.stringify(encoded));
  if(raw.length<=INLINE)return state;
  if(raw.length>MAX_RAW)throw Error('Combat evidence exceeds supported size');
  const data=await gzip(raw),hash=digest(data),count=Math.ceil(data.length/CHUNK);
  for(let start=0;start<count;start+=4) {
    await Promise.all(Array.from({length:Math.min(4,count-start)},async(_,n)=>{
      const i=start+n,command=['SET',keyOf(owner,hash,i),data.subarray(i*CHUNK,(i+1)*CHUNK).toString('base64')];
      if(ttl)command.push('EX',String(ttl));
      if(await store(command,{strict:true})!=='OK')throw Error('Combat evidence chunk not saved');
    }));
  }
  return {__combat_blob:1,codec:'gzip-json-raw64',sha256:hash,bytes:raw.length,compressed_bytes:data.length,chunks:count};
}

async function decode(store,state,owner) {
  if(!state || !state.__combat_blob)return state;
  if(state.__combat_blob!==1 || !['gzip-json','gzip-json-raw64'].includes(state.codec) || !/^[a-f0-9]{64}$/.test(state.sha256)
    || !Number.isSafeInteger(state.bytes) || state.bytes<1 || state.bytes>MAX_RAW
    || !Number.isSafeInteger(state.compressed_bytes) || state.compressed_bytes<1 || state.compressed_bytes>MAX_RAW+65536
    || !Number.isSafeInteger(state.chunks) || state.chunks!==Math.ceil(state.compressed_bytes/CHUNK)) {
    throw Error('Invalid combat evidence manifest');
  }
  const chunks=[];
  for(let i=0;i<state.chunks;i++) {
    const raw=await store(['GET',keyOf(owner,state.sha256,i)],{strict:true});
    if(typeof raw!=='string' || raw.length>Math.ceil(CHUNK/3)*4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(raw))throw Error('Missing combat evidence chunk');
    const chunk=Buffer.from(raw,'base64');
    if(chunk.length!==Math.min(CHUNK,state.compressed_bytes-i*CHUNK))throw Error('Truncated combat evidence chunk');
    chunks.push(chunk);
  }
  const compressed=Buffer.concat(chunks);
  if(digest(compressed)!==state.sha256)throw Error('Corrupt combat evidence');
  const raw=await gunzip(compressed,{maxOutputLength:MAX_RAW});
  if(raw.length!==state.bytes)throw Error('Combat evidence size mismatch');
  const out=JSON.parse(raw.toString('utf8'));
  if(state.codec==='gzip-json-raw64') {
    const restore=row=>typeof row?.raw==='string'?{...row,raw:Buffer.from(row.raw,'base64').toString('utf8')}:row;
    if(Array.isArray(out.rejected))out.rejected=out.rejected.map(restore);
    if(out.overflow)out.overflow=restore(out.overflow);
  }
  return out;
}

async function transform(store,document,owner,fn,ttl) {
  if(!document || typeof document!=='object')return document;
  const out={...document};
  if(document.combatState)out.combatState=await fn(store,document.combatState,owner,ttl);
  if(document.inputs?.combatState)out.inputs={...document.inputs,combatState:await fn(store,document.inputs.combatState,owner,ttl)};
  return out;
}
module.exports={pack:(store,document,owner,{ttl}={})=>transform(store,document,owner,encode,ttl),
  unpack:(store,document,owner)=>transform(store,document,owner,decode),CHUNK};
