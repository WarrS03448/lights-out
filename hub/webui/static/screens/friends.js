/* The persistent Friends dock.
 *
 * Draws from state.friends (hub/webui/screens/friends.py) and sends verbs; nothing here decides
 * anything. A friendship has two sides and only the server can see both, so there is no optimistic
 * update: every action refetches and the list redraws from the answer.
 *
 * The dock fetches once per signed-in account and then only when the server says something
 * changed (the friend_update nudge -> session.refresh_friends). No polling: a list that changes
 * when somebody else presses a button is exactly what the SSE stream is for.
 */
(function () {
  "use strict";
  var fetchedFor = null;
  var account = null;
  var opened = false;
  var draft = "";
  var lastSnapshot = "";
  var lastView = "";
  var latestState, latestContext;
  var menu = null, menuOwner = null;
  var scrollPositions = {};

  function closeMenu(restoreFocus) {
    if (menu) { menu.remove(); menu = null; }
    if (restoreFocus && menuOwner && menuOwner.isConnected) { menuOwner.focus({ preventScroll: true }); }
    menuOwner = null;
  }

  function setOpen(value) {
    opened = value;
    closeMenu(false);
    renderDock(latestState, latestContext, true);
    var focus = document.getElementById(value ? "fr-minimize" : "friends-toggle");
    if (focus) { focus.focus({ preventScroll: true }); }
  }

  // The dock lives outside #app so switching screens never discards it or a draft code.
  function renderDock(state, ctx, force) {
    var root = document.getElementById("friends-dock");
    if (!root) { return; }
    latestState = state; latestContext = ctx;
    var f = state.friends || {};
    var accountKey = f.signed_in ? String((state.auth || {}).steam_id || "signed-in") : "";
    if (account !== accountKey) {
      account = accountKey; fetchedFor = null; draft = ""; scrollPositions = {}; opened = false;
      force = true;
    }
    if (state.view === "friends" && lastView !== "friends") { opened = true; force = true; }
    lastView = state.view;
    if (f.signed_in && fetchedFor !== accountKey) {
      fetchedFor = accountKey;
      ctx.call("friends_refresh");
    }
    var signature = JSON.stringify(f);
    if (!force && signature === lastSnapshot && root.firstChild) { return; }
    lastSnapshot = signature;
    var active = document.activeElement;
    var focusId = root.contains(active) ? active.id : "";
    var selection = focusId === "fr-code-input" ? [active.selectionStart, active.selectionEnd] : null;
    closeMenu(false);
    root.innerHTML = "";
    var ui = ctx.ui, strings = f.strings || {}, title = strings.title || "Friends";
    root.setAttribute("aria-label", title);
    var toggle = ui.btn("fr-toggle", "", function () { setOpen(!opened); }, { ariaLabel: title });
    toggle.id = "friends-toggle";
    toggle.setAttribute("aria-expanded", String(opened));
    toggle.setAttribute("aria-controls", "friends-panel");
    toggle.title = title;
    var icon = ui.el("span", "fr-toggle-icon");
    icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor"><circle cx="8" cy="6" r="3.5"/><path d="M2 22v-4a6 6 0 0 1 12 0v4Z"/><circle cx="19.5" cy="11" r="2.5"/><path d="M16 22v-3a3.5 3.5 0 0 1 7 0v3Z"/></svg>';
    toggle.appendChild(icon);
    var online = (f.list || []).filter(function (p) { return p.online; }).length;
    var count = ui.el("span", "fr-toggle-count", String(online));
    count.setAttribute("aria-label", online + " " + (strings.online || "Online"));
    toggle.appendChild(count);
    if ((f.incoming || []).length) {
      var requests = ui.el("span", "fr-toggle-requests", String(f.incoming.length));
      requests.setAttribute("aria-label", f.incoming.length + " " + strings.incoming);
      toggle.appendChild(requests);
    }
    root.appendChild(toggle);
    var panel = ui.el("section", "fr-panel");
    panel.id = "friends-panel";
    panel.hidden = !opened;
    panel.setAttribute("aria-label", title);
    root.appendChild(panel);
    if (opened) { render(panel, state, ctx); }
    if (focusId) {
      var replacement = document.getElementById(focusId);
      if (replacement) {
        replacement.focus({ preventScroll: true });
        if (selection) { replacement.setSelectionRange(selection[0], selection[1]); }
      }
    }
  }

  document.addEventListener("pointerdown", function (e) {
    if (menu && !menu.contains(e.target)) { closeMenu(false); }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape" || e.defaultPrevented) { return; }
    if (menu) { e.preventDefault(); closeMenu(true); }
    else if (opened && document.getElementById("friends-dock").contains(e.target)) {
      e.preventDefault(); setOpen(false);
    }
  });

  function render(root, state, ctx) {
    var ui = ctx.ui, call = ctx.call, el = ui.el;
    var f = state.friends || {};
    var S = f.strings || {};
    function ft(key, kw) {
      var s = S[key] || key;
      if (kw) {
        s = s.replace(/\{(\w+)\}/g, function (m, name) {
          return (kw[name] === undefined || kw[name] === null) ? m : String(kw[name]);
        });
      }
      return s;
    }

    var wrap = el("div", "fr-wrap");
    var friends = f.list || [];
    var header = el("header", "fr-header");
    header.appendChild(el("h2", "fr-title", ft("title")));
    var presence = el("div", "fr-presence");
    presence.appendChild(el("span", "fr-dot on"));
    presence.appendChild(el("span", "", friends.filter(function (p) { return p.online; }).length + " " + ft("online")));
    header.appendChild(presence);
    var minimize = ui.btn("fr-mini fr-minimize", "", function () { setOpen(false); }, { ariaLabel: ft("minimize") });
    // Center the icon independently of the display font's baseline.
    minimize.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true" fill="none"><path d="M5.5 3 10.5 8 5.5 13" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    minimize.id = "fr-minimize";
    minimize.title = ft("minimize");
    header.appendChild(minimize);
    wrap.appendChild(header);
    root.appendChild(wrap);
    if (!f.signed_in) {
      wrap.appendChild(ui.emptyState({ icon: "◆", message: ft("signed_out") }));
      return;
    }
    var tools = el("div", "fr-tools");
    tools.appendChild(codeCard());
    tools.appendChild(addCard());
    wrap.appendChild(tools);
    if (f.error) { wrap.appendChild(el("div", "fr-error", f.error)); }
    if (f.invite_error) { wrap.appendChild(el("div", "fr-error", f.invite_error)); }
    var layout = el("div", "fr-layout");
    var main = el("div", "fr-main");
    main.appendChild(section(ft("list"), friends, "friend", ft("empty")));
    var side = el("aside", "fr-side");
    if ((f.incoming || []).length) { side.appendChild(section(ft("incoming"), f.incoming, "incoming", ft("no_incoming"))); }
    if ((f.outgoing || []).length) { side.appendChild(section(ft("outgoing"), f.outgoing, "outgoing", ft("no_outgoing"))); }
    layout.appendChild(main);
    layout.appendChild(side);
    wrap.appendChild(layout);

    // ---------------------------------------------------------------- the code
    function codeCard() {
      var card = el("div", "fr-card fr-codecard");
      var head = el("div", "fr-codehead");
      head.appendChild(el("div", "fr-h", ft("code_label")));
      var newCode = ui.btn("fr-mini danger", ft("new_code"), function () { call("friend_code_new"); });
      newCode.title = ft("new_code_hint");
      head.appendChild(newCode);
      card.appendChild(head);

      var row = el("div", "fr-coderow");
      // HIDDEN BY DEFAULT and masked here rather than withheld by Python, so Copy can work while
      // it is hidden - the value is already on this machine; what is being protected is the
      // SCREEN, for someone streaming.
      row.appendChild(el("span", "fr-code" + (f.code_hidden ? " masked" : ""),
        f.code ? (f.code_hidden ? f.code_masked : f.code) : "—"));
      row.appendChild(ui.btn("fr-mini", f.code_hidden ? ft("show") : ft("hide"),
        function () { call("friend_code_toggle"); }, { tag: "button" }));
      row.appendChild(ui.btn("fr-mini", ft("copy"), function () {
        if (!f.code) { return; }
        copyText(f.code, function () { ui.toast(ft("copied")); });
      }, { tag: "button" }));
      card.appendChild(row);

      row.title = ft("code_hint");
      return card;
    }

    // ---------------------------------------------------------------- add by code
    function addCard() {
      var card = el("div", "fr-card fr-addcard");
      card.appendChild(el("div", "fr-h", ft("add_label")));
      var row = el("div", "fr-coderow");
      var input = document.createElement("input");
      input.className = "fr-input";
      input.type = "text";
      input.placeholder = ft("add_placeholder");
      input.setAttribute("aria-label", ft("add_label"));
      input.maxLength = 24;
      input.id = "fr-code-input";
      input.value = draft;
      input.addEventListener("input", function () { draft = input.value; });
      function send() {
        var v = (input.value || "").trim();
        if (!v) { return; }
        call("friend_add", v);
        draft = "";
        input.value = "";
      }
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") { send(); } });
      row.appendChild(input);
      row.appendChild(ui.btn("fr-mini solid", ft("add"), send, { tag: "button" }));
      card.appendChild(row);
      return card;
    }

    // Copy in JS: no CDN and no Python clipboard dependency. navigator.clipboard exists in
    // WebView2; the textarea is the fallback when it does not (same approach as the party code).
    function copyText(text, done) {
      var fallback = function () {
        try {
          var ta = document.createElement("textarea");
          ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select(); document.execCommand("copy");
          document.body.removeChild(ta); done();
        } catch (e) { /* no clipboard: the code is on screen anyway once shown */ }
      };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(done, fallback);
        } else { fallback(); }
      } catch (e) { fallback(); }
    }

    // ---------------------------------------------------------------- lists
    function section(title, rows, kind, emptyText) {
      var box = el("section", "fr-card fr-section fr-section-" + kind);
      var head = el("div", "fr-section-head");
      head.appendChild(el("h2", "fr-h", title));
      head.appendChild(el("span", "fr-count", kind === "friend" ? ft("count", { n: rows.length, max: f.max }) : String(rows.length)));
      box.appendChild(head);
      var list = el("div", "fr-list");
      list.tabIndex = 0;
      list.setAttribute("role", "region");
      list.setAttribute("aria-label", title);
      box.appendChild(list);
      list.addEventListener("scroll", function () { scrollPositions[kind] = list.scrollTop; });
      list.addEventListener("scroll", function () { closeMenu(false); });
      setTimeout(function () { list.scrollTop = scrollPositions[kind] || 0; }, 0);
      if (!rows.length) {
        var empty = el("div", "fr-empty");
        if (kind === "friend") {
          var mark = el("div", "fr-empty-mark", "＋");
          mark.setAttribute("aria-hidden", "true");
          empty.appendChild(mark);
        }
        empty.appendChild(el("div", "fr-note", emptyText));
        list.appendChild(empty);
        return box;
      }
      rows.forEach(function (p) { list.appendChild(personRow(p, kind)); });
      return box;
    }

    function personRow(p, kind) {
      var row = el("div", "fr-row" + (p.online ? " is-online" : ""));
      var who = el("div", "fr-who");
      var name = p.persona || p.steam_id;
      var avatar = ui.avatar({ url: p.avatar, text: ui.initials(name) });
      avatar.classList.add("fr-avatar");
      avatar.setAttribute("aria-hidden", "true");
      who.appendChild(avatar);
      var identity = el("div", "fr-identity");
      identity.appendChild(el("div", "fr-name", name));
      var status = el("div", "fr-status");
      status.appendChild(el("span", "fr-dot" + (p.online ? " on" : "")));
      status.appendChild(el("span", "fr-state", p.online ? ft("online") : ft("offline")));
      identity.appendChild(status);
      who.appendChild(identity);
      row.appendChild(who);

      var acts = el("div", "fr-acts");
      if (kind === "incoming") {
        acts.appendChild(ui.btn("fr-mini solid", ft("accept"),
          function () { call("friend_accept", p.steam_id); }, { tag: "button" }));
        acts.appendChild(ui.btn("fr-mini", ft("decline"),
          function () { call("friend_decline", p.steam_id); }, { tag: "button" }));
      } else if (kind === "outgoing") {
        acts.appendChild(ui.btn("fr-mini", ft("cancel"),
          function () { call("friend_cancel", p.steam_id); }, { tag: "button" }));
      } else {
        if (p.in_party) {
          acts.appendChild(el("span", "fr-state fr-badge", ft("in_party")));
        } else if (p.invited) {
          acts.appendChild(el("span", "fr-state fr-badge", ft("invited")));
        }
        var more = el("span", "fr-more", "⋯");
        more.setAttribute("aria-hidden", "true");
        acts.appendChild(more);
        row.id = "fr-friend-" + p.steam_id;
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-haspopup", "menu");
        row.setAttribute("aria-label", ft("actions_for", { name: name }));
        row.addEventListener("contextmenu", function (e) { e.preventDefault(); openMenu(p, row, e.clientY); });
        row.addEventListener("click", function () { openMenu(p, row); });
        row.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " " || e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
            e.preventDefault(); openMenu(p, row);
          }
        });
      }
      row.appendChild(acts);
      return row;
    }

    function openMenu(p, row, clientY) {
      closeMenu(false);
      menuOwner = row;
      menu = el("div", "fr-menu");
      menu.setAttribute("role", "menu");
      menu.setAttribute("aria-label", ft("actions_for", { name: p.persona || p.steam_id }));
      function action(label, verb, disabled, cls) {
        var item = ui.btn("fr-menu-item" + (cls ? " " + cls : ""), ft(label), function () {
          closeMenu(true); call(verb, p.steam_id);
        }, { disabled: disabled });
        item.setAttribute("role", "menuitem");
        menu.appendChild(item);
      }
      action("invite", "friend_invite", !p.can_invite || p.in_party || p.invited);
      var messageItem = ui.btn("fr-menu-item", ((state.messages || {}).strings || {}).new_message || "Message", function () {
        closeMenu(true); window.HubUI.openMessages(p.steam_id);
      });
      messageItem.setAttribute("role", "menuitem");
      menu.appendChild(messageItem);
      action("remove", "friend_remove", false, "danger");
      menu.addEventListener("keydown", function (e) {
        var items = Array.prototype.slice.call(menu.querySelectorAll("button:not(:disabled)"));
        var index = items.indexOf(document.activeElement);
        if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Home" || e.key === "End") {
          e.preventDefault();
          var next = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1
            : (index + (e.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
          items[next].focus();
        } else if (e.key === "Tab") { closeMenu(false); }
      });
      root.appendChild(menu);
      var bounds = root.getBoundingClientRect();
      var zoom = bounds.height / root.offsetHeight;
      var top = ((clientY === undefined ? row.getBoundingClientRect().bottom : clientY) - bounds.top) / zoom;
      menu.style.top = Math.max(8, Math.min(top, root.clientHeight - menu.offsetHeight - 8)) + "px";
      menu.querySelector("button:not(:disabled)").focus({ preventScroll: true });
    }
  }

  window.HubUI.registerOverlay("friends", { render: renderDock });
}());
