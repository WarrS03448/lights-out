'use strict';

const { createHash } = require('node:crypto');

/**
 * Evidence policy v1. All damage is observed health loss, never hit callbacks.
 * Defaults are deliberately conservative, not a claim to detect intent perfectly:
 * 30 HP makes an incident substantial; >=3 independent incidents, >=150 decayed
 * HP and >=2.5 decayed incident units require repeated targeting across >=2
 * match/round scopes OR continuation after a SERVER-ACKNOWLEDGED warning.
 * Without acknowledgement/target corroboration, >=4 incidents, >=300 decayed HP,
 * >=3.5 decayed units and >=2 scopes are required. Victims never count as incidents.
 * Evidence half-life is 7 days; expiry 30 days. An 8 second quiet interval makes
 * bursts independent; explicit attack IDs always coalesce within a round.
 * A causer may be a persistent weapon, so its identity alone is not an attack.
 * evaluate(history, now, overrides) permits server policy configuration. Version
 * stays 1; applications changing meaning should ship a new explicit rule version.
 *
 * Coverage starts at seq=1 with a fully bound roster and ends with complete=1.
 * A gap, observer/epoch/roster change, rejected authenticated event or bad health
 * continuity invalidates completeness permanently and disqualifies subsequent
 * incident fragments from sanctions. Complete damage coverage does
 * not imply shot/bone/objective coverage. Missing optional observations remain null.
 * The wire has no authoritative life ID: health resets within a round therefore
 * degrade coverage; damage is capped to one max-health budget per target/round.
 * This deliberately undercounts same-round respawns until a validated life signal
 * is added. Self damage is taken damage, never enemy credit or a FF sanction.
 */
const POLICY = Object.freeze({ruleVersion:1,substantialDamage:30,minIncidents:3,minDamage:150,minEffectiveIncidents:2.5,
  heavyIncidents:4,heavyDamage:300,heavyEffectiveIncidents:3.5,halfLifeMs:7*86400000,expiryMs:30*86400000,
  independentSeconds:8,assistDamage:20,assistSeconds:10,maxRowBytes:4096,maxEvents:20000,maxHistory:4096});
const PHASES = new Set(['combat','live','phase.combat','gamephase.combat','game.phase.startround','game.phase.round']);
const KINDS = new Set(['health','hit','shot','objective','phase','coverage']);
const NUMBERS = new Set(['v','seq','n','t','at','bt','old','new','max','distance','roster','bound','gaps','damage','shots','objectives','complete','terminal','ref']);
const FIELDS = new Set([...NUMBERS,'kind','phase','a','b','weapon','causer','attack','bone','epoch','observer','objective']);
const INTEGER = new Set(['v','seq','n','at','bt','roster','bound','gaps','damage','shots','objectives','complete','terminal','ref']);
const SID = /^\d{17}$/;
const TOKEN = /^[A-Za-z0-9_.:/ -]{1,128}$/;
const numeric = value => typeof value === 'number' && Number.isFinite(value);
const hash = value => createHash('sha256').update(value).digest('hex').slice(0,32);
const canonical = event => JSON.stringify(Object.keys(event).sort().map(key=>[key,event[key]]));
const isCombat = phase => typeof phase === 'string' && PHASES.has(phase.toLowerCase());

