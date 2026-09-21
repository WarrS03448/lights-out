#!/usr/bin/env python3.12
"""Headless tests for the RANK BADGES - the 25 pieces of art the hub shows for a visible rank.
Run:

    python3.12 tests/test_screen_rankbadge.py

The art itself (hub/webui/static/ranks.svg, ranksprite.js) is drawn in a browser and cannot be
asserted from here. What CAN be asserted, and what these cover, is everything a badge needs before
it can be drawn at all:

  * the snapshot actually carries a rank. The server describes one in two shapes - `rating` nests
    it under `rank` (rating.publicRating) and `match_result` spreads it flat
    (progress.publicProgress) - and only the flat one was ever read, so a fully ranked player got
    the PLACEMENT line until they finished a match. That bug is invisible in a screenshot of a
    working hub and total on a real one. The two shapes now agree on everything inside the block:
    one ladder, ranks numbered from 1, a null division at the capstone.
  * the badge's key. docs/ranks.md keys icons by INDEX so a rank can be renamed on the service
    without a hub release, and the index is a rank's position in the ladder `hello` sent. The rank
    block therefore has to carry a 1-based number AND a name, whichever event it came from.
  * the leaderboard rows carry the three fields their badge is resolved from, since the board is
    fetched over HTTP and cannot see the ladder itself.
  * the sprite really does hold all 25 symbols, and index.html loads it before any screen that
    draws one.
  * the ranked panel says nothing about the matchmaking rating. That is a product rule, not a detail:
    "we dont even want the players to know there is a matchmaking rating" (Sam, 2026-09-15).

Same shape as the other tests/test_screen_*.py: plain asserts on the JSON snapshot dict, no Tk, no
pywebview, no network.
"""
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_STATE = tempfile.mkdtemp(prefix="hub-rb-state-")
os.environ.setdefault("HUB_STATE_DIR", _STATE)
sys.path.insert(0, str(REPO))

from hub import competitive as C                 # noqa: E402
from hub import i18n                             # noqa: E402
from hub.webui.panel import WebPanel             # noqa: E402
from hub.webui.scheduler import InlineScheduler  # noqa: E402
from hub.webui.snapshot import state_snapshot    # noqa: E402

RESULTS = []
STATIC = REPO / "hub" / "webui" / "static"

# The ladder as `hello` sends it - progress.ranks(), out of server/ladder.cjs. Spelled out here
# rather than imported from the hub, because the hub deliberately ships no copy of this list
# (docs/ranks.md): it renders whatever the server sent, and so does the art.
RANKS = {"names": ["Rookie", "Private", "Soldier", "Veteran", "Operator", "Shadow", "Nightmare", "Spectre"],
         "top": "Reaper", "divisions": 3, "base": 700, "span": 70, "bands": 24}


class _FakeApp:
    def __init__(self):
        self.state = {"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0"}}, "auth": None}
        self.catalogue = None


class _FakeLiveClient:
    def start(self):
        pass

    def stop(self):
        pass


def _panel():
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    s = panel.session
    s._action = lambda call, on_result=None: (lambda r: on_result(*r) if on_result else None)(call())
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6, "matches": 34, "wins": 19}
    s.token = "tok"
    s.client = _FakeLiveClient()
    s.phase = "idle"
    return panel, s


def _rank_after(event):
    """The auth.rank block a live event leaves in the snapshot."""
    panel, s = _panel()
    s.on_live_event(event)
    return state_snapshot(s, panel)["auth"]["rank"]


def _badge_index(rank, ranks=RANKS):
    """The same arithmetic ranksprite.js does, so the Python side can assert which badge a block
    resolves to. Deliberately small and dumb: if this and the JS ever disagree, the JS is the one
    that draws the badge and this is the copy that is wrong."""
    if not rank:
        return 0
    names = (ranks or {}).get("names") or []
    divs = (ranks or {}).get("divisions") or 3
    if rank.get("top"):
        return (len(names) or 8) * divs + 1
    ordinal = 0
    if rank.get("rank_name") in names:
        ordinal = names.index(rank["rank_name"]) + 1
    if not ordinal:
        ordinal = int(rank.get("rank") or 0)
    division = int(rank.get("division") or 0)
    if not ordinal or not division:
        return 0
    return (ordinal - 1) * divs + division


