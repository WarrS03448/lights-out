/* Lights Out — front-end core: the bridge, the screen registry, and the router.
 *
 * SHARED, OWNED BY THE CORE. A screen agent does NOT edit this file. It:
 *   - plumbs the bridge (Python <-> JS) over a LOCALHOST HTTP CHANNEL (see hub/webui/httpbridge.py;
 *     pywebview's js_api/evaluate_js proved unreliable on WebView2):
 *       Python -> JS : the page polls GET /state (the snapshot) and GET /events?since=N (toasts)
 *       JS -> Python : call(verb, ...args) does POST /verb/<name> with a JSON array of args
 *   - owns the shared chrome: the nav (renderNav) and the top bar's counts (renderStatus)
 *   - dispatches the pushed snapshot to the screen module registered for state.view, calling
 *     module.render(root, state, ctx). ctx = { call(verb, ...), ui: HubUI, t, state }.
 *
 * A screen registers itself from its own file with HubUI.registerScreen(name, { render }). Loaded
 * as a plain <script> AFTER ui.js and BEFORE the screen modules (index.html). Nothing here is
 * authoritative: it renders the latest snapshot and calls verbs; the server/session decide. */
(function () {
  "use strict";

  var HubUI = window.HubUI || (window.HubUI = {});

  var state = null;                              // the last snapshot pushed from Python
  var app = document.getElementById("app");
  var navEl = document.getElementById("nav");
  var statusEl = document.getElementById("serverstatus");
  var queuedEl = document.getElementById("statqueued");
  var liveEl = document.getElementById("statlive");
  var registeredEl = document.getElementById("statregistered");

  // Nav is delegated: one listener on the container survives renderNav's innerHTML rewrites. A
  // click on an item switches the active screen via set_view; the re-emitted snapshot re-renders.
  navEl.addEventListener("click", function (e) {
    var item = e.target && e.target.closest && e.target.closest(".navitem");
    var v = item && item.getAttribute("data-view");
    if (v && !(state && state.view === v)) { call("set_view", v); }
  });

  // ---------------------------------------------------------------- window scaling
  // The screens lay out in real px and the whole page is zoomed to fit the window (the zoom
  // itself is `body { zoom: var(--ui-scale) }` in ui.css). A window smaller than the layout
  // needs therefore shows the SAME layout, smaller, instead of clipping whatever hangs off the
  // edge - which is what used to happen: at the minimum window size the competitive hero lost
  // its readiness text and put Find Match right on the bottom edge of the window.
  //
  // FIT_W / FIT_H are the smallest window the screens lay out correctly in, MEASURED rather than
  // guessed: 700 is the height the competitive screen (the tallest) needs for the hero to reach
  // Find Match and its readiness note without scrolling.
  //
  // Keep the measured scale ceiling: 800px minimum / 0.7 = 1142 logical px.
  // The four counts stay together; whole nav items wrap as needed and the header
  // grows to contain them. Seven-language browser checks cover 800–1400px with
  // large counts, loaded webfonts and the real window controls.
  //
  // DOWN ONLY (the Math.min with 1). Zooming past 1 would shrink the LOGICAL viewport below the
  // physical one, and the screens' width breakpoints (@media (max-width: 900px) and friends) are
  // evaluated against the physical viewport, which zoom does not move: a big window would start
  // laying a 1000px-wide design out in 800 logical px with no breakpoint firing to rescue it.
  var FIT_W = 1140, FIT_H = 700;
  var MIN_SCALE = 0.7;                   // past this the 11px labels stop being legible, so the
                                         // zoom stops and the screens' own scrollers take over
  var scale = null;

  function applyScale() {
    var fit = Math.min(window.innerWidth / FIT_W, window.innerHeight / FIT_H, 1);
    // Quantized to 1%: a drag-resize fires resize continuously, and re-zooming the page on every
    // pixel is a relayout of the whole app for a difference nobody can see.
    var next = Math.max(MIN_SCALE, Math.round(fit * 100) / 100);
    if (next === scale) { return; }
    scale = next;
    document.documentElement.style.setProperty("--ui-scale", String(next));
  }
  HubUI.applyScale = applyScale;         // exported for the console / a test, not for screens
  window.addEventListener("resize", applyScale);
  // Belt and braces: a ResizeObserver on <html> catches anything that changes the page's size
  // without a resize event (a DPI change when the window is dragged to a second monitor, the
  // host chrome resizing the view). Both paths land on the same idempotent applyScale, and the
  // 1% quantization means the common case - both firing for one drag - is a single relayout.
  if (window.ResizeObserver) {
    new ResizeObserver(applyScale).observe(document.documentElement);
  }
  applyScale();

  // ---------------------------------------------------------------- screen registry
  var screens = {};                              // view name -> { render(root, state, ctx) }
  HubUI._screens = screens;
  HubUI.registerScreen = function (name, module) {
    if (!name || !module || typeof module.render !== "function") {
      throw new Error("registerScreen(" + name + "): a module with a render(root,state,ctx) is required");
    }
    screens[name] = module;
  };

  // OVERLAYS are the screens that are not a view: something raised OVER whatever the player is
  // looking at, from a slice of the snapshot rather than from the nav. The post-match card is the
  // first - a match ends while you are on Profile and the result still has to reach you.
  //
  // An overlay module is registered exactly like a screen and gets the same ctx, but it renders
  // AFTER the active screen, appends to document.body, and is drawn on every render whatever
  // state.view says. It draws nothing when its slice is closed; there is no show/hide here,
  // because the state - not the DOM - decides (see clearOverlays below).
  var overlays = {};                             // name -> { render(state, ctx) }
  HubUI._overlays = overlays;
  HubUI.registerOverlay = function (name, module) {
    if (!name || !module || typeof module.render !== "function") {
      throw new Error("registerOverlay(" + name + "): a module with a render(state,ctx) is required");
    }
    overlays[name] = module;
  };

  // ---------------------------------------------------------------- bridge plumbing (HTTP)
  var lastStateText = null;                      // raw /state body last rendered (cheap de-dup)
  var lastEventSeq = 0;                           // highest event seq consumed
  var seenCombatWarnings = {};                    // evaluate_js + /events may replay one cue

  // call(verb, ...args) -> POST /verb/<name>. Fire-and-forget: the verb only schedules work on
  // Python's UI thread; the next /state poll re-syncs us, so a failed call is swallowed (the UI is
  // never authoritative). An immediate pull shortens the visible latency after a click.
  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    fetch("/verb/" + encodeURIComponent(name), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(args)
    }).then(function () { pullState(); }).catch(function () { /* poll will re-sync us */ });
  }

  function applySnap(snap) {
    if (!snap) { return; }
    var text = JSON.stringify(snap);
    if (text === lastStateText) { return; }       // unchanged: skip the full innerHTML rebuild
    lastStateText = text;
    state = snap;
    render();
  }

  // window.__hub stays defined so a stray evaluate_js push (if that channel ever works) also
  // renders and shares the de-dup; it is NOT the primary channel — the /state poll below is.
  window.__hub = {
    onState: function (snap) { applySnap(snap); },
    onEvent: function (ev) { handleEvent(ev); }
  };

  function toastWasShown(toast, text) {
    if (!toast || toast.textContent !== String(text)) { return false; }
    if (document.visibilityState !== "visible" || !document.hasFocus()) { return false; }
    var rect = toast.getBoundingClientRect();
    var viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    var viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    return rect.width > 0 && rect.height > 0 &&
      rect.right > 0 && rect.bottom > 0 &&
      rect.left < viewportWidth && rect.top < viewportHeight;
  }

  function handleEvent(ev) {
    if (!ev) { return; }
    if (ev.seq && ev.seq > lastEventSeq) { lastEventSeq = ev.seq; }
    if (ev.type === "toast" && ev.text) { HubUI.toast(ev.text, ev.ms); }
    if (ev.type === "combat_warning" && ev.text) {
      var key = String(ev.match_id || "") + ":" + String(ev.warning_id || "");
      if (seenCombatWarnings[key]) { return; }
      HubUI.toast(ev.text, ev.ms);
      // A receipt requires the foreground page to contain a non-empty toast inside the viewport.
      // Receiving an SSE or polling /events by itself proves nothing was shown to the player.
      var toast = document.getElementById("toast");
      if (!toastWasShown(toast, ev.text)) { return; }
      seenCombatWarnings[key] = true;
      if (ev.match_id && ev.warning_id) {
        call("ack_combat_warning", String(ev.match_id), String(ev.warning_id));
      }
    }
  }

  // Python -> JS is a poll: GET /state (the snapshot) and GET /events (transient toasts). The
  // snapshot render is de-duped on the raw body, so a poll that finds nothing new is nearly free.
  function pullState() {
    fetch("/state").then(function (r) { return r.text(); }).then(function (txt) {
      if (txt === lastStateText) { return; }
      var snap;
      try { snap = JSON.parse(txt); } catch (e) { return; }
      if (snap) { lastStateText = txt; state = snap; render(); }
    }).catch(function () {});
  }

  function pullEvents() {
    fetch("/events?since=" + lastEventSeq).then(function (r) { return r.json(); }).then(function (evs) {
      if (evs && evs.length) { evs.forEach(handleEvent); }
    }).catch(function () {});
  }

  pullState();
  pullEvents();
  setInterval(function () { pullState(); pullEvents(); }, 300);

  // ---------------------------------------------------------------- render
  function render() {
    if (!state) { return; }
    HubUI.setStrings(state.strings);
    if (HubUI.clickSound) {
      HubUI.clickSound.configure((state.settings || {}).click_sound);
      HubUI.clickSound.syncMapBans(state.comp);
    }
    renderNav();
    renderStatus();
    renderUpdate();
    renderGamemodeUpdate();
    // A full rebuild wipes any <input> the user may be mid-typing in (a periodic stats/online push
    // re-renders even though nothing they touched changed). Capture focused inputs' values +
    // selection by id, rebuild, then restore, so typing survives. Generalized from app.js's
    // join-code handling so every screen's inputs are protected.
    var saved = captureInputs();
    app.innerHTML = "";
    clearOverlays();
    renderScreen();
    renderOverlays();
    restoreInputs(saved);
  }

  // EVERY MODAL IS THE RENDER'S TO CLEAN UP. A screen appends its overlay to document.body rather
  // than into #app - the corner-panel versions ran off the bottom of the screen inside the grid -
  // and clearing #app therefore does not touch it. Nothing else removed it either, so each overlay
  // outlived the render that made it and the next render simply stacked another one on top.
  //
  // SAM, 2026-09-16: "in the match detail screen ... there is no way to exit". Exactly that. The
  // backdrop click and Escape both worked - they set the state to closed, the snapshot came back
  // with open_id "", and the next render simply did not append a NEW overlay. The seventeen
  // already in the body stayed exactly where they were, covering the nav, so the hub was stuck on
  // a screen it had already closed. It was never the match detail: the rank card, the report box
  // and the ruleset sheet all leak the same way, and every one of them is over #app's z-index.
  //
  // So the rule is the same one #app follows: what a render draws, the next render clears.
  function clearOverlays() {
    var open = document.querySelectorAll("body > .ui-overlay");
    for (var i = 0; i < open.length; i++) {
      if (open[i].parentNode) { open[i].parentNode.removeChild(open[i]); }
    }
  }

  function renderScreen() {
    var view = (state && state.view) || null;
    // Older Friends links open the global dock over Profile.
    var screen = view === "friends" ? "profile" : view;
    var module = (screen && screens[screen]) || null;
    if (!module) {
      // Unknown/unregistered view: fail honestly rather than blank.
      var msg = view ? ("No screen registered for view: " + view) : "No view in snapshot";
      app.appendChild(HubUI.el("div", "boot", msg));
      // Drop the de-dup so the next poll re-renders even though the snapshot has not changed.
      // The first /state can land BEFORE the screen scripts below core.js have run (index.html
      // loads them after), and without this the app would sit on that message for good: every
      // later poll returns the identical body and returns early, so the screen that registered
      // a moment later never gets to draw.
      lastStateText = null;
      return;
    }
    var ctx = { call: call, ui: HubUI, t: HubUI.t, state: state };
    module.render(app, state, ctx);
  }

  // Every registered overlay, on every render, whatever the view is. AFTER renderScreen, so an
  // overlay sits over the screen and clearOverlays (which runs before both) never eats the one
  // just appended. One overlay throwing must not take the rest - or the screen underneath - with
  // it: a card is a thing on top of the app, never a thing that can black-screen it.
  function renderOverlays() {
    if (!state) { return; }
    var ctx = { call: call, ui: HubUI, t: HubUI.t, state: state };
    for (var name in overlays) {
      if (!Object.prototype.hasOwnProperty.call(overlays, name)) { continue; }
      try { overlays[name].render(state, ctx); }
      catch (e) { if (window.console) { console.error("overlay " + name + " failed", e); } }
    }
  }

  function captureInputs() {
    // Account-link fields live in a body overlay. Preserve them only for its
    // current attempt/step; IDs change across attempts and secrets clear on submit.
    var nodes = document.querySelectorAll("#app input[id], #app textarea[id], .account-link-modal input[id]");
    var saved = [];
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var rec = { id: n.id, value: n.value, active: (document.activeElement === n) };
      if (rec.active) {
        try { rec.start = n.selectionStart; rec.end = n.selectionEnd; } catch (e) { /* unsupported */ }
      }
      saved.push(rec);
    }
    return saved;
  }

  function restoreInputs(saved) {
    if (!saved || !saved.length) { return; }
    for (var i = 0; i < saved.length; i++) {
      var rec = saved[i];
      var n = document.getElementById(rec.id);
      if (!n) { continue; }
      n.value = rec.value;
      if (rec.active) {
        try {
          n.focus();
          if (rec.start !== undefined && rec.end !== undefined) { n.setSelectionRange(rec.start, rec.end); }
        } catch (e) { /* focus/selection unsupported: value is still restored */ }
      }
    }
  }

  // ---------------------------------------------------------------- shared chrome
  var t = HubUI.t, esc = HubUI.esc;

  function renderNav() {
    // Nav lives in the core so it survives i18n/state and is consistent across screens. Items map
    // to a view; the active one is state.view. Clicking an item calls set_view (delegated listener
    // on navEl, bound once above), which switches the screen and re-emits the snapshot.
    var view = (state && state.view) || "competitive";
    if (view === "friends") { view = "profile"; }
    var items = [
      { view: "gamemodes",   label: t("tab_gamemodes") },
      { view: "competitive", label: t("tab_competitive") },
      { view: "leaderboard", label: t("nav_leaderboard") },
      { view: "profile",     label: t("nav_profile") },
      { view: "history",     label: t("nav_history") },
      { view: "settings",    label: t("nav_settings") },
      // Sam, 2026-09-16: "the settings button at the top - let's put another button that's
      // called Bug report". Last, next to Settings, because it is the same kind of thing:
      // somewhere you go about the app rather than about the game you are playing.
      { view: "bugreport",   label: t("nav_bugreport") }
    ];

    navEl.innerHTML = items.map(function (it) {
      return '<span class="navitem' + (it.view === view ? " active" : "") + '" data-view="' + esc(it.view) + '">' + esc(it.label) + "</span>";
    }).join("");
  }

  function renderStatus() {
    // Online, searching, live games, registered players. All clear when the
    // stream drops. The registration inventory can independently be unavailable;
    // a dash preserves the label without claiming an unknown total is zero.
    //
    // `topbar_offline` AND NOT `comp_live_lost`, which is the sentence this used to show: the
    // bar is a row on one line whose width is what sets FIT_W, and "Lost the connection to the
    // competitive service. Reconnecting…" is three times the width of the count it replaces -
    // enough, in Russian, to push the window buttons off the edge of the smallest window. The
    // full sentence is not lost: the session sets it as `error` at the same moment
    // (competitive.py), and the competitive screen draws it where there is room for it.
    var st = state.status || {};
    var ok = st.connected;
    statusEl.className = "status-dot " + (ok ? "ok" : "bad");
    statusEl.innerHTML = '<span class="dot"></span><span>' +
      esc(ok ? t("comp_online", { n: st.online || 0 }) : t("topbar_offline")) + "</span>";
    if (queuedEl) {
      queuedEl.textContent = ok ? t("topbar_queued", { n: st.queued || 0 }) : "";
    }
    if (liveEl) {
      liveEl.textContent = ok ? t("topbar_live", { n: st.live_matches || 0 }) : "";
    }
    if (registeredEl) {
      var registered = st.players_registered;
      var known = Number.isSafeInteger(registered) && registered >= 0;
      registeredEl.textContent = ok ? t("topbar_registered", { n: known ? registered : "—" }) : "";
    }
  }

  // ---------------------------------------------------------------- app self-update strip
  // A slim strip below the window header when a newer hub is available (state.update),
  // matching the old Tk "update strip". It renders in the CORE chrome so it shows over any screen.
  // A forced update (catalogue hub.required) becomes a blocking overlay instead of a dismissable
  // strip. "Update now" calls the apply_hub_update verb, which downloads + launches the installer
  // on a worker thread; progress and errors come back through the snapshot's update slice.
  var rootEl = document.getElementById("root");
  var updateDismissed = false;                   // "Later" hides the strip for this session only

  function removeUpdateBar() {
    var host = document.getElementById("updatebar");
    if (host && host.parentNode) { host.parentNode.removeChild(host); }
    document.body.classList.remove("has-update", "update-forced");
  }

  function renderUpdate() {
    var u = (state && state.update) || null;
    if (!u || !u.available) { removeUpdateBar(); return; }
    var status = u.status || "idle";
    // A non-forced strip the user dismissed stays hidden until something changes it (a download
    // they started, or an error, still shows so they see the outcome). A forced update ignores it.
    if (updateDismissed && !u.forced && status !== "downloading" && status !== "error") {
      removeUpdateBar();
      return;
    }
    var host = document.getElementById("updatebar");
    if (!host) {
      host = document.createElement("div");
      host.id = "updatebar";
      document.getElementById("update-notices").appendChild(host);
    }
    host.className = "updatebar" + (u.forced ? " forced" : "");
    document.body.classList.add("has-update");
    document.body.classList.toggle("update-forced", !!u.forced);

    var msg, pct = Math.max(0, Math.min(100, u.progress | 0));
    var showApply = true, applyLabel = t("update_banner_action"), showLater = !u.forced, showBar = false;
    if (status === "downloading") {
      msg = t("update_banner_progress", { pct: pct });
      showApply = false; showLater = false; showBar = true;
    } else if (status === "error") {
      msg = t("update_banner_failed", { reason: u.error || "" });
    } else if (u.forced) {
      // The forced overlay reuses the old all-or-nothing update screen's strings.
      msg = t("update_title") + " — " + t("update_body", { new: u.latest, old: u.current });
    } else {
      msg = t("update_banner", { new: u.latest, old: u.current });
    }

    var html = '<div class="ub-inner">' +
      '<span class="ub-msg">' + esc(msg) + "</span>" +
      '<span class="ub-actions">';
    if (showApply) {
      html += '<button type="button" class="ub-btn primary" data-ub="apply">' + esc(applyLabel) + "</button>";
    }
    if (showLater) {
      html += '<button type="button" class="ub-btn ghost" data-ub="later">' + esc(t("update_banner_later")) + "</button>";
    }
    html += "</span></div>";
    if (showBar) {
      html += '<div class="ub-progress"><div class="ub-fill" style="width:' + pct + '%"></div></div>';
    }
    host.innerHTML = html;

    var apply = host.querySelector('[data-ub="apply"]');
    if (apply) { apply.addEventListener("click", function () { call("apply_hub_update"); }); }
    var later = host.querySelector('[data-ub="later"]');
    if (later) { later.addEventListener("click", function () { updateDismissed = true; removeUpdateBar(); }); }
  }

  // ---------------------------------------------------------------- gamemode update strip
  // Sam, 2026-09-17: "similar to how the lightsoff app auto detects an update without having to
  // restart or go to a different page, do the same with gamemodes". Same chrome, same place, same
  // rules as the strip above - it just updates a pak instead of the app.
  //
  // IT IS NOT DISMISSABLE, and that is the difference. A hub you have not updated still works; a
  // gamemode you have not updated is a pak that plays by different numbers from everyone else's,
  // and the queue refuses it, so "Later" would only hide the reason Find match is greyed out.
  //
  // It never shows over the app's own strip: two stacked bars push the whole screen down and the
  // app one has to be dealt with first anyway (its installer replaces the hub, taking this with it).
  var gmDone = null;             // the version we last saw finish, so the "Updated" note can clear

  function removeGamemodeBar() {
    var host = document.getElementById("gmupdatebar");
    if (host && host.parentNode) { host.parentNode.removeChild(host); }
    document.body.classList.remove("has-gm-update");
  }

  function renderGamemodeUpdate() {
    var g = (state && state.gamemode_update) || null;
    var u = (state && state.update) || null;
    if (!g || (u && u.available)) { removeGamemodeBar(); return; }

    var host = document.getElementById("gmupdatebar");
    if (!host) {
      host = document.createElement("div");
      host.id = "gmupdatebar";
      document.getElementById("update-notices").appendChild(host);
    }
    // "ub-gamemode", NOT "gm": the gamemodes SCREEN already owns a `.gm` class (height:100%;
    // display:flex), and a bar wearing it stretched its background over the entire window - the
    // strip read correctly and covered the app. Measured in the browser harness, 2026-09-17.
    host.className = "updatebar ub-gamemode";
    document.body.classList.add("has-gm-update");

    // WHY it cannot be pressed matters more than the button being grey: "close Bodycam" is an
    // instruction, a dead button is a bug report.
    var msg, showApply = false;
    if (g.busy) {
      msg = t("gm_update_working", { name: g.name });
    } else if (g.running) {
      msg = t("gm_update_close_game", { name: g.name });
    } else if (!g.ready) {
      msg = t("gm_update_no_game", { name: g.name });
    } else {
      msg = g.rules_only ? t("gm_update_rules", { name: g.name })
                         : t("gm_update", { name: g.name, new: g.latest, old: g.current });
      showApply = !!g.can_update;
    }

    var html = '<div class="ub-inner"><span class="ub-msg">' + esc(msg) + "</span>" +
      '<span class="ub-actions">';
    if (showApply) {
      html += '<button type="button" class="ub-btn primary" data-gmub="apply">' +
              esc(t("gm_update_action")) + "</button>";
    }
    html += "</span></div>";
    host.innerHTML = html;

    var apply = host.querySelector('[data-gmub="apply"]');
    if (apply) {
      apply.addEventListener("click", function () { call("gamemode_update", g.id); });
    }
    gmDone = g.latest;
  }
})();
