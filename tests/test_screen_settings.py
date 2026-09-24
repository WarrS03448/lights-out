"""Headless tests for the Settings screen's Python half (hub/webui/screens/settings.py).

The valuable half of the web UI is testable without a webview (docs/ui-redesign-plan.md: Testing):
the snapshot slice and the bridge verbs run against a real LiveSession driven by the fake live
client, asserting on the JSON dict and on which session/state method each verb reached — the same
shape as test_hub.py's webui tests. No pywebview, no Tk, no window.

Run directly (``python tests/test_screen_settings.py``) or import the test functions.
"""
import os
import sys
import tempfile

# Redirect the state dir to a throwaway BEFORE importing hub, so saving settings never touches the
# developer's real state.json (test_hub.py does the same at import time).
os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-settings-test-"))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub import competitive as C           # noqa: E402
from hub import i18n                        # noqa: E402
from hub import sounds as sounds_mod        # noqa: E402
from hub.webui.bridge import Api            # noqa: E402
from hub.webui.panel import WebPanel        # noqa: E402
from hub.webui.scheduler import InlineScheduler  # noqa: E402
from hub import uninstall as uninstall_mod             # noqa: E402
from hub.webui.screens import settings as settings_mod  # noqa: E402
from hub.webui.snapshot import state_snapshot           # noqa: E402


# ---------------------------------------------------------------- harness
class _FakeApp:
    """The minimal `app` a WebPanel reads: persisted state (dict), catalogue, and game_dir."""

    def __init__(self, state=None):
        self.state = state if state is not None else {"installed": {}, "auth": None}
        self.catalogue = None
        self.game_dir = None


class _CallRecorder:
    """Stands in for a session: records which verb the bridge/verb called, with what args."""

    def __init__(self):
        self.calls = []
        self.me = None

    def locked_in(self):
        return False

    def __getattr__(self, name):
        def rec(*args, **kw):
            self.calls.append((name, args, kw))
        return rec


def _panel(state=None):
    """A headless WebPanel with a signed-in LiveSession (no Tk, no pywebview, no window)."""
    panel = WebPanel(_FakeApp(state=state), scheduler=InlineScheduler(), window=None)
    s = panel.session
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6,
            "matches": 34, "wins": 19}
    s.token = "tok"
    s.phase = "idle"
    s.connected = True
    return panel, s


def _slice(panel):
    return state_snapshot(panel.session, panel)["settings"]


def _make_game_dir(root):
    """A folder that passes game_mod.is_game_dir: <root>/Bodycam/Content/Paks/<x>.pak."""
    paks = os.path.join(root, "Bodycam", "Content", "Paks")
    os.makedirs(paks, exist_ok=True)
    with open(os.path.join(paks, "pakchunk0.pak"), "wb") as f:
        f.write(b"\x00" * 16)
    return root


# ---------------------------------------------------------------- snapshot
def test_click_sound_preferences_persist(tmp_path, monkeypatch):
    from hub import state as state_mod
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod.paths, "state_file", lambda: path)
    panel, _ = _panel()
    assert _slice(panel)["click_sound"] == {"volume": 35, "enabled": True}
    api = Api(panel)
    api.settings_set_click_volume(62)
    api.settings_set_click_enabled(False)
    saved = state_mod.load(path)
    assert saved["ui_click_volume"] == 62
    assert saved["ui_click_enabled"] is False
    restored, _ = _panel(saved)
    assert _slice(restored)["click_sound"] == {"volume": 62, "enabled": False}
    api.settings_set_click_volume(999)
    assert _slice(panel)["click_sound"]["volume"] == 100
    api.settings_set_click_volume(0)
    assert _slice(panel)["click_sound"]["volume"] == 0


