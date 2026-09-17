#!/usr/bin/env python3.12
"""Headless tests for the Gamemodes web screen (hub/webui/screens/gamemodes.py).

Plain asserts, no pytest, no network, no pywebview, no Tk. The long-running ops.apply is stubbed
(as tests/test_hub.py stubs the pak builder), and the install worker is run synchronously by
replacing gamemodes.run_async — so the whole install/uninstall/update flow is deterministic.

Covers: the snapshot slice (installed / available / ranked split, versions, update-available,
ruleset lines, maps, the ready/running guards, per-mode can_* gating, orphan handling, the screen
strings and JSON-serialisability), the language switch, and the bridge verbs (install / uninstall /
update route to ops.apply with the right desired set and surface progress → done → error; the
ruleset open/close view toggle).  Run:  python3.12 tests/test_screen_gamemodes.py
"""
import pytest
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_STATE = tempfile.mkdtemp(prefix="hub-gm-state-")
os.environ["HUB_STATE_DIR"] = _STATE
sys.path.insert(0, str(REPO))

from hub import game as game_mod                 # noqa: E402
from hub import i18n                             # noqa: E402
from hub import ops                              # noqa: E402
from hub import state as state_mod               # noqa: E402
from hub.competitive import COMPETITIVE_MODE_ID  # noqa: E402
from hub.webui import screens as screens_pkg     # noqa: E402
from hub import catalogue as cat_mod             # noqa: E402
from hub.webui.screens import gamemodes          # noqa: E402
from hub.webui.panel import WebPanel             # noqa: E402
from hub.webui.scheduler import InlineScheduler  # noqa: E402

RESULTS = []


# ---------------------------------------------------------------- fixtures
def sample_catalogue():
    return {
        "catalogue_version": 1,
        "hub": {"version": "1.3.0", "download_url": "https://x/h.exe"},
        "gamemodes": [
            {"id": "CTF", "title": "Capture the Flag 10v10", "version": "1.0.3",
             "description": "Return the enemy flag to base to win.",
             "pack_url": "https://x/packs/CTF-1.0.3.zip", "sha256": "a" * 64,
             "rulesets": {"en": "Capture the Flag 10v10\n\n• Two teams of 10.\n• Score 3 to win.",
                          "de": "Capture the Flag 10v10\n\n• Zwei Teams zu je 10."}},
            {"id": COMPETITIVE_MODE_ID, "title": "Bodybomb 5v5", "version": "1.0.6",
             "description": "Competitive Bodybomb.",
             "pack_url": "https://x/packs/BB5-1.0.6.zip", "sha256": "b" * 64,
             "maps": ["Airsoft", "Pool", "Rome"],
             "rulesets": {"en": "Bodybomb 5v5\n\n• One life per round.\n• First to 7 wins."}},
        ],
    }


class FakeApp:
    """The minimal `app` the WebPanel/gamemodes slice reads: state, catalogue, game_dir."""

    def __init__(self, installed=None, catalogue=None, game_dir="/fake/game"):
        self.state = {"installed": dict(installed or {}), "auth": None, "language": "en"}
        self.catalogue = catalogue
        self.game_dir = game_dir


def make_panel(installed=None, catalogue=None, game_dir="/fake/game"):
    app = FakeApp(installed=installed, catalogue=catalogue, game_dir=game_dir)
    panel = WebPanel(app, scheduler=InlineScheduler(), window=None)
    return panel


@pytest.fixture(autouse=True)
def _restore_game_guards():
    """Put game_mod's probes back after every test in this file.

    force_guards() below replaces three module-level functions on `game_mod` and used to leave
    them replaced FOR THE REST OF THE PROCESS. Every test here calls it, so by the end of this
    file `is_game_dir` was a lambda answering True for any non-empty path - and pytest runs the
    whole suite in one process, so the next file inherited it.

    That is not hypothetical: it broke three tests elsewhere, and they were mystifying because
    each PASSED on its own. test_screen_settings.py's game-path tests look one level down for a
    Bodycam folder, and with a stub that says yes to everything they accepted the parent instead
    - a failure in a file that had done nothing wrong, caused by a helper in this one.

    Autouse, so a test that forgets to restore cannot reintroduce it.
    """
    saved = (game_mod.is_game_dir, game_mod.game_running, game_mod.game_running_cached)
    try:
        yield
    finally:
        (game_mod.is_game_dir, game_mod.game_running, game_mod.game_running_cached) = saved


