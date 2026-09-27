#!/usr/bin/env python3.12
"""Linux beta hubs and Windows 3.0.3 hubs in one party and one match, over the REAL wire.

    .venv/Scripts/python.exe tests/test_wire_mixed_platform.py

WHY. The service answers two protocols. A hub that names itself (`x-hub-platform: linux`, which
the Linux beta puts on every request, the stream included) gets the action scopes: capabilities in
hello, queue_actor/queue_context/queue_unit on queue events, party_context, invite ids, and the
strict /scoped/ routes. Every other hub - every Windows hub in the field - has to see exactly what
it saw before the scopes existed. The two meet in a party and in a match, where one server event
goes to both kinds of stream, so this puts them in the same party both ways round and in the same
1v1 both ways round, and plays each match to live:

    Linux leader + Windows member   vs   Windows leader + Linux member     (5v5 ladder, 2v2)
    Linux vs Windows                                                         (1v1 ladder)

each with a Windows host and again with a Linux host (COMP_PREFER_HOST). Every match goes through
the queue, accept, the server lobby and veto, the connect window, the host's hub launching the
game, the host permit (/api/probe/slow), the host's arrival, every joiner's Launch, the start
snapshot and match_live. The game is simulated from here with the host's own report token; no
Steam, no Bodycam, no game folder.

HOW. Both clients are packages called `hub`, so each platform runs in its own python process
(this file, --worker) with its own sys.path, driven over a JSON line pipe. Each worker records its
hubs' raw wire underneath hub.transport: every request, every answer, every stream line.

Needs node, a Windows 3.0.3 checkout and a codex/linux-beta checkout; without the Linux one it
says so and skips. Nothing is written into either checkout (no bytecode, state in temp dirs).
Environment:
    HUB_LINUX_ROOT    a codex/linux-beta checkout, the Linux client under test (required)
    HUB_SERVER_DIR    the server to test (default: this checkout's server/)
    HUB_WINDOWS_ROOT  the Windows hub checkout (default: C:/w/hub303)
    HUB_TEST_PORT     the port (default 8937)
    MIXED_ONLY        run only the scenarios whose names contain this text

RESULT, 2026-09-27: passes, all four scenarios, against this branch with the Linux client at
e5ec3289 and the Windows hub at eb1f9e37 (3.0.3).
"""
import json
import os
import pathlib
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
SERVER = pathlib.Path(os.environ.get("HUB_SERVER_DIR") or REPO / "server")
LINUX_ROOT = pathlib.Path(os.environ["HUB_LINUX_ROOT"]) if os.environ.get("HUB_LINUX_ROOT") else None
WINDOWS_ROOT = pathlib.Path(os.environ.get("HUB_WINDOWS_ROOT") or "C:/w/hub303")
PORT = int(os.environ.get("HUB_TEST_PORT") or 8937)
BASE = "http://127.0.0.1:%d" % PORT

# What only a scoped stream or a scoped caller may ever be shown.
SCOPED_KEYS = frozenset(("capabilities", "queue_actor", "queue_context", "queue_unit",
                         "party_context", "invite_id"))
CAPABILITIES = ("match_action_scopes_v1", "party_action_scopes_v1", "queue_action_scopes_v1",
                "hub_platform_gate_v1")
ACTIONS = ("/api/queue/", "/api/match/", "/api/party/")
ACTION_VERBS = {"/api/queue/join", "/api/queue/leave",
                *("/api/match/" + v for v in ("accept", "leave", "coin", "choose", "side", "ban",
                                              "chat", "connecting", "connected")),
                *("/api/party/" + v for v in ("create", "join", "leave", "refresh-code", "invite",
                                              "invite/accept", "invite/decline"))}

PLAYERS = {  # name: (platform, token, steam id)
    "L1": ("linux", "tok-l1", "76561198000000011"),
    "W1": ("windows", "tok-w1", "76561198000000012"),
    "W2": ("windows", "tok-w2", "76561198000000013"),
    "L2": ("linux", "tok-l2", "76561198000000014"),
    "L3": ("linux", "tok-l3", "76561198000000015"),
    "W3": ("windows", "tok-w3", "76561198000000016"),
}


