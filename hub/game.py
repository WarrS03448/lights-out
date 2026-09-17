"""Finding the player's Bodycam install and looking at its mod folder.

Nothing here touches the game's files: we only read the Steam registry keys and the
library list to locate <GameDir>, and we create <GameDir>\\Bodycam\\Content\\Paks\\~mods
(the folder Unreal itself scans for extra paks) when we need to put our pak there.
"""
import glob
import os
import re
import subprocess
import threading
import sys
import time

from . import version, telemetry

_WIN = sys.platform == "win32"

# Steam's per-game folder name inside steamapps\common
_STEAM_FOLDER = "Bodycam"


# ---------------------------------------------------------------- basic checks
def is_game_dir(path) -> bool:
    """True when `path` looks like a Bodycam install: it has Bodycam\\Content\\Paks and
    that folder holds at least one .pak (the stock paks we read the game's assets from)."""
    if not path:
        return False
    paks = os.path.join(str(path), "Bodycam", "Content", "Paks")
    if not os.path.isdir(paks):
        return False
    try:
        for name in os.listdir(paks):
            if name.lower().endswith(".pak") and os.path.isfile(os.path.join(paks, name)):
                return True
    except OSError:
        return False
    return False


def paks_dir(game) -> str:
    """<GameDir>\\Bodycam\\Content\\Paks — what build_from_packs reads the stock assets from."""
    return os.path.join(str(game), "Bodycam", "Content", "Paks")


def mods_dir(game, create: bool = True) -> str:
    """<GameDir>\\Bodycam\\Content\\Paks\\~mods — where our pak is installed."""
    d = os.path.join(paks_dir(game), "~mods")
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def pak_path(game) -> str:
    """Full path of our installed pak (the file may or may not exist)."""
    return os.path.join(mods_dir(game, create=False), version.PAK_NAME)


def other_mod_paks(game, ours=None) -> list:
    """Other *.pak files sitting in ~mods (someone else's mods). Full paths, sorted.

    Useful for telling the user that something else is installed alongside us; we never
    touch these files."""
    ours = ours or version.PAK_NAME
    ours_name = os.path.basename(str(ours)).lower()
    d = mods_dir(game, create=False)
    if not os.path.isdir(d):
        return []
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*.pak"))):
        if os.path.basename(p).lower() != ours_name:
            out.append(p)
    return out


# ---------------------------------------------------------------- is the game running?
# ---------------------------------------------------------------- "is it running", cheaply
#
# WHY THIS EXISTS (Sam, 2026-09-15). game_running() spawns `tasklist`, and a subprocess that has to
# enumerate every process on the machine costs about a SECOND on a busy one. The web UI's snapshot
# called it three times - settings' game slice, settings' readiness checklist, gamemodes' ready
# guard - and the snapshot is rebuilt on the single UI thread for every state change. Measured:
# state_snapshot 2906 ms, of which 2996 ms was those three calls, which is why every click in the
# hub took three seconds to register. It is worse on a machine with more processes running, which
# is exactly why one player saw it and another did not.
#
# TWO FUNCTIONS, ON PURPOSE. Whether the game is running is a DISPLAY fact for a checklist and a
# DECISION fact for "may I overwrite the pak the game has open". Those want different things:
#   game_running()         exact, blocking. Keep using it for decisions - ops.apply, lobbypak,
#                          anything that would corrupt something by guessing.
#   game_running_cached()  never blocks. Returns the last known answer and refreshes in the
#                          background when it is stale. For anything drawn on a screen.
#
# The cache is deliberately NOT wired into game_running() itself: a close_game() polling loop that
# got a cached "still running" would wait out its whole grace period for nothing, and a caller
# about to write a pak needs the truth, not a two-second-old opinion.
_RUNNING_TTL_SECONDS = 2.0
# What a SNAPSHOT passes for max_age. The web UI rebuilds its snapshot once a second now
# (webui/panel.py REFRESH_MS), and every rebuild asks this whether the game is running — so at the
# 2 s default the hub would spawn a `tasklist` every two seconds for as long as it is open,
# including all through a match. Four seconds halves that for an answer that is only ever a label
# and a greyed-out button; the install itself still calls the exact game_running() (hub/ops.py),
# which is the one place being a moment out of date would actually cost something.
SNAPSHOT_TTL_SECONDS = 4.0
_running_lock = threading.Lock()
_running_at = 0.0                  # monotonic stamp of the last completed probe
_running_value = False
_running_refreshing = False


