#!/usr/bin/env python3.12
"""Two REAL LiveSessions against a REAL server over HTTP + SSE.  Run:  python3.12 tests/test_wire.py

The unit tests prove each side's decisions; this proves the two sides agree about the WIRE. Every
field name in `server/live.cjs` has a reader in `hub/competitive.py`, and a typo in either is
invisible to both test suites and obvious here. It walks a whole two-player match: presence, queue,
accept, the SERVER-AUTHORITATIVE lobby (one captain flips, the server decides the toss, the
side/ban choice and the alternating veto all run server-side), the connect window, one player not
turning up, the penalty, the refusal to queue while banned, and the ban expiring.

Needs `node` on PATH and `/usr/bin/python3.12`. The server is started on a spare port with very
short clocks (`COMP_*` env vars) so the whole thing runs in about half a minute.
"""
import heapq
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time

PORT = 8931
os.environ["HUB_API_BASE"] = "http://127.0.0.1:%d" % PORT
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

env = dict(os.environ,
           PORT=str(PORT), NODE_ENV="test",
           COMP_MATCH_SIZE="2", COMP_ACCEPT_SECONDS="10",
           COMP_LOBBY_SECONDS="120", COMP_CONNECT_SECONDS="8",
           COMP_NO_SHOW_BAN_SECONDS="6", COMP_NO_SHOW_ELO="25",
           HUB_TEST_TOKENS="tok-a=76561198000000001,tok-b=76561198000000002")
