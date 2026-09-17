/* One round selector shared by History and the persistent result card. */
(function () {
  "use strict";
  var selected = {};
  window.HubUI.clearRoundSelection = function (surface) { delete selected[surface]; };
  window.HubUI.roundSelector = function (options) {
    var ui = window.HubUI, el = ui.el, text = options.text;
    var rounds = options.rounds || [], key = String(options.matchId || "");
    var saved = selected[options.surface];
    var viewState = saved && saved.key === key ? saved : { key: key, n: null, expanded: {} };
    var current = saved && saved.key === key ? saved.n : null;
    if (!rounds.some(function (r) { return r.n === current; })) { current = null; }
    var box = el("section", "round-view"), controls = el("div", "round-controls");
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", text("round_hint"));
    var summary = el("div", "round-summary");
    summary.setAttribute("aria-live", "polite");
    var content = el("div", "round-content");
    var buttons = [];
    function choose(n) {
      current = n;
      viewState.n = n;
      selected[options.surface] = viewState;
      buttons.forEach(function (b) { b.node.setAttribute("aria-pressed", b.n === n ? "true" : "false"); });
      var round = rounds.find(function (r) { return r.n === n; }) || null;
      summary.textContent = "";
      if (round) {
        summary.appendChild(el("strong", "round-title", text("round_label", { n: round.n })));
        summary.appendChild(el("span", "", round.won ? text("round_winner", { n: round.won }) : text("round_unknown")));
        if (round.score) {
          var score = options.myTeam === 2 ? round.score.slice().reverse() : round.score;
          summary.appendChild(el("span", "", text("round_score") + ": " + score.join(" – ")));
        }
        if (typeof round.seconds === "number" && round.seconds >= 0) {
          var secs = Math.floor(round.seconds);
          summary.appendChild(el("span", "", text("round_duration") + ": " + Math.floor(secs / 60) + ":" + String(secs % 60).padStart(2, "0")));
        }
        if (!round.has_stats) { summary.appendChild(el("span", "round-missing", text("round_no_stats"))); }
      } else if (rounds.length) { summary.textContent = text("round_hint"); }
      content.textContent = "";
      content.appendChild(options.render(round));
      var expanded = viewState.expanded[String(n)] || {};
      content.querySelectorAll(".combat-toggle[data-player]").forEach(function (toggle) {
        if (expanded[toggle.getAttribute("data-player")]) { toggle.click(); }
      });
    }
    content.addEventListener("click", function (event) {
      var toggle = event.target.closest(".combat-toggle[data-player]");
      if (!toggle) { return; }
      var expanded = viewState.expanded[String(current)] || (viewState.expanded[String(current)] = {});
      expanded[toggle.getAttribute("data-player")] = toggle.getAttribute("aria-expanded") === "true";
    });
    function button(n, label, winner) {
      var b = ui.btn("round-choice" + (winner ? " t" + winner : ""), label, function () { choose(n); });
      b.setAttribute("data-round", n === null ? "all" : String(n));
      buttons.push({ n: n, node: b });
      controls.appendChild(b);
    }
    if (rounds.length) {
      button(null, text("all_rounds"));
      rounds.forEach(function (r) { button(r.n, text("round_label", { n: r.n }), r.won); });
      box.appendChild(controls);
      box.appendChild(summary);
    }
    box.appendChild(content);
    choose(current);
    return box;
  };
})();
