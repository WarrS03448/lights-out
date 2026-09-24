"""Companion recovery observations and account/match-scoped asynchronous actions."""
import csv
import io
import os
import subprocess
import time
from . import version
from .i18n import t

WINDOWS = os.name == "nt"


def game_observation():
    """Read only. Failed enumeration is unknown, never evidence of disconnection."""
    if not WINDOWS:
        return "unknown"
    try:
        result = subprocess.run(["tasklist", "/NH", "/FO", "CSV"], capture_output=True, text=True,
                                timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode != 0 or not result.stdout.strip():
            return "unknown"
        rows = [row for row in csv.reader(io.StringIO(result.stdout), strict=True) if row]
        if not rows or any(len(row) != 5 or not row[1].isdigit() for row in rows):
            return "unknown"
        return "running" if any(row[0].lower() == version.GAME_EXE.lower() for row in rows) else "closed"
    except (OSError, ValueError, csv.Error, subprocess.SubprocessError):
        return "unknown"


def scope(session):
    match_id = getattr(session, "match_id", "")
    return (getattr(session, "_account_epoch", 0), match_id,
            getattr(session, "host_epoch", 0), getattr(session, "session_key", "") or "chm-" + match_id)


def current(session, captured):
    return scope(session) == captured


def server_now(session):
    anchor = getattr(session, "_recovery_clock", None)
    if anchor and anchor[0] == scope(session):
        return anchor[1] + max(0, time.monotonic() - anchor[2]) * 1000
    return time.time() * 1000


def note_clock(session, value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 < value < 1e16:
        return
    anchor = getattr(session, "_recovery_clock", None)
    if anchor and anchor[0] == scope(session):
        value = max(value, server_now(session))
    session._recovery_clock = (scope(session), value, time.monotonic())


def launch_allowed(session):
    restore = getattr(session, "recovery", {}) or {}
    if restore.get("can_rejoin") is False:
        return False
    if (restore.get("phase") == "restoring" and not restore.get("roster") and
            restore.get("rejoin_until") and server_now(session) >= restore["rejoin_until"]):
        return False
    if restore.get("phase") == "playing" and session._i_am_host():
        return False  # A cold replacement needs a new claim, never the old restore seed.
    return restore.get("phase") != "restoring" or session._i_am_host() or restore.get("world_ready") is True


def stale_event(session, event):
    if str(event.get("match_id") or session.match_id) != session.match_id:
        return False
    epoch = int(event.get("host_epoch") or 0)
    current_epoch = getattr(session, "host_epoch", 0)
    if epoch < current_epoch:
        return True
    return (epoch == current_epoch and str(event.get("session_key") or "chm-"+session.match_id) ==
            (getattr(session, "session_key", "") or "chm-"+session.match_id) and
            (((event.get("recovery") or {}).get("revision") or 0) <
             ((getattr(session, "recovery", {}) or {}).get("revision") or 0) or
             int(event.get("roster_revision") or 0) < getattr(session,"roster_revision",0)))


def relaunch(session):
    """Check process state off the UI thread, then fence the result before launch."""
    if getattr(session, "_recovery_launch_busy", False):
        return
    if not launch_allowed(session):
        session.error = t("comp_recovery_unavailable" if (session.recovery or {}).get("phase") == "playing" else "comp_recovery_wait")
        session._changed()
        return
    captured = scope(session)
    session._recovery_launch_busy = True
    session._changed()

    def received(status, body):
        if not current(session, captured):
            return
        session._recovery_launch_busy = False
        if status != 200 or not launch_allowed(session):
            session.error = t("comp_recovery_close" if status != 200 else "comp_recovery_wait")
            session._changed()
            return
        session._relaunch_closed_game()

    session._action(lambda: (200 if game_observation() == "closed" else 409, {}), received)


def poll(session, claim=False):
    if session.phase != "live" or not session.client or not session.match_id:
        return
    busy = "_recovery_claim_busy" if claim else "_recovery_busy"
    if getattr(session, busy, False):
        return
    if not claim and time.monotonic() < getattr(session, "_recovery_next", 0):
        return
    captured, client = scope(session), session.client
    setattr(session, busy, True)
    session._recovery_next = time.monotonic() + 5
    if claim:
        session._changed()

    def work():
        observed = game_observation()
        if not current(session, captured):
            return 409, {}
        if claim and observed != "closed":
            return 409, {"close_game": True}
        payload = {"match_id": captured[1], "epoch": captured[2], "session": captured[3],
                   "operation": "claim" if claim else "closed" if observed == "closed" else "status"}
        status, body = client.recover_match(payload)
        body = dict(body or {})
        body["observation"] = observed
        return status, body

    def received(status, body):
        if not current(session, captured):
            return
        setattr(session, busy, False)
        previous = public_status(session)
        if status == 200 and isinstance(body.get("match"), dict):
            session._update_match_authority(body["match"])
        health = dict(body.get("recovery") or {}) if status == 200 else dict(getattr(session, "recovery_health", {}) or {})
        if status != 200:
            health["can_claim"] = False
        if status == 200 and ((health.get("revision") or 0) < ((session.recovery or {}).get("revision") or 0) or
                (health.get("roster_revision") or 0) < getattr(session,"roster_revision",0)):
            if claim:
                session._changed()
            return
        # The claim may have changed the scope. Its match payload already contains
        # the new service clock; an old background status must not overwrite it.
        if not current(session, captured):
            session._changed()
            return
        note_clock(session, health.get("server_now"))
        health["observation"] = body.get("observation", "unknown")
        health["unavailable"] = status != 200
        session.recovery_health = health
        if claim:
            session.error = "" if status == 200 else t("comp_recovery_close" if body.get("close_game") else "comp_recovery_unavailable")
        if claim or previous != public_status(session):
            session._changed()

    session._action(work, received)


def public_status(session):
    """Only display fields cross the UI bridge; never capabilities or checkpoint rows."""
    health = getattr(session, "recovery_health", {}) or {}
    restore = getattr(session, "recovery", {}) or {}
    restoring = restore.get("phase") == "restoring"
    is_host = getattr(session,"_i_am_host",lambda:False)()
    observed = health.get("observation", "unknown")
    host_wait = restore.get("phase") == "playing" and is_host and observed == "closed"
    stale = health.get("session_stale") is True
    visible = restoring or host_wait or stale or (health.get("checkpoint_round") is not None and
                            observed == "closed" and not health.get("connected"))
    can_claim = health.get("can_claim") is True and observed == "closed"
    can_launch = launch_allowed(session) and (restoring or not is_host) and not can_claim
    sealed = bool(restore.get("roster") or health.get("roster_sealed"))
    end = restore.get("rejoin_until") or health.get("rejoin_until")
    state = ("verifying" if sealed else "returning" if end and server_now(session)<end else "sealing" if end else "creating") if restoring else (
        "available" if can_claim else "close_game" if stale and observed!="closed" else "checking" if stale or host_wait else "resumed")
    if health.get("unavailable"):
        state = "unavailable"
    return {"visible": visible, "restoring": restoring, "can_claim": can_claim, "can_launch": can_launch,
            "state": state, "server_now": int(server_now(session)) if visible else None,
            "round": restore.get("round", health.get("checkpoint_round")),
            "world_ready": restore.get("world_ready") is True,
            "rejoin_until": end,
            "restore_until": restore.get("deadline") or health.get("restore_until"),
            "returned": len(health.get("connected") or []),
            "expected": restore.get("expected") or health.get("expected") or 0,
            "roster_sealed": sealed,
            "can_rejoin": launch_allowed(session) if restore else True,
            "busy": bool(getattr(session, "_recovery_claim_busy", False) or getattr(session, "_recovery_launch_busy", False))}
