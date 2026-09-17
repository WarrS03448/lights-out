#!/usr/bin/env python3.12
"""The per-match lobby pak: retargeting, level resolution, install and cleanup.
Run:  python3.12 tests/test_lobbypak.py

WHAT THIS IS GUARDING. The host's game opens whatever level is baked into the lobby pak, and it does
so unconditionally at BeginPlay. Two things therefore have to be exactly right or a player ends up
somewhere nobody else is:

  1. THE BYTES. A retargeted pak must differ from the one measured auto-hosting (chlobby-28,
     2026-09-15) in nothing but two FName entries. The identity case proves it: retargeting the seed
     to what it already says must reproduce it byte-for-byte (see lobbypak.SEED_SHA256). If that ever stops
     holding, the retarget is moving something it should not and no variant can be trusted.
  2. THE NAME. gamemodes/*/manifest.json carries DISPLAY names; the cooked levels are named
     differently for five of them, and two of those five are in the competitive veto pool -
     "Pool" is BB5_PublicPool and "Russian" is BB5_RussianBuilding. Resolving off the manifest would
     target a level that does not exist in roughly one ranked match in three, and OpenLevel on a
     missing level tears the world down BEFORE the load fails. So resolution reads the installed
     gamemode pak, and this test pins both mismatches by name.

Everything runs against a FAKE game directory under tempfile - the real install is never written to.
Skips (rather than fails) when the machine has no gamemode pak to read levels from, so it is honest
on a checkout that has never installed one.
"""
import os
import pathlib
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hub import lobbypak                                    # noqa: E402
from hub import version                                     # noqa: E402
from hub.competitive import (COMPETITIVE_EXCLUDED_MAPS,      # noqa: E402
                             DEFAULT_MAPS, HOST_GAMEMODE_ID)

FAILED = []


