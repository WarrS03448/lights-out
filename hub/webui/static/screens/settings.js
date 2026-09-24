/* Settings screen — OWNED BY THE SETTINGS SCREEN.
 *
 * The only JS file this screen edits. It registers with the core and renders the detailed controls
 * the design moved into Settings (docs/ui-redesign-plan.md turn-2): the match-found sound, the
 * Bodycam game path + Browse, the language picker and Sign-out.
 *
 * It draws with the shared helpers (ctx.ui) and shared tokens/components; screen-local rules live in
 * screens/settings.css. Verbs it calls (settings_browse_game_path, settings_set_game_path,
 * settings_set_sound_volume, settings_test_sound, settings_sign_out,
 * settings_open_uninstall, settings_close_uninstall, settings_uninstall) are registered by
 * hub/webui/screens/settings.py; the language picker calls the CORE set_language verb.
 *
 * i18n: keys that i18n.py already carries are looked up with ctx.t() (the front-end key-scan test
 * asserts those exist). Strings unique to this screen ship inside the snapshot slice
 * (state.settings.strings) and are read with the local st() helper — NOT t() — so they never trip
 * that test and i18n.py is not edited. */
(function () {
  "use strict";

  window.HubUI.registerScreen("settings", { render: render });

  // The "also delete my data" tick, kept OUTSIDE render on purpose. The snapshot re-renders this
  // screen several times a second, and the core only preserves an input's `value` across a
  // re-render, never its `checked` — so a tick stored in the DOM would untick itself under the
  // player's cursor. It is view-only state and never leaves the page until Uninstall is pressed.
  var wipeData = false;

  function render(root, state, ctx) {
    var ui = ctx.ui, call = ctx.call, t = ctx.t;
    var esc = ui.esc, el = ui.el, initials = ui.initials;
    var s = state.settings || {};
    var strings = s.strings || {};

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

    root.appendChild(build());
    // The overlay goes on the BODY, not inside .settings: this screen is a scroll container, and
    // an overlay nested in one is clipped by it. Every other modal in the hub does the same, and
    // core.render() clears the body's overlays before each draw (test_webui_a_modal_is_cleared_
    // by_the_next_render), which is what closes this one when `confirming` goes false.
    if ((s.uninstall || {}).confirming) { document.body.appendChild(uninstallModal()); }
    if ((((s.account || {}).link) || {}).stage) { document.body.appendChild(accountLinkModal()); }

    function build() {
      var wrap = el("div", "settings");

      var head = el("div", "set-head");
      head.appendChild(el("h1", null, t("nav_settings")));
      head.appendChild(el("p", "set-sub", st("subtitle")));
      wrap.appendChild(head);

      var grid = el("div", "set-grid");
      var left = el("div", "set-col");
      var right = el("div", "set-col");

      left.appendChild(sectionLabel(st("sec_audio")));
      left.appendChild(audioCard());

      right.appendChild(sectionLabel(st("sec_game")));
      right.appendChild(gameCard());
      right.appendChild(sectionLabel(st("sec_account")));
      right.appendChild(accountCard());
      right.appendChild(sectionLabel(st("sec_uninstall")));
      right.appendChild(uninstallCard());

      grid.appendChild(left);
      grid.appendChild(right);
      wrap.appendChild(grid);
      return wrap;
    }

    function sectionLabel(text) { return el("div", "set-seclabel", text); }

    // -- Audio ----------------------------------------------------------------
    function audioCard() {
      var sound = s.sound || {};
      var card = el("div", "set-card");

      var row = el("div", "set-field");
      var top = el("div", "set-field-top");
      top.appendChild(el("span", "set-field-name", t("comp_sound_label")));
      var pct = el("span", "set-vol-pct");
      pct.id = "set-vol-pct";
      pct.textContent = sound.enabled ? (sound.volume + "%") : t("comp_sound_muted");
      top.appendChild(pct);
      row.appendChild(top);

      var control = el("div", "set-vol-row");
      var range = document.createElement("input");
      range.id = "settings-volume";
      range.type = "range";
      range.min = "0"; range.max = "100"; range.step = "1";
      range.value = String(sound.volume || 0);
      range.className = "set-range";
      range.setAttribute("aria-label", st("volume"));
      range.setAttribute("aria-valuemin", "0");
      range.setAttribute("aria-valuemax", "100");
      range.setAttribute("aria-valuenow", String(sound.volume || 0));
      // Live label on drag; commit (persist + play-nothing) on release, so we do not fire a verb
      // per pixel. The core preserves this input's value across snapshot re-renders by its id.
      range.addEventListener("input", function () {
        var v = parseInt(range.value, 10) || 0;
        range.setAttribute("aria-valuenow", String(v));
        var p = document.getElementById("set-vol-pct");
        if (p) { p.textContent = v > 0 ? (v + "%") : t("comp_sound_muted"); }
      });
      range.addEventListener("change", function () {
        call("settings_set_sound_volume", parseInt(range.value, 10) || 0);
      });
      control.appendChild(range);

      var test = ui.btn("set-btn", t("comp_sound_test"), function () { call("settings_test_sound"); },
        { tag: "button" });
      test.setAttribute("data-click-sound", "off");
      if (!sound.can_play) { test.disabled = true; }
      control.appendChild(test);
      row.appendChild(control);

      row.appendChild(el("div", "set-hint", st("sound_hint")));
      card.appendChild(row);
      card.appendChild(clickSoundField());
      return card;
    }

    function clickSoundField() {
      var sound = s.click_sound || { enabled: true, volume: 35 };
      var row = el("div", "set-field bordered");
      var top = el("div", "set-field-top");
      var label = el("label", "set-field-name");
      var toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.id = "settings-click-enabled";
      toggle.checked = sound.enabled;
      toggle.setAttribute("data-click-sound", "off");
      label.appendChild(toggle);
      label.appendChild(document.createTextNode(" " + st("click_sound")));
      top.appendChild(label);
      var pct = el("span", "set-vol-pct", sound.enabled && sound.volume ? sound.volume + "%" : t("comp_sound_muted"));
      top.appendChild(pct);
      row.appendChild(top);
      var control = el("div", "set-vol-row");
      var range = document.createElement("input");
      range.type = "range"; range.min = "0"; range.max = "100"; range.step = "1";
      range.id = "settings-click-volume";
      range.className = "set-range";
      range.value = String(sound.volume);
      range.setAttribute("aria-label", st("click_volume"));
      range.setAttribute("data-click-sound", "off");
      function update() {
        var volume = parseInt(range.value, 10) || 0;
        pct.textContent = toggle.checked && volume ? volume + "%" : t("comp_sound_muted");
        if (ui.clickSound) { ui.clickSound.configure({ enabled: toggle.checked, volume: volume }); }
      }
      toggle.addEventListener("change", function () {
        update();
        call("settings_set_click_enabled", toggle.checked);
      });
      range.addEventListener("input", update);
      range.addEventListener("change", function () {
        update();
        call("settings_set_click_volume", parseInt(range.value, 10) || 0);
      });
      control.appendChild(range);
      var test = ui.btn("set-btn", t("comp_sound_test"), function () {
        update();
        if (ui.clickSound) { ui.clickSound.play(); }
      }, { tag: "button" });
      test.setAttribute("data-click-sound", "off");
      control.appendChild(test);
      row.appendChild(control);
      row.appendChild(el("div", "set-hint", st("click_hint")));
      return row;
    }

    // -- Game (install path + language) ---------------------------------------
    function gameCard() {
      var game = s.game || {};
      var card = el("div", "set-card");

      // install path
      var pathField = el("div", "set-field");
      pathField.appendChild(el("div", "set-field-name", st("path_name")));

      var pathRow = el("div", "set-path-row");
      if (game.can_browse) {
        var box = el("div", "set-path", game.path || "-");
        box.title = game.path || "";
        pathRow.appendChild(box);
        pathRow.appendChild(ui.btn("set-btn", t("browse"),
          function () { call("settings_browse_game_path"); }, { tag: "button" }));
      } else {
        // No native folder dialog (e.g. dev/headless): a labelled text field + Set, validated
        // server-side against a real Bodycam install.
        var input = document.createElement("input");
        input.id = "settings-game-path";
        input.type = "text";
        input.className = "set-path-input";
        input.value = game.path || "";
        input.placeholder = st("path_placeholder");
        input.setAttribute("aria-label", st("path_name"));
        input.setAttribute("autocomplete", "off");
        input.setAttribute("spellcheck", "false");
        input.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { call("settings_set_game_path", input.value); }
        });
        pathRow.appendChild(input);
        pathRow.appendChild(ui.btn("set-btn", st("path_set"),
          function () { call("settings_set_game_path", input.value); }, { tag: "button" }));
      }
      if (game.locked) pathRow.querySelectorAll("button,input").forEach(function(control) { control.disabled = true; });
      pathField.appendChild(pathRow);

      if (game.error) {
        pathField.appendChild(el("div", "set-note bad", game.error));
      } else if (game.detected) {
        pathField.appendChild(el("div", "set-note good",
          game.auto_detected ? st("path_detected") : st("path_manual")));
      } else {
        pathField.appendChild(el("div", "set-note warn", st("path_manual")));
      }
      card.appendChild(pathField);

      // language
      var langField = el("div", "set-field bordered");
      var langTop = el("div", "set-field-row");
      var langLabelWrap = el("div", null);
      var lblId = "settings-language-label";
      var lbl = el("div", "set-field-name", st("lang_name"));
      lbl.id = lblId;
      langLabelWrap.appendChild(lbl);
      langTop.appendChild(langLabelWrap);

      var lang = s.language || {};
      var select = document.createElement("select");
      select.id = "settings-language";
      select.className = "set-select";
      select.setAttribute("aria-labelledby", lblId);
      (lang.options || []).forEach(function (o) {
        var opt = document.createElement("option");
        opt.value = o.code;
        opt.textContent = o.name;
        if (o.code === lang.current) { opt.selected = true; }
        select.appendChild(opt);
      });
      select.addEventListener("change", function () { call("set_language", select.value); });
      langTop.appendChild(select);
      langField.appendChild(langTop);
      card.appendChild(langField);

      return card;
    }

    // -- Account --------------------------------------------------------------
    function accountCard() {
      var acct = s.account || {};
      var card = el("div", "set-card set-acct");

      if (!acct.signed_in) {
        card.appendChild(el("div", "set-hint", t("comp_signin_title")));
        return card;
      }

      card.appendChild(ui.avatar({ text: initials(acct.persona), ariaLabel: acct.persona }));
      var mid = el("div", "set-acct-mid");
      mid.appendChild(el("div", "set-field-name", acct.persona));
      mid.appendChild(el("div", "set-hint", st("acct_meta", { id: acct.steam_id_masked })));
      card.appendChild(mid);

      var out = ui.btn("set-btn danger", t("comp_signout"),
        function () { call("settings_sign_out"); }, { tag: "button" });
      if (acct.locked) {
        out.disabled = true;
        out.title = t("comp_signout_locked");
        out.setAttribute("aria-label", t("comp_signout_locked"));
      }
      card.appendChild(out);
      var linking = el("div", "set-account-link-row");
      if (acct.linked) {
        linking.appendChild(el("div", "set-hint", st("link_connected", {email: acct.email || "Lights Out"})));
      } else if (acct.login_method !== "lightsout") {
        linking.appendChild(el("p", "set-hint", st((acct.link || {}).can_connect ? "link_hint" : "link_idle_required")));
        var connect = ui.btn("set-btn set-account-link", st("link_button"),
          function () { call("settings_account_link", "start"); }, {tag:"button"});
        connect.disabled = !((acct.link || {}).can_connect);
        linking.appendChild(connect);
      }
      if (linking.childNodes.length) card.appendChild(linking);
      return card;
    }

    function accountLinkModal() {
      var link = (s.account || {}).link || {}, stage = link.stage;
      var recovery = ["forgot_password","recovery_code","reset_password"].indexOf(stage) >= 0;
      var kids = [], form = el("form", "set-link-form");
      form.setAttribute("aria-busy", String(!!link.busy));
      var messages = {login:"link_login_intro",login_code:"link_email_code_intro",steam:"link_steam_intro",
        password:"link_password_intro",confirm:"link_confirm_intro",done:"link_success",uncertain:"link_uncertain"};
      var recoveryMessages = {forgot_password:"account_recovery_intro",recovery_code:"account_recovery_sent",reset_password:"account_recovery_verified"};
      kids.push(el("p", "set-hint", recovery ? t(recoveryMessages[stage]) : st(messages[stage] || "link_hint")));
      if (link.email && stage !== "done" && stage !== "uncertain") kids.push(el("p", "set-link-pair", st("link_pair", link)));
      var errorKeys = {progress_conflict:"link_progress_conflict",link_conflict:"link_owner_conflict",
        already_linked:"link_owner_conflict",active_match:"link_match",fresh_steam_required:"link_expired",
        invalid_action_code:"link_code_invalid",invalid_code:"link_code_invalid",account_changed:"link_restart",
        not_signed_in:"link_restart",invalid_credentials:"link_credentials",rate_limited:"link_rate",wrong_steam:"link_wrong_steam"};
      var recoveryErrors = {password_reset:"account_password_reset",recovery_limited:"account_recovery_limited",recovery_invalid:"account_recovery_invalid",
        recovery_email:"account_recovery_email",recovery_password:"account_recovery_password",reset_uncertain:"account_reset_uncertain",recovery_failed:"account_recovery_failed"};
      if (link.error) { var error = el("div", "set-note" + (link.error === "password_reset" ? "" : " bad"), recoveryErrors[link.error] ? t(recoveryErrors[link.error]) : st(errorKeys[link.error] || "link_unavailable")); error.setAttribute("role",link.error === "password_reset" ? "status" : "alert"); kids.push(error); }
      var confirmation = null;
      function field(name, label, type) {
        var row = el("label", "set-link-field", t(label)), input = document.createElement("input");
        input.id = "settings-link-" + (link.epoch || 0) + "-" + stage + "-" + name; input.name = name; input.type = type || "text";
        input.required = true; input.disabled = !!link.busy; input.maxLength = name === "code" ? 6 : 254;
        input.autocomplete = type === "password" ? (stage === "reset_password" ? "new-password" : "current-password") : name === "email" ? "email" : "one-time-code";
        if (name === "code") { input.inputMode="numeric";input.pattern="[0-9]{6}"; }
        if (type === "password") input.minLength = 6;
        input.autocapitalize="none"; input.spellcheck=false; row.appendChild(input);form.appendChild(row);
        input.oninput=function(){if(confirmation)confirmation.setCustomValidity("");};
        return input;
      }
      if (stage === "login" || stage === "forgot_password") field("email", "account_email", "email");
      if (stage === "login" || stage === "password" || stage === "reset_password") field("password", "account_password", "password");
      if (stage === "reset_password") confirmation=field("confirm_password", "account_confirm_password", "password");
      if (stage === "login_code" || stage === "confirm" || stage === "recovery_code") field("code", "account_code");
      if (["login","login_code","password","confirm","forgot_password","recovery_code","reset_password"].indexOf(stage) >= 0) {
        var submit = document.createElement("button"); submit.type="submit";submit.className="set-btn primary";
        submit.disabled=!!link.busy; submit.textContent=link.busy?t("account_working"):stage==="password"?st("link_send_confirmation"):stage==="confirm"?st("link_confirm_button"):t("account_continue");form.appendChild(submit);
        form.onsubmit=function(event){
          event.preventDefault();if(link.busy)return;
          var fields={};form.querySelectorAll("input").forEach(function(input){fields[input.name]=input.value;});
          if(confirmation&&fields.password!==fields.confirm_password){confirmation.setCustomValidity(t("account_password_mismatch"));confirmation.reportValidity();return;}
          delete fields.confirm_password;
          form.querySelectorAll('input[type="password"],input[name="code"]').forEach(function(input){input.value="";});
          var actions={forgot_password:"forgot-password",recovery_code:"forgot-password/verify",reset_password:"reset-password"};
          call("settings_account_link",actions[stage]||stage,fields);
        };
        kids.push(form);
        if(stage === "login" || stage === "password") {
          var forgot=ui.btn("set-btn settings-account-forgot",t("account_forgot"),function(){call("settings_account_link","recover");},{tag:"button"});
          forgot.disabled=!!link.busy;kids.push(forgot);
        }
        if(stage === "recovery_code") {
          var resend=ui.btn("set-btn",t("account_recovery_resend"),function(){call("settings_account_link","forgot-password");},{tag:"button"});
          resend.disabled=!!link.busy;kids.push(resend);
        }
      } else if (stage === "steam") {
        if (link.busy) kids.push(el("p","set-hint",st("link_steam_waiting")));
        var steam=ui.btn("set-btn primary",st("link_steam_button"),function(){call("settings_account_link", "steam");},{tag:"button"});steam.disabled=!!link.busy;kids.push(steam);
        if(link.busy)kids.push(ui.btn("set-btn",t("comp_signin_open_again"),function(){call("settings_account_link","open_steam");},{tag:"button"}));
      } else if ((stage === "done" || stage === "uncertain")) {
        kids.push(ui.btn("set-btn primary",st("link_signin"),function(){call("settings_account_link","signin");},{tag:"button"}));
      }
      function close(){ if(!(stage==="confirm"&&link.busy))call("settings_account_link","cancel"); }
      if(stage!=="done"&&stage!=="uncertain"){
        var cancel=ui.btn("set-btn",t("comp_cancel"),close,{tag:"button"});cancel.disabled=stage==="confirm"&&!!link.busy;kids.push(cancel);
        if(link.error){var restart=ui.btn("set-btn",st("link_start_over"),function(){call("settings_account_link","restart");},{tag:"button"});restart.disabled=!!link.busy;kids.push(restart);}
      }
      var overlay=ui.modal({title:recovery?t("account_reset_title"):st("link_title"),children:kids,onClose:close});
      overlay.querySelector(".ui-modal").classList.add("account-link-modal");
      return overlay;
    }

    // -- Uninstall ------------------------------------------------------------
    // Two steps on purpose: the card only OPENS the modal, and the modal is where the
    // consequences are spelled out and the irreversible tick lives. Nothing is deleted until
    // the danger button in there is pressed.

    // A reason code from the slice -> a sentence. `locked` is the session's own refusal and has
    // its own string; everything else is a uninst_err_* key. Unknown codes fall back to the
    // "not installed" sentence rather than rendering a raw key at the player.
    function uninstErr(code) {
      code = String(code || "");
      if (!code) { return ""; }
      if (code === "locked") { return st("uninst_locked"); }
      var key = "uninst_err_" + code;
      var text = st(key);
      return text === key ? st("uninst_err_not_installed") : text;
    }

    function uninstallCard() {
      var u = s.uninstall || {};
      var card = el("div", "set-card");
      var field = el("div", "set-field");

      var top = el("div", "set-field-row");
      top.appendChild(el("div", "set-field-name", st("uninst_name")));
      var open = ui.btn("set-btn danger", st("uninst_btn"),
        function () { call("settings_open_uninstall"); }, { tag: "button" });
      if (!u.available || u.locked) {
        open.disabled = true;
        // Say WHY on the control itself, not only in the note under it — the note is easy to
        // read as a description of the button rather than the reason it will not press.
        var why = u.locked ? st("uninst_locked") : uninstErr(u.reason || "not_installed");
        open.title = why;
        open.setAttribute("aria-label", st("uninst_name") + ": " + why);
      }
      top.appendChild(open);
      field.appendChild(top);

      field.appendChild(el("div", "set-hint", st("uninst_hint")));
      if (u.locked) {
        field.appendChild(el("div", "set-note bad", st("uninst_locked")));
      } else if (!u.available) {
        field.appendChild(el("div", "set-note warn", uninstErr(u.reason || "not_installed")));
      }
      card.appendChild(field);
      return card;
    }

    function uninstallModal() {
      var u = s.uninstall || {};
      var close = function () { if (!u.busy) { call("settings_close_uninstall"); } };

      var body = el("div", "set-uninst-body", st("uninst_body"));

      var files = el("div", "set-uninst-files");
      var list = u.game_files || [];
      if (list.length) {
        files.appendChild(el("div", "set-field-name", st("uninst_files_label")));
        var ul = el("div", "set-uninst-list");
        list.forEach(function (name) { ul.appendChild(el("div", "set-uninst-file", name)); });
        files.appendChild(ul);
      } else {
        files.appendChild(el("div", "set-hint", st("uninst_files_none")));
      }

      var keep = el("div", "set-hint", st("uninst_keep", { dir: u.state_dir || "" }));

      var wipeRow = el("label", "set-uninst-wipe");
      var box = document.createElement("input");
      box.type = "checkbox";
      box.id = "settings-uninstall-wipe";
      box.checked = !!wipeData;
      box.disabled = !!u.busy;
      box.addEventListener("change", function () { wipeData = !!box.checked; });
      wipeRow.appendChild(box);
      wipeRow.appendChild(el("span", null, st("uninst_wipe")));

      var foot = el("div", "set-uninst-foot");
      var cancel = ui.btn("set-btn", st("uninst_cancel"), close, { tag: "button" });
      cancel.disabled = !!u.busy;
      foot.appendChild(cancel);
      var go = ui.btn("set-btn danger", u.busy ? st("uninst_working") : st("uninst_confirm"),
        function () { call("settings_uninstall", !!wipeData); }, { tag: "button" });
      go.disabled = !!u.busy;
      foot.appendChild(go);

      var kids = [body, files, keep, wipeRow];
      if (u.error) { kids.push(el("div", "set-note bad", uninstErr(u.error))); }
      kids.push(foot);

      var overlay = ui.modal({
        title: st("uninst_title"),
        children: kids,
        onClose: close
      });
      var dialog = overlay.querySelector(".ui-modal");
      if (dialog) { dialog.setAttribute("tabindex", "-1"); setTimeout(function () { dialog.focus(); }, 0); }
      return overlay;
    }


  }
})();
