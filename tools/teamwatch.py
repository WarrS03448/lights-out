#!/usr/bin/env python3
"""Read the team sweep's rows out of the probe log and say whether the WRITE STUCK.

WHAT IS BEING MEASURED. docs/autojoin-teams.md rests on one mechanism nothing in this project has
ever exercised: writing ABodycamPlayerState::TeamID from a Blueprint. The property is protected
with AllowPrivateAccess, replicated through OnRep_TeamId, and the game assigns it itself in
AssignTeamToPlayer, so three things could be true and the design only survives one of them:

    the write never lands          every `before` stays at whatever the game chose
    it lands and is overwritten    `before` matches `want` on the tick after, then drifts back
    it lands and holds             `before` == `want` on every visit after the first

GM_BB5's sweep (bb5_graphs.py gm_teamset) visits one player every 3 s and sends ch_team_write
BEFORE it writes, so `before` is what the LAST visit left behind ~30 s earlier rather than an echo
of the write we are trying to measure.

    event_name   "ch_team_write"
    user_id      the host's SteamID64            who is reporting
    storefront   the subject's SteamID64         who the row is about
    platform     "idx:roster:want:before:lib"
    timestamp    the world clock, truncated

READ THE PAYLOAD BY INDEX, NEVER BY REGEX. Every field can be negative - TeamID is -1 until the
game assigns one, and so is GetPlayerTeamID - and an all-optional positional tail matched with one
pattern is what silently binned 100% of the stats rows on 2026-09-15.

    python tools/teamwatch.py              # the rows, then the verdict
    python tools/teamwatch.py --watch      # the same, live, until Ctrl-C
"""
import argparse
import json
import sys
import time
import urllib.request

LOG = "https://lightsout.up.railway.app/api/probe/log?limit=200"
EVENT = "ch_team_write"
VERIFIED_EVENT = "ch_team_verified"
DENY_EVENT = "ch_team_deny"
KICK_EVENT = "ch_team_kick_out"
FIELDS = ("idx", "roster", "want", "before", "lib")


_CACHE = {"at": 0.0, "entries": None}


def entries(max_age=2.0):
    """Every probe-log entry the ring still holds, newest first. Cached for a couple of seconds.

    ONE FETCH, THREE READERS. The writes, the refusals and the asks are three views of the same
    log, and fetching it three times per report meant the three could disagree with each other -
    a row could arrive between two of the calls and appear in one view and not the next. In
    --watch that is a report that contradicts itself every ten seconds."""
    now = time.time()
    if _CACHE["entries"] is not None and now - _CACHE["at"] < max_age:
        return _CACHE["entries"]
    try:
        raw = urllib.request.urlopen(LOG, timeout=30).read().decode("utf-8")
    except Exception as exc:                                   # noqa: BLE001
        print("[teamwatch] probe log unreachable: %s" % exc, file=sys.stderr)
        return []
    d = json.loads(raw)
    out = d if isinstance(d, list) else (d.get("entries") or d.get("log") or d.get("probes") or [])
    _CACHE["at"], _CACHE["entries"] = now, out
    return out


def asks(tag=None):
    """The RULING QUESTIONS the sweep asked, whatever the answers were.

    THIS IS THE DIFFERENCE BETWEEN "IT DID NOT TICK" AND "IT WAS TOLD NO". Without it, a run where
    the timer never fired and a run where every ask came back 409 both read as "no ch_team_write
    rows in the log yet", and on 2026-09-15 that ambiguity is exactly what a whole match came back
    as. The asks go to /api/probe/team rather than /api/probe, and the server records its verdict
    on the log entry itself, so the status is readable here without guessing.
    """
    out = []
    for e in entries():
        if "/api/probe/team" not in str(e.get("url") or ""):
            continue
        try:
            b = json.loads(e.get("body") or "{}")
        except Exception:                                      # noqa: BLE001
            b = {}
        if tag and str(b.get("first_session_timestamp") or "") != tag:
            continue
        ruling = e.get("ruling") or {}
        out.append({"at": e.get("at", ""),
                    "ask": str(ruling.get("ask") or b.get("platform") or "?"),
                    "subject": str(b.get("storefront") or ""),
                    "status": int(ruling.get("status") or 0),
                    "error": str(ruling.get("error") or "")})
    out.sort(key=lambda r: r["at"])
    return out