srv = subprocess.Popen(["node", "server.cjs"], cwd=str(pathlib.Path(__file__).resolve().parent.parent / "server"), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close(); break
    except OSError:
        time.sleep(0.1)
else:
    print("server never came up:", srv.stderr.read().decode()[:400]); sys.exit(1)

from hub import competitive as C, i18n
i18n.set_language("en")

class Panel:
    """A minimal main loop: after() schedules, post() hops threads, run() drains both."""
    def __init__(self, name):
        self.name = name
        self.timers = []
        self.inbox = []
        self.lock = threading.Lock()
        self.seq = 0
    def after(self, ms, fn):
        with self.lock:
            self.seq += 1
            heapq.heappush(self.timers, (time.monotonic() + ms / 1000.0, self.seq, fn))
    def post(self, fn):
        with self.lock:
            self.inbox.append(fn)
    def on_change(self): pass
    def map_pool(self): return C.competitive_pool(C.DEFAULT_MAPS)
    def save_auth(self, payload): pass
    def tick(self):
        with self.lock:
            due, self.inbox = self.inbox, []
            now = time.monotonic()
            while self.timers and self.timers[0][0] <= now:
                due.append(heapq.heappop(self.timers)[2])
        for fn in due:
            fn()

def current_app():
    """The `app` stand-in a LiveSession reads, holding the RANKED PACK AT THE VERSION THIS SERVER
    IS PUBLISHING.

    The service will not queue a hub that is behind its catalogue (server/live.cjs, the version
    gate), and the hub reports what it has installed. A harness that claimed nothing would be
    refused as an ancient build - correctly - and would never reach the wire it exists to test.
    So it reads the same catalogue the server it just started is serving, which also means this
    file needs no edit the next time the pack is published.

    `catalogue` is None on purpose: the hub's own client-side check stays out of the way, so what
    this file proves is the SERVER's answer rather than a local guess about it.
    """
    import json
    root = pathlib.Path(__file__).resolve().parent.parent
    catalogue = json.loads((root / "server" / "public" / "catalogue.json").read_text("utf-8"))
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
    # These simulated players have no game; the developer may be playing while tests run.
    s.game_is_open = lambda: False
    s.adopt_account({"steam_id": steam_id, "persona": name, "token": token})
    s.phase = "idle"
    s._start_watchdog()
    return s, p

A, pa = make("Sam", "tok-a", "76561198000000001")
B, pb = make("Friend", "tok-b", "76561198000000002")

def pump(seconds, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pa.tick(); pb.tick()
        if until and until():
            return True
        time.sleep(0.02)
    return bool(until and until())

def need(cond, what):
    if not cond:
        print("  FAIL:", what, "| A:", A.phase, A.stage, A.error, "| B:", B.phase, B.stage, B.error)
        srv.terminate(); sys.exit(1)
    print("  ok:", what)

try:
    need(pump(8, lambda: A.connected and B.connected), "both streams connected")
    need(pump(4, lambda: A.online >= 2), "presence sees both (online=%d)" % A.online)

    A.find_match(); B.find_match()
    need(pump(15, lambda: A.phase == "found" and B.phase == "found"), "both matched at 2 players")
    need(A.accept_total == 2 and B.accept_total == 2, "accept screen knows it is 2, not 10")

    A.accept(); B.accept()
    need(pump(10, lambda: A.phase == "lobby" and B.phase == "lobby"), "both reached the lobby")
    need(A.locked_in() and B.locked_in(), "sign-out is locked for both")
    before = A.token
    A.sign_out()
    need(A.token == before and A.phase == "lobby", "sign_out refuses mid-match")

    # The SERVER runs the lobby now: one designated captain flips, the server decides the winner
    # once, the winner takes side/ban, the side picker chooses, and the veto alternates. Every
    # decision goes up as a POST and comes back to BOTH clients as a `lobby` event, so they agree.
    need(A.i_am_coin_captain() != B.i_am_coin_captain(), "exactly one designated coin captain")
    need(A.coin_captain == B.coin_captain, "both clients agree who the coin captain is")
    acted = {id(A): set(), id(B): set()}

    def drive():
        for s in (A, B):
            if s.stage == "coin" and s.i_am_coin_captain() and "coin" not in acted[id(s)]:
                s.pick_coin("heads"); acted[id(s)].add("coin")
            elif s.stage == "choice" and s.toss_winner == s.my_team() and s.i_am_captain() \
                    and "choice" not in acted[id(s)]:
                s.choose("side"); acted[id(s)].add("choice")
            elif s.stage == "side" and s.side_picker == s.my_team() and s.i_am_captain() \
                    and "side" not in acted[id(s)]:
                s.choose_side("attack"); acted[id(s)].add("side")
            elif s.stage == "veto" and s.ban_turn == s.my_team() and s.i_am_captain():
                key = "veto-%d" % len(s.bans)
                if len(s.remaining_maps()) > 1 and key not in acted[id(s)]:
                    s.ban(s.remaining_maps()[0]); acted[id(s)].add(key)

    for _ in range(800):
        pa.tick(); pb.tick()
        drive()
        if A.phase == "connecting" and B.phase == "connecting":
            break
        time.sleep(0.02)
    need(A.phase == "connecting" and B.phase == "connecting", "the veto ends in the connect window")
    need(A.map == B.map, "the server made both clients agree on the map (%s / %s)" % (A.map, B.map))
    need(A.toss_winner == B.toss_winner, "both clients saw the SAME server-decided toss winner")
    need(A.sides.get(1) == B.sides.get(1) and A.sides.get(1) in ("attack", "defend"),
         "both clients agree on the sides the server assigned (%s / %s)" % (A.sides, B.sides))
    need(A.connect_left > 0 and A.connect_total == 2, "the server handed over its clock")
    need(A.penalty_next == 6, "each client is warned with its own next rung (%s)" % A.penalty_next)

    A.report_connected()
    need(pump(5, lambda: len(B.connected_ids) == 1), "B sees A load in")
    need(pump(20, lambda: A.phase != "connecting" and B.phase != "connecting"), "the window closed")

    need(A.banned_left() == 0, "A turned up and lost NOTHING")
    need(A.phase == "queued", "A was put back in the queue")
    need("never loaded in" in A.error, "A is told why: %r" % A.error)
    need(0 < B.banned_left() <= 6, "B is banned (%ds)" % B.banned_left())
    need("25" in B.error, "B is told the Elo cost: %r" % B.error)
    need(B.phase == "idle", "B is not requeued")

    B.error = ""
    B.find_match()
    need(pump(8, lambda: bool(B.error)), "a banned player is refused: %r" % B.error)
    need(B.phase == "idle", "and stays on idle rather than a dead screen")

    A.cancel_queue()
    need(pump(6, lambda: B.banned_left() == 0), "the ban serves itself out")

    # ---------------------------------------------------------------- match history
    # The match that just died is the FIRST real history row either of them has, and the two
    # of them must read the same match differently: B no-showed, A did not.
    for s in (A, B):
        s.load_history(force=True)
    need(pump(10, lambda: A.history is not None and B.history is not None),
         "both clients got a history back")
    need(len(A.history) == 1 and len(B.history) == 1,
         "one match each (A=%d B=%d)" % (len(A.history or []), len(B.history or [])))

    a_row, b_row = A.history[0], B.history[0]
    need(a_row["id"] == b_row["id"], "it is the same match on both sides")
    need(a_row["outcome"] == "cancelled" and a_row["reason"] == "no_show",
         "recorded as a no-show cancellation (%s/%s)" % (a_row["outcome"], a_row["reason"]))
    need(a_row["blamed"] is False and b_row["blamed"] is True,
         "blame lands on B alone (A=%s B=%s)" % (a_row["blamed"], b_row["blamed"]))
    need(b_row["elo"] == -25 and a_row["elo"] in (None, 0),
         "the Elo debt is recorded against B only (A=%r B=%r)" % (a_row["elo"], b_row["elo"]))
    need(a_row["connected"] is True and b_row["connected"] is False,
         "who actually loaded in is on the record")
    # A.map is already cleared by reset_match, which is exactly why the row has to carry it
    need(a_row["map"] in C.competitive_pool(C.DEFAULT_MAPS),
         "the vetoed map survived into the record (%r)" % a_row["map"])
    need(a_row["team"] in (1, 2) and a_row["side"] in ("attack", "defend"),
         "the lobby's teams and sides reached the server (team=%r side=%r)"
         % (a_row["team"], a_row["side"]))
    need(a_row["won"] is None and a_row["score"] is None,
         "the score is honestly absent, not invented")

    # ...and the detail the pop-up asks for
    got = []
    A.load_match(a_row["id"], got.append)
    need(pump(8, lambda: bool(got)), "the detail fetch came back")
    full = got[0]
    need(full is not None, "A may read a match A was in")
    need(len(full["players"]) == 2, "both players are on the record")
    need(full["source"] == "server",
         "the lobby ran on the server, so teams/sides/bans are server-authoritative")
    need(len(full["bans"]) == len(C.competitive_pool(C.DEFAULT_MAPS)) - 1,
         "every ban of the veto is stored (%d)" % len(full["bans"]))
    blamed = [p for p in full["players"] if p["blamed"]]
    need(len(blamed) == 1 and blamed[0]["steam_id"] == "76561198000000002",
         "the detail names B as the one at fault")

    # a match id that is not ours is a 404, not somebody else's match
    got2 = []
    A.load_match("deadbeefdeadbeef", got2.append)
    need(pump(8, lambda: bool(got2)), "the bad-id fetch came back")
    need(got2[0] is None, "a match we were not in is refused")

    print("\nWIRE TEST PASSED")
finally:
    srv.terminate()
    try: srv.wait(5)
    except Exception: srv.kill()