function parseRow(text) {
  if (typeof text !== 'string' || Buffer.byteLength(text,'utf8') > POLICY.maxRowBytes || !text.length) return null;
  const raw = {};
  for (const part of text.split(';')) {
    const i=part.indexOf('='); if(i<1 || i===part.length-1) return null;
    const key=part.slice(0,i), value=part.slice(i+1);
    if(!FIELDS.has(key) || Object.hasOwn(raw,key)) return null;
    if(NUMBERS.has(key)) {
      // Bodycam reports raw health below zero on lethal overkill. Preserve it
      // for continuity and replay identity; only actual living health is damage.
      const pattern=(key==='old'||key==='new')?/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/:/^(?:0|[1-9]\d*)(?:\.\d+)?$/;
      if(!pattern.test(value)) return null;
      raw[key]=Number(value);
      if(!numeric(raw[key]) || Math.abs(raw[key])>Number.MAX_SAFE_INTEGER || (INTEGER.has(key)&&!Number.isSafeInteger(raw[key]))) return null;
    } else { if(!TOKEN.test(value)) return null; raw[key]=value; }
  }
  if(raw.v!==1 || !Number.isSafeInteger(raw.seq) || raw.seq<1 || !KINDS.has(raw.kind) ||
    !Number.isSafeInteger(raw.n) || raw.n<0 || raw.n>10000 || !numeric(raw.t) || raw.t>604800 || !raw.phase) return null;
  for(const key of ['a','b']) if(raw[key]!==undefined&&!SID.test(raw[key])) return null;
  for(const key of ['at','bt']) if(raw[key]!==undefined&&raw[key]>255) return null;
  for(const key of ['damage','shots','objectives','complete','terminal']) if(raw[key]!==undefined&&raw[key]!==0&&raw[key]!==1) return null;
  if(raw.kind!=='health'&&(raw.old<0||raw.new<0)) return null;
  if(raw.kind==='health' && (!raw.b || !numeric(raw.old)||!numeric(raw.new)||!numeric(raw.max)||raw.max<=0||raw.max>100000||raw.old>raw.max||raw.new>raw.max)) return null;
  if(raw.kind==='hit'&&!raw.b) return null;
  if((raw.kind==='shot'||raw.kind==='objective')&&!raw.a) return null;
  if(raw.kind==='coverage' && (!raw.epoch||!raw.observer||!Number.isSafeInteger(raw.roster)||!Number.isSafeInteger(raw.bound)||!Number.isSafeInteger(raw.gaps)||raw.roster>64||raw.bound>64)) return null;
  return raw;
}

function normalize(event) {
  if(typeof event==='string') return parseRow(event);
  if(!event||typeof event!=='object'||Array.isArray(event)||Object.keys(event).length>FIELDS.size) return null;
  if(Object.entries(event).some(([key,value])=>!FIELDS.has(key)||!['number','string'].includes(typeof value)||(typeof value==='string'&&/[;=]/.test(value)))) return null;
  return parseRow(Object.entries(event).map(([key,value])=>`${key}=${value}`).join(';'));
}

function createState() {
  return {version:1,matchId:null,hostId:null,lastSeq:0,seen:{},events:[],incidents:[],health:{},roster:{},
    coverage:{started:false,closed:false,broken:false,epoch:null,observer:null,rosterKey:null,damage:false,shots:false,objectives:false}};
}

function rosterOf(value) {
  if(!value||typeof value!=='object'||Array.isArray(value)) return null;
  const entries=Object.entries(value); if(!entries.length||entries.length>64) return null;
  if(entries.some(([id,team])=>!SID.test(id)||!Number.isInteger(team)||team<0||team>255)) return null;
  return Object.fromEntries(entries.sort(([a],[b])=>a.localeCompare(b)));
}