def rows(tag=None):
    """Every ch_team_write the log still holds, oldest first, parsed.

    FILTERED TO ONE BUILD. The probe log is a 200-entry ring shared by every pak that has ever
    reported, and rows from an OLDER sweep are indistinguishable from new ones by shape alone -
    chteam-1 and chteam-2 both send idx:roster:want:before:lib about the same player. Mixing them
    invents pairs that never happened: the last row of one build followed by the first of the next
    reads as a visit that "changed under us" when it is really two different programs. The build
    tag rides in FirstSessionTimestamp for exactly this reason; tag=None means the NEWEST tag
    present, which is nearly always the one just installed."""
    out = []
    for e in entries():
        try:
            b = json.loads(e.get("body") or "{}")
        except Exception:                                      # noqa: BLE001
            continue
        if b.get("event_name") not in (EVENT, VERIFIED_EVENT):
            continue
        parts = str(b.get("platform") or "").split(":")
        if len(parts) < len(FIELDS):
            continue
        try:
            vals = [int(p) for p in parts[:len(FIELDS)]]
        except ValueError:
            continue
        row = dict(zip(FIELDS, vals))
        row["verified"] = b.get("event_name") == VERIFIED_EVENT
        row["at"] = e.get("at", "")
        row["host"] = str(b.get("user_id") or "")
        row["subject"] = str(b.get("storefront") or "")
        row["clock"] = str(b.get("timestamp") or "")
        row["tag"] = str(b.get("first_session_timestamp") or "")
        out.append(row)
    out.sort(key=lambda r: r["at"])
    if tag is None:
        seen = [r["tag"] for r in out if r["tag"]]
        tag = seen[-1] if seen else None
    if tag:
        dropped = [r for r in out if r["tag"] != tag]
        out = [r for r in out if r["tag"] == tag]
        if dropped:
            print("[teamwatch] tag %s: %d row(s), ignoring %d from %s"
                  % (tag, len(out), len(dropped), sorted({r["tag"] for r in dropped})))
    return out


def verdict(rs):
    """Per subject, what happened between one visit and the next - and whether it PROVED anything.

    A pair is only evidence if the write CHANGED something. chteam-1 (2026-09-15) is the cautionary
    case: solo, `want` was 0, and the game assigns a lone player 0 anyway - so every pair read
    "wrote 0, found 0" and this function happily called it HELD. It was not held; it was
    indistinguishable. A pair whose `want` already equalled its `before` cannot tell "our write
    landed" from "nothing happened at all", and counting it as success is exactly the mistake that
    makes a broken mechanism look finished.

    So pairs are split into DECISIVE (the write asked for a different value than was already there)
    and mute. Only decisive pairs move the verdict.
    """
    by_subject = {}
    for r in rs:
        by_subject.setdefault(r["subject"], []).append(r)

    held = reverted = never = mute = 0
    lines = []
    for subject, seq in sorted(by_subject.items()):
        for prev, nxt in zip(seq, seq[1:]):
            if prev["want"] == prev["before"]:
                mute += 1
                continue
            if nxt["before"] == prev["want"]:
                held += 1
                mark = "HELD"
            elif nxt["before"] == prev["before"]:
                never += 1
                mark = "NEVER LANDED"
            else:
                never += 0
                reverted += 1
                mark = "CHANGED UNDER US (now %d)" % nxt["before"]
            lines.append("  %s  had %d, wrote %d, found %d next visit -> %s"
                         % (subject[-6:], prev["before"], prev["want"], nxt["before"], mark))
    return held, reverted, never, mute, lines


