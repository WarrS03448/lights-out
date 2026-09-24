/* Post-match card — OWNED BY THE POST-MATCH CARD AGENT.
 *
 * NOT A SCREEN: an OVERLAY. It registers with the core as one (HubUI.registerOverlay), so it is
 * drawn on every render regardless of state.view - a match can end while the player is reading
 * their profile, and the result still has to reach them. Its slice is state.postmatch, contributed
 * by hub/webui/screens/postmatch.py.
 *
 * SAM, 2026-09-16: "an immediate post match Victory or Defeat screen in the app that comes up
 * after the game ends and stays there until its closed manually, either by clicking off the window
 * or hitting an X button. make it look similar to the match history match detail screen."
 *
 * So: the same sections in the same order as the match-history detail (head, teams with sides,
 * veto, and a line saying where the scoreboard is - it rides on the archived record, not on the
 * result event this card is built from), under a headline the detail panel has no reason to shout. And NOTHING here dismisses it. There is no timer, no "close on the next
 * push", no close when the phase moves on; the only exit is close_postmatch, which ui.modal wires
 * to the X, the backdrop and Escape alike. If this file ever grows a setTimeout that calls close,
 * that is the feature being undone.
 *
 * i18n: this card ships its own strings in the snapshot (state.postmatch.strings) and reads them
 * with the local ps() helper - NOT ctx.t() - the same arrangement as the history screen. Its own
 * visual rules live in screens/postmatch.css. */
