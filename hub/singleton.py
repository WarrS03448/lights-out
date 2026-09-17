r"""One hub per user session, and a second launch raises the first one's window.

WHY THIS EXISTS (Sam, 2026-09-15, after the "the game got laggy while we built the hub" hunt).
The X button does not quit: `hub/tray.py` hides the window and the hub keeps running in the tray,
which is deliberate and stays. But nothing stopped a SECOND hub starting on top of it, and the
web UI is not a cheap window — each instance is the exe plus a full WebView2 browser stack
(measured on Sam's machine: 1 + 6 processes, ~370 MB) running its own 300 ms `/state` poll
(`webui/static/core.js`). Hide-with-X then relaunch-from-the-shortcut is the ordinary way to use a
tray app, so instances pile up silently over a session: two were live when this was written, and
the pile only clears on a reboot. That is exactly the shape of the complaint — a machine that is
fine after a fresh boot and gets worse the longer the day goes on — and every one of those hidden
browsers is compositing against a game that is trying to hold a frame rate.

FAILS OPEN, ALWAYS. Every error path here returns "you are the first instance". A hub that will
not start because a mutex call misbehaved is worse than two hubs: the user has no way to diagnose
it and no way past it. The guard is an optimisation on top of behaviour that was already correct,
so it is never allowed to be the reason the app is dead.

WHY A NAMED MUTEX AND NOT A LOCK FILE. A file left behind by a crashed or force-killed hub is a
stale lock that locks the user out of their own app, and every lock-file scheme then needs a
liveness check (a pid that may have been recycled) to undo its own damage. Windows frees a mutex
when the owning process dies, however it dies, so a killed hub leaves nothing behind. `Local\`
scopes it to the logon session, so Fast User Switching gives each user their own hub rather than
the second user silently getting nothing.
"""
import os
import sys

_WIN = sys.platform == "win32"

# Bumped only if the handshake below ever changes shape. Two hubs of DIFFERENT versions must still
# see each other — an upgrade that let the old and new exe run side by side would reintroduce the
# very pile this prevents — so the version of the app is deliberately NOT part of the name.
# Keeps its pre-rename name on purpose. It is invisible to the player, and changing it would
# mean a hub from before the Lights Out rename and one from after could not see each other -
# so during an update both could run at once. Nothing gains from renaming it.
_MUTEX_NAME = r"Local\CommunityHub-single-instance-v1"

_ERROR_ALREADY_EXISTS = 183

# The handle is parked here for the life of the process. If it were a local it would be garbage
# collected, Windows would release the mutex, and the guard would stop guarding a few seconds after
# it was taken — the classic way this pattern is got wrong.
_handle = None


def _mutex_name(local_repo=None) -> str:
    """The lock's name. A `--local` test build gets its OWN lock on purpose: running the checkout
    against the installed hub is a normal thing to do while developing, and the guard must not turn
    that into "the app silently refuses to start"."""
    return _MUTEX_NAME + ("-local" if local_repo else "")


def claim(local_repo=None) -> bool:
    """True when this process is THE hub and may carry on starting.

    False means another hub already holds the lock; the caller should raise that one's window
    (`focus_existing`) and exit without drawing anything. Off Windows, or if anything at all goes
    wrong, this returns True — see FAILS OPEN above."""
    global _handle
    if not _WIN or os.environ.get("HUB_ALLOW_MULTIPLE"):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, False, _mutex_name(local_repo))
        err = ctypes.get_last_error()
        if not handle:
            return True                      # could not create it at all: fail open
        if err == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)     # somebody else's lock; do not sit on a handle to it
            return False
        _handle = handle                     # ours, and held until the process exits
        return True
    except Exception:                        # noqa: BLE001 — a guard must never take the app down
        return True


def focus_existing(timeout_ms: int = 200) -> bool:
    """Best effort: bring the hub that IS running to the front, so a second launch looks like
    "the window came back" rather than "the shortcut did nothing".

    True when a window was found and raised. Never raises: a second instance that cannot find the
    first still has to exit quietly, and a silent no-op is a far better outcome than a traceback in
    a shipped app.

    Matched on the window TITLE rather than a class name because the two UIs draw different
    windows — pywebview/WebView2 normally, Tk when the webview cannot start (`hub/app.py`) — and
    both title theirs from `version.APP_NAME`. The `--local` build appends " (local test)", so the
    match is a PREFIX, and the local lock above keeps the two from ever raising each other."""
    if not _WIN:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        from . import version

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        want = version.APP_NAME

        SW_RESTORE = 9
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _visit(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if buf.value.startswith(want):
                    found.append(hwnd)
                    return False             # stop enumerating: one is enough
            return True

        user32.EnumWindows(_visit, 0)
        if not found:
            return False                     # hidden in the tray with no window: nothing to raise
        hwnd = found[0]
        user32.ShowWindow(hwnd, SW_RESTORE)   # undo a minimise; harmless on a normal window
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:                        # noqa: BLE001
        return False


def release():
    """Drop the lock. Only needed by tests: a real hub holds it until the process exits, which is
    the whole point of using a kernel object rather than a file."""
    global _handle
    if _handle and _WIN:
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(_handle)
        except Exception:                    # noqa: BLE001
            pass
    _handle = None
