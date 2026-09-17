"""Steam avatars for the Competitive tab (2026-09-14).

The server hands us an avatar URL (from the Steam Web API, when STEAM_WEB_API_KEY is set).
This module turns that into a Tk image, once, and keeps it on disk so the hub does not
re-download a 64x64 png every time the window is redrawn.

Everything degrades quietly: no Pillow, no network, an odd URL, a corrupt file — the caller
just gets None and draws initials instead. An avatar is never worth an error dialog.

SECURITY: the URL arrives over the network, so it is not followed blindly. Only https and
only Steam's own avatar hosts, because a hub that fetches arbitrary URLs on command is a
hub that can be pointed at anything.
"""
import hashlib
import os
import urllib.parse
import urllib.request

from . import paths

ALLOWED_HOSTS = (
    "steamstatic.com",          # avatars.*.steamstatic.com, cdn.*, community.*
    "steamusercontent.com",
    "steamcommunity.com",
)
TIMEOUT_SECONDS = 10
MAX_BYTES = 512 * 1024          # a Steam avatar is a few KB; anything huge is not one


def is_allowed(url: str) -> bool:
    """https, and a host that is Steam's. Nothing else gets fetched."""
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def cache_path(url: str):
    name = hashlib.sha1(str(url).encode("utf-8")).hexdigest() + ".img"
    return paths.avatars_dir() / name


def fetch(url: str):
    """Download the avatar if it is not cached yet. Returns the file path, or None.

    Call this from a WORKER THREAD; it does network I/O."""
    if not is_allowed(url):
        return None
    path = cache_path(url)
    if path.is_file() and path.stat().st_size > 0:
        return path
    try:
        req = urllib.request.Request(url, headers={"user-agent": "LightsOut"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(MAX_BYTES + 1)
        if not data or len(data) > MAX_BYTES:
            return None
        tmp = str(path) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except Exception:            # noqa: BLE001 — offline, 404, disk full: draw initials
        return None


def load(url: str, size: int):
    """A circular PhotoImage of the cached avatar at `size` px, or None.

    Main thread only (it builds a Tk image). Returns None when the file is not cached yet,
    so the caller should fetch() on a worker first and redraw."""
    if not is_allowed(url):
        return None
    path = cache_path(url)
    if not path.is_file():
        return None
    try:
        from PIL import Image, ImageDraw, ImageTk
    except Exception:            # noqa: BLE001 — Pillow missing: initials are fine
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGBA").resize((size, size), Image.LANCZOS)
            # round it off, so it matches the initials placeholder it replaces
            mask = Image.new("L", (size * 4, size * 4), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
            img.putalpha(mask.resize((size, size), Image.LANCZOS))
            return ImageTk.PhotoImage(img)
    except Exception:            # noqa: BLE001 — corrupt download: fall back
        try:
            os.remove(path)      # and let the next fetch try again
        except OSError:
            pass
        return None
