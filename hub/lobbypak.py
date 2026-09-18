"""The lobby pak that makes the host auto-open the match's map.

WHAT THIS IS FOR. The host's game has to open the map the match chose, with no menu interaction. The
only thing that can make it do that is code running inside the game (the command line is measured
ignored; nothing outside the process can reach a running Bodycam), and that code takes the map as a
literal baked into the pak. So the hub keeps ONE PAK VARIANT PER INSTALLED LEVEL and drops the right
one into ~mods just before it asks Steam to launch the host.

Variants are cut from a pristine seed with tools/pak/retarget_lobby.py: two FName entries and a
repack, ~1-4 ms and 18 KB each, everything else byte-identical. They are cached under
%LOCALAPPDATA%\\CommunityHub\\lobby and cut on first use, so a match pays for the copy only.

THE RULE THAT SHAPES EVERY FUNCTION HERE: NEVER LEAVE A STALE PAK. The graph opens whatever literal
it carries (lobby_graphs.py, host_probe's OpenLevel - one site since chlobby-31 deleted stage 2), so a
pak left over from the last match does not fail safe - it cold-launches the host into the WRONG map while nine people
wait in a lobby that will never fill. Every failure path in this module removes the pak rather than
leaving one behind, and so does match cancellation.

The game install is read-only by default and the hub must ask before writing to it, so nothing here
touches ~mods unless the caller has already taken consent (hub/ops.py owns that for the gamemode
pak; `installed()` and `remove()` are safe to call regardless).
"""
import hashlib
import os
import shutil
import sys

from . import game as game_mod
from . import paths
from . import version

LOBBY_PAK_NAME = "CommunityLobby_P.pak"

# chlobby-28, and it is back here after chlobby-29 crashed the game. 29 added a lobby SEARCH to the
# same BeginPlay as the travel; the search was still inside the EOS SDK when OpenLevel destroyed the
# world that owned it, and EOS called back into freed memory:
#   EXCEPTION_ACCESS_VIOLATION reading 0x0 - Bodycam x11 / EOSSDK x5 / Bodycam x10 (2026-09-15).
# THE RULE: no online or lobby call may be outstanding when the travel fires. The joiner therefore
# has to be its own cooked class in its own pak, never a second arm beside the host.
# A shipped hub has no
# editor and no cook, so it can only ever RETARGET this file - never regenerate it.
# chlobby-30 (2026-09-16): the probe URLs moved to lightsout - the rebrand missed lobby_graphs.py,
# so the chlobby28 seed asks a dead host for its travel permit and NOTHING TRAVELS - and the host
# now stamps a per-match token into "Session Name".
# chlobby-31 (2026-09-16): host_probe's STAGE 2 IS DELETED. It re-opened the map stage 1 had already
# loaded, gated only on `IsServer && !IsStandalone` with no lap gate, so as a listen host it read
# true and travelled to the same map again - one full load per lap. Sam saw auto-host load, load a
# second time and freeze; the third test ended in an application hang. Only reachable once chlobby-30
# restored the travel permit and so produced a second BeginPlay. retarget_lobby.HOST_NAMECONST_SITES
# drops 3 -> 2 in step, because that OpenLevel was one of the sites reaching the map literals.
# The JOINER seed moves to chjoin2 for the same build: chjoin_logic embeds BUILD_TAG, so the tag bump
# alone changed its cooked bytes, and the basename is the cache key.
#
# THE BASENAME MUST CHANGE WHENEVER THE COOKED CLASS DOES. seed_path() looks a cached seed up BY
# FILENAME, so an install that keeps chlobby28 keeps a pak with no token slot and no URL fix, and
# degrades in silence: the host advertises nothing, every joiner laps out, and no log line explains
# it. The name is the cache key; treat it as one.
# chlobby-32 (2026-09-17): writes the per-match reporting capability into BodycamGI.Search String
# before travel. The match GameMode reads that private GameInstance property for every bearer.
#
# The HOST seed's digest, kept as a tripwire rather than a gate: nothing in the runtime path
# refuses on a mismatch, but tests/test_lobbypak.py does, so a seed that changes without anyone
# noticing fails the build instead of a player's match. Built and round-trip verified locally
# on 2026-09-17 from the final UE5.5 cook plus the installed LobbyHost level read-only.
# chlobby-33 removes the diagnostic search and legacy join arms that recooking had restored.
# Fresh solo-host crash dumps match the prior EOS search/travel failure. Source AND shipped
# bytecode tests now forbid FindLobbies/JoinLobby in this traveling class. New basename forces
# cached chlobby32 replacement. Parent host flow, map and report-token retargeting are retained.
# chlobby36 starts native hosting once, waits for its listen range, then commits
# one authenticated match travel. The stage survives map loads in the GameInstance.
# chlobby37/chjoin3 retain a private participant migration capability and five-minute reconnect timeout.
SEED_SHA256 = "b8f015804f78609f3cb431d6210dffa3025857963e9b02f1bb9f829e5f452144"
SEED_BASENAME = "CommunityLobby_chlobby37_P.pak"
JOIN_SEED_BASENAME = "CommunityJoin_chjoin3_P.pak"

