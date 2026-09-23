#!/usr/bin/env python3.12
"""Headless tests for the hub package.  Run:  python3.12 tests/test_hub.py

Plain asserts, no pytest, no network: the catalogue and the pack download are served from
file:// URLs, and the real pak builder is never run (it needs the player's game paks and
takes a minute) — ops.build_from_packs is monkeypatched with a fake that writes a small
file, which is enough to prove the atomic replace and the state update.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
import urllib.request
from urllib.request import pathname2url

REPO = Path(__file__).resolve().parent.parent          # /tmp/bcg
PACKS = REPO / "packs"
SAMPLE_ZIP = PACKS / "CTF-1.0.0.zip"
SAMPLE_ENTRY = PACKS / "CTF-1.0.0.json"

# Point the hub's state at a scratch dir BEFORE importing it (version.py reads the
# environment at import time).
_STATE = tempfile.mkdtemp(prefix="hub-state-")
os.environ["HUB_STATE_DIR"] = _STATE
sys.path.insert(0, str(REPO))

from hub import catalogue as cat               # noqa: E402
from hub import game as game_mod               # noqa: E402
from hub import ops                            # noqa: E402
from hub import paths                          # noqa: E402
from hub import state as state_mod             # noqa: E402
from hub import version                        # noqa: E402

TMPDIRS = [_STATE]
RESULTS = []


def tmpdir(prefix="hub-test-"):
    d = tempfile.mkdtemp(prefix=prefix)
    TMPDIRS.append(d)
    return d


def file_url(path) -> str:
    return "file:" + pathname2url(str(Path(path).resolve()))


def fake_game_dir(name="game") -> str:
    """A folder that passes is_game_dir(): Bodycam/Content/Paks with one dummy .pak."""
    g = os.path.join(tmpdir(), name)
    paks = os.path.join(g, "Bodycam", "Content", "Paks")
    os.makedirs(paks)
    with open(os.path.join(paks, "pakchunk0-Windows.pak"), "wb") as f:
        f.write(b"not a real pak")
    return g


def isolated_game_processes(fn):
    """Fake game folders must not observe a player's real running Bodycam process.

    The tasklist parser test supplies its own process list and exercises the real probe.
    Tests of closing a game replace game_pids themselves and still exercise that behavior.
    """
    from contextlib import nullcontext
    from unittest.mock import patch
    if fn.__name__ == "test_game_running_survives_tasklists_truncated_image_name":
        return nullcontext()
    return patch.object(game_mod, "game_pids", return_value=[])


def test(fn):
    """Run one test function, record pass/fail, keep going."""
    try:
        with isolated_game_processes(fn):
            fn()
    except Exception:
        RESULTS.append((fn.__name__, False))
        print(f"FAIL  {fn.__name__}")
        traceback.print_exc()
    else:
        RESULTS.append((fn.__name__, True))
        print(f"ok    {fn.__name__}")


# ------------------------------------------------------------------ plan()
def test_plan_truth_table():
    # (selected, installed) -> (install, uninstall, can_install, can_uninstall)
    #   install   = selected - installed   (Install adds them:      desired = installed | selected)
    #   uninstall = selected & installed   (Uninstall removes them: desired = installed - selected)
    table = [
        (set(),            set(),            [],      [],      False, False),
        ({"A"},            set(),            ["A"],   [],      True,  False),
        (set(),            {"A"},            [],      [],      False, False),   # installed but not selected: nothing to do
        ({"A"},            {"A"},            [],      ["A"],   False, True),    # click an installed one -> Uninstall
        ({"A", "B"},       {"A"},            ["B"],   ["A"],   True,  True),    # mixed selection: both buttons live
        ({"A"},            {"A", "B"},       [],      ["A"],   False, True),
        ({"B"},            {"A"},            ["B"],   [],      True,  False),
        ({"A", "B", "C"},  {"B"},            ["A", "C"], ["B"], True,  True),
    ]
    for selected, installed, ins, uni, can_i, can_u in table:
        p = ops.plan(selected, installed)
        assert p.install == ins, (selected, installed, p.install)
        assert p.uninstall == uni, (selected, installed, p.uninstall)
        assert p.can_install is can_i, (selected, installed, p.can_install)
        assert p.can_uninstall is can_u, (selected, installed, p.can_uninstall)
        # dict-style access works too (the UI uses attributes, tests may use either)
        assert p["install"] == ins and p["uninstall"] == uni


# ------------------------------------------------------------------ state
def test_state_roundtrip():
    p = paths.state_file()
    assert str(p).startswith(_STATE), p
    if os.path.exists(p):
        os.remove(p)

    st = state_mod.load()                       # missing file -> defaults
    assert st == {"game_dir": None, "installed": {}, "pak_sha256": None,
                  "catalogue_cache": None, "language": None, "tab": None,
                  "comp_sound_volume": None, "comp_sound_last": None, "comp_sound": None,
                  "matchmaking_region": "", "matchmaking_cross_region": False,
                  "ui_click_volume": 35, "ui_click_enabled": True,
                  "auth": None}, st

    st["game_dir"] = r"C:\Games\Bodycam"
    st["installed"]["CTF"] = {"version": "1.0.0", "title": "Capture the Flag 10v10", "titles": {"fr": "Capture du drapeau 10v10"}}
    st["language"] = "fr"
    st["pak_sha256"] = "deadbeef"
    st["catalogue_cache"] = {"catalogue_version": 1, "gamemodes": []}
    state_mod.save(st)
    assert os.path.isfile(p)

    back = state_mod.load()
    assert back == st, back

    # the signed-in account survives a round trip, and a half-written one is dropped
    st["auth"] = {"token": "tok-123", "steam_id": "76561198000999000",
                  "persona": "Sam", "avatar": "https://example/a.jpg"}
    state_mod.save(st)
    assert state_mod.load()["auth"] == st["auth"]
    st["auth"] = {"persona": "no token, no id"}
    state_mod.save(st)
    assert state_mod.load()["auth"] is None, "an account without a token must not be trusted"
    st["auth"] = None
    state_mod.save(st)

    with open(p, "w", encoding="utf-8") as f:   # corrupt file -> defaults, no exception
        f.write("{not json at all")
    assert state_mod.load()["installed"] == {}

    # subdirs are created on demand and live under HUB_STATE_DIR
    for d in (paths.packs_dir(), paths.work_dir(), paths.logs_dir()):
        assert os.path.isdir(d) and str(d).startswith(_STATE), d


def test_an_installed_rules_override_survives_a_restart():
    """The rules the pak was built with are part of "what is installed", so they have to come back
    out of state.json. When load() dropped them, cat.mode_update_available compared the catalogue's
    override against {} on every start and the row read "Update available" forever - pressing
    Update rebuilt the same pak and changed nothing."""
    p = paths.state_file()
    if os.path.exists(p):
        os.remove(p)
    over = {"score_limit": 2, "max_rounds": 3, "team_switch_interval": 1}
    st = state_mod.load()
    st["installed"]["BB5"] = {"version": "1.0.12", "title": "Bodybomb 5v5", "titles": {},
                              "rules_override": dict(over)}
    state_mod.save(st)

    back = state_mod.load()["installed"]["BB5"]
    assert back.get("rules_override") == over, back
    entry = {"id": "BB5", "version": "1.0.12", "rules_override": dict(over)}
    assert not cat.mode_update_available(entry, back), "the same build must not offer an update"
    entry["rules_override"] = {"score_limit": 7}
    assert cat.mode_update_available(entry, back), "a changed override is still an update"

    # junk in the file cannot reach the builder: the same cleaning as the catalogue side
    st["installed"]["BB5"]["rules_override"] = {"score_limit": "two", "nonsense": 1}
    state_mod.save(st)
    assert "rules_override" not in state_mod.load()["installed"]["BB5"]


def test_sound_setting_survives_a_restart():
    """load() copies keys across one at a time, so a key the app writes but load() does not
    know about is silently dropped on the next start. That is exactly what happened to the
    match-found cue: muting it, or moving the slider, lasted until the hub was closed.

    The three keys are tri-state on purpose - None is "never said", which is NOT 0."""
    from hub import competitive as C
    import types
    p = os.path.join(tmpdir("hub-sound-"), "state.json")

    st = state_mod.default_state()
    assert st["comp_sound_volume"] is None and st["comp_sound_last"] is None
    assert st["comp_sound"] is None, "the legacy on/off flag defaults to 'never had one' too"

    # a slider position, and the bell's memory of where it was, both come back
    st["comp_sound_volume"] = 35
    st["comp_sound_last"] = 35
    state_mod.save(st, p)
    back = state_mod.load(p)
    assert back["comp_sound_volume"] == 35 and back["comp_sound_last"] == 35, back

    # ...and so does MUTED, which is the one a truth test would quietly throw away
    st["comp_sound_volume"] = 0
    state_mod.save(st, p)
    assert state_mod.load(p)["comp_sound_volume"] == 0, "a muted player must stay muted"

    # junk in the file costs the setting, never the whole state
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"comp_sound_volume": "loud", "comp_sound_last": True,
                   "comp_sound": 1, "language": "fr"}, f)
    bad = state_mod.load(p)
    assert bad["comp_sound_volume"] is None and bad["comp_sound_last"] is None
    assert bad["comp_sound"] is None, "a 1 is not a bool, and True is not a volume"
    assert bad["language"] == "fr", "one bad key must not cost the rest of the file"

    # out of range is clamped rather than rejected
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"comp_sound_volume": 400, "comp_sound_last": -10}, f)
    clamped = state_mod.load(p)
    assert clamped["comp_sound_volume"] == 100 and clamped["comp_sound_last"] == 0, clamped

    # and the panel reads all of that the way the slider means it
    panel = C.CompetitivePanel.__new__(C.CompetitivePanel)
    panel.app = types.SimpleNamespace(state=state_mod.default_state())
    assert panel.sound_volume() == C.sounds_mod.DEFAULT_VOLUME, "never set is the default"
    assert panel.sound_enabled()
    panel.app.state["comp_sound"] = False            # a hub older than the slider
    assert panel.sound_volume() == 0 and not panel.sound_enabled()
    panel.app.state["comp_sound_volume"] = 0         # the slider itself, at zero
    assert panel.sound_volume() == 0
    panel.app.state["comp_sound_volume"] = 70        # and the slider wins over the old flag
    assert panel.sound_volume() == 70 and panel.sound_enabled()

    # The whole round trip the player actually makes: move the slider, mute with the bell,
    # unmute - and after each one, what a RESTART would read. The panel writes through
    # state_mod.save() to paths.state_file(), so point that at a scratch file for this.
    real_state_file, paths.state_file = paths.state_file, lambda: p
    try:
        panel.app.state = state_mod.default_state()
        panel.set_sound_volume(35)
        assert state_mod.load(p)["comp_sound_volume"] == 35, "the slider survives a restart"
        panel.on_change = lambda: None            # toggle_sound redraws; there is no window
        panel.toggle_sound()                          # the bell: mute
        assert panel.sound_volume() == 0
        restarted = state_mod.load(p)
        assert restarted["comp_sound_volume"] == 0, "a muted player stays muted"
        assert restarted["comp_sound_last"] == 35, "and the bell remembers where to go back to"
        panel.app.state = restarted                   # the restart itself
        assert panel.sound_volume() == 0, "the new hub opens muted, not at the default"
        panel.toggle_sound()                          # ...and the bell still knows the way back
        assert panel.sound_volume() == 35
        assert state_mod.load(p)["comp_sound_volume"] == 35
    finally:
        paths.state_file = real_state_file


def test_state_reconcile():
    game = fake_game_dir("reconcile")
    st = state_mod.default_state()
    st["installed"] = {"CTF": {"version": "1.0.0", "title": "CTF"}}
    st["pak_sha256"] = "abc"

    note = state_mod.reconcile(st, game)        # pak missing -> forget the install
    assert note and st["installed"] == {} and st["pak_sha256"] is None, (note, st)

    mods = game_mod.mods_dir(game)
    with open(os.path.join(mods, version.PAK_NAME), "wb") as f:
        f.write(b"pak")
    st["installed"] = {"CTF": {"version": "1.0.0", "title": "CTF"}}
    assert state_mod.reconcile(st, game) is None
    assert st["installed"] == {"CTF": {"version": "1.0.0", "title": "CTF"}}


# ------------------------------------------------------------------ game
def test_is_game_dir():
    game = fake_game_dir("isgame")
    assert game_mod.is_game_dir(game)
    assert not game_mod.is_game_dir(os.path.join(game, "Bodycam"))
    assert not game_mod.is_game_dir(tmpdir())
    assert not game_mod.is_game_dir(None)
    assert not game_mod.is_game_dir(os.path.join(game, "nope"))

    # a Paks folder with no *.pak in it does not count
    empty = os.path.join(tmpdir(), "empty")
    os.makedirs(os.path.join(empty, "Bodycam", "Content", "Paks"))
    assert not game_mod.is_game_dir(empty)

    assert game_mod.paks_dir(game).endswith(os.path.join("Bodycam", "Content", "Paks"))
    mods = game_mod.mods_dir(game)
    assert os.path.isdir(mods) and mods.endswith("~mods")
    assert game_mod.pak_path(game).endswith(version.PAK_NAME)
    assert game_mod.game_running() is False          # not Windows here

    with open(os.path.join(mods, "SomeoneElse_P.pak"), "wb") as f:
        f.write(b"x")
    with open(os.path.join(mods, version.PAK_NAME), "wb") as f:
        f.write(b"x")
    others = game_mod.other_mod_paks(game, version.PAK_NAME)
    assert [os.path.basename(o) for o in others] == ["SomeoneElse_P.pak"], others


# ------------------------------------------------------------------ catalogue
def sample_entry(pack_url=None, sha=None) -> dict:
    e = json.loads(SAMPLE_ENTRY.read_text())
    entry = {k: e[k] for k in ("id", "title", "version", "description", "sha256", "size")}
    entry["pack_url"] = pack_url or file_url(SAMPLE_ZIP)
    entry["rulesets"] = {"en": "Sample rules.\n\n\u2022 One.", "de": "Beispielregeln.\n\n\u2022 Eins."}
    if sha:
        entry["sha256"] = sha
    return entry


def sample_catalogue(**over) -> dict:
    d = {
        "catalogue_version": 1,
        "hub": {"version": "1.0.0",
                "download_url": "https://example.test/hub/LightsOut-1.0.0.exe",
                "page_url": "https://example.test/"},
        "gamemodes": [sample_entry()],
    }
    d.update(over)
    return d


def test_rules_override_is_cleaned_before_it_reaches_the_builder():
    """It arrives over the network and ends in struct.pack, so anything that is not a number we
    know the meaning of is dropped rather than passed on."""
    entry = dict(sample_entry(), rules_override={
        "score_limit": 2, "max_rounds": "3", "time_limit": 90.5,
        "drone_class": "spectator",          # not a rule the builder takes a number for
        "team_size": [5],                    # not a number
    })
    assert cat.rules_override(entry) == {"score_limit": 2, "max_rounds": 3, "time_limit": 90.5}
    assert cat.rules_override(sample_entry()) == {}
    assert cat.rules_override(dict(sample_entry(), rules_override="2")) == {}


def test_a_changed_override_counts_as_an_update_even_at_the_same_version():
    """Without this the row would read "installed" while the pak on disk still played to seven."""
    entry = dict(sample_entry(), version="1.0.0", rules_override={"score_limit": 2})
    same = {"version": "1.0.0", "rules_override": {"score_limit": 2}}
    assert cat.mode_update_available(entry, same) is False
    assert cat.mode_update_available(entry, {"version": "1.0.0"}) is True
    assert cat.mode_update_available(dict(sample_entry(), version="1.0.0"), same) is True
    # a newer version still wins on its own, override or not
    assert cat.mode_update_available(dict(sample_entry(), version="1.0.1"), same) is True
    # an installed gamemode the catalogue no longer lists has nothing to compare against
    assert cat.mode_update_available(dict(entry, _orphan=True), same) is False


def test_apply_passes_the_rules_override_to_the_builder_and_remembers_it():
    """The end of the wire: catalogue -> build_from_packs(rules_override=...) -> state."""
    game = fake_game_dir("apply-override")
    calls = {}

    def fake_build(paks_dir, work_dir, pack_dirs, out_path, log=print, progress=None,
                   rules_override=None):
        calls["rules_override"] = rules_override
        with open(out_path, "wb") as f:
            f.write(b"MERGED PAK")
        return {"out": out_path, "size": 10,
                "sha256": hashlib.sha256(b"MERGED PAK").hexdigest(), "modes": ["CTF"]}

    real = ops.build_from_packs
    ops.build_from_packs = fake_build
    try:
        st = state_mod.default_state()
        cta = sample_catalogue()
        cta["gamemodes"][0]["rules_override"] = {"score_limit": 2, "max_rounds": 3}
        ops.apply({"CTF"}, cta, st, game, log=lambda m: None)
    finally:
        ops.build_from_packs = real

    assert calls["rules_override"] == {"CTF": {"score_limit": 2, "max_rounds": 3}}, calls
    assert st["installed"]["CTF"]["rules_override"] == {"score_limit": 2, "max_rounds": 3}


def test_fetch_and_validate_catalogue():
    d = sample_catalogue()
    p = os.path.join(tmpdir(), "catalogue.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(d, f)

    got = cat.fetch_catalogue(file_url(p))
    assert got["gamemodes"][0]["id"] == "CTF", got
    assert cat.entry_by_id(got, "CTF")["title"] == "Capture the Flag 10v10"
    assert cat.entry_by_id(got, "Nope") is None

    for broken, why in [
        ({}, "empty"),
        ({"catalogue_version": 1, "gamemodes": []}, "no hub"),
        ({"catalogue_version": 1, "hub": {"version": "1"}, "gamemodes": []}, "no download_url"),
        ({"catalogue_version": 1, "hub": d["hub"], "gamemodes": {}}, "gamemodes not a list"),
        ({"catalogue_version": 1, "hub": d["hub"],
          "gamemodes": [{"id": "X", "title": "X", "version": "1"}]}, "entry missing pack_url"),
        ({"catalogue_version": 1, "hub": d["hub"],
          "gamemodes": [sample_entry(), sample_entry()]}, "duplicate id"),
    ]:
        try:
            cat.validate_catalogue(broken)
        except ValueError:
            pass
        else:
            raise AssertionError(f"validate_catalogue accepted a bad catalogue: {why}")

    # a fetch of something that is not there raises
    try:
        cat.fetch_catalogue(file_url(os.path.join(tmpdir(), "missing.json")))
    except Exception:
        pass
    else:
        raise AssertionError("fetch_catalogue accepted a missing file")


def test_version_newer():
    assert cat.version_newer("1.0.1", "1.0.0")
    assert cat.version_newer("1.1.0", "1.0.9")
    assert cat.version_newer("2.0", "1.9.9")
    assert cat.version_newer("1.0.10", "1.0.9")          # numeric, not lexicographic
    assert cat.version_newer("1.0.1", "1.0")
    assert not cat.version_newer("1.0.0", "1.0.0")
    assert not cat.version_newer("1.0", "1.0.0")         # padded with zeros
    assert not cat.version_newer("1.0.0", "1.0.1")
    assert not cat.version_newer("", "1.0.0")
    assert cat.version_newer("1.0.0", "")


def test_ensure_pack_ok():
    entry = sample_entry()
    packs = tmpdir("hub-packs-")
    seen = []
    d = cat.ensure_pack(entry, packs, progress=lambda done, total: seen.append((done, total)))

    assert d == os.path.join(packs, "CTF-1.0.0"), d
    assert os.path.isfile(os.path.join(d, "manifest.json"))
    assert os.path.isdir(os.path.join(d, "cooked"))
    man = json.load(open(os.path.join(d, "manifest.json")))
    assert man["id"] == "CTF", man
    assert seen and seen[-1][0] == entry["size"], seen[-1]
    assert not [n for n in os.listdir(packs) if n != "CTF-1.0.0"], os.listdir(packs)  # no leftovers

    # second call is a no-op that returns the same folder without downloading
    marker = os.path.join(d, "cooked", "_marker")
    open(marker, "w").close()
    assert cat.ensure_pack(entry, packs) == d
    assert os.path.exists(marker)
    assert open(os.path.join(d, ".sha256")).read().strip() == entry["sha256"].lower()

    # same version, different content published (pre-release re-pack): the cached copy is replaced
    stale = dict(entry)
    with open(os.path.join(d, ".sha256"), "w") as f:
        f.write("deadbeef\n")
    assert cat.ensure_pack(stale, packs) == d
    assert not os.path.exists(marker), "stale cached pack was not refreshed"
    assert open(os.path.join(d, ".sha256")).read().strip() == entry["sha256"].lower()


def test_ensure_pack_bad_sha():
    entry = sample_entry(sha="0" * 64)
    packs = tmpdir("hub-packs-bad-")
    try:
        cat.ensure_pack(entry, packs)
    except Exception as e:
        assert "checksum" in str(e).lower(), e
    else:
        raise AssertionError("ensure_pack accepted a pack with the wrong sha256")
    assert not os.path.exists(os.path.join(packs, "CTF-1.0.0"))
    assert os.listdir(packs) == [], os.listdir(packs)       # nothing left behind


def test_ensure_pack_path_traversal():
    """A zip whose member escapes the pack folder must be rejected, and nothing written."""
    evil_zip = os.path.join(tmpdir(), "evil.zip")
    with zipfile.ZipFile(evil_zip, "w") as z:
        z.writestr("manifest.json", '{"id": "EVIL", "version": "1.0.0"}')
        z.writestr("cooked/ok.txt", "fine")
        z.writestr("../../escaped.txt", "pwned")
    sha = hashlib.sha256(Path(evil_zip).read_bytes()).hexdigest()
    entry = {"id": "EVIL", "title": "Evil", "version": "1.0.0",
             "pack_url": file_url(evil_zip), "sha256": sha,
             "size": os.path.getsize(evil_zip)}

    packs = tmpdir("hub-packs-evil-")
    outside = Path(packs).parent / "escaped.txt"
    if outside.exists():
        outside.unlink()
    try:
        cat.ensure_pack(entry, packs)
    except Exception as e:
        assert "outside" in str(e).lower() or "absolute" in str(e).lower(), e
    else:
        raise AssertionError("ensure_pack extracted a zip with a path-traversal member")
    assert not outside.exists(), "a zip member escaped the pack folder"
    assert os.listdir(packs) == [], os.listdir(packs)


# ------------------------------------------------------------------ ops.apply
def test_apply_empty_removes_pak():
    game = fake_game_dir("apply-empty")
    mods = game_mod.mods_dir(game)
    pak = os.path.join(mods, version.PAK_NAME)
    with open(pak, "wb") as f:
        f.write(b"old pak bytes")

    st = state_mod.default_state()
    st["installed"] = {"CTF": {"version": "1.0.0", "title": "Capture the Flag 10v10"}}
    st["pak_sha256"] = "abc"
    state_mod.save(st)

    lines, prog = [], []
    res = ops.apply(set(), sample_catalogue(), st, game,
                    log=lines.append, progress=lambda f, m: prog.append((f, m)))

    assert not os.path.exists(pak), "the pak was not removed"
    assert res["removed"] is True and res["installed"] == [], res
    assert st["installed"] == {} and st["pak_sha256"] is None, st
    assert state_mod.load()["installed"] == {}, "state.json was not saved"
    assert lines and prog and prog[-1][0] == 1.0, (lines[-1:], prog[-1:])
    assert os.path.isfile(paths.log_file()), "no logs/last-run.log written"

    # a stock install has no ~mods folder at all (the game ships none): the empty one goes too
    assert not os.path.isdir(mods), "empty ~mods folder should be removed on uninstall"

    # and again with no pak present (and no ~mods): still fine, still ends up uninstalled, nothing created
    res = ops.apply(set(), sample_catalogue(), st, game, log=lines.append)
    assert res["removed"] is True and not os.path.exists(pak) and not os.path.isdir(mods)

    # someone else's pak in ~mods: our pak goes, their folder stays
    os.makedirs(mods)
    with open(pak, "wb") as f:
        f.write(b"ours")
    with open(os.path.join(mods, "SomeoneElses_P.pak"), "wb") as f:
        f.write(b"theirs")
    st["installed"] = {"CTF": {"version": "1.0.0", "title": "x"}}
    ops.apply(set(), sample_catalogue(), st, game, log=lines.append)
    assert not os.path.exists(pak) and os.path.isfile(os.path.join(mods, "SomeoneElses_P.pak"))


def test_apply_install_with_fake_builder():
    """The real builder needs the player's game paks and a minute of CPU, so swap it for a
    fake and check the parts the hub owns: download, build into a .tmp, atomic
    replace, state update."""
    game = fake_game_dir("apply-install")
    mods = game_mod.mods_dir(game)
    pak = os.path.join(mods, version.PAK_NAME)
    with open(pak, "wb") as f:
        f.write(b"previous pak")           # must survive until the final replace

    calls = {}

    def fake_build(paks_dir, work_dir, pack_dirs, out_path, log=print):
        calls["paks_dir"] = paks_dir
        calls["work_dir"] = work_dir
        calls["pack_dirs"] = list(pack_dirs)
        calls["out_path"] = out_path
        # the real pak must still be the old one while we build
        assert out_path.endswith(".tmp"), out_path
        assert open(pak, "rb").read() == b"previous pak", "the old pak was replaced too early"
        log("fake builder: writing the merged pak")
        with open(out_path, "wb") as f:
            f.write(b"MERGED PAK")
        return {"out": out_path, "size": 10,
                "sha256": hashlib.sha256(b"MERGED PAK").hexdigest(), "modes": ["CTF"]}

    real = ops.build_from_packs
    ops.build_from_packs = fake_build
    try:
        st = state_mod.default_state()
        cta = sample_catalogue()
        lines, prog = [], []
        res = ops.apply({"CTF"}, cta, st, game,
                        log=lines.append, progress=lambda f, m: prog.append((f, m)))
    finally:
        ops.build_from_packs = real

    assert open(pak, "rb").read() == b"MERGED PAK", "the new pak was not swapped in"
    assert not os.path.exists(pak + ".tmp"), "the .tmp file was left behind"
    assert calls["paks_dir"] == game_mod.paks_dir(game), calls["paks_dir"]
    assert calls["work_dir"] == str(paths.work_dir()), calls["work_dir"]
    assert calls["pack_dirs"] == [os.path.join(str(paths.packs_dir()), "CTF-1.0.0")], calls
    assert os.path.isfile(os.path.join(calls["pack_dirs"][0], "manifest.json"))
    assert res["installed"] == ["CTF"] and res["removed"] is False, res
    assert st["installed"] == {"CTF": {"version": "1.0.0", "title": "Capture the Flag 10v10", "titles": {}}}, st
    assert st["pak_sha256"] == hashlib.sha256(b"MERGED PAK").hexdigest(), st
    assert st["game_dir"] == game
    assert state_mod.load()["installed"] == st["installed"], "state.json was not saved"
    assert prog[-1][0] == 1.0, prog[-1]
    assert any("fake builder" in ln for ln in lines), lines[-3:]


def test_apply_reports_a_real_bar_through_the_build():
    """The install bar is determinate: the builder's own step fractions are forwarded, mapped into
    ops.BUILD_BAND, so the UI never falls back to the indeterminate stripe while the pak is being
    built (that stripe used to cover the whole 30 s build, which was every install's slow part)."""
    game = fake_game_dir("apply-progress")

    def fake_build(paks_dir, work_dir, pack_dirs, out_path, log=print, progress=None):
        assert progress is not None, "ops.apply did not offer the builder a progress callback"
        for f in (0.0, 0.25, 0.5, 0.75, 1.0):
            progress(f, "step")
        with open(out_path, "wb") as f:
            f.write(b"MERGED PAK")
        return {"out": out_path, "size": 10, "sha256": "c" * 64, "modes": ["CTF"]}

    prog = []
    real = ops.build_from_packs
    ops.build_from_packs = fake_build
    try:
        ops.apply({"CTF"}, sample_catalogue(), state_mod.default_state(), game,
                  log=lambda m: None, progress=lambda f, m: prog.append((f, m)))
    finally:
        ops.build_from_packs = real

    fracs = [f for f, _ in prog]
    assert None not in fracs, "the bar went indeterminate during an install: %r" % (prog,)
    assert fracs == sorted(fracs), "the bar went backwards: %r" % (fracs,)
    assert fracs[-1] == 1.0, fracs
    lo, hi = ops.BUILD_BAND
    # the builder's 0.0 and 1.0 land on the ends of the build's share of the bar
    assert lo in fracs and hi in fracs, (ops.BUILD_BAND, fracs)
    from hub.i18n import t
    build_msg = t("building")
    building = [f for f, m in prog if m == build_msg]
    assert len(building) >= 5 and all(lo <= f <= hi for f in building), building
    # a builder that does not take the kwarg is never handed one (older builder, or a test fake)
    ops.build_from_packs = lambda paks_dir, work_dir, pack_dirs, out_path, log=print: None
    try:
        assert not ops._builder_takes_progress(), "a log-only builder must not be handed progress"
    finally:
        ops.build_from_packs = real


def test_apply_failure_keeps_old_pak():
    """If the builder blows up, the previously installed pak and state are untouched."""
    game = fake_game_dir("apply-fail")
    mods = game_mod.mods_dir(game)
    pak = os.path.join(mods, version.PAK_NAME)
    with open(pak, "wb") as f:
        f.write(b"previous pak")

    def boom(paks_dir, work_dir, pack_dirs, out_path, log=print):
        with open(out_path, "wb") as f:
            f.write(b"half written")
        raise RuntimeError("cook verdict missing")

    st = state_mod.default_state()
    st["installed"] = {"CTF": {"version": "0.9.0", "title": "Capture the Flag 10v10"}}
    real = ops.build_from_packs
    ops.build_from_packs = boom
    try:
        ops.apply({"CTF"}, sample_catalogue(), st, game, log=lambda m: None)
    except RuntimeError as e:
        assert "cook verdict missing" in str(e), e
    else:
        raise AssertionError("ops.apply swallowed a builder failure")
    finally:
        ops.build_from_packs = real

    assert open(pak, "rb").read() == b"previous pak", "the old pak was damaged"
    assert not os.path.exists(pak + ".tmp"), "the half-built .tmp was left behind"
    assert st["installed"] == {"CTF": {"version": "0.9.0", "title": "Capture the Flag 10v10"}}, st


def test_apply_rejects_bad_game_dir():
    st = state_mod.default_state()
    try:
        ops.apply(set(), sample_catalogue(), st, os.path.join(tmpdir(), "not-a-game"),
                  log=lambda m: None)
    except RuntimeError as e:
        assert "game folder" in str(e).lower(), e
    else:
        raise AssertionError("ops.apply accepted a folder that is not a game install")


def test_apply_unknown_id():
    game = fake_game_dir("apply-unknown")
    st = state_mod.default_state()
    try:
        ops.apply({"NOPE"}, sample_catalogue(), st, game, log=lambda m: None)
    except RuntimeError as e:
        assert "NOPE" in str(e), e
    else:
        raise AssertionError("ops.apply accepted an id that is not in the catalogue")


# ------------------------------------------------------------------ paths / builder wiring
def test_tools_dir_and_builder_path():
    td = paths.tools_dir()
    assert str(td).endswith(os.path.join("tools", "pak")), td
    assert (td / "build_gamemode.py").is_file(), td
    inserted = paths.ensure_builder_on_path()
    assert inserted in sys.path and inserted == str(td)


def test_builder_reports_its_steps_and_packs_the_same_bytes():
    """The builder half of the install bar: the step weights cover the whole build (they are what
    maps a step onto the bar), and write_pak's progress is byte-counted, monotonic and changes
    nothing about the pak it writes."""
    paths.ensure_builder_on_path()
    import build_gamemode                              # noqa: WPS433 (needs the path above)
    import paklib                                      # noqa: WPS433

    steps = build_gamemode.BUILD_STEPS
    assert abs(sum(steps.values()) - 1.0) < 1e-9, steps   # else the bar stops short or overshoots
    assert all(w > 0 for w in steps.values()), steps

    files = {"a/small.uasset": b"x" * 100, "a/big.uexp": bytes(range(256)) * 400, "c.bin": b"hello"}
    big = [rel for rel, d in files.items() if len(d) >= 65536]
    seen = []
    tmp = tempfile.mkdtemp(prefix="hub-pak-")
    with_progress = os.path.join(tmp, "with.pak")
    without = os.path.join(tmp, "without.pak")
    paklib.write_pak(with_progress, "../../../", files, seed=0, compress=big,
                     progress=lambda done, total: seen.append((done, total)))
    paklib.write_pak(without, "../../../", files, seed=0, compress=big)
    assert open(with_progress, "rb").read() == open(without, "rb").read(), \
        "asking for progress changed the pak"
    total = sum(len(d) for d in files.values())
    assert seen[0] == (0, total) and seen[-1] == (total, total), seen[:1] + seen[-1:]
    assert [d for d, _ in seen] == sorted(d for d, _ in seen), seen
    assert len(seen) > len(files), "the big entry must report per compressed block, not once"


def test_app_imports_without_opening_a_window():
    """importing hub.app must not create a Tk window (needed for PyInstaller too)."""
    import subprocess
    r = subprocess.run([sys.executable, "-c", "import hub.app; print('imported ok')"],
                       cwd=str(REPO), capture_output=True, text=True, timeout=60,
                       env={**os.environ, "DISPLAY": ""})
    assert r.returncode == 0, r.stderr
    assert "imported ok" in r.stdout, (r.stdout, r.stderr)


def test_ui_constructs_under_xvfb():
    """Optional: build the whole window headlessly under xvfb-run, if it is installed."""
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    code = (
        "import tkinter as tk, os, json, tempfile\n"
        "from hub.app import HubApp, SELECT_BG, WHITE\n"
        "HubApp.tray_available = staticmethod(lambda: False)\n"
        "root = tk.Tk()\n"
        "app = HubApp(root, language='en')\n"
        "root.update()\n"
        # let the background catalogue load land first: its queue message resets the status line and rebuilds the rows,
        # which would otherwise race the assertions below (seen once as a flake after the ruleset steps were added)
        "import time\n"
        "for _ in range(100):\n"
        "    root.update(); time.sleep(0.05)\n"
        "    if app.catalogue is not None: break\n"
        "assert app.catalogue is not None, 'catalogue never loaded'\n"
        "app.catalogue = json.load(open(os.environ['HUB_TEST_CAT']))\n"
        "app._refresh_rows()\n"
        "root.update()\n"
        "assert app.rows and 'CTF' in app.rows and not app.selected\n"
        "row = app.rows['CTF']\n"
        "assert row['column'] == 'available' and row['frame'].master is app.columns['available']\n"
        "assert str(app.btn_install['state']) == 'disabled' and str(app.btn_uninstall['state']) == 'disabled'\n"
        # click the name -> selected (blue) -> Install enabled
        "row['name'].event_generate('<Button-1>'); root.update()\n"
        "assert app.selected == {'CTF'} and row['frame']['bg'] == SELECT_BG and row['name']['bg'] == SELECT_BG\n"
        "assert str(app.btn_install['state']) == 'normal', app.btn_install['state']\n"
        "assert str(app.btn_uninstall['state']) == 'disabled'\n"
        # click again -> deselected (white)
        "app._toggle_selected('CTF'); root.update()\n"
        "assert not app.selected and row['frame']['bg'] == WHITE\n"
        "assert str(app.btn_install['state']) == 'disabled'\n"
        # installed: the row moves to the Installed column; a click selects it and enables Uninstall (not Install)
        "app.state['installed'] = {'CTF': {'version': '1.0.0', 'title': 'CTF'}}\n"
        "app._refresh_rows(); root.update(); row = app.rows['CTF']\n"
        "assert row['column'] == 'installed' and row['frame'].master is app.columns['installed']\n"
        "assert not app.selected and str(app.btn_uninstall['state']) == 'disabled'\n"
        "row['frame'].event_generate('<Button-1>'); root.update()   # any part of the row selects it\n"
        "assert app.selected == {'CTF'}\n"
        "assert str(app.btn_uninstall['state']) == 'normal' and str(app.btn_install['state']) == 'disabled'\n"
        # the notepad icon: clicking it opens the Ruleset window (hub language, English fallback) and never selects
        "assert row['ruleset'] is not None and row['ruleset']['bg'] == SELECT_BG\n"
        "assert app._ruleset_text(app.catalogue['gamemodes'][0]) == 'Sample rules.\\n\\n\u2022 One.'\n"
        "import hub.i18n as i18n; i18n.set_language('de'); assert app._ruleset_text(app.catalogue['gamemodes'][0]).startswith('Beispielregeln'); i18n.set_language('en')\n"
        "assert app._ruleset_text({'rulesets': {'fr': 'seulement'}}) == 'seulement' and app._ruleset_text({}) == ''\n"
        "win = app._show_ruleset(app.catalogue['gamemodes'][0]); root.update()\n"
        "assert win is not None and win.winfo_exists() and 'Ruleset' in win.title(), win.title()\n"
        "texts = [w for w in win.winfo_children()[0].winfo_children() if isinstance(w, tk.Text)]\n"
        "assert texts and texts[0].get('1.0', 'end').startswith('Sample rules.') and str(texts[0]['state']) == 'disabled'\n"
        "win.destroy(); root.update()\n"
        "assert app.selected == {'CTF'}   # opening the rules did not touch the selection\n"
        "orphan = {'id': 'OLD', 'title': 'Old', 'version': '0.1', '_orphan': True}\n"
        "assert app._ruleset_text(orphan) == '' and app._show_ruleset(orphan) is None\n"
        # a newer version in the catalogue: the row gets an Update BUTTON; pressing it applies installed (re-fetched)
        "app.catalogue['gamemodes'][0]['version'] = '1.2.0'\n"
        "app._refresh_rows(keep_selection=True); root.update(); row = app.rows['CTF']\n"
        "assert row['update'] is not None and row['update']['text'] == 'Update' and app.selected == {'CTF'}\n"
        "assert str(app.btn_install['state']) == 'normal' and str(app.btn_uninstall['state']) == 'normal'\n"
        "applied = []\n"
        "app._apply = lambda desired: applied.append(set(desired))\n"
        "row['update'].invoke(); root.update()\n"
        "assert applied == [{'CTF'}] and app._updating == 'CTF', applied\n"
        "app.q.put(('done', {'removed': False})); app._pump(); root.update()\n"
        "assert app.status['text'] == 'Updated. Launch Bodycam and pick the gamemode.', app.status['text']\n"
        # no Update button when the installed version is current ('done' reloaded state.json, so re-seed)
        "app.state['installed'] = {'CTF': {'version': '1.0.0', 'title': 'CTF'}}\n"
        "app.catalogue['gamemodes'][0]['version'] = '1.0.0'; app._refresh_rows(); root.update()\n"
        "assert app.rows['CTF']['update'] is None\n"
        "app._toggle_selected('CTF'); root.update()\n"
        # what the buttons would apply
        "assert (set(app._installed()) | set(app.selected)) == {'CTF'}\n"
        "assert (set(app._installed()) - set(app.selected)) == set()\n"
        # a finished job clears the selection
        "app.q.put(('done', {'removed': False})); app._pump(); root.update()\n"
        "assert not app.selected\n"
        # no update screen while the catalogue's hub is not newer than us
        "assert app.update_screen is None and not app._check_hub_update()\n"
        # no tray here: the X quits (root gone afterwards)\n"
        "app._on_close()\n"
        "try:\n"
        "    root.winfo_exists(); alive = True\n"
        "except tk.TclError:\n"
        "    alive = False\n"
        "assert not alive, 'X should have closed the window without a tray'\n"
        "print('ui ok', flush=True); os._exit(0)   # skip Tk teardown: it can segfault under xvfb after the worker threads ran\n"
    )
    cat_path = os.path.join(tmpdir(), "cat.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(sample_catalogue(), f)
    # its own state dir: the other tests left an installed CTF in the shared one
    env = {**os.environ, "HUB_TEST_CAT": cat_path,
           "HUB_STATE_DIR": tmpdir("hub-ui-state-"),
           "HUB_GAME_DIR": fake_game_dir("ui-game"),
           "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "ui ok" in r.stdout, (r.stdout, r.stderr)


def test_i18n_tables_complete():
    """Every language has exactly the English key set, the same {placeholders}, and no empty strings."""
    import re
    from hub import i18n
    assert i18n.CODES == ["en", "de", "es", "fr", "pt", "ru", "zh"], i18n.CODES   # = Content/Localization/Game/<code>
    en = i18n.STRINGS["en"]
    ph = lambda s: set(re.findall(r"{(\w+)}", s))
    for code in i18n.CODES:
        table = i18n.STRINGS[code]
        assert set(table) == set(en), (code, set(table) ^ set(en))
        for k, v in table.items():
            assert isinstance(v, str) and v.strip(), (code, k)
            assert ph(v) == ph(en[k]), (code, k, ph(v), ph(en[k]))
    # lookups: current language, English fallback, key fallback, formatting
    i18n.set_language("de")
    assert i18n.t("install") == "Installieren"
    assert i18n.t("game_line", path="X:\\y") == "Spiel: X:\\y"
    assert i18n.tr("zh", "install") == "安装"
    assert i18n.t("no_such_key") == "no_such_key"
    assert i18n.set_language("xx") == "en" and i18n.get_language() == "en"
    assert i18n.name_of("ru") == "Русский"
    assert i18n.font_family("zh") and i18n.font_family("en") is None
    # system-language guess never raises and always returns a shipped code
    for val in ("de_DE.UTF-8", "German_Germany.1252", "zh-CN", "pt_BR", "C", "", None, "xx_YY"):
        os.environ["LC_ALL"] = val or ""
        assert i18n.detect_system_language() in i18n.CODES, val
    os.environ.pop("LC_ALL", None)
    assert i18n._from_locale_string("fr_FR") == "fr" and i18n._from_locale_string("Russian_Russia") == "ru"


def test_display_title_and_ops_messages_follow_language():
    from hub import i18n, catalogue as cat_mod
    entry = {"id": "CTF", "title": "Capture the Flag 10v10",
             "titles": {"fr": "Capture du drapeau 10v10", "ru": "Захват флага 10 на 10"}}
    i18n.set_language("fr")
    assert cat_mod.display_title(entry) == "Capture du drapeau 10v10"
    i18n.set_language("de")                       # no German entry -> English title
    assert cat_mod.display_title(entry) == "Capture the Flag 10v10"
    assert cat_mod.display_title({"id": "X"}) == "X"
    assert cat_mod.display_title({"id": "X", "titles": "junk"}) == "X"
    # ops.apply speaks the hub's language too (game-folder error is the cheapest path)
    i18n.set_language("es")
    st = state_mod.default_state()
    try:
        ops.apply({"CTF"}, sample_catalogue(), st, tmpdir("not-a-game-"), log=lambda m: None)
        assert False, "expected a failure"
    except RuntimeError as e:
        assert str(e) == i18n.tr("es", "no_game_dir"), e
    i18n.set_language("en")


def test_language_dialog_under_xvfb():
    """Optional: the first-run prompt builds, previews the selected language, and returns the choice."""
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    code = (
        "import tkinter as tk, os, json\n"
        "from hub.app import LanguageDialog, HubApp\n"
        "from hub import i18n, state as state_mod\n"
        "root = tk.Tk(); root.withdraw()\n"
        "d = LanguageDialog(root, 'de'); root.update()\n"
        "assert d.var.get() == 'de' and d.prompt['text'] == i18n.tr('de', 'lang_prompt')\n"
        "assert len(d.buttons) == 7 and d.btn_ok['text'] == 'OK'\n"
        "d.var.set('zh'); root.update()\n"
        "assert d.prompt['text'] == i18n.tr('zh', 'lang_prompt') and d.btn_ok['text'] == '确定'\n"
        "d._ok(); root.update(); assert d.result == 'zh'\n"
        # first run: no language in state -> HubApp asks through its ask_language hook
        "assert state_mod.load()['language'] is None\n"
        "seen = []\n"
        "HubApp.tray_available = staticmethod(lambda: False)\n"
        "HubApp.ask_language = staticmethod(lambda parent, initial: (seen.append(initial), seen.append(parent.state()), 'fr')[2])\n"
        "app = HubApp(root); root.update()\n"
        "assert seen == [i18n.detect_system_language(), 'normal'], seen   # root must be visible under the dialog\n"
        "assert state_mod.load()['language'] == 'fr', state_mod.load()\n"
        "assert app.btn_install['text'] == 'Installer' and i18n.get_language() == 'fr'\n"
        # second construction: the saved language wins, no prompt
        "HubApp.ask_language = staticmethod(lambda parent, initial: (_ for _ in ()).throw(AssertionError('prompted twice')))\n"
        "app2 = HubApp(tk.Toplevel(root)); root.update()\n"
        "assert app2.btn_install['text'] == 'Installer'\n"
        # Language… switches the whole window
        "i18n.set_language('ru'); app.state['language'] = 'ru'; app._rebuild_ui(); root.update()\n"
        "assert app.btn_install['text'] == 'Установить' and app.btn_language['text'] == 'Язык…'\n"
        "print('dialog ok', flush=True); os._exit(0)   # skip Tk teardown: it can segfault under xvfb after the worker threads ran\n"
    )
    cat_path = os.path.join(tmpdir(), "cat.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(sample_catalogue(), f)
    env = {**os.environ, "HUB_STATE_DIR": tmpdir("hub-lang-state-"),
           "HUB_GAME_DIR": fake_game_dir("lang-game"), "HUB_CATALOGUE_URL": file_url(cat_path),
           "LC_ALL": "C"}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "dialog ok" in r.stdout, (r.stdout, r.stderr)


def test_local_mode_helpers():
    """--local: repo_root() finds the checkout, localize_to_repo() rewrites pack_url to the local zips."""
    root = paths.repo_root()
    assert root is not None and (root / "server" / "public" / "catalogue.json").is_file(), root
    with open(root / "server" / "public" / "catalogue.json", encoding="utf-8") as f:
        real = json.load(f)
    cat.validate_catalogue(real)
    data = cat.localize_to_repo(json.loads(json.dumps(real)), root)
    for m in data["gamemodes"]:
        assert m["pack_url"].startswith("file:"), m["pack_url"]
        assert os.path.isfile(root / "server" / "public" / "packs" / os.path.basename(m["pack_url"]))
        # the rewritten URL downloads and matches the catalogue's own sha256
        got = urllib.request.urlopen(m["pack_url"]).read()
        assert hashlib.sha256(got).hexdigest() == m["sha256"], m["id"]
    # an entry whose zip does not exist locally keeps its URL
    keep = cat.localize_to_repo({"gamemodes": [{"id": "X", "pack_url": "https://h/packs/X-9.9.9.zip"}]}, root)
    assert keep["gamemodes"][0]["pack_url"] == "https://h/packs/X-9.9.9.zip"


def test_only_one_hub_runs_at_a_time():
    """The single-instance guard: the second hub must NOT start, and must leave nothing behind.

    Guards the regression this was written for (2026-09-15): the X button hides to the tray rather
    than quitting, so relaunching from the shortcut used to stack a whole extra hub - exe plus a
    six-process WebView2 stack, each polling /state every 300 ms - and the pile only cleared on a
    reboot.

    The claim/refuse pair is asserted only on Windows, because the lock IS a Windows named mutex;
    everywhere else claim() fails open by design and there is nothing to assert but that."""
    from hub import singleton

    from unittest.mock import patch
    import uuid

    # Exercise the real Windows mutex without contending with a running user's hub.
    with patch.object(singleton, "_MUTEX_NAME", "Local\\CommunityHub-test-" + uuid.uuid4().hex):
        singleton.release()                     # a previous test in this process must not decide this
        assert singleton.claim() is True        # first hub: always allowed through

        if sys.platform == "win32":
            assert singleton.claim() is False   # second hub: refused while the first holds the lock
            singleton.release()
            assert singleton.claim() is True    # released (as a dying process would): allowed again
        else:
            assert singleton.claim() is True    # fails open off Windows

        # An escape hatch, and a --local checkout, each get their own lock rather than being refused.
        os.environ["HUB_ALLOW_MULTIPLE"] = "1"
        try:
            assert singleton.claim() is True
        finally:
            del os.environ["HUB_ALLOW_MULTIPLE"]
        assert singleton._mutex_name("repo") != singleton._mutex_name(None)

        # Raising the other window is best effort and must never raise, with or without one to find.
        assert singleton.focus_existing() in (True, False)

        singleton.release()



def test_tray_icon_and_close_to_tray():
    """The tray module draws its icon and, with a fake tray, the X hides the window and 'Close Lights Out' quits."""
    from hub import tray as tray_mod
    img = tray_mod.icon_image(64)
    assert img.size == (64, 64) and img.mode == "RGBA"
    assert not tray_mod.available() or sys.platform == "win32" or os.environ.get("PYSTRAY_BACKEND")
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    code = (
        "import tkinter as tk, os, json\n"
        "from hub.app import HubApp\n"
        "class FakeTray:\n"
        "    made = []\n"
        "    def __init__(self, title, open_label, quit_label, on_open, on_quit):\n"
        "        self.args = (title, open_label, quit_label); self.on_open = on_open; self.on_quit = on_quit\n"
        "        self.started = False; self.notes = []; self.stopped = False; FakeTray.made.append(self)\n"
        "    def start(self): self.started = True\n"
        "    def notify(self, text, title=None): self.notes.append(text)\n"
        "    def stop(self): self.stopped = True\n"
        "import hub.app as appmod, hub.tray as traymod\n"
        "traymod.Tray = FakeTray\n"
        "HubApp.tray_available = staticmethod(lambda: True)\n"
        "root = tk.Tk(); app = HubApp(root, language='de'); root.update()\n"
        "tray = FakeTray.made[0]; assert tray.started and tray.args == ('Lights Out', 'Öffnen', 'Lights Out schließen'), tray.args\n"
        # X -> hidden, one balloon hint, still alive
        "app._on_close(); root.update()\n"
        "assert root.state() == 'withdrawn' and tray.notes == [appmod.t('tray_hint')], (root.state(), tray.notes)\n"
        # tray click -> shown again (through the queue, like the real thread does)
        "tray.on_open(); app._pump(); root.update()\n"
        "assert root.state() == 'normal', root.state()\n"
        "app._on_close(); root.update(); assert len(tray.notes) == 1   # the hint only once\n"
        # 'Close Lights Out' -> really gone
        "tray.on_quit(); app._pump()\n"
        "try:\n"
        "    root.winfo_exists(); alive = True\n"
        "except tk.TclError:\n"
        "    alive = False\n"
        "assert tray.stopped and not alive\n"
        "print('tray ok', flush=True); os._exit(0)\n"
    )
    cat_path = os.path.join(tmpdir(), "cat.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(sample_catalogue(), f)
    env = {**os.environ, "HUB_STATE_DIR": tmpdir("hub-tray-state-"),
           "HUB_GAME_DIR": fake_game_dir("tray-game"), "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "tray ok" in r.stdout, (r.stdout, r.stderr)


def test_icon_assets():
    """The shipped CH icon files exist, load, and the tray uses them (same picture everywhere)."""
    from hub import tray as tray_mod
    assets = paths.assets_dir()
    for name in ("hub.ico", "hub.png", "hub-tray.png"):
        assert (assets / name).is_file(), name
    from PIL import Image
    ico = Image.open(str(assets / "hub.ico"))
    assert (16, 16) in ico.info.get("sizes", set()) and (256, 256) in ico.info.get("sizes", set()), ico.info
    png = Image.open(str(assets / "hub.png")); assert png.size == (256, 256)
    tray_img = tray_mod.icon_image(64)
    assert tray_img.size == (64, 64)
    assert tray_img.tobytes() == Image.open(str(assets / "hub-tray.png")).convert("RGBA").tobytes()
    # the window icon call must not raise, with or without a display
    import subprocess
    if shutil.which("xvfb-run"):
        code = ("import tkinter as tk; from hub.app import apply_window_icon\n"
                "root = tk.Tk(); apply_window_icon(root); assert getattr(root, '_hub_icon', None) is not None\n"
                "print('icon ok', flush=True); import os; os._exit(0)\n")
        r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "icon ok" in r.stdout, (r.stdout, r.stderr)


def test_mandatory_update_screen():
    """A newer hub in the catalogue replaces the window with the update screen; Download fetches the
    installer into a writable folder; Start hands it (with the catalogue's kind) to launch (hooked) and quits."""
    from hub import update as upd
    # dest_path: never the running exe, always a writable folder
    p = upd.dest_path("https://x/hub/LightsOut-9.9.9.exe", "9.9.9")
    assert os.path.basename(p) == "LightsOut-9.9.9.exe" and os.path.isdir(os.path.dirname(p)), p
    assert os.path.basename(upd.dest_path("https://x/hub/download", "9.9.9")) == "LightsOut-9.9.9.exe"
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    fake_exe = os.path.join(tmpdir("hub-upd-src-"), "LightsOut-Setup-9.9.9.exe")
    with open(fake_exe, "wb") as f:
        f.write(b"MZ" + os.urandom(3000))
    sha = hashlib.sha256(open(fake_exe, "rb").read()).hexdigest()
    cat_data = sample_catalogue()
    # "required" is what still buys the whole window; without it this is the strip, which
    # test_update_strip_does_not_block_the_hub covers.
    cat_data["hub"] = {"version": "9.9.9", "download_url": file_url(fake_exe), "page_url": "https://example.test/",
                       "sha256": sha, "kind": "inno-setup", "required": True}
    cat_path = os.path.join(tmpdir(), "cat.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(cat_data, f)
    dest_dir = tmpdir("hub-upd-dest-")
    code = (
        "import tkinter as tk, os, json, time\n"
        "from hub.app import HubApp\n"
        "import hub.update as upd\n"
        "HubApp.tray_available = staticmethod(lambda: False)\n"
        f"upd.candidate_dirs = lambda: [{dest_dir!r}]\n"
        "launched = []\n"
        "upd.launch = lambda path, kind=None: launched.append((path, kind))\n"
        "root = tk.Tk(); app = HubApp(root, language='es'); root.update()\n"
        "for _ in range(50):\n"
        "    app._pump(); root.update(); time.sleep(0.05)\n"
        "    if app.update_screen is not None: break\n"
        "assert app.update_screen is not None, 'update screen not shown'\n"
        "assert not app.rows and app.btn_update['text'] == 'Descargar actualización', app.btn_update['text']\n"
        "assert '9.9.9' in app.update_screen.winfo_children()[2]['text']\n"
        "app._on_update_button(); root.update()\n"
        "for _ in range(100):\n"
        "    app._pump(); root.update(); time.sleep(0.05)\n"
        "    if app._update_file: break\n"
        "assert app._update_file and os.path.isfile(app._update_file), app.upd_status['text']\n"
        f"assert os.path.dirname(app._update_file) == {dest_dir!r} and os.path.getsize(app._update_file) == 3002\n"
        "assert os.path.basename(app._update_file) == 'LightsOut-Setup-9.9.9.exe', app._update_file\n"
        "assert app.btn_update['text'] == 'Iniciar la nueva versión'\n"
        # language change keeps the update screen (rebuilt in German)
        "from hub import i18n, state as state_mod\n"
        "i18n.set_language('de'); app.state['language'] = 'de'; app._rebuild_ui(); root.update()\n"
        "assert app.update_screen is not None and app.btn_update['text'] == 'Neue Version starten'\n"
        "app._on_update_button(); root.update()\n"
        "assert launched == [(app._update_file, 'inno-setup')], launched\n"
        "try:\n"
        "    root.winfo_exists(); alive = True\n"
        "except tk.TclError:\n"
        "    alive = False\n"
        "assert not alive\n"
        "print('update ok', flush=True); os._exit(0)\n"
    )
    env = {**os.environ, "HUB_STATE_DIR": tmpdir("hub-upd-state-"),
           "HUB_GAME_DIR": fake_game_dir("upd-game"), "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "update ok" in r.stdout, (r.stdout, r.stderr)



def test_resumed_lobby_restores_the_real_stage_from_the_server():
    """Closed the hub (or updated, or dropped) while the lobby was running.

    The lobby lives on the SERVER now, so the replayed match_ready carries its real state - the
    teams, the designated captain, the toss and whatever has been banned - and this client drops
    straight back into the right stage instead of waiting blind. An OLD server that replays no
    lobby state still falls back to the visible wait, ended by match_connecting."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session()
    players = [{"steam_id": s.me["steam_id"], "persona": "Sam"},
               {"steam_id": "76561198000000002", "persona": "P2"}]
    ids = [p["steam_id"] for p in players]
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 12,
                     "players": players, "accepted": 2, "total": 2,
                     "accepted_ids": ids, "resumed": True})
    # a resumed match_ready mid-veto: the server hands back the whole lobby, so we restore it
    s.on_live_event({"type": "match_ready", "match_id": "m1", "resumed": True, "players": players,
                     "teams": {"1": [ids[0]], "2": [ids[1]]},
                     "captains": {"1": ids[0], "2": ids[1]}, "coin_captain": ids[0],
                     "stage": "veto", "coin_result": "heads", "toss_winner": 1,
                     "advantage": "side", "side_picker": 1, "ban_advantage": 2,
                     "first_ban": 2, "ban_turn": 2, "sides": {"1": "attack", "2": "defend"},
                     "bans": [{"team": 2, "map": "Airsoft"}],
                     "pool": ["Airsoft", "BombHouse", "Hospital", "Pool", "Rome", "Russian"]})
    assert s.phase == "lobby" and s.stage == "veto", (s.phase, s.stage)
    assert s.coin_result == "heads" and s.toss_winner == 1 and s.ban_advantage == 2
    assert (2, "Airsoft") in s.bans, "the veto so far is restored, not re-run"
    assert s.rejoined is True and s.locked_in()

    # an OLD server with no lobby state falls back to the visible wait, ended by match_connecting
    s3, _p3 = _live_session()
    s3.on_live_event({"type": "match_found", "match_id": "m3", "accept_seconds": 12,
                      "players": players, "accepted": 2, "total": 2,
                      "accepted_ids": ids, "resumed": True})
    s3.on_live_event({"type": "match_ready", "match_id": "m3", "resumed": True, "players": players})
    assert s3.stage == "rejoin", s3.stage
    assert ("lobby", "rejoin") in C.PHASE_LIMITS, "every locked phase needs a deadline"
    s3.on_live_event({"type": "match_connecting", "match_id": "m3", "connect_seconds": 100,
                      "map": "Rome", "host": ids[1], "connected": [], "total": 2, "resumed": False})
    assert s3.phase == "connecting" and s3.map == "Rome", (s3.phase, s3.map)

    # a FRESH match_ready still opens the lobby at the coin stage
    s2, _p2 = _live_session()
    s2.on_live_event({"type": "match_found", "match_id": "m2", "accept_seconds": 20,
                      "players": players})
    s2.on_live_event({"type": "match_ready", "match_id": "m2", "players": players,
                      "teams": {"1": [ids[0]], "2": [ids[1]]},
                      "captains": {"1": ids[0], "2": ids[1]}, "coin_captain": ids[0],
                      "stage": "coin"})
    assert s2.phase == "lobby" and s2.stage == "coin", (s2.phase, s2.stage)



def test_resumed_live_match_comes_back_off_the_server():
    """The hub was closed, updated or offline while the match was being PLAYED.

    Nothing of it exists in this process, so everything the lobby decided has to come back on
    the replay: roster, teams, sides, veto, map, host. Landing on the idle screen instead is
    the bug this covers (the player is still in a match that is running without them)."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session()
    ids = ["76561198000000%03d" % i for i in range(1, 5)]
    ids[0] = s.me["steam_id"]
    players = [{"steam_id": i, "persona": "P%s" % i[-1]} for i in ids]
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    assert s.phase == "idle"
    s.on_live_event({
        "type": "match_live", "match_id": "m1", "map": "Rome", "host": ids[2],
        "live_seconds": 1200, "players": players, "resumed": True,
        "teams": {"1": ids[:2], "2": ids[2:]},
        "sides": {"1": "defend", "2": "attack"},
        "bans": [{"team": 1, "map": "Pool"}, {"team": 2, "map": "Hospital"}],
    })
    assert s.phase == "live", s.phase
    assert s.map == "Rome"
    assert s.match_id == "m1", "a restarted hub must retain the server's match identity"
    assert [p["steam_id"] for p in s.teams[1]] == ids[:2], s.teams
    assert [p["steam_id"] for p in s.teams[2]] == ids[2:], s.teams
    assert s.my_team() == 1 and s.sides[1] == "defend" and s.sides[2] == "attack"
    assert s.bans == [(1, "Pool"), (2, "Hospital")], s.bans
    assert s.host["steam_id"] == ids[2], s.host
    assert s.i_connected and s.i_accepted, "a live match means we accepted and connected"
    assert s.rejoined is True
    assert s.history_stale is True, "the match is already archived, so our copy is stale"

    # replayed AGAIN (a second reconnect): the screen we are already showing, not a reset
    s.map = "Rome"
    s.on_live_event({"type": "match_live", "match_id": "m1", "map": "Rome", "players": [],
                     "resumed": True})
    assert s.phase == "live" and s.map == "Rome" and len(s.players) == 4


def test_reopened_live_match_keeps_game_cleanup_without_relaunching():
    from unittest.mock import patch
    from hub import competitive as C
    for is_host in (False, True):
        s, panel = _live_session()
        me = s.me["steam_id"]
        peer = "76561198000000042"
        closed = []
        s._run_off_thread = lambda work: work()
        with patch.object(game_mod, "game_running", return_value=True), \
             patch.object(game_mod, "launch_game") as launch, \
             patch.object(game_mod, "close_game", side_effect=lambda **kw: closed.append(kw) or "closed"):
            s.on_live_event({"type": "match_live", "match_id": "resumed-game", "resumed": True,
                             "map": "Rome", "host": me if is_host else peer,
                             "players": [{"steam_id": me}, {"steam_id": peer}],
                             "teams": {"1": [me], "2": [peer]}})
            assert s.match_id == "resumed-game" and s.locked_in()
            assert s._game_ours and s.host_ready
            assert s.launched_for == "resumed-game"
            generation = s._close_gen
            s.on_live_event({"type": "match_live", "match_id": "resumed-game", "resumed": True})
            assert s._close_gen == generation
            launch.assert_not_called()
            s.on_live_event({"type": "match_result", "match": "resumed-game", "winner": 1,
                             "score": [2, 0], "you": {}})
            assert s.game_close == "armed" and not closed
            panel.pump(limit=20)
            assert len(closed) == 1
            assert closed[0]["force"] == (C.CLOSE_GAME_FORCE_HOST if is_host else C.CLOSE_GAME_FORCE_OTHERS)


def test_resumed_live_match_without_bodycam_does_not_open_or_claim_a_game():
    from unittest.mock import patch
    s, _ = _live_session()
    with patch.object(game_mod, "game_running", return_value=False), \
         patch.object(game_mod, "launch_game") as launch:
        s.on_live_event({"type": "match_live", "match_id": "running-match", "resumed": True})
        assert s.phase == "live" and s.match_id == "running-match"
        assert not s._game_ours
        launch.assert_not_called()


def test_resumed_connect_window_names_the_real_host():
    """A hub that comes back INSIDE the connect window gets one payload and nothing else.

    It carries the roster for exactly this reason: without names the host lookup falls back to
    the player themselves, and the screen tells them to join their own game."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session()
    other = "76561198000000002"
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    assert not s.players
    s.on_live_event({"type": "match_connecting", "match_id": "m1", "connect_seconds": 90,
                     "map": "Rome", "host": other, "connected": [other], "total": 2,
                     "resumed": True,
                     "players": [{"steam_id": s.me["steam_id"], "persona": "Sam"},
                                 {"steam_id": other, "persona": "P2"}]})
    assert s.phase == "connecting" and s.connect_left == 90
    assert s.host["steam_id"] == other and s.host["name"] == "P2", s.host
    assert s.connect_total == 2 and s.connected_ids == {other}
    assert s.i_connected is False, "we have not reported in on this new hub yet"


def test_rejoin_grace_releases_a_match_the_service_no_longer_has():
    """A reconnect where the server replays NOTHING means the match is gone (a redeploy, or
    it ended while we were away). The player must not be left pinned in a dead match screen
    until the three-hour `live` watchdog fires, and it must cost them nothing."""
    import time as _time
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")

    # (a) the service still has it: the replay lands and the grace decides nothing
    s, _panel = _live_session()
    s.phase, s.map = "live", "Rome"
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    assert s._rejoin_deadline, "a reconnect while locked in must start the grace"
    s.on_live_event({"type": "match_live", "match_id": "m1", "map": "Rome",
                     "players": [{"steam_id": s.me["steam_id"], "persona": "Sam"}],
                     "resumed": True})
    s._rejoin_deadline = _time.monotonic() - 1
    s._check_rejoin_deadline()
    assert s.phase == "live", "a match the server handed back must survive the grace"
    assert s._rejoin_deadline == 0.0

    # (b) nothing comes back: released, blamed for nothing, told why
    s2, _p2 = _live_session()
    s2.phase, s2.map = "live", "Rome"
    s2.on_live_event({"type": "hello", "steam_id": s2.me["steam_id"]})
    s2._rejoin_deadline = _time.monotonic() - 1
    s2._check_rejoin_deadline()
    assert s2.phase == "idle", s2.phase
    assert s2.error == i18n.t("comp_match_stalled"), s2.error
    assert s2.penalty_until == 0.0, "our side losing the thread must never cost the player"
    assert s2.map is None and not s2.players, "the dead match is cleared out"

    # (c) still offline when the grace runs out: nothing is decided, the next hello re-arms
    s3, _p3 = _live_session()
    s3.phase = "live"
    s3.on_live_event({"type": "hello", "steam_id": s3.me["steam_id"]})
    s3.connected = False
    s3._rejoin_deadline = _time.monotonic() - 1
    s3._check_rejoin_deadline()
    assert s3.phase == "live", "a second drop must not be read as 'the match is gone'"
    assert C.REJOIN_GRACE_SECONDS > 0


def test_update_strip_does_not_block_the_hub():
    """A newer hub WITHOUT `required` is an offer, not a gate (Sam, 2026-09-14).

    It shows as a strip packed above everything else - so it covers nothing and the gamemode
    list keeps working - "Later" takes it away, the download is a background job, and the half
    that actually closes the hub asks first when a competitive match is running."""
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    fake_exe = os.path.join(tmpdir("hub-strip-src-"), "LightsOut-Setup-9.9.9.exe")
    with open(fake_exe, "wb") as f:
        f.write(b"MZ" + os.urandom(3000))
    sha = hashlib.sha256(open(fake_exe, "rb").read()).hexdigest()
    cat_data = sample_catalogue()
    # no "required": this is the ordinary release, and the ordinary release does not take
    # the window away from anybody
    cat_data["hub"] = {"version": "9.9.9", "download_url": file_url(fake_exe),
                       "page_url": "https://example.test/", "sha256": sha, "kind": "inno-setup"}
    cat_path = os.path.join(tmpdir(), "cat.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(cat_data, f)
    dest_dir = tmpdir("hub-strip-dest-")
    state_dir = tmpdir("hub-strip-state-")
    code = (
        "import tkinter as tk, os, json, time\n"
        "from tkinter import messagebox\n"
        "from hub.app import HubApp\n"
        "from hub import i18n, state as state_mod, update as upd\n"
        "HubApp.tray_available = staticmethod(lambda: False)\n"
        f"upd.candidate_dirs = lambda: [{dest_dir!r}]\n"
        "launched = []\n"
        "upd.launch = lambda path, kind=None: launched.append((path, kind))\n"
        "root = tk.Tk(); app = HubApp(root, language='en'); root.update()\n"
        "for _ in range(50):\n"
        "    app._pump(); root.update(); time.sleep(0.05)\n"
        "    if app.update_bar is not None: break\n"
        "assert app.update_bar is not None, 'the update strip never appeared'\n"
        # nothing is blocked: no update screen, and the gamemode list is still there
        "assert app.update_screen is None, 'an ordinary update must not take the window'\n"
        "assert app.rows and 'CTF' in app.rows, 'the gamemode list must still be there'\n"
        "assert str(app.btn_install['state']) in ('normal', 'disabled')\n"
        "app.rows['CTF']['name'].event_generate('<Button-1>'); root.update()\n"
        "assert app.selected == {'CTF'}, 'the hub below the strip still works'\n"
        # ...and nothing is covered: the strip is the FIRST thing packed in the root
        "slaves = root.pack_slaves()\n"
        "assert slaves[0] is app.update_bar and slaves[1] is app.outer, slaves\n"
        "assert app._banner_grown > 0, 'the window must grow by the strip instead of eating content'\n"
        "assert '9.9.9' in app.upd_bar_label['text'] and '1' in app.upd_bar_label['text']\n"
        "assert app.btn_update_bar['text'] == 'Update', app.btn_update_bar['text']\n"
        # the tab is remembered, which is what brings a reopened hub back to a running match
        "app._show_tab('competitive'); root.update()\n"
        "assert state_mod.load()['tab'] == 'competitive'\n"
        "app._show_tab('gamemodes'); root.update()\n"
        "assert state_mod.load()['tab'] == 'gamemodes'\n"
        # "Later" takes it away and hands the height back
        "grown = app._banner_grown\n"
        "app._dismiss_update_banner(); root.update()\n"
        "assert app.update_bar is None and app._banner_grown == 0\n"
        "app._check_hub_update()\n"
        "assert app.update_bar is None, 'Later must hold for this run'\n"
        # bring it back and change language: it is rebuilt, and the height is not taken twice
        "app._update_dismissed = False; app._check_hub_update(); root.update()\n"
        "assert app.update_bar is not None and app._banner_grown == grown\n"
        "i18n.set_language('de'); app.state['language'] = 'de'; app._rebuild_ui(); root.update()\n"
        "assert app.update_bar is not None, 'the strip must survive a language change'\n"
        "assert app.btn_update_bar['text'] == 'Aktualisieren', app.btn_update_bar['text']\n"
        "assert app._banner_grown == grown and root.pack_slaves()[0] is app.update_bar\n"
        "i18n.set_language('en'); app.state['language'] = 'en'; app._rebuild_ui(); root.update()\n"
        # downloading is a background job: it changes nothing but the strip
        "app.btn_update_bar.invoke()\n"
        "for _ in range(100):\n"
        "    app._pump(); root.update(); time.sleep(0.05)\n"
        "    if app._update_file: break\n"
        "assert app._update_file and os.path.isfile(app._update_file), app.upd_bar_label['text']\n"
        f"assert os.path.dirname(app._update_file) == {dest_dir!r}, app._update_file\n"
        "assert app.rows and 'CTF' in app.rows, 'downloading must not disturb the hub'\n"
        "assert app.btn_update_bar['text'] == 'Restart and install', app.btn_update_bar['text']\n"
        # in a match, the half that closes the hub asks first - and takes no for an answer
        "asked = []\n"
        "messagebox.askokcancel = lambda title, message=None, **kw: (asked.append(title), False)[1]\n"
        "app.comp.session.phase = 'live'\n"
        "assert app._match_in_progress()\n"
        "app.btn_update_bar.invoke(); root.update()\n"
        "assert len(asked) == 1 and not launched, (asked, launched)\n"
        "assert root.winfo_exists()\n"
        # closing the hub outright asks the same question, and the same no holds
        "app._quit(); root.update()\n"
        "assert len(asked) == 2 and root.winfo_exists()\n"
        # yes: the installer is handed the file and this hub goes
        "messagebox.askokcancel = lambda title, message=None, **kw: True\n"
        "app.btn_update_bar.invoke()\n"
        "assert launched == [(app._update_file, 'inno-setup')], launched\n"
        "try:\n"
        "    root.winfo_exists(); alive = True\n"
        "except tk.TclError:\n"
        "    alive = False\n"
        "assert not alive, 'the hub has to get out of the way of the installer'\n"
        "print('strip ok', flush=True); os._exit(0)\n"
    )
    env = {**os.environ, "HUB_STATE_DIR": state_dir,
           "HUB_GAME_DIR": fake_game_dir("strip-game"), "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "strip ok" in r.stdout, (r.stdout, r.stderr)


def test_installer_command():
    """The silent Inno Setup argv: installer first, the silent switches, /LOG=<path> last."""
    from hub import update as upd
    argv = upd.installer_command(r"C:\x\LightsOut-Setup-1.1.0.exe", r"C:\x\logs\update-install.log")
    assert isinstance(argv, list) and argv[0] == r"C:\x\LightsOut-Setup-1.1.0.exe", argv
    for switch in ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/LAUNCHHUB=1"):
        assert switch in argv, (switch, argv)
    assert argv[-1] == r"/LOG=C:\x\logs\update-install.log", argv
    assert len(argv) == 7, argv


def test_candidate_dirs_prefers_state_updates():
    """The installer goes to <state>/updates first (never beside the exe); Downloads is the last resort."""
    from hub import update as upd
    state = tmpdir("hub-upd-cand-")
    saved = os.environ.get("HUB_STATE_DIR")
    os.environ["HUB_STATE_DIR"] = state
    try:
        dirs = upd.candidate_dirs()
    finally:
        os.environ.pop("HUB_STATE_DIR", None) if saved is None else os.environ.__setitem__("HUB_STATE_DIR", saved)
    assert dirs, dirs
    assert os.path.normcase(dirs[0]) == os.path.normcase(str(Path(state) / "updates")), dirs
    assert os.path.normcase(dirs[-1]) == os.path.normcase(os.path.join(os.path.expanduser("~"), "Downloads")), dirs


def test_dest_path_keeps_setup_basename():
    """An installer URL keeps its own file name (it ends in .exe) and lands in a writable folder."""
    from hub import update as upd
    p = upd.dest_path("https://x/hub/LightsOut-Setup-1.1.0.exe", "1.1.0")
    assert p.endswith("LightsOut-Setup-1.1.0.exe"), p
    assert os.path.isdir(os.path.dirname(p)), p


def test_clean_old_versions_empties_updates_dir():
    """On start the hub throws away the downloaded installer (*.exe, *.part) in <state>/updates, nothing else."""
    from hub import update as upd
    state = tmpdir("hub-upd-clean-")
    updates = os.path.join(state, "updates")
    os.makedirs(updates)
    for name in ("LightsOut-Setup-1.1.0.exe", "LightsOut-Setup-1.1.0.exe.part", "notes.txt"):
        with open(os.path.join(updates, name), "wb") as f:
            f.write(b"x")
    saved = os.environ.get("HUB_STATE_DIR")
    os.environ["HUB_STATE_DIR"] = state
    try:
        removed = upd.clean_old_versions()
    finally:
        os.environ.pop("HUB_STATE_DIR", None) if saved is None else os.environ.__setitem__("HUB_STATE_DIR", saved)
    assert sorted(removed) == ["LightsOut-Setup-1.1.0.exe", "LightsOut-Setup-1.1.0.exe.part"], removed
    assert sorted(os.listdir(updates)) == ["notes.txt"], os.listdir(updates)
    # a missing updates folder is not an error
    os.environ["HUB_STATE_DIR"] = tmpdir("hub-upd-clean-empty-")
    try:
        assert upd.clean_old_versions() == []
    finally:
        os.environ.pop("HUB_STATE_DIR", None) if saved is None else os.environ.__setitem__("HUB_STATE_DIR", saved)


def test_launch_starts_installer_or_legacy_exe():
    """Both installer and portable updates pass the same trust gate before launch."""
    import contextlib
    from unittest.mock import patch
    from hub import update as upd
    events = []

    @contextlib.contextmanager
    def locked(path):
        events.append("locked")
        yield path
        events.append("unlocked")

    def verify(path):
        events.append("verified")

    def started(argv, **kwargs):
        events.append("started")
        assert events[-3:] == ["locked", "verified", "started"]
        assert kwargs["close_fds"] is True

    with patch.object(upd.update_trust, "locked_update", locked), \
         patch.object(upd.update_trust, "verify_update", verify), \
         patch.object(upd, "Popen", side_effect=started) as launch:
        for kind in ("inno-setup", None, "legacy"):
            events.clear()
            upd.launch("C:/fixture/update.exe", kind)
            argv = launch.call_args.args[0]
            assert argv[0] == "C:/fixture/update.exe"
            assert ("/VERYSILENT" in argv) == (kind == "inno-setup")
            assert events == ["locked", "verified", "started", "unlocked"]

# ------------------------------------------------------------------ Competitive tab
def _ranked_app():
    """An app with the ranked pack INSTALLED, which is the state every queueing test assumes.

    It is spelled out because `find_match` refuses when the pack is missing (competitive.py
    Session.gamemode_installed): a panel with no app at all reads as "nothing installed", which is
    exactly the bug that gate exists to catch and not the situation these tests are about. No
    catalogue, so update_needed() has nothing to compare against - the version gate has its own
    fixture further down (_gated_session)."""
    from hub import competitive as C
    return _FakeApp(installed={C.COMPETITIVE_MODE_ID: {"version": "1.0.0"}})


class _StubPanel:
    """Drives MockSession with no Tk at all: after() queues, pump() fires.

    This is the test that matters on Sam's PC, where there is no xvfb and the UI test
    below is skipped — it covers the whole competitive flow as pure logic."""

    def __init__(self):
        self.pending = []
        self.changes = 0
        self.saved_auth = "unset"
        self.app = _ranked_app()

    def after(self, ms, fn):
        self.pending.append(fn)
        return len(self.pending)

    def on_change(self):
        self.changes += 1

    def map_pool(self):
        from hub import competitive as C
        return C.competitive_pool(C.DEFAULT_MAPS)

    def post(self, fn):
        fn()

    def save_auth(self, payload):
        self.saved_auth = payload

    def pump(self, limit=1):
        """Fire up to `limit` queued timers, oldest first."""
        n = 0
        while self.pending and n < limit:
            self.pending.pop(0)()
            n += 1
        return n


def test_competitive_mock_session_flow():
    """The preview session walks sign-in -> check -> queue -> accept -> lobby -> coin ->
    veto -> live -> result, and the rules Sam fixed hold at every step."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    panel = _StubPanel()
    s = C.MockSession(panel)
    assert s.phase == "signed_out" and s.me is None

    # Sign-in is the one REAL part: Steam OpenID through our service. Drive the point the
    # verified account comes back at, rather than opening a browser in a test.
    s.phase = "signing_in"
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "tok-123"})
    assert s.phase == "idle" and s.me["steam_id"] == "76561198000999000"
    assert s.me["name"] == "Sam" and s.token == "tok-123"
    assert panel.saved_auth["token"] == "tok-123"

    # Find match runs the integrity check first (layers 1 and 2), then queues
    s.find_match()
    assert s.phase == "checking" and s.check_step == 0
    for _ in range(3):
        s._check_next()
    assert s.phase == "queued", s.phase

    s.queue_seconds = 4
    s._tick_queue()
    assert s.phase == "found" and len(s.players) == C.LOBBY_SIZE
    assert s.accepted == 0

    s.accept()
    assert s.accepted == 1
    guard = 0
    while s.phase == "found" and guard < 50:
        s._others_accept()
        guard += 1
    assert s.phase == "lobby" and s.stage == "coin", (s.phase, s.stage)

    # two teams of five, one captain each, and the captain is on the team
    assert len(s.teams[1]) == C.TEAM_SIZE and len(s.teams[2]) == C.TEAM_SIZE
    for team in (1, 2):
        ids = {p["steam_id"] for p in s.teams[team]}
        assert s.captains[team]["steam_id"] in ids
    assert s.my_team() == 1 and s.i_am_captain()

    # EVERY stage a person can hold up carries a clock, offline as well as live (Sam, 2026-09-16:
    # "too many points where a user could infinitly stall the lobby"). The preview has no server to
    # take one from, so it keeps its own - and without them the demo would be the one place in the
    # hub where these stages sit with no countdown at all.
    assert s.stage_seconds == C.PICK_SECONDS, "the coin call has no clock offline"

    s.pick_coin("heads")
    assert s.stage == "flipping"
    # The face is decided on the way UP, so the coin being spun already shows what it will land on
    assert s.coin_result in ("heads", "tails"), "the result must exist while it is still in the air"
    assert s.toss_winner in (1, 2)
    assert s.stage_seconds == C.FLIP_SECONDS
    s._coin_lands()
    assert s.stage == "choice"
    assert s.stage_seconds == C.PICK_SECONDS, "the advantage choice has no clock offline"

    # Taking SIDE gives the winner the attack/defend selector and the loser the ban advantage
    # (bans LAST). The selector is a real step now ("side"), not an auto-assign.
    s.toss_winner = 1
    s.stage = "choice"
    s.choose("side")
    assert s.stage == "side" and s.side_picker == 1 and s.ban_advantage == 2
    assert s.first_ban == 2 and s.ban_turn == 2      # 5 bans (odd): advantage team leads and ends
    assert s.stage_seconds == C.PICK_SECONDS, "the side pick has no clock offline"
    s.choose_side("attack")
    assert s.sides[1] == "attack" and s.sides[2] == "defend"
    assert s.stage == "veto"
    assert s.stage_seconds == C.BAN_SECONDS, "the first ban turn has no clock offline"

    # 7 maps, alternating bans, exactly 6 bans, one map left, turn alternates every ban
    seen_turns = []
    guard = 0
    while s.stage == "veto" and guard < 20:
        seen_turns.append(s.ban_turn)
        s.ban(s.remaining_maps()[0], by=s.ban_turn)
        guard += 1
    pool = C.competitive_pool(C.DEFAULT_MAPS)
    assert s.stage == "ready", (s.stage, s.bans)
    assert len(s.bans) == len(pool) - 1
    assert seen_turns == [2, 1, 2, 1, 2][:len(pool) - 1], seen_turns   # strictly alternating
    assert s.map in pool and s.map not in [m for _, m in s.bans]
    # Paintball is shipped by the gamemode but never appears in a ranked veto (Sam, 2026-09-14)
    assert "Paintball" in C.DEFAULT_MAPS and "Paintball" not in pool
    assert all(m != "Paintball" for _, m in s.bans)

    # TWO LOGS, and a line only ever lands in the one it was sent to.
    s.send_chat("gl hf")
    assert {k: s.chat["team"][-1][k] for k in ("steam_id", "name", "text")} == {
        "steam_id": s.me["steam_id"], "name": s.me["name"], "text": "gl hf"}
    s.send_chat("   ")
    assert s.chat["team"][-1]["text"] == "gl hf", "blank messages are dropped"
    before_team = len(s.chat["team"])
    s.send_chat("glhf all", "all")
    assert s.chat["all"][-1]["text"] == "glhf all"
    assert len(s.chat["team"]) == before_team, "an all-chat line must not reach the team log"
    # An unknown channel is the TEAM log, never the all one: getting that fallback backwards
    # would send a message meant for four people to ten.
    s.send_chat("typo", "enemy")
    assert s.chat["team"][-1]["text"] == "typo" and s.chat["all"][-1]["text"] == "glhf all"

    # the host is the lowest ping of the ten (Sam: best ping hosts)
    s._go_live()
    assert s.phase == "live"
    assert s.host["ping"] == min(p["ping"] for p in s.players)

    # Preview votes count only the caller; showing a void result is an explicit preview action.
    s.start_vote()
    s.cast_vote(True)
    assert s.vote["voted"] and s.vote["yes"] == 1
    s.cast_vote(True)
    assert s.vote["yes"] == 1, "a player only votes once"
    assert s.phase == "live"
    s.finish(voided=True)
    assert s.phase == "result" and s.result["voided"] and s.result["delta"] == 0

    s.leave_result()
    assert s.phase == "idle" and not s.bans and s.map is None

    # a normal finish: a score, and a rank move of 1-3 arrows either way
    s.phase = "live"
    s.finish()
    assert s.phase == "result" and not s.result["voided"]
    assert abs(s.result["delta"]) in (1, 2, 3)
    assert 7 in s.result["score"]

    s.sign_out()
    assert s.phase == "signed_out" and s.me is None
    assert s.token == "" and panel.saved_auth is None, "signing out must forget the token"


def test_competitive_party_codes():
    """Party codes are unambiguous, forgiving to type, and the leader rule holds."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")

    # ABCD-EF, and never a character you could misread
    for _ in range(200):
        code = C.make_party_code()
        assert len(code) == 7 and code[4] == "-", code
        body = code.replace("-", "")
        assert len(body) == C.PARTY_CODE_LEN
        assert all(ch in C.PARTY_CODE_ALPHABET for ch in body), code
    assert not (set("IO01") & set(C.PARTY_CODE_ALPHABET)), "ambiguous characters in the alphabet"

    # what a person actually types
    assert C.normalise_party_code("ABCD-EF") == "ABCD-EF"
    assert C.normalise_party_code("abcdef") == "ABCD-EF"
    assert C.normalise_party_code("  abcd ef ") == "ABCD-EF"
    assert C.normalise_party_code("ab-cd-ef") == "ABCD-EF"
    assert C.normalise_party_code("ABCDE") == ""
    assert C.normalise_party_code("ABCDEFG") == ""
    assert C.normalise_party_code("") == ""
    assert C.normalise_party_code(None) == ""

    panel = _StubPanel()
    s = C.MockSession(panel)
    s.phase = "signing_in"
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "tok-123"})
    assert s.phase == "idle"

    # solo counts as leader, so a lone player can always queue
    assert s.party is None and s.party_size() == 1 and s.is_party_leader()

    s.create_party()
    assert s.party and s.party_size() == 1
    assert s.party["leader_id"] == s.me["steam_id"] and s.is_party_leader()
    assert C.normalise_party_code(s.party["code"]) == s.party["code"]
    my_code = s.party["code"]

    panel.pump()                       # the preview friend turns up
    assert s.party_size() == 2, s.party
    assert s.party["leader_id"] == s.me["steam_id"], "the creator stays the leader"

    # your own code is refused, and a malformed one says so rather than doing nothing
    s.join_party(my_code)
    assert s.party_error and s.party_size() == 2
    s.join_party("nope")
    assert s.party_error and s.party["code"] == my_code

    # joining someone else's party: you are in it, and you are NOT the leader
    s.party = None
    s.join_party("QRST-UV")
    assert s.party and s.party["code"] == "QRST-UV"
    assert s.party_size() == 3 and not s.party_error
    assert s.party["leader_id"] != s.me["steam_id"] and not s.is_party_leader()
    assert any(p["steam_id"] == s.me["steam_id"] for p in s.party["members"])
    assert s.party_leader_name() != s.me["name"]

    # a member cannot start the search; only the leader can
    s.find_match()
    assert s.phase == "idle", "a non-leader must not be able to queue the party"

    # never more than a full team
    assert C.MAX_PARTY == C.TEAM_SIZE == 5
    s.party["members"] += [dict(s.me), dict(s.me)]
    s._mock_friend_joins()
    assert s.party_size() == 5, s.party_size()

    # hide-for-stream: every character blanked but the shape kept, so Copy still has the real one
    masked = C.mask_party_code("ABCD-EF")
    assert masked == "\u2022\u2022\u2022\u2022-\u2022\u2022", masked
    assert len(masked) == len("ABCD-EF") and masked.count("-") == 1
    assert C.mask_party_code("") == ""

    # a new code replaces the old one, and only the leader can mint it
    before = s.party["code"]
    assert not s.is_party_leader()
    assert s.refresh_party_code() is None and s.party["code"] == before, "a member must not remint"
    s.party["leader_id"] = s.me["steam_id"]
    after = s.refresh_party_code()
    assert after and after != before and s.party["code"] == after
    assert C.normalise_party_code(after) == after

    s.leave_party()
    assert s.party is None and s.is_party_leader()
    assert s.refresh_party_code() is None, "no party, no code"

    # the party outlives a match
    s.create_party()
    code = s.party["code"]
    s.reset_match()
    assert s.party and s.party["code"] == code, "reset_match must not drop the party"
    s.sign_out()
    assert s.party is None, "signing out does"


def test_steam_signin_wait_for():
    """auth.wait_for turns the poll loop into one of: an account, or an AuthError."""
    from hub import auth

    calls = []
    def poll_ready(code):
        calls.append(code)
        if len(calls) < 3:
            return {"status": "pending"}
        return {"status": "ready", "token": "tok", "steam_id": "76561198000999000",
                "persona": "Sam"}
    out = auth.wait_for("CODE", sleep=lambda _s: None, poll_fn=poll_ready)
    assert out["steam_id"] == "76561198000999000" and len(calls) == 3

    # a "ready" without the two things that matter is not an account
    try:
        auth.wait_for("CODE", sleep=lambda _s: None,
                      poll_fn=lambda c: {"status": "ready", "token": "tok"})
        assert False, "an incomplete account must not be accepted"
    except auth.AuthError as e:
        assert "incomplete" in str(e)

    for status, expect in (("expired", "expired"),):
        try:
            auth.wait_for("CODE", sleep=lambda _s: None, poll_fn=lambda c: {"status": status})
            assert False, status
        except auth.AuthError as e:
            assert expect in str(e), (status, e)

    # the UI can cancel, and it is reported as its own reason so the session stays quiet
    try:
        auth.wait_for("CODE", should_stop=lambda: True, sleep=lambda _s: None,
                      poll_fn=lambda c: {"status": "pending"})
        assert False, "cancel"
    except auth.AuthError as e:
        assert str(e) == "cancelled"

    # and it gives up rather than polling forever
    clock = {"t": 0.0}
    def tick():
        clock["t"] += 30.0
        return clock["t"]
    try:
        auth.wait_for("CODE", sleep=lambda _s: None, now=tick,
                      poll_fn=lambda c: {"status": "pending"})
        assert False, "timeout"
    except auth.AuthError as e:
        assert "timed out" in str(e)


def test_steam_account_adoption():
    """A verified account becomes the signed-in player; a nameless one falls back to the id."""
    from hub import competitive as C
    panel = _StubPanel()
    s = C.MockSession(panel)
    s.adopt_account({"steam_id": "76561198000999000", "persona": "", "token": "t"})
    assert s.me["name"] == "76561198000999000", "no persona -> show the id, never blank"
    assert panel.saved_auth == "unset", "adopt_account only saves when asked"
    s.adopt_account({"steam_id": "76561198000999000", "persona": "Sam",
                     "avatar": "https://example/a.jpg", "token": "t"}, save=True)
    assert s.me["name"] == "Sam" and s.me["avatar"] == "https://example/a.jpg"
    assert panel.saved_auth["steam_id"] == "76561198000999000"


def test_exe_version_is_not_behind_the_catalogue():
    """HUB_VERSION must not be OLDER than server/public/catalogue.json's hub.version.

    If it is, the exe you just built compares itself against the catalogue, decides it is
    out of date, and replaces its whole window with the mandatory update screen — which
    downloads the PUBLISHED build over the top of the one you were trying to test. That
    cost an hour on 2026-09-14 after a stale copy of version.py was written back over a
    publish bump, and the symptom (a freshly built hub behaving like the old one) points
    nowhere near the cause. Fail the build instead."""
    cat_path = REPO / "server" / "public" / "catalogue.json"
    if not cat_path.is_file():
        print("      (skipped: no server/public/catalogue.json)")
        return
    catalogue = json.loads(cat_path.read_text(encoding="utf-8"))
    published = ((catalogue.get("hub") or {}).get("version") or "").strip()
    if not published:
        print("      (skipped: the catalogue has no hub.version)")
        return
    assert not cat.version_newer(published, version.HUB_VERSION), (
        f"hub/version.py says {version.HUB_VERSION} but the catalogue says {published}. "
        "A build from this tree would show itself the mandatory update screen and replace "
        "itself with the published exe. Bump HUB_VERSION (or publish) before building."
    )


def test_avatar_url_allowlist():
    """The avatar URL arrives over the network, so the hub only ever fetches Steam's hosts."""
    from hub import avatars
    for good in ("https://avatars.steamstatic.com/abc_medium.jpg",
                 "https://avatars.cloudflare.steamstatic.com/abc_full.jpg",
                 "https://community.steamstatic.com/public/images/x.png",
                 "https://steamcommunity.com/a.png"):
        assert avatars.is_allowed(good), good
    for bad in ("http://avatars.steamstatic.com/a.jpg",          # plain http
                "https://evil.example/a.jpg",
                "https://steamstatic.com.evil.example/a.jpg",    # suffix trick
                "https://notsteamstatic.com/a.jpg",
                "https://user@evil.example/a.jpg",               # userinfo trick
                "ftp://avatars.steamstatic.com/a.jpg",
                "", None, 123):
        assert not avatars.is_allowed(bad), bad
    # the same URL always lands on the same cache file, and different URLs do not collide
    a = avatars.cache_path("https://avatars.steamstatic.com/a.jpg")
    b = avatars.cache_path("https://avatars.steamstatic.com/b.jpg")
    assert a == avatars.cache_path("https://avatars.steamstatic.com/a.jpg")
    assert a != b and str(a).endswith(".img")


class _FakeLiveClient:
    """Stands in for hub/live.py so the session can be driven with no network."""

    def __init__(self, join=(200, {"position": 1, "size": 1}), history=(200, {"matches": []})):
        self.calls = []
        self._join = join
        self._history = history
        self.lobby = None

    def start(self): self.calls.append("start")
    def stop(self): self.calls.append("stop")
    def join_queue(self): self.calls.append("join"); return self._join
    def leave_queue(self): self.calls.append("leave_queue"); return (200, {})
    def accept(self): self.calls.append("accept"); return (200, {"accepted": 1, "total": 10})
    def leave_match(self): self.calls.append("leave_match"); return (200, {})

    def start_connect(self, map_name, host_id, teams=None, sides=None, bans=None):
        # teams/sides/bans ride along so the match can be archived with a real shape; they are
        # recorded here so a test can check the hub sends what the history record needs.
        self.calls.append("start_connect:%s:%s" % (map_name, host_id))
        self.lobby = {"teams": teams, "sides": sides, "bans": bans}
        return (200, {"ok": True})

    def report_connected(self): self.calls.append("connected"); return (200, {})

    def history(self, match_id=None):
        self.calls.append("history:%s" % (match_id or ""))
        return self._history

    # Party POSTs. The reply is whatever the test set on self.party_reply; the roster arrives
    # over the stream as a party_update, so the reply carries no members - only a status.
    party_reply = (200, {"ok": True})

    def create_party(self):
        self.calls.append("create_party"); return self.party_reply

    def join_party(self, code):
        self.calls.append("join_party:%s" % code); return self.party_reply

    def leave_party(self):
        self.calls.append("leave_party"); return self.party_reply

    def refresh_party_code(self):
        self.calls.append("refresh_party_code"); return self.party_reply
    # Party invites. Like every other party POST the reply carries only a status: the inbox and
    # the roster both arrive over the stream.
    invite_reply = (200, {"ok": True})

    def invite_to_party(self, target):
        self.calls.append("invite:%s" % target); return self.invite_reply

    def accept_party_invite(self, from_id):
        self.calls.append("invite_accept:%s" % from_id); return self.invite_reply

    def decline_party_invite(self, from_id):
        self.calls.append("invite_decline:%s" % from_id); return self.invite_reply



def _live_session(join=(200, {"position": 1, "size": 1})):
    from hub import competitive as C
    panel = _StubPanel()
    s = C.LiveSession(panel)
    # POSTs run on a worker thread in real life; run them inline so the tests are
    # deterministic (the point being tested is the decision, not the threading)
    s._action = lambda call, on_result=None: (lambda r: on_result(*r) if on_result else None)(call())
    # These lifecycle tests simulate a successfully installed match handoff.
    s._prepare_host_pak = lambda: True
    s._prepare_joiner_pak = lambda: True
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6, "elo": 1180,
            "bdr": None, "matches": 0, "wins": 0}
    s.token = "tok"
    s.client = _FakeLiveClient(join)      # set directly: _connect() skips when one exists
    s.phase = "idle"
    return s, panel


def test_live_session_events():
    """The server drives the session; the session must never invent a match of its own."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()

    s.on_live_event({"type": "hello", "steam_id": "76561198000999000"})
    assert s.connected is True
    s.on_live_event({"type": "stats", "online": 7, "queued": 3})
    assert s.online == 7 and s.queue_size == 3

    # Find match asks the real queue immediately; no simulated integrity timers.
    s.find_match()
    assert "join" in s.client.calls
    assert s.phase == "queued" and s.queue_position == 1
    assert s.online == 0 or isinstance(s.online, int)

    # waiting must never conjure a match: only the server can
    for _ in range(10):
        s._tick_queue()
    assert s.phase == "queued", "the live session must not fake a match like the preview does"

    s.on_live_event({"type": "queued", "position": 2, "size": 5})
    assert s.queue_position == 2 and s.queue_size == 5

    players = [{"steam_id": "7656119800000%04d" % i, "persona": "P%d" % i} for i in range(10)]
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20, "players": players})
    assert s.phase == "found" and len(s.players) == 10
    assert s.players[0]["name"] == "P0", "a real persona is used, not a made-up name"
    assert s.accept_left == 20 and s.accepted == 0

    s.accept()
    assert "accept" in s.client.calls
    assert s.accepted == 0, "the count comes from the server, not from pressing the button"
    s.on_live_event({"type": "match_accept", "accepted": 4, "total": 10})
    assert s.accepted == 4

    # the countdown reaching zero must NOT cancel anything client-side
    s.accept_left = 1
    s._tick_accept()
    assert s.accept_left == 0 and s.phase == "found", "only the server ends the accept window"

    # The lobby lives on the SERVER now: match_ready carries the split teams, the captains and
    # the ONE designated coin captain (team 1's), and the client takes them rather than shuffling
    # its own (which is how ten clients used to disagree about who was on which team).
    ids = [p["steam_id"] for p in players]
    s.on_live_event({"type": "match_ready", "match_id": "m1", "players": players,
                     "teams": {"1": ids[:5], "2": ids[5:]},
                     "captains": {"1": ids[0], "2": ids[5]},
                     "coin_captain": ids[0], "stage": "coin",
                     "pool": ["Airsoft", "BombHouse", "Hospital", "Pool", "Rome", "Russian"]})
    assert s.phase == "lobby" and len(s.teams[1]) == C.TEAM_SIZE
    assert s.stage == "coin" and s.coin_captain == ids[0]

    # a cancellation puts us back on the idle screen with a reason
    s.phase = "found"
    s.on_live_event({"type": "match_cancelled", "match_id": "m1", "reason": "declined"})
    assert s.phase == "idle" and s.error

    # losing the stream while queued must not leave the tab claiming to be queued
    s.phase = "queued"
    s._on_live_status(False, "boom")
    assert s.phase == "idle" and not s.connected and "connection" in s.error.lower()

    # signing out stops the stream and lets go of the client
    s.connected = True
    client = s.client
    s.sign_out()
    assert "stop" in client.calls, client.calls
    assert s.client is None
    assert s.phase == "signed_out"


def test_live_accept_other_player_does_not_block_me():
    """Someone else accepting first must NOT stop me from POSTing my own acceptance. The count
    (self.accepted) is the server's global tally; whether I have accepted is self.i_accepted."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session()
    players = [{"steam_id": "7656119800000%04d" % i, "persona": "P%d" % i} for i in range(2)]
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20, "players": players})
    assert s.phase == "found" and s.i_accepted is False

    # another player accepts first: the tally climbs, but I still have not accepted
    s.on_live_event({"type": "match_accept", "accepted": 1, "total": 2})
    assert s.accepted == 1 and s.i_accepted is False

    s.client.calls.clear()
    s.accept()
    assert "accept" in s.client.calls, "another player's acceptance must not block mine"
    assert s.i_accepted is True


def test_live_accept_is_idempotent():
    """A double-click (or a second accept after the first) must not POST twice."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session()
    players = [{"steam_id": "7656119800000%04d" % i, "persona": "P%d" % i} for i in range(2)]
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20, "players": players})
    s.accept()
    assert s.i_accepted is True
    s.client.calls.clear()
    s.accept()
    assert "accept" not in s.client.calls, "accepting again must not POST a second time"


def test_match_found_resets_i_accepted():
    """A fresh match must start with i_accepted cleared, or a returning player is stuck showing
    'waiting for others' and can never accept the new match."""
    s, _panel = _live_session()
    s.phase = "found"
    s.i_accepted = True
    players = [{"steam_id": "7656119800000%04d" % i, "persona": "P%d" % i} for i in range(2)]
    s.on_live_event({"type": "match_found", "match_id": "m2", "accept_seconds": 20, "players": players})
    assert s.i_accepted is False


def test_live_session_retries_a_transient_queue_failure():
    """A redeploy makes Railway's edge answer 404 for a moment. That must not end the attempt
    (2026-09-14: "Could not join the queue: Not found." appeared exactly during a deploy)."""
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session(join=(404, {"error": "Not found."}))
    s.find_match()
    for _ in range(3):
        s._check_next()
    # still trying, not given up, and definitely not claiming to be queued
    assert s.phase == "checking", s.phase
    assert not s.error, s.error
    assert panel.pending, "a retry must be scheduled"

    # once the service comes back, the retry succeeds
    s.client._join = (200, {"position": 1, "size": 1})
    while panel.pending and s.phase == "checking":
        panel.pump()
    assert s.phase == "queued", s.phase
    assert s.client.calls.count("join") >= 2, s.client.calls


def test_live_session_requeues_after_a_reconnect():
    """If the service restarts while we are queued it has forgotten us; sitting there watching
    a timer climb is the worst outcome. A reconnect re-joins."""
    s, _panel = _live_session()
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "queued"
    s.client.calls.clear()
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    assert "join" in s.client.calls, "a reconnect while queued must ask to be requeued"

    # but a reconnect when we are NOT queued must not queue us
    s.phase = "idle"
    s.client.calls.clear()
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    assert "join" not in s.client.calls


def test_live_session_reports_a_queue_failure():
    """If the service refuses or is unreachable, say so instead of pretending to queue."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session(join=(401, {"error": "offline"}))   # 401 is not worth retrying
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "idle", s.phase
    assert "offline" in s.error, s.error


def test_live_queue_tick_starts_when_queued_event_precedes_join_result():
    """Bug 1 + 5: the server writes the `queued` event to the already-open stream BEFORE the
    join POST answers, so the event usually lands first. Entering the queue is what must start
    the countdown; the late 200 must then only confirm position/size, never reset the clock or
    start a SECOND loop (which would double-count the seconds)."""
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()
    s.phase = "checking"                      # mid integrity-check, about to ask the queue
    s.queue_seconds = 0

    # the event arrives first, while we still believe we are only "checking"
    s.on_live_event({"type": "queued", "position": 1, "size": 1})
    assert s.phase == "queued", "the queued event must move us into the queue"
    assert s._queue_ticking, "entering the queue must arm the countdown (bug 1)"
    assert len(panel.pending) == 1, "exactly one tick is scheduled"

    # now the POST's 200 lands, late
    s._join_result(200, {"position": 1, "size": 1}, 0)
    assert s.phase == "queued"
    assert s.queue_seconds == 0, "the late 200 must not reset the queue clock"
    assert len(panel.pending) == 1, "the late 200 must not start a second loop (bug 5)"

    # the clock advances, and there is only ever one loop feeding it
    panel.pump()
    assert s.queue_seconds == 1
    assert len(panel.pending) == 1, "still one loop"
    panel.pump()
    assert s.queue_seconds == 2
    assert len(panel.pending) == 1


def test_live_queue_position_update_does_not_reset_or_double_the_tick():
    """Bug 1 + 5: a `queued` event that is only a position/size update (we are already queued)
    must leave the clock alone and must not start a second countdown."""
    s, panel = _live_session()
    s.phase = "checking"
    s.on_live_event({"type": "queued", "position": 3, "size": 8})
    assert s.phase == "queued" and s._queue_ticking
    panel.pump(); panel.pump()
    assert s.queue_seconds == 2
    loops = len(panel.pending)

    s.on_live_event({"type": "queued", "position": 2, "size": 9})   # a position update
    assert s.queue_position == 2 and s.queue_size == 9
    assert s.queue_seconds == 2, "a position update must not reset the queue clock"
    assert len(panel.pending) == loops, "a position update must not start a second loop"
    panel.pump()
    assert s.queue_seconds == 3 and len(panel.pending) == 1


def test_live_join_retry_treats_a_queued_event_as_success():
    """Bug 4: a `queued` event landing mid-check is SUCCESS. A retry scheduled by an earlier
    retryable failure must not fire an error or knock us back out; it just makes sure the tick
    is running."""
    s, panel = _live_session(join=(404, {"error": "Not found."}))
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "checking" and panel.pending, "a retry is pending after the 404"

    # the stream now delivers `queued` before the retry gets its turn
    s.on_live_event({"type": "queued", "position": 1, "size": 2})
    assert s.phase == "queued" and s._queue_ticking

    # drive everything that is pending: the stale retry must not undo the queue
    for _ in range(len(panel.pending) + 4):
        if not panel.pending:
            break
        panel.pump()
        if s.phase != "queued":
            break
    assert s.phase == "queued", "a stale join retry must not knock a queued player out"
    assert not s.error, s.error


def test_live_reconnect_during_found_recovers_the_accept_screen():
    """Bug 3: a transient stream drop during the accept window must NOT abandon the match. The
    player stays on the accept screen; on reconnect the server replays match_found with who has
    accepted, and the client restores its OWN accepted state from that rather than resetting."""
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()
    players = [{"steam_id": s.me["steam_id"], "persona": "Sam"},
               {"steam_id": "76561198000000002", "persona": "P2"}]
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20,
                     "players": players})
    assert s.phase == "found"
    s.accept()
    assert s.i_accepted is True
    pending_after_accept = len(panel.pending)

    # the stream drops mid accept window
    s._on_live_status(False, "boom")
    assert s.phase == "found", "a transient drop during accept must not drop us to idle (bug 3)"
    assert not s.connected

    # ...and it comes back: hello, then the server's replayed match state
    s.on_live_event({"type": "hello", "steam_id": s.me["steam_id"]})
    assert s.connected is True
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 12,
                     "players": players, "accepted": 1, "total": 2,
                     "accepted_ids": [s.me["steam_id"]]})
    assert s.phase == "found", "the accept screen is restored, not abandoned to idle"
    assert s.i_accepted is True, "the player's own accepted state is restored from the server"
    assert s.accepted == 1 and s.accept_left == 12
    assert len(panel.pending) == pending_after_accept, \
        "the replayed match_found must not start a second accept loop (bug 3 + double-tick)"