# =============================================================================== the worker side
def worker_main(platform, root):
    """One platform's hubs. Commands arrive as JSON lines on stdin; replies go to stdout."""
    import heapq
    import io
    proto = sys.stdout
    sys.stdout = sys.stderr            # the hub prints; nothing it prints may corrupt the pipe
    sys.path.insert(0, root)
    from hub import competitive as C, i18n, game as game_mod, transport, live as live_mod
    from hub import version, match_cleanup, auth
    if platform == "linux":
        version.LINUX_PUBLIC_BETA = True
    i18n.set_language("en")
    catalogue = json.loads(pathlib.Path(os.environ["MIXED_CATALOGUE"]).read_text("utf-8"))
    # The service queues nothing behind the release it publishes, so both report that release.
    # 3.0.3 -> 3.0.4 changed only the settings screen and the version (git diff eb1f9e37 2d25405b).
    C.HUB_VERSION = live_mod.HUB_VERSION = catalogue["hub"]["version"]
    installed = {g["id"]: {"version": g.get("version", "")} for g in catalogue.get("gamemodes", [])
                 if g.get("id") in ("BB5", "BB1")}

    # ------------------------------------------------------------------ no game, ever
    LAUNCHES = []
    launching = [None]
    game_mod.launch_game = lambda: LAUNCHES.append(dict(launching[0] or {}, t=time.time())) or True
    game_mod.launch_game_with_args = lambda *a, **k: True
    game_mod.game_running = lambda: False
    game_mod.close_game = lambda **kw: "not-running"
    for name in ("game_running_cached",):
        if hasattr(game_mod, name):
            setattr(game_mod, name, lambda *a, **k: False)
    if hasattr(game_mod, "game_pids"):
        game_mod.game_pids = lambda: []
    match_cleanup.register = lambda *a, **k: None
    match_cleanup.ensure_worker = lambda *a, **k: None
    match_cleanup.resume_pending_jobs = lambda *a, **k: None

    def host_pak(self):
        self.host_level = str(self.map or "stub")
        self.host_pak_done = True
        return True

    def joiner_pak(self):
        self.pak_done = True
        return True
    C.LiveSession._prepare_host_pak = host_pak
    C.LiveSession._prepare_joiner_pak = joiner_pak
    C.LiveSession._release_host_pak = lambda self: None
    original_launch = C.LiveSession._maybe_launch_game

    def maybe_launch(self, why):
        launching[0] = {"name": self.panel.name, "why": why, "match_id": self.match_id}
        try:
            return original_launch(self, why)
        finally:
            launching[0] = None
    C.LiveSession._maybe_launch_game = maybe_launch

    # ------------------------------------------------------------------ the wire, as sent
    WIRE = {}
    real_urlopen = transport.urlopen

    def parse(raw):
        try:
            return json.loads(raw.decode("utf-8", "replace")) if raw else None
        except ValueError:
            return raw.decode("utf-8", "replace")[:200]

    class Answer:
        def __init__(self, resp, raw):
            self._resp, self._buf = resp, io.BytesIO(raw)
            self.status, self.headers = resp.status, resp.headers

        def read(self, n=-1):
            return self._buf.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._resp.close()

        def close(self):
            self._resp.close()

        def __getattr__(self, key):
            return getattr(self._resp, key)

    class Stream:
        def __init__(self, resp, log, n):
            self._resp, self._log, self._n = resp, log, n
            self.status, self.headers = resp.status, resp.headers

        def __iter__(self):
            for raw in self._resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data: "):
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        event = None
                    if isinstance(event, dict):
                        self._log.append({"kind": "event", "stream": self._n, "event": event,
                                          "t": time.time()})
                yield raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._resp.close()

        def __getattr__(self, key):
            return getattr(self._resp, key)

    def spy(req, *args, **kwargs):
        if isinstance(req, str):
            return real_urlopen(req, *args, **kwargs)
        auth_header = req.get_header("Authorization") or ""
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        log = WIRE.setdefault(token, [])
        url = urllib.parse.urlsplit(req.full_url)
        rec = {"kind": "http", "method": req.get_method(), "path": url.path,
               "query": url.query, "headers": {k.lower(): v for k, v in req.header_items()},
               "body": parse(req.data) if req.data else None, "t": time.time()}
        log.append(rec)
        try:
            resp = real_urlopen(req, *args, **kwargs)
        except urllib.error.HTTPError as e:
            raw = e.read()
            rec.update(status=e.code, answer=parse(raw))
            raise urllib.error.HTTPError(e.url, e.code, e.msg, e.hdrs, io.BytesIO(raw)) from None
        except Exception as e:     # noqa: BLE001
            rec.update(status=0, answer=str(e))
            raise
        rec["status"] = resp.status
        if url.path == "/api/live":
            rec["stream"] = sum(1 for r in log if r.get("path") == "/api/live")
            return Stream(resp, log, rec["stream"])
        raw = resp.read()
        rec["answer"] = parse(raw)
        return Answer(resp, raw)
    import urllib.parse
    transport.urlopen = spy

    # ------------------------------------------------------------------ hubs
    class Panel:
        def __init__(self, name):
            self.name = name
            self.timers, self.inbox, self.seq = [], [], 0
            self.lock = threading.Lock()

            class App:
                state = {"installed": installed}
                catalogue = None
                game_dir = ""
            self.app = App()

        def after(self, ms, fn):
            with self.lock:
                self.seq += 1
                heapq.heappush(self.timers, (time.monotonic() + ms / 1000.0, self.seq, fn))

        def post(self, fn):
            with self.lock:
                self.inbox.append(fn)

        def on_change(self):
            pass

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

    SESSIONS, PANELS = {}, {}
    autopilot = [False]
    acted = {}

    def drive():
        """The server lobby, exactly as tests/test_wire.py drives it."""
        for name, s in SESSIONS.items():
            done = acted.setdefault((name, s.match_id), set())
            if s.stage == "coin" and s.i_am_coin_captain() and "coin" not in done:
                s.pick_coin("heads"); done.add("coin")
            elif s.stage == "choice" and s.toss_winner == s.my_team() and s.i_am_captain() \
                    and "choice" not in done:
                s.choose("side"); done.add("choice")
            elif s.stage == "side" and s.side_picker == s.my_team() and s.i_am_captain() \
                    and "side" not in done:
                s.choose_side("attack"); done.add("side")
            elif s.stage == "veto" and s.ban_turn == s.my_team() and s.i_am_captain():
                key = "veto-%d" % len(s.bans)
                if len(s.remaining_maps()) > 1 and key not in done:
                    s.ban(s.remaining_maps()[0]); done.add(key)

    def make(name, token, steam_id):
        p = Panel(name)
        s = C.LiveSession(p)
        s._network_config = None
        s.adopt_account({"steam_id": steam_id, "persona": name, "token": token})
        s.phase = "idle"
        s._start_watchdog()
        SESSIONS[name], PANELS[name] = s, p
        return True

    def jsonable(value):
        return json.loads(json.dumps(value, default=repr))

    def handle(cmd):
        op, name = cmd.get("op"), cmd.get("name")
        s = SESSIONS.get(name)
        if op == "make":
            return make(name, cmd["token"], cmd["steam_id"])
        if op == "call":
            return getattr(s, cmd["method"])(*cmd.get("args", []))
        if op == "eval":
            return eval(cmd["expr"], {"s": s, "C": C, "S": SESSIONS, "auth": auth,
                                      "LAUNCHES": LAUNCHES, "WIRE": WIRE, "time": time})
        if op == "wire":
            return WIRE.get(s.token if s else cmd.get("token"), [])
        if op == "launches":
            return LAUNCHES
        if op == "autopilot":
            autopilot[0] = bool(cmd.get("on"))
            return autopilot[0]
        if op == "quit":
            for session in SESSIONS.values():
                try:
                    session._disconnect()
                except Exception:      # noqa: BLE001
                    pass
            return "bye"
        raise ValueError("unknown op %r" % op)

    commands = queue.Queue()

    def reader():
        for line in sys.stdin:
            if line.strip():
                commands.put(json.loads(line))
        commands.put({"op": "quit", "id": -1})
    threading.Thread(target=reader, daemon=True).start()

    while True:
        for p in list(PANELS.values()):
            p.tick()
        if autopilot[0]:
            drive()
        try:
            while True:
                cmd = commands.get_nowait()
                try:
                    reply = {"id": cmd.get("id"), "result": jsonable(handle(cmd))}
                except Exception as e:     # noqa: BLE001
                    import traceback
                    reply = {"id": cmd.get("id"), "error": "%s: %s" % (type(e).__name__, e),
                             "trace": traceback.format_exc()[-1500:]}
                proto.write(json.dumps(reply) + "\n")
                proto.flush()
                if cmd.get("op") == "quit":
                    return 0
        except queue.Empty:
            pass
        time.sleep(0.02)


