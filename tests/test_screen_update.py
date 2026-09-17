"""Headless tests for the app self-update flow (the top-of-window update strip).

The web UI's update machinery is testable without a webview or Tk (docs/ui-redesign-plan.md:
Testing): the snapshot's ``update`` slice and the ``apply_hub_update`` core verb run against a real
WebPanel driven by an InlineScheduler, asserting on the JSON dict and on the reused hub/update.py
machinery. No pywebview, no window.

The Mac has no tkinter, so tests/test_hub.py cannot import here; this file stubs tkinter into
sys.modules defensively (nothing under test needs it, but the import stays honest on any box) and
then imports hub.webui.snapshot / hub.webui.bridge directly.

Run directly (``python tests/test_screen_update.py``) or import the test functions.
"""
import io
import os
import sys
import tempfile
import threading
import time
import types

# Defensive tkinter stub: nothing under test imports tkinter, but this keeps the file importable on
# a box without it (the Mac) even if an import chain ever reaches for it.
for name in ("tkinter", "tkinter.ttk", "tkinter.messagebox", "tkinter.filedialog", "tkinter.font"):
    sys.modules.setdefault(name, types.ModuleType(name))

# Redirect the state dir to a throwaway BEFORE importing hub, so nothing touches the real state.json.
os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-update-test-"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import i18n                        # noqa: E402
from hub import update as update_mod        # noqa: E402
from hub.version import HUB_VERSION         # noqa: E402
from hub.webui import httpbridge            # noqa: E402
from hub.webui.bridge import Api            # noqa: E402
from hub.webui.panel import WebPanel        # noqa: E402
from hub.webui.scheduler import InlineScheduler  # noqa: E402
from hub.webui.snapshot import state_snapshot     # noqa: E402


# ---------------------------------------------------------------- harness
def _catalogue(version="9.9.9", required=False):
    """A catalogue whose hub object advertises a (usually newer) version."""
    return {
        "hub": {
            "version": version,
            "download_url": "https://example.invalid/LightsOut-Setup-%s.exe" % version,
            "page_url": "https://example.invalid/download",
            "sha256": "deadbeef",
            "size": 12345,
            "kind": update_mod.INSTALLER_KIND,
            "required": required,
        }
    }


class _FakeApp:
    def __init__(self, catalogue=None):
        self.state = {"installed": {}, "auth": None}
        self.catalogue = catalogue
        self.game_dir = None


def _panel(catalogue=None):
    panel = WebPanel(_FakeApp(catalogue=catalogue), scheduler=InlineScheduler(), window=None)
    s = panel.session
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6}
    s.token = "tok"
    s.phase = "idle"
    s.connected = True
    return panel


def _slice(panel):
    return state_snapshot(panel.session, panel)["update"]


class _Frozen:
    """Context manager: make update_mod.own_exe() report a frozen/installed exe path."""

    def __init__(self, path="/fake/install/LightsOut.exe"):
        self.path = path
        self._orig = None

    def __enter__(self):
        self._orig = update_mod.own_exe
        update_mod.own_exe = lambda: self.path
        return self

    def __exit__(self, *exc):
        update_mod.own_exe = self._orig


def _join_update_worker(timeout=5.0):
    """Wait for the daemon download worker (if any) to finish."""
    deadline = time.time() + timeout
    for th in list(threading.enumerate()):
        if th.name == "hub-update":
            th.join(timeout=max(0.0, deadline - time.time()))


# ---------------------------------------------------------------- snapshot: the update slice
def test_update_slice_present_and_serialisable():
    """The base snapshot always carries an ``update`` slice with the expected keys."""
    import json
    i18n.set_language("en")
    slc = _slice(_panel(catalogue=None))
    json.dumps(slc)                                   # never raises
    for key in ("available", "current", "latest", "forced",
                "download_url", "size", "sha256", "status", "progress", "error"):
        assert key in slc, "update slice missing key %r" % key
    assert slc["current"] == HUB_VERSION
    assert slc["available"] is False                  # no catalogue -> nothing to offer
    assert slc["status"] == "idle"


def test_update_available_when_frozen_and_catalogue_is_newer():
    """A newer catalogue hub.version + a frozen exe -> available True, latest reflected."""
    i18n.set_language("en")
    panel = _panel(catalogue=_catalogue(version="9.9.9"))
    with _Frozen():
        slc = _slice(panel)
    assert slc["available"] is True
    assert slc["latest"] == "9.9.9"
    assert slc["forced"] is False
    assert slc["download_url"] and slc["sha256"] == "deadbeef"


def test_update_not_offered_on_a_dev_checkout():
    """Same newer catalogue, but NOT frozen (own_exe None) -> no offer: an update that could not be
    applied is never shown (the frozen guard, mirroring clean_old_versions)."""
    i18n.set_language("en")
    panel = _panel(catalogue=_catalogue(version="9.9.9"))
    # own_exe() is None in this test process (not frozen), so:
    assert update_mod.own_exe() is None
    assert _slice(panel)["available"] is False


