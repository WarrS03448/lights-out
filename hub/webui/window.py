"""Win32 control of the frameless hub window: move, resize, maximize, snap.

The web UI draws its own title bar (static/titlebar.js) instead of wearing the Windows one, so
everything the native frame used to do has to be done here. All of it is ctypes against the
window's HWND; nothing in this module imports pywebview.

WHY NOT THE NATIVE MOVE LOOP. The obvious implementation of "drag the title bar" is the standard
Win32 one: on mouse-down, ReleaseCapture() and hand the window a WM_NCLBUTTONDOWN/HTCAPTION (or
WM_SYSCOMMAND/SC_MOVE), which starts the OS's own modal move loop and brings Aero Snap with it.
MEASURED 2026-09-16: it does not work here. All three variants - PostMessage NCLBUTTONDOWN,
PostMessage SC_MOVE|HTCAPTION, SendMessage NCLBUTTONDOWN off a worker thread - reach the window
(the verb lands in 0.3 ms) and move it by exactly (0, 0). WebView2 renders in its OWN PROCESS and
holds the mouse capture there, so the move loop starts and then receives no mouse input at all.
Driving the window ourselves with SetWindowPos moved it by exactly the (160, 90) it was asked for,
so that is what `drag_move` does, and `snap` re-implements by hand the three snap gestures the
lost move loop would have given us.

WHY THE CURSOR IS READ HERE, NOT SENT FROM JS. A MouseEvent's screenX/screenY are CSS pixels, so
on a 150% display they are two thirds of the physical pixels GetWindowRect speaks in and every
drag would lag the cursor. The page only ever says "the drag is still going"; this module asks
Windows where the cursor actually is. No coordinate crosses the bridge, so there is no DPI factor
to get wrong.

WHY RESIZE IS DRAWN, NOT NATIVE. pywebview's frameless is FormBorderStyle.None, which drops
WS_THICKFRAME and every resize edge with it. Putting that style bit back does restore native
resize (the bottom edge hit-tests HTBOTTOM once SWP_FRAMECHANGED is sent) - but it also opens a
7px non-client border that Windows fills with the user's ACCENT COLOUR, and the hub wore a purple
band across the top of its own title bar. MEASURED 2026-09-16: that band was RGB (57, 49, 88),
exactly this machine's DWM AccentColor; re-extending the DWM frame did not close it and
DWMWA_BORDER_COLOR did not repaint it, because it is the composited non-client area rather than
the border DWM draws. So the window keeps NO frame at all - the page then covers it edge to edge -
and `resize_start`/`resize_move` drive the edges from grips the page draws, exactly as the drag
does. The cursors come from CSS, which offers the same arrows Windows would have.

WHY MAXIMIZE IS EMULATED. MEASURED 2026-09-16 on a 2560x1392 work area: a frameless window put
into the real WindowState.Maximized lands at (-7, -7, 2574, 1454) - 7 px of it hanging off every
edge (so the title bar's buttons are half off-screen) and 62 px of it over the taskbar. That is
the classic custom-title-bar bug: with no caption, Windows still applies the maximized frame
overhang. `toggle_maximize` sets the window to the monitor's work area instead, which is correct
on every edge and on whichever monitor the window is on.

Windows only. Every method is a no-op elsewhere, and `shell.run` only asks for a frameless window
on Windows, so macOS keeps its native frame and its own traffic lights.
"""
import ctypes
import os
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

SW_MINIMIZE = 6
MONITOR_DEFAULTTONEAREST = 2

SNAP_NONE, SNAP_TOP, SNAP_LEFT, SNAP_RIGHT = "none", "top", "left", "right"
SNAP_EDGE_PX = 12                # how close to the edge a release counts as a snap gesture


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD)]