def force_guards(ready=True, running=False):
    """Pin the two filesystem/OS probes the slice uses so tests are deterministic.

    game_running_cached is pinned as well as game_running. The slice reads the CACHED probe now -
    the exact one spawns `tasklist`, which cost ~1 s on a busy machine and was being paid three
    times per snapshot on the UI thread (hub/game.py, game_running_cached). The cached one answers
    from a background refresh, so setting only the exact probe would leave the slice reading a
    stale value and the gate under test would never flip."""
    game_mod.is_game_dir = lambda p: bool(p) and ready          # noqa: E731
    game_mod.game_running = lambda: running                     # noqa: E731
    game_mod.game_running_cached = lambda *a, **k: running      # noqa: E731


def slice_of(panel):
    return gamemodes.snapshot(None, panel)["gamemodes"]


def strip_of(panel):
    """The gamemode-update strip's slice - a SIBLING of `gamemodes`, because core.js draws it in
    the chrome over whatever screen is open rather than inside the tab."""
    return gamemodes.snapshot(None, panel)["gamemode_update"]


# ---------------------------------------------------------------- the update strip
#
# Sam, 2026-09-17: "similar to how the lightsoff app auto detects an update without having to
# restart or go to a different page, do the same with gamemodes". Before this the only place a
# gamemode update appeared was a button on one row of one tab, so the only way to find out was to
# go and look - which is how an evening of testing ran on a pak whose rules had been replaced
# hours earlier.
def test_no_strip_when_everything_is_up_to_date():
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=sample_catalogue())
    assert strip_of(panel) is None


def test_the_strip_names_the_mode_and_both_versions():
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.0"}}, catalogue=sample_catalogue())
    strip = strip_of(panel)
    assert strip, "an installed mode behind the catalogue must reach the strip"
    assert strip["id"] == "CTF"
    assert strip["current"] == "1.0.0"
    assert strip["latest"] == "1.0.3"
    assert strip["rules_only"] is False
    assert strip["can_update"] is True
    json.dumps(strip)


def test_the_strip_says_why_when_it_cannot_be_pressed():
    """A dead button is a bug report; "close Bodycam" is an instruction."""
    i18n.set_language("en")
    force_guards(ready=True, running=True)             # the game is open, so the pak is locked
    panel = make_panel(installed={"CTF": {"version": "1.0.0"}}, catalogue=sample_catalogue())
    strip = strip_of(panel)
    assert strip["can_update"] is False
    assert strip["running"] is True

    force_guards(ready=False, running=False)           # no Bodycam folder known
    panel = make_panel(installed={"CTF": {"version": "1.0.0"}}, catalogue=sample_catalogue())
    strip = strip_of(panel)
    assert strip["can_update"] is False
    assert strip["ready"] is False


def test_a_rules_only_change_is_marked_as_one():
    """The version does not move when only the rules do - which is exactly the case that used to be
    invisible. `rules_only` is what stops the strip reading "update 1.0.3 -> 1.0.3"."""
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    catalogue = sample_catalogue()
    for entry in catalogue["gamemodes"]:
        if entry["id"] == "CTF":
            entry["rules_override"] = {"score_limit": 2}
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=catalogue)
    strip = strip_of(panel)
    assert strip, "a rules change is an update even though the version is level"
    assert strip["rules_only"] is True
    assert strip["latest"] == strip["current"] == "1.0.3"


