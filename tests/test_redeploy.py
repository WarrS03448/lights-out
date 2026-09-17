#!/usr/bin/env python3.12
"""A REDEPLOY, with a match in flight.  Run:  python3.12 tests/test_redeploy.py

Sam, 2026-09-15: "we dont want redeploys to kill live matches".

Railway redeploys on every push, and until 2026-09-15 every live match lived in the server
process's memory and nowhere else - so a release landed and ten people mid-veto were told, by a
brand-new container, that they were in no match at all. server/live.cjs now writes each live match
through to Upstash on its match tick and reads them back at start.

This is the harshest version of that, end to end and over the real wire:

  * a real node server, with a real (small, local) Upstash behind it,
  * two real LiveSessions that queue, match, accept and reach the connect window,
  * the server KILLED OUTRIGHT - not asked to stop, killed, which is the case where the
    SIGTERM flush in server.cjs never runs and only the periodic write-through can have
    saved anything,
  * a new server started on the same port, exactly as a new container would be,
  * ...and the two hubs, untouched throughout, reconnect and are handed their match back.

The hubs are never rebuilt here, which is what makes this a different test from
test_wire_rejoin.py: that one throws the CLIENT away and proves the server replays; this one
throws the SERVER away and proves there is anything left to replay.

Needs `node` on PATH. Two spare ports, short clocks, about half a minute.
"""
import heapq
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8935
STORE_PORT = 8936
os.environ["HUB_API_BASE"] = "http://127.0.0.1:%d" % PORT
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ---------------------------------------------------------------------------- a small Upstash
# The server talks to Upstash over its REST protocol: POST a JSON array ["SET", key, value, ...]
# and get {"result": ...} back (server.cjs upstashCmd). That is small enough to stand up here,
# and standing it up is the only way to test a redeploy honestly - the state has to outlive the
# process, so it has to live somewhere that is not the process.
#
# Unknown commands answer null, which is exactly what the real upstashCmd returns when a call
# fails; every caller in live.cjs already copes with that, so an unimplemented command degrades
# the same way a flaky Upstash does instead of crashing the server under test.
class _Store:
    def __init__(self):
        self.strings = {}
        self.sets = {}
        self.lists = {}
        self.lock = threading.Lock()

    def run(self, args):
        if not args:
            return None
        op = str(args[0]).upper()
        key = str(args[1]) if len(args) > 1 else ""
        rest = [str(a) for a in args[2:]]
        with self.lock:
            if op == "SET":
                self.strings[key] = rest[0] if rest else ""
                return "OK"
            if op == "GET":
                return self.strings.get(key)
            if op == "DEL":
                self.strings.pop(key, None)
                self.sets.pop(key, None)
                self.lists.pop(key, None)
                return 1
            if op == "SADD":
                self.sets.setdefault(key, set()).update(rest)
                return len(rest)
            if op == "SREM":
                self.sets.get(key, set()).difference_update(rest)
                return len(rest)
            if op == "SMEMBERS":
                return sorted(self.sets.get(key, set()))
            if op == "LPUSH":
                self.lists.setdefault(key, []).insert(0, rest[0] if rest else "")
                return len(self.lists[key])
            if op == "LRANGE":
                rows = self.lists.get(key, [])
                start, stop = int(rest[0]), int(rest[1])
                return rows[start:(None if stop == -1 else stop + 1)]
            if op == "LTRIM":
                rows = self.lists.get(key, [])
                start, stop = int(rest[0]), int(rest[1])
                self.lists[key] = rows[start:(None if stop == -1 else stop + 1)]
                return "OK"
            if op in ("EXPIRE", "ZADD", "LSET", "ZREVRANGE", "ZCOUNT", "SISMEMBER"):
                return None          # not what this test is about; null is a legal answer
        return None


STORE = _Store()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                     # noqa: N802 — BaseHTTPRequestHandler
        length = int(self.headers.get("content-length") or 0)
        try:
            args = json.loads(self.rfile.read(length) or b"[]")
        except ValueError:
            args = []
        body = json.dumps({"result": STORE.run(args)}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):                            # silence: the test prints its own lines
        return