def test_settings_snapshot_shape():
    """The slice carries every control group the screen renders, JSON-serialisable."""
    import json
    i18n.set_language("en")
    panel, s = _panel()
    slc = _slice(panel)
    json.dumps(slc)                                   # never raises

    for key in ("game", "language", "sound", "account", "readiness", "strings"):
        assert key in slc, "missing settings.%s" % key

    # account reflects the signed-in identity, with a masked steam id (not the whole 64-bit value)
    assert slc["account"]["signed_in"] is True
    assert slc["account"]["persona"] == "Sam"
    assert slc["account"]["steam_id_masked"] and "…" in slc["account"]["steam_id_masked"]
    assert slc["account"]["steam_id"] not in ("", None)
    assert slc["account"]["locked"] is False

    # language lists all seven cultures, current marked
    assert slc["language"]["current"] == "en"
    codes = [o["code"] for o in slc["language"]["options"]]
    assert codes == i18n.CODES

    # sound defaults to the shared default when nothing is persisted
    assert slc["sound"]["volume"] == sounds_mod.DEFAULT_VOLUME
    assert slc["sound"]["enabled"] is True


def test_settings_strings_cover_every_language():
    """Every language ships the same settings-string keys (English-filled), so the 7-language
    coverage the app promises holds for this screen's own strings too."""
    en_keys = set(settings_mod._S["en"])
    assert en_keys, "no English settings strings"
    for code in i18n.CODES:
        i18n.set_language(code)
        panel, _ = _panel()
        strings = _slice(panel)["strings"]
        missing = [k for k in en_keys if not strings.get(k)]
        assert not missing, "language %s missing settings strings: %s" % (code, missing)
    i18n.set_language("en")


def test_readiness_does_not_require_bodycam_closed():
    """Opening Bodycam does not introduce a readiness warning or checklist item."""
    from hub import game as game_mod
    i18n.set_language("en")
    panel, s = _panel()
    s.connected = True
    real = game_mod.game_running_cached
    try:
        game_mod.game_running_cached = lambda *a, **k: False
        items = {it["key"]: it for it in _slice(panel)["readiness"]}
        assert set(items) == {"steam", "servers"}

        game_mod.game_running_cached = lambda *a, **k: True
        assert {it["key"]: it for it in _slice(panel)["readiness"]} == items
    finally:
        game_mod.game_running_cached = real


def test_settings_readiness_reflects_state():
    """Account and server connectivity still reflect whether the player is ready."""
    i18n.set_language("en")
    panel, s = _panel()
    s.connected = True
    items = {it["key"]: it for it in _slice(panel)["readiness"]}
    assert set(items) == {"steam", "servers"}
    assert items["steam"]["state"] == "ok"
    assert items["servers"]["state"] == "ok"

    # signed out + server offline flip to blocking
    s.me = None
    s.connected = False
    items = {it["key"]: it for it in _slice(panel)["readiness"]}
    assert items["steam"]["state"] == "bad"
    assert items["servers"]["state"] == "bad"


def test_settings_sound_slice_follows_persisted_volume():
    """The slice reads the same comp_sound_volume key the Tk panel wrote, so both views agree."""
    i18n.set_language("en")
    panel, s = _panel(state={"installed": {}, "auth": None, "comp_sound_volume": 0})
    slc = _slice(panel)
    assert slc["sound"]["volume"] == 0 and slc["sound"]["enabled"] is False


# ---------------------------------------------------------------- verbs
def test_settings_verbs_are_registered():
    """The screen registered exactly its namespaced verbs on the bridge (no collision with core or
    another screen)."""
    from hub.webui.screens import SCREEN_VERBS
    for verb in ("settings_browse_game_path", "settings_set_game_path",
                 "settings_set_sound_volume", "settings_test_sound", "settings_sign_out"):
        assert verb in SCREEN_VERBS, "verb not registered: %s" % verb
    # the language picker uses the CORE verb, not a settings one
    assert "settings_set_language" not in SCREEN_VERBS


def test_settings_set_volume_persists_and_reemits():
    """settings_set_sound_volume writes comp_sound_volume (clamped) and re-emits the snapshot."""
    i18n.set_language("en")
    panel, s = _panel()
    api = Api(panel)
    api.settings_set_sound_volume(42)
    assert panel.app.state["comp_sound_volume"] == 42
    assert panel.app.state["comp_sound_last"] == 42
    assert panel.last_state["settings"]["sound"]["volume"] == 42

    # out-of-range is clamped, and a zero does not overwrite the remembered "last"
    api.settings_set_sound_volume(999)
    assert panel.app.state["comp_sound_volume"] == 100
    api.settings_set_sound_volume(0)
    assert panel.app.state["comp_sound_volume"] == 0
    assert panel.app.state["comp_sound_last"] == 100


