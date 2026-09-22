#!/usr/bin/env python3
"""Point a BUILT CommunityLobby pak at a different level. No cook, no UE, no mirror.

    python3 retarget_lobby.py --selftest              # identity target must reproduce the seed byte-for-byte
    python3 retarget_lobby.py --all <out_dir>         # one variant per level installed on this machine

WHY THIS EXISTS. The lobby pak's OpenLevel target and short-name diagnostic are
LITERALS baked at cook time (lobby_graphs.py HOST_MAP and HOST_MAP_SHORT).
Native "Selected Level Name" stays on the stock range in chlobby35. A real match
has to open whichever of the installed levels the hub chose, and
rebuilding through the editor per match is not a thing a player's machine can do.

It does not have to. MEASURED on the working chlobby-28 pak: both map strings live in
GM_CHLobby.uasset's FName table and NEITHER appears anywhere in GM_CHLobby.uexp - the bytecode
reaches them by index, at HOST_NAMECONST_SITES NameConst sites (the OpenLevel LevelName argument and
the MakeLiteralName feeding the short-name diagnostic). So retargeting is two FName entries plus a repack of
the same four files: ~1-4 ms, 18 KB, with GM_CHLobby.uexp and both LobbyHost files byte-identical.

WHAT MAKES THIS SAFE ENOUGH TO RUN ON A PLAYER'S MACHINE. OpenLevel on a level that does not exist
tears the world down BEFORE the load fails, so a wrong name is not a no-op - it is a broken client
sitting in nothing. Therefore the level set is not taken from gamemodes/*/manifest.json, which lists
35 combinations and is WRONG about the names; it is enumerated from the cooked .umap files in the
gamemode pak actually installed (available_levels), and retarget_pak refuses any target not in that
set. The manifest says "Pool" and "Russian"; the cooked levels are BB5_PublicPool and
BB5_RussianBuilding. Two of the six competitive pool maps diverge, so trusting the manifest would
break roughly one match in three.

The seed pak is never mutated: every variant is written out fresh from the pristine bytes.
"""
import argparse
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paklib                                             # noqa: E402
from pakfile import PakFile                               # noqa: E402
from pkgedit import CookedPackage, case_preserving_hash   # noqa: E402

_C = "Bodycam/Content/"
CHLOBBY_UASSET = _C + "GM/Gamemode/GM_CHLobby.uasset"
CHLOBBY_UEXP = _C + "GM/Gamemode/GM_CHLobby.uexp"
CHJOIN_UASSET = _C + "GM/Gamemode/GM_CHJoin.uasset"
CHJOIN_UEXP = _C + "GM/Gamemode/GM_CHJoin.uexp"
LOBBYHOST_UMAP = _C + "Map/LobbyHost/LobbyHost.umap"
LOBBYHOST_UEXP = _C + "Map/LobbyHost/LobbyHost.uexp"
# Hard rule 7: the pak's FILE LIST is the authoritative ship-safety check. A byte scan is NOT - the
# real GM_Host / BodycamGI import names legitimately appear inside our asset.
#
# ONE SET PER ROLE (hub/lobbypak.py ROLES). The host ships GM_CHLobby and the joiner ships GM_CHJoin;
# both ship LobbyHost and NOTHING else. This list knew only the host until 2026-09-16, so every
# joiner pak ever built was refused here - and prepare() turned that refusal into a silently removed
# lobby pak and a player spawning in the range. Adding a role means adding its set here.
#
# The rule is not relaxed by having two entries: each is still an EXACT four-file set, so a pak
# carrying anything of the game's beyond LobbyHost is refused exactly as before.
EXPECTED_FILE_SETS = (
    {CHLOBBY_UASSET, CHLOBBY_UEXP, LOBBYHOST_UMAP, LOBBYHOST_UEXP},
    {CHJOIN_UASSET, CHJOIN_UEXP, LOBBYHOST_UMAP, LOBBYHOST_UEXP},
)
# Kept as the HOST set for callers that mean that specific one.
EXPECTED_FILES = EXPECTED_FILE_SETS[0]