# =========================================================================== the controller side
class Worker:
    def __init__(self, platform, root, logdir):
        self.platform = platform
        self.state = tempfile.mkdtemp(prefix="hub-mixed-%s-" % platform)
        env = dict(os.environ, HUB_API_BASE=BASE, HUB_STATE_DIR=self.state,
                   PYTHONDONTWRITEBYTECODE="1", MIXED_CATALOGUE=str(SERVER / "public" / "catalogue.json"))
        self.log = open(os.path.join(logdir, "%s.log" % platform), "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-B", str(pathlib.Path(__file__).resolve()),
                                      "--worker", platform, str(root)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                     cwd=self.state, env=env, text=True, encoding="utf-8", bufsize=1)
        self.replies, self.cv, self.seq = {}, threading.Condition(), 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            with self.cv:
                self.replies[reply.get("id")] = reply
                self.cv.notify_all()

    def rpc(self, op, timeout=30, **kw):
        with self.cv:
            self.seq += 1
            n = self.seq
        self.proc.stdin.write(json.dumps(dict(kw, op=op, id=n)) + "\n")
        self.proc.stdin.flush()
        end = time.monotonic() + timeout
        with self.cv:
            while n not in self.replies:
                left = end - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    raise RuntimeError("%s worker did not answer %s (see %s)"
                                       % (self.platform, op, self.log.name))
                self.cv.wait(min(left, 0.5))
            reply = self.replies.pop(n)
        if "error" in reply:
            raise RuntimeError("%s worker: %s\n%s" % (self.platform, reply["error"], reply.get("trace", "")))
        return reply["result"]

    def close(self):
        try:
            self.rpc("quit", timeout=10)
        except Exception:      # noqa: BLE001
            pass
        try:
            self.proc.wait(5)
        except Exception:      # noqa: BLE001
            self.proc.kill()
        self.log.close()


