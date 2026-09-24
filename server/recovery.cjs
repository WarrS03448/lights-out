'use strict';
// Round-boundary recovery. No client observation by itself grants host authority.
const {createHash,randomBytes,timingSafeEqual}=require('node:crypto');
const identity=require('./player-identity.cjs');
const storage=require('./combat-storage.cjs');
const GRACE=60000, FRESH=20000, RESTORE_WINDOW=180000, REJOIN_WINDOW=300000;
const hash=value=>createHash('sha256').update(value).digest('hex');
const equal=(a,b)=>typeof a==='string'&&typeof b==='string'&&a.length===b.length&&timingSafeEqual(Buffer.from(a),Buffer.from(b));
const integer=(n,min,max)=>Number.isSafeInteger(n)&&n>=min&&n<=max;
const sessionFor=m=>m.session_key||`chm-${m.id}`;
const active=m=>m?.state==='live'&&m.start_ready_verified&&!m.finished&&!m.final_snapshot&&!m.terminal&&!m.void_pending&&!m.collecting;

function parseReport(body,epoch) {
  const match=/^chm-([a-f0-9]{16})(?:-r[a-f0-9]{16})?$/.exec(String(body?.storefront||''));
  const operation={ch_recovery_checkpoint:'checkpoint',ch_recovery_prepared:'prepared',ch_recovery_restored:'restored',ch_session_pulse:'pulse'}[body?.event_name];
  if(!match||!operation||body.first_session_timestamp!=='chrecovery-1'||!identity.validSteam(body.user_id)||
     !integer(epoch,0,999999))return null;
  const fields={operation,match_id:match[1],epoch,session:body.storefront,user_id:body.user_id};
  if(operation==='pulse') {
    if(!/^[1-9]\d{0,9}$/.test(String(body.timestamp||''))||!integer(Number(body.timestamp),1,2147483647))return null;
    return {...fields,sequence:Number(body.timestamp)};
  }
  const meta=/^(0|[1-9]\d?);([1-9]\d{0,2});(0|[1-9]\d{0,2});(0|[1-9]\d{0,2});([01])$/.exec(String(body.timestamp||''));
  const raw=String(body.platform||'');if(!meta||raw.length>4096)return null;
  const rows=raw.split(';').map(s=>/^(\d{17}):([01]):(-?(?:0|[1-9]\d{0,2})):(0|[1-9]\d?):(0|[1-9]\d{0,2})$/.exec(s));
  if(rows.length<1||rows.length>10||rows.some(r=>!r))return null;
  return {...fields,checkpoint:{v:1,round:Number(meta[1]),limit:Number(meta[2]),scores:[Number(meta[3]),Number(meta[4])],objective:Number(meta[5]),
    rows:rows.map(r=>({id:r[1],team:Number(r[2]),k:Number(r[3]),d:Number(r[4]),sp:Number(r[5])}))}};
}

// All four records form one compare-and-set domain. No mutation follows a failed
// comparison; types and JSON are checked before the first write (Lua has no rollback).
const CAS=`-- round-recovery-cas-v1
for i=1,4 do
  local t=redis.call('TYPE',KEYS[i]).ok
  if t~='none' and t~='string' then return {'invalid-type'} end
end
local p=cjson.decode(ARGV[1])
for i=1,4 do
  local raw=redis.call('GET',KEYS[i]) or ''
  if raw~=p.expected[i] then return {'conflict'} end
end
if p.expected[3]~='' then return {'closed'} end
for _,i in ipairs({1,2,4}) do
  if p.next[tostring(i)] then cjson.decode(p.next[tostring(i)]) end
end
for _,i in ipairs({1,2,4}) do
  if p.next[tostring(i)] then redis.call('SET',KEYS[i],p.next[tostring(i)],'EX',ARGV[2]) end
end
return {'saved'}
`;

