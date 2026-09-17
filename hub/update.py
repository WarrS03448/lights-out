"""Hub self-update (Sam, 2026-09-14): when the catalogue's hub.version is newer than this exe,
the window offers the update — download it, start it, done.

How that offer LOOKS is hub/app.py's business, and it is now a slim strip along the
top of the window that covers nothing and blocks nothing: the gamemode list still works, and
so does a competitive match running behind it. Downloading is safe at any moment (it writes
one file into <state>/updates and touches nothing else); only `launch` below is disruptive,
which is why it takes a second, deliberate click and, mid-match, a plain warning first. The
match itself survives the restart — it lives on the service, not in this process, and the new
hub reconnects and is handed the phase back. A catalogue that sets hub.required brings back
the old all-or-nothing update screen.

Since 1.1.0 the download is the Inno Setup installer (catalogue `hub.kind == "inno-setup"`).
It is saved to <state>/updates (Downloads only when that is not writable — never beside the
exe, which now lives in the installer's program folder), then run silently: the installer
closes this hub, replaces the installed files and starts the new hub itself (/LAUNCHHUB=1);
its log goes to <state>/logs/update-install.log. The installer is still running when it
launches the new hub, so `clean_old_versions()` throws the downloaded installer away on a
later start (whichever one first finds it no longer locked), not necessarily the next one.

Without `kind` (a legacy portable LightsOut-<v>.exe / CommunityHub-<v>.exe) the download is
simply started and this hub quits; `clean_old_versions()` also still removes older exe files
of either name beside a frozen exe, which is what pre-1.1.0 hubs left behind.
"""
import os
import re
import subprocess
import sys
import tempfile

from . import paths, version

# Both names on purpose: an install that predates the Lights Out rename can still have
# CommunityHub-<v>.exe sitting beside it, and this is what sweeps it up.
_EXE_RE = re.compile(r"^(?:LightsOut|CommunityHub)-(\d+(?:\.\d+)*)\.exe$", re.IGNORECASE)
INSTALLER_KIND = "inno-setup"


def own_exe():
    """Path of the running exe when frozen, else None."""
    return sys.executable if getattr(sys, "frozen", False) else None


def updates_dir() -> str:
    """<state>/updates — where the downloaded installer goes (not created here)."""
    return str(paths.state_dir() / "updates")


def candidate_dirs():
    """Where to put the download, in order of preference: the hub's own state folder, then
    Downloads as the last resort. Never beside the running exe."""
    return [updates_dir(), os.path.join(os.path.expanduser("~"), "Downloads")]


def dest_path(url: str, new_version: str):
    """<writable folder>/<basename of url or LightsOut-<v>.exe>, never the running exe itself."""
    name = os.path.basename(str(url).split("?")[0]) or f"LightsOut-{new_version}.exe"
    if not name.lower().endswith(".exe"):
        name = f"LightsOut-{new_version}.exe"
    exe = own_exe()
    for d in candidate_dirs():
        try:
            os.makedirs(d, exist_ok=True)
            fd, probe = tempfile.mkstemp(prefix=".hubw", dir=d)
            os.close(fd)
            os.remove(probe)
        except OSError:
            continue
        p = os.path.join(d, name)
        # Defence only: candidate_dirs never returns the exe's folder, so this cannot fire today.
        if exe and os.path.normcase(os.path.abspath(p)) == os.path.normcase(os.path.abspath(exe)):
            p = os.path.join(d, f"LightsOut-{new_version}-new.exe")
        return p
    raise OSError("no writable folder for the update")


def download(url: str, dest: str, progress=None, expect_sha=None) -> str:
    """Download `url` to `dest` (atomically via dest.part). progress(done, total)."""
    from .catalogue import _download
    part = dest + ".part"
    _download(url, part, expect_sha, None, progress)
    os.replace(part, dest)
    return dest


def installer_command(path: str, log_path: str) -> list:
    """argv that runs the Inno Setup installer silently and has it start the installed hub."""
    return [str(path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS",
            "/LAUNCHHUB=1", f"/LOG={log_path}"]


def launch(path: str, kind=None) -> None:
    """Start the download; the caller quits right after.

    kind == "inno-setup": run the installer silently, detached from this process so it survives
    the hub quitting (and the installer closing the hub). Anything else: start the file as is
    (legacy portable exe)."""
    if kind == INSTALLER_KIND:
        argv = installer_command(path, str(paths.logs_dir() / "update-install.log"))
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen(argv, close_fds=True, creationflags=flags)     # noqa: S603
        else:
            subprocess.Popen(argv, close_fds=True, start_new_session=True)  # noqa: S603
        return
    if os.name == "nt":
        os.startfile(path)                               # noqa: S606 — the exe we just downloaded
    else:
        subprocess.Popen([path], close_fds=True)         # noqa: S603


def clean_old_versions() -> list:
    """Tidy up on start: the downloaded installer (*.exe, *.part in <state>/updates) and, when
    frozen, older LightsOut-<v>.exe (and pre-rename CommunityHub-<v>.exe) files beside the
    running exe. Returns the removed names."""
    removed = []
    d = updates_dir()
    try:
        names = os.listdir(d)
    except OSError:
        names = []
    for name in names:
        if name.lower().endswith((".exe", ".part")):
            try:
                os.remove(os.path.join(d, name))
                removed.append(name)
            except OSError:
                pass
    exe = own_exe()
    if not exe:
        return removed
    from .catalogue import version_newer
    folder = os.path.dirname(os.path.abspath(exe))
    for name in os.listdir(folder):
        m = _EXE_RE.match(name)
        if not m or os.path.normcase(name) == os.path.normcase(os.path.basename(exe)):
            continue
        if version_newer(version.HUB_VERSION, m.group(1)):
            try:
                os.remove(os.path.join(folder, name))
                removed.append(name)
            except OSError:
                pass
    return removed
