(function(){'use strict';const $=id=>document.getElementById(id);let data=null,anchor=performance.now(),clock=Date.now(),lastPhase='';
const fallback={start_at:1790438400000,end_at:1790611200000};
const money=amount=>'$'+Number(amount).toFixed(Number.isInteger(Number(amount))?0:2)+' USD';
const date=at=>new Intl.DateTimeFormat('en-US',{timeZone:'America/Chicago',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZoneName:'short'}).format(at);
function add(tag,text,parent){const el=document.createElement(tag);el.textContent=text;parent.appendChild(el);return el;}
function draw(){const event=data.event;$('event-schedule').textContent=date(event.start_at)+' → '+date(event.end_at);$('event-extension').textContent=event.extension_ms?Math.round(event.extension_ms/60000)+' minutes added for service issues.':'';
 $('event-outages').replaceChildren();(data.outages||[]).forEach(o=>add('li',date(o.start)+' → '+date(o.end),$('event-outages')));
 $('dispute-deadline').textContent='Disputes close '+date(event.dispute_deadline)+'. Winners are confirmed after review.';
 $('standings').replaceChildren();(data.leaders||[]).forEach(p=>{const row=add('tr','',$('standings'));[p.rank,p.persona,p.net_rr,p.wins,p.eligible===false?p.matches+' / '+event.minimum_matches:p.matches].forEach(v=>add('td',String(v),row));});
 $('standings-note').textContent=data.phase==='scheduled'?'Standings appear when the event starts.':data.leaders.length?'Five completed eligible matches required for a prize.':'No completed results yet.';
 $('winners-list').replaceChildren();(data.winners||[]).forEach(p=>{const row=add('div','',$('winners-list'));row.className='event-winner';add('span','Place '+p.rank,row);add('b',p.persona,row);add('strong',p.prize_usd==null?'Prize under review':money(p.prize_usd),row);add('span',p.net_rr+' RR',row);});
 $('winners-note').textContent=data.results_provisional?'Provisional results — under review.':'Winners confirmed.';$('event-notice').textContent=data.announcement||'';tick();}
async function load(){try{const response=await fetch('/api/public/tournament',{cache:'no-store'}),next=await response.json();if(!response.ok||!next.ok)throw Error();data=next;anchor=performance.now();clock=data.server_now;draw();}catch{$('event-notice').textContent='Live event data is temporarily unavailable. Any displayed standings are from the last update.';}}
function tick(){const now=clock+performance.now()-anchor,event=data?.event||fallback,phase=now<event.start_at?'scheduled':now<event.end_at?'live':'ended',left=Math.max(0,Math.ceil(((phase==='scheduled'?event.start_at:event.end_at)-now)/1000));
 $('clock').textContent=[Math.floor(left/86400),Math.floor(left%86400/3600),Math.floor(left%3600/60),left%60].map(n=>String(n).padStart(2,'0')).join(':');$('clock-label').textContent=phase==='scheduled'?'Starts in':phase==='live'?'Time remaining':'Event ended';$('event-phase').textContent=phase==='live'?'LIVE':phase==='scheduled'?'Upcoming':'Event ended';$('event-winners').hidden=phase!=='ended';$('standings-title').textContent=phase==='ended'?'Final standings':'Top 10';if(lastPhase&&lastPhase!==phase)load();lastPhase=phase;}
tick();load();setInterval(tick,1000);setInterval(load,15000);
}());