def test_the_ranked_mode_takes_the_strip_when_two_are_stale():
    """One strip, not a stack - and ranked first, because it is the one that shuts the queue."""
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.0"},
                                  COMPETITIVE_MODE_ID: {"version": "1.0.0"}},
                       catalogue=sample_catalogue())
    strip = strip_of(panel)
    assert strip["id"] == COMPETITIVE_MODE_ID, strip
    assert strip["more"] == 1, "and it says how many others are waiting"


# ---------------------------------------------------------------- snapshot tests
def test_slice_splits_installed_available_and_flags_ranked():
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=sample_catalogue())
    gm = slice_of(panel)
    json.dumps(gm)                                              # must be serialisable

    inst_ids = [m["id"] for m in gm["installed"]]
    avail_ids = [m["id"] for m in gm["available"]]
    assert inst_ids == ["CTF"], inst_ids
    assert avail_ids == [COMPETITIVE_MODE_ID], avail_ids

    bb5 = gm["available"][0]
    ctf = gm["installed"][0]
    assert bb5["ranked"] is True and ctf["ranked"] is False
    assert ctf["installed"] is True and bb5["installed"] is False
    assert ctf["version"] == "1.0.3" and ctf["installed_version"] == "1.0.3"
    # maps flow through for BB5; CTF has none
    assert bb5["maps"] == ["Airsoft", "Pool", "Rome"] and ctf["maps"] == []
    # ruleset text is split into display lines with the title header dropped and bullets stripped
    assert ctf["ruleset_lines"] == ["Two teams of 10.", "Score 3 to win."], ctf["ruleset_lines"]
    # the screen strings ride in the slice
    assert gm["strings"]["btn_install"] == "Install"
    assert gm["ready"] is True and gm["running"] is False and gm["catalogue_loaded"] is True


def test_slice_gates_actions_on_ready_running_and_state():
    i18n.set_language("en")
    cat = sample_catalogue()
    # ready + idle: install the not-installed one, uninstall the installed one
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=cat)
    gm = slice_of(panel)
    ctf = next(m for m in gm["installed"] if m["id"] == "CTF")
    bb5 = next(m for m in gm["available"] if m["id"] == COMPETITIVE_MODE_ID)
    assert bb5["can_install"] is True and ctf["can_uninstall"] is True
    assert ctf["can_install"] is False and bb5["can_uninstall"] is False

    # game running: everything is blocked (ops.apply would raise "close the game")
    force_guards(ready=True, running=True)
    gm = slice_of(panel)
    assert all(not m["can_install"] and not m["can_uninstall"] and not m["can_update"]
               for m in gm["installed"] + gm["available"])

    # no game dir: also blocked, and the slice says so
    force_guards(ready=False, running=False)
    gm = slice_of(panel)
    assert gm["ready"] is False
    assert all(not m["can_install"] and not m["can_uninstall"] for m in gm["installed"] + gm["available"])


def test_slice_reports_an_available_update():
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    # installed at an older version than the catalogue lists -> update available + actionable
    panel = make_panel(installed={"CTF": {"version": "1.0.0"}}, catalogue=sample_catalogue())
    ctf = next(m for m in slice_of(panel)["installed"] if m["id"] == "CTF")
    assert ctf["update_available"] is True and ctf["can_update"] is True

    # at the catalogue version there is no update
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=sample_catalogue())
    ctf = next(m for m in slice_of(panel)["installed"] if m["id"] == "CTF")
    assert ctf["update_available"] is False and ctf["can_update"] is False


def test_slice_keeps_an_orphan_installed_mode():
    """An installed id the catalogue no longer lists still shows (installed, uninstallable), and it
    is never offered an update (mirrors HubApp._entries / the row logic)."""
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"OLD": {"version": "0.9", "title": "Old Mode"}},
                       catalogue=sample_catalogue())
    gm = slice_of(panel)
    old = next(m for m in gm["installed"] if m["id"] == "OLD")
    assert old["orphan"] is True and old["installed"] is True
    assert old["can_uninstall"] is True and old["can_update"] is False and old["update_available"] is False


