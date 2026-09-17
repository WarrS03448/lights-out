#!/usr/bin/env python3
"""Override the lobby's GameMode so OUR code runs while a player stands in the shooting range.

WHY (docs/autojoin.md). Competitive needs to put a player into a match without asking anybody to
add anybody as a friend. The route is the game's own: `FindLobbies` on a `CH_MATCH` attribute we
already know how to stamp, then `JoinLobby`. The only thing missing is somewhere for that code to
RUN on the joiner's machine, because a GameMode only exists inside a match and the joiner is on
the menu - which, since the update that spawns you into the shooting range, is itself a level:

    /Game/Map/LobbyHost/LobbyHost   909 bytes. Streams /Game/Map/Lobby and its World Settings
                                    set GM_Host_C. Hosting is OpenLevel(..., "listen"), so EVERY
                                    player runs this GameMode on their own machine.

Repointing that one property is the whole change. `UGameInstance::CreateGameModeForURL` takes
World Settings' DefaultGameMode ahead of `?game=`, the prefix list and the global default, and
Bodycam never passes `?game=`.

READ THIS BEFORE RUNNING IT ON YOURSELF
---------------------------------------
Everything the project has shipped so far is ADDITIVE: BB5 is a mode you choose, and if it broke,
only that mode broke. This is the first thing that runs on every launch. Get it wrong and the game
boots into a broken lobby.

Three things make that recoverable rather than frightening:
  * It is its own pak. Delete `~mods/CommunityLobby_P.pak` and the game is stock again.
  * `--null` mode changes NOTHING about behaviour: it rebuilds the level pointing at the very same
    GM_Host_C it already pointed at. Run that first. If the game still boots normally, the override
    pipeline works and only then is it worth pointing somewhere new.
  * The class we eventually point at is a CHILD of GM_Host_C, so everything the lobby does today
    still happens. We add a BeginPlay; we reimplement nothing.

Nothing of the game is redistributed: the stock level is read out of the PLAYER'S OWN paks at
build time, four bytes are changed, and the result goes into their own ~mods. Same rule the
gamemode builder follows.

Usage
-----
    python3 build_lobby_override.py --paks "<...>/Bodycam/Content/Paks" --work /tmp/lw \
        --out CommunityLobby_P.pak --null
    python3 build_lobby_override.py --paks ... --work ... --out ... \
        --class-pkg /Game/GM/Gamemode/GM_CHLobby --class GM_CHLobby_C
    ... --dry-run     inspect and report, write nothing
"""
import argparse
import hashlib
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib                                    # noqa: E402
from pkgedit import CookedPackage                # noqa: E402
from build_gamemode import Builder, add_dep, verify, C   # noqa: E402


def check_cook_is_current(cooked):
    """Refuse a cook that is OLDER than the last Blueprint run.

    `collect_cooked.bat` copies the mirror's blueprints_summary.txt next to the cook output, so the
    two copies are identical whenever the cook is current. If they differ, somebody ran
    2_make_blueprints.bat and then built a pak WITHOUT running 3_cook.bat - which produces a pak of
    the previous graph that looks entirely healthy and silently tests the wrong build. That happened
    three times on 2026-09-14 before this check existed; each one cost a launch to notice.

    Compares CONTENT rather than timestamps, so clock skew and copy order cannot confuse it."""
    cooked_summary = os.path.join(cooked, "blueprints_summary.txt")
    mirror = os.path.dirname(os.path.abspath(cooked))
    live_summary = os.path.join(mirror, "Bodycam", "blueprints_summary.txt")
    if not (os.path.exists(cooked_summary) and os.path.exists(live_summary)):
        return                                   # nothing to compare; the verdict check still runs
    a = open(cooked_summary, "rb").read()
    b = open(live_summary, "rb").read()
    if a != b:
        raise SystemExit(
            "REFUSING TO BUILD: the cook is older than the last Blueprint run.\n"
            "  cooked : %s\n  live   : %s\n"
            "  Run 3_cook.bat (it collects the output into mirror\\cooked), then build the pak again.\n"
            "  Building now would pack the PREVIOUS graph and look perfectly healthy doing it."
            % (cooked_summary, live_summary))
    print("  cook is current with the last Blueprint run")


def check_cook_verdict(cooked):
    """Delegate to the project's own check rather than keeping a second copy.

    Mine read the LAST non-empty line, but the summary ends with a log line AFTER the verdict, so a
    perfectly good cook was rejected (2026-09-14). `Builder.check_cook_verdict` searches the whole
    text for RESULT: OK *and* rejects any line starting with ERROR - graph link errors still compile
    CLEAN, so the verdict rather than the compile status is what decides."""
    Builder.check_cook_verdict(cooked)
    print("  cook verdict: RESULT: OK, no ERROR lines")