def test_settings_set_game_path_validates_and_saves():
    """settings_set_game_path accepts a real Bodycam install (or its steamapps\\common parent),
    persists game_dir, and rejects a non-install with the localized message."""
    i18n.set_language("en")
    root = tempfile.mkdtemp(prefix="hub-game-")
    _make_game_dir(root)
    panel, s = _panel()
    api = Api(panel)

    # the game folder itself
    api.settings_set_game_path(root)
    assert panel.app.state["game_dir"] == os.path.normpath(root)
    assert panel.app.game_dir == os.path.normpath(root)
    assert _slice(panel)["game"]["detected"] is True
    assert _slice(panel)["game"]["error"] == ""

    # a non-install: no change to game_dir, an error surfaces
    bad = tempfile.mkdtemp(prefix="hub-notgame-")
    api.settings_set_game_path(bad)
    assert panel.app.state["game_dir"] == os.path.normpath(root)   # unchanged
    assert _slice(panel)["game"]["error"]                          # a message is shown


def test_settings_set_game_path_accepts_steam_common_parent():
    """A steamapps\\common parent is accepted (we look one level down for Bodycam) — the same rule
    HubApp._on_browse uses."""
    i18n.set_language("en")
    common = tempfile.mkdtemp(prefix="hub-common-")
    game_dir = os.path.join(common, "Bodycam")          # the real GameDir is one level down
    _make_game_dir(game_dir)                             # <common>/Bodycam/Bodycam/Content/Paks
    panel, s = _panel()
    Api(panel).settings_set_game_path(common)           # user picks the steamapps\common parent
    assert panel.app.state["game_dir"] == os.path.normpath(game_dir)


def test_settings_sign_out_delegates_to_session():
    """settings_sign_out reaches the session verb (which itself guards the locked-match case)."""
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    rec = _CallRecorder()
    panel.session = rec
    Api(panel).settings_sign_out()
    assert rec.calls and rec.calls[-1][0] == "sign_out"


def test_settings_browse_is_noop_without_a_window():
    """Headless (no pywebview window) the folder dialog verb is a safe no-op; the JS falls back to
    the manual path field, which the slice signals via can_browse=False."""
    i18n.set_language("en")
    panel, s = _panel()
    assert _slice(panel)["game"]["can_browse"] is False
    Api(panel).settings_browse_game_path()             # must not raise


def test_settings_browse_applies_the_dialog_pick():
    """A fake pywebview window returns a folder from create_file_dialog; the verb validates and
    persists it (proving the browse path, not only the manual fallback)."""
    import types
    i18n.set_language("en")
    root = tempfile.mkdtemp(prefix="hub-dialog-")
    _make_game_dir(root)

    class _Win:
        def create_file_dialog(self, *a, **kw):
            return (root,)

    # a stub `webview` module so `import webview` inside the verb resolves with FOLDER_DIALOG
    stub = types.ModuleType("webview")
    stub.FOLDER_DIALOG = 20
    sys.modules["webview"] = stub
    try:
        panel, s = _panel()
        panel.window = _Win()
        assert _slice(panel)["game"]["can_browse"] is True
        settings_mod._browse_game_path(panel)
        # the worker thread runs the dialog then posts _apply_game_path; give it a moment + pump
        import time
        for _ in range(50):
            if panel.scheduler.pending:
                break
            time.sleep(0.01)
        panel.scheduler.pump(limit=5)
        assert panel.app.state["game_dir"] == os.path.normpath(root)
    finally:
        sys.modules.pop("webview", None)


# ---------------------------------------------------------------- uninstall
def _mods_dir(root):
    """<root>/Bodycam/Content/Paks/~mods, created."""
    from hub import game as game_mod
    return game_mod.mods_dir(root, create=True)


def _write(path, blob=b"\x00" * 8):
    with open(path, "wb") as f:
        f.write(blob)
    return path


