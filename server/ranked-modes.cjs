'use strict';
// Ranked identity is never inferred from translated UI text or roster size.
const MODES=Object.freeze({
  BB5:Object.freeze({id:'BB5',players:10,teamSize:5,maxParty:5,scoreLimit:7,maxRounds:13,
    switchInterval:6,roundSeconds:180,droneCooldown:4,fixedMap:null}),
  BB1:Object.freeze({id:'BB1',players:2,teamSize:1,maxParty:1,scoreLimit:7,maxRounds:13,
    switchInterval:1,roundSeconds:120,droneCooldown:3,fixedMap:'Paintball'}),
});
function modeOf(id){
  const key=id===undefined||id===null?'BB5':id;
  if(typeof key!=='string'||!Object.hasOwn(MODES,key))throw Error('Unknown ranked mode');
  return MODES[key];
}
function rankedPrefix(prefix,id){const mode=modeOf(id);return (prefix||'hub:')+(mode.id==='BB5'?'':'ranked:BB1:');}
function rulesOf(id){const m=modeOf(id);return {score_limit:m.scoreLimit,max_rounds:m.maxRounds,
  team_switch_interval:m.switchInterval,round_seconds:m.roundSeconds,time_limit:m.roundSeconds};}
module.exports={MODES,modeOf,rankedPrefix,rulesOf};
