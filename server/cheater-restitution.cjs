// Public result annotations and ordinary ban persistence.
// Hosted moderation and result-correction execution are not distributed here.
'use strict';
const crypto=require('node:crypto');
const {SET_BAN}=require('./ban-match.cjs');
function create({store,prefix='hub:',now=Date.now}={}){
 const base=prefix+'cheater:';
 const call=args=>{if(!store)throw Error('Persistent storage required');return store(args,{strict:true,timeout:5000});};
 const correctionKey=id=>base+'match:'+id;
 const unavailable=async()=>{throw Error('Hosted moderation is unavailable in this source build.');};
 async function setBan(actor,target,ban,matchKeys=[]){
  const action={at:now(),id:crypto.randomUUID(),by:actor,kind:ban?'ban':'unban'};
  const result=await call(['EVAL',SET_BAN,String(2+matchKeys.length),prefix+'ban:'+target,prefix+'ban-action:'+target,...matchKeys,JSON.stringify(action),ban?JSON.stringify(ban):'']);
  if(result!=='saved')throw Error('This decision has been superseded by a later ban or unban');
 }
 async function annotate(rows){
  if(!store||!rows.length)return rows;const records=rows.length===1?[await call(['GET',correctionKey(rows[0].id)])]:await call(['MGET',...rows.map(r=>correctionKey(r.id))]);
  if(!Array.isArray(records))throw Error('Match correction status unavailable');
  return rows.map((row,i)=>{if(!records[i])return row;const correction=JSON.parse(records[i]);return {...row,cheater_reverted:true,cheaters:correction.cheaters,reverted_at:correction.at,delta:0,rr_delta:0};});
 }
 return {begin:unavailable,correct:unavailable,statuses:unavailable,setBan,annotate,correctionKey,project:async()=>null,run:async()=>{}};
}
module.exports={create,SET_BAN};
