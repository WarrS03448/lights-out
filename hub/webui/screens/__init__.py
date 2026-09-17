"""Per-screen Python contributors: each screen's snapshot slice and its bridge verbs.

The web UI is modular so screens can be built in parallel without two agents touching the same
file. On the Python side that means each screen owns ONE module in this package:

    hub/webui/screens/<name>.py

and contributes to the app through two registries defined here — never by editing a shared
function body or method list:

  * register_snapshot(name)(fn):  fn(session, panel) -> dict is MERGED into the state snapshot
    (hub/webui/snapshot.py). Competitive contributes the top-level ``comp`` and ``party`` keys;
    a new screen returns ``{"<name>": {...}}`` to add its own namespaced slice.

  * register_verbs(name, {verb: fn}):  each fn(panel, *args) becomes a bridge verb
    (hub/webui/httpbridge.py, via bridge.Api) callable from JS as ``POST /verb/<verb>``. Verbs are
    1:1 passthroughs to session/panel actions on the UI thread — nothing here is authoritative.

The screen modules are imported EXPLICITLY at the bottom of this file (``_SCREEN_MODULES``), not
discovered dynamically. That is deliberate and load-bearing: a PyInstaller-frozen build only
bundles a module that is reachable by STATIC import analysis, and ``pkgutil.iter_modules`` returns
nothing for a frozen package — so dynamic discovery silently shipped an app with ZERO screens
registered (raw i18n keys, dead verbs, no sign-in). Adding a screen therefore means BOTH creating
the file AND adding its name to ``_SCREEN_MODULES``; tests/test_hub.py asserts the list matches the
files on disk, so a forgotten entry fails the build instead of the user's download.

This package imports no pywebview and no Tk, so it stays testable headless.
"""
import importlib

# name -> fn(session, panel) -> dict   (merged into the state snapshot)
SCREEN_SNAPSHOTS = {}
# verb name -> fn(panel, *args)        (installed as bridge verbs)
SCREEN_VERBS = {}

# The screens, imported explicitly below. Keep in sync with the *.py files in this package (a test
# enforces it). Order is irrelevant: registries are keyed by name and merged.
_SCREEN_MODULES = ("competitive", "gamemodes", "profile", "history", "leaderboard",
                   "friends", "settings", "postmatch", "bugreport")


def register_snapshot(name):
    """Decorator: register a screen's snapshot contributor under ``name``."""
    def deco(fn):
        SCREEN_SNAPSHOTS[name] = fn
        return fn
    return deco


def register_verbs(name, verbs):
    """Register a screen's bridge verbs. ``verbs`` is a {verb_name: fn(panel, *args)} dict.

    Verb names are global (they become ``POST /verb/<verb>``), so keep them screen-scoped enough
    not to collide with another screen's verbs or the core verbs (ready, get_state, set_language,
    set_view)."""
    for verb, fn in dict(verbs).items():
        SCREEN_VERBS[verb] = fn
    return verbs


def load_screens():
    """Import every screen module so its registrations run. Idempotent.

    The static imports at the bottom already do this on first package import; this stays for tests
    and callers that want to (re)ensure the registries are populated."""
    for name in _SCREEN_MODULES:
        importlib.import_module("%s.%s" % (__name__, name))


def _screen_files_on_disk():
    """The screen module names present as .py files in this package (dev-only; used by the test
    that keeps _SCREEN_MODULES honest). Returns a set, excluding this __init__."""
    import os
    here = os.path.dirname(__file__)
    names = set()
    for fn in os.listdir(here):
        if fn.endswith(".py") and not fn.startswith("_"):
            names.add(fn[:-3])
    return names


# EXPLICIT static imports so a frozen build bundles each screen and its register_* calls run. This
# line is the one PyInstaller's analyser follows; DO NOT replace it with dynamic discovery. Runs
# after the registry functions above are defined, so each screen's module-level registration finds
# them. Keep this list == _SCREEN_MODULES.
from . import (  # noqa: E402,F401  (intentionally at the bottom: registry funcs must exist first)
    competitive,
    gamemodes,
    profile,
    history,
    leaderboard,
    friends,
    settings,
    postmatch,
    bugreport,
)
