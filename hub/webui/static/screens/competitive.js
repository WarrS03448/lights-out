/* Competitive screen (Find match + Party) — OWNED BY THE COMPETITIVE SCREEN.
 *
 * This is the ONLY JS file the competitive screen agent edits. It registers itself with the core
 * and renders into the root it is handed. It draws with the shared helpers (ctx.ui) and the shared
 * tokens/components; its own visual rules live in screens/competitive.css. Ported verbatim from the
 * pre-modular app.js so the screen renders and functions identically.
 *
 * Verbs it calls (sign_in, open_link_again, find_match, cancel_search, accept, create_party,
 * join_party, leave_party, refresh_party_code, toggle_hide_code, open_invites, invite_friend,
 * invite_accept, invite_decline, and the match-flow verbs
 * pick_coin, choose, ban_map, send_chat, launch_game, relaunch_game, start_vote, cast_vote, finish_match,
 * leave_match) are registered on the bridge by hub/webui/screens/competitive.py — the Python half
 * this screen owns. */
(function () {
  "use strict";

  window.HubUI.registerScreen("competitive", { render: render, update: update });
  var latestModeContext = null;
  function modeBar(state, ctx) {
    latestModeContext = {state:state, ctx:ctx};
    var bar=ctx.ui.el("div","ranked-mode-bar");
    ["BB5","BB1"].forEach(function (mode) {
      var button=ctx.ui.el("button","ranked-mode-choice",mode==="BB5"?"5v5 Bodybomb":"1v1 Bodybomb (Paintball)");
      button.type="button";button.dataset.mode=mode;
      button.addEventListener("click",function(){if(latestModeContext)latestModeContext.ctx.call("select_ranked_mode",mode);});
      bar.appendChild(button);
    });
    syncModeBar(bar,state);return bar;
  }
  function syncModeBar(bar,state) {
    Array.from(bar.children).forEach(function(button){
      var active=button.dataset.mode===((state.comp||{}).mode_id||"BB5");
      button.classList.toggle("active",active);button.setAttribute("aria-pressed",String(active));
      button.disabled=(state.comp||{}).mode_selectable===false;
    });
  }
  function refreshKey(state) {
    var a=state.auth||{},c=state.comp||{};
    return JSON.stringify([a.signed_in,a.player_id||a.steam_id||"",state.lang,c.phase,c.match_id,
      (c.lobby||{}).match_id,(c.connect||{}).match_id,a.account_step]);
  }
  var retained = ".ranked-mode-bar,.comp-network-row,.chat-row,.join-row,.account-field,.account-remember,.ranked-history-modes";
  function nodeKey(n) { return n.nodeType===1 ? n.tagName+":"+(n.id||n.classList[0]||"") : "#text"; }
  function syncAttrs(oldNode,newNode) {
    Array.from(oldNode.attributes).forEach(function(a){if(!newNode.hasAttribute(a.name))oldNode.removeAttribute(a.name);});
    Array.from(newNode.attributes).forEach(function(a){if(oldNode.getAttribute(a.name)!==a.value)oldNode.setAttribute(a.name,a.value);});
  }
  // Retain controls together with their closures and every ancestor. Moving even a
  // retained select out of its parent closes its native popup on WebView2.
  function patchChildren(parent,draft) {
    var unused=Array.from(parent.childNodes),cursor=parent.firstChild;
    Array.from(draft.childNodes).forEach(function(next){
      var old=unused.find(function(n){return nodeKey(n)===nodeKey(next);});
      var keep=old&&old.nodeType===1&&(old.matches(retained)||old.querySelector(retained)||old.matches(".chat-log,.lobby-top"));
      if(keep){
        unused.splice(unused.indexOf(old),1);
        while(cursor&&cursor!==old){var after=cursor.nextSibling;if(unused.includes(cursor)){unused.splice(unused.indexOf(cursor),1);cursor.remove();}cursor=after;}
        syncAttrs(old,next);
        if(old.matches(".account-form"))old.onsubmit=next.onsubmit;
        if(old.matches(".ranked-mode-bar"))syncModeBar(old,latestModeContext.state);
        else if(old.matches(".ranked-history-modes")){
          Array.from(old.children).forEach(function(button,i){if(next.children[i])syncAttrs(button,next.children[i]);});
        }else if(old.matches(".comp-network-row")){
          var a=old.querySelector("select"),b=next.querySelector("select");a.disabled=b.disabled;a.title=b.title;
          if(document.activeElement!==a){if(a.innerHTML!==b.innerHTML)a.innerHTML=b.innerHTML;a.value=b.value;}
          var check=old.querySelector("input"),fresh=next.querySelector("input");check.checked=fresh.checked;check.disabled=fresh.disabled;
        }else if(!old.matches(retained)){
          var top=old.scrollTop,left=old.scrollLeft,atBottom=old.scrollHeight-old.clientHeight-top<8;
          patchChildren(old,next);old.scrollTop=old.matches(".chat-log")&&atBottom?old.scrollHeight:top;old.scrollLeft=left;
        }
        cursor=old.nextSibling;
      }else{parent.insertBefore(next,cursor);}
    });
    unused.forEach(function(n){n.remove();});
  }
  window.HubUI.patchRankedChildren=patchChildren;
  function update(root,state,ctx) {
    if(root._refreshKey!==refreshKey(state)||!root.querySelector(".ranked-mode-bar"))return false;
    var draft=document.createElement("div");render(draft,state,ctx,root);
    patchChildren(root,draft);return true;
  }

  // The no-show ban ticks down in the browser between the 300ms /state polls: Python only rebuilds
  // the snapshot at expiry, so without this the clock would look frozen until something else
  // re-rendered. Module-scoped so it survives each render's DOM teardown; cleared and re-armed on
  // every render so the periodic re-renders never stack timers, and it also self-clears once its
  // clock element leaves the DOM (navigating away, or any rebuild).
  var banTimer = null;
  function clearBanTimer() { if (banTimer) { clearInterval(banTimer); banTimer = null; } }

  // The hub closes Bodycam once it has registered the match as complete, because the game has to be
  // shut before the player can queue again. "armed" is deliberately absent: it draws nothing, so the
  // scoreboard is not sharing the screen with a countdown to a shutdown.
  //
  // MODULE SCOPE, AND THAT IS THE WHOLE FIX. This used to sit inside render(), BELOW the call that
  // reads it. `var` hoists the declaration but not the assignment, so by the time resultAction ran
  // - same call to render(), hundreds of lines earlier - CLOSE_LINES was still undefined, and
  // indexing undefined throws whatever the key is. The result screen therefore threw on EVERY
  // render, the exception escaped renderScreen, and the hub painted nothing: Sam pressed "end match
  // (preview)" and got a black window (2026-09-16, shipped in 2.0.13).
  //
  //     TypeError: Cannot read properties of undefined (reading 'armed')
  //         at resultAction (screens/competitive.js:534:32)
  //
  // Up here it is assigned once, before anything can run, and it is a constant table that never
  // depended on render state in the first place.
  var CLOSE_LINES = {
    closing: ["comp_game_closing", "muted"],
    closed: ["comp_game_closed", "muted"],
    forced: ["comp_game_closed", "muted"],
    failed: ["comp_game_close_failed", "warn"]
  };

  // Is the invite picker open? Module-scoped for the same reason banTimer is: this file's DOM is
  // torn down and rebuilt on every /state poll that changes anything, so a flag kept inside
  // render() would close the picker the moment a friend came online. It is view-only state and
  // deliberately NOT in the snapshot - Python has no business knowing which list is unfolded.
  var invitesOpen = false;
  var accountForm = "", accountDraft = {};
  var rankGuideOpen = false;
  var rankGuideFresh = false;
  var rankGuideIdentity = null;
  var rankGuidePhase = null;

  // KEEPING A CSS ANIMATION ALIVE ACROSS A RE-RENDER.
  //
  // render() rebuilds the whole screen - app.innerHTML = "" - every time the snapshot changes, and
  // while queueing the timer changes it once a SECOND. So an animated element here is a brand new
  // element once a second, and a CSS animation on a new element starts again from zero. Measured in
  // a browser 2026-09-15: the search bar's <i> was replaced every ~960 ms against a 1400 ms sweep,
  // so the bar got about 69% of the way across and snapped back to the left, every second, forever.
  // Sam: "it isn't making it all the way through the line before it resets, it looks sloppy."
  //
  // A NEGATIVE animation-delay starts an animation already part-way through. Anchored to the wall
  // clock rather than to render time, the replacement picks up exactly where its predecessor was,
  // and it does not matter when, how often or how unevenly the rebuild happens. Nothing has to
  // remember the old element, which is why this is done here rather than by teaching render() to
  // preserve nodes.
  //
  // MODULE SCOPE, for the reason CLOSE_LINES above is: these first lived just above searchBox,
  // which is INSIDE render() and hundreds of lines BELOW the heroAction call that reaches it. `var`
  // hoists the declaration but not the assignment, so SWEEP_MS was still undefined at the only
  // moment it was read, `Date.now() % undefined` is NaN, and the browser DISCARDS an invalid
  // animation-delay without a word. The bar kept resetting and the element had no style attribute
  // at all - the fix appeared to do nothing while running perfectly.
  //
  // Each duration MUST match its @keyframes rule in competitive.css; test_screen_matchflow.py
  // ::test_the_animation_durations_match_the_css reads both files and fails if they drift.
  var SWEEP_MS = 1400;        // .search-bar > i  ->  hubslide 1.4s
  var FLIP_MS = 700;          // .coin.flip       ->  hubflip .7s

  // THE TOSS IS SLOWER AND IT LANDS (Sam, 2026-09-16: "let the coinflip also flip slower and take
  // slightly more time... i want the users to actually see what the coin lands on").
  //
  // Two things were wrong with it. It turned end over end every .28s, which is fast enough that
  // both faces are a blur and the stop is a jump-cut; and it only ran on the ONE client whose
  // captain pressed the button, because the server went straight from the call to the result.
  // `flipping` is a real server stage now (live.cjs FLIP_SECONDS), so all ten watch the same coin
  // for the same window and see it come down on the same face.
  //
  // The landing is read off the stage clock rather than a timer of our own: this screen is
  // rebuilt about once a second, so any setTimeout here would be thrown away and re-armed on
  // every rebuild and the coin would never actually land. LAND_AT is in SECONDS of that clock -
  // at or below it the spin stops and the real face is shown, which gives the last second and a
  // bit of the window to actually read it.
  var LAND_AT = 1;

  function heldPhase(ms) {
    return "-" + (Date.now() % ms) + "ms";
  }

  // ---------------------------------------------------------------- scrolling across a rebuild
  // A SCROLL POSITION DOES NOT SURVIVE render() ON ITS OWN, and that is the larger half of the
  // lobby scrolling bug Sam and a friend hit. core.js empties #app and draws the screen again
  // on every snapshot that differs from the last one - and during a veto the ban clock differs
  // every second - so a scroll container built by this render starts at scrollTop 0 no matter
  // where the player had dragged it a moment ago. Scrolling down to the chat was not slow, it
  // was impossible: the pane snapped back to the top a second later, every time.
  //
  // Same shape as the animation fix above: nothing preserves the old NODE, a module-level record
  // outlives it. keepScroll remembers where a container was left by id and puts the new one back
  // there; `stick` is for a chat log, which follows the newest line UNLESS the reader had
  // scrolled up to read history, in which case their place is what gets kept.
  //
  // MODULE SCOPE for the reason SWEEP_MS is: the record has to outlive the render, and render()
  // is called afresh every time.
  var chatMatch = null;
  var chatDrafts = {};          // match + channel -> private, unsent draft
  var scrollMemory = {};        // id -> { top: px, atBottom: bool }
  var AT_BOTTOM_SLACK = 8;      // px of "close enough to the end", for sub-pixel line heights

  function keepScroll(node, id, stick) {
    var mem = scrollMemory[id] || { top: 0, atBottom: true };
    node.addEventListener("scroll", function () {
      scrollMemory[id] = {
        top: node.scrollTop,
        atBottom: node.scrollHeight - node.clientHeight - node.scrollTop <= AT_BOTTOM_SLACK
      };
    });
    // Run after attachment, in the same render, so the browser cannot paint at scrollTop 0.
    return function () {
      node.scrollTop = (stick && mem.atBottom) ? node.scrollHeight : mem.top;
    };
  }

  function render(root, state, ctx) {
    var liveRoot=arguments[3];
    root._refreshKey=refreshKey(state);
    var restoreScroll = [];
    var ui = ctx.ui, call = ctx.call, t = ctx.t;
    var esc = ui.esc, initials = ui.initials, clock = ui.clock, el = ui.el;

    // btn() with the pre-modular tag rule: the four solid/ghost action buttons are real <button>s;
    // the inline links/chips stay <span>s (as before). ui.btn is the shared helper.
    function btn(cls, text, onclick) {
      var tag = (cls === "btn-find" || cls === "btn-accept" ||
                 cls === "btn-ghost-light" || cls === "btn-solid" || cls === "hero-link") ? "button" : "span";
      return ui.btn(cls, text, onclick, { tag: tag });
    }

    clearBanTimer();   // any prior ban countdown belongs to a DOM node this render is about to wipe
    var guideAuth = state.auth || {};
    var identity = guideAuth.signed_in ? (guideAuth.player_id || guideAuth.steam_id || "signed-in") : null;
    if (!identity || guideAuth.placing || !guideAuth.rank || identity !== rankGuideIdentity ||
        rankGuidePhase !== (state.comp || {}).phase) { rankGuideOpen = false; }
    rankGuideIdentity = identity;
    var profile = (ui._screens || {}).profile;
    if (rankGuideOpen && profile && profile.renderRankGuide) {
      profile.renderRankGuide(root, state, Object.assign({}, ctx, {
        resetRankGuideScroll: rankGuideFresh,
        rankGuideBackLabel: "← " + t("tab_competitive"),
        closeRankGuide: function () {
          rankGuideOpen = false;
          root.innerHTML = "";
          render(root, state, ctx);
          var trigger = root.querySelector(".rank-guide-open");
          if (trigger) { trigger.focus(); }
        }
      }));
      rankGuideFresh = false;
      return;
    }
    root.appendChild(buildCompetitive());
    if(liveRoot)root=liveRoot;
    restoreScroll.forEach(function (restore) { restore(); });
    // The modal is appended to the BODY, not into the screen, so it is not inside the grid it
    // would otherwise have to fight (the corner-panel version ran off the bottom of the screen).
    if ((state.comp || {}).rank_info_open && (state.comp || {}).ladder) {
      document.body.appendChild(rankModal(state.comp.ladder));
    }
    // Same treatment, same reason: on the BODY, so it is not inside the hero's grid. The two are
    // mutually exclusive server-side (competitive.py _toggle_penalties), so only one is ever up.
    //
    // It stays in render(), unlike the report box below: this one is opened from the hero's own
    // corner and from nowhere else, so there is no screen that can raise it while competitive is
    // not the screen being drawn.
    if ((state.comp || {}).penalties_open && (state.comp || {}).penalties) {
      document.body.appendChild(penaltiesModal(state.comp.penalties));
    }
    // The report box is NOT appended here. It is registered as an OVERLAY at the foot of this
    // file, because it is opened from screens this render knows nothing about - see the comment
    // on that registration.

    // ---------------------------------------------------------------- the screen
    function buildCompetitive() {
      var wrap = el("div", "split");
      var auth = state.auth || {};
      // Both signed_out and signing_in route here: heroSignedOut() branches on phase === "signing_in"
      // to show the waiting/link-code panel, so during sign-in we must NOT fall through to hero().
      if (!auth.signed_in) {
        wrap.appendChild(heroSignedOut());
        wrap.appendChild(paneInfo());
        return wrap;
      }
      wrap.appendChild(hero());
      wrap.appendChild(pane());
      return wrap;
    }

    // THE RANK EXPLAINER'S BUTTON, in the hero's own top corner.
    //
    // It used to be a full-width row ABOVE the split (.comp-bar), and that row was a gap: the red
    // panel started 28px down the window with nothing in the strip above it but one small ghost
    // button on the far side. Sam, 2026-09-16: "theres a gap in the competitive tab between the
    // red player info on the left and the top." Inside the hero it costs no height at all, and it
    // is next to the badge and the rank name - which is what it explains.
    function rankInfoBtn() {
      if (!(state.comp || {}).ladder) { return null; }   // no ladder from the server yet
      return ui.btn("rank-info-btn", t("rank_button"),
        function () { call("toggle_rank_info"); }, { tag: "button" });
    }

    // ...and the PENALTIES explainer, in the same strip and to its left. Same rule about the
    // server's numbers: no `penalties` block on the hello means no button, because a panel that
    // quoted a ban ladder it had guessed at would be worse than no panel at all.
    function penaltiesBtn() {
      if (!(state.comp || {}).penalties) { return null; }
      return ui.btn("rank-info-btn", t("pen_button"),
        function () { call("toggle_penalties"); }, { tag: "button" });
    }

    // The two of them, as one strip. They used to be a single absolutely-positioned button in
    // the hero's corner; a second one pinned to the same corner would have had to know the
    // first one's width, so the strip owns the position and the buttons just sit in it.
    function heroTools() {
      var tools = el("div", "hero-tools");
      var pen = penaltiesBtn();
      if (pen) { tools.appendChild(pen); }
      var info = rankInfoBtn();
      if (info) { tools.appendChild(info); }
      return tools.childNodes.length ? tools : null;
    }

    // -- hero (left red panel) ------------------------------------------------
    function heroSignedOut() {
      var auth = state.auth || {};
      var h = el("div", "hero");
      h.appendChild(modeBar(state,ctx));
      h.appendChild(el("div", "ghost", "◆"));
      var inner = el("div", "hero-inner signin");
      inner.innerHTML =
        '<h1>' + esc(t("comp_signin_title")) + "</h1>" +
        "<p>" + esc(t("account_choice_body")) + "</p>";
      if (auth.phase === "signing_in") {
        var w = el("div", "signin");
        w.innerHTML = "<p>" + esc(t(auth.game_verifying ? "account_game_verifying" : "comp_signin_waiting")) + "</p>" +
          (!auth.game_verifying && auth.link_code ? '<div class="code-lg">' + esc(t("comp_signin_code", { code: auth.link_code })) + "</div>" : "");
        if (!auth.game_verifying) w.appendChild(btn("hero-link", t("comp_signin_open_again"), function () { call("open_link_again"); }));
        w.appendChild(btn("hero-link", t(auth.game_verifying ? "comp_signout" : "comp_cancel"), function () { accountForm = ""; accountDraft = {}; call("cancel_sign_in"); }));
        inner.appendChild(w);
      } else if (auth.phase === "game_unavailable") {
        inner.appendChild(el("p", "", t("account_game_pending")));
        inner.appendChild(btn("btn-find", t("account_game_retry"), function () { call("account_action", "game/retry"); }));
        inner.appendChild(btn("hero-link", t("comp_signout"), function () { call("sign_out"); }));
      } else {
        var choices = el("div", "account-choices");
        choices.appendChild(btn("btn-find", t("comp_signin_button"), function () { call("sign_in"); }));
        choices.appendChild(btn("btn-find", t("account_login"), function () {
          accountForm = "login"; accountDraft = {}; call("account_action", "cancel"); root.innerHTML = "";
          render(root, Object.assign({}, state, {auth:Object.assign({}, auth, {account_step:"", account_busy:false}), comp:Object.assign({}, state.comp, {error:""})}), ctx);
        }));
        inner.appendChild(choices);
        if (!auth.account_step && !accountForm) inner.appendChild(btn("hero-link", t("account_create"), function () {
          accountForm = "register"; accountDraft = {}; root.innerHTML = ""; render(root, state, ctx);
        }));
        var step = auth.account_step || accountForm;
        if (step) {
          var form = el("form", "signin account-form");
          form.setAttribute("aria-busy", String(!!auth.account_busy));
          if (state.comp && state.comp.error) {
            var accountError = el("div", "hero-error", state.comp.error);
            accountError.setAttribute("role", "alert"); form.appendChild(accountError);
          }
          function input(key, label, type) {
            var row = el("label", "account-field", t(label)), field = document.createElement("input");
            field.type = type || "text"; field.name = key; field.required = true;
            if (type === "password") field.minLength = 6;
            if (key === "token" || key === "code" || key === "email") { field.autocapitalize = "none"; field.setAttribute("autocorrect", "off"); field.spellcheck = false; }
            field.maxLength = key === "password" ? 256 : key === "display_name" ? 80 : 256;
            field.autocomplete = type === "password" ? (step === "register_code" || step === "reset_password" ? "new-password" : "current-password") : key === "email" ? "email" : key === "display_name" ? "nickname" : "one-time-code";
            if (step === "recovery_code" && key === "code") { field.inputMode = "numeric"; field.pattern = "[0-9]{6}"; field.maxLength = 6; }
            field.value = accountDraft[key] || "";
            field.oninput = function () {
              accountDraft[key] = field.value;
              if (confirmation) confirmation.setCustomValidity("");
            };
            row.appendChild(field); form.appendChild(row);
            return field;
          }
          var confirmation = null;
          if (step === "forgot_password" || step === "recovery_code" || step === "reset_password") {
            form.appendChild(el("h2", "", t("account_reset_title")));
            form.appendChild(el("p", "muted", t(step === "forgot_password" ? "account_recovery_intro" : step === "recovery_code" ? "account_recovery_sent" : "account_recovery_verified")));
          }
          if (step === "login" || step === "register" || step === "forgot_password") input("email", "account_email", "email");
          if (step === "login" || step === "register_code" || step === "reset_password") input("password", "account_password", "password");
          if (step === "register_code" || step === "reset_password") confirmation = input("confirm_password", "account_confirm_password", "password");
          if (step === "register_code") input("display_name", "account_name");
          if (step === "recovery_code") input("code", "account_code");
          if (step === "login_code" || step === "register_code") {
            form.appendChild(el("p", "muted", t("account_code_sent")));
            input(step === "login_code" ? "code" : "token", "account_code");
          }
          if (step === "login") {
            var remember = el("label", "account-remember"), check = document.createElement("input");
            check.type = "checkbox"; check.checked = accountDraft.remember_me === true;
            check.onchange = function () { accountDraft.remember_me = check.checked; };
            remember.appendChild(check); remember.appendChild(document.createTextNode(t("account_remember"))); form.appendChild(remember);
          }
          var submit = document.createElement("button"); submit.type = "submit"; submit.className = "btn-find";
          submit.textContent = t(auth.account_busy ? "account_working" : "account_continue");
          submit.disabled = !!auth.account_busy; form.appendChild(submit);
          form.onsubmit = function (event) {
            event.preventDefault(); if (auth.account_busy) return;
            var activeForm=event.currentTarget;
            var activeConfirmation=activeForm.querySelector('input[name="confirm_password"]');
            var fields = Object.assign({}, accountDraft);
            if (activeConfirmation && fields.password !== fields.confirm_password) {
              activeConfirmation.setCustomValidity(t("account_password_mismatch")); activeConfirmation.reportValidity(); return;
            }
            delete fields.confirm_password;
            if (fields.code) fields.code = fields.code.trim();
            if (fields.token) fields.token = fields.token.trim();
            delete accountDraft.password; delete accountDraft.confirm_password; delete accountDraft.code; delete accountDraft.token;
            activeForm.querySelectorAll('input[type="password"], input[name="code"], input[name="token"]').forEach(function (field) { field.value = ""; });
            var actions = {login_code:"login/verify", register_code:"verify", forgot_password:"forgot-password", recovery_code:"forgot-password/verify", reset_password:"reset-password"};
            call("account_action", actions[step] || step, fields);
          };
          inner.appendChild(form);
          if (step === "login") {
            var forgot = btn("hero-link account-forgot", t("account_forgot"), function () {
              if (auth.account_busy) return;
              accountForm = "forgot_password"; accountDraft = {email:accountDraft.email || ""};
              call("account_action", "cancel"); root.innerHTML = "";
              render(root, Object.assign({}, state, {auth:Object.assign({}, auth, {account_step:""}), comp:Object.assign({}, state.comp, {error:""})}), ctx);
            });
            forgot.disabled = !!auth.account_busy; inner.appendChild(forgot);
          }
          if (step === "recovery_code") {
            var resend = btn("hero-link account-resend", t("account_recovery_resend"), function () {
              if (!auth.account_busy) { delete accountDraft.code; call("account_action", "forgot-password", {email:accountDraft.email}); }
            });
            resend.disabled = !!auth.account_busy; inner.appendChild(resend);
          }
          inner.appendChild(btn("hero-link", t("comp_cancel"), function () {
            accountForm = ""; accountDraft = {}; call("account_action", "cancel");
            root.innerHTML = ""; render(root, Object.assign({}, state, {auth:Object.assign({}, auth, {account_step:""})}), ctx);
          }));
          if (!auth.account_step) inner.appendChild(btn("hero-link", t(step === "login" ? "account_create" : "account_login"), function () {
            accountForm = step === "login" ? "register" : "login"; accountDraft = {}; root.innerHTML = ""; render(root, state, ctx);
          }));
        }
      }
      if (auth && state.comp && state.comp.error && !(auth.account_step || accountForm)) {
        inner.appendChild(el("div", "hero-error", state.comp.error));
      }
      inner.appendChild(el("p", "account-disclosure", t("account_game_privacy")));
      var policies = el("div", "account-policies");
      policies.appendChild(btn("hero-link", t("account_privacy"), function () { call("account_policy", "privacy"); }));
      policies.appendChild(btn("hero-link", t("account_terms"), function () { call("account_policy", "terms"); }));
      inner.appendChild(policies);
      h.appendChild(inner);
      return h;
    }

    function hero() {
      var auth = state.auth || {}, comp = state.comp || {}, party = state.party || {};
      var h = el("div", "hero");
      h.appendChild(modeBar(state,ctx));
      var tools = heroTools();
      if (tools) { h.appendChild(tools); }

      var inner = el("div", "hero-inner");
      // IDENTITY, and now ONLY identity: who they are and what rank they are. The rank block used
      // to be a small centred emblem ABOVE the name; Sam, 2026-09-16: "lets also move where it
      // shows your rank to where the matches, wins, losses, winrate is and make it big. thats
      // what people care about the most." So it took that slot, at the size that claim deserves.
      inner.appendChild(el("div", "hero-name", auth.persona || ""));
      inner.appendChild(rankBlock(auth));
      // ...and NOT `auth.tier` under the name. It is the rank's name worked out from the level
      // number, and now that there is one ladder it is the same word rankBlock has just printed
      // above - "Operator 3" with "UMBRA" under the persona. It only read as two different things
      // while it came from a different list of names. The field stays in the snapshot; the
      // profile still uses it as its offline fallback.

      // The three stat-column micro-labels are translated via i18n (comp_stat_* in all 7 languages);
      // comp_record combines the same numbers into one sentence elsewhere.
      // NO STATS HERE. The four figures (matches / wins / losses / win rate) and the rotating
      // spotlight cards that were both in this panel are gone - Sam, 2026-09-16: "lets remove the
      // random stats and all stats on this page." The record still has a home, and it is the one
      // built for it: the Profile screen, which draws the whole history rather than a headline
      // off the top of it. What is left here is the player, their rank and the button.
      inner.appendChild(el("div", "hero-spacer"));
      inner.appendChild(heroAction(comp, party));
      h.appendChild(inner);

      return h;
    }

    // COMPETITIVE INFO, drawn from the SERVER's numbers. Every band is an environment dial
    // (server/rating.cjs ladder()), so a table written into this file would keep explaining the
    // old ladder the day one is retuned, and nothing would fail to make that visible.
    //
    // A MODAL, not a corner panel. The first version was an "i" pinned to the hero's corner with
    // the explanation dropping out underneath it, which pushed the Find Match button around and
    // ran off the bottom of the screen. This is the same ui.modal the gamemodes ruleset uses, so
    // it scrolls, closes on Escape, and cannot fight the layout it sits in.
    function rankModal(ladder) {
      var ranks = ladder.ranks || {};
      var kicker = el("div", "rank-kicker", t("rank_kicker"));

      var how = el("div", "rank-block");
      how.appendChild(el("div", "rank-h", t("rank_visible_title")));
      how.appendChild(el("div", "rank-note", t("rank_visible", {
        divisions: ranks.divisions, first: (ranks.names || [])[0] || "",
        last: (ranks.names || [])[(ranks.names || []).length - 1] || "" })));
      how.appendChild(el("div", "rank-note", t("rank_rr")));
      // THE TOP OF THE LADDER WORKS DIFFERENTLY, and this is the only place that says so. Both
      // lines are built from the server's own numbers (progress.ranks()), so retuning the band or
      // the number of seats needs no hub release - and a hub that cannot read them says nothing
      // rather than describing a ladder it is guessing at.
      var countName = (ranks.names || [])[(ranks.counting_rank || 0) - 1];
      if (countName && ranks.rr_per_division) {
        how.appendChild(el("div", "rank-note", t("rank_counting", {
          name: countName,
          two: ranks.rr_per_division,
          three: ranks.rr_per_division * 2 })));
      }
      if (ranks.top) {
        how.appendChild(el("div", "rank-note", (countName && ranks.top_at && ranks.top_slots)
          ? t("rank_top_seats", { name: ranks.top, rr: ranks.top_at,
                                  slots: ranks.top_slots, band: countName })
          : t("rank_top", { name: ranks.top })));
      }

      // The ladder itself: every rank, with its divisions as chips. The chip IS the badge now -
      // the whole point of a per-division icon is that a player can see Operator 3 outranks Operator 1
      // before reading a word, and a panel that explains the ladder in numerals would be the one
      // place in the app that hides that (docs/rank-art.md).
      //
      // `mine.rank` is 1-BASED and this loop index is 0-based. The old comparison was
      // `mine.rank === idx`, which highlighted the rank below the player's - or nothing at all
      // at Rookie, where rank 1 never equals index 0..7's first entry.
      var list = el("div", "rank-list");
      var mine = (state.auth || {}).rank || null;
      (ranks.names || []).forEach(function (name, idx) {
        var isMine = mine && !mine.top && mine.rank === idx + 1;
        var row = el("div", "rank-rank" + (isMine ? " mine" : ""));
        row.appendChild(el("span", "rank-name", name));
        var chips = el("span", "rank-divs");
        for (var d = 1; d <= (ranks.divisions || 3); d += 1) {
          var chip = el("span", "rank-chip" + (isMine && mine.division === d ? " here" : ""));
          var art = ui.rankBadge({ rank: idx + 1, rank_name: name, division: d }, ranks);
          if (art) { chip.appendChild(art); } else { chip.textContent = String(d); }
          chips.appendChild(chip);
        }
        row.appendChild(chips);
        list.appendChild(row);
      });
      if (ranks.top) {
        var topRow = el("div", "rank-rank" + (mine && mine.top ? " mine" : ""));
        topRow.appendChild(el("span", "rank-name", ranks.top));
        var topChips = el("span", "rank-divs");
        var topChip = el("span", "rank-chip" + (mine && mine.top ? " here" : ""));
        var topArt = ui.rankBadge({ top: true }, ranks);
        if (topArt) { topChip.appendChild(topArt); }
        topChips.appendChild(topChip);
        topRow.appendChild(topChips);
        list.appendChild(topRow);
      }

      // WHAT IS LEFT TO SAY once the ladder is explained. There is no section about the hidden
      // rating and there must not be one: Sam, 2026-09-15, "we dont even want the players to know
      // there is a matchmaking rating". This panel used to open on "Two systems, one number" and carry
      // a whole note about matchmaking using a separate rating - which is true, and is exactly the
      // thing a player is never told. rank_hidden is gone from hub/i18n.py, not just from here.
      var rest = el("div", "rank-block");
      rest.appendChild(el("div", "rank-h", t("rank_hidden_title")));
      rest.appendChild(el("div", "rank-note", t("rank_placements", { n: ladder.placement_matches })));
      rest.appendChild(el("div", "rank-note", t("rank_penalty")));

      var foot = el("div", "rank-foot");
      foot.appendChild(ui.btn("gm-btn", t("rank_close"), function () { call("toggle_rank_info"); }));

      var overlay = ui.modal({
        title: t("rank_title"),
        children: [kicker, how, list, rest, foot],
        onClose: function () { call("toggle_rank_info"); }
      });
      overlay.classList.add("rank-overlay");
      var dialog = overlay.querySelector(".ui-modal");
      if (dialog) { dialog.setAttribute("tabindex", "-1"); setTimeout(function () { dialog.focus(); }, 0); }
      return overlay;
    }

    // PENALTIES, drawn from the SERVER's dials (server/live.cjs hello -> `penalties`) for the
    // same reason the ladder above is: COMP_NO_SHOW_BAN_SECONDS, COMP_NO_SHOW_RR and the rest are
    // environment variables, and a table written into this file would keep promising the old ban
    // ladder the day one of them is turned - silently, because nothing would fail.
    //
    // It answers the two questions a player who has just been banned actually has, in that order:
    // what did I do, and how long is this. The RR cost comes third because it is the smaller half
    // of the punishment and the clock is the part they are staring at.
    function penaltiesModal(pen) {
      var rungs = (pen.rungs || []).filter(function (s) { return Number(s) > 0; });

      // A duration a person would say out loud. `clock()` is the running-countdown format and it
      // is wrong here: the top of this ladder is four hours, and "240:00" is not a length of time
      // anybody reads. Whole units only, and never "1 d" for the decay window - a day expressed
      // as days reads as a rounding, where "24 h" reads as the rule it is.
      function dur(seconds) {
        var s = Math.max(0, Math.round(Number(seconds) || 0));
        if (s >= 2 * 86400 && s % 86400 === 0) { return t("pen_dur_day", { n: s / 86400 }); }
        if (s >= 3600 && s % 3600 === 0) { return t("pen_dur_hour", { n: s / 3600 }); }
        if (s >= 60 && s % 60 === 0) { return t("pen_dur_min", { n: s / 60 }); }
        return t("pen_dur_sec", { n: s });
      }

      function block(title, notes) {
        var b = el("div", "rank-block");
        b.appendChild(el("div", "rank-h", title));
        notes.forEach(function (n) { if (n) { b.appendChild(el("div", "rank-note", n)); } });
        return b;
      }

      var kicker = el("div", "rank-kicker", t("pen_kicker"));

      // Keep the team-killing policy general so this panel cannot be used to evade detection.
      var what = block(t("pen_offences_title"), [
        t("pen_offence_no_show", { time: dur(pen.connect_seconds) }),
        t("pen_offence_leave"),
        t("pen_offence_tk"),
        // ...and what does NOT. Declining, or letting the accept window lapse, is not an offence
        // (live.cjs expireAccept closes the match as `declined` and punishes nobody), and a
        // player who believes otherwise accepts matches they are not ready for.
        t("pen_no_offence"),
      ]);

      var time = block(t("pen_time_title"), [t("pen_time_body")]);
      var list = el("div", "pen-list");
      rungs.forEach(function (secs, i) {
        var row = el("div", "pen-rung");
        var last = i === rungs.length - 1;
        row.appendChild(el("span", "pen-rung-n",
          t(last ? "pen_rung_last" : "pen_rung", { n: i + 1 })));
        row.appendChild(el("span", "pen-rung-t", dur(secs)));
        list.appendChild(row);
      });
      if (rungs.length) { time.appendChild(list); }
      if (pen.decay_seconds) {
        time.appendChild(el("div", "rank-note", t("pen_decay", { time: dur(pen.decay_seconds) })));
      }
      time.appendChild(el("div", "rank-note", t("pen_party")));

      // The RR half, and only when there IS one: COMP_NO_SHOW_RR can be dialled to zero, and a
      // heading over "0 RR" would read as a bug.
      var rr = Number(pen.rr) > 0 ? block(t("pen_rr_title"), [
        t("pen_rr", { n: Number(pen.rr) }),
        t("pen_rr_demote"),
        t("pen_rr_placing"),
        t("pen_rr_record"),
      ]) : null;

      var foot = el("div", "rank-foot");
      foot.appendChild(ui.btn("gm-btn", t("rank_close"), function () { call("toggle_penalties"); }));

      var kids = [kicker, what, time];
      if (rr) { kids.push(rr); }
      kids.push(foot);

      var overlay = ui.modal({
        title: t("pen_title"),
        children: kids,
        onClose: function () { call("toggle_penalties"); }
      });
      overlay.classList.add("rank-overlay");
      var dialog = overlay.querySelector(".ui-modal");
      if (dialog) { dialog.setAttribute("tabindex", "-1"); setTimeout(function () { dialog.focus(); }, 0); }
      return overlay;
    }

    // THE RANK EMBLEM: one of the 25 badges in ranksprite.js, keyed by the rank's INDEX on the
    // ladder the server sent - never by its name, so a rank can be renamed on the service without
    // shipping a hub release (docs/ranks.md, docs/rank-art.md). Still no network and no image
    // file: the sprite is inlined, which is also the only form the page's CSP allows.
    //
    // The badge carries the division in its own silhouette, so the CSS mark and the three pips
    // that used to sit on top of it are gone - drawing both would say the same thing twice, in
    // two visual languages. It is also self-coloured, which is the point: the emblem now brings
    // the rank's own colour onto the red hero instead of inheriting its white ink.
    //
    // A player who is still placing has no rank yet, and saying so is better than showing them an
    // empty badge: the rating is real from match one, it is the RANK we are not confident enough
    // to print (server/rating.cjs PLACEMENT_MATCHES).
    function rankBlock(auth) {
      var comp = state.comp || {};
      var box = el("div", "hero-rank");
      var rank = auth.placing ? null : (auth.rank || null);

      var badge = ui.rankBadge(rank, (comp.ladder || {}).ranks);   // null while placing
      if (badge) {
        // No badge means no emblem at all, not an empty 56px box: while placing, the placement
        // line IS the whole answer and a blank plate above it reads as art that failed to load.
        var guideLabel = ((state.profile || {}).strings || {}).profile_ranks_open || t("rank_title");
        var emblem = ui.btn("rank-emblem rank-guide-open" + (rank && rank.top ? " top" : ""), undefined, function () {
          rankGuideOpen = true;
          rankGuideFresh = true;
          rankGuidePhase = (state.comp || {}).phase;
          root.innerHTML = "";
          render(root, state, ctx);
          var back = root.querySelector(".profile-ranks-back");
          if (back) { back.focus(); }
        }, { ariaLabel: guideLabel + " · " + rank.rank_name + (rank.division ? " " + rank.division : "") });
        emblem.title = guideLabel;
        emblem.appendChild(badge);
        box.appendChild(emblem);
      }

      // The emblem sits BESIDE the words now rather than above them, so everything that is not
      // the badge goes in its own column. A player still placing has no badge, and then the
      // column simply fills the row on its own.
      var text = el("div", "hero-rank-text");
      box.appendChild(text);

      if (rank) {
        text.appendChild(el("div", "hero-rankname",
          rank.top ? rank.rank_name : rank.rank_name + " " + rank.division));
        // 0-100 through the division LOWER DOWN THE LADDER ONLY. From the counting band up the
        // figure stops resetting and runs as one count with no ceiling (Spectre 3 is 200+ and
        // still 200+ at 900), so those ranks show the figure alone: a bar up there could only
        // ever be full, which is the one thing a progress bar must never be.
        if (rank.top || rank.counting) {
          // Only when there IS a figure. t() leaves an unresolved placeholder in place, so a null
          // here does not read as "no RR" - it reads as a literal "{rr} RR" under the badge.
          if (rank.rr !== null && rank.rr !== undefined) {
            text.appendChild(el("div", "hero-rr", t("comp_rr", { rr: rank.rr })));
          }
        } else {
          var bar = el("div", "rr-bar");
          var fill = el("div", "rr-fill");
          fill.style.width = Math.max(0, Math.min(100, rank.rr)) + "%";
          bar.appendChild(fill);
          text.appendChild(bar);
          text.appendChild(el("div", "hero-rr", t("comp_rr", { rr: rank.rr })));
        }
      } else {
        text.appendChild(el("div", "hero-rankname", t("comp_unranked")));
        // A COUNTDOWN that says what the matches are FOR. Sam, 2026-09-16, asked first for the
        // progress shape ("0/5 Placement Matches Complete") and then, the same day, for this one -
        // "3 more placement matches required for rank calibration" - which answers both questions a
        // player with no rank has: how much is left, and why there is no badge yet. The numberless
        // line is only for the moment before the server's count has landed.
        var left = auth.placements_left;
        if (left !== null && left !== undefined && left > 0) {
          text.appendChild(el("div", "hero-placements", t("comp_placements_left", { n: left })));
        } else {
          text.appendChild(el("div", "hero-placements", t("comp_placements")));
        }
      }
      return box;
    }

    // Saved preferences and verbs remain shared with Settings; only the controls move.
    function matchmakingControls() {
      var settings = state.settings || {}, network = settings.network;
      if (!network) { return null; }
      var strings = settings.strings || {};
      var wrap = el("div", "comp-network");
      var row = el("div", "comp-network-row");
      var label = el("label", "comp-region", strings.network_region);
      var select = document.createElement("select");
      select.id = "comp-network-region";
      select.setAttribute("aria-label", strings.network_region);
      select.title = strings.network_same_region;
      select.disabled = !!network.locked;
      (network.options || []).forEach(function (option) {
        var opt = document.createElement("option");
        opt.value = option.code;
        opt.textContent = option.code ? option.name + " (" + option.code + ")" : strings.network_select;
        opt.title = option.name;
        opt.selected = option.code === (network.region || "");
        select.appendChild(opt);
      });
      select.addEventListener("change", function () {
        call("settings_set_matchmaking_region", select.value);
      });
      label.appendChild(select);
      row.appendChild(label);
      var cross = el("label", "comp-cross-region");
      cross.title = strings.network_cross_region + " — " + strings.network_cross_hint;
      var check = document.createElement("input");
      check.type = "checkbox";
      check.id = "comp-network-cross-region";
      check.checked = !!network.cross_region;
      check.disabled = !!network.locked;
      check.addEventListener("change", function () {
        call("settings_set_matchmaking_cross_region", check.checked);
      });
      cross.appendChild(check);
      cross.appendChild(el("span", null, strings.network_cross_short));
      row.appendChild(cross);
      wrap.appendChild(row);
      if (network.status !== "ready") {
        var status = el("div", "comp-network-status", strings["network_" + (network.status || "preparing")]);
        if (network.error) { status.title = network.error; }
        status.setAttribute("role", "status");
        wrap.appendChild(status);
      }
      return wrap;
    }

    function heroAction(comp, party) {
      var phase = comp.phase;
      var box = el("div", "hero-action");
      if (phase === "idle" || phase === "queued") {
        var networkControls = matchmakingControls();
        if (networkControls) { box.appendChild(networkControls); }
      }

      if (phase === "queued") {
        box.appendChild(searchBox(comp, party));
        return box;
      }
      if (phase === "checking") {
        var c = el("div", "found-box");
        c.appendChild(el("div", "found-title", t("comp_check_title")));
        box.appendChild(c);
        return box;
      }
      if (phase === "found") {
        box.appendChild(foundBox(comp));
        return box;
      }
      if (phase === "lobby") { box.appendChild(lobbyAction(comp)); return box; }
      if (phase === "connecting") { box.appendChild(connectAction(comp)); return box; }
      if (phase === "live") { box.appendChild(liveAction(comp)); return box; }
      if (phase === "result") { box.appendChild(resultAction(comp)); return box; }

      // idle: the Find match call to action
      //
      // NOTHING INSTALLED, NOTHING TO QUEUE FOR. The Tk tab answers this by replacing its whole
      // body with a gate screen, so its Find match button cannot be reached; this screen had no
      // equivalent, so a hub whose owner opened Competitive before installing anything showed a
      // live Find match button and let them search for a mode they did not own. The install
      // button takes Find match's PLACE - the same shape, because it is the one thing to press
      // here - and comp.can_find is false besides.
      if (!comp.installed) {
        var installMode = comp.mode_id === "BB1" ? "Bodybomb 1v1" : "Bodybomb 5v5";
        if (comp.mode_listed) {
          box.appendChild(btn("btn-find", t("comp_gate_install", {mode: installMode}), function () {
            call("gamemode_install", comp.mode_id);
            // The progress bar for that job lives on the Gamemodes screen, so go and watch it
            // there rather than leaving this panel sitting still while the pak is rebuilt.
            call("set_view", "gamemodes");
          }));
        }
        box.appendChild(el("div", "hero-ready",
                           comp.mode_listed ? t("comp_gate_body", {mode: installMode}) : t("comp_gate_waiting")));
        if (comp.ranked_ban) { box.appendChild(el("div", "hero-error", (comp.mode_strings||{}).banned)); }
        if (comp.mode_id === "BB1" && party.in_party) { box.appendChild(el("div", "hero-error", (comp.mode_strings||{}).solo_only)); }
        if (comp.error) { box.appendChild(el("div", "hero-error", comp.error)); }
        return box;
      }
      if (comp.ranked_ban) { box.appendChild(el("div", "hero-error", (comp.mode_strings||{}).banned)); }
      if (comp.mode_id === "BB1" && party.in_party) { box.appendChild(el("div", "hero-error", (comp.mode_strings||{}).solo_only)); }
      var isLeader = comp.is_leader;
      if (isLeader) {
        var find = btn("btn-find", t("comp_find_match"), function () { call("find_match"); });
        if (!comp.can_find) { find.disabled = true; }
        box.appendChild(find);
      } else {
        box.appendChild(el("div", "hero-ready", t("comp_party_waiting_leader", { name: leaderName(party) })));
      }
      // Behind the release the service is publishing: the queue is shut, and the button says so
      // rather than failing when it is pressed. The line carries the finished sentence from
      // Python (comp.outdated.line) so this screen, the Tk window and the service's own refusal
      // never word it three different ways. A match already under way never reaches here.
      //
      // UNDER the button and above "you do not need Bodycam open" (Sam, 2026-09-16). It used to
      // sit above Find match as a bordered card, which pushed the button - the thing the player
      // came here to press - a long way down the panel whenever it appeared.
      if (comp.outdated) { box.appendChild(outdatedBox(comp.outdated)); }
      // NO PARAGRAPH ABOUT THE GAME BEING OPEN. It used to sit right here, asking the player to
      // remember to keep Bodycam closed. The rule is enforced now instead: find_match does
      // nothing while the game is up and cues "close your game" for three seconds (Python,
      // Session.find_match), so the only people who ever read it are the ones it applies to.
      if (comp.penalty && comp.penalty.left) {
        var pen = comp.penalty;
        var until = +pen.until || 0;   // absolute epoch-seconds deadline; 0 on an older snapshot
        var rem0 = until ? (until - Date.now() / 1000) : 0;
        if (until && rem0 > 0) {
          // Live per-second countdown, no network: recompute from the deadline each tick and update
          // the same element in place, so it keeps ticking even when the de-duped /state poll finds
          // nothing new and never re-renders.
          var banEl = el("div", "hero-error", clock(Math.round(rem0)));
          box.appendChild(banEl);
          banTimer = setInterval(function () {
            if (!banEl.isConnected) { clearBanTimer(); return; }   // navigated away / rebuilt
            var rem = until - Date.now() / 1000;
            if (rem <= 0) { banEl.textContent = clock(0); clearBanTimer(); return; }  // let /state show un-banned
            banEl.textContent = clock(Math.round(rem));
          }, 1000);
        } else {
          // Back-compat: server says banned but sent no `until` — the static clock, as before.
          box.appendChild(el("div", "hero-error", clock(pen.left)));
        }
      }
      if (comp.error) { box.appendChild(el("div", "hero-error", comp.error)); }
      return box;
    }

    // The one card that explains a queue shut by a version. "Update" is the hub's own installer
    // offer (the same verb the top-of-window strip calls); an out-of-date GAMEMODE instead sends
    // them to the Gamemodes screen, where its Update button is - the two are different buttons in
    // different places and telling someone to press the wrong one is worse than saying nothing.
    function outdatedBox(out) {
      var box = el("div", "outdated-box");
      box.appendChild(el("div", "outdated-line", out.line || ""));
      box.appendChild(el("div", "outdated-safe", t("comp_outdated_safe")));
      // The two update verbs stay, as black text links rather than the pair of ghost buttons this
      // used to carry: the block is a footnote to the button above it now, and two more buttons
      // under a disabled Find match read as the thing to press.
      var row = el("div", "outdated-actions");
      if (out.what === "hub" || out.what === "both") {
        row.appendChild(btn("outdated-link", t("update_banner_action"), function () { call("apply_hub_update"); }));
      }
      if (out.what === "mode" || out.what === "both") {
        row.appendChild(btn("outdated-link", t("comp_outdated_update_mode"), function () { call("set_view", "gamemodes"); }));
      }
      box.appendChild(row);
      return box;
    }

    function searchBox(comp, party) {
      var q = comp.queue || {};
      var box = el("div", "search-box");
      var head = el("div", "search-head");
      head.innerHTML = '<div class="lbl">' + esc(t("comp_searching")) + "</div>" +
        '<div class="timer">' + esc(clock(q.seconds || 0)) + "</div>";
      box.appendChild(head);
      var bar = el("div", "search-bar");
      var fill = document.createElement("i");
      fill.style.animationDelay = heldPhase(SWEEP_MS);      // survive the once-a-second rebuild
      bar.appendChild(fill);
      box.appendChild(bar);
      box.appendChild(el("div", "search-reminder", t("comp_search_keep_closed")));
      var meta = el("div", "search-meta");
      meta.appendChild(el("span", "", t("comp_queue_mode")));
      if (comp.party_size > 1) {
        meta.appendChild(el("span", "", t("comp_party_queue_note", { n: comp.party_size })));
      }
      box.appendChild(meta);
      box.appendChild(btn("btn-ghost-light", t("comp_cancel"), function () { call("cancel_search"); }));
      return box;
    }

    function foundBox(comp) {
      var f = comp.found || {};
      var box = el("div", "found-box");
      box.appendChild(el("div", "found-title", t("comp_found_title")));
      box.appendChild(el("div", "found-timer", clock(f.accept_left || 0)));
      box.appendChild(el("div", "found-count", t("comp_accepted_count", { n: f.accepted || 0, total: f.total || 0 })));
      if (f.i_accepted) {
        box.appendChild(el("div", "found-count", t("comp_waiting_others")));
      } else {
        box.appendChild(btn("btn-accept", t("comp_accept"), function () { call("accept"); }));
      }
      return box;
    }

    // -- right pane -----------------------------------------------------------
    // Off the match path (idle / queued / checking / found / result) the pane is the party card.
    // On it (lobby / connecting / live) the party gives way to the match: teams, veto, chat, the
    // connect roster, the live/vote panel — the wide half of what CompetitivePanel drew.
    // Reuse the existing checks and translations from the settings snapshot.
    // Only shown before queueing; game-running warnings do not belong in a live match.
    function readinessStrip() {
      var settings = state.settings || {};
      var strings = settings.strings || {};
      var items = settings.readiness || [];
      if (!items.length) { return null; }
      var strip = el("section", "comp-readiness");
      var title = strings.sec_readiness || "Readiness";
      strip.setAttribute("aria-label", title);
      strip.appendChild(el("div", "comp-readiness-title", title));
      var row = el("div", "comp-readiness-items");
      items.forEach(function (item) {
        var label = strings[item.label_key] || item.label_key;
        var value = strings[item.value_key] || item.value_key;
        var cell = el("div", "comp-readiness-item " + (item.state || "warn"));
        cell.setAttribute("role", "group");
        cell.setAttribute("aria-label", label + ": " + value);
        cell.appendChild(el("span", "comp-readiness-label", label));
        cell.appendChild(el("span", "comp-readiness-value", value));
        row.appendChild(cell);
      });
      strip.appendChild(row);
      return strip;
    }

    function pane() {
      var comp = state.comp || {};
      var phase = comp.phase;
      var p = el("div", "pane");
      // `pane-lobby` hands the scrolling to the lobby's own top half: the chat pair below it is
      // pinned to the bottom of the window, which only works if the pane itself does not scroll.
      if (phase === "lobby") { p.className = "pane pane-lobby"; p.appendChild(lobbyPane(comp)); return p; }
      if (phase === "connecting") { p.appendChild(connectPane(comp)); return p; }
      if (phase === "live") { p.appendChild(livePane(comp)); return p; }
      if (!phase || phase === "idle") {
        var readiness = readinessStrip();
        if (readiness) { p.appendChild(readiness); }
      }
      // The inbox sits ABOVE the party card, and only off the match path: an invite you cannot
      // act on (you are in a lobby, or already in the game) is not an invite, it is a distraction.
      var inbox = inviteInbox();
      if (inbox) { p.appendChild(inbox); }
      p.appendChild(partySection());
      return p;
    }

    // ---------------------------------------------------------------- match flow: hero actions
    function actionRow(children) {
      var row = el("div", "action-row");
      for (var i = 0; i < children.length; i++) { if (children[i]) { row.appendChild(children[i]); } }
      return row;
    }
    // The clock on the current lobby stage, drawn for EVERYONE rather than only the person it is
    // running against: the other nine are the ones wondering whether anything is happening, and a
    // number answers that better than a name does. Urgent in the last ten seconds, like the accept
    // clock. It only DRAWS - when it reaches zero the server has already decided for whoever was
    // on it, and the next lobby payload carries the result.
    function stageClock(lb) {
      var left = lb.stage_seconds || 0;
      if (left <= 0) { return null; }
      return el("div", "found-timer" + (left <= 10 ? " urgent" : ""), clock(left));
    }

    /** "The clock ran out - this was picked at random", under whichever choice it made. */
    function autoNote(flag) {
      return flag ? el("div", "found-count auto-note", t("comp_auto_pick")) : null;
    }

    function addAll(box) {
      for (var i = 1; i < arguments.length; i += 1) {
        if (arguments[i]) { box.appendChild(arguments[i]); }
      }
      return box;
    }

    function sideWord(result) {
      return result === "tails" ? t("comp_tails") : t("comp_heads");
    }

    function faceLetter(result) {
      return (result === "tails" ? t("comp_tails") : t("comp_heads")).slice(0, 1).toUpperCase();
    }

    function lobbyAction(comp) {
      var lb = comp.lobby || {}, coin = lb.coin || {}, stage = lb.stage;
      var box = el("div", "found-box lobby-action");
      if (stage === "coin" || stage === "flipping") {
        box.appendChild(el("div", "found-title", t("comp_coin_title")));
        // TWO REAL FACES, not one letter on a disc. The coin turns end over end, and with a back
        // face it turns into the other side on the way round instead of showing the same letter
        // mirrored - which is what a flat coin with one face does, and it looked like a sticker
        // spinning. `.coin` is only the box; each face carries the whole design (competitive.css).
        // In the air while the clock is above LAND_AT; down, and readable, under it.
        var inAir = stage === "flipping" && (lb.stage_seconds || 0) > LAND_AT;
        var landed = stage === "flipping" && !inAir;
        // The settle plays on the FIRST landed second only. This screen is rebuilt about once a
        // second, and a one-shot animation on a brand new element starts again from zero - so
        // "landed" alone would re-pop the coin every second for the rest of the window, which
        // reads as a coin still moving rather than one that has come down.
        var settling = landed && (lb.stage_seconds || 0) === LAND_AT;
        var coinEl = el("div", "coin" + (inAir ? " flip" : "") + (settling ? " landed" : ""));
        coinEl.appendChild(el("span", "coin-face", faceLetter(coin.result)));
        coinEl.appendChild(el("span", "coin-face back",
          faceLetter(coin.result === "tails" ? "heads" : "tails")));
        if (inAir) { coinEl.style.animationDelay = heldPhase(FLIP_MS); }
        box.appendChild(coinEl);
        if (stage === "flipping") {
          // Say the face out loud as well as showing it: the letter is one character on a struck
          // disc, and "H"/"T" is not a word anybody reads at a glance under pressure.
          box.appendChild(el("div", "found-count" + (landed ? " strong" : ""),
            landed ? t("comp_coin_landed", { side: sideWord(coin.result) })
                   : t("comp_coin_flipping")));
        } else if (lb.i_am_coin_captain) {
          // The ONE designated captain flips for everyone; the server decides the result.
          box.appendChild(el("div", "found-count", t("comp_coin_pick")));
          box.appendChild(actionRow([
            ui.btn("btn-choice", t("comp_heads"), function () { call("pick_coin", "heads"); }, { tag: "button" }),
            ui.btn("btn-choice", t("comp_tails"), function () { call("pick_coin", "tails"); }, { tag: "button" })
          ]));
          addAll(box, stageClock(lb));
        } else {
          box.appendChild(el("div", "found-count", t("comp_coin_wait", { name: lb.coin_captain_name })));
          addAll(box, stageClock(lb));
        }
      } else if (stage === "choice") {
        box.appendChild(el("div", "found-title",
          t("comp_coin_result", { side: coin.result === "tails" ? t("comp_tails") : t("comp_heads"), name: coin.toss_winner_name })));
        if (coin.i_won_toss && lb.i_am_captain) {
          box.appendChild(el("div", "found-count", t("comp_choice_title")));
          box.appendChild(actionRow([
            ui.btn("btn-choice", t("comp_choose_side"), function () { call("choose", "side"); }, { tag: "button" }),
            ui.btn("btn-choice", t("comp_choose_ban"), function () { call("choose", "ban"); }, { tag: "button" })
          ]));
        } else {
          box.appendChild(el("div", "found-count", t("comp_choice_wait", { name: coin.toss_winner_name })));
        }
        addAll(box, stageClock(lb), autoNote(lb.coin_auto));
      } else if (stage === "side") {
        // The side advantage: the picker gets a REAL attack/defend selector (this used to be
        // auto-assigned). The other team waits to be told which side it is on.
        box.appendChild(el("div", "found-title", t("comp_side_title")));
        if (lb.i_pick_side) {
          box.appendChild(el("div", "found-count", t("comp_side_pick")));
          box.appendChild(actionRow([
            ui.btn("btn-choice", t("comp_side_attack"), function () { call("choose_side", "attack"); }, { tag: "button" }),
            ui.btn("btn-choice", t("comp_side_defend"), function () { call("choose_side", "defend"); }, { tag: "button" })
          ]));
        } else {
          box.appendChild(el("div", "found-count", t("comp_side_wait", { name: lb.side_picker_name })));
        }
        addAll(box, stageClock(lb), autoNote(lb.advantage_auto));
      } else {
        // veto / ready — the clickable map list is in the pane; the hero shows whose turn it is
        // and who holds the ban advantage (bans LAST, controls the final map).
        box.appendChild(el("div", "found-title", t("comp_veto_title")));
        if (stage === "ready" || lb.map) {
          box.appendChild(el("div", "found-count", t("comp_veto_done", { map: lb.map })));
        } else if (lb.my_turn) {
          box.appendChild(el("div", "found-count strong", t("comp_veto_your_turn")));
        } else {
          box.appendChild(el("div", "found-count", t("comp_veto_turn", { name: lb.ban_turn_name })));
        }
        if (stage !== "ready" && !lb.map) { addAll(box, stageClock(lb)); }
        if (stage !== "ready" && !lb.map && lb.ban_advantage_name) {
          box.appendChild(el("div", "found-count", t("comp_veto_last_ban", { name: lb.ban_advantage_name })));
        }
      }
      return box;
    }

    function connectAction(comp) {
      var c = comp.connect || {}, left = c.left || 0;
      var isHost = !!c.is_host, ready = !!c.host_ready;
      var box = el("div", "found-box connect-action");
      if (comp.error) box.appendChild(el("div", "hero-error", comp.error));
      box.appendChild(el("div", "found-title", t("comp_connect_title")));
      // A NON-HOST has nothing to join until the host's game has reported in, so no clock runs
      // against them yet — just say we are waiting on the host. Everyone else (the host, or a
      // non-host once the host is ready) gets a live countdown: the host's connect clock, or the
      // joiner's re-based 5-minute join clock.
      if (!isHost && !ready) {
        box.appendChild(el("div", "found-count waiting-host", t("comp_waiting_host")));
        // The wait is not open-ended and the joiner could not see that: `left` is the HOST's
        // connect clock, ticking for every client in the window, so say how long the host has
        // before the match dies on them. It is the host's deadline, not this player's — the
        // joiner's own clock only starts at host-ready, above.
        box.appendChild(el("div", "found-count host-join-timer",
          t("comp_host_join_timer", { time: clock(left) })));
      } else {
        var shown = (!isHost && ready) ? (c.join_left || 0) : left;
        box.appendChild(el("div", "found-timer" + (shown <= 30 ? " urgent" : ""), clock(shown)));
        box.appendChild(el("div", "found-count", t("comp_connect_timer", { time: clock(shown) })));
      }
      box.appendChild(el("div", "found-count", t("comp_connect_count", { n: c.done || 0, total: c.total || 0 })));
      box.appendChild(pips(c.total || 0, c.done || 0));
      // THE LAUNCH, and it is the joiner's only button here (Sam, 2026-09-16). "I am in the game"
      // is gone: a joiner's game no longer opens itself at host-ready, they press Launch, and the
      // press is what reports them in. It is DISABLED until the host's lobby is stamped, because a
      // joiner opened before that spends its one lobby search on an empty Steam — the greyed
      // button plus comp_waiting_host is the reason, shown where the impatience actually is.
      // The host has no launch button at all: their game still opens with the connect window.
      if (c.i_connected) {
        box.appendChild(el("div", "found-count", t("comp_connect_waiting")));
      }
      // One game action: first launch waits for host readiness; after launch the
      // joiner can reconnect using the existing guarded reopen action.
      if (!isHost && !c.i_connected) {
        box.appendChild(ui.btn("btn-accept", t("comp_launch"),
          function () { call("launch_game"); }, { tag: "button", disabled: !ready }));
      } else {
        box.appendChild(ui.btn("btn-ghost-light", t(isHost ? "comp_relaunch" : "comp_reconnect"),
          function () { call("relaunch_game"); }, { tag: "button" }));
      }
      // The start gate: all pips green and still nothing happening is the most confusing state
      // this screen has, so say which of the two it is.
      if (c.teams_status === "wait" || c.teams_status === "mismatch") {
        box.appendChild(el("div", "connect-warn",
          t(c.teams_status === "wait" ? "comp_teams_wait" : "comp_teams_mismatch")));
      }
      box.appendChild(el("div", "connect-warn", t("comp_connect_warn", { time: c.warn_time })));
      return box;
    }

    function liveAction(comp) {
      var lv = comp.live || {}, host = lv.host || {};
      var isHost = !!lv.is_host;
      var box = el("div", "found-box live-action");
      if (comp.error) box.appendChild(el("div", "hero-error", comp.error));
      box.appendChild(el("div", "found-title", t("comp_live_title")));
      (lv.reconnect_waiting || []).forEach(function (row) {
        var seconds = Math.max(0, Math.ceil((Number(row.deadline) - Date.now()) / 1000));
        var clock = Math.floor(seconds / 60) + ":" + String(seconds % 60).padStart(2, "0");
        box.appendChild(el("div", "found-count", t("comp_reconnect_wait", { time: clock })));
      });
      box.appendChild(el("div", "found-map", t("comp_map", { map: lv.map || "?" })));
      box.appendChild(el("div", "found-count", t("comp_host", { name: host.name || "?", ping: host.ping == null ? "—" : (host.estimated ? "≈" : "") + host.ping })));
      // Relaunch is for everyone in a live match: their game should be running, and this re-runs it
      // (guarded, safe if it is already up).
      box.appendChild(ui.btn("btn-ghost-light", t(isHost ? "comp_relaunch" : "comp_reconnect"),
        function () { call("relaunch_game"); }, { tag: "button" }));
      if (!isHost) {
        box.appendChild(el("div", "found-count", t("comp_join_hint", { name: host.name || "?" })));
      }
      if (lv.can_finish) {
        box.appendChild(ui.btn("btn-ghost-light", t("comp_preview_finish"),
          function () { call("finish_match"); }, { tag: "button" }));
      }
      return box;
    }

    function resultAction(comp) {
      var r = comp.result || {};
      var box = el("div", "found-box result-action");
      if (r.voided) {
        box.appendChild(el("div", "result-headline void", t("comp_result_void")));
        box.appendChild(el("div", "found-count", t("comp_result_void_body")));
      } else {
        var won = r.won === true;
        box.appendChild(el("div", "result-headline " + (won ? "win" : "loss"),
          won ? t("comp_result_win") : t("comp_result_loss")));
        var score = r.score || ["—", "—"];
        box.appendChild(el("div", "result-score", score[0] + " : " + score[1]));
        // The RR the match moved - not `delta`, which is an arrow count and read as "+2 RR" here.
        if (r.placing) {
          box.appendChild(el("div", "result-delta", t("comp_placements_left", { n: r.placements_left })));
        } else if (r.rr_delta) {
          var d = r.rr_delta;
          box.appendChild(el("div", "result-delta " + (d > 0 ? "up" : "down"),
            t("comp_rr", { rr: (d > 0 ? "+" : "") + d })));
        }
      }
      var closing = CLOSE_LINES[r.game_close];
      if (closing) { box.appendChild(el("div", "result-close " + closing[1], t(closing[0]))); }
      box.appendChild(ui.btn("btn-accept", t("comp_back"), function () { call("leave_match"); }, { tag: "button" }));
      // THE PEOPLE YOU JUST PLAYED. The result screen carried the score and nobody's name, so
      // there was no way to report (or remember) anyone from the one screen where you have just
      // decided whether you want to.
      var roster = comp.roster || [];
      if (roster.length) {
        var who = el("div", "res-roster");
        who.appendChild(el("div", "section-title", t("comp_result_roster")));
        roster.forEach(function (p) {
          var row = el("div", "res-player" + (p.is_me ? " you" : ""));
          row.appendChild(el("span", "res-name", p.name));
          if (!p.is_me && p.steam_id) { row.appendChild(reportButton(p.steam_id, p.name)); }
          who.appendChild(row);
        });
        box.appendChild(who);
      }
      return box;
    }

    function pips(total, done) {
      var wrap = el("div", "pips");
      total = Math.max(1, total | 0);
      for (var i = 0; i < total; i++) { wrap.appendChild(el("span", "pip" + (i < done ? " on" : ""))); }
      return wrap;
    }

    // ---------------------------------------------------------------- match flow: pane detail
    function teamsBlock(teams, showSide, lobby) {
      var cols = el("div", "teams");
      (teams || []).forEach(function (tm) {
        var col = el("div", "team-col");
        var head = t("comp_team", { n: tm.team });
        if (showSide && tm.side) {
          head += " · " + (tm.side === "attack" ? t("comp_side_attack") : t("comp_side_defend"));
        }
        col.appendChild(el("div", "team-head", head));
        var anyHidden = false;
        (tm.members || []).forEach(function (m) {
          if (m.hidden) { anyHidden = true; }
          var row = el("div", "team-member" + (m.is_me ? " you" : "") + (m.hidden ? " anon" : ""));
          // THE PLAYER'S STEAM PICTURE, with their initials underneath it as the fallback.
          // This plate used to show a LEVEL NUMBER - the old numeric ladder, invented client-side
          // for anyone the server had not rated - so a roster of ten people was ten numbers that
          // meant nothing. Sam, 2026-09-16: "in parties and in lobbies also show the user's steam
          // image instead of just default text."
          //
          // A HIDDEN player gets neither: no picture (the snapshot sends no url for them) and no
          // initials, because the initial of "Bravo" is not their initial and a letter that looks
          // like one invites the wrong guess. Just the silhouette.
          row.appendChild(ui.avatar({ url: m.avatar, text: m.hidden ? "" : initials(m.name),
                                      light: m.is_me, anon: m.hidden, ariaLabel: m.name }));
          if (lobby && !m.is_me && m.steam_id) {
            var muted = (lobby.muted_players || []).indexOf(m.steam_id) !== -1;
            var label = t(muted ? "comp_chat_unmute" : "comp_chat_mute", { name: m.name });
            var mute = ui.btn("tm-mute" + (muted ? " muted" : ""), undefined,
              function () { call("toggle_chat_mute", m.steam_id); },
              { tag: "button", ariaLabel: label });
            mute.title = label;
            mute.setAttribute("aria-pressed", String(muted));
            mute.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 4h16v12H9l-5 4V4z"/>' +
              (muted ? '<path d="M3 3l18 18"/>' : '<path d="M8 9h8M8 12h5"/>') + '</svg>';
            row.appendChild(mute);
          }
          row.appendChild(el("div", "tm-name", m.name));
          if (m.is_captain) { row.appendChild(el("span", "tm-cap", t("comp_captain"))); }
          // REPORT. teamsBlock is shared by the lobby, the connect window and the live screen, so
          // one button here is three of the four places Sam asked for. Never against yourself:
          // the server refuses it anyway, and offering it is just a trap.
          if (!m.is_me && m.steam_id) { row.appendChild(reportButton(m.steam_id, m.name)); }
          col.appendChild(row);
        });
        // Say WHY the names are missing, on the column that is missing them. Without the line a
        // roster of call signs reads as a bug, and the first thing a player does with a bug is
        // wait for it to resolve into real names.
        if (anyHidden) { col.appendChild(el("div", "team-anon-note", t("comp_hidden_note"))); }
        cols.appendChild(col);
      });
      return cols;
    }

    // A small, quiet control. Reporting is rare and serious; it should be findable next to the
    // person it is about and never compete with the buttons people press every match.
    function reportButton(steamId, name) {
      var comp = state.comp || {};
      return ui.btn("tm-report", t("comp_report_player"),
        function () { call("open_report", steamId, comp.match_id || "", name || ""); },
        { tag: "button", ariaLabel: t("comp_report_player") });
    }

    // Only the roster and message history scroll. All map choices and the composer stay visible.
    function lobbyPane(comp) {
      var lb = comp.lobby || {};
      var matchId = String(lb.chat_epoch || 0);
      if (chatMatch !== matchId) {
        chatDrafts = {};
        scrollMemory = {};
        chatMatch = matchId;
      }
      var wrap = el("div", "lobby-pane");
      var top = el("div", "lobby-top");
      top.appendChild(teamsBlock(lb.teams, lb.stage === "veto" || lb.stage === "ready", lb));
      restoreScroll.push(keepScroll(top, "lobby-top", false));
      wrap.appendChild(top);
      if (comp.mode_id !== "BB1" && (lb.stage === "veto" || lb.stage === "ready")) { wrap.appendChild(vetoList(lb)); }
      wrap.appendChild(chatBlock(lb, matchId));
      return wrap;
    }

    function vetoList(lb) {
      var veto = lb.veto || {}, pool = veto.pool || [], bans = veto.bans || [];
      var bannedBy = {};
      bans.forEach(function (b) { bannedBy[b.map] = b.team; });
      var box = el("div", "veto");
      box.appendChild(el("div", "section-title", t("comp_veto_title")));
      if (lb.stage === "ready" || lb.map) {
        box.appendChild(el("div", "veto-turn", t("comp_veto_done", { map: lb.map })));
      } else if (lb.my_turn) {
        box.appendChild(el("div", "veto-turn strong", t("comp_veto_your_turn")));
      } else {
        box.appendChild(el("div", "veto-turn", t("comp_veto_turn", { name: lb.ban_turn_name })));
      }
      // THE BAN CLOCK, drawn for everyone rather than only the captain on it: the other nine are
      // the ones wondering whether anything is happening, and a number answers that better than a
      // name. Urgent in the last ten seconds, like the accept clock. It only DRAWS - when it hits
      // zero the server has already banned for the absent captain and the next lobby payload
      // carries the result.
      if (lb.stage_seconds > 0 && !lb.map) {
        box.appendChild(el("div", "veto-clock" + (lb.stage_seconds <= 10 ? " urgent" : ""),
          t("comp_veto_clock", { time: clock(lb.stage_seconds) })));
      }
      var list = el("div", "veto-list");
      pool.forEach(function (name) {
        var isBanned = Object.prototype.hasOwnProperty.call(bannedBy, name);
        var isFinal = lb.map === name;
        var clickable = lb.my_turn && !isBanned && !isFinal;
        var row = ui.btn("veto-map" + (isBanned ? " banned" : "") + (isFinal ? " final" : ""),
          undefined, clickable ? function () { call("ban_map", name); } : null,
          { tag: clickable ? "button" : "span" });
        // The confirmed ban plays for the whole lobby through the snapshot sound handler.
        row.setAttribute("data-click-sound", "off");
        row.appendChild(ui.mapImage(name, "vm-image"));
        row.appendChild(el("span", "vm-name", (isBanned ? "× " : "") + name));
        if (isBanned) { row.appendChild(el("span", "vm-by", t("comp_banned_by", { n: bannedBy[name] }))); }
        if (!clickable && !isFinal) { row.setAttribute("aria-disabled", "true"); }
        list.appendChild(row);
      });
      box.appendChild(list);
      return box;
    }

    function chatBlock(lb, matchId) {
      var channel = lb.chat_channel === "all" ? "all" : "team";
      var title = channel === "all" ? t("comp_chat_all_title") : t("comp_chat_title");
      var logId = "lobby-chat-log-" + matchId;
      var wrap = el("div", "chat");
      var log = el("div", "chat-log");
      log.id = logId;
      log.setAttribute("role", "log");
      log.setAttribute("aria-label", t("comp_chat_log"));
      // The session merges receipt order and filters muted senders without exposing personas.
      (lb.messages || []).forEach(function (m) {
        var line = el("div", "chat-line");
        line.appendChild(el("span", "chat-audience", "(" +
          t(m.channel === "all" ? "comp_chat_all" : "comp_chat_team") + ") "));
        if (m.name) { line.appendChild(el("span", "chat-name", m.name + ": ")); }
        line.appendChild(document.createTextNode(m.text));
        log.appendChild(line);
      });
      wrap.appendChild(log);
      restoreScroll.push(keepScroll(log, logId, true));

      var row = el("div", "chat-row");
      var channels = el("div", "chat-channels");
      channels.setAttribute("role", "group");
      channels.setAttribute("aria-label", t("comp_chat_audience"));
      var input = document.createElement("input");
      // Separate draft ids prevent a private team draft from silently becoming an all-chat message.
      var draftPrefix = "lobby-chat-input-" + matchId + "-";
      input.id = draftPrefix + channel;
      input.value = chatDrafts[input.id] || "";
      input.type = "text";
      input.maxLength = 200;
      input.placeholder = title;
      input.setAttribute("aria-label", title);
      input.addEventListener("input", function () { chatDrafts[input.id] = input.value; });
      ["team", "all"].forEach(function (destination) {
        var toggle = ui.btn("chat-channel", t(destination === "all" ? "comp_chat_all" : "comp_chat_team"),
          function () {
            if (channel === destination) { return; }
            chatDrafts[input.id] = input.value;
            // Switch the draft and destination together, before the asynchronous snapshot arrives.
            channel = destination;
            input.id = draftPrefix + channel;
            input.value = chatDrafts[input.id] || "";
            input.placeholder = channel === "all" ? t("comp_chat_all_title") : t("comp_chat_title");
            input.setAttribute("aria-label", input.placeholder);
            Array.prototype.forEach.call(channels.children, function (button) {
              button.setAttribute("aria-pressed", String(button.dataset.channel === channel));
            });
            call("set_chat_channel", channel);
            input.focus();
          }, { tag: "button" });
        toggle.dataset.channel = destination;
        toggle.setAttribute("aria-pressed", String(channel === destination));
        channels.appendChild(toggle);
      });
      row.appendChild(channels);
      function send() {
        var v = (input.value || "").trim();
        if (v) { call("send_chat", v, channel); input.value = ""; chatDrafts[input.id] = ""; }
      }
      input.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.isComposing) { send(); } });
      row.appendChild(input);
      row.appendChild(ui.btn("btn-solid", t("comp_chat_send"), send, { tag: "button" }));
      wrap.appendChild(row);
      return wrap;
    }

    function connectPane(comp) {
      var c = comp.connect || {}, host = c.host || {};
      var wrap = el("div", "connect-pane");
      var info = el("div", "connect-info");
      if (c.map) { info.appendChild(el("div", "ci-map", t("comp_connect_map", { map: c.map }))); }
      if (host.name) { info.appendChild(el("div", "ci-host", t("comp_connect_host", { name: host.name }))); }
      if (info.childNodes.length) { wrap.appendChild(info); }
      wrap.appendChild(teamsBlock(c.teams, true));
      return wrap;
    }

    function livePane(comp) {
      var lv = comp.live || {};
      var wrap = el("div", "live-pane");
      wrap.appendChild(teamsBlock(lv.teams, true));
      if (lv.can_concede) {
        var copy=(state.comp||{}).mode_strings||{};
        wrap.appendChild(ui.btn("btn-solid ghost",copy.concede,function(){if(window.confirm(copy.concede_confirm))call("concede_match");},{tag:"button"}));
      } else if (lv.can_vote && lv.vote) {
        wrap.appendChild(voteCard(lv.vote));
      } else if (lv.can_vote) {
        wrap.appendChild(ui.btn("btn-solid ghost void-vote-button", t("comp_report"), function () { call("start_vote"); }, { tag: "button" }));
      }
      return wrap;
    }

    function voteCard(v) {
      var card = el("div", "vote-card");
      card.appendChild(el("div", "section-title", t("comp_vote_title")));
      card.appendChild(el("div", "vote-body", t("comp_vote_body")));
      card.appendChild(el("div", "vote-count", t("comp_vote_count", { n: v.yes || 0, needed: v.needed || 6 })));
      if (v.pending) {
        card.appendChild(el("div", "vote-body", t("comp_vote_pending")));
      } else if (!v.voted) {
        card.appendChild(actionRow([
          ui.btn("btn-solid", t("comp_vote_yes"), function () { call("cast_vote", true); }, { tag: "button" }),
          ui.btn("btn-solid ghost", t("comp_vote_no"), function () { call("cast_vote", false); }, { tag: "button" })
        ]));
      } else {
        card.appendChild(el("div", "vote-body", t("comp_vote_cast")));
      }
      return card;
    }

    function paneInfo() {
      // The signed-OUT pane. It draws from `strings` alone: `comp` is a local of the functions
      // that draw the signed-IN screen, and a reference to it from here threw before
      // buildCompetitive could return - which rendered the whole pre-sign-in screen, hero
      // included, as an empty black page. Read `state.comp` yourself if you ever need it.
      var p = el("div", "pane");
      var readiness = readinessStrip();
      if (readiness) { p.appendChild(readiness); }
      return p;
    }

    function leaderName(party) {
      var members = (party && party.members) || [];
      for (var i = 0; i < members.length; i++) { if (members[i].is_leader) { return members[i].name; } }
      return (state.auth || {}).persona || "?";
    }

    function partySection() {
      var party = state.party || {};
      var wrap = el("div", "party");

      var head = el("div", "section-head");
      var count = party.in_party ? t("comp_party_count", { n: party.size, max: party.max }) : t("comp_party_solo");
      head.innerHTML = '<div class="section-title">' + esc(t("comp_party_title")) +
        ' <span class="dim">· ' + esc(count) + "</span></div>";
      if (party.in_party) {
        head.appendChild(btn("link-muted", t("comp_party_leave"), function () { call("leave_party"); }));
      }
      wrap.appendChild(head);

      if (party.error) { wrap.appendChild(el("div", "party-error", party.error)); }

      if (!party.in_party) {
        // solo: create, or join by code
        var solo = el("div", "solo-actions");
        solo.appendChild(btn("btn-solid", t("comp_party_create"), function () { call("create_party"); }));
        wrap.appendChild(solo);

        var join = el("div", "join-row");
        var input = document.createElement("input");
        input.id = "join-code-input";
        input.type = "text"; input.maxLength = 8; input.placeholder = t("comp_party_code");
        input.setAttribute("aria-label", t("comp_party_code"));
        input.addEventListener("keydown", function (e) { if (e.key === "Enter") { doJoin(input.value); } });
        join.appendChild(input);
        join.appendChild(btn("btn-solid", t("comp_party_join_button"), function () { doJoin(input.value); }));
        wrap.appendChild(join);

        wrap.appendChild(el("div", "party-note", t("comp_party_solo_note")));
        return wrap;
      }

      // in a party: code row + Copy / Hide / New code, member grid
      var codeRow = el("div", "code-row");
      var shown = party.hidden ? party.code_masked : party.code;
      codeRow.appendChild(el("div", "code", shown));
      codeRow.appendChild(btn("chip", t("comp_party_copy"), function () { copyCode(party.code); }));
      codeRow.appendChild(btn("chip", party.hidden ? t("comp_party_show") : t("comp_party_hide"),
        function () { call("toggle_hide_code"); }));
      if (party.is_leader) {
        codeRow.appendChild(btn("chip", t("comp_party_new_code"), function () { call("refresh_party_code"); }));
      }
      wrap.appendChild(codeRow);

      var note = t("comp_party_share") + " " + t("comp_party_new_code_note");
      wrap.appendChild(el("div", "party-note", note));

      wrap.appendChild(members(party));

      // INVITE FRIENDS. Only once there is a party to invite into, and only while there is a seat
      // free (Sam, 2026-09-15). Opening it asks for a fresh friends list: the list is fetched when
      // the Friends screen is opened and nowhere else, so a player who has not been there yet
      // would open an empty picker.
      if (party.can_invite) {
        var inviteRow = el("div", "invite-actions");
        inviteRow.appendChild(btn("btn-solid", invitesOpen ? t("comp_invite_hide") : t("comp_invite_button"),
          function () {
            invitesOpen = !invitesOpen;
            // Ask for a fresh list on the way open; redraw either way. A local re-render, the
            // same move the leaderboard's tabs make: the core owns the state push, not the tab,
            // and nothing about which list is unfolded belongs in the snapshot.
            if (invitesOpen) { call("open_invites"); }
            root.innerHTML = "";
            render(root, state, ctx);
          }, { tag: "button" }));
        wrap.appendChild(inviteRow);
        if (invitesOpen) { wrap.appendChild(invitePicker(party)); }
      } else if (invitesOpen) {
        invitesOpen = false;                 // the party filled up while it was open
      }
      return wrap;
    }

    // The picker: every friend, with the reason an un-pressable row cannot be pressed. See
    // _invite_candidates in the Python half for why the offline ones are listed at all.
    function invitePicker(party) {
      var box = el("div", "invite-picker");
      box.appendChild(el("div", "invite-title", t("comp_invite_title")));
      if (party.invite_error) { box.appendChild(el("div", "party-error", party.invite_error)); }
      var rows = party.friends || [];
      if (!rows.length) {
        box.appendChild(el("div", "party-note", t("comp_invite_none")));
        return box;
      }
      rows.forEach(function (f) {
        var row = el("div", "invite-row");
        var who = el("div", "invite-who");
        who.appendChild(el("span", "invite-dot" + (f.online ? " on" : "")));
        who.appendChild(el("span", "invite-name", f.name));
        row.appendChild(who);
        if (f.in_party) {
          row.appendChild(el("span", "invite-state", t("comp_invite_in_party")));
        } else if (!f.online) {
          row.appendChild(el("span", "invite-state", t("comp_invite_offline")));
        } else if (f.invited) {
          row.appendChild(el("span", "invite-state", t("comp_invite_sent")));
        } else {
          row.appendChild(btn("chip", t("comp_invite_send"), function () {
            call("invite_friend", f.steam_id);
          }));
        }
        box.appendChild(row);
      });
      box.appendChild(el("div", "party-note", t("comp_invite_note")));
      return box;
    }

    // The inbox. Returns null when there is nothing in it, so the pane draws no empty card.
    function inviteInbox() {
      var party = state.party || {};
      var rows = party.invites || [];
      if (!rows.length) { return null; }
      var box = el("div", "party invite-inbox");
      box.appendChild(el("div", "section-title", t("comp_invites_title")));
      rows.forEach(function (inv) {
        var row = el("div", "invite-row");
        var who = el("div", "invite-who");
        who.appendChild(el("span", "invite-name", t("comp_invite_row", { name: inv.name, n: inv.size })));
        if (inv.expires_in) {
          who.appendChild(el("span", "invite-state", t("comp_invite_expires", { n: inv.expires_in })));
        }
        row.appendChild(who);
        var acts = el("div", "invite-acts");
        acts.appendChild(btn("chip solid", t("comp_invite_accept"), function () {
          call("invite_accept", inv.steam_id);
        }));
        acts.appendChild(btn("chip", t("comp_invite_decline"), function () {
          call("invite_decline", inv.steam_id);
        }));
        row.appendChild(acts);
        box.appendChild(row);
      });
      if (party.invite_error) { box.appendChild(el("div", "party-error", party.invite_error)); }
      return box;
    }

    function members(party) {
      var grid = el("div", "members");
      var mine = (state.auth || {}).steam_id;
      var list = party.members || [];
      for (var i = 0; i < party.max; i++) {
        if (i < list.length) {
          var m = list[i];
          var cls = "member" + (m.is_leader ? " leader" : "") + (m.steam_id === mine ? " you" : "");
          var cell = el("div", cls);
          // Built as NODES, not innerHTML: the plate carries an <img> of the player's Steam avatar
          // now (ui.avatar), which needs an error handler to fall back to their initials, and a
          // string of HTML cannot carry one. It also used to print the level number, which was
          // invented client-side for anyone the server had not rated.
          cell.appendChild(ui.avatar({ url: m.avatar, text: initials(m.name),
                                       light: m.steam_id === mine, ariaLabel: m.name }));
          cell.appendChild(el("div", "nm", m.name));
          var role = el("div", "role");
          role.innerHTML = m.is_leader ? esc(t("comp_party_leader")) : "&nbsp;";
          cell.appendChild(role);
          grid.appendChild(cell);
        } else {
          var slot = el("div", "slot");
          slot.innerHTML = '<div class="box"></div>';
          grid.appendChild(slot);
        }
      }
      return grid;
    }

    // ---------------------------------------------------------------- actions
    function doJoin(code) {
      code = (code || "").trim();
      if (code) { call("join_party", code); }
    }

    function copyCode(code) {
      // Copy in JS (no CDN, no Python clipboard dep). navigator.clipboard is available in WebView2
      // and WKWebView; fall back to a hidden textarea if it is not.
      var done = function () { ui.toast(t("comp_party_copied")); };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(code).then(done, function () { legacyCopy(code, done); });
        } else { legacyCopy(code, done); }
      } catch (e) { legacyCopy(code, done); }
    }
    function legacyCopy(text, done) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select(); document.execCommand("copy");
        document.body.removeChild(ta); done();
      } catch (e) { /* ignore */ }
    }
  }
  // ================================================================ the report box (AN OVERLAY)
  //
  // NOT A PART OF THIS SCREEN, even though this file owns it. The report box is opened from the
  // post-match roster, from the lobby and connect rosters, from the post-match card AND from a
  // player row in match history - and it belongs to `comp.report_target`, which is in the snapshot
  // on every view.
  //
  // SAM, 2026-09-16: "whenever i hit the report button on a player in my match history it doesnt
  // open until i go to the competitive tab. the screen that should appear once a user presses
  // report should appear anywhere."  Nothing about the press was broken: open_report reached the
  // session, report_target came back in the very next snapshot, and every screen had it. What was
  // missing was anyone to DRAW it - the box was appended by render() above, which the router only
  // runs while competitive is the active view. So the accusation sat in the state, invisible,
  // until the player wandered onto the one screen that knew how to turn it into a modal, and then
  // it appeared a tab late. Registered here it is drawn on every render whatever state.view says,
  // over whatever is on screen - including the history screen's match-detail modal, which is
  // where the press came from.
  window.HubUI.registerOverlay("report", { render: renderReport });
  var reportDraft = null;

  function renderReport(state, ctx) {
    var comp = (state && state.comp) || {};
    if (!comp.report_target) { reportDraft = null; return; }
    var key = comp.report_target + ":" + (comp.report_match || "") + ":" + (comp.report_seq || 0);
    if (!reportDraft || reportDraft.key !== key) { reportDraft = { key: key, other: false, text: "" }; }
    document.body.appendChild(reportModal(comp, ctx));
  }

  // Standard reasons send immediately; Other asks for an explanation before sending.
  //
  // MODULE SCOPE, and it takes its ctx rather than closing over render()'s: the core's overlay
  // pass calls it, not a render of this screen - and on the history tab there is no render of
  // this screen at all, so there is no enclosing ui/call/t for it to reach.
  function reportModal(comp, ctx) {
    var ui = ctx.ui, call = ctx.call, t = ctx.t, el = ui.el;
    var target = comp.report_target;
    // WHO, IN WORDS. The rosters only know the people in THIS match, so a report opened from
    // history used to fall all the way through to the raw steam id - a 17-digit number standing
    // where the name of the person being accused should be. The opener passes the name it already
    // has on screen (open_report's third argument -> comp.report_name); the roster lookup stays as
    // the fallback for any surface that does not, and the id stays as the last resort.
    var who = (comp.roster || []).concat(
      ((comp.lobby || {}).teams || []).reduce(function (acc, tm) {
        return acc.concat(tm.members || []);
      }, []));
    var name = comp.report_name ||
      (who.find(function (p) { return p.steam_id === target; }) || {}).name || target;

    // EVERY KEY SPELLED OUT. Concatenating the suffix onto the prefix hid these keys from the
    // check that every string the JS asks for exists in all seven languages, and it meant an
    // unknown reason from the server would render as a raw key on screen. The map falls back
    // instead. (The check scans for the literal, so this comment must not contain one either -
    // which is how it caught the first version.)
    var REASON_KEY = {
      cheating: "comp_reason_cheating",
      text_abuse: "comp_reason_text_abuse",
      voice_abuse: "comp_reason_voice_abuse",
      afk: "comp_reason_afk",
      griefing: "comp_reason_griefing",
      team_killing: "comp_reason_team_killing",
      smurfing: "comp_reason_smurfing",
      other: "comp_reason_other"
    };
    var list = el("div", "rep-reasons");
    var custom = el("div", "rep-custom");
    custom.hidden = !reportDraft.other;
    var label = el("label", null, t("comp_report_details"));
    label.htmlFor = "report-details";
    var box = el("textarea", "rep-details");
    box.id = "report-details";
    box.maxLength = 1000;
    box.rows = 4;
    box.value = reportDraft.text;
    var submit = ui.btn("rep-reason", t("comp_report_submit"), function () {
      if (!box.value.trim()) { box.focus(); return; }
      call("report", target, "other", comp.report_match || "", box.value.trim());
    }, { tag: "button" });
    submit.setAttribute("data-click-sound", "report");
    submit.disabled = !box.value.trim();
    box.addEventListener("input", function () {
      reportDraft.text = box.value;
      submit.disabled = !box.value.trim();
    });
    custom.appendChild(label);
    custom.appendChild(box);
    custom.appendChild(submit);
    (comp.report_reasons || []).forEach(function (reason) {
      var reasonButton = ui.btn("rep-reason", t(REASON_KEY[reason] || "comp_reason_other"), function () {
        if (reason === "other") {
          reportDraft.other = true;
          custom.hidden = false;
          box.focus();
          return;
        }
        call("report", target, reason, comp.report_match || "");
      }, { tag: "button" });
      if (reason !== "other") { reasonButton.setAttribute("data-click-sound", "report"); }
      list.appendChild(reasonButton);
    });

    var children = [el("div", "rep-who", name), list, custom];
    if (comp.report_error) { children.push(el("div", "rep-error", comp.report_error)); }
    children.push(el("div", "rep-note", t("comp_report_note")));

    var overlay = ui.modal({
      title: t("comp_report_title"),
      children: children,
      onClose: function () { call("close_report"); }
    });
    overlay.classList.add("rep-overlay");
    var dialog = overlay.querySelector(".ui-modal");
    if (dialog) {
      dialog.setAttribute("tabindex", "-1");
      setTimeout(function () {
        if (!dialog.isConnected) { return; }
        if (reportDraft && reportDraft.other) { box.focus(); } else { dialog.focus(); }
      }, 0);
    }
    return overlay;
  }
})();
