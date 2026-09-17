"""Fetching catalogue.json and downloading the gamemode packs it lists.

catalogue.json:
    {"catalogue_version": 1,
     "hub": {"version": "1.1.0",
             "download_url": "https://.../hub/LightsOut-Setup-1.1.0.exe",
             "kind": "inno-setup",
             "page_url": "https://.../"},
     "gamemodes": [{"id": "CTF", "title": "Capture the Flag 10v10", "version": "1.0.0",
                    "description": "...", "pack_url": "https://.../packs/CTF-1.0.0.zip",
                    "sha256": "...", "size": 36748}]}

hub.kind is optional and not validated: "inno-setup" means the download is an installer the
hub runs silently (hub/update.py); absent means a portable exe the hub starts directly (the
pre-1.1.0 layout). Hubs that do not know the key ignore it. Only hub.version and
hub.download_url are required.

hub.required is optional too, and false unless it is there. A newer hub normally shows as a
strip along the top of the window that hides nothing and blocks nothing (Sam, 2026-09-14):
the player keeps their gamemode list, and a competitive match carries on behind it. Setting
"required": true brings back the all-or-nothing update screen, and is meant for the release
that genuinely cannot interoperate with the one before it - not for an ordinary one.

A pack zip contains manifest.json + cooked/ at its root; we extract it to
<packs_dir>/<id>-<version>/ and hand that folder to the builder.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

from . import i18n
from . import version as _version
from .i18n import t

USER_AGENT = f"LightsOut/{_version.HUB_VERSION}"

# Required keys, checked by validate_catalogue()
_MODE_REQUIRED = ("id", "title", "version", "pack_url", "sha256")


# ---------------------------------------------------------------- fetch + validate
def fetch_catalogue(url, timeout: int = 10) -> dict:
    """GET the catalogue and return it as a dict. Raises on any failure.

    file:// URLs work too, which is how the tests (and a local server run) exercise this.
    """
    req = urllib.request.Request(str(url), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Server returned HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach {url}: {e.reason}") from e
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise RuntimeError(f"{url} is not valid JSON: {e}") from e
    validate_catalogue(data)
    return data


def validate_catalogue(d) -> dict:
    """Raise ValueError unless `d` has the shape the hub relies on."""
    if not isinstance(d, dict):
        raise ValueError("catalogue is not a JSON object")
    if "catalogue_version" not in d:
        raise ValueError("catalogue: missing 'catalogue_version'")
    hub = d.get("hub")
    if not isinstance(hub, dict):
        raise ValueError("catalogue: missing 'hub' object")
    for key in ("version", "download_url"):
        if not hub.get(key):
            raise ValueError(f"catalogue: hub is missing '{key}'")
    modes = d.get("gamemodes")
    if not isinstance(modes, list):
        raise ValueError("catalogue: 'gamemodes' must be a list")
    seen = set()
    for i, m in enumerate(modes):
        if not isinstance(m, dict):
            raise ValueError(f"catalogue: gamemodes[{i}] is not an object")
        for key in _MODE_REQUIRED:
            if not m.get(key):
                raise ValueError(f"catalogue: gamemodes[{i}] is missing '{key}'")
        if m["id"] in seen:
            raise ValueError(f"catalogue: duplicate gamemode id {m['id']!r}")
        seen.add(m["id"])
    return d


def localize_to_repo(catalogue: dict, repo) -> dict:
    """--local mode: point every pack_url at the zip of the same name under
    <repo>/server/public/packs/ (file:// URL) when that zip exists, so the hub can be
    tested end-to-end from a checkout without a server and without editing the catalogue.
    Entries whose zip is missing keep their URL."""
    from urllib.request import pathname2url
    packs = os.path.join(str(repo), "server", "public", "packs")
    for m in catalogue.get("gamemodes", []):
        name = os.path.basename(str(m.get("pack_url", "")).split("?")[0])
        local = os.path.join(packs, name)
        if name and os.path.isfile(local):
            m["pack_url"] = "file:" + pathname2url(os.path.abspath(local))
    return catalogue


def entry_by_id(catalogue: dict, mode_id: str):
    """The gamemode entry with this id, or None."""
    for m in (catalogue or {}).get("gamemodes", []):
        if m.get("id") == mode_id:
            return m
    return None


def display_title(entry: dict, lang: str = None) -> str:
    """The gamemode's name in the hub's language: entry["titles"][lang] (optional, written
    by make_pack from the manifest's display_names), else the English `title`, else the id."""
    titles = entry.get("titles") or {}
    if not isinstance(titles, dict):
        titles = {}
    return (titles.get(lang or i18n.get_language()) or entry.get("title") or entry.get("id") or "?")


# ---------------------------------------------------------------- rules override (testing)
# Which manifest `rules` the server is allowed to overrule, and nothing else. The builder
# understands more, but these are the ones a test wants to shorten and the only ones whose effect
# is understood (tools/pak/build_gamemode.py: config_asset + the GamemodeInfo row).
RULES_OVERRIDE_KEYS = ("score_limit", "max_rounds", "team_switch_interval",
                       "time_limit", "team_size", "max_players")


def rules_override(entry: dict) -> dict:
    """The `rules_override` the catalogue carries for this gamemode, cleaned up.

    The server attaches it from COMP_GAME_RULES_OVERRIDE (server/server.cjs) so a test build can
    play two-round Bodybomb without anyone hand-editing a manifest.json on each machine: the hub
    merges this over the pack's own `rules` before building the pak, so the number in the pak is
    the number the server sent. {} when there is none - the ordinary case, and production.

    Anything that is not a number this hub knows about is dropped. This arrives over the network
    and is fed straight into struct.pack in the builder, so a string, a list or a key we do not
    recognise must never reach it.
    """
    raw = (entry or {}).get("rules_override")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in RULES_OVERRIDE_KEYS:
        if key not in raw:
            continue
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            continue
        out[key] = value if key == "time_limit" else int(value)
    return out


def mode_update_available(entry: dict, installed_info: dict) -> bool:
    """True when what is installed is not what the catalogue is offering: a newer version, or the
    SAME version built with different rules.

    The second half is what makes a rules override actionable. Without it, changing the override
    would change nothing anyone could press: the version has not moved, so the row would read
    "installed" and the Install button would stay grey while the pak on disk still played to
    seven."""
    if not isinstance(installed_info, dict) or entry.get("_orphan"):
        return False
    have = str(installed_info.get("version", "") or "")
    if version_newer(entry.get("version", ""), have):
        return True
    return rules_override(entry) != (installed_info.get("rules_override") or {})


# ---------------------------------------------------------------- versions
def _parts(v):
    """'1.10.2' -> [(1, ''), (10, ''), (2, '')]; a non-numeric chunk sorts before numbers."""
    out = []
    for chunk in str(v or "").strip().split("."):
        m = re.match(r"^(\d+)(.*)$", chunk)
        out.append((int(m.group(1)), m.group(2)) if m else (-1, chunk))
    return out


def version_newer(a, b) -> bool:
    """True when dotted version `a` is newer than `b` ('1.0.1' > '1.0'; '1.0' == '1.0.0')."""
    pa, pb = _parts(a), _parts(b)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else (0, "")
        y = pb[i] if i < len(pb) else (0, "")
        if x != y:
            return x > y
    return False


# ---------------------------------------------------------------- packs
def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _safe_members(zf: zipfile.ZipFile, dest: str):
    """Every member, checked so nothing can escape `dest` (zip slip)."""
    dest_real = os.path.realpath(dest)
    prefix = dest_real + os.sep
    members = []
    for info in zf.infolist():
        name = info.filename
        if name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:", name):
            raise ValueError(f"pack contains an absolute path: {name!r}")
        target = os.path.realpath(os.path.join(dest_real, name))
        if target != dest_real and not target.startswith(prefix):
            raise ValueError(f"pack contains a path outside the pack folder: {name!r}")
        members.append(info)
    return members


def _download(url, out_path, expect_sha, expect_size=None, progress=None) -> None:
    """Download `url` to `out_path`, reporting progress(done_bytes, total_bytes)."""
    req = urllib.request.Request(str(url), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, open(out_path, "wb") as out:
            total = expect_size or 0
            try:
                total = int(resp.headers.get("Content-Length") or total or 0)
            except (TypeError, ValueError):
                pass
            done = 0
            if progress:
                progress(0, total)
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except urllib.error.HTTPError as e:
        raise RuntimeError(t("download_failed", reason=f"HTTP {e.code}", url=url)) from e
    except urllib.error.URLError as e:
        raise RuntimeError(t("download_failed", reason=e.reason, url=url)) from e

    got = _sha256_file(out_path)
    if expect_sha and got.lower() != str(expect_sha).lower():
        try:
            os.remove(out_path)
        except OSError:
            pass
        raise RuntimeError(
            "The downloaded file does not match its checksum "
            f"(expected {expect_sha}, got {got}). Try again."
        )


def ensure_pack(entry: dict, packs_dir, progress=None) -> str:
    """Return the folder of the pack described by `entry`, downloading it if needed.

    <packs_dir>/<id>-<version>/ holding manifest.json counts as already downloaded.
    progress(done_bytes, total_bytes) is called while downloading."""
    mode_id = entry["id"]
    ver = entry["version"]
    packs_dir = str(packs_dir)
    dest = os.path.join(packs_dir, f"{mode_id}-{ver}")
    marker = os.path.join(dest, ".sha256")
    if os.path.isfile(os.path.join(dest, "manifest.json")):
        try:
            have = open(marker, encoding="utf-8").read().strip().lower()
        except OSError:
            have = ""
        if have == str(entry.get("sha256", "")).lower():
            return dest                               # already have exactly this pack
        shutil.rmtree(dest, ignore_errors=True)       # same version, different content: fetch it again

    os.makedirs(packs_dir, exist_ok=True)
    tmp_zip = None
    staging = None
    try:
        fd, tmp_zip = tempfile.mkstemp(prefix=f"{mode_id}-{ver}-", suffix=".zip", dir=packs_dir)
        os.close(fd)
        _download(entry["pack_url"], tmp_zip, entry.get("sha256"), entry.get("size"), progress)

        # Extract into a staging folder first, then swap it in: a half-extracted pack can
        # never be mistaken for a complete one.
        staging = tempfile.mkdtemp(prefix=f".{mode_id}-{ver}-", dir=packs_dir)
        with zipfile.ZipFile(tmp_zip) as zf:
            members = _safe_members(zf, staging)
            zf.extractall(staging, members=members)
        if not os.path.isfile(os.path.join(staging, "manifest.json")):
            raise RuntimeError(f"{mode_id} {ver}: the pack has no manifest.json")
        if not os.path.isdir(os.path.join(staging, "cooked")):
            raise RuntimeError(f"{mode_id} {ver}: the pack has no cooked/ folder")

        with open(os.path.join(staging, ".sha256"), "w", encoding="utf-8") as f:
            f.write(str(entry.get("sha256", "")).lower() + "\n")
        if os.path.exists(dest):
            shutil.rmtree(dest, ignore_errors=True)
        os.replace(staging, dest)
        staging = None
        return dest
    finally:
        if tmp_zip and os.path.exists(tmp_zip):
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
        if staging and os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