# uasset -> (uexp, does this class carry the map literals?).
#
# The HOST's class calls OpenLevel, so its two map names must be retargeted per match and the
# NameConst tripwire must find them - a host pak that silently failed to retarget would drop the
# player into the seed's map. The JOINER's class never opens a level: GM_Host does the search and
# the travel once "SessionToJoin (Client)" is non-empty, so the joiner seed mentions no map at all
# (measured: BB5_Hospital 0, GM_Maps 0). Asking it to retarget one is a KeyError on a correct pak.
GM_CLASSES = {
    CHLOBBY_UASSET: (CHLOBBY_UEXP, True),
    CHJOIN_UASSET: (CHJOIN_UEXP, False),
}

# What the seed was cooked pointing at (lobby_graphs.py HOST_MAP / HOST_MAP_SHORT).
# HOW MANY NameConst SITES THE HOST'S BYTECODE REACHES THE MAP NAMES THROUGH.
#
# Was 3 through chlobby-30: two OpenLevel LevelName arguments plus the MakeLiteralName that feeds
# the "Selected Level Name" property write. chlobby-31 deleted host_probe's stage 2, whose OpenLevel
# re-opened the map stage 1 had already loaded and froze the game in a travel loop - so one of the
# two OpenLevel sites is gone and the count is 2.
#
# This number is not cosmetic. _namconst_tripwire REFUSES a retarget that does not hit it exactly,
# and hub/lobbypak.prepare() turns a refusal into a REMOVED lobby pak - i.e. a host who cold-launches
# into the shooting range while nine people wait. Leaving it at 3 would have broken auto-host far
# worse than the bug being fixed. Change it in the same commit as any graph edit that adds or removes
# an OpenLevel / MakeLiteralName on the map literals.
HOST_NAMECONST_SITES = 2

SEED_PKG = "/Game/GM_Maps/Community/BB5/BB5_Hospital"
SEED_NAME = "BB5_Hospital"

# The joiner's identity literal (lobby_graphs.py HOST_ID). The cooked placeholder never matches a
# real lobby, so a pak that was never stamped joins nobody rather than joining a stranger.
SEED_HOST_ID = "0"

# The join token's placeholder (lobby_graphs.py JOIN_TOKEN). Like SEED_HOST_ID this is a value that
# can never match a real match, so a pak that somehow ships unstamped joins nothing rather than
# joining the wrong thing.
SEED_JOIN_TOKEN = "chjoin-7f3a91"
# Private participant capability placeholder (lobby_graphs.py REPORT_TOKEN). Unlike the join token,
# this value is never advertised in Steam lobby data or put in a joiner pak.
SEED_REPORT_TOKEN = "chreport-7f3a91"

# chlobby35 waits for native hosting, then commits one authenticated match load.
SEED_PAK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                        "paks", "CommunityLobby_chlobby38_P.pak")
SEED_SHA256 = "bfc5ed1ccf1481bc182c667b722ccb2fd3ae77a8ab1f2e86a5ab5879f2459dc8"

LEVEL_ROOT = _C + "GM_Maps/Community/"


def available_levels(gamemodes_pak):
    """{'BB5_Rome': '/Game/GM_Maps/Community/BB5/BB5_Rome', ...} - every level cooked into the
    gamemode pak that is actually installed.

    THIS, NOT THE MANIFEST, IS THE AUTHORITY. gamemodes/*/manifest.json lists 35 (gamemode x map)
    pairs; 13 exist here, and Domination has none at all. The manifest also carries the level's
    DISPLAY name, which is not its cooked name for five of them."""
    out = {}
    with PakFile(gamemodes_pak) as pf:
        files = list(pf.files)
    for rel in files:
        if not rel.startswith(LEVEL_ROOT) or not rel.endswith(".umap"):
            continue
        rest = rel[len(LEVEL_ROOT):-len(".umap")]         # "BB5/BB5_Rome"
        if rest.count("/") != 1:
            continue
        out[rest.split("/", 1)[1]] = "/Game/GM_Maps/Community/" + rest
    return dict(sorted(out.items()))


