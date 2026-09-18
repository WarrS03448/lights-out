"""Steam and Lights Out account sign-in from the hub.

Credentials authenticate a stable player profile. Independently verified game identity
is handled by game_identity; sign-in does not transfer another profile's progress.

The hub is a desktop program and cannot receive a browser redirect, so sign-in is a LINK
CODE handshake against our own service (server/auth.cjs). Deliberately NO local HTTP
listener: using outbound requests avoids local firewall and antivirus configuration.

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


def _request(path, method="GET", token=None, timeout=TIMEOUT_SECONDS, data=None):
    url = API_BASE.rstrip("/") + path
    encoded = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, method=method, data=encoded)
    req.add_header("accept", "application/json")
    if encoded is not None:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read(32769)
            if len(raw) > 32768:
                raise AuthError("Sign-in response was too large.")
            raw = raw.decode("utf-8", "replace")
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:                # 401 and friends carry a JSON body
        try:
            return e.code, json.loads(e.read(32768).decode("utf-8", "replace") or "{}")
        except Exception:                              # noqa: BLE001
            return e.code, {}
    except urllib.error.URLError as e:
        raise AuthError("Sign-in is temporarily unavailable.") from None
    except (ValueError, OSError) as e:                 # bad JSON, socket trouble
        raise AuthError("Sign-in is temporarily unavailable.") from None


def account_request(action, data=None, token=None):
    """Email/account and game-proof requests, always bounded JSON over the app API."""
    status, body = _request("/api/auth/account/" + action,
                           method="POST" if data is not None else "GET", token=token, data=data)
    if status not in (200, 201, 202) or not isinstance(body, dict) or not body.get("ok"):
        raise AuthError("Account verification failed. Please try again.")
    return body


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
        if state == "denied":
            raise AuthError("this Steam account is not invited to the private test")
        sleep(POLL_EVERY_SECONDS)
    raise AuthError("timed out")


def me(token):
    """The account behind a saved token, or None when it is no longer good.

    Also refreshes the token's lifetime server-side, so an active player never has to
    sign in again."""
    if not token:
        return None
    # An interrupted/offline logout must never restore its saved credential.
    if revocation_pending(token):
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
    return revoke_session(token)


def queue_revoke(token, folder=None):
    """Persist revocation before a caller forgets the credential or exits the app."""
    from . import credentials, paths
    import hashlib
    import os
    import uuid
    temporary = None
    try:
        folder = folder if folder is not None else paths.state_dir() / "pending-signouts"
        folder.mkdir(parents=True, exist_ok=True)
        pending = folder / (hashlib.sha256(token.encode("utf-8")).hexdigest() + ".json")
        temporary = pending.with_suffix("." + uuid.uuid4().hex + ".tmp")
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(credentials.seal({"token": token}), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,pending)
        return pending
    finally:
        if temporary:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass


def revocation_pending(token):
    from . import paths
    import hashlib
    try:
        path = paths.state_dir() / 'pending-signouts' / (hashlib.sha256(token.encode('utf-8')).hexdigest() + '.json')
        return path.is_file()
    except OSError:
        return True


def revoke_session(token, pending=None):
    """Revoke without changing another active account's telemetry; retry offline."""
    if not token:
        return True
    from .credentials import CredentialError
    if pending is None:
        try:
            pending = queue_revoke(token)
        except (OSError, CredentialError):
            pass  # Still try the server if local storage is unavailable.
    try:
        status, _ = _request("/api/auth/signout", method="POST", token=token)
        if status == 200:
            if pending:
                try: pending.unlink(missing_ok=True)
                except OSError: pass
            return True
    except AuthError:
        pass
    return False


def retry_signouts(folder=None):
    from . import credentials, paths
    try:
        folder = folder if folder is not None else paths.state_dir() / "pending-signouts"
        for path in folder.glob("*.json"):
            try:
                if path.stat().st_size > 44096: continue
                token = credentials.open_sealed(json.loads(path.read_text(encoding="utf-8"))).get("token")
                if isinstance(token,str): revoke_session(token, pending=path)
            except (OSError, ValueError, credentials.CredentialError):
                continue
    except OSError:
        pass
