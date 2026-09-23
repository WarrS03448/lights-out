'use strict';
// A preference only: never blocks the only available opponent or changes RR/MMR.
const WINDOW=30*60*1000;
function recent(rows,opponent,now){return (rows||[]).filter(r=>r.outcome==='played'&&r.ended>now-WINDOW&&r.opponents?.includes(opponent));}
function cost(units,now){
 const ids=units.flatMap(u=>u.members||[]);if(ids.length!==2)return 0;
 const repeats=Math.max(...units.map(u=>recent(u.recent,ids.find(id=>!u.members.includes(id)),now).length));
 const waited=Math.max(0,Math.min(...units.map(u=>(now-u.joined)/1000)));
 return Math.min(repeats,3)*60*Math.max(0,1-waited/300);
}
// Hosted moderation review is unavailable in the public adapter.
function review(){return false;}
module.exports={cost,review};
