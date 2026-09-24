/* Gamemodes screen (Installed / Available · Ruleset + Maps · install/update/uninstall) —
 * OWNED BY THE GAMEMODES SCREEN.
 *
 * This is the ONLY JS file this screen edits. It registers itself with the shared core and renders
 * from its slice of the snapshot (state.gamemodes, contributed by hub/webui/screens/gamemodes.py).
 * It draws with the shared helpers (ctx.ui) and the shared tokens/components; its screen-local
 * rules live in screens/gamemodes.css. See screens/competitive.js for the end-to-end pattern.
 *
 * Verbs it calls (gamemode_install, gamemode_update, gamemode_uninstall, open_ruleset,
 * close_ruleset) are registered on the bridge by hub/webui/screens/gamemodes.py. Screen-specific
 * strings ship in state.gamemodes.strings (read via gt()); shared labels use ctx.t. */
(function () {
  "use strict";

  window.HubUI.registerScreen("gamemodes", { render: render });

  function render(root, state, ctx) {
    var ui = ctx.ui, call = ctx.call;
    var el = ui.el;
    var gm = state.gamemodes || {};
    var S = gm.strings || {};

    // screen-string lookup with {name} interpolation and key fallback (mirrors core t()).
    function gt(key, kw) {
      var s = (S[key] !== undefined && S[key] !== null) ? S[key] : key;
      if (kw) {
        s = s.replace(/\{(\w+)\}/g, function (m, n) {
          return (kw[n] !== undefined && kw[n] !== null) ? String(kw[n]) : m;
        });
      }
      return s;
    }

    var wrap = el("div", "gm");
    wrap.appendChild(header());
    wrap.appendChild(banner());

    var installed = gm.installed || [];
    var available = gm.available || [];

    if (!gm.catalogue_loaded && !installed.length && !available.length) {
      var loading = el("div", "gm-scroll");
      loading.appendChild(ui.emptyState({ icon: "◆", message: gt("loading") }));
      wrap.appendChild(loading);
      root.appendChild(wrap);
      return;
    }

    var scroll = el("div", "gm-scroll");
    if (!installed.length && !available.length) {
      scroll.appendChild(ui.emptyState({ icon: "◆", message: gt("empty_none") }));
    } else {
      if (installed.length) { scroll.appendChild(section(gt("section_installed"), installed)); }
      if (available.length) { scroll.appendChild(section(gt("section_available"), available)); }
      scroll.appendChild(el("div", "gm-footer", gt("footer")));
    }
    wrap.appendChild(scroll);
    root.appendChild(wrap);

    // the ruleset modal, if one is open (server-authoritative: state.gamemodes.open_ruleset)
    if (gm.open_ruleset) {
      var opened = findMode(gm.open_ruleset);
      if (opened) { document.body.appendChild(rulesetModal(opened)); }
    }

    // ---------------------------------------------------------------- header
    function header() {
      var h = el("div", "gm-head");
      var left = el("div", "gm-head-left");
      left.appendChild(el("h1", "gm-title", ctx.t("tab_gamemodes")));
      left.appendChild(el("p", "gm-intro", gt("intro")));
      h.appendChild(left);
      return h;
    }

    // a status banner for the guards / the last job / the last notice
    function banner() {
      var job = gm.job;
      if (job) {
        // A real progress bar: ops.apply reports a 0..1 fraction through the download, the build
        // (weighted by the builder's own steps) and the swap, so the fill and the percentage both
        // track the actual work. The indeterminate stripe is only the fallback for a step that
        // genuinely cannot say how far along it is (progress === null).
        var known = (job.progress !== null && job.progress !== undefined);
        var pct = known ? Math.max(0, Math.min(100, Math.round(job.progress * 100))) : null;

        var b = el("div", "gm-banner working");
        b.setAttribute("role", "status");
        b.setAttribute("aria-live", "polite");

        var line = el("div", "gm-banner-line");
        line.appendChild(el("span", "gm-banner-msg", job.message || gt("working")));
        if (known) { line.appendChild(el("span", "gm-banner-pct", pct + "%")); }
        b.appendChild(line);

        var bar = el("div", "gm-progress");
        bar.setAttribute("role", "progressbar");
        bar.setAttribute("aria-valuemin", "0");
        bar.setAttribute("aria-valuemax", "100");
        if (known) { bar.setAttribute("aria-valuenow", String(pct)); }
        var fill = el("div", "gm-progress-fill" + (known ? "" : " indeterminate"));
        if (known) { fill.style.width = pct + "%"; }
        bar.appendChild(fill);
        b.appendChild(bar);
        return b;
      }
      if (gm.notice) {
        var n = el("div", "gm-banner " + (gm.notice.kind === "error" ? "error" : "ok"));
        n.setAttribute("role", "status");
        n.appendChild(el("span", "gm-banner-msg", gm.notice.text || ""));
        return n;
      }
      if (!gm.ready) {
        return el("div", "gm-banner warn", gt("no_game"));
      }
      if (gm.running) {
        return el("div", "gm-banner warn", gt("game_running"));
      }
      return el("div", "gm-banner-none");
    }

    // ---------------------------------------------------------------- a titled list of modes
    function section(title, modes) {
      var s = el("div", "gm-section");
      s.appendChild(el("div", "gm-section-title", title));
      var list = el("div", "gm-list");
      modes.forEach(function (m) { list.appendChild(modeRow(m)); });
      s.appendChild(list);
      return s;
    }

    function modeRow(m) {
      var row = el("div", "gm-row" + (m.ranked ? " ranked" : ""));

      var icon = el("div", "gm-icon");
      icon.setAttribute("aria-hidden", "true");
      if (m.id === "BB5" || m.id === "CTF") {
        var image = el("img", "gm-icon-image");
        image.alt = "";
        image.draggable = false;
        image.addEventListener("error", function () { image.remove(); });
        image.src = "/gamemode-thumbnail?mode=" + encodeURIComponent(m.id);
        icon.appendChild(image);
      }
      row.appendChild(icon);

      var body = el("div", "gm-body");
      var nameRow = el("div", "gm-name-row");
      nameRow.appendChild(el("span", "gm-name", m.name));
      if (m.ranked) { nameRow.appendChild(el("span", "gm-tag", gt("ranked"))); }
      if (m.update_available) { nameRow.appendChild(el("span", "gm-tag update", gt("update_available"))); }
      body.appendChild(nameRow);
      var sub = m.installed
        ? (gt("installed_label") + (m.installed_version ? " · " + gt("version") + " " + m.installed_version : ""))
        : (m.version ? gt("version") + " " + m.version : "");
      if (sub) { body.appendChild(el("div", "gm-sub", sub)); }
      if (m.description) { body.appendChild(el("div", "gm-desc", m.description)); }
      row.appendChild(body);

      var actions = el("div", "gm-actions");
      if (m.ruleset) {
        var rb = ui.btn("gm-ruleset", "", function () { call("open_ruleset", m.id); },
          { ariaLabel: gt("ruleset") + " - " + m.name });
        var glyph = el("span", "gm-ruleset-glyph");
        glyph.innerHTML = '<svg viewBox="0 0 18 18" aria-hidden="true" focusable="false"><path d="M5.5 7a3.5 3.5 0 0 1 7 0c0 2.25-3.5 2.25-3.5 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="9" cy="14.5" r="1" fill="currentColor"/></svg>';
        glyph.setAttribute("aria-hidden", "true");
        rb.appendChild(glyph);
        actions.appendChild(rb);
      }
      if (m.installed) {
        if (m.can_update) {
          actions.appendChild(actionBtn("gm-btn update", gt("btn_update"), function () { call("gamemode_update", m.id); }, m.can_update));
        }
        actions.appendChild(actionBtn("gm-btn", gt("btn_uninstall"), function () { call("gamemode_uninstall", m.id); }, m.can_uninstall));
      } else {
        actions.appendChild(actionBtn("gm-btn solid", gt("btn_install"), function () { call("gamemode_install", m.id); }, m.can_install));
      }
      row.appendChild(actions);
      return row;
    }

    function actionBtn(cls, label, onclick, enabled) {
      var b = ui.btn(cls, label, enabled ? onclick : null);
      if (!enabled) { b.disabled = true; }
      return b;
    }

    // ---------------------------------------------------------------- ruleset + maps modal
    function rulesetModal(m) {
      var meta = el("div", "gm-meta");
      metaTile(meta, gt("version"), m.version || "-");
      metaTile(meta, gt("maps"), m.maps && m.maps.length ? String(m.maps.length) : "-");
      metaTile(meta, gt("ranked"), m.ranked ? gt("yes") : gt("no"));
      metaTile(meta, gt("status"), m.installed ? gt("status_installed") : gt("status_available"));

      var lines = el("div", "gm-rules");
      (m.ruleset_lines && m.ruleset_lines.length ? m.ruleset_lines : [m.ruleset]).forEach(function (line) {
        if (!line) { return; }
        var li = el("div", "gm-rule");
        li.appendChild(el("span", "gm-bullet", "•"));
        li.appendChild(el("span", "gm-rule-text", line));
        lines.appendChild(li);
      });

      var mapsBlock = el("div", "gm-maps");
      mapsBlock.appendChild(el("div", "gm-maps-label", gt("maps")));
      mapsBlock.appendChild(el("div", "gm-maps-list",
        (m.maps && m.maps.length) ? m.maps.join(" · ") : gt("maps_none")));

      var kicker = el("div", "gm-modal-kicker", gt("ruleset"));

      var overlay = ui.modal({
        title: m.name,
        children: [kicker, meta, lines, mapsBlock, closeBtn()],
        onClose: function () { call("close_ruleset"); }
      });
      overlay.classList.add("gm-overlay");
      // focus the dialog so Escape (bound by ui.modal) works without a manual click
      var dialog = overlay.querySelector(".ui-modal");
      if (dialog) { dialog.setAttribute("tabindex", "-1"); setTimeout(function () { dialog.focus(); }, 0); }
      return overlay;
    }

    function closeBtn() {
      var f = el("div", "gm-modal-foot");
      f.appendChild(ui.btn("gm-btn", gt("close"), function () { call("close_ruleset"); }));
      return f;
    }

    function metaTile(parent, k, v) {
      var t2 = el("div", "gm-meta-tile");
      t2.appendChild(el("div", "gm-meta-k", k));
      t2.appendChild(el("div", "gm-meta-v", v));
      parent.appendChild(t2);
    }

    function findMode(id) {
      var all = (gm.installed || []).concat(gm.available || []);
      for (var i = 0; i < all.length; i++) { if (all[i].id === id) { return all[i]; } }
      return null;
    }
  }
})();
