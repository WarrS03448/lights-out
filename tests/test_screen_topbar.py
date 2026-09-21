#!/usr/bin/env python3.12
"""Headless tests for the TOP BAR'S THREE COUNTS.

Sam, 2026-09-16: "move the count of online players to the right of bug report. to the right of
count of online players put the count of players in queue and to the right of that put the count
of live games."

So the bar now reads: the eight nav items, then how many are online, how many are searching, how
many matches are in flight, then the window buttons. Three things have to hold for that to be
true, and each has a test here:

  * THE THREE FIGURES ARE ONE MOMENT. They come off a single `stats` broadcast (server/live.cjs
    stats()), so they are read off the session together and shipped in one snapshot slice. A
    count of people taken now next to a count of matches taken a minute ago is how you get
    "4 online, 9 live games", and nothing on screen would say which half was stale.
  * THEY GO DARK TOGETHER. One broadcast means one failure: when the stream drops, all three
    freeze. The dot says so and the other two are emptied, rather than leaving yesterday's
    numbers up looking current.
  * THE BAR STILL FITS. It is the widest thing in the app now, and the window buttons are drawn
    by the page at its right end (static/titlebar.js) - so a bar that overflows does not scroll,
    it pushes Close off the edge of the window. core.js FIT_W is what stops that, and the two
    bounds it lives between are asserted rather than trusted.

Run directly (``.venv/Scripts/python tests/test_screen_topbar.py``) or import the functions.
"""
import os
import re
import sys
import tempfile

os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-topbar-test-"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import i18n                                    # noqa: E402
from hub import paths                                   # noqa: E402
from hub.webui.panel import WebPanel                    # noqa: E402
from hub.webui.scheduler import InlineScheduler         # noqa: E402
from hub.webui.snapshot import state_snapshot           # noqa: E402


# ---------------------------------------------------------------- harness
class _FakeApp:
    def __init__(self):
        self.state = {"installed": {}, "auth": None}
        self.catalogue = None
        self.game_dir = None


def _panel():
    i18n.set_language("en")
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    s = panel.session
    s.me = {"name": "Sam", "steam_id": "76561198000999000"}
    s.token = "tok"
    s.phase = "idle"
    s.connected = True
    return panel, s


def _status(panel):
    return state_snapshot(panel.session, panel)["status"]


def _static(name):
    return (paths.webui_dir() / name).read_text(encoding="utf-8")


def _render_status():
    """core.js renderStatus, with its comments stripped. The assertions below are about what the
    function DOES, and a prose mention of an old string is not a call to it."""
    core = _static("core.js")
    fn = core[core.index("function renderStatus"):]
    fn = fn[:fn.index("\n  }")]
    return "\n".join(l for l in fn.split("\n") if not l.strip().startswith("//"))


# ---------------------------------------------------------------- the wire
def test_one_stats_event_sets_all_three_counts():
    """The whole point of reading them off ONE event: they are the same instant."""
    panel, s = _panel()
    s.on_live_event({"type": "stats", "online": 1284, "queued": 137, "live_matches": 42})
    st = _status(panel)
    assert st["online"] == 1284
    assert st["queued"] == 137
    assert st["live_matches"] == 42


def test_registered_total_is_server_supplied_and_changes_with_stats():
    panel, s = _panel()
    for total in (1290, 1291, 0):
        s.on_live_event({"type": "stats", "online": 10, "queued": 3,
                         "live_matches": 1, "players_registered": total})
        assert _status(panel)["players_registered"] == total


def test_missing_or_invalid_registered_total_clears_last_known_value():
    panel, s = _panel()
    for value in (None, -1, True, "1290", 1.5, 2**53):
        s.on_live_event({"type": "stats", "players_registered": 1290})
        s.on_live_event({"type": "stats", "players_registered": value})
        assert _status(panel)["players_registered"] is None
    s.on_live_event({"type": "stats", "players_registered": 1290})
    s.on_live_event({"type": "stats", "online": 5})
    assert _status(panel)["players_registered"] is None


def test_the_slice_is_json_and_never_carries_a_none():
    """The page does `st.online || 0`, which turns null into 0 - but a null would mean the hub
    forgot to ask, and a 0 means the server said nobody. They must not look the same here."""
    import json
    panel, s = _panel()
    st = _status(panel)
    json.dumps(st)                                      # never raises
    for key in ("online", "queued", "live_matches"):
        assert isinstance(st[key], int), (key, st[key])


def test_an_older_server_sends_no_live_count_and_is_not_an_error():
    """`live_matches` was added to the stats broadcast with this bar. A hub that reaches a server
    from before it must show 0 there, not crash the event loop and lose the other two counts."""
    panel, s = _panel()
    s.on_live_event({"type": "stats", "online": 9, "queued": 3})
    st = _status(panel)
    assert st["online"] == 9 and st["queued"] == 3
    assert st["live_matches"] == 0


