"""PyInstaller entry point (repository root): the package uses relative imports, so the frozen script must import it as a package."""
import sys
from hub.match_cleanup import dispatch
dispatch(sys.argv)
from hub.app import main

if __name__ == "__main__":
    main()