const EXPIRE=`-- round-recovery-expire-v1
if redis.call('GET',KEYS[2]) then return 0 end
local raw=redis.call('GET',KEYS[1])
if not raw then return 0 end
local a=cjson.decode(raw)
if a.closed or a.phase~='restoring' or a.host~=ARGV[1] or a.epoch~=tonumber(ARGV[2]) or
   not a.restore_until or tonumber(ARGV[3])<a.restore_until then return 0 end
a.closed=true
a.closed_reason='recovery_expired'
redis.call('SET',KEYS[1],cjson.encode(a),'EX',ARGV[4])
return 1
`;

function canonicalCheckpoint(m,input,previous) {
  if(!input||input.v!==1||!integer(input.round,0,99)||!integer(input.limit,1,999)||
     input.limit!==(m.expected_score_limit||m.agreedScoreLimit)||!Array.isArray(input.scores)||input.scores.length!==2||
     !input.scores.every(n=>integer(n,0,input.limit-1))||input.scores[0]+input.scores[1]!==input.round||
     !integer(input.objective,0,1)||!Array.isArray(input.rows)||!identity.validBindings(m))return null;
  const players=[...(m.players||[]),...(m.left||[])];
  if(players.length<2||players.length>10||input.rows.length>players.length)return null;
  const supplied=[...input.rows];
  // A removed player has no native PlayerState to read. Preserve their last
  // completed statistics, using the current active teammate's native side.
  for(const left of m.left||[]) {
    const game=identity.gameFor(m,left.player_id);
    const frozen=left.recovery_row||(left.disconnect_confirmed&&previous?.rows.find(row=>row.id===game));
    const reported=supplied.find(row=>row?.id===game);
    if(reported) {
      if(frozen&&['k','d','sp'].some(key=>reported[key]!==frozen[key]))return null;
      continue;
    }
    const side=[1,2].find(s=>(m.assigned_teams||m.teams)?.[s]?.includes(left.player_id));
    const teammate=supplied.find(row=>(m.assigned_teams||m.teams)?.[side]?.includes(identity.playerFor(m,row?.id)));
    if(!left.disconnect_confirmed||!frozen||!teammate)return null;
    supplied.push({...frozen,team:teammate.team});
  }
  if(supplied.length!==players.length)return null;
  const seen=new Set(),sides=new Map(),rows=[];
  for(const row of supplied) {
    if(!row||!identity.validSteam(row.id)||seen.has(row.id)||!integer(row.team,0,1)||
       !integer(row.k,-999,999)||!integer(row.d,0,99)||!integer(row.sp,0,999))return null;
    const player=identity.playerFor(m,row.id);
    const side=[1,2].find(s=>(m.assigned_teams||m.teams)?.[s]?.includes(player));
    if(!player||!players.some(p=>p.player_id===player)||!side||
       (sides.has(side)&&sides.get(side)!==row.team))return null;
    seen.add(row.id);sides.set(side,row.team);
    rows.push({id:row.id,team:row.team,k:row.k,d:row.d,sp:row.sp});
  }
  if(sides.size!==2||sides.get(1)===sides.get(2))return null;
  rows.sort((a,b)=>a.id.localeCompare(b.id));
  return {v:1,round:input.round,limit:input.limit,scores:[...input.scores],objective:input.objective,rows};
}

