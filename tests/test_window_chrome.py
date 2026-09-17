#!/usr/bin/env python3.12
"""Headless tests for the WINDOW CHROME - the hub's own title bar and border.

    python3.12 tests/test_window_chrome.py

The hub's window is frameless on Windows (hub/webui/shell.py) and the page draws the caption and
the resize edges itself (static/titlebar.js), so every job the Windows frame used to do is now
arithmetic in hub/webui/window.py. That arithmetic is what these cover.

None of it talks to Windows: the four methods that touch Win32 - `_rect`, `_cursor`, `_work_area`
and `_place` - are stubbed, so the tests run anywhere and assert the DECISIONS rather than the
ctypes calls. The Win32 behaviour behind them was measured against a real hub instead, and the
findings that shaped this code are written up in window.py's docstring.

Each of these is a bug that was really hit while building it:

  * a double-click on a MAXIMIZED window used to leave it maximized, because the first of the two
    presses restored it and the dblclick then re-maximized it. Windows restores a maximized window
    when you drag the caption, not when you press on it, so the restore waits for movement.
  * merely CLICKING the title bar of a window already sitting against a screen edge used to
    half-tile it, because a release at an edge counted as a snap gesture whether or not anything
    had moved.
  * maximize must be the monitor's WORK AREA. The real WindowState.Maximized hangs a frameless
    window 7px off every edge and 62px over the taskbar, which puts the window buttons the page
    drew partly off-screen.
  * dragging an edge past the minimum size must stop that edge, not start walking the opposite one.
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-wc-state-"))
sys.path.insert(0, str(REPO))

from hub.webui import window as W                # noqa: E402

RESULTS = []

WORK = (0, 0, 2560, 1392)          # a plain single monitor with a taskbar


class FakeWindow:
    """Stands in for the pywebview Window. Only `destroy` and the identity are ever used."""

    def __init__(self):
        self.uid = "master"
        self.title = "Lights Out"
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


class Chrome(W.WindowControl):
    """A WindowControl whose window and cursor live in plain Python."""

    def __init__(self, rect=(300, 200, 1200, 760), work=WORK):
        super().__init__(FakeWindow())
        self.fake_rect = rect
        self.fake_work = work
        self.cursor = (0, 0)
        self.minimized = False

    # the four Win32 seams
    @property
    def hwnd(self):
        return 4242

    def _rect(self, _hwnd=None):
        return self.fake_rect

    def _cursor(self):
        return self.cursor

    def _work_area(self, _hwnd=None):
        return self.fake_work

    def _place(self, x, y, w=0, h=0, size=True):
        cx, cy, cw, ch = self.fake_rect
        self.fake_rect = (int(x), int(y), int(w) if size else cw, int(h) if size else ch)
        return True

    def minimize(self):
        self.minimized = True
        return True


def press_at(chrome, x, y):
    chrome.cursor = (x, y)
    chrome.drag_start()


def move_to(chrome, x, y):
    chrome.cursor = (x, y)
    chrome.drag_move()


# ---------------------------------------------------------------- maximize / restore
def test_maximize_fills_the_work_area_not_the_screen():
    c = Chrome()
    c.toggle_maximize()
    assert c.fake_rect == WORK, c.fake_rect
    assert c.is_maximized()


def test_restore_returns_to_the_rect_it_left():
    c = Chrome(rect=(300, 200, 1200, 760))
    c.toggle_maximize()
    c.toggle_maximize()
    assert c.fake_rect == (300, 200, 1200, 760), c.fake_rect
    assert not c.is_maximized()


def test_restore_without_a_remembered_rect_lands_on_screen():
    """The hub started already filling the work area, so there is nothing to go back to."""
    c = Chrome(rect=WORK)
    assert c.is_maximized()
    c.toggle_maximize()
    x, y, w, h = c.fake_rect
    assert (w, h) == (1200, 760), (w, h)
    assert x >= 0 and y >= 0 and x + w <= WORK[2] and y + h <= WORK[3], c.fake_rect


# ---------------------------------------------------------------- drag
def test_a_drag_moves_the_window_by_the_cursor_delta():
    c = Chrome(rect=(300, 200, 1200, 760))
    press_at(c, 800, 220)
    move_to(c, 980, 340)
    assert c.fake_rect == (480, 320, 1200, 760), c.fake_rect


def test_pressing_a_maximized_title_bar_does_not_restore_it():
    """The first half of a double-click must leave the window exactly where it is."""
    c = Chrome(rect=(300, 200, 1200, 760))
    c.toggle_maximize()
    press_at(c, 1200, 20)
    assert c.fake_rect == WORK, c.fake_rect
    c.drag_end()
    assert c.fake_rect == WORK, "a press and release with no movement resized the window"


def test_a_double_click_on_a_maximized_window_still_restores_it():
    """The whole sequence the page sends: press, release, press, release, then maximize."""
    c = Chrome(rect=(300, 200, 1200, 760))
    c.toggle_maximize()
    for _ in range(2):
        press_at(c, 1200, 20)
        c.drag_end()
    c.toggle_maximize()
    assert c.fake_rect == (300, 200, 1200, 760), c.fake_rect


def test_dragging_a_maximized_window_restores_it_under_the_cursor():
    c = Chrome(rect=(300, 200, 1200, 760))
    c.toggle_maximize()
    press_at(c, 1280, 20)                    # grabbed halfway along a 2560-wide window
    move_to(c, 1280, 60)
    x, y, w, h = c.fake_rect
    assert (w, h) == (1200, 760), (w, h)
    # the grab point stays halfway along the restored window, not at its left edge
    assert abs((1280 - x) - w // 2) <= 2, c.fake_rect


# ---------------------------------------------------------------- snap
def test_a_release_at_the_left_edge_half_tiles():
    c = Chrome(rect=(300, 200, 1200, 760))
    press_at(c, 800, 220)
    move_to(c, 4, 400)
    assert c.drag_end() == W.SNAP_LEFT
    assert c.fake_rect == (0, 0, 1280, 1392), c.fake_rect


def test_a_release_at_the_right_edge_half_tiles():
    c = Chrome(rect=(300, 200, 1200, 760))
    press_at(c, 800, 220)
    move_to(c, 2557, 400)
    assert c.drag_end() == W.SNAP_RIGHT
    assert c.fake_rect == (1280, 0, 1280, 1392), c.fake_rect


def test_a_release_at_the_top_maximizes():
    c = Chrome(rect=(300, 200, 1200, 760))
    press_at(c, 800, 220)
    move_to(c, 1200, 2)
    assert c.drag_end() == W.SNAP_TOP
    assert c.fake_rect == WORK, c.fake_rect


def test_a_click_that_never_moved_never_snaps():
    """A window already against the left edge, clicked - not dragged - on its title bar."""
    c = Chrome(rect=(0, 200, 1000, 700))
    c.cursor = (500, 220)
    c.drag_start()
    assert c.drag_end() == W.SNAP_NONE
    assert c.fake_rect == (0, 200, 1000, 700), c.fake_rect


def test_a_release_in_open_space_never_snaps():
    c = Chrome(rect=(300, 200, 1200, 760))
    press_at(c, 800, 220)
    move_to(c, 1300, 700)
    assert c.drag_end() == W.SNAP_NONE
    assert c.fake_rect == (800, 680, 1200, 760), c.fake_rect


# ---------------------------------------------------------------- resize
def test_the_south_east_corner_grows_both_sides():
    c = Chrome(rect=(400, 250, 1000, 700))
    c.cursor = (1400, 950)
    c.resize_start("se")
    c.cursor = (1550, 1050)
    c.resize_move()
    assert c.fake_rect == (400, 250, 1150, 800), c.fake_rect


def test_the_west_edge_pins_the_east_edge():
    c = Chrome(rect=(400, 250, 1000, 700))
    right = 400 + 1000
    c.cursor = (400, 600)
    c.resize_start("w")
    c.cursor = (520, 600)
    c.resize_move()
    x, y, w, h = c.fake_rect
    assert (x, w) == (520, 880), c.fake_rect
    assert x + w == right, "the right edge moved while the left one was being dragged"


def test_the_north_edge_pins_the_south_edge():
    c = Chrome(rect=(400, 250, 1000, 700))
    bottom = 250 + 700
    c.cursor = (900, 250)
    c.resize_start("n")
    c.cursor = (900, 330)
    c.resize_move()
    x, y, w, h = c.fake_rect
    assert (y, h) == (330, 620), c.fake_rect
    assert y + h == bottom


def test_resizing_past_the_minimum_stops_the_dragged_edge():
    """Drag the left edge far to the right: the window stops at MIN_W with its RIGHT edge still
    pinned. The bug this guards is the left edge carrying on and dragging the right one with it."""
    c = Chrome(rect=(400, 250, 1000, 700))
    right = 400 + 1000
    c.cursor = (400, 600)
    c.resize_start("w")
    c.cursor = (1300, 600)
    c.resize_move()
    x, y, w, h = c.fake_rect
    assert w == W.WindowControl.MIN_W, w
    assert x + w == right, c.fake_rect


def test_the_minimum_applies_to_height_too():
    c = Chrome(rect=(400, 250, 1000, 700))
    c.cursor = (900, 950)
    c.resize_start("s")
    c.cursor = (900, 300)
    c.resize_move()
    assert c.fake_rect[3] == W.WindowControl.MIN_H, c.fake_rect


def test_a_maximized_window_has_no_edges_to_drag():
    c = Chrome(rect=WORK)
    assert c.resize_start("se") is False
    assert c.resize_move() is False
    assert c.fake_rect == WORK


def test_an_unknown_edge_is_refused():
    """The edge arrives as a URL segment (/window/resize/start/<edge>), so it is untrusted."""
    c = Chrome(rect=(400, 250, 1000, 700))
    for bogus in ("", "middle", "../..", "nsew"):
        assert c.resize_start(bogus) is False, bogus
    assert c.fake_rect == (400, 250, 1000, 700)


# ---------------------------------------------------------------- the X
def test_close_goes_through_pywebview_so_the_tray_still_catches_it():
    """The X must fire pywebview's `closing`, which is what CloseToTray hides the hub on - not a
    Win32 close that would bypass it and quit the hub outright."""
    c = Chrome()
    assert c.close() is True
    assert c.window.destroyed, "close() did not go through window.destroy()"


# ---------------------------------------------------------------- no window
def test_every_op_is_inert_without_a_window():
    class Headless(Chrome):
        @property
        def hwnd(self):
            return None

    c = Headless()
    assert c.drag_start() is False
    assert c.drag_move() is False
    assert c.drag_end() == W.SNAP_NONE
    assert c.resize_start("se") is False
    assert c.resize_move() is False
    assert c.toggle_maximize() is False
    assert c.is_maximized() is False


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
        test_maximize_fills_the_work_area_not_the_screen,
        test_restore_returns_to_the_rect_it_left,
        test_restore_without_a_remembered_rect_lands_on_screen,
        test_a_drag_moves_the_window_by_the_cursor_delta,
        test_pressing_a_maximized_title_bar_does_not_restore_it,
        test_a_double_click_on_a_maximized_window_still_restores_it,
        test_dragging_a_maximized_window_restores_it_under_the_cursor,
        test_a_release_at_the_left_edge_half_tiles,
        test_a_release_at_the_right_edge_half_tiles,
        test_a_release_at_the_top_maximizes,
        test_a_click_that_never_moved_never_snaps,
        test_a_release_in_open_space_never_snaps,
        test_the_south_east_corner_grows_both_sides,
        test_the_west_edge_pins_the_east_edge,
        test_the_north_edge_pins_the_south_edge,
        test_resizing_past_the_minimum_stops_the_dragged_edge,
        test_the_minimum_applies_to_height_too,
        test_a_maximized_window_has_no_edges_to_drag,
        test_an_unknown_edge_is_refused,
        test_close_goes_through_pywebview_so_the_tray_still_catches_it,
        test_every_op_is_inert_without_a_window,
    ]:
        _run(fn)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
