"""The pywebview shell: create the native window, register the bridge, run.

This is the ONLY module that imports `webview` (pywebview), and only inside `run()`, so the Tk
path and the headless test suite never require pywebview to be installed.

On Windows pywebview renders through the Edge WebView2 runtime (present by default on Windows
11). On macOS it uses the system WKWebView (Cocoa). `run()` is the `--webui` entry point from
hub/app.py:main.

The window's X hides the hub to the system tray rather than quitting it (`CloseToTray` below).
"""
import os
import threading

from .. import catalogue as cat
from .. import i18n
from .. import paths
from .. import state as state_mod
from .. import game as game_mod
from .. import tray as tray_mod
from .. import update as update_mod
from ..version import APP_NAME, CATALOGUE_URL
from . import httpbridge
from . import window as window_mod
from .panel import WebPanel

# How often the catalogue is re-fetched while the hub is open. It is what decides how long after a
# release the update strip appears for someone who never closes the hub, so it is minutes rather
# than hours; the fetch is one small JSON over HTTPS and nothing renders unless it changed.
CATALOGUE_REFRESH_SECONDS = 60


class WebApp:
    """The minimal `app` the WebPanel needs: persisted state and the catalogue.

    Deliberately NOT HubApp — the web UI does not run Tk. It carries the same two attributes the
    panel reads (`state`, `catalogue`) plus the game dir, and fetches the catalogue in the
    background exactly like HubApp does, re-emitting the snapshot when it lands — and then keeps
    re-fetching it, so a release that lands while the hub is open is noticed without a restart."""

    def __init__(self, language=None, local_repo=None):
        self.state = state_mod.load()
        self.catalogue = None
        self.local_repo = local_repo
        self.panel = None                # set by run() once the panel exists
        self._stop = threading.Event()    # ends the catalogue refresh loop

        lang = language or self.state.get("language")
        if lang not in i18n.CODES:
            lang = i18n.detect_system_language()
        i18n.set_language(lang)
        self.state["language"] = lang

        remembered = self.state.get("game_dir")
        self.game_dir = remembered if game_mod.is_game_dir(remembered) else game_mod.find_game_dir()
        state_mod.reconcile(self.state, self.game_dir)
        if self.game_dir and self.state.get("game_dir") != self.game_dir:
            self.state["game_dir"] = self.game_dir
        state_mod.save(self.state)

    def load_catalogue_async(self):
        """Fetch the catalogue now, then keep fetching it every CATALOGUE_REFRESH_SECONDS.

        THE REPEAT IS THE POINT (Sam, 2026-09-16). The update strip along the top of the window is
        derived from the catalogue every snapshot (webui/snapshot.py _update), so it can only appear
        when the catalogue in memory says a newer hub exists. Until now the catalogue was fetched
        exactly once, on start — which meant a release that went out while the hub was open was
        invisible to it, and the strip only ever showed up after a restart. A hub that lives in the
        tray for days is the normal case, so "restart to find out there is an update" is the same as
        no update prompt at all.

        Refreshing also keeps the gamemode list and the map pool current, which is why the snapshot
        is re-emitted (on the UI thread) whenever the fetched catalogue differs from the one held.

        Runs on a daemon thread and never blocks startup: a failed refresh keeps the catalogue
        already in hand and simply tries again at the next tick. Only the FIRST fetch falls back to
        the cached copy in state.json, because that is the offline-start case — later on, the copy in
        memory is at least as good as the cache.

        THERE IS NO SECOND UPDATE PROMPT. Until 2.0.22 a newer version also put up a native Windows
        MessageBox here, from the days before the web UI drew a strip of its own; it said the same
        thing the strip says, only on start and before the window was even up, so every user who
        kept the hub open got asked twice about the same release and never at the moment it landed.
        The strip is the offer now — do not add the message box back. (What it was really insuring
        against was the page failing to render at all, in 2.0.0 and 2.0.3; the cover for that today
        is hub/app.py:main falling back to the Tk UI, which draws its own strip.)"""
        def work():
            while True:
                data = self._fetch_catalogue()
                if data:
                    self._use_catalogue(data)
                if self._stop.wait(CATALOGUE_REFRESH_SECONDS):
                    return

        threading.Thread(target=work, name="hub-catalogue", daemon=True).start()

    def _fetch_catalogue(self):
        """One catalogue fetch. Returns the dict, or None when it failed and there is nothing to
        fall back to (the cache is only a fallback for the very first fetch)."""
        try:
            if self.local_repo:
                from urllib.request import pathname2url
                local_cat = os.path.join(str(self.local_repo), "server", "public", "catalogue.json")
                data = cat.fetch_catalogue("file:" + pathname2url(os.path.abspath(local_cat)))
                cat.localize_to_repo(data, self.local_repo)
                return data
            return cat.fetch_catalogue(CATALOGUE_URL)
        except Exception:                # noqa: BLE001 — offline / 404 / bad JSON
            if self.catalogue is None:
                return self.state.get("catalogue_cache")
            return None

    def _use_catalogue(self, data):
        """Adopt a freshly fetched catalogue. Cheap and silent when nothing changed: an unchanged
        refresh must not rewrite state.json or push a snapshot every few minutes."""
        if data == self.catalogue:
            return
        self.catalogue = data
        self.state["catalogue_cache"] = data
        try:
            state_mod.update_fields({"catalogue_cache": data})
        except Exception:                # noqa: BLE001
            pass
        if self.panel is not None:
            # Re-emit on the UI thread: the update strip, the map pool and the gamemode-installed
            # flag all read the catalogue.
            # ...and, if the server changed a gamemode's rules under us (a test build playing short
            # rounds), rebuild the pak so a deploy is in the next match without anyone pressing
            # anything. No override, no work: see the screen. This is the other thing the refresh
            # loop buys besides the update strip - a variable changed on Railway reaches a hub that
            # is already open, which is the whole point of setting the rounds from there.
            # ONE post for both, not two: an adoption is one trip to the UI thread (a test counts
            # them, and rightly - this runs every refresh for the life of the hub).
            def adopted():
                self.panel.on_change()
                self._apply_rules_override()

            self.panel.post(adopted)

    def _apply_rules_override(self):
        """UI thread: let the gamemodes screen rebuild the pak when the catalogue's rules override
        no longer matches the one it was built with. Never fatal - a hub that cannot rebuild is
        still a hub."""
        try:
            from .screens import gamemodes as gamemodes_screen
            gamemodes_screen.rebuild_for_rules_override(self.panel)
        except Exception:            # noqa: BLE001
            pass

    def stop(self):
        """End the refresh loop (run() calls this on the way out)."""
        self._stop.set()