function checkpointRecord(m,data,epoch,now) {
  const scope={match:m.id,mode:m.mode||'BB5',map:m.map,bindings:m.game_bindings};
  return {data,scope,epoch,at:now,hash:hash(JSON.stringify({scope,data}))};
}
function compatible(m,cp) {
  if(!cp?.data||!canonicalCheckpoint(m,cp.data))return false;
  return checkpointRecord(m,cp.data,cp.epoch,cp.at).hash===cp.hash;
}
function stateFor(m,a,r) {
  if(r&&r.v!==1)throw Error('Unsupported recovery record');
  if(r?.epoch===a.epoch&&r.session===sessionFor(m))return r;
  return {v:1,epoch:a.epoch,session:sessionFor(m),phase:'playing',pulses:{},closed:{},
    checkpoint:r?.checkpoint||null,previous:r?.previous||null};
}
function sessionStale(m,a,r,now) {
  if(!active(m)||a.closed||!compatible(m,r.checkpoint))return false;
  if(a.phase==='restoring'&&now<(a.restore_until||Infinity))return false;
  if(!a.last_seen||now<a.last_seen||now-a.last_seen<GRACE)return false;
  if(Object.values(r.pulses).some(p=>now-p.at<GRACE))return false;
  return true;
}
function eligibility(m,a,r,player,now) {
  if(!sessionStale(m,a,r,now))return false;
  const closed=m.players.filter(p=>p.player_id!==a.host&&r.closed[p.player_id]!==undefined&&
    now-r.closed[p.player_id]>=0&&now-r.closed[p.player_id]<FRESH);
  const own=r.closed[player];
  return own!==undefined&&now-own>=0&&now-own<FRESH&&closed.length>=Math.min(2,m.players.length-1);
}
function publicState(m,a,r,player,now) {
  return {phase:r.phase,epoch:a.epoch,revision:m.recovery?.revision||0,session:sessionFor(m),checkpoint_round:r.checkpoint?.data.round??null,
    server_now:now,session_stale:sessionStale(m,a,r,now),roster_revision:m.roster_revision||0,
    can_rejoin:canRejoin(m,player,now),
    can_claim:eligibility(m,a,r,player,now),restore_until:a.restore_until||null,
    rejoin_until:m.recovery?.rejoin_until||null,roster_sealed:!!m.recovery?.roster,
    expected:m.recovery?.expected||m.players.length,
    connected:m.players.filter(p=>r.pulses[p.player_id]&&now-r.pulses[p.player_id].at<FRESH).map(p=>p.player_id)};
}

function canRejoin(m,player,now) {
  if(!m.players.some(p=>p.player_id===player))return false;
  const recovery=m.recovery;
  if(recovery?.roster)return recovery.roster.admitted.includes(player);
  return !recovery?.rejoin_until||now<recovery.rejoin_until;
}

function restoredSubset(m,r,input) {
  const saved=r.checkpoint?.data,cohort=m.recovery?.roster;
  if(!saved||!cohort?.done||!input||input.v!==1||input.round!==saved.round||input.limit!==saved.limit||
     input.objective!==saved.objective||JSON.stringify(input.scores)!==JSON.stringify(saved.scores)||
     !Array.isArray(input.rows)||input.rows.length!==cohort.admitted.length)return false;
  const seen=new Set();
  for(const row of input.rows) {
    const player=identity.playerFor(m,row?.id),prior=saved.rows.find(p=>p.id===row?.id);
    if(!prior||seen.has(player)||!cohort.admitted.includes(player)||
       ['team','k','d','sp'].some(key=>row[key]!==prior[key]))return false;
    seen.add(player);
  }
  return seen.has(m.host)&&[1,2].every(side=>m.players.some(p=>(m.assigned_teams||m.teams)?.[side]?.includes(p.player_id)));
}

