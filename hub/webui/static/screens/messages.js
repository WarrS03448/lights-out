(function(){'use strict';var current=null,lastPoll=-Infinity,lastIdentity=null,lastSent=null,readSent={},scrolls={},drafts={},draftRevisions={},renderedKey=null,renderedIdentity=null,renderedSignedIn=null;
 function sync(s){
  if(lastIdentity!==s.identity){readSent={};scrolls={};drafts={};draftRevisions={};lastSent=null;lastPoll=-Infinity;lastIdentity=s.identity;}
  if(s.sent&&lastSent!==s.sent.id){lastSent=s.sent.id;var key=s.identity+':'+s.sent.target;if((drafts[key]||'').trim()===s.sent.text){delete drafts[key];draftRevisions[key]=(draftRevisions[key]||0)+1;}}
 }
 function screenKey(state){return JSON.stringify([state.messages,(state.friends||{}).list]);}
 function update(root,state,ctx){var s=state.messages||{};
  if(s.identity!==renderedIdentity||s.signed_in!==renderedSignedIn)return false;
  if(screenKey(state)!==renderedKey)render(root,state,ctx,true);
  else if(s.signed_in&&s.target)readThread(s,ctx);
  return true;
 }
 function readThread(s,ctx){
  var rows=(s.thread||{}).messages||[],through=rows.length?rows[rows.length-1].seq:0,receipt=readSent[s.target];
  if(through&&(!receipt||receipt.through!==through||performance.now()-receipt.at>15000)&&!document.hidden){readSent[s.target]={through:through,at:performance.now()};ctx.call('messages_read',s.target,through);}
 }
 window.HubUI.registerScreen('messages',{render:render,update:update});
 window.HubUI.registerOverlay('messages-status',{render:function(state,ctx){current={state:state,ctx:ctx};var s=state.messages||{},button=document.getElementById('messages-toggle'),count=(s.data||{}).unread||0;
  sync(s);
  if(!button)return;button.hidden=!s.signed_in;button.setAttribute('aria-label',(s.strings||{}).title+' · '+count+' '+(s.strings||{}).unread);button.title=(s.strings||{}).title;
  button.classList.toggle('active',state.view==='messages');button.onclick=function(){ctx.call('set_view','messages');ctx.call('messages_refresh');};
  var badge=document.getElementById('messages-unread');badge.textContent=count>99?'99+':String(count);badge.hidden=!count;
 }});
 setInterval(function(){if(!current)return;var s=current.state.messages||{};if(s.signed_in&&!s.loading&&performance.now()-lastPoll>=15000){lastPoll=performance.now();current.ctx.call('messages_refresh');if(current.state.view==='messages'&&s.target&&!s.thread_loading)current.ctx.call('messages_open',s.target);}},1000);
 window.HubUI.openMessages=function(target){if(current){current.ctx.call('messages_open',target);current.ctx.call('set_view','messages');}};
 function render(root,state,ctx,patch){sync(state.messages||{});var s=state.messages||{},data=s.data||{},strings=s.strings||{},el=ctx.ui.el;function st(k){return strings[k]||k;}function button(text,fn){var b=el('button','mail-button',text);b.addEventListener('click',fn);return b;}
  renderedKey=screenKey(state);renderedIdentity=s.identity;renderedSignedIn=s.signed_in;
  if(patch&&!s.signed_in){root.replaceChildren();patch=false;}
  var wrap=el('div','mail');wrap.appendChild(el('h1','',st('title')));if(!s.signed_in){wrap.appendChild(el('p','',st('signin')));root.appendChild(wrap);return;}
  var grid=el('div','mail-grid'),inbox=el('aside','mail-inbox'),conversation=el('section','mail-conversation');
  inbox.appendChild(button(st('refresh'),function(){ctx.call('messages_refresh');}));
  var friends=(state.friends||{}).list||[],select=el('select','mail-select');select.setAttribute('aria-label',st('new_message'));var placeholder=el('option','',st('new_message'));placeholder.value='';select.appendChild(placeholder);
  friends.forEach(function(f){var o=el('option','',f.persona||f.name||f.player_id||f.steam_id);o.value=f.player_id||f.steam_id;select.appendChild(o);});select.addEventListener('change',function(){if(select.value)ctx.call('messages_open',select.value);select.value='';if(select.onblur)select.onblur();});inbox.appendChild(select);
  if(!(data.threads||[]).length)inbox.appendChild(el('p','mail-muted',st('empty')));
  (data.threads||[]).forEach(function(t){var b=button('',function(){ctx.call('messages_open',t.target);});b.className+=' mail-thread'+(t.target===s.target?' selected':'');b.appendChild(el('strong','',t.official?st('official'):t.persona));b.appendChild(el('span','mail-preview',t.last_text));if(t.unread)b.appendChild(el('span','mail-count',String(t.unread)));inbox.appendChild(b);});
  if(s.error)conversation.appendChild(el('p','mail-error',st(s.error)));
  if(!s.target)conversation.appendChild(el('p','mail-muted',st('select')));
  else {var thread=s.thread||{},summary=(data.threads||[]).find(function(t){return t.target===s.target;}),friend=friends.find(function(f){return (f.player_id||f.steam_id)===s.target;});
   conversation.appendChild(el('h2','',s.target==='admin'?st('official'):summary?summary.persona:friend?(friend.persona||friend.name):s.target));
   if(s.target==='admin')conversation.appendChild(el('p','mail-official',st('official_note')));
   else conversation.appendChild(button(st(thread.blocked?'unblock':'block'),function(){ctx.call('messages_block',s.target,!thread.blocked);}));
   var log=el('div','mail-log');log.id='mail-log';log.setAttribute('role','log');log.setAttribute('aria-label',st('title'));
   if(thread.next_before)log.appendChild(button(st('older'),function(){ctx.call('messages_open',s.target,thread.next_before);}));
   (thread.messages||[]).forEach(function(m){var row=el('div','mail-message'+(m.sender===s.identity?' mine':'')+(m.official?' official':''));row.appendChild(el('small','',m.official?st('official'):m.sender===s.identity?st('you'):summary?summary.persona:s.target));row.appendChild(el('p','',m.text));row.appendChild(el('time','',new Date(m.at).toLocaleString()));log.appendChild(row);});
   conversation.appendChild(log);
   if(!thread.blocked){var form=el('form','mail-compose'),input=el('textarea');var draftKey=s.identity+':'+s.target;input.id='mail-draft-'+s.identity+'-'+s.target+'-'+(draftRevisions[draftKey]||0);input.value=drafts[draftKey]||'';input.placeholder=st('compose');input.setAttribute('aria-label',st('compose'));input.maxLength=1000;input.required=true;input.disabled=!!s.sending;input.rows=3;input.addEventListener('input',function(){drafts[draftKey]=input.value;});var send=button(st('send'),function(){});send.type='submit';send.disabled=!!s.sending;form.appendChild(input);form.appendChild(send);form.addEventListener('submit',function(e){e.preventDefault();if(!input.value.trim())return;send.disabled=true;ctx.call('messages_send',input.value);});conversation.appendChild(form);}
   conversation.appendChild(el('p','mail-muted',st('retention')));
  }
  grid.appendChild(inbox);grid.appendChild(conversation);wrap.appendChild(grid);
  if(patch){
   // Keep the native friend picker attached, including while new mail arrives.
   // Moving it into a replacement tree would also close the browser's popup.
   var existing=root.querySelector('.mail'),oldInbox=existing.querySelector('.mail-inbox'),oldSelect=oldInbox.querySelector('select');
   existing.querySelector('h1').textContent=st('title');
   oldSelect.setAttribute('aria-label',st('new_message'));
   oldSelect.onblur=null;
   if(oldSelect.innerHTML!==select.innerHTML){
    var replaceOptions=function(){var value=oldSelect.value;oldSelect.replaceChildren.apply(oldSelect,Array.from(select.children));oldSelect.value=value;oldSelect.onblur=null;};
    if(document.activeElement===oldSelect)oldSelect.onblur=replaceOptions;else replaceOptions();
   }
   Array.from(oldInbox.children).forEach(function(n){if(n!==oldSelect)n.remove();});
   oldInbox.insertBefore(inbox.firstChild,oldSelect);
   Array.from(inbox.children).forEach(function(n){if(n!==select)oldInbox.appendChild(n);});
   existing.querySelector('.mail-conversation').replaceWith(conversation);wrap=existing;
  }else root.appendChild(wrap);
  var logNode=wrap.querySelector('.mail-log');if(logNode){logNode.scrollTop=scrolls[s.target]===undefined?logNode.scrollHeight:scrolls[s.target];logNode.addEventListener('scroll',function(){scrolls[s.target]=logNode.scrollTop;});
   readThread(s,ctx);
  }
 }
}());