def resolve_level(levels, gamemode_id, map_name):
    """A gamemode id and a map name as the hub knows them -> (level_pkg, level_name).

    Case-insensitive, and the match must be UNIQUE: a naive prefix test would tie "Pool" to both
    BB5_PublicPool and a hypothetical BB5_Pool. Raises rather than guessing - a wrong level name
    destroys the host's world (see the module docstring)."""
    want = "%s_%s" % (gamemode_id, map_name)
    hits = [n for n in levels if n.lower() == want.lower()]
    if not hits:                                           # the display name may differ from the cooked one
        hits = [n for n in levels
                if n.lower().startswith(gamemode_id.lower() + "_") and map_name.lower() in n.lower()]
    if len(hits) != 1:
        raise KeyError("%r resolves to %d installed levels (%s); installed: %s"
                       % (want, len(hits), hits, sorted(levels)))
    return levels[hits[0]], hits[0]


def _rename_two(uasset_bytes, old_pkg, new_pkg, old_name, new_name, workdir, one=False):
    """Swap two FName entries and reserialize. Returns (bytes, (index_pkg, index_name)).

    The entries are found BY VALUE, never by a hard-coded index: the name table's order is a cook
    artifact and would silently move the moment the graph changes. Only the case-preserving hash is
    recomputed - the other one is FCrc::Strihash_DEPRECATED, which the loader discards (pkgedit.py:31
    calls them DummyHashes), so it is carried through untouched rather than guessed at."""
    seed = os.path.join(workdir, "seed.uasset")
    with open(seed, "wb") as fh:
        fh.write(uasset_bytes)
    pk = CookedPackage.load(seed)                          # strict: no lenient fallback
    if pk.serialize() != pk.src:
        raise ValueError("seed GM_CHLobby.uasset does not round-trip; refusing to edit it")
    i_pkg = pk.name_index(old_pkg)
    i_name = i_pkg if one else pk.name_index(old_name)
    if not one and i_pkg == i_name:
        raise ValueError("the package path and the level name share one FName entry")
    pairs = ((i_pkg, new_pkg),) if one else ((i_pkg, new_pkg), (i_name, new_name))
    for idx, new in pairs:
        text, h1, _h2 = pk.names[idx]
        pk.names[idx] = [new, h1, case_preserving_hash(new)]
    return pk.serialize(), (i_pkg, i_name)


def _namconst_tripwire(uasset, uexp, workdir, expect):
    """Disassemble the retargeted bytecode and confirm the swap actually REACHES the graph.

    The rename is only effective because HOST_NAMECONST_SITES NameConst sites resolve through the
    two entries we touched. If a future graph edit adds a site, or stops routing one of them through
    the name table, this catches it at build time instead of at a player's cold launch."""
    ua = os.path.join(workdir, "t.uasset")
    ue = os.path.join(workdir, "t.uexp")
    with open(ua, "wb") as fh:
        fh.write(uasset)
    with open(ue, "wb") as fh:
        fh.write(uexp)
    import kismet
    pkg = kismet.Pkg(ua, ue)
    lines = kismet.Dis(pkg, kismet.parse_function(pkg, 1)["code"]).run()
    found = [ln.split("NameConst ", 1)[1].strip().strip("'")
             for ln in lines if "NameConst " in ln]
    hits = [f for f in found if f in expect]
    if len(hits) != HOST_NAMECONST_SITES:
        raise ValueError("expected %d NameConst references to %s, found %d (%s)"
                         % (HOST_NAMECONST_SITES, sorted(expect), len(hits), hits))
    return hits


