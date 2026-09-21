/* Match history screen — OWNED BY THE HISTORY SCREEN AGENT.
 *
 * This is the ONLY JS file this screen edits. It registers itself with the shared core and renders
 * the player's read-only match record from its slice of the snapshot (state.history, contributed by
 * hub/webui/screens/history.py). It draws with the shared helpers (ctx.ui) and shared tokens; its
 * own visual rules live in screens/history.css. See screens/competitive.js for the end-to-end
 * pattern.
 *
 * i18n: this screen ships its own strings in the snapshot (state.history.strings) and reads them via
 * the local hs() helper — NOT ctx.t() — on purpose. The core's string-completeness test scans every
 * JS file for t() keys and asserts each exists in hub/i18n.py; this screen adds NEW strings that are
 * not (and per the task must not be) in i18n.py, so it references none of its own strings via t().
 *
 * Verbs it calls (load_history, refresh_history) are registered on the bridge by history.py. */
(function () {
  "use strict";

  // The All / Played / Cancelled filter is a view-only choice; it lives across snapshot pushes (a
  // periodic stats push must not silently reset what the user is looking at) but never leaves JS.
  var activeFilter = "all";

  window.HubUI.registerScreen("history", { render: render });
  window.HubUI.renderMatchHistory = render;

  function render(root, state, ctx) {
    var ui = ctx.ui, call = ctx.call, el = ui.el, esc = ui.esc;
    var h = (state && state.history) || {};
    var auth = (state && state.auth) || {};
    var strings = h.strings || {};
    var combatPanelCounter = 0;

    // Local string reader (see header): resolves this screen's own keys with {tokens}.
    function hs(key, kw) {
      var s = (strings && strings[key]);
      if (s === undefined || s === null) { s = key; }
      if (kw) {
        s = s.replace(/\{(\w+)\}/g, function (m, name) {
          return (kw[name] !== undefined && kw[name] !== null) ? String(kw[name]) : m;
        });
      }
      return s;
    }

    // draw() rebuilds the whole screen into root. The core calls render() on every push (clearing
    // root first); the filter tabs also call draw() so a filter click re-renders without a round
    // trip to Python (the record itself has not changed — only which rows we show).
    function draw() {
      root.innerHTML = "";
      root.appendChild(build());
    }

    function build() {
      var wrap = el("div", "history");
      wrap.appendChild(header());

      // Signed out: nothing to fetch. Say so with a shared empty state, don't call the verb.
      if (!auth.signed_in) {
        wrap.appendChild(ui.emptyState({ icon: "◆", message: hs("signed_out_cta") }));
        return wrap;
      }

      // Lazy first load: ask once when we have neither an answer, an in-flight request, nor an
      // error. LiveSession.load_history guards itself (it will not double-fetch), and the guard
      // here means an offline session that set an error does not spin (asked stays false, but the
      // error is truthy, so we stop). refresh_history is the user's explicit retry.
      if (!h.asked && !h.loading && !h.error) { call("load_history"); }
      // Appended to the BODY, not into the screen, so it is not inside the scroll region it
      // would otherwise be clipped by.
      if (h.open_id) { document.body.appendChild(detailModal(h)); }
      else { ui.clearRoundSelection("history"); }

      if (h.error) { wrap.appendChild(errorBar()); }

      var rows = h.rows || [];

      // First load, no rows in hand yet: a skeleton beats a spinner and reserves the space.
      if (h.loading && !rows.length) { wrap.appendChild(skeleton()); return wrap; }

      var shown = filterRows(rows);

      if (!shown.length) {
        // Distinguish "the server said you have none / this filter has none" (asked) from "we
        // could not even ask" (not asked and no error path already handled above).
        if (h.asked) {
          wrap.appendChild(rows.length ? filteredEmpty() : emptyState());
        } else if (!h.error) {
          wrap.appendChild(skeleton());
        }
        return wrap;
      }

      // Any played row whose scoreboard is not in yet gets the honest note once, above the list.
      if (anyPending(shown)) { wrap.appendChild(pendingNote()); }
      wrap.appendChild(list(shown));
      return wrap;
    }

    // -- header (title + summary + filters + refresh) -------------------------
    function header() {
      var head = el("div", "hist-head");

      var titles = el("div", "hist-titles");
      titles.appendChild(el("h1", "hist-title", hs("title")));
      var sub = subtitle();
      if (sub) { titles.appendChild(el("div", "hist-sub", sub)); }
      head.appendChild(titles);

      var controls = el("div", "hist-controls");
      controls.appendChild(ui.tabs([
        { id: "all", label: hs("filter_all") },
        { id: "played", label: hs("filter_played") },
        { id: "cancelled", label: hs("filter_cancelled") }
      ], activeFilter, function (id) { activeFilter = id; draw(); }));
      if (auth.signed_in) {
        var refresh = ui.chip(hs("refresh"), {
          onClick: function () { call("refresh_history"); },
          ariaLabel: hs("refresh")
        });
        if (h.loading) { refresh.setAttribute("aria-busy", "true"); }
        controls.appendChild(refresh);
      }
      head.appendChild(controls);
      return head;
    }

    function subtitle() {
      var sm = h.summary || {};
      if (!auth.signed_in || !h.asked) { return ""; }
      var parts = [hs("subtitle_recorded", { n: sm.recorded || 0 })];
      if (sm.played) { parts.push(hs("subtitle_played", { n: sm.played })); }
      if (sm.cancelled) { parts.push(hs("subtitle_cancelled", { n: sm.cancelled })); }
      // Only claim a W/L record once a match has actually been decided (win_rate is non-null).
      if (sm.win_rate !== null && sm.win_rate !== undefined) {
        parts.push(hs("subtitle_record", { w: sm.wins || 0, l: sm.losses || 0 }));
      }
      return parts.join(" · ");
    }

    // -- states ---------------------------------------------------------------
    function errorBar() { return el("div", "hist-error", h.error); }

    function emptyState() {
      return ui.emptyState({
        icon: "◆",
        message: hs("empty_title") + ". " + hs("empty_hint")
      });
    }

    function filteredEmpty() {
      return ui.emptyState({ icon: "◆", message: hs("empty_title") });
    }

    function skeleton() {
      var box = el("div", "hist-skeleton");
      box.setAttribute("aria-hidden", "true");
      for (var i = 0; i < 5; i++) { box.appendChild(el("div", "hist-skel-row")); }
      var live = el("div", "sr-only");
      live.setAttribute("role", "status");
      live.textContent = hs("loading");
      box.appendChild(live);
      return box;
    }

    function pendingNote() { return el("div", "hist-note", hs("pending_note")); }

    // -- one match, in full ---------------------------------------------------
    //
    // WHAT IS HERE IS WHAT IS REAL. Teams, sides, the veto in order, the round-by-round timeline
    // and - when the gamemode reported one - the scoreboard. A match it said nothing about gets
    // the honest line instead of a column of zeroes that reads as "everyone went 0-0".
    function detailModal(h) {
      var d = h.open;
      var children = [];

      if (h.open_loading && !d) {
        children.push(el("div", "hist-note", hs("loading")));
      } else if (h.open_error) {
        children.push(el("div", "hist-note", h.open_error));
      } else if (d) {
        var head = el("div", "md-head" + (d.score ? "" : " no-score"));
        head.appendChild(el("div", "md-map", d.map || hs("no_map")));
        if (d.score) {
          // Archived scores use team IDs; show this player's side first, even for a loss.
          var me = (d.players || []).concat(d.scoreboard || []).find(function (p) {
            return p.is_me && (String(p.team) === "1" || String(p.team) === "2");
          });
          var myTeam = me ? String(me.team) : ["1", "2"].find(function (n) {
            return auth.steam_id && ((d.teams || {})[n] || []).indexOf(String(auth.steam_id)) !== -1;
          });
          var leftTeam = myTeam === "2" ? "2" : "1";
          var rightTeam = leftTeam === "1" ? "2" : "1";
          head.appendChild(el("div", "md-score",
            (d.score[leftTeam] || 0) + " - " + (d.score[rightTeam] || 0)));
        }
        children.push(head);
        if (d.rounds_played) {
          children.push(el("div", "md-sub", hs("rounds_played", { n: d.rounds_played })));
        }

        children.push(ui.roundSelector({
          surface: "history", matchId: d.id, rounds: d.round_details,
          myTeam: Number(myTeam || 0), text: hs,
          render: function (round) {
            var section = el("div", "round-content");
            var board = round ? round.scoreboard : (d.has_scoreboard ? (d.scoreboard || []) : []);
            // TK only earns a column when a kill feed actually arrived. Null everywhere means we were
            // never told, and a column of dashes teaches nobody anything.
            var showTk = board.some(function (r) {
              return r.team_kills !== null && r.team_kills !== undefined;
            });
            if (board.length) { section.appendChild(el("div", "md-h", hs("scoreboard"))); }

            var teamsBox = el("div", "md-teams" + (board.length ? " sb" : ""));
            ["1", "2"].forEach(function (n) {
              var col = el("div", "md-team" + (board.length ? " sb" : ""));
              var side = round ? null : (d.sides || {})[n];
              col.appendChild(el("div", "md-team-h",
                hs("team", { n: n }) + (side ? "  " + side : "")));
              if (board.length) { col.appendChild(colHeads(showTk)); }
              var mine = board.length
                ? board.filter(function (r) { return String(r.team) === n; })
                : (d.players || []).filter(function (p) { return String(p.team) === n; });
              mine.forEach(function (p) {
                col.appendChild(board.length ? boardRow(p, showTk, d) : plainRow(p, d));
              });
              teamsBox.appendChild(col);
            });
            section.appendChild(teamsBox);
            if (board.length && showTk) { section.appendChild(el("div", "md-foot", hs("kills_note"))); }

            if (!round && !d.has_scoreboard) { section.appendChild(el("div", "hist-note", hs("no_scoreboard"))); }
            return section;
          }
        }));

        // the veto, in the order it happened - an auto-ban is marked, because "the clock banned
        // it" and "they banned it" are different facts about the same map
        if ((d.bans || []).length) {
          children.push(el("div", "md-h", hs("veto")));
          var vetoBox = el("div", "md-veto");
          d.bans.forEach(function (b) {
            vetoBox.appendChild(el("span", "md-ban" + (b.auto ? " auto" : ""),
              b.map + (b.auto ? "  " + hs("auto_ban") : "")));
          });
          children.push(vetoBox);
        }

      }

      var overlay = ui.modal({
        title: hs("match_detail"),
        children: children,
        onClose: function () { ui.clearRoundSelection("history"); call("close_match"); }
      });
      overlay.classList.add("md-overlay");
      var dialog = overlay.querySelector(".ui-modal");
      if (dialog) { dialog.setAttribute("tabindex", "-1"); setTimeout(function () { dialog.focus(); }, 0); }
      return overlay;
    }

    // The column strip above each side. Same grid as the rows, so numbers line up under it.
    function colHeads(showTk) {
      var head = el("div", "md-cols" + (showTk ? "" : " no-tk"));
      head.appendChild(el("span", "md-pname", hs("col_player")));
      head.appendChild(el("span", "md-n", hs("col_k")));
      head.appendChild(el("span", "md-n", hs("col_d")));
      head.appendChild(el("span", "md-n", hs("col_kd")));
      if (showTk) { head.appendChild(el("span", "md-n", hs("col_tk"))); }
      head.appendChild(el("span", "md-act", ""));
      return head;
    }

    // One scoreboard line. Every number is honest or absent: `reported` false means the stat
    // sweep never reached this player, and each null uses the missing-stat placeholder.
    function boardRow(p, showTk, d) {
      var fragment = document.createDocumentFragment();
      var row = el("div", "md-player sb" + (p.is_me ? " you" : "") + (p.left ? " left" : "")
                          + (showTk ? "" : " no-tk") + (p.reported ? "" : " unreported"));
      var detail = combatPanel(p, "md");
      row.appendChild(combatPlayerCell(p, detail, "md"));
      row.appendChild(num(p.kills));
      row.appendChild(num(p.deaths));
      row.appendChild(num(p.kd === null || p.kd === undefined ? null : p.kd.toFixed(2)));
      if (showTk) {
        var tk = num(p.team_kills);
        // A team kill is the one number here somebody is answerable for, so it is marked rather
        // than left to blend into the rest.
        if (p.team_kills) { tk.classList.add("bad"); }
        row.appendChild(tk);
      }
      row.appendChild(actionCell(p, d));
      if (!p.reported) { row.title = hs("not_reported"); }
      fragment.appendChild(row);
      fragment.appendChild(detail);
      return fragment;
    }

    /** Name, honest evidence status and the control for this player's compact combat detail. */
    function combatPlayerCell(p, panel, prefix) {
      var combat = p.combat || {};
      var status = combat.status || "unavailable";
      var cell = el("span", prefix + "-pidentity");
      cell.appendChild(el("span", prefix + "-pname", p.name));
      cell.appendChild(el("span", "combat-status status-" + status, combatStatus(status)));
      var toggle = ui.btn("combat-toggle", hs("combat_details"), function () {
        var willOpen = panel.hidden;
        panel.hidden = !willOpen;
        toggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
      }, { ariaLabel: hs("combat_details") + ": " + p.name });
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("data-player", p.steam_id);
      toggle.setAttribute("aria-controls", panel.id);
      cell.appendChild(toggle);
      return cell;
    }

    function combatStatus(status) {
      if (status === "complete") { return hs("combat_complete"); }
      if (status === "partial") { return hs("combat_partial"); }
      return hs("combat_unavailable");
    }

    /** Combat totals, then recorded damage exchanged with each player. */
    function combatPanel(p, prefix) {
      var combat = p.combat || { status: "unavailable", weaponStats: [] };
      var panel = el("div", prefix + "-combat combat-panel");
      panel.id = prefix + "-combat-" + (++combatPanelCounter);
      panel.hidden = true;
      panel.appendChild(el("div", "combat-status status-" + combat.status,
        combatStatus(combat.status)));
      if (combat.status === "unavailable") {
        panel.appendChild(el("div", "combat-note", hs("combat_unavailable_note")));
        return panel;
      }
      if (combat.status === "partial") {
        panel.appendChild(el("div", "combat-note", hs("combat_partial_note")));
      }
      var metrics = el("div", "combat-metrics");
      metrics.appendChild(combatMetric("damage", hs("col_damage"), combat.enemyDamage));
      metrics.appendChild(combatMetric("friendly-damage", hs("col_friendly_damage"), combat.friendlyDamage));
      metrics.appendChild(combatMetric("damage-taken", hs("col_damage_taken"), combat.damageTaken));
      metrics.appendChild(combatMetric("adr", hs("col_adr"), combat.adr, "decimal"));
      metrics.appendChild(combatMetric("assists", hs("col_assists"), combat.assists));
      metrics.appendChild(combatMetric("headshots", hs("col_headshots"), combat.headshots));
      metrics.appendChild(combatMetric("accuracy", hs("col_accuracy"), combat.accuracy, "percent"));
      panel.appendChild(metrics);
      if ((combat.playerStats || []).length) { panel.appendChild(playerDamageTable(combat.playerStats)); }
      else { panel.appendChild(el("div", "combat-note", hs("player_damage_unavailable"))); }
      return panel;
    }
    function combatMetric(key, label, value, format) {
      var metric = el("div", "combat-metric");
      metric.setAttribute("data-stat", key);
      metric.appendChild(el("span", "combat-label", label));
      metric.appendChild(el("span", "combat-value" + (value === null || value === undefined ? " pending" : ""),
        combatNumber(value, format)));
      return metric;
    }

    function combatNumber(value, format) {
      if (value === null || value === undefined) { return hs("stat_none"); }
      var number = Number(value);
      if (!Number.isFinite(number)) { return hs("stat_none"); }
      var text = Number.isInteger(number) ? String(number) : String(Math.round(number * 10) / 10);
      return format === "percent" ? text + "%" : text;
    }

    function playerDamageTable(rows) {
      var section = el("div", "combat-weapons-section");
      section.appendChild(el("div", "combat-weapons-title", hs("player_damage")));
      var scroll = el("div", "combat-weapons-scroll");
      var table = el("table", "combat-weapons combat-player-damage");
      var head = document.createElement("thead"), headRow = document.createElement("tr");
      [hs("damage_player"), hs("damage_to"), hs("damage_from")].forEach(function (label) {
        headRow.appendChild(el("th", "", label));
      });
      head.appendChild(headRow); table.appendChild(head);
      var body = document.createElement("tbody");
      rows.forEach(function (player) {
        var tr = document.createElement("tr");
        tr.appendChild(el("td", "combat-weapon-name", player.name || hs("unknown_source")));
        [player.damageDealt, player.damageTaken].forEach(function (value) {
          tr.appendChild(el("td", value === null || value === undefined ? "pending" : "",
            combatNumber(value)));
        });
        body.appendChild(tr);
      });
      table.appendChild(body); scroll.appendChild(table); section.appendChild(scroll);
      return section;
    }

    // The roster line drawn when there is no board at all - what this panel has always shown.
    function plainRow(p, d) {
      var row = el("div", "md-player" + (p.is_me ? " you" : "") + (p.left ? " left" : ""));
      row.appendChild(el("span", "md-pname", p.name));
      if (p.elo) { row.appendChild(el("span", "md-elo", String(p.elo))); }
      var act = actionCell(p, d);
      if (act.firstChild) { row.appendChild(act.firstChild); }
      return row;
    }

    /** One number, or the missing-stat placeholder. Never use 0 for an unreported stat. */
    function num(value) {
      var cell = el("span", "md-n");
      if (value === null || value === undefined) {
        cell.classList.add("pending");
        cell.textContent = hs("stat_none");
      } else {
        cell.textContent = String(value);
      }
      return cell;
    }

    /** The Report button, in its own cell so the grid stays aligned when there is none. */
    function actionCell(p, d) {
      var cell = el("span", "md-act");
      if (p.steam_id && !p.is_me) {
        cell.appendChild(ui.btn("tm-report", hs("report"), function () {
          // The name goes with the id: the report box is drawn from the competitive
                  // slice, which only knows the people in the CURRENT match - so a report
                  // opened from a months-old row would name a bare steam id.
                  call("open_report", p.steam_id, d.id, p.name || "");
        }, { tag: "button" }));
      }
      return cell;
    }

    // -- the list -------------------------------------------------------------
    function list(rows) {
      var nodes = rows.map(rowNode);
      var tableEl = ui.table(nodes);
      tableEl.setAttribute("role", "list");
      tableEl.setAttribute("aria-label", hs("title"));
      // A dedicated scroll region so a long history scrolls without moving the header/filters.
      var scroller = el("div", "hist-scroll");
      scroller.appendChild(tableEl);
      return scroller;
    }

    function rowNode(r) {
      // Built with the shared listRow primitive: the result word leads, the metric grid grows, the
      // relative time trails. The left accent + result colour come from the res-* class.
      var row = ui.listRow({
        leading: resultCell(r),
        children: [metrics(r)],
        trailing: whenCell(r)
      });
      row.classList.add("hist-row", "res-" + r.result);
      row.setAttribute("role", "listitem");
      var label = resultWord(r) + ", " + (r.map || hs("no_map"));
      row.setAttribute("aria-label", label);
      // OPEN THE MATCH. The row is a summary; the record behind it has the roster, the veto and
      // the round timeline. Keyboard too - a list of clickable things that only answers the mouse
      // is a list half the people cannot use.
      if (r.id) {
        row.classList.add("hist-open");
        row.setAttribute("tabindex", "0");
        row.addEventListener("click", function () { call("open_match", r.id); });
        row.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); call("open_match", r.id); }
        });
      }
      return row;
    }

    function resultCell(r) {
      var cell = el("div", "hist-result");
      cell.appendChild(el("span", "hist-res-word", resultWord(r)));
      var reason = reasonWord(r);
      if (reason) { cell.appendChild(el("span", "hist-res-reason", reason)); }
      return cell;
    }

    function metrics(r) {
      var grid = el("div", "hist-metrics");
      grid.appendChild(mapCell(r));
      grid.appendChild(scoreCell(r));
      grid.appendChild(kdaCell(r));
      grid.appendChild(rrCell(r));
      return grid;
    }

    function mapCell(r) {
      var cell = el("div", "hist-map");
      var art = el("div", "hist-map-art");
      art.appendChild(ui.mapImage(r.map, "hist-map-image"));
      cell.appendChild(art);
      var text = el("div", "hist-map-text");
      text.appendChild(el("div", "hist-map-name", r.map || hs("no_map")));
      var meta = mapMeta(r);
      if (meta) { text.appendChild(el("div", "hist-map-meta", meta)); }
      cell.appendChild(text);
      return cell;
    }

    function mapMeta(r) {
      // Honest, from real fields: the side the player played, plus Host/Preview tags. No fabricated
      // "mode" string — the record does not carry one.
      var bits = [];
      if (r.side === "attack") { bits.push(hs("side_attack")); }
      else if (r.side === "defend") { bits.push(hs("side_defend")); }
      if (r.host) { bits.push(hs("host_tag")); }
      if (r.preview) { bits.push(hs("preview_tag")); }
      return bits.join(" · ");
    }

    function scoreCell(r) {
      var cell = el("div", "hist-score");
      if (r.score && r.score.length === 2) {
        cell.appendChild(el("span", "hist-score-num", r.score[0] + " - " + r.score[1]));
      } else {
        cell.classList.add("pending");
        cell.textContent = hs("score_pending");
      }
      return cell;
    }

    function kdaCell(r) {
      // The player's OWN line, shipped on the row itself so a fifty-row list costs no detail
      // fetches. Null means the gamemode never reported them; keep missing stats distinct from 0.
      var have = (r.kills !== null && r.kills !== undefined
                  && r.deaths !== null && r.deaths !== undefined);
      var cell = el("div", "hist-kda" + (have ? "" : " pending"));
      cell.appendChild(el("span", "hist-kda-num",
        have ? (r.kills + " / " + r.deaths) : hs("kda_pending")));
      cell.appendChild(el("span", "hist-kda-lbl", hs("kda_label")));
      return cell;
    }

    function rrCell(r) {
      var cell = el("div", "hist-rr");
      // A placement match moves no RR by design, so it says what it was rather than "0 RR".
      if (r.placement) {
        cell.classList.add("flat");
        cell.textContent = hs("rr_placement");
        return cell;
      }
      // THE RR THE MATCH MOVED. Not `delta`: that is an arrow count, 1 to 3, and printing it with
      // an RR unit is what made every match read "+1 RR" (2026-09-16). A row the service wrote
      // before it sent `rr_delta` has no RR to show, and gets the dash rather than the arrows.
      var value = r.rr_delta;
      if (value === null || value === undefined) { value = r.elo; }   // else a penalty debt, or null
      if (value === null || value === undefined) {
        cell.classList.add("pending");
        cell.textContent = hs("rr_pending");
        return cell;
      }
      var n = Number(value);
      cell.classList.add(n > 0 ? "up" : (n < 0 ? "down" : "flat"));
      var sign = n > 0 ? "+" : "";
      cell.textContent = sign + n + " " + hs("rr_unit");
      return cell;
    }

    function whenCell(r) { return el("div", "hist-when", when(r.ended)); }

    // -- helpers --------------------------------------------------------------
    function filterRows(rows) {
      if (activeFilter === "played") {
        return rows.filter(function (r) { return r.outcome !== "cancelled" && r.outcome !== "voided"; });
      }
      if (activeFilter === "cancelled") {
        return rows.filter(function (r) { return r.outcome === "cancelled"; });
      }
      return rows;
    }

    function anyPending(rows) {
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        if (r.outcome !== "cancelled" && r.outcome !== "voided" && (r.won === null || r.won === undefined)) { return true; }
      }
      return false;
    }

    function resultWord(r) {
      return hs({ win: "res_win", loss: "res_loss", played: "res_played",
                  cancelled: "res_cancelled", voided: "res_voided", fault: "res_fault" }[r.result] || "res_played");
    }

    function reasonWord(r) {
      var map = { no_show: "reason_no_show", declined: "reason_declined",
                  abandoned: "reason_abandoned" };
      return map[r.reason] ? hs(map[r.reason]) : "";
    }

    function when(ms) {
      var n = Number(ms) || 0;
      if (!n) { return ""; }
      var diff = Date.now() - n;
      if (diff < 0) { diff = 0; }
      var min = Math.floor(diff / 60000);
      if (min < 1) { return hs("when_now"); }
      if (min < 60) { return hs("when_min", { n: min }); }
      var hr = Math.floor(min / 60);
      if (hr < 24) { return hs("when_hour", { n: hr }); }
      var day = Math.floor(hr / 24);
      if (day < 7) { return hs("when_day", { n: day }); }
      try { return new Date(n).toLocaleDateString(state.lang || undefined); }
      catch (e) { return new Date(n).toLocaleDateString(); }
    }

    draw();
  }
})();
