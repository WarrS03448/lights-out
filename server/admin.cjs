/**
 * The admin console, as a web page you sign into with Steam.
 *
 * Sam, 2026-09-15: "a web page where we sign in and see all that info". It lives on the same
 * Railway service as everything else - a second service would be a second deploy of the same code
 * reading the same database, and the thing protecting the data is the sign-in, not the hostname.
 *
 * WHY STEAM AND NOT A PASSWORD. The option on the table was a shared password in an environment
 * variable. One string, guarding every player's report history: it cannot be rotated without
 * telling everyone who has it, it cannot be revoked for one person, and it leaves no trace of WHO
 * looked at what. Steam sign-in has none of those problems and needs no new secret, because the
 * identity is the one the hub already trusts everywhere else - the SteamID64 that keys reports,
 * ratings, friends and bans.
 *
 * THE BROWSER FLOW IS NOT THE HUB'S FLOW. The hub is a desktop program that cannot receive a
 * redirect, so it uses a link-code handshake (auth.cjs). A browser can receive a redirect, so this
 * is the ordinary OpenID round trip with a session cookie at the end:
 *
 *   GET /admin            no cookie -> a page with one button
 *   GET /admin/login      -> Steam
 *   GET /admin/return     verify WITH STEAM, check the allowlist, set the cookie
 *   GET /admin            the console
 *   GET /admin/logout     drop the cookie and the stored session
 *
 * THE PLAYER DIRECTORY (Sam, 2026-09-16) hangs off the same session:
 *
 *   GET  /admin/players       the page - a search bar, a table, and a column picker
 *   GET  /admin/players/data  the rows, filtered and sorted BY THE SERVER
 *   GET  /admin/prefs         this moderator's saved column sets
 *   POST /admin/prefs         replace them
 *   POST /admin/action        ban, unban, reset rank, set rank, set MMR - one account at a time
 *
 * THE ACTIONS ARE NOT IMPLEMENTED HERE. Every one of them is a live.cjs call that already exists
 * or belongs beside the ones that do (banAccount, resetRank, setRank, setElo), so this route is
 * a switch and nothing else: the rules about what may be set, and the logging of who set it, are
 * the service's business, and a console that enforced its own copy of them would be a second set
 * of rules to keep in step.
 *
 * THE ROWS ARE NOT FILTERED IN THE BROWSER. A page that narrows what it was sent can only ever
 * search what it was sent, which is the bug where a moderator types a name, sees nothing, and
 * concludes the player does not exist. Columns are the other way round - every row carries every
 * field - so turning one on is a redraw and costs no request.
 *
 * THE PRESETS ARE STORED PER ADMIN, SERVER-SIDE, and not in localStorage: a moderator who opens
 * the console from a different machine is the same moderator, and a column set they spent five
 * minutes arranging should not be a property of a browser profile.
 *
 * NO CSRF TOKEN, FOR THE COOKIE'S REASON. It is SameSite=Lax, so it does not ride along on a
 * cross-site POST; the prefs route additionally refuses anything that is not application/json,
 * which a cross-site form cannot send without a preflight this origin never answers.
 *
 * THE ASSERTION IS NEVER TRUSTED AS IT ARRIVES. Every parameter goes back to Steam with
 * openid.mode=check_authentication (auth.cjs verifyWithSteam, reused rather than reimplemented -
 * this is the one step that makes the whole thing safe, and two copies of it is one too many).
 *
 * NON-ADMINS ARE TOLD SO, and no session is created. A signed-in stranger sees a refusal, not a
 * blank page that might be a bug and not a login loop.
 */
const crypto = require('crypto');
// The directory's column table, read rather than copied: the console renders these columns and
// validates a saved preset against them, and a second copy of the list is a list that drifts.
const { PLAYER_COLUMNS } = require('./live.cjs');
// The rank ladder, for the one prompt that has to name it: "set this account to rank 5" is not a
// question anybody can answer without being told what rank 5 is called. ladder.cjs exists so that
// nothing keeps its own copy of these names, and that includes this page.
const ladder = require('./ladder.cjs');

const STEAM_OPENID = 'https://steamcommunity.com/openid/login';
const CLAIMED_ID_RE = /^https?:\/\/steamcommunity\.com\/openid\/id\/(\d{17})$/;
const COOKIE = 'hubadmin';
// A working day. Long enough not to be re-signing in all afternoon, short enough that a session
// left on a machine somewhere does not outlive the reason it was created.
const SESSION_TTL_SECONDS = Math.max(300, Number(process.env.COMP_ADMIN_SESSION_SECONDS) || 8 * 3600);

// How many column sets one moderator may keep, and how long a name may be. Neither number is
// interesting; both exist because this is a free-text field written into a store.
const MAX_PRESETS = 24;
const PRESET_NAME_MAX = 48;

/**
 * THE COLUMN SETS A NEW MODERATOR STARTS WITH.
 *
 * Three, because the directory answers three different questions and no single arrangement of
 * columns answers more than one of them: who is this, should I act on them, and how do they
 * actually play. They are ordinary presets - renameable, editable, deletable - and not a special
 * case the code has to keep working around.
 */
function defaultPresets() {
  return [
    { id: 'overview', name: 'Overview', sort: 'last_seen', dir: 'desc',
      columns: ['persona', 'player_id', 'status', 'rank_name', 'matches', 'win_rate', 'kd',
                'seconds', 'last_seen', 'actions'] },
    { id: 'moderation', name: 'Moderation', sort: 'reports', dir: 'desc',
      columns: ['persona', 'player_id', 'reports', 'reporters', 'reports_made', 'team_kills',
                'abandons', 'no_shows', 'banned', 'last_seen', 'actions'] },
    { id: 'performance', name: 'Performance', sort: 'progress', dir: 'desc',
      columns: ['persona', 'rank_name', 'rr', 'mmr', 'played', 'kills', 'deaths', 'kd', 'kpr',
                'clutches', 'rounds', 'win_rate'] },
  ];
}

/**
 * What came back off the wire, made safe to store and to render.
 *
 * EVERY COLUMN NAME IS CHECKED against the one table live.cjs owns. A preset is written by a
 * signed-in admin, so this is not a trust boundary in the usual sense - it is a DRIFT boundary:
 * a column that is renamed or removed one day would otherwise leave every saved preset quietly
 * asking for a field that no longer exists, and the page would draw an empty column rather than
 * say so.
 */
