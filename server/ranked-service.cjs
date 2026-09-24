'use strict';
// One process and identity service, two instances of the same ranked engine.
const live=require('./live.cjs'),identity=require('./player-identity.cjs'),modes=require('./ranked-modes.cjs');
function create(options){
  const sharedNetworkRegistry=new (require('./network.cjs').Registry)();
  const engines=new Map();let recovered=false;
  const guard={
    inParty(id){return engines.get('BB5')._internals.partyOf.has(id);},
    canChangeParty(ids){return !engines.get('BB1').activity().some(a=>ids.includes(a.player_id));},
    canEnter(mode,ids,games){
    if(!recovered)return false;
    if(mode==='BB1'){
      if(ids.some(id=>guard.inParty(id)))return false;
    }
    for(const [other,engine]of engines){
      if(other===mode)continue;
      if(engine.activity().some(a=>ids.includes(a.player_id)||games.includes(a.game_steam_id)))return false;
    }
    return true;
  }};
  // Presence is account-wide in the Players directory; ranks and admin actions stay mode-specific.
  function directoryPresence(id){
    let status='offline',online=false,queueMode='';
    for(const [mode,engine]of engines){
      const I=engine._internals;
      online=online||I.bySteam.has(id);
      if(I.inMatch.has(id))status='match';
      else if(status!=='match'&&I.queueOf.has(id)){status='queued';queueMode=mode;}
    }
    if(status==='offline'&&online)status='online';
    return {status,online,queue_mode:status==='queued'?queueMode:''};
  }
  for(const modeId of ['BB5','BB1'])engines.set(modeId,live.create({...options,modeId,competitionGuard:guard,sharedNetworkRegistry,directoryPresence,
    socialPrefix:options.prefix||'hub:',tournament:modeId==='BB5'?options.tournament:null,
    requiredVersions:()=>options.requiredVersions?.(modeId)||null,
    rankedRules:()=>options.rankedRules?.(modeId)||modes.rulesOf(modeId),
    expectedRules:()=>options.expectedRules?.(modeId)||modes.rulesOf(modeId)}));
  const base=engines.get('BB5');
  const forMode=id=>engines.get(modes.modeOf(id).id);
  async function ensureRecovery(){
    await Promise.all([...engines.values()].map(e=>e._internals.ensureRecovery()));recovered=true;
  }
  const ready=Promise.all([...engines.values()].map(e=>e._internals.ready)).then(ensureRecovery);
  ready.catch(()=>{});
  function activeEngine(player){
    const found=[...engines.values()].filter(e=>e.activity().some(a=>a.player_id===player||a.game_steam_id===player));
    if(found.length>1)throw Error('Conflicting ranked activity');
    return found[0]||base;
  }
  function matchEngine(id){
    const found=[...engines.values()].filter(e=>e._internals.matches.has(id)||e._internals.archived.has(id));
    if(found.length>1)throw Error('Conflicting match identity');
    return found[0];
  }
  async function authority(method,...args){
    await ensureRecovery();
    const found=(await Promise.all([...engines].map(async([mode,e])=>{
      const a=await e[method](...args);return a?{...a,mode}:null;
    }))).filter(Boolean);
    if(found.length>1)throw Error('Conflicting match authority');return found[0]||null;
  }
  function stampedRequest(req,id){
    const headers={...req.headers,'x-ranked-mode':id};
    const version=headers['x-'+id.toLowerCase()+'-version'];
    if(version!==undefined)headers['x-mode-version']=version;
    else if(id!==(req.headers?.['x-ranked-mode']||'BB5'))delete headers['x-mode-version'];
    return new Proxy(req,{get(target,key){if(key==='headers')return headers;
      const value=Reflect.get(target,key);return typeof value==='function'?value.bind(target):value;}});
  }
  function streamResponse(res,mode){
    return new Proxy(res,{get(target,key){
      if(key==='writeHead')return (...args)=>{if(!target.headersSent)return target.writeHead(...args);};
      if(key==='setHeader')return (...args)=>{if(!target.headersSent)return target.setHeader(...args);};
      if(key==='write')return value=>{
        if(typeof value==='string'&&value.startsWith('data: ')){
          const event=JSON.parse(value.slice(6));
          if(mode==='BB1'&&['party_update','party_invites','party_invite','friend_update','friend_request'].includes(event.type))return true;
          value='data: '+JSON.stringify({...event,mode})+'\n\n';
        }
        return target.write(value);
      };
      const value=Reflect.get(target,key);return typeof value==='function'?value.bind(target):value;
    }});
  }
  async function route(req,res,method,pathname,url){
    if(pathname==='/api/match/history'&&method==='GET'&&url?.searchParams.get('mode')==='all'){
      try{
        await ready;await ensureRecovery();
        const account=await options.whoami(options.bearer(req)),id=identity.playerOf(account);
        if(!identity.validPlayer(id)){options.sendJson(res,401,{ok:false});return true;}
        const wanted=url.searchParams.get('id');
        if(wanted){
          const found=(await Promise.all([...engines.values()].map(e=>e._internals.readMatch(wanted.slice(0,64),id)))).filter(Boolean);
          options.sendJson(res,found.length===1?200:404,identity.wire(found.length===1?{ok:true,match:found[0]}:{ok:false}));
        }else{
          const rows=(await Promise.all([...engines].map(async([mode,e])=>(await e._internals.readHistory(id)).map(r=>({...r,mode}))))).flat().sort((a,b)=>b.ended-a.ended).slice(0,100);
          options.sendJson(res,200,identity.wire({ok:true,matches:rows}));
        }
      }catch{options.sendJson(res,503,{ok:false,error:'History unavailable.'});}return true;
    }
    let selected;
    try{selected=modes.modeOf(url?.searchParams.get('mode')??req.headers?.['x-ranked-mode']).id;}
    catch{options.sendJson(res,400,{ok:false,error:'Unknown ranked mode.'});return true;}
    try{await ready;await ensureRecovery();}
    catch{options.sendJson(res,503,{ok:false,error:'Ranked state is recovering.'});return true;}
    if(pathname==='/api/ranked/profile'&&method==='GET'){
      const account=await options.whoami(options.bearer(req));const id=identity.playerOf(account);
      if(!identity.validPlayer(id)){options.sendJson(res,401,{ok:false});return true;}
      try{
        const ranks=Object.fromEntries(await Promise.all([...engines].map(async([mode,e])=>[mode,await e.publicRank(id)])));
        options.sendJson(res,200,{ok:true,ranks});
      }catch{options.sendJson(res,503,{ok:false,error:'Ranks unavailable.'});}return true;
    }
    if(pathname==='/api/live'&&method==='GET'){
      // Both channels share one authenticated HTTP connection. Mode-tagged events let the
      // client retain both ranks while displaying only the selected queue's match state.
      for(const [id,e]of engines){
        await e.route(stampedRequest(req,id),streamResponse(res,id),method,pathname,url);
        if(res.writableEnded)break;
      }
      return true;
    }
    const shared=/^\/api\/(friends|party|messages|tournament)(\/|$)/.test(pathname)||pathname==='/api/bug';
    if(pathname.startsWith('/api/match/')&&pathname!=='/api/match/history'){
      const account=await options.whoami(options.bearer(req)),player=identity.playerOf(account);
      if(identity.validPlayer(player)){
        const active=[...engines.values()].find(e=>e.activity().some(a=>a.player_id===player));
        if(active)return active.route(stampedRequest(req,active.mode),res,method,pathname,url);
      }
    }
    return forMode(shared?'BB5':selected).route(stampedRequest(req,shared?'BB5':selected),res,method,pathname,url);
  }
  const api={forMode,route,
    _internals:{...base._internals,ready,ensureRecovery,engines},
    async shutdown(){await Promise.all([...engines.values()].map(e=>e.shutdown()));},
    broadcastStats(){for(const e of engines.values())e.broadcastStats();},
    async prepareOwnership(ids){await ensureRecovery();for(const e of engines.values())await e.prepareOwnership(ids);},
    ownershipChanged(change){for(const e of engines.values())e.ownershipChanged(change);},
    async correctCheaterMatches(receipt){return forMode(receipt.mode||receipt.publicMatch?.mode).correctCheaterMatches(receipt);},
    async correctionJobs(){return (await Promise.all([...engines].map(async([mode,e])=>(await e.correctionJobs()).map(row=>({...row,mode}))))).flat();},
    authoriseReport(token){
      const found=[...engines].map(([mode,e])=>{const a=e.authoriseReport(token);return a?{...a,mode}:null;}).filter(Boolean);
      return found.length===1?found[0]:null;
    },
    authoriseReportFresh:(...args)=>authority('authoriseReportFresh',...args),
    authoriseFinalReport:(...args)=>authority('authoriseFinalReport',...args),
    withReportAuthority:(auth,operation)=>forMode(auth.mode).withReportAuthority(auth,operation),
    async migrationReport(token,fields){await ensureRecovery();const e=matchEngine(fields?.match_id);return e?e.migrationReport(token,fields):{ok:false,error:'No match.'};},
  };
  for(const name of ['gameReportedIn','teamRuling','startReady','matchPresence','finalSnapshot','combatBatch',
    'gameReportedTeam','teamKillReported','gameReportedCombat','gameReportedScore','gameReportedStats',
    'gameReportedRound','gameReportedKill','grantHostPermit','takeHostPermit','noteHostLaunching',
    'revokeHostPermit','takeJoinPermit','revokeJoinPermit','authoriseLegacyReport']){
    api[name]=(player,...args)=>activeEngine(player)[name](player,...args);
  }
  return new Proxy(api,{get(target,key){if(key in target)return target[key];
    const value=base[key];return typeof value==='function'?value.bind(base):value;}});
}
module.exports={create};
