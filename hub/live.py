"""The hub's client for the live competitive service (2026-09-14).

The server pushes over Server-Sent Events and takes actions as ordinary POSTs
(server/live.cjs explains why that transport). From Python an SSE stream is just a long
GET whose body arrives in pieces, so there is no dependency here beyond the standard
library, keeping the desktop client's dependencies small.

    client = LiveClient(token, on_event=..., on_status=..., versions=...)
    client.start()                       # connects, reconnects, until stop()
    client.join_queue() / leave_queue() / accept() / leave_match()
    client.start_connect(map, host, teams, sides, bans) / report_connected()
    client.history() / client.history(match_id)     # the Match History sub-tab
    client.create_party() / join_party(code) / leave_party() / refresh_party_code()
    client.invite_to_party(steam_id) / accept_party_invite(id) / decline_party_invite(id)
    client.stop()

THREADING: the reader runs on its own daemon thread and calls `on_event` FROM THAT THREAD.
Callers must hop to the main thread themselves (CompetitivePanel.post) before touching
tkinter. Nothing in this module imports tkinter, so that rule is easy to keep.
"""
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .version import API_BASE, HUB_VERSION
from . import telemetry

# A ping arrives every 15 s, so a minute of silence means the pipe is dead, not idle.
READ_TIMEOUT_SECONDS = 60
ACTION_TIMEOUT_SECONDS = 15
NETWORK_REFRESH_SECONDS = 15
# Steam may need several callback pumps after InitRelayNetworkAccess before the first marker.
NETWORK_WARMUP_SECONDS = 2
RECONNECT_BACKOFF = (1, 2, 5, 10, 20, 30)     # seconds, then 30 forever


