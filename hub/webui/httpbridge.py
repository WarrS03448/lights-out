"""Localhost HTTP bridge: the reliable JS <-> Python channel for the web UI.

pywebview's WebView2 `js_api` / `evaluate_js` bridge proved unreliable in the packaged app: the
api methods were not always exposed (`window.pywebview.api.<verb> is not a function`) and pushes
issued from the scheduler thread were dropped, so nothing but the first screen worked. This
replaces that bridge with a tiny HTTP server the page talks to over `fetch`:

    GET  /state            -> the current snapshot JSON (the page polls this and reconciles)
    GET  /events?since=N   -> transient cues (toasts) with a seq greater than N
    POST /window/<op>      -> the web UI's own title bar and border: drag/start|move|end,
                              resize/start/<edge>|move|end, minimize, maximize, close.
                              NOT verbs - see `_window` for why.
    POST /verb/<name>      -> body is a JSON array of args; routes to the SAME verb table the old
                              js_api exposed (the core verbs + every screen's registered verbs),
                              each of which hops onto the UI thread via panel.post. Returns /state.
    GET  /avatar?u=<url>   -> the Steam avatar at that URL, from the on-disk cache (hub/avatars.py
                              downloads it once). Same origin, so the page's CSP can show it -
                              `img-src 'self'` allows this and would not allow steamstatic.com.
    GET  /<path>           -> the web UI's static files (index.html, css, js), same origin.

Only verbs on the allow-list (core verbs + registered screen verbs) are dispatchable, and the
server binds to 127.0.0.1 on an OS-assigned free port, so nothing off the machine can reach it.

This module imports no pywebview: it is a stdlib http.server, driven by a WebPanel. `shell.run`
starts it, points the pywebview window at its URL, and stops it on close.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .bridge import Api
from . import screens as screens_pkg
from .. import avatars as avatars_mod
from .. import mapart

# The verbs the bridge will dispatch. The core four live on Api as methods; every screen's verbs
# are registered into SCREEN_VERBS at import. Anything not in this set is refused (a POST to an
# arbitrary Api attribute must never run), so the surface is exactly the old js_api surface.
_CORE_VERBS = ("ready", "get_state", "set_language", "set_view", "apply_hub_update")


def _allowed_verbs():
    return set(_CORE_VERBS) | set(screens_pkg.SCREEN_VERBS.keys())


# What a cached avatar actually is, read off its first bytes. Steam serves JPEG today and has
# served PNG and GIF; the file on disk is whatever came back, so the type is sniffed rather than
# assumed. Anything unrecognised is not served at all - this route exists to show avatars, not to
# be a general file server for the cache directory.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),            # RIFF....WEBP; the four bytes at 8 are checked below
)


def _image_type(data: bytes):
    for magic, ctype in _IMAGE_MAGIC:
        if data.startswith(magic):
            if ctype == "image/webp" and data[8:12] != b"WEBP":
                continue
            return ctype
    return None


def avatar_bytes(url: str):
    """(bytes, content-type) for a Steam avatar URL, or None.

    The URL comes off the PAGE, which means it came off the wire, so it is checked the same way
    the Tk path checks it: `avatars.is_allowed` passes https Steam hosts and nothing else. A hub
    that fetched whatever a payload told it to would be a hub that can be pointed at anything
    (hub/avatars.py). Everything here degrades to None, and the page draws initials instead.

    This runs on the bridge's own handler thread, never the UI thread, so the one-off download
    that fills the cache cannot stall the window."""
    if not avatars_mod.is_allowed(url):
        return None
    try:
        path = avatars_mod.fetch(url)          # cached: no network at all
        if not path:
            return None
        with open(path, "rb") as f:
            data = f.read(avatars_mod.MAX_BYTES + 1)
    except OSError:
        return None
    if not data or len(data) > avatars_mod.MAX_BYTES:
        return None
    ctype = _image_type(data)
    return (data, ctype) if ctype else None


class _StaticFiles:
    """Minimal, safe static file resolver rooted at the web UI's static dir. Only serves files
    under the root (no traversal), with a small content-type map — enough for the hub's assets."""

    _TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
        ".ico": "image/x-icon", ".map": "application/json",
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
    }

    def __init__(self, root):
        import os
        self.root = os.path.realpath(str(root))

    def resolve(self, url_path):
        """Return (bytes, content_type) for url_path, or None if it is missing / outside root."""
        import os
        rel = url_path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(self.root, rel))
        # Containment check: the resolved path must live under the static root.
        if full != self.root and not full.startswith(self.root + os.sep):
            return None
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return None
        ext = os.path.splitext(full)[1].lower()
        ctype = self._TYPES.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            return f.read(), ctype


