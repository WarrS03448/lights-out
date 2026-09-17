/* Bug report screen — OWNED BY THE BUG REPORT SCREEN.
 *
 * The only JS file this screen edits. It draws one box, one button and the line that says who the
 * report goes as, and calls the verbs hub/webui/screens/bugreport.py registers (bug_set_text,
 * bug_send). Nothing here decides anything: the session refuses an empty box, a signed-out hub
 * and a second report inside five seconds, and the server refuses the same again.
 *
 * i18n: `nav_bugreport` lives in i18n.py and is read with ctx.t(); everything else ships inside
 * the snapshot slice (state.bugreport.strings) and is read with the local st() helper — NOT t() —
 * so the front-end key-scan test stays honest and i18n.py is not edited for one screen. */
(function () {
  "use strict";

  window.HubUI.registerScreen("bugreport", { render: render });

  // The countdown under the send button, ticking between snapshots.
  //
  // WHY A LOCAL TICKER AND NOT THE SNAPSHOT. The snapshot is rebuilt when the SESSION changes, not
  // on a clock (hub/webui/panel.py on_change), so a "wait 4s" drawn from it would sit there saying
  // 4 until something else moved. The session does re-emit once the cooldown expires, which is what
  // actually re-enables the button; this only keeps the number honest in between. It is cleared at
  // the top of every render because core.render() throws the whole screen away each time.
  var tick = null;

  function stopTick() {
    if (tick !== null) { clearInterval(tick); tick = null; }
  }

  function render(root, state, ctx) {
    var ui = ctx.ui, call = ctx.call, t = ctx.t;
    var el = ui.el;
    var s = state.bugreport || {};
    var strings = s.strings || {};
    var limit = s.max || 2000;
    // THE BOX'S ID CARRIES THE SEND COUNTER, and that is load-bearing rather than tidy. The core
    // saves every #app input's value before it rebuilds the screen and writes it back afterwards
    // BY ID (core.js restoreInputs), so that a snapshot landing mid-sentence cannot wipe what
    // somebody is typing. It does that unconditionally, which means the empty box drawn after a
    // successful send was refilled with the report that had just been filed - one press of Send
    // away from filing it a second time. A filed report bumps `seq` (hub/competitive.py
    // bug_seq), the id changes, the restore finds no such element and skips it, and the box is
    // properly empty. Every other render keeps the same id and the same protection.
    var boxId = "bugreport-text-" + (s.seq || 0);

    stopTick();

    // Local string lookup for this screen's own slice (see the i18n note above).
    function st(key, kw) {
      var v = (strings && strings[key] !== undefined) ? strings[key] : key;
      if (kw) {
        v = String(v).replace(/\{(\w+)\}/g, function (m, name) {
          return (kw[name] !== undefined && kw[name] !== null) ? String(kw[name]) : m;
        });
      }
      return v;
    }

    var wrap = el("div", "bug");

    var head = el("div", "bug-head");
    head.appendChild(el("h1", null, t("nav_bugreport")));
    head.appendChild(el("p", "bug-sub", st("subtitle")));
    wrap.appendChild(head);

    var card = el("div", "bug-card");

    // -- who it goes as -------------------------------------------------------
    // Shown rather than asked for: the id and the name are taken off the signed-in account by the
    // server, so the honest thing is to say whose they are before anything is sent.
    var who = el("div", "bug-who");
    if (s.signed_in) {
      who.appendChild(ui.avatar({ text: ui.initials(s.persona || ""), ariaLabel: s.persona || "" }));
      var whoText = el("div", "bug-whotext");
      whoText.appendChild(el("span", "bug-whoname", st("as", { name: s.persona || "" })));
      whoText.appendChild(el("span", "bug-whoid", st("as_id", { id: s.steam_id || "" })));
      who.appendChild(whoText);
    } else {
      who.appendChild(el("span", "bug-whoname", st("err_signed_out")));
    }
    card.appendChild(who);

    // -- the box --------------------------------------------------------------
    var field = el("div", "bug-field");
    var label = el("label", "bug-label", st("label"));
    label.setAttribute("for", boxId);
    field.appendChild(label);

    var box = document.createElement("textarea");
    box.id = boxId;                         // see boxId above: the core preserves an input's
    box.className = "bug-box";              // value BY ID across re-renders
    box.rows = 8;
    box.maxLength = limit;
    box.placeholder = st("placeholder");
    box.value = s.text || "";
    box.disabled = !s.signed_in;
    field.appendChild(box);

    var meter = el("div", "bug-meter");
    var left = el("span", "bug-left");
    left.id = "bugreport-left";
    left.textContent = st("left", { n: Math.max(0, limit - (box.value || "").length) });
    meter.appendChild(left);
    meter.appendChild(el("span", "bug-limit", st("limit", { n: s.cooldown_seconds || 5 })));
    field.appendChild(meter);
    card.appendChild(field);

    // -- send -----------------------------------------------------------------
    var cooling = (s.cooldown_ms || 0) > 0;
    // A local deadline rather than the raw number: the ticker below counts down against it while
    // the snapshot stands still.
    var until = cooling ? (Date.now() + s.cooldown_ms) : 0;

    var actions = el("div", "bug-actions");
    var send = ui.btn("bug-send", sendLabel(), function () {
      var node = document.getElementById(boxId);
      call("bug_send", node ? node.value : "");
    }, { tag: "button" });
    send.id = "bugreport-send";
    actions.appendChild(send);

    var note = el("div", "bug-note");
    note.id = "bugreport-note";
    card.appendChild(actions);
    card.appendChild(note);
    card.appendChild(el("div", "bug-privacy", st("privacy")));
    wrap.appendChild(card);
    root.appendChild(wrap);

    // The draft is synced on `change` (which fires on blur), NOT on every keystroke: the verb is
    // an HTTP POST to the local bridge and one per character typed is a lot of traffic for a
    // convenience. Send carries the live value itself, so nothing typed can be lost by this.
    box.addEventListener("change", function () { call("bug_set_text", box.value); });
    box.addEventListener("input", function () {
      var n = document.getElementById("bugreport-left");
      if (n) { n.textContent = st("left", { n: Math.max(0, limit - box.value.length) }); }
      refresh();
    });

    refresh();
    if (cooling) { tick = setInterval(refresh, 250); }

    function secondsLeft() {
      return until ? Math.max(0, Math.ceil((until - Date.now()) / 1000)) : 0;
    }

    function sendLabel() {
      if (s.sending) { return st("sending"); }
      var n = secondsLeft();
      if (n > 0) { return st("wait", { n: n }); }
      return s.sent ? st("sent_again") : st("send");
    }

    /** Re-label and re-enable the button, and redraw the line under it, without a full render. */
    function refresh() {
      var btn = document.getElementById("bugreport-send");
      var node = document.getElementById(boxId);
      var noteEl = document.getElementById("bugreport-note");
      if (!btn) { stopTick(); return; }
      var n = secondsLeft();
      if (n === 0 && tick !== null) { stopTick(); }
      btn.textContent = sendLabel();
      var empty = !node || !String(node.value || "").trim();
      btn.disabled = Boolean(s.sending) || n > 0 || empty || !s.signed_in;
      if (!noteEl) { return; }
      noteEl.className = "bug-note";
      var text = "";
      // THE COUNTDOWN IS NOT A TELLING-OFF. It lives on the button; this line only says something
      // when there is something to say. Keying it on `n > 0` as well as on the error put "One
      // report every 5 seconds - try again in a moment" under a report that had just been
      // accepted, which reads as a refusal of the thing that in fact worked. Only a send that was
      // actually REFUSED (error === "too_fast") earns the warning.
      if (s.error === "too_fast") {
        text = st("err_too_fast", { n: s.cooldown_seconds || 5 });
        noteEl.classList.add("warn");
      } else if (s.error === "empty") {
        text = st("err_empty");
        noteEl.classList.add("warn");
      } else if (s.error === "signed_out") {
        text = st("err_signed_out");
        noteEl.classList.add("warn");
      } else if (s.error) {
        text = st("err_failed");
        noteEl.classList.add("warn");
      } else if (s.sent) {
        text = st("sent");
        noteEl.classList.add("ok");
      }
      noteEl.textContent = text;
    }
  }
})();
