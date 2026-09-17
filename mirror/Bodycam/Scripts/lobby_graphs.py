"""lobby_graphs.py - GM_CHLobby, the gamemode that runs while a player stands in the shooting range.

WHY (docs/autojoin.md). Competitive has to put ten people into one match without anybody adding
anybody as a friend. The game's own `FindLobbies` + `JoinLobby` does exactly that, but a GameMode
only exists inside a match and the joiner is on the menu - which, since the shooting-range update,
is itself a level: `/Game/Map/LobbyHost/LobbyHost`, whose World Settings select `GM_Host_C`, opened
"listen" so EVERY player runs it on their own machine. Point that at a child of ours and our code is
running exactly where the joining has to happen.

HOW THE PARENT IS REACHED. `GM_Host_C` is one of the game's Blueprints, so the mirror cannot pick it
as a parent. It does not need to: this is the stand-in pattern BB5 already uses for `Bombe` and
`BP_InventoryComponent`. We create an EMPTY `GM_Host` at the game's own path, parent it to the
engine's `AGameModeBase` (which is what the real one derives from - verified by dumping the cooked
class), and author `GM_CHLobby` as its child. The cooked child then imports
`/Game/GM/GM_Host.GM_Host_C` as its super, and at runtime that resolves to the GAME's class.
**The stand-in is never shipped.**

TWO RULES THIS FILE MUST KEEP, both of them load-bearing:

  1. `GM_CHLobby` declares NO variables and changes NO class defaults. A cooked CDO is written by
     property index across the whole hierarchy; our stand-in parent has 0 properties and the real
     `GM_Host_C` has 12, so any stored default would land in the wrong slot. A Blueprint that
     stores nothing has nothing to misalign, because unversioned data omits everything sitting at
     its default.
  2. BeginPlay CALLS THE PARENT FIRST. `GM_Host_C::ReceiveBeginPlay` is where the lobby does its
     Steam login, app id, rank init, privilege checks and session settings. A child event REPLACES
     the parent's unless it calls Parent, so forgetting this does not break our feature - it breaks
     the player's game.

CORRECTED (2026-09-14, chlobby-11): BeginPlay fires ONCE PER LEVEL LOAD, and normal use does not
reload the level. This file previously said it "runs MORE THAN ONCE per session - three times in
three minutes while Sam moved around the menus"; that was wrong. Tested directly: a full pass
through every menu and settings screen produced no second ch_lobby_alive in 5.6 minutes, and the
three events behind the original note were three separate LAUNCHES. Being safe to re-run is still
right (a relaunch re-runs it), but nothing here may ASSUME a second firing - which is why the queue
is a self-chaining HTTP loop and not a re-entry, see late_probe.

WHAT IT DOES TODAY. Nothing but ask questions, and every question reports its own answer. Step 3
is settled - probes arrive while Sam stands in the shooting range, so the foothold is real, and the
one response bit demonstrably tracks the HTTP status rather than merely the transport. What is open
is step 4: whether `FindLobbies` will actually run from here. The joining logic comes after that
answer, and only then.
"""
from ctf_graphs import G, P, SYS, STR, ARR, ACTOR, MATH, GS_LIB, CONTROLLER   # noqa: F401

# the game's own path: our stand-in sits here at build time so the cooked super import names it,
# and the REAL class answers to it at runtime. Never shipped.
GI_PKG = "/Game/MenuSystemPro/Blueprints/Core/BodycamGI"
GI_CLASS = GI_PKG + ".BodycamGI_C"

# THE MATCH TOKEN. One literal, written by the HOST into "Session Name" and by the JOINER into
# "SessionToJoin (Client)"; the game matches them with an exact server-side `Name` search, which
# chtjoin-12 measured is actually honoured (unlike Map and Access, which the backend ignores).
#
# It is fed through MakeLiteralName -> Conv_NameToString so the value lands in the .uasset FName
# table, where tools/pak/retarget_lobby.py can rewrite it per match in 1-4 ms with no cook - the
# same machinery that already retargets the host map.
JOIN_TOKEN = "chjoin-7f3a91"
# A second, private value. The host writes it into a local GameInstance scratch string; it is
# never copied into Steam lobby metadata or into the joiner's pak. GM_BB5 reads the same property
# after travel and supplies it as SendAttributionEvent's bearer credential.
REPORT_TOKEN = "chreport-7f3a91"

# HOW MANY TIMES THE JOINER MAY TRY. Each lap is a full LobbyHost reload, measured at ~300 ms, so
# eight laps is ~2.4 s of "waiting for connection" before the player boots normally. That is sized
# against the ~2.5 s lobby warm-up a search needs (chpeek-6): a host who has only just stamped its
# lobby may genuinely not be visible on the first attempt, and a single shot would lose that race.
LAP_PROP = "MostFillServer"
LAP_TRIES = 8
LAP_DONE = LAP_TRIES          # the rung that writes no token; also where any unknown value lands

# What chlobby-16 hosts. BB5 is enum 14 (stock GameMode runs None=0 .. Wingman=12, GameMode_MAX=13,
# and build_gamemode.py hands community modes 13, 14, ... - CTF took 13). The map is OUR cooked
# level: the gamemode paks ship BB5_* and CTF_* as separate files, so the name is unambiguous.
HOST_GAMEMODE = "14"              # BB5
HOST_MAP = "/Game/GM_Maps/Community/BB5/BB5_Hospital"   # the real cooked path, from our own pak
HOST_MAP_SHORT = "BB5_Hospital"                        # what the lobby ADVERTISES (stock carries "Rome")
CREATE_LOBBY = "/Script/Bodycam.BodycamCreateLobby"
DESTROY_LOBBY = "/Script/Bodycam.BodycamDestroyLobby"
# The game becomes a host by opening ITS OWN lobby level "listen" - not the match map.
LOBBY_LEVEL = "/Game/Map/LobbyHost/LobbyHost"

HOST_PKG = "/Game/GM/GM_Host"
HOST_CLASS = HOST_PKG + ".GM_Host_C"

CHLOBBY_DIR = "/Game/GM/Gamemode"
CHLOBBY = CHLOBBY_DIR + "/GM_CHLobby.GM_CHLobby_C"

# WHICH LIBRARY OWNS WHAT. These two are easy to mix up and the editor only tells you on a run
# (2026-09-14: GetOnlinePlatformName was called on BodycamOnlineManager and the build failed).
#   AttributionHttpRequestLibrary : SendAttributionEvent, GetOnlinePlatformName, GetOnlineStoreFront
#   BodycamOnlineManager          : GetPlatformUserNetId, IsInLobby, GetCurrentLobbyInfo,
#                                   RetrievePlatformIdAsStringFromPlayerState, GetRichPresence
# The mirror stubs under Source/Bodycam/Public are the authority: a function absent there fails the
# build even if the real game has it.
# The constant tag one of our match lobbies carries. A joiner can hard-code this and needs
# nothing from our backend to search for it (docs/autojoin.md section 4).
#
# NB: this tag and the LobbyName below keep their pre-rename wording after the Lights Out
# rename (2026-09-15) ON PURPOSE. They are wire values, not branding: a joiner searches for
# the exact string, so changing one here makes new hosts invisible to every client that has
# not been rebuilt - and this file must not be rebuilt casually anyway (it still builds
# chlobby-29). Rename them only as a deliberate, coordinated protocol change.
QUEUE_KEY = "CH_QUEUE"
QUEUE_VALUE = "1"

# THE KEY GM_BB5 ACTUALLY WRITES, and the last unproven link in the whole design: that a key
# stamped by one machine is SEARCHABLE by another, rather than merely readable back locally.
# bb5_graphs.py LOBBY_KEY/LOBBY_VALUE - its gm_probe calls UpdateLobby at t+30 s into a match
# with bAllowJoinInProgress=true. Arm B searched {CH_QUEUE:"1"} in chlobby-9 and returned 0
# through OnSuccess twice, which is what settled "empty is success, not failure"; that control
# is spent, so the arm is repointed at the real question. Arm C keeps {Access:"false"} as the
# positive control (25 results, twice).
MATCH_KEY = "CH_MATCH"
MATCH_VALUE = "ch-test-4821"

# A key/value the GAME ITSELF puts on every lobby it creates, so a search for it MUST match if the
# search runs at all. This is the positive control chlobby-6 lacked: CH_QUEUE matches nothing in the
# world, so "no results" and "refused to search" produced the identical ch_find_failed and the run
# settled nothing.
#
# VERIFIED from the stock game's own bytecode, not inferred (2026-09-14). BodycamGI::
# MakeLobbySearchParams (export 88, pakchunk24 MenuSystemPro/Blueprints/Core/BodycamGI) builds a
# four-key base filter - GameID, Access, Build, Platform - with String 'false' at offset 0x0086 fed
# through MakeStringLobbyAttribute and bound to String 'Access' at 0x0127. MakeCreateLobbyParams
# (export 87) publishes 'Access' as one of 14 keys, its value produced by Conv_BoolToString
# (bPartyPrivate) - which is exactly the lowercase 'false' the search side hard-codes. It is the ONLY
# true/false string literal in all 14.5 KB of BodycamGI bytecode, and the server browser that
# demonstrably lists 139 lobbies in-game hard-codes the same one.
#
# Deliberately NOT sending GameID: the AppID would have to be a literal, and a wrong literal fails
# silently by returning zero - the worst possible failure for a positive control. A strict subset of
# the game's own filter can only ever match MORE lobbies, never fewer.
ACCESS_KEY = "Access"
ACCESS_VALUE = "false"
# A map name the stock game ships and real lobbies advertise, for chlobby-29's filter test. Its only
# job is to be a key the backend did not invent and a value that plausibly matches SOME lobby.
FILTER_TEST_VALUE = "Rome"

# THE JOINER'S IDENTITY LITERAL, patched per match by the hub (hub/lobbypak.py, the same FName
# rewrite that points the host at his map). It must be an FName and not an FString: FNames live in
# the .uasset name table where pkgedit can rewrite them at ANY length, while an FString is an inline
# EX_StringConst in the .uexp where a length change would shift every jump offset after it.
#
# WHY THE HOST'S STEAMID64 AND NOT OUR OWN CH_MATCH TAG. The joiner has to pick its host out of ~25
# strangers' lobbies, and the identity has to be one BOTH machines can produce: the hub knows it
# offline from the match roster (live.cjs match.host), and the host derives it at runtime from
# GetPlatformUserNetId. Nothing on either machine can derive a CH_MATCH value, so that tag would
# have to be baked into a pak on both sides. The game itself publishes PlayerSteamId on every lobby
# it creates - disassembled out of the shipping MakeCreateLobbyParams (export 87, a real body, the
# key at bytecode 0x3f5), one of fourteen attributes.
#
# UNMEASURED, and the peek arm is what settles it: the game writes this attribute as
# BreakSteamID(FSteamID) while our host writes GetPlatformUserNetId, and the two formats have never
# been compared. If they differ, this literal changes format and nothing else about the design does.
JOIN_KEY = "PlayerSteamId"
HOST_ID = "0"                    # a placeholder the hub overwrites; never matches a real lobby
PROBE_JOIN_URL = "https://lightsout.up.railway.app/api/probe/join"

# The delay now lives on the SERVER (server.cjs PROBE_SLOW_SECONDS), reachable at
# PROBE_SLOW_URL, so it can be retuned with a redeploy rather than a pak rebuild.

HTTP_LIB = "/Script/Bodycam.AttributionHttpRequestLibrary"
ONLINE = "/Script/Bodycam.BodycamOnlineManager"
FIND_LOBBIES = "/Script/Bodycam.BodycamFindLobbies"
JOIN_LOBBY = "/Script/Bodycam.BodycamJoinLobby"
# The subsystem that owns the SECOND half of the game's join flow - the travel. Every member we
# touch is public and NATIVE in the real class, so rule 1 permits it; see the stub's header for
# what is deliberately absent (BP_HandleRequestServerTravel is a BlueprintImplementableEvent, i.e.
# an ubergraph thunk, and TryPush/TryPopLobbySearchResult are protected).
LOBBY_MGR = "/Script/Bodycam.BodycamLobbyManager"
SUBSYS_LIB = "/Script/Engine.SubsystemBlueprintLibrary"
TYPES = "/Script/Bodycam.BodycamOnlineTypesLibrary"
SEARCH_RESULT = "/Script/Bodycam.BodycamLobbySearchResult"
ASYNC_BASE = "/Script/Engine.BlueprintAsyncActionBase"

PROBE_URL = "https://lightsout.up.railway.app/api/probe"
# A route that certainly does NOT exist, so the service answers 404. See bit_probe() below.
PROBE_404 = "https://lightsout.up.railway.app/api/probe/no-such-route"
# Answers 200, but only after the server has sat on it (server.cjs PROBE_SLOW_SECONDS). This is
# the delay mechanism, because K2_SetTimer does not fire on the lobby GameMode - see late_probe().
PROBE_SLOW_URL = "https://lightsout.up.railway.app/api/probe/slow"
# The same 4 s hold, but NEVER gated. /api/probe/slow became the host's travel permit on 2026-09-15
# (server/live.cjs grantHostPermit): it answers only the host of a connecting match and drops
# everyone else, which is what stops a normal launch auto-hosting. Any OTHER arm that needs a clock
# must therefore use this route instead, or it would go silent exactly when the host is not playing.
PROBE_WAIT_URL = "https://lightsout.up.railway.app/api/probe/wait"
BUILD_TAG = "chlobby-31"        # bumped per build, so the probe log says WHICH pak is running


def host_stub_events():
    """The stand-in's ReceiveBeginPlay. It has no body: it exists only so the child has a parent
    function to call, and so the cooked call resolves to the real one at runtime."""
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    return g.json()


def chlobby_events():
    """Every delegate handler the graph binds, declared once so `createevent` can name them.

    Two shapes, and the difference is the whole of section 4 in docs/autojoin.md.
    `SendAttributionEvent`'s delegate is DECLARE_DYNAMIC_DELEGATE_OneParam(..., bool, bSuccess):
    exactly one bool, which is the ONLY thing our backend can ever tell the game. `FindLobbies`'
    OnSuccess carries the full search results, attribute maps and all - so the game's OWN lobby
    layer, not our backend, is the rich inbound channel. Its OnFailure (FEmptyOnlineDelegate)
    carries nothing at all; that it fired is the entire message."""
    g = G()
    g.custom("bit_ok", "OnBitOk", [P("bSuccess", "bool")])
    g.custom("bit_404", "OnBit404", [P("bSuccess", "bool")])
    g.custom("found", "OnLobbiesFound",
             [P("SearchResults", "struct", struct=SEARCH_RESULT, array=True)])
    # FEmptyOnlineDelegate takes NOTHING - the failure tells you only that it failed.
    g.custom("findfail", "OnLobbiesFailed")

    # chlobby-7's late arms. LateFind is not a delegate handler at all - it is the TIMER's target,
    # named by string in K2_SetTimer, which is why it has to be a real declared custom event.
    # chlobby-13's RING. Two of everything, because the loop alternates between two physical
    # delay nodes and each needs its own handler - see late_probe for why one node cannot do it.
    g.custom("delaya", "OnDelayA", [P("bSuccess", "bool")])
    g.custom("delayb", "OnDelayB", [P("bSuccess", "bool")])
    g.custom("founda", "OnFoundA",
             [P("SearchResults", "struct", struct=SEARCH_RESULT, array=True)])
    g.custom("faila", "OnFailA")
    g.custom("foundb", "OnFoundB",
             [P("SearchResults", "struct", struct=SEARCH_RESULT, array=True)])
    g.custom("failb", "OnFailB")

    # chlobby-11's join. BOTH are bare: UBodycamJoinLobby declares OnSuccess and OnFailure as
    # FEmptyOnlineDelegate, so neither outcome carries a payload - and a successful join TRAVELS
    # this client away, so OnJoinOk may well never get to report before its world is torn down.
    # chlobby-17: the host arm hangs off a server-held delay, not off BeginPlay directly.
    g.custom("hostdelay", "OnHostDelay", [P("bSuccess", "bool")])
    g.custom("hostdelay2", "OnHostDelay2", [P("bSuccess", "bool")])
    # chlobby-18: CreateLobby's own delegates. FEmptyOnlineDelegate, so neither carries a payload.
    # chlobby-21's read-only peek at a real lobby
    g.custom("peekfound", "OnPeekFound",
             [P("SearchResults", "struct", struct=SEARCH_RESULT, array=True)])
    g.custom("peekfailed", "OnPeekFailed")

    g.custom("lobbymade", "OnLobbyMade")
    g.custom("lobbyfailed", "OnLobbyFailed")
    # chlobby-19: the pre-clean. Both outcomes lead to the same place, so both are handlers.
    g.custom("wiped", "OnLobbyWiped")
    g.custom("wipefailed", "OnLobbyWipeFailed")

    g.custom("joinok", "OnJoinOk")
    g.custom("joinfail", "OnJoinFail")
    return g.json()