# ---------------------------------------------------------------- the rank reaches the page
def test_the_nested_rating_event_produces_a_rank_block():
    """A `rating` event from a server older than 2026-09-15 nests the whole block under `rank`.

    The hub read it as though it were flat, so rank_name/division/rr all came back None and
    snapshot._rank_block - which returns None without a name - drew nothing. A signed-in player at
    Operator 3 was shown "Unranked" until they finished a match. Today's server sends the flat shape
    (see the next test), but an installed hub outlives a deploy and a rollback is one click."""
    rank = _rank_after({"type": "rating", "level": 5,
                        "rank": {"band": 14, "rank": 5, "rank_name": "Operator", "division": 3,
                                 "rr": 42, "top": False},
                        "matches": 34, "wins": 19, "losses": 15,
                        "placing": False, "placements_left": 0})
    assert rank is not None, "a ranked player must get a rank block"
    assert rank["rank_name"] == "Operator" and rank["division"] == 3 and rank["rr"] == 42
    assert rank["rank"] == 5, "ranks are 1-based, whichever event the block came from"
    assert _badge_index(rank) == 15, "Operator 3 is the 15th badge"


def test_the_flat_match_result_shape_still_produces_a_rank_block():
    """THE SHAPE THE SERVER ACTUALLY SENDS, on both `rating` and `match_result`: progress
    .publicProgress, spread flat, with `rank` a plain 1-based integer beside a top-level name.

    Both events carry the same call now. They used to carry different ones - `rating` named the
    rank the player's matchmaking rating deserved and `match_result` the rank they had climbed to - from
    different ladders with different names, so a finished match could rename a player's rank out
    from under the badge they had been looking at all session."""
    rank = _rank_after({"type": "match_result", "won": True, "delta": 2, "level": 5,
                        "rank": 5, "rank_name": "Operator", "division": 2, "rr": 40,
                        "bdr": None, "placing": False, "placements_left": 0,
                        "matches": 35, "wins": 20,
                        "you": {"level": 5, "rank": 5, "rank_name": "Operator",
                                "division": 2, "rr": 40}})
    assert rank is not None and rank["rank"] == 5 and rank["division"] == 2
    assert rank["rank_name"] in RANKS["names"], "the ladder `hello` sent is the only ladder"
    assert _badge_index(rank) == 14


def test_the_capstone_has_no_division_and_keeps_its_figure():
    """The capstone is one band with no divisions, and its figure has no ceiling.

    `top` has to survive as a null division (which is how the page tells the two apart) and the
    figure has to survive at all: reaching only for BDR - which rating.cjs does not send - left a
    null, which the page renders as the literal placeholder "{rr} RR"."""
    rank = _rank_after({"type": "rating", "level": 9,
                        "rank": {"band": 24, "rank": 9, "rank_name": "Reaper",
                                 "division": None, "rr": 312, "top": True},
                        "matches": 90, "placing": False, "placements_left": 0})
    assert rank is not None and rank["top"] is True
    assert rank["division"] is None, "a 0 here would print 'Reaper 0'"
    assert rank["rank"] == 9, "the capstone is the rank above the eight named ones"
    assert rank["rr"] == 312
    assert _badge_index(rank) == 25, "the capstone is the 25th badge"


def test_the_counting_band_keeps_its_division_and_loses_its_bar():
    """THE TOP NAMED RANK IS NOT LIKE THE SEVEN BELOW IT (docs/ranks.md, Sam 2026-09-16).

    From Spectre 1 the figure stops resetting every division and counts: 100 RR is Spectre 2, 200
    RR is Spectre 3, and Spectre 3 has no ceiling. So the block has to carry BOTH a division (it
    is still a named rank with three of them, and its own badge) AND `counting`, which is what
    tells the page not to draw a 0-100 progress bar for a figure that can be 940.
    """
    rank = _rank_after({"type": "rating", "level": 8, "rank": 8, "rank_name": "Spectre",
                        "division": 2, "rr": 150, "bdr": 150, "counting": True, "top": False,
                        "top_eligible": False, "placing": False, "placements_left": 0,
                        "matches": 90})
    assert rank is not None and rank["top"] is False, "not the capstone: that is 150 seats"
    assert rank["division"] == 2, "150 RR is Spectre 2"
    assert rank["rr"] == 150, "and the figure is the running count, not 50 through a division"
    assert rank["counting"] is True, "which is the page's cue that there is no bar to draw"
    assert _badge_index(rank) == 23, "Spectre 2 is the 23rd badge, same as any other rank's 2"


