#!/usr/bin/env python3.12
"""Coming back to a match, over the REAL wire.  Run:  python3.12 tests/test_wire_rejoin.py

Sam, 2026-09-14: "make it so if there is an ongoing competitive match the user can update and go
back to the match. also make it so if the user closes out the hub they can re open it and join
back or if their internet disconnects, etc".

Closing the hub, updating it and losing the network are the same event as far as the service is
concerned: the stream goes, and a new one comes. So this test does the harshest version of all
three - it throws the whole LiveSession away and builds a brand new one on the same token, which
is exactly what the player gets after the installer restarts the hub - and proves the new one
lands back where the old one was.

test_wire.py proves the forward flow agrees about the wire; this proves the REPLAY does, which is
a different set of field names (`resumed`, `teams`, `sides`, `bans`, `live_seconds`) that no unit
test on either side can check on its own.

Needs `node` on PATH. The server runs on a spare port with very short clocks.
"""
import heapq
import json
import urllib.request
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import tempfile

PORT = 8933
os.environ["HUB_API_BASE"] = "http://127.0.0.1:%d" % PORT
os.environ["HUB_STATE_DIR"] = tempfile.mkdtemp(prefix="hub-wire-rejoin-")
REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

env = dict(os.environ,
           PORT=str(PORT), NODE_ENV="test",
           COMP_MATCH_SIZE="2", COMP_ACCEPT_SECONDS="20",
           COMP_NETWORK_TEST_BYPASS="1",
           COMP_TEAMS_GATE_SECONDS="0",  # the explicit host snapshot below opens the gate
           COMP_LOBBY_SECONDS="120", COMP_CONNECT_SECONDS="60",
           # long enough that the match is still being played when the "new hub" arrives
           COMP_LIVE_SECONDS="120",
           HUB_TEST_TOKENS="tok-a=76561198000000001,tok-b=76561198000000002")
