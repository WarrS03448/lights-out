#!/usr/bin/env python3.12
"""Wire parity for a legacy Windows hub: the action-scopes redeploy, checked on the wire.

Run:  <venv python> tests/test_wire_legacy_parity.py [--ref SERVER_DIR | --ref-commit REV]
                          [--gate SERVER_DIR] [--self-check] [--skip SECTION] [--keep DIR]
Needs `node` on PATH, a python with fakeredis[lua] (the shared .venv has it) and a Windows hub
checkout (HUB303_DIR, default C:/w/hub303 = eb1f9e37 = 3.0.3; C:/w/hub306 = d80ac059 = 3.0.6 is
the current release). Ports: PARITY_PORT_BASE (default 19200) to +21.
Takes about three minutes per server run.

WHY THIS EXISTS. Deploy a78f9384 (the queue/match/party action scopes and the per-platform version
gate) stalled the only two connect windows it saw: the host's Windows hub never launched Bodycam.
It was rolled back as 547b805a and the cause is still unknown. The redeploy is allowed only if a
hub that does not say `x-hub-platform: linux` sees EXACTLY what the pre-scopes server showed it -
events, fields, statuses, bodies and their order - so that whatever the cause was, a Windows hub
cannot meet it again. This proves that on the wire, with the real hub.

WHAT RUNS. The UNMODIFIED Windows hub (hub/live.py LiveClient and hub/competitive.py LiveSession,
imported from HUB303_DIR, default C:/w/hub303 = eb1f9e37 = 3.0.3) over real HTTP and SSE, against
  A  the reference server, which has to be what production runs NOW: origin/main d80ac059 (hub
     3.0.6; it was 2d25405b, hub 3.0.4, until main moved past it). --ref names a checkout's
     server/, and without it `git archive <--ref-commit, default d80ac059> server` of this
     repository is unpacked into the work dir (the server needs no npm packages for any of this),
     and
  B  the gate server, this tree's server/ (--gate),
each with its own fakeredis-backed Upstash REST stand-in, one after the other, then compares.
Nothing here edits the hub: it is instrumented from outside (transport.urlopen, the session's
_action seam, the panel it posts to, and the game/pak stand-ins test_wire_ban_launch uses).

The hubs report the hub version the reference publishes (hub.version in its public/catalogue.json:
3.0.6 at d80ac059; PARITY_HUB_VERSION overrides it). The gate is STRICT, so a hub reporting
anything older cannot queue on either server. 3.0.3 -> 3.0.6 left hub/live.py alone and changed
the wire code only where a relaunch meets a stale host world (3c1cd127: competitive.py
relaunch_game and match_recovery.launch_allowed), which the day never reaches, so hub303 drives
the 3.0.6 wire; point HUB303_DIR at a d80ac059 checkout to run 3.0.6's own code as well. One
extra hub (111) still reports 3.0.3 and is refused at the queue (the 426 body is part of the
comparison).

THE DAY (one server process per target, everything in this order):
  party   friend requests; a party created and an invite accepted; a join by code; a bad code
          (404); refresh-code; an invite declined; invite-from-friends (create + invite); a member
          leaves and rejoins; a party member's and a solo's stream dropped and replayed.
  queue   relay profiles; a stale-version join (426); a party search ended by a member's region
          change, then by a member's {unavailable:true}, then by a member leaving the party; a
          search left by a member; a solo leave; a party search while a member has the 1v1 tab
          open, whose relay posts (a new location, {unavailable:true}) go through BB1 and must
          leave the BB5 search alone.
  5v5     a party of 3 + a party of 2 + 5 solos: queue; a searcher's stream dropped (the hub goes
          idle and searches again); a searcher whose relay location changed; relay measurements;
          accept; chat; coin, choice, side and the whole veto, with a revision change,
          {unavailable:true}, a refused region change, a refused party leave and a replayed lobby
          stream mid-veto; the connect window: the host's launch, the host's stream dropped and
          replayed, relay refreshes inside the window, the cleanup worker's completion poll, the
          /api/probe/slow permit, the host's report-in, a joiner's stream replayed, every joiner's
          launch + connected, start-ready, live, the live-phase polls, a replayed live stream, and
          a player leaving the live match (/api/match/leave).
  1v1     the same on BB1 (coin, side), with {unavailable:true} and a region change while queued.
  decline a 1v1 that one player never accepts, and the requeue of the other.
  resend  a party invite re-sent while the first is still pending.
--skip resend (or tab) leaves that section out.

WHAT IS RECORDED, per hub: every SSE line of every stream it opened (full JSON), and every HTTP
exchange it made (method, path, query, x-ranked-mode, request body, status, response body), plus
the game-side requests a pak makes with the report credential, the launches, and the paks the hub
asked for. Each HTTP exchange is anchored to how many stream events that hub had seen when it was
sent, so the comparison covers the interleaving of the two channels as well as each channel.

DETERMINISM, without which "A == B" would mean nothing:
  - Every hub request is held at the transport and released one at a time, in the order the
    hubs made them on their (single, emulated) main thread, and nothing is released or ticked
    until the wire has been quiet for Q seconds. The panel runs stream callbacks before request
    results. So both servers see the same requests in the same order with the same state behind.
  - Every stream (first connect and every reconnect) is opened one at a time, when released.
  - The server's own crypto.randomBytes is made repeatable by a preload (-r): one hash stream per
    byte length, so match ids, team shuffles, coin faces, party codes and report tokens are the
    same on both servers. The scopes draw only 16- and 24-byte values, which no pre-scopes wire
    value shares with anything structural; remaining random hex is numbered by first appearance.
  - The same preload tags every SSE line a server TIMER writes with a comment line naming where
    the timer was armed (a hub skips comment lines). Broadcasts from the account directory's 15 s
    refresh (admin-accounts.cjs, a stats pair to every stream) are dropped and counted: they come
    from a wall clock, not from anything a hub did. The first directory scan is waited out before
    any hub connects, so stats never flip players_registered null -> 0 mid-run.
  - Heartbeats are dropped: a `stats` event followed at once by `: ping` is the 15 s heartbeat.
  - Requests a hub makes because a clock ran (the stats-driven /api/tournament poll every >=15 s,
    the live-phase /api/match/recovery `status` poll every 5 s) are neither held nor ordered: they
    are compared as a SET of distinct exchanges, with their countdowns (live_seconds) blanked. A
    poll whose connection failed before an answer (status 0) is counted and left out, and so is a
    status poll answered 409 "Superseded recovery request" before that match's first 200: it
    went out on the hub's clock as the match went live and beat the server's write of the
    match's recovery records (either server, and the reference against itself).
  - Timestamps (epoch ms/s, ISO) are blanked; countdown fields (TIMER_KEYS) compare within 2 s.
  - Nothing reads this machine's state: game running/launch, the pak, the cleanup worker and the
    recovery poll's tasklist observation ("running") are all stood in for.
Run with --self-check to compare the reference with ITSELF first: anything that differs there is
harness noise, not a server difference, and has to be fixed before an A/B result means anything.

RESULT, 2026-09-27 (gate = claude/scopes-legacy-gate after 9b8cc451, reference = 2d25405b):
  --self-check, every section: the reference against itself and the gate against the reference
  are IDENTICAL (100 steps each, 2457 stream entries, 396 ordered exchanges, 27 distinct polls).
  Controls, each flagged by this test:
    - the ungated scopes head 745f0df0 differs on every hub from its first stream event
      (hello.capabilities, queue_actor/queue_context/queue_unit, the solo party_update);
    - the gate before 9b8cc451 (21b8a6fc) failed the `tab` step (the member's BB1 posts ended the
      leader's BB5 search) and differed on the resend: it kept the re-sent invite where 2d25405
      sends the invitee an empty inbox and answers the accept 409 "That invite has expired.".
  Incidental, identical on both servers: POST /api/match/recovery (the hub's live-phase recovery
  poll) is not in live.cjs NEEDS_AUTH, so server.cjs answers 404 "Not found." without reading the
  body, and many of those polls end client-side in ConnectionAbortedError. Fixing that changes
  what a Windows hub sees, so it is not part of the scopes redeploy.
"""
import argparse
import hashlib
import heapq
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parent.parent
HUB303 = pathlib.Path(os.environ.get("HUB303_DIR") or "C:/w/hub303")
REF_SERVER = os.environ.get("PARITY_REF_SERVER") or ""
# origin/main, the pre-scopes wire production runs: 3.0.6 (it was 2d25405b, 3.0.4, before main moved)
REF_COMMIT = os.environ.get("PARITY_REF_COMMIT") or "d80ac059"
GATE_SERVER = REPO / "server"
PORT_BASE = int(os.environ.get("PARITY_PORT_BASE") or 19200)
# What the hubs report: the reference's published hub version, which orchestrate() reads from its
# catalogue and hands to each driver through PARITY_HUB_VERSION (set it to override).
REPORTED_HUB_VERSION = os.environ.get("PARITY_HUB_VERSION") or "3.0.6"


def published_hub_version(server_dir):
    """hub.version in a server's public/catalogue.json: the oldest hub its STRICT gate queues."""
    try:
        catalogue = json.loads((pathlib.Path(server_dir) / "public" / "catalogue.json").read_text("utf-8"))
        return str(catalogue["hub"]["version"] or "")
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def sid(n):
    return "76561198000000%03d" % n