def test_the_queue_figure_is_everyone_searching_not_our_own_place():
    """`queued` is the service-wide count. Our own position in the queue is a different number
    and belongs to the queue card; putting a position in the top bar would tell every player a
    different 'how busy is it' answer."""
    panel, s = _panel()
    s.on_live_event({"type": "stats", "online": 50, "queued": 12, "live_matches": 1})
    s.on_live_event({"type": "queued", "position": 4, "size": 12})
    st = _status(panel)
    assert st["queued"] == 12
    assert s.queue_position == 4, "the position is still kept, just not in the bar"


def test_a_count_that_moves_does_not_rebuild_the_tk_body():
    """The Tk tab redraws off a render signature and these three move on every broadcast, so all
    three have to be in the live list or the tab flickers (hub/competitive.py _SIG_LIVE)."""
    from hub import competitive as C
    for key in ("online", "queue_size", "live_matches"):
        assert key in C.CompetitivePanel._SIG_LIVE, key


# ---------------------------------------------------------------- the bar
def test_the_counts_sit_to_the_right_of_the_nav():
    """Sam asked for them next to Bug report, which is the LAST nav item - so #topstats has to be
    inside .brand-wrap and after #nav, and the old right-hand status dot has to be gone from the
    right end of the bar rather than duplicated."""
    html = _static("index.html")
    brand = html[html.index('class="brand-wrap"'):html.index("</header>")]
    assert 'id="nav"' in brand and 'id="topstats"' in brand, \
        "the counts are not in the same group as the nav"
    assert brand.index('id="nav"') < brand.index('id="topstats"'), \
        "the counts are drawn before the nav, not to the right of it"
    # exactly one status dot in the page, and it is inside the new group
    assert html.count('id="serverstatus"') == 1
    stats_block = html[html.index('id="topstats"'):html.index("</header>")]
    assert 'id="serverstatus"' in stats_block


def test_online_then_queue_then_live_in_that_order():
    """The order is the ask, left to right."""
    html = _static("index.html")
    block = html[html.index('id="topstats"'):html.index("</header>")]
    order = [m for m in re.findall(r'id="(serverstatus|statqueued|statlive|statregistered)"', block)]
    assert order == ["serverstatus", "statqueued", "statlive", "statregistered"], order


def test_the_nav_is_still_the_last_word_before_the_counts():
    """Bug report is the item the counts sit next to, so it stays last in the nav."""
    core = _static("core.js")
    items = re.findall(r'\{ view: "(\w+)",', core)
    assert items[-1] == "bugreport", items


def test_a_dropped_stream_empties_the_two_counts():
    """All three freeze together, so all three have to stop claiming to be current together."""
    fn = _render_status()
    assert 't("topbar_offline")' in fn, "the dot no longer says the connection is gone"
    assert 't("comp_live_lost")' not in fn, \
        "the long sentence is back in the bar, and it does not fit (see FIT_W below)"
    for var in ("queuedEl", "liveEl"):
        assert re.search(re.escape(var) + r'\.textContent = ok \? t\(', fn), \
            "%s is not gated on the connection: a stale count would stay on screen" % var
        assert re.search(re.escape(var) + r'\.textContent = ok \? t\([^;]*: "";', fn), \
            "%s is not emptied when the connection is gone" % var


def test_the_counts_come_from_the_shared_status_slice():
    """One slice, so the page cannot draw two of them from one moment and one from another."""
    fn = _render_status()
    assert fn.count("state.status") == 1, "renderStatus reads the status slice more than once"
    for key in ("st.online", "st.queued", "st.live_matches"):
        assert key in fn, key


def test_the_bar_fits_the_smallest_window_it_can_be_given():
    """THE MEASUREMENT THIS FILE EXISTS TO PROTECT.

    The bar is one row and does not scroll, so what saves it in a small window is the zoom
    (ui.css `zoom: var(--ui-scale)`, core.js applyScale). FIT_W is the layout width that zoom
    delivers, and it lives between two bounds that this asserts rather than trusts:

      * at least as wide as the bar NEEDS. Measured with the webfont loaded, in every language:
        en 1080, de 1102, fr 1105, es 1131, pt 1133, zh 850, ru 1185. 1133 covers six of the
        seven; Russian has the longest nav there is and still wraps at the smallest window,
        which is what it did before the counts were added too.
      * no wider than the smallest window divided by the zoom's floor. Past that the zoom stops
        and the bar is CLIPPED instead of scaled, which takes the close button off the edge.
    """
    from hub.webui.window import WindowControl
    core = _static("core.js")
    fit_w = int(re.search(r"var FIT_W = (\d+)", core).group(1))
    min_scale = float(re.search(r"var MIN_SCALE = ([\d.]+)", core).group(1))
    assert fit_w >= 1133, "FIT_W no longer covers the bar six of the seven languages need"
    ceiling = WindowControl.MIN_W / min_scale
    assert fit_w <= ceiling, (
        "FIT_W %d is past %d: at the %dpx minimum window the zoom would clamp at %s and the "
        "window buttons would be drawn off the edge" % (fit_w, ceiling, WindowControl.MIN_W,
                                                        min_scale))


