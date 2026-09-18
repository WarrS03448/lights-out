"""PyInstaller entry point (repository root): the package uses relative imports, so the frozen script must import it as a package."""
import sys
from hub.game_identity import dispatch as game_identity_dispatch
game_identity_dispatch(sys.argv)
from hub.match_cleanup import dispatch
dispatch(sys.argv)
from hub.app import main

if __name__ == "__main__":
    from hub import recording, singleton
    if '--restore-public' in sys.argv:
        if not singleton.claim():
            if '--quiet' not in sys.argv:
                recording.notify('Close Lights Out first', 'Quit Lights Out from its tray menu, then restore again.')
            raise SystemExit(1)
        try:
            recording.restore()
            ok = True
        except Exception as exc:
            ok = False
            if '--quiet' not in sys.argv:
                recording.notify('Public restore needs attention', str(exc))
        if ok and '--quiet' not in sys.argv:
            recording.notify('Public setup restored', 'Your public game files are restored.')
        raise SystemExit(0 if ok else 1)
    try:
        main()
    finally:
        if recording.enabled() and singleton._handle and '--selfcheck' not in sys.argv:
            recording.ensure_exit()
