"""Uninstalling Lights Out from inside Lights Out.

WHY THIS EXISTS. The hub installs itself with Inno Setup (hub/installer.iss), which registers a
perfectly good uninstaller in Apps & features — but nothing in the app ever pointed at it, and the
program folder is <Downloads>\\LightsOut, which nobody thinks to look in. Worse, the Windows
uninstaller only removes ``{app}``: the files we wrote into somebody's *Bodycam* folder
(CommunityGamemodes_P.pak and, if a match was in flight, CommunityLobby_P.pak) are OURS and would
be left behind, modding a game the player just told us to stop modding. So "uninstall" here means
two things in a fixed order:

    1. put the game back the way we found it   (remove_game_files, below)
    2. hand over to the Windows uninstaller    (find_uninstaller + launch)

and only then does the hub quit. Step 1 is the part only we can do, which is why it happens first
and why a running game is a hard refusal (a mounted pak is locked; deleting it either fails on the
lock or is silently ignored, and which one is unmeasured — see lobbypak.install).

STATE IS KEPT BY DEFAULT. %LOCALAPPDATA%\\CommunityHub (state.json, the downloaded packs, the
signed-in account) survives, exactly as installer.iss decided it should, so a reinstall picks up
where the player left off. ``wipe_data=True`` is the opt-in for a player who wants it gone; it is a
separate tick box because it is the only irreversible part of this.

NOT FROZEN = NOT AVAILABLE. Running from source there is no Inno install to remove, so
``availability()`` reports why and the button is disabled rather than doing something surprising to
a developer's checkout.
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile

from . import game as game_mod
from . import lobbypak
from . import paths
from . import version

_WIN = os.name == "nt"

# The Inno Setup AppId from hub/installer.iss with Inno's "_is1" suffix — the name of the registry
# key it writes its uninstall entry under. It must match installer.iss exactly; a mismatch here just
# means we fall back to finding unins*.exe on disk, which is the primary route anyway.
INNO_APP_ID = "{2d78e401-2c1f-483f-9b67-51407892cac7}_is1"
_UNINSTALL_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall" + "\\" + INNO_APP_ID

# The files in <GameDir>\Bodycam\Content\Paks\~mods that belong to us. Anything else in ~mods is
# another mod and is NEVER touched (game.other_mod_paks exists to say so out loud).
OUR_PAKS = (version.PAK_NAME, lobbypak.LOBBY_PAK_NAME)


# ---------------------------------------------------------------- finding the uninstaller
def program_dir() -> str:
    """The installed program folder (where LightsOut.exe and unins000.exe live), or ""."""
    if not getattr(sys, "frozen", False):
        return ""
    return os.path.dirname(os.path.abspath(sys.executable))


def _from_registry() -> str:
    """The UninstallString Inno wrote, with its quotes and switches stripped. "" if absent."""
    if not _WIN:
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, _UNINSTALL_SUBKEY) as key:
                raw, _ = winreg.QueryValueEx(key, "UninstallString")
        except OSError:
            continue
        except Exception:                      # noqa: BLE001 — a broken value must not crash us
            continue
        path = str(raw or "").strip()
        if path.startswith('"'):               # "C:\...\unins000.exe" /SWITCH  ->  C:\...\unins000.exe
            path = path[1:].split('"', 1)[0]
        else:
            path = path.split(" /", 1)[0].strip()
        if path and os.path.isfile(path):
            return os.path.normpath(path)
    return ""


def find_uninstaller() -> str:
    """Full path of the Inno uninstaller for THIS copy, or "" when there is not one.

    FROZEN ONLY, and that is the whole point of the check. The registry entry below belongs to
    whatever Lights Out the machine has installed, which from a dev checkout is somebody ELSE'S
    copy: a `python hub_entry.py` offering to uninstall the installed hub would quit the process
    you are running while removing a program you never launched. Disk first (unins000.exe sits
    next to the exe we are running, so it is unambiguously ours), registry second, for an install
    whose folder we somehow cannot read."""
    if not getattr(sys, "frozen", False):
        return ""
    d = program_dir()
    if d:
        found = sorted(glob.glob(os.path.join(d, "unins*.exe")))
        if found:
            return os.path.normpath(found[0])
    return _from_registry()


def availability() -> tuple:
    """(path, reason) — reason is "" when uninstalling is possible, else a key for the UI.

    "not_installed" covers both halves of the same answer: running from source, and a frozen build
    that is not an Inno install (someone unzipped the dist folder). Both mean there is no
    uninstall entry of OUR OWN to hand over to (see find_uninstaller)."""
    if not _WIN:
        return "", "not_windows"
    path = find_uninstaller()
    if not path:
        return "", "not_installed"
    return path, ""


# ---------------------------------------------------------------- putting the game back
def game_files(game_dir) -> list:
    """Our files that are currently sitting in the player's ~mods folder. Full paths, sorted.

    Includes the ``.tmp`` half-written forms, because an install or a lobby swap that died leaves
    one behind and it is just as much ours to clean up."""
    if not game_dir:
        return []
    mods = game_mod.mods_dir(game_dir, create=False)
    if not os.path.isdir(mods):
        return []
    out = []
    for name in OUR_PAKS:
        for path in (os.path.join(mods, name), os.path.join(mods, name + ".tmp")):
            if os.path.isfile(path):
                out.append(path)
    return sorted(out)


def remove_game_files(game_dir, log=None) -> dict:
    """Take our paks out of ~mods and leave the stock layout behind.

    Returns {"removed": [names], "failed": [names]} and never raises: a file we cannot delete is
    reported, not fatal — the app uninstall must still happen. The ~mods folder itself goes only
    when it ends up EMPTY; another mod living there keeps it."""
    def say(msg):
        if log:
            try:
                log(str(msg))
            except Exception:                  # noqa: BLE001
                pass

    removed, failed = [], []
    for path in game_files(game_dir):
        try:
            os.remove(path)
            removed.append(os.path.basename(path))
            say("Removed %s" % path)
        except OSError as e:
            failed.append(os.path.basename(path))
            say("Could not remove %s (%s)" % (path, e))

    if game_dir:
        mods = game_mod.mods_dir(game_dir, create=False)
        try:
            if os.path.isdir(mods) and not os.listdir(mods):
                os.rmdir(mods)
                say("Removed the empty %s" % mods)
        except OSError as e:
            say("Note: could not remove the empty ~mods folder: %s" % e)
    return {"removed": removed, "failed": failed}


def wipe_state(log=None) -> bool:
    """Delete the hub's own folder (state.json, packs, avatars, logs, the saved sign-in).

    Opt-in only. Best effort: the hub is still running, so a log file it is holding open can
    survive, and that is fine — the Windows uninstaller is about to take the program folder and
    what is left here is inert."""
    d = str(paths.state_dir())
    shutil.rmtree(d, ignore_errors=True)
    gone = not os.path.isdir(d)
    if log:
        try:
            log(("Removed %s" if gone else "Note: some files remain in %s") % d)
        except Exception:                      # noqa: BLE001
            pass
    return gone


# ---------------------------------------------------------------- handing over to Windows
def log_path() -> str:
    """Where the uninstaller's own log goes. TEMP, not <state>/logs, on purpose: with
    ``wipe_data`` the state folder is deleted moments earlier and recreating it to hold a log
    would leave exactly the folder the player asked us to remove."""
    return os.path.join(tempfile.gettempdir(), "LightsOut-uninstall.log")


def uninstall_command(path, log=None) -> list:
    """argv for the Inno uninstaller.

    /SILENT, not /VERYSILENT: the modal in Settings was the confirmation, so the extra "are you
    sure" is noise, but a progress window is the only sign the player gets that anything is
    happening after the hub disappears. /SUPPRESSMSGBOXES drops the confirmation and the final
    message box with it; /NORESTART because nothing we install ever needs one."""
    return [str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/LOG=%s" % (log or log_path())]


def launch(path, log=None) -> None:
    """Start the uninstaller detached, so it outlives the hub quitting a moment later."""
    argv = uninstall_command(path, log)
    if _WIN:
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(argv, close_fds=True, creationflags=flags)      # noqa: S603
    else:
        subprocess.Popen(argv, close_fds=True, start_new_session=True)   # noqa: S603


# ---------------------------------------------------------------- the whole thing
def perform(game_dir, wipe_data=False, log=None) -> dict:
    """Clean the game folder, optionally wipe our state, then start the Windows uninstaller.

    Returns {"ok", "error", "removed", "failed", "wiped", "uninstaller"}. The caller quits the hub
    when ``ok`` — and must NOT quit when it is false, because then nothing has been handed over and
    the player is owed the message.

    The game-running check is the exact one (game_running(), not the cached display value): this is
    a decision about whether we may delete a file the game may have mounted.
    """
    def say(msg):
        if log:
            try:
                log(str(msg))
            except Exception:                  # noqa: BLE001
                pass

    path, reason = availability()
    if reason:
        return {"ok": False, "error": reason, "removed": [], "failed": [],
                "wiped": False, "uninstaller": ""}
    if game_mod.game_running():
        return {"ok": False, "error": "game_running", "removed": [], "failed": [],
                "wiped": False, "uninstaller": path}

    say("%s %s - uninstalling" % (version.APP_NAME, version.HUB_VERSION))
    from . import recording
    if recording.enabled():
        try:
            recording.restore()
        except Exception as exc:
            say(str(exc))
            return {"ok":False,"error":"launch_failed","removed":[],"failed":[],"wiped":False,"uninstaller":path}
        files = {"removed":[],"failed":[]}
    else:
        files = remove_game_files(game_dir, log=say)
    wiped = wipe_state(log=say) if wipe_data else False
    say("Starting %s" % path)
    try:
        launch(path)
    except OSError as e:
        say("Could not start the uninstaller: %s" % e)
        return {"ok": False, "error": "launch_failed", "removed": files["removed"],
                "failed": files["failed"], "wiped": wiped, "uninstaller": path}
    return {"ok": True, "error": "", "removed": files["removed"], "failed": files["failed"],
            "wiped": wiped, "uninstaller": path}