def test_slice_localizes_strings_and_ruleset():
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=sample_catalogue())
    try:
        i18n.set_language("de")
        gm = slice_of(panel)
        assert gm["strings"]["btn_install"] == "Installieren"
        ctf = next(m for m in gm["installed"] if m["id"] == "CTF")
        # the German ruleset is used, title header dropped
        assert ctf["ruleset_lines"] == ["Zwei Teams zu je 10."], ctf["ruleset_lines"]
    finally:
        i18n.set_language("en")


# ---------------------------------------------------------------- verb tests
def test_verbs_are_registered():
    for verb in ("gamemode_install", "gamemode_uninstall", "gamemode_update",
                 "open_ruleset", "close_ruleset"):
        assert verb in screens_pkg.SCREEN_VERBS, verb


def _run_install_flow(action, mode_id, installed, expect_desired, removed=False):
    """Drive one verb with ops.apply stubbed and the worker run synchronously; capture the call."""
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed=installed, catalogue=sample_catalogue())
    seen = {}

    def fake_apply(desired, catalogue, st, game_dir, log=None, progress=None):
        seen["desired"] = set(desired)
        seen["game_dir"] = game_dir
        if progress:
            progress(0.5, "half")
        st["installed"] = {} if removed else {i: {"version": "9.9"} for i in desired}
        state_mod.save(st)
        return {"installed": list(desired), "pak": None, "removed": removed}

    orig_apply, orig_spawn = ops.apply, gamemodes.run_async
    ops.apply = fake_apply
    gamemodes.run_async = lambda fn: fn()                       # synchronous worker
    try:
        verb = {"install": gamemodes._install, "uninstall": gamemodes._uninstall,
                "update": gamemodes._update}[action]
        verb(panel, mode_id)
    finally:
        ops.apply, gamemodes.run_async = orig_apply, orig_spawn

    assert seen.get("desired") == expect_desired, (action, seen.get("desired"), expect_desired)
    return panel


def test_install_verb_applies_installed_plus_selected():
    panel = _run_install_flow("install", COMPETITIVE_MODE_ID,
                              installed={"CTF": {"version": "1.0.3"}},
                              expect_desired={"CTF", COMPETITIVE_MODE_ID})
    # the job finished and a success notice is set; app.state reflects the new install
    assert panel._gamemodes_job is None
    assert panel._gamemodes_notice["kind"] == "installed"
    assert COMPETITIVE_MODE_ID in (panel.app.state.get("installed") or {})


def test_uninstall_verb_applies_installed_minus_selected():
    panel = _run_install_flow("uninstall", "CTF",
                              installed={"CTF": {"version": "1.0.3"}},
                              expect_desired=set(), removed=True)
    assert panel._gamemodes_job is None
    assert panel._gamemodes_notice["kind"] == "uninstalled"
    assert [m["id"] for m in panel.last_state["gamemodes"]["installed"]] == []


def test_a_finished_install_is_visible_in_the_snapshot_it_pushes():
    """The push that says "Installed." has to show the row as installed too.

    app.state is what every row is drawn from, and ops.apply does not touch it - it saves its own
    copy. The worker used to re-read it in a `finally` AFTER it had already queued the redraw, so
    the snapshot carrying the success notice was still built from the pre-install state: the banner
    said Installed and the row underneath it still offered to install it. Nothing re-emitted
    afterwards either, so it stayed that way until the player left the tab and came back (Sam,
    2026-09-16). The reload now happens on the UI thread, before the push."""
    panel = _run_install_flow("install", COMPETITIVE_MODE_ID,
                              installed={"CTF": {"version": "1.0.3"}},
                              expect_desired={"CTF", COMPETITIVE_MODE_ID})
    gm = panel.last_state["gamemodes"]
    assert gm["notice"]["kind"] == "installed"
    assert COMPETITIVE_MODE_ID in [m["id"] for m in gm["installed"]]
    assert COMPETITIVE_MODE_ID not in [m["id"] for m in gm["available"]]
    assert panel.last_state["status"]["gamemode_installed"] is True


