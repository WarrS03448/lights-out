#!/usr/bin/env python3.12
"""Headless tests for the competitive HERO — the red panel.  Run:

    python3.12 tests/test_screen_hero.py

The hero is where Sam's direction lands most often, and every rule below is one of his. They are
file-scan and i18n tests rather than DOM tests, in the shape tests/test_screen_matchflow.py uses:
the hub's UI tests need xvfb and skip on this machine, so what can be checked without a display is
checked here.

  * the rank block sits UNDER the player's name and is the big thing on the panel - "move where it
    shows your rank to where the matches, wins, losses, winrate is and make it big. thats what
    people care about the most" (2026-09-16);
  * NO STATS on this page at all: neither the four figures nor the rotating spotlight cards that
    briefly lived above the button - "lets remove the random stats and all stats on this page"
    (2026-09-16). The record belongs to the Profile screen, which draws the whole history;
  * a player who is still placing sees the count in one shape: "0/5 Placement Matches Complete";
  * the version line sits between the Find match button and "you do not need Bodycam open";
  * the rank explainer's button costs the hero no height, because the row it used to live in was
    a gap between the window chrome and the red panel.
"""
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-hero-state-"))
sys.path.insert(0, str(REPO))

from hub import i18n                                   # noqa: E402

RESULTS = []
STATIC = REPO / "hub" / "webui" / "static" / "screens"
JS = (STATIC / "competitive.js").read_text(encoding="utf-8")
CSS = (STATIC / "competitive.css").read_text(encoding="utf-8")


def hero_body():
    """Just the hero() function: from its own line to the rank block that follows it."""
    start = JS.index("    function hero() {")
    end = JS.index("    function rankBlock(auth) {")
    return JS[start:end]


# ---------------------------------------------------------------- the shape of the panel
def test_the_rank_comes_after_the_name_and_before_the_action():
    body = hero_body()
    name = body.index('el("div", "hero-name"')
    rank = body.index("rankBlock(auth)")
    action = body.index("heroAction(comp, party)")
    assert name < rank < action, (name, rank, action)


def test_the_rank_block_is_the_big_thing_on_the_panel():
    """Big is the whole point of the move, so it is worth a number: the rank name is within reach
    of the player's own name, and the emblem is nearly double the 56px it used to be."""
    rankname = float(re.search(r"\.hero-rankname\s*\{[^}]*font-size:\s*([\d.]+)px", CSS, re.S).group(1))
    emblem = float(re.search(r"\.rank-emblem\s*\{[^}]*width:\s*([\d.]+)px", CSS, re.S).group(1))
    assert rankname >= 28, rankname
    assert emblem >= 84, emblem
    # ...and it is a row, so the badge stands beside the words rather than on top of them.
    block = CSS[CSS.index(".hero-rank {"):]
    assert "flex-direction: row" in block[:260], block[:260]


def test_no_stats_on_this_page():
    """Both kinds: the four figures and the rotating cards. Nothing that draws either may survive
    anywhere in this screen - the JS that built them, the CSS that styled them, or the snapshot
    slice and verb that fed them."""
    for gone in ('el("div", "hero-stats")', "statCell(", "spotlight(", "load_spotlight",
                 "spot-track", "spot-card"):
        assert gone not in JS, "%s must be gone from competitive.js" % gone
    for gone in (".hero-stats", ".hero-stat ", ".spotlight", ".spot-view", ".spot-dot"):
        assert gone not in CSS, "%s must be gone from competitive.css" % gone
    screen_py = (REPO / "hub" / "webui" / "screens" / "competitive.py").read_text(encoding="utf-8")
    assert "spotlight" not in screen_py, "the snapshot slice and its verb must be gone"
    comp_py = (REPO / "hub" / "competitive.py").read_text(encoding="utf-8")
    assert "spotlight_stats" not in comp_py, "the arithmetic must be gone with its only caller"
    # The strings went too: an i18n table that keeps words nothing prints is a table nobody trusts.
    assert not [k for k in i18n.STRINGS["en"] if k.startswith("comp_spot_")], "stale spotlight keys"


# ---------------------------------------------------------------- the placement line
def test_the_placement_line_counts_down_to_calibration():
    """Sam gave the shape twice on 2026-09-16, and the second one is the one that ships: "X More
    placement matches required for rank calibration". It replaced "0/5 Placement Matches Complete",
    which said how far along you were but never what the matches were FOR - and a player with no
    badge is asking both."""
    line = i18n.STRINGS["en"]["comp_placements_left"]
    assert line == "{n} more placement matches required for rank calibration", line
    assert line.format(n=3) == "3 more placement matches required for rank calibration"
    # ...and the last one is one MATCH: the hero asks for the count's form (tests/test_i18n_plurals.py)
    assert i18n.trn("en", "comp_placements_left", 1) == "1 more placement match required for rank calibration"
    # every language keeps the number, or the hero raises KeyError in that language alone
    for code in i18n.CODES:
        assert "{n}" in i18n.STRINGS[code]["comp_placements_left"], code
    # The progress string went with its only caller: an i18n table that keeps words nothing prints
    # is a table nobody trusts.
    assert "comp_placements_progress" not in i18n.STRINGS["en"], "stale progress key"


