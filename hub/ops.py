"""Install / uninstall orchestration. No tkinter in here on purpose: app.py drives this
from a worker thread and the tests drive it headlessly.

The whole install is "build a new pak somewhere else, then swap it in":

    <mods>/CommunityGamemodes_P.pak.tmp     <- built here (a few seconds, reported step by step)
    os.replace(tmp, <mods>/CommunityGamemodes_P.pak)   <- atomic on Windows and POSIX

so any failure (download, build, checksum) leaves whatever the player had installed
before completely untouched.
"""
import hashlib
import inspect
import os
import time
import traceback

from . import catalogue as cat
from . import game as game_mod
from . import paths
from . import state as state_mod
from .i18n import t
from . import version

# Resolved lazily by _load_builder() so that importing this module never needs the
# builder (or pyooz) to be present. The tests replace this with a fake.
build_from_packs = None


def _load_builder():
    global build_from_packs
    if build_from_packs is None:
        paths.ensure_builder_on_path()
        try:
            from build_gamemode import build_from_packs as _b   # noqa: WPS433 (lazy on purpose)
        except Exception as e:
            raise RuntimeError(
                "The pak builder could not be loaded, so the install is incomplete "
                f"({e})."
            ) from e
        build_from_packs = _b
    return build_from_packs


# ---------------------------------------------------------------- plan
class Plan:
    """What pressing Install / Uninstall would do, given the checkboxes.

    Supports both plan.install and plan["install"] so the UI and the tests can use
    whichever reads better."""

    __slots__ = ("install", "uninstall")

    def __init__(self, install, uninstall):
        self.install = list(install)
        self.uninstall = list(uninstall)

    @property
    def can_install(self) -> bool:
        """At least one selected gamemode is not installed yet."""
        return bool(self.install)

    @property
    def can_uninstall(self) -> bool:
        """At least one selected gamemode is installed."""
        return bool(self.uninstall)

    def __getitem__(self, key):
        return getattr(self, key)

    def __eq__(self, other):
        if isinstance(other, Plan):
            return self.install == other.install and self.uninstall == other.uninstall
        if isinstance(other, dict):
            return self.install == list(other.get("install", [])) and \
                self.uninstall == list(other.get("uninstall", []))
        return NotImplemented

    def __repr__(self):
        return (f"Plan(install={self.install!r}, uninstall={self.uninstall!r}, "
                f"can_install={self.can_install}, can_uninstall={self.can_uninstall})")


def plan(selected_ids, installed_ids) -> Plan:
    """selected -> the rows the user highlighted; installed -> what is in the pak right now.
    install   = selected gamemodes that are not installed      (Install adds them)
    uninstall = selected gamemodes that are installed          (Uninstall removes them)"""
    selected = set(selected_ids or ())
    installed = set(installed_ids or ())
    return Plan(sorted(selected - installed), sorted(selected & installed))


