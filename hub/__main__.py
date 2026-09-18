"""`python -m hub` starts the app (same as `python -m hub.app`)."""
import sys
from hub.game_identity import dispatch as game_identity_dispatch
game_identity_dispatch(sys.argv)
from hub.match_cleanup import dispatch
dispatch(sys.argv)
from hub.app import main

if __name__ == "__main__":
    main()