def test_render_signature_ignores_live_counters():
    """Bug 2: the render signature is what decides whether the whole panel is torn down and
    rebuilt. The online count, the queue size and the per-second timers must NOT be part of it,
    or every `stats` broadcast and every tick would rebuild the panel and it would flicker. A
    real change of layout (the phase) MUST change it."""
    import types
    from hub import competitive as C
    s, _panel = _live_session()
    panel = C.CompetitivePanel.__new__(C.CompetitivePanel)   # no Tk, no _build
    panel.session = s
    panel.view = "play"
    panel.party_code_hidden = False
    panel.app = types.SimpleNamespace(
        state={"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0"}}}, busy=False)

    s.phase = "queued"
    s.queue_seconds = 5
    s.online = 3
    s.queue_size = 4
    base = panel._render_signature()

    # a stats broadcast and a queue tick: none of these may rebuild the panel
    s.online = 42
    s.queue_size = 9
    s.stats_queued = 9
    s.stats_ready = True
    s.queue_seconds = 6
    assert panel._render_signature() == base, "stats and the queue timer must not rebuild (flicker)"

    # a real change of screen must
    s.phase = "found"
    assert panel._render_signature() != base, "a phase change must rebuild the panel"


def test_avatar_download_forces_a_full_rebuild():
    """Fix 1: a downloaded Steam avatar is cached on the PANEL (_avatar_images), not in session
    state, so the render signature does not move. The avatar-completion callback must force a
    full rebuild anyway (via _force_redraw), or the fetched avatar never replaces the initials on
    the idle/queued screen, where nothing else forces a rebuild. This test FAILS if done() calls
    on_change() directly (rebuild=False, only _refresh_live runs, the Canvas is never recreated).
    Observed headlessly by counting _draw_body without real Tk widgets."""
    import types
    from hub import competitive as C
    s, _panel = _live_session()
    panel = C.CompetitivePanel.__new__(C.CompetitivePanel)   # no Tk, no _build
    panel.session = s
    panel.view = "play"
    panel.party_code_hidden = False
    panel.app = types.SimpleNamespace(
        state={"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0"}}}, busy=False)
    panel._live_labels = []
    panel._avatar_images = {}
    panel._avatar_tried = set()
    panel._avatar_failed = set()
    panel.frame = types.SimpleNamespace(winfo_exists=lambda: True)
    panel.post = lambda fn: fn()             # done() marshals to the main thread; run it inline

    counts = {"body": 0, "live": 0}
    panel._phase_changed = lambda: None
    panel._subtabs_available = lambda: True
    panel._history_locked = lambda: False
    panel._draw_header = lambda: None
    panel._draw_subtabs = lambda: None
    panel._draw_body = lambda: counts.__setitem__("body", counts["body"] + 1)
    panel._refresh_live = lambda: counts.__setitem__("live", counts["live"] + 1)

    # settle the cached signature so a plain on_change refreshes in place and does NOT rebuild
    panel._render_sig = panel._render_signature()
    panel.on_change()
    assert counts["body"] == 0 and counts["live"] >= 1, "an unchanged signature must not rebuild"

    # an avatar download completes: fetch returns an image (anything non-None). The panel cache
    # changes but the session does not, so ONLY _force_redraw makes on_change rebuild the body.
    real_fetch = C.avatars_mod.fetch
    C.avatars_mod.fetch = lambda url: object()
    try:
        panel._fetch_avatar("https://avatars.steamstatic.com/deadbeef_full.jpg")
    finally:
        C.avatars_mod.fetch = real_fetch
    assert counts["body"] == 1, "a downloaded avatar must force a full body rebuild (Fix 1)"