def _refresh_running():
    global _running_at, _running_value, _running_refreshing
    try:
        value = game_running()
    except Exception:              # noqa: BLE001 — a probe must never take the app down
        value = False
    with _running_lock:
        _running_value = value
        _running_at = time.monotonic()
        _running_refreshing = False


def game_running_cached(max_age=_RUNNING_TTL_SECONDS) -> bool:
    """The last known answer, refreshed in the background. NEVER blocks the caller.

    The first call returns False and starts a probe, so a screen drawn in the first fraction of a
    second says "not running" and corrects itself a moment later. That is the right trade for a
    checklist: being briefly wrong costs a redraw, and blocking the UI thread cost three seconds a
    click."""
    global _running_refreshing
    now = time.monotonic()
    with _running_lock:
        fresh = (now - _running_at) <= max(0.0, float(max_age)) and _running_at > 0.0
        value = _running_value
        start = not fresh and not _running_refreshing
        if start:
            _running_refreshing = True
    if start:
        threading.Thread(target=_refresh_running, name="hub-game-probe", daemon=True).start()
    return value


def game_running() -> bool:
    """True when Bodycam-Win64-Shipping.exe is in the task list (Windows only).

    Installing while the game runs would either fail on a locked file or be ignored, so
    ops.apply refuses in that case. Off Windows there is no game, so: False.

    IT ASKS game_pids(), AND IT HAS TO (Sam, 2026-09-16). This used to run its own tasklist and
    test `GAME_EXE.lower() in out.lower()` against the DEFAULT table format. That never matched,
    on any machine, for one reason: tasklist's table pads and TRUNCATES the Image Name column at
    25 characters, and "Bodycam-Win64-Shipping.exe" is 26. Measured against a process of exactly
    that name, the output is:

        Bodycam-Win64-Shipping.ex   166044 Console   1   5,736 K

    - the trailing "e" is gone, so the substring test was False with the game plainly running.
    Everything built on this answered "Bodycam is closed" no matter what: the Competitive tab let
    a player queue with the game up (the one rule the queue has), ops.apply installed over a pak
    the game had open, and settings' readiness row never lit. /FO CSV quotes the name in full and
    is not truncated, which is what game_pids() already parses - so there is exactly one place
    that knows how to read tasklist, instead of two that disagree."""
    return bool(game_pids())


# ---------------------------------------------------------------- launching the game
# Bodycam's Steam AppID. steam:// hands the whole job to Steam: it starts Steam if it is not
# running, shows its own launch UI, and does nothing if the game is already up. We never touch the
# exe directly - launching it ourselves would skip Steam's own initialisation and the online
# layer would come up wrong.
STEAM_APPID = "2406770"
STEAM_RUN_URL = "steam://rungameid/" + STEAM_APPID


GAME_LAUNCHER = "Bodycam.exe"     # the UE shim at <GameDir>; this is what Steam itself launches,
                                  # and it forwards its command line to Bodycam-Win64-Shipping.exe


