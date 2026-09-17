/* The hub's own title bar.
 *
 * The window is frameless on Windows (hub/webui/shell.py), so #topbar IS the caption: the nav sits
 * in it as before, and this adds the drag strip and the minimise / maximise / close buttons on the
 * right. Everything here talks to POST /window/<op>, which is handled off the UI thread and does
 * the Win32 work (hub/webui/window.py).
 *
 * BOUND ONCE, AT LOAD. core.js re-renders #nav's innerHTML on every snapshot, so a listener
 * attached to anything inside #nav would be thrown away several times a second. The buttons are
 * appended to #topbar itself, which core.js never rebuilds, and the handlers go on #topbar.
 *
 * WHY THE DRAG IS A STREAM OF TICKS. The native move loop cannot be used at all here (WebView2
 * holds the mouse capture in its own process — hub/webui/window.py has the measurements), so the
 * host moves the window itself, once per tick. The tick carries NO COORDINATES: the host reads the
 * cursor with GetCursorPos. A MouseEvent's screenX is in CSS pixels, so sending it would put the
 * window two thirds of the way to the cursor on a 150% display.
 */
(function () {
  "use strict";

  var HubUI = window.HubUI;
  var topbar = document.getElementById("topbar");
  if (!topbar) { return; }

  var available = true;          // flipped off if the host says it has no frame for us to draw

  function op(name) {
    return fetch("/window/" + name, { method: "POST" }).then(function (r) {
      return r.status === 200 && r.headers.get("Content-Length") !== "0"
        ? r.json().catch(function () { return null; })
        : null;
    }).catch(function () { return null; });
  }

  // ---------------------------------------------------------------- the buttons
  // English fallbacks, not bare t(): t() returns the KEY when a string is missing, and the
  // strings only arrive with the first snapshot - so without these the tooltips would read
  // "win_minimize" for the first moment of every launch.
  var FALLBACK = { win_minimize: "Minimise", win_maximize: "Maximise", win_close: "Close" };

  function t(key) {
    return (HubUI && HubUI._strings && HubUI._strings[key]) || FALLBACK[key] || key;
  }

  var controls = document.createElement("div");
  controls.className = "wincontrols";
  controls.innerHTML =
    '<button class="winbtn" data-win="minimize" tabindex="-1"><svg viewBox="0 0 10 10" aria-hidden="true">' +
    '<path d="M0 5h10" /></svg></button>' +
    '<button class="winbtn" data-win="maximize" tabindex="-1"><svg viewBox="0 0 10 10" aria-hidden="true">' +
    '<rect class="win-restore-back" x="2.5" y="0.5" width="7" height="7" />' +
    '<rect class="win-box" x="0.5" y="2.5" width="7" height="7" /></svg></button>' +
    '<button class="winbtn winbtn-close" data-win="close" tabindex="-1"><svg viewBox="0 0 10 10" aria-hidden="true">' +
    '<path d="M0 0l10 10M10 0L0 10" /></svg></button>';
  topbar.appendChild(controls);

  function label() {
    var min = controls.querySelector('[data-win="minimize"]');
    var max = controls.querySelector('[data-win="maximize"]');
    var cls = controls.querySelector('[data-win="close"]');
    // Tooltips and screen-reader names come from the same string table as the rest of the UI, so
    // they follow the language picker. Re-applied on every snapshot (setLanguage changes them).
    [[min, "win_minimize"], [max, "win_maximize"], [cls, "win_close"]].forEach(function (pair) {
      var text = t(pair[1]);
      pair[0].setAttribute("title", text);
      pair[0].setAttribute("aria-label", text);
    });
  }
  label();
  // Re-label whenever the strings change. core.js calls HubUI.setStrings on every snapshot and
  // looks the property up at call time, so wrapping it here works whatever the script order is -
  // and is less invasive than making core.js announce a language change it has no other use for.
  var setStrings = HubUI.setStrings;
  HubUI.setStrings = function (strings) { setStrings(strings); label(); };

  function setMaximized(on) {
    topbar.classList.toggle("is-maximized", !!on);
    // On <html> too: the border grips live outside #topbar and have to disappear when the window
    // fills the screen, because there is no edge left to drag.
    document.documentElement.classList.toggle("win-maximized", !!on);
  }

  controls.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest(".winbtn");
    if (!btn) { return; }
    e.stopPropagation();
    op(btn.getAttribute("data-win")).then(function (res) {
      if (res) { setMaximized(res.maximized); }
    });
  });

  // ---------------------------------------------------------------- drag + double-click
  // Anything interactive in the bar (a nav item, a window button, the status dot) is NOT a handle;
  // only the bar's own background is.
  function isHandle(target) {
    return !(target.closest && target.closest(".navitem, .winbtn, button, a, input, select"));
  }

  var dragging = false;
  var pending = false;           // a tick is already queued for the next frame

  topbar.addEventListener("mousedown", function (e) {
    if (e.button !== 0 || !available || !isHandle(e.target)) { return; }
    e.preventDefault();          // no text selection, no drag-image of the bar
    dragging = true;
    op("drag/start");
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });

  // Throttled to one tick per animation frame. A mousemove stream can run well past the frame
  // rate, and every extra tick is a round trip that moves the window to the same place twice.
  function onMove() {
    if (!dragging || pending) { return; }
    pending = true;
    window.requestAnimationFrame(function () {
      pending = false;
      if (dragging) { op("drag/move"); }
    });
  }

  function onUp() {
    if (!dragging) { return; }
    dragging = false;
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
    // drag/end is where the host decides whether the release was a snap gesture, so the window can
    // still move after the last tick — the answer tells us which glyph the maximise button wears.
    op("drag/end").then(function (res) {
      if (res) { setMaximized(res.maximized); }
    });
  }

  topbar.addEventListener("dblclick", function (e) {
    if (!available || !isHandle(e.target)) { return; }
    op("maximize").then(function (res) {
      if (res) { setMaximized(res.maximized); }
    });
  });

  // ---------------------------------------------------------------- the border grips
  // The window has no frame at all, so it has no resize edges either (hub/webui/window.py says
  // why the native ones were given up). These eight strips are the border: the four edges and,
  // over them, the four corners. They carry the resize cursors, and dragging one drives
  // SetWindowPos on the host exactly as the title bar drives a move.
  var EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
  var grips = document.createElement("div");
  grips.className = "wingrips";
  grips.innerHTML = EDGES.map(function (e) {
    return '<div class="wingrip wingrip-' + e + '" data-edge="' + e + '"></div>';
  }).join("");
  document.body.appendChild(grips);

  var sizing = false;
  var sizePending = false;

  grips.addEventListener("mousedown", function (e) {
    var grip = e.target.closest && e.target.closest(".wingrip");
    if (e.button !== 0 || !available || !grip) { return; }
    e.preventDefault();
    sizing = true;
    op("resize/start/" + grip.getAttribute("data-edge"));
    window.addEventListener("mousemove", onSizeMove);
    window.addEventListener("mouseup", onSizeUp);
  });

  function onSizeMove() {
    if (!sizing || sizePending) { return; }
    sizePending = true;
    window.requestAnimationFrame(function () {
      sizePending = false;
      if (sizing) { op("resize/move"); }
    });
  }

  function onSizeUp() {
    if (!sizing) { return; }
    sizing = false;
    window.removeEventListener("mousemove", onSizeMove);
    window.removeEventListener("mouseup", onSizeUp);
    op("resize/end");
  }

  // ---------------------------------------------------------------- native-frame hosts
  // On a platform where the window keeps its own frame (macOS), /window/* answers with an empty
  // body. Take the buttons back off rather than showing a row that does nothing, and stop
  // treating the bar as a drag handle.
  op("state").then(function (res) {
    if (res === null) {
      available = false;
      controls.remove();
      grips.remove();
      document.documentElement.classList.add("native-frame");
    } else {
      setMaximized(res.maximized);
    }
  });
})();