def test_reconnect_in_lobby_does_not_restart_the_client_local_lobby():
    """Bug 3: the coin flip and veto lobby is client-local. A match_ready (or match_found)
    REPLAYED on reconnect while we are already in the lobby must be ignored — restarting it would
    throw away the coin flip and the vetoes the player already made (guards at competitive.py
    ~1285 for match_ready and ~1261 for match_found)."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _live_session()
    players = [{"steam_id": s.me["steam_id"], "persona": "Sam"},
               {"steam_id": "76561198000000002", "persona": "P2"}]
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20,
                     "players": players})
    s.on_live_event({"type": "match_ready", "match_id": "m1"})
    assert s.phase == "lobby" and s.stage == "coin"

    # progress the client-local lobby past the coin flip and into the veto
    s.stage = "veto"
    s.coin_result = "heads"
    s.toss_winner = 1
    s.first_ban = 1

    # the stream drops and comes back: the server replays the match events it already sent
    s.on_live_event({"type": "match_ready", "match_id": "m1"})
    assert s.phase == "lobby" and s.stage == "veto", \
        "a replayed match_ready must not restart the lobby (bug 3)"
    assert s.coin_result == "heads" and s.toss_winner == 1 and s.first_ban == 1, \
        "the coin flip result must survive a reconnect"

    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20,
                     "players": players})
    assert s.phase == "lobby" and s.stage == "veto", \
        "a replayed match_found must not drag us back to the accept screen (bug 3)"


def test_accept_tick_clears_exactly_once_at_zero_and_can_be_re_armed():
    """The accept countdown stops itself at zero WITHOUT a phase change (only the server ends the
    window). It must clear _accept_ticking exactly once, schedule nothing further (no loop leak),
    and leave the flag clear so a later match can re-arm the loop."""
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()
    s.phase = "found"
    s.accept_left = 2
    s._accept_ticking = False

    s._start_accept_tick()
    assert s._accept_ticking is True
    assert panel.pump(1) == 1 and s.accept_left == 1 and s._accept_ticking is True

    before = len(panel.pending)
    assert panel.pump(1) == 1, "the second tick runs"
    assert s.accept_left == 0 and s.phase == "found", "only the server ends the accept window"
    assert s._accept_ticking is False, "the loop clears itself once at zero"
    assert len(panel.pending) == before - 1, "no further tick is scheduled at zero (no loop leak)"

    # the flag being clear (not stuck True) is exactly what lets a new match re-arm the loop
    s.accept_left = 20
    s._start_accept_tick()
    assert s._accept_ticking is True, "a cleared flag lets the accept loop re-arm"
    assert len(panel.pending) == before, "re-arming schedules exactly one fresh tick"


def test_competitive_connect_window():
    """After the veto everyone has three minutes to actually be in the game (Sam, 2026-09-14).

    The preview drives the whole thing locally, which is what makes the shape testable: the
    window opens, people arrive, and the match only starts once the last one is in."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    panel = _StubPanel()
    s = C.MockSession(panel)
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    s.players = s._fake_players()
    s.accept_total = len(s.players)
    s._start_lobby()
    s.map = "Rome"
    s.phase = "lobby"

    s._begin_connect()
    assert s.phase == "connecting", "the veto ends in the connect window, not straight in-game"
    assert s.connect_left == C.CONNECT_SECONDS
    assert s.connect_total == C.LOBBY_SIZE
    assert s.connected_ids == set() and not s.i_connected
    assert s.host and s.host.get("ping") == min(p["ping"] for p in s.players), "best ping hosts"

    s.report_connected()
    assert s.i_connected and s.me["steam_id"] in s.connected_ids
    assert s.phase == "connecting", "one player being in is not a match"

    # everyone else wanders in; the match starts on the last one, not before
    for _ in range(C.LOBBY_SIZE * 4):
        if s.phase == "live":
            break
        s._others_connect()
    assert s.phase == "live", "the match starts once everybody is in"
    assert len(s.connected_ids) == C.LOBBY_SIZE


