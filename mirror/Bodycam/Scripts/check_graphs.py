"""check_graphs.py — static checks on the JSON graphs before they ever reach the editor.

Usage: python3 check_graphs.py dom | ctf | bb5 | chlobby

Beyond the self-check each *_graphs.py already does (ids unique, no link to an unknown node), this looks for the failure
mode that has actually cost this project a build: a node that does work but is never reached, or a pure node wired into an
exec chain. A graph error would be reported by BuildGraph, but an ORPHANED SET node compiles perfectly and silently does
nothing — which is the same class of bug as v20's unlinked HitResult pin.

Checks
  1. every node with an outgoing exec pin also has an incoming exec pin (events / entries / existing events excepted)
  2. every set / branch / foreach / spawn / sequence / non-pure cast node has an incoming exec pin
  3. no exec link into a KismetMathLibrary node (those are pure: the link would be refused)
  4. no duplicate node ids, no link to an unknown node, no link to an unknown pin of a *typed* node
  5. no exec OUTPUT pin driving more than one node  <- chlobby-29's lost peek arm, see below
  6. every link to a Sequence node names a pin it actually has (`exec`, or `then_N` for N < count)

Check 5 is the expensive one to have learned. An exec output pin in Unreal takes exactly ONE link:
making a second BREAKS the first (CONNECT_RESPONSE_BREAK_OTHERS_A). `peek_probe` linked two search
arms straight onto `hostdelay2.then`, so the second silently replaced the first and only ONE of them
ever ran - for three builds, with the JSON carrying both links and the source reading correctly.
The probe log was the only witness: ch_filter_* arrived every run and ch_peek_ask never did.

A Sequence is the usual fix, but NOT always: a class that travels must have nothing outstanding when
OpenLevel fires, so fanning out two lobby searches there trades a silent bug for a crash. KNOWN_FANOUT
records those, with the reason and where the arm should move to instead.

Check 7 is the one that got away, and it cost a build. An async-action FACTORY node (JoinLobby,
FindLobbies, UpdateLobby - anything whose ReturnValue feeds an adddelegate or Activate) has a real
Exec pin. Leave it unconnected and the compiler PRUNES the node, which then strands every binding
and the Activate with no Target: three errors that all point at the bindings and none of them at the
cause. Checks 1 and 2 both miss it, because a node with NO exec links at all - neither in nor out -
looks exactly like a pure node.

Check 6 catches the same failure in the other direction: `then 0` (a space) instead of `then_0`
names a pin that does not exist, so the link is dropped and the branch is silently never reached.
"""
import hashlib, json, sys, importlib

EXEC_IN = {"exec", "Exec"}
EXEC_OUT = {"then", "else", "LoopBody", "Completed", "cast_ok", "cast_fail"}
SOURCES = {"event", "customevent", "existing", "entry"}


def _exec_out(nodes, node_id, pin):
    """Is `node_id.pin` an EXEC output? `cast_ok` is the only ambiguous name: on an impure cast it is the valid-cast exec
    pin, but on a PURE cast it is the bSuccess BOOL (BodycamGraphBuilder.cpp FindPinLoose says exactly that). Reading it as
    exec made checks 1 and 5 lie about every pure cast whose success is tested - which BB5's PlaceBomb does on purpose, to
    keep a failed cast's TeamID 0 from reading as the real team 0."""
    if pin not in EXEC_OUT: return False
    n = nodes.get(node_id, {})
    if n.get("type") == "cast" and n.get("pure") and pin in ("cast_ok", "cast_fail"): return False
    return True