function ingest(state,event,context={}) {
  const result=(accepted,reason,incident=null,duplicate=false)=>({accepted,duplicate,reason,incident,
    decision:incident?evaluate(state.incidents.filter(i=>i.actorId===incident.actorId),context.now):null});
  if(context.authenticated!==true) return result(false,'unauthenticated');
  const provisional=context.validated!==true;
  if(provisional) {
    if(state?.coverage)state.coverage.broken=true;
    if(context.observationOnly!==true)return result(false,'unvalidated');
  }
  if(!state||state.version!==1) return {accepted:false,duplicate:false,reason:'invalid_state',incident:null,decision:null};
  const fail=reason=>{state.coverage.broken=true;return result(false,reason);};
  const e=normalize(event); if(!e) return fail('malformed');
  const roster=rosterOf(context.roster);
  if(!roster||!numeric(context.now)||context.now<0||typeof context.matchId!=='string'||!context.matchId.length||context.matchId.length>128||typeof context.hostId!=='string'||!SID.test(context.hostId)) return fail('invalid_context');
  if(state.matchId!==null&&(state.matchId!==context.matchId||state.hostId!==context.hostId)) return result(false,'scope_mismatch');
  const fingerprint=hash(canonical(e));
  if(Object.hasOwn(state.seen,e.seq)) {
    if(state.seen[e.seq]===fingerprint) return result(false,'duplicate',null,true);
    return fail('sequence_conflict');
  }
  if(e.seq<=state.lastSeq) return fail('old_sequence');
  // Older captures may already fill the ordinary ceiling. Permit one terminal
  // coverage record to close them, preserving incomplete coverage on overflow.
  if(state.events.length>=POLICY.maxEvents) {
    if(e.kind!=='coverage'||e.terminal!==1||state.events.some(row=>row.terminal===1)) return fail('event_limit');
    state.coverage.broken=true;
  }
  if(e.kind==='shot'&&context.validatedShot!==true) return fail('unvalidated_shot');
  const rosterKey=canonical(roster);
  if(state.coverage.rosterKey&&state.coverage.rosterKey!==rosterKey) state.coverage.broken=true;
  if(e.seq!==state.lastSeq+1) state.coverage.broken=true;
  if(e.epoch&&state.coverage.epoch&&e.epoch!==state.coverage.epoch) state.coverage.broken=true;
  state.matchId=context.matchId;state.hostId=context.hostId;state.roster=roster;
  state.lastSeq=e.seq;state.seen[e.seq]=fingerprint;state.coverage.closed=false;
  if(e.kind==='coverage') {
    const full=e.roster===Object.keys(roster).length&&e.bound===e.roster&&e.gaps===0;
    if(e.seq===1&&full&&e.complete!==1) {
      Object.assign(state.coverage,{started:true,epoch:e.epoch,observer:e.observer,rosterKey,damage:e.damage===1,shots:e.shots===1,objectives:e.objectives===1});
    }
    if(!full||state.coverage.observer!==e.observer||state.coverage.epoch!==e.epoch) state.coverage.broken=true;
    for(const key of ['damage','shots','objectives']) if(e[key]!==1) state.coverage[key]=false;
    state.coverage.closed=e.complete===1&&full;
    state.events.push(e); return result(true,'coverage');
  }
  if(e.kind==='phase') {state.events.push(e);return result(true,'phase');}
  if(typeof context.phase!=='string'||e.phase.toLowerCase()!==context.phase.toLowerCase()) {
    state.coverage.broken=true;state.events.push({...e,ignored:true});return result(false,'phase_disagreement');
  }
  if(!isCombat(e.phase)) {
    state.events.push({...e,ignored:true});return result(false,'non_combat_phase');
  }
  const validActor=!e.a||(Object.hasOwn(roster,e.a)&&e.at!==undefined&&roster[e.a]===e.at);
  const validTarget=!e.b||(Object.hasOwn(roster,e.b)&&e.bt!==undefined&&roster[e.b]===e.bt);
  if(!validActor||!validTarget) {state.coverage.broken=true;state.events.push({...e,ignored:true});return result(false,'team_disagreement');}
  if(e.kind!=='health') {state.events.push(e);return result(true,e.kind);}
  const key=`${e.n}:${e.b}`;
  let health=state.health[key];
  if(!health) health=state.health[key]={last:e.old,spent:0,max:e.max};
  let loss=Math.max(0,Math.max(0,Math.min(e.old,health.last))-Math.max(0,e.new));
  if(e.old!==health.last||e.max!==health.max||e.new>e.old) state.coverage.broken=true;
  loss=Math.min(loss,Math.max(0,health.max-health.spent));
  health.spent+=loss;health.last=e.new;
  // An unresolved source may be a player whose credit is missing. Keep observed
  // health loss visible, but do not compare incomplete attribution for ratings.
  if(loss>0&&!e.a)state.coverage.attributionIncomplete=true;
  const relation=e.a&&e.a!==e.b?(e.at===e.bt?'friendly':'enemy'):'environment';
  const saved={...e,loss,relation}; state.events.push(saved);
  if(relation!=='friendly'||loss<=0) return result(true,'health');
  const group=e.attack?`attack:${e.attack}`:null;
  const healthEvidence=Object.fromEntries(['seq','n','t','a','b','at','bt','old','new','max','weapon','causer','attack','bone']
    .filter(field=>e[field]!==undefined).map(field=>[field,e[field]]));
  const eligible=!provisional&&!state.coverage.broken;
  let incident=state.incidents.find(i=>i.actorId===e.a&&i.round===e.n&&(group?i.group===group:!i.group&&e.t>=i.lastGameTime&&e.t-i.lastGameTime<POLICY.independentSeconds));
  if(!incident) {
    incident={id:`ci1_${hash(`${state.matchId}:${e.a}:${e.n}:${group||e.seq}`)}`,actorId:e.a,matchId:state.matchId,round:e.n,
      startedAt:context.now,endedAt:context.now,gameTime:e.t,lastGameTime:e.t,group,damage:0,victimIds:[],lethalVictimIds:[],eventSeqs:[],validated:!provisional,sanctionEligible:eligible,
      evidence:{phase:e.phase,firstHealth:healthEvidence,lastHealth:healthEvidence}};
    state.incidents.push(incident);
  }
  incident.damage+=loss;incident.endedAt=Math.max(incident.endedAt,context.now);incident.lastGameTime=Math.max(incident.lastGameTime,e.t);
  if(provisional) {incident.validated=false;incident.sanctionEligible=false;}
  if(!eligible)incident.sanctionEligible=false;
  if(!incident.evidence)incident.evidence={phase:e.phase,firstHealth:healthEvidence,lastHealth:healthEvidence};
  incident.evidence.lastHealth=healthEvidence;
  if(!incident.victimIds.includes(e.b)) incident.victimIds.push(e.b);
  if(e.old>0&&e.new<=0&&!incident.lethalVictimIds.includes(e.b)) incident.lethalVictimIds.push(e.b);
  incident.eventSeqs.push(e.seq);
  if(numeric(context.warningAckAt)&&context.warningAckAt>=0&&context.warningAckAt<context.now) incident.warningAckAt=context.warningAckAt;
  return result(true,'health',incident);
}