// Rewind only match accounting. Retain conduct evidence in sealed segments: a
// recovery must never erase an already committed sanction or claim new coverage.
function rewind(m,cp,now) {
  const c=cp.data,sideFor=id=>[1,2].find(s=>(m.assigned_teams||m.teams)[s].includes(identity.playerFor(m,id)));
  const mapping=Object.fromEntries(c.rows.map(row=>[row.team,sideFor(row.id)]));
  m.score={[mapping[0]]:c.scores[0],[mapping[1]]:c.scores[1]};
  m.team_map=mapping;m.ingame={__map:c.rows.map(row=>[identity.playerFor(m,row.id),row.team])};
  m.recovery_interrupted={at:now,score:m.game_scores||null,round:c.round};
  delete m.game_scores;delete m.lastAlive;delete m.rounds_played;
  if(Array.isArray(m.rounds))m.rounds=m.rounds.filter(row=>Number.isInteger(row?.round)
    ?row.round<c.round : Number.isInteger(row?.[1])&&Number.isInteger(row?.[2])&&
      row[1]<=m.score[1]&&row[2]<=m.score[2]&&row[1]+row[2]<=c.round);
  else if(m.rounds)m.rounds=Object.fromEntries(Object.entries(m.rounds).filter(([round])=>Number(round)<c.round));
  if(Array.isArray(m.kills))m.kills=m.kills.filter(row=>Number.isInteger(row?.round)&&row.round<c.round);
  for(const name of ['round_reports','round_wins','round_results'])if(m[name])
    m[name]=Object.fromEntries(Object.entries(m[name]).filter(([round])=>Number(round)<c.round));
  const oldSeries=m.stats?.series||{};
  m.stats={rounds:c.round,limit:c.limit,players:c.rows.map(row=>({steamId:identity.playerFor(m,row.id),gameSteamId:row.id,
    kills:row.k,deaths:row.d,spawnCount:row.sp,teamId:row.team,teamScore:c.scores[row.team]})),
    series:Object.fromEntries(Object.entries(oldSeries).map(([id,series])=>[id,Object.fromEntries(Object.entries(series).filter(([n])=>Number(n)<c.round))])),
    seenAt:{},seenRounds:{}};
  m.combat_segments ||= [];
  if(m.combatState){m.combatState.coverage.broken=true;m.combat_segments.push(m.combatState);}
  delete m.combatState;delete m.combat_end;m.combat_migrated=true;
  // The replacement grants a new full return window; outage time is not charged.
  delete m.recovery_reconnect;
  m.reconnect={};m.host_ready=false;
}

