"""Headless tests for the Bug report screen (hub/webui/screens/bugreport.py + the session half).

Sam, 2026-09-16: a Bug report button next to Settings, opening a screen that files a bug against
the hub; the report reaches the admin console with the reporter's steam id and name; and one
report every five seconds.

The valuable half of the web UI is testable without a webview (docs/ui-redesign-plan.md: Testing):
the snapshot slice and the bridge verbs run against a real LiveSession driven by a fake live
client, asserting on the JSON dict and on what actually reached the wire - the same shape as
tests/test_screen_settings.py. No pywebview, no Tk, no window.

THE ONE THING THESE TESTS EXIST FOR is the five seconds. A cooldown drawn in the page is not a
cooldown: there are two UIs over this session and the web one is a page that can be reloaded. So
every test below asks what reached the CLIENT, never what the button looked like.

Run directly (``.venv/Scripts/python tests/test_screen_bugreport.py``) or import the functions.
"""
import os
import sys
import tempfile
import time

os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-bugreport-test-"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import competitive as C                        # noqa: E402
from hub import i18n                                    # noqa: E402
from hub.webui.bridge import Api                        # noqa: E402
from hub.webui.panel import WebPanel                    # noqa: E402
from hub.webui.scheduler import InlineScheduler         # noqa: E402
from hub.webui.snapshot import state_snapshot           # noqa: E402


# ---------------------------------------------------------------- harness
class _FakeApp:
    """The minimal `app` a WebPanel reads: persisted state, catalogue, game dir."""

    def __init__(self):
        self.state = {"installed": {}, "auth": None}
        self.catalogue = None
        self.game_dir = None


class _FakeClient:
    """Records every bug report that reached the wire, and answers with whatever `reply` says."""

    def __init__(self):
        self.sent = []
        self.reply = (200, {"ok": True, "recorded": True})

    def bug_report(self, text):
        self.sent.append(text)
        return self.reply


def _panel(signed_in=True):
    """A headless WebPanel with a LiveSession whose POSTs run inline (deterministic)."""
    i18n.set_language("en")
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    s = panel.session
    if signed_in:
        s.me = {"name": "Sam", "steam_id": "76561198000999000"}
    s.token = "tok"
    s.phase = "idle"
    s.connected = True
    s.client = _FakeClient()
    s._action = lambda call, on_result=None: (
        lambda r: on_result(*r) if on_result else None)(call())
    return panel, s


def _slice(panel):
    return state_snapshot(panel.session, panel)["bugreport"]


# ---------------------------------------------------------------- snapshot
def test_the_slice_is_json_and_says_who_the_report_goes_as():
    """The reporter's name and id are SHOWN, not asked for - the server takes them off the token
    (server/live.cjs submitBugReport), and a box to type an id into is a box to type someone
    else's into."""
    import json
    panel, s = _panel()
    slc = _slice(panel)
    json.dumps(slc)                                     # never raises
    assert slc["signed_in"] is True
    assert slc["persona"] == "Sam"
    assert slc["steam_id"] == "76561198000999000"
    assert slc["cooldown_seconds"] == int(C.BUG_COOLDOWN_SECONDS)
    assert slc["max"] == C.BUG_TEXT_MAX
    for key in ("text", "sending", "sent", "error", "cooldown_ms", "strings"):
        assert key in slc, key


def test_every_language_carries_the_same_keys():
    """The app's 7-language coverage: a screen that ships its own strings must ship all of them in
    all seven, or a player on one language gets raw keys on screen."""
    from hub.webui.screens import bugreport as mod
    en = set(mod._S["en"])
    assert set(mod._S) == set(i18n.CODES), set(mod._S) ^ set(i18n.CODES)
    for code in i18n.CODES:
        assert set(mod._S[code]) == en, (code, en ^ set(mod._S[code]))
    # ...and the nav label, which the CORE draws, lives in i18n.py where the core can reach it.
    for code in i18n.CODES:
        assert i18n.tr(code, "nav_bugreport") != "nav_bugreport", code


