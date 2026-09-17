#!/usr/bin/env python3
"""Headless test for the no-show ban's snapshot fields and its self-clearing expiry one-shot.  Run:

    python3 tests/test_screen_penalty.py

The full suite (tests/test_hub.py) needs Tk to import hub.competitive; this box has none, so we
inject fake ``tkinter*`` / ``_tkinter`` modules into sys.modules first and then import the screen
module against them. It asserts the additive ``penalty.until`` field the live JS countdown reads,
that it stays back-compat (``left`` / ``reason`` unchanged), and that ``_arm_penalty_expiry`` arms a
single self-clearing one-shot that flips ``can_find`` true at the deadline. No Tk, no network.
"""
import os
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_STATE = tempfile.mkdtemp(prefix="hub-pen-state-")
os.environ.setdefault("HUB_STATE_DIR", _STATE)
sys.path.insert(0, str(REPO))

# --- fake Tk so hub.competitive (which does `import tkinter`) imports on a box with no _tkinter ---
for name in ("_tkinter", "tkinter", "tkinter.ttk", "tkinter.font", "tkinter.messagebox"):
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
# a couple of attributes competitive.py reads at import time off the tkinter namespace
_tk = sys.modules["tkinter"]
for attr in ("Tk", "Toplevel", "Frame", "Label", "Button", "StringVar", "Canvas", "Entry"):
    setattr(_tk, attr, type(attr, (), {}))

from hub import competitive as C                       # noqa: E402
from hub.webui.screens.competitive import comp_snapshot  # noqa: E402

RESULTS = []


class _FakeApp:
    """The slice of HubApp the session reads. The ranked pack is INSTALLED because these tests are
    about bans, and `can_find` is false without it (competitive.py Session.gamemode_installed)."""

    def __init__(self):
        self.state = {"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0.0"}}, "auth": None}
        self.catalogue = None
        self.busy = False


class _FakePanel:
    """The bare panel protocol a MockSession touches: a no-op scheduler + change hook."""

    def __init__(self):
        self.pending = []      # (ms, fn) queued by after(), so a test can fire them by hand
        self.changes = 0
        self.app = _FakeApp()

    def after(self, ms, fn):
        self.pending.append((ms, fn))
        return fn

    def post(self, fn):
        """The bridge's "run this on the UI thread". Straight through: these tests ARE the UI
        thread, and a queue would only mean every assertion had to pump it first."""
        return fn()

    def on_change(self):
        self.changes += 1

    def pump(self):
        due, self.pending = self.pending, []
        for _ms, fn in due:
            fn()
        return len(due)


def _session():
    panel = _FakePanel()
    s = C.MockSession(panel)
    s.phase = "idle"
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6}
    return panel, s


def test_until_present_when_banned():
    """A live ban puts the absolute epoch deadline in penalty.until, alongside the old left/reason."""
    panel, s = _session()
    deadline = time.time() + C.NO_SHOW_BAN_SECONDS
    s.penalty_until = deadline
    s.penalty_reason = "no_show"
    pen = comp_snapshot(s, panel)["penalty"]
    assert "until" in pen, "penalty dict must carry the new `until` field"
    assert abs(pen["until"] - deadline) < 0.01, pen["until"]
    assert isinstance(pen["until"], float)
    assert pen["left"] > 0 and pen["reason"] == "no_show"   # back-compat fields unchanged


def test_until_zero_when_not_banned():
    """No ban -> until is 0 (falsy), so the JS never starts a countdown; left/reason stay too."""
    panel, s = _session()
    pen = comp_snapshot(s, panel)["penalty"]
    assert pen["until"] == 0
    assert pen["left"] == 0 and pen["reason"] == ""


def test_expired_ban_clears_until_and_frees_find():
    """A deadline already in the past: banned_left() zeroes it during the snapshot, so until is 0
    and can_find is true again (no stale ban left behind)."""
    panel, s = _session()
    s.penalty_until = time.time() - 1     # expired a second ago
    s.penalty_reason = "no_show"
    comp = comp_snapshot(s, panel)
    assert comp["penalty"]["until"] == 0
    assert comp["penalty"]["left"] == 0
    assert comp["can_find"] is True


def test_arm_penalty_expiry_is_single_and_self_clearing():
    """Arming schedules one one-shot; re-arming does not stack a second live timer; and once the
    deadline passes the fired one-shot clears the ban and rebuilds (can_find flips true)."""
    panel, s = _session()
    s.penalty_until = time.time() + C.NO_SHOW_BAN_SECONDS
    s._arm_penalty_expiry()
    assert len(panel.pending) == 1, "one one-shot armed"
    gen1 = s._penalty_gen
    # a second ban re-arms: the generation advances so the first timer becomes a no-op when it fires
    s.penalty_until = time.time() + C.NO_SHOW_BAN_SECONDS
    s._arm_penalty_expiry()
    assert s._penalty_gen == gen1 + 1
    # firing the STALE first timer must do nothing (wrong generation)
    before = panel.changes
    panel.pending[0][1]()                 # the first (stale) one-shot
    assert panel.changes == before, "a stale one-shot must not rebuild"
    # now make the ban already-expired and fire the current timer: it clears and rebuilds
    s.penalty_until = time.time() - 1
    panel.pending[-1][1]()                # the latest (current-generation) one-shot
    assert s.penalty_until == 0.0, "the fired one-shot let banned_left() clear the ban"
    assert panel.changes > before, "expiry rebuilds the snapshot"
    assert comp_snapshot(s, panel)["can_find"] is True