class CloseToTray:
    """The X hides the hub to the system tray; the tray icon brings it back.

    WHY (Sam, 2026-09-15). hub/tray.py and the "the X does not quit" behaviour were built for the
    Tk UI (hub/app.py:_on_close) and never followed the UI to pywebview — so from 2.0.0, when the
    web UI became the DEFAULT, the X quit the hub outright for everybody. That is the wrong answer
    to the commonest reason anyone presses it: the game is starting and they want the window off
    the screen, not the hub gone. Killing it there also loses the live match state the hub is in
    the middle of, and the way back is a shortcut, which is how the instance pile-up in
    hub/singleton.py got started in the first place.

    Deliberately the same shape as the Tk path, so the two UIs behave identically: X hides and
    shows a one-time balloon explaining where the hub went; a tray click opens it again;
    right-click -> "Close Lights Out" is the only thing that ends the process. The tray menu is the ONLY
    real quit in the web UI (the page has no quit button), which is why `quit` has to work even
    when the window is hidden.

    FAILS OPEN. No pystray, no Windows, an icon that will not start, a hide that raises: every one
    of those paths lets the X close the window exactly as it does today. A hub whose X does
    nothing is worse than a hub that closes — the user would just press it again and conclude the
    window is stuck.

    THREADS. pystray's menu callbacks run on pystray's own thread and pywebview's `closing` runs on
    the GUI thread; `window.show/hide/destroy` marshal onto the GUI thread themselves (pywebview's
    winforms backend Invokes, and Invoke from the GUI thread runs inline), so no queue is needed
    here — unlike the Tk path, where every tray callback has to hop through `app.q`.
    """

    # hooks, so the tests can force the tray on/off without pystray installed (mirrors
    # HubApp.tray_available in hub/app.py)
    tray_available = staticmethod(tray_mod.available)

    def __init__(self, window):
        self.window = window
        self.tray = None                 # a tray_mod.Tray once start() has succeeded
        self._quitting = False           # "Close Lights Out" was chosen: let the next close through
        self._hinted = False             # the "still running in the tray" balloon, shown once

    def start(self) -> bool:
        """Put the icon in the tray and take over the window's X. False (and the X keeps quitting)
        when there is no tray to hide to."""
        if not self.tray_available():
            return False
        try:
            self.tray = tray_mod.Tray(APP_NAME, i18n.t("tray_open"), i18n.t("tray_close"),
                                      on_open=self.open, on_quit=self.quit)
            self.tray.start()
        except Exception:                # noqa: BLE001 — no tray, the X closes normally
            self.tray = None
            return False
        self.window.events.closing += self.on_closing
        return True

    def on_closing(self):
        """pywebview's `closing` handler, on the GUI thread. False cancels the close.

        Takes no arguments on purpose: pywebview passes the window to any handler with a `window`
        parameter (webview/event.py), and a bound method with none is called bare.
        """
        if self.tray is None or self._quitting:
            return None                  # nothing to hide to, or we asked for this: really close
        try:
            self.window.hide()
        except Exception:                # noqa: BLE001
            return None                  # could not hide: close rather than leave a dead X
        if not self._hinted:
            self._hinted = True
            self.tray.notify(i18n.t("tray_hint"), APP_NAME)
        return False

    def open(self):
        """Tray icon clicked (pystray's thread)."""
        try:
            self.window.show()
        except Exception:                # noqa: BLE001 — a window that has already gone
            pass

    def quit(self):
        """Tray menu "Close Lights Out" (pystray's thread): end the hub for real."""
        self._quitting = True
        self.stop()                      # the icon goes now, so the click has visible effect
        try:
            self.window.destroy()        # -> closing again, which _quitting now lets through
        except Exception:                # noqa: BLE001
            pass

    def stop(self):
        """Take the icon out of the tray. Idempotent; also called when run() unwinds any other
        way, so a webview that dies on its own does not leave an icon behind."""
        tray, self.tray = self.tray, None
        if tray is not None:
            tray.stop()


