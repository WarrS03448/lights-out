"""Shared exclusions for game-file changes and competitive play."""


def files_busy(panel):
    return bool(getattr(getattr(panel, "app", None), "busy", False)
                or (getattr(panel, "_gamemodes_job", None) or {}).get("busy")
                or getattr(panel, "settings_uninstall_busy", False))


def match_active(panel):
    session = getattr(panel, "session", None)
    if session is None:
        return False
    phase = getattr(session, "phase", "")
    party = getattr(session, "party", None)
    locked = getattr(session, "locked_in", lambda: False)
    return (bool(getattr(session, "_party_transition", False)) or bool(locked())
            or phase in ("checking", "queued", "found", "lobby", "connecting", "live")
            or bool(party and not session.is_party_leader()))


def files_locked(panel):
    return files_busy(panel) or match_active(panel)