def test_enough_rr_for_the_capstone_is_not_the_capstone():
    """Eligibility is a number; the rank is 150 seats on the leaderboard, and only the server can
    say which. A player on 300+ RR who is not seated stays Spectre 3 and is told they are
    eligible, so the hub can explain a badge that has not moved instead of looking stuck."""
    rank = _rank_after({"type": "rating", "level": 8, "rank": 8, "rank_name": "Spectre",
                        "division": 3, "rr": 940, "bdr": 940, "counting": True, "top": False,
                        "top_eligible": True, "placing": False, "placements_left": 0,
                        "matches": 200})
    assert rank["division"] == 3, "there is no Spectre 4, however high the figure goes"
    assert rank["rr"] == 940 and rank["top"] is False
    assert rank["top_eligible"] is True
    assert _badge_index(rank) == 24, "still the 24th badge, not the capstone's 25th"


def test_taking_a_seat_in_a_match_clears_the_division_on_the_hero():
    """The capstone can be taken - and lost - by a match, so `match_result` has to be able to move
    a player ONTO it. The division goes null there, and a null is exactly what the field-by-field
    copy skips (a missing field must not wipe a good one), so the capstone is handled on its own.
    Without that the new Reaper wore "Reaper III" until the hub was restarted."""
    rank = _rank_after({"type": "match_result", "won": True, "delta": 2,
                        "rank": 9, "rank_name": "Reaper", "division": None, "rr": 402,
                        "bdr": 402, "counting": True, "top": True, "top_eligible": True,
                        "placing": False, "placements_left": 0, "matches": 201, "wins": 130,
                        "you": {"rank": 9, "rank_name": "Reaper", "division": None, "rr": 402,
                                "bdr": 402, "counting": True, "top": True, "top_eligible": True,
                                "arrows": 2, "bdr_delta": 18, "bdr_before": 384}})
    assert rank is not None and rank["top"] is True
    assert rank["division"] is None, "a leftover 3 here would print 'Reaper 3'"
    assert rank["rank_name"] == "Reaper" and rank["rr"] == 402
    assert _badge_index(rank) == 25


def test_a_placing_player_gets_no_rank_and_therefore_no_badge():
    """The rating is real from match one; the RANK is the part we are not confident enough to
    print. None is the page's cue to show the placement line, and no badge at all."""
    rank = _rank_after({"type": "rating", "level": None, "rank": None, "placing": True,
                        "placements_left": 3, "matches": 2})
    assert rank is None
    assert _badge_index(rank) == 0


def test_every_rung_of_the_ladder_resolves_to_its_own_badge():
    """24 divisions plus the capstone, each to a distinct index in 1..25 - which is the whole
    promise of per-division art. A collision here means two ranks wear the same badge."""
    seen = []
    for i, name in enumerate(RANKS["names"]):
        for d in (1, 2, 3):
            seen.append(_badge_index({"rank": i + 1, "rank_name": name, "division": d}))
    seen.append(_badge_index({"top": True, "rank_name": RANKS["top"]}))
    assert seen == list(range(1, 26)), seen


def test_a_name_off_the_ladder_falls_back_to_the_rank_number():
    """A snapshot can render before `hello` has landed, or against a ladder that has since been
    reordered. Neither is a reason to draw nothing, so the numeric rank is the fallback."""
    assert _badge_index({"rank": 5, "rank_name": "Nameless", "division": 2}) == 14
    assert _badge_index({"rank": 5, "rank_name": "Operator", "division": 2}, None) == 14


def test_a_renamed_rank_keeps_its_badge():
    """docs/ranks.md: the names are env dials on the service, so the art is keyed by INDEX. A
    rename must move the name and leave the badge exactly where it was. Zenith really was the
    eighth rank for seven hours on 2026-09-15."""
    renamed = dict(RANKS, names=["Rookie", "Private", "Soldier", "Veteran", "Operator", "Shadow",
                                 "Nightmare", "Zenith"])
    assert _badge_index({"rank": 8, "rank_name": "Zenith", "division": 2}, renamed) == 23
    assert _badge_index({"rank": 8, "rank_name": "Spectre", "division": 2}) == 23


