'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const target=path.join(__dirname,'..','steam-ticket.cjs');
const SID='76561198000000001';
const IDENTITY='loABCDEFGHIJKLMNOPQRSTUV';
const TICKET='ab'.repeat(128);
const KEY='private-server-key';
const factory=()=>fs.existsSync(target)?require(target).create:null;

test('Steam verifies the exact Bodycam challenge and returns only the active player ID',async()=>{
  assert.equal(typeof factory(),'function','Steam ticket verifier must exist');
  let called=0;
  const verify=factory()({key:KEY,fetchImpl:async(url,options)=>{
    called++;
    assert.equal(url.origin,'https://api.steampowered.com');
    assert.equal(url.pathname,'/ISteamUserAuth/AuthenticateUserTicket/v1/');
    assert.deepEqual(Object.fromEntries(url.searchParams),{key:KEY,appid:'2406770',ticket:TICKET,identity:IDENTITY});
    assert.equal(options.redirect,'error');
    assert.equal(options.signal instanceof AbortSignal,true);
    return Response.json({response:{params:{result:'OK',steamid:SID,ownersteamid:'76561198000000099'}}});
  }});
  assert.deepEqual(await verify(TICKET,IDENTITY),{steam_id:SID});
  assert.equal(called,1);
});

test('Steam rejection never becomes an identity',async()=>{
  assert.equal(typeof factory(),'function');
  const verify=factory()({key:KEY,fetchImpl:async()=>Response.json({response:{error:{errorcode:105,errordesc:'Identity parameter did not match'}}})});
  assert.equal(await verify(TICKET,IDENTITY),null);
});

test('unconfigured or malformed requests never contact Steam',async()=>{
  assert.equal(typeof factory(),'function');
  let calls=0;const fetchImpl=async()=>{calls++;throw Error('unexpected');};
  await assert.rejects(()=>factory()({key:'',fetchImpl})(TICKET,IDENTITY),/unavailable/);
  for(const [ticket,identity] of [['ab','short'],[TICKET,'a'.repeat(42)],['gg'.repeat(128),IDENTITY],
    ['a'.repeat(5130),IDENTITY],['a'.repeat(255),IDENTITY],[TICKET,IDENTITY+'?'],[null,IDENTITY]]) {
    assert.equal(await factory()({key:KEY,fetchImpl})(ticket,identity),null);
  }
  assert.equal(calls,0);
});

test('provider failures, malformed success and oversized responses never leak credentials',async()=>{
  assert.equal(typeof factory(),'function');
  const cases=[
    async()=>{throw Error(KEY+' '+TICKET);},
    async()=>new Response(KEY,{status:503}),
    async()=>new Response('not json '+KEY),
    async()=>Response.json({response:{params:{result:'OK',steamid:123}}}),
    async()=>Response.json({response:{params:{result:'OK',steamid:'not-a-player'}}}),
    async()=>Response.json({response:{params:{result:'Unknown',steamid:SID}}}),
    async()=>new Response('x'.repeat(16385)),
    async()=>new Response('small',{headers:{'content-length':'1000000'}}),
  ];
  for(const fetchImpl of cases) {
    await assert.rejects(()=>factory()({key:KEY,fetchImpl})(TICKET,IDENTITY),error=>{
      assert.equal(error.message,'Steam identity verification is unavailable.');
      assert.equal(error.cause,undefined);
      return true;
    });
  }
});
