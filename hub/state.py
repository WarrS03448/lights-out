"""The hub's little JSON state file.

    {"game_dir": "C:\\\\...\\\\Bodycam" | null,
     "installed": {"CTF": {"version": "1.0.0", "title": "Capture the Flag 10v10"}},
     "pak_sha256": "..." | null,
     "auth": {"token": "...", "steam_id": "7656...", "persona": "...", "avatar": "..."} | null,
     "tab": "gamemodes" | "competitive",
     "comp_sound_volume": 0-100 | null,  "comp_sound_last": 0-100 | null,  "comp_sound": bool | null,
     "matchmaking_region": "" | "NA" | "SA" | "EU" | "AS" | "OC" | "AF" | "ME",
     "matchmaking_cross_region": bool,
     "catalogue_cache": {...last catalogue we successfully fetched...} | null}

A missing or corrupt file is never fatal: we fall back to the defaults, because the
truth about what is installed is also checked against the pak on disk (see reconcile).

load() copies keys across ONE AT A TIME rather than accepting whatever the file holds, so
that a hand-edited or half-written state.json cannot make the hub read a string where it
expects a dict. The cost of that is real and was paid once: a key the app writes but this
function does not know about is silently dropped on the next start. Anything saved into
state.json needs a line in both default_state() and load(), or it does not persist.
"""
import json
import os
import tempfile
import threading

from . import catalogue
from . import game as game_mod
from . import paths
from .i18n import t


MATCHMAKING_REGIONS = ("NA", "SA", "EU", "AS", "OC", "AF", "ME")
INSTALL_FIELDS = ("installed", "pak_sha256", "game_dir")
_write_lock = threading.RLock()


def default_state() -> dict:
    return {"game_dir": None, "installed": {}, "pak_sha256": None, "catalogue_cache": None,
            "language": None,
            # Which tab was up when the hub last closed. Restored on start so a player who
            # shut the hub mid-match (or let the updater restart it) comes back to
            # Competitive and watches it rejoin, instead of to the gamemode list.
            "tab": None,
            # The Competitive match-found cue. Three keys, and all three are tri-state:
            # None means "the player has never said", which is NOT the same as 0 (muted) and
            # is what makes the default volume and the migration below possible.
            #   comp_sound_volume  the slider, 0-100
            #   comp_sound_last    where the slider was before the bell muted it, so it can go back
            #   comp_sound         the plain on/off flag a hub older than the slider wrote;
            #                      read once by CompetitivePanel.sound_volume() and never written
            "comp_sound_volume": None,
            "comp_sound_last": None,
            "comp_sound": None,
            # Empty means the player has not selected a region yet. Steam relay markers are opaque,
            # so the hub never invents geography from them, locale, or time zone. Cross-region play
            # is an explicit opt-in and still observes the service's hard ping ceiling.
            "matchmaking_region": "",
            "matchmaking_cross_region": False,
            "ui_click_volume": 35,
            "ui_click_enabled": True,
            # Competitive sign-in (2026-09-14): {"token", "steam_id", "persona", "avatar"}.
            # The token is an opaque server-issued string, not a Steam credential — the hub
            # never sees a Steam password (see hub/auth.py).
            "auth": None}


