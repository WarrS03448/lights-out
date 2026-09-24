/* Profile screen — OWNED BY THE PROFILE SCREEN.
 *
 * This is the ONLY JS file this screen edits. It registers itself with the shared core and renders
 * the signed-in player's profile from its slice of the snapshot (state.profile, contributed by
 * hub/webui/screens/profile.py) plus the shared identity slice (state.auth). It draws with the
 * shared helpers (ctx.ui) and tokens/components; its own visual rules live in screens/profile.css.
 *
 * HONESTY IS THE WHOLE POINT of this screen. The numbers come from competitive.profile_stats(),
 * which returns null/0/"" for anything with no data yet: win_rate is null until a scoreboard
 * exists, top_map is "" until a match is played, and rank rating / peak / per-round stats have no
 * backend at all. This screen renders every one of those as a plain "—" / "Unranked" / "not
 * tracked yet" placeholder — it NEVER invents a figure to fill a tile.
 *
 * Screen-local strings (labels this screen adds) are shipped in state.profile.strings by the
 * Python half and read here via pt() — they are not in the shared i18n table. Shared strings
 * (nav/section labels) still resolve through ctx.t(). */
(function () {
  "use strict";

  window.HubUI.registerScreen("profile", { render: render, update: function(root,state,ctx){
    var a=state.auth||{},scope=JSON.stringify([a.signed_in,a.player_id||a.steam_id,state.lang]);
    if(root._rankedScope!==scope||rankGuideOpen||root.querySelector('.profile-ranks'))return false;
    var draft=document.createElement('div');render(draft,state,ctx,root);
    ctx.ui.patchRankedChildren(root,draft);return true;
  }, renderRankGuide: function (root, state, ctx) {
    if (ctx.resetRankGuideScroll) { rankGuideScroll = 0; }
    render(root, state, Object.assign({}, ctx, { rankGuide: true }));
  } });

  // View-only navigation survives the core's snapshot redraws, like History's filter.
  var rankGuideOpen = false;
  var rankGuideScroll = 0;
  var profileIdentity = null;
  var returnToBadge = false;

  // Match outcomes used by the recent-match rows.
  var FORM = {
    win:       { letter: "W", cls: "win",       key: "profile_form_win" },
    loss:      { letter: "L", cls: "loss",      key: "profile_form_loss" },
    played:    { letter: "P", cls: "played",    key: "profile_form_played" },
    cancelled: { letter: "C", cls: "cancelled", key: "profile_form_cancelled" },
    fault:     { letter: "F", cls: "fault",     key: "profile_form_fault" }
  };

  function render(root, state, ctx) {
    var liveRoot=arguments[3],a=state.auth||{};
    root._rankedScope=JSON.stringify([a.signed_in,a.player_id||a.steam_id,state.lang]);
    var ui = ctx.ui, call = ctx.call, t = ctx.t, esc = ui.esc, el = ui.el;
    var auth = state.auth || {};
    var prof = state.profile || {};
    // The ladder the server sent with `hello`: the names in their real order, which is what the
    // rank badges are keyed by (docs/ranks.md). It lives on the competitive slice, and every
    // slice is in every snapshot, so it is here whichever screen is showing.
    var ranks = ((state.comp || {}).ladder || {}).ranks || null;
    var strings = prof.strings || {};
    var stats = prof.stats || {};
    var currentRank = auth.placing ? null : (auth.rank || null);
    var identity = auth.signed_in && prof.signed_in ? (auth.player_id || auth.steam_id || prof.steam_id || auth.persona || "signed-in") : null;
    if (profileIdentity !== identity) {
      rankGuideOpen = false;
      rankGuideScroll = 0;
      profileIdentity = identity;
    }

    // pt(key) — a screen-local string, falling back to the raw key (English is server-filled).
    function pt(key, vars) {
      var s = strings[key];
      if (s === undefined || s === null) { return key; }
      return s.replace(/\{(\w+)\}/g, function (match, name) {
        return vars && vars[name] !== undefined ? vars[name] : match;
      });
    }

    // signed out (or no profile slice): one honest empty state, nothing invented.
    if (!auth.signed_in || !prof.signed_in) {
      root.appendChild(ui.emptyState({ icon: "◆", message: pt("profile_signed_out") }));
      return;
    }

    var history = state.history || {};
    if (!history.asked && !history.loading && !history.error) { call("load_history"); }

    draw();
    if(liveRoot)root=liveRoot;

    function draw() {
      root.innerHTML = "";
      if (rankGuideOpen || ctx.rankGuide) {
        root.appendChild(buildRankGuide());
        root.querySelector(".profile-ranks-scroll").scrollTop = rankGuideScroll;
        return;
      }
      var wrap = el("div", "profile");
      wrap.appendChild(buildSide());
      wrap.appendChild(buildMain());
      root.appendChild(wrap);
    }

    function openRankGuide(fromBadge) {
      returnToBadge = fromBadge;
      rankGuideOpen = true;
      rankGuideScroll = 0;
      draw();
      root.querySelector(".profile-ranks-back").focus();
    }

    function closeRankGuide() {
      if (ctx.closeRankGuide) { ctx.closeRankGuide(); return; }
      rankGuideOpen = false;
      draw();
      var trigger = root.querySelector(returnToBadge ? ".profile-id-badge button" : ".profile-rank-link");
      if (!trigger) { trigger = root.querySelector(".profile-rank-link"); }
      if (trigger) { trigger.focus(); }
    }

    function rankName(rank) {
      if (!rank) { return pt("profile_unranked"); }
      return rank.rank_name + (rank.top || !rank.division ? "" : " " + rank.division);
    }

    function buildRankGuide() {
      var guide = el("section", "profile-ranks");
      guide.setAttribute("aria-labelledby", "profile-ranks-title");
      var head = el("div", "profile-ranks-head");
      head.appendChild(ui.btn("ui-chip profile-ranks-back", ctx.rankGuideBackLabel || pt("profile_back_to_profile"), closeRankGuide));
      var heading = el("h1", "profile-ranks-title", pt("profile_rank_ladder"));
      heading.id = "profile-ranks-title";
      head.appendChild(heading);
      head.appendChild(el("p", "profile-ranks-intro", pt("profile_ranks_intro")));
      guide.appendChild(head);

      var scroll = el("div", "profile-ranks-scroll");
      scroll.addEventListener("scroll", function () { rankGuideScroll = scroll.scrollTop; });
      var current = el("div", "profile-ranks-current");
      var myBadge = ui.rankBadge(currentRank, ranks);
      if (myBadge) { current.appendChild(myBadge); }
      var standing = el("div", "profile-ranks-standing");
      standing.appendChild(el("div", "profile-ranks-label", pt("profile_ranks_your_rank")));
      standing.appendChild(el("div", "profile-ranks-current-name", rankName(currentRank)));
      if (auth.placing) {
        standing.appendChild(el("div", "profile-ranks-status", auth.placements_left !== null && auth.placements_left !== undefined
          ? t("comp_placements_left", { n: auth.placements_left }) : t("comp_placements")));
      } else if (currentRank && currentRank.rr !== null && currentRank.rr !== undefined) {
        standing.appendChild(el("div", "profile-ranks-status", currentRank.rr + " RR"));
      }
      current.appendChild(standing);
      scroll.appendChild(current);

      if (!ranks || !Array.isArray(ranks.names) || !ranks.names.length || !ranks.divisions) {
        scroll.appendChild(ui.emptyState({ icon: "◆", message: pt("profile_ranks_unavailable") }));
      } else {
        var rrStep = Number(ranks.rr_per_division);
        var hasRR = Number.isFinite(rrStep) && rrStep > 0;
        var countingRank = Number(ranks.counting_rank);
        var countingName = ranks.names[countingRank - 1];
        if (hasRR && countingName) {
          scroll.appendChild(el("p", "profile-ranks-note", pt("profile_ranks_progress", {
            rank: countingName, max: rrStep - 1, step: rrStep
          })));
        }
        var cards = el("div", "profile-ranks-grid");
        var myIndex = ui.rankBadgeIndex(currentRank, ranks);
        ranks.names.forEach(function (name, index) {
          var card = el("section", "profile-ranks-card");
          var cardHead = el("div", "profile-ranks-card-head");
          var mainBadge = ui.rankBadge({ rank: index + 1, rank_name: name, division: 1 }, ranks);
          if (mainBadge) { cardHead.appendChild(mainBadge); }
          cardHead.appendChild(el("h2", "profile-ranks-name", name));
          card.appendChild(cardHead);
          var divisions = el("div", "profile-ranks-divisions");
          for (var d = 1; d <= ranks.divisions; d += 1) {
            var rank = { rank: index + 1, rank_name: name, division: d };
            var isCurrent = currentRank && !currentRank.top && myIndex === ui.rankBadgeIndex(rank, ranks);
            var division = el("div", "profile-ranks-division" + (isCurrent ? " is-current" : ""));
            var badge = ui.rankBadge(rank, ranks);
            if (badge) { division.appendChild(badge); }
            division.appendChild(el("span", "profile-ranks-division-name", name + " " + d));
            if (hasRR) {
              var counting = index + 1 === countingRank;
              var low = counting ? (d - 1) * rrStep : 0;
              var range = counting && d === ranks.divisions ? low + "+" : low + "–" + (low + rrStep - 1);
              division.appendChild(el("span", "profile-ranks-rr profile-ranks-division-rr", range + " RR"));
            }
            if (isCurrent) {
              division.setAttribute("aria-current", "step");
              division.appendChild(el("span", "profile-ranks-here", pt("profile_ranks_here")));
              card.classList.add("is-current");
            }
            divisions.appendChild(division);
          }
          card.appendChild(divisions);
          if (index + 1 === countingRank) {
            card.appendChild(el("p", "profile-ranks-note", pt("profile_ranks_counting", {
              rank: name, divisions: ranks.divisions
            })));
          }
          cards.appendChild(card);
        });
        if (ranks.top) {
          var isTop = currentRank && currentRank.top;
          var capstone = el("section", "profile-ranks-card profile-ranks-capstone" + (isTop ? " is-current" : ""));
          var capHead = el("div", "profile-ranks-card-head");
          var capBadge = ui.rankBadge({ top: true }, ranks);
          if (capBadge) { capHead.appendChild(capBadge); }
          capHead.appendChild(el("h2", "profile-ranks-name", ranks.top));
          capstone.appendChild(capHead);
          capstone.appendChild(el("div", "profile-ranks-status", pt("profile_ranks_no_divisions")));
          if (countingName && Number.isFinite(ranks.top_at) && ranks.top_at >= 0 &&
              Number.isFinite(ranks.top_slots) && ranks.top_slots > 0) {
            capstone.appendChild(el("div", "profile-ranks-rr", ranks.top_at + "+ RR"));
            var rules = { rank: countingName, top: ranks.top, rr: ranks.top_at, slots: ranks.top_slots };
            capstone.appendChild(el("p", "profile-ranks-note", pt("profile_ranks_top_qualify", rules)));
            capstone.appendChild(el("p", "profile-ranks-note", pt("profile_ranks_top_keep", rules)));
          }
          if (isTop) {
            capstone.setAttribute("aria-current", "step");
            capstone.appendChild(el("span", "profile-ranks-here", pt("profile_ranks_here")));
          }
          cards.appendChild(capstone);
        }
        scroll.appendChild(cards);
      }
      guide.appendChild(scroll);
      return guide;
    }

    // -- left column: identity, rank, honest stat grid ------------------------
    function buildSide() {
      var side = el("div", "profile-side");

      // IDENTITY: the Steam picture, the name, and the rank's emblem at the other end of the row.
      //
      // NO NUMERALS. This plate used to carry the player's LEVEL - a 6 in an accent square - and
      // the rank card below it carried the same numeral again whenever no badge could be drawn.
      // Sam, 2026-09-16: "it still shows two number sixes ... remove the one at the top and
      // replace the six next to the user's name with the rank icon of the respective rank of the
      // user." `level` is the OLD numeric ladder (docs/ranks.md), it is not what a player is shown
      // anywhere else, and the screen that is about their standing was the worst place left for
      // it. Then, in the same breath: "put the user's steam image next to their name in the
      // profile. for me its just a big NE."
      //
      // Both, because they are not the same fact: the picture says who this is, the emblem says
      // how good they are. They sit at opposite ends of the row so neither is decoration on the
      // other. "NE" was the INITIALS fallback showing through - it still sits under the picture,
      // for an account with no avatar, a hub that is offline, or the first draw before the cache
      // is warm. There is no badge for a player who is still placing, and inventing one would be
      // inventing a rank, so that end of the row is simply empty until they have one.
      var rank = currentRank;
      var idRow = el("div", "profile-id");
      idRow.appendChild(ui.avatar({
        url: prof.avatar || auth.avatar,
        text: ui.initials(prof.persona || auth.persona),
        ariaLabel: prof.persona
      }));
      var idText = el("div", "profile-id-text");
      idText.appendChild(el("div", "profile-name", prof.persona || auth.persona || ""));
      idText.appendChild(el("div", "profile-sub", pt("profile_signed_in_via")));
      idRow.appendChild(idText);
      var idBadge = ui.rankBadge(rank, ranks);
      if (idBadge) {
        var idHolder = el("div", "profile-id-badge");
        var rankButton = ui.btn("profile-rank-open", undefined, function () { openRankGuide(true); },
          { ariaLabel: pt("profile_ranks_open") + " · " + rankName(rank) });
        rankButton.title = pt("profile_ranks_open");
        rankButton.appendChild(idBadge);
        idHolder.appendChild(rankButton);
        idRow.appendChild(idHolder);
      }
      side.appendChild(idRow);
      var eventState=state.tournament||{},eventData=eventState.data||{},eventStrings=eventState.strings||{};
      (eventData.badges||[]).forEach(function(badge){side.appendChild(el("span","tournament-badge",(eventStrings[badge.type]||badge.type)+(badge.rank?" #"+badge.rank:"")));});

      // THE RANK CARD. The name and the RR come from `auth.rank` - the same block the competitive
      // hero draws - rather than from a tier this screen worked out from `level`. The two
      // disagreed: `level` is the old numeric ladder, so the label here could read "Operator"
      // while the server had the player at Operator 3 and the leaderboard on the next tab agreed
      // with the server. With no rank block (signed out, or still placing) it falls back to the
      // old level-derived label and the honest "not tracked" line, unchanged.
      //
      // WORDS ONLY now. The emblem moved up beside the player's name (see above), and a second
      // copy of it here would say the same thing twice a centimetre apart; the numeral this card
      // fell back to when there was no badge is gone with it.
      var rankCard = el("div", "profile-rank");
      var tier = prof.tier || auth.tier || "";
      var rankHead = el("div", "profile-rank-head");
      // WHILE PLACING, "UNRANKED" - never the level-derived tier. `tier` is worked out from the
      // old numeric ladder, and `level` survives on the session from before the placements began,
      // so a player who has no rank at all was being shown one they have not earned ("SPECTRE",
      // under a plate that had just told them their rating is not tracked yet). The tier stays as
      // the fallback for the other case it was written for: a snapshot with no rank block because
      // the stream has not said hello yet.
      var rankText = el("div", null);
      rankText.appendChild(el("div", "profile-rank-name",
        rank ? (rank.top ? rank.rank_name : rank.rank_name + " " + rank.division)
             : ((!auth.placing && tier) ? tier : pt("profile_unranked"))));
      var hasRr = rank && rank.rr !== null && rank.rr !== undefined;
      rankText.appendChild(el("div", "profile-rank-rr", pt("profile_rank_rating") + " · " +
        (hasRr ? (rank.rr + " RR") : pt("profile_not_tracked"))));
      rankHead.appendChild(rankText);
      rankCard.appendChild(el("div","profile-section-title",prof.ranked_mode==="BB1"?"1v1 Bodybomb":"5v5 Bodybomb"));
      rankCard.appendChild(rankHead);
      rankCard.appendChild(ui.btn("profile-rank-link", pt("profile_ranks_open"), function () { openRankGuide(false); }));
      side.appendChild(rankCard);

      // honest stat grid — only the numbers profile_stats actually computes
      var grid = el("div", "profile-stats");
      grid.appendChild(ui.statTile(numOrNull(stats.played), pt("profile_stat_matches")));
      grid.appendChild(ui.statTile(numOrNull(stats.wins), pt("profile_stat_wins")));
      grid.appendChild(ui.statTile(numOrNull(stats.losses), pt("profile_stat_losses")));
      grid.appendChild(ui.statTile(winRate(), pt("profile_stat_winrate")));
      grid.appendChild(ui.statTile(topMap(), pt("profile_stat_topmap")));
      grid.appendChild(ui.statTile(numOrNull(stats.cancelled), pt("profile_stat_cancelled")));
      side.appendChild(grid);

      side.appendChild(el("div", "profile-note", prof.history_mode==="all"?pt("profile_ranked_only"):(prof.history_mode==="BB1"?"1v1 Bodybomb":"5v5 Bodybomb")));
      return side;
    }

    // -- right column: recent matches ----------------------------------------
    function buildMain() {
      var main = el("div", "profile-main");
      var ladders=el("div","profile-section ranked-profile-ranks");
      ["BB5","BB1"].forEach(function(mode){
        var rank=(prof.ranked_ranks||{})[mode],card=el("div","profile-section");
        card.appendChild(el("div","profile-section-title",mode==="BB1"?"1v1 Bodybomb (Paintball)":"5v5 Bodybomb"));
        if(rank&&!rank.placing){var badge=ui.rankBadge(rank,ranks);if(badge)card.appendChild(badge);card.appendChild(el("div","",rankName(rank)));card.appendChild(el("div","",String(rank.rr||0)+" RR"));}
        else card.appendChild(el("div","",pt("profile_unranked")));
        ladders.appendChild(card);
      });
      main.appendChild(ladders);
      var modeTabs=ui.tabs([{id:"all",label:(history.strings||{}).filter_all||"All"},{id:"BB5",label:"5v5"},{id:"BB1",label:"1v1"}],prof.history_mode||"all",function(mode){call("history_mode",mode);});
      modeTabs.classList.add("ranked-history-modes");main.appendChild(modeTabs);
      main.appendChild(recentSection());
      return main;
    }

    function recentSection() {
      var sec = el("div", "profile-section profile-recent");
      var head = el("div", "profile-section-head");
      head.appendChild(el("div", "profile-section-title", pt("profile_recent_matches")));
      head.appendChild(ui.btn("ui-chip", pt("profile_view_all"), function () { call("set_view", "history"); }));
      sec.appendChild(head);

      var recent = prof.recent || [];
      // No data yet: say so honestly. Distinguish "never fetched" (offer refresh) from "fetched,
      // but empty" (no matches yet) — but either way we never draw a fake row.
      if (!recent.length) {
        var msg = prof.history_loading ? pt("profile_loading")
          : prof.history_error ? prof.history_error
          : pt("profile_no_matches");
        sec.appendChild(ui.emptyState({
          icon: "◆",
          message: prof.history_loading || prof.history_error ? msg
            : pt("profile_no_matches") + " · " + pt("profile_no_matches_body"),
          action: prof.history_loading ? null : refreshBtn()
        }));
        return sec;
      }

      var rows = recent.map(function (m) {
        var res = resultOf(m);
        var row = ui.listRow({
          leading: el("span", "recent-res " + res.cls, res.letter),
          children: [
            el("div", "recent-map", (m.mode==="BB1"?"1v1":"5v5")+" · "+(m.map || "-")),
            el("div", m.cheater_reverted ? "cheater-reverted-notice" : "recent-meta", m.cheater_reverted ? pt("profile_cheater_reverted") : eloText(m.elo) + (m.ended ? " · " + whenText(m.ended) : ""))
          ]
        });
        if (m.id) {
          function openMatch() {
            call("set_view", "history");
            call("open_match", m.id);
          }
          row.classList.add("recent-open");
          row.setAttribute("role", "button");
          row.setAttribute("tabindex", "0");
          row.addEventListener("click", openMatch);
          row.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openMatch(); }
          });
        }
        return row;
      });
      sec.appendChild(ui.table(rows));
      return sec;
    }

    // ---------------------------------------------------------------- helpers
    function refreshBtn() {
      var b = ui.btn("ui-chip", prof.history_loading ? pt("profile_loading") : pt("profile_refresh"),
        function () { call("refresh_profile"); });
      if (prof.history_loading) { b.disabled = true; }
      return b;
    }

    function numOrNull(n) { return (n === null || n === undefined) ? null : n; }

    function winRate() {
      // null until a scoreboard exists: show a plain dash, never a fabricated 0%.
      return (stats.win_rate === null || stats.win_rate === undefined) ? null : (stats.win_rate + "%");
    }

    function topMap() {
      return stats.top_map ? stats.top_map : null;   // "" -> statTile draws "—"
    }

    function resultOf(m) {
      if (m.cancelled) { return m.blamed ? letter("fault") : letter("cancelled"); }
      if (m.won === true) { return letter("win"); }
      if (m.won === false) { return letter("loss"); }
      return letter("played");
    }
    function letter(kind) { var f = FORM[kind]; return { letter: f.letter, cls: f.cls }; }

    function eloText(elo) {
      if (elo === null || elo === undefined) { return pt("profile_not_tracked"); }
      var n = Number(elo);
      return (n > 0 ? "+" + n : String(n)) + " RR";
    }

    function whenText(ended) {
      var ms = Number(ended);
      if (!isFinite(ms) || ms <= 0) { return "-"; }
      try { return new Date(ms).toLocaleDateString(); } catch (e) { return "-"; }
    }
  }
})();