store_srv = ThreadingHTTPServer(("127.0.0.1", STORE_PORT), _Handler)
threading.Thread(target=store_srv.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------- the server(s)
ENV = dict(
    os.environ,
    PORT=str(PORT), NODE_ENV="test",
    COMP_MATCH_SIZE="2", COMP_ACCEPT_SECONDS="30",
    COMP_LOBBY_SECONDS="180", COMP_CONNECT_SECONDS="180",
    COMP_LIVE_SECONDS="600",
    # The write-through tick. Fast, so the test does not spend ten seconds waiting to be allowed
    # to kill the server - the DEFAULT is 2 s and the point being proven is the same either way.
    COMP_MATCH_TICK_MS="400",
    UPSTASH_REDIS_REST_URL="http://127.0.0.1:%d" % STORE_PORT,
    UPSTASH_REDIS_REST_TOKEN="test-token",
    HUB_TEST_TOKENS="tok-a=76561198000000001,tok-b=76561198000000002",
)


def start_server():
    """One container. Starting a second one after killing the first IS the redeploy."""
    proc = subprocess.Popen(["node", "server.cjs"], cwd=str(REPO / "server"), env=ENV,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(150):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            return proc
        except OSError:
            time.sleep(0.1)
    print("server never came up:", (proc.stderr.read() or b"").decode()[:400])
    proc.kill()
    sys.exit(1)


def kill_server(proc):
    """Killed, not asked to stop. On Windows terminate() is not a SIGTERM, so server.cjs's tidy
    shutdown never runs - which is the point: what survives here survived on the tick alone."""
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # ...and wait for the port to actually come free, or the replacement cannot bind it.
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            time.sleep(0.1)
        except OSError:
            return
    print("the old server never let go of the port")
    sys.exit(1)


srv = start_server()

from hub import competitive as C, i18n     # noqa: E402

i18n.set_language("en")

A_ID, B_ID = "76561198000000001", "76561198000000002"
PANELS = []


class Panel:
    """A minimal main loop: after() schedules, post() hops threads, tick() drains both."""

    def __init__(self, name):
        self.name = name
        self.timers = []
        self.inbox = []
        self.lock = threading.Lock()
        self.seq = 0
        self.app = None
        self.surfaced = 0

    def after(self, ms, fn):
        with self.lock:
            self.seq += 1
            heapq.heappush(self.timers, (time.monotonic() + ms / 1000.0, self.seq, fn))

    def post(self, fn):
        with self.lock:
            self.inbox.append(fn)

    def on_change(self):
        pass

    def surface_match(self):
        self.surfaced += 1

    def map_pool(self):
        return C.competitive_pool(C.DEFAULT_MAPS)

    def save_auth(self, payload):
        pass

    def tick(self):
        with self.lock:
            due, self.inbox = self.inbox, []
            now = time.monotonic()
            while self.timers and self.timers[0][0] <= now:
                due.append(heapq.heappop(self.timers)[2])
        for fn in due:
            fn()


def current_app():
    """The `app` a LiveSession reads. The ranked pack at the version this server publishes, so
    the version gate lets this hub queue (server/live.cjs); no catalogue, so the hub's own
    client-side check stays out of the way and the SERVER's answer is what is under test."""
    catalogue = json.loads((REPO / "server" / "public" / "catalogue.json").read_text("utf-8"))
    entry = next((m for m in catalogue.get("gamemodes", [])
                  if m.get("id") == C.COMPETITIVE_MODE_ID), {})

    class _App:
        state = {"installed": {C.COMPETITIVE_MODE_ID: {"version": entry.get("version", "")}}}
        catalogue = None
    return _App()


def make(name, token, steam_id):
    p = Panel(name)
    p.app = current_app()
    s = C.LiveSession(p)
    s.adopt_account({"steam_id": steam_id, "persona": name, "token": token})
    s.phase = "idle"
    s._start_watchdog()
    PANELS.append(p)
    return s, p


def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for p in PANELS:
            p.tick()
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())


STATE = {}


def need(cond, what):
    if not cond:
        print("  FAIL:", what)
        for key, s in STATE.items():
            print("   ", key, s.phase, s.stage, repr(s.error))
        try:
            srv.kill()
        except Exception:        # noqa: BLE001
            pass
        sys.exit(1)
    print("  ok:", what)


def drive_to_connecting(sessions):
    """The server-authoritative lobby, driven to the connect window (same shape as
    test_wire_rejoin.py: the coin, the choice, the side, then the alternating veto)."""
    acted = {id(s): set() for s in sessions}
    for _ in range(2000):
        for p in PANELS:
            p.tick()
        for s in sessions:
            a = acted[id(s)]
            if s.stage == "coin" and s.i_am_coin_captain() and "coin" not in a:
                s.pick_coin("heads"); a.add("coin")
            elif s.stage == "choice" and s.toss_winner == s.my_team() and s.i_am_captain() \
                    and "choice" not in a:
                s.choose("side"); a.add("choice")
            elif s.stage == "side" and s.side_picker == s.my_team() and s.i_am_captain() \
                    and "side" not in a:
                s.choose_side("attack"); a.add("side")
            elif s.stage == "veto" and s.ban_turn == s.my_team() and s.i_am_captain():
                key = "veto-%d" % len(s.bans)
                if len(s.remaining_maps()) > 1 and key not in a:
                    s.ban(s.remaining_maps()[0]); a.add(key)
        if all(s.phase == "connecting" for s in sessions):
            return True
        time.sleep(0.02)
    return False


try:
    A, pa = make("Sam", "tok-a", A_ID)
    B, pb = make("Friend", "tok-b", B_ID)
    STATE["A"], STATE["B"] = A, B
    need(pump(10, lambda: A.connected and B.connected), "both streams connected")

    A.find_match()
    B.find_match()
    need(pump(25, lambda: A.phase == "found" and B.phase == "found"), "both matched")
    A.accept()
    B.accept()
    need(pump(10, lambda: A.phase == "lobby" and B.phase == "lobby"), "both reached the lobby")
    need(drive_to_connecting([A, B]), "the veto ends in the connect window")

    played_map = A.map
    host_id = (A.host or {}).get("steam_id")
    left_before = A.connect_left
    need(played_map and played_map == B.map, "both agree on the map (%r)" % played_map)
    need(left_before > 0, "and there is a connect clock running (%ss)" % left_before)

    # Give the write-through tick time to land the match. This is the ONLY thing standing
    # between the players and a lost match, which is exactly why it is not hurried past.
    time.sleep(1.5)
    need(any(k.endswith("live:matches") for k in STORE.sets),
         "the live match was written through (keys: %s)" % sorted(STORE.sets))
    saved = [k for k in STORE.strings if "live:match:" in k]
    need(len(saved) == 1, "exactly one live match on disk (%s)" % saved)
    record = json.loads(STORE.strings[saved[0]])
    need(record.get("state") == "connecting", "in the phase it is actually in (%r)" % record.get("state"))
    need(record.get("map") == played_map, "with the map the veto settled on")
    need(record.get("expiry") == "connect", "and WHICH clock it is on, so the next one can re-arm it")

    # ------------------------------------------------------------------ THE REDEPLOY
    print("  -- killing the server (a push lands) --")
    kill_server(srv)
    need(pump(6, lambda: not A.connected and not B.connected),
         "both hubs noticed the service go away")
    need(A.phase == "connecting" and B.phase == "connecting",
         "and neither threw its match away just because the pipe broke")

    srv = start_server()
    print("  -- the new container is up --")

    # The hubs are untouched: their own reconnect backoff brings them back by itself.
    need(pump(40, lambda: A.connected and B.connected), "both hubs reconnected on their own")
    need(pump(20, lambda: A.phase == "connecting" and B.phase == "connecting"),
         "and they are STILL IN THE MATCH (A=%r B=%r)" % (A.phase, B.phase))
    need(A.map == played_map and B.map == played_map,
         "with the same map, not a new veto (%r / %r)" % (A.map, B.map))
    need((A.host or {}).get("steam_id") == host_id, "and the same host")
    need(A.connect_left > 0, "the connect clock is running again (%ss)" % A.connect_left)
    need(len(A.players) == 2, "the roster came back with it")
    need(A.locked_in() and B.locked_in(), "it is a match: sign-out stays hidden")

    # ...and it still finishes. A match that comes back but cannot be played is not a match.
    A.report_connected()
    B.report_connected()
    need(pump(30, lambda: A.phase == "live" and B.phase == "live"),
         "the recovered match goes live when both report in (A=%r B=%r)" % (A.phase, B.phase))

    print("\nREDEPLOY TEST PASSED")
finally:
    try:
        srv.kill()
    except Exception:        # noqa: BLE001
        pass
    store_srv.shutdown()
