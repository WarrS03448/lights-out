/* Leaderboard: real standings, local player search, rank/status filters and column sorting.
 * Controls survive snapshot redraws; filtering does not change official positions.
 * Strings come from the leaderboard snapshot in the selected hub language. */
(function () {
  "use strict";

  // Also runnable without a browser for the ordering/filtering checks.
  if (typeof module !== "undefined" && module.exports) { module.exports = selectRows; return; }
  window.HubUI.registerScreen("leaderboard", { render: render, update: update });
  function accountOf(state) {
    var auth = state.auth || {};
    return auth.signed_in ? String(auth.player_id || auth.steam_id || "signed-in") : "";
  }
  function viewKey(state) {
    return JSON.stringify([accountOf(state), state.lang, (state.leaderboard || {}).mode || "BB5"]);
  }
  function update(root, state, ctx) {
    if (root._leaderboardKey !== viewKey(state) || !root._updateLeaderboard) { return false; }
    root._updateLeaderboard(state, ctx);
    return true;
  }

  // Keep the selected scope and controls across core snapshot redraws.
  var activeScope = "global";
  var controls = { query: "", tier: "", status: "", sort: "", direction: "none" };
  var scrollPosition = { top: 0, left: 0 };

  function selectRows(rows, options, ranks) {
    var query = (options.query || "").trim().toLocaleLowerCase();
    var selected = rows.filter(function (p) {
      if (query && String(p.name || "").toLocaleLowerCase().indexOf(query) === -1 &&
          String(p.steam_id || "").indexOf(query) === -1) { return false; }
      if (options.tier && p.rank_name !== options.tier) { return false; }
      if (options.status === "ranked" && !p.rank_name) { return false; }
      if (options.status === "placing" && p.rank_name) { return false; }
      return true;
    });
    if (!options.sort || options.direction === "none") { return selected; }
    var names = (ranks || {}).names || [];
    function value(p) {
      if (options.sort === "player") { return p.name || null; }
      if (options.sort === "tier") {
        if (!p.rank_name) { return null; }
        if (p.top) { return (names.length + 1) * ((ranks || {}).divisions || 3); }
        var index = names.indexOf(p.rank_name);
        return index < 0 ? null : index * ((ranks || {}).divisions || 3) + (p.division || 0);
      }
      var raw = p[options.sort === "winrate" ? "win_rate" : options.sort];
      if (raw === null || raw === undefined || raw === "" || raw === "-") { return null; }
      var number = Number(String(raw).replace(/%$/, ""));
      return isFinite(number) ? number : null;
    }
    return selected.sort(function (a, b) {
      var av = value(a), bv = value(b);
      if (av === null) { return bv === null ? 0 : 1; }
      if (bv === null) { return -1; }
      var diff = options.sort === "player"
        ? av.localeCompare(bv, undefined, { sensitivity: "base", numeric: true }) : av - bv;
      // A lower position number is a higher place on the leaderboard.
      if (options.sort === "rank") { diff = -diff; }
      return options.direction === "highest" ? -diff : diff;
    });
  }

  var COLS = ["col_rank", "col_player", "col_tier", "col_rr", "col_matches", "col_winrate"];

  // One fetch when the screen is first opened. Module scope, so it survives the DOM rebuild
  // core.js does on every state change but resets on reload.
  var fetchedAccount = null;
  var fetched = false;
  var friendsFetched = false;
  var fetchedMode = null;

  function render(root, state, ctx) {
    var ui = ctx.ui, el = ui.el;
    var account = accountOf(state);
    if (account !== fetchedAccount) {
      fetchedAccount = account; fetched = false; friendsFetched = false;
      activeScope = "global";
      controls = { query: "", tier: "", status: "", sort: "", direction: "none" };
      scrollPosition = { top: 0, left: 0 };
    }
    var lb = state.leaderboard || {};
    if (fetchedMode !== (lb.mode || "BB5")) {
      fetchedMode = lb.mode || "BB5"; fetched = false;
      controls = { query: "", tier: "", status: "", sort: "", direction: "none" };
      scrollPosition = { top:0, left:0 };
    }
    // The ladder the server sent with `hello`, which is what turns a row's rank name into the
    // index its badge is keyed by. It rides on the competitive slice because that is the screen
    // that owns it; every slice is in every snapshot, so it is here whichever view is showing.
    var ranks = ((state.comp || {}).ladder || {}).ranks || null;
    if (!fetched && ctx.call) { fetched = true; ctx.call("leaderboard_refresh"); }
    if (activeScope === "friends" && !friendsFetched && ctx.call) {
      friendsFetched = true; ctx.call("friends_refresh");
    }
    var strings = lb.strings || {};
    function lt(key) { return (strings && strings[key]) || key; }

    // The service sends your standing separately when it falls outside the loaded leaders.
    var sourceRows = (lb.rows || []).slice();
    if (lb.you && !sourceRows.some(function (p) {
      return p.is_you || (p.steam_id && p.steam_id === lb.you.steam_id);
    })) {
      sourceRows.push(Object.assign({}, lb.you, { is_you: true }));
    }

    var scopes = (lb.scopes && lb.scopes.length) ? lb.scopes : ["global", "friends"];
    if (scopes.indexOf(activeScope) === -1) { activeScope = scopes[0]; }

    var wrap = el("div", "leaderboard");
    wrap.setAttribute("aria-label", lt("title"));

    // -- header: heading + Global/Friends tabs ------------------------------
    var head = el("div", "lb-topbar");
    var heading = el("div", "lb-heading");
    heading.appendChild(el("h1", "lb-title", lt("title")));
    head.appendChild(heading);
    head.appendChild(ui.tabs([{id:"BB5",label:"5v5 Bodybomb"},{id:"BB1",label:"1v1 Bodybomb"}], lb.mode || "BB5", function (mode) {
      ctx.call("leaderboard_mode", mode);
    }));

    var tabItems = scopes.map(function (id) {
      return { id: id, label: lt(id === "friends" ? "tab_friends" : "tab_global") };
    });
    head.appendChild(ui.tabs(tabItems, activeScope, function (id) {
      if (id === activeScope) { return; }
      activeScope = id;
      scrollPosition = { top: 0, left: 0 };
      root.innerHTML = "";          // local re-render: the core owns the state push, not the tab
      render(root, state, ctx);
    }));
    wrap.appendChild(head);

    var toolbar = el("div", "lb-tools");
    var searchLabel = el("label", "lb-search-label", lt("search"));
    var search = el("input", "lb-control lb-search");
    search.id = "lb-player-search";
    search.type = "search";
    search.placeholder = lt("search_hint");
    search.value = controls.query;
    search.addEventListener("input", function () { controls.query = search.value; updateRows(); });
    searchLabel.appendChild(search);
    toolbar.appendChild(searchLabel);

    function select(id, value, items, change) {
      var node = el("select", "lb-control");
      node.id = id;
      items.forEach(function (item) {
        var option = el("option", "", item.label);
        option.value = item.value;
        node.appendChild(option);
      });
      node.value = value;
      node.addEventListener("change", function () { change(node.value); updateRows(); });
      return node;
    }
    function field(label, node) {
      var group = el("label", "lb-filter", label);
      group.appendChild(node);
      toolbar.appendChild(group);
    }
    var tierNames = ((ranks || {}).names || []).slice();
    if ((ranks || {}).top) { tierNames.push(ranks.top); }
    sourceRows.forEach(function (p) {
      if (p.rank_name && tierNames.indexOf(p.rank_name) === -1) { tierNames.push(p.rank_name); }
    });
    var tierSelect = select("lb-tier-filter", controls.tier,
      [{ value: "", label: lt("all_tiers") }].concat(tierNames.map(function (name) {
        return { value: name, label: name };
      })), function (value) { controls.tier = value; });
    field(lt("col_tier"), tierSelect);
    var statusSelect = select("lb-status-filter", controls.status, [
      { value: "", label: lt("all_players") }, { value: "ranked", label: lt("ranked") },
      { value: "placing", label: lt("placing") }
    ], function (value) { controls.status = value; });
    field(lt("status"), statusSelect);
    var reset = ui.btn("lb-reset", lt("reset"), function () {
      controls = { query: "", tier: "", status: "", sort: "", direction: "none" };
      search.value = ""; tierSelect.value = ""; statusSelect.value = "";
      sortSelects.forEach(function (node) { node.value = "none"; });
      updateRows();
    }, { tag: "button" });
    toolbar.appendChild(reset);
    wrap.appendChild(toolbar);
    var count = el("div", "lb-result-count");
    count.setAttribute("role", "status");
    wrap.appendChild(count);

    // Keep all six columns available in narrow windows, including their sort controls.
    var viewport = el("div", "lb-table-viewport");
    function rememberScroll() {
      if (viewport.isConnected) {
        scrollPosition = { top: viewport.scrollTop, left: viewport.scrollLeft };
      }
    }
    viewport.addEventListener("scroll", rememberScroll);
    var tableContent = el("div", "lb-table-content");
    viewport.appendChild(tableContent);
    wrap.appendChild(viewport);

    // -- column header (always shown so the layout reads as a ranked table) --
    var cols = el("div", "lb-cols");
    var sortSelects = [];
    COLS.forEach(function (k, i) {
      var column = k.replace("col_", "");
      var cell = el("label", "lb-cell col-" + column + (i === 0 ? " lb-first" : ""), lt(k));
      var sorting = select("lb-sort-" + column, controls.sort === column ? controls.direction : "none", [
        { value: "none", label: lt("sort_none") },
        { value: "highest", label: lt(column === "player" ? "sort_za" : "sort_highest") },
        { value: "lowest", label: lt(column === "player" ? "sort_az" : "sort_lowest") }
      ], function (direction) {
        controls.sort = direction === "none" ? "" : column;
        controls.direction = direction;
        sortSelects.forEach(function (node) { if (node !== sorting) { node.value = "none"; } });
      });
      sorting.setAttribute("aria-label", lt(k) + " · " + lt("sort"));
      sortSelects.push(sorting);
      cell.appendChild(sorting);
      cols.appendChild(cell);
    });
    tableContent.appendChild(cols);

    // -- body: real rows if available, else an honest empty state -----------
    var body = el("div", "lb-body");
    tableContent.appendChild(body);
    function updateRows() {
      body.innerHTML = "";
      var source = sourceRows;
      if (activeScope === "friends") {
        var friendIds = ((state.friends || {}).list || []).map(function (p) { return String(p.steam_id); });
        source = source.filter(function (p) { return p.is_you || friendIds.indexOf(String(p.steam_id)) !== -1; });
      }
      var rows = selectRows(source, controls, ranks);
      count.textContent = lb.available ? lt("results").replace("{shown}", rows.length).replace("{total}", source.length) : "";
      if (lb.available) {
        if (rows.length) { body.appendChild(rankedTable(ui, rows, lt, ranks)); }
        else { body.appendChild(ui.emptyState({ icon: "◆", message: lt("no_results") })); }
      } else { body.appendChild(emptyBoard(ui, lt)); }
    }
    updateRows();

    var footer = el("div", "lb-footer");
    footer.appendChild(ui.btn("lb-go-to-me", lt("go_to_me"), function () {
      var row = body.querySelector(".lb-row.you");
      if (!row) {
        controls.query = ""; controls.tier = ""; controls.status = "";
        search.value = ""; tierSelect.value = ""; statusSelect.value = "";
        updateRows();
        row = body.querySelector(".lb-row.you");
      }
      if (row) {
        row.focus({ preventScroll: true });
        row.scrollIntoView({ block: "center", inline: "start" });
        rememberScroll();
      }
    }, { disabled: !lb.available || !sourceRows.some(function (p) { return p.is_you; }) }));
    wrap.appendChild(footer);

    root.appendChild(wrap);
    root._leaderboardKey = viewKey(state);
    var dataKey = JSON.stringify([lb, (state.friends || {}).list, ranks]);
    root._updateLeaderboard = function (next, nextCtx) {
      var nextLb = next.leaderboard || {}, nextRanks = ((next.comp || {}).ladder || {}).ranks || null;
      var nextKey = JSON.stringify([nextLb, (next.friends || {}).list, nextRanks]);
      // Callbacks read the latest snapshot even when only unrelated state changed.
      state = next; ctx = nextCtx;
      if (nextKey === dataKey) { return; }
      dataKey = nextKey; lb = nextLb; ranks = nextRanks; strings = lb.strings || {};
      sourceRows = (lb.rows || []).slice();
      if (lb.you && !sourceRows.some(function (p) { return p.is_you ||
          (p.player_id && p.player_id === lb.you.player_id) || (p.steam_id && p.steam_id === lb.you.steam_id); })) {
        sourceRows.push(Object.assign({}, lb.you, { is_you: true }));
      }
      var names = ((ranks || {}).names || []).slice();
      if ((ranks || {}).top) { names.push(ranks.top); }
      sourceRows.forEach(function (p) { if (p.rank_name && names.indexOf(p.rank_name) === -1) { names.push(p.rank_name); } });
      var patchOptions = function () {
        if (document.activeElement === tierSelect) { return; }
        var wanted = [{ value: "", label: lt("all_tiers") }].concat(names.map(function (n) { return { value:n, label:n }; }));
        if (JSON.stringify(Array.from(tierSelect.options).map(function (o) { return [o.value,o.textContent]; })) !== JSON.stringify(wanted.map(function (o) { return [o.value,o.label]; }))) {
          tierSelect.replaceChildren.apply(tierSelect, wanted.map(function (o) { var n=el("option","",o.label);n.value=o.value;return n; }));
          tierSelect.value=controls.tier;
        }
      };
      tierSelect.onblur = patchOptions;
      patchOptions();
      var top = viewport.scrollTop, left = viewport.scrollLeft;
      updateRows();
      viewport.scrollTop = top; viewport.scrollLeft = left;
      var go = footer.querySelector("button");
      if (go) { go.disabled = !lb.available || !sourceRows.some(function (p) { return p.is_you; }); }
    };
    // Core rebuilds the screen for unrelated live updates; keep the row the player reached.
    viewport.scrollTop = scrollPosition.top;
    viewport.scrollLeft = scrollPosition.left;
  }

  // A stack of rank rows, wrapped in the shared table container.
  function rankedTable(ui, rows, lt, ranks) {
    var nodes = rows.map(function (p) { return rankRow(ui, p, lt, ranks); });
    return ui.table(nodes);
  }

  // One ranked row. Uses the shared avatar helper; the 6-column grid is styled in the CSS.
  function rankRow(ui, p, lt, ranks) {
    var el = ui.el;
    var youFlag = !!p.is_you;
    var row = el("div", "lb-row" + (youFlag ? " you" : ""));
    row.setAttribute("role", "row");
    if (youFlag) { row.tabIndex = -1; }

    row.appendChild(el("span", "lb-cell lb-rank lb-first", p.rank != null ? String(p.rank) : "-"));

    var player = el("span", "lb-cell lb-player");
    player.appendChild(ui.avatar({
      url: p.avatar,
      text: (p.level !== null && p.level !== undefined) ? p.level : ui.initials(p.name),
      ariaLabel: p.name || ""
    }));
    player.appendChild(el("span", "lb-name", p.name || "-"));
    if (youFlag) { player.appendChild(el("span", "lb-youtag", lt("you_label"))); }
    row.appendChild(player);

    // The tier cell carries the badge as well as the words. A ranked table is read down a
    // column, and 25 silhouettes let someone find where the board stops looking like them
    // without parsing a name on every row (docs/rank-art.md).
    var tier = el("span", "lb-cell lb-tier");
    var art = ui.rankBadge(p, ranks);
    if (art) { tier.appendChild(art); }
    tier.appendChild(el("span", "lb-tier-name", p.tier || "-"));
    row.appendChild(tier);
    row.appendChild(el("span", "lb-cell lb-rr", p.rr != null ? (p.rr + " RR") : "-"));
    row.appendChild(el("span", "lb-cell lb-matches", p.matches != null ? String(p.matches) : "-"));
    row.appendChild(el("span", "lb-cell lb-winrate", p.win_rate != null ? String(p.win_rate) : "-"));
    return row;
  }

  // The service did not return standings; do not present this as a filtered-empty board.
  function emptyBoard(ui, lt) {
    var el = ui.el;
    var note = el("p", "lb-empty-note",
      lt(activeScope === "friends" ? "empty_friends" : "empty_global"));
    return ui.emptyState({ icon: "◆", message: lt("empty_title"), action: note });
  }
})();