def test_competitive_no_show_penalty():
    """Sam, 2026-09-14: "nobody except the person who didnt connect loses anything. the person
    who didnt connect will lose a medium size of elo and get a 5 [minute] queue ban"."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    panel = _StubPanel()
    s = C.MockSession(panel)
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    s.players = s._fake_players()
    s._start_lobby()
    s.map = "Pool"
    s.phase = "lobby"
    s._begin_connect()

    # I never press the button, and the window closes
    # In the real preview the mock fakes the host reporting in 2.5 s later; these tests
    # drive the clock by hand, so say it happened. Without it there is no offence to take:
    # a joiner whose host never came up is never fined (_connect_expired).
    s.host_ready = True
    s.connect_left = 1
    s._tick_connect()
    assert s.phase == "idle", "a closed window cancels the match"
    assert str(C.NO_SHOW_ELO) in s.error and C.format_duration(C.NO_SHOW_BAN_SECONDS) in s.error
    left = s.banned_left()
    assert 0 < left <= C.NO_SHOW_BAN_SECONDS
    assert s.penalty_reason == "no_show"

    # ...and the ban actually stops the queue, rather than only being written on a screen
    s.error = ""
    s.find_match()
    assert s.phase == "idle" and s.error, "a banned player cannot queue"

    # the same window, but this time I turned up: the match still dies and I pay nothing
    s.penalty_until = 0.0
    s.penalty_reason = ""
    s.players = s._fake_players()
    s._start_lobby()
    s.phase = "lobby"
    s._begin_connect()
    s.report_connected()
    # In the real preview the mock fakes the host reporting in 2.5 s later; these tests
    # drive the clock by hand, so say it happened. Without it there is no offence to take:
    # a joiner whose host never came up is never fined (_connect_expired).
    s.host_ready = True
    s.connect_left = 1
    s._tick_connect()
    assert s.phase == "idle"
    assert s.banned_left() == 0, "turning up costs nothing, whatever anybody else did"
    assert str(C.NO_SHOW_ELO) not in s.error


def test_competitive_no_show_ladder():
    """A flat 5 minutes is farmable, so the ban climbs on repeats and decays with clean time
    (Sam, 2026-09-14), up to a one-hour cap (Sam, 2026-09-16). The FIRST offence stays exactly
    what he asked for."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")

    assert C.no_show_ban_seconds(1) == C.NO_SHOW_BAN_SECONDS == 300, "Sam's five minutes"
    rungs = [C.no_show_ban_seconds(n) for n in range(1, 9)]
    assert rungs[:4] == [300, 900, 1800, 3600], rungs
    assert rungs[4] == rungs[7] == 3600, "it stops at an hour; abandoning owns the long end"
    assert rungs == sorted(rungs), "a rung must never be softer than the one before it"

    # a length quoted in a sentence gets units; a live countdown stays a clock
    assert C.format_duration(300) == "5 min" and C.format_duration(3600) == "1 h"
    assert C.format_clock(59) == "0:59" and C.format_clock(3600) == "1:00:00"
    # the C6-C9 abandon ladder still reaches hours, and 240:00 is not something to show a player
    assert C.format_duration(14400) == "4 h" and ":" not in C.format_duration(14400)

    # the preview escalates the same way, so the tab behaves the same with no backend
    panel = _StubPanel()
    s = C.MockSession(panel)
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    seen = []
    for _ in range(3):
        s.players = s._fake_players()
        s._start_lobby()
        s.phase = "lobby"
        s._begin_connect()
        # In the real preview the mock fakes the host reporting in 2.5 s later; these tests
        # drive the clock by hand, so say it happened. Without it there is no offence to take:
        # a joiner whose host never came up is never fined (_connect_expired).
        s.host_ready = True
        s.connect_left = 1
        s._tick_connect()                  # never press the button
        seen.append(s.banned_left())
        s.penalty_until = 0.0              # serve it instantly so the next one can be taken
    assert seen[0] <= 300 < seen[1] <= 900 < seen[2] <= 1800, seen
    assert s.penalty_count == 3


def test_competitive_match_found_cue():
    """The hub makes a noise when a match is found. It must fire on ENTERING the phase, not on
    every redraw, and it must be silenceable.

    ONE ring, not three (Sam, 2026-09-15: "reduce the match found sound to its base level instead
    of 3x"). It was three - "3 instances of the sound", 2026-09-14 - which is three rings over
    900 ms on the one screen a player is already being hurried through."""
    from hub import sounds
    import wave as wave_mod

    assert sounds.BEEPS == 1, "one ring"
    path = os.path.join(tmpdir("hub-cue-"), "cue.wav")
    sounds.write_wav(path)
    with wave_mod.open(path) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        ms = w.getnframes() / w.getframerate() * 1000
        assert 50 <= ms <= 400, ms
        # a cue that is all silence is a cue nobody hears
        frames = w.readframes(w.getnframes())
        assert max(abs(int.from_bytes(frames[i:i+2], "little", signed=True))
                   for i in range(0, len(frames), 2)) > 5000

    # the slider is baked into the samples, because winsound has no volume control
    quiet = os.path.join(tmpdir("hub-cue-"), "quiet.wav")
    sounds.write_wav(quiet, 25)
    def peak(path):
        with wave_mod.open(path) as w:
            f = w.readframes(w.getnframes())
            return max(abs(int.from_bytes(f[i:i+2], "little", signed=True))
                       for i in range(0, len(f), 2))
    assert peak(quiet) < peak(path) * 0.5, "25% must be audibly quieter than full"
    silent = os.path.join(tmpdir("hub-cue-"), "silent.wav")
    sounds.write_wav(silent, 0)
    assert peak(silent) == 0, "zero on the slider is silence, not a quiet noise"
    assert sounds.cue_path(25) != sounds.cue_path(100), "each level needs its own cached file"
    assert [sounds.clamp_volume(v) for v in (-5, 0, 100, 300, "x", None)] == \
           [0, 0, 100, 100, sounds.DEFAULT_VOLUME, sounds.DEFAULT_VOLUME]

    # off Windows every entry point is a no-op rather than an error
    if sys.platform != "win32":
        assert sounds.play_once() is False
        assert sounds.play_match_found(lambda ms, fn: None) is False
    assert sounds.play_once(0) is False, "muted never reaches the audio device at all"

    # the EDGE rule, without a UI: _phase_changed must fire once per entry into "found"
    from hub import competitive as C

    class FakePanel(C.CompetitivePanel):
        def __init__(self):                      # deliberately no Tk
            self.app = type("A", (), {"state": {}})()
            self.session = type("S", (), {"phase": "idle"})()
            self._last_phase_seen = None
            self.plays = 0
            # A found match also drags a player out of the Match History sub-tab. _set_view
            # commits the flag only once the layout actually moved, so the fake frames have to
            # accept the pack calls rather than be None.
            class _Frame:
                def pack(self, **kw): pass
                def pack_forget(self): pass
            self.view = "history"
            self.body = _Frame()
            self.history_frame = _Frame()
        def after(self, ms, fn):
            pass
        def on_change(self):
            pass

    panel = FakePanel()
    fired = []
    real = C.sounds_mod.play_match_found
    # the real signature, so a mismatch fails here instead of being swallowed into silence
    C.sounds_mod.play_match_found = lambda schedule, volume=100: fired.append(volume)
    try:
        panel.session.phase = "found"
        panel._phase_changed()
        assert panel.view == "play", \
            "a found match must pull the player off Match History and onto the accept window"
        panel._phase_changed()               # a redraw while still in "found"
        panel._phase_changed()
        assert len(fired) == 1, "the cue must follow the transition, not the redraw"
        panel.session.phase = "lobby"
        panel._phase_changed()
        panel.session.phase = "found"        # a second match later on
        panel._phase_changed()
        assert len(fired) == 2

        assert fired[-1] == sounds.DEFAULT_VOLUME, "an untouched slider plays at the default"

        # the slider reaches the cue
        panel.set_sound_volume(35, save=False)
        panel.session.phase = "idle"; panel._phase_changed()
        panel.session.phase = "found"; panel._phase_changed()
        assert fired[-1] == 35

        # ...and silence means silence
        panel.set_sound_volume(0, save=False)
        panel.session.phase = "idle"; panel._phase_changed()
        panel.session.phase = "found"; panel._phase_changed()
        assert len(fired) == 3, "a muted hub must stay muted"

        # the bell puts the slider back where it was rather than to some default
        panel.on_change = lambda: None
        panel.toggle_sound()
        assert panel.sound_volume() == 35, "unmuting restores the level the player chose"

        # a hub that predates the slider carried a plain on/off flag
        panel.app.state = {"comp_sound": False}
        assert panel.sound_volume() == 0, "an old mute must survive the upgrade"
        panel.app.state = {}
        assert panel.sound_volume() == sounds.DEFAULT_VOLUME
    finally:
        C.sounds_mod.play_match_found = real


def test_competitive_phase_watchdog():
    """No screen may hang forever (Sam, 2026-09-14). Every phase a player cannot leave by
    themselves has a deadline, and reaching one never blames them."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")

    # every locked phase MUST have a limit, or the sign-out lock would be a trap
    for phase in C.LOCKED_PHASES:
        keys = [k for k in C.PHASE_LIMITS if k == phase or (isinstance(k, tuple) and k[0] == phase)]
        assert keys, "%s can be entered but never left" % phase
    for stage in ("coin", "flipping", "choice", "veto", "ready"):
        assert ("lobby", stage) in C.PHASE_LIMITS, stage
    # ...and no limit may be shorter than the thing it guards
    assert C.PHASE_LIMITS["found"] > C.ACCEPT_SECONDS
    assert C.PHASE_LIMITS["connecting"] > C.CONNECT_SECONDS

    panel = _StubPanel()
    s = C.MockSession(panel)
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    s.players = s._fake_players()
    s._start_lobby()
    s.stage = "coin"
    assert s._phase_key() == ("lobby", "coin"), "the lobby is watched per stage, not as a whole"

    s.on_stalled(("lobby", "coin"))
    assert s.phase == "idle", "a stalled lobby hands the window back"
    assert s.error == i18n.t("comp_match_stalled")
    assert s.banned_left() == 0, "a stall is our bug; it must never cost the player anything"

    # the tick itself: the key holding still past its limit is what fires it
    s2 = C.MockSession(panel)
    s2._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    s2._start_watchdog()
    panel.pending.clear()
    s2.phase = "checking"
    s2._watch_tick()                       # first sight of the key: just records it
    assert s2.phase == "checking"
    s2._watch_since -= C.PHASE_LIMITS["checking"] + 5
    s2._watch_tick()
    assert s2.phase == "idle" and s2.error, "an overdue phase is released"

    # a phase with no limit is left alone however long it sits there
    s2.phase = "queued"
    s2._watch_tick()
    s2._watch_since -= 100000
    s2._watch_tick()
    assert s2.phase == "queued", "queueing is allowed to take as long as it takes"


def test_competitive_signout_is_locked_during_a_match():
    """Sam, 2026-09-14: the sign-out button goes away once a match is found, so nobody can
    use it to cancel one during the pre-round stages."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    panel = _StubPanel()
    s = C.MockSession(panel)
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    assert not s.locked_in(), "idle is not locked"

    for phase in ("found", "lobby", "connecting", "live"):
        s.phase = phase
        assert s.locked_in(), phase
        s.error = ""
        s.sign_out()
        assert s.phase == phase, "signing out of %s must do nothing" % phase
        assert s.me is not None and s.token == "t"
        assert s.error == i18n.t("comp_signout_locked")

    # queueing is NOT a match yet, so leaving is still the player's business
    s.phase = "queued"
    assert not s.locked_in()
    s.phase = "result"
    assert not s.locked_in(), "the match is over; let them out"
    s.sign_out()
    assert s.phase == "signed_out" and s.me is None


def test_live_session_connect_window():
    """The live session draws the connect window; the SERVER owns the clock and the penalty."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()

    players = [{"steam_id": "7656119800000%04d" % i, "persona": "P%d" % i} for i in range(10)]
    s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 20, "players": players})
    s.on_live_event({"type": "match_ready", "match_id": "m1"})
    assert s.phase == "lobby"
    s.map = "Rome"
    s._begin_connect()
    assert any(c.startswith("start_connect:Rome:") for c in s.client.calls)
    assert s.phase == "lobby", "nothing moves until the server opens the window"

    s.on_live_event({"type": "match_connecting", "match_id": "m1", "connect_seconds": 180,
                     "map": "Rome", "host": players[3]["steam_id"], "connected": [], "total": 10})
    assert s.phase == "connecting" and s.connect_left == 180 and s.connect_total == 10
    assert s.host["steam_id"] == players[3]["steam_id"]

    s.report_connected()
    assert "connected" in s.client.calls
    s.on_live_event({"type": "match_connect", "match_id": "m1",
                     "connected": [p["steam_id"] for p in players[:4]], "total": 10})
    assert len(s.connected_ids) == 4

    # the countdown running out must NOT decide anything: that is the server's call
    # In the real preview the mock fakes the host reporting in 2.5 s later; these tests
    # drive the clock by hand, so say it happened. Without it there is no offence to take:
    # a joiner whose host never came up is never fined (_connect_expired).
    s.host_ready = True
    s.connect_left = 1
    s._tick_connect()
    assert s.connect_left == 0 and s.phase == "connecting"
    s.on_live_event({"type": "match_live", "match_id": "m1", "map": "Rome"})
    assert s.phase == "live"


def test_live_session_no_show_penalty():
    """A cancelled match says WHY, and only the player who caused it is told it cost them."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()

    # somebody ELSE failed to load in: we lose nothing and go straight back to queueing
    s.phase = "connecting"
    s.on_live_event({"type": "match_cancelled", "match_id": "m1", "reason": "no_show",
                     "requeued": True, "blamed": False, "penalty": None})
    assert s.phase == "queued", "the server requeued us, so the tab must agree"
    assert s.error == i18n.t("comp_no_show_others")
    assert s.banned_left() == 0

    # WE failed to load in: a ban and an Elo loss, and no requeue
    s.phase = "connecting"
    s.on_live_event({"type": "match_cancelled", "match_id": "m2", "reason": "no_show",
                     "requeued": False, "blamed": True,
                     "penalty": {"reason": "no_show", "seconds": 300, "elo": 25}})
    assert s.phase == "idle"
    assert "25" in s.error and C.format_duration(300) in s.error
    assert 0 < s.banned_left() <= 300 and s.penalty_reason == "no_show"

    # the server refuses the next queue join, and the tab explains rather than saying "failed"
    s.penalty_until = 0.0
    s.client = _FakeLiveClient((403, {"ok": False, "banned": True, "reason": "no_show",
                                      "seconds": 240, "error": "You are banned from the queue."}))
    s.phase = "idle"
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "idle"
    assert "4:00" in s.error, s.error
    assert 0 < s.banned_left() <= 240

    # SOMEBODY ELSE'S BAN IS NOT OURS. The service refuses the join for the whole party when any
    # member is banned and marks it with `who`; without reading that, an innocent member's hub
    # started its own countdown and the idle screen told them they had never loaded into a game.
    s.penalty_until = 0.0
    s.penalty_reason = ""
    s.party = {"code": "ABCD", "leader_id": s.me["steam_id"],
               "members": [dict(s.me), {"steam_id": "76561198000000002", "name": "Wario"}]}
    s.client = _FakeLiveClient((403, {"ok": False, "banned": True, "reason": "no_show",
                                      "seconds": 240, "who": "76561198000000002",
                                      "count": 3, "next_seconds": 1800,
                                      "error": "Somebody in your party is banned from the queue."}))
    s.phase = "idle"
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "idle"
    assert s.banned_left() == 0, "an innocent member is not serving a ban"
    assert s.penalty_reason == "", "and carries no record of one"
    assert "Wario" in s.error and "4:00" in s.error, s.error
    s.party = None

    # a stalled lobby is nobody's fault and costs nothing
    s.penalty_until = 0.0
    s.phase = "lobby"
    s.on_live_event({"type": "match_cancelled", "match_id": "m3", "reason": "stalled",
                     "requeued": True, "blamed": False, "penalty": None})
    assert s.phase == "queued" and s.error == i18n.t("comp_match_stalled")
    assert s.banned_left() == 0