def launch_game_with_args(game, args) -> bool:
    """Start Bodycam DIRECTLY with a command line, e.g. ["BB5_Hospital?listen"].

    WHY THIS EXISTS (docs/autojoin-plan-review.md 6b). The host was supposed to start its own match
    by calling the game's own host flow from our lobby GameMode. It cannot: the flow ends in
    OpenLevel(..., "listen"), which tears down and recreates the world, and the only call site our
    pak has is an HTTP response delegate owned by an actor IN that world - so it destroys the ground
    it is standing on, and the game crashed (chlobby-17). There is no third call site, because
    timers and Tick do not run in the lobby world at all.

    A command line sidesteps every bit of that: UE opens the map as a listen server during startup,
    before any of our code exists, and the in-game host flow is never entered.

    THE TRADE, STATED PLAINLY: this bypasses Steam's own launch. Steamworks normally attaches to a
    running Steam client anyway, so the online layer is expected to come up - but "expected" is not
    "measured", and if the lobby never advertises then the game must be launched through Steam and
    this approach is dead. Check IsInLobby / the server browser before believing it worked.
    """
    telemetry.emit("launch.request", action="direct_game")
    if not _WIN:
        return False
    exe = os.path.join(str(game), GAME_LAUNCHER)
    if not os.path.isfile(exe):
        return False
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0)
        subprocess.Popen([exe] + list(args), cwd=str(game),        # noqa: S603 - our own game dir
                         close_fds=True, creationflags=flags)
        telemetry.emit("launch.outcome", action="direct_game", status="handed_off")
        return True
    except Exception:
        telemetry.emit("launch.outcome", action="direct_game", status="failed", severity="error")
        return False


def launch_game() -> bool:
    """Ask Steam to start Bodycam. True when the request was handed off (NOT when the game is up).

    Steam gives no synchronous answer, so the caller has to poll game_running() if it needs to
    know. Off Windows there is no game: False.

    WHEN TO CALL THIS, AND WHY THE TIMING IS LOAD-BEARING (2026-09-14, docs/autojoin.md step 4
    result). The pak gets exactly ONE lobby search per launch: BeginPlay fires once per level load
    and fires the search ~4 s later, and there is no clock in the lobby world to try again with. So
    a joiner launched BEFORE the host has stamped CH_MATCH onto its lobby searches an empty Steam
    and has spent its only shot. The host must be in and stamped first - its game reports that
    itself, as ch_lobby_write - and only then may the joiners be launched.

    And if the game is ALREADY running when a match is found, that single BeginPlay is long gone.
    There is no channel into a live Bodycam process, so this call is a no-op for that player and
    they have to be told, not silently left behind. Check game_running() first and say something.
    """
    telemetry.emit("launch.request", action="steam_game")
    if not _WIN:
        return False
    try:
        os.startfile(STEAM_RUN_URL)                  # noqa: S606 - a steam:// URL, not a path
        telemetry.emit("launch.outcome", action="steam_game", status="handed_off")
        return True
    except Exception:
        telemetry.emit("launch.outcome", action="steam_game", status="failed", severity="error")
        return False


# ---------------------------------------------------------------- Steam detection
def _steam_paths() -> list:
    """Steam's own install folder(s), from the registry (Windows only)."""
    if not _WIN:
        return []
    import winreg  # imported lazily: the module only exists on Windows

    found = []
    for root, key, value in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(root, key) as k:
                v, _ = winreg.QueryValueEx(k, value)
            if v:
                v = os.path.normpath(str(v))
                if v not in found:
                    found.append(v)
        except OSError:
            continue
    return found


def _library_paths(steam: str) -> list:
    """Every Steam library folder, parsed out of steamapps\\libraryfolders.vdf.

    The file is Valve's own KeyValues format; we only need the "path" entries, so a
    regex is enough (and cannot throw on a format change the way a real parser would)."""
    libs = [steam]
    vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return libs
    for raw in re.findall(r'"path"\s*"([^"]+)"', text):
        p = os.path.normpath(raw.replace("\\\\", "\\"))
        if p not in libs:
            libs.append(p)
    return libs


def find_game_dir():
    """Best guess at <GameDir>, or None.

    HUB_GAME_DIR wins if it is set and looks like a game folder. Otherwise (Windows)
    walk every Steam library and return the first steamapps\\common\\Bodycam that passes
    is_game_dir()."""
    override = os.environ.get("HUB_GAME_DIR") or version.GAME_DIR_OVERRIDE
    if override and is_game_dir(override):
        return os.path.normpath(override)
    if not _WIN:
        return None
    for steam in _steam_paths():
        for lib in _library_paths(steam):
            cand = os.path.join(lib, "steamapps", "common", _STEAM_FOLDER)
            if is_game_dir(cand):
                return os.path.normpath(cand)
        if is_game_dir(steam):        # unusual, but the Steam folder itself may be it
            return os.path.normpath(steam)
    return None


