"""Steam sign-in from the hub (2026-09-14).

Sam's decision C1: Competitive identity is the Steam account. Steam OpenID proves one
thing — a SteamID64 — which is exactly what a rank should hang off.

The hub is a desktop program and cannot receive a browser redirect, so sign-in is a LINK
CODE handshake against our own service (server/auth.cjs). Deliberately NO local HTTP
listener: opening a port would fight the player's firewall and antivirus, and an unsigned
exe already has SmartScreen to get past.

    start()            -> {"code", "url", "expires_in"}   ask for a pending sign-in
    webbrowser.open(url)                                   the player signs in with Steam
    poll(code)         -> {"status": "pending"|"ready"|"expired", ...}
    wait_for(code)     -> the "ready" payload, or raises AuthError

Everything here runs on a WORKER THREAD; nothing in this module touches tkinter.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .version import API_BASE

TIMEOUT_SECONDS = 20          # per request
POLL_EVERY_SECONDS = 2.0
GIVE_UP_AFTER_SECONDS = 300   # 5 minutes of not finishing in the browser


class AuthError(Exception):
    """Sign-in could not be completed. The message is shown to the player as-is."""


def _request(path, method="GET", token=None, timeout=TIMEOUT_SECONDS):
    url = API_BASE.rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("accept", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:                # 401 and friends carry a JSON body
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:                              # noqa: BLE001
            return e.code, {}
    except urllib.error.URLError as e:
        raise AuthError(str(getattr(e, "reason", e))) from e
    except (ValueError, OSError) as e:                 # bad JSON, socket trouble
        raise AuthError(str(e)) from e


def start():
    """Ask the service for a link code and the URL the player should open."""
    status, body = _request("/api/auth/start")
    if status != 200 or not body.get("code") or not body.get("url"):
        raise AuthError("the sign-in service did not hand out a link (HTTP %s)" % status)
    return body


def poll(code):
    status, body = _request("/api/auth/poll?code=" + urllib.parse.quote(code))
    if status != 200:
        raise AuthError("the sign-in service returned HTTP %s" % status)
    return body


def wait_for(code, should_stop=None, sleep=time.sleep, now=time.monotonic, poll_fn=None):
    """Block until the browser half finishes. `should_stop()` lets the UI cancel.

    Raises AuthError on expiry, cancellation or give-up, so the caller has one path.
    `sleep`/`now`/`poll_fn` are injection points for the tests; nothing else passes them."""
    ask = poll_fn or poll
    deadline = now() + GIVE_UP_AFTER_SECONDS
    while now() < deadline:
        if should_stop is not None and should_stop():
            raise AuthError("cancelled")
        body = ask(code)
        state = body.get("status")
        if state == "ready":
            if not body.get("token") or not body.get("steam_id"):
                raise AuthError("the sign-in service returned an incomplete account")
            return body
        if state == "expired":
            raise AuthError("the sign-in link expired")
        sleep(POLL_EVERY_SECONDS)
    raise AuthError("timed out")


def me(token):
    """The account behind a saved token, or None when it is no longer good.

    Also refreshes the token's lifetime server-side, so an active player never has to
    sign in again."""
    if not token:
        return None
    try:
        status, body = _request("/api/auth/me", token=token)
    except AuthError:
        return None                       # offline: the caller decides what to do
    if status != 200 or not body.get("ok"):
        return None
    return body


def sign_out(token):
    """Best effort: drop the token server-side. The hub forgets it either way."""
    from . import telemetry
    telemetry.identify(None)
    if not token:
        return
    try:
        _request("/api/auth/signout", method="POST", token=token)
    except AuthError:
        pass