FIVE = [sid(n) for n in range(101, 111)]
PARTY_A, PARTY_B, SOLOS = FIVE[0:3], FIVE[3:5], FIVE[5:10]
OLD = sid(111)
DUEL = [sid(201), sid(202)]
DECLINE = [sid(203), sid(204)]
EVERYONE = FIVE + [OLD] + DUEL + DECLINE
TOKENS = {s: "tok-%s" % s[-3:] for s in EVERYONE}
SID_OF = {t: s for s, t in TOKENS.items()}

Q = 0.3                          # seconds of wire silence that count as "settled"
ACCEPT_SECONDS = 20
SERVER_ENV = {
    "NODE_ENV": "test", "COMP_ACCEPT_SECONDS": str(ACCEPT_SECONDS), "COMP_LOBBY_SECONDS": "300",
    "COMP_CONNECT_SECONDS": "180", "COMP_FLIP_SECONDS": "1", "PROBE_SLOW_SECONDS": "2",
    # Gate-only, logging-only (the "[connect] ... silent" line); the reference ignores it.
    "COMP_CONNECT_SILENT_SECONDS": "600",
}
TRANSPORT = "webrtc-relay-v1"
SDP = "\r\n".join(["v=0", "o=- 1 2 IN IP4 0.0.0.0", "s=-", "c=IN IP4 0.0.0.0", "t=0 0",
                   "m=application 9 UDP/DTLS/SCTP webrtc-datachannel", "a=sctp-port:5000"]) + "\r\n"

# Countdown fields: computed from a server deadline and Date.now(), so they move with wall time.
# Compared numerically within TIMER_SLACK rather than blanked, so a wrong clock still shows.
TIMER_KEYS = {"seconds", "accept_seconds", "lobby_seconds", "connect_seconds", "stage_seconds",
              "ban_seconds", "expires_in", "join_seconds", "wait_seconds", "waited", "left",
              "seconds_left", "remaining", "age", "live_seconds"}
TIMER_SLACK = 2
# The preload tags every SSE line a server timer writes (see PRELOAD_JS). Broadcasts from these
# files' timers run on a wall clock of their own, not on anything a hub did, so they are dropped
# from the comparison (and counted): admin-accounts.cjs is the account directory's 15 s refresh,
# whose onUpdate re-broadcasts `stats` to every stream on both ladders.
TIMER_TAG = ": parity-timer "
CLOCK_FILES = {"admin-accounts.cjs"}

PRELOAD_JS = r"""'use strict';
// wire-parity preload (test only): the server's own crypto.randomBytes becomes repeatable, one
// hash stream per byte length, so two servers fed the same requests draw the same values.
const crypto = require('node:crypto');
const path = require('node:path');
const norm = s => String(s || '').replace(/\\/g, '/').toLowerCase();
const root = norm(path.resolve(process.env.WIRE_PARITY_SERVER || process.cwd())) + '/';
const self = norm(__filename);
const seed = process.env.WIRE_PARITY_SEED || 'parity';
const counters = new Map();
function serverCaller() {
  const lines = String(new Error().stack || '').split('\n').slice(1);
  for (const line of lines) {
    const l = norm(line);
    if (l.includes(self)) continue;
    return l.includes(root) && !l.includes('/node_modules/');
  }
  return false;
}
function draw(size) {
  const n = (counters.get(size) || 0) + 1;
  counters.set(size, n);
  const out = Buffer.alloc(size);
  let off = 0, block = 0;
  while (off < size) {
    const h = crypto.createHash('sha256').update(seed + '|' + size + '|' + n + '|' + (block++)).digest();
    off += h.copy(out, off);
  }
  return out;
}
const real = crypto.randomBytes;
crypto.randomBytes = function randomBytes(size, callback) {
  if (!Number.isSafeInteger(size) || size < 0 || !serverCaller()) return real.apply(this, arguments);
  const buf = draw(size);
  if (typeof callback === 'function') { process.nextTick(callback, null, buf); return undefined; }
  return buf;
};

// Every SSE `data:` line written from inside a server timer (setTimeout/setInterval callback, and
// anything async it started) is preceded by a comment naming where that timer was armed:
//   : parity-timer <file>:<function>
// A hub skips comment lines, so the hub sees exactly what it would have seen; the recorder uses
// the tag to tell a clock-driven broadcast (the account directory's 15 s refresh) from one a
// request caused.
const {AsyncLocalStorage} = require('node:async_hooks');
const als = new AsyncLocalStorage();
function armedAt() {
  const lines = String(new Error().stack || '').split('\n').slice(1);
  for (const line of lines) {
    const l = norm(line);
    if (l.includes(self)) continue;
    if (!l.includes(root) || l.includes('/node_modules/')) return null;
    const file = /\/([^/]+\.c?js):\d+:\d+/.exec(l);
    const fn = /at (?:async )?([^\s(]+) \(/.exec(line);
    return (file ? file[1] : '?') + ':' + (fn ? fn[1] : '<anonymous>');
  }
  return null;
}
for (const name of ['setTimeout', 'setInterval']) {
  const realTimer = global[name];
  global[name] = function (fn, ...rest) {
    const where = typeof fn === 'function' ? armedAt() : null;
    if (!where) return realTimer.call(this, fn, ...rest);
    return realTimer.call(this, function (...args) {
      return als.run({timer: where}, () => fn.apply(this, args));
    }, ...rest);
  };
}
const http = require('node:http');
const realWrite = http.ServerResponse.prototype.write;
http.ServerResponse.prototype.write = function (chunk, ...rest) {
  const store = als.getStore();
  if (store && typeof chunk === 'string' && chunk.startsWith('data: '))
    chunk = ': parity-timer ' + store.timer + '\n' + chunk;
  return realWrite.call(this, chunk, ...rest);
};
console.error('[wire-parity] repeatable randomBytes, tagged timer broadcasts for ' + root);
"""


# ====================================================================== Upstash stand-in
def upstash_main(port):
    """Test-only Upstash REST stand-in: POST a JSON command array, get {"result": ...}.

    Single-threaded on purpose: commands are answered in the order they arrive, which removes one
    source of reordering between two runs (two async store reads racing each other)."""
    import fakeredis
    from http.server import BaseHTTPRequestHandler, HTTPServer
    db = fakeredis.FakeRedis(decode_responses=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            try:
                command = json.loads(raw)
                result = db.execute_command(*command)
                name = str(command[0]).upper()
                if name == "PING" and result is True:
                    result = "PONG"
                elif name == "SET" and result is True:
                    result = "OK"
                elif isinstance(result, set):
                    result = sorted(result)
                elif result is True:
                    result = 1
                body = {"result": result}
            except Exception as error:  # noqa: BLE001
                body = {"error": str(error)}
                print("FAILED %s: %s" % (raw[:300], error), file=sys.stderr, flush=True)
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ====================================================================== orchestration
def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.3).close()
        return True
    except OSError:
        return False


def wait_until(fn, seconds, step=0.1):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(step)
    return bool(fn())


class Stack:
    """One server under test with its own store, on ports [port, port + 1]."""

    def __init__(self, tag, server_dir, port, work):
        self.tag, self.server_dir, self.port, self.work = tag, pathlib.Path(server_dir), port, work
        self.procs = []
        self.server_log = work / ("%s-server.log" % tag)

    def start(self):
        up_port = self.port + 1
        for p in (self.port, up_port):
            if port_open(p):
                raise SystemExit("port %d is already taken - stop whatever holds it (it may be a "
                                 "leftover server from an earlier run)" % p)
        up_log = open(self.work / ("%s-upstash.log" % self.tag), "w", encoding="utf-8")
        up = subprocess.Popen([sys.executable, str(HERE), "--upstash", str(up_port)],
                              stdout=up_log, stderr=subprocess.STDOUT)
        self.procs.append(up)
        if not wait_until(lambda: port_open(up_port), 20):
            raise SystemExit("the Upstash stand-in never came up on %d" % up_port)
        preload = self.work / "wire-parity-preload.cjs"
        preload.write_text(PRELOAD_JS, encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("UPSTASH", "REDIS", "RAILWAY", "COMP_", "PROBE_", "SMTP",
                                            "HUB_", "WIRE_PARITY"))}
        env.update(SERVER_ENV, PORT=str(self.port),
                   UPSTASH_REDIS_REST_URL="http://127.0.0.1:%d" % up_port,
                   UPSTASH_REDIS_REST_TOKEN="parity",
                   HUB_TEST_TOKENS=",".join("%s=%s" % (t, s) for s, t in TOKENS.items()),
                   WIRE_PARITY_SERVER=str(self.server_dir.resolve()),
                   WIRE_PARITY_SEED="lights-out-wire-parity")
        log = open(self.server_log, "w", encoding="utf-8")
        srv = subprocess.Popen(["node", "-r", str(preload), "server.cjs"], cwd=str(self.server_dir),
                               env=env, stdout=log, stderr=subprocess.STDOUT)
        self.procs.append(srv)

        def up():
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/live/stats" % self.port,
                                            timeout=2) as r:
                    return r.status == 200
            except Exception:  # noqa: BLE001
                return srv.poll() is not None
        wait_until(up, 60, 0.25)

        def directory_ready():
            # Until the account directory's first scan completes, stats say players_registered
            # null; it turns into a number at a moment no hub action decides. Start after it.
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/live/stats" % self.port,
                                            timeout=2) as r:
                    return bool(re.search(rb'"players_registered"\s*:\s*\d', r.read()))
            except Exception:  # noqa: BLE001
                return False
        if not wait_until(directory_ready, 30, 0.25):
            print("[%s] warning: the account directory never reported players_registered" % self.tag)
        time.sleep(1.0)
        text = self.server_log.read_text("utf-8", "replace")
        if srv.poll() is not None or "EADDRINUSE" in text:
            raise SystemExit("%s server did not start cleanly (see %s):\n%s"
                             % (self.tag, self.server_log, text[-2000:]))
        if "[wire-parity] repeatable randomBytes, tagged timer broadcasts" not in text:
            raise SystemExit("%s server did not load the parity preload (see %s)"
                             % (self.tag, self.server_log))

    def stop(self):
        for p in reversed(self.procs):
            try:
                p.terminate()
                p.wait(10)
            except Exception:  # noqa: BLE001
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.procs = []