function cleanPrefs(raw) {
  const known = new Set(PLAYER_COLUMNS.map((c) => c.key));
  const presets = [];
  const rows = (raw && Array.isArray(raw.presets)) ? raw.presets.slice(0, MAX_PRESETS) : [];
  for (const row of rows) {
    const id = String((row && row.id) || '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 24);
    const name = String((row && row.name) || '').trim().slice(0, PRESET_NAME_MAX);
    const columns = [...new Set(((row && Array.isArray(row.columns)) ? row.columns : [])
      .map(k => String(k) === 'steam_id' ? 'player_id' : String(k)))].filter((k) => known.has(k));
    // A preset with no columns would draw a blank page that looks like a broken console.
    if (!id || !name || !columns.length) continue;
    if (presets.some((p) => p.id === id)) continue;
    presets.push({
      id,
      name,
      columns,
      sort: known.has(String(row.sort)) ? String(row.sort) : 'last_seen',
      dir: row.dir === 'asc' ? 'asc' : 'desc',
    });
  }
  if (!presets.length) presets.push(...defaultPresets());
  const wanted = String((raw && raw.active) || '');
  const active = presets.some((p) => p.id === wanted) ? wanted : presets[0].id;
  return { presets, active };
}

function baseUrlOf(req) {
  const proto = String(req.headers['x-forwarded-proto'] || 'https').split(',')[0].trim();
  const host = String(req.headers['x-forwarded-host'] || req.headers.host || '').split(',')[0].trim();
  return `${proto}://${host}`;
}

function cookiesOf(req) {
  const out = {};
  for (const part of String(req.headers.cookie || '').split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

const esc = (v) => String(v === null || v === undefined ? '' : v)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

const PLAYERS_JS = `
/* The player directory, in the browser.
 *
 * It does three things: ask the server for rows, draw them, and remember which columns the
 * person looking wants. The FILTERING AND SORTING ARE NOT HERE - a page that narrows what it
 * was sent can only search what it was sent, and the whole point of the search bar is to find
 * a player who is not on the screen. Columns are the opposite: every row already carries every
 * field, so turning one on is a redraw and never a request.
 */
(function () {
  var boot = JSON.parse(document.getElementById('boot').textContent);
  var COLUMNS = boot.columns;
  var byKey = {};
  COLUMNS.forEach(function (c) { byKey[c.key] = c; });

  var state = {
    q: '',
    sort: 'last_seen',
    dir: 'desc',
    columns: [],
    presets: boot.prefs.presets || [],
    active: boot.prefs.active || '',
    dirty: false,
    rows: [],
    meta: null,
  };

  var el = {
    search: document.getElementById('q'),
    tbody: document.getElementById('rows'),
    head: document.getElementById('head'),
    count: document.getElementById('count'),
    picker: document.getElementById('picker'),
    boxes: document.getElementById('boxes'),
    presets: document.getElementById('presets'),
    save: document.getElementById('save'),
    note: document.getElementById('note'),
  };

  // ---------------------------------------------------------------- drawing a cell
  function pad(n) { return (n < 10 ? '0' : '') + n; }

  function ago(ms) {
    if (!ms) return '';
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    if (s < 86400) return Math.floor(s / 3600) + 'h ago';
    if (s < 86400 * 30) return Math.floor(s / 86400) + 'd ago';
    var d = new Date(ms);
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function duration(seconds) {
    if (!seconds) return '';
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (!h) return m + 'm';
    return h + 'h ' + pad(m) + 'm';
  }

  function text(value) {
    var span = document.createElement('span');
    span.textContent = value;
    return span;
  }

  function cell(row, column) {
    var td = document.createElement('td');
    var v = row[column.key];
    td.className = 'c-' + column.type;
    if (column.type === 'name') {
      // Straight to their Steam profile: the next thing a moderator does with a name is look at
      // the account behind it. rel=noreferrer, because this page is a signed-in console.
      var a = document.createElement('a');
      if (/^[0-9]{17}$/.test(row.game_steam_id || '')) a.href = 'https://steamcommunity.com/profiles/' + row.game_steam_id;
      a.target = '_blank';
      a.rel = 'noreferrer noopener';
      a.textContent = v || row.player_id;
      if (!v) a.className = 'unnamed';
      td.appendChild(a);
      if (row.admin) td.appendChild(tag('admin', 'strong'));
    } else if (column.type === 'status') {
      td.appendChild(tag(v, v === 'offline' ? '' : 'ok'));
    } else if (column.type === 'rank') {
      td.textContent = row.placing ? 'placing'
        : (v ? v + (row.division ? ' ' + row.division : '') : '');
      if (row.placing) td.className += ' muted';
    } else if (column.type === 'ban') {
      if (!v) td.textContent = '';
      else {
        td.appendChild(tag(row.ban_until ? 'until ' + new Date(row.ban_until).toISOString().slice(0, 10)
                                         : 'permanent', 'warn'));
        if (row.ban_reason) td.appendChild(text(' ' + row.ban_reason));
      }
    } else if (column.type === 'date') {
      td.textContent = ago(v);
      if (v) td.title = new Date(v).toLocaleString();
    } else if (column.type === 'time') {
      td.textContent = duration(v);
    } else if (column.type === 'pct') {
      td.textContent = row.matches ? v + '%' : '';
    } else if (column.type === 'num' || column.type === 'ratio') {
      td.textContent = (v === null || v === undefined) ? '' : String(v);
      if (!v) td.className += ' muted';
    } else if (column.type === 'actions') {
      actions(td, row);
    } else {
      td.textContent = (v === null || v === undefined) ? '' : String(v);
    }
    return td;
  }

  function tag(label, kind) {
    var span = document.createElement('span');
    span.className = 'tag' + (kind ? ' ' + kind : '');
    span.textContent = label;
    return span;
  }

  function button(label, title, danger, onclick) {
    var b = document.createElement('button');
    b.type = 'button';
    b.className = 'mini' + (danger ? ' danger' : '');
    b.textContent = label;
    b.title = title;
    b.addEventListener('click', onclick);
    return b;
  }

  /* One account, one decision. Every one of these is a POST the SERVER rules on - the page asks
   * the question and reports the answer, and nothing here decides what is allowed. */
  function act(payload, done) {
    return fetch('/admin/action', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (!data || !data.ok) throw new Error((data && data.error) || 'refused');
      say(done);
      load();
    }).catch(function (err) {
      say('Refused: ' + err.message, true);
    });
  }

  /**
   * ASK, AND MEAN IT. A dialog this page draws rather than window.prompt/confirm.
   *
   * Not decoration. The native ones are suppressed outright in some embedded browsers - measured
   * here, 2026-09-16: the button was pressed, no dialog appeared, and the handler took the
   * dismissal as a cancel. A ban button that silently does nothing in somebody's browser is worse
   * than no button, and these are the irreversible ones.
   *
   * Resolves with the typed value, '' for a plain confirmation, or null for a cancel - so every
   * caller below reads the same way whether it asked a question or only asked permission.
   */
  function ask(opts) {
    return new Promise(function (resolve) {
      var back = document.getElementById('modal');
      var input = document.getElementById('minput');
      var ok = document.getElementById('mok');
      var no = document.getElementById('mno');
      document.getElementById('mtitle').textContent = opts.title;
      document.getElementById('mbody').textContent = opts.body || '';
      input.hidden = !opts.field;
      input.value = opts.value === undefined ? '' : String(opts.value);
      input.placeholder = opts.field || '';
      ok.textContent = opts.confirm || 'Confirm';
      ok.className = 'btn' + (opts.danger ? ' danger' : '');
      back.hidden = false;

      function done(value) {
        back.hidden = true;
        ok.removeEventListener('click', yes);
        no.removeEventListener('click', cancel);
        back.removeEventListener('mousedown', backdrop);
        document.removeEventListener('keydown', key);
        resolve(value);
      }
      function yes() { done(opts.field ? input.value.trim() : ''); }
      function cancel() { done(null); }
      function backdrop(e) { if (e.target === back) cancel(); }
      function key(e) {
        if (e.key === 'Escape') cancel();
        // Enter commits a question that has a box to type in. A bare confirmation does NOT take
        // Enter: the last thing somebody pressed was a button, and a stray return should not ban
        // an account.
        else if (e.key === 'Enter' && opts.field) yes();
      }
      ok.addEventListener('click', yes);
      no.addEventListener('click', cancel);
      back.addEventListener('mousedown', backdrop);
      document.addEventListener('keydown', key);
      if (opts.field) { input.focus(); input.select(); } else ok.focus();
    });
  }

  function rankHelp() {
    var names = boot.ladder.names.map(function (name, i) { return (i + 1) + ' ' + name; });
    return names.join('   ') + '\\nDivisions 1 to ' + boot.ladder.divisions
      + '.\\n\\nOr "r" and an RR total to set it outright, e.g. "r1450".';
  }

  function actions(td, row) {
    var who = row.persona || row.player_id;
    if (row.admin) {
      // An admin cannot be banned (live.cjs refuses it), and a console that offers a button it
      // knows will be refused is a console nobody trusts the rest of.
      td.appendChild(text('\\u2014'));
      td.className += ' muted';
      return;
    }
    if (row.banned) {
      td.appendChild(button('Unban', 'Let this account back into competitive', false, function () {
        ask({ title: 'Unban ' + who,
              body: 'They can sign into competitive again straight away.',
              confirm: 'Unban' }).then(function (answer) {
          if (answer === null) return;
          act({ action: 'unban', steam_id: row.player_id }, who + ' is unbanned.');
        });
      }));
    } else {
      td.appendChild(button('Ban', 'Keep this Steam account out of competitive', true, function () {
        ask({ title: 'Ban ' + who,
              body: 'How many days? 0 is permanent.\\n\\nThey are dropped from the queue and any '
                + 'match they are in, and the ban is tied to the Steam account.',
              field: 'days', value: '7', confirm: 'Ban', danger: true }).then(function (days) {
          if (days === null) return;
          if (!/^\\d{1,4}$/.test(days)) { say('That is not a number of days.', true); return; }
          return ask({ title: 'Why?', body: 'Shown beside them in the ban list.',
                       field: 'reason', value: 'admin console', confirm: 'Ban', danger: true })
            .then(function (why) {
              if (why === null) return;
              act({ action: 'ban', steam_id: row.player_id, days: Number(days), reason: why },
                  who + (Number(days) ? ' is banned for ' + days + ' days.'
                                      : ' is banned permanently.'));
            });
        });
      }));
    }
    td.appendChild(button('Rank', 'Set the rank the player sees', false, function () {
      ask({ title: 'Set the rank of ' + who, body: rankHelp(),
            field: 'rank and division, e.g. 5 2', value: '', confirm: 'Set rank' })
        .then(function (answer) {
          if (answer === null) return;
          var flat = /^r\\s*(\\d{1,6})$/i.exec(answer);
          if (flat) {
            act({ action: 'rank', steam_id: row.player_id, progress: Number(flat[1]) },
                who + ' set to ' + flat[1] + ' RR.');
            return;
          }
          var parts = answer.split(/[^0-9]+/).filter(Boolean);
          if (!parts.length) { say('That is not a rank.', true); return; }
          act({ action: 'rank', steam_id: row.player_id,
                rank: Number(parts[0]), division: Number(parts[1] || 1) },
              who + ' set to ' + (boot.ladder.names[Number(parts[0]) - 1] || 'rank ' + parts[0])
                + ' ' + (parts[1] || 1) + '.');
        });
    }));
    td.appendChild(button('Elo', 'Set the hidden MMR the matchmaker reads', false, function () {
      ask({ title: 'Set the MMR of ' + who,
            body: 'The hidden number the matchmaker reads. Players never see it, and this does '
              + 'not move their rank.\\n\\nIts deviation is reset with it, so the next few matches '
              + 'are allowed to correct the figure.',
            field: 'mmr', value: row.mmr || '', confirm: 'Set MMR' }).then(function (answer) {
        if (answer === null) return;
        if (!/^\\d{1,5}$/.test(answer)) { say('That is not a rating.', true); return; }
        act({ action: 'elo', steam_id: row.player_id, rating: Number(answer) },
            who + ' set to ' + answer + ' MMR.');
      });
    }));
    td.appendChild(button('Reset', 'Back to placements', true, function () {
      ask({ title: 'Reset the rank of ' + who,
            body: 'They go back to placements with no RR and leave the leaderboard.\\n\\nTheir '
              + 'matches, kills, hours and reports are not touched.',
            confirm: 'Reset rank', danger: true }).then(function (answer) {
        if (answer === null) return;
        act({ action: 'reset', steam_id: row.player_id }, who + ' is back on placements.');
      });
    }));
  }

  // ---------------------------------------------------------------- drawing the table
  function draw() {
    var shown = state.columns.filter(function (k) { return byKey[k]; });
    el.head.innerHTML = '';
    var tr = document.createElement('tr');
    shown.forEach(function (key) {
      var column = byKey[key];
      var th = document.createElement('th');
      th.className = 'c-' + column.type + (state.sort === key ? ' sorted' : '');
      th.textContent = column.label + (state.sort === key ? (state.dir === 'asc' ? ' \\u2191' : ' \\u2193') : '');
      if (column.noSort) {
        // Actions are not a field, so there is nothing to order them by, and a header that looks
        // clickable and does nothing is worse than one that plainly is not.
        th.style.cursor = 'default';
        tr.appendChild(th);
        return;
      }
      th.title = 'Sort by ' + column.label;
      th.addEventListener('click', function () {
        // A second click on the same column turns it around; a new column starts on the order
        // that answers the question being asked - biggest first for a figure, A-Z for a name.
        if (state.sort === key) state.dir = state.dir === 'asc' ? 'desc' : 'asc';
        else { state.sort = key; state.dir = column.text ? 'asc' : 'desc'; }
        markDirty();
        load();
      });
      tr.appendChild(th);
    });
    el.head.appendChild(tr);

    el.tbody.innerHTML = '';
    state.rows.forEach(function (row) {
      var line = document.createElement('tr');
      if (row.banned) line.className = 'banned';
      shown.forEach(function (key) { line.appendChild(cell(row, byKey[key])); });
      el.tbody.appendChild(line);
    });
    if (!state.rows.length) {
      var empty = document.createElement('tr');
      var td = document.createElement('td');
      td.colSpan = Math.max(1, shown.length);
      td.className = 'empty';
      td.textContent = state.meta && state.meta.total
        ? 'No player matches that.'
        : 'Nobody has signed in yet.';
      empty.appendChild(td);
      el.tbody.appendChild(empty);
    }
    var m = state.meta || {};
    var parts = [];
    parts.push((m.found || 0) + ' of ' + (m.total || 0) + ' accounts');
    if ((m.shown || 0) < (m.found || 0)) parts.push('showing the first ' + m.shown);
    if (m.persisted === false) parts.push('memory only \\u2014 no store is configured');
    el.count.textContent = parts.join(' \\u00b7 ');
  }

  // ---------------------------------------------------------------- the server
  var pending = null;
  function load() {
    var url = '/admin/players/data?q=' + encodeURIComponent(state.q)
      + '&sort=' + encodeURIComponent(state.sort)
      + '&dir=' + encodeURIComponent(state.dir);
    if (pending) pending.abort();
    pending = new AbortController();
    var mine = pending;
    fetch(url, { signal: mine.signal, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.ok) throw new Error(data && data.error ? data.error : 'refused');
        state.rows = data.rows;
        state.meta = data;
        draw();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return;
        el.count.textContent = 'Could not read the directory: ' + err.message;
      });
  }

  var typing = null;
  el.search.addEventListener('input', function () {
    state.q = el.search.value;
    // Debounced, because a search is a round trip and a moderator types a whole name.
    if (typing) clearTimeout(typing);
    typing = setTimeout(load, 220);
  });
  el.search.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { el.search.value = ''; state.q = ''; load(); }
  });
  document.getElementById('refresh').addEventListener('click', load);

  // ---------------------------------------------------------------- columns
  function buildPicker() {
    el.boxes.innerHTML = '';
    COLUMNS.forEach(function (column) {
      var label = document.createElement('label');
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = state.columns.indexOf(column.key) >= 0;
      box.addEventListener('change', function () {
        if (box.checked) {
          if (state.columns.indexOf(column.key) < 0) {
            // Kept in the table's own order rather than in the order they were ticked, so a
            // column always appears where the person expects to find it.
            state.columns = COLUMNS.map(function (c) { return c.key; })
              .filter(function (k) {
                return k === column.key || state.columns.indexOf(k) >= 0;
              });
          }
        } else {
          state.columns = state.columns.filter(function (k) { return k !== column.key; });
          // Never nothing: a table with no columns is a blank page that looks broken.
          if (!state.columns.length) { state.columns = [column.key]; box.checked = true; }
        }
        markDirty();
        draw();
      });
      label.appendChild(box);
      label.appendChild(text(column.label));
      el.boxes.appendChild(label);
    });
  }

  document.getElementById('columns').addEventListener('click', function () {
    el.picker.hidden = !el.picker.hidden;
  });

  // ---------------------------------------------------------------- presets
  function activePreset() {
    for (var i = 0; i < state.presets.length; i += 1) {
      if (state.presets[i].id === state.active) return state.presets[i];
    }
    return null;
  }

  function apply(preset) {
    if (!preset) return;
    state.active = preset.id;
    state.columns = preset.columns.filter(function (k) { return byKey[k]; });
    state.sort = byKey[preset.sort] ? preset.sort : 'last_seen';
    state.dir = preset.dir === 'asc' ? 'asc' : 'desc';
    state.dirty = false;
    buildPicker();
    drawPresets();
    load();
  }

  function markDirty() {
    state.dirty = true;
    drawPresets();
  }

  function drawPresets() {
    el.presets.innerHTML = '';
    state.presets.forEach(function (preset) {
      var option = document.createElement('option');
      option.value = preset.id;
      option.textContent = preset.name;
      if (preset.id === state.active) option.selected = true;
      el.presets.appendChild(option);
    });
    el.save.disabled = !state.dirty;
    el.save.textContent = state.dirty ? 'Save *' : 'Saved';
  }

  el.presets.addEventListener('change', function () {
    for (var i = 0; i < state.presets.length; i += 1) {
      if (state.presets[i].id === el.presets.value) { apply(state.presets[i]); return; }
    }
  });

  function put(message) {
    return fetch('/admin/prefs', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ presets: state.presets, active: state.active }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (!data || !data.ok) throw new Error((data && data.error) || 'refused');
      state.presets = data.presets;
      state.active = data.active;
      state.dirty = false;
      drawPresets();
      say(message || 'Saved.');
    }).catch(function (err) {
      say('Not saved: ' + err.message, true);
    });
  }

  var saying = null;
  function say(message, bad) {
    el.note.textContent = message;
    el.note.className = 'note' + (bad ? ' warn' : '');
    if (saying) clearTimeout(saying);
    saying = setTimeout(function () { el.note.textContent = ''; }, 4000);
  }

  function snapshot(preset) {
    preset.columns = state.columns.slice();
    preset.sort = state.sort;
    preset.dir = state.dir;
    return preset;
  }

  el.save.addEventListener('click', function () {
    var preset = activePreset();
    if (!preset) return;
    snapshot(preset);
    put('Saved "' + preset.name + '".');
  });

  document.getElementById('newpreset').addEventListener('click', function () {
    var name = window.prompt('Name this column set', 'New set');
    if (!name) return;
    var preset = snapshot({ id: 'p' + Date.now().toString(36), name: name.slice(0, 48) });
    state.presets.push(preset);
    state.active = preset.id;
    drawPresets();
    put('Added "' + preset.name + '".');
  });

  document.getElementById('rename').addEventListener('click', function () {
    var preset = activePreset();
    if (!preset) return;
    var name = window.prompt('Rename this column set', preset.name);
    if (!name) return;
    preset.name = name.slice(0, 48);
    drawPresets();
    put('Renamed to "' + preset.name + '".');
  });

  document.getElementById('deletepreset').addEventListener('click', function () {
    var preset = activePreset();
    if (!preset) return;
    if (state.presets.length < 2) { say('The last column set cannot be deleted.', true); return; }
    if (!window.confirm('Delete the column set "' + preset.name + '"?')) return;
    state.presets = state.presets.filter(function (p) { return p.id !== preset.id; });
    state.active = state.presets[0].id;
    apply(state.presets[0]);
    put('Deleted "' + preset.name + '".');
  });

  // ---------------------------------------------------------------- go
  apply(activePreset() || state.presets[0]);
  el.search.focus();
}());
`;

function create({ upstashCmd, prefix = 'hub:', live, verifyWithSteam, analytics }) {
  const key = (token) => `${prefix}adminsession:${token}`;
  const store = {
    async get(k) {
      if (!upstashCmd) return null;
      try {
        const raw = await upstashCmd(['GET', k]);
        return raw ? (typeof raw === 'string' ? JSON.parse(raw) : raw) : null;
      } catch { return null; }
    },
    async set(k, v, ttl) {
      if (!upstashCmd) return;
      try { await upstashCmd(['SET', k, JSON.stringify(v), 'EX', String(ttl)]); } catch { /* ignore */ }
    },
    async del(k) {
      if (!upstashCmd) return;
      try { await upstashCmd(['DEL', k]); } catch { /* ignore */ }
    },
  };

  function send(res, status, html, headers = {}) {
    const body = Buffer.from(html, 'utf8');
    res.writeHead(status, {
      'content-type': 'text/html; charset=utf-8',
      'content-length': body.length,
      // A moderation console must never be cached by a proxy or left in the back button.
      'cache-control': 'no-store, private',
      'referrer-policy': 'no-referrer',
      'x-content-type-options': 'nosniff',
      'x-frame-options': 'DENY',
      ...headers,
    });
    res.end(body);
  }

  function json(res, status, payload) {
    const out = Buffer.from(JSON.stringify(payload), 'utf8');
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'content-length': out.length,
      'cache-control': 'no-store, private',
      'x-content-type-options': 'nosniff',
    });
    res.end(out);
  }

  /** The request body, capped. A console route is signed in, which is a reason to bound what it
   *  will read rather than a reason not to. */
  function body(req, cap) {
    return new Promise((resolve) => {
      let data = '';
      req.on('data', (c) => { data += c; if (data.length > cap) req.destroy(); });
      req.on('end', () => resolve(data));
      req.on('error', () => resolve(''));
    });
  }

  // ONE RECORD PER ADMIN, not one per browser: a moderator signing in from a different machine
  // is the same moderator, and a column set they spent five minutes arranging is not a property
  // of a browser profile. No TTL - unlike the session, these are meant to outlive the afternoon.
  const prefsKey = (steamId) => `${prefix}adminprefs:${steamId}`;

  async function readPrefs(steamId) {
    if (!upstashCmd) return cleanPrefs(null);
    try {
      const raw = await upstashCmd(['GET', prefsKey(steamId)]);
      return cleanPrefs(raw ? (typeof raw === 'string' ? JSON.parse(raw) : raw) : null);
    } catch {
      // An unreadable record is the default set, never a broken page: the worst case is a
      // moderator re-ticking some boxes.
      return cleanPrefs(null);
    }
  }

  /** Whether it was actually written down. Said out loud to the page, because "saved" that only
   *  lasted until the tab closed is the one failure a moderator must not be lied to about. */
  async function writePrefs(steamId, prefs) {
    if (!upstashCmd) return false;
    try {
      await upstashCmd(['SET', prefsKey(steamId), JSON.stringify(prefs)]);
      return true;
    } catch { return false; }
  }

  async function sessionOf(req) {
    const token = cookiesOf(req)[COOKIE];
    if (!token) return null;
    const rec = await store.get(key(token));
    if (!rec || !rec.steam_id) return null;
    // The allowlist is re-read on EVERY request, not baked into the session: taking someone off
    // COMP_ADMIN_STEAM_IDS has to lock them out now, not in eight hours.
    if (!live().isAdmin(rec.steam_id)) return null;
    return { ...rec, token };
  }

  // ---------------------------------------------------------------- pages
  function page(title, body, extraHead = '', wrapClass = '') {
    return `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>${esc(title)}</title>${extraHead}
<style>
  :root { --bg:#0e0e10; --panel:#141417; --line:#26262b; --text:#e8e8ea; --muted:#8a8a90;
          --accent:#d0323c; --gold:#d8a13a; --ok:#4caf72; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  a { color: var(--accent); }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 28px 22px 60px; }
  header { display:flex; align-items:center; gap:16px; flex-wrap:wrap; margin-bottom:18px; }
  h1 { font-size:16px; letter-spacing:.18em; text-transform:uppercase; margin:0; font-weight:600; }
  .who { margin-left:auto; color:var(--muted); font-size:12px; }
  .btn { display:inline-block; padding:8px 16px; border:1px solid var(--line); background:transparent;
         color:var(--text); font:inherit; font-size:12px; letter-spacing:.12em;
         text-transform:uppercase; cursor:pointer; text-decoration:none; }
  .btn:hover { border-color:var(--accent); color:var(--accent); }
  .stats { display:flex; gap:26px; flex-wrap:wrap; margin:0 0 16px; }
  .stat b { display:block; font-size:22px; font-weight:600; }
  .stat span { font-size:10px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); }
  .card { border:1px solid var(--line); background:var(--panel); padding:14px 16px; margin-bottom:12px; }
  .card h2 { font-size:11px; letter-spacing:.14em; text-transform:uppercase; margin:0 0 10px;
             color:var(--muted); font-weight:600; }
  .note { font-size:12px; color:var(--muted); }
  .note.warn { color:var(--gold); }
  .row { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
         padding:8px 0; border-top:1px solid var(--line); }
  .card .row:first-of-type { border-top:none; }
  .name { font-size:13px; }
  .id { font-family:ui-monospace,Consolas,monospace; font-size:11px; color:var(--muted); }
  .tag { font-size:10px; letter-spacing:.06em; text-transform:uppercase; padding:2px 6px;
         border:1px solid var(--line); color:var(--muted); }
  .tag.strong { color:var(--text); border-color:var(--text); }
  .tag.warn { color:var(--gold); border-color:var(--gold); }
  .acts { margin-left:auto; display:flex; gap:6px; }
  .strip { display:flex; gap:2px; flex-wrap:wrap; margin:6px 0; }
  .rd { width:18px; height:18px; display:grid; place-items:center; font-size:9px;
        background:var(--line); }
  .rd.t1 { background:var(--accent); color:#fff; }
  .rd.t2 { background:var(--muted); color:#111; }
  .players { display:flex; gap:8px; flex-wrap:wrap; font-size:11px; color:var(--muted); }
  .empty { color:var(--muted); font-size:12px; }
  /* A bug report is prose somebody typed, so it keeps its own line breaks and wraps rather than
     running off the card - and it breaks mid-word, because a pasted path or stack frame has no
     spaces in it to break at. */
  .bug { display:block; padding:10px 0; border-top:1px solid var(--line); }
  .card .bug:first-of-type { border-top:none; }
  .bug-who { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
  .bug-text { white-space:pre-wrap; overflow-wrap:anywhere; font-size:13px; margin:0; }
  .signin { text-align:center; padding:80px 20px; }
  .signin p { color:var(--muted); max-width:420px; margin:12px auto 22px; }
  .wrap.wide { max-width: none; }
  /* the player directory */
  .nav { display:flex; gap:4px; }
  .nav a { color:var(--muted); text-decoration:none; font-size:11px; letter-spacing:.14em;
           text-transform:uppercase; padding:6px 10px; border:1px solid transparent; }
  .nav a:hover { color:var(--text); }
  .nav a.on { color:var(--text); border-color:var(--line); }
  .bar { position:sticky; top:0; z-index:5; background:var(--bg); padding:10px 0 12px;
         display:flex; gap:8px; align-items:center; flex-wrap:wrap;
         border-bottom:1px solid var(--line); }
  .bar input[type=search] { flex:1 1 320px; min-width:200px; padding:10px 12px;
         background:var(--panel); border:1px solid var(--line); color:var(--text); font:inherit; }
  .bar input[type=search]:focus { outline:none; border-color:var(--accent); }
  .bar select { background:var(--panel); border:1px solid var(--line); color:var(--text);
         font:inherit; font-size:12px; padding:8px; max-width:200px; }
  .btn[disabled] { opacity:.45; cursor:default; }
  .btn[disabled]:hover { border-color:var(--line); color:var(--text); }
  .picker { border:1px solid var(--line); background:var(--panel); padding:12px 14px;
            margin:12px 0; }
  .boxes { columns: 160px auto; column-gap: 18px; }
  .boxes label { display:block; font-size:12px; padding:3px 0; cursor:pointer;
                 break-inside:avoid; }
  .boxes input { margin-right:8px; }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  th { position:sticky; top:53px; z-index:4; background:var(--bg); text-align:left;
       font-weight:600; font-size:10px; letter-spacing:.1em; text-transform:uppercase;
       color:var(--muted); padding:9px 10px; border-bottom:1px solid var(--line);
       cursor:pointer; white-space:nowrap; user-select:none; }
  th:hover { color:var(--text); }
  th.sorted { color:var(--accent); }
  td { padding:7px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  tbody tr:hover td { background:#17171b; }
  tr.banned td { opacity:.6; }
  th.c-num, th.c-ratio, th.c-pct, td.c-num, td.c-ratio, td.c-pct { text-align:right; }
  td.c-id { font-family:ui-monospace,Consolas,monospace; color:var(--muted); }
  td.c-name a { color:var(--text); text-decoration:none; }
  td.c-name a:hover { color:var(--accent); text-decoration:underline; }
  td.c-name a.unnamed { font-family:ui-monospace,Consolas,monospace; color:var(--muted); }
  td.muted { color:var(--muted); }
  td .tag { margin-left:6px; }
  .tag.ok { color:var(--ok); border-color:var(--ok); }
  td.empty { text-align:center; padding:30px; color:var(--muted); }
  .count { color:var(--muted); font-size:11px; margin:12px 0 0; }
  td.c-actions { white-space:nowrap; }
  .mini { background:transparent; border:1px solid var(--line); color:var(--muted);
          font:inherit; font-size:10px; letter-spacing:.08em; text-transform:uppercase;
          padding:3px 7px; margin-right:4px; cursor:pointer; }
  .mini:hover { color:var(--text); border-color:var(--text); }
  .mini.danger:hover { color:var(--accent); border-color:var(--accent); }
  .boxes input, .bar input[type=search] { accent-color: var(--accent); }
  /* the ask-first dialog. display:grid beats the browser own rule for [hidden], so the hidden
     state has to be spelled out here or the sheet is always on screen. */
  .modal { position:fixed; inset:0; z-index:20; background:rgba(0,0,0,.74);
           display:grid; place-items:center; }
  .modal[hidden] { display:none; }
  .sheet { background:var(--panel); border:1px solid var(--line); padding:20px 22px;
           width:min(480px, calc(100vw - 40px)); }
  .sheet h2 { font-size:12px; letter-spacing:.14em; text-transform:uppercase; margin:0 0 10px;
              font-weight:600; }
  .sheet p { color:var(--muted); font-size:12px; white-space:pre-line; margin:0 0 14px; }
  .sheet input { width:100%; padding:9px 10px; background:var(--bg); border:1px solid var(--line);
                 color:var(--text); font:inherit; margin:0 0 14px; }
  .sheet input:focus { outline:none; border-color:var(--accent); }
  .sheet input[hidden] { display:none; }
  .sheetacts { display:flex; gap:8px; justify-content:flex-end; }
  .btn.danger { border-color:var(--accent); color:var(--accent); }
  .btn.danger:hover { background:var(--accent); color:#fff; }
</style></head><body><div class="wrap${wrapClass ? ' ' + wrapClass : ''}">${body}</div></body></html>`;
  }

  function signinPage(base, message = '') {
    return page('Hub admin', `<div class="signin">
      <h1>Lights Out admin</h1>
      <p>${message ? esc(message) : 'Sign in with the Steam account on the admin list.'}</p>
      <a class="btn" href="${esc(base)}/admin/login">Sign in with Steam</a>
    </div>`);
  }

  function consolePage(me, d) {
    const n = (v) => Number(v || 0);
    const stat = (v, label) => `<div class="stat"><b>${esc(v)}</b><span>${esc(label)}</span></div>`;
    // "1 reporters" reads as a bug in the page rather than a fact about the player.
    const plural = (v, one, many) => `${n(v)} ${n(v) === 1 ? one : many}`;
    // An offline player has no persona, so the name falls back to the id - and printing the id
    // twice on the same row looks like two different numbers at a glance.
    const named = (name, id) => (name && name !== id)
      ? `<span class="name">${esc(name)}</span><span class="id">${esc(id)}</span>`
      : `<span class="id">${esc(id)}</span>`;
    const banBtns = (id) => `<form method="post" action="/admin/ban" style="display:flex;gap:6px;margin:0">
        <input type="hidden" name="steam_id" value="${esc(id)}">
        <button class="btn" name="days" value="7">7 days</button>
        <button class="btn" name="days" value="0">Permanent</button></form>`;

    const reported = (d.reported || []).map((p) => `<div class="row">
      ${named(p.persona, (p.player_id || p.steam_id))}
      <span class="tag strong">${plural(p.reporters, 'reporter', 'reporters')}</span>
      <span class="tag">${plural(p.total, 'report', 'reports')}</span>
      ${Object.entries(p.by_reason || {}).map(([k, v]) => `<span class="tag">${esc(k)} ${n(v)}</span>`).join('')}
      <span class="acts">${banBtns((p.player_id || p.steam_id))}</span></div>
      ${(p.reports || []).filter(r => r.note).map(r => `<div class="bug">
        <div class="bug-who">Something else &middot; reported by ${esc(r.by)}
        &middot; ${esc(new Date(n(r.at)).toISOString())}</div>
        <p class="bug-text">${esc(r.note)}</p></div>`).join('')}`).join('')
      || '<div class="empty">Nobody has been reported.</div>';

    const matches = (d.matches || []).map((m) => `<div class="row" style="flex-direction:column;align-items:stretch">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span class="name">${esc(m.map || '-')}</span>
        <span class="tag">${esc(m.state)}</span>
        ${m.score ? `<span class="name">${n(m.score[1])} : ${n(m.score[2])}</span>` : ''}
        ${m.team_kills ? `<span class="tag warn">TK ${n(m.team_kills)}</span>` : ''}
        ${m.teams_agreed ? '' : '<span class="tag">teams ?</span>'}
      </div>
      ${(m.rounds || []).length ? `<div class="strip">${m.rounds.map((r, i) =>
        `<span class="rd t${n(r.won)}" title="R${i + 1} ${n(r[1])}:${n(r[2])}">${i + 1}</span>`).join('')}</div>` : ''}
      <div class="players">${(m.players || []).map((p) =>
        `<span>${esc(p.persona || (p.player_id || p.steam_id))}${p.team ? ' (' + n(p.team) + ')' : ''}</span>`).join('')}</div>
    </div>`).join('') || '<div class="empty">No matches running.</div>';

    const kills = (d.team_kills || []).map((k) => `<div class="row">
      <span class="id">${esc(k.killer)}</span><span class="tag">to</span>
      <span class="id">${esc(k.victim)}</span>
      ${(k.flags || []).map((f) => `<span class="tag warn">${esc(f)}</span>`).join('')}
      <span class="tag">r${n(k.round)} +${n(k.elapsed)}s</span>
      <span class="acts">${banBtns(k.killer)}</span></div>`).join('')
      || '<div class="empty">No flagged team kills.</div>';

    const banned = (d.banned || []).map((b) => `<div class="row">
      ${named(b.persona, (b.player_id || b.steam_id))}
      ${b.reason ? `<span class="tag">${esc(b.reason)}</span>` : ''}
      <span class="tag">by ${esc(b.by)}</span>
      <span class="tag">${b.until ? esc(new Date(b.until).toISOString().slice(0, 10)) : 'permanent'}</span>
      <span class="acts"><form method="post" action="/admin/unban" style="margin:0">
        <input type="hidden" name="steam_id" value="${esc((b.player_id || b.steam_id))}">
        <button class="btn">Unban</button></form></span></div>`).join('')
      || '<div class="empty">Nobody is banned.</div>';

    // BUG REPORTS, newest first (live.cjs submitBugReport). Sam asked for the reporter's steam
    // id AND their name, so both are on every row even when they are the same string - `named`
    // collapses that case rather than printing the id twice.
    const bugs = (d.bugs || []).map((b) => `<div class="bug">
      <div class="bug-who">
        ${named(b.persona, b.by)}
        <span class="tag">${esc(new Date(n(b.at)).toISOString().slice(0, 16).replace('T', ' '))}</span>
        ${b.hub ? `<span class="tag">hub ${esc(b.hub)}</span>` : ''}
        ${b.mode ? `<span class="tag">mode ${esc(b.mode)}</span>` : ''}
      </div>
      <p class="bug-text">${esc(b.text)}</p></div>`).join('')
      || '<div class="empty">Nobody has reported a bug.</div>';

    const enforcing = (d.notes || {}).enforcement === 'on';
    return page('Hub admin', `
      ${chrome(me, 'overview')}
      <div class="stats">
        ${stat(n(d.online), 'online')}
        ${stat(n((d.queue || {}).players), 'queued')}
        ${stat((d.matches || []).length, 'live matches')}
        ${stat((d.bugs || []).length, 'bug reports')}
        ${stat((d.banned || []).length, 'banned')}
      </div>
      <div class="note ${enforcing ? '' : 'warn'}">${enforcing
        ? 'Team-kill enforcement is ON.'
        : 'Team-kill enforcement is OFF: verdicts are recorded, nobody is punished.'}</div>
      <div class="note">Player reports include saved reports from before a server restart. Team kills shown are what this server process has seen since it started.</div>
      <div class="card"><h2>Bug reports</h2>${bugs}</div>
      <div class="card"><h2>Reported players</h2>${reported}</div>
      <div class="card"><h2>Live matches</h2>${matches}</div>
      <div class="card"><h2>Flagged team kills</h2>${kills}</div>
      <div class="card"><h2>Banned accounts</h2>${banned}</div>
      <p class="note">This page refreshes every 20 seconds.</p>`,
      // Live data, so it reloads itself. 20s rather than 2s: a moderator reading a list of
      // accusations should not have it reorder itself under the cursor.
      '<meta http-equiv="refresh" content="20">');
  }


  /** The header every console page shares, so the two of them cannot drift apart. */
  function chrome(me, here) {
    const tab = (href, key, label) =>
      `<a class="${here === key ? 'on' : ''}" href="${esc(href)}">${esc(label)}</a>`;
    return `<header>
      <h1>Lights Out admin</h1>
      <nav class="nav">${tab('/admin', 'overview', 'Overview')}${tab('/admin/players', 'players', 'Players')}${tab('/admin/analytics', 'analytics', 'Analytics')}</nav>
      <span class="who">${esc(me.persona || me.steam_id)} &middot;
        <a href="/admin/logout">Sign out</a></span>
    </header>`;
  }

  /**
   * THE PLAYER DIRECTORY PAGE.
   *
   * The shell only. Everything in it is drawn by the script below from JSON fetched after load,
   * for one reason worth stating: the table has thirty-odd columns and a moderator chooses which
   * of them they are looking at, and a server-rendered table would mean a round trip to tick a
   * box. The first render is the exception - the presets ride along in the page rather than
   * behind a second request, so the table draws itself once rather than twice.
   *
   * NO META REFRESH, unlike the overview. That page is a list of things happening now; this one
   * is a table somebody is reading, sorting and searching, and a page that reloads itself under
   * the cursor loses their place and their search. There is a Refresh button instead.
   */
  function playersPage(me, prefs) {
    // JSON inside a script tag: the only sequence that can end the element is '</script>', and
    // escaping '<' makes it unwritable. The values here are ours, not a player's, but this page
    // also carries names typed by strangers on the overview beside it - the rule is the rule.
    const boot = JSON.stringify({
      columns: PLAYER_COLUMNS,
      prefs,
      // What the rank prompt has to be able to say out loud.
      ladder: { tiered: ladder.TIERED, divisions: ladder.DIVISIONS, names: ladder.NAMES },
    })
      .replace(/</g, '\\u003c');
    return page('Players - hub admin', `
      ${chrome(me, 'players')}
      <div class="bar">
        <input id="q" type="search" autocomplete="off" spellcheck="false"
               placeholder="Search by Steam name or SteamID64">
        <select id="presets" title="Column sets"></select>
        <button class="btn" id="save" title="Save the columns and sort into this set">Saved</button>
        <button class="btn" id="newpreset" title="Save these columns as a new set">New</button>
        <button class="btn" id="rename">Rename</button>
        <button class="btn" id="deletepreset">Delete</button>
        <button class="btn" id="columns">Columns</button>
        <button class="btn" id="refresh">Refresh</button>
      </div>
      <div class="picker" id="picker" hidden><div class="boxes" id="boxes"></div></div>
      <p class="note" id="note"></p>
      <table><thead id="head"></thead><tbody id="rows"></tbody></table>
      <p class="count" id="count">Loading the directory...</p>
      <div class="modal" id="modal" hidden><div class="sheet">
        <h2 id="mtitle"></h2>
        <p id="mbody"></p>
        <input id="minput" type="text" autocomplete="off" spellcheck="false">
        <div class="sheetacts">
          <button class="btn" id="mno">Cancel</button>
          <button class="btn" id="mok">Confirm</button>
        </div>
      </div></div>
      <script type="application/json" id="boot">${boot}</script>
      <script>${PLAYERS_JS}</script>`, '', 'wide');
  }

  // ---------------------------------------------------------------- routes
  async function route(req, res, method, pathname, url) {
    if (!pathname.startsWith('/admin')) return false;
    const base = baseUrlOf(req);

    if (pathname === '/admin/login' && method === 'GET') {
      const params = new URLSearchParams({
        'openid.ns': 'http://specs.openid.net/auth/2.0',
        'openid.mode': 'checkid_setup',
        'openid.return_to': `${base}/admin/return`,
        'openid.realm': base,
        'openid.identity': 'http://specs.openid.net/auth/2.0/identifier_select',
        'openid.claimed_id': 'http://specs.openid.net/auth/2.0/identifier_select',
      });
      res.writeHead(302, { location: `${STEAM_OPENID}?${params}`, 'cache-control': 'no-store' });
      res.end();
      return true;
    }

    if (pathname === '/admin/return' && method === 'GET') {
      const claimed = String(url.searchParams.get('openid.claimed_id') || '');
      const m = CLAIMED_ID_RE.exec(claimed);
      if (!m) { send(res, 400, signinPage(base, 'Steam did not return a valid account id.')); return true; }
      // NEVER trust the assertion as it arrives: this is the step that makes it safe.
      const valid = await verifyWithSteam(url.searchParams);
      if (!valid) {
        send(res, 400, signinPage(base, 'Steam did not confirm that sign-in. Please try again.'));
        return true;
      }
      const steamId = m[1];
      if (!live().isAdmin(steamId)) {
        // Told plainly, and NO session created. A blank page here looks like a bug and invites
        // someone to keep trying.
        send(res, 403, signinPage(base, `${steamId} is not on the admin list.`));
        return true;
      }
      const token = crypto.randomBytes(32).toString('hex');
      await store.set(key(token), { steam_id: steamId, created: Date.now() }, SESSION_TTL_SECONDS);
      res.writeHead(302, {
        location: `${base}/admin`,
        'cache-control': 'no-store',
        // httpOnly: JS can never read it. SameSite=Lax: it does not ride along on a cross-site
        // POST, which is what makes the ban forms below safe without a separate CSRF token.
        'set-cookie': `${COOKIE}=${token}; Path=/admin; HttpOnly; Secure; SameSite=Lax; Max-Age=${SESSION_TTL_SECONDS}`,
      });
      res.end();
      return true;
    }

    if (pathname === '/admin/logout') {
      const s = await sessionOf(req);
      if (s) await store.del(key(s.token));
      res.writeHead(302, {
        location: `${base}/admin`,
        'set-cookie': `${COOKIE}=; Path=/admin; HttpOnly; Secure; SameSite=Lax; Max-Age=0`,
      });
      res.end();
      return true;
    }

    const me = await sessionOf(req);
    if (!me && pathname.startsWith('/admin/analytics/')) { json(res,401,{ok:false,error:'Sign in required.'}); return true; }
    if (!me) { send(res, 200, signinPage(base)); return true; }

    if (pathname === '/admin/analytics' || pathname.startsWith('/admin/analytics/')) {
      if(method!=='GET'){json(res,405,{ok:false,error:'Use GET.'});return true;}
      if(!analytics){json(res,503,{ok:false,error:'Analytics is not configured.'});return true;}
      const options=Object.fromEntries(url.searchParams);
      try {
        if(pathname==='/admin/analytics')send(res,200,require('./admin-analytics.cjs').page());
        else if(pathname.endsWith('/data'))json(res,200,await analytics.summary(options));
        else if(pathname.endsWith('/matches'))json(res,200,await analytics.query('matches',options));
        else if(pathname.endsWith('/events'))json(res,200,await analytics.query('events',options));
        else if(pathname.endsWith('/audits'))json(res,200,await analytics.query('audits',options));
        else if(pathname.endsWith('/health'))json(res,200,{ok:true,...await analytics.status()});
        else if(pathname.endsWith('/reliability'))json(res,200,await analytics.reliability(options));
        else if(pathname.endsWith('/match'))json(res,200,{ok:true,match:await analytics.detail(String(options.id||''))});
        else if(pathname.endsWith('/combat'))json(res,200,{ok:true,...await analytics.combat(String(options.id||''),options.offset)});
        else if(pathname.endsWith('/export')){
          const kind=['matches','audits'].includes(options.kind)?options.kind:'events';
          const data=await analytics.query(kind,{...options,limit:200});
          analytics.emit('admin.export',{action:kind,count:data.rows.length},{actor_id:me.steam_id,source:'admin'});
          await analytics.flush();
          if(options.format==='csv'){
            const columns=kind==='matches'?['id','at','map','version','match_size','outcome','score','coverage']:kind==='audits'?['id','at','action','actor_id','target_id','before','after']:['id','received_at','type','severity','source','actor_id','match_id','version','data'];
            const cell=require('./analytics.cjs').csvCell;
            const csv=[columns.map(cell).join(','),...data.rows.map(r=>columns.map(k=>cell(typeof r[k]==='object'?JSON.stringify(r[k]):r[k])).join(','))].join('\r\n');
            res.writeHead(200,{'content-type':'text/csv; charset=utf-8','content-disposition':`attachment; filename="analytics-${kind}.csv"`,'cache-control':'no-store, private','x-analytics-next':data.next===null?'':String(data.next)});res.end(csv);
          }else{res.setHeader('content-disposition',`attachment; filename="analytics-${kind}.json"`);json(res,200,data);}
        }else json(res,404,{ok:false,error:'Unknown analytics view.'});
      }catch(error){json(res,/Invalid|Date range/.test(error.message)?400:503,{ok:false,error:/Invalid|Date range/.test(error.message)?error.message:'Analytics storage is unavailable. Try again shortly.'});}
      return true;
    }

    if (pathname === '/admin/players' && method === 'GET') {
      send(res, 200, playersPage(me, await readPrefs(me.steam_id)));
      return true;
    }

    if (pathname === '/admin/players/data' && method === 'GET') {
      const data = await live().adminPlayers(me.steam_id, {
        q: url.searchParams.get('q') || '',
        sort: url.searchParams.get('sort') || '',
        dir: url.searchParams.get('dir') || '',
        limit: url.searchParams.get('limit') || '',
      });
      json(res, data.ok ? 200 : 403, data);
      return true;
    }

    if (pathname === '/admin/prefs' && method === 'GET') {
      json(res, 200, { ok: true, ...(await readPrefs(me.steam_id)) });
      return true;
    }

    if (pathname === '/admin/prefs' && method === 'POST') {
      // A cross-site form cannot set this content type without a preflight this origin never
      // answers, which is the second half of what SameSite=Lax already does for the cookie.
      if (!String(req.headers['content-type'] || '').includes('application/json')) {
        json(res, 415, { ok: false, error: 'send json' });
        return true;
      }
      const raw = await body(req, 32768);
      let parsed;
      try { parsed = JSON.parse(raw || '{}'); } catch { parsed = null; }
      if (!parsed) { json(res, 400, { ok: false, error: 'that is not json' }); return true; }
      const prefs = cleanPrefs(parsed);
      const saved = await writePrefs(me.steam_id, prefs);
      json(res, 200, { ok: true, ...prefs, persisted: saved });
      return true;
    }

    if (pathname === '/admin/action' && method === 'POST') {
      if (!String(req.headers['content-type'] || '').includes('application/json')) {
        json(res, 415, { ok: false, error: 'send json' });
        return true;
      }
      let ask;
      try { ask = JSON.parse(await body(req, 4096) || '{}'); } catch { ask = null; }
      if (!ask) { json(res, 400, { ok: false, error: 'that is not json' }); return true; }
      const what = String(ask.action || '');
      const on = { steam_id: String(ask.steam_id || '') };
      let out;
      if (what === 'ban') {
        out = await live().banAccount(me.steam_id, {
          ...on, days: Number(ask.days) || 0,
          reason: String(ask.reason || '').slice(0, 200) || 'admin console',
        });
      } else if (what === 'unban') {
        out = await live().unbanAccount(me.steam_id, on);
      } else if (what === 'reset') {
        out = await live().resetRank(me.steam_id, on);
      } else if (what === 'elo') {
        out = await live().setElo(me.steam_id, { ...on, rating: ask.rating });
      } else if (what === 'rank') {
        out = await live().setRank(me.steam_id, {
          ...on, rank: ask.rank, division: ask.division, progress: ask.progress,
        });
      } else {
        out = { ok: false, error: 'no such action' };
      }
      json(res, out.ok ? 200 : 400, out);
      analytics?.emit('admin.action',{action:what,target_id:on.steam_id,status:out.ok?200:400,mmr:Number.isFinite(Number(ask.rating))?Number(ask.rating):undefined,rr:Number.isFinite(Number(ask.progress))?Number(ask.progress):undefined},{actor_id:me.steam_id,source:'admin',severity:out.ok?'info':'warn'});
      if(analytics)await analytics.flush();
      return true;
    }

    if ((pathname === '/admin/ban' || pathname === '/admin/unban') && method === 'POST') {
      const body = await new Promise((resolve) => {
        let data = '';
        req.on('data', (c) => { data += c; if (data.length > 4096) req.destroy(); });
        req.on('end', () => resolve(data));
        req.on('error', () => resolve(''));
      });
      const form = new URLSearchParams(body);
      const target = String(form.get('steam_id') || '');
      const days = Number(form.get('days') || 0);
      if (pathname.endsWith('/unban')) await live().unbanAccount(me.steam_id, { steam_id: target });
      else await live().banAccount(me.steam_id, { steam_id: target, days, reason: 'admin console' });
      // Redirect after POST so a refresh does not re-submit the ban.
      res.writeHead(303, { location: `${base}/admin`, 'cache-control': 'no-store' });
      res.end();
      return true;
    }

    if (pathname === '/admin' || pathname === '/admin/') {
      const data = await live().adminOverview(me.steam_id, { limit: 100 });
      send(res, 200, consolePage(me, data.ok ? data : {}));
      return true;
    }
    return false;
  }

  return { route,
           _internals: { sessionOf, cookiesOf, baseUrlOf, COOKIE, SESSION_TTL_SECONDS,
                         readPrefs, writePrefs, playersPage, prefsKey } };
}

module.exports = { create, COOKIE, CLAIMED_ID_RE, cleanPrefs, defaultPresets,
                   MAX_PRESETS, PRESET_NAME_MAX };
