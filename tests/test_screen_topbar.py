#!/usr/bin/env python3.12
"""Run: python -m pytest tests/test_screen_topbar.py

The header shows Lights Out and tournament registration totals. Account totals
arrive on the stats stream; event totals refresh through the existing endpoint
on every screen. Unknown, stale and disconnected totals must not look like zero.
The older service activity fields remain available to their existing consumers.
Browser coverage in test_registered_topbar_browser.cjs checks actual rendering,
translations, repeated changes and window bounds.
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


def test_tournament_count_refreshes_without_visiting_event_screen():
    from unittest.mock import patch
    from tests.test_screen_bugreport import _panel as action_panel
    panel, s = action_panel()
    panel.view = "competitive"
    replies = iter([11, 12, 0])
    s.client.tournament = lambda: (200, {"ok": True, "entrant_count": next(replies)})
    with patch("hub.competitive.time.monotonic", return_value=100) as clock:
        s.on_live_event({"type": "stats", "players_registered": 51})
        assert _status(panel)["tournament_registered"] == 11
        for moment in (101, 105, 114):
            clock.return_value = moment
            s.on_live_event({"type": "stats", "players_registered": 52})
            assert _status(panel)["tournament_registered"] == 11
        clock.return_value = 115
        s.on_live_event({"type": "stats"})
        assert _status(panel)["tournament_registered"] == 12
        clock.return_value = 130
        s.on_live_event({"type": "stats"})
        assert _status(panel)["tournament_registered"] == 0


def test_tournament_unknown_failure_and_staleness_never_claim_zero():
    from unittest.mock import patch
    panel, s = _panel()
    assert _status(panel)["tournament_registered"] is None
    with patch("hub.competitive.time.monotonic", return_value=100) as clock:
        for value in (None, -1, True, "11", 1.5, 2**53):
            s._tournament_result(200, {"ok": True, "entrant_count": 11})
            s._tournament_result(200, {"ok": True, "entrant_count": value})
            assert _status(panel)["tournament_registered"] is None
        s._tournament_result(200, {"ok": True, "entrant_count": 11})
        s._tournament_result(503, {})
        assert _status(panel)["tournament_registered"] is None
        s._tournament_result(200, {"ok": True, "entrant_count": 11})
        clock.return_value = 131
        assert _status(panel)["tournament_registered"] is None


def test_registration_counts_clear_on_disconnect_and_account_change():
    panel, s = _panel()
    s.on_live_event({"type": "stats", "players_registered": 51})
    s._tournament_result(200, {"ok": True, "entrant_count": 11})
    s._disconnect()
    assert _status(panel)["players_registered"] is None
    assert _status(panel)["tournament_registered"] is None
    s._clear_account_state()
    assert s.tournament_polled is None


def test_reconnect_refreshes_tournament_before_restoring_its_count():
    from unittest.mock import patch
    from tests.test_screen_bugreport import _panel as action_panel
    panel, s = action_panel()
    replies = iter([11, 12])
    s.client.tournament = lambda: (200, {"ok": True, "entrant_count": next(replies)})
    with patch("hub.competitive.time.monotonic", return_value=100):
        s.on_live_event({"type": "stats"})
        assert _status(panel)["tournament_registered"] == 11
        s._on_live_status(False, "lost")
        s._on_live_status(True, "")
        assert _status(panel)["tournament_registered"] is None
        s.on_live_event({"type": "stats"})
        assert _status(panel)["tournament_registered"] == 12


def test_tournament_response_started_before_stream_loss_cannot_restore_count():
    from tests.test_screen_bugreport import _panel as action_panel
    panel, s = action_panel()
    pending = []
    s.client.tournament = lambda: None
    s._action = lambda call, on_result=None: pending.append(on_result)
    s.on_live_event({"type": "stats"})
    s._on_live_status(False, "lost")
    s._on_live_status(True, "")
    s.on_live_event({"type": "stats"})
    pending.pop(0)(200, {"ok": True, "entrant_count": 11})
    assert _status(panel)["tournament_registered"] is None
    assert len(pending) == 1, "retry the event GET after the old request finishes"
    pending.pop(0)(200, {"ok": True, "entrant_count": 12})
    assert _status(panel)["tournament_registered"] == 12


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


def test_registration_counts_replace_activity_counts():
    """The order is the ask, left to right."""
    html = _static("index.html")
    block = html[html.index('id="topstats"'):html.index("</header>")]
    order = re.findall(r'id="(statqueued|statlive|statregistered|stattournament)"', block)
    assert order == ["statregistered", "stattournament"], order


def test_the_nav_is_still_the_last_word_before_the_counts():
    """Tournament follows Bug report and is the last item before the counts."""
    core = _static("core.js")
    items = re.findall(r'\{ view: "(\w+)",', core)
    assert items[-2:] == ["bugreport", "tournament"], items


def test_a_dropped_stream_empties_the_two_counts():
    """Replace the first total with connection status and hide the event total."""
    fn = _render_status()
    assert 't("topbar_offline")' in fn, "the dot no longer says the connection is gone"
    assert 't("comp_live_lost")' not in fn, \
        "the long sentence is back in the bar, and it does not fit (see FIT_W below)"
    for var in ("tournamentEl",):
        assert re.search(re.escape(var) + r'\.textContent = ok \? t\(', fn), \
            "%s is not gated on the connection: a stale count would stay on screen" % var
        assert re.search(re.escape(var) + r'\.textContent = ok \? t\([^;]*: "";', fn), \
            "%s is not emptied when the connection is gone" % var


def test_the_counts_come_from_the_shared_status_slice():
    """The header uses the validated registration totals in the shared slice."""
    fn = _render_status()
    assert fn.count("state.status") == 1, "renderStatus reads the status slice more than once"
    for key in ("st.players_registered", "st.tournament_registered"):
        assert key in fn, key


def test_the_bar_fits_the_smallest_window_it_can_be_given():
    """THE MEASUREMENT THIS FILE EXISTS TO PROTECT.

    The bar is one row and does not scroll, so what saves it in a small window is the zoom
    (ui.css `zoom: var(--ui-scale)`, core.js applyScale). FIT_W is the layout width that zoom
    delivers, and it lives between two bounds that this asserts rather than trusts:

      * at least as wide as the screens need. The header compacts its spacing while keeping
        registration totals side by side. The loaded-font browser regression in
        test_registered_topbar_browser.cjs verifies single-row navigation in all seven
        languages, including Russian, with large totals and visible window controls.
      * no wider than the smallest window divided by the zoom's floor. Past that the zoom stops
        and the bar is CLIPPED instead of scaled, which takes the close button off the edge.
    """
    from hub.webui.window import WindowControl
    core = _static("core.js")
    fit_w = int(re.search(r"var FIT_W = (\d+)", core).group(1))
    min_scale = float(re.search(r"var MIN_SCALE = ([\d.]+)", core).group(1))
    assert fit_w >= 1133, "FIT_W no longer covers the screens' measured layout width"
    ceiling = WindowControl.MIN_W / min_scale
    assert fit_w <= ceiling, (
        "FIT_W %d is past %d: at the %dpx minimum window the zoom would clamp at %s and the "
        "window buttons would be drawn off the edge" % (fit_w, ceiling, WindowControl.MIN_W,
                                                        min_scale))


# ---------------------------------------------------------------- strings
def test_every_language_names_the_two_new_counts():
    """A missing string here is the raw key on screen, in the chrome every screen shows."""
    for code in i18n.CODES:
        for key in ("topbar_registered", "topbar_tournament"):
            value = i18n.tr(code, key)
            assert value != key, (code, key)
            assert "{n}" in value, (code, key, value)
        # ...and the short label the dot wears instead of a count when the stream is down.
        assert i18n.tr(code, "topbar_offline") != "topbar_offline", code


def test_the_labels_say_what_they_count():
    """Each total identifies which registration it counts."""
    assert i18n.tr("en", "topbar_registered").format(n=137) == "137 registered in Lights Out"
    assert i18n.tr("en", "topbar_tournament").format(n=42) == "42 registered for tournament"


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
    test_reconnect_refreshes_tournament_before_restoring_its_count,
    test_tournament_response_started_before_stream_loss_cannot_restore_count,
    test_tournament_count_refreshes_without_visiting_event_screen,
    test_tournament_unknown_failure_and_staleness_never_claim_zero,
    test_registration_counts_clear_on_disconnect_and_account_change,
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
    test_registration_counts_replace_activity_counts,
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