# ---------------------------------------------------------------- the leaderboard's badges
def test_leaderboard_rows_carry_what_their_badge_is_resolved_from():
    """The board is fetched over HTTP and knows nothing about the ladder, so the row ships the
    server's own three fields and the page does the lookup. `rank` on a row is the POSITION on
    the board, not the rank index - a badge resolved from it would be nonsense."""
    from hub.webui.screens.leaderboard import snapshot as lb_snapshot
    panel, s = _panel()
    s.board_available = True
    s.board_rows = ({"rank": 1, "steam_id": "1", "persona": "Sam", "rank_name": "Reaper",
                     "division": None, "rr": 312, "top": True, "matches": 140, "win_rate": 61},
                    {"rank": 2, "steam_id": "2", "persona": "Wario", "rank_name": "Operator",
                     "division": 3, "rr": 42, "top": False, "matches": 114, "win_rate": 57})
    s.board_you = None
    rows = lb_snapshot(s, panel)["leaderboard"]["rows"]
    assert rows[0]["top"] is True and rows[0]["division"] is None
    assert rows[0]["rank_name"] == "Reaper" and _badge_index(rows[0]) == 25
    assert rows[1]["rank_name"] == "Operator" and rows[1]["division"] == 3
    assert _badge_index(rows[1]) == 15
    assert rows[1]["rank"] == 2, "board position, which is not the rank index"


def test_a_placing_player_on_the_board_gets_no_badge():
    """No name from the server means no rank, on the board for the same reason as on the hero."""
    from hub.webui.screens.leaderboard import snapshot as lb_snapshot
    panel, s = _panel()
    s.board_available = True
    s.board_rows = ({"rank": 9, "steam_id": "9", "persona": "New", "rank_name": None,
                     "division": 0, "rr": 0, "top": False, "matches": 2, "win_rate": 50},)
    s.board_you = None
    row = lb_snapshot(s, panel)["leaderboard"]["rows"][0]
    assert row["rank_name"] is None and _badge_index(row) == 0


# ---------------------------------------------------------------- the art ships and is loaded
def test_the_sprite_holds_all_twenty_five_badges():
    """Every rung has art, and every id is the zero-padded index the loader builds."""
    sprite = (STATIC / "ranksprite.js").read_text(encoding="utf-8")
    ids = sorted(set(re.findall(r'<symbol id="(rk-\d\d)"', sprite)))
    assert ids == ["rk-%02d" % i for i in range(1, 26)], ids
    master = (STATIC / "ranks.svg").read_text(encoding="utf-8")
    assert sorted(set(re.findall(r'<symbol id="(rk-\d\d)"', master))) == ids, \
        "ranks.svg is the master and must not drift from the inlined copy"


