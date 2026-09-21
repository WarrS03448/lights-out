// An unavailable provider must never imply that a player has been checked.
'use strict';
const POLICY=Object.freeze({available:false});
function score(){return {available:false,complete:false,policy:POLICY,status:null,score:null,enforcement:'none',contributions:[],note:'Hosted moderation is unavailable in this source build.'};}
module.exports={score,POLICY};