def test_update_verb_keeps_the_whole_installed_set():
    panel = _run_install_flow("update", "CTF",
                              installed={"CTF": {"version": "1.0.0"},
                                         COMPETITIVE_MODE_ID: {"version": "1.0.6"}},
                              expect_desired={"CTF", COMPETITIVE_MODE_ID})
    assert panel._gamemodes_job is None
    assert panel._gamemodes_notice["kind"] == "updated"


def test_verb_surfaces_an_error_from_ops():
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={}, catalogue=sample_catalogue())

    def boom(*a, **k):
        raise RuntimeError("Close Bodycam first.")

    orig_apply, orig_spawn = ops.apply, gamemodes.run_async
    ops.apply = boom
    gamemodes.run_async = lambda fn: fn()
    try:
        gamemodes._install(panel, "CTF")
    finally:
        ops.apply, gamemodes.run_async = orig_apply, orig_spawn

    assert panel._gamemodes_job is None
    assert panel._gamemodes_notice["kind"] == "error"
    assert "Close Bodycam" in panel._gamemodes_notice["text"]


def _override_panel(installed, override, running=False):
    i18n.set_language("en")
    force_guards(ready=True, running=running)
    cta = sample_catalogue()
    if override is not None:
        cat_mod.entry_by_id(cta, COMPETITIVE_MODE_ID)["rules_override"] = override
    return make_panel(installed=installed, catalogue=cta)


def _drive_rebuild(panel):
    """Run rebuild_for_rules_override with ops.apply stubbed and the worker synchronous."""
    seen = {}

    def fake_apply(desired, catalogue, st, game_dir, log=None, progress=None):
        seen["desired"] = set(desired)
        st["installed"] = {i: {"version": "1.0.6", "rules_override": {"score_limit": 2}}
                           for i in desired}
        state_mod.save(st)
        return {"installed": list(desired), "pak": None, "removed": False}

    orig_apply, orig_spawn = ops.apply, gamemodes.run_async
    ops.apply = fake_apply
    gamemodes.run_async = lambda fn: fn()
    try:
        started = gamemodes.rebuild_for_rules_override(panel)
    finally:
        ops.apply, gamemodes.run_async = orig_apply, orig_spawn
    return started, seen


def test_a_changed_rules_override_rebuilds_the_pak_by_itself():
    """Hit deploy with a new COMP_GAME_RULES_OVERRIDE and the next match plays the new rounds: the
    catalogue says 2, the pak was built with nothing, so the hub rebuilds without being asked."""
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6"},
                             "CTF": {"version": "1.0.3"}},
                            {"score_limit": 2, "max_rounds": 3})
    started, seen = _drive_rebuild(panel)
    assert started is True
    # everything installed is rebuilt, not only the mode whose rules moved: one pak holds them all
    assert seen["desired"] == {COMPETITIVE_MODE_ID, "CTF"}, seen


def test_an_unchanged_override_rebuilds_nothing():
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6",
                                                   "rules_override": {"score_limit": 2}}},
                            {"score_limit": 2})
    started, seen = _drive_rebuild(panel)
    assert started is False and not seen, seen


def test_no_override_at_all_rebuilds_nothing():
    """Production: the catalogue carries none and the pak was built with none."""
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6"}}, None)
    started, seen = _drive_rebuild(panel)
    assert started is False and not seen, seen


def test_an_override_dropped_by_the_server_rebuilds_back_to_the_shipped_rules():
    """Turning the variable OFF has to travel too, or the tester is stuck on short rounds."""
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6",
                                                   "rules_override": {"score_limit": 2}}}, None)
    started, _ = _drive_rebuild(panel)
    assert started is True


def test_the_game_running_blocks_the_automatic_rebuild():
    """The pak is locked while Bodycam runs; ops.apply would refuse and show an error notice for
    something nobody asked for."""
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6"}},
                            {"score_limit": 2}, running=True)
    started, seen = _drive_rebuild(panel)
    assert started is False and not seen, seen