def test_update_not_offered_when_catalogue_not_newer():
    """A catalogue at the current version is not an update even when frozen."""
    i18n.set_language("en")
    panel = _panel(catalogue=_catalogue(version=HUB_VERSION))
    with _Frozen():
        assert _slice(panel)["available"] is False


def test_forced_update_flag_from_catalogue_required():
    """hub.required -> forced True (the blocking overlay path)."""
    i18n.set_language("en")
    panel = _panel(catalogue=_catalogue(version="9.9.9", required=True))
    with _Frozen():
        slc = _slice(panel)
    assert slc["available"] is True and slc["forced"] is True


# ---------------------------------------------------------------- the verb
def test_apply_hub_update_is_a_core_verb():
    """The bridge allow-list dispatches apply_hub_update, and Api exposes it."""
    assert "apply_hub_update" in httpbridge._CORE_VERBS
    assert "apply_hub_update" in httpbridge._allowed_verbs()
    panel = _panel(catalogue=_catalogue())
    assert callable(getattr(Api(panel), "apply_hub_update", None))


def test_apply_hub_update_downloads_verifies_and_launches():
    """apply_hub_update downloads to dest_path (passing the catalogue sha256) then launches the
    installer with its kind — reusing hub/update.py, off the UI thread."""
    i18n.set_language("en")
    cat = _catalogue(version="9.9.9")
    panel = _panel(catalogue=cat)
    seen = {}

    def fake_download(url, dest, progress=None, expect_sha=None):
        seen["url"] = url
        seen["dest"] = dest
        seen["sha"] = expect_sha
        if progress:
            progress(50, 100)
            progress(100, 100)
        return dest

    def fake_launch(path, kind=None):
        seen["launch"] = (path, kind)

    orig = (update_mod.own_exe, update_mod.dest_path, update_mod.download, update_mod.launch)
    update_mod.own_exe = lambda: "/fake/install/LightsOut.exe"
    update_mod.dest_path = lambda url, v: "/tmp/hub-update-test-installer.exe"
    update_mod.download = fake_download
    update_mod.launch = fake_launch
    try:
        Api(panel).apply_hub_update()                 # posts start_hub_update (runs inline)
        _join_update_worker()
    finally:
        (update_mod.own_exe, update_mod.dest_path, update_mod.download, update_mod.launch) = orig

    assert seen.get("sha") == "deadbeef"              # sha256 from the catalogue was verified
    assert seen.get("url") == cat["hub"]["download_url"]
    assert seen.get("launch") == ("/tmp/hub-update-test-installer.exe", update_mod.INSTALLER_KIND)
    # progress was mirrored into the slice as the download ran
    assert panel.last_state["update"]["progress"] == 100


def test_apply_hub_update_reports_download_errors():
    """A failed download lands in update.status/error, not an exception."""
    i18n.set_language("en")
    panel = _panel(catalogue=_catalogue(version="9.9.9"))

    def boom(*a, **k):
        raise RuntimeError("network is down")

    orig = (update_mod.own_exe, update_mod.dest_path, update_mod.download)
    update_mod.own_exe = lambda: "/fake/install/LightsOut.exe"
    update_mod.dest_path = lambda url, v: "/tmp/x.exe"
    update_mod.download = boom
    try:
        Api(panel).apply_hub_update()
        _join_update_worker()
    finally:
        (update_mod.own_exe, update_mod.dest_path, update_mod.download) = orig

    slc = panel.last_state["update"]
    assert slc["status"] == "error"
    assert "network is down" in slc["error"]


def test_apply_hub_update_is_a_noop_on_a_dev_checkout():
    """Not frozen -> start_hub_update refuses, status stays idle (no installer over a checkout)."""
    i18n.set_language("en")
    panel = _panel(catalogue=_catalogue(version="9.9.9"))
    assert update_mod.own_exe() is None
    Api(panel).apply_hub_update()
    _join_update_worker()
    assert panel.update_state["status"] == "idle"


# ---------------------------------------------------------------- i18n keys the strip uses
def test_update_strip_i18n_keys_exist():
    """Every key core.js renderUpdate references must exist in English (guards the JS-key test)."""
    for key in ("update_banner", "update_banner_action", "update_banner_later",
                "update_banner_progress", "update_banner_failed", "update_title", "update_body"):
        assert key in i18n.STRINGS["en"], "missing i18n key %r" % key


# ---------------------------------------------------------------- the catalogue refresh loop
def _webapp(panel=None, catalogue=None):
    """A WebApp with none of __init__'s disk work (state load, game-dir scan, language pick):
    the refresh loop only ever touches these four attributes."""
    from hub.webui.shell import WebApp
    app = WebApp.__new__(WebApp)
    app.state = {}
    app.catalogue = catalogue
    app.local_repo = None
    app.panel = panel
    app._stop = threading.Event()
    return app