async function execute(store,keys,args) {
  const {operation,player,token,epoch,session,now=Date.now(),ttl=86400}=args;
  if(!['checkpoint','pulse','closed','status','claim','opened','seal','adjudicated','prepared','restored'].includes(operation)||!identity.validPlayer(player)||
     !/^[a-f0-9]{64}$/.test(token||'')||!integer(epoch,0,999999)||!integer(now,1,Number.MAX_SAFE_INTEGER)||
     !integer(ttl,60,604800))return {ok:false,error:'Invalid recovery request'};
  for(let attempt=0;attempt<8;attempt++) {
    const expected=(await store(['MGET',...keys],{strict:true})).map(v=>v||'');
    if(!expected[0]||!expected[1]||expected[2])return {ok:false,error:'Match is no longer live'};
    const a=JSON.parse(expected[0]),m=await storage.unpack(store,JSON.parse(expected[1]),keys[1]);
    if(!active(m)||a.closed||a.epoch!==epoch||m.host_epoch!==epoch||a.host!==m.host||session!==sessionFor(m)||
       !m.players.some(p=>p.player_id===player)||!equal(m.migration_digests?.[player],hash(token)))
      return {ok:false,error:'Superseded recovery request'};
    const r=stateFor(m,a,expected[3]?JSON.parse(expected[3]):null),next={};
    if(m.recovery?.roster?.excluded.includes(player))return {ok:false,error:'Return deadline missed'};
    let changed=false;
    const saveMatch=()=>{
      m.recovery.revision=(m.recovery.revision||0)+1;
      a.recovery_revision=m.recovery.revision;
      changed=true;
    };
    if(operation==='status')return {ok:true,recovery:publicState(m,a,r,player,now)};
    if(['opened','seal','adjudicated'].includes(operation)) {
      if(player!==a.host||!equal(a.digest,hash(token))||a.phase!=='restoring'||!m.recovery||now>=a.restore_until)
        return {ok:false,error:'Replacement is not available'};
      if(operation==='opened') {
        if(!m.recovery.rejoin_until) {
          m.recovery.ready_at=now;m.recovery.rejoin_until=now+REJOIN_WINDOW;
          m.recovery.expected=m.players.length;m.recovery.deadline=now+REJOIN_WINDOW+RESTORE_WINDOW;
          a.restore_until=m.recovery.deadline;
          m.lobby_stamped=true;m.joiners_released=true;saveMatch();
        }
      } else if(operation==='seal') {
        if(!m.recovery.rejoin_until)return {ok:false,error:'Replacement lobby is not ready'};
        if(!m.recovery.roster) {
          const end=m.recovery.rejoin_until,at=Math.min(now,end);
          const admitted=m.players.filter(p=>r.pulses[p.player_id]&&r.pulses[p.player_id].at<=at&&
            at-r.pulses[p.player_id].at<FRESH).map(p=>p.player_id);
          if(!admitted.includes(m.host)||!r.pulses[m.host]||now-r.pulses[m.host].at>=FRESH)
            return {ok:false,error:'Replacement host is not confirmed'};
          if(now<end&&admitted.length!==m.players.length)return {ok:false,error:'Waiting for players'};
          const excluded=m.players.filter(p=>!admitted.includes(p.player_id)).map(p=>p.player_id);
          const bothSides=[1,2].every(side=>admitted.some(id=>(m.assigned_teams||m.teams)?.[side]?.includes(id)));
          m.recovery.roster={at,admitted,excluded,done:excluded.length===0&&bothSides};
          saveMatch();
        }
      } else {
        const cohort=m.recovery.roster;
        if(!cohort)return {ok:false,error:'Return roster is not sealed'};
        if(!cohort.done) {
          const duel=m.mode==='BB1';
          if(!duel&&cohort.excluded.some(id=>args.penalties?.[id]?.match_id!==m.id||
             args.penalties[id].player_id!==id||args.penalties[id].reason!=='reconnect_timeout'))
            return {ok:false,error:'Absence penalties are not saved'};
          const gone=m.players.filter(p=>cohort.excluded.includes(p.player_id));
          for(const p of gone) {
            const row=r.checkpoint.data.rows.find(row=>row.id===identity.gameFor(m,p.player_id));
            (m.left||=[]).push({...p,at:cohort.at,left_state:'live',disconnect_confirmed:true,
              reason:'reconnect_timeout',recovery_excluded:true,recovery_row:row,penalty:args.penalties?.[p.player_id]||null,
              last_stats:{steamId:p.player_id,gameSteamId:row.id,kills:row.k,deaths:row.d,spawnCount:row.sp,teamId:row.team,teamScore:r.checkpoint.data.scores[row.team]}});
          }
          m.players=m.players.filter(p=>cohort.admitted.includes(p.player_id));
          cohort.done=true;m.reconnect={};
          const sides=[1,2].filter(side=>m.players.some(p=>(m.assigned_teams||m.teams)?.[side]?.includes(p.player_id)));
          if(sides.length===1) {
            const losingSide=sides[0]===1?2:1;
            const loser=[...cohort.excluded,...(m.left||[]).map(p=>p.player_id)]
              .find(id=>(m.assigned_teams||m.teams)[losingSide].includes(id));
            m.terminal={reason:'reconnect_timeout',recovery:true,loser,losers:cohort.excluded,
              winner:sides[0],at:cohort.at,score:{...m.score}};
          }
          saveMatch();
        }
      }
    } else if(operation==='pulse') {
      if(a.phase==='restoring'&&m.recovery?.rejoin_until&&now>=m.recovery.rejoin_until&&!m.recovery.roster)
        return {ok:false,error:'Return roster is being sealed'};
      if(!integer(args.sequence,1,2147483647)||args.sequence<=(r.pulses[player]?.sequence||0))return {ok:false,error:'Stale session pulse'};
      r.pulses[player]={sequence:args.sequence,at:now};delete r.closed[player];
    } else if(operation==='closed') r.closed[player]=now;
    else if(['checkpoint','prepared','restored'].includes(operation)) {
      if(player!==a.host||!equal(a.digest,hash(token)))return {ok:false,error:'Only the current host can save'};
      if(operation==='restored'||operation==='prepared') {
        if(!compatible(m,r.checkpoint)||!restoredSubset(m,r,args.checkpoint)||!m.recovery||m.recovery.hash!==r.checkpoint.hash)
          return {ok:false,error:'Restored state differs from checkpoint'};
        if(a.phase==='playing'&&r.phase==='playing')return operation==='restored'
          ?{ok:true,replayed:true}:{ok:false,error:'Restore already completed'};
        if(a.phase!=='restoring'||now>=a.restore_until)return {ok:false,error:'Restore expired'};
        if(operation==='prepared') {
          r.prepared=r.checkpoint.hash;
        } else {
        if(r.prepared!==r.checkpoint.hash||m.players.some(p=>!r.pulses[p.player_id]||now-r.pulses[p.player_id].at>=FRESH))
          return {ok:false,error:'Waiting for participants to confirm the new session'};
        a.phase='playing';a.last_seen=now;delete a.restore_until;r.phase='playing';
        m.recovery.phase='playing';m.recovery.completed_at=now;m.host_ready=true;
        if(m.deadline)m.deadline+=Math.max(0,now-m.recovery.started_at);
        m.reconnect={};
        delete m.recovery_reconnect;
        saveMatch();
        }
      } else {
        if(a.phase==='restoring')return {ok:false,error:'Restore is not verified'};
        const data=canonicalCheckpoint(m,args.checkpoint,r.checkpoint?.data);
        if(!data)return {ok:false,error:'Invalid round boundary'};
        const cp=checkpointRecord(m,data,epoch,now);
        if(r.checkpoint?.data.round>=data.round) {
          return r.checkpoint.hash===cp.hash?{ok:true,replayed:true}:{ok:false,error:'Stale or conflicting checkpoint'};
        }
        r.previous=r.checkpoint;r.checkpoint=cp;
      }
    } else if(operation==='claim') {
      if(!eligibility(m,a,r,player,now)||epoch>=999999)return {ok:false,error:'Recovery is not ready'};
      const previous=m.host;rewind(m,r.checkpoint,now);
      m.host=player;m.host_epoch=epoch+1;m.session_key=`chm-${m.id}-r${randomBytes(8).toString('hex')}`;
      m.migration_capabilities=Object.fromEntries(m.players.map(p=>[p.player_id,randomBytes(32).toString('hex')]));
      m.migration_digests=Object.fromEntries(Object.entries(m.migration_capabilities).map(([id,t])=>[id,hash(t)]));
      m.reportToken=m.migration_capabilities[player];m.legacyReportAuth=false;
      m.recovery={phase:'restoring',hash:r.checkpoint.hash,round:r.checkpoint.data.round,started_at:now,deadline:now+RESTORE_WINDOW,revision:0};
      m.recovery_checkpoint=r.checkpoint.data;
      m.lobby_stamped=false;m.joiners_released=false;m.host_permit_until=now+RESTORE_WINDOW;
      for(const p of m.players)p.connected=false;
      (m.host_migrations ||= []).push({epoch:m.host_epoch,previous,host:player,at:now,recovery:true});
      Object.assign(a,{host:player,epoch:m.host_epoch,digest:hash(m.reportToken),candidate:'',last_seen:now,phase:'restoring',restore_until:now+RESTORE_WINDOW});
      Object.assign(r,{epoch:a.epoch,session:m.session_key,phase:'restoring',pulses:{},closed:{}});
      delete r.prepared;
      saveMatch();
    }
    if(changed){next['1']=JSON.stringify(a);next['2']=JSON.stringify(await storage.pack(store,m,keys[1],{ttl:ttl+86400}));}
    next['4']=JSON.stringify(r);
    const reply=await store(['EVAL',CAS,'4',...keys,JSON.stringify({expected,next}),String(ttl)],{strict:true});
    if(reply?.[0]==='conflict')continue;
    if(reply?.[0]!=='saved')throw Error('Recovery state could not be saved');
    return {ok:true,changed,snapshot:next['2']?m:null,recovery:publicState(m,a,r,player,now)};
  }
  return {ok:false,error:'Recovery is busy; retry'};
}
module.exports={execute,canonicalCheckpoint,parseReport,sessionFor,canRejoin,CAS,EXPIRE,GRACE,FRESH,RESTORE_WINDOW,REJOIN_WINDOW};