# Nodes that are UNREACHED ON PURPOSE. Kept out of the results so a NEW orphan still stands out - a
# checker that always fails is a checker everybody learns to ignore. Each entry needs a reason, and
# removing one should be part of the change that reconnects the node.
# Exec pins that drive more than one node and MUST NOT be "fixed" by adding a Sequence. Normally
# check 5 is right and a Sequence is the answer; here the broken link is load-bearing.
# THE HOSTING GRAPHS ARE FROZEN (Sam, 2026-09-15: "we spent a ton of time nailing it and its in a
# spot where we dont want to touch it"). chlobby-28 hosts correctly and travels with nothing
# outstanding; that is a property worth protecting by machine rather than by memory.
#
# These are sha256 prefixes of each graph's generated JSON. A mismatch is not automatically wrong -
# it means you changed the HOST pak, deliberately or not - but it must be a decision, not a
# surprise. It has already been a surprise once: an unbounded string replace while adding GM_CHPeek
# retagged chlobby_logic's ch_lobby_alive from chlobby-29 to chpeek-1, and the check run at the time
# compared the function to ITSELF and happily reported no change.
#
# Changing one on purpose: re-run with UPDATE_FROZEN=1 to print the new values, and say why in the
# commit.
# UPDATED 2026-09-16, deliberately, with Sam's go-ahead ("you can touch the host mechanics
# if it allows this form of auto join to work"). chlobby_logic b84858eace0a7a9d ->
# 288e20dcdbef6455, and BUILD_TAG chlobby-29 -> chlobby-30 so a run is attributable.
# Three changes, the first two forced:
#   * the probe URLs moved to lightsout.up.railway.app. The Lights Out rebrand updated
#     bb5_graphs.py, peekrun.py and hub/version.py but missed lobby_graphs.py, so the
#     SHIPPED seed was asking a dead hostname for its travel permit - and the held reply IS
#     the permission, so auto-host had silently stopped travelling.
#   * the host now stamps BodycamGI "Session Name" = JOIN_TOKEN before Parent BeginPlay.
#     MakeCreateLobbyParams copies it into the lobby's `Name` attribute, which chtjoin-12
#     measured IS honoured server-side - so a joiner can be pointed at us by exact name
#     instead of sifting a browser that does not reliably list everything.
# UPDATED 2026-09-16 (second time today), deliberately: chlobby_logic 288e20dcdbef6455 ->
# c7ef29cfab296335, BUILD_TAG chlobby-30 -> chlobby-31. STAGE 2 OF host_probe IS DELETED.
# It re-opened the map stage 1 had already loaded, gated only on `IsServer && !IsStandalone` with
# no lap gate - so as a listen host in BB5_Hospital it read true and travelled to BB5_Hospital
# again, forever. Sam saw auto-host load, load a second time, and freeze; the third test ended in
# an application hang. It only became reachable because the rebrand fix above restored the travel
# permit and so produced a second BeginPlay for the first time in weeks. Full reasoning in
# host_probe's "STAGE 2 IS GONE" block.
# UPDATED 2026-09-17: remove diagnostic search/legacy join from the traveling host.
# Both fresh null-read crash dumps match the historical EOS search/travel stack;
# generated-graph and shipped-bytecode tests now forbid those calls independently.
FROZEN = {
    "chlobby_events": "4847cf5728a1a18b",
    # 2026-09-18: native range first; authenticated, one-shot match travel.
    "chlobby_logic": "89d9abfb69e7554e",
    "host_stub_events": "059a9394d3dc91d4",
}

KNOWN_FANOUT = {}

KNOWN_ORPHANS = {}
PURE_LIBS = {"/Script/Engine.KismetMathLibrary"}
# functions outside the math library that are BlueprintPure in this engine build, so wiring an exec pin to them is refused.
# Add to this list whenever the editor reports "no input pin 'exec'" — that is how GetOverlappingActors got here (2026-09-14).
PURE_FUNCS = {"GetOverlappingActors", "IsValid", "GetController", "HasAuthority", "K2_GetActorLocation", "K2_GetPawn", "IsVisible"}


