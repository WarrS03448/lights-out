'use strict';
// Used only by the validated private account-test deployment. Native bots have
// no platform ID; they are never converted into player accounts or rating rows.
function humans(rows,host) {
  if(!Array.isArray(rows)||rows.length!==10)return null;
  const people=rows.filter(r=>r.steam_id!=='');
  if(people.length!==1||people[0].steam_id!==host)return null;
  if(rows.some(r=>![0,1].includes(r.team)||r.active!==1))return null;
  if(!rows.some(r=>r.steam_id===''&&r.team!==people[0].team))return null;
  return people;
}
function finalHumans(lines,host) {
  if(lines.length!==10)return null;
  const parsed=lines.map(line=>/^(\d{17}|)\|k=(-?\d{1,5});d=(\d{1,5});sp=(\d{1,5});t=([01]);s=(-?\d{1,5});a=(true|false)$/i.exec(line));
  if(parsed.some(row=>!row))return null;
  const people=parsed.filter(row=>row[1]!=='');
  if(people.length!==1||people[0][1]!==host||!parsed.some(row=>!row[1]&&row[5]!==people[0][5]))return null;
  return [people[0][0]];
}
module.exports={humans,finalHumans};
