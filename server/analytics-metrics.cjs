'use strict';
const crypto = require('node:crypto');
const DAY = 86400000;
const number = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
const text = (v, n=96) => typeof v === 'string' ? v.slice(0,n) : '';
const hash = v => crypto.createHash('sha256').update(JSON.stringify(v)).digest('hex').slice(0,24);
function canonical(v){if(v instanceof Set)return [...v].map(canonical).sort();if(Array.isArray(v))return v.map(canonical);if(v&&typeof v==='object')return Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])]));return v;}
const pick = (o, keys) => Object.fromEntries(keys.filter(k => o?.[k] !== undefined).map(k=>[k,o[k]]));
const numeric = o => Object.fromEntries(Object.entries(o || {}).filter(([k,v])=>/^[a-zA-Z][a-zA-Z0-9_]{0,63}$/.test(k) && (number(v)!==null || typeof v==='boolean')));
// Only these containers/scalars survive the evidence projection. Never serialize a match object.
const EVIDENCE = new Set(('round n at t ev kind phase state from to who host map team team_id steam_id steamId persona name kills deaths team_kills won winner score score_limit seconds duration duration_seconds elapsed alive spawns spawnCount roundsWon roundsPlayed lateJoin spectator reported coverage status enemyDamage friendlyDamage damageTaken damageDealt assists headshots shots hits adr accuracy weapon weaponStats playerStats weaponStats coverage damage objectives ratingsEligible measured actual expected excess impactInTeam impactInLobby decisive integrity presence quality kpr survival spr roundWinShare clutch clutches teamKills roundsPlayed seq epoch observer a b old new max loss relation ignored attack bone distance at bt ref complete terminal gaps roster bound reason code by blame expired connected left data_collected source valid raw start end index players teams events timeline stats score_before score_after first_kill last_kill duration_known rounds_played final_stats net_kills enemy_kills damage_dealt damage_taken teamkill round_number winner_team rows metrics value total count complete_damage complete_shots').split(' '));
for(const k of 'scoreboard ratings rating rd parties waited tolerance delta spread network region predicted_win attack defense attacker defender side duration_ms start_at end_at round_seconds alive_start alive_end combat damage_dealt damage_taken health samples'.split(' '))EVIDENCE.add(k);
function evidence(value, depth=0) {
  if (depth>12) return null;
  if (value===null || typeof value==='boolean') return value;
  if (typeof value==='number') return number(value);
  if (typeof value==='string') return value.slice(0,160);
  if (Array.isArray(value)) return value.slice(0,20000).map(v=>evidence(v,depth+1));
  if (!value || typeof value!=='object') return null;
  return Object.fromEntries(Object.entries(value).filter(([k])=>EVIDENCE.has(k)||/^\d{1,17}$/.test(k)).map(([k,v])=>[k,evidence(v,depth+1)]));
}
function ruleSnapshot() {
  const out={};
  for(const module of ['rating','progress','valuation','matchmaker','network']) {
    out[module]=Object.fromEntries(Object.entries(require(`./${module}.cjs`)).filter(([k,v])=>/^[A-Z][A-Z0-9_]+$/.test(k)&&typeof v!=='function'));
  }
  out.policy=Object.fromEntries(Object.entries(process.env).filter(([k])=>/^COMP_(?:TK_ENFORCE|COMBAT_RATINGS_ENABLED|QUEUE_PENALTIES_PAUSED|NO_SHOW_PENALTIES_PAUSED|MATCH_SIZE|GAME_RULES_OVERRIDE)$/.test(k)));
  return canonical(out);
}
function projectReceipt(r) {
  if(!r || typeof r!=='object') throw Error('Invalid match receipt');
  const full=r.publicMatch||r.full||r;
  const id=text(r.matchId||r.match_id||full.id,80);
  if(!/^[A-Za-z0-9_.:-]{1,80}$/.test(id)) throw Error('Invalid match ID');
  const at=number(full.ended)||number(r.at);
  if(at===null||at<=0)throw Error('Receipt has no valid end time');
  const teams=r.inputs?.teams||full.teams||{};
  const rows=Array.isArray(r.rows)?r.rows:[];
  const roster=Array.isArray(full.players)?full.players:(r.inputs?.players||[]);
  const ids=[...new Set([...Object.values(teams).flat(),...rows.map(p=>p.steamId),...roster.map(p=>p.steam_id)])].filter(v=>/^\d{17}$/.test(v));
  const board=Array.isArray(r.board)?r.board:(full.scoreboard||[]);
  const mm=r.inputs?.mm||full.mm||{};
  const players=ids.map(sid=>{
    const row=rows.find(p=>p.steamId===sid), person=roster.find(p=>p.steam_id===sid)||{}, stats=board.find(p=>p.steam_id===sid)||{};
    const before=numeric(row?.before),after=numeric(row?.after);
    const team=Object.keys(teams).find(k=>(teams[k]||[]).includes(sid))||person.team;
    const c=stats.combat||{};
    return {steam_id:sid,persona:text(person.persona||person.name,64),team:Number(team)||null,starting_side:['attack','defend'].includes(full.sides?.[team])?full.sides[team]:null,
      won:typeof row?.won==='boolean'?row.won:null,party_size:Array.isArray(mm.parties)?(mm.parties.find(p=>p.includes(sid))?.length||1):null,
      mmr_delta:number(row?.delta) ?? (number(before.rating)!==null&&number(after.rating)!==null?after.rating-before.rating:null),
      rr_delta:number(row?.rr?.delta),before,after,
      stats:{kills:number(stats.kills),deaths:number(stats.deaths),team_kills:number(stats.team_kills),ping:number(stats.ping),
        clutches:number(stats.clutches),damage:number(c.enemyDamage),damage_taken:number(c.damageTaken),assists:number(c.assists),
        headshots:number(c.headshots),accuracy:number(c.accuracy),combat:evidence(c)},
      coverage:{reported:stats.reported===true,performance:row?.valuation?.breakdown?.measured===true,damage:c.coverage?.damage===true,shots:c.coverage?.shots===true,objectives:c.coverage?.objectives===true},
      rating:row?{before,after,rr:{...numeric(row.rr),factors:numeric(row.rr?.factors)},
        valuation:{...numeric(row.valuation),breakdown:{...numeric(row.valuation?.breakdown),coverage:numeric(row.valuation?.breakdown?.coverage)}}}:null};
  });
  const score=evidence(r.score||full.score||{}), completed=rows.length>0||r.data_collected===true;
  const version=text(r.analytics_context?.hub||r.analytics_context?.deployment||'unknown');
  const rules=r.analytics_context?.rules || r.rules || {};
  // Context is written by the server from resolved constants, never client environment data.
  const config=canonical(JSON.parse(JSON.stringify(rules)));
  const out={schema:1,id,at,day:new Date(at).toISOString().slice(0,10),created:number(full.created),
    map:text(full.map)||'Unknown',mode:text(r.analytics_context?.mode)||'BB5',version,
    deployment:text(r.analytics_context?.deployment),scoring_version:text(r.version),rules_id:hash(config),rules:config,
    resolved_rules:Boolean(r.analytics_context?.rules),match_size:ids.length||number(full.size)||0,
    region:text(mm.region||r.analytics_context?.region)||'unknown',host:text(full.host||r.host,17),
    outcome:completed?(r.draw?'draw':'completed'):(text(full.outcome)||'unfinished'),reason:text(full.reason),
    winner:Number(r.winner||full.won_team)||null,score,completed,draw:r.draw===true,
    duration_seconds:number(full.created)!==null?Math.max(0,(at-full.created)/1000):null,
    quality:number(mm.quality),formation:evidence(mm),teams:evidence(teams),sides:evidence(full.sides||{}),bans:(full.bans||[]).map(b=>({team:b.team,map:text(b.map)})),
    source:r.analytics_context?.terminal?'terminal-archive':'canonical-settlement',formation_source:text(full.source)||'unknown',gameplay_source:board.length?'authenticated-host':'unavailable',players,
    rounds:evidence(full.rounds||r.inputs?.rounds||[]),round_details:evidence(full.round_details||[]),
    timeline:evidence(full.diag||[]),kills:evidence(r.inputs?.kills||full.kills||[]),
    combat_event_count:r.inputs?.combatState?.events?.length||0,combat_coverage:evidence(r.inputs?.combatState?.coverage||{}),
    rejected_count:r.inputs?.combatState?.rejected?.length||0,
    stats_series:evidence(r.inputs?.stats?.series||{}),
    coverage:{reported:players.filter(p=>p.coverage.reported).length,players:ids.length,damage:players.filter(p=>p.coverage.damage).length,ratings:rows.length},
    predicted_win:number(r.analytics_context?.predicted_win),duration_basis:'Formation to match end'};
  out.test_match=out.match_size!==10;
  return out;
}
function cohort(m){return pick(m,['map','mode','version','rules_id','match_size','region']);}
function contribution(m){
  const score=Object.values(m.score||{}).filter(v=>typeof v==='number');
  const n={matches:1,completed:m.completed?1:0,draws:m.draw?1:0,player_matches:m.players.length,
    reported:m.coverage.reported,damage_complete:m.coverage.damage,rated:m.coverage.ratings,
    rounds:score.reduce((a,b)=>a+b,0),duration_sum:m.completed?(m.duration_seconds||0):0,duration_count:m.completed&&m.duration_seconds!==null?1:0,
    quality_sum:m.quality||0,quality_count:m.quality===null?0:1,rr_sum:0,mmr_sum:0,rr_count:0,mmr_count:0,
    kills_sum:0,kills_count:0,deaths_sum:0,deaths_count:0,damage_sum:0,damage_count:0,
    decisive:score.length===2&&Math.abs(score[0]-score[1])>=5?1:0,team1_wins:m.winner===1?1:0,
    host_wins:0,host_matches:0,calibration_sum:0,calibration_count:0,
    wait_sum:m.formation.waited||0,wait_count:typeof m.formation.waited==='number'?1:0,
    spread_sum:m.formation.spread||0,spread_count:typeof m.formation.spread==='number'?1:0,
    cancelled:m.completed?0:1,rd_sum:0,rd_count:0};
  if(m.predicted_win!==null&&m.winner){n.calibration_sum=(m.predicted_win-(m.winner===1?1:0))**2;n.calibration_count=1;}
  for(const p of m.players){
    if(number(p.before.rd)!==null){n.rd_sum+=p.before.rd;n.rd_count++;}
    if(p.rr_delta!==null){n.rr_sum+=p.rr_delta;n.rr_count++;}if(p.mmr_delta!==null){n.mmr_sum+=p.mmr_delta;n.mmr_count++;}
    for(const k of ['kills','deaths','damage'])if(p.stats[k]!==null&&(k!=='damage'||p.coverage.damage)){n[`${k}_sum`]+=p.stats[k];n[`${k}_count`]++;}
    if(p.steam_id===m.host&&p.won!==null){n.host_matches++;n.host_wins+=p.won?1:0;}
    if(p.starting_side&&p.won!==null){const k=p.starting_side;n[k+'_count']=(n[k+'_count']||0)+1;n[k+'_wins']=(n[k+'_wins']||0)+(p.won?1:0);}
    const rank=number(p.before.matches)===null?'unknown':require('./rating.cjs').isPlacing(p.before)?'placing':String(require('./progress.cjs').rankOf(p.before.progress));
    n[`rank_${rank}_count`]=(n[`rank_${rank}_count`]||0)+1;
    n[`rank_${rank}_rr`]=(n[`rank_${rank}_rr`]||0)+(p.rr_delta||0);
    const party=p.party_size||'unknown';n[`party_${party}_count`]=(n[`party_${party}_count`]||0)+(p.won!==null?1:0);
    n[`party_${party}_wins`]=(n[`party_${party}_wins`]||0)+(p.won?1:0);
  }
  return n;
}
function add(a,b){for(const[k,v]of Object.entries(b))a[k]=(a[k]||0)+v;return a;}
function matches(m,f){return (!f.map||m.map===f.map)&&(!f.version||m.version===f.version)&&(!f.rules_id||m.rules_id===f.rules_id)&&(!f.region||m.region===f.region)&&(!f.mode||m.mode===f.mode)&&(f.size==='all'||m.match_size===f.size);}
function aggregate(buckets,f){
  const totals={matches:0,completed:0,player_matches:0},days={},maps={},versions={};
  for(const b of buckets){if(!matches(b.cohort,f))continue;add(totals,b.metrics);add(days[b.day]||=( {day:b.day}),b.metrics);add(maps[b.cohort.map]||={},b.metrics);add(versions[b.cohort.version]||={},b.metrics);}
  return {totals,days:Object.values(days).sort((a,b)=>a.day.localeCompare(b.day)),maps:Object.entries(maps).map(([name,metrics])=>({name,...metrics})),versions:Object.entries(versions).map(([name,metrics])=>({name,...metrics}))};
}
module.exports={DAY,number,text,hash,pick,numeric,evidence,ruleSnapshot,projectReceipt,cohort,contribution,aggregate,matches};
