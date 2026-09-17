"""Where the hub keeps its own files, and where the pak builder lives.

State layout (Windows: %LOCALAPPDATA%\\CommunityHub):
    state.json          installed gamemodes, game folder, last good catalogue
    packs/<id>-<ver>/   downloaded + extracted packs (manifest.json + cooked/)
    work/               builder scratch (it caches extracted stock files in work/stock)
    logs/last-run.log   full log of the last install/uninstall
    avatars/            cached Steam avatars for the Competitive tab
"""
import os
import sys
from pathlib import Path

from . import version

_WIN = sys.platform == "win32"


def state_dir() -> Path:
    """Root folder for everything the hub writes. Created on demand.

    The folder is still called CommunityHub (and .community-hub off Windows) after the rename
    to Lights Out, ON PURPOSE. It holds state.json, the downloaded packs and the signed-in
    account; renaming it would hand every existing install an empty state dir, so they would
    all be signed out and re-download every pack. It is not user-facing - nothing in the UI
    shows this path except the uninstall note - so the cost of moving it is real and the
    benefit is cosmetic. If it ever does move, it needs a migration that copies the old folder
    first, not a rename."""
    override = os.environ.get("HUB_STATE_DIR") or version.STATE_DIR_OVERRIDE
    if override:
        p = Path(override)
    elif _WIN:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        p = Path(base) / "CommunityHub"
    else:
        p = Path(os.path.expanduser("~")) / ".community-hub"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sub(name: str) -> Path:
    p = state_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def packs_dir() -> Path:
    """Downloaded gamemode packs, one folder per <id>-<version>."""
    return _sub("packs")


def work_dir() -> Path:
    """Scratch folder handed to build_from_packs (it caches stock files here)."""
    return _sub("work")


def logs_dir() -> Path:
    return _sub("logs")


def avatars_dir() -> Path:
    """Downloaded Steam avatars, one file per URL (hub/avatars.py)."""
    return _sub("avatars")


def state_file() -> Path:
    return state_dir() / "state.json"


def log_file() -> Path:
    return logs_dir() / "last-run.log"


def tools_dir() -> Path:
    """The folder that holds the pak builder (build_gamemode.py and its helpers).

    Frozen by PyInstaller (one-folder build) the data files live in <dist>/LightsOut/_internal,
    which is what sys._MEIPASS points at, so it is <sys._MEIPASS>/tools/pak; running from source
    the repo root is the parent of this package, so it is <repo>/tools/pak.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:                                   # PyInstaller build (_internal/ in onedir)
        return Path(meipass) / "tools" / "pak"
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "tools" / "pak"


def assets_dir() -> Path:
    """hub/assets (icon files). Frozen: <_MEIPASS>/hub/assets (= _internal/hub/assets in the
    one-folder build); from source: next to this package."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "hub" / "assets"
    return Path(__file__).resolve().parent / "assets"


def webui_dir() -> Path:
    """hub/webui/static (the web UI's index.html, css, js, fonts). Frozen:
    <_MEIPASS>/hub/webui/static (= _internal/hub/webui/static in the one-folder build);
    from source: next to this package. Bundled by --add-data exactly like tools/pak and
    hub/assets, and resolved through sys._MEIPASS the same way."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "hub" / "webui" / "static"
    return Path(__file__).resolve().parent / "webui" / "static"


def repo_root() -> "Path | None":
    """The Gamemode Project checkout this hub belongs to, or None.

    From source: the parent of this package. Frozen: the exe lives in <repo>/dist/LightsOut/
    (one-folder build), so walk up from its folder taking the first parent that really looks like the
    repo (server/public exists). Two levels are the real cases — dist/LightsOut/ and a stray copy
    in dist/ — but we allow three for a bit of slack. An installed hub
    (the installer's program folder — %USERPROFILE%\\Downloads\\LightsOut) finds
    nothing and gets None, which is what --local reports as an error."""
    if getattr(sys, "frozen", False):
        cand = Path(sys.executable).resolve().parent
        for _ in range(3):
            cand = cand.parent
            if (cand / "server" / "public").is_dir():
                return cand
        return None
    cand = Path(__file__).resolve().parent.parent
    return cand if (cand / "server" / "public").is_dir() else None


def ensure_builder_on_path() -> str:
    """Make `import build_gamemode` work. Returns the folder that was put on sys.path."""
    d = str(tools_dir())
    if d not in sys.path:
        sys.path.insert(0, d)
    return d