class Hub:
    """One player's hub, in whichever worker runs its platform."""
    def __init__(self, name, worker):
        self.name, self.worker = name, worker
        self.platform, self.token, self.steam_id = PLAYERS[name]

    def call(self, method, *args):
        return self.worker.rpc("call", name=self.name, method=method, args=list(args))

    def ev(self, expr):
        return self.worker.rpc("eval", name=self.name, expr=expr)

    def wire(self):
        return self.worker.rpc("wire", name=self.name)

    def launches(self):
        return [l for l in self.worker.rpc("launches") if l.get("name") == self.name]


FAILED = []


def need(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILED.append(what)
    return cond


class Stop(Exception):
    pass


def must(cond, what):
    if not need(cond, what):
        raise Stop(what)


def wait(seconds, until, step=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        time.sleep(step)
    return bool(until())


def post(path, body, token, timeout=15):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + token})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}"), time.monotonic() - started
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}"), time.monotonic() - started
    except Exception as e:       # noqa: BLE001
        return None, str(e), time.monotonic() - started


def scoped_keys_in(value, path=""):
    """Every place a scoped-only key appears in a value, as a readable path."""
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            if k in SCOPED_KEYS:
                found.append(path + "." + k)
            found += scoped_keys_in(v, path + "." + k)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            found += scoped_keys_in(v, "%s[%d]" % (path, i))
    return found