class LiveClient:
    def __init__(self, token, on_event=None, on_status=None, versions=None,
                 network_config=None, network_probe_factory=None):
        self.token = token
        self.on_event = on_event or (lambda event: None)
        self.on_status = on_status or (lambda connected, detail: None)
        # What this hub is running, as a callable so it is read FRESH on every request: update
        # the gamemode without restarting and the very next join is the new number. See _stamp.
        self.versions = versions or (lambda: {})
        # Network measurement is opt-in for compatibility with older callers and tests. The
        # factory is injectable so focused tests never load the native Steam API.
        self.network_config = network_config
        self._network_probe_factory = network_probe_factory or self._make_network_probe
        self.network_status = {}
        self._network_lock = threading.Lock()
        self._network_session = None
        # Queue intent changes must never wait behind a 15-second HTTP request. The intent
        # lock protects only the generation; the action lock orders join/leave transports.
        self._queue_intent_lock = threading.Lock()
        self._queue_lock = threading.RLock()
        self._queue_generation = 0
        self._stop = threading.Event()
        self._thread = None
        self.connected = False

    # ---------------------------------------------------------------- what we are running
    def _stamp(self, req):
        """Tell the service what this hub is: the hub's own version, and the installed ranked
        gamemode's.

        WHY EVERY REQUEST AND NOT JUST THE JOIN. The server shuts the queue to anything behind
        the release it is publishing (server/live.cjs, the version gate), and it has to know
        that about EVERY MEMBER OF A PARTY - not just whoever pressed the button. A member who
        is not the leader never POSTs anything, so the only request of theirs the server ever
        sees is the SSE stream. Stamping one route would leave them permanently unknown, and
        unknown reads as behind.

        Nothing else is gated, so these headers ride along on accept, the veto, the connect
        window and the result without changing a thing about them - which is exactly what lets
        a match that formed before a release finish after it.

        The version callable is the caller's, so it is wrapped: a hub that cannot say what it
        is running should still be able to talk to the service and be told to update."""
        try:
            v = self.versions() or {}
        except Exception:              # noqa: BLE001 — never let this be what breaks a request
            v = {}
        req.add_header("user-agent", "LightsOut/%s" % HUB_VERSION)
        req.add_header("x-hub-version", str(v.get("hub") or HUB_VERSION))
        # Omitted rather than sent empty when the gamemode is not installed: an absent header
        # and an empty one mean the same thing to the server. It used to say "and the tab cannot
        # reach the queue without the gamemode anyway" - that was true of the Tk tab and never of
        # the web UI, which is how a fresh install queued for a mode it did not own. The server
        # now refuses hub-present-mode-absent outright (live.cjs versionProblem), so this silence
        # IS the signal rather than a thing nobody was meant to be able to send.
        mode = str(v.get("mode") or "")
        if mode:
            req.add_header("x-mode-version", mode)

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        if self._thread is not None:
            return
        telemetry.identify(self.token)
        telemetry.emit("connection.start", action="live_stream")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if self.network_config is not None:
            with self._network_lock:
                stop = threading.Event()
                wake = threading.Event()
                session = {"stop": stop, "wake": wake, "probe": None, "game_dir": None,
                           "unavailable_sent": False, "retry_delay": NETWORK_REFRESH_SECONDS,
                           "change": 0, "thread": None}
                worker = threading.Thread(target=self._network_run, args=(session,), daemon=True,
                                          name="hub-network-measurements")
                session["thread"] = worker
                self._network_session = session
                worker.start()

    def stop(self):
        self._stop.set()
        with self._network_lock:
            session = self._network_session
            self._network_session = None
            if session is not None:
                session["stop"].set()
                session["wake"].set()
        if session is not None:
            worker = session["thread"]
            # Each transport operation is bounded. Waiting here ensures the retired generation
            # cannot finish one request and issue another after stop() has returned.
            if worker is not threading.current_thread():
                worker.join(ACTION_TIMEOUT_SECONDS + 1)
        self._thread = None
        self.connected = False

    # ------------------------------------------------------- Relay network measurements
    @staticmethod
    def _make_network_probe(game_dir):
        from .network import RelayNetworkProbe
        return RelayNetworkProbe(game_dir)

    def _network_active(self, session, change=None):
        with self._network_lock:
            return (self._network_session is session and not session["stop"].is_set()
                    and (change is None or session["change"] == change))

    @staticmethod
    def _close_network_probe(session):
        probe = session.get("probe")
        session["probe"] = None
        session["game_dir"] = None
        if probe is not None:
            try:
                probe.close()
            except Exception:          # noqa: BLE001 — shutdown must remain reliable
                pass

    def _network_set_status(self, ready, region="", error="", force=False, session=None,
                            change=None):
        status = {"ready": bool(ready), "region": str(region or ""),
                  "error": str(error or "")}
        with self._network_lock:
            if session is not None and (self._network_session is not session
                                        or session["stop"].is_set()
                                        or (change is not None and session["change"] != change)):
                return
            if not force and status == self.network_status:
                return
            self.network_status = status
        try:
            self.on_event({"type": "network_status", **status})
        except Exception:              # noqa: BLE001 — UI callback cannot kill refreshes
            pass

    @staticmethod
    def _network_response_error(body, fallback):
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
        return fallback

    def _network_config_snapshot(self):
        try:
            value = self.network_config() or {}
        except Exception as exc:       # noqa: BLE001 — setting access is caller-owned
            raise RuntimeError(str(exc) or "Network settings are unavailable.") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Network settings are unavailable.")
        return {"game_dir": str(value.get("game_dir") or ""),
                "region": str(value.get("region") or "").strip().upper(),
                "cross_region": value.get("cross_region") is True}

    def _network_mark_unavailable(self, session, change, region, error):
        if not self._network_active(session, change):
            return
        self._network_set_status(False, region, error, session=session, change=change)
        if session["unavailable_sent"] or not self._network_active(session, change):
            return
        status, _body = self._post("/api/network/profile", {"unavailable": True})
        if not self._network_active(session, change):
            return
        # Record acknowledgement, not merely an attempt. Failed invalidations retry on the
        # normal cadence so an outage cannot leave an old ready profile behind indefinitely.
        session["unavailable_sent"] = status == 200
        if not session["unavailable_sent"]:
            session["retry_delay"] = NETWORK_REFRESH_SECONDS

    def _network_probe_for(self, session, game_dir):
        if session["probe"] is not None and session["game_dir"] == game_dir:
            return session["probe"]
        self._close_network_probe(session)
        probe = self._network_probe_factory(game_dir)
        session["probe"] = probe
        session["game_dir"] = game_dir
        return probe

    def _browser_probe(self):
        with self._network_lock:
            session = self._network_session
            probe = session.get("probe") if session is not None else None
            return probe if hasattr(probe, "browser_state") else None

    def network_browser_state(self):
        probe = self._browser_probe()
        return probe.browser_state() if probe else None

    def network_browser_input(self, body):
        probe = self._browser_probe()
        if not probe or not probe.browser_input(body):
            return 409, {"ok": False}
        if body.get("type") == "signal":
            state = probe.browser_state()
            return self._post("/api/network/signal", {
                "revision": state["revision"], "peer": body["peer"],
                "peer_revision": body["peer_revision"], "attempt": body["attempt"],
                "type": body.get("signal_type"), "data": body.get("data"),
            })
        return 200, {"ok": True}

    def _network_cycle(self, session):
        with self._network_lock:
            if self._network_session is not session or session["stop"].is_set():
                return
            change = session["change"]
        session["retry_delay"] = NETWORK_REFRESH_SECONDS
        try:
            config = self._network_config_snapshot()
        except Exception as exc:       # noqa: BLE001
            self._network_mark_unavailable(session, change, "", str(exc))
            return
        region = config["region"]
        try:
            probe = self._network_probe_for(session, config["game_dir"])
            if hasattr(probe, "browser_state"):
                from .relaytransport import refresh
                refresh(self, session, change, config, probe)
                return
            profile = probe.profile()
        except Exception as exc:       # noqa: BLE001 — native adapter failure is ordinary here
            self._close_network_probe(session)  # failed construction/use is retried next cycle
            self._network_mark_unavailable(session, change, region, str(exc))
            return

        probe_error = getattr(probe, "error", "") or ""
        if probe_error:
            # Constructor failures are reported on the probe rather than raised. This object
            # cannot recover; release it so the next normal refresh can discover Steam/DLLs.
            self._close_network_probe(session)
            self._network_mark_unavailable(session, change, region, probe_error)
            return
        if not isinstance(profile, dict):
            session["retry_delay"] = NETWORK_WARMUP_SECONDS
            self._network_mark_unavailable(
                session, change, region, "Steam network measurements are unavailable.")
            return
        location = profile.get("location")
        age = profile.get("age_seconds")
        if (not isinstance(location, str) or not location or isinstance(age, bool)
                or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0):
            session["retry_delay"] = NETWORK_WARMUP_SECONDS
            self._network_mark_unavailable(
                session, change, region, profile.get("error") or
                "Steam network measurements are unavailable.")
            return
        if not region:
            self._network_mark_unavailable(session, change, "", "Choose a matchmaking region.")
            return
        if not self._network_active(session, change):
            return
        status, body = self._post("/api/network/profile", {
            "region": region, "cross_region": config["cross_region"],
            "location": location, "age_seconds": age,
        })
        if not self._network_active(session, change):
            return
        if status != 200 or not isinstance(body, dict) or not body.get("ready"):
            self._network_set_status(False, region, self._network_response_error(
                body, "Could not refresh network measurements."), session=session,
                                     change=change)
            return
        revision = body.get("revision")
        if not isinstance(revision, str) or not revision:
            self._network_set_status(False, region, "Connection profile revision is missing.",
                                     session=session, change=change)
            return
        session["unavailable_sent"] = False

        offset = 0
        while self._network_active(session, change):
            get_status, page = self._get("/api/network/peers?offset=%d" % offset)
            if not self._network_active(session, change):
                return
            if get_status != 200 or not isinstance(page, dict):
                self._network_set_status(False, region, self._network_response_error(
                    page, "Could not fetch peer network measurements."), session=session,
                                         change=change)
                return
            # The response revision is the binding supplied by the server. Never upload a
            # batch against an earlier profile generation.
            page_revision = page.get("revision")
            if page_revision != revision:
                self._network_set_status(False, region,
                                         "Connection profile changed during refresh.",
                                         session=session, change=change)
                return
            peers = page.get("peers")
            if not isinstance(peers, list) or len(peers) > 400:
                self._network_set_status(False, region, "Invalid peer measurement response.",
                                         session=session, change=change)
                return
            estimates = []
            for peer in peers:
                if not isinstance(peer, dict):
                    continue
                steam_id, marker, peer_revision = (peer.get("steam_id"), peer.get("location"),
                                                    peer.get("revision"))
                if not all(isinstance(value, str) and value
                           for value in (steam_id, marker, peer_revision)):
                    continue
                try:
                    ping = probe.estimate(marker)
                except Exception:      # noqa: BLE001 — one opaque marker may be unusable
                    ping = None
                if isinstance(ping, bool) or not isinstance(ping, int) or ping < 0:
                    continue
                estimates.append({"steam_id": steam_id, "revision": peer_revision,
                                  "ping": ping})
            if not self._network_active(session, change):
                return
            post_status, post_body = self._post("/api/network/pings", {
                "revision": page_revision, "peers": estimates,
            })
            if not self._network_active(session, change):
                return
            if post_status != 200:
                self._network_set_status(False, region, self._network_response_error(
                    post_body, "Could not report peer network measurements."), session=session,
                                         change=change)
                return
            next_offset = page.get("next_offset")
            if next_offset is None:
                break
            if (isinstance(next_offset, bool) or not isinstance(next_offset, int)
                    or next_offset <= offset):
                self._network_set_status(False, region, "Invalid peer measurement page.",
                                         session=session, change=change)
                return
            offset = next_offset
        if self._network_active(session, change):
            self._network_set_status(True, region, "", session=session, change=change)

    def _network_run(self, session):
        try:
            while self._network_active(session):
                self._network_cycle(session)
                if session["stop"].is_set():
                    return
                session["wake"].wait(session.get("retry_delay", NETWORK_REFRESH_SECONDS))
                session["wake"].clear()
        finally:
            self._close_network_probe(session)

    def network_changed(self):
        """Invalidate local readiness and wake this client's current worker generation."""
        self.cancel_pending_queue()
        if self.network_config is None:
            return
        try:
            region = self._network_config_snapshot()["region"]
        except Exception:              # noqa: BLE001
            region = ""
        with self._network_lock:
            session = self._network_session
            if session is not None:
                session["change"] += 1
                session["unavailable_sent"] = False
                if hasattr(session.get("probe"), "browser_state"):
                    self._close_network_probe(session)
                session["wake"].set()
        self._network_set_status(False, region, "", force=True)

    # ---------------------------------------------------------------- actions
    def _post(self, path, body=None):
        started = time.monotonic()
        status, result = self._post_request(path, body)
        telemetry.emit("request.outcome", action=path.strip("/").replace("/", "."), status=status,
                       duration_ms=round((time.monotonic()-started)*1000),
                       severity="error" if status == 0 or status >= 500 else "warn" if status >= 400 else "info")
        return status, result

    def _post_request(self, path, body=None):
        """Fire an action at the service. Returns (status, body) and never raises."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(API_BASE.rstrip("/") + path, data=data, method="POST")
        req.add_header("authorization", "Bearer " + self.token)
        req.add_header("accept", "application/json")
        self._stamp(req)
        if data is not None:
            req.add_header("content-type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=ACTION_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
            except Exception:          # noqa: BLE001
                return e.code, {}
        except Exception as e:         # noqa: BLE001 — offline, DNS, timeout
            return 0, {"error": str(e)}

    def prepare_queue_join(self):
        """Capture the current queue intent for an action that will run asynchronously."""
        with self._queue_intent_lock:
            generation = self._queue_generation
        return lambda: self.join_queue(expected_generation=generation)

    def cancel_pending_queue(self):
        """Invalidate scheduled joins immediately, without waiting for transport I/O."""
        with self._queue_intent_lock:
            self._queue_generation += 1

    def join_queue(self, expected_generation=None):
        if expected_generation is None:
            with self._queue_intent_lock:
                expected_generation = self._queue_generation
        with self._queue_lock:
            with self._queue_intent_lock:
                if expected_generation != self._queue_generation:
                    return 409, {"ok": False, "cancelled": True,
                                 "error": "Queue request was cancelled."}
            if self.network_config is not None and not self.network_status.get("ready"):
                return 409, {"ok": False, "error": self.network_status.get("error") or
                             "Network measurements are not ready."}
            return self._post("/api/queue/join")

    def leave_queue(self):
        with self._queue_lock:
            return self._post("/api/queue/leave")

    def accept(self):
        return self._post("/api/match/accept")

    def leave_match(self):
        return self._post("/api/match/leave")

    def start_connect(self, map_name, host_id, teams=None, sides=None, bans=None):
        """The veto is over: open the connect window. Any player in the match may do this;
        the server treats the second and later calls as no-ops.

        The lobby's outcome rides along so the match can be written into everyone's history
        with teams and a veto rather than just a map name. The coin flip and the veto still
        run here rather than on the server, so the server records this as client-reported and
        checks it against the roster before it stores it."""
        body = {"map": str(map_name or ""), "host": str(host_id or "")}
        if teams:
            body["teams"] = {str(k): [str(i) for i in v] for k, v in teams.items()}
        if sides:
            body["sides"] = {str(k): str(v) for k, v in sides.items()}
        if bans:
            body["bans"] = [{"team": int(team or 0), "map": str(name)} for team, name in bans]
        return self._post("/api/match/connecting", body)

    def flip_coin(self, side):
        """The designated captain calls heads/tails. The SERVER decides the result once and
        broadcasts it as a `lobby` event to every player, so all ten agree on the winner."""
        return self._post("/api/match/coin", {"side": str(side or "")})

    def choose_advantage(self, kind):
        """The toss winner takes the side selector or the last ban. Server-arbitrated so the
        loser cannot disagree about who got what."""
        return self._post("/api/match/choose", {"kind": str(kind or "")})

    def pick_side(self, side):
        """The team that holds the side advantage picks attack or defend."""
        return self._post("/api/match/side", {"side": str(side or "")})

    def ban_map(self, map_name):
        """One veto ban, by the captain whose turn the server says it is."""
        return self._post("/api/match/ban", {"map": str(map_name or "")})

    def send_chat(self, text, channel="team"):
        """One chat line. The service filters it, decides who may read it, and sends it back to
        everyone who may - including us, which is why nothing is echoed locally."""
        return self._post("/api/match/chat", {"text": str(text or ""), "channel": str(channel or "team")})

    def report_connected(self):
        """"I am in the game." The server starts the match when everyone has said it, and
        punishes whoever has not when the window runs out."""
        return self._post("/api/match/connected")

    def ack_combat_warning(self, match_id, warning_id):
        """Confirm a specific warning only after the UI says it actually rendered it."""
        return self._post("/api/match/combat-warning", {
            "match_id": str(match_id or ""),
            "warning_id": str(warning_id or ""),
        })

    def create_party(self):
        """Mint a party of just me. The roster arrives on the stream as a party_update."""
        return self._post("/api/party/create")

    def join_party(self, code):
        """Join a party by its code. 404 if the code is bad or unknown, 409 if it is full."""
        return self._post("/api/party/join", {"code": str(code or "")})

    def leave_party(self):
        """Leave the party I am in. The server tells me I am solo with a party_update."""
        return self._post("/api/party/leave")

    def refresh_party_code(self):
        """Leader only: mint a new code. The new one arrives on the stream, not in this reply."""
        return self._post("/api/party/refresh-code")

    # Party invites. The inbox itself is never fetched: it is in-memory, right-now state on the
    # server, so it arrives on the stream as `party_invites` (and is replayed on reconnect).
    def invite_to_party(self, target):
        """Offer a friend the seat next to me. 409 with a reason if the world says no."""
        return self._post("/api/party/invite", {"target": str(target or "")})

    def accept_party_invite(self, from_id):
        """Take a seat. The roster arrives as a party_update, as any other join does."""
        return self._post("/api/party/invite/accept", {"from": str(from_id or "")})

    def decline_party_invite(self, from_id):
        return self._post("/api/party/invite/decline", {"from": str(from_id or "")})

    def _get(self, path):
        """Read something. Same contract as _post: returns (status, body), never raises."""
        req = urllib.request.Request(API_BASE.rstrip("/") + path, method="GET")
        req.add_header("authorization", "Bearer " + self.token)
        req.add_header("accept", "application/json")
        self._stamp(req)
        try:
            with urllib.request.urlopen(req, timeout=ACTION_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
            except Exception:          # noqa: BLE001
                return e.code, {}
        except Exception as e:         # noqa: BLE001 — offline, DNS, timeout
            return 0, {"error": str(e)}

    def history(self, match_id=None):
        """The player's own past matches, newest first, or one match in full.

        Without an id: {"matches": [compact row, ...]}. With one: {"match": {...}} for the
        detail pop-up, or a 404 if the player was not in that match."""
        if match_id:
            return self._get("/api/match/history?id=" + urllib.parse.quote(str(match_id), safe=""))
        return self._get("/api/match/history")

    # ---------------------------------------------------------------- friends
    # Keyed by SteamID64 on the server and persisted, so this list follows the account rather
    # than the machine. Everything here is a thin passthrough; the server is authoritative.
    def friends(self):
        return self._get("/api/friends/list")

    def friend_request(self, code="", target=""):
        return self._post("/api/friends/request", {"code": code, "target": target})

    def friend_accept(self, target):
        return self._post("/api/friends/accept", {"target": target})

    def friend_decline(self, target):
        return self._post("/api/friends/decline", {"target": target})

    def friend_cancel(self, target):
        return self._post("/api/friends/cancel", {"target": target})

    def friend_remove(self, target):
        return self._post("/api/friends/remove", {"target": target})

    def friend_code_refresh(self):
        return self._post("/api/friends/code")

    # ---------------------------------------------------------------- reports
    def report(self, target, reason, match_id="", note=""):
        return self._post("/api/report", {"target": target, "reason": reason,
                                          "match_id": match_id, "note": note})

    def bug_report(self, text):
        """File a bug against the hub (server/live.cjs submitBugReport).

        One field. WHO it is from is the bearer token this request already carries, not something
        the page fills in - the admin console needs a steam id it can trust to go back to."""
        return self._post("/api/bug", {"text": text})

    # ---------------------------------------------------------------- leaderboard
    def leaderboard(self, limit=50):
        return self._get("/api/leaderboard?limit=%d" % int(limit))

    # ---------------------------------------------------------------- the stream
    def _run(self):
        attempt = 0
        while not self._stop.is_set():
            try:
                self._read_stream()
                attempt = 0                      # a clean end: reconnect promptly
            except Exception as e:               # noqa: BLE001 — every failure is a reconnect
                if self._stop.is_set():
                    return
                self.connected = False
                self.on_status(False, str(e))
                telemetry.emit("connection.state", connected=False, reason="stream_disconnected", severity="warn")
                attempt = min(attempt + 1, len(RECONNECT_BACKOFF) - 1)
            if self._stop.is_set():
                return
            time.sleep(RECONNECT_BACKOFF[attempt])

    def _read_stream(self):
        req = urllib.request.Request(API_BASE.rstrip("/") + "/api/live")
        req.add_header("authorization", "Bearer " + self.token)
        req.add_header("accept", "text/event-stream")
        req.add_header("cache-control", "no-cache")
        # The stream carries the version too, and for a party member it is the ONLY request
        # that does - see _stamp.
        self._stamp(req)
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise RuntimeError("stream refused: HTTP %s" % response.status)
            self.connected = True
            self.on_status(True, "")
            telemetry.emit("connection.state", connected=True, reason="stream_connected")
            for raw in response:
                if self._stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line or line.startswith(":"):
                    continue              # blank separator, or a ": ping" keep-alive
                if not line.startswith("data: "):
                    continue              # "retry:" and any field we do not use
                try:
                    event = json.loads(line[6:])
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type"):
                    probe = self._browser_probe()
                    if event["type"] == "network_signal":
                        if probe:
                            probe.receive_signal(event)
                        continue
                    if probe and event["type"] in ("unqueued", "match_found", "match_ready", "lobby", "match_connecting"):
                        probe.set_peers([], False)
                    self.on_event(event)
        self.connected = False


def stats():
    """The open endpoint: how many people are online and queueing, before sign-in.
    Returns a dict or None; never raises."""
    try:
        req = urllib.request.Request(API_BASE.rstrip("/") + "/api/live/stats")
        req.add_header("accept", "application/json")
        with urllib.request.urlopen(req, timeout=ACTION_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")
    except Exception:              # noqa: BLE001
        return None