class FakeSession:
    def __init__(self, phase):
        self.phase = phase

    def locked_in(self):
        from hub.competitive import LOCKED_PHASES
        return self.phase in LOCKED_PHASES


@pytest.mark.parametrize("phase", ["queued", "checking", "found", "lobby", "connecting", "live"])
def test_no_automatic_rebuild_while_a_match_is_in_the_air(phase):
    """The build takes 30-60 s and a match can pop in the middle of it. One more match on the old
    rules beats the pak being swapped under somebody about to be sent into a game."""
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6"}}, {"score_limit": 2})
    panel.session = FakeSession(phase)
    started, seen = _drive_rebuild(panel)
    assert started is False and not seen, (phase, seen)


@pytest.mark.parametrize("phase", ["idle", "result", ""])
def test_the_rebuild_still_runs_between_matches(phase):
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6"}}, {"score_limit": 2})
    panel.session = FakeSession(phase)
    started, _ = _drive_rebuild(panel)
    assert started is True, phase


def test_a_changed_override_shows_the_row_update_button():
    panel = _override_panel({COMPETITIVE_MODE_ID: {"version": "1.0.6"}}, {"score_limit": 2})
    row = next(m for m in slice_of(panel)["installed"] if m["id"] == COMPETITIVE_MODE_ID)
    assert row["update_available"] is True, row


def test_ruleset_open_close_toggle():
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={"CTF": {"version": "1.0.3"}}, catalogue=sample_catalogue())
    assert slice_of(panel)["open_ruleset"] is None
    gamemodes._open_ruleset(panel, "CTF")
    assert slice_of(panel)["open_ruleset"] == "CTF"
    gamemodes._close_ruleset(panel)
    assert slice_of(panel)["open_ruleset"] is None


def test_busy_job_rejects_a_second_install():
    """One job at a time: while a job is busy a new verb is a no-op (as HubApp guards on self.busy)."""
    i18n.set_language("en")
    force_guards(ready=True, running=False)
    panel = make_panel(installed={}, catalogue=sample_catalogue())
    panel._gamemodes_job = {"busy": True, "action": "install", "target": "CTF",
                            "progress": 0.5, "message": "working", "error": ""}
    called = {"n": 0}

    def fake_apply(*a, **k):
        called["n"] += 1
        return {"installed": [], "removed": False}

    orig_apply, orig_spawn = ops.apply, gamemodes.run_async
    ops.apply = fake_apply
    gamemodes.run_async = lambda fn: fn()
    try:
        gamemodes._install(panel, COMPETITIVE_MODE_ID)          # should be rejected
    finally:
        ops.apply, gamemodes.run_async = orig_apply, orig_spawn
    assert called["n"] == 0 and panel._gamemodes_job["target"] == "CTF"


# ---------------------------------------------------------------- runner
def test(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, True))
        print(f"ok    {fn.__name__}")
    except Exception:
        RESULTS.append((fn.__name__, False))
        print(f"FAIL  {fn.__name__}")
        traceback.print_exc()


def main():
    for fn in [
        test_slice_splits_installed_available_and_flags_ranked,
        test_slice_gates_actions_on_ready_running_and_state,
        test_slice_reports_an_available_update,
        test_slice_keeps_an_orphan_installed_mode,
        test_slice_localizes_strings_and_ruleset,
        test_verbs_are_registered,
        test_install_verb_applies_installed_plus_selected,
        test_uninstall_verb_applies_installed_minus_selected,
        test_a_finished_install_is_visible_in_the_snapshot_it_pushes,
        test_update_verb_keeps_the_whole_installed_set,
        test_verb_surfaces_an_error_from_ops,
        test_ruleset_open_close_toggle,
        test_busy_job_rejects_a_second_install,
    ]:
        test(fn)

    import shutil
    shutil.rmtree(_STATE, ignore_errors=True)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
