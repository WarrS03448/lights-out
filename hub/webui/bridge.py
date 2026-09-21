"""JS -> Python actions: the pywebview `js_api` object.

pywebview exposes each public method of this class to JS as
`window.pywebview.api.<name>(...)`, returning a Promise. The rule from the plan holds: this is
a 1:1 passthrough to the existing session verbs — NOTHING here is authoritative. Each verb hops
onto the UI thread (`panel.post`) so it mutates session state on the one thread, then returns;
the result arrives back as the next `onState` push, exactly as the Tk panel gets it through
`on_change()` today.

MODULAR: the CORE verbs (`ready`, `get_state`, `set_language`, `set_view`) live here as methods.
Every SCREEN's verbs are registered by that screen's module in `hub/webui/screens/` via
`register_verbs(...)` and installed onto each Api instance at construction (`_install_screen_verbs`).
So a screen agent adds verbs by editing ONLY their own screen module — never this shared method
list. pywebview discovers instance-attribute functions the same way it discovers methods (it scans
`dir()` for `inspect.isfunction`/`ismethod`) and forwards all JS arguments, so a closure set via
`setattr` is a first-class verb.

`get_state()`/`ready()` are the only synchronous methods: JS pulls the current snapshot on load
(and thereafter receives pushes). Everything else returns nothing meaningful — watch `onState`.
"""
from . import screens as screens_pkg
from .. import telemetry


class Api:
    """The bridge. Construct with a WebPanel; register the instance as pywebview's `js_api`."""

    def __init__(self, panel):
        self.panel = panel
        self._install_screen_verbs()

    # ------------------------------------------------------------ per-screen verbs
    def _install_screen_verbs(self):
        """Install each registered screen verb as a callable instance attribute. The closure binds
        this panel and forwards JS args to the registered fn(panel, *args). pywebview calls the
        attribute as a plain function (no implicit self), so the fn must take the panel explicitly
        — which is exactly the registered signature."""
        for verb, fn in screens_pkg.SCREEN_VERBS.items():
            self._bind_verb(verb, fn)

    def _bind_verb(self, verb, fn):
        panel = self.panel

        def method(*args):
            telemetry.emit("ui.action", action=str(verb))
            return fn(panel, *args)

        method.__name__ = str(verb)
        setattr(self, verb, method)

    # ------------------------------------------------------------ core verbs (state pull / chrome)
    def ready(self):
        """JS calls this once the page has loaded: return the current snapshot AND schedule a
        fresh push (so a snapshot built before the page was ready is not lost)."""
        self.panel.post(self.panel.on_change)
        return self._state()

    def get_state(self):
        return self._state()

    def _state(self):
        return self.panel.last_state

    def set_language(self, code):
        code = str(code or "")
        self.panel.post(lambda: self.panel.set_language(code))

    def set_view(self, name):
        # Nav lives in Python so it survives i18n/state (plan): switch the active screen on the UI
        # thread; the re-emitted snapshot carries the new view and the router renders it.
        if name in ("competitive", "gamemodes", "friends", "profile", "history", "leaderboard", "settings", "bugreport", "tournament", "messages"):
            telemetry.emit("ui.screen", screen=name)
        self.panel.post(lambda: self.panel.set_view(str(name or "")))

    def apply_hub_update(self):
        # The "Update now" button in the top strip. Hop onto the UI thread; start_hub_update spins
        # the download onto a worker thread (never blocks the UI thread) and pushes progress back
        # through the snapshot, then launches the installer. See WebPanel.start_hub_update.
        self.panel.post(self.panel.start_hub_update)