# ---------------------------------------------------------------- strings
def test_every_language_names_the_two_new_counts():
    """A missing string here is the raw key on screen, in the chrome every screen shows."""
    for code in i18n.CODES:
        for key in ("comp_online", "topbar_queued", "topbar_live", "topbar_registered"):
            value = i18n.tr(code, key)
            assert value != key, (code, key)
            assert "{n}" in value, (code, key, value)
        # ...and the short label the dot wears instead of a count when the stream is down.
        assert i18n.tr(code, "topbar_offline") != "topbar_offline", code


def test_the_labels_say_what_they_count():
    """Three bare numbers in a row is a puzzle. Each carries its own word - and the words are
    the short form on purpose, because the bar is what sets FIT_W above."""
    assert i18n.tr("en", "topbar_queued").format(n=137) == "137 in queue"
    assert i18n.tr("en", "topbar_live").format(n=42) == "42 live games"


def test_signin_never_exposes_preview_queue_counts():
    from unittest.mock import patch
    panel, s = _panel()
    with patch("hub.competitive.random.randint", return_value=7), patch.object(s, "_connect"):
        s.adopt_account({"steam_id": "76561198000999000", "persona": "Sam", "token": "tok"})
    assert s.queue_size == 0
    assert _status(panel)["queued"] == 0
    assert not _status(panel)["connected"], "wait for actual stats before displaying counts"


def test_late_queue_responses_cannot_overwrite_server_stats():
    panel, s = _panel()
    s.on_live_event({"type": "stats", "online": 10, "queued": 0, "live_matches": 1})
    s.on_live_event({"type": "queued", "position": 1, "size": 5})
    assert _status(panel)["queued"] == 0
    s._join_result(200, {"position": 1, "size": 5}, 0)
    assert _status(panel)["queued"] == 0
    s._requeue_result(200, {"position": 1, "size": 5})
    assert _status(panel)["queued"] == 0
    s.on_live_event({"type": "stats", "online": 10, "queued": 2, "live_matches": 0})
    assert _status(panel)["queued"] == 2
    s.on_live_event({"type": "stats", "online": 10, "queued": 0, "live_matches": 0})
    assert _status(panel)["queued"] == 0


def test_reconnect_hides_old_counts_until_fresh_stats():
    panel, s = _panel()
    s.on_live_event({"type": "stats", "online": 10, "queued": 5, "live_matches": 0})
    assert _status(panel)["connected"]
    s._on_live_status(False, "lost")
    assert not _status(panel)["connected"]
    s._on_live_status(True, "")
    assert not _status(panel)["connected"]
    s.on_live_event({"type": "stats", "online": 1, "queued": 0, "live_matches": 0})
    assert _status(panel)["connected"]
    assert _status(panel)["queued"] == 0


def test_signout_hides_stale_counts():
    panel, s = _panel()
    s.on_live_event({"type": "stats", "online": 10, "queued": 5, "live_matches": 0})
    s._disconnect()
    assert not _status(panel)["connected"]


def test_disconnected_client_cannot_deliver_pending_stats():
    from unittest.mock import patch
    panel, s = _panel()
    pending = []
    with patch.object(panel, "post", side_effect=pending.append), \
            patch("hub.competitive.live_mod.LiveClient") as client:
        s._connect()
        callbacks = client.call_args.kwargs
        callbacks["on_status"](True, "")
        callbacks["on_event"]({"type": "stats", "online": 10, "queued": 5})
        s._disconnect()
        for deliver in pending:
            deliver()
    assert not _status(panel)["connected"]
    assert _status(panel)["queued"] == 0


_TESTS = [
    test_registered_total_is_server_supplied_and_changes_with_stats,
    test_missing_or_invalid_registered_total_clears_last_known_value,
    test_disconnected_client_cannot_deliver_pending_stats,
    test_signin_never_exposes_preview_queue_counts,
    test_late_queue_responses_cannot_overwrite_server_stats,
    test_reconnect_hides_old_counts_until_fresh_stats,
    test_signout_hides_stale_counts,
    test_one_stats_event_sets_all_three_counts,
    test_the_slice_is_json_and_never_carries_a_none,
    test_an_older_server_sends_no_live_count_and_is_not_an_error,
    test_the_queue_figure_is_everyone_searching_not_our_own_place,
    test_a_count_that_moves_does_not_rebuild_the_tk_body,
    test_the_counts_sit_to_the_right_of_the_nav,
    test_online_then_queue_then_live_in_that_order,
    test_the_nav_is_still_the_last_word_before_the_counts,
    test_a_dropped_stream_empties_the_two_counts,
    test_the_counts_come_from_the_shared_status_slice,
    test_the_bar_fits_the_smallest_window_it_can_be_given,
    test_every_language_names_the_two_new_counts,
    test_the_labels_say_what_they_count,
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