LOBBY_PKG = "/Game/Map/LobbyHost/LobbyHost"
STOCK_CLASS = "GM_Host_C"                        # what it points at today
STOCK_CLASS_PKG = "/Game/GM/GM_Host"


def find_world_settings(pk):
    """The WorldSettings export. The gamemode builder takes the last export; here we look it up by
    name, because this level is not one of the map clones and its export order is its own."""
    for i, e in enumerate(pk.exports):
        if pk.names[e["name_idx"]][0] == "WorldSettings":
            return i, e
    raise SystemExit("no WorldSettings export in %s - the level is not shaped as expected" % LOBBY_PKG)


# pkgedit stores an import as [cp.idx, cp.num, cn.idx, cn.num, outer, on.idx, on.num, optional].
# The CLASS name is index 2; index 3 is that name's NUMBER field and is almost always 0, so reading
# it as a name index makes every import look like it has the class `names[0]` (2026-09-14).
IMP_CLASS_NAME, IMP_OBJECT_NAME = 2, 5


def import_pairs(pk):
    """[(class name, object name, package index)] for every import, for matching and for errors."""
    names = [n[0] for n in pk.names]
    return [(names[imp[IMP_CLASS_NAME]], names[imp[IMP_OBJECT_NAME]], -(i + 1))
            for i, imp in enumerate(pk.imports)]