def run_target(tag, server_dir, port, work, skip):
    stack = Stack(tag, server_dir, port, work)
    out = work / ("%s-recording.json" % tag)
    state = pathlib.Path(tempfile.mkdtemp(prefix="parity-hub-%s-" % tag, dir=str(work)))
    print("[%s] %s on :%d" % (tag, server_dir, port), flush=True)
    stack.start()
    try:
        env = dict(os.environ, HUB_API_BASE="http://127.0.0.1:%d" % port, HUB_STATE_DIR=str(state),
                   PYTHONIOENCODING="utf-8")
        cmd = [sys.executable, str(HERE), "--drive", "--server-dir", str(server_dir), "--out", str(out)]
        for name in skip:
            cmd += ["--skip", name]
        started = time.monotonic()
        with open(work / ("%s-driver.log" % tag), "w", encoding="utf-8") as log:
            code = subprocess.call(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1500)
        print("[%s] driver exit %s in %.0f s" % (tag, code, time.monotonic() - started), flush=True)
    finally:
        stack.stop()
    if not out.exists():
        raise SystemExit("[%s] the driver wrote no recording; see %s"
                         % (tag, work / ("%s-driver.log" % tag)))
    run = json.loads(out.read_text("utf-8"))
    run["server_log"] = stack.server_log.read_text("utf-8", "replace")
    up_log = work / ("%s-upstash.log" % tag)
    run["store_failures"] = [l for l in up_log.read_text("utf-8", "replace").splitlines()
                             if l.startswith("FAILED")] if up_log.exists() else []
    return run


# ====================================================================== normalisation
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d")
HEX = re.compile(r"^[0-9a-f]{16,128}$")


class Normaliser:
    """Blank what is clock-derived; number random hex by first appearance (per run).

    number=False (the polls): random hex is not numbered and countdowns are blanked outright. A
    poll is sent on the hub's own clock, so which countdown value it catches is the clock's."""

    def __init__(self, number=True):
        self.tags, self.number = {}, number

    def hexid(self, value):
        if not self.number:
            return "<hex%d>" % len(value)
        if value not in self.tags:
            self.tags[value] = "<hex%d:%d>" % (len(value), len(self.tags) + 1)
        return self.tags[value]

    def __call__(self, v, key=None):
        if isinstance(v, bool) or v is None:
            return v
        if isinstance(v, (int, float)):
            if 1.5e12 <= v <= 2.5e12:
                return "<ts>"
            if 1.5e9 <= v <= 2.5e9 and key not in TIMER_KEYS:
                return "<ts-s>"
            if key in TIMER_KEYS:
                return {"~": v} if self.number else "<countdown>"
            return v
        if isinstance(v, str):
            if ISO.match(v):
                return "<iso>"
            if HEX.match(v) and not re.fullmatch(r"\d+", v):
                return self.hexid(v)
            return v
        if isinstance(v, list):
            return [self(x, key) for x in v]
        if isinstance(v, dict):
            return {k: self(x, k) for k, x in v.items()}
        return v


def is_poll(rec):
    if rec["m"] == "GET" and rec["p"] == "/api/tournament":
        return True
    if rec["p"] == "/api/match/recovery" and isinstance(rec.get("b"), dict) \
            and rec["b"].get("operation") == "status":
        return True
    return False


def digest(run):
    """A run as the comparison sees it: per hub, the stream without heartbeats, the HTTP exchanges
    in order (anchored to that stream), and the set of clock-driven polls."""
    out = {"hubs": {}, "facts": {}}
    labels = sorted(set(run["streams"]) | set(run["http"]))
    for label in labels:
        # Numbered per hub, so an extra random value on one hub's wire is reported on that hub
        # instead of renumbering every hub after it. Polls are unordered, so theirs are not
        # numbered at all (their order would decide the numbers).
        n, unnumbered = Normaliser(), Normaliser(number=False)
        raw = run["streams"].get(label, [])
        kept, before, dropped, clocked, failed_polls, raced_polls = [], [], 0, 0, 0, 0
        i = 0
        while i < len(raw):
            before.append(len(kept))
            e = raw[i]
            nxt = raw[i + 1] if i + 1 < len(raw) else None
            if e["k"] == "comment" and e.get("t", "").startswith(TIMER_TAG):
                i += 1                                  # the preload's tag, not hub-visible
                continue
            if (e["k"] == "ev" and isinstance(e.get("e"), dict) and e["e"].get("type") == "stats"
                    and nxt and nxt["k"] == "comment" and nxt.get("t") == ": ping"):
                before.append(len(kept))
                i += 2
                dropped += 1
                continue
            if e["k"] == "comment" and e.get("t") == ": ping":
                i += 1
                continue
            if e["k"] == "ev" and str(e.get("timer", "")).split(":")[0] in CLOCK_FILES:
                i += 1
                clocked += 1
                continue
            kept.append(n({k: v for k, v in e.items() if k != "timer"}))
            i += 1
        before.append(len(kept))
        ordered, polls, answered = [], set(), set()
        for rec in run["http"].get(label, []):
            view = {"m": rec["m"], "p": rec["p"], "q": rec.get("q") or "", "mode": rec.get("mode"),
                    "b": rec.get("b"), "s": rec.get("s"), "r": rec.get("r")}
            if rec.get("err"):
                view["err"] = rec["err"]
            if is_poll(rec):
                # A poll whose connection failed (the server answered before reading the body and
                # closed; see the report) has no server answer to compare.
                if rec.get("s") == 0:
                    failed_polls += 1
                    continue
                # The hub's first recovery status poll goes out on its own 5 s clock as the match
                # goes live, and can land before the server has written the match's recovery
                # records: 409 "Superseded recovery request" instead of 200. Whether it does is the
                # wall clock's doing (seen on either server, and on the reference against itself),
                # so a superseded poll before that match's first answered one is counted and left
                # out. After it, a superseded poll is compared like any other.
                match = (rec.get("b") or {}).get("match_id") if isinstance(rec.get("b"), dict) else None
                if rec["p"] == "/api/match/recovery":
                    if rec.get("s") == 200:
                        answered.add(match)
                    elif (rec.get("s") == 409 and match not in answered and
                          (rec.get("r") or {}).get("error") == "Superseded recovery request"):
                        raced_polls += 1
                        continue
                polls.add(json.dumps(unnumbered({k: view[k] for k in ("m", "p", "s", "r")}),
                                     sort_keys=True))
                continue
            a = rec.get("anchor")
            view["at_event"] = before[a] if isinstance(a, int) and a < len(before) else (
                len(kept) if isinstance(a, int) else None)
            ordered.append(n(view))
        out["hubs"][label] = {"stream": kept, "http": ordered, "polls": sorted(polls),
                              "heartbeats": dropped, "clock_broadcasts": clocked,
                              "failed_polls": failed_polls, "raced_polls": raced_polls}
    out["facts"] = Normaliser()({"launches": run.get("launches", []), "prepared": run.get("prepared", []),
                                 "steps": [(s["name"], s["ok"]) for s in run.get("steps", [])]})
    return out


def same(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) == {"~"} and set(b) == {"~"}:
            return abs(a["~"] - b["~"]) <= TIMER_SLACK
        return list(a) == list(b) and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def show(x, limit=1600):
    s = json.dumps(x, sort_keys=False)
    return s if len(s) <= limit else s[:limit] + " ..."


def first_diff(a, b, path="$"):
    """Where two normalised values first disagree, as (path, a-part, b-part)."""
    if isinstance(a, dict) and isinstance(b, dict) and not (set(a) == {"~"} == set(b)):
        if list(a) != list(b):
            if set(a) == set(b):
                return path, "key order %s" % list(a), "key order %s" % list(b)
            return path, "keys %s" % sorted(set(a) - set(b)), "keys %s" % sorted(set(b) - set(a))
        for k in a:
            if not same(a[k], b[k]):
                return first_diff(a[k], b[k], path + "." + k)
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b)):
            if not same(x, y):
                return first_diff(x, y, "%s[%d]" % (path, i))
        if len(a) != len(b):
            return path, "length %d" % len(a), "length %d" % len(b)
    return path, a, b


