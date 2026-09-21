'use strict';
// Run: node --test server/scripts/test-account-console.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const live = require('../live.cjs');
const ADMIN = '76561198000000001', STEAM = '76561198000000002';
const PLAYER = '12345678-1234-1234-1234-123456789abc';
const ACCOUNT = '87654321-4321-4321-4321-cba987654321';
const at = Date.now();
const snapshot = () => ({available:true, updated_at:at, stale:false, rows:[
  {player_id:PLAYER, account_id:ACCOUNT, persona:'Website Player', account_type:'Lights Out', account_created:at,
    linked_steam_id:'', steam_login_id:''},
]});
function service(accounts = {snapshot}) {
  const l = live.create({whoami:async()=>null, bearer:()=>'', sendJson:(res,status,body)=>{res.body=body;}, badRequest:()=>{},
    readBody:async()=>Buffer.alloc(0), prefix:'test:', accountDirectory:accounts});
  l._internals.ADMIN_IDS.add(ADMIN);
  return l;
}
test('public stats include only a complete fresh registration total', async t => {
  let current={...snapshot(),players_registered:27};
  const l=service({snapshot:()=>current});t.after(()=>l.shutdown());
  const read=async()=>{
    const res={};
    await l.route({},res,'GET','/api/live/stats');
    return res.body;
  };
  assert.equal((await read()).players_registered,27);
  current={...current,players_registered:0};assert.equal((await read()).players_registered,0);
  for(const flags of [{stale:true},{available:false}]) {
    current={...snapshot(),players_registered:27,...flags};
    assert.equal((await read()).players_registered,null);
  }
});
test('website-only accounts appear without a gameplay session; search finds account and player IDs', async t => {
  const l = service(); t.after(()=>l.shutdown());
  for (const q of ['Website Player', PLAYER, ACCOUNT]) {
    const d = await l.adminPlayers(ADMIN, {q});
    assert.equal(d.rows.length,1);
    assert.equal(d.rows[0].player_id,PLAYER);
    assert.equal(d.rows[0].first_seen,0);
    assert.equal(d.rows[0].sessions,0);
    assert.equal(d.rows[0].account_type,'Lights Out');
  }
});
test('linked logins count once, while a separate player using the same game Steam ID stays separate', async t => {
  const accounts={snapshot:()=>({...snapshot(),rows:[{...snapshot().rows[0],player_id:STEAM,account_type:'Linked',linked_steam_id:STEAM,steam_login_id:STEAM}]})};
  const l=service(accounts);t.after(()=>l.shutdown());
  l._internals.noteSeen(STEAM,'Steam name');
  l._internals.noteSeen(PLAYER,'Independent');
  l._internals.saveCareer(PLAYER,{...l._internals.careerOf(PLAYER),game_steam_id:STEAM,auth_method:'lightsout'});
  const d=await l.adminPlayers(ADMIN);
  assert.equal(d.total,2);
  assert.equal(d.rows.find(p=>p.player_id===STEAM).account_type,'Linked');
  assert.equal(d.rows.find(p=>p.player_id===PLAYER).game_steam_id,STEAM);
  assert.equal((await l.adminPlayers(ADMIN,{q:STEAM})).found,2);
  assert.equal((await l.adminPlayers(ADMIN,{player_id:STEAM})).found,1);
  assert.equal(d.accounts.linked,1);
  assert.equal((await l.adminOverview(ADMIN)).accounts.total,d.accounts.total);
});
test('missing account inventory is explicitly unavailable and does not present complete population totals', async t => {
  const l=service({snapshot:()=>({available:false,stale:true,updated_at:null,rows:[]})});t.after(()=>l.shutdown());
  l._internals.noteSeen(STEAM,'Existing');
  const d=await l.adminPlayers(ADMIN);
  assert.equal(d.rows.length,1);
  assert.equal(d.accounts.available,false);
  assert.equal(d.accounts.total,null);
});
test('analytics free-text search finds canonical UUIDs as well as game IDs',async t=>{
  const analytics=require('../analytics.cjs').create();t.after(()=>analytics.close());
  await analytics.project({matchId:'account-match',at,winner:1,score:{1:7,2:0},
    inputs:{teams:{1:[PLAYER],2:[]}},publicMatch:{id:'account-match',ended:at,players:[{player_id:PLAYER,game_steam_id:STEAM,persona:'Account player'}]},
    rows:[{steamId:PLAYER,won:true,before:{},after:{}}]});
  assert.equal((await analytics.query('matches',{size:'all',q:PLAYER})).rows.length,1);
  assert.equal((await analytics.query('matches',{size:'all',q:STEAM})).rows.length,1);
});
test('registration date metrics do not filter current population by match settings', async t=>{
  const l=service();t.after(()=>l.shutdown());
  const current=await l.adminAccountSummary(ADMIN,{from:at-1,to:at+1,size:10,map:'Other map'});
  assert.equal(current.total,1);assert.equal(current.registrations,1);
  const earlier=await l.adminAccountSummary(ADMIN,{from:at-10000,to:at-5000});
  assert.equal(earlier.total,1);assert.equal(earlier.registrations,0);
  assert.equal((await l.adminAccountSummary(STEAM)).ok,false);
});

test('failed player storage cannot masquerade as a complete account population and retries recover',async t=>{
  let down=true;
  const l=live.create({whoami:async()=>null,bearer:()=>'',sendJson:()=>{},badRequest:()=>{},readBody:async()=>Buffer.alloc(0),
    prefix:'test:',accountDirectory:{snapshot},upstashCmd:async ([op])=>{
      if(down)throw Error('private storage error');
      if(['HGETALL','ZRANGE'].includes(op))return [];
      throw Error('Unexpected '+op);
    }});
  l._internals.ADMIN_IDS.add(ADMIN);t.after(()=>l.shutdown());
  assert.equal((await l.adminAccountSummary(ADMIN)).available,false);
  down=false;
  assert.equal((await l.adminAccountSummary(ADMIN)).total,1);
});

test('account inventory handles oversized SCAN pages and duplicates without publishing partial counts',async()=>{
  const rows=Array.from({length:105},(_,n)=>({id:'12345678-1234-1234-1234-'+String(n).padStart(12,'0'),
    display_name:'Player '+n,created_at:at}));
  const prefix='isolated:',base=prefix+'accounts:user:';
  const records=new Map(rows.map(r=>[base+r.id,JSON.stringify(r)]));
  let time=at;
  const directory=require('../admin-accounts.cjs').create({prefix,autostart:false,now:()=>time,store:async command=>{
    if(command[0]==='SCAN')return ['0',command[3]===base+'*'?[...records.keys(),...records.keys()]:[]];
    if(command[0]==='MGET') {
      assert.ok(command.length<=101,'account reads must be bounded even when Redis COUNT is advisory');
      return command.slice(1).map(k=>records.get(k));
    }
    throw Error('Unexpected account command');
  }});
  await directory.refresh();assert.equal(directory.snapshot().available,false);
  await directory.refresh();assert.equal(directory.snapshot().available,false);
  await directory.refresh();assert.equal(directory.snapshot().rows.length,105);
  assert.equal(directory.snapshot().available,true);
  time+=120001;assert.equal(directory.snapshot().stale,true);
});