def test_the_screen_is_registered_and_reachable():
    """It has to be in _SCREEN_MODULES (frozen builds import statically), in the page, and in the
    nav - a screen wired into only two of the three is a button that goes nowhere."""
    from hub import paths
    from hub.webui import screens as S
    assert "bugreport" in S._SCREEN_MODULES
    assert "bugreport" in S.SCREEN_SNAPSHOTS
    assert "bug_send" in S.SCREEN_VERBS and "bug_set_text" in S.SCREEN_VERBS

    static = paths.webui_dir()
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "screens/bugreport.js" in html and "screens/bugreport.css" in html
    core = (static / "core.js").read_text(encoding="utf-8")
    assert 'view: "bugreport"' in core, "the nav has no Bug report button"
    assert (static / "screens" / "bugreport.js").exists()
    assert (static / "screens" / "bugreport.css").exists()


# ---------------------------------------------------------------- sending
def test_a_report_reaches_the_wire_and_empties_the_box():
    panel, s = _panel()
    Api(panel).bug_send("The queue button does nothing after a match ends.")
    assert s.client.sent == ["The queue button does nothing after a match ends."]
    slc = _slice(panel)
    assert slc["sent"] is True and slc["error"] == ""
    assert slc["text"] == "", "a filed report must not be left in the box to be sent twice"


def test_an_empty_box_is_never_sent():
    panel, s = _panel()
    Api(panel).bug_send("   \n  ")
    assert s.client.sent == []
    assert _slice(panel)["error"] == "empty"


def test_a_signed_out_hub_never_sends():
    """There would be no steam id and no name on the other end, which is the whole point of it."""
    panel, s = _panel(signed_in=False)
    Api(panel).bug_send("something broke")
    assert s.client.sent == []
    assert _slice(panel)["error"] == "signed_out"


# ---------------------------------------------------------------- the five seconds
def test_one_report_every_five_seconds():
    """Sam: "only allow the user to send 1 bug every 5 seconds". The SESSION refuses the second
    one, so it never reaches the wire at all - and the slice says how long is left so the page can
    put a number on the button."""
    panel, s = _panel()
    api = Api(panel)
    api.bug_send("first")
    assert s.client.sent == ["first"]
    left = _slice(panel)["cooldown_ms"]
    assert 4000 < left <= C.BUG_COOLDOWN_SECONDS * 1000, left

    api.bug_send("second, one moment later")
    assert s.client.sent == ["first"], "a second report got through inside the five seconds"
    assert _slice(panel)["error"] == "too_fast"


def test_the_cooldown_lets_go_after_five_seconds():
    panel, s = _panel()
    api = Api(panel)
    api.bug_send("first")
    # Wind the clock back rather than sleeping five seconds in a test suite.
    s.bug_last_sent = time.monotonic() - (C.BUG_COOLDOWN_SECONDS + 0.01)
    assert s.bug_cooldown_left() == 0.0
    assert _slice(panel)["cooldown_ms"] == 0
    api.bug_send("second, five seconds later")
    assert s.client.sent == ["first", "second, five seconds later"]


def test_the_cooldown_is_measured_on_a_monotonic_clock():
    """Not time.time(). The only question asked of the stamp is "how long since", and a wall clock
    that steps backwards over a DST change or an NTP correction would answer it with a cooldown
    lasting an hour."""
    panel, s = _panel()
    Api(panel).bug_send("first")
    assert abs(s.bug_last_sent - time.monotonic()) < 1.0, \
        "bug_last_sent is not on the monotonic clock"


def test_the_servers_refusal_wins_over_our_clock():
    """A restart clears our stamp and the same account can file from a second machine, so the
    server has the last word (live.cjs BUG_COOLDOWN_MS). Its 429 carries how long is left, and the
    session adopts it rather than offering a button that will be refused again."""
    panel, s = _panel()
    s.client.reply = (429, {"ok": False, "error": "too_fast", "retry_after_ms": 3200})
    Api(panel).bug_send("filed from the other machine a moment ago")
    slc = _slice(panel)
    assert slc["error"] == "too_fast"
    assert 2500 < slc["cooldown_ms"] <= 3300, slc["cooldown_ms"]


def test_a_failed_send_keeps_what_they_wrote():
    """It is the only copy. Losing a paragraph to a dropped connection is how somebody decides not
    to bother reporting the next one."""
    panel, s = _panel()
    s.client.reply = (0, {"error": "offline"})
    Api(panel).bug_send("a long and carefully written account of what went wrong")
    slc = _slice(panel)
    assert slc["sent"] is False and slc["error"]
    assert slc["text"] == "a long and carefully written account of what went wrong"