def check(name, js):
    d = json.loads(js)
    nodes = {n["id"]: n for n in d["nodes"]}
    problems = []
    if name == "chlobby_logic":
        for node in nodes.values():
            if node.get("type") == "call" and node.get("func") in {"FindLobbies", "JoinLobby"}:
                problems.append(f"host travel cannot overlap {node['func']} ({node['id']}); "
                                "use the separate joiner or diagnostic class")
    has_exec_in, has_exec_out = set(), set()
    for a, b in d["links"]:
        an, ap = a.split(".", 1); bn, bp = b.split(".", 1)
        if _exec_out(nodes, an, ap): has_exec_out.add(an)
        if bp in EXEC_IN:
            has_exec_in.add(bn)
            n = nodes.get(bn, {})
            if n.get("type") == "call" and (n.get("class") in PURE_LIBS or n.get("func") in PURE_FUNCS):
                problems.append(f"exec link into the pure node {bn} ({n.get('func')})")
    for nid, n in nodes.items():
        t = n["type"]
        if t in SOURCES: continue
        needs = t in ("set", "branch", "foreach", "spawn", "sequence") or (t == "cast" and not n.get("pure"))
        if (needs or nid in has_exec_out) and nid not in has_exec_in:
            if (name, nid) in KNOWN_ORPHANS:
                continue
            problems.append(f"{t} node {nid} is never reached (no incoming exec)")

    # 5. an exec OUTPUT pin may drive exactly one node. A second link does not fan out - it BREAKS
    #    the first, and nothing anywhere says so. See the module docstring.
    driven = {}
    for a, b in d["links"]:
        node, pin = a.split(".", 1) if "." in a else (a, "")
        if _exec_out(nodes, node, pin) or pin.startswith("then_") or pin in EXEC_IN:
            driven.setdefault(a, []).append(b)
    for a, dests in sorted(driven.items()):
        if len(dests) > 1:
            if (name, a) in KNOWN_FANOUT:
                continue
            problems.append(
                f"exec pin {a} drives {len(dests)} nodes ({', '.join(sorted(dests))}) - "
                f"an exec output takes ONE link, the rest are silently broken; use a sequence")

    # 7. An async-action factory must be ON the exec chain, not merely referenced by its bindings.
    #    Identified by what CONSUMES it: a ReturnValue wired into an adddelegate's or Activate's
    #    self/Target pin is a proxy, and a proxy with no incoming exec is pruned at compile time.
    proxies = set()
    for a, b in d["links"]:
        src, _, spin = a.partition(".")
        dst, _, dpin = b.partition(".")
        if spin != "ReturnValue" or dpin not in ("self", "Target"):
            continue
        n = nodes.get(dst)
        if not n:
            continue
        if n.get("type") == "adddelegate" or n.get("func") == "Activate":
            proxies.add(src)
    for nid in sorted(proxies):
        if nid in has_exec_in:
            continue
        problems.append(
            f"async proxy {nid} has no incoming exec - it will be PRUNED at compile time "
            f"(\"was pruned because its Exec pin is not connected\"), stranding its bindings "
            f"and Activate with no Target. Put it ON the exec chain, not just beside it")

    # 6. a Sequence's pins are `exec` and `then_0..then_{count-1}`. Anything else is a dropped link.
    for nid, n in nodes.items():
        if n.get("type") != "sequence":
            continue
        count = int(n.get("count") or 0)
        legal = {"exec"} | {f"then_{i}" for i in range(count)}
        for a, b in d["links"]:
            for end in (a, b):
                if end.split(".", 1)[0] != nid:
                    continue
                pin = end.split(".", 1)[1] if "." in end else ""
                if pin not in legal:
                    problems.append(
                        f"sequence {nid} has no pin {pin!r} (count={count}); "
                        f"legal: exec, then_0..then_{count - 1}")
    return problems


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "dom"
    # chlobby lives in lobby_graphs.py; everything else follows the <name>_graphs.py convention
    m = importlib.import_module("lobby_graphs" if which == "chlobby" else "bb5_graphs" if which == "bb1" else f"{which}_graphs")
    graphs = {}
    if which == "dom":
        graphs = {"point_events": m.point_events(), "point_onrep_ownerteam": m.point_onrep_ownerteam(),
                  "point_onrep_eventseq": m.point_onrep_eventseq(), "point_logic": m.point_logic(),
                  "gm_events": m.gm_events(), "gm_logic": m.gm_logic(),
                  "gm_findplayerstart": m.gm_findplayerstart(), "gm_chooseplayerstart": m.gm_chooseplayerstart()}
        for f in m.GM_FUNCTIONS: graphs["fn_" + f] = m.GM_FN_BODIES[f]()
    elif which == "chlobby":
        # The lobby pak was never checked by this script, which is precisely how an exec pin driving
        # two search arms survived three builds. chlobby_logic() is the whole of GM_CHLobby.
        graphs = {"chlobby_events": m.chlobby_events(), "chlobby_logic": m.chlobby_logic(),
                  "host_stub_events": m.host_stub_events(),
                  # the measurement class. It shares no graph with GM_CHLobby on purpose.
                  "chpeek_events": m.chpeek_events(), "chpeek_logic": m.chpeek_logic(),
                  # the joiner, packed for the machines that are not hosting
                  "chjoin_events": m.chjoin_events(), "chjoin_logic": m.chjoin_logic(),
                  # the stranger-join test class - never shipped, never travels-by-config
                  "chtjoin_events": m.chtjoin_events(), "chtjoin_logic": m.chtjoin_logic()}
    elif which in ("bb5", "bb1"):
        # bb5's gm_logic() is where the probes live, so this is the graph that matters here: the
        # match-world callback probe fans three LATENT sends out of a Sequence, and an exec pin left
        # off one of them would compile perfectly and simply never ask (see team_bit_probe).
        graphs = {"gm_events": m.gm_events(), "gm_logic": m.gm_logic(),
                  "gm_shouldspawnbots": m.gm_shouldspawnbots(),
                  # the start gate (2026-09-16/17). NEITHER of these was checked here until now, and
                  # they are the two predicates that decide whether a match begins at all - a pure
                  # override that loses a link does not fail a cook, it just answers false for ever.
                  "gm_min_players": m.gm_min_players(), "gm_player_full": m.gm_player_full(),
                  # the team-base spawn overrides (2026-09-16): both call the parent FIRST and hand its
                  # answer to the component, so a missing exec link here silently returns the stock pick
                  "gm_findplayerstart": m.gm_findplayerstart(), "gm_chooseplayerstart": m.gm_chooseplayerstart(),
                  "inventory_events": m.inventory_events(),
                  "rule_events": m.rule_events(), "rule_logic": m.rule_logic(),
                  "rule_assign": m.rule_assign()}
        for f in m.RULE_FUNCTIONS: graphs["fn_" + f] = m.RULE_FN_BODIES[f]()
    else:
        graphs = {"flag_logic": m.flag_logic(), "base_logic": m.base_logic(), "gm_logic": m.gm_logic(),
                  "gm_findplayerstart": m.gm_findplayerstart(), "gm_chooseplayerstart": m.gm_chooseplayerstart()}
        for f in m.GM_FUNCTIONS: graphs["fn_" + f] = m.GM_FN_BODIES[f]()
    if which == "bb1":
        import bodybomb_variant
        graphs = {name: bodybomb_variant.graph(js, "BB1") for name, js in graphs.items()}
    bad = 0
    import os
    if os.environ.get("UPDATE_FROZEN"):
        for name, js in sorted(graphs.items()):
            if name in FROZEN:
                print(f'    "{name}": "{hashlib.sha256(js.encode()).hexdigest()[:16]}",')
        sys.exit(0)
    for name, js in sorted(graphs.items()):
        want = FROZEN.get(name) if which != "bb1" else None
        if want:
            got = hashlib.sha256(js.encode()).hexdigest()[:16]
            if got != want:
                bad += 1
                print(f"{name}: FROZEN graph changed ({want} -> {got}). The host pak is not "
                      f"supposed to move; if you meant it, UPDATE_FROZEN=1 and say why.")
    for name, js in sorted(graphs.items()):
        ps = check(name, js)
        if ps:
            bad += len(ps)
            for p in ps: print(f"{name}: {p}")
    print(f"{'FAIL' if bad else 'OK'}: {len(graphs)} graphs, {bad} problem(s)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
