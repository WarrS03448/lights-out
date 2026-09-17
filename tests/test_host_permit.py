#!/usr/bin/env python3.12
"""Auto-host is gated: only a real match's host gets the travel clock.
Run:  python3.12 tests/test_host_permit.py   (needs `node` on PATH)

THE BUG THIS EXISTS FOR. GM_CHLobby's BeginPlay runs on EVERY load of the lobby world, and until now
it travelled unconditionally. Two consequences, both of which Sam hit on 2026-09-15:

  - starting the game normally hosted him into the match map, with no match anywhere;
  - LEAVING that match loaded the lobby world, which fired BeginPlay again, which hosted him
    straight back in. There was no way out except uninstalling the pak.

Deleting the pak mid-session does not help either: paks are mounted at process start, so a running
game keeps the copy it mounted no matter what happens to the file.

THE GATE. The pak has no clock of its own - no timer, no Delay, no Tick - so it travels when
/api/probe/slow answers, and that makes the ANSWER the permission. The server grants a permit
when a match opens its connect window, to that match's host. It survives retries until arrival
in the match world revokes it, so a dropped response cannot strand the host.
Everything else is held and dropped: the game's HTTP client abandons a request it gets no reply to
and never fires the response delegate, so the pak sits there and the player has a stock game.

No pak rebuild was needed for any of this, which is the point - the gate is entirely server-side.

Identity comes from ch_lobby_alive, which the same BeginPlay sends 6-23 ms earlier carrying
GetPlatformUserNetId; the host arm itself sends no id at all. Correlated by source IP - see the
comment above askerSteamId() in server.cjs for the NAT caveat.
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

PORT = 8934
REPO = pathlib.Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:%d" % PORT
HOST_ID = "76561198000999000"
OTHER_ID = "76561198000000001"

FAILED = []


def need(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        FAILED.append(what)


def post(path, body, timeout=30):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer chlobby"}, method="POST")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, time.time() - started
    except urllib.error.HTTPError as e:
        return e.code, time.time() - started
    except Exception:
        return None, time.time() - started       # no reply: dropped, or we timed out waiting


def probe(event, user_id="", timeout=30):
    return post("/api/probe", {"ip": "", "event_name": event, "user_id": user_id,
                               "storefront": "lobby", "platform": "", "timestamp": "",
                               "first_session_timestamp": "chlobby-28",
                               "is_first_game_open": False}, timeout=timeout)


def slow(timeout=30):
    return post("/api/probe/slow", {"ip": "", "event_name": "ch_host_wait", "user_id": "",
                                    "storefront": "host", "platform": "", "timestamp": "",
                                    "first_session_timestamp": "chlobby-28",
                                    "is_first_game_open": False}, timeout=timeout)


def node_permits():
    """Drive live.cjs's permit rules directly. The HTTP test proves the server OBEYS the answer;
    this proves the rule the answer is based on."""
    script = (
        "const m=require('./live.cjs');"
        "const l=m.create({whoami:()=>null,bearer:()=>null,sendJson:()=>{},badRequest:()=>{},"
        "readBody:async()=>Buffer.alloc(0),upstashCmd:async()=>null,prefix:'t:'});"
        "const H=" + json.dumps(HOST_ID) + ", O=" + json.dumps(OTHER_ID) + ";"
        "const out={};"
        "out.ungranted=l.takeHostPermit(H);"
        "out.bad_id=l.grantHostPermit('not-a-steamid');"
        "out.granted=l.grantHostPermit(H);"
        "out.first_take=l.takeHostPermit(H);"
        "out.second_take=l.takeHostPermit(H);"
        "out.other_player=l.takeHostPermit(O);"
        "l.grantHostPermit(H);l.revokeHostPermit(H);"
        "out.revoked=l.takeHostPermit(H);"
        "l.grantHostPermit(H,-1);"
        "out.expired=l.takeHostPermit(H);"
        "console.log(JSON.stringify(out));"
    )
    out = subprocess.run(["node", "-e", script], cwd=str(REPO / "server"),
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None, (out.stderr or "").strip()[:120]
    return json.loads(out.stdout.strip().splitlines()[-1]), ""


env = dict(os.environ, PORT=str(PORT), NODE_ENV="test",
           PROBE_SLOW_SECONDS="2", HUB_STORE_PREFIX="hubtest_permit:")
srv = subprocess.Popen(["node", "server.cjs"], cwd=str(REPO / "server"), env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)

    print("--- a launch with no match: the clock must never tick ---")
    probe("ch_lobby_alive", HOST_ID)          # the identity half of the same BeginPlay
    # 8 s is past the 4.09 s that WORKS and short of the 12 s that is measured dead, so a reply
    # inside this window is the failure we are testing for. What matters is not that the socket is
    # eventually dropped - the edge turns that into a 502, which the pak would act on - but that
    # NOTHING arrives while the game is still listening.
    status, took = slow(timeout=8)
    need(status is None, "an ungranted host arm gets no reply while the game is still listening")
    need(took >= 7.5, "and it is held for the whole window, not refused early (waited %.1fs)" % took)

    print("\n--- the permit itself: reusable until arrival ---")
    g, err = node_permits()
    need(g is not None, "live.cjs exposes the permit API (%s)" % (err or "ok"))
    if g:
        need(g["ungranted"] is False, "a player with no permit cannot take one")
        need(g["bad_id"] is False, "a malformed steam id is refused a permit")
        need(g["granted"] is True, "the host of a connecting match is granted one")
        need(g["first_take"] is True, "the first lobby load is permitted to travel")
        need(g["second_take"] is True,
             "a retry after a dropped reply is permitted until the host arrives")
        need(g["other_player"] is False, "one player's permit is not usable by another")
        need(g["revoked"] is False, "revoking it (the host arrived) prevents a later travel")
        need(g["expired"] is False, "an expired permit is not honoured")

    print("\n--- telemetry is unaffected ---")
    status, _ = probe("ch_lobby_alive", HOST_ID)
    need(status == 200, "ordinary probes still answer normally")
    with urllib.request.urlopen(BASE + "/api/probe/log?limit=50", timeout=10) as r:
        log = json.loads(r.read().decode())
    events = [json.loads(e["body"])["event_name"] for e in log["entries"] if e.get("body")]
    need("ch_host_wait" in events,
         "an ungranted host arm is still RECORDED - the log shows the ask, the game gets no answer")

finally:
    srv.terminate()
    try:
        srv.wait(5)
    except Exception:
        srv.kill()

print()
if FAILED:
    print("HOST PERMIT TEST FAILED: %d" % len(FAILED))
    for f in FAILED:
        print("   -", f)
    sys.exit(1)
print("HOST PERMIT TEST PASSED")