def compare(A, B, names=("A", "B")):
    """Every difference, per hub and channel, with context."""
    problems = []
    for label in sorted(set(A["hubs"]) | set(B["hubs"])):
        ha, hb = A["hubs"].get(label), B["hubs"].get(label)
        if ha is None or hb is None:
            problems.append("%s: present only in %s" % (label, names[0] if hb is None else names[1]))
            continue
        for channel in ("stream", "http"):
            la, lb = ha[channel], hb[channel]
            for i in range(max(len(la), len(lb))):
                x = la[i] if i < len(la) else None
                y = lb[i] if i < len(lb) else None
                if same(x, y):
                    continue
                where = first_diff(x, y) if x is not None and y is not None else ("$", x, y)
                ctx = la[max(0, i - 2):i]
                problems.append("\n".join([
                    "%s %s[%d] differs at %s (%s has %d, %s has %d)" % (
                        label, channel, i, where[0], names[0], len(la), names[1], len(lb)),
                    "   before: " + " | ".join(show(c, 300) for c in ctx),
                    "   %s: %s" % (names[0], show(x)),
                    "   %s: %s" % (names[1], show(y)),
                    "   %s part: %s" % (names[0], show(where[1], 600)),
                    "   %s part: %s" % (names[1], show(where[2], 600))]))
                break
        pa, pb = set(ha["polls"]), set(hb["polls"])
        if pa != pb:
            problems.append("%s polls: only %s %s ; only %s %s" % (
                label, names[0], sorted(pa - pb)[:3], names[1], sorted(pb - pa)[:3]))
    fa, fb = A["facts"], B["facts"]
    for key in fa:
        if not same(fa[key], fb.get(key)):
            problems.append("facts.%s differ:\n   %s: %s\n   %s: %s" % (
                key, names[0], show(fa[key]), names[1], show(fb.get(key))))
    return problems


def reference_server(commit, work):
    """server/ at `commit`, unpacked from this repository into the work dir (no worktree)."""
    import tarfile
    out = work / ("ref-%s" % re.sub(r"[^A-Za-z0-9._-]", "_", commit))
    if not (out / "server" / "server.cjs").exists():
        data = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", commit, "server"],
                              capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(out, filter="data")
            else:
                tar.extractall(out)
    print("reference: %s at %s -> %s" % (REPO, commit, out / "server"))
    return out / "server"


def orchestrate(args):
    work = pathlib.Path(args.keep).resolve() if args.keep else pathlib.Path(
        tempfile.mkdtemp(prefix="wire-parity-"))
    work.mkdir(parents=True, exist_ok=True)
    if not (HUB303 / "hub" / "live.py").exists():
        print("SKIPPED: no Windows hub checkout at %s (set HUB303_DIR; e.g. "
              "git worktree add --detach <dir> eb1f9e37, or d80ac059 for 3.0.6)" % HUB303)
        return 0
    ref = pathlib.Path(args.ref).resolve() if args.ref else reference_server(args.ref_commit, work)
    gate = pathlib.Path(args.gate).resolve()
    for d in (ref, gate):
        if not (d / "server.cjs").exists():
            raise SystemExit("not a server directory: %s" % d)
    global REPORTED_HUB_VERSION
    if not os.environ.get("PARITY_HUB_VERSION"):
        published = {d: published_hub_version(d) for d in (ref, gate)}
        if published[ref] != published[gate]:
            raise SystemExit("the reference publishes hub %r and the gate %r: a hub cannot report "
                             "both (set PARITY_HUB_VERSION)" % (published[ref], published[gate]))
        REPORTED_HUB_VERSION = published[ref] or REPORTED_HUB_VERSION
        os.environ["PARITY_HUB_VERSION"] = REPORTED_HUB_VERSION   # the drivers inherit it
    print("hub under test: %s (reporting %s)" % (HUB303, REPORTED_HUB_VERSION))
    print("work dir: %s" % work)
    if args.recompare:
        # Compare recordings an earlier run kept (--keep), without starting anything.
        saved = {p.name.split("-recording.json")[0]: json.loads(p.read_text("utf-8"))
                 for p in work.glob("*-recording.json")}
        digests = {tag: digest(run) for tag, run in saved.items()}
        failed = False
        for x, y in [(a, b) for a, b in (("A2", "A"), ("A", "B")) if a in digests and b in digests]:
            problems = compare(digests[x], digests[y], names=(x, y))
            print("==== %s vs %s: %s" % (x, y, "IDENTICAL" if not problems else "%d DIFFERENCES" % len(problems)))
            for problem in problems:
                print(problem)
            failed = failed or bool(problems)
        return 1 if failed else 0
    runs = []
    if args.self_check:
        runs.append(("A2", ref, PORT_BASE + 20))
    runs += [("A", ref, PORT_BASE)]
    if not args.only_ref:
        runs.append(("B", gate, PORT_BASE + 10))
    recorded = {tag: run_target(tag, d, port, work, args.skip) for tag, d, port in runs}
    digests = {tag: digest(run) for tag, run in recorded.items()}
    for tag, d in digests.items():
        (work / ("%s-digest.json" % tag)).write_text(json.dumps(d, indent=1), "utf-8")
    failed = False
    for tag, run in recorded.items():
        bad = [s for s in run.get("steps", []) if not s["ok"]]
        errors = run.get("errors", [])
        print("\n[%s] %d steps, %d failed, %d harness errors, %d hubs" % (
            tag, len(run.get("steps", [])), len(bad), len(errors), len(digests[tag]["hubs"])))
        for s in bad:
            print("   FAILED STEP %s: %s" % (s["name"], s.get("detail", "")))
        for e in errors[:10]:
            print("   ERROR %s" % e[:600])
        for line in run.get("store_failures", [])[:5]:
            print("   store command refused by the stand-in (same code on both servers): %s" % line[:300])
        if bad or errors:
            failed = True
    pairs = ([("A2", "A")] if args.self_check else []) + ([] if args.only_ref else [("A", "B")])
    for x, y in pairs:
        problems = compare(digests[x], digests[y], names=(x, y))
        title = "SELF-CHECK (reference vs itself)" if x == "A2" else "PARITY (reference vs gate)"
        print("\n==== %s: %s" % (title, "IDENTICAL" if not problems else "%d DIFFERENCES" % len(problems)))
        for p in problems:
            print(p)
        if problems:
            failed = True
    for tag, d in digests.items():
        hubs = d["hubs"].values()
        print("[%s] compared %d stream entries, %d ordered HTTP exchanges, %d distinct polls; "
              "left out: %d heartbeats, %d directory-clock broadcasts, %d polls with no answer, "
              "%d status polls that raced the go-live write" % (
                  tag, sum(len(h["stream"]) for h in hubs), sum(len(h["http"]) for h in hubs),
                  sum(len(h["polls"]) for h in hubs), sum(h["heartbeats"] for h in hubs),
                  sum(h["clock_broadcasts"] for h in hubs), sum(h["failed_polls"] for h in hubs),
                  sum(h.get("raced_polls", 0) for h in hubs)))
    print("\nrecordings, digests and logs: %s" % work)
    print("WIRE PARITY TEST %s" % ("FAILED" if failed else "PASSED"))
    return 1 if failed else 0


# ====================================================================== the driver (child)
TL = threading.local()
WIRE = None
C = LV = T = AUTH = None
ORIG_URLOPEN = None
BASE = ""
ANCHOR_DIRECT = [True]
GAME_LABEL = {}                 # report token -> "game:<steam id>"
LAUNCHES, PREPARED = [], []
STEPS, ERRORS = [], []
S, PANELS = {}, []
REV = {}
LAST_PROFILE = {}
T0 = [time.monotonic()]


class Wire:
    def __init__(self, labels):
        self.cv = threading.Condition(threading.RLock())
        self.seq = 0
        self.spawned, self.finished, self.posted = set(), set(), set()
        self.expects_post = {}
        self.pending = {}
        self.last = time.monotonic()
        self.streams = {l: [] for l in labels}
        self.http = {}
        self.gates = {l: threading.Event() for l in labels}
        self.current = {}
        self.opens = {l: 0 for l in labels}
        self.tentative = {}
        self.timer_tag = {}

    def bump(self):
        with self.cv:
            self.last = time.monotonic()
            self.cv.notify_all()

    def line(self, label, text):
        if text.startswith("data: "):
            try:
                entry = {"k": "ev", "e": json.loads(text[6:])}
            except ValueError:
                entry = {"k": "bad", "t": text}
        elif text.startswith(":"):
            entry = {"k": "comment", "t": text}
        else:
            entry = {"k": "field", "t": text}
        self.mark(label, entry)

    def mark(self, label, entry):
        """Record a stream line. A `stats` line is held as TENTATIVE activity until the next line
        on that stream says what it was: `: ping` right behind it makes it the 15 s heartbeat,
        which must not keep the wire from ever looking quiet; anything else makes it real."""
        with self.cv:
            now = time.monotonic()
            self.streams.setdefault(label, []).append(entry)
            if entry["k"] == "comment" and entry.get("t", "").startswith(TIMER_TAG):
                self.timer_tag[label] = entry["t"][len(TIMER_TAG):].strip()
                return
            origin = self.timer_tag.pop(label, None)
            if origin is not None and entry["k"] == "ev":
                entry["timer"] = origin
                if origin.split(":")[0] in CLOCK_FILES:
                    return                             # a clock-driven broadcast: not activity
            held = self.tentative.pop(label, None)
            if entry["k"] == "comment" and entry.get("t") == ": ping" and held is not None:
                pass                                   # a heartbeat: not activity
            else:
                if held is not None:
                    self.last = max(self.last, held)
                if entry["k"] == "ev" and isinstance(entry.get("e"), dict) \
                        and entry["e"].get("type") == "stats":
                    self.tentative[label] = now
                else:
                    self.last = now
            self.cv.notify_all()

    def last_activity(self):
        with self.cv:
            return max([self.last] + list(self.tentative.values()))


