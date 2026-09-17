#!/usr/bin/env python3.12
"""Headless tests for the Leaderboard screen's Python half. Run: python3.12 tests/test_screen_leaderboard.py

The valuable half of this screen is testable with no webview (docs/ui-redesign-plan.md, "Testing"):
the snapshot slice the JS renders from. There is NO leaderboard/ranking endpoint yet (server/live.cjs
has no such route; hub/competitive.py notes "there is no ranking service"), so the contract these
tests pin is the HONEST placeholder: available:false + rows:[], the Global/Friends scopes, and a
complete per-language string set carried in the slice. When a real endpoint is wired into
leaderboard._board_from_backend, `available` flips to True and these tests still hold.

Imports no pywebview and no Tk. Runnable directly or under pytest.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hub import i18n
from hub.webui import screens as screens_pkg
from hub.webui.screens import leaderboard as LB


class _StubSession:
    """The leaderboard slice reads nothing off the session today (no rank service), so a bare
    stub is enough; when a backend read is added, this is where it would be exercised."""


class _StubPanel:
    pass


def test_registered_in_snapshot_registry():
    """The screen contributes through register_snapshot, so the assembler picks it up by name."""
    assert "leaderboard" in screens_pkg.SCREEN_SNAPSHOTS
    assert screens_pkg.SCREEN_SNAPSHOTS["leaderboard"] is LB.snapshot


def test_slice_is_honest_placeholder_and_serialisable():
    """With no rank service, the slice must be an honest empty board — never fabricated rows."""
    i18n.set_language("en")
    slice_ = LB.snapshot(_StubSession(), _StubPanel())
    json.dumps(slice_)                                  # JSON-serialisable, never raises
    assert set(slice_.keys()) == {"leaderboard"}
    lb = slice_["leaderboard"]
    assert lb["available"] is False
    assert lb["rows"] == []
    assert lb["you"] is None
    assert lb["scope"] == "global"
    assert lb["scopes"] == ["global", "friends"]
    assert lb["season"] is None


def test_slice_ships_active_language_strings():
    """Strings ride in the slice (i18n.py is not edited); the active language is shipped, English-
    filled, so the JS can look up every key it references by name."""
    keys = {"title", "go_to_me", "tab_global", "tab_friends",
            "col_rank", "col_player", "col_tier", "col_rr", "col_matches", "col_winrate",
            "you_label", "empty_title", "empty_global", "empty_friends"}

    i18n.set_language("en")
    en = LB.snapshot(_StubSession(), _StubPanel())["leaderboard"]["strings"]
    assert keys <= set(en)
    assert en["tab_global"] == "Global" and en["tab_friends"] == "Friends"

    # every shipped language resolves every key (English-filled), and translates the tab labels
    for code in i18n.CODES:
        i18n.set_language(code)
        strings = LB.snapshot(_StubSession(), _StubPanel())["leaderboard"]["strings"]
        missing = keys - set(strings)
        assert not missing, "lang %s missing leaderboard keys: %s" % (code, sorted(missing))
        assert all(strings[k] for k in keys), "lang %s has an empty leaderboard string" % code
    i18n.set_language("en")


def test_every_language_defines_the_full_key_set():
    """The per-language STRINGS dicts stay in lockstep — the analogue of i18n's completeness test,
    but scoped to this screen's own vocabulary."""
    reference = set(LB.STRINGS["en"])
    for code in i18n.CODES:
        assert code in LB.STRINGS, "leaderboard STRINGS missing language: %s" % code
        assert set(LB.STRINGS[code]) == reference, \
            "leaderboard STRINGS[%s] keys differ from en: %s" % (code, set(LB.STRINGS[code]) ^ reference)


def test_state_snapshot_merges_the_leaderboard_slice():
    """End-to-end through the real assembler + a real WebPanel/LiveSession (no window, no Tk): the
    top-level snapshot carries the leaderboard slice next to the shared chrome, JSON-serialisable."""
    from hub.webui.panel import WebPanel
    from hub.webui.scheduler import InlineScheduler
    from hub.webui.snapshot import state_snapshot

    class _FakeApp:
        def __init__(self):
            self.state = {"installed": {}, "auth": None}
            self.catalogue = None

    i18n.set_language("en")
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    snap = state_snapshot(panel.session, panel)
    json.dumps(snap)
    assert "leaderboard" in snap
    assert snap["leaderboard"]["available"] is False
    assert snap["leaderboard"]["rows"] == []
    assert snap["leaderboard"]["strings"]["title"]


def test_rows_come_from_the_server_and_never_from_nothing():
    """A board that could not be read must not claim nobody is ranked.

    `available` is the SERVER's word. None rows render the "not live yet" state; an empty LIST
    renders an empty table. Collapsing the two would have the screen assert something false the
    first time the store hiccups."""
    from hub.webui.panel import WebPanel
    from hub.webui.scheduler import InlineScheduler
    from hub.webui.screens import leaderboard as L

    class _FakeApp:
        def __init__(self):
            self.state = {"installed": {}, "auth": None}
            self.catalogue = None

    i18n.set_language("en")
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    sess = panel.session

    rows, you = L._board_from_backend(sess)
    assert rows is None and you is None, "unavailable must not look like an empty board"
    assert L.snapshot(sess, panel)["leaderboard"]["available"] is False

    sess.board_available = True
    sess.board_rows = ()
    rows, you = L._board_from_backend(sess)
    assert rows == [] and you is None, "an empty board is a real answer"
    assert L.snapshot(sess, panel)["leaderboard"]["available"] is True

    sess.board_rows = ({"rank": 1, "steam_id": "76561198000999000", "persona": "Sam",
                        "rank_name": "Operator", "division": 3, "rr": 42, "matches": 20,
                        "wins": 12, "win_rate": 60, "is_you": True},)
    sess.board_you = sess.board_rows[0]
    rows, you = L._board_from_backend(sess)
    assert rows[0]["tier"] == "Operator 3", "the board shows the same rank the hero does"
    assert rows[0]["rr"] == 42 and rows[0]["win_rate"] == "60%"
    assert rows[0]["is_you"] is True and you is not None
    # the capstone has no division to print
    sess.board_rows = ({"rank": 1, "steam_id": "1", "persona": "X", "rank_name": "Reaper",
                        "division": 0, "rr": 220, "top": True, "matches": 99, "win_rate": 70},)
    assert L._board_from_backend(sess)[0][0]["tier"] == "Reaper"


TESTS = [
    test_registered_in_snapshot_registry,
    test_slice_is_honest_placeholder_and_serialisable,
    test_slice_ships_active_language_strings,
    test_every_language_defines_the_full_key_set,
    test_state_snapshot_merges_the_leaderboard_slice,
    test_rows_come_from_the_server_and_never_from_nothing,
]



def main():
    failed = 0
    for fn in TESTS:
        try:
            fn()
            print("ok   %s" % fn.__name__)
        except Exception as exc:                        # noqa: BLE001 — report and continue
            failed += 1
            print("FAIL %s: %s" % (fn.__name__, exc))
    if failed:
        print("\n%d/%d failed" % (failed, len(TESTS)))
        sys.exit(1)
    print("\nall %d passed" % len(TESTS))


if __name__ == "__main__":
    main()