class _Catalogues:
    """Stands in for catalogue.fetch_catalogue: hands out the next answer each call (the last one
    repeats forever). An Exception instance is raised instead of returned."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0
        self._orig = None

    def __call__(self, *_a, **_kw):
        self.calls += 1
        a = self.answers[min(self.calls, len(self.answers)) - 1]
        if isinstance(a, Exception):
            raise a
        return a

    def __enter__(self):
        from hub.webui import shell as shell_mod
        self._orig = shell_mod.cat.fetch_catalogue
        shell_mod.cat.fetch_catalogue = self
        return self

    def __exit__(self, *exc):
        from hub.webui import shell as shell_mod
        shell_mod.cat.fetch_catalogue = self._orig


class _FastRefresh:
    """Shrink the refresh interval so a test can watch the loop go round."""

    def __init__(self, seconds=0.02):
        self.seconds = seconds
        self._orig = None

    def __enter__(self):
        from hub.webui import shell as shell_mod
        self._orig = shell_mod.CATALOGUE_REFRESH_SECONDS
        shell_mod.CATALOGUE_REFRESH_SECONDS = self.seconds
        return self

    def __exit__(self, *exc):
        from hub.webui import shell as shell_mod
        shell_mod.CATALOGUE_REFRESH_SECONDS = self._orig


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_catalogue_is_refetched_while_the_hub_is_open():
    """THE BUG: the update strip only ever appeared after a restart, because the catalogue was
    fetched exactly once. A release that lands mid-session must reach the running hub."""
    old, new = _catalogue(version="0.0.1"), _catalogue(version="9.9.9")
    panel = _panel(catalogue=old)
    app = _webapp(panel=panel, catalogue=old)
    panel.app = app
    with _Frozen(), _FastRefresh(), _Catalogues(old, new) as fetch:
        app.load_catalogue_async()
        try:
            assert _wait_for(lambda: app.catalogue == new), "catalogue never refreshed (%d fetches)" % fetch.calls
            assert _slice(panel)["available"] is True, "the update strip is still not offered"
            assert _slice(panel)["latest"] == "9.9.9"
        finally:
            app.stop()


def test_an_unchanged_refresh_changes_nothing():
    """The normal case is "still the same catalogue": it must not re-save state or push a
    snapshot every few minutes."""
    same = _catalogue(version="9.9.9")
    panel = _panel(catalogue=same)
    app = _webapp(panel=panel, catalogue=None)
    panel.app = app
    posts = []
    panel.post = lambda fn: posts.append(fn)
    with _FastRefresh(), _Catalogues(same) as fetch:
        app.load_catalogue_async()
        try:
            assert _wait_for(lambda: fetch.calls >= 3), "the loop stopped after %d fetches" % fetch.calls
        finally:
            app.stop()
    assert len(posts) == 1, "an unchanged refresh re-emitted the snapshot (%d posts)" % len(posts)


def test_a_failed_refresh_keeps_the_catalogue_it_has():
    """Offline for one tick must not blank the gamemode list or the update offer."""
    good = _catalogue(version="9.9.9")
    app = _webapp(catalogue=None)
    app.state = {"catalogue_cache": None}
    with _FastRefresh(), _Catalogues(good, OSError("offline")) as fetch:
        app.load_catalogue_async()
        try:
            assert _wait_for(lambda: fetch.calls >= 3), "the loop stopped after %d fetches" % fetch.calls
            assert app.catalogue == good, "a failed refresh threw the catalogue away"
        finally:
            app.stop()


def test_the_first_fetch_still_falls_back_to_the_cache():
    """Starting offline is the one case the cached copy is for."""
    cached = _catalogue(version="9.9.9")
    app = _webapp(catalogue=None)
    app.state = {"catalogue_cache": cached}
    with _FastRefresh(), _Catalogues(OSError("offline")):
        app.load_catalogue_async()
        try:
            assert _wait_for(lambda: app.catalogue == cached), "the offline start ignored the cache"
        finally:
            app.stop()


def test_no_native_update_message_box():
    """The Windows MessageBox that asked about updates on start is gone: it said the same thing as
    the in-app strip, one restart earlier, and only on start (Sam, 2026-09-16)."""
    from hub.webui import shell as shell_mod
    src = io.open(shell_mod.__file__, encoding="utf-8").read()
    assert "MessageBoxW" not in src, "the native update popup is still there"
    assert not hasattr(shell_mod.WebApp, "_offer_hub_update"), "_offer_hub_update survived"


_TESTS = [
    test_update_slice_present_and_serialisable,
    test_update_available_when_frozen_and_catalogue_is_newer,
    test_update_not_offered_on_a_dev_checkout,
    test_update_not_offered_when_catalogue_not_newer,
    test_forced_update_flag_from_catalogue_required,
    test_apply_hub_update_is_a_core_verb,
    test_apply_hub_update_downloads_verifies_and_launches,
    test_apply_hub_update_reports_download_errors,
    test_apply_hub_update_is_a_noop_on_a_dev_checkout,
    test_update_strip_i18n_keys_exist,
    test_catalogue_is_refetched_while_the_hub_is_open,
    test_an_unchanged_refresh_changes_nothing,
    test_a_failed_refresh_keeps_the_catalogue_it_has,
    test_the_first_fetch_still_falls_back_to_the_cache,
    test_no_native_update_message_box,
]


def main():
    results = []
    for fn in _TESTS:
        try:
            fn()
            results.append((fn.__name__, True))
            print("ok   %s" % fn.__name__)
        except Exception:                              # noqa: BLE001
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