def test_the_placement_line_is_drawn_as_its_own_headline():
    body = JS[JS.index("    function rankBlock(auth) {"):JS.index("    function heroAction(comp, party) {")]
    # Both arms: the count when the ladder has told us how many placements there are, and the
    # plain "play your placement matches" when it has not.
    assert body.count('"hero-placements"') == 2, body.count('"hero-placements"')
    assert ".hero-placements" in CSS
    # It is NOT the small RR line any more - that class is for the figure under a real rank.
    assert 'el("div", "hero-rr", t("comp_placements' not in body


# ---------------------------------------------------------------- what earlier rounds fixed
def test_the_version_line_sits_under_find_match():
    """Under the button (2026-09-16). It used to be a bordered card ABOVE it, which pushed the one
    thing the player came here to press a long way down the panel every time it appeared.

    This asserted a THREE-way order and the third anchor is gone. `t("comp_ready_state")` was the
    "you do not need Bodycam open" paragraph that sat below the version line, and 714f791 deleted
    it on purpose: the rule is enforced now rather than explained, because find_match refuses
    while Bodycam is up and cues "close your game" for three seconds. That commit added tests for
    the new behaviour in test_hub.py and test_screen_matchflow.py but never updated this file, so
    the assertion went on hunting a string that no longer exists and failed against a screen that
    was perfectly correct - red for a fix, which is the worst kind of stale test.

    The ordering it actually guards only ever needed two anchors. The third is now pinned as an
    ABSENCE, which is the fact that replaced it.
    """
    action = JS.split("// idle: the Find match call to action", 1)[1].split("function outdatedBox", 1)[0]
    # The STRING, not btn("btn-find"): the install gate reuses that class for its own button
    # higher up the same block, so the class alone matches the wrong button (and always did -
    # this test has been measuring the gate's position, not Find match's).
    find = action.index('t("comp_find_match")')
    outdated = action.index("outdatedBox(comp.outdated)")
    assert find < outdated, (find, outdated)
    # The paragraph stays gone. Bringing it back means bringing back advice nobody needed, and
    # the enforcement in Session.find_match is what replaced it.
    assert "comp_ready_state" not in action,         "the idle hero explains the rule again instead of enforcing it (see 714f791)"


def test_the_gap_above_the_hero_is_gone():
    """The rank explainer's button was a full-width row ABOVE the split, and that row was the gap
    between the window chrome and the red panel."""
    assert 'el("div", "comp-bar")' not in JS and 'el("div", "comp-screen")' not in JS
    assert ".comp-bar" not in CSS and ".comp-screen" not in CSS
    assert "rank-info-btn" in JS and ".rank-info-btn" in CSS
    # The position moved off the button and onto the strip that holds it when Penalties
    # arrived beside Competitive info: two buttons both pinned to right:16px would sit on
    # top of each other. The rule being defended is unchanged - neither button may take
    # height from the hero.
    assert 'el("div", "hero-tools")' in JS and ".hero-tools" in CSS
    assert "position: absolute" in CSS.split(".hero-tools {", 1)[1][:200], \
        "the strip must not take height from the hero"
    assert "position:" not in CSS.split(".rank-info-btn {", 1)[1].split("}", 1)[0], \
        "the buttons position themselves inside the strip, not against the hero"


def test_the_coin_has_two_faces():
    """A flat disc with one face shows the same letter mirrored on the way round; the coin turns
    end over end onto a real back face."""
    assert 'el("span", "coin-face", faceLetter(coin.result))' in JS
    assert '"coin-face back"' in JS
    assert "rotateX" in CSS.split("@keyframes hubflip", 1)[1][:120]
    assert "backface-visibility: hidden" in CSS.split(".coin-face {", 1)[1][:400]


def _run(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, True))
        print("ok   %s" % fn.__name__)
    except Exception:                       # noqa: BLE001
        RESULTS.append((fn.__name__, False))
        print("FAIL %s\n%s" % (fn.__name__, traceback.format_exc()))


def main():
    for fn in [
        test_the_rank_comes_after_the_name_and_before_the_action,
        test_the_rank_block_is_the_big_thing_on_the_panel,
        test_no_stats_on_this_page,
        test_the_placement_line_counts_down_to_calibration,
        test_the_placement_line_is_drawn_as_its_own_headline,
        test_the_version_line_sits_under_find_match,
        test_the_gap_above_the_hero_is_gone,
        test_the_coin_has_two_faces,
    ]:
        _run(fn)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