srv = subprocess.Popen(["node", "server.cjs"], cwd=str(REPO / "server"), env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        break
    except OSError:
        time.sleep(0.1)
else:
    print("server never came up:", srv.stderr.read().decode()[:400])
    sys.exit(1)

from hub import competitive as C, i18n, game as game_mod     # noqa: E402

# This is a real HTTP/SSE lifecycle test, with game and relay hardware simulated.
# Never open Steam, install a pak, or close the developer's running game.
GAME = {"running": False, "launches": 0}
def launch_fake():
    GAME["launches"] += 1
    return True
game_mod.launch_game = launch_fake
game_mod.game_running = lambda: GAME["running"]
game_mod.close_game = lambda **kw: "closed"

i18n.set_language("en")

A_ID, B_ID = "76561198000000001", "76561198000000002"


class Panel:
    """A minimal main loop: after() schedules, post() hops threads, tick() drains both."""

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


PANELS = []


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
    """One hub. Building a SECOND one on the same token is what a restart looks like."""
    p = Panel(name)
    p.app = current_app()
    s = C.LiveSession(p)
    s._network_config = None  # relay behavior is covered by the network/probe suites
    s._prepare_host_pak = lambda: True
    s._prepare_joiner_pak = lambda: True
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
        srv.terminate()
        sys.exit(1)
    print("  ok:", what)


def close_hub(session, panel):
    """The hub goes away: the stream is dropped and the object is thrown out. Whatever the
    session knew about the match dies with it, which is the whole point."""
    session._disconnect()
    if panel in PANELS:
        PANELS.remove(panel)


def drive_to_connecting(sessions):
    """Drive the SERVER-authoritative lobby to the connect window: one designated captain flips,
    the winner chooses side, the side picker picks attack/defend, the veto alternates - each an
    action POSTed and applied back from the server's broadcast, exactly as test_wire.py does."""
    acted = {id(s): set() for s in sessions}
    for _ in range(1200):
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
    # ---------------------------------------------------------------- a match, played
    A, pa = make("Sam", "tok-a", A_ID)
    B, pb = make("Friend", "tok-b", B_ID)
    STATE["A"] = A
    STATE["B"] = B
    need(pump(10, lambda: A.connected and B.connected), "both streams connected")

    A.find_match()
    B.find_match()
    need(pump(20, lambda: A.phase == "found" and B.phase == "found"), "both matched")
    A.accept()
    B.accept()
    need(pump(10, lambda: A.phase == "lobby" and B.phase == "lobby"), "both reached the lobby")

    # ------------------------------------------- a hub that dies IN THE LOBBY is handed it back
    # The lobby lives on the SERVER now, so a brand new hub drops back into the REAL stage - the
    # teams, the designated captain, whatever has been decided - rather than waiting blind.
    close_hub(A, pa)
    A2, pa2 = make("Sam", "tok-a", A_ID)
    STATE["A"] = A2
    need(pump(12, lambda: A2.phase == "lobby"), "the new hub is told it is still in a match")
    need(A2.stage in ("coin", "flipping", "choice", "side", "veto", "ready"),
         "the server replays the real lobby stage, not a blind wait (stage=%r)" % A2.stage)
    need(A2.coin_captain and A2.coin_captain == B.coin_captain,
         "and both hubs agree who the ONE designated captain is")
    need(A2.locked_in(), "the rejoin is a match: sign-out stays hidden")
    need(len(A2.players) == 2, "the roster came back with it")
    A, pa = A2, pa2                 # carry the recovered hub on under the old name

    # the restored lobby runs to the end over the wire with both live clients
    need(drive_to_connecting([A, B]), "the veto ends in the connect window")
    played_map = A.map
    need(played_map and played_map == B.map, "both agree on the map (%r)" % played_map)

    # ------------------------------------------- a hub that dies IN THE CONNECT WINDOW
    close_hub(A, pa)
    A2, pa2 = make("Sam", "tok-a", A_ID)
    STATE["A"] = A2
    need(pump(12, lambda: A2.phase == "connecting"), "the new hub lands in the connect window")
    need(A2.map == played_map, "with the map the veto settled on (%r)" % A2.map)
    need(A2.connect_left > 0, "and what is LEFT of the clock, not the whole window again")
    host_id = (A2.host or {}).get("steam_id")
    need(host_id in (A_ID, B_ID), "and a host it can actually name (%r)" % (A2.host or {}).get("name"))
    need(A2.i_connected is False, "it has not reported in yet, and knows it")

    # ------------------------------------------- everyone connects: the match goes live
    A2.report_connected()
    B.report_connected()
    rows = [str(p['steam_id']) + ':' + str(side - 1) + ':1;' for side in (1, 2) for p in B.teams[side]]
    request = urllib.request.Request(os.environ['HUB_API_BASE'] + '/api/match-report/start-ready',
        data=json.dumps({'event_name': 'ch_start_ready', 'user_id': host_id,
                         'storefront': 'chm-' + A2.match_id,
                         'platform': str(len(rows)) + '|' + ''.join(rows)}).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (A2.report_token if host_id == A_ID else B.report_token)})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            need(response.status == 200, 'the game confirms the complete current roster')
    except urllib.error.HTTPError as error:
        raise AssertionError('start snapshot rejected: ' + error.read().decode('utf-8') + ' payload=' + request.data.decode('utf-8')) from error
    need(pump(15, lambda: A2.phase == "live" and B.phase == "live"), "the match went live")

    # ------------------------------------------- and a hub that dies MID-MATCH comes back to it
    active_match = A2.match_id
    GAME["running"] = True
    launches = GAME["launches"]
    close_hub(A2, pa2)
    A3, pa3 = make("Sam", "tok-a", A_ID)
    STATE["A"] = A3
    need(pump(12, lambda: A3.phase == "live"),
         "a brand new hub walks straight back into the live match")
    need(A3.map == played_map, "the map came off the server (%r)" % A3.map)
    need(len(A3.players) == 2, "so did the roster")
    need((A3.host or {}).get("steam_id") in (A_ID, B_ID), "so did the host")
    need(sorted(len(v) for v in A3.teams.values()) == [1, 1], "so did the teams (%r)" % A3.teams)
    need(A3.my_team() in (1, 2), "and this player is on one of them")
    need(A3.sides[1] in ("attack", "defend") and A3.sides[2] in ("attack", "defend"),
         "so did the sides (%r)" % A3.sides)
    need(len(A3.bans) == len(C.competitive_pool(C.DEFAULT_MAPS)) - 1,
         "so did every ban of the veto (%d)" % len(A3.bans))
    need(A3.i_connected and A3.i_accepted, "a live match means we accepted and connected")
    need(A3.locked_in() and not A3.error, "and it is a clean match screen, not an error")
    need(A3.match_id == active_match, "the same match ID survives a complete hub restart")
    need(A3._game_ours and A3.host_ready, "the running game is adopted for end-of-match cleanup")
    need(GAME["launches"] == launches, "reopening never launches another game")

    # ------------------------------------------- the panel is told to put it in front
    need(A3.rejoined is True, "the panel is asked to surface the match it just got back")

    print("\nWIRE REJOIN TEST PASSED")
finally:
    srv.terminate()
    try:
        srv.wait(5)
    except Exception:       # noqa: BLE001
        srv.kill()