def need(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILED.append(what)


def real_gamemodes_pak():
    """The installed gamemode pak, or "" - it is the only source of the cooked level list."""
    from hub import game as game_mod
    found = game_mod.find_game_dir()
    if not found:
        return ""
    pak = os.path.join(game_mod.mods_dir(found, create=False), version.PAK_NAME)
    return pak if os.path.isfile(pak) else ""


def fake_game_dir(tmp, gamemodes_pak):
    """A directory shaped like a Bodycam install: <game>/Bodycam/Content/Paks/~mods with the real
    gamemode pak copied in, so available_levels() has something true to read."""
    mods = os.path.join(tmp, "Bodycam", "Content", "Paks", "~mods")
    os.makedirs(mods, exist_ok=True)
    shutil.copyfile(gamemodes_pak, os.path.join(mods, version.PAK_NAME))
    return tmp


print("--- the seed ---")
seed = lobbypak.seed_path()
need(bool(seed), "a pristine lobby pak seed is on this machine")
if seed:
    need(lobbypak.seed_digest() == lobbypak.SEED_SHA256,
         "the seed is the pak measured auto-hosting (sha256 %s...)" % lobbypak.SEED_SHA256[:8])

gm_pak = real_gamemodes_pak()
if not gm_pak or not seed:
    print("\nSKIPPED: needs an installed CommunityGamemodes_P.pak and a lobby seed")
    sys.exit(0)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools" / "pak"))
import retarget_lobby                                       # noqa: E402

try:
    print("\n--- retargeting the bytes ---")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "identity.pak")
        rep = retarget_lobby.retarget_pak(seed, out, retarget_lobby.SEED_PKG, retarget_lobby.SEED_NAME)
        need(rep["sha256"] == lobbypak.SEED_SHA256,
             "retargeting the seed to its own level reproduces it byte-for-byte")
        need(len(rep["nameconsts"]) == retarget_lobby.HOST_NAMECONST_SITES,
             "%d NameConst sites reach the two renamed FName entries"
             % retarget_lobby.HOST_NAMECONST_SITES)

        levels = retarget_lobby.available_levels(gm_pak)
        need(len(levels) > 0, "the installed gamemode pak lists cooked levels (%d)" % len(levels))

        built = 0
        for name, pkg in levels.items():
            v = os.path.join(td, "v_%s.pak" % name)
            r = retarget_lobby.retarget_pak(seed, v, pkg, name, allowed=levels)
            built += r["sha256"] != ""
        need(built == len(levels), "every installed level produces a variant (%d)" % built)

        # A target that is not installed must be refused rather than built: this is the guard
        # standing between a hub bug and a host whose world is torn down by OpenLevel.
        refused = False
        try:
            retarget_lobby.retarget_pak(seed, os.path.join(td, "nope.pak"),
                                        "/Game/GM_Maps/Community/BB5/BB5_Nowhere",
                                        "BB5_Nowhere", allowed=levels)
        except KeyError:
            refused = True
        need(refused, "a level that is not installed is refused, not built")

    print("\n--- the JOINER pak, which is a different class and a different shape ---")
    join_seed = lobbypak.seed_path(role="join")
    if not join_seed:
        print("  SKIPPED: no joiner seed on this machine")
    else:
        with tempfile.TemporaryDirectory() as td:
            levels = retarget_lobby.available_levels(gm_pak)
            pkg, name = next(iter(levels.items()))[::-1]
            token = "chm-0123456789abcdef"

            # IT MUST BUILD AT ALL. The ship-safety file list knew only GM_CHLobby, so this raised
            # ValueError for every joiner pak ever built - and hub/lobbypak.prepare() turned that
            # into a silently removed lobby pak and a player in the range, with nothing logged.
            out = os.path.join(td, "join.pak")
            rep = retarget_lobby.retarget_pak(join_seed, out, pkg, name,
                                              allowed=levels, token=token)
            need(rep["sha256"] != "", "the joiner seed retargets at all")
            need(rep["gm"] == "GM_CHJoin.uasset",
                 "and it is recognised as the joiner's class, not the host's")

            # IT CARRIES NO MAP, so it must not be retargeted or tripwired for one. GM_Host does the
            # search and the travel; the joiner class never calls OpenLevel.
            need(rep["level_retargeted"] is False,
                 "the joiner has no map literal, so no level rewrite is attempted")
            need(rep["nameconsts"] == [],
                 "and no NameConst tripwire, which would demand a name it never mentions")

            # THE TOKEN IS THE WHOLE POINT. It is what pairs this joiner with its host's lobby
            # `Name`; a pak that shipped the placeholder would find nobody and look like a bug.
            need(rep["token_stamped"] is True, "the per-match token is stamped into the joiner")
            with open(out, "rb") as fh:
                blob = fh.read()
            need(blob.count(token.encode()) == 1, "the new token is in the pak exactly once")
            need(blob.count(retarget_lobby.SEED_JOIN_TOKEN.encode()) == 0,
                 "and the placeholder token is gone, so it cannot pair with a stale host")

            # THE HOST HALF IS UNCHANGED by any of this - the same call still retargets its map.
            hout = os.path.join(td, "host.pak")
            hrep = retarget_lobby.retarget_pak(seed, hout, pkg, name, allowed=levels, token=token)
            need(hrep["level_retargeted"] is True, "the host still retargets its map")
            need(len(hrep["nameconsts"]) == retarget_lobby.HOST_NAMECONST_SITES,
                 "and still proves it with %d NameConst sites"
                 % retarget_lobby.HOST_NAMECONST_SITES)
            need(hrep["token_stamped"] is True, "and carries the SAME token as the joiner")

    print("\n--- resolving the veto's map names ---")
    with tempfile.TemporaryDirectory() as td:
        game = fake_game_dir(td, gm_pak)
        pool = [m for m in DEFAULT_MAPS if m not in COMPETITIVE_EXCLUDED_MAPS]
        unresolved = [m for m in pool if not lobbypak.resolve(game, HOST_GAMEMODE_ID, m)]
        need(not unresolved, "every competitive pool map resolves to an installed level (%s)"
             % (unresolved or "none missing"))
        need(lobbypak.resolve(game, HOST_GAMEMODE_ID, "Pool")[1] == "BB5_PublicPool",
             'the display name "Pool" resolves to BB5_PublicPool, not BB5_Pool')
        need(lobbypak.resolve(game, HOST_GAMEMODE_ID, "Russian")[1] == "BB5_RussianBuilding",
             'the display name "Russian" resolves to BB5_RussianBuilding')
        need(lobbypak.resolve(game, HOST_GAMEMODE_ID, "Atlantis") is None,
             "a map this player does not have resolves to None, not a guess")

    print("\n--- install and cleanup, on a fake install ---")
    with tempfile.TemporaryDirectory() as td:
        game = fake_game_dir(td, gm_pak)
        need(not lobbypak.installed(game), "nothing of ours is installed to begin with")

        level = lobbypak.prepare(game, HOST_GAMEMODE_ID, "Rome")
        need(level == "BB5_Rome", "prepare() for Rome reports BB5_Rome")
        need(lobbypak.installed(game), "and the pak is in ~mods")

        # Read through a context manager and let go of the handle. On Windows a live PakFile on the
        # installed pak blocks the very os.replace the next match performs (WinError 32) - which is
        # how this test caught a real leak in hub/lobbypak.py, not just in itself.
        from pakfile import PakFile
        with PakFile(lobbypak.installed_path(game)) as got:
            names = set(got.files)
            ua = got.read(retarget_lobby.CHLOBBY_UASSET).decode("latin-1")
        need(names == retarget_lobby.EXPECTED_FILES,
             "the installed pak still carries exactly the four shippable files")
        need("BB5_Rome" in ua and "BB5_Hospital" not in ua,
             "and it names BB5_Rome, with the seed's BB5_Hospital gone")

        # Switching maps between matches must not leave the old one behind.
        level = lobbypak.prepare(game, HOST_GAMEMODE_ID, "Pool")
        need(level == "BB5_PublicPool", "preparing a second match retargets to BB5_PublicPool")
        with PakFile(lobbypak.installed_path(game)) as got:
            ua = got.read(retarget_lobby.CHLOBBY_UASSET).decode("latin-1")
        need("BB5_PublicPool" in ua and "BB5_Rome" not in ua, "and the previous map is gone")

        # A map we cannot resolve must REMOVE the pak, never leave the last one in place: a stale
        # pak sends the host's next cold launch into a match that is over.
        level = lobbypak.prepare(game, HOST_GAMEMODE_ID, "Atlantis")
        need(level == "", "an unresolvable map reports failure")
        need(not lobbypak.installed(game), "and removes the pak rather than leaving a stale one")

        need(lobbypak.remove(game) and not lobbypak.installed(game),
             "remove() is idempotent when there is nothing to remove")

except Exception:
    traceback.print_exc()
    FAILED.append("unhandled exception")

print()
if FAILED:
    print("LOBBY PAK TEST FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("   -", f)
    sys.exit(1)
print("LOBBY PAK TEST PASSED")