# WHICH COOKED CLASS EACH ROLE PACKS. build_lobby_override.py takes these straight through as
# --class-pkg / --class, which is how one tool builds both paks from one cooked tree.
ROLES = {
    "host": ("/Game/GM/Gamemode/GM_CHLobby", "GM_CHLobby_C", SEED_BASENAME),
    "join": ("/Game/GM/Gamemode/GM_CHJoin", "GM_CHJoin_C", JOIN_SEED_BASENAME),
}


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def seed_dir():
    """<bundle>/hub/lobbyseed - OUR cooked lobby GameMode, shipped with the hub.

    The finished pak is deliberately NOT shipped: it carries the game's LobbyHost level with four
    bytes changed, and this project does not redistribute Reissad Studio's asset bytes. Our Blueprint
    travels, their level is read out of their own install, and generate_seed() joins the two."""
    for base in (getattr(sys, "_MEIPASS", ""), _repo_root()):
        if not base:
            continue
        d = os.path.join(base, "hub", "lobbyseed")
        if os.path.isfile(os.path.join(d, "blueprints_summary.txt")):
            return d
    return ""


def generate_seed(game, log=None, role="host"):
    """Assemble the pristine lobby pak on THIS machine, from this player's own game files.

    Runs tools/pak/build_lobby_override.py, which reads the stock LobbyHost level out of the
    player's paks, repoints its World Settings DefaultGameMode at our GM_CHLobby_C, and packs it
    with our cooked Blueprint. Its own cook checks run too: it refuses a cook whose summary does not
    say RESULT: OK, which is why blueprints_summary.txt ships alongside the asset.

    Returns the seed path, or "" if it cannot be built - a player with no game, no paks folder, or a
    game version whose LobbyHost no longer matches is a normal case, not a crash."""
    cooked = seed_dir()
    if not cooked or not game:
        return ""
    class_pkg, class_name, basename = ROLES.get(role, ROLES["host"])
    out = os.path.join(str(paths.state_dir()), "lobby", basename)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    work = os.path.join(str(paths.state_dir()), "lobby", "work")
    os.makedirs(work, exist_ok=True)

    mod = _pak_module("build_lobby_override")
    argv = ["build_lobby_override.py",
            "--paks", game_mod.paks_dir(game),
            "--work", work,
            "--out", out,
            "--cooked", cooked,
            "--class-pkg", class_pkg,
            "--class", class_name]
    saved = sys.argv
    try:
        sys.argv = argv                      # main() parses sys.argv; there is no argv parameter
        rc = mod.main()
    except SystemExit as e:                  # it calls SystemExit for every refusal, with a reason
        rc = e.code if isinstance(e.code, int) else 1
        if log:
            log("lobby pak seed: %s" % e)
    except Exception as e:
        rc = 1
        if log:
            log("lobby pak seed failed: %r" % e)
    finally:
        sys.argv = saved
    if rc or not os.path.isfile(out):
        return ""
    if log:
        log("built the lobby pak seed from your own install")
    return out