function evaluate(history,now,overrides={}) {
  const p={...POLICY,...overrides};
  const insufficient={classification:'insufficient',ruleVersion:p.ruleVersion,reasons:[],decisionId:null,incidentIds:[]};
  if(!numeric(now)||!Array.isArray(history)||history.length>p.maxHistory) return insufficient;
  // Fail closed on mixed actors: caller must partition persisted history by actor.
  const unique=new Map();
  for(const i of history) {
    if(!i||typeof i.id!=='string'||!SID.test(i.actorId)||i.validated!==true||i.sanctionEligible!==true||i.sanctioned===true||i.sanctionId||
      !numeric(i.damage)||i.damage<=0||!numeric(i.startedAt)||!numeric(i.endedAt)||i.endedAt<i.startedAt||i.startedAt>now||i.endedAt>now||now-i.endedAt>p.expiryMs||
      typeof i.matchId!=='string'||!Number.isInteger(i.round)||!numeric(i.gameTime)||!Array.isArray(i.victimIds)||!i.victimIds.length||i.victimIds.some(v=>!SID.test(v)||v===i.actorId)) continue;
    if(!unique.has(i.id)) unique.set(i.id,i);
    else if(canonical(unique.get(i.id))!==canonical(i)) return insufficient;
  }
  const valid=[...unique.values()].sort((a,b)=>a.startedAt-b.startedAt||a.id.localeCompare(b.id));
  if(!valid.length||new Set(valid.map(i=>i.actorId)).size!==1) return insufficient;
  const independent=[];
  for(const i of valid) {
    if(i.damage<p.substantialDamage) continue;
    if(independent.some(j=>i.matchId===j.matchId&&i.round===j.round&&((i.group&&i.group===j.group)||Math.abs(i.gameTime-(j.lastGameTime??j.gameTime))<p.independentSeconds))) continue;
    independent.push(i);
  }
  const weight=i=>Math.pow(0.5,(now-i.endedAt)/p.halfLifeMs);
  const damage=independent.reduce((sum,i)=>sum+i.damage*weight(i),0);
  const effective=independent.reduce((sum,i)=>sum+weight(i),0);
  const scopes=new Set(independent.map(i=>`${i.matchId}:${i.round}`));
  const victimScopes=new Map();
  for(const i of independent) for(const v of i.victimIds) {if(!victimScopes.has(v)) victimScopes.set(v,new Set());victimScopes.get(v).add(`${i.matchId}:${i.round}`);}
  const targeting=[...victimScopes.values()].some(sc=>sc.size>=2);
  const ack=independent.some(i=>numeric(i.warningAckAt)&&i.warningAckAt>=0&&i.warningAckAt>=now-p.expiryMs&&i.warningAckAt<i.startedAt);
  const substantial=independent.length>=p.minIncidents&&effective>=p.minEffectiveIncidents&&damage>=p.minDamage;
  const heavy=independent.length>=p.heavyIncidents&&effective>=p.heavyEffectiveIncidents&&damage>=p.heavyDamage&&scopes.size>=2;
  const malicious=(substantial&&(targeting||ack))||heavy;
  const reasons=malicious?['repeated_substantial_friendly_damage',...(targeting?['persistent_targeting']:[]),...(ack?['continued_after_acknowledged_warning']:[]),...(heavy?['large_repeated_harm']:[])]:['friendly_fire_observed','intent_not_established'];
  const incidentIds=(malicious?independent:valid).map(i=>i.id).sort();
  return {classification:malicious?'malicious':'warning',ruleVersion:p.ruleVersion,reasons,
    decisionId:`cd${p.ruleVersion}_${hash(`${valid[0].actorId}:${incidentIds.join(':')}`)}`,incidentIds};
}