def test_live_session_watchdog_tells_the_server():
    """Giving up on a phase must not just close the screen: the service is still holding a
    match, and a player who vanished from it is nine other people's problem."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()

    s.phase = "connecting"
    s.on_stalled("connecting")
    assert "leave_match" in s.client.calls
    assert s.phase == "idle" and s.error == i18n.t("comp_match_stalled")
    assert s.banned_left() == 0, "the watchdog never punishes anybody"

    s.client.calls.clear()
    s.phase = "queued"
    s.on_stalled("queued")
    assert "leave_queue" in s.client.calls


def test_live_session_party_is_server_driven():
    """Creating a party must NOT fabricate a roster locally (that was the mock's bug). Nothing
    happens to self.party until the server's party_update arrives; then the real personas show."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()

    s.create_party()
    assert "create_party" in s.client.calls
    assert s.party is None, "the live session must wait for the server, not invent a party"

    # the server answers over the stream with the two real personas
    s.on_live_event({"type": "party_update", "code": "ABCD-EF",
                     "leader_id": "76561198000999000",
                     "members": [{"steam_id": "76561198000999000", "persona": "Sam",
                                  "level": None, "ping": None},
                                 {"steam_id": "76561198000000102", "persona": "Wario",
                                  "level": None, "ping": None}]})
    assert s.party is not None and s.party["code"] == "ABCD-EF"
    assert s.party_size() == 2
    names = [m["name"] for m in s.party["members"]]
    assert "Sam" in names and "Wario" in names
    assert not any(n.startswith("Player ") for n in names), "no fabricated placeholder names"
    # honest: level/ping stay unknown, never invented
    assert all(m["level"] is None and m["ping"] is None for m in s.party["members"])

    # a code:null party_update means "you are solo now"
    s.on_live_event({"type": "party_update", "code": None})
    assert s.party is None


def test_live_session_no_mock_friend_in_live():
    """The mock schedules _mock_friend_joins 2.6 s after create_party (the 'random level 10
    player appears' bug). The live session must NEVER do that: no timer, no self-growing party,
    no invented level."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()

    before = len(panel.pending)
    s.create_party()
    # no _later was scheduled by the live create_party (the mock schedules exactly one)
    assert len(panel.pending) == before, "live create_party must not schedule _mock_friend_joins"

    # and even if a stray callback fired, it is a no-op that cannot inject a member
    s.on_live_event({"type": "party_update", "code": "ABCD-EF",
                     "leader_id": "76561198000999000",
                     "members": [{"steam_id": "76561198000999000", "persona": "Sam",
                                  "level": None, "ping": None}]})
    assert s.party_size() == 1
    s._mock_friend_joins()
    panel.pump(limit=10)
    assert s.party_size() == 1, "_mock_friend_joins must not grow a live party"
    assert s.party["members"][0]["level"] is None, "no invented level in live mode"


def test_live_session_party_error():
    """A join that the server refuses maps to the right party-card message; a locally-malformed
    code fails instantly with no round trip."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")

    # a malformed code never reaches the client
    s, panel = _live_session()
    s.join_party("nope")
    assert "join_party" not in "".join(s.client.calls)
    assert s.party_error == i18n.t("comp_party_bad_code")

    # 404 -> bad code
    s, panel = _live_session()
    s.client.party_reply = (404, {"ok": False})
    s.join_party("ABCD-EF")
    assert s.party_error == i18n.t("comp_party_bad_code")

    # 409 -> full
    s, panel = _live_session()
    s.client.party_reply = (409, {"ok": False})
    s.join_party("ABCD-EF")
    assert s.party_error == i18n.t("comp_party_is_full")

    # anything else -> the generic "could not reach the party service"
    s, panel = _live_session()
    s.client.party_reply = (0, {"error": "offline"})
    s.join_party("ABCD-EF")
    assert s.party_error == i18n.t("comp_party_failed")


def test_party_membership_is_in_render_signature():
    """Guards the flicker/rebuild contract: a party_update that changes membership MUST move the
    render signature, or a new member would never appear (competitive.py _render_signature)."""
    import types
    from hub import competitive as C
    s, _panel = _live_session()
    panel = C.CompetitivePanel.__new__(C.CompetitivePanel)   # no Tk, no _build
    panel.session = s
    panel.view = "play"
    panel.party_code_hidden = False
    panel.app = types.SimpleNamespace(
        state={"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0"}}}, busy=False)

    s.on_live_event({"type": "party_update", "code": "ABCD-EF",
                     "leader_id": "76561198000999000",
                     "members": [{"steam_id": "76561198000999000", "persona": "Sam",
                                  "level": None, "ping": None}]})
    one = panel._render_signature()

    s.on_live_event({"type": "party_update", "code": "ABCD-EF",
                     "leader_id": "76561198000999000",
                     "members": [{"steam_id": "76561198000999000", "persona": "Sam",
                                  "level": None, "ping": None},
                                 {"steam_id": "76561198000000102", "persona": "Wario",
                                  "level": None, "ping": None}]})
    two = panel._render_signature()
    assert one != two, "a party membership change must rebuild the panel"


# ====================================================================== the web UI (pywebview)
# These cover the valuable, headless half of the redesign (docs/ui-redesign-plan.md, "Testing"):
# the state snapshot the webview renders from, the js_api verb -> session-verb mapping, and the
# Python -> JS push. The webview window itself needs a real run on the box; none of this does, and
# none of it imports pywebview (hub.webui.shell is the only importer, and only run() imports it).
import types as _types


class _FakeApp:
    """The minimal `app` a WebPanel reads: persisted state (dict) and the catalogue."""

    def __init__(self, installed=None, catalogue=None):
        self.state = {"installed": installed or {}, "auth": None}
        self.catalogue = catalogue