def seed_path(game="", log=None, role="host"):
    """The pristine lobby pak, building it from the player's own install if we do not have one yet.

    Returns "" when there is none and none can be built, which is not an error - it means this
    machine cannot auto-host and the caller must fall back to telling the player to host by hand."""
    basename = ROLES.get(role, ROLES["host"])[2]
    for candidate in (os.path.join(str(paths.state_dir()), "lobby", basename),
                      os.path.join(_repo_root(), "paks", basename)):
        if os.path.isfile(candidate):
            return candidate
    return generate_seed(game, log=log, role=role) if game else ""


def cache_dir():
    d = os.path.join(str(paths.state_dir()), "lobby", "variants")
    os.makedirs(d, exist_ok=True)
    return d


def _pak_module(name):
    """Import tools/pak/<name>.py BY PATH, the way ops.py imports the pak builder - it travels as a
    data file, so a frozen build cannot import it normally and the analyser cannot see it either.

    That invisibility is why hub.spec keeps an explicit PAK_MODULES list: a module missing from it
    builds fine and fails at the player's first use. 2.0.4 shipped without retarget_lobby.py and did
    exactly that."""
    import importlib.util
    for base in (str(paths.tools_dir()), os.path.join(_repo_root(), "tools")):
        path = os.path.join(base, "pak", name + ".py")
        if not os.path.isfile(path):
            continue
        pak_dir = os.path.join(base, "pak")
        if pak_dir not in sys.path:
            sys.path.insert(0, pak_dir)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    raise FileNotFoundError("tools/pak/%s.py not shipped" % name)


def _retarget_module():
    return _pak_module("retarget_lobby")


def available_levels(game):
    """The cooked levels the player actually has, from the gamemode pak in their ~mods.

    Empty dict when the gamemode pak is not installed. This is the authority for what can be hosted -
    gamemodes/*/manifest.json lists combinations that do not exist here and names five levels
    differently from their cooked names."""
    pak = os.path.join(game_mod.mods_dir(game, create=False), version.PAK_NAME)
    if not os.path.isfile(pak):
        return {}
    try:
        return _retarget_module().available_levels(pak)
    except Exception:
        return {}


def resolve(game, gamemode_id, map_name):
    """(level_pkg, level_name) for a gamemode id and the map name the veto produced, or None.

    None means "do not auto-host this match" and must be treated as such by the caller. It is never
    a reason to fall back to some other map: opening a level the player does not have tears their
    world down before the load fails."""
    levels = available_levels(game)
    if not levels:
        return None
    try:
        return _retarget_module().resolve_level(levels, gamemode_id, map_name)
    except KeyError:
        return None


def variant(game, level_pkg, level_name, log=None, host_id="", token="", role="host",
            report_token=""):
    """Path to the cached pak that opens `level_name`, cutting it from the seed on first use.

    `host_id` is the match host's SteamID64, stamped in so a JOINER can recognise its host among the
    strangers' lobbies a search returns. It is part of the cache key: two matches on the same map
    with different hosts are different paks."""
    seed = seed_path(game, log=log, role=role)
    if not seed:
        raise FileNotFoundError("no lobby pak seed, and none could be built from this install")
    # THE TOKEN IS PART OF THE CACHE KEY, and leaving it out would be the subtle kind of wrong: the
    # key would say "Hospital, this host" for a pak whose baked token belongs to LAST match, and the
    # stale variant would win silently. Nothing downstream re-checks it, so the key is the check.
    # THE ROLE IS PART OF THE KEY TOO. Host and joiner variants are different cooked classes that
    # install under the SAME filename, so a key without the role would hand a joiner the host's pak
    # and auto-host nine people into their own private matches.
    # A report token is a credential. Its digest makes variants distinct without putting the raw
    # secret in a filename, crash report, directory listing, or support screenshot.
    report_tag = ("report-" + hashlib.sha256(str(report_token).encode("ascii")).hexdigest()[:16]
                  if report_token else "")
    # An app update can replace the seed while this same match is being retried.
    # Include its bytes so a cached pre-fix variant cannot override the new host logic.
    with open(seed, "rb") as seed_file:
        seed_tag = "seed-" + hashlib.sha256(seed_file.read()).hexdigest()[:16]
    tag = "_".join([p for p in (role, seed_tag, level_name, str(host_id or ""), str(token or ""), report_tag) if p])
    out = os.path.join(cache_dir(), "CommunityLobby_%s_P.pak" % tag)
    if os.path.isfile(out):
        return out
    mod = _retarget_module()
    levels = available_levels(game)
    tmp = out + ".tmp"
    try:
        mod.retarget_pak(seed, tmp, level_pkg, level_name, allowed=levels or None,
                         host_id=str(host_id or ""), token=str(token or ""),
                         report_token=str(report_token or ""))
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if log:
        log("cut lobby pak for %s" % level_name)
    return out