def make_handler(panel, static):
    api = Api(panel)                       # the verb router: same core + screen verb binding as JS had
    allowed = _allowed_verbs()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # http.server logs every request to stderr; silence it (this is an app, not a web server).
        def log_message(self, *args):      # noqa: D401
            pass

        # ---- helpers ----------------------------------------------------
        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _state_body(self):
            return panel.last_payload or "null"

        def _trusted_request(self, write=False):
            host = "127.0.0.1:%d" % self.server.server_port
            origin = self.headers.get("Origin")
            if (self.headers.get("Host") != host
                    or (write and origin != "http://" + host)
                    or (origin is not None and origin != "http://" + host)):
                self.close_connection = True
                self._send(403, '{"ok":false}')
                return False
            return True

        def _network(self, write=False):
            # Temporary TURN credentials are available only to this localhost page.
            # Checking Host as well as Origin also blocks DNS-rebinding requests.
            host = "127.0.0.1:%d" % self.server.server_port
            if self.headers.get("Host") != host or (write and self.headers.get("Origin") != "http://" + host):
                self.close_connection = True
                self._send(403, '{"ok":false}')
                return
            client = getattr(getattr(panel, "session", None), "client", None)
            if not client or not hasattr(client, "network_browser_state"):
                self._send(200 if not write else 409, "null" if not write else '{"ok":false}')
                return
            if not write:
                self._send(200, json.dumps(client.network_browser_state()))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 65536 or self.headers.get_content_type() != "application/json":
                    raise ValueError("invalid body")
                self.connection.settimeout(5)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(body, dict):
                    raise ValueError("invalid body")
            except (ValueError, OSError):
                self.close_connection = True
                self._send(400, '{"ok":false}')
                return
            status, response = client.network_browser_input(body)
            self._send(status, json.dumps(response))

        # ---- routes -----------------------------------------------------
        def do_GET(self):
            if not self._trusted_request():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/network":
                self._network()
                return
            if path == "/state":
                self._send(200, self._state_body())
                return
            if path == "/debug/push":
                # What the evaluate_js channel is costing right now. The answer to "the hub is
                # laggy" used to need a debugger; this makes it one curl.
                try:
                    stats = panel.push_stats()
                except Exception:                      # noqa: BLE001
                    stats = {"error": "unavailable"}
                self._send(200, json.dumps(stats))
                return
            if path == "/events":
                try:
                    since = int((parse_qs(parsed.query).get("since") or ["0"])[0])
                except (TypeError, ValueError):
                    since = 0
                self._send(200, json.dumps(panel.events_since(since)))
                return
            if path in ("/map-thumbnail", "/gamemode-thumbnail"):
                is_mode = path == "/gamemode-thumbnail"
                name = (parse_qs(parsed.query).get("mode" if is_mode else "map") or [""])[0]
                loader = mapart.gamemode_thumbnail if is_mode else mapart.thumbnail
                data = loader(getattr(getattr(panel, "app", None), "game_dir", None), name)
                if data is None:
                    self._send(404, "no thumbnail", "text/plain; charset=utf-8")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "private, max-age=3600")
                    self.end_headers()
                    try:
                        self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            if path == "/avatar":
                url = (parse_qs(parsed.query).get("u") or [""])[0]
                hit = avatar_bytes(url)
                if hit is None:
                    self._send(404, "no avatar", "text/plain; charset=utf-8")
                    return
                body, ctype = hit
                # The URL is content-addressed by Steam (the hash changes when the picture does),
                # so this answer can be kept for the life of the window.
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:                # the page navigated away mid-write
                    pass
                return
            hit = static.resolve(path)
            if hit is None:
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            body, ctype = hit
            self._send(200, body, ctype)

        def do_POST(self):
            if not self._trusted_request(write=True):
                return
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/network":
                self._network(write=True)
                return
            if path.startswith("/window/"):
                self._window(path[len("/window/"):])
                return
            if not path.startswith("/verb/"):
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            name = path[len("/verb/"):]
            if name not in allowed:
                self._send(403, json.dumps({"error": "unknown verb"}))
                return
            try:
                args = self._read_args()
            except (ValueError, OSError):
                self.close_connection = True
                self._send(400, '{"ok":false}')
                return
            fn = getattr(api, name, None)
            if callable(fn):
                try:
                    fn(*args)
                except Exception:          # noqa: BLE001 — a bad verb must not kill the server
                    pass
            # The verb only schedules work on the UI thread; the page re-syncs via the next poll.
            self._send(200, self._state_body())

        # ---- the title bar ----------------------------------------------
        # Deliberately NOT verbs. A verb hops onto the UI thread and answers with the whole state
        # snapshot; a drag issues one call per mouse-move, so going through that table would put
        # tens of snapshot serialisations a second behind whatever the UI thread is already doing,
        # and the window would trail the cursor. These ops touch nothing the UI thread owns - they
        # are Win32 calls on an HWND (hub/webui/window.py) - so they run right here on the
        # handler thread and answer with almost nothing.
        #
        # "state" does nothing but answer: the page asks it once at load to find out whether this
        # platform gave us a frame to draw at all, and takes its own buttons back off if not.
        # "resize/start" arrives with the grabbed edge as a further segment (resize/start/se),
        # which is split off before this check - the edge itself is validated by resize_start.
        _WINDOW_OPS = ("state", "drag/start", "drag/move", "drag/end",
                       "resize/start", "resize/move", "resize/end",
                       "minimize", "maximize", "close")

        def _window(self, op):
            chrome = getattr(panel, "chrome", None)
            edge = None
            if op.startswith("resize/start/"):
                op, edge = "resize/start", op[len("resize/start/"):]
            if op not in self._WINDOW_OPS:
                self._send(403, json.dumps({"error": "unknown window op"}))
                return
            if chrome is None:                 # native frame (macOS, or a frameless window that
                self._send(200, b"")           # never came up): the page just has no title bar
                return
            snap = None
            try:
                if op == "state":
                    pass
                elif op == "drag/start":
                    chrome.drag_start()
                elif op == "drag/move":
                    chrome.drag_move()
                    self._send(200, b"")       # the hot path: no body, no state, no work
                    return
                elif op == "drag/end":
                    snap = chrome.drag_end()
                elif op == "resize/start":
                    chrome.resize_start(edge)
                elif op == "resize/move":
                    chrome.resize_move()
                    self._send(200, b"")       # hot path, like drag/move
                    return
                elif op == "resize/end":
                    chrome.resize_end()
                elif op == "minimize":
                    chrome.minimize()
                elif op == "maximize":
                    chrome.toggle_maximize()
                elif op == "close":
                    chrome.close()
            except Exception:                  # noqa: BLE001 — a window op must never kill the
                pass                           # bridge; the worst case is a drag that stops
            try:
                maximized = chrome.is_maximized()
            except Exception:                  # noqa: BLE001
                maximized = False
            self._send(200, json.dumps({"maximized": maximized, "snap": snap}))

        def _read_args(self):
            length = int(self.headers.get("Content-Length") or 0)
            if (not 0 < length <= 65536 or self.headers.get_content_type() != "application/json"
                    or self.headers.get("Transfer-Encoding")):
                raise ValueError("Invalid request body")
            self.connection.settimeout(5)
            args = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(args, list):
                raise ValueError("Expected argument list")
            return args

    return Handler


def start(panel):
    """Start the bridge on 127.0.0.1:<free port>. Returns (server, base_url).

    Serves the web UI's static files plus /state, /events and /verb/<name>. Call server.shutdown()
    (WebPanel.shutdown does this via shell.run's finally) to stop it."""
    from .. import paths
    static = _StaticFiles(paths.webui_dir())
    handler = make_handler(panel, static)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, name="hub-http", daemon=True).start()
    return server, "http://127.0.0.1:%d/index.html" % port