class _CallRecorder:
    """Stands in for a session: records which verb the bridge called, with what args."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*args, **kw):
            self.calls.append((name, args, kw))
        return rec


def _web_panel():
    """A headless WebPanel driving a LiveSession with the fake live client (no Tk, no pywebview,
    no window). Mirrors _live_session so the same events can be replayed."""
    from hub import competitive as C
    from hub.webui.panel import WebPanel
    from hub.webui.scheduler import InlineScheduler
    panel = WebPanel(_FakeApp(installed={C.COMPETITIVE_MODE_ID: {"version": "1.0"}}),
                     scheduler=InlineScheduler(), window=None)
    s = panel.session
    s._action = lambda call, on_result=None: (lambda r: on_result(*r) if on_result else None)(call())
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6, "elo": 1180,
            "bdr": None, "matches": 34, "wins": 19}
    s.token = "tok"
    s.client = _FakeLiveClient()
    s.phase = "idle"
    return panel, s


def test_webui_snapshot_serializes_live_session():
    """state_snapshot turns a live match_found + party_update into the exact JSON the screen
    renders, and it is JSON-serialisable (the same shape as test_live_session_* but on the dict)."""
    import json
    from hub import i18n
    from hub.webui.snapshot import state_snapshot
    i18n.set_language("en")
    panel, s = _web_panel()

    # signed in and idle: the hero shows the persona/level/tier, Find match is enabled
    snap = state_snapshot(s, panel)
    json.dumps(snap)                                    # must be serialisable, never raises
    assert snap["view"] == "competitive" and snap["lang"] == "en"
    assert snap["auth"]["signed_in"] and snap["auth"]["persona"] == "Sam"
    assert snap["auth"]["level"] == 6 and snap["auth"]["tier"]      # a named tier, display-only
    assert snap["comp"]["phase"] == "idle" and snap["comp"]["can_find"] is True
    assert snap["status"]["gamemode_installed"] is True
    assert snap["party"]["in_party"] is False and snap["party"]["size"] == 1
    # the strings the screen references are shipped in the snapshot (i18n flows through it)
    assert snap["strings"]["comp_find_match"] == i18n.STRINGS["en"]["comp_find_match"]

    # the server finds a match: the snapshot reflects the accept window
    s.on_live_event({"type": "match_found", "players": [
        {"steam_id": "76561198000999000", "persona": "Sam"},
        {"steam_id": "76561198000000102", "persona": "Wario"}],
        "total": 10, "accept_seconds": 20})
    snap = state_snapshot(s, panel)
    assert snap["comp"]["phase"] == "found"
    assert snap["comp"]["found"]["total"] == 10
    assert snap["comp"]["found"]["accept_left"] == 20

    # a party_update fills the party card with the REAL personas (server-authoritative)
    s.on_live_event({"type": "party_update", "code": "ABCD-EF",
                     "leader_id": "76561198000999000",
                     "members": [{"steam_id": "76561198000999000", "persona": "Sam"},
                                 {"steam_id": "76561198000000102", "persona": "Wario"}]})
    snap = state_snapshot(s, panel)
    json.dumps(snap)
    assert snap["party"]["in_party"] and snap["party"]["code"] == "ABCD-EF"
    assert snap["party"]["size"] == 2 and snap["party"]["max"] == 5
    names = [m["name"] for m in snap["party"]["members"]]
    assert "Sam" in names and "Wario" in names
    assert snap["party"]["members"][0]["is_leader"] is True
    # hiding the code (streamer toggle) is reflected, and the masked code has no real characters
    panel.party_code_hidden = True
    snap = state_snapshot(s, panel)
    assert snap["party"]["hidden"] is True
    assert snap["party"]["code"] == "ABCD-EF" and set(snap["party"]["code_masked"]) <= {"•", "-"}


def test_webui_snapshot_is_the_on_change_output():
    """WebPanel.on_change builds the snapshot and stores it as last_state (what the bridge serves
    and the push sends), so driving the session updates last_state with no window."""
    from hub import i18n
    i18n.set_language("en")
    panel, s = _web_panel()
    panel.on_change()
    assert panel.last_state and panel.last_state["auth"]["persona"] == "Sam"
    s.on_live_event({"type": "stats", "online": 42, "queued": 3})   # ends with _changed()
    assert panel.last_state["status"]["online"] == 42


def test_webui_bridge_verbs_map_to_session():
    """Each js_api verb is a 1:1 passthrough to the matching session verb, run on the UI thread."""
    from hub import competitive as C
    from hub.webui.bridge import Api
    from hub.webui.panel import WebPanel
    from hub.webui.scheduler import InlineScheduler
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    rec = _CallRecorder()
    panel.session = rec                                 # record what the bridge calls
    api = Api(panel)

    api.find_match();          assert rec.calls[-1][0] == "find_match"
    api.cancel_search();       assert rec.calls[-1][0] == "cancel_queue"    # the verb it maps to
    api.accept();              assert rec.calls[-1][0] == "accept"
    api.sign_in();             assert rec.calls[-1][0] == "sign_in"
    api.sign_out();            assert rec.calls[-1][0] == "sign_out"
    api.create_party();        assert rec.calls[-1][0] == "create_party"
    api.leave_party();         assert rec.calls[-1][0] == "leave_party"
    api.refresh_party_code();  assert rec.calls[-1][0] == "refresh_party_code"
    api.join_party("abcd-ef")
    assert rec.calls[-1] == ("join_party", ("abcd-ef",), {})

    # toggle_hide_code is a view-only toggle on the panel, not a session verb
    assert panel.party_code_hidden is False
    api.toggle_hide_code()
    assert panel.party_code_hidden is True


def test_webui_bridge_pushes_state_to_the_window():
    """on_change serialises the snapshot and calls window.__hub.onState via evaluate_js, and
    set_language re-emits with the new strings (the Python -> JS half of the bridge)."""
    import json
    from hub import i18n
    i18n.set_language("en")

    class _FakeWindow:
        def __init__(self): self.scripts = []
        def evaluate_js(self, s): self.scripts.append(s)

    panel, s = _web_panel()
    win = _FakeWindow()
    panel.window = win
    panel.on_change()
    # The push is ASYNCHRONOUS now: evaluate_js blocks its caller with no timeout, so it may not
    # run on the UI thread (hub/webui/panel.py, the push queue). Drain before asserting.
    panel.flush_pushes()
    assert win.scripts and "window.__hub" in win.scripts[-1] and "onState(" in win.scripts[-1]
    payload = win.scripts[-1].split("onState(", 1)[1].rsplit(")", 1)[0]
    snap = json.loads(payload)                          # what JS actually receives is valid JSON
    assert snap["lang"] == "en"

    panel.set_language("de")
    panel.flush_pushes()
    snap = json.loads(win.scripts[-1].split("onState(", 1)[1].rsplit(")", 1)[0])
    assert snap["lang"] == "de"
    assert snap["strings"]["comp_find_match"] == i18n.STRINGS["de"]["comp_find_match"]
    i18n.set_language("en")


def test_webui_js_constant_tables_are_declared_before_they_are_read():
    """A `var NAME = {...}` lookup table must not be READ above the line that assigns it.

    THIS SHIPPED, AND IT BLACK-SCREENED THE HUB (2.0.13, found 2026-09-16). CLOSE_LINES lived
    inside competitive.js's render(), below the code that read it. `var` hoists the declaration but
    not the assignment, so during that same call to render() - hundreds of lines before the
    assignment - the name was still undefined, and indexing undefined throws whatever the key is:

        TypeError: Cannot read properties of undefined (reading 'armed')
            at resultAction (screens/competitive.js:534:32)

    The exception escaped renderScreen, so the whole screen painted nothing. Every render of the
    post-match result did it; pressing "end match" gave a black window and no error a player could
    see. `node --check` passes such a file happily - it is valid JavaScript that is simply wrong.

    So the rule is positional and cheap: find every table constant, and fail if any use of its name
    appears on an earlier line than its declaration. Module scope satisfies this for free, which is
    where a constant table belongs anyway.
    """
    import re
    from hub import paths
    js_files = sorted(paths.webui_dir().glob("**/*.js"))
    assert js_files, "no JS files found under the webui static dir"
    bad = []
    for path in js_files:
        lines = path.read_text(encoding="utf-8").splitlines()
        # SCREAMING_CASE only: the convention for these tables, and it keeps the scan off ordinary
        # locals, whose one-pass ordering JavaScript guarantees anyway.
        decls = {}
        for i, line in enumerate(lines):
            m = re.match(r"\s*var\s+([A-Z][A-Z0-9_]{2,})\s*=", line)
            if m and m.group(1) not in decls:
                decls[m.group(1)] = i
        for name, decl_line in decls.items():
            use = re.compile(r"\b" + re.escape(name) + r"\s*\[")
            for i, line in enumerate(lines):
                if i < decl_line and use.search(line):
                    bad.append("%s:%d reads %s, declared at line %d"
                               % (path.name, i + 1, name, decl_line + 1))
    assert not bad, ("a constant table is read before it is assigned; `var` hoists the name but "
                     "not the value, so this throws at runtime:\n  " + "\n  ".join(bad))


def test_webui_a_modal_is_cleared_by_the_next_render():
    """Every overlay a screen appends to document.body must be removed by the next render.

    SAM, 2026-09-16: "in the match detail screen when you click on a match in match history there
    is no way to exit the match detail screen." The dismissals all worked - backdrop click and
    Escape both called close_match, the session cleared match_detail_id, and the next snapshot came
    back with open_id "". What did not happen was the overlay leaving the DOM. A screen appends its
    modal to document.body (inside #app it would be clipped by the scroll region it lives in), and
    core.render() only ever cleared #app - so the closed modal stayed exactly where it was, on top
    of the nav, and every re-render stacked another copy on it. Measured in a browser against the
    real files: seventeen overlays, still covering the screen after the state said closed.

    All four modals - the match detail, the rank card, the report box and the gamemodes ruleset -
    are appended the same way, so this is asserted of the core, not of one screen: render() must
    remove the body's overlays, and it must do it BEFORE the screen is drawn, or it would remove
    the one the screen has just appended.
    """
    from hub import paths
    core = (paths.webui_dir() / "core.js").read_text(encoding="utf-8")
    lines = core.splitlines()

    def line_of(needle, start=0):
        for i in range(start, len(lines)):
            if needle in lines[i]:
                return i
        return -1

    render_at = line_of("function render() {")
    assert render_at >= 0, "core.js has no render()"
    draw_at = line_of("renderScreen();", render_at)
    clear_at = line_of("clearOverlays();", render_at)
    assert clear_at >= 0, "render() must clear the previous render's body overlays"
    assert clear_at < draw_at, "the overlays must be cleared BEFORE the screen appends its own"

    body = core[core.index("function clearOverlays()"):]
    assert ".ui-overlay" in body[:400], "clearOverlays must target the overlay class ui.modal makes"
    assert "removeChild" in body[:400] or "remove()" in body[:400], "...and actually remove them"

    # ...and the screens really do append to the body, which is what makes the above load-bearing.
    appenders = [f.name for f in sorted((paths.webui_dir() / "screens").glob("*.js"))
                 if "document.body.appendChild" in f.read_text(encoding="utf-8")]
    assert appenders, "no screen appends to the body any more - is this test still describing the UI?"


def test_webui_the_report_box_opens_on_whatever_screen_you_pressed_report_on():
    """The report picker follows the STATE that opened it, not the tab that happened to be drawn.

    SAM, 2026-09-16: "whenever i hit the report button on a player in my match history it doesnt
    open until i go to the competitive tab. the screen that should appear once a user presses
    report should appear anywhere."

    The button and the wire were both fine: history.js called open_report, the session set
    report_target, and the very next snapshot carried it on every view. What was missing was
    anyone to DRAW it - the box was appended by competitive.js's render(), which the router only
    runs while competitive is the active view. So the accusation sat in the state, invisible,
    until the player wandered onto the one screen that knew how to turn it into a modal, and then
    it appeared a tab late.

    The overlay registry the post-match card introduced is exactly the fix - a modal raised over
    whatever the player is looking at - and the report box simply had not been moved onto it.
    This asserts both halves: the state reaches every view, and the box is an overlay rather than
    something one screen's render appends.
    """
    from hub import i18n, paths
    from hub.webui.bridge import Api
    from hub.webui.snapshot import state_snapshot
    i18n.set_language("en")
    panel, s = _web_panel()
    api = Api(panel)

    # ...standing on the history tab, which is where the press came from.
    api.set_view("history")
    api.open_report("76561198000000102", "match-77", "Wario")
    snap = state_snapshot(s, panel)
    assert snap["view"] == "history", "the press must not move the player off the tab they are on"
    assert snap["comp"]["report_target"] == "76561198000000102"
    assert snap["comp"]["report_match"] == "match-77"
    # WHO, IN WORDS. The live roster cannot name a player from a match that finished weeks ago, so
    # the opener passes the name it already has on screen or the box accuses a 17-digit number.
    assert snap["comp"]["report_name"] == "Wario"
    assert snap["comp"]["report_reasons"], "the reason list must travel with it"

    api.close_report()
    assert state_snapshot(s, panel)["comp"]["report_target"] == ""

    # ...and it is registered as an overlay, not appended by the competitive screen's render.
    comp_js = (paths.webui_dir() / "screens" / "competitive.js").read_text(encoding="utf-8")
    assert 'registerOverlay("report"' in comp_js, "the report box is not registered as an overlay"
    render_body = comp_js[comp_js.index("function render(root, state, ctx)"):]
    render_body = render_body[:render_body.index("registerOverlay(")]
    assert "reportModal(" not in render_body, \
        "competitive.js's render() appends the report box again - it is only drawn on that tab"


def test_webui_every_modal_has_a_visible_way_out():
    """A modal that covers the window must show how to leave it.

    The backdrop click and Escape are both invisible: nothing on screen says either exists, which
    is why the match-detail report above was "there is no way to exit" rather than "Escape does not
    work". ui.modal draws a close control whenever it is given an onClose, so every modal in the hub
    gets one from the one place that builds them.
    """
    from hub import paths
    ui_js = (paths.webui_dir() / "ui.js").read_text(encoding="utf-8")
    ui_css = (paths.webui_dir() / "ui.css").read_text(encoding="utf-8")
    modal = ui_js[ui_js.index("function modal(opts)"):]
    modal = modal[:modal.index("function appendChildren")]
    assert "ui-modal-x" in modal, "ui.modal must build a visible close control"
    assert "aria-label" in modal, "...labelled, because its text is a glyph"
    assert "addEventListener(\"click\", close)" in modal, "...and it must actually close the modal"
    assert ".ui-modal-x" in ui_css, "the close control needs a rule, or it lands wherever flex puts it"

    # Every caller that can be closed gets it for free; none may hand-roll its own.
    for js in sorted((paths.webui_dir() / "screens").glob("*.js")):
        text = js.read_text(encoding="utf-8")
        assert "ui-modal-x" not in text, f"{js.name} builds its own close button instead of ui.modal's"


def test_webui_js_references_only_real_i18n_keys():
    """Every t("...") key the front end references exists in English (the analogue of the panel's
    string-completeness test, for the JS view). Guards a typo'd key rendering as the raw key."""
    import re
    from hub import i18n
    from hub import paths
    # The front end is modular now: scan every JS file (ui.js, core.js, screens/*.js), not one app.js.
    js_files = sorted(paths.webui_dir().glob("**/*.js"))
    assert js_files, "no JS files found under the webui static dir"
    js = "\n".join(f.read_text(encoding="utf-8") for f in js_files)
    keys = sorted(set(re.findall(r'\bt\(\s*["\']([^"\']+)["\']', js)))
    assert keys, "no t() keys found — the scan regex is wrong"
    missing = [k for k in keys if k not in i18n.STRINGS["en"]]
    assert not missing, "front-end JS references i18n keys that do not exist: %s" % missing


def test_webui_scales_itself_to_the_window_and_never_sizes_in_viewport_units():
    """The window-scaling contract: the page is zoomed to fit the window, and nothing inside it
    measures itself in vh/vw.

    The hub draws its screens in real px at a fixed comfortable size and zooms the whole page
    (`body { zoom: var(--ui-scale) }`) when the window is smaller than that, so a short window
    shows the same layout smaller instead of cutting the bottom off it. That only holds if
    nothing sizes itself in VIEWPORT units: vh/vw are raw viewport pixels and the zoom does not
    move them, so a single `height: 100vh` brings back exactly the clipping this replaced - and
    it looks perfect at the default window size, which is why it needs a test and not an eye."""
    import re
    from hub import paths
    static = paths.webui_dir()

    ui_css = (static / "ui.css").read_text(encoding="utf-8")
    assert "zoom: var(--ui-scale" in ui_css, "ui.css no longer zooms the page to fit the window"
    assert "--ui-scale: 1;" in (static / "tokens.css").read_text(encoding="utf-8"), \
        "tokens.css must keep the fallback scale the page renders with before it is measured"

    core_js = (static / "core.js").read_text(encoding="utf-8")
    assert "--ui-scale" in core_js and 'addEventListener("resize"' in core_js, \
        "core.js no longer measures the window and writes the scale"

    # No vh/vw anywhere in the app's own CSS or in a style the JS writes. Comments are stripped
    # first: those files explain this rule in prose and must be allowed to name the unit.
    offenders = []
    for f in sorted(static.glob("**/*.css")) + sorted(static.glob("**/*.js")):
        text = re.sub(r"/\*.*?\*/", "", f.read_text(encoding="utf-8"), flags=re.S)
        text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
        for m in re.finditer(r"\b\d+(?:\.\d+)?(?:vh|vw|vmin|vmax)\b", text):
            offenders.append("%s: %s" % (f.name, m.group(0)))
    assert not offenders, "viewport units break under the page zoom: %s" % offenders


def test_webui_shell_import_does_not_require_pywebview():
    """Importing the package and the shell module must not import pywebview (only run() does), so
    the Tk path and this suite never need it installed."""
    import importlib
    import sys
    sys.modules.pop("webview", None)
    importlib.import_module("hub.webui")
    importlib.import_module("hub.webui.shell")
    assert "webview" not in sys.modules, "hub.webui must not import pywebview at module load"


def test_webui_x_hides_to_the_tray():
    """The web UI's X hides the window to the tray; only "Close Lights Out" quits.

    The same contract test_tray_icon_and_close_to_tray pins for the Tk UI. The web UI shipped
    WITHOUT it from 2.0.0 — hub/tray.py was only ever wired into hub/app.py — so for every user on
    the default UI the X killed the hub outright, losing a live match and leaving the way back a
    shortcut (the instance pile-up hub/singleton.py exists for). Driven through fakes rather than
    a real window: pywebview is not installed for this suite, and this is all of the behaviour."""
    from hub import i18n
    from hub import tray as tray_mod
    from hub.webui import shell as shell_mod

    class FakeEvent:
        """pywebview's `closing` event (webview/event.py, should_lock=True): every handler runs
        inline on the GUI thread and the close is cancelled if ANY of them returned False."""
        def __init__(self):
            self.items = []

        def __iadd__(self, fn):
            self.items.append(fn)
            return self

        def fire(self):
            return False in [fn() for fn in self.items]      # True == the close was cancelled

    class FakeWindow:
        def __init__(self, hide_raises=False):
            self.events = type("Events", (), {})()
            self.events.closing = FakeEvent()
            self.visible = True
            self.destroyed = False
            self.hide_raises = hide_raises

        def hide(self):
            if self.hide_raises:
                raise RuntimeError("no handle")
            self.visible = False

        def show(self):
            self.visible = True

        def destroy(self):
            # pywebview's destroy() is Form.Close(), which raises `closing` again — so the quit
            # path has to be let through by the handler that cancels every other close.
            if not self.events.closing.fire():
                self.destroyed = True

    class FakeTray:
        made = []

        def __init__(self, title, open_label, quit_label, on_open, on_quit):
            self.args = (title, open_label, quit_label)
            self.on_open, self.on_quit = on_open, on_quit
            self.started = False
            self.notes = []
            self.stopped = False
            FakeTray.made.append(self)

        def start(self):
            self.started = True

        def notify(self, text, title=None):
            self.notes.append(text)

        def stop(self):
            self.stopped = True

    real_tray, real_avail = tray_mod.Tray, shell_mod.CloseToTray.tray_available
    real_lang = i18n.get_language()
    i18n.set_language("de")                  # the menu must be translated, like the Tk path's
    try:
        tray_mod.Tray = FakeTray
        shell_mod.CloseToTray.tray_available = staticmethod(lambda: True)

        win = FakeWindow()
        closer = shell_mod.CloseToTray(win)
        assert closer.start() is True
        tray = FakeTray.made[-1]
        assert tray.started and tray.args == ("Lights Out", "Öffnen", "Lights Out schließen"), tray.args

        # X -> hidden, still running, one balloon saying where it went
        assert win.events.closing.fire() is True, "the X must not close the window"
        assert not win.visible and not win.destroyed
        assert tray.notes == [i18n.t("tray_hint")], tray.notes

        # tray click -> back on screen; X again -> hidden again, and no second balloon
        tray.on_open()
        assert win.visible
        assert win.events.closing.fire() is True
        assert not win.visible and tray.notes == [i18n.t("tray_hint")], tray.notes

        # "Close Lights Out" -> really gone, and the icon leaves the tray with it
        tray.on_quit()
        assert win.destroyed, "the tray menu is the only quit the web UI has"
        assert tray.stopped and closer.tray is None

        # FAILS OPEN 1: a window that will not hide closes instead of leaving a dead X.
        stuck = FakeWindow(hide_raises=True)
        shell_mod.CloseToTray(stuck).start()
        assert stuck.events.closing.fire() is False, "a hide that raises must let the close through"

        # FAILS OPEN 2: no pystray / not Windows -> no icon, and the X closes as it always did.
        shell_mod.CloseToTray.tray_available = staticmethod(lambda: False)
        plain_win = FakeWindow()
        plain = shell_mod.CloseToTray(plain_win)
        assert plain.start() is False and plain.tray is None
        assert plain_win.events.closing.fire() is False, "without a tray the X must close the hub"
        plain.stop()                                       # and unwinding run() without one is safe
    finally:
        tray_mod.Tray = real_tray
        shell_mod.CloseToTray.tray_available = real_avail
        i18n.set_language(real_lang)


def test_webui_snapshot_carries_the_signing_in_state():
    """During sign-in signed_in is False but phase is "signing_in"; the snapshot must carry that
    phase AND the link_code so JS can render the waiting/code panel. (app.js buildCompetitive was
    fixed to route BOTH signed_out and signing_in to heroSignedOut(), which branches on phase.)"""
    from hub import i18n
    from hub.webui.snapshot import state_snapshot
    i18n.set_language("en")
    panel, s = _web_panel()
    # enter the sign-in wait: no account yet, but a browser hand-off is in flight
    s.me = None
    s.phase = "signing_in"
    s.link_url = "https://steamcommunity.com/openid/login?stub"
    s.link_code = "WXYZ"

    snap = state_snapshot(s, panel)
    assert snap["auth"]["signed_in"] is False
    assert snap["auth"]["phase"] == "signing_in"
    assert snap["auth"]["link_code"] == "WXYZ"        # the code the waiting panel shows
    assert snap["auth"]["link_url"]                   # "open again" needs the URL
    # the strings the waiting panel references are shipped so JS can look them up
    for key in ("comp_signin_waiting", "comp_signin_open_again"):
        assert key in snap["strings"]


def test_game_running_survives_tasklists_truncated_image_name():
    """The game was ALWAYS reported closed, and this is the byte that did it.

    tasklist's default table format pads and truncates Image Name at 25 characters.
    "Bodycam-Win64-Shipping.exe" is 26, so the row really reads "Bodycam-Win64-Shipping.ex" and
    the old `GAME_EXE.lower() in out.lower()` was False with the game plainly running. Every rule
    built on that answer was dead: Find match queued with Bodycam open, ops.apply installed over a
    pak the game held, settings' readiness row never lit.

    So the test feeds the REAL truncated table row as well as the CSV one. The truncated table is
    the trap: a reader that still parses it fails here."""
    import subprocess as _sp
    from hub import game as game_mod, version

    assert len(version.GAME_EXE) > 25, (
        "GAME_EXE is short enough for tasklist's table again - this test no longer proves anything")

    table = "%s   166044 Console                    1      5,736 K\n" % version.GAME_EXE[:25]
    csv = '"%s","166044","Console","1","5,736 K"\n' % version.GAME_EXE
    assert version.GAME_EXE.lower() not in table.lower(), "the truncated row is the whole point"

    seen = []

    class _Out:
        def __init__(self, text):
            self.stdout = text

    def _fake_run(args, **kw):
        seen.append(list(args))
        return _Out(csv if "CSV" in args else table)

    real_win, real_run = game_mod._WIN, _sp.run
    game_mod._WIN = True                      # the tests also run off Windows, where there is no game
    _sp.run = _fake_run
    try:
        assert game_mod.game_running() is True, (
            "the game is in the task list and the hub says it is closed")
        assert any("CSV" in a for a in seen), (
            "game_running read the truncating table format again")
        # and it still says False when the game really is absent
        seen.clear()
        csv = ""
        table = ""
        assert game_mod.game_running() is False
    finally:
        game_mod._WIN, _sp.run = real_win, real_run


def test_game_running_cached_never_blocks_and_then_settles():
    """`tasklist` costs ~1 s on a busy machine; a screen must never pay that inline.

    The 2026-09-15 bug: the web UI snapshot called the exact game_running() THREE times - settings'
    game slice, settings' readiness row, gamemodes' ready guard - and the snapshot is rebuilt on the
    single UI thread for every state change. Measured 2906 ms per snapshot, so every click in the
    hub took three seconds. Worse the more processes are running, which is why one player saw it
    and another did not."""
    import time as _t
    import threading as _threading
    from hub import game as game_mod

    calls = []
    real = game_mod.game_running
    release = _threading.Event()
    completed = _threading.Event()
    caller = _threading.get_ident()

    def _slow():
        calls.append(_threading.get_ident())
        release.wait(10)
        completed.set()
        return True

    game_mod.game_running = _slow
    try:
        # reset the module cache so this test is not reading another test's answer
        game_mod._running_at = 0.0
        game_mod._running_value = False
        game_mod._running_refreshing = False

        first = game_mod.game_running_cached()
        # Prove the probe is still blocked after the caller returns. A strict
        # 50ms wall-clock budget mistakes a busy CI runner for an inline probe.
        assert not completed.is_set(), "game_running_cached waited for the probe"
        assert first is False, "with no answer yet it must not invent one"
        release.set()

        deadline = _t.monotonic() + 3.0
        while _t.monotonic() < deadline and not game_mod.game_running_cached():
            _t.sleep(0.01)
        assert game_mod.game_running_cached() is True, "the background probe never landed"
        assert calls and caller not in calls, "the probe ran on the calling thread"

        # ...and a second read inside the TTL must not spawn another tasklist
        before = len(calls)
        for _ in range(20):
            game_mod.game_running_cached()
        assert len(calls) == before, "the cache re-probed %d times" % (len(calls) - before)
    finally:
        release.set()
        game_mod.game_running = real
        game_mod._running_at = 0.0
        game_mod._running_value = False
        game_mod._running_refreshing = False


def test_snapshot_does_not_block_on_tasklist():
    """The snapshot must not WAIT on game_running, however slow it is.

    Not "must never call it": the cache refreshes in the background, and that refresh calling it is
    correct. What must never happen is the snapshot paying for it inline, which is what made every
    click cost three seconds (measured: state_snapshot 2906 ms, three tasklist spawns)."""
    import time as _t
    from hub import game as game_mod
    from hub.webui import snapshot as snap_mod

    real = game_mod.game_running
    game_mod.game_running = lambda: (_t.sleep(0.5), False)[1]
    try:
        game_mod._running_at = 0.0
        game_mod._running_value = False
        game_mod._running_refreshing = False
        panel, _s = _web_panel()
        started = _t.monotonic()
        snap_mod.state_snapshot(panel.session, panel)
        elapsed = _t.monotonic() - started
        assert elapsed < 0.15, "state_snapshot blocked %.0f ms on game_running" % (elapsed * 1000)
    finally:
        game_mod.game_running = real
        game_mod._running_at = 0.0
        game_mod._running_value = False
        game_mod._running_refreshing = False


def test_webui_push_never_blocks_the_ui_thread():
    """A webview that takes a second to answer must not cost the UI thread a second.

    This is the 2026-09-15 bug in one test: evaluate_js is synchronous and pywebview's
    edgechromium backend waits on a semaphore with NO timeout, so calling it from the single UI
    thread serialised every click, SSE event and timer behind the slowest JS round trip. Measured
    3.0-4.2 s per interaction on a machine where the same call took 0.4 ms in an empty window."""
    import time as _t
    from hub import i18n
    i18n.set_language("en")

    class _SlowWindow:
        def __init__(self): self.scripts = []
        def evaluate_js(self, s):
            _t.sleep(0.5)
            self.scripts.append(s)

    panel, _s = _web_panel()
    panel.window = _SlowWindow()
    started = _t.monotonic()
    panel.on_change()
    elapsed = _t.monotonic() - started
    assert elapsed < 0.1, "on_change blocked %.0f ms on a slow webview" % (elapsed * 1000)
    assert panel.flush_pushes(timeout=5.0)
    assert len(panel.window.scripts) == 1, "the push must still arrive, just not inline"


def test_webui_on_change_dedups_identical_pushes():
    """Two identical on_change() calls push evaluate_js only once (periodic stats/online ticks call
    on_change with an unchanged snapshot), yet ready()/last_state still return the current state."""
    from hub import i18n
    from hub.webui.bridge import Api
    i18n.set_language("en")

    class _FakeWindow:
        def __init__(self): self.scripts = []
        def evaluate_js(self, s): self.scripts.append(s)

    panel, s = _web_panel()
    panel.window = _FakeWindow()

    panel.on_change()
    panel.flush_pushes()
    assert len(panel.window.scripts) == 1              # first snapshot: pushed
    panel.on_change()
    panel.flush_pushes()
    assert len(panel.window.scripts) == 1              # identical: no second push

    # ready() (the JS state pull) still returns the current snapshot even though we skipped a push
    state = Api(panel).ready()
    assert state and state["auth"]["persona"] == "Sam"
    assert len(panel.window.scripts) == 1              # ready()'s re-emit is also de-duped

    # a real change (online count) pushes again
    s.on_live_event({"type": "stats", "online": 7, "queued": 1})
    assert panel.last_state["status"]["online"] == 7
    panel.flush_pushes()
    assert len(panel.window.scripts) == 2


def test_webui_refresh_tick_keeps_the_screens_live():
    """The panel re-derives the snapshot on a timer, so a change nothing announced still reaches
    the page — no verb, no session event, no switching tab and coming back.

    Most of the app state does call on_change; some of what a screen draws does not and cannot
    (what state.json says is installed, whether Bodycam is running, whether the catalogue has
    landed). Those used to sit wrong in `last_state` until something unrelated re-emitted."""
    from hub import i18n
    i18n.set_language("en")
    panel, _s = _web_panel()
    panel.start()                       # InlineScheduler: _start_on_ui_thread runs inline
    assert panel.last_state["status"]["gamemode_installed"] is True

    # something outside the session changes what a screen draws, and tells nobody
    panel.app.state["installed"] = {}
    assert panel.last_state["status"]["gamemode_installed"] is True, "nothing has re-emitted yet"

    fired = panel.scheduler.pump(limit=4)               # the watchdog tick and the refresh tick
    assert fired == 4
    assert panel.last_state["status"]["gamemode_installed"] is False
    assert panel.scheduler.pending, "the refresh tick must rebook itself, or it is a one-off"

    # ...and it stops with the panel, rather than ticking on against a dead session (the
    # session watchdog keeps its own timer going; only ours has to go)
    panel._closed = True
    panel.scheduler.pump(limit=4)
    assert panel._refresh_tick not in panel.scheduler.pending


def test_webui_scheduler_fires_in_due_order_and_stop_halts():
    """The UiScheduler fires each timer exactly once in due order (not insertion order), and stop()
    halts the loop so no further posted work runs."""
    import threading
    import time
    from hub.webui.scheduler import UiScheduler

    sched = UiScheduler()
    sched.start()
    lock = threading.Lock()
    fired = []
    done = threading.Event()

    def mk(tag):
        def f():
            with lock:
                fired.append(tag)
            if tag == "c":
                done.set()
        return f

    # queued out of order; must run a (20ms) -> b (40ms) -> c (60ms)
    sched.after(60, mk("c"))
    sched.after(20, mk("a"))
    sched.after(40, mk("b"))
    assert done.wait(2.0), "timers never fired"
    time.sleep(0.05)                                   # let any stray duplicate surface
    with lock:
        assert fired == ["a", "b", "c"], fired         # due order, each exactly once

    sched.stop()
    time.sleep(0.05)                                   # let the loop notice the stop flag and exit
    late = []
    sched.after(1, lambda: late.append("timer"))
    sched.post(lambda: late.append("post"))
    time.sleep(0.1)
    assert late == [], "scheduler ran work after stop()"


def test_competitive_start_lobby_survives_any_roster():
    """COMP_MATCH_SIZE is configurable so one person can test the queue. A one-player match
    once made _start_lobby raise IndexError on an empty team 2, the pump swallowed it, and the
    tab froze on "Match found" with nothing to go on (2026-09-14)."""
    from hub import competitive as C
    panel = _StubPanel()
    s = C.MockSession(panel)
    s._sign_in_done({"steam_id": "76561198000999000", "persona": "Sam", "token": "t"})
    for size in (1, 2, 3, 5, 9, 10):
        s.players = [{"name": "P%d" % i, "steam_id": "7656119800000%04d" % i, "level": 5, "ping": 30}
                     for i in range(size)]
        # I am somewhere in the roster, and NOT always first: the server promises no order
        s.players[size // 2] = dict(s.me, ping=30)
        s._start_lobby()
        assert s.phase == "lobby"
        assert len(s.teams[1]) + len(s.teams[2]) == size, size
        assert s.captains[1] and s.captains[2], size
        assert s.my_team() == 1, "the preview always puts me on team 1 so every control works"


def test_competitive_strings_exist_in_every_language():
    """Every comp_* / tab_* key the panel asks for is present in all seven languages."""
    from hub import i18n
    wanted = [
        "tab_gamemodes", "tab_competitive", "comp_gate_title", "comp_gate_install",
        "comp_signin_title", "comp_signin_button", "comp_signout", "comp_find_match",
        "comp_check_title", "comp_check_what_body", "comp_searching", "comp_found_title",
        "comp_accept", "comp_team", "comp_captain", "comp_coin_title", "comp_heads",
        "comp_tails", "comp_choose_side", "comp_choose_ban", "comp_veto_title",
        "comp_veto_done", "comp_chat_title", "comp_live_title", "comp_join",
        "comp_vote_title", "comp_result_win", "comp_result_loss", "comp_result_void",
        "comp_back", "comp_level", "comp_bdr", "comp_online", "comp_preview",
        "comp_party_title", "comp_party_create", "comp_party_join", "comp_party_code",
        "comp_party_leave", "comp_party_leader", "comp_party_share",
        "comp_party_bad_code", "comp_party_is_full", "comp_party_own_code",
        "comp_party_failed", "comp_party_waiting_leader", "comp_party_queue_note",
        "comp_party_hide", "comp_party_show", "comp_party_new_code",
        "comp_party_new_code_note", "comp_party_copy", "comp_party_copied",
        # the Match History sub-tab
        "comp_tab_play", "comp_tab_history", "comp_history_title", "comp_history_refresh",
        "comp_history_loading", "comp_history_empty", "comp_history_empty_hint",
        "comp_history_failed", "comp_history_signed_out", "comp_history_played",
        "comp_history_cancelled", "comp_history_no_show", "comp_history_declined",
        "comp_history_abandoned", "comp_history_pending", "comp_history_pending_note",
        "comp_history_elo", "comp_history_team", "comp_history_side_attack",
        "comp_history_side_defend", "comp_history_no_map", "comp_history_back_to_play",
        "comp_history_alert_queued", "comp_history_alert_lobby", "comp_history_alert_turn",
        "comp_history_alert_connect", "comp_history_alert_live", "comp_history_alert_result",
        "comp_history_detail_title", "comp_history_detail_veto", "comp_history_detail_host",
        "comp_history_detail_blamed", "comp_history_detail_missing",
        "comp_history_detail_gone", "comp_history_detail_source", "comp_history_count",
        "comp_history_alert_action", "comp_history_players",
        # the profile page, opened from the avatar
        "comp_profile_back", "comp_profile_steam", "comp_profile_id",
        "comp_profile_sample", "comp_profile_completed",
        "comp_profile_cancelled", "comp_profile_hosted", "comp_profile_at_fault",
        "comp_profile_record", "comp_profile_record_none", "comp_profile_winrate",
        "comp_profile_unscored", "comp_profile_form", "comp_profile_form_played",
        "comp_profile_form_fault", "comp_profile_conduct", "comp_profile_conduct_clean",
        "comp_profile_elo_lost", "comp_profile_banned", "comp_profile_next_ban",
        "comp_profile_maps", "comp_profile_sides", "comp_profile_empty",
        "comp_profile_empty_hint",
    ]
    for code in i18n.CODES:
        for key in wanted:
            assert key in i18n.STRINGS[code], (code, key)
            assert i18n.STRINGS[code][key].strip(), (code, key)
    # the arrows are the only rank feedback below level 11: no number anywhere
    assert "{delta}" not in i18n.STRINGS["en"]["comp_result_win"]
    # every placeholder the history screens interpolate must survive translation, or the
    # panel raises KeyError in whichever language got it wrong (and only in that language)
    for code in i18n.CODES:
        for key in ("comp_history_alert_queued", "comp_history_alert_connect",
                    "comp_profile_banned", "comp_profile_next_ban"):
            assert "{time}" in i18n.STRINGS[code][key], (code, key)
        for key in ("comp_history_team", "comp_history_elo", "comp_history_count",
                    "comp_profile_sample", "comp_profile_winrate", "comp_profile_unscored",
                    "comp_profile_elo_lost"):
            assert "{n}" in i18n.STRINGS[code][key], (code, key)
        assert "{id}" in i18n.STRINGS[code]["comp_profile_id"], code
        for key, text in i18n.STRINGS[code].items():
            assert "—" not in text, ("em dash in hub text", code, key)


def test_match_history_session():
    """The sub-tab's data: fetched once, cached while queued, re-asked when a match ends."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    rows = [{"id": "m1", "ended": 1789000000000, "map": "Rome", "outcome": "cancelled",
             "reason": "no_show", "blamed": True, "team": 2, "side": "defend",
             "host": False, "players": 10, "won": None, "score": None, "delta": None,
             "elo": -25, "connected": False, "accepted": True}]
    s, panel = _live_session()
    s.client._history = (200, {"matches": rows})

    assert s.history is None, "nothing is fetched until the tab is opened"
    s.load_history()
    assert s.history == rows and s.history_error == ""
    assert s.client.calls.count("history:") == 1

    # opening the tab again while queued must NOT hit the server every time
    s.load_history()
    s.load_history()
    assert s.client.calls.count("history:") == 1, "the cached list is reused"

    # ...but a finished match makes it stale, and the next look re-asks
    s.on_live_event({"type": "match_over", "match_id": "m1"})
    assert s.history_stale is True
    s.load_history()
    assert s.client.calls.count("history:") == 2
    assert s.history_stale is False

    # Refresh always re-asks
    s.load_history(force=True)
    assert s.client.calls.count("history:") == 3

    # a server that is down leaves the last good list on screen and says so
    s.client._history = (0, {"error": "offline"})
    s.load_history(force=True)
    assert s.history == rows, "a failed refresh must not blank the list"
    assert s.history_error == i18n.t("comp_history_failed")

    # an expired token says something a player can act on instead of "failed"
    s.client._history = (401, {})
    s.load_history(force=True)
    assert s.history_error == i18n.t("comp_history_signed_out")

    # one match in full, for the detail pop-up
    got = []
    s.client._history = (200, {"match": {"id": "m1", "players": []}})
    s.load_match("m1", got.append)
    assert got == [{"id": "m1", "players": []}]
    s.client._history = (404, {"ok": False})
    s.load_match("nope", got.append)
    assert got[-1] is None, "a match we were not in comes back as nothing, not a crash"


def test_match_history_rows_read_correctly():
    """Each row's headline is written from the player's own point of view."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    panel = C.CompetitivePanel.__new__(C.CompetitivePanel)

    def row(**kw):
        base = {"outcome": "played", "reason": "", "blamed": False, "won": None}
        base.update(kw)
        return base

    assert panel._history_outcome(row())[0] == i18n.t("comp_history_played")
    assert panel._history_outcome(row(won=True))[0] == i18n.t("comp_result_win")
    assert panel._history_outcome(row(won=False))[0] == i18n.t("comp_result_loss")
    # the offence is only named when it was OURS; otherwise the match was simply cancelled
    mine = row(outcome="cancelled", reason="no_show", blamed=True)
    theirs = row(outcome="cancelled", reason="no_show", blamed=False)
    assert panel._history_outcome(mine)[0] == i18n.t("comp_history_no_show")
    assert panel._history_outcome(mine)[1] == C.RED
    assert panel._history_outcome(theirs)[0] == i18n.t("comp_history_cancelled")
    assert panel._history_outcome(theirs)[1] != C.RED, "somebody else's no-show is not our red"
    for reason, key in (("declined", "comp_history_declined"),
                        ("abandoned", "comp_history_abandoned")):
        blamed = row(outcome="cancelled", reason=reason, blamed=True)
        assert panel._history_outcome(blamed)[0] == i18n.t(key)
    # a malformed timestamp must not blank a row
    assert panel._history_when({"ended": "nonsense"}) == "?"
    assert panel._history_when({"ended": 1789000000000}).startswith("20")


def test_a_replayed_match_does_not_reopen_the_game():
    """A reconnect must not open Bodycam a second time for a match it already opened.

    THE BUG THIS PINS (2026-09-16). Sam closed Bodycam and it reopened immediately, over and over,
    with nothing queued on the server. The hub's stream was measured flapping - on1 / on0 / on1 -
    and every drop long enough to exhaust the rejoin grace ran _rejoin_gave_up(), which calls
    reset_match(), which cleared `launched`. The service then replays match_connecting to any hub
    that reconnects mid-window - that is exactly what the payload is for - and the guard read a
    match it had already opened the game for as a brand new one.

    So the flag cannot be a bare boolean tied to the session's own memory of itself. It is keyed on
    the MATCH ID, which is unique, comes from the server, and survives reset_match deliberately.
    """
    from hub import game as game_mod

    HOST = "76561198000999000"        # _live_session's own steam_id
    roster = [{"name": "Sam", "steam_id": HOST, "level": 6, "ping": 20}]

    calls = []
    real_launch, real_running = game_mod.launch_game, game_mod.game_running
    game_mod.launch_game = lambda: (calls.append("launch"), True)[1]
    game_mod.game_running = lambda: False
    try:
        s, _ = _live_session()
        s.players = [dict(p) for p in roster]
        s._on_connecting({"match_id": "m1", "host": HOST, "players": roster,
                          "connect_seconds": 180})
        assert calls == ["launch"], calls

        # The stream drops and the rejoin grace runs out. This is the real path: it does NOT mean
        # the match is over, only that we lost the thread.
        s.reset_match()
        s.phase = "idle"

        # ...and the service hands the SAME match back on reconnect.
        s._on_connecting({"match_id": "m1", "host": HOST, "players": roster,
                          "connect_seconds": 120})
        assert calls == ["launch"], \
            "a replayed match_connecting reopened Bodycam: %r" % (calls,)

        # Twice more, because the stream flapped more than once.
        s.reset_match()
        s._on_connecting({"match_id": "m1", "host": HOST, "players": roster,
                          "connect_seconds": 60})
        s.reset_match()
        s._on_connecting({"match_id": "m1", "host": HOST, "players": roster,
                          "connect_seconds": 30})
        assert calls == ["launch"], "still only one launch for match m1: %r" % (calls,)

        # A genuinely NEW match must still open the game - the guard must not be a one-shot for
        # the life of the process.
        s.reset_match()
        s._on_connecting({"match_id": "m2", "host": HOST, "players": roster,
                          "connect_seconds": 180})
        assert calls == ["launch", "launch"], \
            "a different match must still open Bodycam: %r" % (calls,)

        # And a service that sends no match id at all falls back to the old behaviour rather than
        # refusing to launch: "" must never be treated as a match we have already handled.
        del calls[:]
        s4, _ = _live_session()
        s4.players = [dict(p) for p in roster]
        s4._on_connecting({"host": HOST, "players": roster, "connect_seconds": 180})
        assert calls == ["launch"], "no match id must still open the game once: %r" % (calls,)
    finally:
        game_mod.launch_game, game_mod.game_running = real_launch, real_running


def test_game_opens_host_first_then_everybody_else():
    """The pak gets ONE lobby search per launch (docs/autojoin.md, step 4 result): BeginPlay fires
    once per level load and the search goes out ~4 s later, with no clock in the lobby world to try
    again with. So a joiner whose game opens BEFORE the host has stamped CH_MATCH searches an empty
    Steam and has silently spent its only shot.

    The host therefore opens alone when the connect window does. A JOINER's game does not open by
    itself at all any more (Sam, 2026-09-16): host-ready only un-greys their "Launch game" button,
    and `launch_game` is what spends the search. So the ordering guarantee is now TWO facts - the
    joiner never launches before host-ready even if they mash the button, and never launches
    without being asked even after it."""
    from hub import competitive as C
    from hub import game as game_mod

    HOST = "76561198000999000"        # _live_session's own steam_id
    OTHER = "76561198000000042"
    roster = [{"name": "Sam", "steam_id": HOST, "level": 6, "ping": 20},
              {"name": "Pal", "steam_id": OTHER, "level": 5, "ping": 40}]

    calls = []
    real_launch, real_running = game_mod.launch_game, game_mod.game_running
    game_mod.launch_game = lambda: (calls.append("launch"), True)[1]
    game_mod.game_running = lambda: False
    try:
        # --- I AM the host: the game opens the moment the connect window does
        s, _ = _live_session()
        s.players = [dict(p) for p in roster]
        s._on_connecting({"host": HOST, "players": roster, "connect_seconds": 180})
        assert s.phase == "connecting", s.phase
        assert calls == ["launch"], calls
        assert s.game_opened_by_us == "host", s.game_opened_by_us

        # and never twice for the same match, however many events arrive
        s.on_live_event({"type": "match_connect", "connected": [HOST], "total": 2})
        assert calls == ["launch"], calls

        # --- I am NOT the host: nothing opens on its own, before OR after the host reports in.
        del calls[:]
        s2, _ = _live_session()
        s2.players = [dict(p) for p in roster]
        s2._on_connecting({"host": OTHER, "players": roster, "connect_seconds": 180})
        assert calls == [], "a joiner must not open before the host is in: %r" % (calls,)

        # The button is there but dead: pressing it before host-ready must not spend the search.
        s2.launch_game()
        assert calls == [], "the greyed Launch must be a hard guard, not just a style: %r" % (calls,)
        assert s2.host_ready is False

        # The host stamps its lobby. That OPENS THE GATE and nothing more - the joiner's game must
        # still be waiting for them to ask for it.
        s2.on_live_event({"type": "match_connect", "connected": [OTHER], "total": 2})
        assert s2.host_ready is True
        assert calls == [], "host-ready must not launch a joiner's game for them: %r" % (calls,)

        # ...and now the press does the whole job, in the only order that works: the joiner pak is
        # mounted at process start, so it has to be installed before Steam is asked to launch.
        s2.launch_game()
        assert calls == ["launch"], calls
        assert s2.game_opened_by_us == "player", s2.game_opened_by_us
        assert s2.i_connected is True, "pressing Launch is what reports a joiner in"

        # --- the game was ALREADY open: that player's one BeginPlay is spent, and we must not
        # pretend otherwise - there is no channel into a live Bodycam process to fix it.
        del calls[:]
        game_mod.game_running = lambda: True
        s3, _ = _live_session()
        s3.players = [dict(p) for p in roster]
        s3._on_connecting({"host": HOST, "players": roster, "connect_seconds": 180})
        assert calls == [], "must not ask Steam to launch a game that is already running"
        assert s3.game_was_open is True
    finally:
        game_mod.launch_game, game_mod.game_running = real_launch, real_running


def _match_ready_to_close():
    """A LiveSession that has been taken into a match and has a Bodycam of ours running.

    Returns (session, panel, closed, restore) where `closed` collects every close_game call, so
    a test can prove not only THAT the game was closed but that nothing closed it before the
    gate did."""
    from hub import game as game_mod

    HOST = "76561198000999000"        # _live_session's own steam_id
    roster = [{"name": "Sam", "steam_id": HOST, "level": 6, "ping": 20},
              {"name": "Pal", "steam_id": "76561198000000042", "level": 5, "ping": 40}]
    closed = []
    saved = (game_mod.launch_game, game_mod.game_running, game_mod.close_game)

    def restore():
        game_mod.launch_game, game_mod.game_running, game_mod.close_game = saved

    game_mod.launch_game = lambda: True
    game_mod.game_running = lambda: False
    game_mod.close_game = lambda grace_seconds=0, force=True: (closed.append(force), "closed")[1]
    s, panel = _live_session()
    # run the close's worker inline: the point under test is the decision, not the thread
    s._run_off_thread = lambda work: work()
    s.players = [dict(p) for p in roster]
    s._on_connecting({"host": HOST, "players": roster, "connect_seconds": 180})
    s.on_live_event({"type": "match_live", "map": "Rome"})
    return s, panel, closed, restore


def test_old_match_completion_cannot_close_a_new_match():
    s, panel, closed, restore = _match_ready_to_close()
    try:
        s.match_id = "new-match"
        for kind in ("match_result", "match_over", "match_cancelled"):
            s.on_live_event({"type": kind, "match_id": "old-match"})
        assert s.phase == "live"
        assert not closed
        assert s.history_stale
    finally:
        restore()


def test_migrated_host_replay_keeps_original_host_as_a_joiner():
    s, panel, closed, restore = _match_ready_to_close()
    try:
        s.match_id = "match"
        old_host = s.host["steam_id"]
        successor = next(p["steam_id"] for p in s.players if p["steam_id"] != old_host)
        s.host_pak_done = s.pak_done = True
        s.on_live_event({"type": "match_live", "match_id": "match", "host": successor,
                         "host_epoch": 1, "migration_token": "ab" * 32})
        assert s.phase == "live"
        assert not s._i_am_host()
        assert not s.host_pak_done and not s.pak_done
        assert s.migration_token == "ab" * 32
        assert not closed
    finally:
        restore()


def test_game_closes_only_after_the_hub_registers_the_match_complete():
    """Sam, 2026-09-15: "make sure the game only closes after flashbang registers the game as
    complete." The hub IS Flashbang, so the gate is register_match_complete() and nothing else.

    A match that is merely over in the GAME closes nothing. The host is the only reporter
    (docs/match-result.md hop 1), so a hub that shut the window on the strength of a scoreboard
    appearing could cut off the very report it is waiting for. It waits to be told."""
    from hub import competitive as C
    s, panel, closed, restore = _match_ready_to_close()
    try:
        assert s.phase == "live", s.phase
        assert s._game_ours is True, "the hub opened this game, so it owes it a close"
        assert s.match_complete == "" and s.game_close == ""

        # Time passing is not an ending, and neither is anything the game says on its own.
        panel.pump(limit=20)
        assert closed == [], "nothing may close a game the hub has not finished the match for"

        # The service confirms the scoreboard was collected and accepted.
        s.on_live_event({"type": "match_result", "match": "m1", "map": "Rome", "winner": 1,
                         "score": [7, 4], "voided": False,
                         "you": {"level": 6, "arrows": 2, "bdr": None}})
        assert s.phase == "result", s.phase
        assert s.match_complete == "match_result", s.match_complete
        assert (s.result or {}).get("won") is True
        assert (s.result or {}).get("score") == (7, 4), s.result
        assert (s.result or {}).get("delta") == 2, s.result
        # ARMED, not closed: the scoreboard is still on screen.
        assert s.game_close == "armed", s.game_close
        assert closed == [], "the game must not go before the player has read the result"

        panel.pump(limit=20)
        assert closed == [C.CLOSE_GAME_FORCE_HOST], closed
        assert s.game_close == "closed", s.game_close
        assert s._game_ours is False, "the debt is paid; nothing may close this game twice"

        # A second ending for the same match (match_over always follows) closes nothing again.
        s.on_live_event({"type": "match_over", "match_id": "m1"})
        panel.pump(limit=20)
        assert len(closed) == 1, closed
    finally:
        restore()


def test_a_losing_side_and_a_voided_match_still_close_the_game():
    """The result the player got has nothing to do with whether their game is shut: they all have
    to close it before they can queue again. A void carries no score and still closes."""
    s, panel, closed, restore = _match_ready_to_close()
    try:
        s.teams = {1: [dict(p) for p in s.players[:1]], 2: [dict(p) for p in s.players[1:]]}
        s.on_live_event({"type": "match_result", "match": "m1", "winner": 2, "score": [3, 7],
                         "voided": False, "you": {"arrows": -2, "bdr": None}})
        assert (s.result or {}).get("won") is False, s.result
        assert (s.result or {}).get("score") == (3, 7), "ours:theirs, not team1:team2"
        panel.pump(limit=20)
        assert closed and s.game_close == "closed", (closed, s.game_close)
    finally:
        restore()


def test_a_voided_match_closes_the_game_too():
    s, panel, closed, restore = _match_ready_to_close()
    try:
        s.on_live_event({"type": "match_result", "match": "m1", "voided": True,
                         "void_reason": "voted"})
        assert (s.result or {}).get("voided") is True
        assert s.match_complete == "match_result"
        panel.pump(limit=20)
        assert closed and s.game_close == "closed", (closed, s.game_close)
    finally:
        restore()


def test_game_close_never_touches_a_game_the_hub_did_not_open():
    """A match that dies before the connect window never opened anything. The player may well be
    in the game doing something else of their own, and it was never ours to take away."""
    from hub import game as game_mod
    closed = []
    saved = game_mod.close_game
    game_mod.close_game = lambda grace_seconds=0, force=True: (closed.append(force), "closed")[1]
    try:
        s, panel = _live_session()
        s._run_off_thread = lambda work: work()
        s.phase = "lobby"
        assert s.register_match_complete("cancelled") is True
        assert s.game_close == "skipped", s.game_close
        panel.pump(limit=20)
        assert closed == [], closed
    finally:
        game_mod.close_game = saved


def test_a_close_armed_for_one_match_can_never_reach_the_next_game():
    """A cancelled match can put the player straight back in the queue, so the close armed for it
    may still be pending when the NEXT match opens a new game. The close is scoped to the launch
    (_close_gen), so the stale one declines rather than shutting a match that is starting."""
    from hub import game as game_mod
    closed = []
    saved = (game_mod.launch_game, game_mod.game_running, game_mod.close_game)
    game_mod.launch_game = lambda: True
    game_mod.game_running = lambda: False
    game_mod.close_game = lambda grace_seconds=0, force=True: (closed.append(force), "closed")[1]
    HOST = "76561198000999000"
    roster = [{"name": "Sam", "steam_id": HOST, "level": 6, "ping": 20}]
    try:
        s, panel = _live_session()
        s._run_off_thread = lambda work: work()
        s.players = [dict(p) for p in roster]
        s._on_connecting({"host": HOST, "players": roster, "connect_seconds": 180})
        gen_one = s._close_gen
        # the match dies and we are put back in the queue: a close is armed for THIS game
        s.on_live_event({"type": "match_cancelled", "reason": "no_show", "requeued": True})
        assert s.match_complete == "cancelled", s.match_complete
        assert s.game_close == "armed", s.game_close

        # ...and a new match opens a new game before that timer ever fires
        s.players = [dict(p) for p in roster]
        s._on_connecting({"host": HOST, "players": roster, "connect_seconds": 180})
        assert s._close_gen != gen_one, "a new launch must be a new generation"

        panel.pump(limit=40)
        assert closed == [], "a stale close must never reach the game the next match is using"
    finally:
        game_mod.launch_game, game_mod.game_running, game_mod.close_game = saved


def test_close_game_asks_before_it_kills():
    """game.close_game posts WM_CLOSE (taskkill with no /F) and only kills if that is ignored.

    A /F kill is indistinguishable from a crash, and a crash while hosting has been measured to
    lock the account out of hosting for 20+ minutes (docs/HANDOFF-autojoin.md). So the request
    always goes first, and force=False never escalates at all."""
    from hub import game as game_mod
    saved = (game_mod.game_pids, game_mod._taskkill, game_mod.GAME_CLOSE_POLL_SECONDS)
    game_mod.GAME_CLOSE_POLL_SECONDS = 0.001
    kills = []
    try:
        # --- it goes away when asked: one polite taskkill, no kill
        alive = [[111]]
        game_mod.game_pids = lambda: alive.pop(0) if alive else []
        game_mod._taskkill = lambda pids, force: (kills.append((tuple(pids), force)), True)[1]
        assert game_mod.close_game(grace_seconds=1) == "closed"
        assert kills == [((111,), False)], kills

        # --- nothing running: nothing asked
        del kills[:]
        game_mod.game_pids = lambda: []
        assert game_mod.close_game(grace_seconds=1) == "not-running"
        assert kills == [], kills

        # --- it ignores the request. force=False leaves it alone and says so...
        del kills[:]
        game_mod.game_pids = lambda: [222]
        assert game_mod.close_game(grace_seconds=0.01, force=False) == "failed"
        assert kills == [((222,), False)], kills

        # ...and force=True escalates, to the pids found at the START (never a fresh lookup, so a
        # game the player has relaunched by hand is not in the blast)
        del kills[:]
        # it goes away only once the KILL lands, so the grace really does have to run out first
        game_mod.game_pids = lambda: [] if any(f for _, f in kills) else [222]
        assert game_mod.close_game(grace_seconds=0.01, force=True) == "forced"
        assert kills == [((222,), False), ((222,), True)], kills
    finally:
        game_mod.game_pids, game_mod._taskkill, game_mod.GAME_CLOSE_POLL_SECONDS = saved


def test_connect_reports_the_lobby_for_the_record():
    """The veto still runs in the hub, so the connect POST is the only chance to tell the
    server who was on which team. Without it a history row has a map and nothing else."""
    from hub import competitive as C
    s, panel = _live_session()
    s.phase = "lobby"
    s.players = [{"name": "P%d" % i, "steam_id": "7656119800000%04d" % i, "level": 5, "ping": 30}
                 for i in range(4)]
    s.teams = {1: s.players[:2], 2: s.players[2:]}
    s.sides = {1: "attack", 2: "defend"}
    s.bans = [(1, "Pool"), (2, "Hospital")]
    s.map = "Rome"
    s._begin_connect()

    sent = s.client.lobby
    assert sent["teams"] == {1: ["76561198000000000", "76561198000000001"],
                             2: ["76561198000000002", "76561198000000003"]}, sent["teams"]
    assert sent["sides"] == {1: "attack", 2: "defend"}
    assert sent["bans"] == [(1, "Pool"), (2, "Hospital")]

    # ...and LiveClient turns that into the JSON shape server/live.cjs reads: string keys,
    # and bans as objects rather than tuples, which JSON has no way to express.
    from hub import live as live_mod
    posted = {}
    client = live_mod.LiveClient("tok")
    client._post = lambda path, body=None: posted.update({"path": path, "body": body}) or (200, {})
    client.start_connect("Rome", "76561198000000000", sent["teams"], sent["sides"], sent["bans"])
    assert posted["path"] == "/api/match/connecting"
    assert posted["body"]["teams"] == {"1": ["76561198000000000", "76561198000000001"],
                                       "2": ["76561198000000002", "76561198000000003"]}
    assert posted["body"]["sides"] == {"1": "attack", "2": "defend"}
    assert posted["body"]["bans"] == [{"team": 1, "map": "Pool"}, {"team": 2, "map": "Hospital"}]
    import json
    json.dumps(posted["body"])          # it has to survive the wire, tuples would not


def test_match_history_under_xvfb():
    """The sub-tab, and the promise it rests on: Sam, 2026-09-14, it has to be readable
    "while staying in the searching for game queue or at any time during the pre-game
    selection phases without disturbing anything with that functionality"."""
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    cat_path = os.path.join(tmpdir(), "cat-hist.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(sample_catalogue(), f)
    env = {**os.environ, "HUB_STATE_DIR": tmpdir("hub-hist-state-"),
           "HUB_GAME_DIR": fake_game_dir("hist-game"),
           "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", MATCH_HISTORY_UI_CODE],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "history ui ok" in r.stdout, (r.stdout, r.stderr)


MATCH_HISTORY_UI_CODE = r"""
import os, time, tkinter as tk
from hub.app import HubApp
from hub import competitive as C
import hub.i18n as i18n

HubApp.tray_available = staticmethod(lambda: False)
root = tk.Tk()
app = HubApp(root, language='en')
root.update()
for _ in range(100):
    root.update(); time.sleep(0.05)
    if app.catalogue is not None: break

app._show_tab('competitive'); root.update()
p = app.comp
app.state['installed'] = {'BB5': {'version': '1.0.4', 'title': 'Bodybomb 5v5'}}
# The lobby and everything after it is still the preview, so drive this with MockSession.
p.session = s = C.MockSession(p)
s.adopt_account({'steam_id': '76561198000999000', 'persona': 'Sam', 'token': 'tok'})
s.phase = 'idle'
p.refresh(); root.update()

def texts_of(widget):
    out = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, (tk.Label, tk.Button)): out.append(str(c['text']))
            walk(c)
    walk(widget)
    return out

def find_label(parent, text):
    hit = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Label) and str(c['text']) == text: hit.append(c)
            walk(c)
    walk(parent)
    return hit[0]

# the strip is there once signed in, Play is the active one
strip = texts_of(p.subtabs)
assert 'Play' in strip and 'Match History' in strip, strip
assert p.view == 'play'
assert p.body.winfo_ismapped() and not p.history_frame.winfo_ismapped()

# clicking it swaps the view and touches NOTHING in the session
before = (s.phase, s.queue_seconds)
find_label(p.subtabs, 'Match History').event_generate('<Button-1>'); root.update()
assert p.view == 'history', p.view
assert p.history_frame.winfo_ismapped() and not p.body.winfo_ismapped()
assert (s.phase, s.queue_seconds) == before, 'switching the view changed the session'
assert 'No matches yet' in texts_of(p._hist_content), texts_of(p._hist_content)

# THE PROMISE: queue, open history, and let the queue tick
p._show_view('play'); root.update()
s.find_match()
for _ in range(3): s._check_next()
root.update(); assert s.phase == 'queued', s.phase
s.history = [{'id': 'm%d' % i, 'ended': (time.time() - i * 3600) * 1000, 'map': 'Rome',
              'outcome': 'played', 'reason': '', 'blamed': False, 'team': 1, 'side': 'attack',
              'host': False, 'players': 10, 'won': None, 'score': None, 'delta': None,
              'elo': None, 'connected': True, 'accepted': True} for i in range(30)]
s.history[0].update({'outcome': 'cancelled', 'reason': 'no_show', 'blamed': True, 'elo': -25})
p._show_view('history'); root.update()
assert s.phase == 'queued', 'opening history left the queue'

draws = {'n': 0}
real_draw = p._draw_history_list
def counted():
    draws['n'] += 1
    real_draw()
p._draw_history_list = counted
root.update()
children_before = len(p._hist_content.winfo_children())
for _ in range(5):
    s.queue_seconds += 1
    s._changed()                     # exactly what the queue timer does every second
    root.update()
assert s.phase == 'queued', 'the queue died while history was open'
assert draws['n'] == 0, 'the list was rebuilt %d times by queue ticks' % draws['n']
assert len(p._hist_content.winfo_children()) == children_before

# the strip on top of it IS live, so the player still sees the queue clock
alert = texts_of(p._hist_alert)
assert any('Still searching' in x for x in alert), alert
assert any('Back to Play' in x for x in alert), alert
first = [x for x in alert if 'Still searching' in x][0]
s.queue_seconds += 7; s._changed(); root.update()
second = [x for x in texts_of(p._hist_alert) if 'Still searching' in x][0]
assert first != second, (first, second)
p._draw_history_list = real_draw

# an error the player is owed must not be hidden behind the sub-tab: the queue screen that
# would normally show it is not being drawn at all while history is up
s.error = 'the service went away'
s._changed(); root.update()
assert any('the service went away' in x for x in texts_of(p._hist_alert)), texts_of(p._hist_alert)
s.error = ''
s._changed(); root.update()

# a found match is the one thing that takes the view away from them
s.queue_seconds = 4
s._tick_queue(); root.update()
assert s.phase == 'found', s.phase
assert p.view == 'play', 'match found must force the accept window in front of the player'
assert p.body.winfo_ismapped() and not p.history_frame.winfo_ismapped()
assert any('Match found' in x for x in texts_of(p.body)), texts_of(p.body)

# ...and it stays taken away: the sub-tab is refused for the whole accept window, not just
# on the tick the match was found
assert p._history_locked()
p._show_view('history'); root.update()
assert p.view == 'play', 'history must stay shut while the accept window is open'
find_label(p.subtabs, 'Match History').event_generate('<Button-1>'); root.update()
assert p.view == 'play', 'clicking the greyed sub-tab must do nothing'
assert p.body.winfo_ismapped() and any('Match found' in x for x in texts_of(p.body))

# ...and during the veto it only warns, because the player can still read
s.accept(); root.update()
while s.phase == 'found': s._others_accept()
root.update(); assert s.phase == 'lobby'
p._show_view('history'); root.update()
assert p.view == 'history', 'the pre-game selection must not lock the player out of history'
assert s.phase == 'lobby', 'reading history during the lobby disturbed it'
s.pick_coin('heads'); root.update(); s._coin_lands(); root.update()
s.toss_winner = 1; s.stage = 'choice'; s.choose('ban'); root.update()
assert s.stage == 'veto' and s.ban_turn == 1 and s.i_am_captain()
assert any('your turn to ban' in x for x in texts_of(p._hist_alert)), texts_of(p._hist_alert)
guard = 0
while s.stage == 'veto' and guard < 20:
    s.ban(s.remaining_maps()[0], by=s.ban_turn); root.update(); guard += 1
assert s.stage == 'ready' and s.map, 'the veto ran to the end with history open'

# the rows themselves
s.phase = 'idle'; s.reset_match()
p._show_view('history'); root.update()
body = texts_of(p._hist_content)
assert any('No show' == x for x in body), body
assert any('-25 Elo' == x for x in body), body
assert any('Played' == x for x in body), body
assert any('Rome' == x for x in body), body
assert any('Team 1' in x for x in body), body
assert any('no score' == x for x in body), body
assert any('Scores and Elo are not recorded yet' in x for x in body), 'the pending note'

# the detail pop-up opens even when the record has gone
win = p._show_match_detail('m1', s.history[1]); root.update()
assert win.winfo_exists() and any('no longer on record' in x for x in texts_of(win))
win.destroy(); root.update()

# signing out takes the strip away and puts the view back
s.sign_out(); root.update()
assert p.view == 'play' and texts_of(p.subtabs) == []

print('history ui ok', flush=True)
os._exit(0)   # skip Tk teardown: it can segfault under xvfb after the worker threads ran
"""


def test_competitive_tab_under_xvfb():
    """Optional: build the window, switch to Competitive, walk the flow, if xvfb is here."""
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    code = COMPETITIVE_UI_CODE
    cat_path = os.path.join(tmpdir(), "cat.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(sample_catalogue(), f)
    env = {**os.environ, "HUB_STATE_DIR": tmpdir("hub-comp-state-"),
           "HUB_GAME_DIR": fake_game_dir("comp-game"),
           "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "competitive ui ok" in r.stdout, (r.stdout, r.stderr)


COMPETITIVE_UI_CODE = r"""
import os, time, tkinter as tk
from hub.app import HubApp
from hub import competitive as C
from hub import state as state_mod
from hub.theme import ACCENT, WHITE
import hub.i18n as i18n

HubApp.tray_available = staticmethod(lambda: False)
root = tk.Tk()
app = HubApp(root, language='en')
root.update()
for _ in range(100):
    root.update(); time.sleep(0.05)
    if app.catalogue is not None: break
assert app.catalogue is not None

# the strip exists, Gamemodes is active, and the old list still works
assert set(app.tab_frames) == {'gamemodes', 'competitive'}
assert app.tab == 'gamemodes'
gm_label, gm_rule = app.tab_buttons['gamemodes']
cp_label, cp_rule = app.tab_buttons['competitive']
assert gm_rule['bg'] == ACCENT and cp_rule['bg'] == WHITE
assert app.rows, 'the gamemode list did not survive the tab strip'

# clicking the tab swaps the frames and repaints the underline
cp_label.event_generate('<Button-1>'); root.update()
assert app.tab == 'competitive'
assert cp_rule['bg'] == ACCENT and gm_rule['bg'] == WHITE
assert app.tab_frames['competitive'].winfo_ismapped()
assert not app.tab_frames['gamemodes'].winfo_ismapped()

p = app.comp
assert p is not None

def texts_of(widget):
    out = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, (tk.Label, tk.Button)):
                out.append(str(c['text']))
            walk(c)
    walk(widget)
    return out

# gated until Bodybomb 5v5 is installed
assert not p.gamemode_installed()
assert any('Bodybomb 5v5 is required' in x for x in texts_of(p.body))

app.state['installed'] = {'BB5': {'version': '1.0.4', 'title': 'Bodybomb 5v5'}}
p.refresh(); root.update()
assert any('Sign in with Steam' in x for x in texts_of(p.body))

# walk the flow; every phase must draw without raising
s = p.session
s.phase = 'signing_in'
s._sign_in_done({'steam_id': '76561198000999000', 'persona': 'Sam', 'token': 'tok-123'})
root.update()
assert s.phase == 'idle' and s.me['name'] == 'Sam'

# the party card: solo -> create -> the code is on screen -> join dialog -> leave
assert any('Create a party' in x for x in texts_of(p.body))
s.create_party(); root.update()
code = s.party['code']
assert any(code == x for x in texts_of(p.body)), texts_of(p.body)
assert any('leader' in x for x in texts_of(p.body))
p._copy_party_code(); root.update()

# hide it for a stream: the code is blanked on screen but Copy and the party still work
assert not p.party_code_hidden
p._toggle_party_code(); root.update()
assert p.party_code_hidden
assert not any(code == x for x in texts_of(p.body)), 'the code is still readable while hidden'
assert any(C.mask_party_code(code) == x for x in texts_of(p.body)), texts_of(p.body)
assert any('Show' == x for x in texts_of(p.body))
p._copy_party_code(); root.update()          # copying must work while hidden
p._toggle_party_code(); root.update()
assert not p.party_code_hidden and any(code == x for x in texts_of(p.body))
assert any('Hide' == x for x in texts_of(p.body))

# a new code replaces it on screen and reveals it again
p._toggle_party_code(); root.update()
assert p.party_code_hidden
p._new_party_code(); root.update()
assert not p.party_code_hidden, 'a fresh code is shown, not hidden'
new_code = s.party['code']
assert new_code != code and any(new_code == x for x in texts_of(p.body))
code = new_code

win = p._ask_party_code(); root.update()
assert win.winfo_exists()
win.destroy(); root.update()
s.leave_party(); root.update()
assert s.party is None
assert any('Create a party' in x for x in texts_of(p.body))

# a member of someone else's party waits for the leader instead of seeing Find match
s.join_party('QRST-UV'); root.update()
assert not s.is_party_leader()
assert any('to start the search' in x for x in texts_of(p.body)), texts_of(p.body)
assert not any('Find match' == x for x in texts_of(p.body))
s.leave_party(); root.update()

# The panel now uses LiveSession. Offline, pressing Find match must SAY so rather than
# pretend to queue - the preview used to invent a match here.
assert type(s).__name__ == 'LiveSession', type(s).__name__
s.find_match(); root.update()
for _ in range(3): s._check_next()
root.update()
# Offline it retries before giving up. The invariant that matters is that it never
# claims to be queued when the server has not said so.
assert s.phase in ('checking', 'idle'), s.phase

# Everything from the lobby onwards is still the preview, so walk that with MockSession.
p.session = s = C.MockSession(p)
s.adopt_account({'steam_id': '76561198000999000', 'persona': 'Sam', 'token': 'tok-123'})
s.phase = 'idle'
p.refresh(); root.update()

s.find_match(); root.update()
for _ in range(3): s._check_next()
root.update(); assert s.phase == 'queued'

# flicker guard (bug 2): while queued, a stats-only change (the online count) or a queue tick
# must refresh the label IN PLACE, never tear down and rebuild the whole body.
_body_draws = {'n': 0}
_real_body = p._draw_body
def _counted_body():
    _body_draws['n'] += 1
    return _real_body()
p._draw_body = _counted_body
p.on_change(); root.update()                 # settle: one rebuild registers the live labels
_mark = _body_draws['n']
s.online = 777; s._changed(); root.update()
assert _body_draws['n'] == _mark, 'a stats change rebuilt the body (flicker)'
assert any('777' in x for x in texts_of(p.header)), texts_of(p.header)
_c = s.queue_seconds + 1
s.queue_seconds = _c; s._changed(); root.update()
assert _body_draws['n'] == _mark, 'a queue tick rebuilt the body (flicker)'
_want = '%d:%02d' % (_c // 60, _c % 60)
assert any(_want in x for x in texts_of(p.body)), (_want, texts_of(p.body))
p._draw_body = _real_body

s.party = {'code': 'ABCD-EF', 'leader_id': s.me['steam_id'],
           'members': [dict(s.me), dict(s.me), dict(s.me)]}
p.refresh(); root.update()
assert any('party of 3' in x for x in texts_of(p.body)), texts_of(p.body)
s.party = None
s.queue_seconds = 4; s._tick_queue(); root.update()
assert s.phase == 'found'
s.accept(); root.update()
while s.phase == 'found': s._others_accept()
root.update(); assert s.phase == 'lobby'
assert any('Coin flip' in x for x in texts_of(p.body))
s.pick_coin('heads'); root.update(); s._coin_lands(); root.update()
s.toss_winner = 1; s.stage = 'choice'; s.choose('ban'); root.update()
assert s.ban_turn == 1 and s.first_ban == 1
guard = 0
while s.stage == 'veto' and guard < 20:
    s.ban(s.remaining_maps()[0], by=s.ban_turn); root.update(); guard += 1
assert s.stage == 'ready' and s.map and s.map != 'Paintball' 
s._go_live(); root.update()
assert s.phase == 'live' and any('Join the match' in x for x in texts_of(p.body))
s.start_vote(); root.update()
assert any('Vote to void the match' in x for x in texts_of(p.body))
s.cast_vote(False); root.update()
s.finish(); root.update()
assert s.phase == 'result'
s.leave_result(); root.update()

# the integrity note opens and says what is read
win = p._show_integrity_note(); root.update()
assert win.winfo_exists() and any('~mods' in x for x in texts_of(win))
win.destroy(); root.update()

# the match-found volume slider and its Test button (Sam, 2026-09-14). The tab has to be
# MAPPED for any of this: Tk does not deliver widget events to an unmapped page.
assert app.tab == 'competitive' and app.tab_frames['competitive'].winfo_ismapped()
scales, testbtn = [], []
def hunt(w):
    if isinstance(w, tk.Scale): scales.append(w)
    if isinstance(w, tk.Button) and w['text'] == i18n.t('comp_sound_test'): testbtn.append(w)
    for c in w.winfo_children(): hunt(c)
hunt(p.body)
assert len(scales) == 1, 'one volume slider on the idle screen'
assert len(testbtn) == 1, 'a Test button next to it'
assert scales[0].get() == p.sound_volume()

scales[0].set(35); scales[0].event_generate('<ButtonRelease-1>'); root.update()
assert p.sound_volume() == 35, ('the slider must stick', p.sound_volume())
assert app.state['comp_sound_volume'] == 35, 'and be written to state.json'
# ...and be READ BACK from it: state.load() copies keys one at a time, so a key it does not
# know about is written and then silently dropped on the next start
assert state_mod.load()['comp_sound_volume'] == 35, 'the slider must survive a restart'

testbtn[0].invoke(); root.update()          # no audio device here; it must not raise

# the bell mutes and puts the slider back where the player left it, not at a default
hdr = []
def hunt_bell(w):
    if isinstance(w, tk.Label) and i18n.t('comp_sound_off') in str(w['text']) + '|': hdr.append(w)
    for c in w.winfo_children(): hunt_bell(c)
p.toggle_sound(); root.update()
assert p.sound_volume() == 0
assert state_mod.load()['comp_sound_volume'] == 0, 'a muted player stays muted after a restart'
assert state_mod.load()['comp_sound_last'] == 35, 'and the bell remembers where to go back to'
p.toggle_sound(); root.update()
assert p.sound_volume() == 35, 'unmuting restores the chosen level'

# a language change rebuilds the window and stays on the Competitive tab
i18n.set_language('de'); app.state['language'] = 'de'
app._rebuild_ui(); root.update()
assert app.tab == 'competitive'
assert app.tab_buttons['competitive'][0]['text'] == 'Wettkampf'
assert app.comp is not None and app.comp is not p
i18n.set_language('en')

app._show_tab('gamemodes'); root.update()
assert app.tab == 'gamemodes' and app.tab_frames['gamemodes'].winfo_ismapped()

print('competitive ui ok', flush=True)
os._exit(0)
"""



def test_profile_stats():
    """The arithmetic behind the profile page, with no window in sight.

    This is the test that matters on Sam's PC, where there is no xvfb: every number the
    profile prints is counted here, including the ones it must REFUSE to print."""
    from hub import competitive as C

    def row(**over):
        base = {"id": "m", "ended": 1789000000000, "map": "Rome", "outcome": "played",
                "reason": "", "blamed": False, "team": 1, "side": "attack", "host": False,
                "players": 10, "won": None, "score": None, "delta": None, "elo": None,
                "connected": True, "accepted": True}
        base.update(over)
        return base

    rows = [
        row(id="m1", won=True, ended=1789000006000),
        row(id="m2", won=False, ended=1789000005000),
        row(id="m3", ended=1789000004000),                       # played, no scoreboard
        row(id="m4", map="Airsoft", side="defend", host=True, ended=1789000003000),
        row(id="m5", outcome="cancelled", reason="no_show", blamed=True, elo=-25,
            connected=False, ended=1789000002000),
        row(id="m6", outcome="cancelled", reason="no_show", blamed=False,
            ended=1789000001000),
    ]
    s = C.profile_stats(rows)
    assert s["recorded"] == 6, s
    assert s["played"] == 4 and s["cancelled"] == 2, s
    assert (s["wins"], s["losses"], s["undecided"]) == (1, 1, 2), s
    assert s["win_rate"] == 50, s
    assert s["at_fault"] == 1 and s["no_show"] == 1, s
    assert s["declined"] == 0 and s["abandoned"] == 0, s
    assert s["hosted"] == 1, s
    assert s["elo_lost"] == 25, "a penalty is stored as a negative Elo and counted as a loss"
    assert (s["attack"], s["defend"]) == (3, 1), s
    assert s["maps"] == [("Rome", 3), ("Airsoft", 1)], s
    assert s["top_map"] == "Rome"
    assert s["last_played"] == 1789000006000, s
    assert s["form"] == [C.FORM_WIN, C.FORM_LOSS, C.FORM_PLAYED, C.FORM_PLAYED,
                         C.FORM_FAULT, C.FORM_CANCELLED], s["form"]

    # A cancelled match says nothing about maps or sides: nobody ever loaded that map.
    only_cancelled = C.profile_stats([row(outcome="cancelled", reason="declined", blamed=True)])
    assert only_cancelled["maps"] == [] and only_cancelled["attack"] == 0, only_cancelled
    assert only_cancelled["declined"] == 1 and only_cancelled["last_played"] == 0

    # THE ONE THAT MATTERS: `won` is null on every row until the gamemode reports a
    # scoreboard, and a profile that turned that into "0 / 0, 0% wins" would be telling the
    # player they have never won a match.
    unscored = C.profile_stats([row(), row(), row()])
    assert unscored["win_rate"] is None, "an unknown record must stay unknown, not become 0%"
    assert unscored["wins"] == 0 and unscored["undecided"] == 3, unscored

    assert C.profile_stats([])["recorded"] == 0
    assert C.profile_stats(None)["win_rate"] is None

    # The form strip is the tail of the sample, not the whole of it.
    many = C.profile_stats([row(won=True) for _ in range(15)])
    assert len(many["form"]) == C.FORM_SHOWN and many["wins"] == 15, many["wins"]

    # Rows come off the wire, so one bad field costs one statistic and never the page.
    junk = C.profile_stats([
        "not a row", None,
        row(elo="-25", ended="nonsense", won="yes"),
        row(map=None, side="sideways", elo={"nope": 1}),
    ])
    assert junk["recorded"] == 2, "non-dict entries are dropped before anything is counted"
    assert junk["elo_lost"] == 25, "a numeric string is still a number"
    assert junk["last_played"] == 1789000000000,         "an unreadable timestamp on one row must not blank the date the others carry"
    assert junk["undecided"] == 2, "'yes' is not True: an unknown result stays unknown"
    assert junk["maps"] == [("Rome", 1)] and junk["attack"] == 1, junk


PROFILE_UI_CODE = r"""
import os, time, tkinter as tk
from hub.app import HubApp
from hub import competitive as C

HubApp.tray_available = staticmethod(lambda: False)
root = tk.Tk()
app = HubApp(root, language='en')
root.update()
for _ in range(100):
    root.update(); time.sleep(0.05)
    if app.catalogue is not None: break

app._show_tab('competitive'); root.update()
p = app.comp
app.state['installed'] = {'BB5': {'version': '1.0.4', 'title': 'Bodybomb 5v5'}}
p.session = s = C.MockSession(p)
s.adopt_account({'steam_id': '76561198000999000', 'persona': 'Sam', 'token': 'tok'})
s.phase = 'idle'
s.history = [{'id': 'm%d' % i, 'ended': (time.time() - i * 3600) * 1000, 'map': 'Rome',
              'outcome': 'played', 'reason': '', 'blamed': False, 'team': 1, 'side': 'attack',
              'host': i == 0, 'players': 10, 'won': None, 'score': None, 'delta': None,
              'elo': None, 'connected': True, 'accepted': True} for i in range(6)]
s.history[5].update({'outcome': 'cancelled', 'reason': 'no_show', 'blamed': True, 'elo': -25})
p.refresh(); root.update()

def texts_of(widget):
    out = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, (tk.Label, tk.Button)): out.append(str(c['text']))
            walk(c)
    walk(widget)
    return out

def first_canvas(parent):
    hit = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Canvas) and not hit: hit.append(c)
            walk(c)
    walk(parent)
    return hit[0]

# THE ASK (Sam): click your picture, get your profile.
assert p.view == 'play'
before = (s.phase, s.queue_seconds)
first_canvas(p.header).event_generate('<Button-1>'); root.update()
assert p.view == 'profile', p.view
assert p.profile_frame.winfo_ismapped() and not p.body.winfo_ismapped()
assert (s.phase, s.queue_seconds) == before, 'opening the profile changed the session'

body = texts_of(p._prof_content)
assert 'Sam' in body, body
assert any('Steam ID' in x for x in body), body
assert any('placeholders' in x for x in body), 'the invented rank must say it is invented'
assert any('last 6 matches' in x for x in body), body
assert 'Recent form' in body and 'Conduct' in body and 'Maps played' in body, body
# the numbers, counted from the planted history: 5 played, 1 cancelled, 1 hosted, 1 at fault
assert any('No scores recorded yet' in x for x in body), 'an unknown record stays unknown'
assert any('No show' in x for x in body), body

# Back returns to whichever view the avatar was clicked FROM.
p._show_view('play'); root.update()
p._show_view('history'); root.update()
first_canvas(p.header).event_generate('<Button-1>'); root.update()
assert p.view == 'profile' and p._profile_from == 'history'
def find_back(parent):
    hit = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, tk.Label) and 'Back' in str(c['text']): hit.append(c)
            walk(c)
    walk(parent)
    return hit[0]
find_back(p._prof_content).event_generate('<Button-1>'); root.update()
assert p.view == 'history', p.view

# THE SAFETY RULE: the accept window owns the screen. It pulls a player off the profile and
# refuses to open it, exactly as it does for Match History.
p._show_view('profile'); root.update()
assert p.view == 'profile'
s.phase = 'found'
p.on_change(); root.update()
assert p.view == 'play', 'the accept window must take the screen back'
p._show_view('profile'); root.update()
assert p.view == 'play', 'the profile must not open during the accept window'
first_canvas(p.header).event_generate('<Button-1>'); root.update()
assert p.view == 'play', 'the avatar must not open the profile during the accept window'

# A match running behind the profile still has to say so.
s.phase = 'queued'; s.queue_seconds = 42
p._show_view('profile'); root.update()
p.on_change(); root.update()
assert any('Still searching' in x for x in texts_of(p._prof_alert)), texts_of(p._prof_alert)

# Signing out puts the player back on Play rather than a profile for nobody.
s.sign_out(); root.update()
assert p.view == 'play', p.view

print('profile ui ok')
"""


def test_profile_under_xvfb():
    """Clicking the avatar opens the profile, and the accept window still owns the screen."""
    import subprocess
    if not shutil.which("xvfb-run"):
        print("      (skipped: no xvfb-run)")
        return
    cat_path = os.path.join(tmpdir(), "cat-prof.json")
    with open(cat_path, "w", encoding="utf-8") as f:
        json.dump(sample_catalogue(), f)
    env = {**os.environ, "HUB_STATE_DIR": tmpdir("hub-prof-state-"),
           "HUB_GAME_DIR": fake_game_dir("prof-game"),
           "HUB_CATALOGUE_URL": file_url(cat_path)}
    r = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", PROFILE_UI_CODE],
                       cwd=str(REPO), capture_output=True, text=True, timeout=120, env=env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "profile ui ok" in r.stdout, (r.stdout, r.stderr)


def test_friends_slice_and_verbs():
    """The Friends screen contributes its slice and its verbs, and masks the code when hidden.

    The mask is done in JS-visible data rather than by withholding the code, because Copy has to
    work while it is hidden: the value is already on this machine and what is being protected is
    the SCREEN, for someone who is streaming."""
    from hub.webui import screens as S
    from hub.webui.screens import friends as F
    from hub import i18n
    i18n.set_language("en")

    panel, sess = _web_panel()
    sess.me = {"steam_id": "76561198000000001", "name": "Sam"}
    sess.friend_code = "ABCDE-FGHJK"
    sess.friends = ({"steam_id": "76561198000000002", "persona": "Mate", "online": True},)
    sess.friend_requests_in = ({"steam_id": "76561198000000003", "persona": "Foe", "online": False},)

    slice_ = F.snapshot(sess, panel)["friends"]
    assert slice_["signed_in"] is True
    assert slice_["code"] == "ABCDE-FGHJK", "Copy needs the real value even while hidden"
    assert slice_["code_hidden"] is True, "hidden by default: a stream shows the hub by definition"
    assert "ABCDE" not in slice_["code_masked"], "the mask still shows the code"
    assert slice_["code_masked"].count("-") == 1, "the shape is kept so it still reads as a code"
    assert len(slice_["list"]) == 1 and len(slice_["incoming"]) == 1

    # revealing it is a panel toggle, not a separate fetch
    sess.friend_code_hidden = False
    assert F.snapshot(sess, panel)["friends"]["code"] == "ABCDE-FGHJK"

    for verb in ("friend_add", "friend_accept", "friend_decline", "friend_cancel",
                 "friend_remove", "friend_code_new", "friend_code_toggle", "friends_refresh"):
        assert verb in S.SCREEN_VERBS, f"{verb} is not reachable from the UI"

def test_party_invite_slice_and_verbs():
    """The invite surface: the button only exists once there is a party, the picker lists every
    friend with the reason an un-pressable row cannot be pressed, and the inbox is whatever the
    server last pushed.

    Nothing here is decided locally. The one piece of local state is `invite_sent` (the row that
    says "Invited"), and it is taken back when the POST fails - a button that lies about what
    happened is worse than one that does nothing."""
    from hub.webui import screens as S
    from hub.webui.screens import competitive as CS
    from hub import i18n
    i18n.set_language("en")

    panel, sess = _web_panel()
    sess.friends = (
        {"steam_id": "76561198000000002", "persona": "Mate", "online": True},
        {"steam_id": "76561198000000003", "persona": "Away", "online": False},
    )

    # solo: no party, so no picker - but an invite can still arrive, and it is how you get one
    solo = CS.party_snapshot(sess, panel)
    assert solo["in_party"] is False and solo["can_invite"] is False
    assert solo["friends"] == [] and solo["invites"] == []

    sess.on_live_event({"type": "party_update", "code": "ABCD12",
                        "leader_id": sess.me["steam_id"],
                        "members": [{"steam_id": sess.me["steam_id"], "persona": "Sam"}]})
    party = CS.party_snapshot(sess, panel)
    assert party["can_invite"] is True, "a party with a free seat is what the button needs"
    rows = {r["name"]: r for r in party["friends"]}
    assert rows["Mate"]["can_invite"] is True
    assert rows["Away"]["can_invite"] is False and rows["Away"]["online"] is False, \
        "an offline friend is listed, not hidden: a picker that hides half the list answers " \
        "'where is she?' with nothing at all"

    # inviting marks the row, and the POST goes out
    sess.invite_to_party("76561198000000002")
    assert "invite:76561198000000002" in sess.client.calls
    assert {r["name"]: r for r in CS.party_snapshot(sess, panel)["friends"]}["Mate"]["invited"] is True

    # a refusal takes the label back and says why
    sess.client.invite_reply = (409, {"ok": False, "error": "They are not online."})
    sess.invite_to_party("76561198000000003")
    after = CS.party_snapshot(sess, panel)
    assert {r["name"]: r for r in after["friends"]}["Away"]["invited"] is False
    assert after["invite_error"] == "They are not online."

    # somebody already in the party cannot be invited into it again
    sess.on_live_event({"type": "party_update", "code": "ABCD12",
                        "leader_id": sess.me["steam_id"],
                        "members": [{"steam_id": sess.me["steam_id"], "persona": "Sam"},
                                    {"steam_id": "76561198000000002", "persona": "Mate"}]})
    mate = {r["name"]: r for r in CS.party_snapshot(sess, panel)["friends"]}["Mate"]
    assert mate["in_party"] is True and mate["can_invite"] is False

    # the inbox is the server's push, whole, every time
    sess.on_live_event({"type": "party_invites", "invites": [
        {"from": {"steam_id": "76561198000000009", "persona": "Ally"},
         "code": "ZZZZ99", "size": 2, "max": 5, "expires_in": 120}]})
    inbox = CS.party_snapshot(sess, panel)["invites"]
    assert len(inbox) == 1 and inbox[0]["name"] == "Ally" and inbox[0]["size"] == 2
    sess.client.invite_reply = (200, {"ok": True})
    sess.accept_party_invite("76561198000000009")
    assert "invite_accept:76561198000000009" in sess.client.calls
    sess.decline_party_invite("76561198000000009")
    assert "invite_decline:76561198000000009" in sess.client.calls
    # ...and an empty push empties it, rather than the hub keeping a row the server dropped
    sess.on_live_event({"type": "party_invites", "invites": []})
    assert CS.party_snapshot(sess, panel)["invites"] == []

    for verb in ("open_invites", "invite_friend", "invite_accept", "invite_decline"):
        assert verb in S.SCREEN_VERBS, f"{verb} is not reachable from the UI"


def test_friends_invite_makes_a_party_when_there_is_none():
    """Invite, from the Friends list, with no party: the hub mints one and THEN sends the invite.

    Sam, 2026-09-16: "add an invite button to the left of remove to invite them to a party. if
    the inviter isnt already in a party, create a party and then send the invite."

    The order is the whole test. The server refuses an invite from somebody who is not in a
    party ("You are not in a party."), so the create has to land first - and it is the create's
    OWN 200 that is waited on, not the party_update broadcast it causes, because hanging the
    invite off a socket message would mean racing the stream to find out whether the party we
    just made exists. Already in a party, nothing is minted: one POST, not two.
    """
    from hub.webui import screens as S
    from hub.webui.screens import friends as F
    from hub import i18n
    i18n.set_language("en")

    panel, sess = _web_panel()
    sess.friends = (
        {"steam_id": "76561198000000002", "persona": "Mate", "online": True},
        {"steam_id": "76561198000000003", "persona": "Away", "online": False},
    )

    assert "friend_invite" in S.SCREEN_VERBS, "the Invite button has no verb to call"

    # solo, and idle: the row is live, because the party is made on the way
    rows = {r["persona"]: r for r in F.snapshot(sess, panel)["friends"]["list"]}
    assert sess.party is None
    assert rows["Mate"]["can_invite"] is True, "being solo is not a reason it cannot be pressed"
    assert rows["Away"]["can_invite"] is False, "an invite to somebody offline would evaporate"
    assert rows["Away"]["in_party"] is False and rows["Mate"]["invited"] is False

    # press it: create first, invite second, in that order
    sess.invite_friend_to_party("76561198000000002")
    assert sess.client.calls == ["create_party", "invite:76561198000000002"], sess.client.calls
    assert F.snapshot(sess, panel)["friends"]["list"][0]["invited"] is True

    # the party the server made now arrives on the stream, as it would for any other join
    sess.on_live_event({"type": "party_update", "code": "ABCD12",
                        "leader_id": sess.me["steam_id"],
                        "members": [{"steam_id": sess.me["steam_id"], "persona": "Sam"}]})
    # ...and from here a second invite mints nothing: one POST, not two
    sess.client.calls = []
    sess.invite_friend_to_party("76561198000000003")
    assert sess.client.calls == ["invite:76561198000000003"], sess.client.calls

    # somebody already sitting with me is not invited again, and the row says which it is
    sess.on_live_event({"type": "party_update", "code": "ABCD12",
                        "leader_id": sess.me["steam_id"],
                        "members": [{"steam_id": sess.me["steam_id"], "persona": "Sam"},
                                    {"steam_id": "76561198000000002", "persona": "Mate"}]})
    mate = {r["persona"]: r for r in F.snapshot(sess, panel)["friends"]["list"]}["Mate"]
    assert mate["in_party"] is True and mate["can_invite"] is False and mate["invited"] is False


def test_friends_invite_reports_a_failure_and_never_lies_about_it():
    """A create that fails is reported as a FAILED INVITE - it is the invite the player asked
    for, and the party card they never asked to see is not where they are looking. Either way
    the row takes its "Invited" back: a button that lies about what happened is worse than one
    that does nothing.

    And while queued there is no party to make: the row goes dead rather than firing a POST the
    server would refuse."""
    from hub.webui.screens import friends as F
    from hub import i18n
    i18n.set_language("en")

    panel, sess = _web_panel()
    sess.friends = ({"steam_id": "76561198000000002", "persona": "Mate", "online": True},)

    # the create is refused: no party, no invite, no "Invited"
    sess.client.party_reply = (409, {"ok": False, "error": "Nope."})
    sess.invite_friend_to_party("76561198000000002")
    assert sess.client.calls == ["create_party"], "the invite must not go out without a party"
    slice_ = F.snapshot(sess, panel)["friends"]
    assert slice_["list"][0]["invited"] is False
    assert slice_["invite_error"] == "Nope."

    # the create lands but the invite is refused: same rollback, the invite's own reason
    sess.client.calls = []
    sess.client.party_reply = (200, {"ok": True})
    sess.client.invite_reply = (409, {"ok": False, "error": "They are not online."})
    sess.invite_friend_to_party("76561198000000002")
    assert sess.client.calls == ["create_party", "invite:76561198000000002"]
    slice_ = F.snapshot(sess, panel)["friends"]
    assert slice_["list"][0]["invited"] is False
    assert slice_["invite_error"] == "They are not online."

    # queued: no party can be minted, so the row is dead and nothing is sent
    sess.client.calls = []
    sess.phase = "queued"
    assert F.snapshot(sess, panel)["friends"]["list"][0]["can_invite"] is False
    sess.invite_friend_to_party("76561198000000002")
    assert sess.client.calls == [], sess.client.calls


def test_party_invite_toast_reaches_any_screen():
    """An invite arrives while the player is on some other tab, so the cue is a toast pushed
    through the panel's event channel - the inbox itself only exists on Competitive."""
    from hub import i18n
    i18n.set_language("en")
    panel, sess = _web_panel()
    before = len(panel.events_since(0))
    sess.on_live_event({"type": "party_invite",
                        "from": {"steam_id": "76561198000000009", "persona": "Ally"},
                        "code": "ZZZZ99"})
    cues = panel.events_since(0)[before:]
    assert [c for c in cues if c["type"] == "toast" and "Ally" in c["text"]], cues



def test_webui_every_screen_module_is_registered():
    """Frozen-build guard: the screens are imported EXPLICITLY (hub/webui/screens/__init__.py
    _SCREEN_MODULES), never discovered with pkgutil.iter_modules — which returns nothing in a
    PyInstaller bundle and once shipped an app with ZERO screens (raw i18n keys, dead verbs).

    So this asserts (1) _SCREEN_MODULES equals the *.py files on disk (a new screen that is not
    added to the list fails HERE, during the build's test run, not on the user's download), and
    (2) importing the package registers a snapshot slice + at least one verb for the screens that
    own them. If this passes from source, the frozen build has the same modules (static import)."""
    # Importing the package runs the explicit screen imports (its __init__), which register each
    # screen. Do NOT reload(): reload resets the registry dicts but the screen modules are already
    # in sys.modules, so their module-level register_* would NOT re-run — leaving the registry
    # empty and corrupting it for later tests.
    from hub.webui import screens as S

    on_disk = S._screen_files_on_disk()
    listed = set(S._SCREEN_MODULES)
    assert listed == on_disk, ("_SCREEN_MODULES is out of sync with the screen files on disk; "
                               "add new screens to _SCREEN_MODULES so the frozen build bundles them: "
                               f"only-on-disk={on_disk - listed}, only-listed={listed - on_disk}")

    # Every listed screen contributes a snapshot slice (competitive owns comp/party; the rest
    # namespace under their own name). The registry must be non-empty and cover all six.
    assert set(S.SCREEN_SNAPSHOTS.keys()) == listed, (
        "a screen module imported but did not register_snapshot: "
        f"missing={listed - set(S.SCREEN_SNAPSHOTS.keys())}")
    # And the action screens must have registered verbs (sign-in, search, install, ...). If this
    # dict is empty the app renders but nothing is clickable — the exact 2.0.1 regression.
    assert S.SCREEN_VERBS, "no screen verbs registered — the app would render but do nothing"
    for _needed in ("find_match", "sign_in", "create_party"):
        assert _needed in S.SCREEN_VERBS, f"core action verb {_needed!r} not registered"


# ---------------------------------------------------------------------------------------------
# The version gate (Sam, 2026-09-15): "just make it so people cant actually queue comp unless
# theyre on the most recent update" - and the half of the sentence that comes first, "stay in a
# competitive game if already in one, even if a new hub or gamemode update is pushed".
#
# The SERVER is the authority (server/live.cjs refuses /api/queue/join with a 426 and nothing
# else). These cover the hub's side: what it reports, what it refuses to even try, and - the
# part that matters most - everything it must go on doing while it is out of date.

# ---------------------------------------------------------------- the install gate
# Sam's friend installed Lights Out, opened Competitive before installing anything, and the web UI
# let him press Find match - the Tk tab's gate screen was the ONLY thing stopping that, and the web
# UI has no gate screen. hub/live.py and server/live.cjs had both written down "the tab cannot
# reach the queue without the gamemode" as a reason not to check, so all three layers were leaning
# on one Tk-only widget. These pin the one that now does the refusing.

def _unranked_session():
    """A live session on a hub with NOTHING installed - a fresh Lights Out, which is exactly how
    this was found."""
    s, panel = _live_session()
    panel.app = _FakeApp(installed={})
    return s, panel


def test_find_match_refuses_when_the_gamemode_is_not_installed():
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _unranked_session()
    assert s.gamemode_installed() is False

    s.find_match()
    assert s.phase == "idle", s.phase          # never even starts the integrity check
    assert s.error, "the refusal says why"
    assert s.client.calls.count("join") == 0, s.client.calls

    # ...and fixing it is enough: no restart, the very next press goes through (the pack's version
    # is read live, exactly as _versions does).
    panel.app.state["installed"] = {C.COMPETITIVE_MODE_ID: {"version": "1.0.0"}}
    s.error = ""
    s.find_match()
    assert s.phase == "queued", s.phase


def test_the_web_uis_find_button_is_dead_without_the_gamemode():
    """The snapshot the web hero renders from: `installed` puts the install button in Find match's
    place, and can_find kills the button either way."""
    from hub import competitive as C
    from hub.webui.screens.competitive import comp_snapshot
    s, panel = _unranked_session()
    snap = comp_snapshot(s, panel)
    assert snap["installed"] is False
    assert snap["can_find"] is False

    panel.app.state["installed"] = {C.COMPETITIVE_MODE_ID: {"version": "1.0.0"}}
    snap = comp_snapshot(s, panel)
    assert snap["installed"] is True
    assert snap["can_find"] is True


def test_a_missing_pack_is_worded_as_an_install_not_an_update():
    """The service's 426 for a pack that is ABSENT (live.cjs missing_mode). "Update it to 1.0.7"
    means nothing to somebody who has never installed it - but only when it is about YOU."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    mine = {"what": "mode", "missing_mode": True, "need_hub": "1.0.0", "need_mode": "1.0.7"}
    assert C.outdated_line(mine) == i18n.t("comp_gate_body", mode="Bodybomb 5v5")
    # somebody ELSE in the party is missing it: "install Bodybomb 5v5" is about the wrong person
    theirs = dict(mine, who="76561198000000002")
    assert C.outdated_line(theirs) != i18n.t("comp_gate_body", mode="Bodybomb 5v5")
    # an ordinary out-of-date pack is still worded as an update
    assert "1.0.7" in C.outdated_line({"what": "mode", "need_mode": "1.0.7"})


def test_a_party_mates_version_is_not_worded_as_yours():
    """The service shuts the queue for the WHOLE party when any member is behind and marks the
    refusal with `who`. Read as if it were ours, it told a player who is perfectly up to date to
    update - beside an Update button that would do nothing for them."""
    from hub import competitive as C
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()
    s.party = {"code": "ABCD", "leader_id": s.me["steam_id"],
               "members": [dict(s.me), {"steam_id": "76561198000000002", "name": "Wario"}]}
    body = {"ok": False, "outdated": True, "what": "mode", "need_hub": "2.3.38",
            "need_mode": "1.0.14", "who": "76561198000000002",
            "error": "Somebody in your party is not on the current version."}
    s.client = _FakeLiveClient((426, body))
    s.phase = "idle"
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "idle"
    assert "Wario" in s.error and "1.0.14" in s.error, s.error
    assert s.error != C.outdated_line(body), "our own sentence is about us, and this one is not"

    # the same body WITHOUT `who` is ours again, and reads exactly as it always did
    s.client = _FakeLiveClient((426, {k: v for k, v in body.items() if k != "who"}))
    s.phase = "idle"
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.error == C.outdated_line(body), s.error

    # every wording has a party twin, including the ABSENT-pack one
    assert "Wario" in C.party_outdated_line({"what": "mode", "missing_mode": True}, "Wario")
    assert C.party_outdated_line({"what": "mode", "missing_mode": True}, "Wario")         != i18n.t("comp_gate_body", mode="Bodybomb 5v5")
    both = C.party_outdated_line({"what": "both", "need_hub": "2.3.38", "need_mode": "1.0.14"},
                                 "Wario")
    assert "2.3.38" in both and "1.0.14" in both
    s.party = None


def _gated_session(hub_version="9.9.9", mode_version="9.9.9", installed="1.0.0",
                   offered_rules=None, built_rules=None):
    """A LiveSession whose catalogue advertises `hub_version` / `mode_version`, against an
    installed pack at `installed`. The hub's own version is whatever this build is.

    `offered_rules` is the catalogue's `rules_override` (what the service is publishing) and
    `built_rules` is what the installed pak was actually built with. They are the same thing in
    the ordinary case and differ exactly when a rules override has moved underneath a player."""
    from hub import competitive as C
    mode_entry = {"id": C.COMPETITIVE_MODE_ID, "title": "Bodybomb 5v5",
                  "version": mode_version, "pack_url": "https://example.invalid/p.zip",
                  "sha256": "x"}
    if offered_rules is not None:
        mode_entry["rules_override"] = dict(offered_rules)
    have = {"version": installed}
    if built_rules is not None:
        have["rules_override"] = dict(built_rules)
    s, panel = _live_session()
    panel.app = _FakeApp(
        installed={C.COMPETITIVE_MODE_ID: have},
        catalogue={"catalogue_version": 1,
                   "hub": {"version": hub_version,
                           "download_url": "https://example.invalid/setup.exe"},
                   "gamemodes": [mode_entry]})
    return s, panel


def test_a_rules_change_shuts_the_queue_even_at_the_same_version():
    """THE SAME VERSION BUILT WITH DIFFERENT RULES IS A DIFFERENT PAK.

    Sam, 2026-09-17, after four test matches lost to this: "whenever there is a gamemode change in
    anyway ... its guaranteed to require to update". The gate compared version numbers only, so a
    changed COMP_GAME_RULES_OVERRIDE - which is how the score limit, the round count and the team
    sizes reach the game at all - left the queue open on a pak playing something else. In his case
    the catalogue said max_players 10 and the pak had been built with 2, the value that empties the
    game's team array, so no match could ever end."""
    from hub import competitive as C
    from hub.version import HUB_VERSION

    live = {"score_limit": 2, "max_rounds": 12, "team_switch_interval": 6, "max_players": 10}
    stale = dict(live, max_players=2)

    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.15", installed="1.0.15",
                          offered_rules=live, built_rules=stale)
    need = s.update_needed()
    assert need, "a pak built with different rules is out of date"
    assert need["what"] == "mode", need
    assert need["mode_rules"] is True, "the version did not move - the rules did"

    # ...and the sentence must not read "update 1.0.15 to 1.0.15", which is what makes a hub look
    # broken to the person being asked.
    line = C.outdated_line(need)
    assert "1.0.15" not in line, line
    assert "settings" in line.lower(), line

    # the same rules on both sides is not an update
    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.15", installed="1.0.15",
                          offered_rules=live, built_rules=live)
    assert s.update_needed() is None

    # neither is production, where there is no override at all
    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.15", installed="1.0.15")
    assert s.update_needed() is None

    # a pak built with rules, against a catalogue that has since dropped them, is still stale
    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.15", installed="1.0.15",
                          built_rules=stale)
    need = s.update_needed()
    assert need and need["mode_rules"] is True, need

    # a NEWER version still reads as a version update, not a settings one, even when both moved
    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.16", installed="1.0.15",
                          offered_rules=live, built_rules=stale)
    need = s.update_needed()
    assert need["what"] == "mode" and need["mode_rules"] is False, need
    assert "1.0.16" in C.outdated_line(need)


def test_the_catalogue_is_refetched_often_enough_to_notice_a_rules_change():
    """A minute, not fifteen. Sam changed a Railway variable, pressed Update, and got a pak built
    from the value he had just replaced - the hub had not looked again yet. Both UIs must agree, or
    an update appears in one window and not the other."""
    from hub import app as A
    from hub.webui import shell as S
    assert A.CATALOGUE_REFRESH_MS == S.CATALOGUE_REFRESH_SECONDS * 1000, "the two UIs disagree"
    assert S.CATALOGUE_REFRESH_SECONDS <= 60, "too slow to notice a rules change mid-test"


def test_version_gate_spots_an_old_hub_and_an_old_pack():
    from hub import competitive as C
    from hub.version import HUB_VERSION

    s, _ = _gated_session(hub_version="9.9.9", mode_version="1.0.0", installed="1.0.0")
    need = s.update_needed()
    assert need and need["what"] == "hub", need
    assert need["hub"] == "9.9.9" and need["have_hub"] == HUB_VERSION, need

    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="2.0.0", installed="1.0.0")
    need = s.update_needed()
    assert need and need["what"] == "mode", need
    assert need["mode"] == "2.0.0" and need["have_mode"] == "1.0.0", need

    s, _ = _gated_session(hub_version="9.9.9", mode_version="2.0.0", installed="1.0.0")
    assert s.update_needed()["what"] == "both"

    # up to date: nothing to say
    s, _ = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.0", installed="1.0.0")
    assert s.update_needed() is None

    # AHEAD of the catalogue (a dev build) is not behind it
    s, _ = _gated_session(hub_version="0.0.1", mode_version="0.0.1", installed="1.0.0")
    assert s.update_needed() is None