def retarget_pak(src_pak, out_pak, level_pkg, level_name,
                 old_pkg=SEED_PKG, old_name=SEED_NAME, allowed=None,
                 host_id="", old_host_id=SEED_HOST_ID,
                 token="", old_token=SEED_JOIN_TOKEN,
                 report_token="", old_report_token=SEED_REPORT_TOKEN):
    """Write `out_pak`: `src_pak` with its map literals pointing at `level_pkg` / `level_name`.

    `allowed` is the level set from available_levels(); passing it is what makes a nonexistent target
    unreachable rather than merely unlikely. `old_pkg`/`old_name` are arguments so this is idempotent
    - a variant can be retargeted again without hunting for what it currently says."""
    if allowed is not None and level_name not in allowed:
        raise KeyError("%r is not installed on this machine; have: %s" % (level_name, sorted(allowed)))
    if allowed is not None and allowed[level_name] != level_pkg:
        raise ValueError("%r is installed at %r, not %r" % (level_name, allowed[level_name], level_pkg))

    with PakFile(src_pak) as pf:
        if set(pf.files) not in EXPECTED_FILE_SETS:
            raise ValueError("unexpected file list in %s: %s" % (src_pak, sorted(pf.files)))
        files = {rel: pf.read(rel) for rel in sorted(pf.files)}
    expected = set(files)
    # WHICH class this pak carries, and therefore what may be done to it. The file list has already
    # been checked against EXPECTED_FILE_SETS, so exactly one of these is present.
    gm_uasset = next(k for k in GM_CLASSES if k in files)
    gm_uexp, retargets_level = GM_CLASSES[gm_uasset]

    if host_id and not str(host_id).isdigit():
        raise ValueError("host_id must be a SteamID64, got %r" % (host_id,))
    # The token becomes an FName. Refuse anything that could be read as a name NUMBER (a trailing
    # _<digits> is split off by UE) or that carries characters a name table should not: a token that
    # silently changes shape between the two paks pairs nobody with nobody, and would look exactly
    # like a broken join.
    if token:
        token = str(token)
        if not all(c.isalnum() or c == "-" for c in token) or token != token.lower():
            raise ValueError("token must be lowercase alphanumeric or '-', got %r" % (token,))
    if report_token:
        report_token = str(report_token)
        if len(report_token) != 64 or any(c not in "0123456789abcdef" for c in report_token):
            raise ValueError("report_token must be 64 lowercase hex characters")
    stamped = False
    token_stamped = False
    report_token_stamped = False

    with tempfile.TemporaryDirectory() as workdir:
        new_ua = files[gm_uasset]
        idxs = []          # the name-table slots the map rewrite touched; none for a class with no map
        if retargets_level:
            new_ua, idxs = _rename_two(new_ua, old_pkg, level_pkg,
                                       old_name, level_name, workdir)
        # The joiner's target, stamped the same way and for the same reason: an FName lives in the
        # name table where it can be rewritten at any length, so one seed serves every match.
        # OPTIONAL BY DESIGN. A seed cooked before the joiner arm existed has no HOST_ID entry in
        # its name table, and refusing to build would take AUTO-HOST down with the joiner - the map
        # stamp is independent and still correct. So: stamp it if the slot is there, report it if it
        # is not, and never let the joiner half break the half that works.
        if host_id:
            try:
                new_ua, _ = _rename_two(new_ua, old_host_id, str(host_id),
                                        old_host_id, str(host_id), workdir, one=True)
                stamped = True
            except KeyError:
                stamped = False
        # THE JOIN TOKEN, stamped the same optional way and for the same reason. It is what pairs a
        # joiner with its host: the host writes it into "Session Name" (-> the lobby's searchable
        # Name attribute) and the joiner into "SessionToJoin (Client)" (-> JoiningByName). A seed
        # without the slot predates the joiner arm; the map stamp beside it is still correct, and
        # auto-host must not be taken down by the half that is newer.
        if token:
            try:
                new_ua, _ = _rename_two(new_ua, old_token, token,
                                        old_token, token, workdir, one=True)
                token_stamped = True
            except KeyError:
                token_stamped = False
        # Authentication must fail closed. If the hub was given a report capability, launching a
        # host pak that could not carry it would strand a ranked match on an old unauthenticated
        # client. Only the host class may contain this private slot.
        if report_token:
            if gm_uasset not in (CHLOBBY_UASSET, CHJOIN_UASSET):
                raise ValueError("only a private participant lobby pak may carry a report capability")
            try:
                new_ua, _ = _rename_two(new_ua, old_report_token, report_token,
                                        old_report_token, report_token, workdir, one=True)
                report_token_stamped = True
            except KeyError as exc:
                raise ValueError("host lobby seed has no report-token slot; rebuild it") from exc
        # The tripwire proves the map the class will actually OPEN is the one we asked for. A class
        # that opens nothing has nothing to prove here, and demanding three references to a name it
        # never mentions would fail every correct joiner pak.
        consts = (_namconst_tripwire(new_ua, files[gm_uexp], workdir, {level_pkg, level_name})
                  if retargets_level else [])
    untouched = {k: v for k, v in files.items() if k != gm_uasset}
    files[gm_uasset] = new_ua

    # `expected` is THIS pak's own four-file set, captured before anything was rewritten, so the
    # check is still "the file list did not change" rather than "it matches the host's".
    if set(files) != expected:
        raise ValueError("ship-safety: file list changed during retarget")
    paklib.write_pak(out_pak, "../../../", files, seed=0, compress=())

    check = paklib.PakReader(out_pak)
    if set(check.files) != expected:
        raise ValueError("ship-safety: written pak has %s" % sorted(check.files))
    with PakFile(out_pak) as back:                         # only the gamemode asset may differ
        for rel, data in untouched.items():
            if back.read(rel) != data:
                raise ValueError("%s changed; only %s may" % (rel, os.path.basename(gm_uasset)))

    with open(out_pak, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return {"out": out_pak, "level_pkg": level_pkg, "level_name": level_name,
            "host_id": str(host_id or ""), "host_id_stamped": stamped,
            "token": str(token or ""), "token_stamped": token_stamped,
            "report_token_stamped": report_token_stamped,
            "gm": os.path.basename(gm_uasset), "level_retargeted": retargets_level,
            "name_indices": idxs, "nameconsts": consts,
            "uasset_bytes": len(new_ua), "sha256": digest}


def _selftest(seed_pak):
    """Retargeting the seed to what it already says must reproduce it EXACTLY.

    This is the regression test for the whole mechanism: it exercises the load, the two renames, the
    hash recompute, the reserialize and the repack, and compares against a pak measured auto-hosting
    on 2026-09-15. A byte-for-byte match means none of those steps moved anything."""
    with open(seed_pak, "rb") as fh:
        before = hashlib.sha256(fh.read()).hexdigest()
    if before != SEED_SHA256:
        print("WARN: seed is %s, expected %s" % (before[:16], SEED_SHA256[:16]))
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "identity.pak")
        rep = retarget_pak(seed_pak, out, SEED_PKG, SEED_NAME)
    ok = rep["sha256"] == before
    print("identity retarget -> %s  %s" % (rep["sha256"][:16], "IDENTICAL" if ok else "DIFFERS"))
    print("  name indices %s, NameConst sites %d" % (rep["name_indices"], len(rep["nameconsts"])))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", default=os.path.normpath(SEED_PAK), help="the pristine lobby pak")
    ap.add_argument("--gamemodes", default="", help="installed CommunityGamemodes_P.pak (for the level set)")
    ap.add_argument("--selftest", action="store_true", help="identity target must reproduce the seed")
    ap.add_argument("--list", action="store_true", help="list the levels installed on this machine")
    ap.add_argument("--all", metavar="OUT_DIR", default="", help="write one variant per installed level")
    ap.add_argument("--level", default="", help="a single cooked level name, e.g. BB5_Rome")
    ap.add_argument("--out", default="", help="output pak for --level")
    a = ap.parse_args()

    if a.selftest:
        return _selftest(a.seed)

    levels = available_levels(a.gamemodes) if a.gamemodes else None
    if a.list:
        if not levels:
            print("--list needs --gamemodes")
            return 2
        for name, pkg in levels.items():
            print("%-34s %s" % (name, pkg))
        return 0

    if a.level:
        if not levels:
            print("--level needs --gamemodes (the level set is what makes a bad target unreachable)")
            return 2
        out = a.out or ("CommunityLobby_%s_P.pak" % a.level)
        rep = retarget_pak(a.seed, out, levels[a.level], a.level, allowed=levels)
        print("%-34s %6d B  %s" % (rep["level_name"], rep["uasset_bytes"], rep["sha256"][:16]))
        return 0

    if a.all:
        if not levels:
            print("--all needs --gamemodes")
            return 2
        os.makedirs(a.all, exist_ok=True)
        for name, pkg in levels.items():
            out = os.path.join(a.all, "CommunityLobby_%s_P.pak" % name)
            rep = retarget_pak(a.seed, out, pkg, name, allowed=levels)
            print("%-34s %6d B  %s" % (name, rep["uasset_bytes"], rep["sha256"][:16]))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