def load(path=None) -> dict:
    """Read state.json. Always returns a dict with every key present."""
    p = str(path or paths.state_file())
    st = default_state()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return st                       # missing or corrupt -> defaults
    if not isinstance(data, dict):
        return st
    if isinstance(data.get("game_dir"), str):
        st["game_dir"] = data["game_dir"]
    inst = data.get("installed")
    if isinstance(inst, dict):
        for mode_id, info in inst.items():
            if not isinstance(info, dict):
                continue
            titles = info.get("titles")
            st["installed"][str(mode_id)] = {
                "version": str(info.get("version", "")),
                "title": str(info.get("title", mode_id)),
                "titles": {str(k): str(v) for k, v in titles.items()} if isinstance(titles, dict) else {},
            }
            # The rules the pak was actually built with, cleaned the same way the catalogue side
            # cleans the server's override so the two compare equal. Dropping it here is what used
            # to leave a mode reading "Update available" forever: ops.py wrote the override with
            # the install, the next load threw it away, and cat.mode_update_available then saw
            # {} against the catalogue's override every time - an update that could never be done.
            over = catalogue.rules_override(info)
            if over:
                st["installed"][str(mode_id)]["rules_override"] = over
    if isinstance(data.get("pak_sha256"), str):
        st["pak_sha256"] = data["pak_sha256"]
    if isinstance(data.get("catalogue_cache"), dict):
        st["catalogue_cache"] = data["catalogue_cache"]
    if isinstance(data.get("language"), str):
        st["language"] = data["language"]   # validated against i18n.CODES by the app
    if isinstance(data.get("tab"), str):
        st["tab"] = data["tab"]             # validated against the real tabs by _show_tab
    for key in ("comp_sound_volume", "comp_sound_last", "ui_click_volume"):
        value = data.get(key)
        # `not isinstance(value, bool)` is doing real work: True IS an int in Python, and a
        # `true` in one of these fields is somebody's idea of the legacy flag, not a volume.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            st[key] = max(0, min(100, int(round(value))))   # sounds.clamp_volume, sans import
    if isinstance(data.get("comp_sound"), bool):
        st["comp_sound"] = data["comp_sound"]
    region = data.get("matchmaking_region")
    if region == "" or region in MATCHMAKING_REGIONS:
        st["matchmaking_region"] = region
    if isinstance(data.get("matchmaking_cross_region"), bool):
        st["matchmaking_cross_region"] = data["matchmaking_cross_region"]
    if isinstance(data.get("ui_click_enabled"), bool):
        st["ui_click_enabled"] = data["ui_click_enabled"]
    from . import credentials
    saved_auth = data.get("auth")
    if isinstance(saved_auth, dict):
        try:
            protected = "protected" in saved_auth
            if protected:
                saved_auth = credentials.open_sealed(saved_auth)
            if saved_auth.get("token") and (saved_auth.get("player_id") or saved_auth.get("steam_id")) \
                    and not str(saved_auth["token"]).startswith("lg_"):
                if not protected:
                    # Persist and verify protection before erasing legacy plaintext.
                    sealed = credentials.seal(saved_auth)
                    if credentials.open_sealed(sealed) != saved_auth:
                        raise credentials.CredentialError()
                    _write_state({**data, "auth": sealed}, path)
                st["auth"] = saved_auth
        except (credentials.CredentialError, OSError):
            st["auth"] = None
    return st


def save(state: dict, path=None) -> None:
    from . import credentials
    output = dict(state)
    account = output.get("auth")
    if isinstance(account, dict) and account.get("token"):
        output["auth"] = None if str(account["token"]).startswith("lg_") else credentials.seal(account)
    _write_state(output, path)


def _write_state(state: dict, path=None) -> None:
    """Write state.json atomically (temp file + os.replace) so a crash cannot corrupt it."""
    p = str(path or paths.state_file())
    with _write_lock:
        folder = os.path.dirname(os.path.abspath(p))
        os.makedirs(folder, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder, delete=False) as f:
                tmp = f.name
                json.dump(state, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, p)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)


def update_fields(changes: dict, path=None) -> None:
    """Merge only the caller's fields with the latest state under the same write lock."""
    with _write_lock:
        latest = load(path)
        latest.update(changes)
        if path is None:
            save(latest)
        else:
            save(latest, path)


def reconcile(state: dict, game_dir):
    """Make the state agree with what is actually on disk.

    If we think gamemodes are installed but our pak is gone from ~mods (the user deleted
    it, verified the game files, or reinstalled), forget the install. Returns a short
    note for the status line, or None when nothing changed."""
    if not state.get("installed"):
        return None
    if not game_dir:
        return None                      # cannot verify without a game folder
    if os.path.isfile(game_mod.pak_path(game_dir)):
        return None
    state["installed"] = {}
    state["pak_sha256"] = None
    return t("pak_gone")
