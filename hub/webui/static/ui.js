/* Lights Out — shared UI core (window.HubUI).
 *
 * SHARED, OWNED BY THE CORE. This is the ONE place every screen imports its component helpers and
 * primitives from, so the whole app stays design-consistent. A screen module does NOT edit this
 * file; it builds its DOM with these helpers (passed in as `ctx.ui`) and gets the shared look for
 * free. If a screen needs a new shared primitive, that is a deliberate core change here.
 *
 * Loaded as a plain <script> (no ES modules): under file:// + WebView2/WKWebView, `import` hits
 * module CORS restrictions, so the whole front end uses plain scripts and this global registry
 * (see docs/ui-redesign-plan.md and index.html). This script runs FIRST and creates window.HubUI;
 * core.js augments it with the screen registry/router; screen modules register onto it last.
 *
 * Everything here is pure DOM/string work: no state is owned here. i18n strings are handed to the
 * core each render (setStrings) so t() can resolve keys; components never reach into app state. */
(function () {
  "use strict";

  var HubUI = window.HubUI || (window.HubUI = {});

  // ---------------------------------------------------------------- i18n + escaping
  HubUI._strings = {};                         // set by core each render (setStrings)
  HubUI.setStrings = function (strings) { HubUI._strings = strings || {}; };

  function t(key, kw) {
    var s = (HubUI._strings && HubUI._strings[key]) || key;
    if (kw) {
      s = s.replace(/\{(\w+)\}/g, function (m, name) {
        return (kw[name] !== undefined && kw[name] !== null) ? String(kw[name]) : m;
      });
    }
    return s;
  }

  function esc(v) {
    return String(v === undefined || v === null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function initials(name) {
    var parts = String(name || "?").replace(/_/g, " ").split(/\s+/).filter(Boolean);
    if (!parts.length) { return "?"; }
    if (parts.length === 1) { return parts[0].slice(0, 2).toUpperCase(); }
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }

  function clock(seconds) {
    seconds = Math.max(0, seconds | 0);
    var m = Math.floor(seconds / 60), s = seconds % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  // ---------------------------------------------------------------- tiny DOM helpers
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }

  // btn(cls, text, onclick, opts) — an interactive element. Defaults to a real <button> (keyboard
  // accessible); pass opts.tag to render another element (e.g. a "span" link). type="button" avoids
  // implicit form submits. touch-action is handled by the component CSS.
  function btn(cls, text, onclick, opts) {
    opts = opts || {};
    var tag = opts.tag || "button";
    var b = document.createElement(tag);
    if (cls) { b.className = cls; }
    if (tag === "button") { b.type = "button"; }
    if (text !== undefined) { b.textContent = text; }
    if (opts.ariaLabel) { b.setAttribute("aria-label", opts.ariaLabel); }
    if (opts.disabled) { b.disabled = true; }
    if (onclick) { b.addEventListener("click", onclick); }
    return b;
  }

  // ---------------------------------------------------------------- transient cues (toast)
  var toastTimer = null;
  // toast(text, ms) — ms is how long it stays up; omitted means the 1.8 s default. Python sets it
  // per cue (competitive's "close your game" asks for three seconds), so the length lives with the
  // sentence rather than being one number for every cue there will ever be.
  function toast(text, ms) {
    var e = document.getElementById("toast");
    if (!e) { e = document.createElement("div"); e.id = "toast"; document.body.appendChild(e); }
    e.textContent = text;
    e.classList.add("show");
    if (toastTimer) { clearTimeout(toastTimer); }
    toastTimer = setTimeout(function () { e.classList.remove("show"); }, +ms > 0 ? +ms : 1800);
  }

  // ================================================================ shared components
  // Each returns a detached DOM node the caller appends. Styles live in ui.css (ui-* classes).

  // chip(text, opts) — status pill / inline action. opts: {variant:'accent'|'green'|'gold'|'solid',
  // onClick, ariaLabel}. With onClick it is a real <button>; otherwise an inert <span>.
  function chip(text, opts) {
    opts = opts || {};
    var cls = "ui-chip" + (opts.variant ? " " + opts.variant : "");
    if (opts.onClick) { return btn(cls, text, opts.onClick, { ariaLabel: opts.ariaLabel }); }
    var s = el("span", cls, text);
    if (opts.ariaLabel) { s.setAttribute("aria-label", opts.ariaLabel); }
    return s;
  }

  // card(opts) — a titled surface. opts: {title, accent:bool, children:[nodes]}.
  function card(opts) {
    opts = opts || {};
    var c = el("div", "ui-card" + (opts.accent ? " accent" : ""));
    if (opts.title) { c.appendChild(el("div", "ui-card-title", opts.title)); }
    appendChildren(c, opts.children);
    return c;
  }

  // panel(children) — a plain surface (no title).
  function panel(children) {
    var p = el("div", "ui-panel");
    appendChildren(p, children);
    return p;
  }

  // statTile(number, label) — a number + label tile.
  function statTile(number, label) {
    var s = el("div", "ui-stat");
    s.appendChild(el("div", "n", number === null || number === undefined ? "—" : String(number)));
    s.appendChild(el("div", "l", label || ""));
    return s;
  }

  // tabs(items, activeId, onSelect) — a segmented control. items: [{id, label}].
  function tabs(items, activeId, onSelect) {
    var wrap = el("div", "ui-tabs");
    wrap.setAttribute("role", "tablist");
    (items || []).forEach(function (it) {
      var b = btn("ui-tab" + (it.id === activeId ? " active" : ""), it.label, function () {
        if (onSelect) { onSelect(it.id); }
      });
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", it.id === activeId ? "true" : "false");
      wrap.appendChild(b);
    });
    return wrap;
  }

  // listRow(opts) — one row. opts: {leading:node, children:[nodes], trailing:node}.
  function listRow(opts) {
    opts = opts || {};
    var row = el("div", "ui-listrow");
    if (opts.leading) { row.appendChild(opts.leading); }
    var grow = el("div", "grow");
    appendChildren(grow, opts.children);
    row.appendChild(grow);
    if (opts.trailing) { row.appendChild(opts.trailing); }
    return row;
  }

  // table(rows) — a stack of listRow() nodes.
  function table(rows) {
    var t2 = el("div", "ui-table");
    appendChildren(t2, rows);
    return t2;
  }

  // avatar(opts) — {text, url, light, anon, ariaLabel}. With `url` (a Steam avatar) the plate shows
  // the PICTURE, with the text still underneath it as the fallback: the image is loaded from the
  // hub's own bridge (/avatar?u=...), because the page's CSP allows `img-src 'self'` and would
  // not allow steamstatic.com - and because the bridge caches the file instead of re-fetching it
  // from Steam on every render.
  //
  // A FAILED IMAGE IS NOT AN EMPTY BOX. The hub may be offline, the account may have no avatar,
  // and the cache may be empty on the very first draw; on any of those the <img> removes itself
  // and what is left is exactly the initials plate this has always drawn.
  //
  // `anon` is the opposite case: a blank silhouette for someone whose identity is being withheld
  // (the pre-round lobby's enemy team). It drops the picture as well as the letters, so a url
  // that reaches it anyway cannot uncover the person the caller meant to hide.
  function avatar(opts) {
    opts = opts || {};
    var a = el("div", "ui-avatar" + (opts.light ? " light" : "") + (opts.anon ? " anon" : ""),
               opts.text !== undefined ? String(opts.text) : "");
    if (opts.ariaLabel) { a.setAttribute("aria-label", opts.ariaLabel); }
    if (opts.url && !opts.anon) {
      var img = document.createElement("img");
      img.className = "ui-avatar-img";
      img.alt = "";
      img.addEventListener("error", function () {
        if (img.parentNode) { img.parentNode.removeChild(img); }
      });
      img.src = "/avatar?u=" + encodeURIComponent(opts.url);
      a.appendChild(img);
    }
    return a;
  }

  // emptyState(opts) — {icon, message, action:node}.
  function emptyState(opts) {
    opts = opts || {};
    var e = el("div", "ui-empty");
    e.appendChild(el("div", "ui-empty-icon", opts.icon || "◆"));
    if (opts.message) { e.appendChild(el("div", "ui-empty-msg", opts.message)); }
    if (opts.action) { e.appendChild(opts.action); }
    return e;
  }

  // modal(opts) — an overlay + dialog. opts: {title, children:[nodes], onClose}. Returns the
  // overlay node; append it to document.body. The X, the backdrop and Escape all call onClose.
  //
  // THE X IS NOT DECORATION. Until 2026-09-16 the only ways out were a backdrop click and
  // Escape, and neither is visible: Sam opened a match from the history list and reported
  // there was no way to exit the screen. A player should never have to guess at a keystroke
  // to leave something that covers the whole window.
  function modal(opts) {
    opts = opts || {};
    var overlay = el("div", "ui-overlay");
    var dialog = el("div", "ui-modal");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    if (opts.title) {
      var titleEl = el("div", "ui-modal-title", opts.title);
      titleEl.id = "ui-modal-title";
      dialog.setAttribute("aria-labelledby", "ui-modal-title");
      dialog.appendChild(titleEl);
    }
    appendChildren(dialog, opts.children);
    overlay.appendChild(dialog);
    function close() { if (opts.onClose) { opts.onClose(); } }
    if (opts.onClose) {
      // A real <button>, so it is tabbable and reachable without a mouse, and appended to the
      // dialog rather than the title row so it sits in the same corner with or without a title.
      var x = document.createElement("button");
      x.className = "ui-modal-x";
      x.type = "button";
      x.setAttribute("aria-label", "Close");
      x.textContent = "×";
      x.addEventListener("click", close);
      dialog.appendChild(x);
    }
    overlay.addEventListener("click", function (e) { if (e.target === overlay) { close(); } });
    overlay.addEventListener("keydown", function (e) { if (e.key === "Escape") { close(); } });
    return overlay;
  }

  function appendChildren(parent, children) {
    if (!children) { return; }
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c) { parent.appendChild(c); }
    }
  }

  // Decorative: the adjacent map name is always the accessible label. Missing
  // installations/new maps leave the existing styled placeholder intact.
  function mapImage(name, className) {
    var image = el("img", className);
    image.alt = "";
    image.setAttribute("aria-hidden", "true");
    image.decoding = "async";
    image.draggable = false;
    image.addEventListener("error", function () { image.remove(); });
    if (name) { image.src = "/map-thumbnail?map=" + encodeURIComponent(name); }
    else { image.hidden = true; }
    return image;
  }

  // ---------------------------------------------------------------- export
  HubUI.mapImage = mapImage;
  HubUI.t = t;
  HubUI.esc = esc;
  HubUI.initials = initials;
  HubUI.clock = clock;
  HubUI.el = el;
  HubUI.btn = btn;
  HubUI.toast = toast;
  HubUI.chip = chip;
  HubUI.card = card;
  HubUI.panel = panel;
  HubUI.statTile = statTile;
  HubUI.tabs = tabs;
  HubUI.listRow = listRow;
  HubUI.table = table;
  HubUI.avatar = avatar;
  HubUI.emptyState = emptyState;
  HubUI.modal = modal;
})();