# ---------------------------------------------------------------- logging helper
class _Log:
    """Tees every line to the caller's log callback and to <state>/logs/last-run.log."""

    def __init__(self, log=None, path=None):
        self._cb = log
        self._fh = None
        try:
            self._fh = open(str(path or paths.log_file()), "w", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None

    def __call__(self, msg=""):
        line = str(msg)
        if self._cb:
            try:
                self._cb(line)
            except Exception:
                pass
        if self._fh:
            try:
                self._fh.write(f"{time.strftime('%H:%M:%S')}  {line}\n")
                self._fh.flush()
            except OSError:
                pass

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def _noop_progress(fraction, message):
    pass


# Where each stage of an install sits on the 0..1 bar the UI draws. The packs are tiny (tens of
# KB) and the build is the minutes-long-feeling part, so the build owns most of the bar.
DOWNLOAD_BAND = (0.02, 0.18)
BUILD_BAND = (0.18, 0.94)


def _builder_takes_progress() -> bool:
    """Whether the loaded builder reports build progress (build_from_packs(..., progress=...)).

    The builder is resolved at runtime from tools/pak, and the tests swap in fakes that only take
    `log`, so the kwarg is offered, never assumed. Without it the build step is one long
    indeterminate stretch, exactly as it was before."""
    try:
        return "progress" in inspect.signature(build_from_packs).parameters
    except (TypeError, ValueError):           # builtins / C callables have no signature
        return False


def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------- apply
def apply(desired_ids, catalogue, state, game_dir, log=None, progress=None) -> dict:
    """Make the installed set exactly `desired_ids`.

    desired_ids   ids that must end up installed (empty set = uninstall everything)
    catalogue     the dict from catalogue.fetch_catalogue (only needed when installing)
    state         the dict from state.load(); updated and saved here
    game_dir      <GameDir>
    log(str)      every step
    progress(f, msg)  f is 0..1 or None (unknown/indeterminate)

    Returns {"installed": [...], "pak": path|None, "removed": bool}. Raises RuntimeError
    with a message meant for the status bar on any failure; the previously installed pak
    is never touched except by the final os.replace / os.remove.
    """
    desired = sorted(set(desired_ids or ()))
    progress = progress or _noop_progress
    out = _Log(log)
    tmp_out = None
    try:
        out(f"{version.APP_NAME} {version.HUB_VERSION}")
        out(f"Desired gamemodes: {', '.join(desired) if desired else '(none)'}")
        progress(0.0, t("checking"))

        # ---- refuse while the game is running: the pak would be locked or ignored
        if game_mod.game_running():
            raise RuntimeError(t("close_game"))

        if not game_dir or not game_mod.is_game_dir(game_dir):
            raise RuntimeError(t("no_game_dir"))

        # ~mods does not exist in a stock install (the game ships none): it is created only
        # when there is something to put in it, and removed again when it ends up empty.
        mods = game_mod.mods_dir(game_dir, create=bool(desired))
        pak = os.path.join(mods, version.PAK_NAME)
        out(f"Game folder: {game_dir}")
        out(f"Mods folder: {mods}")

        others = game_mod.other_mod_paks(game_dir)
        if others:
            out(f"Note: other mods are present in ~mods: {', '.join(os.path.basename(p) for p in others)}")

        # ---- nothing wanted: remove our pak and forget the install
        if not desired:
            if os.path.isfile(pak):
                progress(0.5, t("removing_pak"))
                if game_mod.game_running():
                    raise RuntimeError(t("close_game"))
                out(f"Removing {pak}")
                os.remove(pak)
            else:
                out("Nothing to remove (no pak installed).")
            # back to the stock layout: no ~mods folder at all, unless someone else's files live there
            if os.path.isdir(mods) and not os.listdir(mods):
                try:
                    os.rmdir(mods)
                    out(f"Removed the empty {mods}")
                except OSError as e:
                    out(f"Note: could not remove the empty ~mods folder: {e}")
            state["installed"] = {}
            state["pak_sha256"] = None
            state["game_dir"] = str(game_dir)
            state_mod.update_fields({key: state[key] for key in state_mod.INSTALL_FIELDS})
            progress(1.0, t("done_uninstalled"))
            out("Done: uninstalled.")
            return {"installed": [], "pak": None, "removed": True}

        # ---- make sure every wanted pack is on disk
        entries = []
        for mode_id in desired:
            entry = cat.entry_by_id(catalogue or {}, mode_id)
            if not entry:
                raise RuntimeError(t("not_listed", id=mode_id))
            entries.append(entry)

        pack_dirs = []
        for i, entry in enumerate(entries):
            base = DOWNLOAD_BAND[0] + (DOWNLOAD_BAND[1] - DOWNLOAD_BAND[0]) * (i / max(len(entries), 1))
            span = (DOWNLOAD_BAND[1] - DOWNLOAD_BAND[0]) / max(len(entries), 1)
            title = cat.display_title(entry)

            def _dl_progress(done, total, _base=base, _span=span, _title=title):
                if total:
                    frac = _base + _span * min(done / total, 1.0)
                    progress(frac, t("downloading_pct", title=_title, pct=done * 100 // total))
                else:
                    progress(None, t("downloading", title=_title))

            progress(base, t("preparing", title=title))
            out(f"Pack {entry['id']} {entry['version']}: checking local copy")
            pack_dir = cat.ensure_pack(entry, paths.packs_dir(), progress=_dl_progress)
            out(f"Pack {entry['id']} {entry['version']}: {pack_dir}")
            pack_dirs.append(pack_dir)

        # ---- build the merged pak next to the real one, then swap it in
        _load_builder()
        tmp_out = pak + ".tmp"
        if os.path.exists(tmp_out):
            os.remove(tmp_out)                      # leftover from an interrupted run
        progress(BUILD_BAND[0], t("building"))
        out(f"Building {tmp_out} from {len(pack_dirs)} pack(s)")
        build_kwargs = {}
        if _builder_takes_progress():
            lo, hi = BUILD_BAND
            # The builder reports per zlib block and per record, which is hundreds of callbacks in
            # a couple of seconds; each one costs the web UI a snapshot and a re-render. Forward at
            # most one every 100 ms, and only when the bar would actually move a percent.
            last = [0.0, 0.0]                   # [fraction emitted, when]

            def _build_progress(fraction, _step):
                now = time.monotonic()
                if fraction - last[0] < 0.01 and now - last[1] < 0.1:
                    return
                last[0], last[1] = fraction, now
                progress(lo + (hi - lo) * fraction, t("building"))

            build_kwargs["progress"] = _build_progress
        # The catalogue may carry a rules override for some of these modes (a test build playing
        # two-round Bodybomb, say). It is merged over the pack's manifest INSIDE the builder, so
        # the pack on disk stays exactly the bytes we checksummed and a later build without the
        # override goes back to the shipped rules on its own.
        overrides = {}
        for e in entries:
            over = cat.rules_override(e)
            if over:
                overrides[e["id"]] = over
                out(f"Rules override for {e['id']}: " + ", ".join(f"{k}={v}" for k, v in sorted(over.items())))
        if overrides:
            build_kwargs["rules_override"] = overrides
        result = build_from_packs(
            game_mod.paks_dir(game_dir), str(paths.work_dir()), pack_dirs, tmp_out, log=out,
            **build_kwargs
        )
        if not os.path.isfile(tmp_out):
            raise RuntimeError(t("no_pak_produced"))

        progress(0.95, t("installing"))
        if game_mod.game_running():
            raise RuntimeError(t("close_game"))
        os.replace(tmp_out, pak)                    # atomic swap
        tmp_out = None
        out(f"Installed {pak} ({os.path.getsize(pak)} bytes)")

        sha = (result or {}).get("sha256") if isinstance(result, dict) else None
        if not sha:
            sha = _sha256_file(pak)

        # An override is remembered with the version, because it is the other half of "what is
        # actually in the pak": the same pack version built with different rules is a different
        # install, and cat.mode_update_available compares both. Only written when there IS one, so
        # an ordinary install keeps the state file it has always had.
        state["installed"] = {
            e["id"]: {"version": str(e.get("version", "")), "title": str(e.get("title", e["id"])),
                      "titles": dict(e.get("titles") or {}),
                      **({"rules_override": overrides[e["id"]]} if e["id"] in overrides else {})}
            for e in entries
        }
        state["pak_sha256"] = sha
        state["game_dir"] = str(game_dir)
        state_mod.update_fields({key: state[key] for key in state_mod.INSTALL_FIELDS})

        progress(1.0, t("done"))
        out(f"Done: installed {', '.join(desired)}")
        return {"installed": list(desired), "pak": pak, "removed": False}

    except Exception as e:
        out("FAILED: " + str(e))
        out(traceback.format_exc())
        raise RuntimeError(str(e) or e.__class__.__name__) from e
    finally:
        # A half-built pak must never be left lying around; the real pak is untouched.
        if tmp_out and os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass
        out.close()
