'use strict';
const identity = require('./player-identity.cjs');

// Read-only, bounded background inventory. Page requests never scan account keys.
// Keep only an allowlist in memory; credentials and email never reach the console.
function create({store, prefix = 'hub:', now = Date.now, autostart = true} = {}) {
  const base = prefix + 'accounts:';
  let current = {available:false, stale:false, updated_at:null, rows:[], reassigned:[]};
  let cycle = null, busy = null, timer = null, stopped = false;
  const call = args => store(args, {strict:true, timeout:5000});
  function startCycle() { return {kind:0, cursor:'0', keys:[], scanned:false, accounts:new Map(), ledgers:new Map()}; }
  async function step() {
    if (!store || stopped) return;
    if (busy) return busy;
    busy = (async () => {
      cycle ||= startCycle();
      const kind = cycle.kind === 0 ? 'user:' : 'steam-identity:';
      if (!cycle.keys.length && (!cycle.scanned || cycle.cursor !== '0')) {
        const result = await call(['SCAN',cycle.cursor,'MATCH',base+kind+'*','COUNT','100']);
        if (!Array.isArray(result) || !/^\d+$/.test(String(result[0])) || !Array.isArray(result[1])) throw Error('Invalid inventory');
        cycle.cursor = String(result[0]); cycle.scanned = true;
        cycle.keys = [...new Set(result[1])];
        if (cycle.keys.some(k => typeof k !== 'string' || !k.startsWith(base+kind))) throw Error('Invalid inventory');
      }
      const keys = cycle.keys.slice(0,100);
      if (keys.length) {
        const records = await call(['MGET',...keys]);
        if (!Array.isArray(records) || records.length !== keys.length) throw Error('Invalid inventory');
        records.forEach((raw,i) => {
          if (raw === null) return; // Record removed between SCAN and MGET.
          const r = typeof raw === 'string' ? JSON.parse(raw) : raw;
          const id = keys[i].slice((base+kind).length);
          if (cycle.kind === 0) {
            const player = r.player_id || (r.steam_id || r.id);
            if (!identity.validPlayer(id) || r.id !== id || !identity.validPlayer(player) ||
                (r.steam_id && !identity.validSteam(r.steam_id)) || !Number.isSafeInteger(r.created_at)) throw Error('Invalid inventory');
            cycle.accounts.set(id, {player_id:player, account_id:id, persona:String(r.display_name||'').slice(0,64),
              account_created:r.created_at, linked_steam_id:r.steam_id||'', steam_login_id:r.steam_id||'',
              account_type:r.steam_id?'Linked':'Lights Out'});
          } else {
            if (!identity.validSteam(id) || !identity.validPlayer(r.player_id) ||
                !['linked','disconnected'].includes(r.state)) throw Error('Invalid inventory');
            cycle.ledgers.set(id, {player_id:r.player_id, account_id:r.account_id, state:r.state});
          }
        });
        cycle.keys.splice(0,keys.length);
      }
      if (!cycle.keys.length && cycle.cursor === '0') {
        if (cycle.kind === 0) { cycle.kind = 1; cycle.scanned = false; return; }
        const rows = new Map(), reassigned = [];
        for (const row of cycle.accounts.values()) {
          if (rows.has(row.player_id)) throw Error('Conflicting account inventory');
          if (row.linked_steam_id) {
            const ledger = cycle.ledgers.get(row.linked_steam_id);
            if (!ledger || ledger.state !== 'linked' || ledger.account_id !== row.account_id ||
                ledger.player_id !== row.player_id) throw Error('Account inventory changed');
          }
          rows.set(row.player_id,row);
        }
        for (const [steam,ledger] of cycle.ledgers) {
          reassigned.push(steam);
          if (ledger.state === 'linked') {
            const account = cycle.accounts.get(ledger.account_id);
            if (!account || account.player_id !== ledger.player_id || account.linked_steam_id !== steam) throw Error('Account inventory changed');
          } else {
            if (ledger.account_id || rows.has(ledger.player_id)) throw Error('Account inventory changed');
            rows.set(ledger.player_id,{player_id:ledger.player_id, account_id:'', persona:'', account_created:0,
              linked_steam_id:'', steam_login_id:steam, account_type:'Steam'});
          }
        }
        current = {available:true, stale:false, updated_at:now(), rows:[...rows.values()], reassigned};
        cycle = null;
      }
    })().catch(() => {
      current = {...current, stale:true}; cycle = null;
    }).finally(() => { busy = null; });
    return busy;
  }
  async function run() {
    await step();
    if (!stopped) { timer = setTimeout(run, cycle ? 250 : 60000); timer.unref?.(); }
  }
  if (autostart && store) { timer = setTimeout(run,0); timer.unref?.(); }
  return {snapshot:()=>({...current, stale:current.stale || Boolean(current.updated_at && now()-current.updated_at>120000)}),
    refresh:step, close:async()=>{stopped=true;clearTimeout(timer);if(busy)await busy;}};
}

function enrich(row, snapshot) {
  const account = snapshot.byPlayer ? snapshot.byPlayer.get(row.player_id) : snapshot.rows.find(a=>a.player_id===row.player_id);
  const reassigned = snapshot.reassignedIds ? snapshot.reassignedIds.has(row.player_id) : snapshot.reassigned?.includes(row.player_id);
  const legacySteam = snapshot.available && !reassigned && identity.validSteam(row.player_id);
  return {...row, account_id:'', account_created:0, linked_steam_id:'', steam_login_id:legacySteam?row.player_id:'',
    account_type:row.auth_method==='lightsout'?'Lights Out':legacySteam?'Steam':'Unknown', ...account,
    persona:account?.persona || row.persona || '',
    game_steam_id:identity.validSteam(row.game_steam_id)?row.game_steam_id:''};
}
function summary(rows, snapshot, range = {}) {
  const available = snapshot.available;
  const count = predicate => available ? rows.filter(predicate).length : null;
  return {available, stale:snapshot.stale, updated_at:snapshot.updated_at, total:available?rows.length:null,
    lightsout:count(p=>Boolean(p.account_id)), linked:count(p=>p.account_type==='Linked'),
    steam:count(p=>p.account_type==='Steam'), unknown:count(p=>p.account_type==='Unknown'),
    registrations:count(p=>p.account_id && p.account_created >= (range.from||0) && p.account_created <= (range.to||Infinity)),
    note:!available?'Account inventory is loading or unavailable. Showing known player records.':
      snapshot.stale?'Account inventory refresh is delayed; showing the last complete snapshot.':
      'Current player profiles. Linked sign-in methods count once. Inventory refreshes every minute.'};
}
module.exports = {create, enrich, summary};