def find_class_import(pk, object_name):
    """The import index (a NEGATIVE package index) of a BlueprintGeneratedClass by object name."""
    for cls, obj, idx in import_pairs(pk):
        if obj == object_name and cls == "BlueprintGeneratedClass":
            return idx
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paks", required=True, help="the player's Bodycam/Content/Paks folder")
    ap.add_argument("--work", required=True, help="scratch dir (stock files are cached here)")
    ap.add_argument("--out", help="pak to write")
    ap.add_argument("--class-pkg", default=None, help="package of the GameMode to point at")
    ap.add_argument("--class", dest="cls", default=None, help="its class name, e.g. GM_CHLobby_C")
    ap.add_argument("--cooked", default=None,
                    help="<mirror>/Saved/Cooked/Windows - when given, the cooked GameMode package "
                         "named by --class-pkg is packed alongside the level")
    ap.add_argument("--null", action="store_true",
                    help="rebuild pointing at the SAME class: proves the override boots, changes nothing")
    ap.add_argument("--dry-run", action="store_true", help="report what was found, write nothing")
    ap.add_argument("--inspect-host", action="store_true",
                    help="dump GM_Host's cooked class (super, own properties in order, functions) "
                         "and write nothing. This is what decides how a child class can be built.")
    a = ap.parse_args()

    if a.inspect_host:
        b = Builder(a.paks, a.work, cooked=None, allow_unverified_cook=True)
        ua, ue = b.stock_pkg(STOCK_CLASS_PKG)
        print("GM_Host cooked package:\n  %s\n  %s\n" % (ua, ue))
        import classinfo
        classinfo.class_info(ua, ue)
        print("\nThe SUPER line is the parent our child would have to declare, and the property "
              "count\nis what a cooked CDO is written against - both decide whether a child is "
              "buildable\nat all (docs/autojoin.md section 6, step 3).")
        return 0

    if not a.null and not a.dry_run and not (a.class_pkg and a.cls):
        ap.error("give --class-pkg and --class, or --null")
    if not a.dry_run and not a.out:
        ap.error("--out is required unless --dry-run")

    b = Builder(a.paks, a.work, cooked=None, allow_unverified_cook=True)
    ua, ue = b.stock_pkg(LOBBY_PKG, ".umap")
    pk = CookedPackage.load(ua, lenient=True)
    u = bytearray(open(ue, "rb").read())
    names = [n[0] for n in pk.names]

    ws_i, ws = find_world_settings(pk)
    off = ws["serial_offset"] - pk.total_header_size
    body = bytes(u[off:off + ws["serial_size"]])
    print("LobbyHost: %d names, %d imports, %d exports; WorldSettings is export %d, %d bytes"
          % (len(names), len(pk.imports), len(pk.exports), ws_i, len(body)))
    print("  WorldSettings unversioned header: %s" % body[:8].hex(" "))

    old_idx = find_class_import(pk, STOCK_CLASS)
    if old_idx is None:
        raise SystemExit(
            "no %s BlueprintGeneratedClass import in the level.\n"
            "  imports present (class -> object):\n%s\n"
            "  The lobby does not select its GameMode the way docs/autojoin.md records, so STOP and\n"
            "  re-read the level rather than guessing at bytes." %
            (STOCK_CLASS, "\n".join("    %-28s %s" % (c, o) for c, o, _ in import_pairs(pk))))
    print("  %s is import index %d" % (STOCK_CLASS, old_idx))

    # Layout-independent: find the int32 that IS that import index inside the WorldSettings data,
    # rather than pattern-matching an unversioned header we have not seen. If it is not there
    # exactly once, something is different from what we believe and we must not write anything.
    needle = struct.pack("<i", old_idx)
    hits = [i for i in range(0, len(body) - 3) if body[i:i + 4] == needle]
    print("  DefaultGameMode candidates in WorldSettings data: %s" % hits)
    if len(hits) != 1:
        raise SystemExit("expected exactly one reference to %s in WorldSettings, found %d. STOP."
                         % (STOCK_CLASS, len(hits)))
    at = hits[0]

    if a.dry_run:
        print("\ndry run: nothing written. The DefaultGameMode int32 sits at WorldSettings+%d." % at)
        return 0

    if a.null:
        new_idx, label = old_idx, "%s (unchanged)" % STOCK_CLASS
    else:
        p_imp = pk.add_import("/Script/CoreUObject", "Package", 0, a.class_pkg)
        new_idx = pk.add_import("/Script/Engine", "BlueprintGeneratedClass", p_imp, a.cls)
        pk.add_import(a.class_pkg, a.cls, p_imp, "Default__" + a.cls)
        add_dep(pk, ws_i, new_idx, "create_before_ser")
        label = "%s (%s)" % (a.cls, a.class_pkg)
        # adding imports moves the header, but the export DATA is untouched in size, so the only
        # edit to `u` is the four bytes below
        off = pk.exports[ws_i]["serial_offset"] - pk.total_header_size

    struct.pack_into("<i", u, off + at, new_idx)
    print("  DefaultGameMode -> %s (import %d)" % (label, new_idx))

    header = pk.serialize()
    verify(header, bytes(u), "LobbyHost override", a.work)


    # A null build must come out BYTE-IDENTICAL to what we read. If it does not, the difference is
    # our serialiser rather than the override, and installing it would test the wrong thing.
    if a.null:
        original = open(ua, "rb").read()
        if header == original:
            print("  null build is byte-identical to the stock level (%d B)" % len(header))
        else:
            print("  WARNING: null build differs from stock: %d B vs %d B" % (len(header), len(original)))
            first = next((i for i in range(min(len(header), len(original)))
                          if header[i] != original[i]), min(len(header), len(original)))
            print("           first difference at byte %d" % first)
            print("           The round trip is not clean, so a boot failure would tell us nothing")
            print("           about the override itself. Do not install this; send the output back.")
    files = {C + LOBBY_PKG[len("/Game/"):] + ".umap": header,
             C + LOBBY_PKG[len("/Game/"):] + ".uexp": bytes(u)}

    if a.cooked:
        check_cook_verdict(a.cooked)
        check_cook_is_current(a.cooked)
        cb = Builder(a.paks, a.work, cooked=a.cooked, allow_unverified_cook=True)
        cua, cue = cb.cooked_pkg(a.class_pkg)
        for src, ext in ((cua, ".uasset"), (cue, ".uexp")):
            if not os.path.exists(src):
                raise SystemExit("cooked class not found: %s\n"
                                 "  Run 2_make_blueprints.bat then 3_cook.bat in the mirror first."
                                 % src)
            files[C + a.class_pkg[len("/Game/"):] + ext] = open(src, "rb").read()
        print("  packed cooked class %s (%d + %d B)"
              % (a.class_pkg, os.path.getsize(cua), os.path.getsize(cue)))

    # The stand-in parent lives at the GAME'S path so the cooked super import names it. Shipping it
    # would REPLACE the real GM_Host and take the lobby's Steam login, session settings and
    # privilege checks with it. Never let it into the pak.
    forbidden = [rel for rel in files if "/GM/GM_Host." in "/" + rel]
    if forbidden:
        raise SystemExit("refusing to ship the GM_Host stand-in: %s\n"
                         "  That file is a build-time stand-in only (docs/autojoin.md)." % forbidden)
    # compress= is the SET OF PATHS to Zlib, not a flag. Two small files; store them plain.
    paklib.write_pak(a.out, "../../../", files, seed=0, compress=())
    # read it back the way the game will, so a pak that cannot be parsed never reaches ~mods
    r = paklib.PakReader(a.out)
    assert set(r.files) == set(files), (sorted(r.files), sorted(files))
    print("  pak verified: %d files, mount %s" % (len(r.files), r.mount_point))
    blob = open(a.out, "rb").read()
    print("\nwrote %s  %d B  sha256 %s" % (a.out, len(blob), hashlib.sha256(blob).hexdigest()[:16]))
    print("Install: copy it into Bodycam/Content/Paks/~mods/ . Remove it to go back to stock.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