def test_version_gate_says_nothing_without_a_catalogue():
    """Offline, or before the first fetch, the hub has nothing to compare against - and must NOT
    lock the queue on a guess. The service answers instead."""
    s, panel = _live_session()
    assert s.update_needed() is None, "no catalogue must never mean 'you are out of date'"
    panel.app = _FakeApp(catalogue=None)
    assert s.update_needed() is None


def test_version_gate_ignores_a_gamemode_that_is_not_installed():
    """Not installed is not out of date: the tab has its own Install screen for that, and calling
    it an update would send the player to a button that is not there."""
    from hub import competitive as C
    from hub.version import HUB_VERSION
    s, panel = _live_session()
    panel.app = _FakeApp(installed={}, catalogue={
        "catalogue_version": 1,
        "hub": {"version": HUB_VERSION, "download_url": "https://example.invalid/setup.exe"},
        "gamemodes": [{"id": C.COMPETITIVE_MODE_ID, "title": "BB5", "version": "5.0.0",
                       "pack_url": "https://example.invalid/p.zip", "sha256": "x"}]})
    assert s.update_needed() is None


def test_an_out_of_date_hub_never_reaches_the_queue():
    """Find match must not spend three seconds on the integrity check and THEN be refused."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _gated_session(hub_version="9.9.9", mode_version="1.0.0", installed="1.0.0")
    s.find_match()
    assert s.phase == "idle", s.phase
    assert "join" not in s.client.calls, "it must not have asked the service at all"
    assert s.error and "9.9.9" in s.error, s.error


def test_the_services_426_is_a_decision_not_a_failure():
    """The server refuses a join from an old build with 426. That must not be retried (no retry
    can make an old build current) and must read like the hub's own sentence."""
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session(join=(426, {"ok": False, "outdated": True, "what": "hub",
                                         "need_hub": "9.9.9", "need_mode": "1.0.6"}))
    s.find_match()
    for _ in range(3):
        s._check_next()
    assert s.phase == "idle", s.phase
    assert "9.9.9" in s.error, s.error
    # ...and it stays refused. Every timer the check phase left behind is fired; none of them
    # may turn into a second join, because no retry can make an old build current (the hub's
    # RETRY_STATUSES deliberately leaves 426 out).
    for _ in range(10):
        if not panel.pending:
            break
        panel.pump()
    assert s.client.calls.count("join") == 1, s.client.calls
    assert s.phase == "idle", s.phase