class _FakeUninstaller:
    """Stands in for the Inno uninstaller: records the argv instead of running anything."""

    def __init__(self, root):
        self.path = _write(os.path.join(root, "unins000.exe"), b"MZ")
        self.argv = None

    def launch(self, path, log=None):
        self.argv = uninstall_mod.uninstall_command(path, log)


def _patched_uninstall(tmp, game_running=False):
    """(fake, restore) — an installed-looking Lights Out whose uninstaller never really runs."""
    from hub import game as game_mod
    fake = _FakeUninstaller(tmp)
    saved = (uninstall_mod.find_uninstaller, uninstall_mod.launch,
             uninstall_mod._WIN, game_mod.game_running)
    uninstall_mod.find_uninstaller = lambda: fake.path
    uninstall_mod.launch = fake.launch
    uninstall_mod._WIN = True
    game_mod.game_running = lambda *a, **kw: game_running

    def restore():
        (uninstall_mod.find_uninstaller, uninstall_mod.launch,
         uninstall_mod._WIN, game_mod.game_running) = saved
    return fake, restore


def _wait_for_uninstall(panel, timeout=5.0):
    """The verb does its work on a worker thread; wait for it to report back."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not getattr(panel, "settings_uninstall_busy", False) and panel.last_state is not None:
            return True
        time.sleep(0.01)
    return False


def test_uninstall_slice_shape():
    """The slice carries everything the card and modal need, and is JSON-serialisable."""
    import json
    i18n.set_language("en")
    panel, s = _panel()
    u = _slice(panel)["uninstall"]
    json.dumps(u)
    for key in ("available", "reason", "program_dir", "state_dir", "game_files",
                "confirming", "busy", "locked", "error"):
        assert key in u, "missing settings.uninstall.%s" % key
    # running from source there is no Inno install to hand over to, and the slice says so rather
    # than offering a button that would do nothing
    assert u["available"] is False and u["reason"] == "not_installed"
    assert u["confirming"] is False and u["busy"] is False


def test_uninstall_strings_exist_for_every_reason_the_slice_can_report():
    """Every reason/error code the backend can produce has a sentence in every language, so the
    modal can never render a raw key at somebody who is trying to leave."""
    codes = ["not_installed", "not_windows", "game_running", "launch_failed"]
    for code in i18n.CODES:
        i18n.set_language(code)
        panel, _ = _panel()
        strings = _slice(panel)["strings"]
        for c in codes:
            assert strings.get("uninst_err_" + c), "%s has no uninst_err_%s" % (code, c)
        assert strings.get("uninst_locked"), "%s has no uninst_locked" % code
    i18n.set_language("en")


def test_uninstall_verbs_are_registered():
    from hub.webui.screens import SCREEN_VERBS
    for verb in ("settings_open_uninstall", "settings_close_uninstall", "settings_uninstall"):
        assert verb in SCREEN_VERBS, "verb not registered: %s" % verb


def test_uninstall_modal_opens_and_closes():
    """Open/close are view-only and re-emit; closing also clears a previous error."""
    i18n.set_language("en")
    panel, s = _panel()
    api = Api(panel)
    api.settings_open_uninstall()
    assert _slice(panel)["uninstall"]["confirming"] is True
    panel.settings_uninstall_error = "launch_failed"
    api.settings_close_uninstall()
    u = _slice(panel)["uninstall"]
    assert u["confirming"] is False and u["error"] == ""


def test_uninstall_refuses_while_a_match_is_locked_in():
    """Same rule as sign-out (C14): a committed match outranks leaving, and nothing is deleted."""
    i18n.set_language("en")
    tmp = tempfile.mkdtemp(prefix="hub-uninst-locked-")
    game = _make_game_dir(tempfile.mkdtemp(prefix="hub-uninst-game-"))
    ours = _write(os.path.join(_mods_dir(game), "CommunityGamemodes_P.pak"))
    fake, restore = _patched_uninstall(tmp)
    try:
        panel, s = _panel()
        panel.app.game_dir = game
        s.locked_in = lambda: True
        Api(panel).settings_uninstall(False)
        assert _slice(panel)["uninstall"]["error"] == "locked"
        assert fake.argv is None, "the uninstaller must not have been started"
        assert os.path.isfile(ours), "nothing may be deleted on the refused path"
    finally:
        restore()


def test_uninstall_refuses_while_the_game_is_running():
    """A mounted pak is locked, so the whole thing stops before anything is removed and the modal
    says which one of the two things to do about it."""
    i18n.set_language("en")
    tmp = tempfile.mkdtemp(prefix="hub-uninst-running-")
    game = _make_game_dir(tempfile.mkdtemp(prefix="hub-uninst-game2-"))
    ours = _write(os.path.join(_mods_dir(game), "CommunityGamemodes_P.pak"))
    fake, restore = _patched_uninstall(tmp, game_running=True)
    try:
        panel, s = _panel()
        panel.app.game_dir = game
        Api(panel).settings_uninstall(False)
        assert _wait_for_uninstall(panel), "the verb never finished"
        assert _slice(panel)["uninstall"]["error"] == "game_running"
        assert fake.argv is None
        assert os.path.isfile(ours), "our pak must survive a refusal"
    finally:
        restore()


def test_uninstall_removes_only_our_paks_and_then_hands_over():
    """The happy path: our two paks (and a half-written .tmp) go, a stranger's mod stays, the
    ~mods folder stays because it is not empty, and the Windows uninstaller is started."""
    i18n.set_language("en")
    tmp = tempfile.mkdtemp(prefix="hub-uninst-ok-")
    game = _make_game_dir(tempfile.mkdtemp(prefix="hub-uninst-game3-"))
    mods = _mods_dir(game)
    ours = _write(os.path.join(mods, "CommunityGamemodes_P.pak"))
    lobby = _write(os.path.join(mods, "CommunityLobby_P.pak"))
    half = _write(os.path.join(mods, "CommunityLobby_P.pak.tmp"))
    theirs = _write(os.path.join(mods, "SomeoneElsesMod_P.pak"))
    fake, restore = _patched_uninstall(tmp)
    try:
        panel, s = _panel()
        panel.app.game_dir = game
        quit_calls = []

        def _quit():
            quit_calls.append(True)
            return False                      # False: a test must not be taken down with the hub
        panel.quit = _quit

        # the slice tells the modal exactly what it is about to remove — and not the other mod
        names = _slice(panel)["uninstall"]["game_files"]
        assert "CommunityGamemodes_P.pak" in names and "CommunityLobby_P.pak" in names
        assert "SomeoneElsesMod_P.pak" not in names

        Api(panel).settings_uninstall(False)
        assert _wait_for_uninstall(panel), "the verb never finished"

        assert not os.path.exists(ours) and not os.path.exists(lobby)
        assert not os.path.exists(half), "a half-written pak of ours is ours to clean up"
        assert os.path.isfile(theirs), "another mod must never be touched"
        assert os.path.isdir(mods), "~mods still holds someone else's mod"
        assert fake.argv and fake.argv[0] == fake.path, "the uninstaller was not started"
        assert "/SILENT" in fake.argv and "/SUPPRESSMSGBOXES" in fake.argv, fake.argv
        assert quit_calls, "the hub must end itself once the uninstaller is running"
        assert _slice(panel)["uninstall"]["error"] == ""
    finally:
        restore()


def test_uninstall_leaves_an_empty_mods_folder_behind_as_stock():
    """With nothing else in it, ~mods goes too — the stock install has no such folder."""
    i18n.set_language("en")
    tmp = tempfile.mkdtemp(prefix="hub-uninst-stock-")
    game = _make_game_dir(tempfile.mkdtemp(prefix="hub-uninst-game4-"))
    mods = _mods_dir(game)
    _write(os.path.join(mods, "CommunityGamemodes_P.pak"))
    fake, restore = _patched_uninstall(tmp)
    try:
        panel, s = _panel()
        panel.app.game_dir = game
        panel.quit = lambda: False
        Api(panel).settings_uninstall(False)
        assert _wait_for_uninstall(panel)
        assert not os.path.isdir(mods), "an empty ~mods should not be left behind"
    finally:
        restore()


def test_uninstall_keeps_your_data_unless_you_ask():
    """state.json, the packs and the sign-in survive by default (installer.iss decided that) and
    only the opt-in tick removes them."""
    from hub import paths as paths_mod
    i18n.set_language("en")
    tmp = tempfile.mkdtemp(prefix="hub-uninst-data-")
    game = _make_game_dir(tempfile.mkdtemp(prefix="hub-uninst-game5-"))
    _write(os.path.join(_mods_dir(game), "CommunityGamemodes_P.pak"))
    state_dir = str(paths_mod.state_dir())
    marker = _write(os.path.join(state_dir, "uninstall-test-marker"))
    fake, restore = _patched_uninstall(tmp)
    try:
        panel, s = _panel()
        panel.app.game_dir = game
        panel.quit = lambda: False
        Api(panel).settings_uninstall(False)
        assert _wait_for_uninstall(panel)
        assert os.path.isfile(marker), "the default uninstall must keep the player's data"

        # ...and the tick really does wipe it
        result = uninstall_mod.perform(game, wipe_data=True)
        assert result["ok"] and result["wiped"] is True
        assert not os.path.exists(marker)
    finally:
        restore()
        paths_mod.state_dir()                  # recreate it for whatever runs next


def test_uninstall_command_is_silent_but_visible():
    """/SILENT (a progress window) rather than /VERYSILENT (nothing at all): our modal was the
    confirmation, but the player still needs a sign that something is happening after the hub
    vanishes. The log goes to TEMP, because <state> may have just been deleted."""
    argv = uninstall_mod.uninstall_command(os.path.join("C:\\", "x", "unins000.exe"))
    assert "/VERYSILENT" not in argv, "the player would see nothing at all"
    assert "/SILENT" in argv and "/SUPPRESSMSGBOXES" in argv and "/NORESTART" in argv, argv
    log = [a for a in argv if a.startswith("/LOG=")]
    assert log and tempfile.gettempdir().lower() in log[0].lower(), log


def test_uninstaller_app_id_matches_the_installer_script():
    """The registry fallback is keyed on installer.iss's AppId. If the two ever drift, the only
    route to an install whose folder we cannot glob is silently gone."""
    from hub import paths as paths_mod
    root = paths_mod.repo_root()
    if root is None:
        return                                  # not a checkout: nothing to compare against
    iss = root / "hub" / "installer.iss"
    if not iss.is_file():
        return
    text = iss.read_text(encoding="utf-8", errors="replace")
    app_id = uninstall_mod.INNO_APP_ID[:-len("_is1")]
    assert "AppId={" + app_id in text, \
        "hub/uninstall.py INNO_APP_ID no longer matches installer.iss AppId"


# ---------------------------------------------------------------- runner
_TESTS = [
    test_settings_snapshot_shape,
    test_settings_strings_cover_every_language,
    test_settings_readiness_reflects_state,
    test_readiness_does_not_require_bodycam_closed,
    test_settings_sound_slice_follows_persisted_volume,
    test_settings_verbs_are_registered,
    test_settings_set_volume_persists_and_reemits,
    test_settings_set_game_path_validates_and_saves,
    test_settings_set_game_path_accepts_steam_common_parent,
    test_settings_sign_out_delegates_to_session,
    test_settings_browse_is_noop_without_a_window,
    test_settings_browse_applies_the_dialog_pick,
    test_uninstall_slice_shape,
    test_uninstall_strings_exist_for_every_reason_the_slice_can_report,
    test_uninstall_verbs_are_registered,
    test_uninstall_modal_opens_and_closes,
    test_uninstall_refuses_while_a_match_is_locked_in,
    test_uninstall_refuses_while_the_game_is_running,
    test_uninstall_removes_only_our_paks_and_then_hands_over,
    test_uninstall_leaves_an_empty_mods_folder_behind_as_stock,
    test_uninstall_keeps_your_data_unless_you_ask,
    test_uninstall_command_is_silent_but_visible,
    test_uninstaller_app_id_matches_the_installer_script,
]


def main():
    results = []
    for fn in _TESTS:
        try:
            fn()
            results.append((fn.__name__, True))
            print("ok   %s" % fn.__name__)
        except Exception as exc:                       # noqa: BLE001
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