def _label(req):
    auth = dict(req.header_items()).get("Authorization", "")
    tok = auth[7:] if auth.startswith("Bearer ") else ""
    return SID_OF.get(tok) or GAME_LABEL.get(tok) or "anonymous"


def _parse(data):
    if not data:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return {"_raw": data.decode("utf-8", "replace")[:2000]}


class _Reply:
    def __init__(self, status, data, headers):
        self.status, self.headers, self._buf = status, headers, io.BytesIO(data)

    def read(self, n=-1):
        return self._buf.read(-1 if n is None else n)

    def getcode(self):
        return self.status

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _exchange(req, timeout, label, src, anchor, quiet=False):
    parts = urllib.parse.urlsplit(req.full_url)
    headers = dict(req.header_items())
    rec = {"src": src, "m": req.get_method(), "p": parts.path, "q": parts.query,
           "mode": headers.get("X-ranked-mode"), "b": _parse(req.data)}
    with WIRE.cv:
        rec["anchor"] = len(WIRE.streams[label]) if anchor and label in WIRE.streams else None
        if not quiet:
            WIRE.last = time.monotonic()
    try:
        resp = ORIG_URLOPEN(req, timeout=timeout)
        with resp:
            data, status, rh = resp.read(), resp.status, resp.headers
        rec.update(s=status, r=_parse(data))
        outcome = ("ok", status, data, rh)
    except urllib.error.HTTPError as e:
        data = e.read()
        rec.update(s=e.code, r=_parse(data))
        outcome = ("http", e, data)
    except Exception as e:  # noqa: BLE001 - recorded, then handed back exactly as it was raised
        rec.update(s=0, err=type(e).__name__)
        outcome = ("exc", e)
    with WIRE.cv:
        WIRE.http.setdefault(label, []).append(rec)
        if not quiet:
            WIRE.last = time.monotonic()
        WIRE.cv.notify_all()
    return outcome


class _Stream:
    """The SSE response, passed through line by line and recorded."""

    def __init__(self, label, real):
        self.label, self.real, self.status = label, real, real.status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        try:
            self.real.close()
        except Exception:  # noqa: BLE001
            pass
        return False

    def __iter__(self):
        try:
            for raw in self.real:
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if text:
                    WIRE.line(self.label, text)
                yield raw
        except Exception as e:  # noqa: BLE001
            WIRE.mark(self.label, {"k": "dropped"})
            raise
        WIRE.mark(self.label, {"k": "dropped"})

    def kill(self):
        """A network blip: the socket goes away under the reader, as it would on a real drop."""
        try:
            self.real.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            try:
                self.real.close()
            except Exception:  # noqa: BLE001
                pass