# --------------------------------------------------------------------------- the server
def start_server(logdir, tag, prefer_host):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.3).close()
        raise SystemExit("port %d already answers: refusing to test a leftover server" % PORT)
    except OSError:
        pass
    log_path = os.path.join(logdir, "server-%s.log" % tag)
    log = open(log_path, "w", encoding="utf-8")
    env = dict(os.environ, PORT=str(PORT), NODE_ENV="test",
               COMP_MATCH_SIZE="4", COMP_ACCEPT_SECONDS="20", COMP_NETWORK_TEST_BYPASS="1",
               COMP_TEAMS_GATE_SECONDS="0", COMP_LOBBY_SECONDS="120", COMP_CONNECT_SECONDS="60",
               COMP_LIVE_SECONDS="120", PROBE_SLOW_SECONDS="1", COMP_PREFER_HOST=prefer_host,
               HUB_STORE_PREFIX="hubtest_mixed_%s:" % tag,
               HUB_TEST_TOKENS=",".join("%s=%s" % (tok, sid) for _, tok, sid in PLAYERS.values()))
    for key in ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        env.pop(key, None)
    proc = subprocess.Popen(["node", "server.cjs"], cwd=str(SERVER), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    up = wait(15, lambda: _answers(PORT) or proc.poll() is not None)
    log.flush()
    text = pathlib.Path(log_path).read_text("utf-8", "replace")
    if not up or proc.poll() is not None or "EADDRINUSE" in text:
        proc.kill()
        raise SystemExit("server did not come up on %d:\n%s" % (PORT, text[-800:]))
    return proc, log, log_path


def _answers(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.2).close()
        return True
    except OSError:
        return False


def stop_server(proc, log):
    proc.terminate()
    try:
        proc.wait(5)
    except Exception:          # noqa: BLE001
        proc.kill()
    log.close()


# --------------------------------------------------------------------------- the steps
def connect_all(hubs):
    for h in hubs:
        h.worker.rpc("make", name=h.name, token=h.token, steam_id=h.steam_id)
    must(wait(15, lambda: all(h.ev("s.connected") for h in hubs)),
         "every hub's stream is up (%s)" % ", ".join(h.name for h in hubs))
    for h in hubs:
        hello = next((r["event"] for r in h.wire() if r["kind"] == "event"
                      and r["event"].get("type") == "hello"), None)
        must(hello is not None, "%s received hello" % h.name)
        if h.platform == "linux":
            caps = hello.get("capabilities") or {}
            need(all(caps.get(c) is True for c in CAPABILITIES),
                 "%s (Linux) is offered all four capabilities: %s" % (h.name, caps))
            need(h.ev("s.client._match_scopes_ready") is True and h.ev("s.client.platform_gate_ready") is True,
                 "%s (Linux) client adopted the match and platform capabilities" % h.name)
        else:
            need("capabilities" not in hello, "%s (Windows) hello carries no capabilities" % h.name)


def befriend(a, b):
    a.call("add_friend", b.steam_id)
    must(wait(10, lambda: any(r["kind"] == "event" and r["event"].get("type") in ("friend_request", "friend_update")
                              for r in b.wire())),
         "%s sees %s's friend request" % (b.name, a.name))
    b.call("accept_friend", a.steam_id)
    must(wait(10, lambda: any(str(f.get("steam_id")) == b.steam_id
                              for f in (a.ev("[dict(f) for f in (s.friends or [])]") or []))
              or _friends_now(a, b)), "%s and %s are friends" % (a.name, b.name))


def _friends_now(a, b):
    a.call("refresh_friends")
    time.sleep(0.3)
    rows = a.ev("[dict(f) for f in (s.friends or [])]") or []
    return any(str(f.get("steam_id")) == b.steam_id for f in rows)


def party_by_code(leader, member):
    leader.call("create_party")
    must(wait(10, lambda: bool(leader.ev("(s.party or {}).get('code')"))),
         "%s (%s) created a party" % (leader.name, leader.platform))
    code = leader.ev("s.party['code']")
    member.call("join_party", code)
    must(wait(10, lambda: leader.ev("s.party_size()") == 2 and member.ev("s.party_size()") == 2
              and member.ev("(s.party or {}).get('code')") == code),
         "%s (%s) joined %s's party by code; both see two members"
         % (member.name, member.platform, leader.name))


def party_by_invite(leader, member):
    befriend(leader, member)
    leader.call("create_party")
    must(wait(10, lambda: bool(leader.ev("(s.party or {}).get('code')"))),
         "%s (%s) created a party" % (leader.name, leader.platform))
    leader.call("invite_to_party", member.steam_id)
    must(wait(10, lambda: bool(member.ev("list(s.party_invites)"))),
         "%s (%s) received %s's invite" % (member.name, member.platform, leader.name))
    member.call("accept_party_invite", leader.steam_id)
    must(wait(10, lambda: leader.ev("s.party_size()") == 2 and member.ev("s.party_size()") == 2),
         "%s (%s) accepted %s's invite; both see two members" % (member.name, member.platform, leader.name))
    need(member.ev("s.party['leader_id']") == leader.steam_id, "%s's party is led by %s" % (member.name, leader.name))


def search_and_cancel(leader, member):
    """A mixed party's search starts and stops for BOTH members, whichever platform leads."""
    leader.call("find_match")
    must(wait(15, lambda: leader.ev("s.phase") == "queued" and member.ev("s.phase") == "queued"),
         "%s (%s) searched for the party; both are queued: %s" % (leader.name, leader.platform,
         {h.name: h.ev("(s.phase, s.error)") for h in (leader, member)}))
    leader.call("cancel_queue")
    must(wait(15, lambda: leader.ev("s.phase") == "idle" and member.ev("s.phase") == "idle"),
         "%s (%s) cancelled; both are back to idle: %s" % (leader.name, leader.platform,
         {h.name: h.ev("(s.phase, s.error)") for h in (leader, member)}))
    need(any(r["kind"] == "event" and r["event"].get("type") == "unqueued" for r in member.wire()),
         "%s (%s) was told 'unqueued'" % (member.name, member.platform))


def play_to_live(hubs, leaders, workers):
    for h in leaders:
        h.call("find_match")
    must(wait(25, lambda: all(h.ev("s.phase") == "found" for h in hubs)),
         "everyone was matched: %s" % {h.name: h.ev("(s.phase, s.error)") for h in hubs})
    match_id = hubs[0].ev("s.match_id")
    must(all(h.ev("s.match_id") == match_id for h in hubs), "one match for everyone (%s)" % match_id)
    for h in hubs:
        h.call("accept")
    must(wait(15, lambda: all(h.ev("s.phase") == "lobby" for h in hubs)), "everyone reached the lobby")
    for w in workers:
        w.rpc("autopilot", on=True)
    ok = wait(40, lambda: all(h.ev("s.phase") == "connecting" for h in hubs), step=0.2)
    for w in workers:
        w.rpc("autopilot", on=False)
    must(ok, "the veto ended in the connect window: %s" % {h.name: h.ev("(s.phase, s.stage, s.error)") for h in hubs})
    maps = {h.ev("s.map") for h in hubs}
    need(len(maps) == 1, "everyone agrees on the map (%s)" % maps)
    hosts = {h.ev("(s.host or {}).get('steam_id')") for h in hubs}
    must(len(hosts) == 1, "everyone agrees on the host (%s)" % hosts)
    host_id = hosts.pop()
    host = next(h for h in hubs if h.steam_id == host_id)
    joiners = [h for h in hubs if h is not host]
    must(host.ev("s._i_am_host()") is True, "%s (%s) knows it is the host" % (host.name, host.platform))

    # THE INCIDENT'S STEP: the host's hub opens the game as soon as the window opens.
    need(wait(10, lambda: any(l["why"] == "host" and l["match_id"] == match_id for l in host.launches())),
         "the host's hub (%s, %s) launched the game for this match" % (host.name, host.platform))
    launch = next((l for l in host.launches() if l["why"] == "host"), None)
    window = next((r["t"] for r in host.wire() if r["kind"] == "event"
                   and r["event"].get("type") == "match_connecting"), None)
    if launch and window:
        print("       the host's hub launched %.2fs after its stream delivered match_connecting"
              % (launch["t"] - window))
    need(not any(l["why"] == "host" for h in joiners for l in h.launches()), "no joiner launched as host")
    token = host.ev("s.report_token")
    must(isinstance(token, str) and len(token) == 64, "the host holds a 64-hex report token")
    need(all(not h.ev("s.report_token") for h in joiners), "no joiner was handed the report token")
    game_id = host.ev("(s.host or {}).get('game_steam_id') or s.host['steam_id']")

    # The host's game, simulated with the host's own credential.
    status, body, took = post("/api/probe/slow", {"ip": "", "event_name": "ch_host_wait", "user_id": "",
                                                  "storefront": "host", "platform": "",
                                                  "timestamp": "", "first_session_timestamp": "chlobby-28",
                                                  "is_first_game_open": False}, token, timeout=20)
    need(status == 200 and took < 10, "the host permit is granted: /api/probe/slow answered %s in %.1fs" % (status, took))
    if host.platform == "windows":
        # The host's cleanup worker polls this beside its game (hub/match_cleanup.py _receipt).
        got = host.ev("auth._request('/api/match/completion?id=' + s.match_id, token=s.token, timeout=10)")
        need(got[0] == 409 and got[1].get("ok") is False and got[1].get("close_allowed") is False
             and not scoped_keys_in(got[1]),
             "the Windows host's completion poll gets the in-progress answer (%s %s)" % tuple(got))
    status, body, _ = post("/api/match-report", {"event_name": "ch_lobby_read", "user_id": game_id,
                                                 "storefront": "chm-" + match_id, "platform": "",
                                                 "timestamp": "", "first_session_timestamp": "",
                                                 "is_first_game_open": False}, token)
    need(status == 200, "the host's game reports in from the match world (%s %s)" % (status, body))
    need(wait(10, lambda: all(h.ev("s.host_ready") for h in joiners)),
         "every joiner is released (host_ready): %s" % {h.name: h.ev("s.host_ready") for h in joiners})
    for h in joiners:
        h.call("launch_game")
    need(wait(10, lambda: all(any(l["why"] == "player" and l["match_id"] == match_id for l in h.launches())
                               for h in joiners)),
         "every joiner's Launch opened the game")
    need(wait(10, lambda: all(len(h.ev("list(s.connected_ids)")) == len(hubs) for h in hubs)),
         "everyone sees everyone connected: %s" % {h.name: len(h.ev("list(s.connected_ids)")) for h in hubs})
    teams = host.ev("{str(k): [p['steam_id'] for p in v] for k, v in s.teams.items()}")
    rows = [sid + ":" + str(int(side) - 1) + ":1;" for side in ("1", "2") for sid in teams[side]]
    status, body, _ = post("/api/match-report/start-ready",
                           {"event_name": "ch_start_ready", "user_id": game_id,
                            "storefront": "chm-" + match_id, "platform": str(len(rows)) + "|" + "".join(rows)},
                           token)
    need(status == 200, "the start snapshot is accepted (%s %s)" % (status, body))
    must(wait(15, lambda: all(h.ev("s.phase") == "live" for h in hubs)),
         "THE MATCH WENT LIVE for everyone: %s" % {h.name: h.ev("(s.phase, s.error)") for h in hubs})
    return match_id, host


def check_wire(hubs):
    """Windows saw nothing scoped; Linux spoke the scopes and every scoped action succeeded."""
    for h in hubs:
        wire = h.wire()
        requests = [r for r in wire if r["kind"] == "http"]
        events = [r["event"] for r in wire if r["kind"] == "event"]
        actions = [r for r in requests if r["method"] == "POST" and r["path"].startswith(ACTIONS)]
        failed = ["%s %s -> %s %s" % (r["method"], r["path"], r.get("status"), r.get("answer"))
                  for r in actions if r.get("status") != 200]
        need(not failed, "%s (%s): every queue/match/party action answered 200%s"
             % (h.name, h.platform, "" if not failed else ": " + "; ".join(failed)))
        streams = [r for r in requests if r["path"] == "/api/live"]
        if h.platform == "windows":
            leaks = ["%s%s" % (e.get("type"), p) for e in events for p in scoped_keys_in(e)]
            need(not leaks, "%s (Windows): no scoped field on any of %d events%s"
                 % (h.name, len(events), "" if not leaks else ": " + ", ".join(sorted(set(leaks)))))
            answers = ["%s%s" % (r["path"], p) for r in requests for p in scoped_keys_in(r.get("answer"))]
            need(not answers, "%s (Windows): no scoped field in any of %d answers%s"
                 % (h.name, len(requests), "" if not answers else ": " + ", ".join(sorted(set(answers)))))
            need(not [r for r in requests if "/scoped/" in r["path"]], "%s (Windows): no /scoped/ route used" % h.name)
            need(not [r for r in requests if "x-hub-platform" in r["headers"]],
                 "%s (Windows): no request names a platform" % h.name)
        else:
            unnamed = ["%s %s" % (r["method"], r["path"]) for r in requests
                       if r["headers"].get("x-hub-platform") != "linux"]
            need(not unnamed and streams, "%s (Linux): all %d requests, %d stream(s) included, say x-hub-platform: linux%s"
                 % (h.name, len(requests), len(streams), "" if not unnamed else ": " + ", ".join(unnamed)))
            bare = sorted({e.get("type") for e in events
                           if not all(k in e for k in ("queue_actor", "queue_context", "queue_unit"))})
            need(not bare, "%s (Linux): all %d events carry queue_actor/queue_context/queue_unit%s"
                 % (h.name, len(events), "" if not bare else ": missing on " + ", ".join(map(str, bare))))
            parties = [e for e in events if e.get("type") == "party_update"]
            need(parties and all(len(str(e.get("party_context") or "")) == 32 for e in parties),
                 "%s (Linux): all %d party_update events carry a party_context" % (h.name, len(parties)))
            rows = [row for e in events if e.get("type") == "party_invites" for row in e.get("invites") or []]
            need(all(len(str(row.get("invite_id") or "")) == 32 for row in rows),
                 "%s (Linux): all %d invite rows carry an invite_id" % (h.name, len(rows)))
            legacy = sorted({r["path"] for r in actions if r["path"] in ACTION_VERBS})
            need(not legacy, "%s (Linux): no unscoped action route used%s"
                 % (h.name, "" if not legacy else ": " + ", ".join(legacy)))
            scoped = sorted({r["path"] for r in actions if "/scoped/" in r["path"]})
            print("       %s scoped routes: %s" % (h.name, ", ".join(scoped)))


# --------------------------------------------------------------------------- the scenarios
def scenario_parties(workers, windows_host, p1_invite):
    """P1 = Linux leader + Windows member, P2 = Windows leader + Linux member, one 2v2."""
    lin, win = workers
    L1, W1, W2, L2 = Hub("L1", lin), Hub("W1", win), Hub("W2", win), Hub("L2", lin)
    hubs = [L1, W1, W2, L2]
    connect_all(hubs)
    if p1_invite:
        party_by_invite(L1, W1)
        party_by_code(W2, L2)
    else:
        party_by_code(L1, W1)
        party_by_invite(W2, L2)
    search_and_cancel(L1, W1)
    search_and_cancel(W2, L2)
    match_id, host = play_to_live(hubs, [L1, W2], workers)
    need(host.platform == ("windows" if windows_host else "linux"), "the host was the %s hub asked for (%s)"
         % ("Windows" if windows_host else "Linux", host.name))
    teams = L1.ev("{str(k): sorted(p['steam_id'] for p in v) for k, v in s.teams.items()}")
    need(sorted(map(sorted, teams.values())) == sorted([sorted([L1.steam_id, W1.steam_id]),
                                                         sorted([W2.steam_id, L2.steam_id])]),
         "each mixed party stayed together on its own team (%s)" % teams)
    check_wire(hubs)


def scenario_duel(workers, windows_host):
    lin, win = workers
    L3, W3 = Hub("L3", lin), Hub("W3", win)
    hubs = [L3, W3]
    connect_all(hubs)
    for h in hubs:
        h.call("select_ranked_mode", "BB1")
    must(all(h.ev("s.ranked_mode") == "BB1" for h in hubs), "both hubs are on the 1v1 ladder")
    match_id, host = play_to_live(hubs, hubs, workers)
    need(host.platform == ("windows" if windows_host else "linux"), "the host was the %s hub asked for (%s)"
         % ("Windows" if windows_host else "Linux", host.name))
    modes = {r["headers"].get("x-ranked-mode") for h in hubs for r in h.wire()
             if r["kind"] == "http" and r["method"] == "POST" and r["path"].startswith("/api/match/")}
    need(modes == {"BB1"}, "every match action was addressed to the 1v1 ladder (%s)" % modes)
    check_wire(hubs)


SCENARIOS = [
    ("parties, Windows host", PLAYERS["W2"][2], lambda w: scenario_parties(w, True, True)),
    ("parties, Linux host", PLAYERS["L1"][2], lambda w: scenario_parties(w, False, False)),
    ("1v1, Windows host", PLAYERS["W3"][2], lambda w: scenario_duel(w, True)),
    ("1v1, Linux host", PLAYERS["L3"][2], lambda w: scenario_duel(w, False)),
]


def main():
    if LINUX_ROOT is None or not (LINUX_ROOT / "hub" / "linux_beta.py").is_file():
        print("SKIPPED: set HUB_LINUX_ROOT to a codex/linux-beta checkout (hub/linux_beta.py); "
              "got %s" % (LINUX_ROOT or "nothing"))
        return 0
    if not (WINDOWS_ROOT / "hub" / "competitive.py").is_file():
        print("no Windows hub checkout at %s (HUB_WINDOWS_ROOT)" % WINDOWS_ROOT)
        return 2
    logdir = tempfile.mkdtemp(prefix="hub-mixed-logs-")
    print("server: %s   linux hub: %s   windows hub: %s   port: %d   logs: %s"
          % (SERVER, LINUX_ROOT, WINDOWS_ROOT, PORT, logdir))
    only = os.environ.get("MIXED_ONLY", "")
    for i, (title, prefer, run) in enumerate(SCENARIOS):
        if only and only not in title:
            continue
        print("\n--- %s ---" % title)
        proc, log, _ = start_server(logdir, str(i), prefer)
        workers = []
        try:
            workers = [Worker("linux", LINUX_ROOT, logdir), Worker("windows", WINDOWS_ROOT, logdir)]
            run(workers)
        except Stop:
            pass
        except Exception as e:     # noqa: BLE001
            need(False, "%s: %s" % (type(e).__name__, e))
        finally:
            for w in workers:
                w.close()
            stop_server(proc, log)
    print()
    if FAILED:
        print("MIXED PLATFORM WIRE TEST FAILED: %d" % len(FAILED))
        for f in FAILED:
            print("   -", f)
        print("logs:", logdir)
        return 1
    print("MIXED PLATFORM WIRE TEST PASSED")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        sys.exit(worker_main(sys.argv[2], sys.argv[3]))
    sys.exit(main())