function summary(state,steamId,rounds) {
  const output={version:1,status:'unavailable',coverage:{damage:false,shots:false,objectives:false},enemyDamage:null,friendlyDamage:null,damageTaken:null,
    assists:null,headshots:null,shots:null,hits:null,adr:null,accuracy:null,weaponStats:[],playerStats:[],ratingsEligible:false};
  if(!state||state.version!==1||!SID.test(steamId)) return output;
  const c=state.coverage,known=Object.hasOwn(state.roster,steamId);
  const continuous=known&&c.started&&c.closed&&!c.broken;
  for(const key of ['damage','shots','objectives']) output.coverage[key]=continuous&&c[key];
  if(c.attributionIncomplete)output.coverage.damage=false;
  output.ratingsEligible=output.coverage.damage;
  const events=state.events.filter(e=>!e.ignored);
  const health=events.filter(e=>e.kind==='health'&&e.loss>0);
  const own=health.filter(e=>e.a===steamId),received=health.filter(e=>e.b===steamId);
  // Counterparties come from accepted health loss, never effect-causer class names.
  // Missing observations in partial captures are unknown, not measured zeroes.
  const players=new Map();
  const player=id=>{
    if(!players.has(id)) players.set(id,{steam_id:id,
      damageDealt:output.coverage.damage?0:null,damageTaken:output.coverage.damage?0:null});
    return players.get(id);
  };
  if(known) {
    for(const id of Object.keys(state.roster)) if(id!==steamId) player(id);
    for(const e of own) if(e.b!==steamId) {
      const row=player(e.b);row.damageDealt=(row.damageDealt||0)+e.loss;
    }
    for(const e of received) {
      const row=player(e.a||'');row.damageTaken=(row.damageTaken||0)+e.loss;
    }
    output.playerStats=[...players.values()].sort((a,b)=>a.steam_id.localeCompare(b.steam_id));
  }
  const observed=own.length||received.length;
  if(observed||output.coverage.damage) {
    output.enemyDamage=own.filter(e=>e.relation==='enemy').reduce((s,e)=>s+e.loss,0);
    output.friendlyDamage=own.filter(e=>e.relation==='friendly').reduce((s,e)=>s+e.loss,0);
    output.damageTaken=received.reduce((s,e)=>s+e.loss,0);
    output.adr=numeric(rounds)&&rounds>0?output.enemyDamage/rounds:null;
  }
  const shots=[...new Map(events.filter(e=>e.kind==='shot'&&e.a===steamId).map(e=>[`${e.n}:${e.attack||`seq-${e.seq}`}`,e])).values()];
  if(shots.length||output.coverage.shots) output.shots=shots.length;
  const contexts=events.filter(e=>e.kind==='hit');
  const contextIndex=new Map(),healthIndex=new Map();
  const pair=e=>`${e.a}:${e.b}:${e.n}`;
  const add=(map,key,value)=>{if(!map.has(key))map.set(key,[]);map.get(key).push(value);};
  for(const h of contexts) {
    if(h.ref!==undefined)add(contextIndex,`ref:${h.ref}`,h);
    else if(h.attack)add(contextIndex,`attack:${pair(h)}:${h.attack}`,h);
    else add(contextIndex,`time:${pair(h)}:${Math.floor(h.t*4)}`,h);
  }
  for(const h of health) if(!h.attack)add(healthIndex,`time:${pair(h)}:${Math.floor(h.t*4)}`,h);
  const nearby=(map,e)=>[-1,0,1].flatMap(offset=>map.get(`time:${pair(e)}:${Math.floor(e.t*4)+offset}`)||[]).filter(h=>Math.abs(h.t-e.t)<=0.25);
  const weapons=new Map(), hitIds=new Set(),headIds=new Set(),weaponHits=new Map(),weaponHeads=new Map();
  let knownBone=false;
  for(const e of own) {
    let candidates=(contextIndex.get(`ref:${e.seq}`)||[]).filter(h=>pair(h)===pair(e));
    if(!candidates.length&&e.attack)candidates=contextIndex.get(`attack:${pair(e)}:${e.attack}`)||[];
    if(!candidates.length&&!e.attack) candidates=nearby(contextIndex,e).filter(h=>nearby(healthIndex,h).length===1);
    // Ambiguous metadata is absent, never arbitrarily selected by arrival order.
    const signatures=new Set(candidates.map(h=>`${h.bone||''}:${h.weapon||''}`));
    const hit=signatures.size===1?candidates[0]:null;
    const weapon=e.weapon||hit?.weapon;
    if(weapon&&!weapons.has(weapon)) weapons.set(weapon,{weapon,enemyDamage:0,friendlyDamage:0,shots:null,hits:null,headshots:null});
    if(weapon&&e.relation==='enemy') weapons.get(weapon).enemyDamage+=e.loss;
    if(weapon&&e.relation==='friendly') weapons.get(weapon).friendlyDamage+=e.loss;
    if(e.relation!=='enemy'||(!hit&&!e.bone)) continue;
    const id=`${e.n}:${e.attack||hit?.attack||e.seq}:${e.b}`;
    hitIds.add(id);const bone=e.bone||hit?.bone||'';
    const head=/^(head|headshot|b_head)$/i.test(bone);
    const body=/^(?:spine(?:_\d+)?|chest|pelvis|body|neck|(?:upperarm|lowerarm|hand|thigh|calf|foot)_[lr])$/i.test(bone);
    if(head)headIds.add(id); if(head||body)knownBone=true;
    if(weapon) {
      if(!weaponHits.has(weapon))weaponHits.set(weapon,new Set());weaponHits.get(weapon).add(id);
      if(head||body) {if(!weaponHeads.has(weapon))weaponHeads.set(weapon,new Set());if(head)weaponHeads.get(weapon).add(id);}
    }
  }
  if(hitIds.size) output.hits=hitIds.size;
  if(knownBone) output.headshots=headIds.size;
  for(const [weapon,ids] of weaponHits)weapons.get(weapon).hits=ids.size;
  for(const [weapon,ids] of weaponHeads)weapons.get(weapon).headshots=ids.size;
  for(const shot of shots) if(shot.weapon) {
    if(!weapons.has(shot.weapon)) weapons.set(shot.weapon,{weapon:shot.weapon,enemyDamage:0,friendlyDamage:0,shots:null,hits:null,headshots:null});
    const w=weapons.get(shot.weapon);w.shots=(w.shots||0)+1;
  }
  // Accuracy requires complete shots plus every enemy loss associated with a real
  // attack ID present on an observed shot. Pellet/victim counts cannot exceed shots.
  const shotIds=new Set(shots.filter(s=>s.attack).map(s=>`${s.n}:${s.attack}`));
  const enemy=own.filter(e=>e.relation==='enemy');
  if(output.coverage.shots&&output.coverage.damage&&shots.length&&shots.every(s=>s.attack)&&enemy.every(e=>e.attack&&shotIds.has(`${e.n}:${e.attack}`))) {
    output.accuracy=100*new Set(enemy.map(e=>`${e.n}:${e.attack}`)).size/shots.length;
  }
  let assists=0;
  for(const kill of health.filter(e=>e.relation==='enemy'&&e.old>0&&e.new<=0&&e.a!==steamId)) {
    const contribution=own.filter(e=>e.relation==='enemy'&&e.b===kill.b&&e.n===kill.n&&e.t<=kill.t&&kill.t-e.t<=POLICY.assistSeconds).reduce((sum,e)=>sum+e.loss,0);
    if(contribution>=POLICY.assistDamage) assists++;
  }
  if(assists||output.coverage.damage) output.assists=assists;
  output.weaponStats=[...weapons.values()].sort((a,b)=>a.weapon.localeCompare(b.weapon));
  output.status=output.coverage.damage?'complete':(observed||shots.length||output.hits!==null?'partial':'unavailable');
  return output;
}

module.exports={POLICY,createState,parseRow,ingest,summary,evaluate};