def kicks(tag=None):
    """Every player the sweep tried to REMOVE, oldest first.

    The row is sent BEFORE KickPlayerInLobby is called, because the call has never been made by this
    project and may well take the caller's world with it. That ordering is also what makes the list
    readable as evidence: KickPlayerInLobby names the LOBBY, and whether it removes someone from the
    match world they are standing in is unmeasured. A subject that appears ONCE went. A subject that
    appears every bucket is still in the match and the call did nothing - which is the outcome to
    look for first, and the reason report() counts repeats rather than just listing names."""
    out = []
    for e in entries():
        try:
            b = json.loads(e.get("body") or "{}")
        except Exception:                                      # noqa: BLE001
            continue
        if b.get("event_name") != KICK_EVENT:
            continue
        if tag and str(b.get("first_session_timestamp") or "") != tag:
            continue
        out.append({"at": e.get("at", ""), "subject": str(b.get("storefront") or "")})
    out.sort(key=lambda r: r["at"])
    return out


def denials(tag=None):
    """The rows where the backend REFUSED to rule, which stage 2 reports and acts on by writing
    nothing. This is the diagnostic that matters most in a first run: a refusal and a real "no" both
    arrive as bSuccess=false, and the difference is only visible here plus in the server's own log
    line. Everyone denied means the ruling is not reaching us - wrong host, no connecting match, or
    a deploy that does not have the route - and NOT that the sweep is broken."""
    out = []
    for e in entries():
        try:
            b = json.loads(e.get("body") or "{}")
        except Exception:                                      # noqa: BLE001
            continue
        if b.get("event_name") != DENY_EVENT:
            continue
        if tag and str(b.get("first_session_timestamp") or "") != tag:
            continue
        out.append({"at": e.get("at", ""), "subject": str(b.get("storefront") or "")})
    out.sort(key=lambda r: r["at"])
    return out