(function () {
  "use strict";

  function refreshKey(state){
    var a=state.auth||{};
    return JSON.stringify([a.signed_in,a.player_id||a.steam_id||"",state.lang,state.postmatch]);
  }
  window.HubUI.registerOverlay("postmatch", { render: render, preserveOverlay:function(overlay,state){
    return !!(state.postmatch||{}).open&&overlay.classList.contains('pm-overlay')&&overlay._postmatchKey===refreshKey(state);
  } });

  function render(state, ctx) {
    var pm = (state && state.postmatch) || {};
    if (!pm.open || !pm.card) { window.HubUI.clearRoundSelection("postmatch"); return; }
    var preserved=document.querySelector('body > .pm-overlay');
    if(preserved&&preserved._postmatchKey===refreshKey(state))return;

    var ui = ctx.ui, call = ctx.call, el = ui.el;
    var card = pm.card;
    var strings = pm.strings || {};
    var combatPanelCounter = 0;

    // Local string reader (see header): resolves this card's own keys with {tokens}.
    function ps(key, kw) {
      var s = (strings && strings[key]);
      if (s === undefined || s === null) { s = key; }
      if (kw) {
        s = s.replace(/\{(\w+)\}/g, function (m, name) {
          return (kw[name] !== undefined && kw[name] !== null) ? String(kw[name]) : m;
        });
      }
      return s;
    }

    var children = [];
    var headline = headlineNode();
    if (headline) { children.push(headline); }
    children.push(head());
    var rr = rrNode();
    if (rr) { children.push(rr); }
    children.push(ui.roundSelector({
      surface: "postmatch", matchId: card.match_id, rounds: card.round_details,
      myTeam: Number(card.my_team || 0), text: ps,
      render: function (round) {
        var scoped = card;
        if (round) {
          scoped = Object.assign({}, card, {
            winner: round.won, sides: {}, has_scoreboard: true,
            has_team_kills: round.scoreboard.some(function (p) { return p.team_kills !== null && p.team_kills !== undefined; }),
            teams: { "1": round.scoreboard.filter(function (p) { return p.team === 1; }),
                     "2": round.scoreboard.filter(function (p) { return p.team === 2; }) }
          });
        }
        var section = el("div", "round-content");
        section.appendChild(teams(scoped));
        if (!scoped.has_scoreboard) { section.appendChild(el("div", "pm-note", ps("note"))); }
        else if (scoped.has_team_kills) { section.appendChild(el("div", "pm-note", ps("kills_note"))); }
        return section;
      }
    }));
    var veto = vetoNode();
    if (veto) { children.push(el("div", "pm-h", ps("veto")), veto); }
    children.push(el("div", "pm-hint", ps("stays_hint")));

    var overlay = ui.modal({
      title: ps("title"),
      children: children,
      // THE ONE WAY OUT, and it is all three of them: ui.modal gives the X, the backdrop click and
      // Escape the same handler. Python clears the card; the next snapshot simply has nothing to
      // draw, and the core's clearOverlays takes this node away.
      onClose: function () { ui.clearRoundSelection("postmatch"); call("close_postmatch"); }
    });
    overlay.classList.add("pm-overlay");
    overlay._postmatchKey=refreshKey(state);
    var dialog = overlay.querySelector(".ui-modal");
    if (dialog) {
      dialog.classList.add("pm-modal", "pm-" + tone());
      // Focus it so Escape works without a click first, and so a screen reader lands on the
      // result rather than leaving the player on the page underneath.
      dialog.setAttribute("tabindex", "-1");
      setTimeout(function () { dialog.focus(); }, 0);
    }
    document.body.appendChild(overlay);

    // -- the verdict ----------------------------------------------------------
    // Three answers and no fourth. `won` null is a real state - a match that ended without the
    // service naming a winner - and it gets NO headline rather than a guess: the map and the
    // scoreline below still say everything that is actually known.
    function tone() {
      if (card.voided) { return "void"; }
      if (card.won === true) { return "win"; }
      if (card.won === false) { return "loss"; }
      return "unknown";
    }

    function headlineNode() {
      var kind = tone();
      if (kind === "unknown") { return null; }
      var box = el("div", "pm-verdict " + kind);
      box.appendChild(el("div", "pm-headline",
        kind === "win" ? ps("victory") : (kind === "loss" ? ps("defeat") : ps("voided"))));
      if (kind === "void") { box.appendChild(el("div", "pm-void-body", ps("voided_body"))); }
      if (card.recovery_forfeit) { box.appendChild(el("div", "pm-void-body", ps("recovery_forfeit"))); }
      return box;
    }

    // -- head (map + scoreline + length) --------------------------------------
    function head() {
      var box = el("div", "pm-head");
      var left = el("div", "pm-head-left");
      left.appendChild(el("div", "pm-map", card.map || ps("no_map")));
      if (card.duration) {
        left.appendChild(el("div", "pm-meta", ps("duration") + " " + card.duration));
      }
      box.appendChild(left);
      box.appendChild(scoreNode());
      return box;
    }

    function scoreNode() {
      // BY TEAM NUMBER, like the match detail, so the two columns below read straight off it.
      // No score is an honest line, never a 0 : 0 nobody played.
      if (!card.score) { return el("div", "pm-score none", ps("no_score")); }
      var box = el("div", "pm-score");
      box.appendChild(el("span", "pm-num" + (card.winner === 1 ? " won" : ""), String(card.score[0])));
      box.appendChild(el("span", "pm-colon", ":"));
      box.appendChild(el("span", "pm-num" + (card.winner === 2 ? " won" : ""), String(card.score[1])));
      return box;
    }

    // -- the rank move --------------------------------------------------------
    function rrNode() {
      if (card.voided) { return null; }              // a voided match moved nobody
      // Placements pay no RR, so say how many are left, or where this one landed the player.
      if (card.placing) { return el("div", "pm-rr flat", ps("placements_left", { n: card.placements_left })); }
      if (card.placed && card.placed_rank) { return el("div", "pm-rr flat", ps("placed", { rank: card.placed_rank })); }
      // THE RR THE MATCH MOVED. Not `delta`: that is an arrow count, 1 to 3, and printing it with
      // "RR" after it is what read as "only gaining and losing 1-3 RR" (2026-09-16). No figure
      // from the service means no line, never the arrows.
      if (card.rr_delta === null || card.rr_delta === undefined) { return null; }
      var d = Number(card.rr_delta) || 0;
      if (!d) { return el("div", "pm-rr flat", ps("no_rr")); }
      return el("div", "pm-rr " + (d > 0 ? "up" : "down"),
        (d > 0 ? "+" : "") + d + " " + ps("rr_unit"));
    }

    // -- the two sides --------------------------------------------------------
    function teams(card) {
      var box = el("div", "pm-teams");
      ["1", "2"].forEach(function (n) {
        var col = el("div", "pm-team" + (String(card.my_team) === n ? " mine" : ""));
        var side = (card.sides || {})[n];
        // " · " and not two spaces: HTML collapses runs of whitespace, so the match detail's
        // "Team 1  attack" arrives on screen as "Team 1 attack" - three facts run together into
        // one unreadable line once the side and the "you" marker are both on it.
        var label = ps("team", { n: n });
        if (side) { label += " · " + (side === "attack" ? ps("side_attack") : ps("side_defend")); }
        if (String(card.my_team) === n) { label += " · " + ps("you"); }
        var headEl = el("div", "pm-team-h" + (card.winner === Number(n) ? " won" : ""), label);
        col.appendChild(headEl);
        // The column strip, only when there are numbers to head. Same grid as the rows below.
        if (card.has_scoreboard) { col.appendChild(colHeads(card)); }
        ((card.teams || {})[n] || []).forEach(function (p) {
          var row = el("div", "pm-player" + (p.is_me ? " you" : "") + (p.left ? " left" : "")
                              + (card.has_scoreboard ? " sb" : "")
                              + (card.has_team_kills ? "" : " no-tk")
                              + (card.has_scoreboard && !p.reported ? " unreported" : ""));
          // Preserve the old roster-only card when neither scoreboard nor combat evidence exists.
          // A newer result may carry combat before K/D, so a real combat object still earns detail.
          var detail = (card.has_scoreboard || p.combat) ? combatPanel(p, "pm") : null;
          row.appendChild(detail ? combatPlayerCell(p, detail, "pm")
                                 : el("span", "pm-pname", p.name));
          if (card.has_scoreboard) {
            row.appendChild(statCell(p.kills));
            row.appendChild(statCell(p.deaths));
            row.appendChild(statCell(p.kd === null || p.kd === undefined ? null : p.kd.toFixed(2)));
            if (card.has_team_kills) {
              var tk = statCell(p.team_kills);
              // The one number on this card somebody is answerable for.
              if (p.team_kills) { tk.classList.add("bad"); }
              row.appendChild(tk);
            }
          }
          col.appendChild(row);
          if (detail) { col.appendChild(detail); }
        });
        box.appendChild(col);
      });
      return box;
    }

    // The column heads above each side. Built with the same classes as a row so one grid
    // template positions both and the numbers cannot drift out from under their labels.
    function colHeads(card) {
      var head = el("div", "pm-cols" + (card.has_team_kills ? "" : " no-tk"));
      head.appendChild(el("span", "pm-pname", ""));
      head.appendChild(el("span", "pm-n", ps("col_k")));
      head.appendChild(el("span", "pm-n", ps("col_d")));
      head.appendChild(el("span", "pm-n", ps("col_kd")));
      if (card.has_team_kills) { head.appendChild(el("span", "pm-n", ps("col_tk"))); }
      return head;
    }

    /** One number, or an em dash. Never a 0 standing in for "we were not told". */
    function statCell(value) {
      var cell = el("span", "pm-n");
      if (value === null || value === undefined) {
        cell.classList.add("pending");
        cell.textContent = ps("stat_none");
      } else {
        cell.textContent = String(value);
      }
      return cell;
    }

    function combatPlayerCell(p, panel, prefix) {
      var combat = p.combat || {};
      var status = combat.status || "unavailable";
      var cell = el("span", prefix + "-pidentity");
      cell.appendChild(el("span", prefix + "-pname", p.name));
      cell.appendChild(el("span", "combat-status status-" + status, combatStatus(status)));
      var toggle = ui.btn("combat-toggle", ps("combat_details"), function () {
        var willOpen = panel.hidden;
        panel.hidden = !willOpen;
        toggle.setAttribute("aria-expanded", willOpen ? "true" : "false");
      }, { ariaLabel: ps("combat_details") + ": " + p.name });
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("data-player", p.steam_id);
      toggle.setAttribute("aria-controls", panel.id);
      cell.appendChild(toggle);
      return cell;
    }

    function combatStatus(status) {
      if (status === "complete") { return ps("combat_complete"); }
      if (status === "partial") { return ps("combat_partial"); }
      return ps("combat_unavailable");
    }

    function combatPanel(p, prefix) {
      var combat = p.combat || { status: "unavailable", weaponStats: [] };
      var panel = el("div", prefix + "-combat combat-panel");
      panel.id = prefix + "-combat-" + (++combatPanelCounter);
      panel.hidden = true;
      panel.appendChild(el("div", "combat-status status-" + combat.status,
        combatStatus(combat.status)));
      if (combat.status === "unavailable") {
        panel.appendChild(el("div", "combat-note", ps("combat_unavailable_note")));
        return panel;
      }
      if (combat.status === "partial") {
        panel.appendChild(el("div", "combat-note", ps("combat_partial_note")));
      }
      var metrics = el("div", "combat-metrics");
      metrics.appendChild(combatMetric("damage", ps("col_damage"), combat.enemyDamage));
      metrics.appendChild(combatMetric("friendly-damage", ps("col_friendly_damage"), combat.friendlyDamage));
      metrics.appendChild(combatMetric("damage-taken", ps("col_damage_taken"), combat.damageTaken));
      metrics.appendChild(combatMetric("adr", ps("col_adr"), combat.adr, "decimal"));
      metrics.appendChild(combatMetric("assists", ps("col_assists"), combat.assists));
      metrics.appendChild(combatMetric("headshots", ps("col_headshots"), combat.headshots));
      metrics.appendChild(combatMetric("accuracy", ps("col_accuracy"), combat.accuracy, "percent"));
      panel.appendChild(metrics);
      if ((combat.playerStats || []).length) { panel.appendChild(playerDamageTable(combat.playerStats)); }
      else { panel.appendChild(el("div", "combat-note", ps("player_damage_unavailable"))); }
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
      if (value === null || value === undefined) { return ps("stat_none"); }
      var number = Number(value);
      if (!Number.isFinite(number)) { return ps("stat_none"); }
      var text = Number.isInteger(number) ? String(number) : String(Math.round(number * 10) / 10);
      return format === "percent" ? text + "%" : text;
    }

    function playerDamageTable(rows) {
      var section = el("div", "combat-weapons-section");
      section.appendChild(el("div", "combat-weapons-title", ps("player_damage")));
      var scroll = el("div", "combat-weapons-scroll");
      var table = el("table", "combat-weapons combat-player-damage");
      var head = document.createElement("thead"), headRow = document.createElement("tr");
      [ps("damage_player"), ps("damage_to"), ps("damage_from")].forEach(function (label) {
        headRow.appendChild(el("th", "", label));
      });
      head.appendChild(headRow); table.appendChild(head);
      var body = document.createElement("tbody");
      rows.forEach(function (player) {
        var tr = document.createElement("tr");
        tr.appendChild(el("td", "combat-weapon-name", player.name || ps("unknown_source")));
        [player.damageDealt, player.damageTaken].forEach(function (value) {
          tr.appendChild(el("td", value === null || value === undefined ? "pending" : "",
            combatNumber(value)));
        });
        body.appendChild(tr);
      });
      table.appendChild(body); scroll.appendChild(table); section.appendChild(scroll);
      return section;
    }

    // -- the veto, in the order it happened ------------------------------------
    function vetoNode() {
      var bans = card.bans || [];
      if (!bans.length) { return null; }
      var box = el("div", "pm-veto");
      bans.forEach(function (b) { box.appendChild(el("span", "pm-ban", b.map)); });
      return box;
    }
  }
})();