class WindowControl:
    """Moves and sizes one frameless window. Every entry point is safe to call from the HTTP
    bridge's handler threads: the Win32 calls used here (SetWindowPos, ShowWindow, GetCursorPos)
    marshal to the owning thread themselves, so no hop onto the UI thread is needed - which
    matters, because a drag issues one call per mouse-move and must never queue behind UI work.
    """

    def __init__(self, window):
        self.window = window             # the pywebview Window, for destroy() and the uid
        self._hwnd = None
        # Drag state, written by drag_start and read by drag_move on another thread. A tuple is
        # swapped in wholesale rather than mutated field by field so a half-written drag origin can
        # never be read.
        self._drag = None                # (cursor_x, cursor_y, win_x, win_y)
        self._restore = None             # the rect to go back to when un-maximizing
        self._pending_restore = None     # width of the maximized window this drag began on
        self._moved = False              # this drag has actually moved the window
        self._resize = None              # (edge, cursor_x, cursor_y, win_x, win_y, w, h)

    # ---------------------------------------------------------------- the handle
    @property
    def hwnd(self):
        """The top-level HWND, found once and cached.

        pywebview's winforms backend keeps its Forms in BrowserView.instances keyed by the
        window's uid, which is exact. The title lookup behind it is a fallback for a pywebview
        that has moved its internals - and is why the title is matched in full, including the
        " (local test)" suffix run() appends.
        """
        if not IS_WINDOWS:
            return None
        if self._hwnd:
            return self._hwnd
        try:
            from webview.platforms.winforms import BrowserView
            form = BrowserView.instances.get(self.window.uid)
            if form is not None:
                self._hwnd = int(form.Handle.ToInt64())
        except Exception:                # noqa: BLE001 — fall through to the title lookup
            pass
        if not self._hwnd:
            try:
                found = ctypes.windll.user32.FindWindowW(None, self.window.title)
                self._hwnd = int(found) or None
            except Exception:            # noqa: BLE001
                self._hwnd = None
        return self._hwnd

    # ---------------------------------------------------------------- geometry helpers
    @staticmethod
    def _rect(hwnd):
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    @staticmethod
    def _cursor():
        p = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    @staticmethod
    def _work_area(hwnd):
        """The usable rect of the monitor this window is on - the whole monitor minus the taskbar.

        Deliberately per-monitor (MonitorFromWindow) rather than SystemParametersInfo's
        SPI_GETWORKAREA, which only ever describes the primary display: the first drag probe
        opened the window on a second monitor at y=-179, and a maximize computed from the primary
        work area would have thrown it back onto the wrong screen.
        """
        mon = ctypes.windll.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(info))
        w = info.rcWork
        return w.left, w.top, w.right - w.left, w.bottom - w.top

    def _place(self, x, y, w=0, h=0, size=True):
        hwnd = self.hwnd
        if not hwnd:
            return False
        flags = SWP_NOZORDER | SWP_NOACTIVATE | (0 if size else SWP_NOSIZE)
        ctypes.windll.user32.SetWindowPos(hwnd, 0, int(x), int(y), int(w), int(h), flags)
        return True

    # ---------------------------------------------------------------- resize
    # The window's minimum, matching create_window's min_size in shell.py: the smallest size the
    # screens still lay out in (static/core.js scales the page down to meet it).
    MIN_W, MIN_H = 800, 560
    EDGES = ("n", "s", "e", "w", "ne", "nw", "se", "sw")

    def resize_start(self, edge):
        """Mouse went down on one of the page's border grips."""
        hwnd = self.hwnd
        if not hwnd or edge not in self.EDGES or self.is_maximized():
            return False
        cx, cy = self._cursor()
        x, y, w, h = self._rect(hwnd)
        self._resize = (edge, cx, cy, x, y, w, h)
        return True

    def resize_move(self):
        """Drag the grabbed edge to the cursor, keeping the opposite edge pinned."""
        st = self._resize
        if not st or not self.hwnd:
            return False
        edge, cx0, cy0, x0, y0, w0, h0 = st
        cx, cy = self._cursor()
        dx, dy = cx - cx0, cy - cy0
        x, y, w, h = x0, y0, w0, h0
        if "w" in edge:
            x, w = x0 + dx, w0 - dx
        elif "e" in edge:
            w = w0 + dx
        if "n" in edge:
            y, h = y0 + dy, h0 - dy
        elif "s" in edge:
            h = h0 + dy
        # Clamp at the minimum WITHOUT letting the pinned edge drift: dragging the left edge
        # rightwards past the minimum must stop the left edge, not start walking the right one.
        if w < self.MIN_W:
            x, w = (x0 + w0 - self.MIN_W) if "w" in edge else x, self.MIN_W
        if h < self.MIN_H:
            y, h = (y0 + h0 - self.MIN_H) if "n" in edge else y, self.MIN_H
        return self._place(x, y, w, h)

    def resize_end(self):
        self._resize = None
        return True

    # ---------------------------------------------------------------- maximize / restore
    def is_maximized(self):
        """Maximized means "filling this monitor's work area", because that is what
        toggle_maximize does - the real WindowState is never touched (see the module docstring)."""
        hwnd = self.hwnd
        if not hwnd:
            return False
        return self._rect(hwnd) == self._work_area(hwnd)

    def toggle_maximize(self):
        hwnd = self.hwnd
        if not hwnd:
            return False
        if self.is_maximized():
            x, y, w, h = self._restore or self._centred_default(hwnd)
            self._restore = None
            return self._place(x, y, w, h)
        self._restore = self._rect(hwnd)
        return self._place(*self._work_area(hwnd))

    def _centred_default(self, hwnd):
        """Somewhere sane to restore to when we have no remembered rect (the window was already
        filling the work area when the hub started)."""
        wx, wy, ww, wh = self._work_area(hwnd)
        w, h = min(1200, ww - 80), min(760, wh - 80)
        return wx + (ww - w) // 2, wy + (wh - h) // 2, w, h

    def minimize(self):
        hwnd = self.hwnd
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, SW_MINIMIZE)
        return True

    def close(self):
        """The title bar's X. Goes through pywebview's destroy() rather than a Win32 close so it
        fires the `closing` event - which is what CloseToTray hangs the hide-to-tray on. The X
        must keep meaning "put the hub in the tray", exactly as the native one did."""
        try:
            self.window.destroy()
        except Exception:                # noqa: BLE001 — a window that has already gone
            return False
        return True

    # ---------------------------------------------------------------- drag
    def drag_start(self):
        """Mouse went down on the title bar: remember where the cursor and the window are.

        A maximized window is NOT restored here, only armed to be. Windows restores a maximized
        window when you actually drag its caption, not when you merely press on it - and doing it
        on the press breaks double-click, which is a press with no movement at all: MEASURED, the
        second double-click on a maximized hub restored it on the first press and then the
        dblclick's own `maximize` put it straight back, so the window appeared stuck maximized.
        """
        hwnd = self.hwnd
        if not hwnd:
            return False
        cx, cy = self._cursor()
        x, y, w, h = self._rect(hwnd)
        self._drag = (cx, cy, x, y)
        self._pending_restore = w if self.is_maximized() else None
        self._moved = False
        return True

    def drag_move(self):
        """The page says the drag is still live; move the window to wherever the cursor now is.

        Takes no coordinates on purpose - see the module docstring on DPI.
        """
        drag = self._drag
        hwnd = self.hwnd
        if not drag or not hwnd:
            return False
        cx0, cy0, wx0, wy0 = drag
        if self._pending_restore is not None:
            # First actual movement of a drag that began on a maximized window: un-maximize now,
            # and put the restored window under the cursor in PROPORTION to where along the
            # maximized width it was grabbed, so grabbing near the right-hand buttons does not
            # teleport the window's left edge to the pointer.
            maxw, self._pending_restore = self._pending_restore, None
            _, _, rw, rh = self._restore or self._centred_default(hwnd)
            frac = (cx0 - wx0) / float(maxw) if maxw else 0.5
            wx0 = int(cx0 - rw * frac)    # the y origin is unchanged: same title bar height
            self._place(wx0, wy0, rw, rh)
            self._restore = None
            self._drag = (cx0, cy0, wx0, wy0)
        cx, cy = self._cursor()
        self._moved = True
        return self._place(wx0 + (cx - cx0), wy0 + (cy - cy0), size=False)

    def drag_end(self):
        """Mouse released. Returns the snap gesture the release landed on, and performs it."""
        drag, self._drag = self._drag, None
        moved, self._moved = self._moved, False
        self._pending_restore = None
        hwnd = self.hwnd
        if not drag or not hwnd:
            return SNAP_NONE
        # Only a drag that moved something can be a snap gesture. Without this, merely CLICKING
        # the title bar of a window that already sits near a screen edge would half-tile it -
        # and every double-click is two such clicks.
        if not moved:
            return SNAP_NONE
        return self.snap_for_cursor()

    # ---------------------------------------------------------------- snap
    def snap_for_cursor(self):
        """Aero Snap, by hand. The native move loop would have done this; it never runs here (see
        the module docstring), so the three gestures everyone actually uses - top to maximize,
        left and right to half-tile - are detected from where the cursor was let go. Win+Arrow is
        untouched by any of this and still works, because it is handled by the shell, not by us.
        """
        hwnd = self.hwnd
        if not hwnd:
            return SNAP_NONE
        cx, cy = self._cursor()
        wx, wy, ww, wh = self._work_area(hwnd)
        if not (wx - SNAP_EDGE_PX <= cx <= wx + ww + SNAP_EDGE_PX):
            return SNAP_NONE
        if cy <= wy + SNAP_EDGE_PX:
            self._restore = self._rect(hwnd)
            self._place(wx, wy, ww, wh)
            return SNAP_TOP
        if cx <= wx + SNAP_EDGE_PX:
            self._restore = self._rect(hwnd)
            self._place(wx, wy, ww // 2, wh)
            return SNAP_LEFT
        if cx >= wx + ww - SNAP_EDGE_PX:
            self._restore = self._rect(hwnd)
            self._place(wx + ww - ww // 2, wy, ww // 2, wh)
            return SNAP_RIGHT
        return SNAP_NONE