# ------------------------------------------------------- the Penalties explainer (2026-09-16)
# Sam: "in the competitive tab to the left of competitive info lets add a penalties button and
# clicking it will open a screen, exactly like competitive info, that gives more details in what
# warrants issuing a queue and RR penalty and what the time penalties are."
def _hello(session, event):
    """Run the REAL hello handler against a MockSession.

    `on_live_event` lives on LiveSession, which wants a client and a socket; the parsing this
    tests does not. Calling it unbound exercises the branch that actually ships rather than a
    paraphrase of it, which is the whole point - a guard rewritten into the test is a guard that
    cannot fail when the shipping one is deleted.
    """
    from hub.competitive import LiveSession
    return LiveSession.on_live_event(session, event)


_RULES = {"rungs": [300, 900, 1800, 3600, 7200, 14400], "decay_seconds": 86400, "rr": 25,
          "accept_seconds": 30, "connect_seconds": 300,
          "team_kill": {"enforced": False, "limit": 3, "early_seconds": 3}}


def test_the_rules_are_the_servers_or_absent():
    """Every number on the panel is an env dial on the service (COMP_NO_SHOW_BAN_SECONDS,
    COMP_NO_SHOW_RR, ...). A hub that shipped its own copy would go on explaining the old ladder
    the day one was turned, so a session that has not been told the rules publishes None and the
    JS draws no button at all."""
    panel, s = _session()
    assert comp_snapshot(s, panel)["penalties"] is None, "a hub must not invent the ban ladder"
    _hello(s, {"type": "hello", "penalties": _RULES})
    assert comp_snapshot(s, panel)["penalties"] == _RULES


def test_a_hello_without_the_rules_does_not_wipe_the_ones_we_have():
    """`rungs` is the test, because a block with no ban ladder cannot answer the question the
    panel exists to answer - and a reconnect to an older service must not blank a panel that was
    working a second ago."""
    panel, s = _session()
    _hello(s, {"type": "hello", "penalties": _RULES})
    _hello(s, {"type": "hello"})                       # an older service, or a partial hello
    _hello(s, {"type": "hello", "penalties": {"rr": 25}})       # no rungs -> not the rules
    assert comp_snapshot(s, panel)["penalties"] == _RULES


def test_the_button_toggles_and_the_two_panels_are_exclusive():
    """Both are modals over the same hero. Opening one from behind the other would stack two
    dialogs whose Escape handlers close to the same screen, so opening either closes the other."""
    from hub.webui.screens.competitive import _toggle_penalties, _toggle_rank_info

    panel, s = _session()
    panel.session = s
    assert comp_snapshot(s, panel)["penalties_open"] is False

    _toggle_penalties(panel)
    assert comp_snapshot(s, panel)["penalties_open"] is True
    _toggle_penalties(panel)
    assert comp_snapshot(s, panel)["penalties_open"] is False, "the same button closes it"

    _toggle_penalties(panel)
    _toggle_rank_info(panel)
    snap = comp_snapshot(s, panel)
    assert snap["rank_info_open"] is True and snap["penalties_open"] is False
    _toggle_penalties(panel)
    snap = comp_snapshot(s, panel)
    assert snap["penalties_open"] is True and snap["rank_info_open"] is False


def test_every_string_the_panel_interpolates_survives_translation():
    """A placeholder lost in one language raises there and only there, which is exactly the bug
    nobody finds. Same guard the history screens have."""
    from hub import i18n

    needs = {"pen_offence_no_show": "{time}", "pen_decay": "{time}",
             "pen_rung": "{n}", "pen_rung_last": "{n}", "pen_rr": "{n}",
             "pen_dur_sec": "{n}", "pen_dur_min": "{n}",
             "pen_dur_hour": "{n}", "pen_dur_day": "{n}"}
    for code in i18n.CODES:
        table = i18n.STRINGS[code]
        for key, mark in needs.items():
            assert key in table, (code, key)
            assert mark in table[key], (code, key, mark)
        # The general team-killing policy is displayed without detection thresholds.
        assert table["pen_offence_tk"].strip(), code
        assert "{" not in table["pen_offence_tk"], code


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                RESULTS.append((name, None))
            except Exception:              # noqa: BLE001
                RESULTS.append((name, traceback.format_exc()))
    ok = [n for n, e in RESULTS if e is None]
    bad = [(n, e) for n, e in RESULTS if e is not None]
    for n, e in bad:
        print("FAIL", n, "\n", e)
    print("%d passed, %d failed" % (len(ok), len(bad)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(_run())
