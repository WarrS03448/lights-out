#!/usr/bin/env python3.12
"""Headless tests for the PROFILE screen's Python half.  Run:  python3.12 tests/test_screen_profile.py

No Tk, no pywebview, no network: the profile slice is pure — it feeds the player's history rows
through competitive.profile_stats() and serialises the result honestly. These tests pin the honesty
contract the screen depends on: with NO data every derived number stays null/0/"" (drawn as a
placeholder), and with real rows the numbers are the real ones. They mirror the shape of the
webui snapshot tests in tests/test_hub.py but target only this screen's slice.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-state-profile-"))
sys.path.insert(0, str(REPO))

from hub import i18n                                    # noqa: E402
from hub.webui.screens import profile as P              # noqa: E402
from hub.webui.screens import SCREEN_SNAPSHOTS, SCREEN_VERBS  # noqa: E402

RESULTS = []


class _FakeSession:
    """The handful of attributes the profile slice reads off a session (no logic)."""

    def __init__(self, me=None, history=None, loading=False, error=""):
        self.me = me
        self.history = history
        self.history_loading = loading
        self.history_error = error


class _RecorderSession:
    """Records load_history() calls so the refresh verb can be checked."""

    def __init__(self):
        self.calls = []

    def load_history(self, force=False):
        self.calls.append(("load_history", force))


class _FakePanel:
    """post(fn) runs inline (the verb's marshalling is exercised, not the threading)."""

    def __init__(self, session):
        self.session = session

    def post(self, fn):
        fn()


REAL_ROWS = [
    {"won": True,  "map": "Vault", "outcome": "played", "ended": 1789000000000, "elo": 21,
     "side": "attack", "host": True},
    {"won": False, "map": "Vault", "outcome": "played", "ended": 1788000000000, "elo": -18},
    {"won": True,  "map": "Dock",  "outcome": "played", "ended": 1787000000000, "elo": 19},
    {"outcome": "cancelled", "blamed": True, "reason": "no_show", "elo": -30},
]

ME = {"name": "Sam", "steam_id": "76561198000999000", "level": 6,
      "avatar": "https://avatars.example/abc_full.jpg"}


def _slice(session):
    """The profile slice as it ships in the snapshot (and prove it is JSON-serialisable)."""
    snap = P.profile_snapshot(session, _FakePanel(session))
    json.dumps(snap)                       # must not raise — it goes over evaluate_js as JSON
    return snap["profile"]


# ---------------------------------------------------------------- tests
def test_null_stats_render_as_placeholders():
    """No history yet: every derived number is null/0/"" — the screen shows a placeholder and the
    slice never invents a figure (win_rate is None, not a fabricated 0%)."""
    i18n.set_language("en")
    prof = _slice(_FakeSession(me=ME, history=None))

    assert prof["signed_in"] is True
    assert prof["persona"] == "Sam"
    assert prof["tier"], "a signed-in player has a named tier label"
    assert prof["history_loaded"] is False, "history was never fetched"

    s = prof["stats"]
    assert s["win_rate"] is None, "no scoreboard yet -> win_rate stays null, not 0%"
    assert s["top_map"] == "", "no played match -> no top map"
    assert s["played"] == 0 and s["wins"] == 0 and s["losses"] == 0
    assert s["recorded"] == 0
    assert s["maps"] == [] and s["form"] == []
    assert prof["recent"] == []

    # rank rating / peak / region have no backend — honest nulls, never invented
    assert prof["rr"] is None and prof["rr_max"] is None
    assert prof["peak_tier"] is None and prof["region"] is None


def test_real_stats_render():
    """Real history rows: the numbers are the real ones from profile_stats — computed, not faked."""
    i18n.set_language("en")
    prof = _slice(_FakeSession(me=ME, history=list(REAL_ROWS)))

    s = prof["stats"]
    assert s["played"] == 3
    assert s["wins"] == 2 and s["losses"] == 1
    assert s["win_rate"] == 67, "2 of 3 decided -> 67%"
    assert s["cancelled"] == 1 and s["at_fault"] == 1
    assert s["top_map"] == "Vault"
    assert s["recorded"] == 4
    # maps normalised to explicit {map,count} objects, most-played first
    assert s["maps"][0] == {"map": "Vault", "count": 2}
    # form carries the real per-match outcomes, newest first (blamed cancel -> "fault")
    assert s["form"] == ["win", "loss", "win", "fault"]

    # recent list is straight from the rows: won stays a real bool, never coerced
    assert len(prof["recent"]) == 4
    assert prof["recent"][0]["map"] == "Vault" and prof["recent"][0]["won"] is True
    assert prof["recent"][1]["won"] is False
    assert prof["recent"][3]["cancelled"] is True and prof["recent"][3]["blamed"] is True
    assert prof["history_loaded"] is True


def test_signed_out_slice_is_honest():
    """Signed out: no identity, no tier, nothing invented — the screen shows its empty state."""
    i18n.set_language("en")
    prof = _slice(_FakeSession(me=None, history=None))
    assert prof["signed_in"] is False
    assert prof["persona"] == "" and prof["tier"] is None
    assert prof["stats"]["win_rate"] is None


def test_ladder_flags_the_current_tier():
    """The ladder shows all eight named tiers with the player's current one flagged."""
    i18n.set_language("en")
    prof = _slice(_FakeSession(me=ME, history=None))
    names = [r["name"] for r in prof["ladder"]]
    assert names == P.TIER_ORDER
    current = [r["name"] for r in prof["ladder"] if r["current"]]
    assert current == [prof["tier"]], "exactly the player's tier is flagged current"


def test_strings_shipped_for_every_language():
    """The slice ships the active language's strings, and every language is complete (English fills
    any gap so no key ever renders as its raw name)."""
    en_keys = set(P.PROFILE_STRINGS["en"])
    for lang in ("en", "de", "es", "fr", "pt", "ru", "zh"):
        i18n.set_language(lang)
        prof = _slice(_FakeSession(me=ME, history=None))
        shipped = prof["strings"]
        assert en_keys.issubset(set(shipped)), "%s is missing keys" % lang
        assert all(shipped[k] for k in en_keys), "%s has an empty string" % lang
    # a translated language differs from English somewhere (proves the merge picks the lang table)
    i18n.set_language("de")
    de = _slice(_FakeSession(me=ME, history=None))["strings"]
    assert de["profile_refresh"] != P.PROFILE_STRINGS["en"]["profile_refresh"]
    i18n.set_language("en")


def test_the_level_numeral_is_gone_and_the_emblem_is_beside_the_name():
    """Sam, 2026-09-16: "it still shows two number sixes. one to left of my name and one in the
    rank box. remove the one at the top and replace the six next to the user's name with the rank
    icon of the respective rank of the user."

    `level` is the OLD numeric ladder - it is not what a player is shown anywhere else, and this
    screen printed it twice. The plate beside the name carries the rank's emblem now, and the rank
    card carries words only. A file scan, because the hub's UI tests need xvfb and skip here."""
    js = (REPO / "hub" / "webui" / "static" / "screens" / "profile.js").read_text(encoding="utf-8")
    css = (REPO / "hub" / "webui" / "static" / "screens" / "profile.css").read_text(encoding="utf-8")
    assert "prof.level" not in js, "no numeral may be drawn from the old level ladder"
    assert 'el("div", "profile-id-badge")' in js and ".profile-id-badge" in css
    assert "profile-rank-badge" not in js and ".profile-rank-badge" not in css, \
        "the card's own emblem moved up beside the name; two would say it twice"
    # and with no rank to draw, the plate falls back to the player's INITIALS, not a number
    assert "ui.initials(prof.persona || auth.persona)" in js


def test_a_placing_player_is_never_given_a_tier_they_have_not_earned():
    """`level` survives on the session from before the placements began, so the card's fallback
    label could name a rank the player does not hold - "Abyss", right under a line saying their
    rating is not tracked yet. While placing it says Unranked."""
    js = (REPO / "hub" / "webui" / "static" / "screens" / "profile.js").read_text(encoding="utf-8")
    assert "(!auth.placing && tier) ? tier : pt(\"profile_unranked\")" in js


def test_registered_with_the_core():
    """The screen registers its snapshot contributor and its one refresh verb."""
    assert "profile" in SCREEN_SNAPSHOTS
    assert "refresh_profile" in SCREEN_VERBS


def test_refresh_verb_forces_a_history_fetch():
    """refresh_profile is a 1:1 passthrough: it re-asks the server for the player's matches."""
    rec = _RecorderSession()
    panel = _FakePanel(rec)
    SCREEN_VERBS["refresh_profile"](panel)
    assert rec.calls == [("load_history", True)], "refresh must force a re-fetch"


def test_slice_merges_into_the_full_snapshot():
    """state_snapshot merges the profile slice under the top-level ``profile`` key."""
    from hub.webui.snapshot import state_snapshot

    class _App:
        state = {"installed": {}, "auth": None}
        catalogue = None

    class _Sess(_FakeSession):
        # state_snapshot's shared slices read a few more attributes; give it just enough.
        phase = "idle"
        online = 5
        connected = True
        link_url = ""
        link_code = ""

        def locked_in(self):
            return False

    i18n.set_language("en")
    sess = _Sess(me=ME, history=list(REAL_ROWS))
    snap = state_snapshot(sess, _FakePanel(sess))
    assert "profile" in snap
    assert snap["profile"]["stats"]["wins"] == 2
    json.dumps(snap)


def _run(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, True))
        print("ok   %s" % fn.__name__)
    except Exception as exc:                       # noqa: BLE001
        RESULTS.append((fn.__name__, False))
        import traceback
        print("FAIL %s\n%s" % (fn.__name__, traceback.format_exc()))


def main():
    for fn in [
        test_null_stats_render_as_placeholders,
        test_real_stats_render,
        test_signed_out_slice_is_honest,
        test_ladder_flags_the_current_tier,
        test_strings_shipped_for_every_language,
        test_the_level_numeral_is_gone_and_the_emblem_is_beside_the_name,
        test_a_placing_player_is_never_given_a_tier_they_have_not_earned,
        test_registered_with_the_core,
        test_refresh_verb_forces_a_history_fetch,
        test_slice_merges_into_the_full_snapshot,
    ]:
        _run(fn)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