def test_being_out_of_date_never_touches_a_match_you_are_already_in():
    """THE WHOLE POINT. A release landing mid-match must not take the match away: every action
    that carries a match forward still works, and the tab is not thrown back to idle."""
    from hub import i18n
    i18n.set_language("en")
    s, _panel = _gated_session(hub_version="9.9.9", mode_version="2.0.0", installed="1.0.0")
    assert s.update_needed(), "this hub is behind on both counts"

    # ...and it is in a match.
    s.phase = "found"
    s.client.calls.clear()
    s.accept()
    assert "accept" in s.client.calls, "accept must still reach the service"
    assert s.phase == "found", s.phase

    for phase in ("lobby", "connecting", "live"):
        s.phase = phase
        assert s.locked_in(), phase
        assert s.update_needed(), "still behind, and still in the match"

    # the connect report, which is what stops the player being blamed for a no-show
    s.phase = "connecting"
    s.client.calls.clear()
    s.report_connected()
    assert "connected" in s.client.calls, s.client.calls


def test_the_hub_reports_its_versions_on_every_request():
    """The service reads two headers to decide who may queue - and for a party MEMBER the only
    request it ever sees is the stream, so they have to be on everything."""
    import urllib.request
    from hub import live as live_mod
    from hub.version import HUB_VERSION

    client = live_mod.LiveClient("tok", versions=lambda: {"hub": "1.2.3", "mode": "4.5.6"})
    req = urllib.request.Request("http://example.invalid/x")
    client._stamp(req)
    assert req.get_header("X-hub-version") == "1.2.3", req.header_items()
    assert req.get_header("X-mode-version") == "4.5.6", req.header_items()
    assert "LightsOut/" in (req.get_header("User-agent") or "")

    # no gamemode installed: the mode header is simply absent, which the server reads the same
    # way as an empty one
    client = live_mod.LiveClient("tok", versions=lambda: {"hub": "1.2.3", "mode": ""})
    req = urllib.request.Request("http://example.invalid/x")
    client._stamp(req)
    assert req.get_header("X-mode-version") is None

    # a versions callable that throws must not be what stops the hub talking to the service
    def boom():
        raise RuntimeError("nope")
    client = live_mod.LiveClient("tok", versions=boom)
    req = urllib.request.Request("http://example.invalid/x")
    client._stamp(req)
    assert req.get_header("X-hub-version") == HUB_VERSION


def test_live_session_reports_the_installed_pack_version():
    from hub import competitive as C
    from hub.version import HUB_VERSION
    s, panel = _live_session()
    panel.app = _FakeApp(installed={C.COMPETITIVE_MODE_ID: {"version": "1.0.6"}})
    assert s._versions() == {"hub": HUB_VERSION, "mode": "1.0.6", "BB5": "1.0.6", "BB1": ""}
    # updating the pack under a running hub changes the very next request, with no restart
    panel.app.state["installed"][C.COMPETITIVE_MODE_ID]["version"] = "1.0.7"
    assert s._versions()["mode"] == "1.0.7"


def test_a_forced_update_waits_for_the_match_to_end():
    """A forced update is a blocking overlay. Mid-match it would take away accept, the veto and
    "I am in the game" - and make a no-show of somebody for a release they did not push."""
    from hub.webui.snapshot import _update
    from hub.version import HUB_VERSION

    class _P:
        def __init__(self, phase):
            self.app = _FakeApp(catalogue={
                "hub": {"version": "9.9.9", "required": True,
                        "download_url": "https://example.invalid/setup.exe"}})
            self.session = _types.SimpleNamespace(
                locked_in=lambda: phase in ("found", "lobby", "connecting", "live"))

    import hub.update as update_mod
    real = update_mod.own_exe
    update_mod.own_exe = lambda: "C:/fake/LightsOut.exe"       # pretend we are the installed exe
    try:
        idle = _update(_P("idle"))
        assert idle["available"] and idle["forced"], idle
        assert not idle["forced_deferred"]

        playing = _update(_P("connecting"))
        assert playing["available"], "the offer still stands..."
        assert not playing["forced"], "...but it must not take the window from a live match"
        assert playing["forced_deferred"], "and it says it is only being held back"
    finally:
        update_mod.own_exe = real
    assert HUB_VERSION                     # the comparison above is against this build


def test_the_idle_screen_disables_find_match_when_behind():
    """can_find is what the button reads, and the card next to it carries the finished sentence
    rather than the JS assembling one."""
    from hub import i18n
    from hub.webui.screens.competitive import comp_snapshot
    i18n.set_language("en")
    s, panel = _gated_session(hub_version="9.9.9", mode_version="1.0.0", installed="1.0.0")
    snap = comp_snapshot(s, panel)
    assert snap["can_find"] is False
    assert snap["outdated"] and snap["outdated"]["what"] == "hub"
    assert "9.9.9" in snap["outdated"]["line"], snap["outdated"]

    from hub.version import HUB_VERSION
    s, panel = _gated_session(hub_version=HUB_VERSION, mode_version="1.0.0", installed="1.0.0")
    snap = comp_snapshot(s, panel)
    assert snap["outdated"] is None
    assert snap["can_find"] is True



def test_find_match_does_nothing_while_bodycam_is_open():
    """Pressing Find match with the game up queues nothing and says why for three seconds.

    The hub opens Bodycam ITSELF when the match is found and puts the player straight into the
    lobby; a game that was already running has spent its one BeginPlay before the match existed
    and cannot be put into one (that is what `game_was_open` is for on the connect screen). The
    screen used to carry a paragraph asking the player to work this out and remember it. This is
    the rule enforced instead: the button is live, the press is a no-op, and the cue is the whole
    message - no error line left sitting under the button afterwards.
    """
    from hub import competitive as C
    from hub import game as game_mod
    from hub import i18n
    i18n.set_language("en")
    panel, s = _web_panel()
    real = game_mod.game_running
    game_mod.game_running = lambda: True
    try:
        before = len(panel.events_since(0))
        s.find_match()
        assert s.phase == "idle", s.phase                  # not even the integrity check
        assert s.client.calls.count("join") == 0, s.client.calls
        assert not s.error, s.error                        # the toast is the whole message
        cues = [c for c in panel.events_since(0)[before:] if c["type"] == "toast"]
        assert len(cues) == 1, cues
        assert cues[0]["text"] == i18n.STRINGS["en"]["comp_close_game_queue"], cues
        assert cues[0]["ms"] == C.CLOSE_GAME_CUE_MS == 3000, cues

        # close the game and the very same press goes through: this gates, it does not latch
        game_mod.game_running = lambda: False
        s.find_match()
        assert s.phase == "queued", s.phase
    finally:
        game_mod.game_running = real


def test_a_panel_without_toasts_still_says_close_your_game():
    """The classic Tk window has no cue channel, so the sentence goes on its error line and is
    taken back off on a one-shot. Without this its Find match button would look broken."""
    from hub import competitive as C
    from hub import game as game_mod
    from hub import i18n
    i18n.set_language("en")
    s, panel = _live_session()                 # the Tk-shaped fake panel: no push_event
    assert not hasattr(panel, "push_event")
    real = game_mod.game_running
    game_mod.game_running = lambda: True
    try:
        s.find_match()
        assert s.phase == "idle", s.phase
        assert s.error == i18n.STRINGS["en"]["comp_close_game_queue"], s.error
        assert panel.pending, "the line must be scheduled to come back off"
        while panel.pending:
            panel.pump()
        assert not s.error, s.error
        assert C.CLOSE_GAME_CUE_MS == 3000
    finally:
        game_mod.game_running = real


# ------------------------------------------------------------------ runner
def main():
    for fn in [
        test_webui_every_screen_module_is_registered,
        test_party_invite_slice_and_verbs,
        test_friends_invite_makes_a_party_when_there_is_none,
        test_friends_invite_reports_a_failure_and_never_lies_about_it,
        test_party_invite_toast_reaches_any_screen,
        test_find_match_does_nothing_while_bodycam_is_open,
        test_a_panel_without_toasts_still_says_close_your_game,
        test_friends_slice_and_verbs,
        test_plan_truth_table,
        test_state_roundtrip,
        test_an_installed_rules_override_survives_a_restart,
        test_sound_setting_survives_a_restart,
        test_state_reconcile,
        test_is_game_dir,
        test_fetch_and_validate_catalogue,
        test_version_newer,
        test_ensure_pack_ok,
        test_ensure_pack_bad_sha,
        test_ensure_pack_path_traversal,
        test_apply_empty_removes_pak,
        test_apply_install_with_fake_builder,
        test_apply_reports_a_real_bar_through_the_build,
        test_apply_failure_keeps_old_pak,
        test_apply_rejects_bad_game_dir,
        test_apply_unknown_id,
        test_tools_dir_and_builder_path,
        test_builder_reports_its_steps_and_packs_the_same_bytes,
        test_app_imports_without_opening_a_window,
        test_ui_constructs_under_xvfb,
        test_i18n_tables_complete,
        test_display_title_and_ops_messages_follow_language,
        test_language_dialog_under_xvfb,
        test_local_mode_helpers,
        test_tray_icon_and_close_to_tray,
        test_only_one_hub_runs_at_a_time,
        test_icon_assets,
        test_mandatory_update_screen,
        test_update_strip_does_not_block_the_hub,
        test_installer_command,
        test_candidate_dirs_prefers_state_updates,
        test_dest_path_keeps_setup_basename,
        test_clean_old_versions_empties_updates_dir,
        test_launch_starts_installer_or_legacy_exe,
        test_competitive_mock_session_flow,
        test_competitive_party_codes,
        test_steam_signin_wait_for,
        test_steam_account_adoption,
        test_avatar_url_allowlist,
        test_live_session_events,
        test_live_accept_other_player_does_not_block_me,
        test_live_accept_is_idempotent,
        test_match_found_resets_i_accepted,
        test_live_session_retries_a_transient_queue_failure,
        test_live_session_requeues_after_a_reconnect,
        test_live_session_reports_a_queue_failure,
        test_live_queue_tick_starts_when_queued_event_precedes_join_result,
        test_live_queue_position_update_does_not_reset_or_double_the_tick,
        test_live_join_retry_treats_a_queued_event_as_success,
        test_live_reconnect_during_found_recovers_the_accept_screen,
        test_render_signature_ignores_live_counters,
        test_avatar_download_forces_a_full_rebuild,
        test_reconnect_in_lobby_does_not_restart_the_client_local_lobby,
        test_resumed_lobby_restores_the_real_stage_from_the_server,
        test_resumed_live_match_comes_back_off_the_server,
        test_reopened_live_match_keeps_game_cleanup_without_relaunching,
        test_resumed_live_match_without_bodycam_does_not_open_or_claim_a_game,
        test_resumed_connect_window_names_the_real_host,
        test_rejoin_grace_releases_a_match_the_service_no_longer_has,
        test_accept_tick_clears_exactly_once_at_zero_and_can_be_re_armed,
        test_competitive_connect_window,
        test_competitive_no_show_penalty,
        test_competitive_no_show_ladder,
        test_competitive_match_found_cue,
        test_competitive_phase_watchdog,
        test_competitive_signout_is_locked_during_a_match,
        test_competitive_start_lobby_survives_any_roster,
        test_live_session_connect_window,
        test_live_session_no_show_penalty,
        test_live_session_watchdog_tells_the_server,
        test_live_session_party_is_server_driven,
        test_live_session_no_mock_friend_in_live,
        test_live_session_party_error,
        test_party_membership_is_in_render_signature,
        test_webui_snapshot_serializes_live_session,
        test_webui_snapshot_is_the_on_change_output,
        test_webui_bridge_verbs_map_to_session,
        test_webui_bridge_pushes_state_to_the_window,
        test_webui_js_references_only_real_i18n_keys,
        test_webui_a_modal_is_cleared_by_the_next_render,
        test_webui_every_modal_has_a_visible_way_out,
        test_webui_the_report_box_opens_on_whatever_screen_you_pressed_report_on,
        test_webui_scales_itself_to_the_window_and_never_sizes_in_viewport_units,
        test_webui_js_constant_tables_are_declared_before_they_are_read,
        test_webui_shell_import_does_not_require_pywebview,
        test_webui_x_hides_to_the_tray,
        test_webui_snapshot_carries_the_signing_in_state,
        test_webui_on_change_dedups_identical_pushes,
        test_webui_refresh_tick_keeps_the_screens_live,
        test_webui_push_never_blocks_the_ui_thread,
        test_game_running_survives_tasklists_truncated_image_name,
        test_game_running_cached_never_blocks_and_then_settles,
        test_snapshot_does_not_block_on_tasklist,
        test_webui_scheduler_fires_in_due_order_and_stop_halts,
        test_exe_version_is_not_behind_the_catalogue,
        test_competitive_strings_exist_in_every_language,
        test_match_history_session,
        test_match_history_rows_read_correctly,
        test_game_opens_host_first_then_everybody_else,
        test_a_replayed_match_does_not_reopen_the_game,
        test_game_closes_only_after_the_hub_registers_the_match_complete,
        test_a_losing_side_and_a_voided_match_still_close_the_game,
        test_a_voided_match_closes_the_game_too,
        test_game_close_never_touches_a_game_the_hub_did_not_open,
        test_a_close_armed_for_one_match_can_never_reach_the_next_game,
        test_close_game_asks_before_it_kills,
        test_connect_reports_the_lobby_for_the_record,
        test_profile_stats,
        test_competitive_tab_under_xvfb,
        test_match_history_under_xvfb,
        test_profile_under_xvfb,
        test_version_gate_spots_an_old_hub_and_an_old_pack,
        test_version_gate_says_nothing_without_a_catalogue,
        test_version_gate_ignores_a_gamemode_that_is_not_installed,
        test_an_out_of_date_hub_never_reaches_the_queue,
        test_the_services_426_is_a_decision_not_a_failure,
        test_being_out_of_date_never_touches_a_match_you_are_already_in,
        test_the_hub_reports_its_versions_on_every_request,
        test_live_session_reports_the_installed_pack_version,
        test_a_forced_update_waits_for_the_match_to_end,
        test_the_idle_screen_disables_find_match_when_behind,
    ]:
        test(fn)

    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)

    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