def wire_urlopen(request, timeout=20):
    parts = urllib.parse.urlsplit(request.full_url)
    label = _label(request)
    if parts.path == "/api/live":
        threading.current_thread()._wire_stream = True
        gate = WIRE.gates[label]
        gate.wait()
        gate.clear()
        try:
            real = ORIG_URLOPEN(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            WIRE.mark(label, {"k": "open", "s": e.code})
            raise
        stream = _Stream(label, real)
        with WIRE.cv:
            WIRE.current[label] = stream
            WIRE.opens[label] += 1
        WIRE.mark(label, {"k": "open", "s": real.status})
        return stream
    seq = getattr(TL, "seq", None)
    background = is_poll({"m": request.get_method(), "p": parts.path, "b": _parse(request.data)})
    if seq is not None and not background:
        gate = threading.Event()
        with WIRE.cv:
            WIRE.pending[seq] = (gate, label, parts.path)
            WIRE.cv.notify_all()
        gate.wait()
    # A clock-driven poll is neither held nor counted as activity: a hub makes one whenever its
    # clock says so, it changes nothing the others see, and it is compared as a set (is_poll).
    outcome = _exchange(request, timeout, label,
                        "poll" if background else "hub" if seq is not None else "direct",
                        anchor=not background and (seq is not None or ANCHOR_DIRECT[0]),
                        quiet=background)
    if outcome[0] == "ok":
        return _Reply(outcome[1], outcome[2], outcome[3])
    if outcome[0] == "http":
        e = outcome[1]
        raise urllib.error.HTTPError(e.url, e.code, e.msg, e.hdrs, io.BytesIO(outcome[2]))
    raise outcome[1]


class Panel:
    """The hub's main loop, emulated: after() schedules, post() hops threads, tick() drains.

    Stream callbacks run before request results, results in the order the requests were made, so
    the order of work does not depend on which thread the OS happened to wake first."""

    def __init__(self, name, app):
        self.name, self.app = name, app
        self.lock = threading.Lock()
        self.timers, self.tseq = [], 0
        self.main, self.stream, self.results = [], [], []

    def after(self, ms, fn):
        with self.lock:
            self.tseq += 1
            heapq.heappush(self.timers, (time.monotonic() + ms / 1000.0, self.tseq, fn))

    def post(self, fn):
        seq = getattr(TL, "seq", None)
        with self.lock:
            if seq is not None:
                self.results.append((seq, fn))
            elif getattr(threading.current_thread(), "_wire_stream", False):
                self.stream.append(fn)
            else:
                self.main.append(fn)
        if seq is not None:
            with WIRE.cv:
                WIRE.posted.add(seq)
                WIRE.cv.notify_all()

    def on_change(self):
        pass

    def map_pool(self):
        return C.competitive_pool(C.DEFAULT_MAPS)

    def save_auth(self, payload):
        pass

    def tick(self):
        """Run what is due. Returns how many POSTED callbacks ran: the hub's own clocks (the
        once-a-second countdowns and watchdog) never stop, so they cannot count as activity.
        Anything a clock does that matters shows up as a posted callback or a held request."""
        with self.lock:
            inbox = self.main + self.stream + [fn for _, fn in sorted(self.results, key=lambda r: r[0])]
            self.main, self.stream, self.results = [], [], []
            now, timers = time.monotonic(), []
            while self.timers and self.timers[0][0] <= now:
                timers.append(heapq.heappop(self.timers)[2])
        for fn in inbox + timers:
            try:
                fn()
            except Exception:  # noqa: BLE001
                ERRORS.append("%s: %s" % (self.name, traceback.format_exc()))
        return len(inbox)


def current_app(server_dir):
    catalogue = json.loads((server_dir / "public" / "catalogue.json").read_text("utf-8"))
    installed = {m.get("id"): {"version": m.get("version", "")}
                 for m in catalogue.get("gamemodes", []) if m.get("id") in ("BB5", "BB1")}

    class _App:
        state = {"installed": installed}
        catalogue = None
        game_dir = os.path.join(os.environ["HUB_STATE_DIR"], "fake-bodycam")
    return _App()


def instrument():
    """The seams: transport, _action, the game and the pak. Nothing in hub/ is edited."""
    global ORIG_URLOPEN
    ORIG_URLOPEN = T.urlopen
    T.urlopen = wire_urlopen

    orig_action = C.LiveSession._action

    def _action(self, call, on_result=None):
        with WIRE.cv:
            WIRE.seq += 1
            seq = WIRE.seq
            WIRE.spawned.add(seq)
            WIRE.expects_post[seq] = on_result is not None

        def wrapped():
            TL.seq = seq
            try:
                return call()
            finally:
                with WIRE.cv:
                    WIRE.finished.add(seq)
                    WIRE.cv.notify_all()
        return orig_action(self, wrapped, on_result)
    C.LiveSession._action = _action

    # Never a real game, never a real pak (test_wire_ban_launch's stand-ins).
    C.game_mod.game_running = lambda *a, **k: False
    C.game_mod.game_running_cached = lambda *a, **k: False
    C.game_mod.launch_game = lambda *a, **k: True
    C.lobbypak_mod.remove = lambda *a, **k: True
    C.match_cleanup.register = lambda *a, **k: None
    # The live-phase recovery poll asks the OS whether Bodycam is running (tasklist). The game
    # these hubs "launched" is running as far as they are concerned, and the real process list
    # of this machine must not decide what goes on the wire.
    from hub import match_recovery
    match_recovery.game_observation = lambda: "running"
    orig_launch = C.LiveSession._maybe_launch_game
    orig_host_pak = C.LiveSession._prepare_host_pak
    orig_join_pak = C.LiveSession._prepare_joiner_pak

    def maybe_launch(self, why):
        me = self.me["steam_id"]
        C.game_mod.launch_game = lambda *a, **k: LAUNCHES.append([me, why, self.match_id]) or True
        try:
            return orig_launch(self, why)
        finally:
            C.game_mod.launch_game = lambda *a, **k: True

    def prepare_for(self):
        def prepare(game, mode, level, **k):
            token = str(k.get("report_token") or "")
            PREPARED.append([self.me["steam_id"], k.get("role") or "host", str(mode), str(level),
                             token])
            if len(token) == 64:
                GAME_LABEL[token] = "game:" + self.me["steam_id"]
            return "/Game/Maps/" + str(level)
        return prepare

    def host_pak(self):
        C.lobbypak_mod.prepare = prepare_for(self)
        return orig_host_pak(self)

    def join_pak(self):
        C.lobbypak_mod.prepare = prepare_for(self)
        return orig_join_pak(self)
    C.LiveSession._maybe_launch_game = maybe_launch
    C.LiveSession._prepare_host_pak = host_pak
    C.LiveSession._prepare_joiner_pak = join_pak


# ---------------------------------------------------------------- the scheduler
def quiet(q=Q):
    while True:
        idle = time.monotonic() - WIRE.last_activity()
        if idle >= q:
            return
        time.sleep(min(q - idle, 0.05) + 0.005)


def workers_settled(timeout=3.0):
    end = time.monotonic() + timeout
    with WIRE.cv:
        while True:
            outstanding = WIRE.spawned - WIRE.finished
            if outstanding <= set(WIRE.pending):
                return True
            left = end - time.monotonic()
            if left <= 0:
                ERRORS.append("workers never reached the wire: %s" % sorted(outstanding - set(WIRE.pending)))
                WIRE.finished |= outstanding - set(WIRE.pending)
                return False
            WIRE.cv.wait(min(left, 0.05))


def release_next():
    with WIRE.cv:
        if not WIRE.pending:
            return False
        seq = min(WIRE.pending)
        gate, label, path = WIRE.pending.pop(seq)
    gate.set()
    with WIRE.cv:
        ok = WIRE.cv.wait_for(lambda: seq in WIRE.finished and (
            not WIRE.expects_post.get(seq) or seq in WIRE.posted), timeout=30)
    if not ok:
        ERRORS.append("request %d %s %s never finished" % (seq, label, path))
    return True


def step():
    quiet()
    ran = sum(p.tick() for p in PANELS)
    workers_settled()
    if release_next():
        return True
    return ran > 0


def settle(until=None, seconds=30, drive=None, name=None):
    """Run the hubs until nothing is pending, the wire is quiet and `until` holds."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if step():
            continue
        if drive and drive():
            continue
        if until is None or until():
            return True
        time.sleep(0.05)
    ok = until is None or bool(until())
    if name:
        note(name, ok, "" if ok else "timed out after %d s" % seconds)
    return ok


def note(name, ok, detail=""):
    STEPS.append({"name": name, "ok": bool(ok), "detail": detail})
    print("  %5.0fs %s %s %s" % (time.monotonic() - T0[0], "ok  " if ok else "FAIL", name, detail),
          flush=True)
    return ok


def check(name, cond, sessions=(), detail=""):
    if not cond and sessions:
        detail += " | " + "; ".join("%s phase=%s stage=%s err=%r" % (
            s.me["steam_id"][-3:], s.phase, getattr(s, "stage", ""), s.error) for s in sessions)
    return note(name, cond, detail)


def within(name, until, seconds=30, drive=None, sessions=()):
    ok = settle(until, seconds, drive)
    return check(name, ok, sessions)


# ---------------------------------------------------------------- hubs
def make(steam_id, server_dir, mode="BB5", hub_version=None):
    panel = Panel(steam_id, current_app(server_dir))
    s = C.LiveSession(panel)
    s._network_config = None          # relay measurements are posted by the day, over the hub's client
    if hub_version:
        base = s._versions
        s._versions = lambda: {**base(), "hub": hub_version}
    s.adopt_account({"steam_id": steam_id, "persona": "P" + steam_id[-3:], "token": TOKENS[steam_id]})
    s.phase = "idle"
    if mode != "BB5":
        s.select_ranked_mode(mode)
    s._start_watchdog()
    S[steam_id] = s
    PANELS.append(panel)
    return s


def hellos(label):
    entries = WIRE.streams[label]
    last_open = max((i for i, e in enumerate(entries) if e["k"] == "open"), default=-1)
    return sum(1 for e in entries[last_open + 1:] if e["k"] == "ev" and e["e"].get("type") == "hello")


def open_stream(label):
    """Let this hub's (next) stream connect, and wait for both engines' hello and the replay."""
    before = WIRE.opens[label]
    WIRE.gates[label].set()
    ok = settle(lambda: WIRE.opens[label] > before and hellos(label) >= 2, 20)
    settle()
    return ok


def drop_stream(label):
    """Take the stream away and bring it back: a reconnect with the server replaying the state."""
    stream = WIRE.current.get(label)
    count = len(WIRE.streams[label])
    stream.kill()
    ok = settle(lambda: any(e["k"] == "dropped" for e in WIRE.streams[label][count:]), 10)
    return open_stream(label) and ok


def post(s, path, body):
    """A POST through the hub's own client (its headers, its x-ranked-mode), from the main loop."""
    return s.client._post(path, body)


def location(steam_id, version=0):
    return hashlib.sha256(("%s:%d" % (steam_id, version)).encode()).hexdigest()[:32]


def profile(s, region="NA", version=0, cross=False):
    """The relay path's profile post (hub/relaytransport.py refresh), as a 3.0.3 hub sends it."""
    me = s.me["steam_id"]
    LAST_PROFILE[me] = (region, version)
    status, body = post(s, "/api/network/profile", {
        "region": region, "cross_region": cross, "location": location(me, version),
        "age_seconds": 0, "transport": TRANSPORT})
    if status == 200 and isinstance(body, dict) and body.get("revision"):
        REV[me] = body["revision"]
    return status, body


def unavailable(s):
    return post(s, "/api/network/profile", {"unavailable": True})


def relay_cycle(s):
    """One refresh of the relay path as it runs every cycle, queued or not, in a match or not
    (hub/relaytransport.py refresh): the same profile again, then the first page of peers. Only
    used when the hub is NOT queued: a queued peer page is rotated by a 30 s wall-clock bucket."""
    region, version = LAST_PROFILE.get(s.me["steam_id"], ("NA", 0))
    status, body = profile(s, region, version)
    if status == 200:
        s.client._get("/api/network/peers?offset=0")
    return status, body


def measure(group):
    """Every pair signals once and reports a relay ping both ways (the WebView relay's traffic).
    Unanchored: nobody waits for quiet between these, exactly like the real relay."""
    ANCHOR_DIRECT[0] = False
    try:
        ids = [s.me["steam_id"] for s in group]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                attempt = hashlib.sha256((a + b).encode()).hexdigest()[:32]
                ping = 20 + i + j
                post(S[a], "/api/network/signal", {"revision": REV[a], "peer": b, "peer_revision": REV[b],
                                                   "attempt": attempt, "type": "offer", "data": SDP})
                post(S[b], "/api/network/signal", {"revision": REV[b], "peer": a, "peer_revision": REV[a],
                                                   "attempt": attempt, "type": "answer", "data": SDP})
                for x, y in ((a, b), (b, a)):
                    post(S[x], "/api/network/pings", {"revision": REV[x], "peers": [{
                        "steam_id": y, "revision": REV[y], "ping": ping, "samples": 5,
                        "attempt": attempt, "transport": TRANSPORT, "age_seconds": 0}]})
    finally:
        ANCHOR_DIRECT[0] = True


def game(token, path, body, timeout=15):
    """What the pak sends: the report credential, no ranked-mode header."""
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("authorization", "Bearer " + token)
    req.add_header("content-type", "application/json")
    label = GAME_LABEL.get(token, "game:?")
    outcome = _exchange(req, timeout, label, "game", anchor=False)
    return outcome[1] if outcome[0] == "ok" else (outcome[1].code if outcome[0] == "http" else 0)


def phases(group, *wanted):
    return lambda: all(s.phase in wanted for s in group)


def lobby_driver(group, extra=None):
    acted = {id(s): set() for s in group}

    def drive():
        if extra and extra():
            return True
        for s in group:
            if s.phase != "lobby":
                continue
            if s.stage == "coin" and s.i_am_coin_captain() and "coin" not in acted[id(s)]:
                acted[id(s)].add("coin")
                s.pick_coin("heads")
                return True
            if (s.stage == "choice" and s.toss_winner == s.my_team() and s.i_am_captain()
                    and "choice" not in acted[id(s)]):
                acted[id(s)].add("choice")
                s.choose("side")
                return True
            if (s.stage == "side" and s.side_picker == s.my_team() and s.i_am_captain()
                    and "side" not in acted[id(s)]):
                acted[id(s)].add("side")
                s.choose_side("attack")
                return True
            if s.stage == "veto" and s.ban_turn == s.my_team() and s.i_am_captain():
                key = "veto-%d" % len(s.bans)
                if len(s.remaining_maps()) > 1 and key not in acted[id(s)]:
                    acted[id(s)].add(key)
                    s.ban(s.remaining_maps()[0])
                    return True
        return False
    return drive


def connect_window(label, group, live_leave=False):
    """From the open window to live: the host's launch, a replayed host stream, the cleanup
    worker's poll, the travel permit, the host's report-in, a replayed joiner stream, every joiner's
    launch and connected, and the host game's start-ready."""
    hosts = [s for s in group if s._i_am_host()]
    if not check("%s: exactly one hub is host" % label, len(hosts) == 1, group):
        return False
    host = hosts[0]
    hid = host.me["steam_id"]
    joiners = [s for s in group if s is not host]
    check("%s: the host's hub launched Bodycam by itself" % label,
          any(row[0] == hid and row[1] == "host" for row in LAUNCHES), group)
    tokens = [row[4] for row in PREPARED if row[0] == hid and row[1] == "host" and len(row[4]) == 64]
    if not check("%s: the host pak carries a report credential" % label, bool(tokens)):
        return False
    token = tokens[-1]
    check("%s: host stream dropped and replayed mid-window" % label, drop_stream(hid))
    check("%s: host still connecting after the replay" % label, host.phase == "connecting", [host])
    # The relay worker keeps refreshing inside the connect window, host and joiners alike.
    settle()
    relay_cycle(host)
    settle()
    unavailable(joiners[-1])
    settle()
    relay_cycle(joiners[-1])
    settle()
    # The cleanup worker the hub registers beside the game polls this (hub/match_cleanup.py).
    status, _ = AUTH._request("/api/match/completion?" + urllib.parse.urlencode({"id": host.match_id}),
                              token=host.token, timeout=10)
    note("%s: cleanup worker completion poll answered %s" % (label, status), status in (200, 409))
    settle()
    started = time.monotonic()
    status = game(token, "/api/probe/slow", {
        "ip": "", "event_name": "ch_host_wait", "user_id": "", "storefront": "host", "platform": "",
        "timestamp": "", "first_session_timestamp": "chlobby-28", "is_first_game_open": False}, 10)
    took = time.monotonic() - started
    note("%s: the host's travel permit answered %s" % (label, status), status == 200 and took < 4.0)
    settle()
    status = game(token, "/api/match-report", {
        "event_name": "ch_lobby_read", "user_id": hid, "storefront": "", "platform": "",
        "timestamp": "", "first_session_timestamp": "", "is_first_game_open": False})
    note("%s: the host reported in from the match world (%s)" % (label, status), status == 200)
    within("%s: every joiner released (host_ready)" % label,
           lambda: all(s.host_ready for s in joiners), 15, sessions=joiners)
    check("%s: joiner stream dropped and replayed" % label, drop_stream(joiners[0].me["steam_id"]))
    for s in joiners:
        s.launch_game()
    within("%s: every joiner launched and reported connected" % label,
           lambda: all(s.i_connected for s in joiners) and not WIRE.pending, 30, sessions=joiners)
    settle()
    rows = []
    for s in group:
        team = 0 if any(p.get("steam_id") == s.me["steam_id"] for p in host.teams.get(1, [])) else 1
        rows.append("%s:%d:1" % (s.me["steam_id"], team))
    status = game(token, "/api/match-report/start-ready", {
        "event_name": "ch_start_ready", "storefront": "chm-" + host.match_id,
        "platform": "%d|%s;" % (len(rows), ";".join(rows)), "user_id": hid,
        "timestamp": "", "ip": "", "first_session_timestamp": "", "is_first_game_open": False})
    note("%s: start-ready accepted (%s)" % (label, status), status == 200)
    ok = within("%s: the match went live on every hub" % label, phases(group, "live"), 20, sessions=group)
    # A few seconds of the live phase: the recovery status polls (compared as a set).
    end = time.monotonic() + 7
    while time.monotonic() < end:
        step()
    settle()
    check("%s: a live stream dropped and replayed" % label, drop_stream(joiners[-1].me["steam_id"]))
    if live_leave:
        leaver = joiners[-1]
        leaver.leave_everything()                   # the hub's own "leave" path: /api/match/leave
        settle()
        note("%s: a player left the live match" % label, True)
    return ok


# ---------------------------------------------------------------- the day
def day_party():
    print("\n--- party", flush=True)
    a1, a2, a3 = (S[x] for x in PARTY_A)
    b1, b2 = (S[x] for x in PARTY_B)
    s6, s7 = S[SOLOS[0]], S[SOLOS[1]]
    for asker, answerer in ((a1, a2), (a1, s6), (b1, b2)):
        asker.add_friend(answerer.me["steam_id"])
        settle()
        answerer.accept_friend(asker.me["steam_id"])
        settle()
    check("party: friendships made", a2.me["steam_id"] in {str(f.get("steam_id") or f.get("player_id"))
                                                            for f in (a1.friends or ())})
    a1.create_party()
    within("party: A created", lambda: bool(a1.party and a1.party.get("code")), 10, sessions=[a1])
    a1.invite_to_party(a2.me["steam_id"])
    within("party: invite reached its target", lambda: bool(a2.party_invites), 10, sessions=[a2])
    a2.accept_party_invite(a1.me["steam_id"])
    within("party: invite accepted", lambda: len((a1.party or {}).get("members", [])) == 2, 10, sessions=[a1, a2])
    a3.join_party(a1.party["code"])
    within("party: joined by code", lambda: len((a1.party or {}).get("members", [])) == 3, 10, sessions=[a1, a3])
    s7.join_party("AAAA-AA")
    settle()
    check("party: a code nobody holds is refused", not s7.party and bool(s7.party_error), [s7])
    old = a1.party["code"]
    a1.refresh_party_code()
    within("party: code refreshed", lambda: all((s.party or {}).get("code") not in (None, old)
                                                for s in (a1, a2, a3)), 10, sessions=[a1, a2, a3])
    a1.invite_to_party(s6.me["steam_id"])
    within("party: second invite delivered", lambda: bool(s6.party_invites), 10, sessions=[s6])
    s6.decline_party_invite(a1.me["steam_id"])
    within("party: invite declined", lambda: not s6.party_invites, 10, sessions=[s6])
    b1.invite_friend_to_party(b2.me["steam_id"])
    within("party: B created by an invite", lambda: bool(b2.party_invites) and bool(b1.party), 10, sessions=[b1, b2])
    b2.accept_party_invite(b1.me["steam_id"])
    within("party: B formed", lambda: len((b1.party or {}).get("members", [])) == 2, 10, sessions=[b1, b2])
    b2.leave_party()
    within("party: member left", lambda: not b2.party and len((b1.party or {}).get("members", [])) == 1,
           10, sessions=[b1, b2])
    b2.join_party(b1.party["code"])
    within("party: member rejoined", lambda: len((b1.party or {}).get("members", [])) == 2, 10, sessions=[b1, b2])
    check("party: a party member's stream dropped and the roster replayed", drop_stream(a2.me["steam_id"]))
    check("party: a solo's stream dropped and replayed", drop_stream(s7.me["steam_id"]))


def day_queue(server_dir):
    print("\n--- queue", flush=True)
    a1, a2, a3 = (S[x] for x in PARTY_A)
    b1, b2 = (S[x] for x in PARTY_B)
    s6 = S[SOLOS[0]]
    old = S[OLD]
    for x in FIVE + [OLD]:
        settle()
        profile(S[x])
    settle()
    old.find_match()
    within("queue: a 3.0.3 hub is refused at the queue", lambda: old.phase == "idle" and bool(old.error), 10, sessions=[old])
    a1.find_match()
    within("queue: party A searching", phases([a1, a2, a3], "queued"), 10, sessions=[a1, a2, a3])
    a2.client.network_changed()
    profile(a2, region="EU")
    within("queue: a member's region change ended the search", phases([a1, a2, a3], "idle"), 10, sessions=[a1, a2, a3])
    profile(a2, region="NA")
    settle()
    a1.find_match()
    within("queue: party A searching again", phases([a1, a2, a3], "queued"), 10, sessions=[a1, a2, a3])
    unavailable(a3)
    within("queue: {unavailable:true} ended the search", phases([a1, a2, a3], "idle"), 10, sessions=[a1, a2, a3])
    profile(a3)
    settle()
    a1.find_match()
    within("queue: party A searching a third time", phases([a1, a2, a3], "queued"), 10, sessions=[a1, a2, a3])
    a3.leave_party()
    within("queue: a member leaving the party ended the search",
           lambda: phases([a1, a2, a3], "idle")() and not a3.party, 10, sessions=[a1, a2, a3])
    a3.join_party(a1.party["code"])
    within("queue: and rejoined", lambda: len((a1.party or {}).get("members", [])) == 3, 10, sessions=[a1, a3])
    b1.find_match()
    within("queue: party B searching", phases([b1, b2], "queued"), 10, sessions=[b1, b2])
    b2.cancel_queue()
    within("queue: a member left the search", phases([b1, b2], "idle"), 10, sessions=[b1, b2])
    s6.find_match()
    within("queue: a solo searching", phases([s6], "queued"), 10, sessions=[s6])
    s6.cancel_queue()
    within("queue: the solo left", phases([s6], "idle"), 10, sessions=[s6])


def day_tab():
    """A party member with the 1v1 tab open while the leader searches BB5. The hub stamps every
    request with its selected ladder (hub/live.py _stamp), so this member's relay refreshes go to
    BB1; its tab ignores the BB5 'queued' (competitive.py on_live_event), so it stays idle there.
    2d25405 looked for a search to end on BB1 only, and the leader's BB5 search went on."""
    print("\n--- a member on the 1v1 tab", flush=True)
    b1, b2 = (S[x] for x in PARTY_B)
    b2.select_ranked_mode("BB1")
    settle()
    b1.find_match()
    within("tab: party B searching BB5, one member on the 1v1 tab", phases([b1], "queued"), 10,
           sessions=[b1, b2])
    profile(b2, version=3)                                # a new relay location, through BB1
    settle()
    unavailable(b2)                                       # and {unavailable:true}, through BB1
    settle()
    profile(b2)
    settle()
    check("tab: the member's posts through BB1 left the BB5 search alone", b1.phase == "queued",
          [b1, b2])
    b1.cancel_queue()
    within("tab: the leader left the search", phases([b1], "idle"), 10, sessions=[b1, b2])
    b2.select_ranked_mode("BB5")
    settle()


def day_five(server_dir):
    print("\n--- 5v5 BB5", flush=True)
    group = [S[x] for x in FIVE]
    for s in group:
        settle()
        profile(s)                                        # fresh inside the 120 s profile TTL
    for x in [PARTY_A[0], PARTY_B[0]] + SOLOS:
        S[x].find_match()
        settle()
    if not within("5v5: all ten searching", phases(group, "queued"), 15, sessions=group):
        return False
    # A searcher's stream drops. The hub's own rule (competitive.py _on_live_status) takes a
    # queued hub back to idle when its stream fails, so the player presses Find match again.
    check("5v5: a searching solo's stream dropped and replayed", drop_stream(SOLOS[2]))
    if S[SOLOS[2]].phase == "idle":
        S[SOLOS[2]].find_match()
    within("5v5: all ten searching after the replay", phases(group, "queued"), 15, sessions=group)
    mover = S[SOLOS[1]]
    profile(mover, version=1)                             # a new relay location: a new revision
    within("5v5: a changed relay location ended one search", phases([mover], "idle"), 10, sessions=[mover])
    mover.find_match()
    within("5v5: and it searched again", phases(group, "queued"), 10, sessions=group)
    measure(group)
    if not within("5v5: one match found for all ten", phases(group, "found"), 30, sessions=group):
        return False
    for s in group:
        s.accept()
    if not within("5v5: all ten accepted into the lobby", phases(group, "lobby"), 30, sessions=group):
        return False
    S[FIVE[0]].send_chat("gl hf", "all")
    S[FIVE[1]].send_chat("mid first", "team")
    settle()
    a2, a3 = S[PARTY_A[1]], S[PARTY_A[2]]
    done = []

    def mid_lobby():
        if done or not any(s.stage == "veto" and s.bans for s in group):
            return False
        done.append(1)
        profile(a2, version=2)                        # revision change in a match: nothing to end
        settle()
        unavailable(a3)                               # in a match: answered, nothing ends
        settle()
        profile(a3)
        settle()
        a2.client.network_changed()
        status, body = profile(a2, region="EU", version=2)
        note("5v5: region change refused mid-match (%s)" % status, status == 409)
        settle()
        a3.leave_party()                              # refused: "Finish your match first"
        settle()
        check("5v5: a lobby stream dropped and replayed", drop_stream(SOLOS[3]))
        return True
    if not within("5v5: coin, choice, side and the veto opened the connect window",
                  phases(group, "connecting"), 150, drive=lobby_driver(group, mid_lobby), sessions=group):
        return False
    check("5v5: the mid-lobby network posts ran", bool(done))
    return connect_window("5v5", group, live_leave=True)


def day_duel():
    print("\n--- 1v1 BB1", flush=True)
    group = [S[x] for x in DUEL]
    d1, d2 = group
    for s in group:
        settle()
        profile(s)
    d1.find_match()
    within("1v1: searching", phases([d1], "queued"), 10, sessions=[d1])
    unavailable(d1)
    within("1v1: {unavailable:true} ended the search", phases([d1], "idle"), 10, sessions=[d1])
    profile(d1)
    settle()
    d1.find_match()
    within("1v1: searching again", phases([d1], "queued"), 10, sessions=[d1])
    d1.client.network_changed()
    profile(d1, region="EU")
    within("1v1: a region change ended the search", phases([d1], "idle"), 10, sessions=[d1])
    profile(d1, region="NA")
    settle()
    d1.find_match()
    settle()
    d2.find_match()
    if not within("1v1: both searching", phases(group, "queued", "found"), 10, sessions=group):
        return False
    measure(group)
    if not within("1v1: match found", phases(group, "found"), 30, sessions=group):
        return False
    for s in group:
        s.accept()
    if not within("1v1: both in the lobby", phases(group, "lobby"), 20, sessions=group):
        return False
    done = []

    def mid_lobby():
        if done:
            return False
        done.append(1)
        d2.client.network_changed()
        status, _ = profile(d2, region="EU")
        note("1v1: region change refused mid-match (%s)" % status, status == 409)
        settle()
        unavailable(d2)
        settle()
        profile(d2)
        settle()
        return True
    if not within("1v1: coin and side opened the connect window", phases(group, "connecting"), 90,
                  drive=lobby_driver(group, mid_lobby), sessions=group):
        return False
    return connect_window("1v1", group)


def day_decline():
    print("\n--- a declined accept (BB1)", flush=True)
    group = [S[x] for x in DECLINE]
    x1, x2 = group
    for s in group:
        settle()
        profile(s)
    x1.find_match()
    settle()
    x2.find_match()
    within("decline: both searching", phases(group, "queued", "found"), 10, sessions=group)
    measure(group)
    if not within("decline: match found", phases(group, "found"), 30, sessions=group):
        return
    x1.accept()
    settle()
    within("decline: the accept window ran out", lambda: all(s.phase != "found" for s in group),
           ACCEPT_SECONDS + 20, sessions=group)
    settle()
    if x1.phase == "queued":
        x1.cancel_queue()
        within("decline: the innocent player left the requeue", phases([x1], "idle"), 10, sessions=[x1])


def day_resend():
    print("\n--- a party invite sent twice (BB1 hubs, BB5 parties)", flush=True)
    x1, x2 = (S[x] for x in DECLINE)
    x1.add_friend(x2.me["steam_id"])
    settle()
    x2.accept_friend(x1.me["steam_id"])
    settle()
    x1.invite_friend_to_party(x2.me["steam_id"])
    within("resend: first invite delivered", lambda: bool(x2.party_invites), 10, sessions=[x2])
    x1.invite_to_party(x2.me["steam_id"])
    settle()
    x2.accept_party_invite(x1.me["steam_id"])
    settle()
    # Recorded, not required: this is exactly where the two servers are expected to disagree.
    note("resend: the invitee answered the invite", True, "in the party afterwards: %s" % bool(x2.party))


def drive_main(args):
    global WIRE, C, LV, T, AUTH, BASE
    server_dir = pathlib.Path(args.server_dir).resolve()
    BASE = os.environ["HUB_API_BASE"].rstrip("/")
    sys.path.insert(0, str(HUB303))
    from hub import version as V
    V.HUB_VERSION = REPORTED_HUB_VERSION                  # before live/competitive read it
    from hub import competitive, i18n, live, transport, auth
    C, LV, T, AUTH = competitive, live, transport, auth
    i18n.set_language("en")
    WIRE = Wire(EVERYONE)
    instrument()
    print("driving %s (hub %s from %s)" % (BASE, REPORTED_HUB_VERSION, HUB303), flush=True)
    try:
        for x in FIVE:
            make(x, server_dir)
        make(OLD, server_dir, hub_version="3.0.3")
        for x in DUEL + DECLINE:
            make(x, server_dir, mode="BB1")
        for x in EVERYONE:
            check("stream open %s" % x[-3:], open_stream(x))
        day_party()
        day_queue(server_dir)
        if "tab" not in args.skip:
            day_tab()
        day_five(server_dir)
        day_duel()
        day_decline()
        if "resend" not in args.skip:
            day_resend()
        settle()
    except Exception:  # noqa: BLE001
        ERRORS.append("driver: " + traceback.format_exc())
        print(ERRORS[-1], flush=True)
    with WIRE.cv:
        data = {"streams": {k: list(v) for k, v in WIRE.streams.items()},
                "http": {k: list(v) for k, v in WIRE.http.items()},
                "launches": LAUNCHES, "prepared": PREPARED, "steps": STEPS, "errors": ERRORS}
    pathlib.Path(args.out).write_text(json.dumps(data, indent=0), "utf-8")
    print("recorded %d stream lines and %d HTTP exchanges; %d steps failed; %d errors" % (
        sum(len(v) for v in data["streams"].values()), sum(len(v) for v in data["http"].values()),
        sum(1 for s in STEPS if not s["ok"]), len(ERRORS)), flush=True)
    os._exit(0)                                           # daemon readers still hold their sockets


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ref", default=REF_SERVER, help="reference server dir (default: --ref-commit)")
    parser.add_argument("--ref-commit", default=REF_COMMIT,
                        help="unpack the reference server from this commit (default %s)" % REF_COMMIT)
    parser.add_argument("--gate", default=str(GATE_SERVER), help="server dir under test")
    parser.add_argument("--self-check", action="store_true", help="also run the reference twice")
    parser.add_argument("--only-ref", action="store_true", help="run only the reference (harness work)")
    parser.add_argument("--skip", action="append", default=[], help="leave a section out: resend, tab")
    parser.add_argument("--keep", help="write recordings and logs here")
    parser.add_argument("--recompare", action="store_true",
                        help="only re-compare the recordings already in --keep")
    parser.add_argument("--upstash", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--drive", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--server-dir", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.upstash:
        upstash_main(args.upstash)
        return 0
    if args.drive:
        return drive_main(args)
    return orchestrate(args)


if __name__ == "__main__":
    sys.exit(main())
