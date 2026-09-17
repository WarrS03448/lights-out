/* One delegated listener survives screen redraws and covers modal/title-bar controls. */
(function () {
  "use strict";
  var settings = { enabled: true, volume: 35 };
  var cues = {};
  var sources = {
    click: "audio/ui-click.wav",
    report: "audio/report-submit.mp3",
    "map-ban": "audio/map-ban.mp3",
    "map-ban-turn": "audio/map-ban-turn.mp3"
  };
  var banMatch = null;
  var seenBans = new Set();
  var notifiedTurns = new Set();
  function configure(next) {
    if (!next) { return; }
    settings.enabled = next.enabled !== false;
    var volume = Number(next.volume);
    settings.volume = Number.isFinite(volume) ? Math.max(0, Math.min(100, volume)) : 35;
    if (!settings.enabled || !settings.volume) {
      Object.keys(cues).forEach(function (name) { cues[name].pause(); });
    }
  }
  function play(kind) {
    if (!settings.enabled || !settings.volume) { return; }
    try {
      var name = kind === "report" || kind === "map-ban" || kind === "map-ban-turn" ? kind : "click";
      var cue = cues[name];
      if (!cue) {
        cue = cues[name] = new Audio(sources[name]);
        cue.preload = "auto";
      }
      if (name === "map-ban" || name === "map-ban-turn") {
        var other = cues[name === "map-ban" ? "map-ban-turn" : "map-ban"];
        if (other) { other.pause(); }
      }
      cue.volume = settings.volume / 100;
      cue.currentTime = 0; // Rapid clicks restart the cue instead of stacking loud voices.
      var pending = cue.play();
      if (pending && pending.catch) { pending.catch(function () {}); }
    } catch (e) { /* Audio/device failure must never interfere with a UI action. */ }
  }
  function syncMapBans(comp) {
    var match = (comp && comp.match_id) || "";
    var bans = (comp && comp.map_bans) || [];
    var added = false;
    if (match !== banMatch) {
      banMatch = match;
      // Loading/recovering a match establishes a baseline; old bans are silent.
      seenBans = new Set(bans.map(function (ban) { return ban.map; }));
      notifiedTurns = new Set();
    } else {
      bans.forEach(function (ban) {
        if (!seenBans.has(ban.map)) { seenBans.add(ban.map); added = true; }
      });
    }
    var lobby = comp && comp.lobby;
    var yourTurn = match && comp.phase === "lobby" && lobby && lobby.stage === "veto" && lobby.my_turn;
    var turnKey = lobby ? bans.length + ":" + lobby.ban_turn : "";
    // A captain may hold both teams, so each added ban can begin another turn
    // without my_turn ever becoming false. Record muted turns as observed too.
    if (yourTurn && !notifiedTurns.has(turnKey)) {
      notifiedTurns.add(turnKey);
      play("map-ban-turn");
    } else if (match && added && (comp.phase === "lobby" || comp.phase === "connecting")) {
      // A handoff to this captain uses only the higher cue, avoiding a double sound.
      play("map-ban");
    }
  }
  window.HubUI.clickSound = { configure: configure, play: play, syncMapBans: syncMapBans };
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) { return; }
    if (target.closest('[disabled], [aria-disabled="true"], [inert], [data-click-sound="off"]')) { return; }
    // Reserve the cue for actions/navigation; editing and small form controls stay quiet.
    if (target.closest('input:not([type="button"]):not([type="submit"]):not([type="reset"]), textarea, select, label, summary, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="searchbox"], [role="combobox"], [role="slider"], [role="checkbox"], [role="switch"]')) { return; }
    var control = target.closest('button, a[href], input[type="button"], input[type="submit"], input[type="reset"], [role="button"], [role="tab"], .navitem, .hist-open');
    if (control) { play(control.getAttribute("data-click-sound")); }
  }, true); // Capture before action handlers remove their clicked control.
}());
