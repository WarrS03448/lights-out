'use strict';
const crypto = require('node:crypto');
const {promisify} = require('node:util');
const scrypt = promisify(crypto.scrypt);
const COST = {N:131072, r:8, p:1, maxmem:160*1024*1024};
// One expensive hash at a time, with bounded waiting, keeps authentication from
// exhausting the process that also serves live matches.
let hashing = false;
const waiting = [];
class AccountError extends Error {
  constructor(status, code, message) { super(message); this.status=status; this.code=code; }
}
function fail(status, code, message) { throw new AccountError(status,code,message); }
async function derive(password, salt) {
  if(hashing) {
    if(waiting.length >= 8) fail(503,'account_busy','Account service is busy. Try again shortly.');
    await new Promise(resolve=>waiting.push(resolve));
  }
  hashing=true;
  try { return await scrypt(password,salt,64,COST); }
  finally { const next=waiting.shift(); if(next)next();else hashing=false; }
}
function email(value) {
  if(typeof value!=='string') fail(400,'invalid_email','Enter a valid email address.');
  const normalized=value.trim().toLowerCase();
  // Deliberately do not collapse dots/plus suffixes: those are provider-specific.
  if(normalized.length>254 || !/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/.test(normalized))
    fail(400,'invalid_email','Enter a valid email address.');
  return normalized;
}
function password(value) {
  if(typeof value!=='string' || [...value].length<6 || [...value].length>128 || Buffer.byteLength(value,'utf8')>512)
    fail(400,'invalid_password','Use a password between 6 and 128 characters.');
  return value;
}
function displayName(value) {
  if(typeof value!=='string') fail(400,'invalid_display_name','Enter a display name.');
  const name=value.trim();
  if([...name].length<1 || [...name].length>40 || /[\x00-\x1f\x7f-\x9f<>]/.test(name))
    fail(400,'invalid_display_name','Use a display name between 1 and 40 characters.');
  return name;
}
async function hashPassword(value) {
  password(value);
  const salt=crypto.randomBytes(16);
  return ['scrypt',131072,8,1,salt.toString('hex'),(await derive(value,salt)).toString('hex')].join('$');
}
async function checkPassword(value, encoded) {
  // Unknown users pay the same hashing cost. Invalid input remains bounded.
  if(typeof value!=='string' || Buffer.byteLength(value,'utf8')>512) return false;
  const match=/^scrypt\$131072\$8\$1\$([a-f0-9]{32})\$([a-f0-9]{128})$/.exec(String(encoded||''));
  const result=await derive(value,Buffer.from(match ? match[1] : '0'.repeat(32),'hex'));
  return crypto.timingSafeEqual(result,Buffer.from(match ? match[2] : '0'.repeat(128),'hex')) && Boolean(match);
}
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const randomToken = () => crypto.randomBytes(32).toString('base64url');
const loginCode = () => String(crypto.randomInt(0,1000000)).padStart(6,'0');
const codeDigest = (secret,challenge,code) => crypto.createHmac('sha256',secret).update('login:'+challenge+':'+code).digest('hex');
const isToken = value => typeof value==='string' && /^[A-Za-z0-9_-]{43}$/.test(value);
const isSession = value => typeof value==='string' && /^lo_[A-Za-z0-9_-]{43}$/.test(value);
module.exports={AccountError,fail,email,password,displayName,hashPassword,checkPassword,digest,randomToken,loginCode,codeDigest,isToken,isSession};