def _report(g, p, event_name, bool_pin):
    """One probe carrying a bool in `timestamp`, so the answer is readable in the log."""
    g.call(p + "s", STR, "Conv_BoolToString")
    g.link((bool_pin, p + "s.InBool"))
    g.call(p + "send", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": event_name,
        "Storefront": "bit", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link((p + "s.ReturnValue", p + "send.Timestamp"))
    return p + "send"


def bit_probe(g):
    """Can the game read the one bit, and does it actually VARY?

    Everything in the autojoin design that is not a lobby attribute rides on this single bool: the
    host asking "does this player belong in my match", a joiner asking "is this lobby mine". If the
    bit is always true it is not an answer, it is a heartbeat, and the design has to change again.

    So: two calls, one to a route that EXISTS (200) and one to a route that does not (404), each
    reporting what it was told. No server change is needed for that - a 404 is free.

      ch_bit_ok   timestamp=true   and   ch_bit_404  timestamp=false   -> the bit tracks the reply
      both true                                                        -> bSuccess is transport-only
    """
    g.call("okcall", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_bit_ask_ok",
        "Storefront": "bit", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.n("okdel", "createevent", func="OnBitOk")
    # the pin is OutputDelegate, not "delegate" (UK2Node_CreateDelegate's own naming)
    g.link(("okdel.OutputDelegate", "okcall.OnResponse"))

    g.call("misscall", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_404, "BearerToken": "chlobby", "IP": "", "EventName": "ch_bit_ask_404",
        "Storefront": "bit", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.n("missdel", "createevent", func="OnBit404")
    g.link(("missdel.OutputDelegate", "misscall.OnResponse"))

    g.existing("bit_ok", "OnBitOk")
    g.chain("bit_ok", _report(g, "ro_", "ch_bit_ok", "bit_ok.bSuccess"))
    g.existing("bit_404", "OnBit404")
    g.chain("bit_404", _report(g, "rm_", "ch_bit_404", "bit_404.bSuccess"))
    return ["okcall", "misscall"]


def find_probe(g):
    """Can a player standing in the SHOOTING RANGE see other people's lobbies?

    This is the game's own server-browser call, which is why it needs no friendship. What comes
    back is the thing that matters - each result carries the lobby's whole attribute map, and that
    map is the only way anything richer than a bool reaches a game client (docs/autojoin.md 4).

    There is NO such thing as an empty search: the editor refuses an unwired map pin outright
    ("Map inputs must have an input wired into them", 2026-09-14), so this searches for our own
    constant tag. Whether we can see SOMEBODY ELSE'S lobby is a two-machine question and cannot be
    answered alone whatever we search for.

    WHY THIS IS FOUR PROBES AND NOT ONE. chlobby-5 asked a single question - "how many lobbies?" -
    and got SILENCE: `ch_lobby_alive`, `ch_bit_ask_ok`, `ch_bit_404`, `ch_bit_ok` all arrived and
    `ch_find_result` never did. Silence is the one answer that means nothing, because four quite
    different faults produce it. So the chain now reports at every point it could break, and the
    log names the fault instead of leaving us to guess:

      no ch_find_ask                      the exec never reached here (fan-out pin unwired)
      ch_find_ask, no ch_find_proxy       the FindLobbies call itself aborted the chain
      ch_find_proxy=false                 the static call returned null; binds and Activate are
                                          no-ops and nothing downstream can ever fire
      ch_find_proxy=true, then silence    it activated and never answered - a menu-context limit,
                                          which is the only finding that would change the design
      ch_find_failed                      it answered, badly. The likeliest single outcome: an
                                          online-subsystem wrapper that reports "no results" as a
                                          failure rather than an empty success. Harmless - a host
                                          stamping the tag turns it into ch_find_result.
      ch_find_result=<n>                  it answered. ANY number, zero included, is a PASS.

    Returns the id of the node to hang off the fan-out.
    """
    # A Make Map's pins are WILDCARDS until something is wired in, so the key cannot be a pin
    # default - it comes from a literal-string node (the same fix the CH_MATCH write needed).
    g.call("qkey", SYS, "MakeLiteralString", {"Value": QUEUE_KEY})
    g.call("qval", TYPES, "MakeStringLobbyAttribute", {"Value": QUEUE_VALUE})
    g.n("qmap", "makemap", count=1)
    g.link(("qkey.ReturnValue", "qmap.Key 0"), ("qval.ReturnValue", "qmap.Value 0"))

    g.call("find", FIND_LOBBIES, "FindLobbies", {"MaxResults": "25", "Timeout": "10.0"})
    g.link(("qmap.out", "find.SearchSettings"))
    # Bind BOTH delegates on the returned proxy BEFORE activating it, or the search can finish first.
    g.n("founddel", "createevent", func="OnLobbiesFound")
    g.n("bind", "adddelegate", delegate="OnSuccess", **{"class": FIND_LOBBIES})
    g.link(("find.ReturnValue", "bind.self"), ("founddel.OutputDelegate", "bind.Delegate"))
    g.n("faildel", "createevent", func="OnLobbiesFailed")
    g.n("bindfail", "adddelegate", delegate="OnFailure", **{"class": FIND_LOBBIES})
    g.link(("find.ReturnValue", "bindfail.self"), ("faildel.OutputDelegate", "bindfail.Delegate"))
    g.call("act", ASYNC_BASE, "Activate")
    g.link(("find.ReturnValue", "act.self"))
    g.chain("find", "bind", "bindfail", "act")

    # MARKER 1 - "the exec chain got here at all". Without it, silence is unreadable: a fan-out pin
    # that was never wired and a search that never answers look exactly the same in the log.
    g.call("askf", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_find_ask",
        "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})

    # MARKER 2 - did the static call hand back a proxy? A null return would make the two binds and
    # Activate no-ops and nothing downstream could ever fire.
    g.call("pvalid", SYS, "IsValid")
    g.link(("find.ReturnValue", "pvalid.Object"))
    g.call("pvalidS", STR, "Conv_BoolToString")
    g.link(("pvalid.ReturnValue", "pvalidS.InBool"))
    g.call("psend", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_find_proxy",
        "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("pvalidS.ReturnValue", "psend.Timestamp"))

    # Report LAST, after both binds and Activate have gone out, so the report can never be what
    # delays them - SendAttributionEvent is latent and nothing may sit downstream of it.
    g.chain("act", "psend")

    # Ask first, then search. Both hang off a Sequence for the usual latent reason.
    g.seq("askseq", 2)
    g.link(("askseq.then_0", "askf.exec"), ("askseq.then_1", "find.exec"))

    g.existing("found", "OnLobbiesFound")
    # array=True -> UK2Node_CallArrayFunction; the array libraries take a WILDCARD TargetArray and
    # the plain call node cannot type it (the pattern every other graph here uses)
    g.call("len", ARR, "Array_Length", array=True)
    g.link(("found.SearchResults", "len.TargetArray"))
    g.call("lenS", STR, "Conv_IntToString")
    g.link(("len.ReturnValue", "lenS.InInt"))
    g.call("fsend", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_find_result",
        "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("lenS.ReturnValue", "fsend.Timestamp"))
    g.chain("found", "fsend")

    g.existing("findfail", "OnLobbiesFailed")
    g.call("ffsend", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_find_failed",
        "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.chain("findfail", "ffsend")
    return "askseq"


def _search_arm(g, p, key, value, found_event, fail_event, ask_event, proxy_event,
                tag=None):
    # `tag` defaults to BUILD_TAG so every existing caller is byte-identical; GM_CHPeek passes
    # PEEK_TAG so a peek run is distinguishable from a host run in the probe log.
    """One complete FindLobbies: build the one-key filter, bind BOTH delegates, activate.

    Identical in shape to find_probe's arm - the only differences are the key/value searched for and
    which custom events the results land on. Returns the node whose exec pin STARTS the arm.

    The map is built from a literal-string node rather than a pin default because a Make Map's pins
    are WILDCARDS until something is wired in (the same fix the CH_MATCH write needed), and an
    unwired Map pin is refused outright by the editor.

    EVERY BREAK POINT IS INSTRUMENTED, and chlobby-7 is why. Its late arms carried no markers at
    all, so when the log came back with no ch_late_* events whatsoever there was no way to tell
    "the timer never fired" from "the timer fired and the search died without calling either
    delegate". That is precisely the silence chlobby-6 was built to eliminate for arm A, and the
    lesson had to be learned twice. So, exactly as find_probe does:

      ask_event    exec physically reached this arm (for a timed arm, the timer FIRED)
      proxy_event  timestamp=true/false: did the static call hand back a non-null proxy? A null
                   one makes both binds and Activate no-ops, so nothing downstream can ever fire
    """
    # MARKER 1, on its own Sequence pin. It cannot be chained in front of the search: the report is
    # SendAttributionEvent, which is LATENT, and its exec output waits for the HTTP round trip - so
    # chaining would delay the very thing being measured.
    g.seq(p + "seq", 2)
    g.call(p + "ask", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": ask_event,
        "Storefront": "find", "FirstSessionTimestamp": (tag or BUILD_TAG), "IsFirstGameOpen": "false"})

    g.call(p + "key", SYS, "MakeLiteralString", {"Value": key})
    g.call(p + "val", TYPES, "MakeStringLobbyAttribute", {"Value": value})
    g.n(p + "map", "makemap", count=1)
    g.link((p + "key.ReturnValue", p + "map.Key 0"), (p + "val.ReturnValue", p + "map.Value 0"))

    g.call(p + "find", FIND_LOBBIES, "FindLobbies", {"MaxResults": "25", "Timeout": "10.0"})
    g.link((p + "map.out", p + "find.SearchSettings"))
    # Bind BOTH delegates BEFORE Activate, or a fast search can finish before anything is listening.
    g.n(p + "fd", "createevent", func=found_event)
    g.n(p + "bind", "adddelegate", delegate="OnSuccess", **{"class": FIND_LOBBIES})
    g.link((p + "find.ReturnValue", p + "bind.self"), (p + "fd.OutputDelegate", p + "bind.Delegate"))
    g.n(p + "fl", "createevent", func=fail_event)
    g.n(p + "bindf", "adddelegate", delegate="OnFailure", **{"class": FIND_LOBBIES})
    g.link((p + "find.ReturnValue", p + "bindf.self"), (p + "fl.OutputDelegate", p + "bindf.Delegate"))
    g.call(p + "act", ASYNC_BASE, "Activate")
    g.link((p + "find.ReturnValue", p + "act.self"))
    g.chain(p + "find", p + "bind", p + "bindf", p + "act")

    # MARKER 2, reported LAST - after both binds and Activate have gone out - so the latent report
    # can never be what delays them.
    g.call(p + "valid", SYS, "IsValid")
    g.link((p + "find.ReturnValue", p + "valid.Object"))
    g.call(p + "valids", STR, "Conv_BoolToString")
    g.link((p + "valid.ReturnValue", p + "valids.InBool"))
    g.call(p + "psend", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": proxy_event,
        "Storefront": "find", "FirstSessionTimestamp": (tag or BUILD_TAG), "IsFirstGameOpen": "false"})
    g.link((p + "valids.ReturnValue", p + "psend.Timestamp"))
    g.chain(p + "act", p + "psend")

    g.link((p + "seq.then_0", p + "ask.exec"), (p + "seq.then_1", p + "find.exec"))
    return p + "seq"


def _report_count(g, p, event_name, results_pin, tag=None):
    """Report an array length in `timestamp`. Array_Length is PURE, so it never joins the exec chain
    (array=True -> UK2Node_CallArrayFunction: the array libraries take a WILDCARD TargetArray and a
    plain call node cannot type it)."""
    g.call(p + "len", ARR, "Array_Length", array=True)
    g.link((results_pin, p + "len.TargetArray"))
    g.call(p + "lens", STR, "Conv_IntToString")
    g.link((p + "len.ReturnValue", p + "lens.InInt"))
    g.call(p + "snd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": event_name,
        "Storefront": "find", "FirstSessionTimestamp": (tag or BUILD_TAG), "IsFirstGameOpen": "false"})
    g.link((p + "lens.ReturnValue", p + "snd.Timestamp"))
    return p + "snd"


def _report_bare(g, p, event_name, tag=None):
    """A failure carries no payload: FEmptyOnlineDelegate has no parameters, so that it fired IS the
    whole message."""
    g.call(p + "snd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": event_name,
        "Storefront": "find", "FirstSessionTimestamp": (tag or BUILD_TAG), "IsFirstGameOpen": "false"})
    return p + "snd"


def _join_arm(g, p, results_pin, loop_target=None, handlers=True):
    """Join the FIRST lobby a search returned - the other half of the chain (chlobby-11).

    WHY THIS CAN BE TESTED ALONE. JoinLobby takes an FBodycamLobbySearchResult BY VALUE, and that
    is exactly one element of the array FindLobbies already hands back: no lobby id parsing, no
    connection string, no travel URL of ours. So hanging it off arm C - {Access:"false"}, the query
    that returns 25 real public lobbies today - exercises find->join END TO END on one machine. The
    second machine is only ever needed for whether an attribute WE wrote is visible remotely, which
    is a different question and is arm B's.

    WHAT SUCCESS LOOKS LIKE, AND WHY IT IS NOT A PROBE. A successful join TRAVELS this client to the
    host's map, which tears down this world - and SendAttributionEvent is latent, so ch_join_ok can
    easily die with the world that was about to send it. Its ABSENCE therefore proves nothing. The
    real evidence is Sam's screen: he is either in somebody's match or he is not. ch_join_proxy is
    the marker that carries the weight here, because it fires BEFORE any travel can start.

    GUARDED. A search can legitimately return zero, and Array_Get past the end of an empty array is
    not a question worth asking of cooked bytecode. Length > 0 or nothing happens, and the empty
    case says so out loud (ch_join_skip) rather than going quiet - the chlobby-7 lesson.

    NO STATE, SO NO GATE. Rule 1 forbids variables, so there is no "already joined" flag to set:
    this arm re-arms on every BeginPlay, and BeginPlay fires repeatedly. That is fine for a
    deliberate one-shot test build and is NOT fine to leave installed. The pak comes back out.
    """
    g.call(p + "len", ARR, "Array_Length", array=True)
    g.link((results_pin, p + "len.TargetArray"))
    g.call(p + "any", MATH, "Greater_IntInt", {"B": "0"})
    g.link((p + "len.ReturnValue", p + "any.A"))
    g.branch(p + "br")
    g.link((p + "any.ReturnValue", p + "br.condition"))
    if loop_target:
        # EMPTY -> GO ROUND AGAIN. No extra probe on this edge: one iteration already announces
        # itself five times over (delay ask/done, search ask/proxy/result), and at a 4 s poll every
        # avoidable request is 15 more per minute.
        g.link((p + "br.else", "%s.exec" % loop_target))
    else:
        g.link((p + "br.else", "%s.exec" % _report_bare(g, p + "skip", "ch_join_skip")))

    # Marker on its own Sequence pin, never in front of the join: SendAttributionEvent is LATENT.
    g.seq(p + "seq", 2)
    g.link((p + "br.then", p + "seq.exec"))
    g.call(p + "ask", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_join_ask",
        "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})

    g.call(p + "first", ARR, "Array_Get", {"Index": "0"}, array=True)
    g.link((results_pin, p + "first.TargetArray"))
    g.call(p + "pc", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.call(p + "join", JOIN_LOBBY, "JoinLobby")
    g.link((p + "first.Item", p + "join.Lobby"), (p + "pc.ReturnValue", p + "join.PlayerController"))

    # Bind BOTH delegates BEFORE Activate - same reason as the search arms.
    g.n(p + "jd", "createevent", func="OnJoinOk")
    g.n(p + "bind", "adddelegate", delegate="OnSuccess", **{"class": JOIN_LOBBY})
    g.link((p + "join.ReturnValue", p + "bind.self"), (p + "jd.OutputDelegate", p + "bind.Delegate"))
    g.n(p + "jf", "createevent", func="OnJoinFail")
    g.n(p + "bindf", "adddelegate", delegate="OnFailure", **{"class": JOIN_LOBBY})
    g.link((p + "join.ReturnValue", p + "bindf.self"), (p + "jf.OutputDelegate", p + "bindf.Delegate"))
    g.call(p + "act", ASYNC_BASE, "Activate")
    g.link((p + "join.ReturnValue", p + "act.self"))
    g.chain(p + "join", p + "bind", p + "bindf", p + "act")

    # Reported LAST, after Activate has gone out, so the latent report cannot delay the join.
    g.call(p + "valid", SYS, "IsValid")
    g.link((p + "join.ReturnValue", p + "valid.Object"))
    g.call(p + "valids", STR, "Conv_BoolToString")
    g.link((p + "valid.ReturnValue", p + "valids.InBool"))
    g.call(p + "psend", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_join_proxy",
        "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link((p + "valids.ReturnValue", p + "psend.Timestamp"))
    g.chain(p + "act", p + "psend")

    g.link((p + "seq.then_0", p + "ask.exec"), (p + "seq.then_1", p + "join.exec"))

    # Both outcomes are bare: FEmptyOnlineDelegate carries no parameters either way. Only ONE copy
    # of the arm may build the handler nodes - an `existing` node for a custom event that already
    # has one is an error, and the ring has two copies of this arm.
    if handlers:
        g.existing(p + "ok", "OnJoinOk")
        g.chain(p + "ok", _report_bare(g, p + "okr", "ch_join_ok"))
        g.existing(p + "no", "OnJoinFail")
        g.chain(p + "no", _report_bare(g, p + "nor", "ch_join_fail"))
    return p + "br"


def late_probe(g):
    """The QUEUE: poll for our match lobby until it appears, then join it (chlobby-12).

    WHAT TONIGHT SETTLED (2026-09-14, chlobby-11, one clean run).
      FindLobbies works from the lobby world               ch_game_result=25
      empty is delivered as SUCCESS with a 0-length array  ch_late_result=0
      the readiness race is real                           arm A failed after a 2.1 s search at
                                                           BeginPlay+eps; the same shape at +4 s
                                                           succeeded
      JoinLobby takes a search result and reports success  ch_join_proxy=true -> ch_join_ok
    So the mechanism is not in question any more. What was missing is a CLOCK.

    WHY THIS HAS TO BE A LOOP, AND WHY THE LOOP IS MADE OF HTTP.
    A queue has to keep looking. Three ways to keep looking were tried or considered:

      timers          DEAD in this world. K2_SetTimer's cooked bytecode is provably correct and
                      ch_timer_fired never arrived. Delay and RetriggerableDelay run off the same
                      world timer/latent managers, so they are dead for the same reason - and that
                      predicts Actor Tick is too.
      BeginPlay       fires once per level load. Tested 2026-09-14: a full pass through every menu
                      and settings screen produced NO second ch_lobby_alive in 5.6 minutes. (The
                      older "three times in three minutes while moving around the menus" note was
                      wrong - those were three launches.) Forcing it with OpenLevel would work and
                      is not shippable: a loading screen over the player's menu, and with no
                      variables there is no counter to ever stop it.
      HTTP response   WORKS. ch_delay_ask -> ch_delay_done measured 4.069 s against a 4 s
                      server-held route, every run. It is dispatched by the engine's HTTP module on
                      the game thread, NOT by the world - which is exactly why it survives where
                      everything world-driven does not.

    So the clock is the only one this world has. Chain the response back into the call and the
    queue runs itself:

        delay -> search {CH_MATCH} -> 0 results -> delay -> search -> ...
                                   -> found     -> join

    No variables, no timers, no re-entry of BeginPlay. Each hop is just the next HTTP response.

    RE-ENTERING A LATENT NODE. dlycall is SendAttributionEvent, which is latent, and the loop drives
    its exec input again from inside its own response handler. That is safe only because the
    previous latent action has COMPLETED by then - we are being called by its completion. If the
    engine drops the second call anyway, the log says so precisely: one ch_delay_ask, one
    ch_late_result, and then silence.

    COST. Five requests per iteration (delay ask/done, search ask/proxy/result) at a 4 s poll is
    ~75/min per player. Fine for a bounded two-machine test, not fine as a default. The interval
    lives on the server (PROBE_SLOW_SECONDS), so slowing it down is one env var and a redeploy -
    no editor build, no cook, no pak, nobody standing in a game.

    ARM C IS GONE. {Access:"false"} existed to prove a search could return anything at all, and it
    did (25). Keeping it would have joined a RANDOM public lobby on every poll.

    Returns the node to hang off BeginPlay's fan-out.
    """
    # THE DELAY, AND WHY IT IS NOT A TIMER (chlobby-8 -> chlobby-9, 2026-09-14).
    #
    # K2_SetTimer does not fire on the lobby GameMode. That is not a wiring mistake: the cooked
    # bytecode was disassembled and is provably correct -
    #     05f2: CallMath K2_SetTimer@KismetSystemLibrary(Self, String 'LateFind', Float 25.0, False, ...)
    # reached by 000f: PushExecutionFlow -> 05e0 off BeginPlay's Sequence (pushed first, so it runs
    # last: then_4), with LateFind a real cooked function entering the ubergraph at offset 2480 whose
    # first act is the ch_timer_fired probe. ch_timer_fired never arrived in three minutes, from a
    # run where BeginPlay fired exactly once. Nothing in the graph is left to fix - timers simply do
    # not run here.
    #
    # So the delay moves to the one late-firing mechanism this context has PROVEN, every build:
    # SendAttributionEvent's response delegate. ch_bit_ok has come back 250-400 ms after its call in
    # every run, and bSuccess demonstrably tracks the HTTP status rather than transport. We therefore
    # call a route that deliberately holds its reply open (server.cjs handleProbeSlow), and hang the
    # late search off the response. The wait happens on OUR server, where it can be retuned with a
    # redeploy instead of an editor build, a cook, a pak and Sam standing in a game.
    #
    # It also measures itself: ch_delay_ask lands immediately, ch_delay_done only when the reply
    # comes back. An ask with no done means the game's own HTTP timeout is shorter than the delay,
    # which is exactly the next thing we would need to know.
    # THE RING. Two physical delay nodes; each one's completion drives THE OTHER.
    #
    # chlobby-12 used a single node and looped its completion back into itself. The exec re-entry
    # WORKED - the log shows ch_delay_ask firing again 2 ms after ch_late_result - and the request
    # reached the server. What never came back was the SECOND completion. A latent action is keyed
    # by (CallbackTarget, UUID), and chlobby-12 added a new action with the SAME uuid from inside
    # that very action's callback, while the manager was still ticking it. It is dropped silently.
    #
    # Alternating two nodes removes exactly that: node A is only ever re-entered from B's callback,
    # by which time A's own action has long completed and been retired. Nothing is ever re-entered
    # from its own completion.
    #
    # THE COST OF HAVING NO VARIABLES. "Which node is next" is one bit of state, and rule 1 forbids
    # storing it - so the alternation cannot be computed, it has to be WIRED. That means two copies
    # of the whole hop: two delay calls, two search arms, two join arms. They are identical except
    # for where they point, and the pairing is what makes the log readable:
    #
    #     ch_delay_a -> ch_done_a -> ch_result_a -> ch_delay_b -> ch_done_b -> ch_result_b -> ...
    #
    # a/b/a/b means the ring is turning. Stopping after ch_delay_b would mean two nodes are not
    # enough and the action is being refused for some other reason entirely.
    for me, other, ev, done_ev, found, failed, ask_ev, proxy_ev, res_ev, jp, handlers in (
            ("dlya", "dlyb", "ch_delay_a", "ch_done_a", "OnFoundA", "OnFailA",
             "ch_sa_ask", "ch_sa_proxy", "ch_result_a", "jna", True),
            ("dlyb", "dlya", "ch_delay_b", "ch_done_b", "OnFoundB", "OnFailB",
             "ch_sb_ask", "ch_sb_proxy", "ch_result_b", "jnb", False)):
        g.call(me, HTTP_LIB, "SendAttributionEvent", {
            "URL": PROBE_SLOW_URL, "BearerToken": "chlobby", "IP": "", "EventName": ev,
            "Storefront": "find", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
        handler = "OnDelayA" if me == "dlya" else "OnDelayB"
        g.n(me + "del", "createevent", func=handler)
        g.link((me + "del.OutputDelegate", me + ".OnResponse"))

        g.existing(me + "done", handler)
        g.seq(me + "seq", 2)
        g.chain(me + "done", me + "seq")
        g.link((me + "seq.then_0",
                "%s.exec" % _report(g, me + "_r", done_ev, me + "done.bSuccess")))
        g.link((me + "seq.then_1", "%s.exec" % _search_arm(
            g, me + "s", MATCH_KEY, MATCH_VALUE, found, failed, ask_ev, proxy_ev)))

        # FOUND: report the count, then join if there is anything to join, else hand the ring on.
        g.existing(me + "f", found)
        g.seq(me + "fseq", 2)
        g.chain(me + "f", me + "fseq")
        g.link((me + "fseq.then_0",
                "%s.exec" % _report_count(g, me + "c", res_ev, me + "f.SearchResults")),
               (me + "fseq.then_1", "%s.exec" % _join_arm(
                   g, jp, me + "f.SearchResults", loop_target=other, handlers=handlers)))

        # FAILED: report and hand the ring on, so one refused search never ends the queue.
        g.existing(me + "x", failed)
        g.seq(me + "xseq", 2)
        g.chain(me + "x", me + "xseq")
        g.link((me + "xseq.then_0", "%s.exec" % _report_bare(g, me + "xr", res_ev + "_failed")),
               (me + "xseq.then_1", "%s.exec" % other))

    # BeginPlay enters the ring at A; from there it turns on its own.
    return "dlya"


def gi_stub_signature(name):
    """The stand-in's declaration of a BodycamGI_C function. SIGNATURE IS EVERYTHING HERE.

    A cooked `CallFunction` names the function by import and pushes its arguments per the signature
    the caller was COMPILED against. At runtime that import resolves to the REAL BodycamGI_C
    function - so if our declaration disagrees with the game's, we push the wrong bytes into a real
    function. That is not a failed call, it is memory corruption in someone's game.

    Which is exactly why the first one tried takes NO INPUTS. Disassembled from the shipped
    BodycamGI (2026-09-14):

        ===== function GetCurrentLevel (export 33) flags=0xc420000
            param out NameProperty CurrentLevelName

    One output, no inputs, and its whole body is GetCurrentLevelName + Conv_StringToName. There is
    nothing to get wrong on the way in, and nothing it can change on the way out.


    EVERY signature below was disassembled from the shipped BodycamGI, never inferred:

        UpdateGamemode (export 148)               param ByteProperty Gamemode [enum=GameMode]
        HostPartyInLobby (export 71)              (no params)
        UpdateSessionAndTravelToMap (export 157)  param StrProperty TargetMap
        GetCurrentLevel (export 33)               param out NameProperty CurrentLevelName

    `Gamemode` is declared here as a PLAIN byte rather than the game's enum. The underlying
    property is a ByteProperty either way, so the pushed byte is identical - and it saves mirroring
    /Game/GM/DATA/Enum/GameMode just to name a number we already know."""
    g = G()
    if name == "GetCurrentLevel":
        g.entry()
        g.result(params=[P("CurrentLevelName", "name")])
    elif name == "UpdateGamemode":
        g.entry(params=[P("Gamemode", "byte")])
        g.result()
    elif name == "HostPartyInLobby":
        g.entry()
        g.result()
    elif name == "UpdateSessionAndTravelToMap":
        g.entry(params=[P("TargetMap", "string")])
        g.result()
    else:
        raise KeyError(name)
    return g.json()


def gi_probe(g):
    """chlobby-15: call the GAME's own BodycamGI_C.GetCurrentLevel through a stand-in.

    WHY THIS IS THE WHOLE QUESTION. Step 5 wants to call the game's host flow
    (UpdateSessionAndTravelToMap, TravelToMap, MakeCreateLobbyParams) rather than reimplement it -
    reimplementing would miss the UpdateLobby {Map, Gamemode, Ingame, Access, Bots} write that the
    server browser filters on. But the stand-in pattern is only PROVEN for inheritance: GM_CHLobby
    is a child of a stand-in GM_Host and the cooked super import resolves to the real class. Whether
    the same trick works for a CALL - a function import on a class we only stubbed - has never been
    tested. This tests it, with the safest function in the game.

    Reports:
      timestamp  the level name the GAME returned, or "" if the cast failed
      platform   did the cast to BodycamGI_C succeed

    A cast that fails means the stand-in did not resolve to the real class and the whole approach is
    dead. A cast that succeeds and a name that comes back means step 5 is unlocked.

    (It also finally delivers the level name chlobby-14 lost: GetCurrentLevelName is impure and was
    pruned there for having no exec connection.)
    """
    g.call("gi", GS_LIB, "GetGameInstance")
    g.cast("gic", GI_CLASS, pure=True)
    g.link(("gi.ReturnValue", "gic.cast_object"))
    g.call("gicS", STR, "Conv_BoolToString")
    g.link(("gic.cast_ok", "gicS.InBool"))

    # Only call through a cast that WORKED. Calling a function on a null object is the one way this
    # probe could do damage rather than just report.
    g.branch("gibr")
    g.link(("gic.cast_ok", "gibr.condition"))

    g.call("gilvl", GI_CLASS, "GetCurrentLevel")
    g.link(("gic.cast_result", "gilvl.self"), ("gibr.then", "gilvl.exec"))
    g.call("gilvlS", STR, "Conv_NameToString")
    g.link(("gilvl.CurrentLevelName", "gilvlS.InName"))

    g.call("gipid", ONLINE, "GetPlatformUserNetId")
    g.call("gisnd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_gi_call",
        "Storefront": "gi", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("gipid.ReturnValue", "gisnd.UserId"),
           ("gilvlS.ReturnValue", "gisnd.Timestamp"),
           ("gicS.ReturnValue", "gisnd.Platform"))
    g.chain("gilvl", "gisnd")

    # cast failed: say so rather than going quiet - the chlobby-7 lesson
    g.call("gixsnd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_gi_castfail",
        "Storefront": "gi", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("gicS.ReturnValue", "gixsnd.Platform"), ("gibr.else", "gixsnd.exec"))
    return "gibr"


# The fifteen keys MakeCreateLobbyParams writes, read out of its disassembly. chlobby-18/19 created
# a lobby carrying TWO of them, and the match exited after ~3 s - so these are almost certainly what
# it went looking for and did not find.
# ---------------------------------------------------------------- the lobby we advertise
# EVERY FORMAT BELOW WAS READ OFF A LIVE BODYCAM LOBBY (chlobby-21), not guessed. Six of them would
# have been written wrong from reasonable assumptions, and the conventions are not even
# self-consistent - Ingame is "1"/"0" while Bots and Access are "true"/"false".
#
#   observed:  Gamemode=GunGame  Map=Rome  MaxPlayers=N/A  Ingame=1  Build=V0.8.11.18805
#              GameID=2406770  Bots=true  Training=""  Name=tompearl6769  Platform=Windows
#              RankInteger=0  Access=false
#
# TWO ENTRIES ARE STILL INFERRED, and they are the ones to suspect first if this is rejected:
#   Map       stock lobbies carry a short name ("Rome"); ours is the level name BB5_Hospital
#   Gamemode  the observed value is a stock ENUMERATOR name. build_gamemode.py registers community
#             modes as NewEnumerator14 with display text "BB5", so the game may advertise ours as
#             "NewEnumerator14". "BB5" is the guess here; if the match still leaves, try the other.
LOBBY_ATTRS = [
    ("Gamemode", "BB5"),                 # INFERRED - see above
    ("Map", HOST_MAP_SHORT),             # INFERRED - see above
    ("MaxPlayers", "N/A"),               # a literal string, not a number
    ("Ingame", "0"),                     # "0" at creation; the observed 1 was a lobby mid-match
    ("Build", "V0.8.11.18805"),
    ("GameID", "2406770"),
    ("Bots", "false"),
    ("Training", ""),
    ("Platform", "Windows"),             # NOT SteamCore, which is what our own probes report
    ("RankInteger", "0"),
    ("Access", "false"),
    ("Password", ""),
    ("IpCountry", ""),
    (MATCH_KEY, MATCH_VALUE),            # our own tag, so a joiner can find exactly this match
]

# chlobby-21 measured the first twelve off a real lobby. The last four are chlobby-29's question,
# and PlayerSteamId is the one that matters: the joiner has to recognise ITS host among 25 strangers,
# and the only identity both machines can produce is the host's SteamID64 - the hub knows it offline
# from the match roster, and the host derives it at runtime from GetPlatformUserNetId. But the GAME
# writes that attribute as BreakSteamID(FSteamID) (disassembled out of the shipping
# MakeCreateLobbyParams, export 87, key at bytecode 0x3f5), and nobody has ever compared the two
# formats. Read it off somebody else's real lobby and the question is settled for good.
LOBBY_KEYS = ["Gamemode", "Map", "MaxPlayers", "Ingame",
              "Build", "GameID", "Bots", "Training",
              "Name", "Platform", "RankInteger", "Access",
              "PlayerSteamId", "Password", "IpCountry", "LobbyName"]


def _peek_group(g, p, result_pin, event_name, keys, tag=None):
    """Read four attributes off a real search result and pack them into one probe.

    GetStringAttribute(SearchResult, Key, OutString) takes the result and the key DIRECTLY - no map
    iteration, which is what makes this cheap. (FBodycamLobbyAttribute exposes no reflected members
    at all; it is readable only through UBodycamOnlineTypesLibrary, so this accessor is the only
    way in.)

    Four per event because SendAttributionEvent has six string fields and two are already spoken
    for: first_session_timestamp carries the build tag, event_name labels the group."""
    pins = ("UserId", "Platform", "Storefront", "Timestamp")
    g.call(p + "snd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": event_name,
        "FirstSessionTimestamp": (tag or BUILD_TAG), "IsFirstGameOpen": "false"})
    for i, key in enumerate(keys):
        nid = "%s%d" % (p, i)
        g.call(nid, TYPES, "GetStringAttribute", {"Key": key})
        g.link((result_pin, nid + ".SearchResult"))
        g.link((nid + ".OutString", "%s.%s" % (p + "snd", pins[i])))
    return p + "snd"


def peek_probe(g):
    """chlobby-21: ask a REAL lobby what a Bodycam lobby looks like. Read-only; hosts nothing.

    WHY. chlobby-18 created a lobby with {CH_MATCH, Access} and landed in Bodybomb/Hospital - then
    the match played the LEAVE animation after ~3 s and the game died. Disassembling
    MakeCreateLobbyParams showed the game writes FIFTEEN attributes. We wrote two. The match almost
    certainly went looking for Map, Gamemode, MaxPlayers, Ingame, Build, GameID and found nothing.

    The next build has to supply those - and every VALUE would otherwise be a guess. Is Gamemode
    "BB5", "Bodybomb 5v5", or "14"? What format is Build? So: stop guessing. FindLobbies already
    returns 25 real lobbies from the shooting range (chlobby-9) and GetStringAttribute reads any key
    straight off a search result. The game will simply tell us.

    This also happens to be the ONLY useful thing testable right now: an orphaned lobby from an
    earlier crash is making CreateLobby fail every launch, and this build never calls it.
    """
    # OnHostDelay2 and PROBE_WAIT_URL, NOT the host's pair. Sharing OnHostDelay would have put this
    # read-only measurement behind the travel permit, so it would only ever run on the one machine
    # that is about to host - and never while we are trying to measure anything.
    g.call("pkdly", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_WAIT_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_peek_wait",
        "Storefront": "peek", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.n("pkdlyd", "createevent", func="OnHostDelay2")
    g.link(("pkdlyd.OutputDelegate", "pkdly.OnResponse"))
    g.existing("hostdelay2", "OnHostDelay2")

    # the same query that returned 25 real lobbies in chlobby-9
    g.link(("hostdelay2.then", "%s.exec" % _search_arm(
        g, "pk", ACCESS_KEY, ACCESS_VALUE, "OnPeekFound", "OnPeekFailed",
        "ch_peek_ask", "ch_peek_proxy")))

    # THE SECOND QUESTION, on the same clock: does the backend actually FILTER on a key that is not
    # one of its own base four? The joiner's whole design assumes it can search for the host, so if
    # extra keys are ignored the joiner must fall back to filtering client-side (which the
    # CompareStringAttribute guard does anyway, and which the ForEachLoop makes possible).
    # Compare the count against arm A's: fewer means it filtered, equal means it ignored the key.
    # 'Map' is a key the GAME itself writes and searches on (the GI ubergraph passes {'Map': Level}
    # into MakeLobbySearchParams at 0x11e3), so a zero here is a real answer, not a broken query.
    g.link(("hostdelay2.then", "%s.exec" % _search_arm(
        g, "fk", "Map", FILTER_TEST_VALUE, "OnFoundA", "OnFailA",
        "ch_filter_ask", "ch_filter_proxy")))
    g.existing("founda", "OnFoundA")
    g.chain("founda", _report_count(g, "fkc", "ch_filter_count", "founda.SearchResults"))
    g.existing("faila", "OnFailA")
    g.chain("faila", _report_bare(g, "fkno", "ch_filter_failed"))

    g.existing("peekfound", "OnPeekFound")
    g.call("pklen", ARR, "Array_Length", array=True)
    g.link(("peekfound.SearchResults", "pklen.TargetArray"))
    g.call("pkany", MATH, "Greater_IntInt", {"B": "0"})
    g.link(("pklen.ReturnValue", "pkany.A"))
    g.branch("pkbr")
    g.link(("pkany.ReturnValue", "pkbr.condition"))
    g.chain("peekfound", "pkbr")
    g.link(("pkbr.else", "%s.exec" % _report_bare(g, "pkempty", "ch_peek_empty")))

    g.call("pkfirst", ARR, "Array_Get", {"Index": "0"}, array=True)
    g.link(("peekfound.SearchResults", "pkfirst.TargetArray"))

    # three groups of four, each on its own Sequence pin: SendAttributionEvent is LATENT, so
    # chaining them would make the first hold up the other two forever (the chlobby-2 lesson).
    g.seq("pkgrp", 1 + len(LOBBY_KEYS) // 4)
    g.link(("pkbr.then", "pkgrp.exec"))
    g.link(("pkgrp.then_0",
            "%s.exec" % _report_count(g, "pkc", "ch_peek_count", "peekfound.SearchResults")))
    for gi in range(len(LOBBY_KEYS) // 4):
        keys = LOBBY_KEYS[gi * 4:gi * 4 + 4]
        node = _peek_group(g, "pk%d_" % gi, "pkfirst.Item", "ch_peek_%d" % gi, keys)
        g.link(("pkgrp.then_%d" % (gi + 1), "%s.exec" % node))

    g.existing("peekfailed", "OnPeekFailed")
    g.chain("peekfailed", _report_bare(g, "pkno", "ch_peek_failed"))
    return "pkdly"


def host_probe(g):
    """chlobby-18: AUTO-HOST through the API, not through the game's graph.

    WHY THE PREVIOUS TWO ATTEMPTS FAILED, and why this one is shaped differently:

      chlobby-16/17  called BodycamGI_C::HostPartyInLobby(). That reached the game's real hosting
                     code - and CRASHED. The function is a thunk,
                     `LocalFinalFunction ExecuteUbergraph_BodycamGI(Int 3843)`, and those ubergraph
                     entry offsets are preceded by runs of PopExecutionFlow: entering one cold pops
                     an execution-flow stack nothing ever pushed. The fault was never TIMING (the
                     PlayerController gate reported true/true and the calls returned) - it was
                     jumping into the middle of somebody else's graph.
      command line   `Bodycam.exe <map>?listen`, both a short name and the real cooked path, via the
                     shim AND via the shipping exe directly (no Steam, no prompt). The game opened
                     the shooting range every time. Its own startup owns the map; a command line
                     does not override it.

    SO: touch no ubergraph. Every call below is either an async PROXY or plain engine code, and both
    kinds are already proven in this exact context -

      UBodycamFindLobbies  ran here, and fired OnSuccess from INSIDE another delegate's callback
      UBodycamJoinLobby    ran here, took its argument, reported success
      UGameplayStatics     engine code, no Bodycam graph involved

    UBodycamCreateLobby is the same BlueprintAsyncActionBase shape as the first two.

    AND IT SKIPS THE LOBBY. The game hosts by creating a lobby, opening LobbyHost "listen", then
    travelling to the map. We open the MATCH MAP as a listen server directly. Fewer steps, and none
    of them is a jump into game code.

    HONEST UNKNOWN: whether a lobby we build ourselves looks like a real Bodycam lobby. The game's
    flow writes {Map, Gamemode, Ingame, Access, Bots} and sets party id / match context through
    BodycamLobbyManager. We write CH_MATCH (so we can find it) and Access=false (the key that
    matched 25 real lobbies in chlobby-9). If it is created but nobody can see it in the browser,
    the next move is MakeCreateLobbyParams - which, unlike HostPartyInLobby, IS public - to get the
    game's own parameter map and feed THAT to CreateLobby.
    """
    g.call("hdly", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_SLOW_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_host_wait",
        "Storefront": "host", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.n("hdlyd", "createevent", func="OnHostDelay")
    g.link(("hdlyd.OutputDelegate", "hdly.OnResponse"))

    g.existing("hostdelay", "OnHostDelay")


    # only from the range (standalone). A client or an existing host must not be disturbed.
    g.call("hstd", SYS, "IsStandalone")
    g.branch("hbr")
    g.link(("hstd.ReturnValue", "hbr.condition"))
    g.chain("hostdelay", "hbr")
    g.link(("hbr.else", "%s.exec" % _report_bare(g, "hxs", "ch_host_notstandalone")))

    # THE FULL ATTRIBUTE SET (chlobby-22). chlobby-18/19 advertised TWO keys and the match left
    # after ~3 s; MakeCreateLobbyParams writes fifteen, and chlobby-21 read the real formats off a
    # live lobby rather than guessing them. Plus PlayerSteamId and Name, which are per-player and
    # so are wired from calls rather than literals.
    n_static = len(LOBBY_ATTRS)
    g.n("mkmap", "makemap", count=n_static + 2)
    for i, (key, val) in enumerate(LOBBY_ATTRS):
        g.call("mkk%d" % i, SYS, "MakeLiteralString", {"Value": key})
        g.call("mkv%d" % i, TYPES, "MakeStringLobbyAttribute", {"Value": val})
        g.link(("mkk%d.ReturnValue" % i, "mkmap.Key %d" % i),
               ("mkv%d.ReturnValue" % i, "mkmap.Value %d" % i))

    # PlayerSteamId and Name both come from this client. A real lobby carries the host's STEAM NAME
    # in Name (observed: "tompearl6769"), not a lobby title - we have no persona-name getter
    # stubbed, so the id stands in for both. If the match cares about Name specifically, that is a
    # thing to fix rather than a thing to guess.
    g.call("mkpid", ONLINE, "GetPlatformUserNetId")
    g.call("mkk_id", SYS, "MakeLiteralString", {"Value": "PlayerSteamId"})
    g.call("mkv_id", TYPES, "MakeStringLobbyAttribute")
    g.link(("mkpid.ReturnValue", "mkv_id.Value"))
    g.link(("mkk_id.ReturnValue", "mkmap.Key %d" % n_static),
           ("mkv_id.ReturnValue", "mkmap.Value %d" % n_static))

    g.call("mkk_nm", SYS, "MakeLiteralString", {"Value": "Name"})
    g.call("mkv_nm", TYPES, "MakeStringLobbyAttribute")
    g.link(("mkpid.ReturnValue", "mkv_nm.Value"))
    g.link(("mkk_nm.ReturnValue", "mkmap.Key %d" % (n_static + 1)),
           ("mkv_nm.ReturnValue", "mkmap.Value %d" % (n_static + 1)))

    g.call("mk", CREATE_LOBBY, "CreateLobby", {
        "LobbyName": "Community Hub match", "MaxPlayers": "10", "bUseLAN": "false",
        "bAllowInvites": "true", "bUsesPresence": "true", "bAllowJoinViaPresence": "true",
        "bAllowJoinViaPresenceFriendsOnly": "false", "bAntiCheatProtected": "false",
        "bUsesStats": "false", "bShouldAdvertise": "true",
        "bUseLobbiesVoiceChatIfAvailable": "true", "Timeout": "30.0"})
    g.link(("mkmap.out", "mk.SessionSettings"))
    g.n("mkd", "createevent", func="OnLobbyMade")
    g.n("mkb", "adddelegate", delegate="OnSuccess", **{"class": CREATE_LOBBY})
    g.link(("mk.ReturnValue", "mkb.self"), ("mkd.OutputDelegate", "mkb.Delegate"))
    g.n("mkfd", "createevent", func="OnLobbyFailed")
    g.n("mkfb", "adddelegate", delegate="OnFailure", **{"class": CREATE_LOBBY})
    g.link(("mk.ReturnValue", "mkfb.self"), ("mkfd.OutputDelegate", "mkfb.Delegate"))
    g.call("mkact", ASYNC_BASE, "Activate")
    g.link(("mk.ReturnValue", "mkact.self"))
    g.chain("mk", "mkb", "mkfb", "mkact")

    # NO PRE-CLEAN (chlobby-24). DestroyLobby was added in chlobby-19 on the stale-lobby theory of
    # 6f. That theory was wrong - it reported ch_wipe_none on EVERY run, so it never once had
    # anything to destroy - and it is now the prime suspect for something worse.
    #
    # GM_CHLobby calls Parent BeginPlay FIRST, which is where the lobby performs its Steam login and
    # session setup. Four seconds later we were telling the online layer to destroy "the session
    # this client is in" - which may be the session the LOBBY legitimately just created. Then
    # CreateLobby fails and the client has no session at all: Sam saw "waiting for connection"
    # followed by a black screen, twice, and only ever on builds that call DestroyLobby.
    #
    # chlobby-18, which had no pre-clean, reached a match twice. Back to that.
    # chlobby-25: NO LOBBY AT ALL. Straight to OpenLevel.
    #
    # CreateLobby has been refused for 21 minutes - through a Steam restart, an attribute change and
    # the removal of DestroyLobby - so it cannot be used to test anything right now. But it is not
    # what the open question is ABOUT. The question is whether the MATCH accepts our session or
    # exits after ~3 s with the leave animation, and a listen server does not need a Steam lobby to
    # answer that. Nobody can join this match; we are not testing joinability.
    #
    #   exits at ~3 s  -> the lobby was never the issue, and the fifteen attributes of chlobby-22/24
    #                     would not have helped. The rejection is about something else.
    #   stays          -> the lobby IS the issue, and specifically what it advertises.
    #
    # It also stops us poking CreateLobby, which lets any rate limit decay untouched.
    g.link(("hbr.then", "setsel.exec"))

    # BOTH destroy outcomes continue to the create. A failure here is the NORMAL case (there was no
    # lobby to destroy) and must not stop the chain - but they are reported apart, because "there
    # was a stale lobby and we cleared it" and "there was nothing" are different facts about the
    # world and we want to know which one we are in.
    g.seq("hseq", 2)
    g.link(("hseq.then_0", "%s.exec" % _report_bare(g, "hask", "ch_mk_ask")))
    g.link(("hseq.then_1", "mk.exec"))

    # proxy non-null? reported AFTER Activate so the latent probe cannot delay it
    g.call("mkv", SYS, "IsValid")
    g.link(("mk.ReturnValue", "mkv.Object"))
    g.call("mkvS", STR, "Conv_BoolToString")
    g.link(("mkv.ReturnValue", "mkvS.InBool"))
    g.call("mkp", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_mk_proxy",
        "Storefront": "host", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("mkvS.ReturnValue", "mkp.Timestamp"))
    g.chain("mkact", "mkp")

    # SUCCESS -> report, then open the match map as a listen server.
    # OpenLevel is deferred (the engine processes the map change at end of frame), so calling it
    # from inside this callback is safe - unlike entering an ubergraph, which is what crashed 17.
    g.existing("lobbymade", "OnLobbyMade")
    g.seq("mkseq", 2)
    g.chain("lobbymade", "mkseq")
    g.link(("mkseq.then_0", "%s.exec" % _report_bare(g, "mkok", "ch_mk_ok")))
    # STRAIGHT TO THE MATCH MAP (chlobby-23, reverting chlobby-20).
    #
    # chlobby-18/19 did this and RELIABLY reached Bodybomb on Hospital - twice. chlobby-20/22
    # changed it to open LobbyHost "listen" first, on the theory that the match rejected our session
    # because we skipped the stage where BodycamLobbyManager sets party id and match context. That
    # theory was never actually TESTED: every run since has died at CreateLobby instead, so the
    # change bought nothing and cost us the one configuration known to get into a match.
    #
    # So: back to the shape with results, and vary ONE thing - the attribute set, which chlobby-21
    # measured off a real lobby. Two attributes got a 3-second rejection; this carries fifteen.
    #
    # (kept from chlobby-20, unused here:)
    #
    # chlobby-18/19 went straight to BB5_Hospital "listen" and the match EXITED after ~3 s - Sam saw
    # the leave-the-match animation, so the game inspected the session and chose to go. The game's
    # own flow never does that: it opens LobbyHost "listen" FIRST, which is where
    # BodycamLobbyManager gets its party id and match context (GenerateNewPartyId,
    # UpdateMatchContext(1), Session Max Players = 10 - ubergraph 0f03), and only then travels.
    # We were dropping a listen server into a match map carrying a session that had been through
    # none of that.
    # STAGE 1: become a host IN THE LOBBY (chlobby-26).
    #
    # chlobby-25 proved the ejection happens with NO LOBBY AT ALL, so lobby metadata was never what
    # the match objects to (6i). What is left is subsystem state - GenerateNewPartyId,
    # UpdateMatchContext(1), Session Max Players - which BodycamLobbyManager sets during the game's
    # own host flow, and which nothing we have built has ever established.
    #
    # The game picks that up by opening LobbyHost "listen". chlobby-20 was built to do exactly this
    # and was NEVER TESTED - every run died at CreateLobby before reaching the travel. Now that
    # OpenLevel alone is known reliable, it finally can be.
    # SET THE GAME'S OWN TARGET FIRST (chlobby-28).
    #
    # Six builds tried to stop the match ejecting us. It never was ejecting us: the game opens
    # `Selected Level Name` as a listen server in its hosting-success path, and ours held the lobby,
    # so it overwrote our travel a moment later. Set it to the match map and the game's follow-up
    # agrees with us instead of fighting us.
    g.call("gi2", GS_LIB, "GetGameInstance")
    g.cast("gi2c", GI_CLASS, pure=True)
    g.link(("gi2.ReturnValue", "gi2c.cast_object"))
    g.call("selname", SYS, "MakeLiteralName", {"Value": HOST_MAP_SHORT})
    g.set("setsel", "Selected Level Name", GI_CLASS)
    g.link(("gi2c.cast_result", "setsel.self"),
           ("selname.ReturnValue", "setsel.Selected Level Name"))
    g.chain("setsel", "mkopen")

    g.call("mkopen", GS_LIB, "OpenLevel",
           {"LevelName": HOST_MAP, "bAbsolute": "true", "Options": "listen"})
    g.link(("mkseq.then_1", "mkopen.exec"))

    g.existing("lobbyfailed", "OnLobbyFailed")
    g.chain("lobbyfailed", _report_bare(g, "mkno", "ch_mk_fail"))

    # ---- STAGE 2 IS GONE (chlobby-31). It was the freeze.
    #
    # Stage 2 was written for chlobby-26, when stage 1 opened LobbyHost "listen" and stage 2 was the
    # follow-up hop from the lobby to the match map. chlobby-23/28 changed stage 1 to open HOST_MAP
    # directly (and to set `Selected Level Name` so the game's own follow-up agrees with us), which
    # left stage 2 re-opening the map it was ALREADY IN.
    #
    # Its gate was `IsServer && !IsStandalone`, and it hung straight off the BeginPlay fan with no
    # delay and no lap gate - so it re-armed itself in the world it had just travelled to:
    #
    #   lobby, first BeginPlay        IsServer true, IsStandalone true   -> false, stays put
    #   BB5_Hospital as listen host   IsServer true, IsStandalone false  -> TRAVELS AGAIN
    #
    # and the destination of that travel is a listen server in BB5_Hospital, which reads true again.
    # One full map load per lap. Sam, 2026-09-16: auto-host "loads in then tries to load in again",
    # the second load appears frozen; twice it eventually landed and the third closed the game with
    # an application hang. Non-deterministic because this OpenLevel dropped `listen` - on a lap
    # where the netmode collapsed to Standalone the gate went false and the loop ended by luck.
    #
    # It was harmless until today only because auto-host was not travelling at all: the shipped seed
    # asked the dead pre-rebrand hostname for its travel permit (see check_graphs.FROZEN), so there
    # was never a second BeginPlay. 6debe78 fixed the hostname and handed stage 2 its first one.
    #
    # Stage 1 already does the whole job. If a second hop is ever needed again it needs a lap gate
    # (_lap_gate) and a DIFFERENT target - not the map it is standing in.
    return "hdly"


def join_probe(g):
    """THE JOINER: find the host's lobby among the strangers', and join it.

    WHY THIS IS SAFE ON EVERY PLAYER'S MACHINE. Like the host arm it hangs off a server-held reply,
    but its own route - /api/probe/join - which the backend answers ONLY for a player who is in a
    connecting match, is NOT that match's host, and whose host has already reported in from inside
    the match world. A normal launch gets no answer and searches nothing, exactly as the host arm
    behaves for a non-host.

    That gating also disposes of the "one search per launch" problem without any retry: the hub does
    not launch a joiner's game until the host's own GM_BB5 has reported arrival, so by the time this
    arm runs the lobby it is looking for already exists. The single shot is fired when it can hit.

    PICKING THE RIGHT LOBBY, and the belief that had to be overturned to do it. The record said this
    was impossible - "choosing the best of 25 means looping and remembering a candidate, and the rule
    forbids the variable". That is wrong twice over, and both halves are measured:

      * Rule 3 bans CLASS properties, because a cooked CDO is written by property index. A
        ForEachLoop's temporaries are FUNCTION-FRAME locals: our own cooked GM_DOM declares
        Temp_int_Array_Index_Variable and Temp_int_Loop_Counter_Variable inside its EVENT GRAPH,
        while classinfo reports neither among its class properties. GM_CHLobby_C itself has exactly
        ONE own property (UberGraphFrame) and its ubergraph already carries 29 locals, four of them
        arrays of BodycamLobbySearchResult.
      * An exact-identity pick needs no accumulator at all. JoinLobby fires INSIDE the loop body on
        the element that matches; nothing has to be remembered between iterations.

    The game does this very loop itself in BodycamGI::FilterLobbySearchResults (export 22, a real
    966-byte body, not a thunk): Array_Length / Array_Get over the results, testing each with an
    attribute getter.

    WHY THE CLIENT-SIDE COMPARE IS NOT REDUNDANT with searching for the host directly: whether the
    backend honours a search key outside its own base set is still formally open (the filter arm is
    measuring it). Searching the key it is KNOWN to honour and then checking each result here turns
    "the backend ignored my filter" from "joined a stranger's match" into "joined nobody", which is
    the failure we want.
    """
    g.call("jndly", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_JOIN_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_join_wait",
        "Storefront": "join", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.n("jndlyd", "createevent", func="OnDelayA")
    g.link(("jndlyd.OutputDelegate", "jndly.OnResponse"))
    g.existing("delaya", "OnDelayA")

    # Same standalone gate as the host arm: a player already in somebody else's game must not be
    # dragged out of it, and a listen server must not try to join itself.
    g.call("jnstd", SYS, "IsStandalone")
    g.branch("jnbr")
    g.link(("jnstd.ReturnValue", "jnbr.condition"))
    g.chain("delaya", "jnbr")
    g.link(("jnbr.else", "%s.exec" % _report_bare(g, "jnxs", "ch_join_notstandalone")))

    # Search the key the backend is KNOWN to honour: it returned 25 real lobbies in chlobby-9.
    g.link(("jnbr.then", "%s.exec" % _search_arm(
        g, "js", ACCESS_KEY, ACCESS_VALUE, "OnFoundB", "OnFailB",
        "ch_join_find_ask", "ch_join_find_proxy")))

    g.existing("foundb", "OnFoundB")
    g.seq("jnseq", 2)
    g.chain("foundb", "jnseq")
    g.link(("jnseq.then_0",
            "%s.exec" % _report_count(g, "jnc", "ch_join_count", "foundb.SearchResults")))

    # THE LOOP. Array / Exec / Array Element / LoopBody / Completed are the ForEachLoop macro's own
    # pin names (bb5_graphs.py and ctf_graphs.py already drive it with exactly these).
    g.foreach("jnfe")
    g.link(("foundb.SearchResults", "jnfe.Array"), ("jnseq.then_1", "jnfe.Exec"))

    # The identity as an FName so the hub can rewrite it, converted for the FString compare pin.
    g.call("jnnm", SYS, "MakeLiteralName", {"Value": HOST_ID})
    g.call("jnns", STR, "Conv_NameToString")
    g.link(("jnnm.ReturnValue", "jnns.InName"))
    g.call("jncmp", TYPES, "CompareStringAttribute", {"Key": JOIN_KEY})
    g.link(("jnfe.Array Element", "jncmp.SearchResult"), ("jnns.ReturnValue", "jncmp.CompareString"))
    g.branch("jncbr")
    g.link(("jncmp.ReturnValue", "jncbr.condition"))
    g.link(("jnfe.LoopBody", "jncbr.exec"))

    # MATCH -> join THIS element. Marker on its own Sequence pin, never in front of the join:
    # SendAttributionEvent is latent and would hold the join behind an HTTP round trip.
    g.seq("jnjseq", 2)
    g.link(("jncbr.then", "jnjseq.exec"))
    g.link(("jnjseq.then_0", "%s.exec" % _report_bare(g, "jnhit", "ch_join_matched")))

    g.call("jnpc", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.call("jnjoin", JOIN_LOBBY, "JoinLobby")
    g.link(("jnfe.Array Element", "jnjoin.Lobby"), ("jnpc.ReturnValue", "jnjoin.PlayerController"))
    g.n("jnjd", "createevent", func="OnJoinOk")
    g.n("jnbind", "adddelegate", delegate="OnSuccess", **{"class": JOIN_LOBBY})
    g.link(("jnjoin.ReturnValue", "jnbind.self"), ("jnjd.OutputDelegate", "jnbind.Delegate"))
    g.n("jnjf", "createevent", func="OnJoinFail")
    g.n("jnbindf", "adddelegate", delegate="OnFailure", **{"class": JOIN_LOBBY})
    g.link(("jnjoin.ReturnValue", "jnbindf.self"), ("jnjf.OutputDelegate", "jnbindf.Delegate"))
    g.call("jnact", ASYNC_BASE, "Activate")
    g.link(("jnjoin.ReturnValue", "jnact.self"))
    g.link(("jnjseq.then_1", "jnjoin.exec"))
    g.chain("jnjoin", "jnbind", "jnbindf", "jnact")

    # Nothing matched anywhere in the array: say so, or the log cannot tell "searched and found
    # nobody" from "never searched".
    g.link(("jnfe.Completed", "%s.exec" % _report_bare(g, "jnmiss", "ch_join_nomatch")))

    g.existing("failb", "OnFailB")
    g.chain("failb", _report_bare(g, "jnfail", "ch_join_find_failed"))

    g.existing("joinok", "OnJoinOk")
    g.chain("joinok", _report_bare(g, "jnok", "ch_join_ok"))
    g.existing("joinfail", "OnJoinFail")
    g.chain("joinfail", _report_bare(g, "jnno", "ch_join_failed"))
    return "jndly"


def net_probe(g):
    """chlobby-14: WHAT NETMODE IS THE SHOOTING RANGE? Read-only; opens nothing.

    This decides whether step 5 (the host starting its own match) is possible at all. research/
    says LobbyHost is opened "listen", which would make every player a listen server standing in
    the range - and OpenLevel on a listen server travels everyone, which is exactly the mechanism
    the plan needs. If it is STANDALONE instead, OpenLevel would change only the host's own level
    and take nobody with it, and the whole approach has to change.

      timestamp  IsServer          true on a listen server AND on standalone (authority)
      platform   IsStandalone      the discriminator: true means NOT a listen server
      storefront GetCurrentLevelName   which level we are actually standing in

    IsServer alone cannot answer it - standalone has authority too - which is why both ride along.
    """
    g.call("nsrv", SYS, "IsServer")
    g.call("nsrvS", STR, "Conv_BoolToString")
    g.link(("nsrv.ReturnValue", "nsrvS.InBool"))
    g.call("nstd", SYS, "IsStandalone")
    g.call("nstdS", STR, "Conv_BoolToString")
    g.link(("nstd.ReturnValue", "nstdS.InBool"))
    g.call("nlvl", GS_LIB, "GetCurrentLevelName", {"bRemovePrefixString": "true"})
    g.call("npid", ONLINE, "GetPlatformUserNetId")
    g.call("nsnd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_netmode",
        "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("npid.ReturnValue", "nsnd.UserId"),
           ("nsrvS.ReturnValue", "nsnd.Timestamp"),
           ("nstdS.ReturnValue", "nsnd.Platform"),
           ("nlvl.ReturnValue", "nsnd.Storefront"))
    return "nsnd"


# ==================================================================================================
# GM_CHTJoin - JOIN A STRANGER, to prove a stranger could join us
# ==================================================================================================
# Sam, 2026-09-15: "lets test this by attempting to join a random person's game. if we do this we
# can then prove someone could join our game through the same method."
#
# That is the right test and it is the LAST one that can be done from a single machine. The joiner's
# chain is: warm up -> FindLobbies -> read attributes off the results -> pick one -> JoinLobby ->
# travel. Every link in it is identical whether the lobby belongs to our own match host or to a
# stranger; the only thing that differs is WHICH result is chosen. So joining a stranger exercises
# the whole mechanism end to end, and if it works the real joiner's remaining risk is only "is our
# host's lobby in the list", which is already measured (ch_lobby_verify, eight times over six
# minutes).
#
# NO PERMIT HERE, DELIBERATELY. The real joiner is gated on /api/probe/join, and that route's
# instant-status behaviour is on this branch and not deployed - Sam's call, and the right one. This
# class asks for nothing from the backend beyond the hop chain's clock, so the test does not wait on
# a deploy.
#
# WHAT IT PICKS, and chtjoin-11 INVERTS IT. The first candidate that is public AND MID-MATCH:
# Access == "false" and Ingame == "1". chtjoin-9 filtered on Access alone and drew a match in
# progress by luck (WornHouse) - the run that reached a stranger's map and then died on
# "Couldn't spawn player:". fceb842 then measured that every lobby in the browser is mid-match
# anyway, so demanding Ingame == "1" costs us nothing in targets and makes the experiment
# deliberate instead of lucky. Unrolled over the candidates rather than looped, because a
# ForEachLoop evaluates EVERY element and could fire JoinLobby several times over; there is no
# variable to remember "already joined" with (rule 3). Sequential branches are stateless,
# bounded, and cannot double-join.
#
# It is polite by construction: it joins a PUBLIC lobby that is sitting in its own menu rather than
# a match in progress, and peekrun closes the game straight after. Players join and leave public
# lobbies constantly; this is the game's own stranger-join flow, which is the whole point.
#
# READING IT: ch_tj_pick carries the lobby we chose (its Name, PlayerSteamId, Gamemode, Map) and
# fires BEFORE the join, so it is the evidence even if the travel destroys the world before
# ch_tj_ok can send - which it may well do, and its absence proves nothing. The real proof is Sam's
# screen: he is either standing in a stranger's game or he is not.
TJOIN_HOPS = 75
TJOIN_CANDIDATES = 10
TJOIN_TAG = "chtjoin-14"
# how many search results to DESCRIBE before choosing one to join
TJOIN_RECON = 8
# search early, then look at where we ended up - twice, seconds apart
TJOIN_SEARCH_AT = 20
TJOIN_STATE_AT = (45, 74)
# THE TARGET FILTER (chtjoin-11). Ingame is "1"/"0" on a real lobby while Access and Bots are
# "true"/"false" - the game's own keys are not self-consistent, so these are separate literals
# rather than a shared one.
INGAME_KEY = "Ingame"
INGAME_VALUE = "1"
# THE FILTER UNDER TEST (chtjoin-12). A lobby Name that cannot exist, so the only two outcomes are
# "0, the backend filtered" and "everything, the backend ignored the key". The value is deliberately
# not a plausible player name: if it ever matched something, the run would be unreadable.
FILTER_KEY = "Name"
FILTER_VALUE = "chfilter-no-such-lobby-7f3a91"


def _tj_instant(g, nid, handler):
    """One silent instant round trip, bound. Silent because the probe log keeps 50 entries."""
    # PROBE_404, not PROBE_URL: the reply still comes (a 404 is a real response, chlobby-5) but the
    # generic 404 handler writes no probe entry, so a long chain no longer fills the 50-entry log
    # and evicts its own evidence - which is how chtjoin-6 lost ch_tj_state74.
    g.call(nid, HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_404, "BearerToken": "chlobby", "IP": "", "EventName": "ch_tj_hop",
        "Storefront": "tjoin", "FirstSessionTimestamp": TJOIN_TAG, "IsFirstGameOpen": "false"})
    g.n(nid + "d", "createevent", func=handler)
    # the pin is OutputDelegate, not "delegate" (UK2Node_CreateDelegate's own naming)
    g.link((nid + "d.OutputDelegate", nid + ".OnResponse"))
    return nid


def chtjoin_events():
    """GM_CHTJoin's handlers. `createevent` resolves by NAME, so they exist before the logic pass."""
    g = G()
    for i in range(TJOIN_HOPS):
        g.custom("tjhop%d" % i, "OnTJHop%d" % i, [P("bSuccess", "bool")])
    g.custom("tjfound", "OnTJFound",
             [P("SearchResults", "struct", struct=SEARCH_RESULT, array=True)])
    g.custom("tjfailed", "OnTJFailed")
    g.custom("tjok", "OnTJOk")
    g.custom("tjno", "OnTJNo")
    g.custom("tjready", "OnTJReady")
    return g.json()


def _lap_gate(g, token, first="bp", parent="bp_parent"):
    """Write the join token at most LAP_TRIES times, then fall through to a normal boot.

    Shared by the shipped joiner (GM_CHJoin) and the measurement class (GM_CHTJoin) so the two can
    never drift on the one thing that decides whether a player can boot at all.

    The counter lives on BodycamGI #35 MostFillServer, an int at flags 0x10005 that NOTHING in the
    game reads or writes (zero hits across 21 disassembled Blueprints). It has to live on the
    GameInstance because the retry IS a level reload - HandleLobbyJoinFailure calls
    OpenLevel(LobbyHost) - and the GameInstance is the only thing that survives one. It cannot live
    on the gamemode: rule 3, the cooked CDO is written by property index.

    LITERALS, NEVER ARITHMETIC. UE inlines pure nodes per consumer, so a pure Get feeding both a Set
    and a check is evaluated once per consumer at that consumer's execution time; an
    increment-and-verify ladder compares n+1 against a freshly recomputed n+2, never matches, and
    bounds at two laps while looking like eight. Every comparand and every written value here is a
    literal pin default.

    THE DEFAULT IS TO BOOT. The bottom `else` - which is also where an exhausted counter, a
    corrupted value or any stock default we did not predict lands - writes no token, so GM_Host's
    own IsEmpty test takes the HostPartyInLobby arm. Every failure direction is a working game.
    """
    g.call("lap_gi", GS_LIB, "GetGameInstance")
    g.cast("lap_gic", GI_CLASS, pure=True)
    g.link(("lap_gi.ReturnValue", "lap_gic.cast_object"))
    g.get("lap_n", LAP_PROP, GI_CLASS)
    g.link(("lap_gic.cast_result", "lap_n.self"))

    jn = _gi_set_str(g, "jn_", "SessionToJoin (Client)", token)
    for k in range(LAP_TRIES):
        g.call("lap_eq%d" % k, MATH, "EqualEqual_IntInt", {"B": str(k)})
        g.link(("lap_n." + LAP_PROP, "lap_eq%d.A" % k))
        g.branch("lap_br%d" % k)
        g.link(("lap_eq%d.ReturnValue" % k, "lap_br%d.condition" % k))
        # claim the lap BEFORE attempting, so a world that dies mid-flight has still spent its turn
        g.set("lap_w%d" % k, LAP_PROP, GI_CLASS, {LAP_PROP: str(k + 1)})
        g.link(("lap_gic.cast_result", "lap_w%d.self" % k),
               ("lap_br%d.then" % k, "lap_w%d.exec" % k))
        g.link(("lap_w%d.then" % k, "%s.exec" % jn))
        if k:
            g.link(("lap_br%d.else" % (k - 1), "lap_br%d.exec" % k))

    # bp -> ladder; the attempt arm and the give-up arm both converge on the parent call. An exec
    # OUTPUT pin drives exactly one link, but an exec INPUT accepts many - so no Sequence here.
    g.chain(first, "lap_br0")
    g.chain(jn, parent)
    g.link(("lap_br%d.else" % (LAP_TRIES - 1), parent + ".exec"))
    return jn


def _gi_set_str(g, p, prop, token):
    """Write a string property on BodycamGI, and return the node whose exec pin starts the write.

    Modelled exactly on the host arm's "Selected Level Name" write, which is MEASURED to take
    effect in a cooked build: GetGameInstance -> pure cast to BodycamGI_C -> Set. A cooked Set
    resolves the property through the import table by name, the same way chlobby-15 proved a
    function call does.

    The value goes in as a NAME and is converted, not as a string literal - see JOIN_TOKEN.
    """
    g.call(p + "gi", GS_LIB, "GetGameInstance")
    g.cast(p + "gic", GI_CLASS, pure=True)
    g.link((p + "gi.ReturnValue", p + "gic.cast_object"))
    g.call(p + "lit", SYS, "MakeLiteralName", {"Value": token})
    g.call(p + "str", STR, "Conv_NameToString")
    g.link((p + "lit.ReturnValue", p + "str.InName"))
    g.set(p + "set", prop, GI_CLASS)
    g.link((p + "gic.cast_result", p + "set.self"),
           (p + "str.ReturnValue", p + "set." + prop))
    return p + "set"


def _tj_cand(g, p, idx, results_pin):
    """One search result described, so the next target is chosen from data rather than guessed.

    CurrentMembers and MaxMembers come off the STRUCT, not out of an attribute string: the
    "MaxPlayers" attribute reads 'N/A' on real lobbies (chpeek-6), so it cannot be used for this.
    Ingame, Access and Map are attributes and do carry real values.

    Fields, packed into the four usable strings:
        user_id    "<current>/<max>"    platform  Ingame
        storefront Map                  timestamp Access
    """
    g.call(p + "get", ARR, "Array_Get", {"Index": str(idx)}, array=True)
    g.link((results_pin, p + "get.TargetArray"))
    g.brk(p + "brk", SEARCH_RESULT)
    g.link((p + "get.Item", p + "brk.in"))
    g.call(p + "cs", STR, "Conv_IntToString"); g.link((p + "brk.CurrentMembers", p + "cs.InInt"))
    g.call(p + "xs", STR, "Conv_IntToString"); g.link((p + "brk.MaxMembers", p + "xs.InInt"))
    g.call(p + "sl", STR, "Concat_StrStr", {"B": "/"}); g.link((p + "cs.ReturnValue", p + "sl.A"))
    g.call(p + "both", STR, "Concat_StrStr")
    g.link((p + "sl.ReturnValue", p + "both.A"), (p + "xs.ReturnValue", p + "both.B"))
    g.call(p + "ing", TYPES, "GetStringAttribute", {"Key": "Ingame"})
    g.link((p + "get.Item", p + "ing.SearchResult"))
    g.call(p + "acc", TYPES, "GetStringAttribute", {"Key": ACCESS_KEY})
    g.link((p + "get.Item", p + "acc.SearchResult"))
    g.call(p + "map", TYPES, "GetStringAttribute", {"Key": "Map"})
    g.link((p + "get.Item", p + "map.SearchResult"))
    g.call(p + "snd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "",
        "EventName": "ch_tj_cand%d" % idx,
        "FirstSessionTimestamp": TJOIN_TAG, "IsFirstGameOpen": "false"})
    g.link((p + "both.ReturnValue", p + "snd.UserId"),
           (p + "ing.OutString", p + "snd.Platform"),
           (p + "map.OutString", p + "snd.Storefront"),
           (p + "acc.OutString", p + "snd.Timestamp"))
    return p + "snd"


def _tj_mgr(g, p):
    """Reach UBodycamLobbyManager and return the pin holding it.

    GetGameInstanceSubsystem is the only Blueprint route to a GameInstanceSubsystem - there is no
    node kind for it in the builder, so it is called as the plain static library function it is. It
    is marked BlueprintInternalUseOnly, which hides it from the editor's PALETTE but does not stop a
    node being created programmatically; if that turns out to be wrong the Blueprint pass says so
    loudly and nothing is lost."""
    g.call(p + "sub", SUBSYS_LIB, "GetGameInstanceSubsystem", {"Class": LOBBY_MGR})
    g.selfnode(p + "subself")
    g.link((p + "subself.self", p + "sub.ContextObject"))
    g.cast(p + "subc", LOBBY_MGR, pure=True)
    g.link((p + "sub.ReturnValue", p + "subc.cast_object"))
    return p + "subc.cast_result"


def _tj_state(g, p, event_name):
    """Where are we NOW: are we in a lobby, and how full is it.

    This is the discriminator chtjoin-5 needed and did not have. IsInLobby() is FALSE at BeginPlay
    (ch_tj_alive reports it every run), so if JoinLobby actually joined a Steam lobby it must flip
    to true. That separates two very different failures which look identical from outside:

        still false  -> the JOIN itself did not take. The search and the pick were fine and the
                        join was refused or silently dropped.
        now true     -> the join WORKED and we are sitting in a stranger's lobby; what is missing
                        is the TRAVEL into their match, which the game does as a separate step
                        (BodycamLobbyManager::CommunicateJoinLobby / BP_HandleRequestServerTravel).

    Sam is in the range after chtjoin-5, so one of those two is true and they need opposite work.
    Crash-free by construction: nothing here binds a delegate or travels."""
    # IsStandalone is the real discriminator, and it comes straight out of the auto-host arm:
    # host_probe gates its travel on it, noting that "a client in somebody else's lobby is ALSO NOT
    # STANDALONE, and travelling them would drag them out of a stranger's game". So the game already
    # distinguishes the state we are trying to reach. IsInLobby cannot - chtjoin-6 showed it goes
    # true about a second after load whether or not we join anything, because the game puts the
    # player in a lobby of their own.
    g.call(p + "std", SYS, "IsStandalone")
    g.call(p + "srv", SYS, "IsServer")
    g.call(p + "stds", STR, "Conv_BoolToString"); g.link((p + "std.ReturnValue", p + "stds.InBool"))
    g.call(p + "srvs", STR, "Conv_BoolToString"); g.link((p + "srv.ReturnValue", p + "srvs.InBool"))
    g.call(p + "sj", STR, "Concat_StrStr", {"B": "/"}); g.link((p + "stds.ReturnValue", p + "sj.A"))
    g.call(p + "both2", STR, "Concat_StrStr")
    g.link((p + "sj.ReturnValue", p + "both2.A"), (p + "srvs.ReturnValue", p + "both2.B"))
    # THE GAME'S OWN WORDS. EMatchContext is { Undefined, Lobby, QuickPlayCasual,
    # QuickPlayCompetitive, Custom } - if a join is taking effect at all, this is where it shows up,
    # and it says far more than a bool ever could.
    mgr = _tj_mgr(g, p + "m")
    g.call(p + "ctx", LOBBY_MGR, "GetCurrentMatchContext"); g.link((mgr, p + "ctx.self"))
    g.call(p + "ctxs", STR, "Conv_ByteToString"); g.link((p + "ctx.ReturnValue", p + "ctxs.InByte"))
    g.call(p + "hcj", LOBBY_MGR, "GetHostCanJoinLobby"); g.link((mgr, p + "hcj.self"))
    g.call(p + "hcjs", STR, "Conv_BoolToString"); g.link((p + "hcj.ReturnValue", p + "hcjs.InBool"))
    g.call(p + "cj", STR, "Concat_StrStr", {"B": "/"}); g.link((p + "ctxs.ReturnValue", p + "cj.A"))
    g.call(p + "ctxboth", STR, "Concat_StrStr")
    g.link((p + "cj.ReturnValue", p + "ctxboth.A"), (p + "hcjs.ReturnValue", p + "ctxboth.B"))
    g.call(p + "il", ONLINE, "IsInLobby")
    g.call(p + "ils", STR, "Conv_BoolToString")
    g.link((p + "il.ReturnValue", p + "ils.InBool"))
    g.call(p + "inf", ONLINE, "GetCurrentLobbyInfo")
    g.call(p + "mem", TYPES, "GetCurrentLobbyMembers")
    g.link((p + "inf.ReturnValue", p + "mem.LobbyInfo"))
    g.call(p + "max", TYPES, "GetMaxLobbyMembers")
    g.link((p + "inf.ReturnValue", p + "max.LobbyInfo"))
    g.call(p + "ms", STR, "Conv_IntToString"); g.link((p + "mem.ReturnValue", p + "ms.InInt"))
    g.call(p + "xs", STR, "Conv_IntToString"); g.link((p + "max.ReturnValue", p + "xs.InInt"))
    g.call(p + "sl", STR, "Concat_StrStr", {"B": "/"}); g.link((p + "ms.ReturnValue", p + "sl.A"))
    g.call(p + "bo", STR, "Concat_StrStr")
    g.link((p + "sl.ReturnValue", p + "bo.A"), (p + "xs.ReturnValue", p + "bo.B"))
    g.call(p + "snd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": event_name,
        "Storefront": "tjoin", "FirstSessionTimestamp": TJOIN_TAG, "IsFirstGameOpen": "false"})
    # timestamp = IsInLobby, platform = members, storefront = "<standalone>/<server>"
    g.link((p + "ils.ReturnValue", p + "snd.Timestamp"), (p + "bo.ReturnValue", p + "snd.Platform"),
           (p + "both2.ReturnValue", p + "snd.Storefront"),
           (p + "ctxboth.ReturnValue", p + "snd.UserId"))
    return p + "snd"


def chtjoin_logic():
    """BeginPlay -> warm up -> search -> pick a public, idle stranger -> join them."""
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    # RULE 2: the parent performs the Steam login, without which there is nothing to search with.
    g.callparent("bp_parent", HOST_CLASS, "ReceiveBeginPlay")
    # ASK THE GAME TO JOIN BY NAME, AT MOST LAP_TRIES TIMES - the same gate the shipped joiner
    # uses, so the measurement class and the product can never drift on the one thing that decides
    # whether a player can boot at all. See _lap_gate.
    _lap_gate(g, JOIN_TOKEN)

    g.call("pid", ONLINE, "GetPlatformUserNetId")
    g.call("plat", HTTP_LIB, "GetOnlinePlatformName")
    g.call("inlobby", ONLINE, "IsInLobby")
    g.call("inlobbyS", STR, "Conv_BoolToString")
    g.link(("inlobby.ReturnValue", "inlobbyS.InBool"))
    g.call("alive", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_tj_alive",
        "Storefront": "tjoin", "FirstSessionTimestamp": TJOIN_TAG, "IsFirstGameOpen": "false"})
    g.link(("pid.ReturnValue", "alive.UserId"),
           ("plat.ReturnValue", "alive.Platform"),
           ("inlobbyS.ReturnValue", "alive.Timestamp"))

    # RULE 5: fan out, never chain through a latent node.
    # BIND THE GAME'S OWN "ready to travel" SIGNAL. If this fires after our JoinLobby, the game has
    # finished the half we can do and is waiting to be told to go - and AllowJoinLobby() becomes a
    # one-call fix. If it never fires, the join is not taking effect at all and calling anything
    # would be guessing. Binding is read-only and cannot break the flow.
    rdy = _tj_mgr(g, "rdy")
    g.n("rdyev", "createevent", func="OnTJReady")
    g.n("rdybind", "adddelegate", delegate="OnReadyToJoinLobby", **{"class": LOBBY_MGR})
    g.link((rdy, "rdybind.self"), ("rdyev.OutputDelegate", "rdybind.Delegate"))

    g.seq("fan", 3)
    g.chain("bp_parent", "fan")
    g.link(("fan.then_0", "alive.exec"))
    g.link(("fan.then_1", "rdybind.exec"))
    g.link(("fan.then_2", "%s.exec" % _tj_instant(g, "th0", "OnTJHop0")))

    # the warm-up: a search before ~+2.4 s is refused (chpeek-6), so buy the time first
    # ONE branch per hop, decided up front. The extras (the search, the two state reports) each
    # need their own Sequence pin: hanging them off the same pin as the next hop would silently
    # break one of the two links (check 5).
    for i in range(TJOIN_HOPS):
        g.existing("tph%d" % i, "OnTJHop%d" % i)
        extra = None
        if i == TJOIN_SEARCH_AT:
            # chtjoin-12: the filter is the EXPERIMENT, not a convenience. Everything else about
            # this arm is byte-identical to the run that found 25 lobbies on {Access:"false"}, so
            # the count is comparable against that baseline directly.
            extra = _search_arm(g, "ts", FILTER_KEY, FILTER_VALUE, "OnTJFound", "OnTJFailed",
                                "ch_tj_ask", "ch_tj_proxy", tag=TJOIN_TAG)
        elif i in TJOIN_STATE_AT:
            extra = _tj_state(g, "st%d_" % i, "ch_tj_state%d" % i)
        nxt = (_tj_instant(g, "th%d" % (i + 1), "OnTJHop%d" % (i + 1))
               if i + 1 < TJOIN_HOPS else None)
        if extra and nxt:
            g.seq("hq%d" % i, 2)
            g.chain("tph%d" % i, "hq%d" % i)
            g.link(("hq%d.then_0" % i, "%s.exec" % nxt))
            g.link(("hq%d.then_1" % i, "%s.exec" % extra))
        elif extra:
            g.chain("tph%d" % i, extra)
        elif nxt:
            g.chain("tph%d" % i, nxt)
    g.existing("tjfound", "OnTJFound")
    g.call("tlen", ARR, "Array_Length", array=True)
    g.link(("tjfound.SearchResults", "tlen.TargetArray"))
    # FAN, NEVER CHAIN - and chtjoin-2 is why this rule exists. The cascade used to hang off the
    # count report's `.then`, and SendAttributionEvent is LATENT: its exec output waits for the HTTP
    # response. ch_tj_count reached the server and NOTHING followed, twice, because the response to
    # a send started from a SEARCH DELEGATE does not come back - the hop chain proved HTTP->HTTP
    # nesting works, but that is a different origin and this is evidence it does not generalise.
    # So the count and the cascade are independent pins and the cascade never waits on a reply.
    g.seq("tfan", 2)
    g.chain("tjfound", "tfan")
    g.link(("tfan.then_0", "%s.exec" % _report_count(
        g, "tc", "ch_tj_count", "tjfound.SearchResults", tag=TJOIN_TAG)))

    # RECON FIRST, THEN JOIN. chtjoin-10 was recon-only and answered its question: every lobby in
    # the browser is mid-match. The recon stays because it is how the CHOSEN target is described -
    # without it a crash cannot be compared against chtjoin-9's (3/10 members, WornHouse).
    #
    # Sam, 2026-09-15: "shouldnt we confirm i enter their game in a playable state?" - and he is
    # right. The run that became a client ended in LowLevelFatalError "Couldn't spawn player", which
    # is not an arrival. Both joins so far were blind: the first at a player idling in their own
    # lobby (nothing to travel INTO, so we bounced), the second at a match already running
    # (travelled, then failed to spawn). Choose the next target with data rather than a third guess.
    #
    # It matters beyond this test: GM_BB5 sets bAllowJoinInProgress=true, so our own joiners arrive
    # into a match that is already running. If late-join spawning is what fails, that hits autojoin
    # directly and is not a stranger-only problem.
    g.seq("recon", TJOIN_RECON)
    g.link(("tfan.then_1", "recon.exec"))
    for i in range(TJOIN_RECON):
        g.link(("recon.then_%d" % i,
                "%s.exec" % _tj_cand(g, "cd%d" % i, i, "tjfound.SearchResults")))

    # THE JOIN LADDER IS DELIBERATELY ABSENT IN chtjoin-12.
    #
    # This build answers one question and joins nobody. A measurement that cannot crash, cannot
    # drag a stranger out of their match, and cannot leave Sam's account unable to host for twenty
    # minutes is worth more than one that might - and the count from ch_tj_count is the whole
    # result. chtjoin-11's ladder is in git (restored from chtjoin-9's 8bd369f) and comes back the
    # moment there is something worth joining.

    # ---------------------------------------------------------------- THE TEARDOWN PROBE
    #
    # THE ONLY THING THAT CAN REPORT FROM INSIDE LoadMap. Verified in UE 5.5 source on this
    # machine: UnrealEngine.cpp from SetGameMode(URL) to the SpawnPlayActor loop that fires
    # "Couldn't spawn player:" is straight-line synchronous game-thread code - FlushLevelStreaming,
    # two FMoviePlayerProxy::BlockingTick()s, InitializeActorsForPlay, and nothing that pumps the
    # HTTP manager or runs a Blueprint frame. So no hop chain, however dense, can sample that
    # window; a tick train aimed at it would have been 34 probes that cannot fire.
    #
    # EndPlay can, because it is the engine tearing US down: the old world's actors get EndPlay on
    # the game thread during the load, while the world is still non-null. One send, one level name,
    # nothing latent left behind.
    #
    # HONEST: ReceiveEndPlay has never been built in this project before. If the editor pass
    # refuses it, drop this block - the reproduction above does not depend on it.
    g.event("ep", "ReceiveEndPlay", ACTOR)
    g.call("eplvl", GS_LIB, "GetCurrentLevelName")
    g.call("epsnd", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_tj_teardown",
        "Storefront": "tjoin", "FirstSessionTimestamp": TJOIN_TAG, "IsFirstGameOpen": "false"})
    g.link(("eplvl.ReturnValue", "epsnd.Platform"))
    g.chain("ep", "eplvl", "epsnd")

    g.existing("tjfailed", "OnTJFailed")
    g.chain("tjfailed", _report_bare(g, "tfail", "ch_tj_searchfailed", tag=TJOIN_TAG))
    g.existing("tjok", "OnTJOk")
    g.chain("tjok", _report_bare(g, "tok", "ch_tj_ok", tag=TJOIN_TAG))
    g.existing("tjno", "OnTJNo")
    g.chain("tjno", _report_bare(g, "tno", "ch_tj_rejected", tag=TJOIN_TAG))
    g.existing("tjready", "OnTJReady")
    g.chain("tjready", _tj_state(g, "rdys_", "ch_tj_ready"))
    return g.json()


# ==================================================================================================
# GM_CHPeek - THE MEASUREMENT CLASS THAT NEVER TRAVELS
# ==================================================================================================
# Everything below is a SEPARATE cooked class in a SEPARATE pak. It shares no graph with GM_CHLobby
# and changes nothing about hosting; the two are never installed at the same time.
#
# WHY IT HAD TO BE ITS OWN CLASS. GM_CHLobby travels, and no online or lobby call may be outstanding
# when OpenLevel fires - chlobby-29 crashed exactly that way, the search still inside the EOS SDK
# when OpenLevel destroyed the world that owned it, EOS calling back into freed memory
# (EXCEPTION_ACCESS_VIOLATION, EOSSDK_Win64_Shipping mid-stack). So a FindLobbies cannot live beside
# the host arm at any price: not on a second Sequence pin, not on a longer delay, not "probably far
# enough apart". It has to be in a build that never opens a level at all. This one does not, and
# that is the single property that makes it safe.
#
# WHAT IT ANSWERS. One question, unmeasured since the design was written, and the joiner cannot be
# built until it is settled:
#
#   IS THE `PlayerSteamId` A LOBBY ADVERTISES THE SAME STRING OUR HOST WOULD PRODUCE?
#
# The game writes that attribute itself on every lobby it creates - disassembled out of the shipping
# MakeCreateLobbyParams, export 87, key at bytecode 0x3f5 - via BreakSteamID(FSteamID). Our side
# knows the host's SteamID64 two ways that agree with each other (the hub reads it off the match
# roster, the host derives it from GetPlatformUserNetId) but NEITHER has ever been compared against
# what the game actually advertises. If the formats differ, hub/lobbypak.py's stamped literal simply
# changes format and nothing else about the joiner design moves.
#
# It is the right identity for the job because it is the only one BOTH machines can produce without
# our backend: a CH_MATCH tag would have to be baked into a pak on both sides, and the joiner would
# still have to be told the value.
#
# ALSO SETTLED IN THE SAME RUN, free, because the peek reads sixteen keys either way: the real
# values of every attribute a stock lobby carries (LOBBY_KEYS). Twelve were measured in chlobby-21;
# Name, PlayerSteamId, Password, IpCountry and LobbyName were not.
#
# HOW TO READ THE RESULT, and every case says something different:
#   ch_peek_ask + ch_peek_proxy=true + ch_peek_count=N + ch_peek_0..3   the whole point. Read
#                                                                       PlayerSteamId out of the
#                                                                       group that carries it.
#   ch_peek_empty                       the search ran and matched nothing. Not a failure of ours -
#                                       try again when lobbies exist (25 is the usual answer).
#   ch_peek_failed                      OnFailure fired. The online layer refused the search.
#   ch_peek_ask but nothing after       the proxy activated and never called back: a real
#                                       menu-context limit, and the one outcome that forces a
#                                       redesign.
#   ch_peek_alive but no ch_peek_ask    the clock never came back. /api/probe/wait is ungated, so
#                                       suspect the network before the design.
#
# RULES IT STILL OBEYS. Rule 2, parent BeginPlay first, or the lobby's Steam login never runs (and
# without that login there is nothing to search WITH). Rule 3, no variables and no changed class
# defaults - the cooked CDO is written by property index and our stand-in parent has 0 properties
# where the real GM_Host_C has 12. Rule 5, fan out of a Sequence, never chain through
# SendAttributionEvent, which is latent.


# Its OWN build tag, never BUILD_TAG. Changing BUILD_TAG would change a string constant inside
# chlobby_logic and therefore the hosting build, which is frozen; and the probe log needs to say
# which of the two paks produced a run, since both report from the same lobby world.
# ~50 ms per hop once the connection is warm, so 80 hops is roughly four seconds.
PEEK_HOPS = 80
# Where along the chain to try a search. Sampling, not one attempt at the end: the
# question is no longer "does it ever work" but WHEN.
PEEK_SAMPLES = (9, 19, 39, 59, 79)
PEEK_TAG = "chpeek-6"


# ==================================================================================================
# GM_CHJoin - THE JOINER, and why it identifies its host by CH_MATCH
# ==================================================================================================
# Its own cooked class in its own pak, like GM_CHPeek and for a related reason: the joiner is
# packed for the machines that are NOT hosting, so it never shares a build with the host arm. The
# hub already picks per machine (build_lobby_override.py --class-pkg/--class).
#
# THE IDENTITY DECISION, and both candidates were real.
#
#   PlayerSteamId   The game writes it on lobbies it creates (MakeCreateLobbyParams, export 87,
#                   key at 0x3f5) via BreakSteamID(FSteamID), while our side would stamp
#                   GetPlatformUserNetId. The two formats have NEVER been compared, so choosing
#                   this means the joiner cannot be finished until somebody runs a peek in-game.
#
#   CH_MATCH        Our own tag, and it is MEASURED WORKING on the shipping build. From the probe
#                   log, one host, one match:
#                       ch_lobby_read   t+12s  platform="1/10"  storefront=''   timestamp='false'
#                       ch_lobby_write  t+30s                   storefront='ch-test-4821' timestamp='true'
#                       ch_lobby_verify x8 over six minutes     storefront='ch-test-4821' timestamp='true'
#                   So the host HAS a lobby in the match world, UpdateLobby stamps it, and the tag
#                   persists. Both sides of the comparison are values WE choose - the hub bakes the
#                   same literal into the joiner's pak that GM_BB5 writes onto the lobby - so there
#                   is no format question to answer at all.
#
# CH_MATCH wins on evidence: it is the one that is already proven end to end on one machine, and
# the one whose correctness does not depend on a measurement nobody has taken. The old objection to
# it - "nothing on either machine can derive a CH_MATCH value, so it would have to be baked into a
# pak on both sides" - is answered by the fact that the hub ALREADY bakes a per-match FName into the
# joiner's pak (hub/lobbypak.py stamps host_id exactly this way); baking the match id instead costs
# nothing new.
#
# The joiner reads PlayerSteamId anyway, on the same candidate, and reports it. One run therefore
# also settles the format question as a by-product, so the fallback is ready if it is ever wanted.
#
# WHAT IS STILL UNPROVEN, and it is the same single fact for EITHER identity: whether a lobby
# returned by FindLobbies on machine B carries the attributes machine A wrote. `ch_join_peek` below
# answers it - it reads four keys off the first candidate and reports them, so silence, empty
# strings and real values are three distinguishable outcomes rather than one.
#
# THE SEARCH IS DELIBERATELY UNFILTERED. A FindLobbies filtered on {Map:"Rome"} returned 25 -
# identical to {Access:"false"}'s 25 - so the backend IGNORES non-base search keys and a joiner can
# never filter server-side by host. Pull the page and compare locally; that is the only thing that
# can work. The ForEachLoop this needs is allowed: rule 3 bans CLASS properties because the cooked
# CDO is written by property index, and a loop's temporaries are FUNCTION-FRAME locals (measured -
# a compiled GM_CHLobby_C carrying one still reports exactly one own property). The game does the
# same loop itself in BodycamGI::FilterLobbySearchResults.
#
# TRAVEL SAFETY. JoinLobby travels, and no lobby call may be outstanding when that happens. The
# search has already COMPLETED here - we are inside its OnSuccess, holding the array it handed back
# - so nothing of ours is in flight. chlobby-11 proved the call itself: JoinLobby(results[0], PC)
# reported OnSuccess and put Sam into a stranger's game.

# The match id, patched per match by the hub - the same FName rewrite that points the host at his
# map and stamps host_id. An FName, never an FString: FNames live in the .uasset name table where
# pkgedit can rewrite them at ANY length, while an FString is an inline EX_StringConst in the .uexp
# and a length change would shift every jump offset after it.
JOIN_MATCH_ID = "ch-none"        # a placeholder the hub overwrites; matches no real lobby
# ~50 ms per hop once the connection is warm. chpeek-6 saw a search start working
# around +2.4 s, so the permit (~490 ms) plus 50 hops (~2.5 s) clears it.
JOIN_HOPS = 50
JOIN_TAG = "chjoin-2"


def _instant_j(g, nid, handler):
    """One request to the route that answers AT ONCE, bound, and SILENT - no probe of its own.

    The hops are pure delay. chpeek-6 flooded a 50-entry probe log with 80 hop markers and evicted
    its own early events; the joiner reports decisions only."""
    g.call(nid, HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_join_hop",
        "Storefront": "join", "FirstSessionTimestamp": JOIN_TAG, "IsFirstGameOpen": "false"})
    g.n(nid + "d", "createevent", func=handler)
    # the pin is OutputDelegate, not "delegate" (UK2Node_CreateDelegate's own naming)
    g.link((nid + "d.OutputDelegate", nid + ".OnResponse"))
    return nid


def chjoin_events():
    """EMPTY, ON PURPOSE. GM_CHJoin binds nothing and creates no delegate.

    The old joiner declared handlers for a permit reply, fifty warm-up hops, a FindLobbies pair and
    JoinLobby's two delegates. Every one of those is gone: the GAME performs the search, the
    destroy, the join and the travel once "SessionToJoin (Client)" is set. Binding JoinLobby's
    delegates is a measured crasher (chtjoin-3), and a FindLobbies of ours still inside EOS when the
    game travels is how chlobby-29 died. The safest online call is the one we never make."""
    return G().json()


def chjoin_logic():
    """BeginPlay -> write the match token (at most LAP_TRIES times) -> let GM_Host do the rest.

    That is the whole class. See _lap_gate for the bound, and the module docstring on
    JOIN_TOKEN for how the hub stamps a per-match value into this pak without a cook.
    """
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    # RULE 2: the parent performs the Steam login - and, for this class, the join itself.
    g.callparent("bp_parent", HOST_CLASS, "ReceiveBeginPlay")

    _lap_gate(g, JOIN_TOKEN)

    # One row so a run is attributable at all: which pak, which player, and whether the lobby layer
    # can see a lobby from here. Fanned off the parent rather than chained into the ladder, because
    # SendAttributionEvent is LATENT and must never sit between the token write and the parent call.
    g.call("pid", ONLINE, "GetPlatformUserNetId")
    g.call("plat", HTTP_LIB, "GetOnlinePlatformName")
    g.call("inlobby", ONLINE, "IsInLobby")
    g.call("inlobbyS", STR, "Conv_BoolToString")
    g.link(("inlobby.ReturnValue", "inlobbyS.InBool"))
    g.call("alive", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_join_alive",
        "Storefront": "join", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("pid.ReturnValue", "alive.UserId"),
           ("plat.ReturnValue", "alive.Platform"),
           ("inlobbyS.ReturnValue", "alive.Timestamp"))
    g.chain("bp_parent", "alive")
    return g.json()


def chpeek_events():
    """The delegate handlers GM_CHPeek binds. Declared in their own pass because `createevent`
    resolves its target BY NAME, so the events must exist before the logic pass runs."""
    g = G()
    # ONE success handler shared by every sampled search: whichever works lands in the same read.
    g.custom("peekfound", "OnPeekFound",
             [P("SearchResults", "struct", struct=SEARCH_RESULT, array=True)])
    # one failure handler per SAMPLE, so the log says which attempt was refused
    for i in PEEK_SAMPLES:
        g.custom("sfail%d" % i, "OnPeekFail%d" % i)
    # the hop chain: each instant reply's handler sends the next request
    for i in range(PEEK_HOPS):
        g.custom("hop%d" % i, "OnPeekHop%d" % i, [P("bSuccess", "bool")])
    # the held reply, asked once on its own line so its silence stays a separate fact
    g.custom("peekheld", "OnPeekHeld", [P("bSuccess", "bool")])
    return g.json()

def _instant(g, nid, event_name, handler=None):
    """One request to the route that answers AT ONCE, optionally with its response bound."""
    g.call(nid, HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": event_name,
        "Storefront": "peek", "FirstSessionTimestamp": PEEK_TAG, "IsFirstGameOpen": "false"})
    if handler:
        g.n(nid + "d", "createevent", func=handler)
        # the pin is OutputDelegate, not "delegate" (UK2Node_CreateDelegate's own naming)
        g.link((nid + "d.OutputDelegate", nid + ".OnResponse"))
    return nid


def chpeek_logic():
    """BeginPlay -> walk a long chain of instant round trips, trying a search at five points.

    WHAT chpeek-5 SETTLED, and it overturned something the record asserted:

        34.982  ch_peek_hop0_ask
        35.449  ch_peek_hop0        467 ms - the FIRST round trip
        35.450  ch_peek_hop1_ask
        35.496  ch_peek_hop1         46 ms - and every hop after it
        ...     hop2 .. hop9        ~50 ms each, ALL TEN ARRIVED
        35.951  ch_peek_ask         the search, ~969 ms after BeginPlay
        35.980  ch_peek_failed      refused 29 ms later

    CALLBACKS DO NEST. chlobby-12 and -13 concluded they do not, and that was load-bearing in the
    design for months; it is simply not true here. A request sent from inside a response handler is
    answered, ten deep, without trouble.

    But the first round trip costs ~470 ms and every one after it costs ~50 ms - HTTP keep-alive -
    so ten hops bought a second, not five. The same shape as the searches in chpeek-4: the first
    attempt is slow and the rest are instant. Nothing in this world is slow twice.

    SO: MORE HOPS, and stop guessing where the line is. Eighty hops is about four seconds, and a
    search is attempted at five points along the way (hops 9, 19, 39, 59, 79 - roughly 1.0 s, 1.5 s,
    2.5 s, 3.5 s and 4.5 s in). Each attempt is refused in ~30 ms, far clear of the next hop, so
    they are sequential and never concurrent - which chpeek-4 showed matters.

    The point is no longer "does a search ever work" but WHEN, and the answer is now a number
    instead of another round of guessing: ch_peek_no9 / no19 / no39 / no59 / no79 say which attempts
    were refused, and the first ch_peek_count says which one got through.

    If ALL FIVE are refused, the warm-up theory is wrong - four and a half seconds is past the +4 s
    where chlobby-9 saw a search return 25 - and the search is being refused for a reason that has
    nothing to do with time. That would be worth knowing too, and it ends the guessing either way.

    No OpenLevel, no Selected Level Name write: this class must never travel."""
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    # RULE 2: the parent performs the Steam login, without which there is nothing to search with.
    g.callparent("bp_parent", HOST_CLASS, "ReceiveBeginPlay")

    g.call("pid", ONLINE, "GetPlatformUserNetId")
    g.call("plat", HTTP_LIB, "GetOnlinePlatformName")
    g.call("inlobby", ONLINE, "IsInLobby")
    g.call("inlobbyS", STR, "Conv_BoolToString")
    g.link(("inlobby.ReturnValue", "inlobbyS.InBool"))
    g.call("alive", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_peek_alive",
        "Storefront": "peek", "FirstSessionTimestamp": PEEK_TAG, "IsFirstGameOpen": "false"})
    g.link(("pid.ReturnValue", "alive.UserId"),
           ("plat.ReturnValue", "alive.Platform"),
           ("inlobbyS.ReturnValue", "alive.Timestamp"))

    _instant(g, "h0", "ch_peek_hop0_ask", "OnPeekHop0")

    # the held reply, still unexplained, still asked - it starts nothing
    g.call("held", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_WAIT_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_peek_held_ask",
        "Storefront": "peek", "FirstSessionTimestamp": PEEK_TAG, "IsFirstGameOpen": "false"})
    g.n("heldd", "createevent", func="OnPeekHeld")
    g.link(("heldd.OutputDelegate", "held.OnResponse"))

    # RULE 5: fan out, never chain through a latent node.
    g.seq("fan", 3)
    g.chain("bp", "bp_parent", "fan")
    g.link(("fan.then_0", "alive.exec"), ("fan.then_1", "h0.exec"), ("fan.then_2", "held.exec"))

    # THE CHAIN. A sampled hop needs THREE pins, not two - marker, next hop, and the search - and
    # hanging two of them off one pin would silently drop one (an exec output takes ONE link).
    for i in range(PEEK_HOPS):
        g.existing("ph%d" % i, "OnPeekHop%d" % i)
        sampled = i in PEEK_SAMPLES
        g.seq("hs%d" % i, 3 if sampled else 2)
        g.chain("ph%d" % i, "hs%d" % i)
        g.link(("hs%d.then_0" % i,
                "%s.exec" % _report_bare(g, "hm%d" % i, "ch_peek_hop%d" % i, tag=PEEK_TAG)))
        if i + 1 < PEEK_HOPS:
            g.link(("hs%d.then_1" % i,
                    "%s.exec" % _instant(g, "h%d" % (i + 1), "ch_peek_hop%d_ask" % (i + 1),
                                         "OnPeekHop%d" % (i + 1))))
        if sampled:
            g.link(("hs%d.then_2" % i if i + 1 < PEEK_HOPS else "hs%d.then_1" % i,
                    "%s.exec" % _search_arm(
                        g, "s%d" % i, ACCESS_KEY, ACCESS_VALUE, "OnPeekFound",
                        "OnPeekFail%d" % i, "ch_peek_at%d" % i, "ch_peek_atproxy%d" % i,
                        tag=PEEK_TAG)))

    g.existing("peekheld", "OnPeekHeld")
    g.chain("peekheld", _report_bare(g, "heldr", "ch_peek_held", tag=PEEK_TAG))

    # --- the read, shared by every sampled attempt ---
    g.existing("peekfound", "OnPeekFound")
    g.call("pklen", ARR, "Array_Length", array=True)
    g.link(("peekfound.SearchResults", "pklen.TargetArray"))
    g.call("pkany", MATH, "Greater_IntInt", {"B": "0"})
    g.link(("pklen.ReturnValue", "pkany.A"))
    g.branch("pkbr")
    g.link(("pkany.ReturnValue", "pkbr.condition"))
    g.chain("peekfound", "pkbr")
    g.link(("pkbr.else", "%s.exec" % _report_bare(g, "pkempty", "ch_peek_empty", tag=PEEK_TAG)))

    g.call("pkfirst", ARR, "Array_Get", {"Index": "0"}, array=True)
    g.link(("peekfound.SearchResults", "pkfirst.TargetArray"))

    g.seq("pkgrp", 1 + len(LOBBY_KEYS) // 4)
    g.link(("pkbr.then", "pkgrp.exec"))
    g.link(("pkgrp.then_0",
            "%s.exec" % _report_count(g, "pkc", "ch_peek_count", "peekfound.SearchResults",
                                      tag=PEEK_TAG)))
    for gi in range(len(LOBBY_KEYS) // 4):
        keys = LOBBY_KEYS[gi * 4:gi * 4 + 4]
        node = _peek_group(g, "pk%d_" % gi, "pkfirst.Item", "ch_peek_%d" % gi, keys, tag=PEEK_TAG)
        g.link(("pkgrp.then_%d" % (gi + 1), "%s.exec" % node))

    # which attempts were refused, and in what order
    for i in PEEK_SAMPLES:
        g.existing("sf%d" % i, "OnPeekFail%d" % i)
        g.chain("sf%d" % i, _report_bare(g, "sfr%d" % i, "ch_peek_no%d" % i, tag=PEEK_TAG))
    return g.json()

def chlobby_logic():
    """BeginPlay: hand control to the real GM_Host first, then say hello to our backend.

    Six string fields ride on SendAttributionEvent (the 2026-09-14 proof):
      user_id                 our SteamID64, so the log says WHO
      platform                GetOnlinePlatformName (on the HTTP library), expected "SteamCore"
      timestamp               IsInLobby as a string - does the lobby layer see a lobby from here
      storefront              a fixed marker, so a probe from the LOBBY is never confused with the
                              one riding along in BB5
      first_session_timestamp the build tag
    """
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    # RULE 2. Everything the lobby does lives behind this call.
    g.callparent("bp_parent", HOST_CLASS, "ReceiveBeginPlay")
    # STAMP OUR NAME BEFORE THE PARENT RUNS. GM_Host's BeginPlay is what reaches CreateLobby, and
    # MakeCreateLobbyParams reads "Session Name" to fill the lobby's `Name` attribute - so this has
    # to be set BEFORE the parent call, not after it. One property write; the game does the rest.
    _gi_set_str(g, "hn_", "Session Name", JOIN_TOKEN)
    # The installed BodycamGI has no references to this writable StrProperty in any of its own
    # functions. It stays local to this process and survives the OpenLevel into GM_BB5.
    _gi_set_str(g, "rt_", "Search String", REPORT_TOKEN)

    g.call("pid", ONLINE, "GetPlatformUserNetId")
    g.call("plat", HTTP_LIB, "GetOnlinePlatformName")     # NOT on BodycamOnlineManager
    g.call("inlobby", ONLINE, "IsInLobby")
    g.call("inlobbyS", STR, "Conv_BoolToString")
    g.link(("inlobby.ReturnValue", "inlobbyS.InBool"))

    g.call("send", HTTP_LIB, "SendAttributionEvent", {
        "URL": PROBE_URL, "BearerToken": "chlobby", "IP": "", "EventName": "ch_lobby_alive",
        "Storefront": "lobby", "FirstSessionTimestamp": BUILD_TAG, "IsFirstGameOpen": "false"})
    g.link(("pid.ReturnValue", "send.UserId"),
           ("plat.ReturnValue", "send.Platform"),
           ("inlobbyS.ReturnValue", "send.Timestamp"))

    # A host must have no lobby search in flight when OpenLevel tears down this world.
    # The old diagnostic peek/legacy join arms reintroduced the chlobby-29 EOS crash
    # when the seed was recooked. Keep searches in GM_CHPeek / GM_CHJoin only.
    asks = [net_probe(g), host_probe(g)]
    # Use the host-only capability already stamped into the seed's FName table.
    # The server can authorize travel without correlating connection addresses.
    g.link(("rt_str.ReturnValue", "hdly.BearerToken"))
    # FAN OUT, never chain, through SendAttributionEvent. It is a LATENT node
    # (meta=(Latent, LatentInfo="LatentInfo")), so its exec output fires only when the latent action
    # completes - and chaining the three calls in a line meant the first one held up the other two
    # forever: chlobby-2 sent ch_lobby_alive and then nothing at all (2026-09-14). A Sequence makes
    # each call independent, which is what they always were.
    g.seq("fan", 1 + len(asks))
    g.chain("bp", "rt_set", "hn_set", "bp_parent", "fan")
    for i, node in enumerate(["send"] + asks):
        g.link(("fan.then_%d" % i, "%s.exec" % node))
    return g.json()


if __name__ == "__main__":
    import json
    for name, graph in (("host_stub_events", host_stub_events()), ("chlobby_logic", chlobby_logic())):
        nodes = json.loads(graph)["nodes"] if isinstance(graph, str) else graph["nodes"]
        print("%-20s %d nodes" % (name, len(nodes)))