def run(language=None, local_repo=None):
    """Open the web UI window and block until it closes. The `--webui` entry point."""
    import webview          # imported here ONLY: the Tk path never needs pywebview

    app = WebApp(language=language, local_repo=local_repo)
    panel = WebPanel(app)
    app.panel = panel

    # The JS <-> Python channel is a localhost HTTP bridge, NOT pywebview's js_api/evaluate_js
    # (which proved unreliable on WebView2 — see httpbridge.py). The bridge also serves the static
    # files, so the window loads its own http://127.0.0.1:<port> origin and the page talks to it
    # over fetch. pywebview's own http_server is off; no js_api is registered.
    server, url = httpbridge.start(panel)

    # The page scales itself to whatever size this window is (static/core.js applyScale), so the
    # minimum no longer has to be the size the screens were drawn at - it only has to be a size
    # the page can still shrink to and show everything, which is 800x560 outer (~784x520 of page,
    # a 0.74 zoom, comfortably above core.js's 0.7 floor). Anyone on a small laptop can now put
    # the hub in a corner of the screen without losing the bottom of it.
    # FRAMELESS ON WINDOWS. The hub wears its own title bar (static/titlebar.js), drawn into the
    # same #topbar the nav already lives in, so the window reads as one piece instead of a dark app
    # under a light grey Windows caption. Everything the native frame did is re-implemented in
    # hub/webui/window.py — read its docstring before changing any of this, it is all measured.
    #
    # `shadow=True` gives the Win11 drop shadow and rounded corners; pywebview applies it BEFORE it
    # strips the frame (webview/platforms/winforms.py), so the order is already right.
    # `easy_drag=False` is not optional: pywebview's easy drag binds mousedown to the whole window,
    # which would move the hub whenever anyone dragged anything inside it.
    #
    # Windows only. On macOS the Cocoa backend keeps its frame and its traffic lights, and the page
    # notices there is no title bar to draw because /window/* answers empty (httpbridge._window).
    frameless = os.name == "nt"
    window = webview.create_window(
        APP_NAME + (" (local test)" if local_repo else ""),
        url=url,
        width=1200, height=760, min_size=(800, 560),
        background_color="#0b0b0c",
        text_select=False,
        frameless=frameless, easy_drag=False, shadow=frameless,
    )
    # The window is still handed to the panel: the settings screen's native folder picker calls
    # window.create_file_dialog. It is no longer the state channel.
    panel.window = window
    # The title bar's hands. Hung on the panel because the bridge is already running by now (it has
    # to be — the window needs its URL), so the handler picks it up lazily off `panel.chrome`.
    panel.chrome = window_mod.WindowControl(window) if frameless else None

    # The X hides to the tray instead of quitting. Started from on_started rather than here so the
    # icon only ever appears once the GUI loop is actually up: a webview that fails to start falls
    # back to the Tk UI (hub/app.py:main), which puts up a tray icon of its own, and two "LO" icons
    # in the tray — one of them attached to a window that no longer exists — is worse than none.
    closer = CloseToTray(window)
    # The panel needs it too: Settings' Uninstall has to END the hub, and a bare window.destroy()
    # would be swallowed by the X handler above and merely hide it (see WebPanel.quit).
    panel.closer = closer

    def on_started():
        closer.start()
        panel.start()
        # Tidy the previously downloaded installer / older exes on start, exactly like the Tk main
        # (hub/app.py). on_started runs on pywebview's worker thread, so this disk work never
        # blocks the UI thread.
        try:
            update_mod.clean_old_versions()
        except Exception:            # noqa: BLE001 — best-effort tidy, never block launch
            pass
        app.load_catalogue_async()

    try:
        webview.start(on_started, gui="edgechromium" if os.name == "nt" else None, http_server=False)
    finally:
        closer.stop()                # the icon must not outlive the window it opens
        app.stop()                   # and neither must the catalogue refresh loop
        panel.shutdown()
        try:
            server.shutdown()
        except Exception:            # noqa: BLE001 — best-effort on close
            pass