def test_the_draft_survives_leaving_the_screen():
    """Switching to Settings and back must not throw away what is half-written, which is why the
    draft lives on the session and not in the page."""
    panel, s = _panel()
    api = Api(panel)
    api.bug_set_text("half a sentence")
    api.set_view("settings")
    api.set_view("bugreport")
    assert _slice(panel)["text"] == "half a sentence"


def test_a_filed_report_leaves_the_box():
    """FOUND IN A BROWSER, not in this file, which is why it is pinned here now.

    The core saves every #app input's value before it rebuilds the screen and writes it back
    afterwards BY ID (core.js restoreInputs), so that a snapshot landing mid-sentence cannot wipe
    what somebody is typing. It does that unconditionally - so the empty box drawn after a
    successful send was refilled with the report that had just been filed, leaving it one press of
    Send away from being filed a second time.

    The fix is a counter that only moves when a report is FILED: the page hangs the textarea's id
    off it, so a send makes a NEW input that the restore skips, while every other render keeps the
    same id and the same protection."""
    from hub import paths
    panel, s = _panel()
    api = Api(panel)
    assert _slice(panel)["seq"] == 0

    api.bug_send("first")
    assert _slice(panel)["seq"] == 1, "a filed report must give the page a new box"

    # A refusal is NOT a new box: what they wrote is still in it and must stay.
    s.client.reply = (0, {"error": "offline"})
    s.bug_last_sent = 0.0
    api.bug_send("second")
    assert _slice(panel)["seq"] == 1, "a failed send must not throw away the box"

    js = (paths.webui_dir() / "screens" / "bugreport.js").read_text(encoding="utf-8")
    assert 'var boxId = "bugreport-text-" + (s.seq || 0);' in js, \
        "the page no longer hangs the box's id off the send counter"
    assert 'box.id = boxId;' in js


def test_the_countdown_is_not_a_telling_off():
    """Also found in a browser. The line under the button was keyed on "is the cooldown running"
    as well as on the error, so a report that had just been ACCEPTED was answered with "One report
    every 5 seconds - try again in a moment": a refusal, under the thing that worked. Only a send
    that was actually refused earns the warning; the countdown belongs on the button."""
    from hub import paths
    js = (paths.webui_dir() / "screens" / "bugreport.js").read_text(encoding="utf-8")
    body = js[js.index("function refresh()"):]
    assert 'if (s.error === "too_fast") {' in body, \
        "the cooldown warning is not keyed on the error alone"
    assert 'if (s.error === "too_fast" || n > 0)' not in body, \
        "a successful report is being answered with a refusal again"
    assert 'st("sent")' in body, "the receipt is no longer drawn"


def test_the_text_is_capped_where_the_server_caps_it():
    """The hub truncates at the same BUG_TEXT_MAX the server does, so the counter under the box is
    telling the truth rather than letting someone type 4000 characters it will quietly halve."""
    panel, s = _panel()
    Api(panel).bug_send("x" * (C.BUG_TEXT_MAX + 500))
    assert len(s.client.sent[0]) == C.BUG_TEXT_MAX


_TESTS = [
    test_the_slice_is_json_and_says_who_the_report_goes_as,
    test_every_language_carries_the_same_keys,
    test_the_screen_is_registered_and_reachable,
    test_a_report_reaches_the_wire_and_empties_the_box,
    test_an_empty_box_is_never_sent,
    test_a_signed_out_hub_never_sends,
    test_one_report_every_five_seconds,
    test_the_cooldown_lets_go_after_five_seconds,
    test_the_cooldown_is_measured_on_a_monotonic_clock,
    test_the_servers_refusal_wins_over_our_clock,
    test_a_failed_send_keeps_what_they_wrote,
    test_the_draft_survives_leaving_the_screen,
    test_a_filed_report_leaves_the_box,
    test_the_countdown_is_not_a_telling_off,
    test_the_text_is_capped_where_the_server_caps_it,
]


def main():
    results = []
    for fn in _TESTS:
        try:
            fn()
            results.append((fn.__name__, True))
            print("ok   %s" % fn.__name__)
        except Exception:                               # noqa: BLE001
            import traceback
            results.append((fn.__name__, False))
            print("FAIL %s\n%s" % (fn.__name__, traceback.format_exc()))
    failed = [n for n, ok in results if not ok]
    print("\n%d/%d passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