# ---------------------------------------------------------------- closing the game
# WHY THE HUB CLOSES BODYCAM AT ALL (Sam, 2026-09-15). The game has to be shut down before the
# player can queue again: the lobby pak gets exactly ONE lobby search per launch (BeginPlay fires
# once per level load and there is no clock in the lobby world to try again with), so a hub that
# leaves the game running has to tell the next match's player to close it by hand — which is
# _maybe_launch_game's `game_was_open` branch, and it is the worst screen in the tab. Closing the
# game ourselves the moment the match is finished puts them back at base 1 with nothing to do.
#
# WHY IT IS A CLOSE REQUEST AND NOT A KILL. `taskkill /PID` without /F posts WM_CLOSE to the
# process's windows — the same thing Alt+F4 does — and Unreal shuts the engine down through its
# own path, tearing the online session down on the way out. A /F kill is indistinguishable from a
# crash, and a crash while hosting has been measured to lock the account out of CreateLobby for
# 20+ minutes (docs/HANDOFF-autojoin.md, "Unclean exit locks out hosting"). So the close request
# comes first and always, and /F is only ever the last resort after the grace has run out.
GAME_CLOSE_GRACE_SECONDS = 25        # how long a close REQUEST is given before /F is considered
GAME_CLOSE_POLL_SECONDS = 0.5


def game_pids() -> list:
    """The pids of every running Bodycam-Win64-Shipping.exe. [] off Windows, and [] when we
    cannot tell — a caller that cannot see the game must do nothing, never guess."""
    if not _WIN:
        return []
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {version.GAME_EXE}", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15, creationflags=flags,
        ).stdout or ""
    except Exception:              # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        # "Bodycam-Win64-Shipping.exe","12345","Console","1","1,234,567 K"
        fields = re.findall(r'"([^"]*)"', line)
        if len(fields) < 2 or fields[0].lower() != version.GAME_EXE.lower():
            continue
        try:
            pids.append(int(fields[1]))
        except ValueError:
            continue
    return pids


def _taskkill(pids, force: bool) -> bool:
    """One taskkill over `pids`. `force` adds /F /T. True when the command ran at all."""
    if not pids:
        return False
    args = ["taskkill"]
    if force:
        args += ["/F", "/T"]
    for pid in pids:
        args += ["/PID", str(pid)]
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(args, capture_output=True, text=True, timeout=20, creationflags=flags)
        return True
    except Exception:              # noqa: BLE001
        return False


def close_game(grace_seconds: float = GAME_CLOSE_GRACE_SECONDS, force: bool = True) -> str:
    """Shut Bodycam down. BLOCKS for up to `grace_seconds` — never call this on the UI thread.

    Returns what happened, for the log and the screen:
        "not-running"   there was nothing to close (also the answer off Windows)
        "closed"        it took the close request and went away by itself
        "forced"        the request was ignored, so it was killed (only when `force`)
        "failed"        it is still running and we are not going to kill it

    `force=False` is the honest option for a caller that would rather leave the window open
    than risk the hosting lockout a /F kill can look like; the caller then has to say so on
    screen, because a game left running is a player who cannot queue.
    """
    if not _WIN:
        return "not-running"
    pids = game_pids()
    if not pids:
        return "not-running"
    _taskkill(pids, force=False)               # WM_CLOSE: the engine's own shutdown path
    deadline = time.monotonic() + max(0.0, float(grace_seconds))
    while time.monotonic() < deadline:
        time.sleep(GAME_CLOSE_POLL_SECONDS)
        if not game_pids():
            return "closed"
    if not force:
        return "failed"
    # Still there. Whatever it is doing, it is not shutting down, and the player is stuck with a
    # window they have to close before they can queue. Kill the tree we found at the start — NOT
    # a fresh lookup, so a game the player has since relaunched by hand is never in the blast.
    _taskkill(pids, force=True)
    for _ in range(int(5 / GAME_CLOSE_POLL_SECONDS)):
        time.sleep(GAME_CLOSE_POLL_SECONDS)
        if not game_pids():
            return "forced"
    return "failed"