def test_index_html_loads_the_sprite_before_any_screen_that_draws_one():
    """ui.js creates window.HubUI, ranksprite.js registers onto it, and the screens call it. Out
    of order, rankBadge is undefined at render and the emblem silently disappears."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    order = re.findall(r'<script src="([^"]+)"', html)
    assert "ranksprite.js" in order, "the sprite must be loaded at all"
    assert order.index("ui.js") < order.index("ranksprite.js") < order.index("core.js")
    for screen in ("screens/competitive.js", "screens/leaderboard.js", "screens/profile.js"):
        assert order.index("ranksprite.js") < order.index(screen), screen


def test_nothing_still_draws_the_old_css_emblem():
    """The CSS shield and its three division pips were replaced by art that carries both. A
    leftover rule or element would draw a second, contradictory rank mark over the badge."""
    for path in (STATIC / "screens" / "competitive.js", STATIC / "screens" / "competitive.css"):
        text = path.read_text(encoding="utf-8")
        for dead in ("rank-mark", "rank-pip"):
            assert dead not in text, "%s still references %s" % (path.name, dead)


# ---------------------------------------------------------------- one ladder, everywhere
def _default_names(path, const):
    """The default value of a `const X = (process.env.Y || 'a,b,c')` list in a .cjs module.

    Read out of the source rather than by running node: these tests are the Python suite, and the
    thing under test is which FILE a list of words lives in."""
    src = (REPO / path).read_text(encoding="utf-8")
    head = "const %s = (process.env." % const
    assert head in src, "%s not found in %s" % (const, path)
    tail = src.split(head, 1)[1]
    quoted = tail.split("||", 1)[1]
    default = quoted.split("'")[1]
    return [x.strip() for x in default.split(",") if x.strip()]


def test_the_names_live_in_exactly_one_file():
    """server/ladder.cjs, and nowhere else.

    progress.cjs owns the visible RR ladder and rating.cjs owns matchmaking rating; both need to know the
    ladder, neither can require the other (progress already requires rating), and both keeping a
    copy is precisely how this repository came to have TWO ladders - Static..Blackout in one and
    Rookie..Reaper in the other - with a hub drawing a badge from one beside a tier label from
    the other. A third copy appearing anywhere is the same bug starting over."""
    assert _default_names("server/ladder.cjs", "NAMES") == RANKS["names"]
    ladder_src = (REPO / "server/ladder.cjs").read_text(encoding="utf-8")
    assert "'%s'" % RANKS["top"] in ladder_src, "ladder.cjs has no capstone"
    for path in ("server/progress.cjs", "server/rating.cjs"):
        src = (REPO / path).read_text(encoding="utf-8")
        assert "require('./ladder.cjs')" in src, "%s does not read the shared ladder" % path
        for name in RANKS["names"][:3] + [RANKS["top"]]:
            # a name may still be NAMED in a comment explaining the history; it must not be data
            for line in src.splitlines():
                bare = line.strip()
                if bare.startswith("//") or bare.startswith("*") or bare.startswith("/*"):
                    continue
                assert "'%s" % name not in bare, "%s still hardcodes %s: %s" % (path, name, bare)


def test_rating_cjs_does_not_name_a_visible_rank():
    """The hidden half must not be able to answer "what rank is this player". It used to export
    `rankOf`, which named one from MMR - a second answer to a question progress.cjs already
    answers from RR, and the two could disagree about the same player on the same screen."""
    src = (REPO / "server/rating.cjs").read_text(encoding="utf-8")
    assert "function rankOf" not in src
    assert "rankBandCount" not in src
    # ...and its ladder payload no longer carries the visible one; live.cjs merges that in from
    # progress.ranks(), which is the module that owns it.
    live = (REPO / "server/live.cjs").read_text(encoding="utf-8")
    assert "ranks: progressLib.ranks()" in live


def test_the_hub_fallback_lists_are_the_same_ladder_plus_the_capstone():
    """The hub renders the server's ladder, but it keeps a list for the offline case, and a stale
    one is worse than none: it is a confident wrong answer. Nine entries, capstone last."""
    from hub.webui.snapshot import TIER_NAMES, tier_for
    from hub.webui.screens.profile import TIER_ORDER
    expected = RANKS["names"] + [RANKS["top"]]
    assert TIER_NAMES == expected, TIER_NAMES
    assert TIER_ORDER == expected, TIER_ORDER
    assert tier_for(1) == "Rookie" and tier_for(9) == "Reaper"
    assert tier_for(99) == "Reaper", "off the end of the ladder is still the top of it"
    # No level means no rank, and a name here would be one the player has not earned.
    assert tier_for(None) == "" and tier_for(0) == "" and tier_for("") == ""


def test_the_tk_badge_has_a_colour_for_every_rank():
    """hub/theme.py draws the same ladder the web UI does. It used to stop at 8 and run BRIGHTER
    to the top, which is backwards for this ladder (docs/ranks.md: darker, denser, quieter)."""
    from hub.theme import RANK_COLOURS, RANK_INK, RANKS as N, rank_colour, rank_text_colour
    assert N == len(RANKS["names"]) + 1 == 9
    assert sorted(RANK_COLOURS) == list(range(1, 10))
    assert sorted(RANK_INK) == list(range(1, 10))

    def luma(hexv):
        r, g, b = (int(hexv[i:i + 2], 16) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    plates = [luma(rank_colour(i)) for i in range(1, 10)]
    assert plates == sorted(plates, reverse=True),         "the plate must darken all the way up the ladder, never brighten: %s" % plates
    # and the numeral on it stays readable at every rank
    for i in range(1, 10):
        assert luma(rank_text_colour(i)) - luma(rank_colour(i)) > 80, i


# ---------------------------------------------------------------- what the panel may not say
def test_the_ranked_panel_never_mentions_a_hidden_rating():
    """Sam, 2026-09-15: "we dont even want the players to know there is a matchmaking rating."

    "How ranked works" used to open on the kicker "Two systems, one number" and carry a whole note
    - rank_hidden - explaining that matchmaking uses a separate matchmaking rating. Every word of it was
    true, and it is exactly the thing a player is never told.

    Asserted of the STRINGS and in EVERY language, not of the English copy, because a translation
    is where a deleted sentence survives: hub/i18n.py is seven parallel tables and nothing makes
    them agree about meaning, only about keys."""
    assert "rank_hidden" not in i18n.STRINGS["en"], "the hidden-rating note is back"
    for lang, table in i18n.STRINGS.items():
        assert "rank_hidden" not in table, "%s still has the hidden-rating note" % lang

    # Latin-script tells survive translation - a Russian or Chinese string still spells "Elo".
    # Whole words only: "elo" is inside "below" and "belongs", both of which are ordinary copy.
    UNIVERSAL = re.compile(r"(elo|mmr|glicko)|matchmaking rating|matchmaking rating", re.I)
    for lang, table in i18n.STRINGS.items():
        for key, value in table.items():
            if not key.startswith("rank_"):
                continue
            hit = UNIVERSAL.search(str(value))
            assert not hit, "%s/%s says %r: %s" % (lang, key, hit.group(0), value)

    # ...and the English, which every translation was made from, must not describe one either.
    ENGLISH = ("hidden", "separate rating", "two systems", "behind it", "secret")
    for key, value in i18n.STRINGS["en"].items():
        if not key.startswith("rank_"):
            continue
        low = str(value).lower()
        for word in ENGLISH:
            assert word not in low, "en/%s says %r: %s" % (key, word, value)


def test_no_live_screen_quotes_a_figure_from_the_hidden_ladder():
    """The connect window told a player, in the shipped UI, "you lose {elo} Elo".

    Only the strings the WEB UI draws are asserted here. The Tk panel (hub/competitive.py) still
    has comp_no_show_me, comp_history_elo and comp_profile_elo_lost, which name Elo outright - it
    is the pre-2.0.0 UI and no player has seen it since, but it is the obvious place for this to
    come back from, so it is named here rather than left as a silent exception."""
    drawn = set()
    for path in sorted((STATIC / "screens").glob("*.js")) + [STATIC / "core.js", STATIC / "ui.js"]:
        src = path.read_text(encoding="utf-8")
        for key in i18n.STRINGS["en"]:
            if '"%s"' % key in src:
                drawn.add(key)
    assert "comp_connect_warn" in drawn, "the connect warning should be one of these"
    word = re.compile(r"(elo|mmr|glicko)", re.I)
    for lang, table in i18n.STRINGS.items():
        for key in sorted(drawn):
            hit = word.search(str(table.get(key, "")))
            assert not hit, "%s/%s is drawn by the web UI and says %r: %s" % (
                lang, key, hit.group(0), table.get(key))


# ---------------------------------------------------------------- the string under the badge
def test_comp_rr_resolves_in_every_language():
    """The RR line sits directly under the badge, and it rendered as a literal "{n} RR": every
    language had comp_rr TWICE, and the second, shadowing copy used the wrong placeholder. A
    duplicate key is silent in Python, so only the rendered string catches it."""
    for lang in i18n.STRINGS:
        i18n.set_language(lang)
        out = i18n.t("comp_rr", rr=42)
        assert "42" in out and "{" not in out, (lang, out)
    i18n.set_language("en")


# ---------------------------------------------------------------- runner
def _run(fn):
    name = fn.__name__
    try:
        fn()
        RESULTS.append((name, True))
        print("ok   " + name)
    except Exception:                                  # noqa: BLE001 - report, do not abort
        RESULTS.append((name, False))
        print("FAIL " + name)
        traceback.print_exc()


def main():
    for fn in [
        test_the_nested_rating_event_produces_a_rank_block,
        test_the_flat_match_result_shape_still_produces_a_rank_block,
        test_the_capstone_has_no_division_and_keeps_its_figure,
        test_the_counting_band_keeps_its_division_and_loses_its_bar,
        test_enough_rr_for_the_capstone_is_not_the_capstone,
        test_taking_a_seat_in_a_match_clears_the_division_on_the_hero,
        test_a_placing_player_gets_no_rank_and_therefore_no_badge,
        test_every_rung_of_the_ladder_resolves_to_its_own_badge,
        test_a_name_off_the_ladder_falls_back_to_the_rank_number,
        test_a_renamed_rank_keeps_its_badge,
        test_leaderboard_rows_carry_what_their_badge_is_resolved_from,
        test_a_placing_player_on_the_board_gets_no_badge,
        test_the_sprite_holds_all_twenty_five_badges,
        test_index_html_loads_the_sprite_before_any_screen_that_draws_one,
        test_nothing_still_draws_the_old_css_emblem,
        test_the_names_live_in_exactly_one_file,
        test_rating_cjs_does_not_name_a_visible_rank,
        test_the_hub_fallback_lists_are_the_same_ladder_plus_the_capstone,
        test_the_tk_badge_has_a_colour_for_every_rank,
        test_the_ranked_panel_never_mentions_a_hidden_rating,
        test_no_live_screen_quotes_a_figure_from_the_hidden_ladder,
        test_comp_rr_resolves_in_every_language,
    ]:
        _run(fn)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