def report(rs):
    # chteam-4 reports AFTER the write. Never feed these rows into the legacy before/after
    # persistence proof: an immediate readback proves application, not survival of a round.
    if rs and rs[0].get("verified"):
        print("=== post-write verification (%s)" % rs[0]["tag"])
        for r in rs[-20:]:
            ok = r["want"] == r["before"] == r["lib"]
            print("  %s  %s  want=%d actual=%d lib=%d  %s" %
                  (r["at"][11:19], r["subject"][-6:], r["want"], r["before"], r["lib"],
                   "APPLIED" if ok else "MISMATCH"))
        print("  %d player(s) reported. Round-transition persistence still needs in-game testing." %
              len({r["subject"] for r in rs}))
        return
    tag = rs[0]["tag"] if rs else None
    ak = asks(tag)

    # DID THE SWEEP TICK AT ALL. Answered first, and separately from what the answers were, because
    # every other line below is meaningless if the timer never fired - and "no rows" on its own
    # cannot tell the two apart.
    print("=== did the sweep tick?")
    if not ak:
        print("  NO ASKS. Nothing reached /api/probe/team, so the sweep did not run: either the")
        print("  installed pak has no TeamSweep timer, or GM_BB5's BeginPlay chain never reached")
        print("  it. Check the pak on disk for the string TeamSweep before blaming the backend.")
    else:
        good = [a for a in ak if a["status"] == 200]
        no = [a for a in ak if a["status"] == 404]
        cant = [a for a in ak if a["status"] not in (200, 404)]
        print("  %d ask(s): %d yes, %d no, %d could-not-rule; last at %s"
              % (len(ak), len(good), len(no), len(cant), ak[-1]["at"][11:19]))
        member = [a for a in ak if a["ask"] == "member"]
        side = [a for a in ak if a["ask"] == "side"]
        stranger = [a for a in ak if a["ask"] == "stranger"]
        print("  member %d, side %d, stranger %d" % (len(member), len(side), len(stranger)))
        # The kick question is asked only after a `member` said no, and answers yes only when the
        # backend positively knows the subject is off the roster. Asked-but-never-yes is the HEALTHY
        # reading during warm-up: bots send an empty id and get a 409, which kicks nobody.
        if stranger and not [a for a in stranger if a["status"] == 200]:
            print("  (asked about %d possible stranger(s); the backend confirmed none, so nobody "
                  "was kicked)" % len(stranger))
        if member and not side:
            print("  !! no `side` ask ever followed a `member` one, so no write was ever attempted.")
        if cant:
            why = sorted({a["error"] for a in cant if a["error"]}) or ["(no reason recorded)"]
            print("  !! the backend could not rule %d time(s): %s" % (len(cant), why))
    print()

    kk = kicks(tag)
    if kk:
        seen = {}
        for r in kk:
            seen.setdefault(r["subject"], []).append(r["at"])
        print("=== %d KICK(S) ordered" % len(kk))
        for subject, whens in sorted(seen.items()):
            if len(whens) == 1:
                print("  %s  once at %s - command logged; removal is not yet confirmed"
                      % (subject[-6:] or "(empty)", whens[0][11:19]))
            else:
                print("  %s  %d times, %s .. %s  !! REPEATED ATTEMPTS: the sweep kept finding them,"
                      % (subject[-6:] or "(empty)", len(whens), whens[0][11:19], whens[-1][11:19]))
                print("          so check roster observations for failed removal or rejoining.")
        print()

    dn = denials(tag)
    if dn:
        who = sorted({r["subject"][-6:] for r in dn})
        print("!! %d REFUSAL(s) (ch_team_deny) for %s - the backend would not rule, so nothing was"
              % (len(dn), who))
        print("   written for them. A 409 and a real 'no' look the same here; the server's own probe")
        print("   log line carries the reason.")
    if not rs:
        if dn:
            return
        print("no %s rows in the log yet. The sweep only runs in the MATCH world - GM_BB5's "
              "BeginPlay - so nothing arrives until a Bodybomb 5v5 match is actually loaded." % EVENT)
        return
    print("=== %d rows, %s .. %s" % (len(rs), rs[0]["at"][11:19], rs[-1]["at"][11:19]))
    clocks = [int(r["clock"]) for r in rs if r["clock"].lstrip("-").isdigit()]
    if clocks and len(set(clocks)) == 1:
        print("!! the world clock never moved (%d in every row) - the derived cursor is pinned "
              "and only one player will ever be visited" % clocks[0])
    idxs = sorted({r["idx"] for r in rs})
    print("visited indexes: %s   roster sizes seen: %s"
          % (idxs, sorted({r["roster"] for r in rs})))
    disagree = [r for r in rs if r["before"] != r["lib"]]
    if disagree:
        print("!! %d row(s) where the TeamID property and GetPlayerTeamID disagree - a direct "
              "write is not the whole story" % len(disagree))
    for r in rs[-20:]:
        print("  %s  clock=%-5s idx=%d/%d  %s  want=%d before=%d lib=%d"
              % (r["at"][11:19], r["clock"], r["idx"], r["roster"], r["subject"][-6:],
                 r["want"], r["before"], r["lib"]))

    held, reverted, never, mute, lines = verdict(rs)
    print()
    print("=== does the write stick?")
    for line in lines[-20:]:
        print(line)
    print("  decisive: held %d   changed under us %d   never landed %d   (mute pairs: %d)"
          % (held, reverted, never, mute))
    if not (held or reverted or never):
        print("  VERDICT: NOTHING PROVEN. Every pair asked for the value the player already had, so")
        print("  a write that landed and no write at all look identical. `want` has to differ from")
        print("  what the game would choose on its own - that is what chteam-2's +1 is for.")
    elif held and not never and not reverted:
        print("  VERDICT: the write sticks, and it was distinguishable. The sweep can be the")
        print("  enforcement mechanism; stage 2 only changes where `want` comes from.")
    elif never and not held:
        print("  VERDICT: the write does NOT land. The sweep cannot be the enforcement mechanism")
        print("  and the roster has to travel in lobby attributes instead.")
    else:
        print("  VERDICT: the write lands but something overwrites it. The sweep has to be faster")
        print("  than whatever that is - measure the gap before picking a period.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="poll until Ctrl-C")
    ap.add_argument("--every", type=float, default=10.0)
    args = ap.parse_args()
    if not args.watch:
        report(rows())
        return
    seen = -1
    while True:
        rs = rows()
        if len(rs) != seen:
            seen = len(rs)
            print("\n" + time.strftime("%H:%M:%S"))
            report(rs)
        time.sleep(args.every)


if __name__ == "__main__":
    main()