def installed_path(game):
    return os.path.join(game_mod.mods_dir(game, create=False), LOBBY_PAK_NAME)


def installed(game):
    return os.path.isfile(installed_path(game))


def install(game, variant_path):
    """Atomically put `variant_path` in ~mods. Raises on failure; the caller must then remove().

    The game must not be running: replacing a mounted pak either fails on the lock or is ignored, and
    which of those happens is unmeasured - so an OSError here is taken as authoritative even though
    game_running() fails open."""
    dest = installed_path(game)
    os.makedirs(game_mod.mods_dir(game, create=True), exist_ok=True)
    tmp = dest + ".tmp"
    shutil.copyfile(variant_path, tmp)
    from . import recording
    recording.before_write(game, LOBBY_PAK_NAME, tmp)
    os.replace(tmp, dest)
    return dest


def remove(game):
    """Take our lobby pak out of ~mods. Safe to call when it is not there.

    Called on every failure path and on match cancellation, because a stale pak is worse than no pak:
    it sends the host to the last match's map."""
    from . import recording
    if recording.enabled() and os.path.isfile(installed_path(game)):
        try:
            recording.before_write(game, LOBBY_PAK_NAME, None)
        except (OSError, RuntimeError):
            return False
    for path in (installed_path(game), installed_path(game) + ".tmp"):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            return False
    return True


def _complain(message, log=None):
    """Put a failure where somebody will find it, whether or not a caller passed a logger.

    Both pak arms call prepare() with log=None - they have no screen to draw on - so a message that
    only went to `log` would go nowhere at all. The hub's log file is the place a person actually
    looks, so it gets a copy with the traceback attached.

    Wrapped whole: a diagnostic may never be the thing that breaks the feature it is diagnosing."""
    try:
        if log:
            log(message)
    except Exception:           # noqa: BLE001
        pass
    try:
        import traceback
        with open(paths.log_file(), "a", encoding="utf-8") as fh:
            fh.write("\n[lobbypak] %s\n%s" % (message, traceback.format_exc()))
    except Exception:           # noqa: BLE001
        pass


def prepare(game, gamemode_id, map_name, log=None, host_id="", token="", role="host",
            report_token=""):
    """Put the right lobby pak in place for this match. Returns the level name, or "" if we cannot.

    "" is a complete answer: the caller tells the host to open the map themselves, and the manual
    "I'm in" button still releases the joiners. What it must NOT do is launch the game anyway - that
    is how a host ends up in the wrong map."""
    if game_mod.game_running():
        remove(game)
        return ""
    target = resolve(game, gamemode_id, map_name)
    if not target:
        remove(game)
        return ""
    level_pkg, level_name = target
    made = ""
    try:
        made = variant(game, level_pkg, level_name, log=log,
                       host_id=host_id, token=token, role=role,
                       report_token=report_token)
        install(game, made)
    except Exception as e:
        # SAY WHY. Removing the pak and returning "" is the safe answer - better a host who is told
        # to open the map by hand than one launched into the wrong one - but doing it silently made
        # a real bug indistinguishable from "nothing to do". The joiner's ship-safety file list
        # rejected every pak it was ever handed (2026-09-16) and the only visible symptom was a
        # player spawning in the range with no pak and no log line anywhere.
        _complain("lobby pak for %s (%s) failed: %r" % (level_name, role, e), log)
        remove(game)
        return ""
    finally:
        # Secret variants have no reuse value: every match gets a new capability. Keep only the
        # installed copy, which reset_match removes at the end of the match. Clean this copy even
        # when replacement fails, so a locked game install cannot strand a credential in cache.
        if report_token and made:
            try:
                os.remove(made)
            except OSError:
                pass
    return level_name


def seed_digest(path=""):
    path = path or seed_path()   # never builds one: this only reports on a seed we already have
    if not path or not os.path.isfile(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
