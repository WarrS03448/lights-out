(function () {
  'use strict';
  var timer = null, lastPoll = -Infinity, scrollTop = 0, draftIdentity = null, draftSeq = null, draft = {}, ledgerOpen = false;
  window.HubUI.registerScreen('tournament', {render: render});
  function render(root, state, ctx) {
    if (timer !== null) clearInterval(timer);
    var s = state.tournament || {}, strings = s.strings || {}, data = s.data || {};
    if(draftIdentity!==s.identity){draftIdentity=s.identity;draftSeq=null;draft={};ledgerOpen=false;scrollTop=0;lastPoll=-Infinity;}
    if(draftSeq!==s.ticket_seq){draftSeq=s.ticket_seq;draft={};}
    var event = data.event || {start_at: 1790438400000, end_at: 1790611200000, prize_pool:500, prizes: [250,125,75,37,13]};
    var el = ctx.ui.el, anchor = performance.now(), serverNow = data.server_now || Date.now();
    function st(key) { return strings[key] || key; }
    function money(amount) { return '$'+Number(amount).toFixed(Number.isInteger(Number(amount))?0:2)+' USD'; }
    function node(tag, cls, text, id) { var n = el(tag, cls, text); if (id) n.id = id; return n; }
    var wrap = node('div', 'tournament');
    wrap.addEventListener('scroll',function(){scrollTop=wrap.scrollTop;});
    var hero = node('section', 'tournament-hero');
    var intro = node('div', 'tournament-intro');
    intro.appendChild(node('div', 'tournament-eyebrow', 'LIGHTS OUT'));
    intro.appendChild(node('h1', '', st('title')));
    intro.appendChild(node('p', 'tournament-subtitle', st('subtitle')));
    var status = node('span', 'tournament-status', '', 'tournament-phase');intro.appendChild(status);
    hero.appendChild(intro);
    var clock = node('div', 'tournament-clock');
    var clockLabel = node('div', 'tournament-clock-label', '', 'tournament-clock-label');
    var digits = node('div', 'tournament-digits', '', 'tournament-countdown');
    clock.appendChild(clockLabel); clock.appendChild(digits);clock.appendChild(node('div','tournament-clock-units',st('clock_units')));hero.appendChild(clock);wrap.appendChild(hero);
    var winners=node('section','tournament-card tournament-winners',null,'tournament-winners');
    winners.appendChild(node('h2','',st('winners')));winners.appendChild(node('p','tournament-muted',st(data.results_provisional?'review':'confirmed')));
    var winnerList=node('div','tournament-winner-list');
    (data.winners||[]).forEach(function(p){var card=node('div','tournament-winner');card.appendChild(node('span','tournament-muted',st('place')+' '+p.rank));card.appendChild(node('strong','tournament-winner-name',p.persona));card.appendChild(node('span','tournament-winner-prize',p.prize_usd==null?st('prize_review'):money(p.prize_usd)));card.appendChild(node('span','tournament-muted',p.net_rr+' RR'));winnerList.appendChild(card);});
    if(!(data.winners||[]).length)winnerList.appendChild(node('p','tournament-muted',!s.data||s.error?st('unavailable'):st('empty')));
    winners.appendChild(winnerList);wrap.appendChild(winners);
    var grid = node('div', 'tournament-grid'), details = node('section', 'tournament-card');
    details.appendChild(node('h2', '', st('schedule')));
    var format = new Intl.DateTimeFormat(s.language || 'en', {timeZone:'America/Chicago',year:'numeric',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
    details.appendChild(node('p', 'tournament-dates', format.format(event.start_at) + ' → ' + format.format(event.end_at)));
    details.appendChild(node('p', 'tournament-muted', st('timezone')));
    if(event.extension_ms)details.appendChild(node('p','tournament-registered',st('extension').replace('{count}',Math.round(event.extension_ms/60000))));
    if(data.announcement)details.appendChild(node('p','tournament-agreement',data.announcement));
    details.appendChild(node('p', 'tournament-rules', st('rules')));
    details.appendChild(node('p', 'tournament-agreement', st('payout_agreement'), 'tournament-payout-agreement'));
    var register = node('button', 'tournament-register', st('register'), 'tournament-register');
    register.addEventListener('click', function () { register.disabled = true; ctx.call('tournament_register'); });details.appendChild(register);
    var registered = node('p', 'tournament-registered', data.registered_at ? st('registered') + ' · ' + format.format(data.registered_at) : st('signin'));
    details.appendChild(registered);
    if(data.entrant_count!==undefined)details.appendChild(node('p','tournament-muted',st('entrants').replace('{count}',data.entrant_count)));
    var note = node('p', 'tournament-error', '', 'tournament-error');note.setAttribute('role','status');
    if (s.error) note.textContent = st(s.error) + (s.data ? ' ' + st('stale') : '');
    details.appendChild(note);grid.appendChild(details);
    var prizes = node('section', 'tournament-card tournament-prizes');prizes.appendChild(node('h2', '', st('prizes')));
    prizes.appendChild(node('div', 'tournament-pool', money(event.prize_pool)));
    event.prizes.forEach(function (amount,i) {var row=node('div','tournament-prize');row.appendChild(node('span','',String(i+1).padStart(2,'0')));row.appendChild(node('strong','',money(amount)));prizes.appendChild(row);});
    prizes.appendChild(node('p','tournament-muted',st('payout')));grid.appendChild(prizes);wrap.appendChild(grid);
    var personal=node('section','tournament-card tournament-personal',null,'tournament-you');personal.appendChild(node('h2','',st('your_score')));
    if(data.payout){personal.appendChild(node('p','tournament-agreement',st('payout_status')+': '+st(data.payout.status)+(data.payout.method?' · '+data.payout.method:'')));if(data.payout.due_at)personal.appendChild(node('p','tournament-muted',st('payment_due')+': '+format.format(data.payout.due_at)));}
    (data.badges||[]).forEach(function(b){personal.appendChild(node('span','tournament-badge',st(b.type)+(b.rank?' #'+b.rank:'')));});
    if(data.disqualified)personal.appendChild(node('p','tournament-error',st('disqualified')+': '+data.exclusion_reason));
    else if(data.registered_at&&data.matches_needed)personal.appendChild(node('p','tournament-muted',st('qualification').replace('{count}',data.matches_needed)));
    if(data.gap_to_fifth>0)personal.appendChild(node('p','tournament-muted',st('gap').replace('{count}',data.gap_to_fifth)));
    var stats=node('div','tournament-stats'),you=data.you;
    ['place','net','gained','lost','wins','matches'].forEach(function(key){var field={place:'rank',net:'net_rr',gained:'gained_rr',lost:'lost_rr',wins:'wins',matches:'matches'}[key],value=you?you[field]:(data.registered_at&&key!=='place'?0:'—');var tile=node('div','');tile.appendChild(node('span','tournament-muted',st(key)));tile.appendChild(node('strong','',String(value==null?'—':value)));stats.appendChild(tile);});personal.appendChild(stats);wrap.appendChild(personal);
    var board=node('section','tournament-card tournament-board');var head=node('div','tournament-board-head');var boardTitle=node('h2','',st('leaders'));head.appendChild(boardTitle);
    var refresh=node('button','tournament-button',st('refresh'));refresh.disabled=!!s.loading||!s.signed_in;refresh.addEventListener('click',function(){lastPoll=performance.now();ctx.call('tournament_refresh');});head.appendChild(refresh);board.appendChild(head);
    var review=node('p','tournament-muted',st('review'),'tournament-review');board.appendChild(review);
    var empty=node('p','tournament-muted','','tournament-empty');board.appendChild(empty);
    var table=node('table','','','tournament-leaders'),thead=node('thead'),tr=node('tr');
    ['place','player','net','wins','matches'].forEach(function(key){tr.appendChild(node('th','',st(key)));});thead.appendChild(tr);table.appendChild(thead);var tbody=node('tbody');
    (data.leaders||[]).slice(0,10).forEach(function(p){var row=node('tr',p.is_you?'tournament-you-row':'');[p.rank,p.persona,p.net_rr,p.wins,p.eligible===false?p.matches+' / '+event.minimum_matches:p.matches].forEach(function(value){row.appendChild(node('td','',String(value)));});tbody.appendChild(row);});table.appendChild(tbody);board.appendChild(table);wrap.appendChild(board);
    var ledger=node('details','tournament-card');ledger.open=ledgerOpen;ledger.addEventListener('toggle',function(){if(ledger.isConnected)ledgerOpen=ledger.open;});ledger.appendChild(node('summary','',st('ledger')));
    (data.history||[]).slice().reverse().forEach(function(h){ledger.appendChild(node('p','tournament-ledger-row',format.format(h.ended)+' · '+h.match_id+' · '+h.delta+' RR · '+(h.counted?st('counted'):st('excluded')+': '+st(h.reason))));});wrap.appendChild(ledger);
    var policies=node('section','tournament-card');['policies','fair_play','outage_policy'].forEach(function(k){policies.appendChild(node('p','tournament-rules',st(k)));});
    (data.outages||[]).forEach(function(o){policies.appendChild(node('p','tournament-muted',st('outage')+': '+format.format(o.start)+' → '+format.format(o.end)));});
    if(event.dispute_deadline)policies.appendChild(node('p','tournament-muted',st('support')+' · '+format.format(event.dispute_deadline)+' · '+st('timezone')));
    var share=node('input','tournament-share');share.readOnly=true;share.value='https://lightsoutranked.com/tournament';share.setAttribute('aria-label',st('share'));share.addEventListener('click',function(){share.select();});policies.appendChild(share);wrap.appendChild(policies);
    var support=node('section','tournament-card',null,'tournament-support');support.appendChild(node('h2','',st('support')));
    if(s.ticket_seq)support.appendChild(node('p','tournament-registered',st('ticket_sent')));
    if(data.registered_at){
      var form=node('form','tournament-support-form'),category=node('select'),reference=node('input'),message=node('textarea');
      category.id='tournament-category';category.setAttribute('aria-label',st('category'));
      ['missing_rr','fair_play','appeal','outage','other'].forEach(function(k){var option=node('option','',st(k==='fair_play'?'fair_play_report':k));option.value=k;category.appendChild(option);});
      category.value=draft.category||'missing_rr';category.addEventListener('change',function(){draft.category=category.value;});
      reference.id='tournament-match-'+s.identity+'-'+(s.ticket_seq||0);reference.placeholder=st('match_id');reference.setAttribute('aria-label',st('match_id'));reference.maxLength=80;reference.value=draft.reference||'';reference.addEventListener('input',function(){draft.reference=reference.value;});
      message.id='tournament-message-'+s.identity+'-'+(s.ticket_seq||0);message.placeholder=st('message');message.setAttribute('aria-label',st('message'));message.maxLength=1000;message.minLength=5;message.required=true;message.rows=3;message.value=draft.message||'';message.addEventListener('input',function(){draft.message=message.value;});
      var send=node('button','tournament-button',st('send'));send.type='submit';send.disabled=!!s.loading||serverNow>=event.dispute_deadline;
      [category,reference,message,send].forEach(function(n){form.appendChild(n);});form.addEventListener('submit',function(e){e.preventDefault();send.disabled=true;ctx.call('tournament_support',category.value,reference.value,message.value);});support.appendChild(form);
    }
    (data.tickets||[]).slice().reverse().forEach(function(ticket){var item=node('div','tournament-ticket');item.appendChild(node('strong','',st(ticket.status)+' · '+format.format(ticket.at)));item.appendChild(node('p','',ticket.message));(ticket.replies||[]).forEach(function(r){item.appendChild(node('p','tournament-registered',r.message));});support.appendChild(item);});wrap.appendChild(support);
    root.appendChild(wrap);wrap.scrollTop=scrollTop;
    var previousPhase=null;
    function tick() {
      if (!wrap.isConnected) {clearInterval(timer);timer=null;lastPoll=-Infinity;return;}
      var now=serverNow+performance.now()-anchor, phase=now<event.start_at?'scheduled':now<event.end_at?'live':'ended';
      var left=Math.max(0,Math.ceil(((phase==='scheduled'?event.start_at:event.end_at)-now)/1000));
      var days=Math.floor(left/86400),hours=Math.floor(left%86400/3600),minutes=Math.floor(left%3600/60),seconds=left%60;
      // DD:HH:MM:SS has a stable width and is read by screen readers as one labelled timer.
      digits.textContent=[days,hours,minutes,seconds].map(function(v){return String(v).padStart(2,'0');}).join(':');
      clockLabel.textContent=st(phase==='scheduled'?'starts_in':phase==='live'?'remaining':'ended');status.textContent=st(phase);status.dataset.phase=phase;
      clock.hidden=phase==='ended';review.hidden=phase!=='ended'||!data.results_provisional;
      winners.hidden=phase!=='ended';boardTitle.textContent=st(phase==='ended'?'final_standings':'leaders');
      register.hidden=!!data.registered_at||phase==='ended'||!s.signed_in;register.disabled=!!s.loading;
      registered.hidden=!data.registered_at&&s.signed_in;
      table.hidden=phase==='scheduled'||!(data.leaders||[]).length;
      empty.hidden=!table.hidden;empty.textContent=phase==='scheduled'?st('upcoming'):!s.signed_in?st('signin'):!s.data||s.error?st('unavailable'):st('empty');
      if(s.signed_in&&!s.loading&&(performance.now()-lastPoll>=15000||previousPhase!==null&&previousPhase!==phase)) {lastPoll=performance.now();ctx.call('tournament_refresh');}
      previousPhase=phase;
    }
    tick();timer=setInterval(tick,1000);
  }
}());
