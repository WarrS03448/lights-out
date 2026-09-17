"""The web UI (pywebview) front end for Lights Out.

Phase 1 of docs/ui-redesign-plan.md: the native-webview shell, the JS<->Python bridge, and
one real screen (Competitive / Find match + Party) wired to the live backend. The Tkinter UI
in hub/app.py is untouched and stays the default; this package is only used when the hub is
launched with `--webui` (`python -m hub --webui`).

Nothing here imports `webview` (pywebview) at module load: only `shell.run()` does, so the Tk
path and the headless test suite never require pywebview to be installed. The reusable pieces
(WebPanel, the UiScheduler, state_snapshot, the Api verbs) are pywebview-free and testable
headless.
"""
