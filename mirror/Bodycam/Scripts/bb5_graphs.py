"""bb5_graphs.py — node graphs for Bodybomb 5v5 (Sam, 2026-09-14), built by UBodycamMirrorTools.BuildGraph like ctf_graphs.py.

What the mode changes against the game's Bodybomb (everything else — bomb, bomb zones, rounds, plant/defuse — is the game's own,
driven by the native ObjectiveRuleSetComponent and the BB_ levels):
  * AC_BB5BombRule (our rule component, child of the native ObjectiveRuleSetComponent, replacing the game's AC_BombRuleComponent):
      - AssignPlayerToObjective(Player): the game hands the bomb to Player's inventory (SpawnSpecialItem) and equips it. We spawn it
        UNEQUIPPED, then a 0.2 s poll drops it with the inventory's own DropItem (the game's drop pipeline: item.Drop + multicast)
        and moves the dropped bomb to the centre of the attacking team's spawn (average of the attackers' pawn locations, floor
        trace) — "the bomb spawns on the ground in the attacker spawn instead of equipped on a random attacker".
      - LookUpForBomb: exactly the game's version (poll until the Bombe actor exists -> OnObjectiveReady).
  * GM_BB5: ShouldSpawnBots = false (like GM_BodyBomb), 4x drone cooldown (GE_BB5_DroneCooldown re-applied every 2 s, the CTF
    pattern), class defaults ConfigDataAsset -> DA_BB5. Rules tags / DefaultDroneClass are written by the pak builder.
  * TEMPORARY (2026-09-14): a SendAttributionEvent probe rides along in GM_BB5 — see the PROBE block below. It exists to
    prove the gamemode can talk to our backend before the competitive mode is built on that assumption, and must be
    deleted once the answer is in. It cannot affect play: a failed HTTP call does nothing.
Stand-ins at the game's paths (referenced, never shipped): Bombe (the bomb item class), BP_InventoryComponent (the three
functions we call, same names + parameter order as the game's, so the by-name calls resolve to the real ones at runtime).
"""
import json
from ctf_graphs import G, P, SYS, MATH, GS_LIB, ARR, ACTOR, PAWN, BC_GM, BC_GS, BC_PS, GM_PARENT, STR, V_TEAMDATA, CONTROLLER, PLAYERSTART

BB5_DIR = "/Game/GM/Gamemode/BB5"
GM_BB5 = "/Game/GM/Gamemode/GM_BB5.GM_BB5_C"
RULE = BB5_DIR + "/AC_BB5BombRule.AC_BB5BombRule_C"
OBJ_RULE = "/Script/Bodycam.ObjectiveRuleSetComponent"
BOMBE_PKG, BOMBE = "/Game/BodycamWeapons/Core/Blueprint/Bombe", "/Game/BodycamWeapons/Core/Blueprint/Bombe.Bombe_C"
INV_PKG, INV = "/Game/AdvancedLocomotionV4/Blueprints/Components/BP_InventoryComponent", "/Game/AdvancedLocomotionV4/Blueprints/Components/BP_InventoryComponent.BP_InventoryComponent_C"
PLAYERSTATE = "/Script/Engine.PlayerState"
GAMESTATE = "/Script/Engine.GameStateBase"
GE_DRONECD = BB5_DIR + "/GE_BB5_DroneCooldown.GE_BB5_DroneCooldown_C"
SPECIAL_SLOT = '(TagName="Inventory.Slots.Special")'   # the inventory slot the game's own AC_BombRuleComponent puts the bomb in
DROP_TRIES = 50            # 0.2 s x 50 = 10 s: after that the bomb simply stays with the attacker (the game's own behaviour)
DRONE_COOLDOWN_FACTOR = 4.0
# Where the dropped bomb ends up (Sam, 2026-09-16, after the 1v1 tests: "make sure it always spawns with the attacking team and
# that it never spawns under the ground ... you could have it spawn at the same height as a player's chest").
PAWN_CENTRE_CM = 90.0                            # a pawn's actor location is its CAPSULE CENTRE, ~90 cm above the floor it stands on
BOMB_CHEST_CM = 120.0                            # chest height above the FLOOR: where the bomb goes when the floor trace found one
BOMB_LIFT_CM = BOMB_CHEST_CM - PAWN_CENTRE_CM    # the same height expressed above the pawns' capsule centres: the no-trace fallback
TEAM_CLUSTER_CM = 2500.0   # a team-mate further than this from the carrier is not standing in the attacker spawn and is not averaged in

# ------------------------------------------------------------------------------------------------------------------
# Authenticated match reporting uses the game HTTP transport.
HTTP_LIB = "/Script/Bodycam.AttributionHttpRequestLibrary"        # stub added to the mirror 2026-09-14
ONLINE = "/Script/Bodycam.BodycamOnlineManager"                   # stub added to the mirror 2026-09-14
ONLINE_TYPES = "/Script/Bodycam.BodycamOnlineTypesLibrary"        # stub added to the mirror 2026-09-14
UPDATE_LOBBY = "/Script/Bodycam.BodycamUpdateLobby"               # stub added to the mirror 2026-09-14
ASYNC_BASE = "/Script/Engine.BlueprintAsyncActionBase"            # Activate(): how a Blueprint kicks off an async node
DESTROY_LOBBY = "/Script/Bodycam.BodycamDestroyLobby"             # stub added to the mirror 2026-09-14
EXIT_DELAY = "15.0"        # seconds of scoreboard before the session is torn down
EXIT_TIMEOUT = "10.0"      # DestroyLobby's own timeout
REPORT_URL = "https://lightsout.up.railway.app/api/match-report"
REPORT_TOKEN_PROP = "Search String"
GI_PKG = "/Game/MenuSystemPro/Blueprints/Core/BodycamGI"
GI_CLASS = GI_PKG + ".BodycamGI_C"


def combat_transport():
    """Manager queue transport; capability stays in the private GameInstance field."""
    g = G(); g.existing('combat_transmit', 'CombatTransmit')
    g.call('combat_host', GS_LIB, 'GetPlayerState', {'PlayerStateIndex': '0'})
    g.call('combat_hostid', ONLINE, 'RetrievePlatformIdAsStringFromPlayerState')
    g.link(('combat_host.ReturnValue', 'combat_hostid.PlayerState'))
    _report_send(g, 'combat_http', {'URL': REPORT_URL, 'EventName': 'ch_combat_v1'})
    g.link(('combat_transmit.Row', 'combat_http.Platform'), ('combat_hostid.ReturnValue', 'combat_http.UserId'))
    g.n('combat_response', 'createevent', func='CombatReportResult')
    g.link(('combat_response.OutputDelegate', 'combat_http.OnResponse'))
    g.chain('combat_transmit', 'combat_http')
    return g.json()


def _report_send(g, node, defaults):
    """Send one authenticated match-world report using the host-only GameInstance token.

    The lobby pak writes the per-match value into BodycamGI before travel. GameInstance survives
    OpenLevel, while neither the public Steam lobby nor GM_BB5's cooked defaults receive it.
    """
    values = dict(defaults)
    values.pop("BearerToken", None)
    g.call(node, HTTP_LIB, "SendAttributionEvent", values)
    p = node + "_auth_"
    g.call(p + "gi", GS_LIB, "GetGameInstance")
    g.cast(p + "cast", GI_CLASS, pure=True)
    g.get(p + "token", REPORT_TOKEN_PROP, GI_CLASS)
    g.link((p + "gi.ReturnValue", p + "cast.cast_object"),
           (p + "cast.cast_result", p + "token.self"),
           (p + "token." + REPORT_TOKEN_PROP, node + ".BearerToken"))

# The lobby half of the proof. CreateLobby/UpdateLobby/FindLobbies all take an arbitrary
# TMap<FString, FBodycamLobbyAttribute> of session attributes, and FindLobbies SEARCHES on
# them — so if we can stamp a match id onto the live lobby, the backend can hand ten players
# a code and their games can find each other. This writes one and reads it back.
LOBBY_KEY = "CH_MATCH"
LOBBY_VALUE = ""
SESSION_NAME = "GameSession"     # UE's default session name; a wrong one makes UpdateLobby a no-op
LOBBY_MIN_MAXPLAYERS = "10"      # never write MaxPlayers 0 into a live lobby if the getter returns 0
T_READ, T_WRITE, T_VERIFY = "12.0", "30.0", "45.0"    # seconds after BeginPlay

# ---------------------------------------------------------------------------------------------
# THE MATCH-WORLD CALLBACK PROBE (2026-09-15, branch feat/autojoin-and-teams).
# See docs/autojoin-teams.md. Two questions, one cook, both of them blocking the team system:
#
#   1. DOES A RESPONSE DELEGATE FIRE IN THE MATCH WORLD AT ALL? In the LOBBY world it is proven
#      that it does not, except from BeginPlay's own exec (chlobby-12 and -13 both stalled with
#      the request sent and the callback never arriving). The stated reason - latent HTTP
#      callbacks route through a world manager that does not tick there - implies the match
#      world is fine, because the match world ticks. IMPLIES. Every probe GM_BB5 sends today is
#      fire-and-forget, so the pak has never once bound a response delegate in here.
#   2. WHAT INTEGERS ARE THE TWO TEAMS? ABodycamPlayerState starts TeamID = -1 and our own CTF
#      only ever compares ids rather than asserting their values, so 0/1 vs 1/2 is unknown.
#      Guessing inverts the teams, which is worse than not assigning them because it LOOKS like
#      it worked. One report of what the game actually assigned settles it.
#
# Both answers arrive in the same run. Nothing is built on either until they do.
T_TEAMBIT = "20.0"         # seconds after BeginPlay: late enough that players have real teams
PROBE_URL = REPORT_URL
PROBE_TEAM_URL = REPORT_URL + "/team"
PROBE_404_URL = REPORT_URL + "/no-such-route"    # a 404 is free and needs no server change

# ==================================================================================================================
# stand-ins (the game's classes, only what we call)
# ==================================================================================================================
def inventory_events():
    """BP_InventoryComponent stand-in: SpawnSpecialItem is a custom event in the game's BP (class param passed by reference there,
    so callers hand it a variable, never a literal — see rule_assign)."""
    g = G()
    g.custom("spawn", "SpawnSpecialItem", [P("ItemClass", "class", **{"class": ACTOR}), P("bShouldEquip", "bool")])
    return g.json()

def inventory_signature(name):
    g = G()
    if name == "DropItem":
        g.entry(params=[P("ItemSlot", "struct", struct="/Script/GameplayTags.GameplayTag")]); g.result(params=[P("WasDropped", "bool")])
    elif name == "GetItemForSlot":
        g.entry(params=[P("ItemSlot", "struct", struct="/Script/GameplayTags.GameplayTag")])
        g.result(params=[P("Item", "object", **{"class": ACTOR}), P("IsValid", "bool"), P("ItemIndex", "int")])
    else: raise KeyError(name)
    return g.json()
INVENTORY_FUNCTIONS = ["DropItem", "GetItemForSlot"]

# ==================================================================================================================
# AC_BB5BombRule
#   vars: BombClass class<Actor>, Attacker PlayerState, DropTries int, TeamPawns Actor[], SpawnCentre vector
# ==================================================================================================================
def rule_events():
    g = G()
    g.custom("drop", "DropBombWhenReady")
    g.custom("place", "PlaceBomb")
    g.custom("poll", "PollBomb")
    return g.json()

def rule_assign():
    """Override AssignPlayerToObjective(Player) -> Actor. The game's version: pawn -> BP_InventoryComponent -> SpawnSpecialItem(Bombe,
    equip=true); return the Bombe actor. Ours: equip=false, then start the drop poll; same return value."""
    g = G()
    g.entry(); g.result()
    g.set("clearbomb", "CurrentBomb"); g.chain("entry", "clearbomb")
    g.selfnode("resetself"); g.call("resetplace", SYS, "K2_ClearTimer", {"FunctionName": "PlaceBomb"})
    g.link(("resetself.self", "resetplace.Object")); g.chain("clearbomb", "resetplace")
    g.set("cls", "BombClass", defaults={"BombClass": BOMBE}); g.chain("resetplace", "cls")  # SpawnSpecialItem takes a class variable by reference
    g.set("att", "Attacker"); g.link(("entry.Player", "att.Attacker")); g.chain("cls", "att")
    g.set("tries", "DropTries", defaults={"DropTries": "0"}); g.chain("att", "tries")
    g.call("pawn", PLAYERSTATE, "GetPawn"); g.link(("entry.Player", "pawn.self"))
    g.call("inv", ACTOR, "GetComponentByClass", {"ComponentClass": INV}); g.link(("pawn.ReturnValue", "inv.self"))
    g.cast("asinv", INV, pure=True); g.link(("inv.ReturnValue", "asinv.cast_object"))
    g.get("clsg", "BombClass")
    g.call("spawn", INV, "SpawnSpecialItem", {"bShouldEquip": "false"}); g.link(("asinv.cast_result", "spawn.self"), ("clsg.BombClass", "spawn.ItemClass"))
    g.chain("tries", "spawn")
    g.call("timer", SYS, "K2_SetTimer", {"FunctionName": "DropBombWhenReady", "Time": "0.2", "bLooping": "true"}); g.selfnode("s1"); g.link(("s1.self", "timer.Object"))
    g.chain("spawn", "timer")
    # Creation may be pending while the inventory initializes. Return its actual
    # slot item if ready; DropBombWhenReady retains it before dropping, and the
    # native LookUpForBomb -> PollBomb path waits for that exact actor.
    g.call("slot", INV, "GetItemForSlot", {"ItemSlot": SPECIAL_SLOT}); g.link(("asinv.cast_result", "slot.self")); g.chain("timer", "slot")
    g.cast("bomb", BOMBE, pure=True); g.link(("slot.Item", "bomb.cast_object"), ("bomb.cast_result", "result.ReturnValue"))
    g.set("keepbomb", "CurrentBomb"); g.link(("bomb.cast_result", "keepbomb.CurrentBomb")); g.chain("slot", "keepbomb", "result")
    return g.json()

def rule_logic():
    g = G()
    # --- DropBombWhenReady (server timer, 0.2 s): once the bomb sits in the attacker's Special slot, drop it with the game's own
    #     DropItem; give up after DROP_TRIES (the bomb then stays with the attacker, exactly as in the stock mode) ---
    g.existing("drop", "DropBombWhenReady")
    g.get("t0", "DropTries"); g.call("inc", MATH, "Add_IntInt", {"B": "1"}); g.link(("t0.DropTries", "inc.A"))
    g.set("t1", "DropTries"); g.link(("inc.ReturnValue", "t1.DropTries")); g.chain("drop", "t1")
    g.get("t2", "DropTries"); g.call("over", MATH, "Greater_IntInt", {"B": str(DROP_TRIES)}); g.link(("t2.DropTries", "over.A"))
    g.branch("br_over"); g.link(("over.ReturnValue", "br_over.condition")); g.chain("t1", "br_over")
    g.call("stop_a", SYS, "K2_ClearTimer", {"FunctionName": "DropBombWhenReady"}); g.selfnode("s2"); g.link(("s2.self", "stop_a.Object"), ("br_over.then", "stop_a.exec"))
    g.get("att", "Attacker"); g.call("pawn", PLAYERSTATE, "GetPawn"); g.link(("att.Attacker", "pawn.self"))
    g.call("pv", SYS, "IsValid"); g.link(("pawn.ReturnValue", "pv.Object"))
    g.branch("br_pawn"); g.link(("pv.ReturnValue", "br_pawn.condition"), ("br_over.else", "br_pawn.exec"))
    g.call("inv", ACTOR, "GetComponentByClass", {"ComponentClass": INV}); g.link(("pawn.ReturnValue", "inv.self"))
    g.cast("asinv", INV, pure=True); g.link(("inv.ReturnValue", "asinv.cast_object"))
    g.call("slot", INV, "GetItemForSlot", {"ItemSlot": SPECIAL_SLOT}); g.link(("asinv.cast_result", "slot.self"), ("br_pawn.then", "slot.exec"))
    g.branch("br_has"); g.link(("slot.IsValid", "br_has.condition")); g.chain("slot", "br_has")
    g.cast("slotbomb", BOMBE); g.link(("slot.Item", "slotbomb.cast_object"), ("br_has.then", "slotbomb.exec"))
    g.set("retainbomb", "CurrentBomb"); g.link(("slot.Item", "retainbomb.CurrentBomb")); g.chain("slotbomb", "retainbomb")
    g.call("dropit", INV, "DropItem", {"ItemSlot": SPECIAL_SLOT}); g.link(("asinv.cast_result", "dropit.self")); g.chain("retainbomb", "dropit")
    g.branch("br_dropped"); g.link(("dropit.WasDropped", "br_dropped.condition")); g.chain("dropit", "br_dropped")
    g.call("stop_b", SYS, "K2_ClearTimer", {"FunctionName": "DropBombWhenReady"}); g.selfnode("s3"); g.link(("s3.self", "stop_b.Object"), ("br_dropped.then", "stop_b.exec"))
    g.call("placetimer", SYS, "K2_SetTimer", {"FunctionName": "PlaceBomb", "Time": "0.3", "bLooping": "false"}); g.selfnode("s4"); g.link(("s4.self", "placetimer.Object"))
    g.chain("stop_b", "placetimer")   # a beat later: the drop has started (physics on, detached from the hand) before we move it

    # --- PlaceBomb: the dropped bomb -> centre of the ATTACKING team's spawn, at chest height. Three separate guarantees, two of
    #     them added 2026-09-16 after the 1v1 tests ("always ... with the attacking team", "never ... under the ground"):
    #
    #       1. IT IS THE ATTACKING TEAM. The filter is the carrier's TeamID - correct by construction, because the game only calls
    #          AssignPlayerToObjective for a player on the attacking side. What was NOT safe is what happens when that read fails:
    #          a failed pure cast or a carrier who left reads TeamID as 0, and 0 is a REAL team id here (measured: the two teams
    #          are 0 and 1), so the old graph would have quietly gathered the DEFENDERS and posted the bomb in their spawn. Now a
    #          carrier that is not a BodycamPlayerState with an assigned team aborts the move outright, and the bomb stays exactly
    #          where it fell - at the carrier's own feet, which is on the attacking team by definition. The same cast_ok guard is
    #          applied per player in the sweep, so an unassigned (-1) or non-Bodycam state can never read as "team 0" and join in.
    #       2. WITH THE TEAM, NOT BETWEEN IT. Only team-mates within TEAM_CLUSTER_CM of the carrier are averaged. One player who
    #          already ran for a site cannot drag the average out of the spawn and into a wall halfway across the map. With no
    #          carrier pawn to measure from (he died in the 0.3 s gap) there is no anchor and every team-mate counts, as before.
    #       3. NOT IN THE GROUND - see rule_fn_AverageOf, which owns that one.
    g.existing("place", "PlaceBomb")
    g.get("tp0", "TeamPawns"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("tp0.TeamPawns", "clr.TargetArray")); g.chain("place", "clr")
    g.set("anchor0", "bHaveAnchor", defaults={"bHaveAnchor": "false"}); g.chain("clr", "anchor0")
    g.get("att2", "Attacker"); g.cast("attbps", BC_PS, pure=True); g.link(("att2.Attacker", "attbps.cast_object"))
    g.get("attteam", "TeamID", BC_PS); g.link(("attbps.cast_result", "attteam.self"))
    g.call("attassigned", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link(("attteam.TeamID", "attassigned.A"))   # ABodycamPlayerState::TeamID starts at -1
    g.call("attok", MATH, "BooleanAND"); g.link(("attbps.cast_ok", "attok.A"), ("attassigned.ReturnValue", "attok.B"))
    g.branch("br_att"); g.link(("attok.ReturnValue", "br_att.condition")); g.chain("anchor0", "br_att")   # else: leave the bomb at the carrier's feet
    g.call("attpawn", PLAYERSTATE, "GetPawn"); g.link(("att2.Attacker", "attpawn.self"))
    g.call("attpv", SYS, "IsValid"); g.link(("attpawn.ReturnValue", "attpv.Object"))
    g.branch("br_anchor"); g.link(("attpv.ReturnValue", "br_anchor.condition"), ("br_att.then", "br_anchor.exec"))
    g.call("attloc", ACTOR, "K2_GetActorLocation"); g.link(("attpawn.ReturnValue", "attloc.self"))
    g.set("anchor1", "AnchorLoc"); g.link(("attloc.ReturnValue", "anchor1.AnchorLoc"), ("br_anchor.then", "anchor1.exec"))
    g.set("anchor2", "bHaveAnchor", defaults={"bHaveAnchor": "true"}); g.chain("anchor1", "anchor2")
    g.call("gs", GS_LIB, "GetGameState"); g.get("pa", "PlayerArray", GAMESTATE); g.link(("gs.ReturnValue", "pa.self"))
    g.foreach("fe"); g.link(("pa.PlayerArray", "fe.Array"), ("anchor2.then", "fe.Exec"), ("br_anchor.else", "fe.Exec"))
    g.cast("psb", BC_PS, pure=True); g.link(("fe.Array Element", "psb.cast_object"))
    g.get("pteam", "TeamID", BC_PS); g.link(("psb.cast_result", "pteam.self"))
    g.call("same", MATH, "EqualEqual_IntInt"); g.link(("pteam.TeamID", "same.A"), ("attteam.TeamID", "same.B"))
    g.call("samereal", MATH, "BooleanAND"); g.link(("same.ReturnValue", "samereal.A"), ("psb.cast_ok", "samereal.B"))   # a failed cast reads TeamID 0, which is a real team
    g.call("ppawn", PLAYERSTATE, "GetPawn"); g.link(("fe.Array Element", "ppawn.self"))
    g.call("ppv", SYS, "IsValid"); g.link(("ppawn.ReturnValue", "ppv.Object"))
    g.call("ploc", ACTOR, "K2_GetActorLocation"); g.link(("ppawn.ReturnValue", "ploc.self"))
    g.get("anchor3", "AnchorLoc"); g.call("pdist", MATH, "Vector_Distance"); g.link(("ploc.ReturnValue", "pdist.V1"), ("anchor3.AnchorLoc", "pdist.V2"))
    g.call("pnear", MATH, "Less_DoubleDouble", {"B": str(TEAM_CLUSTER_CM)}); g.link(("pdist.ReturnValue", "pnear.A"))
    g.get("anchor4", "bHaveAnchor"); g.call("noanchor", MATH, "Not_PreBool"); g.link(("anchor4.bHaveAnchor", "noanchor.A"))
    g.call("pkeep", MATH, "BooleanOR"); g.link(("pnear.ReturnValue", "pkeep.A"), ("noanchor.ReturnValue", "pkeep.B"))
    g.call("alive", MATH, "BooleanAND"); g.link(("samereal.ReturnValue", "alive.A"), ("ppv.ReturnValue", "alive.B"))
    g.call("both", MATH, "BooleanAND"); g.link(("alive.ReturnValue", "both.A"), ("pkeep.ReturnValue", "both.B"))
    g.branch("br_mate"); g.link(("both.ReturnValue", "br_mate.condition"), ("fe.LoopBody", "br_mate.exec"))
    g.get("tp1", "TeamPawns"); g.call("add", ARR, "Array_Add", array=True); g.link(("tp1.TeamPawns", "add.TargetArray"), ("ppawn.ReturnValue", "add.NewItem"), ("br_mate.then", "add.exec"))
    g.get("tp2", "TeamPawns"); g.call("n", ARR, "Array_Length", array=True); g.link(("tp2.TeamPawns", "n.TargetArray"))
    g.call("any", MATH, "Greater_IntInt", {"B": "0"}); g.link(("n.ReturnValue", "any.A"))
    g.branch("br_any"); g.link(("any.ReturnValue", "br_any.condition"), ("fe.Completed", "br_any.exec"))
    g.selfcall("avg", RULE, "AverageOf"); g.get("tp3", "TeamPawns"); g.link(("tp3.TeamPawns", "avg.Pawns"), ("br_any.then", "avg.exec"))
    g.set("centre", "SpawnCentre"); g.link(("avg.Centre", "centre.SpawnCentre")); g.chain("avg", "centre")
    g.get("bomb", "CurrentBomb")
    g.call("bv", SYS, "IsValid"); g.link(("bomb.CurrentBomb", "bv.Object"))
    g.branch("br_bomb"); g.link(("bv.ReturnValue", "br_bomb.condition")); g.chain("centre", "br_bomb")
    g.get("centre2", "SpawnCentre")
    g.call("move", ACTOR, "K2_SetActorLocation", {"bSweep": "false", "bTeleport": "true"}); g.link(("bomb.CurrentBomb", "move.self"), ("centre2.SpawnCentre", "move.NewLocation"), ("br_bomb.then", "move.exec"))

    # --- LookUpForBomb (the game calls it on the component): the game's own AC_BombRuleComponent polls every 0.2 s until the
    #     Bombe actor exists, then OnObjectiveReady(bomb). Same here, with a timer instead of a Delay loop. ---
    g.event("lookup", "LookUpForBomb", OBJ_RULE)
    g.call("polltimer", SYS, "K2_SetTimer", {"FunctionName": "PollBomb", "Time": "0.2", "bLooping": "true"}); g.selfnode("s5"); g.link(("s5.self", "polltimer.Object"))
    g.chain("lookup", "polltimer")
    g.existing("poll", "PollBomb")
    g.get("bomb2", "CurrentBomb")
    g.call("bv2", SYS, "IsValid"); g.link(("bomb2.CurrentBomb", "bv2.Object"))
    g.branch("br_ready"); g.link(("bv2.ReturnValue", "br_ready.condition")); g.chain("poll", "br_ready")
    # Inventory creation can finish after the bounded drop attempts. Continue to
    # register that assigned bomb even when it stays in the carrier's inventory.
    g.call("pollpv", SYS, "IsValid"); g.link(("pawn.ReturnValue", "pollpv.Object"))
    g.branch("pollpawn"); g.link(("pollpv.ReturnValue", "pollpawn.condition"), ("br_ready.else", "pollpawn.exec"))
    g.call("pollslot", INV, "GetItemForSlot", {"ItemSlot": SPECIAL_SLOT}); g.link(("asinv.cast_result", "pollslot.self"), ("pollpawn.then", "pollslot.exec"))
    g.cast("pollbomb", BOMBE); g.link(("pollslot.Item", "pollbomb.cast_object")); g.chain("pollslot", "pollbomb")
    g.set("pollkeep", "CurrentBomb"); g.link(("pollslot.Item", "pollkeep.CurrentBomb")); g.chain("pollbomb", "pollkeep")
    g.call("stop_c", SYS, "K2_ClearTimer", {"FunctionName": "PollBomb"}); g.selfnode("s6"); g.link(("s6.self", "stop_c.Object"), ("br_ready.then", "stop_c.exec"))
    g.chain("pollkeep", "stop_c")
    g.call("ready", OBJ_RULE, "OnObjectiveReady"); g.link(("bomb2.CurrentBomb", "ready.TheObjectiveActor")); g.chain("stop_c", "ready")
    return g.json()

# ------------------------------------------------------------------------------------------------------------------
# PER-PLAYER STATS (Sam, 2026-09-15: "get the pak to send per player stats")
#
# ONE PLAYER PER TICK, and the tick is the loop. That shape is forced by three separate constraints, each of which has
# already cost this project something:
#
#   1. SendAttributionEvent is LATENT. Unreal's latent action manager keys on the node's UUID plus the calling object,
#      so the SAME latent node fired ten times inside a ForEach does not send ten requests - the repeats are dropped.
#      A loop was never going to work. (The alternative, ten unrolled send nodes, is ten copies of this graph.)
#   2. A field long enough for the whole roster is UNMEASURED. Packing ten players into one call needs ~280 characters
#      in one string and nothing the game has ever sent is over 30. One player per call needs about 40.
#   3. A cursor is state, and state cannot live on GM_BB5 - the pak builder writes the cooked CDO by property index, so
#      one extra own-property on a gamemode shifts every later index. It lives here, on the component, which may hold
#      variables (memory: gamemode-classes-cannot-hold-variables). This is also why gm_probe uses three timers instead
#      of one counter.
#
# So: a looping timer walks StatCursor through PlayerArray, one player per fire, wrapping with a modulo. At 3 s a ten
# player roster is swept every 30 s, and a Bodybomb match runs minutes - so the backend gets several complete passes
# and the last one before the final round carries near-final numbers. Reports are running totals and the server keeps
# the newest per player, so a dropped call costs one refresh and nothing else.
#
# WHAT GOES OUT, per call:
#   event_name  "ch_bb5_stats"
#   user_id     the HOST's SteamID64 - the REPORTER, not the subject. The server authenticates the report against the
#               match host, so this must stay the host on every call or nine of ten are refused.
#   storefront  the CH_MATCH id off the live lobby
#   platform    "<subjectSteamId>|<kills>:<deaths>:<roundsPlayed>:<score>"
#   timestamp   GetCurrentRound()
#
# `roundsPlayed` is SpawnCount: the mode is one life per round, so spawns and rounds-played are the same number, and it
# is what tells the backend a substitute played four rounds of twelve. `score` is the game's own APlayerState score,
# which is what its scoreboard sorts on (SortPlayersByScore) - what feeds it in Bodybomb is NOT verified, so the
# backend weights it modestly and treats it as one component among several.
# Seconds between players. 1 s, not the original 3: the index is trunc(GetRoundElapsedSeconds())
# modulo the roster, so a 1 s tick walks 0,1,2,... and covers ten players every TEN seconds instead
# of every thirty. That matters for one open question in particular - whether Kill/Death reset each
# round - which needs samples either side of a round boundary, and at 30 s a boundary slips through
# the gap. It is also what made the last two matches read 0/0 and settle nothing.
STAT_PERIOD = "1.0"
STATS_EVENT = "ch_bb5_stats"
ROUND_EVENT = "ch_bb5_round"      # from OnRoundEnded - exact, but unproven to fire every round
STATE_EVENT = "ch_bb5_state"      # from a plain timer - always fires, see gm_heartbeat
STATE_PERIOD = "5.0"
KILL_EVENT = "ch_bb5_kill"
GM_LIB = "/Script/Bodycam.BodycamGameModeLibrary"    # GetPlayerScore / GetPlayerTeamID, both BlueprintPure

def gm_stat_report(g):
    """The stats sweep. Runs on GM_BB5, and keeps NO STATE AT ALL.

    THE CURSOR IS GONE, not moved (2026-09-15). It had to live on the component, because a
    gamemode in this pak may not declare a variable - and aiming the timer at the component to
    reach it is precisely what stopped the sweep from ever firing (see gm_stats). Moving only the
    variable and leaving the timer on Self would have meant writing a component's variable from
    the GameMode, which nothing in this project has ever done and which would be the second
    unproven mechanism in a row.

    So the index is DERIVED instead: `trunc(GetRoundElapsedSeconds()) % roster`. The clock is
    already there, already advances on its own, and belongs to nobody. With a 3 s timer the index
    walks 0, 3, 6, 9, 2, 5, ... and because 3 and 10 share no factor it still visits every player
    - one full sweep per ten ticks, the same coverage the cursor gave, with nothing to store.

    That also makes this graph structurally identical to gm_score, which is the arrangement the
    probe log proves works."""
    g.existing("ev_stat", "StatReport")
    g.call("st_gs", GS_LIB, "GetGameState")
    g.get("st_pa", "PlayerArray", GAMESTATE); g.link(("st_gs.ReturnValue", "st_pa.self"))
    g.call("st_n", ARR, "Array_Length", array=True); g.link(("st_pa.PlayerArray", "st_n.TargetArray"))

    # Nothing to report before anyone has a PlayerState. Without this the modulo below divides by zero.
    g.call("st_any", MATH, "Greater_IntInt", {"B": "0"}); g.link(("st_n.ReturnValue", "st_any.A"))
    g.get("st_rule", "BB5BombRule")
    g.get("st_started", "CompetitiveStarted", RULE); g.link(("st_rule.BB5BombRule", "st_started.self"))
    g.branch("st_live"); g.link(("st_started.CompetitiveStarted", "st_live.condition")); g.chain("ev_stat", "st_live")
    g.branch("st_br"); g.link(("st_any.ReturnValue", "st_br.condition")); g.chain("st_live", "st_br")

    # The derived cursor. Modulo keeps it in range whatever the roster does - and the roster can
    # SHRINK between two ticks (somebody disconnects), which a stored index would not survive.
    g.cast("st_asgs0", BC_GS, pure=True); g.link(("st_gs.ReturnValue", "st_asgs0.cast_object"))
    g.call("st_el", BC_GS, "GetRoundElapsedSeconds"); g.link(("st_asgs0.cast_result", "st_el.self"))
    g.call("st_eli", MATH, "FTrunc"); g.link(("st_el.ReturnValue", "st_eli.A"))
    # ABS BEFORE THE MODULO, and this is not defensive tidying - it is a CRASH FIX.
    #
    # C++ keeps the sign of the left operand, so -5 % 10 is -5, and Array_Get with a negative index
    # reads out of bounds. That is exactly what Sam's game did on 2026-09-15: EXCEPTION_ACCESS_
    # VIOLATION *reading address 0xffffffffffffffff* - address -1 - 48 s after launch, twice, with
    # not one probe event sent beforehand.
    #
    # GetRoundElapsedSeconds() is negative during the warm-up countdown, so the index was negative
    # whenever the sweep fired before the round proper began. The bug was always here; taking
    # STAT_PERIOD from 3 s to 1 s is what made it reachable, because the first tick now lands at
    # t=1 s - inside the warm-up - instead of at t=3 s.
    #
    # Abs cannot change the cycling: with the roster length guarded above zero, abs(x) % n is
    # always in [0, n-1], so every index is in range by construction rather than by luck.
    g.call("st_elabs", MATH, "Abs_Int"); g.link(("st_eli.ReturnValue", "st_elabs.A"))
    g.call("st_idx", MATH, "Percent_IntInt")
    g.link(("st_elabs.ReturnValue", "st_idx.A"), ("st_n.ReturnValue", "st_idx.B"))
    g.call("st_get", ARR, "Array_Get", array=True)
    g.link(("st_pa.PlayerArray", "st_get.TargetArray"), ("st_idx.ReturnValue", "st_get.Index"))
    g.cast("st_ps", BC_PS, pure=True); g.link(("st_get.Item", "st_ps.cast_object"))

    # The subject's numbers. Kill/Death/SpawnCount are replicated properties on ABodycamPlayerState, so the host can
    # read them for EVERY player, not only its own - which is the whole reason one machine can report the roster.
    g.get("st_k", "Kill", BC_PS); g.link(("st_ps.cast_result", "st_k.self"))
    g.get("st_d", "Death", BC_PS); g.link(("st_ps.cast_result", "st_d.self"))
    g.get("st_sp", "SpawnCount", BC_PS); g.link(("st_ps.cast_result", "st_sp.self"))
    g.get("st_tm", "TeamID", BC_PS); g.link(("st_ps.cast_result", "st_tm.self"))
    g.call("st_al", BC_PS, "IsPlayerAlive"); g.link(("st_ps.cast_result", "st_al.self"))
    g.call("st_sc", GM_LIB, "GetPlayerScore"); g.link(("st_get.Item", "st_sc.Player"))
    g.call("st_sid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState"); g.link(("st_get.Item", "st_sid.PlayerState"))

    # The REPORTER is the host, always - player state 0, the same node the probe has been using since 2026-09-14. The
    # subject's id rides inside the row instead. Confusing the two would have the server refuse nine calls in ten.
    g.call("st_hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("st_hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState"); g.link(("st_hps.ReturnValue", "st_hid.PlayerState"))

    for node, src in (("st_kS", "st_k.Kill"), ("st_dS", "st_d.Death"),
                      ("st_spS", "st_sp.SpawnCount"), ("st_scS", "st_sc.ReturnValue"),
                      ("st_tmS", "st_tm.TeamID")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))
    g.call("st_alS", STR, "Conv_BoolToString"); g.link(("st_al.ReturnValue", "st_alS.InBool"))

    def _cat(node, a_pin, b_pin=None, literal=None):
        g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
        g.link((a_pin, node + ".A"))
        if b_pin:
            g.link((b_pin, node + ".B"))
        return node + ".ReturnValue"

    # THE KEYED ROW (2026-09-15): <steamId>|k=3;d=1;sp=4;s=250;t=0;a=true
    #
    # Positional is gone. A field was measured to carry 2048 characters intact, so the narrowness
    # that forced it never existed, and it cost us twice - a negative score matched nothing and
    # binned every row, and an all-optional tail let `0:0:-1:0` parse by SKIPPING a field. Neither
    # is expressible once a field is named. See docs/round-context.md section 3b.
    #
    # ';' BETWEEN FIELDS, NEVER ','. The server splits `rows` on ',' so one call can carry several
    # players, so a comma here is eaten by that split: the row is torn up, only the first fragment
    # keeps its SteamID, and exactly one field survives with nothing reporting an error.
    p = _cat("st_c1", "st_sid.ReturnValue", literal="|k=")
    p = _cat("st_c2", p, b_pin="st_kS.ReturnValue")
    p = _cat("st_c3", p, literal=";d=")
    p = _cat("st_c4", p, b_pin="st_dS.ReturnValue")
    p = _cat("st_c5", p, literal=";sp=")
    p = _cat("st_c6", p, b_pin="st_spS.ReturnValue")
    p = _cat("st_c7", p, literal=";s=")
    p = _cat("st_c8", p, b_pin="st_scS.ReturnValue")
    p = _cat("st_c9", p, literal=";t=")
    p = _cat("st_c10", p, b_pin="st_tmS.ReturnValue")
    p = _cat("st_c11", p, literal=";a=")
    p = _cat("st_c12", p, b_pin="st_alS.ReturnValue")

    g.cast("st_asgs", BC_GS, pure=True); g.link(("st_gs.ReturnValue", "st_asgs.cast_object"))
    g.call("st_rd", BC_GS, "GetCurrentRound"); g.link(("st_asgs.cast_result", "st_rd.self"))
    g.call("st_rdS", STR, "Conv_IntToString"); g.link(("st_rd.ReturnValue", "st_rdS.InInt"))

    g.call("st_info", ONLINE, "GetCurrentLobbyInfo")
    g.call("st_attr", ONLINE_TYPES, "GetLobbyInfoStringAttribute", {"Key": LOBBY_KEY})
    g.link(("st_info.ReturnValue", "st_attr.LobbyInfo"))

    _report_send(g, "st_send", {
        "URL": PROBE_URL, "IP": "", "EventName": STATS_EVENT,
        "FirstSessionTimestamp": "", "IsFirstGameOpen": "false"})
    g.link(("st_hid.ReturnValue", "st_send.UserId"),
           ("st_attr.OutValue", "st_send.Storefront"),
           ("st_c12.ReturnValue", "st_send.Platform"),
           ("st_rdS.ReturnValue", "st_send.Timestamp"))
    # BOTS HAVE NO STEAM ID. Measured 2026-09-15 in the first match with bots filling the lobby:
    # RetrievePlatformIdAsStringFromPlayerState answers an EMPTY STRING for a bot, so the row went
    # out as `|1:0:4:1` with nothing before the pipe. The server drops those (it needs 17 digits),
    # which is right - a bot must never touch a ladder - but 40 of the 50 events that match sent
    # were unusable, and an empty id is indistinguishable from a lookup that failed on a REAL
    # player. So the send is skipped instead: the subject is checked here, and a tick that lands
    # on a bot simply does nothing until the next one three seconds later.
    g.call("st_isbot", STR, "EqualEqual_StrStr", {"B": ""})
    g.link(("st_sid.ReturnValue", "st_isbot.A"))
    g.branch("st_br2"); g.link(("st_isbot.ReturnValue", "st_br2.condition"))
    g.chain("st_br", "st_br2")
    g.link(("st_br2.else", "st_send.exec"))   # else = the id is NOT empty, i.e. a real player
    # NOTHING TO ADVANCE. The index comes off the round clock, so there is no cursor to write back
    # and the send is the end of the chain. The two nodes that used to bump StatCursor lived here;
    # deleting the variable without deleting them is what the build caught on the first re-run.

def rule_signature(name):
    g = G()
    if name == "AverageOf":
        g.entry(params=[P("Pawns", "object", **{"class": ACTOR, "array": True})]); g.result(params=[P("Centre", "struct", struct="/Script/CoreUObject.Vector")])
    elif name == "GatherStarts":
        g.entry()
    elif name == "LatchFrom":
        g.entry(params=[P("Player", "object", **{"class": CONTROLLER}), P("Start", "object", **{"class": ACTOR})])
    elif name == "SideForPlayer":
        g.entry(params=[P("Player", "object", **{"class": CONTROLLER})]); g.result(params=[P("Side", "int")])
    elif name == "PickStart":
        g.entry(params=[P("Side", "int")]); g.result(params=[P("Start", "object", **{"class": ACTOR})])
    elif name == "ResolveStart":
        g.entry(params=[P("Player", "object", **{"class": CONTROLLER}), P("Fallback", "object", **{"class": ACTOR})]); g.result(params=[P("Start", "object", **{"class": ACTOR})])
    elif name in ("RecountTeams", "BeginCompetitive"):
        g.entry()
    elif name in ("StartRoster", "FinalRows"):
        g.entry(); g.result(params=[P("ReturnValue", "string")])
    else: raise KeyError(name)
    return g.json()

def rule_fn_AverageOf():
    """AverageOf(Pawns) -> Centre: the average location of the pawns, lifted to CHEST HEIGHT above the floor beneath it.

    Sam, 2026-09-16, after the 1v1 tests: "it never spawns under the ground ... you could have it spawn at the same height as a
    player's chest". The floor is a Visibility trace from 50 cm above the average down to 300 cm below it, and the hit is trusted
    only when the trace ran, did not begin inside geometry, and landed BELOW the average (a railing between the players is not
    the floor). That is FloorPoint's rule set from ctf_graphs, plus two things the old version got wrong:

      * THE PAWNS ARE IGNORED BY THE TRACE. Characters block Visibility, and the trace starts 50 cm above a capsule CENTRE -
        i.e. inside a capsule that reaches ~88 cm above it. In a 1v1 there is one pawn and the average IS his capsule centre, so
        the trace began inside him every single time: bInitialOverlap, no floor, fall through to the raw average - which put the
        bomb inside the player, where physics depenetration is free to eject it in any direction, including down through a thin
        floor. That is the one instance of the bomb in the ground. ActorsToIgnore takes the whole team, so the trace now reports
        the floor rather than a team-mate.
      * THE RESULT CAN NEVER BE BELOW THE PAWNS. Both branches sit at chest height: the floor hit + BOMB_CHEST_CM when there is
        a floor, the average + BOMB_LIFT_CM when there is not - and the second is the same height, because a pawn's location is
        its capsule centre ~PAWN_CENTRE_CM above its own feet. A trace result is only taken when it clears the average too, so
        whatever the geometry does, the bomb is placed at or above the players' own chest line. It cannot be in the floor,
        because a point at or above a standing player's chest is not in the floor he is standing on.

    (FHitResult is read through GameplayStatics.BreakHitResult: the generic break node has no output pins for it.)"""
    g = G(); g.entry(); g.result()
    g.get("tmp", "TmpLocs"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("tmp.TmpLocs", "clr.TargetArray"), ("entry.then", "clr.exec"))
    g.foreach("fe"); g.link(("entry.Pawns", "fe.Array"), ("clr.then", "fe.Exec"))
    g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
    g.get("tmp2", "TmpLocs"); g.call("add", ARR, "Array_Add", array=True); g.link(("tmp2.TmpLocs", "add.TargetArray"), ("loc.ReturnValue", "add.NewItem"), ("fe.LoopBody", "add.exec"))
    g.get("tmp3", "TmpLocs"); g.call("avg", MATH, "GetVectorArrayAverage"); g.link(("tmp3.TmpLocs", "avg.Vectors"))
    g.set("keep", "SpawnCentre"); g.link(("avg.ReturnValue", "keep.SpawnCentre"), ("fe.Completed", "keep.exec"))   # store once: pure nodes are re-evaluated per consumer
    g.get("c", "SpawnCentre")
    g.call("tr_s", MATH, "Add_VectorVector", {"B": "0.0,0.0,50.0"}); g.link(("c.SpawnCentre", "tr_s.A"))
    g.call("tr_e", MATH, "Subtract_VectorVector", {"B": "0.0,0.0,300.0"}); g.link(("c.SpawnCentre", "tr_e.A"))
    g.call("trace", SYS, "LineTraceSingle", {"TraceChannel": "TraceTypeQuery1", "bTraceComplex": "false", "DrawDebugType": "None", "bIgnoreSelf": "true"})
    g.link(("tr_s.ReturnValue", "trace.Start"), ("tr_e.ReturnValue", "trace.End"), ("entry.Pawns", "trace.ActorsToIgnore")); g.chain("keep", "trace")
    g.call("hit", GS_LIB, "BreakHitResult"); g.link(("trace.OutHit", "hit.Hit"))
    g.call("hz", MATH, "BreakVector"); g.link(("hit.Location", "hz.InVec"))
    g.call("cz", MATH, "BreakVector"); g.link(("c.SpawnCentre", "cz.InVec"))
    g.call("below", MATH, "Less_DoubleDouble"); g.link(("hz.Z", "below.A"), ("cz.Z", "below.B"))               # a hit ABOVE the average is a railing, not the floor
    g.call("chestz", MATH, "Add_DoubleDouble", {"B": str(BOMB_CHEST_CM)}); g.link(("hz.Z", "chestz.A"))
    g.call("clears", MATH, "GreaterEqual_DoubleDouble"); g.link(("chestz.ReturnValue", "clears.A"), ("cz.Z", "clears.B"))   # ... and chest above it must still clear the pawns
    g.call("notpen", MATH, "Not_PreBool"); g.link(("hit.bInitialOverlap", "notpen.A"))
    g.call("ok1", MATH, "BooleanAND"); g.link(("trace.ReturnValue", "ok1.A"), ("notpen.ReturnValue", "ok1.B"))
    g.call("ok2", MATH, "BooleanAND"); g.link(("ok1.ReturnValue", "ok2.A"), ("below.ReturnValue", "ok2.B"))
    g.call("ok", MATH, "BooleanAND"); g.link(("ok2.ReturnValue", "ok.A"), ("clears.ReturnValue", "ok.B"))
    g.call("chest", MATH, "Add_VectorVector", {"B": f"0.0,0.0,{BOMB_CHEST_CM}"}); g.link(("hit.Location", "chest.A"))       # chest above the floor we found
    g.call("lift", MATH, "Add_VectorVector", {"B": f"0.0,0.0,{BOMB_LIFT_CM}"}); g.link(("c.SpawnCentre", "lift.A"))         # chest above the floor the pawns are proof of
    g.call("pick", MATH, "SelectVector"); g.link(("chest.ReturnValue", "pick.A"), ("lift.ReturnValue", "pick.B"), ("ok.ReturnValue", "pick.bPickA"))
    g.link(("pick.ReturnValue", "result.Centre"), ("trace.then", "result.exec"))
    return g.json()

# ------------------------------------------------------------------------------------------------------------------
# TEAM-BASE SPAWNS (Sam, 2026-09-16: "ensure that you always spawn at your team base")
#
# The spawn system is native, scored and has NO team knob (research/2026-09-14-spawn-system.md). Every stock map tags its
# PlayerStarts `1` / `2` (the two bases) and `waiting` (a pool spread over the whole map); Bodybomb excludes nothing, so the
# scorer may drop a player anywhere. The pak builder now excludes `waiting`/`Drone`, which forces a BASE — but the scorer
# still chooses WHICH base, so the guarantee needs the CTF route: override FindPlayerStart and ChoosePlayerStart.
#
# WHICH base belongs to which team is the one thing we must not guess: pin it backwards and the attacking team starts on top
# of the bomb sites every round. So we do not guess — we LEARN it. The GM always calls the parent first, and hands its answer
# to ResolveStart as `Fallback`. The first time we see a fallback that is a real `1`/`2` start for a player with an assigned
# team, LatchFrom records that pairing; from then on every player is placed on their team's side of that pairing. If the
# native placement is already right, we agree with it and merely make it consistent; if the latch never happens (no team yet,
# untagged start) every caller keeps the game's own answer, so this can never spawn anyone worse than stock.
#
# State lives on THIS COMPONENT, never on GM_BB5 — see the CDO-by-index rule the probe block above is also written around.
# The initial pairing stays latched, but the bases follow the actual ObjectiveTeam role.
# Native ObjectiveRuleSet changes that role at the configured TeamSwitchInterval;
# comparing roles avoids a second round counter and keeps players on their assigned teams.
START_FREE_CM = "120.0"    # a start counts as taken when a pawn is within this distance (CTF's IsStartFree number)

def rule_fn_GatherStarts():
    """Base1Starts / Base2Starts from the level's PlayerStart tags, once per match. Starts tagged anything else (`waiting`,
    `Drone`) are not candidates at all — the pak builder excludes those tags from the native scorer for the same reason."""
    g = G(); g.entry()
    g.get("ready", "bStartsReady"); g.branch("br"); g.link(("ready.bStartsReady", "br.condition"), ("entry.then", "br.exec"))
    for i, v in enumerate(("Base1Starts", "Base2Starts")):
        g.get(f"g{i}", v); g.call(f"clr{i}", ARR, "Array_Clear", array=True); g.link((f"g{i}.{v}", f"clr{i}.TargetArray"))
    g.link(("br.else", "clr0.exec")); g.chain("clr0", "clr1")
    g.call("starts", GS_LIB, "GetAllActorsOfClass", {"ActorClass": PLAYERSTART}); g.chain("clr1", "starts")
    g.foreach("fe"); g.link(("starts.OutActors", "fe.Array"), ("starts.then", "fe.Exec"))
    g.cast("asps", PLAYERSTART, pure=True); g.link(("fe.Array Element", "asps.cast_object"))
    g.get("pstag", "PlayerStartTag", PLAYERSTART); g.link(("asps.cast_result", "pstag.self"))
    g.call("is1", MATH, "EqualEqual_NameName", {"B": "1"}); g.call("is2", MATH, "EqualEqual_NameName", {"B": "2"})
    g.link(("pstag.PlayerStartTag", "is1.A"), ("pstag.PlayerStartTag", "is2.A"))
    g.branch("br1"); g.branch("br2"); g.link(("is1.ReturnValue", "br1.condition"), ("is2.ReturnValue", "br2.condition"))
    g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
    g.call("add1", ARR, "Array_Add", array=True); g.call("add2", ARR, "Array_Add", array=True)
    g.link(("b1.Base1Starts", "add1.TargetArray"), ("fe.Array Element", "add1.NewItem"),
           ("b2.Base2Starts", "add2.TargetArray"), ("fe.Array Element", "add2.NewItem"))
    g.link(("fe.LoopBody", "br1.exec"), ("br1.then", "add1.exec"), ("br1.else", "br2.exec"), ("br2.then", "add2.exec"))
    # ready only when BOTH bases exist: a map with one tagged base would put every team on the same side
    g.get("n1g", "Base1Starts"); g.call("n1", ARR, "Array_Length", array=True); g.link(("n1g.Base1Starts", "n1.TargetArray"))
    g.get("n2g", "Base2Starts"); g.call("n2", ARR, "Array_Length", array=True); g.link(("n2g.Base2Starts", "n2.TargetArray"))
    g.call("p1", MATH, "Greater_IntInt", {"B": "0"}); g.link(("n1.ReturnValue", "p1.A"))
    g.call("p2", MATH, "Greater_IntInt", {"B": "0"}); g.link(("n2.ReturnValue", "p2.A"))
    g.call("both", MATH, "BooleanAND"); g.link(("p1.ReturnValue", "both.A"), ("p2.ReturnValue", "both.B"))
    g.set("rdy", "bStartsReady"); g.link(("both.ReturnValue", "rdy.bStartsReady"), ("fe.Completed", "rdy.exec"))
    return g.json()

def rule_fn_LatchFrom():
    """Learn the game's own team -> base pairing from one start IT chose, once per match. Ignored unless the start really is
    a `1`/`2` base start and the player already has a team (ABodycamPlayerState.TeamID is -1 until assigned)."""
    g = G(); g.entry()
    g.get("done", "bLatched"); g.branch("brd"); g.link(("done.bLatched", "brd.condition"), ("entry.then", "brd.exec"))
    g.call("sv", SYS, "IsValid"); g.link(("entry.Start", "sv.Object"))
    g.branch("brv"); g.link(("sv.ReturnValue", "brv.condition"), ("brd.else", "brv.exec"))
    g.cast("asps", PLAYERSTART, pure=True); g.link(("entry.Start", "asps.cast_object"))
    g.get("pstag", "PlayerStartTag", PLAYERSTART); g.link(("asps.cast_result", "pstag.self"))
    g.call("is1", MATH, "EqualEqual_NameName", {"B": "1"}); g.link(("pstag.PlayerStartTag", "is1.A"))
    g.call("is2", MATH, "EqualEqual_NameName", {"B": "2"}); g.link(("pstag.PlayerStartTag", "is2.A"))
    g.call("tagged", MATH, "BooleanOR"); g.link(("is1.ReturnValue", "tagged.A"), ("is2.ReturnValue", "tagged.B"))
    g.get("pps", "PlayerState", CONTROLLER); g.link(("entry.Player", "pps.self"))
    g.cast("asbps", BC_PS, pure=True); g.link(("pps.PlayerState", "asbps.cast_object"))
    g.get("pteam", "TeamID", BC_PS); g.link(("asbps.cast_result", "pteam.self"))
    valid_team = _binary_team(g, "assigned", "pteam.TeamID")
    objective = _objective_team(g)
    valid_objective = _binary_team(g, "objectivevalid", objective)
    g.call("validstates", MATH, "BooleanAND"); g.link(("asbps.cast_ok", "validstates.A"), ("role_gs.cast_ok", "validstates.B"))
    g.call("validids", MATH, "BooleanAND"); g.link((valid_team, "validids.A"), (valid_objective, "validids.B"))
    g.call("validboth", MATH, "BooleanAND"); g.link(("validstates.ReturnValue", "validboth.A"), ("validids.ReturnValue", "validboth.B"))
    g.call("ok", MATH, "BooleanAND"); g.link(("tagged.ReturnValue", "ok.A"), ("validboth.ReturnValue", "ok.B"))
    g.branch("brok"); g.link(("ok.ReturnValue", "brok.condition"), ("brv.then", "brok.exec"))
    g.call("side", MATH, "SelectInt", {"A": "1", "B": "2"}); g.link(("is1.ReturnValue", "side.bPickA"))
    g.set("st", "LatchSide"); g.link(("side.ReturnValue", "st.LatchSide"), ("brok.then", "st.exec"))
    g.set("tm", "LatchTeam"); g.link(("pteam.TeamID", "tm.LatchTeam")); g.chain("st", "tm")
    g.set("role", "LatchObjectiveTeam"); g.link((objective, "role.LatchObjectiveTeam")); g.chain("tm", "role")
    g.set("fl", "bLatched", defaults={"bLatched": "true"}); g.chain("role", "fl")
    return g.json()


def _binary_team(g, prefix, source):
    g.call(prefix + "min", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link((source, prefix + "min.A"))
    g.call(prefix + "max", MATH, "Less_IntInt", {"B": "2"}); g.link((source, prefix + "max.A"))
    g.call(prefix, MATH, "BooleanAND"); g.link((prefix + "min.ReturnValue", prefix + ".A"), (prefix + "max.ReturnValue", prefix + ".B"))
    return prefix + ".ReturnValue"


def _objective_team(g):
    g.call("role_state", GS_LIB, "GetGameState")
    g.cast("role_gs", BC_GS, pure=True); g.link(("role_state.ReturnValue", "role_gs.cast_object"))
    g.call("role_current", BC_GS, "GetObjectiveTeam"); g.link(("role_gs.cast_result", "role_current.self"))
    return "role_current.ReturnValue"


def rule_fn_SideForPlayer():
    """1 or 2 once the pairing is latched, else 0 (= caller keeps the game's own answer)."""
    g = G(); g.entry(); g.result()
    g.get("done", "bLatched")
    g.get("pps", "PlayerState", CONTROLLER); g.link(("entry.Player", "pps.self"))
    g.cast("asbps", BC_PS, pure=True); g.link(("pps.PlayerState", "asbps.cast_object"))
    g.get("pteam", "TeamID", BC_PS); g.link(("asbps.cast_result", "pteam.self"))
    g.get("lt", "LatchTeam"); g.call("same", MATH, "EqualEqual_IntInt"); g.link(("pteam.TeamID", "same.A"), ("lt.LatchTeam", "same.B"))
    g.get("ls", "LatchSide")
    g.call("other", MATH, "Subtract_IntInt", {"A": "3"}); g.link(("ls.LatchSide", "other.B"))    # 1 <-> 2
    g.call("pick", MATH, "SelectInt"); g.link(("ls.LatchSide", "pick.A"), ("other.ReturnValue", "pick.B"), ("same.ReturnValue", "pick.bPickA"))
    objective = _objective_team(g)
    g.get("initialrole", "LatchObjectiveTeam")
    g.call("samerole", MATH, "EqualEqual_IntInt"); g.link((objective, "samerole.A"), ("initialrole.LatchObjectiveTeam", "samerole.B"))
    g.call("swapped", MATH, "Subtract_IntInt", {"A": "3"}); g.link(("pick.ReturnValue", "swapped.B"))
    g.call("rolebase", MATH, "SelectInt"); g.link(("pick.ReturnValue", "rolebase.A"), ("swapped.ReturnValue", "rolebase.B"), ("samerole.ReturnValue", "rolebase.bPickA"))
    valid_team = _binary_team(g, "assigned", "pteam.TeamID")
    valid_objective = _binary_team(g, "objectivevalid", objective)
    g.call("validids", MATH, "BooleanAND"); g.link((valid_team, "validids.A"), (valid_objective, "validids.B"))
    g.call("validstates", MATH, "BooleanAND"); g.link(("asbps.cast_ok", "validstates.A"), ("role_gs.cast_ok", "validstates.B"))
    g.call("validboth", MATH, "BooleanAND"); g.link(("validids.ReturnValue", "validboth.A"), ("validstates.ReturnValue", "validboth.B"))
    g.call("ready", MATH, "BooleanAND"); g.link(("validboth.ReturnValue", "ready.A"), ("done.bLatched", "ready.B"))
    g.call("fin", MATH, "SelectInt", {"B": "0"}); g.link(("rolebase.ReturnValue", "fin.A"), ("ready.ReturnValue", "fin.bPickA"))
    g.link(("fin.ReturnValue", "result.Side"), ("entry.then", "result.exec"))
    return g.json()

def rule_fn_PickStart():
    """A start from Side's base: the first with no pawn within START_FREE_CM, else the first in the list."""
    g = G(); g.entry(); g.result()
    g.get("c0", "SpawnCandidates"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("c0.SpawnCandidates", "clr.TargetArray"), ("entry.then", "clr.exec"))
    g.call("is1", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("entry.Side", "is1.A"))
    g.branch("br"); g.link(("is1.ReturnValue", "br.condition"), ("clr.then", "br.exec"))
    g.get("c1", "SpawnCandidates"); g.get("b1", "Base1Starts"); g.call("ap1", ARR, "Array_Append", array=True)
    g.link(("c1.SpawnCandidates", "ap1.TargetArray"), ("b1.Base1Starts", "ap1.SourceArray"), ("br.then", "ap1.exec"))
    g.get("c2", "SpawnCandidates"); g.get("b2", "Base2Starts"); g.call("ap2", ARR, "Array_Append", array=True)
    g.link(("c2.SpawnCandidates", "ap2.TargetArray"), ("b2.Base2Starts", "ap2.SourceArray"), ("br.else", "ap2.exec"))
    g.call("pawns", GS_LIB, "GetAllActorsOfClass", {"ActorClass": PAWN}); g.link(("ap1.then", "pawns.exec"), ("ap2.then", "pawns.exec"))
    g.set("cs0", "ChosenStart"); g.link(("pawns.then", "cs0.exec"))                     # no input link = cleared to null
    g.get("cand", "SpawnCandidates"); g.foreach("fe"); g.link(("cand.SpawnCandidates", "fe.Array"), ("cs0.then", "fe.Exec"))
    g.set("occ0", "bStartTaken", defaults={"bStartTaken": "false"}); g.link(("fe.LoopBody", "occ0.exec"))
    g.call("sloc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "sloc.self"))
    g.foreach("fp"); g.link(("pawns.OutActors", "fp.Array"), ("occ0.then", "fp.Exec"))
    g.call("ploc", ACTOR, "K2_GetActorLocation"); g.link(("fp.Array Element", "ploc.self"))
    g.call("dist", MATH, "Vector_Distance"); g.link(("ploc.ReturnValue", "dist.V1"), ("sloc.ReturnValue", "dist.V2"))
    g.call("near", MATH, "Less_DoubleDouble", {"B": START_FREE_CM}); g.link(("dist.ReturnValue", "near.A"))
    g.branch("brn"); g.link(("near.ReturnValue", "brn.condition"), ("fp.LoopBody", "brn.exec"))
    g.set("occ1", "bStartTaken", defaults={"bStartTaken": "true"}); g.link(("brn.then", "occ1.exec"))
    g.get("occ", "bStartTaken"); g.call("free", MATH, "Not_PreBool"); g.link(("occ.bStartTaken", "free.A"))
    g.get("cs", "ChosenStart"); g.call("hasc", SYS, "IsValid"); g.link(("cs.ChosenStart", "hasc.Object"))
    g.call("noc", MATH, "Not_PreBool"); g.link(("hasc.ReturnValue", "noc.A"))
    g.call("take", MATH, "BooleanAND"); g.link(("free.ReturnValue", "take.A"), ("noc.ReturnValue", "take.B"))
    g.branch("brt"); g.link(("take.ReturnValue", "brt.condition"), ("fp.Completed", "brt.exec"))
    g.set("cs1", "ChosenStart"); g.link(("fe.Array Element", "cs1.ChosenStart"), ("brt.then", "cs1.exec"))
    # nothing free (or nothing chosen): the first candidate, but only if there IS one — Array_Get is pure, so an empty
    # base never evaluates it
    g.get("cs2", "ChosenStart"); g.call("okc", SYS, "IsValid"); g.link(("cs2.ChosenStart", "okc.Object"))
    g.call("needfb", MATH, "Not_PreBool"); g.link(("okc.ReturnValue", "needfb.A"))
    g.get("cand3", "SpawnCandidates"); g.call("n", ARR, "Array_Length", array=True); g.link(("cand3.SpawnCandidates", "n.TargetArray"))
    g.call("any", MATH, "Greater_IntInt", {"B": "0"}); g.link(("n.ReturnValue", "any.A"))
    g.call("fb", MATH, "BooleanAND"); g.link(("needfb.ReturnValue", "fb.A"), ("any.ReturnValue", "fb.B"))
    g.branch("brf"); g.link(("fb.ReturnValue", "brf.condition"), ("fe.Completed", "brf.exec"))
    g.get("cand2", "SpawnCandidates"); g.call("first", ARR, "Array_Get", {"Index": "0"}, array=True); g.link(("cand2.SpawnCandidates", "first.TargetArray"))
    g.set("cs3", "ChosenStart"); g.link(("first.Item", "cs3.ChosenStart"), ("brf.then", "cs3.exec"))
    g.get("out", "ChosenStart"); g.link(("out.ChosenStart", "result.Start"))
    g.link(("cs3.then", "result.exec"), ("brf.else", "result.exec"))
    return g.json()

def rule_fn_ResolveStart():
    """The team-base answer for Player, or Fallback (the game's own choice) whenever we do not have one."""
    g = G(); g.entry(); g.result()
    g.selfcall("gather", RULE, "GatherStarts"); g.link(("entry.then", "gather.exec"))
    g.selfcall("latch", RULE, "LatchFrom"); g.link(("entry.Player", "latch.Player"), ("entry.Fallback", "latch.Start")); g.chain("gather", "latch")
    g.selfcall("side", RULE, "SideForPlayer"); g.link(("entry.Player", "side.Player")); g.chain("latch", "side")
    g.call("known", MATH, "Greater_IntInt", {"B": "0"}); g.link(("side.Side", "known.A"))
    g.get("rdy", "bStartsReady"); g.call("ok", MATH, "BooleanAND"); g.link(("rdy.bStartsReady", "ok.A"), ("known.ReturnValue", "ok.B"))
    g.branch("br"); g.link(("ok.ReturnValue", "br.condition")); g.chain("side", "br")
    g.selfcall("pick", RULE, "PickStart"); g.link(("side.Side", "pick.Side"), ("br.then", "pick.exec"))
    g.call("pv", SYS, "IsValid"); g.link(("pick.Start", "pv.Object"))
    g.branch("brp"); g.link(("pv.ReturnValue", "brp.condition")); g.chain("pick", "brp")
    g.set("out1", "ChosenStart"); g.link(("pick.Start", "out1.ChosenStart"), ("brp.then", "out1.exec"))
    g.set("out2", "ChosenStart"); g.link(("entry.Fallback", "out2.ChosenStart"), ("brp.else", "out2.exec"), ("br.else", "out2.exec"))
    g.get("outv", "ChosenStart"); g.link(("outv.ChosenStart", "result.Start"))
    g.link(("out1.then", "result.exec"), ("out2.then", "result.exec"))
    return g.json()

# ------------------------------------------------------------------------------------------------------------------
# THE TEAM HEAD-COUNT (2026-09-16) - why no BB5 match ever ended itself at the score limit.
#
# Read out of Bodycam-Win64-Shipping.exe, because the probe log could only show that it did not happen. A won round makes
# the native game mode request Game.Phase.EndRound, and that request is the only place a match can end:
#
#     over = (MaxPhases > 0 && CurrentRound + 1 >= GetMaxRound())
#         || GetCurrentBestTeam().TeamScore >= GetScoreLimit()        -> Game.Phase.EndMatch, otherwise the next round
#
# GetCurrentBestTeam() is the highest TeamScore AMONG TEAMS WITH PlayerCount > 0, and PlayerCount is not derived: it is a
# counter that only AddPlayerToTeam moves, and that runs only when the GAME assigns a team. Its post-login and round-warm-up
# assignment both skip a player whose TeamID is already >= 0, and gm_teamset writes TeamID while it is still -1 (that
# evening's probe log: "0:1:0:-1:-1" for the host, "1:2:1:-1:-1" for the joiner). So the game never counted anybody onto a
# team, every PlayerCount stayed 0, the best team was nobody with a score of 0, and 0 >= 2 was never true. The round cap
# reads no PlayerCount, which is the one way a match could still end.
#
# So the count is rebuilt from PlayerArray - exactly what the game itself does after a host migration, where its own recount
# zeroes every PlayerCount and adds one per player whose team id matches. It runs on every team-sweep tick and straight after
# every TeamID write, so it is right long before a round can be won. The ch_exit_armed seen ~23 s after the deciding round was
# never the game ending itself: by the timings (settle + CLOSE_GAME_AFTER_SECONDS) it is the shutdown Lights Out's close request
# starts, 0.2-0.4 s after that request in both matches.
#
# IN PLACE, through Array_Set on GameState.Teams. The verdict is taken on the server's own copy, so nothing needs a Set of the
# whole array (which would also run OnRep_Teams on the server). TeamTally is on this component because GM_BB5 may not declare a
# variable (the CDO-by-index rule the PROBE block describes).
def rule_fn_RecountTeams():
    """PlayerCount of every GameState.Teams entry := how many PlayerArray entries carry that team id."""
    g = G(); g.entry()
    g.call("gs", GS_LIB, "GetGameState")
    g.cast("gsc", BC_GS, pure=True); g.link(("gs.ReturnValue", "gsc.cast_object"))
    g.branch("brg"); g.link(("gsc.cast_ok", "brg.condition"), ("entry.then", "brg.exec"))
    g.get("teams", "Teams", BC_GS); g.link(("gsc.cast_result", "teams.self"))
    g.foreach("ft"); g.link(("teams.Teams", "ft.Array"), ("brg.then", "ft.Exec"))
    g.brk("td", V_TEAMDATA); g.link(("ft.Array Element", "td.in"))
    g.set("zero", "TeamTally", defaults={"TeamTally": "0"}); g.link(("ft.LoopBody", "zero.exec"))
    g.get("pa", "PlayerArray", GAMESTATE); g.link(("gs.ReturnValue", "pa.self"))
    g.foreach("fp"); g.link(("pa.PlayerArray", "fp.Array"), ("zero.then", "fp.Exec"))
    # GetPlayerTeamID rather than a cast and TeamID: a failed pure cast reads TeamID 0, which is a real team here
    g.call("tid", GM_LIB, "GetPlayerTeamID"); g.link(("fp.Array Element", "tid.PlayerState"))
    g.call("same", MATH, "EqualEqual_IntInt"); g.link(("tid.ReturnValue", "same.A"), ("td.TeamID", "same.B"))
    g.branch("brs"); g.link(("same.ReturnValue", "brs.condition"), ("fp.LoopBody", "brs.exec"))
    g.get("tally", "TeamTally"); g.call("inc", MATH, "Add_IntInt", {"B": "1"}); g.link(("tally.TeamTally", "inc.A"))
    g.set("bump", "TeamTally"); g.link(("inc.ReturnValue", "bump.TeamTally"), ("brs.then", "bump.exec"))
    # TeamID and TeamScore go back exactly as read; only the count changes
    g.get("tally2", "TeamTally")
    g.n("mk", "make", struct=V_TEAMDATA)
    g.link(("td.TeamID", "mk.TeamID"), ("td.TeamScore", "mk.TeamScore"), ("tally2.TeamTally", "mk.PlayerCount"))
    g.get("teams2", "Teams", BC_GS); g.link(("gsc.cast_result", "teams2.self"))
    g.call("put", ARR, "Array_Set", {"bSizeToFit": "false"}, array=True)
    g.link(("teams2.Teams", "put.TargetArray"), ("ft.Array Index", "put.Index"), ("mk.out", "put.Item"), ("fp.Completed", "put.exec"))
    return g.json()

def rule_fn_StartRoster():
    """One synchronous snapshot: count|SteamID:TeamID:controller-present;... .

    The entire PlayerArray is captured in one call, never assembled from asynchronous reports.
    StartScratch is scratch space on the rule component, not an indexed GameMode property.
    """
    g = G(); g.entry(); g.result(params=[P("ReturnValue", "string")])
    g.call("gs", GS_LIB, "GetGameState")
    g.get("players", "PlayerArray", GAMESTATE); g.link(("gs.ReturnValue", "players.self"))
    g.call("count", ARR, "Array_Length", array=True); g.link(("players.PlayerArray", "count.TargetArray"))
    g.call("count_s", STR, "Conv_IntToString"); g.link(("count.ReturnValue", "count_s.InInt"))
    g.call("prefix", STR, "Concat_StrStr", {"B": "|"}); g.link(("count_s.ReturnValue", "prefix.A"))
    g.set("clear", "StartScratch"); g.link(("prefix.ReturnValue", "clear.StartScratch")); g.chain("entry", "clear")
    g.foreach("each"); g.link(("players.PlayerArray", "each.Array"), ("clear.then", "each.Exec"))
    g.call("id", ONLINE, "RetrievePlatformIdAsStringFromPlayerState"); g.link(("each.Array Element", "id.PlayerState"))
    g.call("team", GM_LIB, "GetPlayerTeamID"); g.link(("each.Array Element", "team.PlayerState"))
    g.call("team_s", STR, "Conv_IntToString"); g.link(("team.ReturnValue", "team_s.InInt"))
    g.call("owner", ACTOR, "GetOwner"); g.link(("each.Array Element", "owner.self"))
    g.cast("controller", CONTROLLER, pure=True); g.link(("owner.ReturnValue", "controller.cast_object"))
    g.call("pawn", CONTROLLER, "K2_GetPawn"); g.link(("controller.cast_result", "pawn.self"))
    g.call("valid", SYS, "IsValid"); g.link(("pawn.ReturnValue", "valid.Object"))
    g.call("active", MATH, "SelectString", {"A": "1;", "B": "0;"}); g.link(("valid.ReturnValue", "active.bPickA"))
    g.get("scratch", "StartScratch")
    previous = "scratch.StartScratch"
    for i, (value, literal) in enumerate((("id.ReturnValue", None), (None, ":"), ("team_s.ReturnValue", None),
                                         (None, ":"), ("active.ReturnValue", None))):
        name = "append" + str(i)
        g.call(name, STR, "Concat_StrStr", {"B": literal} if literal is not None else None)
        g.link((previous, name + ".A"))
        if value: g.link((value, name + ".B"))
        previous = name + ".ReturnValue"
    g.set("save", "StartScratch"); g.link((previous, "save.StartScratch"), ("each.LoopBody", "save.exec"))
    g.get("out", "StartScratch"); g.link(("out.StartScratch", "result.ReturnValue"), ("each.Completed", "result.exec"))
    return g.json()


def rule_fn_FinalRows():
    """Capture every final player row synchronously; one HTTP request sends the whole batch."""
    g = G(); g.entry(); g.result(params=[P("ReturnValue", "string")])
    g.set("clear", "FinalScratch", defaults={"FinalScratch": ""}); g.chain("entry", "clear")
    g.call("gs", GS_LIB, "GetGameState")
    g.get("players", "PlayerArray", GAMESTATE); g.link(("gs.ReturnValue", "players.self"))
    g.n("each", "foreach"); g.link(("players.PlayerArray", "each.Array"), ("clear.then", "each.Exec"))
    g.cast("ps", BC_PS, pure=True); g.link(("each.Array Element", "ps.cast_object"))
    g.call("id", ONLINE, "RetrievePlatformIdAsStringFromPlayerState"); g.link(("each.Array Element", "id.PlayerState"))
    fields = []
    for key, prop in (("k", "Kill"), ("d", "Death"), ("sp", "SpawnCount"), ("t", "TeamID")):
        g.get(key, prop, BC_PS); g.link(("ps.cast_result", key + ".self"))
        g.call(key + "s", STR, "Conv_IntToString"); g.link((key + "." + prop, key + "s.InInt"))
        fields.append((key, key + "s.ReturnValue"))
    g.call("score", GM_LIB, "GetPlayerScore"); g.link(("each.Array Element", "score.Player"))
    g.call("scores", STR, "Conv_IntToString"); g.link(("score.ReturnValue", "scores.InInt")); fields.append(("s", "scores.ReturnValue"))
    g.call("alive", BC_PS, "IsPlayerAlive"); g.link(("ps.cast_result", "alive.self"))
    g.call("alives", STR, "Conv_BoolToString"); g.link(("alive.ReturnValue", "alives.InBool")); fields.append(("a", "alives.ReturnValue"))
    g.get("scratch", "FinalScratch")
    g.call("row", STR, "Concat_StrStr"); g.link(("scratch.FinalScratch", "row.A"), ("id.ReturnValue", "row.B"))
    previous = "row.ReturnValue"
    for i, (key, value) in enumerate(fields):
        label, val = "label" + str(i), "value" + str(i)
        g.call(label, STR, "Concat_StrStr", {"B": ("|" if i == 0 else ";") + key + "="}); g.link((previous, label + ".A"))
        g.call(val, STR, "Concat_StrStr"); g.link((label + ".ReturnValue", val + ".A"), (value, val + ".B")); previous = val + ".ReturnValue"
    g.call("comma", STR, "Concat_StrStr", {"B": ","}); g.link((previous, "comma.A"))
    g.set("append", "FinalScratch"); g.link(("comma.ReturnValue", "append.FinalScratch"), ("each.LoopBody", "append.exec"))
    g.link(("each.Completed", "result.exec"), ("scratch.FinalScratch", "result.ReturnValue"))
    return g.json()


def rule_fn_BeginCompetitive():
    """Clear warmup K/D exactly once, at the native first-round start (round index is still zero)."""
    g = G(); g.entry()
    g.get("started", "CompetitiveStarted"); g.branch("once")
    g.link(("started.CompetitiveStarted", "once.condition"), ("entry.then", "once.exec"))
    g.call("gs", GS_LIB, "GetGameState")
    g.get("players", "PlayerArray", GAMESTATE); g.link(("gs.ReturnValue", "players.self"))
    g.foreach("each"); g.link(("once.else", "each.Exec"), ("players.PlayerArray", "each.Array"))
    g.cast("ps", BC_PS); g.link(("each.Array Element", "ps.cast_object"), ("each.LoopBody", "ps.exec"))
    g.set("kills", "Kill", BC_PS, defaults={"Kill": "0"}); g.link(("ps.cast_result", "kills.self")); g.chain("ps", "kills")
    g.set("deaths", "Death", BC_PS, defaults={"Death": "0"}); g.link(("ps.cast_result", "deaths.self")); g.chain("kills", "deaths")
    g.set("done", "CompetitiveStarted", defaults={"CompetitiveStarted": "true"}); g.link(("each.Completed", "done.exec"))
    return g.json()


RULE_FUNCTIONS = ["AverageOf", "GatherStarts", "LatchFrom", "SideForPlayer", "PickStart", "ResolveStart", "RecountTeams", "StartRoster", "FinalRows", "BeginCompetitive"]
RULE_FN_BODIES = {"AverageOf": rule_fn_AverageOf, "GatherStarts": rule_fn_GatherStarts, "LatchFrom": rule_fn_LatchFrom,
                  "SideForPlayer": rule_fn_SideForPlayer, "PickStart": rule_fn_PickStart, "ResolveStart": rule_fn_ResolveStart,
                  "RecountTeams": rule_fn_RecountTeams, "StartRoster": rule_fn_StartRoster, "FinalRows": rule_fn_FinalRows,
                  "BeginCompetitive": rule_fn_BeginCompetitive}

def gm_findplayerstart():
    """Override FindPlayerStart(Player, IncomingName) -> Actor. The parent runs FIRST, always: its answer is both the fallback
    and the sample LatchFrom learns the team -> base pairing from."""
    g = G(); g.entry(); g.result()
    g.callparent("parent", GM_PARENT, "FindPlayerStart")
    g.link(("entry.Player", "parent.Player"), ("entry.IncomingName", "parent.IncomingName"), ("entry.then", "parent.exec"))
    g.get("comp", "BB5BombRule")
    g.call("res", RULE, "ResolveStart"); g.link(("comp.BB5BombRule", "res.self"), ("entry.Player", "res.Player"), ("parent.ReturnValue", "res.Fallback"))
    g.chain("parent", "res")
    g.link(("res.Start", "result.ReturnValue"), ("res.then", "result.exec"))
    return g.json()

def gm_chooseplayerstart():
    """Override ChoosePlayerStart(Player) -> Actor: the same answer, for callers that skip FindPlayerStart."""
    g = G(); g.entry(); g.result()
    g.callparent("parent", GM_PARENT, "ChoosePlayerStart")
    g.link(("entry.Player", "parent.Player"), ("entry.then", "parent.exec"))
    g.get("comp", "BB5BombRule")
    g.call("res", RULE, "ResolveStart"); g.link(("comp.BB5BombRule", "res.self"), ("entry.Player", "res.Player"), ("parent.ReturnValue", "res.Fallback"))
    g.chain("parent", "res")
    g.link(("res.Start", "result.ReturnValue"), ("res.then", "result.exec"))
    return g.json()

# ==================================================================================================================
# GM_BB5
# ==================================================================================================================
def gm_events():
    g = G()
    g.custom("perkmod", "RefreshPerkMods")
    # Delayed authenticated arrival reports; neither changes lobby settings.
    g.custom("lread", "LobbyRead")
    g.custom("lwrite", "LobbyWrite")
    # the match-end exit (2026-09-14). OnMatchOver is the GameState delegate handler; MatchExit is
    # the timer target, named by string in K2_SetTimer, so it has to be a real declared event.
    g.custom("matchover", "OnMatchOver")
    g.custom("mexit", "MatchExit")
    g.custom("exitok", "OnExitOk")
    g.custom("exitfail", "OnExitFail")
    # The score report (2026-09-15). Named by string in K2_SetTimer, so like MatchExit it has to
    # be a really declared event or the timer has nothing to find. See the SCORE REPORT block.
    g.custom("scorerep", "ScoreReport")
    # The per-player stats sweep. On the GAMEMODE, not the component: a component-targeted timer
    # was measured never to fire (see gm_stats), and the cursor that forced it there is gone.
    g.custom("statrep", "StatReport")
    # The round snapshot's handler. Bound to ABodycamGameState::OnRoundEnded, which is
    # BlueprintAssignable and therefore purely additive - see gm_round.
    g.custom("roundrep", "OnRoundOver")
    g.custom("roundstart", "OnCompetitiveRoundStarted")
    g.custom("statebeat", "StateBeat")
    # OnPlayerKilled delivers two Controllers - the proven signature, copied from ctf_graphs.
    g.custom("killrep", "HandleKill",
             [P("InstigatorController", "object", **{"class": CONTROLLER}),
              P("VictimController", "object", **{"class": CONTROLLER})])
    # The team sweep (2026-09-15). Named by string in K2_SetTimer, so like MatchExit
    # and ScoreReport it has to be a really declared event or the timer finds nothing.
    g.custom("teamsweep", "TeamSweep")
    g.custom("arrivalguard", "ArrivalGuardTick")
    g.custom("startgate", "StartGateTick")
    g.custom("finaltick", "FinalTick")
    g.custom("finalreply", "OnFinalReply", [P("bSuccess", "bool")])
    # The OTHER OnPlayerKilled binding, from the team-kill block. Two handlers on one multicast
    # delegate is legal and both fire; see gm_logic for why they are chained rather than parallel.
    g.custom("onkill", "OnKill", [P("InstigatorController", "object", **{"class": CONTROLLER}),
                                  P("VictimController", "object", **{"class": CONTROLLER})])
    return g.json()

def gm_logic():
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    g.callparent("bp_parent", GM_PARENT, "ReceiveBeginPlay")
    g.call("perktimer", SYS, "K2_SetTimer", {"FunctionName": "RefreshPerkMods", "Time": "2.0", "bLooping": "true"}); g.selfnode("selfp"); g.link(("selfp.self", "perktimer.Object"))
    # THE BEGINPLAY EXEC ORDER LIVES HERE, in one line, because an exec output pin drives exactly
    # ONE link and every block below wants to hang off the end of it. gm_score and gm_stats only
    # declare their timers; gm_probe then chains from perktimer and gm_exit from the probe's last.
    gm_score(g)
    gm_stats(g)
    gm_stat_report(g)
    gm_teamset(g)
    gm_arrival_guard(g)
    gm_start_gate(g)
    gm_final_report(g)
    # Both timers are on the ONE chain. Each block only declares its timer node; the order lives
    # here, in a single line, because an exec output pin drives exactly one link.
    import combat_graphs as combat
    g.call("combat_transform", MATH, "MakeTransform")
    g.spawn("combat_manager", combat.GM)
    g.link(("combat_transform.ReturnValue", "combat_manager.SpawnTransform"))
    g.call("combat_start", combat.GM, "CombatStart")
    g.link(("combat_manager.ReturnValue", "combat_start.self"))
    g.get("combat_rule", "BB5BombRule"); g.set("keep_combat", "CombatManager", RULE)
    g.link(("combat_rule.BB5BombRule", "keep_combat.self"), ("combat_manager.ReturnValue", "keep_combat.CombatManager"))
    g.chain("bp", "bp_parent", "combat_manager", "combat_start", "keep_combat", "t_score", "t_stats", "t_team", "t_arrival", "t_startgate", "t_final", "perktimer")
    # RefreshPerkMods (server, every 2 s): every pawn carries the infinite "GadgetCooldown x4" effect (stack limit 1 -> never compounds;
    # the ASC lives on the pawn, so a new round's pawn needs it again) — the CTF mechanism with factor 4
    g.existing("perkmod", "RefreshPerkMods")
    g.call("applyge", BC_GM, "ApplyGameplayEffectToAllPlayers", {"EffectClass": GE_DRONECD}); g.chain("perkmod", "applyge")
    gm_arrival_reports(g)
    # The score report hangs off the END of the probe's timer chain, because a Blueprint exec pin
    # takes exactly ONE outgoing link and gm_probe already owns perktimer's. When the temporary
    # PROBE block is deleted, change this to "perktimer" - it is the only line that needs to know.
    gm_exit(g)
    gm_round(g)
    gm_heartbeat(g)
    gm_kill(g)
    # The player-count gate's measurement. Its two events are their own roots (the native fires them), so this adds
    # no link to the BeginPlay chain and cannot disturb the exec order above.
    gm_phase(g)
    # gm_teamkill(g) IS NO LONGER CALLED. It bound OnPlayerKilled a second time and sent a second
    # event per kill, deciding for itself what a team kill was. The feed above carries every kill,
    # and the server classifies it against the roster IT assigned - which cannot be misreported by
    # a host - so the same punishments now run off that instead. The function is left in place
    # because its enforcement rules are the ones still in use; only its emitter is retired.
    # THE BIND CHAIN, and the merge that nearly broke it. Both sides of this merge chained their
    # own bind off `x_bind` - and an exec OUTPUT pin drives exactly ONE link, so the second would
    # have silently replaced the first and one whole feature would never have armed. They are
    # serialised instead: gm_round owns x_bind's, gm_heartbeat follows it, gm_kill follows that,
    # and the team-kill bind goes last.
    # (nothing follows gm_kill's bind now that the team-kill emitter is retired)
    return g.json()

# ------------------------------------------------------------------------------------------------------------------
# THE SCORE REPORT (Sam, 2026-09-15) — "have it so the game data is sent to the hub ... and then whichever team
# reaches the score limit first, wins."
#
# NOT temporary, unlike the PROBE block above. This is how a ranked match gets a result at all: the backend settles
# ratings from it (server/live.cjs gameReportedScore -> settleMatch).
#
# It runs on the GameMode, so it runs on the SERVER only — which in Bodycam's peer-hosted matches means the host, and
# the host alone. That is what makes `GetPlayerState(0)` the right identity here: the same node in the probe above has
# been returning the host's real SteamID64 in production since 2026-09-14, and the backend refuses any report whose
# user_id is not the host of that match.
#
# What it reads (all BlueprintPure, all in the UHT dump AND in the mirror stub — reference/UHTHeaderDump_game_modules.zip,
# mirror/Bodycam/Source/Bodycam/Public/BodycamGameState.h):
#   ABodycamGameState::GetTeams()      -> TArray<FBodycamTeamData>{ TeamID, TeamScore, PlayerCount }
#   ABodycamGameState::GetScoreLimit() -> int32   (DA_BB5.ScoringConfig.ScoreLimit, which ships as 7)
#
# What it sends, packed into SendAttributionEvent's fixed string parameters:
#   EventName   "ch_bb5_score"
#   UserId      our SteamID64                              <- who is reporting
#   Storefront  the CH_MATCH attribute off the live lobby   <- WHICH match this is
#   Platform    "<hostTeamId>|<teamId>:<score>|<teamId>:<score>"
#   Timestamp   GetScoreLimit() as a string
#
# THE HOST'S OWN TEAM IS IN THERE ON PURPOSE. We never call SetTeamId (docs/autojoin.md, "never called"), so the game
# picks its own team ids and they mean nothing to the backend by themselves. The host is on the backend's roster, so
# saying which in-game team the host is on pins the mapping for both sides. Without it a report reading "team 0 won
# 7-3" is unattributable and the match cannot be settled.
#
# The backend decides the winner, not this graph: it compares the scores against the limit. That keeps Sam's rule in
# ONE place, settles a match even if the very last report is the one that gets lost, and needs no delegate binding
# (the graph builder has no CreateEvent node kind, so OnMatchEnded cannot be bound from here yet).
SCORE_PERIOD = "15.0"      # seconds. A Bodybomb round runs minutes; this is far finer than it needs to be, and the
                           # backend ignores a report that has not moved, so the cost of being generous is nothing.
SCORE_EVENT = "ch_bb5_score"
SCORE_NONE_EVENT = "ch_bb5_score_none"   # the SAME tick, when GetTeams() cannot fill a scoreline - see gm_score

def gm_stats(g):
    """Arm the per-player stats sweep. Self-targeted, exactly like gm_score.

    MEASURED, 2026-09-15, on Sam's first hosted launch. The first version aimed this timer at the
    COMPONENT - `GetComponentByClass(RULE)` into `K2_SetTimer.Object` - so the sweep could keep a
    cursor in a component variable, GM_BB5 being unable to hold one. It compiled clean, cooked
    clean, and **never fired once**: the probe log shows `ch_bb5_score` arriving every 15 s for
    two solid minutes and not a single `ch_bb5_stats`. The two graphs differ in exactly one
    thing - who the timer is aimed at - so a component-targeted K2_SetTimer is the part that does
    not work here, whatever the underlying reason. No pruned-node warning; the compiler was happy.

    So it is aimed at Self, which is the arrangement the probe log proves works, and the cursor
    that forced the component on us is gone rather than moved - see `gm_stat_report`.

    Declares the node only: gm_logic owns the BeginPlay exec order, because an exec output pin
    drives exactly one link and appending here silently steals it from whoever already had it."""
    g.call("t_stats", SYS, "K2_SetTimer",
           {"FunctionName": "StatReport", "Time": STAT_PERIOD, "bLooping": "true"})
    g.selfnode("s_stats"); g.link(("s_stats.self", "t_stats.Object"))


def gm_score(g):
    """The looping score report.

    DECLARES its timer node and wires its own event graph, but does NOT chain the timer onto
    anything: gm_logic owns the BeginPlay exec order and splices `t_score` into it. That is not
    fastidiousness - a Blueprint exec OUTPUT pin drives exactly ONE link, and the first version
    of this appended itself to the probe's last timer, which gm_exit was already using. The
    result built, looked right, and silently dropped one of the two branches. gm_logic is the
    one place that can see the whole chain, so it is the only place that may order it."""
    g.call("t_score", SYS, "K2_SetTimer",
           {"FunctionName": "ScoreReport", "Time": SCORE_PERIOD, "bLooping": "true"})
    g.selfnode("s_score"); g.link(("s_score.self", "t_score.Object"))

    g.existing("ev_score", "ScoreReport")
    g.call("sc_gs", GS_LIB, "GetGameState")
    g.cast("sc_asgs", BC_GS, pure=True); g.link(("sc_gs.ReturnValue", "sc_asgs.cast_object"))
    g.call("sc_teams", BC_GS, "GetTeams"); g.link(("sc_asgs.cast_result", "sc_teams.self"))

    # GetTeams lists only the teams that EXIST so far (ctf_graphs.py gm_fn_EnsureTeams learned this the hard way: a
    # lobby without both sides present has one entry, or none). Reporting a one-sided scoreboard would let the backend
    # read an absent team as 0 and hand somebody a 7-0 win, so no SCORELINE is sent until both are there.
    #
    # THE FALSE ARM IS NOT SILENCE ANY MORE (2026-09-16). This branch is the ONLY conditional between the timer and
    # the send, and in Sam's 1v1 of that evening it swallowed the entire feature: 270 ch_bb5_stats, 53 ch_bb5_state
    # and ZERO ch_bb5_score in the probe ring, so the backend was never told anybody won and the hub never closed a
    # game. A gate whose failure looks exactly like a graph that was never built is not a gate worth having, so the
    # else arm now REPORTS - ch_bb5_score_none carrying the length it actually saw. One test now says whether this
    # branch is the thing that is false; before, it could only be inferred from an absence.
    g.call("sc_n", ARR, "Array_Length", array=True); g.link(("sc_teams.ReturnValue", "sc_n.TargetArray"))
    g.call("sc_two", MATH, "GreaterEqual_IntInt", {"B": "2"}); g.link(("sc_n.ReturnValue", "sc_two.A"))
    g.branch("sc_br"); g.link(("sc_two.ReturnValue", "sc_br.condition"))
    g.chain("ev_score", "sc_br")

    g.call("sc_t0", ARR, "Array_Get", {"Index": "0"}, array=True); g.link(("sc_teams.ReturnValue", "sc_t0.TargetArray"))
    g.call("sc_t1", ARR, "Array_Get", {"Index": "1"}, array=True); g.link(("sc_teams.ReturnValue", "sc_t1.TargetArray"))
    g.brk("sc_b0", V_TEAMDATA); g.link(("sc_t0.Item", "sc_b0.in"))
    g.brk("sc_b1", V_TEAMDATA); g.link(("sc_t1.Item", "sc_b1.in"))

    # Who we are, and which side we are on. Same two nodes the probe uses for identity.
    g.call("sc_ps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("sc_pid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("sc_ps.ReturnValue", "sc_pid.PlayerState"))
    g.cast("sc_psbc", BC_PS, pure=True); g.link(("sc_ps.ReturnValue", "sc_psbc.cast_object"))
    g.get("sc_hteam", "TeamID", BC_PS); g.link(("sc_psbc.cast_result", "sc_hteam.self"))

    g.call("sc_lim", BC_GS, "GetScoreLimit"); g.link(("sc_asgs.cast_result", "sc_lim.self"))
    g.call("sc_limS", STR, "Conv_IntToString"); g.link(("sc_lim.ReturnValue", "sc_limS.InInt"))

    # "<hostTeamId>|<teamId>:<score>|<teamId>:<score>", built the only way a Blueprint can build a string: one
    # Concat at a time. The separators ride as pin defaults on B; the values come in on A.
    for node, src in (("sc_htS", "sc_hteam.TeamID"), ("sc_i0", "sc_b0.TeamID"), ("sc_s0", "sc_b0.TeamScore"),
                      ("sc_i1", "sc_b1.TeamID"), ("sc_s1", "sc_b1.TeamScore")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))

    def _cat(node, a_pin, b_pin=None, literal=None):
        g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
        g.link((a_pin, node + ".A"))
        if b_pin:
            g.link((b_pin, node + ".B"))
        return node + ".ReturnValue"

    p = _cat("sc_c1", "sc_htS.ReturnValue", literal="|")
    p = _cat("sc_c2", p, b_pin="sc_i0.ReturnValue")
    p = _cat("sc_c3", p, literal=":")
    p = _cat("sc_c4", p, b_pin="sc_s0.ReturnValue")
    p = _cat("sc_c5", p, literal="|")
    p = _cat("sc_c6", p, b_pin="sc_i1.ReturnValue")
    p = _cat("sc_c7", p, literal=":")
    p = _cat("sc_c8", p, b_pin="sc_s1.ReturnValue")

    # WHICH match. The lobby carries CH_MATCH (the probe's LobbyWrite stamps it), and echoing it back is what stops a
    # report from a previous match being applied to this one. "" when the attribute is not there; the backend then
    # falls back to "this host's current live match", which is right in every case except an overlap it cannot have.
    g.call("sc_info", ONLINE, "GetCurrentLobbyInfo")
    g.call("sc_attr", ONLINE_TYPES, "GetLobbyInfoStringAttribute", {"Key": LOBBY_KEY})
    g.link(("sc_info.ReturnValue", "sc_attr.LobbyInfo"))

    _report_send(g, "sc_send", {
        "URL": PROBE_URL, "IP": "", "EventName": SCORE_EVENT,
        "FirstSessionTimestamp": "", "IsFirstGameOpen": "false"})
    g.link(("sc_pid.ReturnValue", "sc_send.UserId"),
           ("sc_attr.OutValue", "sc_send.Storefront"),
           ("sc_c8.ReturnValue", "sc_send.Platform"),
           ("sc_limS.ReturnValue", "sc_send.Timestamp"))
    g.chain("sc_br", "sc_send")

    # ...and the else arm. A SECOND send node rather than a shared one, exactly as gm_kill forks around a null
    # instigator: the graph builder has no Select node, and a chain through the true send would never reach here
    # anyway - SendAttributionEvent is latent (memory: chained sends stall).
    g.call("sc_nS", STR, "Conv_IntToString"); g.link(("sc_n.ReturnValue", "sc_nS.InInt"))
    g.call("sc_nc", STR, "Concat_StrStr", {"A": "teams="}); g.link(("sc_nS.ReturnValue", "sc_nc.B"))
    _report_send(g, "sc_none", {
        "URL": PROBE_URL, "IP": "", "EventName": SCORE_NONE_EVENT,
        "FirstSessionTimestamp": "", "IsFirstGameOpen": "false"})
    g.link(("sc_pid.ReturnValue", "sc_none.UserId"),
           ("sc_attr.OutValue", "sc_none.Storefront"),
           ("sc_nc.ReturnValue", "sc_none.Platform"),
           ("sc_limS.ReturnValue", "sc_none.Timestamp"))
    g.link(("sc_br.else", "sc_none.exec"))
    return "t_score"

def _lobby_report(g, p, event_name):
    """Pure read of the CURRENT lobby, packed into one SendAttributionEvent. `p` prefixes the
    node ids so this can be dropped into the graph more than once.

    What each string field carries (six usable strings, see the 2026-09-14 change log):
      user_id                 our SteamID64, to tie the reports together
      storefront              the CH_MATCH attribute read back off the live lobby ("" if absent)
      platform                "<members>/<max>" from GetCurrentLobbyMembers / GetMaxLobbyMembers
      timestamp               whether GetLobbyInfoStringAttribute found the key at all
      first_session_timestamp the value we intend to write, so the read-back can be compared
    Returns the id of the send node, for the caller to put on its exec chain."""
    g.call(p + "info", ONLINE, "GetCurrentLobbyInfo")
    g.call(p + "mem", ONLINE_TYPES, "GetCurrentLobbyMembers"); g.link((p + "info.ReturnValue", p + "mem.LobbyInfo"))
    g.call(p + "max", ONLINE_TYPES, "GetMaxLobbyMembers"); g.link((p + "info.ReturnValue", p + "max.LobbyInfo"))
    g.call(p + "attr", ONLINE_TYPES, "GetLobbyInfoStringAttribute", {"Key": LOBBY_KEY})
    g.link((p + "info.ReturnValue", p + "attr.LobbyInfo"))

    g.call(p + "memS", STR, "Conv_IntToString"); g.link((p + "mem.ReturnValue", p + "memS.InInt"))
    g.call(p + "maxS", STR, "Conv_IntToString"); g.link((p + "max.ReturnValue", p + "maxS.InInt"))
    g.call(p + "slash", STR, "Concat_StrStr", {"B": "/"}); g.link((p + "memS.ReturnValue", p + "slash.A"))
    g.call(p + "both", STR, "Concat_StrStr")
    g.link((p + "slash.ReturnValue", p + "both.A"), (p + "maxS.ReturnValue", p + "both.B"))
    g.call(p + "had", STR, "Conv_BoolToString"); g.link((p + "attr.ReturnValue", p + "had.InBool"))

    g.call(p + "ps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call(p + "pid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link((p + "ps.ReturnValue", p + "pid.PlayerState"))

    _report_send(g, p + "send", {
        "URL": PROBE_URL, "IP": "", "EventName": event_name,
        "FirstSessionTimestamp": LOBBY_VALUE, "IsFirstGameOpen": "false"})
    g.link((p + "pid.ReturnValue", p + "send.UserId"),
           (p + "attr.OutValue", p + "send.Storefront"),
           (p + "both.ReturnValue", p + "send.Platform"),
           (p + "had.ReturnValue", p + "send.Timestamp"))
    return p + "send"

# ------------------------------------------------------------------------------------------------------------------
# ROSTER / TEAM SWEEP - docs/autojoin-teams.md.
#
# Each asynchronous transaction owns a short-lived Actor. GM_BB5 gains no properties (its
# cooked CDO is indexed against the game's parent). Never reconstruct a subject from the clock
# in a response handler: PlayerArray can change even during a fast HTTP request.
# Dispatch at most one player per second, avoiding a burst of simultaneous HTTP calls.
# Pad small rosters to four slots: a stable player's three-second request expires
# before their next visit. Full 5v5 rosters are scanned in ten seconds rather than forty.
TEAM_PERIOD = "1.0"
TEAM_MIN_SLOTS = "4"
TEAM_REQUEST = BB5_DIR + "/BP_BB5TeamRequest.BP_BB5TeamRequest_C"
TEAM_REQUEST_LIFETIME = "3.0"  # late callbacks remain inert, including during roster churn
TEAM_EVENT = "ch_team_verified"
TEAM_KICK_EVENT = "ch_team_kick_out"
TEAM_TAG = "chteam-4"
TEAM_ONE, TEAM_TWO = "0", "1"
GM_LIB = "/Script/Bodycam.BodycamGameModeLibrary"
CONTROLLER = "/Script/Engine.Controller"
TEAMKILL_EVENT = "ch_team_kill"


def gm_teamset(g):
    """The GM timer dispatches; request Actors own captured state and response delegates."""
    g.call("t_team", SYS, "K2_SetTimer",
           {"FunctionName": "TeamSweep", "Time": TEAM_PERIOD, "bLooping": "true", "bMaxOncePerFrame": "true"})
    g.selfnode("s_team"); g.link(("s_team.self", "t_team.Object"))
    g.existing("ev_team", "TeamSweep")
    g.get("tw_rule", "BB5BombRule")
    g.call("tw_recount", RULE, "RecountTeams"); g.link(("tw_rule.BB5BombRule", "tw_recount.self"))
    g.call("tw_gs", GS_LIB, "GetGameState")
    g.get("tw_pa", "PlayerArray", GAMESTATE); g.link(("tw_gs.ReturnValue", "tw_pa.self"))
    g.call("tw_n", ARR, "Array_Length", array=True); g.link(("tw_pa.PlayerArray", "tw_n.TargetArray"))
    g.call("tw_any", MATH, "Greater_IntInt", {"B": "0"}); g.link(("tw_n.ReturnValue", "tw_any.A"))
    g.branch("tw_br"); g.link(("tw_any.ReturnValue", "tw_br.condition"))
    g.chain("ev_team", "tw_recount", "tw_br")
    g.call("tw_clk", SYS, "GetGameTimeInSeconds")
    g.call("tw_clki", MATH, "FTrunc"); g.link(("tw_clk.ReturnValue", "tw_clki.A"))
    g.call("tw_slots", MATH, "Max", {"B": TEAM_MIN_SLOTS}); g.link(("tw_n.ReturnValue", "tw_slots.A"))
    g.call("tw_idx", MATH, "Percent_IntInt")
    g.link(("tw_clki.ReturnValue", "tw_idx.A"), ("tw_slots.ReturnValue", "tw_idx.B"))
    g.call("tw_inrange", MATH, "Less_IntInt")
    g.link(("tw_idx.ReturnValue", "tw_inrange.A"), ("tw_n.ReturnValue", "tw_inrange.B"))
    g.branch("tw_slotbr"); g.link(("tw_inrange.ReturnValue", "tw_slotbr.condition"), ("tw_br.then", "tw_slotbr.exec"))
    g.call("tw_get", ARR, "Array_Get", array=True)
    g.link(("tw_pa.PlayerArray", "tw_get.TargetArray"), ("tw_idx.ReturnValue", "tw_get.Index"))
    g.cast("tw_ps", BC_PS); g.link(("tw_get.Item", "tw_ps.cast_object"), ("tw_slotbr.then", "tw_ps.exec"))
    g.call("tw_hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("tw_hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("tw_hps.ReturnValue", "tw_hid.PlayerState"))
    g.call("tw_transform", MATH, "MakeTransform")
    g.spawn("tw_request", TEAM_REQUEST)
    g.link(("tw_transform.ReturnValue", "tw_request.SpawnTransform"), ("tw_ps.then", "tw_request.exec"))
    g.call("tw_init", TEAM_REQUEST, "BeginRequest")
    g.link(("tw_request.ReturnValue", "tw_init.self"), ("tw_ps.cast_result", "tw_init.Player"),
           ("tw_hid.ReturnValue", "tw_init.Host"), ("tw_rule.BB5BombRule", "tw_init.RuleComponent"))
    g.chain("tw_request", "tw_init")


def gm_arrival_guard(g):
    """Detect new identities locally at 10 Hz; send only one fast stranger ask per arrival.

    The cache records attempts, never authorizes membership. It is rebuilt from current
    identities each scan, so departures, reconnects and changed IDs cannot inherit an attempt.
    The regular team sweep remains the retry/revocation path. Keep all state on our component,
    not the retargeted GameMode's indexed property layout.
    """
    g.call("t_arrival", SYS, "K2_SetTimer", {"FunctionName": "ArrivalGuardTick", "Time": "0.1",
           "bLooping": "true", "bMaxOncePerFrame": "true"})
    g.selfnode("ag_self"); g.link(("ag_self.self", "t_arrival.Object"))
    g.existing("ag_tick", "ArrivalGuardTick")
    g.get("ag_rule", "BB5BombRule")
    g.call("ag_rule_valid", SYS, "IsValid"); g.link(("ag_rule.BB5BombRule", "ag_rule_valid.Object"))
    key, ranked = _start_key(g, "ag_context_")
    g.get("ag_token", REPORT_TOKEN_PROP, GI_CLASS)
    g.link(("ag_context_cast.cast_result", "ag_token.self"))
    g.call("ag_token_len", STR, "Len"); g.link(("ag_token." + REPORT_TOKEN_PROP, "ag_token_len.S"))
    g.call("ag_token_ready", MATH, "EqualEqual_IntInt", {"B": "64"})
    g.link(("ag_token_len.ReturnValue", "ag_token_ready.A"))
    g.call("ag_host_ps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("ag_host_id", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("ag_host_ps.ReturnValue", "ag_host_id.PlayerState"))

    def steam_id_valid(prefix, value):
        g.call(prefix + "_len", STR, "Len"); g.link((value, prefix + "_len.S"))
        g.call(prefix + "_length_ok", MATH, "EqualEqual_IntInt", {"B": "17"})
        g.link((prefix + "_len.ReturnValue", prefix + "_length_ok.A"))
        g.call(prefix + "_numeric", STR, "IsNumeric"); g.link((value, prefix + "_numeric.SourceString"))
        g.call(prefix + "_valid", MATH, "BooleanAND")
        g.link((prefix + "_length_ok.ReturnValue", prefix + "_valid.A"),
               (prefix + "_numeric.ReturnValue", prefix + "_valid.B"))
        return prefix + "_valid.ReturnValue"

    previous = "ag_rule_valid.ReturnValue"
    for i, condition in enumerate((ranked, "ag_token_ready.ReturnValue")):
        node = "ag_ready" + str(i)
        g.call(node, MATH, "BooleanAND"); g.link((previous, node + ".A"), (condition, node + ".B"))
        previous = node + ".ReturnValue"
    g.branch("ag_ready"); g.link((previous, "ag_ready.condition")); g.chain("ag_tick", "ag_ready")
    for node, prop, value in (("ag_clear", "GuardNext", ""), ("ag_reset", "GuardDispatched", "false")):
        g.set(node, prop, RULE, defaults={prop: value}); g.link(("ag_rule.BB5BombRule", node + ".self"))
    g.chain("ag_ready", "ag_clear", "ag_reset")
    g.call("ag_gs", GS_LIB, "GetGameState")
    g.get("ag_players", "PlayerArray", GAMESTATE); g.link(("ag_gs.ReturnValue", "ag_players.self"))
    g.foreach("ag_each"); g.link(("ag_players.PlayerArray", "ag_each.Array"), ("ag_reset.then", "ag_each.Exec"))
    g.cast("ag_player", BC_PS)
    g.link(("ag_each.Array Element", "ag_player.cast_object"), ("ag_each.LoopBody", "ag_player.exec"))
    g.call("ag_id", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("ag_player.cast_result", "ag_id.PlayerState"))
    subject_ok = steam_id_valid("ag_subject", "ag_id.ReturnValue")
    g.branch("ag_identity"); g.link((subject_ok, "ag_identity.condition")); g.chain("ag_player", "ag_identity")
    g.call("ag_path", SYS, "GetPathName"); g.link(("ag_player.cast_result", "ag_path.Object"))
    g.call("ag_key0", STR, "Concat_StrStr", {"A": "|"}); g.link((key, "ag_key0.B"))
    previous = "ag_key0.ReturnValue"
    for i, (literal, dynamic) in enumerate(((":", None), (None, "ag_id.ReturnValue"),
                                           (":", None), (None, "ag_path.ReturnValue"), ("|", None))):
        node = "ag_key" + str(i + 1)
        g.call(node, STR, "Concat_StrStr", {"B": literal} if literal is not None else None)
        g.link((previous, node + ".A"))
        if dynamic: g.link((dynamic, node + ".B"))
        previous = node + ".ReturnValue"
    identity_key = previous
    for node, prop in (("ag_seen", "GuardSeen"), ("ag_next", "GuardNext"), ("ag_dispatched", "GuardDispatched")):
        g.get(node, prop, RULE); g.link(("ag_rule.BB5BombRule", node + ".self"))
    g.call("ag_contains", STR, "Contains", {"bUseCase": "true"})
    g.link(("ag_seen.GuardSeen", "ag_contains.SearchIn"), (identity_key, "ag_contains.Substring"))
    g.branch("ag_known"); g.link(("ag_contains.ReturnValue", "ag_known.condition")); g.chain("ag_identity", "ag_known")
    g.branch("ag_busy"); g.link(("ag_dispatched.GuardDispatched", "ag_busy.condition"), ("ag_known.else", "ag_busy.exec"))
    g.set("ag_reserve", "GuardDispatched", RULE, defaults={"GuardDispatched": "true"})
    g.link(("ag_rule.BB5BombRule", "ag_reserve.self"), ("ag_busy.else", "ag_reserve.exec"))
    g.call("ag_transform", MATH, "MakeTransform")
    g.spawn("ag_request", TEAM_REQUEST)
    g.link(("ag_transform.ReturnValue", "ag_request.SpawnTransform")); g.chain("ag_reserve", "ag_request")
    g.call("ag_spawned", SYS, "IsValid"); g.link(("ag_request.ReturnValue", "ag_spawned.Object"))
    g.branch("ag_spawn_ok"); g.link(("ag_spawned.ReturnValue", "ag_spawn_ok.condition")); g.chain("ag_request", "ag_spawn_ok")
    g.call("ag_init", TEAM_REQUEST, "BeginRequest", {"FastOnly": "true"})
    g.link(("ag_request.ReturnValue", "ag_init.self"), ("ag_player.cast_result", "ag_init.Player"),
           ("ag_host_id.ReturnValue", "ag_init.Host"), ("ag_rule.BB5BombRule", "ag_init.RuleComponent"))
    g.chain("ag_spawn_ok", "ag_init")
    g.call("ag_append", STR, "Concat_StrStr")
    g.link(("ag_next.GuardNext", "ag_append.A"), (identity_key, "ag_append.B"))
    g.set("ag_remember", "GuardNext", RULE)
    g.link(("ag_rule.BB5BombRule", "ag_remember.self"), ("ag_append.ReturnValue", "ag_remember.GuardNext"),
           ("ag_known.then", "ag_remember.exec"), ("ag_init.then", "ag_remember.exec"))
    g.set("ag_finish", "GuardSeen", RULE)
    g.link(("ag_rule.BB5BombRule", "ag_finish.self"), ("ag_next.GuardNext", "ag_finish.GuardSeen"),
           ("ag_each.Completed", "ag_finish.exec"))


def team_request_events():
    g = G()
    g.custom("begin", "BeginRequest", [P("Player", "object", **{"class": BC_PS}),
             P("Host", "string"), P("RuleComponent", "object", **{"class": RULE}), P("FastOnly", "bool")])
    for name in ("One", "Two", "Stranger"):
        g.custom("answer_" + name, "On" + name, [P("bSuccess", "bool")])
    g.custom("verify_kick", "VerifyKick")
    return g.json()


def team_request_logic(*, kick_enabled=True):
    """One immutable player per Actor, positive side confirmations, post-write telemetry.

    Phase guards reject duplicate callbacks. Lifetime + explicit clock guard reject late ones;
    membership in PlayerArray rejects departures (including a reconnect with a new PlayerState).
    False side-one -> ask side-two, false side-two -> ask stranger, false stranger -> no action.
    No failure, refusal, or timeout is an instruction to write or kick.
    """
    g = G()
    g.existing("begin", "BeginRequest")
    g.get("subject", "Subject"); g.get("host", "HostId"); g.get("sid", "SubjectId")
    g.get("rule", "Rule"); g.get("deadline", "Deadline"); g.get("phase", "Phase")
    g.call("clock", SYS, "GetGameTimeInSeconds")
    g.call("clocki", MATH, "FTrunc"); g.link(("clock.ReturnValue", "clocki.A"))
    g.set("capture", "Subject"); g.link(("begin.Player", "capture.Subject"))
    g.set("capture_host", "HostId"); g.link(("begin.Host", "capture_host.HostId"))
    g.call("identity", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("subject.Subject", "identity.PlayerState"))
    g.set("capture_id", "SubjectId"); g.link(("identity.ReturnValue", "capture_id.SubjectId"))
    g.set("capture_rule", "Rule"); g.link(("begin.RuleComponent", "capture_rule.Rule"))
    g.call("expiry", MATH, "Add_IntInt", {"B": "3"}); g.link(("clocki.ReturnValue", "expiry.A"))
    g.set("capture_expiry", "Deadline"); g.link(("expiry.ReturnValue", "capture_expiry.Deadline"))
    g.call("lifetime", ACTOR, "SetLifeSpan", {"InLifespan": TEAM_REQUEST_LIFETIME})
    g.chain("begin", "capture", "capture_host", "capture_id", "capture_rule", "capture_expiry", "lifetime")

    def ask(name, kind, handler, phase):
        g.set(name + "_phase", "Phase", defaults={"Phase": str(phase)})
        _report_send(g, name + "_ask", {
            "URL": PROBE_TEAM_URL, "IP": "",
            "EventName": "ch_team_ask_" + kind, "Platform": kind,
            "FirstSessionTimestamp": TEAM_TAG, "IsFirstGameOpen": "false"})
        g.link(("host.HostId", name + "_ask.UserId"), ("sid.SubjectId", name + "_ask.Storefront"))
        g.n(name + "_delegate", "createevent", func=handler)
        g.link((name + "_delegate.OutputDelegate", name + "_ask.OnResponse"))
        g.chain(name + "_phase", name + "_ask")

    ask("one", "side-one", "OnOne", 1)
    ask("two", "side-two", "OnTwo", 2)
    ask("stranger", "stranger", "OnStranger", 3)
    g.branch("fast_only"); g.link(("begin.FastOnly", "fast_only.condition"))
    g.chain("lifetime", "fast_only")
    g.link(("fast_only.then", "stranger_phase.exec"), ("fast_only.else", "one_phase.exec"))

    # Guard all callbacks before looking at their bool. The request's target never changes.
    g.call("gs", GS_LIB, "GetGameState")
    g.get("players", "PlayerArray", GAMESTATE); g.link(("gs.ReturnValue", "players.self"))
    g.call("present", ARR, "Array_Contains", array=True)
    g.link(("players.PlayerArray", "present.TargetArray"), ("subject.Subject", "present.ItemToFind"))
    g.call("valid", SYS, "IsValid"); g.link(("subject.Subject", "valid.Object"))
    g.call("same_id", STR, "EqualEqual_StrStr")
    g.link(("identity.ReturnValue", "same_id.A"), ("sid.SubjectId", "same_id.B"))
    g.call("fresh", MATH, "Less_IntInt"); g.link(("clocki.ReturnValue", "fresh.A"), ("deadline.Deadline", "fresh.B"))
    previous = "valid.ReturnValue"
    for i, condition in enumerate(("present.ReturnValue", "same_id.ReturnValue", "fresh.ReturnValue")):
        node = "guard" + str(i)
        g.call(node, MATH, "BooleanAND"); g.link((previous, node + ".A"), (condition, node + ".B"))
        previous = node + ".ReturnValue"
    for name, event, phase in (("one", "OnOne", 1), ("two", "OnTwo", 2), ("stranger", "OnStranger", 3)):
        g.existing(name + "_answer", event)
        g.call(name + "_phase_ok", MATH, "EqualEqual_IntInt", {"B": str(phase)})
        g.link(("phase.Phase", name + "_phase_ok.A"))
        g.call(name + "_valid", MATH, "BooleanAND")
        g.link((previous, name + "_valid.A"), (name + "_phase_ok.ReturnValue", name + "_valid.B"))
        g.branch(name + "_guard"); g.link((name + "_valid.ReturnValue", name + "_guard.condition"))
        g.set(name + "_consume", "Phase", defaults={"Phase": "4"})
        g.chain(name + "_answer", name + "_guard", name + "_consume")
        g.branch(name + "_yes"); g.link((name + "_answer.bSuccess", name + "_yes.condition"))
        g.chain(name + "_consume", name + "_yes")
    g.link(("one_yes.else", "two_phase.exec"), ("two_yes.else", "stranger_phase.exec"))

    for name, want in (("one", TEAM_ONE), ("two", TEAM_TWO)):
        g.set(name + "_set", "TeamID", BC_PS, defaults={"TeamID": want})
        g.link(("subject.Subject", name + "_set.self"), (name + "_yes.then", name + "_set.exec"))
        g.call(name + "_recount", RULE, "RecountTeams"); g.link(("rule.Rule", name + "_recount.self"))
        g.get(name + "_actual", "TeamID", BC_PS); g.link(("subject.Subject", name + "_actual.self"))
        g.call(name + "_lib", GM_LIB, "GetPlayerTeamID"); g.link(("subject.Subject", name + "_lib.PlayerState"))
        # Retain the five-column wire shape, but column 3 is now the AFTER value.
        for suffix, pin in (("actual_s", name + "_actual.TeamID"), ("lib_s", name + "_lib.ReturnValue"), ("time_s", "clocki.ReturnValue")):
            g.call(name + "_" + suffix, STR, "Conv_IntToString"); g.link((pin, name + "_" + suffix + ".InInt"))
        g.call(name + "_row1", STR, "Concat_StrStr", {"A": "-1:0:" + want + ":"})
        g.link((name + "_actual_s.ReturnValue", name + "_row1.B"))
        g.call(name + "_row2", STR, "Concat_StrStr", {"B": ":"}); g.link((name + "_row1.ReturnValue", name + "_row2.A"))
        g.call(name + "_row3", STR, "Concat_StrStr")
        g.link((name + "_row2.ReturnValue", name + "_row3.A"), (name + "_lib_s.ReturnValue", name + "_row3.B"))
        _report_send(g, name + "_report", {
            "URL": PROBE_URL, "IP": "", "EventName": TEAM_EVENT,
            "FirstSessionTimestamp": TEAM_TAG, "IsFirstGameOpen": "false"})
        g.link(("host.HostId", name + "_report.UserId"), ("sid.SubjectId", name + "_report.Storefront"),
               (name + "_row3.ReturnValue", name + "_report.Platform"), (name + "_time_s.ReturnValue", name + "_report.Timestamp"))
        g.chain(name + "_set", name + "_recount", name + "_report")

    if not kick_enabled:
        # Explicit diagnostic generation remains available; normal builds enforce removal.
        _report_send(g, "outsider_report", {
            "URL": PROBE_URL, "IP": "", "EventName": "ch_team_outsider_detected",
            "Platform": "kick-disabled-diagnostic", "FirstSessionTimestamp": TEAM_TAG,
            "IsFirstGameOpen": "false"})
        g.link(("host.HostId", "outsider_report.UserId"), ("sid.SubjectId", "outsider_report.Storefront"),
               ("stranger_yes.then", "outsider_report.exec"))
        return g.json()

    # Call the existing Bodycam controller RPC reached by a successful KickPlayerInLobby,
    # bypassing its Steam string -> net-ID reconstruction and global controller lookup.
    # Native Server_KickPlayer checks authority then sends Client_LeaveSelf to its owner.
    g.call("kick_controller", PLAYERSTATE, "GetPlayerController")
    g.link(("subject.Subject", "kick_controller.self"))
    g.call("kick_pc_valid", SYS, "IsValid"); g.link(("kick_controller.ReturnValue", "kick_pc_valid.Object"))
    g.branch("kick_pc_guard"); g.link(("kick_pc_valid.ReturnValue", "kick_pc_guard.condition"))
    g.link(("stranger_yes.then", "kick_pc_guard.exec"))
    g.call("kick_authority", ACTOR, "HasAuthority")
    g.call("kick_local", CONTROLLER, "IsLocalController")
    g.link(("kick_controller.ReturnValue", "kick_local.self"))
    g.call("kick_remote", MATH, "Not_PreBool"); g.link(("kick_local.ReturnValue", "kick_remote.A"))
    g.get("kick_ps", "PlayerState", CONTROLLER); g.link(("kick_controller.ReturnValue", "kick_ps.self"))
    g.call("kick_same", MATH, "EqualEqual_ObjectObject")
    g.link(("kick_ps.PlayerState", "kick_same.A"), ("subject.Subject", "kick_same.B"))
    g.call("kick_owned", MATH, "BooleanAND")
    g.link(("kick_same.ReturnValue", "kick_owned.A"), ("kick_remote.ReturnValue", "kick_owned.B"))
    g.call("kick_allowed", MATH, "BooleanAND")
    g.link(("kick_owned.ReturnValue", "kick_allowed.A"), ("kick_authority.ReturnValue", "kick_allowed.B"))
    g.branch("kick_guard"); g.link(("kick_allowed.ReturnValue", "kick_guard.condition"))
    g.chain("kick_pc_guard", "kick_guard")
    g.cast("kick_bodycam", "/Script/Bodycam.BodycamPlayerController")
    g.link(("kick_controller.ReturnValue", "kick_bodycam.cast_object"))
    g.seq("kick_seq", 2); g.chain("kick_guard", "kick_bodycam", "kick_seq")
    _report_send(g, "kick_report", {
        "URL": PROBE_URL, "IP": "", "EventName": TEAM_KICK_EVENT,
        "Platform": "stranger", "FirstSessionTimestamp": TEAM_TAG, "IsFirstGameOpen": "false"})
    g.link(("host.HostId", "kick_report.UserId"), ("sid.SubjectId", "kick_report.Storefront"), ("kick_seq.then_0", "kick_report.exec"))
    g.call("kick", "/Script/Bodycam.BodycamPlayerController", "Server_KickPlayer")
    g.link(("kick_bodycam.cast_result", "kick.self"), ("kick_seq.then_1", "kick.exec"))
    g.set("kick_pending", "Phase", defaults={"Phase": "5"})
    g.call("kick_lifetime", ACTOR, "SetLifeSpan", {"InLifespan": "2.0"})
    g.selfnode("kick_self")
    g.call("kick_timer", SYS, "K2_SetTimer", {"FunctionName": "VerifyKick", "Time": "1.0", "bLooping": "false"})
    g.link(("kick_self.self", "kick_timer.Object"))
    _report_send(g, "kick_sent", {"URL": PROBE_URL, "IP": "", "EventName": "ch_team_kick_sent",
                 "Platform": "controller-rpc", "FirstSessionTimestamp": TEAM_TAG, "IsFirstGameOpen": "false"})
    g.link(("host.HostId", "kick_sent.UserId"), ("sid.SubjectId", "kick_sent.Storefront"))
    g.chain("kick", "kick_pending", "kick_lifetime", "kick_timer", "kick_sent")

    # The RPC has no result. Observe the captured PlayerState one second later without
    # touching its destroyed controller or mistaking a sent command for a disconnect.
    g.existing("kick_check", "VerifyKick")
    g.call("kick_check_phase", MATH, "EqualEqual_IntInt", {"B": "5"})
    g.link(("phase.Phase", "kick_check_phase.A"))
    g.branch("kick_check_guard"); g.link(("kick_check_phase.ReturnValue", "kick_check_guard.condition"))
    g.set("kick_checked", "Phase", defaults={"Phase": "6"})
    g.branch("kick_present"); g.link(("present.ReturnValue", "kick_present.condition"))
    g.chain("kick_check", "kick_check_guard", "kick_checked", "kick_present")
    for pin, status in (("then", "present"), ("else", "absent")):
        name = "kick_" + status + "_report"
        _report_send(g, name, {"URL": PROBE_URL, "IP": "", "EventName": "ch_team_kick_check",
                     "Platform": status, "FirstSessionTimestamp": TEAM_TAG, "IsFirstGameOpen": "false"})
        g.link(("host.HostId", name + ".UserId"), ("sid.SubjectId", name + ".Storefront"),
               ("kick_present." + pin, name + ".exec"))
    return g.json()

# ------------------------------------------------------------------------------------------------------------------
# TEAM KILLS (Sam, 2026-09-15: "add some kind of penalty for team killing ... issue them a queue
# penalty and a medium elo decrease as well as a warning").
#
# THE PAK ONLY REPORTS. It never judges and never punishes: what counts as malicious is a policy
# that has to be tuned against real matches, and a rule baked into a cooked asset can only be
# changed by shipping a new pak to every player. The backend decides (live.cjs teamKillReported),
# where the thresholds are environment dials.
#
# WHY THIS IS SAFE TO BIND. ABodycamGameMode::OnPlayerKilled is BlueprintAssignable and hands over
# both controllers - the same shape as ABodycamGameState::OnMatchEnded, which gm_exit has been
# binding since 2026-09-14. Binding is purely ADDITIVE; it cannot take anything away from whatever
# the game already does with the event. That is the distinction rule 1 is about: overriding
# BP_HandleRequestServerTravel (BlueprintImplementableEvent) is what crashed chlobby-17.
#
# THREE THINGS IT REFUSES TO REPORT, each of which would be a false accusation:
#   * a SUICIDE. Fall damage and your own grenade both arrive here with instigator == victim.
#   * a kill where either TeamID is still -1. The game leaves it there for the first ~30 s of the
#     match world (measured), and two unassigned players are not "on the same team" - reading it
#     that way would report every early death in the game as a team kill.
#   * an ordinary enemy kill, which is the overwhelming majority of what this event carries.
#
# WHAT IT SENDS, and why each field is there rather than being worked out on the server:
#   Storefront  the killer's SteamID64      who
#   Timestamp   the victim's SteamID64      to whom (repeat targeting is a signal on its own)
#   Platform    "team:elapsed:round:alive0:alive1"
#     team      the killer's in-game team
#     elapsed   seconds into the round. Under ~3 s nobody has engaged an enemy yet, so there is
#               no crossfire to have been caught in.
#     round     0 during warm-up, where a team kill means much less
#     alive0/1  how many are alive on EACH team. Both, rather than "how many enemies", because
#               working that out needs 1 - team and that assumes the ids are 0 and 1 - measured
#               true today, but an assumption that would fail silently rather than loudly.
def gm_teamkill(g):
    """Bind OnPlayerKilled and report the team kills. DECLARES its bind node; gm_logic chains it."""
    g.selfnode("tk_self")
    g.n("tk_ev", "createevent", func="OnKill")
    g.n("tk_bind", "adddelegate", delegate="OnPlayerKilled", **{"class": BC_GM})
    g.link(("tk_self.self", "tk_bind.self"), ("tk_ev.OutputDelegate", "tk_bind.Delegate"))

    g.existing("ev_kill", "OnKill")

    # SUICIDES FIRST. Same team trivially, and reporting one would accuse a player of killing
    # themselves on purpose - which the backend would then count towards a ban.
    g.call("tk_same", MATH, "EqualEqual_ObjectObject")
    g.link(("ev_kill.InstigatorController", "tk_same.A"), ("ev_kill.VictimController", "tk_same.B"))
    g.call("tk_notself", MATH, "Not_PreBool"); g.link(("tk_same.ReturnValue", "tk_notself.A"))
    g.branch("tk_br1"); g.link(("tk_notself.ReturnValue", "tk_br1.condition"))
    g.chain("ev_kill", "tk_br1")

    # both player states, and the teams off them
    g.get("tk_kps", "PlayerState", CONTROLLER); g.link(("ev_kill.InstigatorController", "tk_kps.self"))
    g.get("tk_vps", "PlayerState", CONTROLLER); g.link(("ev_kill.VictimController", "tk_vps.self"))
    g.cast("tk_kbp", BC_PS, pure=True); g.link(("tk_kps.PlayerState", "tk_kbp.cast_object"))
    g.cast("tk_vbp", BC_PS, pure=True); g.link(("tk_vps.PlayerState", "tk_vbp.cast_object"))
    g.get("tk_kteam", "TeamID", BC_PS); g.link(("tk_kbp.cast_result", "tk_kteam.self"))
    g.get("tk_vteam", "TeamID", BC_PS); g.link(("tk_vbp.cast_result", "tk_vteam.self"))

    # an UNASSIGNED player is on no team; -1 == -1 must never read as "same side"
    g.call("tk_known", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link(("tk_kteam.TeamID", "tk_known.A"))
    g.call("tk_sameteam", MATH, "EqualEqual_IntInt")
    g.link(("tk_kteam.TeamID", "tk_sameteam.A"), ("tk_vteam.TeamID", "tk_sameteam.B"))
    g.call("tk_isTK", MATH, "BooleanAND")
    g.link(("tk_known.ReturnValue", "tk_isTK.A"), ("tk_sameteam.ReturnValue", "tk_isTK.B"))
    g.branch("tk_br2"); g.link(("tk_isTK.ReturnValue", "tk_br2.condition"))
    g.link(("tk_br1.then", "tk_br2.exec"))

    # the context the backend classifies on
    g.call("tk_gs", GS_LIB, "GetGameState")
    g.cast("tk_asgs", BC_GS, pure=True); g.link(("tk_gs.ReturnValue", "tk_asgs.cast_object"))
    g.call("tk_el", BC_GS, "GetRoundElapsedSeconds"); g.link(("tk_asgs.cast_result", "tk_el.self"))
    g.call("tk_eli", MATH, "FTrunc"); g.link(("tk_el.ReturnValue", "tk_eli.A"))
    g.call("tk_rd", BC_GS, "GetCurrentRound"); g.link(("tk_asgs.cast_result", "tk_rd.self"))
    g.call("tk_a0", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "0"}); g.link(("tk_asgs.cast_result", "tk_a0.self"))
    g.call("tk_a1", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "1"}); g.link(("tk_asgs.cast_result", "tk_a1.self"))

    g.call("tk_kid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("tk_kps.PlayerState", "tk_kid.PlayerState"))
    g.call("tk_vid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("tk_vps.PlayerState", "tk_vid.PlayerState"))
    g.call("tk_hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("tk_hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("tk_hps.ReturnValue", "tk_hid.PlayerState"))

    for node, src in (("tk_teamS", "tk_kteam.TeamID"), ("tk_elS", "tk_eli.ReturnValue"),
                      ("tk_rdS", "tk_rd.ReturnValue"), ("tk_a0S", "tk_a0.ReturnValue"),
                      ("tk_a1S", "tk_a1.ReturnValue")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))

    def _cat(node, a_pin, b_pin=None, literal=None):
        g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
        g.link((a_pin, node + ".A"))
        if b_pin:
            g.link((b_pin, node + ".B"))
        return node + ".ReturnValue"

    # team:elapsed:round:alive0:alive1 - fixed positions, split on ":" and read by index
    q = _cat("tk_c1", "tk_teamS.ReturnValue", literal=":")
    q = _cat("tk_c2", q, b_pin="tk_elS.ReturnValue")
    q = _cat("tk_c3", q, literal=":")
    q = _cat("tk_c4", q, b_pin="tk_rdS.ReturnValue")
    q = _cat("tk_c5", q, literal=":")
    q = _cat("tk_c6", q, b_pin="tk_a0S.ReturnValue")
    q = _cat("tk_c7", q, literal=":")
    q = _cat("tk_c8", q, b_pin="tk_a1S.ReturnValue")

    _report_send(g, "tk_send", {
        "URL": PROBE_URL, "IP": "", "EventName": TEAMKILL_EVENT,
        "FirstSessionTimestamp": TEAM_TAG, "IsFirstGameOpen": "false"})
    g.link(("tk_hid.ReturnValue", "tk_send.UserId"),
           ("tk_kid.ReturnValue", "tk_send.Storefront"),
           ("tk_c8.ReturnValue", "tk_send.Platform"),
           ("tk_vid.ReturnValue", "tk_send.Timestamp"))
    g.link(("tk_br2.then", "tk_send.exec"))

def gm_arrival_reports(g):
    """Report native arrival twice, allowing for late lobby metadata, without mutating it."""
    for node, fn, period in (("t_read", "LobbyRead", T_READ),
                              ("t_write", "LobbyWrite", T_WRITE)):
        g.call(node, SYS, "K2_SetTimer", {"FunctionName": fn, "Time": period, "bLooping": "false"})
        g.selfnode("s_" + node); g.link(("s_" + node + ".self", node + ".Object"))
    g.chain("perktimer", "t_read", "t_write")
    g.existing("ev_read", "LobbyRead")
    g.chain("ev_read", _lobby_report(g, "r_", "ch_lobby_read"))
    g.existing("ev_write", "LobbyWrite")
    g.chain("ev_write", _lobby_report(g, "w_", "ch_lobby_write"))

def _exit_report(g, p, event_name):
    """One bare marker on the exit path. DestroyLobby's delegates are FEmptyOnlineDelegate, so
    there is no payload either way - that a given event fired IS the message."""
    g.call(p + "ps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call(p + "pid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link((p + "ps.ReturnValue", p + "pid.PlayerState"))
    _report_send(g, p + "snd", {
        "URL": PROBE_URL, "IP": "", "EventName": event_name,
        "Storefront": "bb5exit", "FirstSessionTimestamp": LOBBY_VALUE, "IsFirstGameOpen": "false"})
    g.link((p + "pid.ReturnValue", p + "snd.UserId"))
    return p + "snd"


def gm_exit(g):
    """Leave the lobby once the match is over, instead of leaving everyone sitting in it.

    THE GOAL (Sam, 2026-09-14): copy what FACEIT does - after a match, put people back where they
    started "so people dont get confused and keep playing". Exit the LOBBY; do not close the game.

    SUPERSEDED IN PART (Sam, 2026-09-15): the game IS closed after a match now - "the game must be
    closed anyways to queue so its much faster to just put the user back at base 1". THE CLOSE IS
    NOT DONE HERE, and deliberately. A GameMode exists on the server alone, so a QuitGame in this
    graph would close the host's game and leave the other nine sitting in a dead match; and the
    host is the one machine that must not quit early, because it is the only reporter (hop 1 of
    docs/match-result.md) and a game that quits at match end can shoot its own report. The hub
    closes it instead - on all ten PCs, and only after the hub has registered the match complete
    (hub/competitive.py, the CLOSE_GAME_* block).

    WHAT THAT MEANS FOR THIS GRAPH. Nothing, today: it is unchanged and still never run. The one
    thing to keep true is the ORDERING - the hub waits CLOSE_GAME_AFTER_SECONDS (20 s) before it
    closes anything, which is deliberately longer than EXIT_DELAY (15 s) below, so this exit path
    has finished its online calls before the process goes away. Closing a process with an online
    async call in flight is how a clean exit turns into the kind of dirty one that has been
    measured to lock an account out of hosting for 20+ minutes. If EXIT_DELAY is ever raised,
    raise CLOSE_GAME_AFTER_SECONDS with it.

    WHY THE GAMESTATE DELEGATE AND NOT THE GAMEMODE EVENT. ABodycamGameMode::OnMatchEnded is a
    BlueprintImplementableEvent, so overriding it in GM_BB5 would REPLACE whatever
    BP_BodycamGameModeAbstract does at match end - the same trap as BeginPlay's rule 2, except here
    we cannot see the parent's implementation to know what we would be throwing away.
    ABodycamGameState::OnMatchEnded is BlueprintAssignable, so BINDING to it is purely additive and
    cannot take anything away from anyone. Same moment, no risk.

    WHY A TIMER IS SAFE HERE, WHEN IT WAS NOT IN THE LOBBY. K2_SetTimer provably does not fire on
    the lobby GameMode - that cost chlobby-8 and pushed the lobby delay onto our own server. It does
    fire in the MATCH world: gm_probe's three timers are what produced ch_lobby_read / _write /
    _verify. The two worlds differ, and this is the one where timers work.

    WHY ONLY THE HOST RUNS THIS. A GameMode exists on the server alone, so this is the host's
    machine and nowhere else. That is expected to be enough: tearing down the session is the
    standard way clients get returned to the menu. If it turns out clients keep sitting there, the
    fix is a client-side binding to the same GameState delegate, which needs an actor we own on
    clients - we do not have one in BB5 today.

    EXIT_DELAY exists so the scoreboard is not cut off mid-read. It is one constant.
    """
    # BIND. GetGameState -> cast -> AddDelegate. The cast is pure, so it never joins an exec chain.
    g.call("x_gs", GS_LIB, "GetGameState")
    g.cast("x_gsc", BC_GS, pure=True)
    g.link(("x_gs.ReturnValue", "x_gsc.cast_object"))
    g.n("x_ev", "createevent", func="OnMatchOver")
    g.n("x_bind", "adddelegate", delegate="OnMatchEnded", **{"class": BC_GS})
    g.link(("x_gsc.cast_result", "x_bind.self"), ("x_ev.OutputDelegate", "x_bind.Delegate"))
    # Chained after the probe's last timer. When the TEMPORARY PROBE block is deleted this moves
    # back to hang off perktimer - it is the only thing holding it there.
    g.chain("t_write", "x_bind")

    # MATCH OVER -> wait out the scoreboard.
    g.existing("matchover", "OnMatchOver")
    g.call("x_timer", SYS, "K2_SetTimer",
           {"FunctionName": "MatchExit", "Time": EXIT_DELAY, "bLooping": "false"})
    g.selfnode("x_self"); g.link(("x_self.self", "x_timer.Object"))
    g.seq("x_mseq", 2)
    g.chain("matchover", "x_mseq")
    g.link(("x_mseq.then_0", "%s.exec" % _exit_report(g, "x_over", "ch_exit_armed")))
    # Ranked games keep the reporting world alive until the final batch is durably saved.
    # A timeout is never permission to destroy the lobby.
    _, ranked = _start_key(g, "exit_")
    g.branch("exit_ranked_branch"); g.link((ranked, "exit_ranked_branch.condition"), ("x_mseq.then_1", "exit_ranked_branch.exec"))
    g.get("exit_rule", "BB5BombRule")
    g.set("final_pending", "FinalPending", RULE, defaults={"FinalPending": "true"})
    g.link(("exit_rule.BB5BombRule", "final_pending.self"), ("exit_ranked_branch.then", "final_pending.exec"), ("exit_ranked_branch.else", "x_timer.exec"))

    import combat_graphs as combat
    g.get("exit_combat", "CombatManager", RULE); g.link(("exit_rule.BB5BombRule", "exit_combat.self"))
    g.cast("exit_manager", combat.GM, pure=True); g.link(("exit_combat.CombatManager", "exit_manager.cast_object"))
    g.call("close_combat", combat.GM, "CombatClose"); g.link(("exit_manager.cast_result", "close_combat.self")); g.chain("final_pending", "close_combat")

    # THE EXIT ITSELF. Marker on its own Sequence pin - SendAttributionEvent is LATENT and must
    # never sit in front of the thing being measured.
    g.existing("mexit", "MatchExit")
    g.seq("x_seq", 2)
    g.chain("mexit", "x_seq")
    g.link(("x_seq.then_0", "%s.exec" % _exit_report(g, "x_ask", "ch_exit_ask")))

    g.call("x_dl", DESTROY_LOBBY, "DestroyLobby", {"Timeout": EXIT_TIMEOUT})
    g.n("x_okd", "createevent", func="OnExitOk")
    g.n("x_okb", "adddelegate", delegate="OnSuccess", **{"class": DESTROY_LOBBY})
    g.link(("x_dl.ReturnValue", "x_okb.self"), ("x_okd.OutputDelegate", "x_okb.Delegate"))
    g.n("x_nod", "createevent", func="OnExitFail")
    g.n("x_nob", "adddelegate", delegate="OnFailure", **{"class": DESTROY_LOBBY})
    g.link(("x_dl.ReturnValue", "x_nob.self"), ("x_nod.OutputDelegate", "x_nob.Delegate"))
    g.call("x_act", ASYNC_BASE, "Activate")
    g.link(("x_dl.ReturnValue", "x_act.self"))
    g.chain("x_dl", "x_okb", "x_nob", "x_act")
    g.link(("x_seq.then_1", "x_dl.exec"))

    # Reported last, after Activate. Like the join, a SUCCESSFUL teardown may take the world with
    # it before this can send - so ch_exit_ok going missing proves nothing, and the screen is the
    # real evidence. ch_exit_ask is the marker that carries the weight.
    g.existing("exitok", "OnExitOk")
    g.chain("exitok", _exit_report(g, "x_ok", "ch_exit_ok"))
    g.existing("exitfail", "OnExitFail")
    g.chain("exitfail", _exit_report(g, "x_no", "ch_exit_fail"))


# ----------------------------------------------------------------------------------------------------------------------
# THE ROUND SNAPSHOT (layer 1 of docs/round-context.md). One call per round, carrying what the round
# WAS - which is the thing a match total can never reconstruct.
#
# WHY THE GAMESTATE DELEGATE AND NOT A GAMEMODE EVENT: the same reasoning gm_exit gives for
# OnMatchEnded. ABodycamGameMode's round events are BlueprintImplementableEvents, so overriding one
# REPLACES whatever BP_BodycamGameModeAbstract does at that moment and we cannot see what we would
# be throwing away. ABodycamGameState::OnRoundEnded is BlueprintAssignable, so BINDING to it is
# additive and cannot take anything from anyone. Same instant, no risk.
#
# The delegate carries NO PARAMETERS - it is a timing signal. Everything of substance comes from the
# pure getters, read at the instant it fires. The delegate says WHEN; the getters say WHAT.
#
# UNPROVEN, and deliberately said out loud: only OnMatchEnded has been watched live. The binding is
# the identical shape, so the risk is low - but a delegate that silently never fires is exactly what
# the component-targeted timer in gm_stats cost us, so the 1 s sweep is left running alongside this
# and is not to be removed until ch_bb5_round has been seen arriving in a real match.
def gm_round(g):
    g.call("rd_gs", GS_LIB, "GetGameState")
    g.cast("rd_gsc", BC_GS, pure=True); g.link(("rd_gs.ReturnValue", "rd_gsc.cast_object"))
    g.n("rd_ev", "createevent", func="OnRoundOver")
    g.n("rd_bind", "adddelegate", delegate="OnRoundEnded", **{"class": BC_GS})
    g.link(("rd_gsc.cast_result", "rd_bind.self"), ("rd_ev.OutputDelegate", "rd_bind.Delegate"))
    g.chain("x_bind", "rd_bind")

    g.existing("ev_round", "OnRoundOver")
    g.call("rr_gs", GS_LIB, "GetGameState")
    g.cast("rr_gsc", BC_GS, pure=True); g.link(("rr_gs.ReturnValue", "rr_gsc.cast_object"))

    g.call("rr_n", BC_GS, "GetCurrentRound"); g.link(("rr_gsc.cast_result", "rr_n.self"))
    g.call("rr_w", BC_GS, "GetRoundWinTeam"); g.link(("rr_gsc.cast_result", "rr_w.self"))
    g.call("rr_el", BC_GS, "GetRoundElapsedSeconds"); g.link(("rr_gsc.cast_result", "rr_el.self"))
    g.call("rr_eli", MATH, "FTrunc"); g.link(("rr_el.ReturnValue", "rr_eli.A"))
    g.call("rr_a0", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "0"}); g.link(("rr_gsc.cast_result", "rr_a0.self"))
    g.call("rr_a1", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "1"}); g.link(("rr_gsc.cast_result", "rr_a1.self"))

    for node, src in (("rr_nS", "rr_n.ReturnValue"), ("rr_wS", "rr_w.ReturnValue"),
                      ("rr_elS", "rr_eli.ReturnValue"),
                      ("rr_a0S", "rr_a0.ReturnValue"), ("rr_a1S", "rr_a1.ReturnValue")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))

    def _rcat(node, a_pin, b_pin=None, literal=None):
        g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
        g.link((a_pin, node + ".A"))
        if b_pin:
            g.link((b_pin, node + ".B"))
        return node + ".ReturnValue"

    # Keyed, like the stat row, and for the same reasons: ';' between fields, never ','.
    # n=<round>;w=<winner>;sec=<len>;a0=<alive team 0>;a1=<alive team 1>
    g.call("rr_c0", STR, "Concat_StrStr", {"A": "n="})
    g.link(("rr_nS.ReturnValue", "rr_c0.B"))
    p = _rcat("rr_c1", "rr_c0.ReturnValue", literal=";w=")
    p = _rcat("rr_cw", p, b_pin="rr_wS.ReturnValue")
    p = _rcat("rr_c2", p, literal=";sec=")
    p = _rcat("rr_c3", p, b_pin="rr_elS.ReturnValue")
    p = _rcat("rr_c4", p, literal=";a0=")
    p = _rcat("rr_c5", p, b_pin="rr_a0S.ReturnValue")
    p = _rcat("rr_c6", p, literal=";a1=")
    p = _rcat("rr_c7", p, b_pin="rr_a1S.ReturnValue")

    g.call("rr_hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("rr_hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("rr_hps.ReturnValue", "rr_hid.PlayerState"))
    g.call("rr_info", ONLINE, "GetCurrentLobbyInfo")
    g.call("rr_attr", ONLINE_TYPES, "GetLobbyInfoStringAttribute", {"Key": LOBBY_KEY})
    g.link(("rr_info.ReturnValue", "rr_attr.LobbyInfo"))

    _report_send(g, "rr_send", {
        "URL": PROBE_URL, "IP": "", "EventName": ROUND_EVENT,
        "FirstSessionTimestamp": "", "IsFirstGameOpen": "false"})
    g.link(("rr_hid.ReturnValue", "rr_send.UserId"),
           ("rr_attr.OutValue", "rr_send.Storefront"),
           ("rr_c7.ReturnValue", "rr_send.Platform"),
           ("rr_nS.ReturnValue", "rr_send.Timestamp"))
    g.chain("ev_round", "rr_send")


# ----------------------------------------------------------------------------------------------------------------------
# THE GAME-STATE HEARTBEAT, and it exists because of a measured risk rather than a hypothetical one.
#
# ch_bb5_round rides on ABodycamGameState::OnRoundEnded. That delegate DOES fire - it was watched
# live, n=0;w=0;sec=14;a0=1;a1=0 - but in a three-round match exactly ONE arrived, while the sweep,
# the score report and the lobby probes all kept reporting throughout. Why is not understood.
#
# Rather than keep guessing, the round state is ALSO sent from a plain looping timer, which is the
# one mechanism in this file with no open questions against it: the stats sweep and the score report
# have both run for whole matches on exactly this pattern. So correctness no longer depends on the
# delegate at all. If the delegate works it gives the exact instant a round ended; if it never fires
# again we still have every round's state, just sampled rather than pinpointed.
#
# It carries the same payload and lands in the same per-round store on the server, keyed by round
# number, last write wins. Within a round the heartbeat overwrites itself, so the value kept for
# round N is the last sample taken during it - the closest thing to its end state. A round winner
# that is not known yet reads -1 and is dropped as the sentinel it is, so a mid-round sample cannot
# invent one.
#
# Separate EVENT NAME on purpose: same handler, but the probe log then says plainly which mechanism
# produced which row, which is what will eventually explain the delegate.
def gm_heartbeat(g):
    g.call("t_state", SYS, "K2_SetTimer",
           {"FunctionName": "StateBeat", "Time": STATE_PERIOD, "bLooping": "true"})
    g.selfnode("s_state"); g.link(("s_state.self", "t_state.Object"))
    g.chain("rd_bind", "t_state")

    g.existing("ev_state", "StateBeat")
    g.call("hb_gs", GS_LIB, "GetGameState")
    g.cast("hb_gsc", BC_GS, pure=True); g.link(("hb_gs.ReturnValue", "hb_gsc.cast_object"))
    g.call("hb_n", BC_GS, "GetCurrentRound"); g.link(("hb_gsc.cast_result", "hb_n.self"))
    g.call("hb_w", BC_GS, "GetRoundWinTeam"); g.link(("hb_gsc.cast_result", "hb_w.self"))
    g.call("hb_el", BC_GS, "GetRoundElapsedSeconds"); g.link(("hb_gsc.cast_result", "hb_el.self"))
    g.call("hb_eli", MATH, "FTrunc"); g.link(("hb_el.ReturnValue", "hb_eli.A"))
    g.call("hb_elabs", MATH, "Abs_Int"); g.link(("hb_eli.ReturnValue", "hb_elabs.A"))   # the clock runs negative in warm-up
    g.call("hb_a0", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "0"}); g.link(("hb_gsc.cast_result", "hb_a0.self"))
    g.call("hb_a1", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "1"}); g.link(("hb_gsc.cast_result", "hb_a1.self"))

    for node, src in (("hb_nS", "hb_n.ReturnValue"), ("hb_wS", "hb_w.ReturnValue"),
                      ("hb_elS", "hb_elabs.ReturnValue"),
                      ("hb_a0S", "hb_a0.ReturnValue"), ("hb_a1S", "hb_a1.ReturnValue")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))

    def _hcat(node, a_pin, b_pin=None, literal=None):
        g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
        g.link((a_pin, node + ".A"))
        if b_pin:
            g.link((b_pin, node + ".B"))
        return node + ".ReturnValue"

    g.call("hb_c0", STR, "Concat_StrStr", {"A": "n="}); g.link(("hb_nS.ReturnValue", "hb_c0.B"))
    p = _hcat("hb_c1", "hb_c0.ReturnValue", literal=";w=")
    p = _hcat("hb_c2", p, b_pin="hb_wS.ReturnValue")
    p = _hcat("hb_c3", p, literal=";sec=")
    p = _hcat("hb_c4", p, b_pin="hb_elS.ReturnValue")
    p = _hcat("hb_c5", p, literal=";a0=")
    p = _hcat("hb_c6", p, b_pin="hb_a0S.ReturnValue")
    p = _hcat("hb_c7", p, literal=";a1=")
    p = _hcat("hb_c8", p, b_pin="hb_a1S.ReturnValue")

    # THE SCORELINE RIDES THE HEARTBEAT (2026-09-16). `FirstSessionTimestamp` was the one string field this event
    # did not use, and the match of that evening showed why it needed to: ch_bb5_score has a GetTeams() gate in front
    # of it and sent NOTHING all match, so nothing ever told the backend who won. This heartbeat has no gate at all -
    # it fired every 5 s throughout the same match, 53 times - so carrying the score here gives the settle path a
    # second route that cannot be closed by a branch.
    #
    # `GetTeamData(TeamID)` RATHER THAN GetTeams()[i], deliberately: it is the same BlueprintPure accessor family as
    # the CountAlivePlayerInTeam(0) / (1) two lines up, which have been answering with real numbers in every match
    # since 2026-09-15, and it takes a team ID instead of an array index - so it cannot be defeated by an array that
    # lists one team, which is exactly what is suspected of the gate. It returns the struct BY VALUE, so an absent
    # team can only come back zeroed, never out of range.
    #
    #   FirstSessionTimestamp   "<hostTeamId>|0:<score>|1:<score>;lim=<scoreLimit>"
    #
    # The host's own TeamID leads, for the same reason it leads ch_bb5_score's payload: we never call SetTeamId, so
    # the game's team ids mean nothing to the backend until one of them is pinned to a player on its roster.
    g.call("hb_td0", BC_GS, "GetTeamData", {"TeamID": "0"}); g.link(("hb_gsc.cast_result", "hb_td0.self"))
    g.call("hb_td1", BC_GS, "GetTeamData", {"TeamID": "1"}); g.link(("hb_gsc.cast_result", "hb_td1.self"))
    g.brk("hb_bd0", V_TEAMDATA); g.link(("hb_td0.ReturnValue", "hb_bd0.in"))
    g.brk("hb_bd1", V_TEAMDATA); g.link(("hb_td1.ReturnValue", "hb_bd1.in"))
    g.call("hb_lim", BC_GS, "GetScoreLimit"); g.link(("hb_gsc.cast_result", "hb_lim.self"))

    g.call("hb_hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("hb_hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("hb_hps.ReturnValue", "hb_hid.PlayerState"))
    g.cast("hb_psbc", BC_PS, pure=True); g.link(("hb_hps.ReturnValue", "hb_psbc.cast_object"))
    g.get("hb_hteam", "TeamID", BC_PS); g.link(("hb_psbc.cast_result", "hb_hteam.self"))

    for node, src in (("hb_htS", "hb_hteam.TeamID"), ("hb_s0S", "hb_bd0.TeamScore"),
                      ("hb_s1S", "hb_bd1.TeamScore"), ("hb_limS", "hb_lim.ReturnValue")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))

    q = _hcat("hb_d1", "hb_htS.ReturnValue", literal="|0:")
    q = _hcat("hb_d2", q, b_pin="hb_s0S.ReturnValue")
    q = _hcat("hb_d3", q, literal="|1:")
    q = _hcat("hb_d4", q, b_pin="hb_s1S.ReturnValue")
    q = _hcat("hb_d5", q, literal=";lim=")
    q = _hcat("hb_d6", q, b_pin="hb_limS.ReturnValue")

    # THE HEAD-COUNT, WITNESSED (2026-09-16). The two inputs of the game's own score-limit check, so a match that still fails
    # to end says why instead of leaving it to be inferred again: p0/p1 are Teams' PlayerCount for teams 0 and 1, bt/bs are
    # GetCurrentBestTeam()'s TeamID and TeamScore (THE TEAM HEAD-COUNT, above rule_fn_RecountTeams). Appended to Platform and
    # never to FirstSessionTimestamp: gameReportedRound ignores keys it does not know, while the scoreline is a strict regex.
    g.call("hb_best", BC_GS, "GetCurrentBestTeam"); g.link(("hb_gsc.cast_result", "hb_best.self"))
    g.brk("hb_bb", V_TEAMDATA); g.link(("hb_best.ReturnValue", "hb_bb.in"))
    for node, src in (("hb_p0S", "hb_bd0.PlayerCount"), ("hb_p1S", "hb_bd1.PlayerCount"),
                      ("hb_btS", "hb_bb.TeamID"), ("hb_bsS", "hb_bb.TeamScore")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))
    p = _hcat("hb_c9", p, literal=";p0=")
    p = _hcat("hb_c10", p, b_pin="hb_p0S.ReturnValue")
    p = _hcat("hb_c11", p, literal=";p1=")
    p = _hcat("hb_c12", p, b_pin="hb_p1S.ReturnValue")
    p = _hcat("hb_c13", p, literal=";bt=")
    p = _hcat("hb_c14", p, b_pin="hb_btS.ReturnValue")
    p = _hcat("hb_c15", p, literal=";bs=")
    p = _hcat("hb_c16", p, b_pin="hb_bsS.ReturnValue")
    # Record actual attack/defense roles and the native configured interval in the
    # existing heartbeat, so a spawn/pickup report can be checked against gameplay.
    for suffix, function in (("ot", "GetObjectiveTeam"), ("ct", "GetCounterObjectiveTeam"), ("si", "GetTeamSwitchInterval")):
        g.call("hb_" + suffix, BC_GS, function); g.link(("hb_gsc.cast_result", "hb_" + suffix + ".self"))
        g.call("hb_" + suffix + "S", STR, "Conv_IntToString"); g.link(("hb_" + suffix + ".ReturnValue", "hb_" + suffix + "S.InInt"))
        p = _hcat("hb_" + suffix + "key", p, literal=";" + suffix + "=")
        p = _hcat("hb_" + suffix + "val", p, b_pin="hb_" + suffix + "S.ReturnValue")

    g.call("hb_info", ONLINE, "GetCurrentLobbyInfo")
    g.call("hb_attr", ONLINE_TYPES, "GetLobbyInfoStringAttribute", {"Key": LOBBY_KEY})
    g.link(("hb_info.ReturnValue", "hb_attr.LobbyInfo"))
    _report_send(g, "hb_send", {
        "URL": PROBE_URL, "IP": "", "EventName": STATE_EVENT,
        "IsFirstGameOpen": "false"})
    g.link(("hb_hid.ReturnValue", "hb_send.UserId"),
           ("hb_attr.OutValue", "hb_send.Storefront"),
           (p, "hb_send.Platform"),
           ("hb_nS.ReturnValue", "hb_send.Timestamp"),
           (q, "hb_send.FirstSessionTimestamp"))
    g.chain("ev_state", "hb_send")


# ----------------------------------------------------------------------------------------------------------------------
# THE KILL FEED (layer 2). One event per kill: who killed whom, when, and how many were left.
#
# Sam, 2026-09-15: *"a kill feed of which user killed which user and based off the hub team
# selection it can then tell if a teammate killed a teammate"*. The pak deliberately does NOT decide
# that. It sends two SteamIDs and the server, which assigned the teams when it formed the match,
# decides whose side each was on. Everything the host sends is its unauthenticated word, and a host
# wanting to hide its team killing could report any TeamID it liked - it cannot rewrite our roster.
#
# WHY THIS EXISTS AT ALL: the kill COUNTER cannot see team killing. It is a net score, so a team
# kill and an enemy kill cancel and a sample taken after both shows nothing happened. No polling
# rate fixes that - the events cancel exactly. Two events cannot.
#
# THE INSTIGATOR CAN BE NULL - fall damage, the bomb, any world kill - and reading a PlayerState off
# a null Controller is a crash, not a warning. So IsValid gates the whole instigator read and the
# graph FORKS: one path names a killer, the other sends an empty `k=` and the server credits the
# death to nobody. Two send nodes rather than one is the price of never dereferencing null; the
# alternative would be a Select node, which this graph builder does not have.
def gm_kill(g):
    g.existing("kl_ev", "HandleKill")
    g.n("kl_bind", "adddelegate", delegate="OnPlayerKilled")
    g.link(("kl_ev.OutputDelegate", "kl_bind.Delegate"))
    g.chain("t_state", "kl_bind")
    g.existing("kl_start", "OnCompetitiveRoundStarted")
    g.n("kl_startbind", "adddelegate", delegate="OnRoundStarted", **{"class": BC_GS})
    g.link(("kl_gsc.cast_result", "kl_startbind.self"), ("kl_start.OutputDelegate", "kl_startbind.Delegate"))
    g.chain("kl_bind", "kl_startbind")
    g.get("kl_rule", "BB5BombRule")
    g.call("kl_begin", RULE, "BeginCompetitive"); g.link(("kl_rule.BB5BombRule", "kl_begin.self")); g.chain("kl_start", "kl_begin")

    # --- the victim, and the round context. All pure, so both branches below share them. ---
    g.get("kl_vps", "PlayerState", CONTROLLER); g.link(("kl_ev.VictimController", "kl_vps.self"))
    g.call("kl_vid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("kl_vps.PlayerState", "kl_vid.PlayerState"))

    g.call("kl_gs", GS_LIB, "GetGameState")
    g.cast("kl_gsc", BC_GS, pure=True); g.link(("kl_gs.ReturnValue", "kl_gsc.cast_object"))
    g.call("kl_n", BC_GS, "GetCurrentRound"); g.link(("kl_gsc.cast_result", "kl_n.self"))
    g.call("kl_el", BC_GS, "GetRoundElapsedSeconds"); g.link(("kl_gsc.cast_result", "kl_el.self"))
    g.call("kl_eli", MATH, "FTrunc"); g.link(("kl_el.ReturnValue", "kl_eli.A"))
    g.call("kl_elabs", MATH, "Abs_Int"); g.link(("kl_eli.ReturnValue", "kl_elabs.A"))
    g.call("kl_a0", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "0"}); g.link(("kl_gsc.cast_result", "kl_a0.self"))
    g.call("kl_a1", BC_GS, "CountAlivePlayerInTeam", {"TeamID": "1"}); g.link(("kl_gsc.cast_result", "kl_a1.self"))
    for node, src in (("kl_nS", "kl_n.ReturnValue"), ("kl_elS", "kl_elabs.ReturnValue"),
                      ("kl_a0S", "kl_a0.ReturnValue"), ("kl_a1S", "kl_a1.ReturnValue")):
        g.call(node, STR, "Conv_IntToString"); g.link((src, node + ".InInt"))

    g.call("kl_hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call("kl_hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("kl_hps.ReturnValue", "kl_hid.PlayerState"))
    g.call("kl_info", ONLINE, "GetCurrentLobbyInfo")
    g.call("kl_attr", ONLINE_TYPES, "GetLobbyInfoStringAttribute", {"Key": LOBBY_KEY})
    g.link(("kl_info.ReturnValue", "kl_attr.LobbyInfo"))

    # --- null check on the instigator: the fork ---
    g.call("kl_iv", SYS, "IsValid"); g.link(("kl_ev.InstigatorController", "kl_iv.Object"))
    g.branch("kl_br"); g.link(("kl_iv.ReturnValue", "kl_br.condition"))
    g.get("kl_started", "CompetitiveStarted", RULE); g.link(("kl_rule.BB5BombRule", "kl_started.self"))
    g.branch("kl_live"); g.link(("kl_started.CompetitiveStarted", "kl_live.condition")); g.chain("kl_ev", "kl_live", "kl_br")

    def _row(tag, killer_pin, killer_literal):
        """k=<killer>;v=<victim>;n=<round>;t=<sec>;a0=<alive>;a1=<alive>, then the send."""
        def cat(node, a_pin, b_pin=None, literal=None):
            g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
            g.link((a_pin, node + ".A"))
            if b_pin:
                g.link((b_pin, node + ".B"))
            return node + ".ReturnValue"
        if killer_pin:
            g.call(tag + "c0", STR, "Concat_StrStr", {"A": "k="})
            g.link((killer_pin, tag + "c0.B"))
            p = tag + "c0.ReturnValue"
        else:
            g.call(tag + "c0", STR, "Concat_StrStr", {"A": "k=", "B": killer_literal})
            p = tag + "c0.ReturnValue"
        p = cat(tag + "c1", p, literal=";v=")
        p = cat(tag + "c2", p, b_pin="kl_vid.ReturnValue")
        p = cat(tag + "c3", p, literal=";n=")
        p = cat(tag + "c4", p, b_pin="kl_nS.ReturnValue")
        p = cat(tag + "c5", p, literal=";t=")
        p = cat(tag + "c6", p, b_pin="kl_elS.ReturnValue")
        p = cat(tag + "c7", p, literal=";a0=")
        p = cat(tag + "c8", p, b_pin="kl_a0S.ReturnValue")
        p = cat(tag + "c9", p, literal=";a1=")
        p = cat(tag + "c10", p, b_pin="kl_a1S.ReturnValue")
        _report_send(g, tag + "snd", {
            "URL": PROBE_URL, "IP": "", "EventName": KILL_EVENT,
            "FirstSessionTimestamp": "", "IsFirstGameOpen": "false"})
        g.link(("kl_hid.ReturnValue", tag + "snd.UserId"),
               ("kl_attr.OutValue", tag + "snd.Storefront"),
               (p, tag + "snd.Platform"),
               ("kl_nS.ReturnValue", tag + "snd.Timestamp"))
        return tag + "snd"

    # A real killer: read its PlayerState only on this side of the branch.
    g.get("kl_ips", "PlayerState", CONTROLLER); g.link(("kl_ev.InstigatorController", "kl_ips.self"))
    g.call("kl_iid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link(("kl_ips.PlayerState", "kl_iid.PlayerState"))
    g.link(("kl_br.then", _row("kla_", "kl_iid.ReturnValue", None) + ".exec"))
    # A world kill: nobody is named, and nothing null is ever touched.
    g.link(("kl_br.else", _row("klb_", None, "") + ".exec"))



# ------------------------------------------------------------------------------------------------------------------
# THE PLAYER-COUNT GATE, and the measurement that has to come with it (Sam, 2026-09-16: "for a real host we dont
# want the ingame match to start unless all 10 players are in the game").
#
# WHAT THE GAME GIVES US. There is no minimum-players NUMBER anywhere in the mode config: FPhaseConfig carries only
# `bUseTimerForWaitingPlayers` and `WaitingForPlayersDuration`, a flag and a clock. What it does give us is a
# PREDICATE - `ABodycamGameMode::MinimalPlayerCountMeetCriteria()`, a BlueprintNativeEvent, i.e. overridable from a
# child Blueprint exactly like ShouldSpawnBots - and `RefreshWaitingForPlayer()`, a BlueprintCallable that asks the
# native to look at the waiting state again. So the gate is: answer the predicate ourselves, and nudge the native to
# re-ask each time the roster changes.
#
# THE THRESHOLD IS NOT A LITERAL. It is read from the config asset's `TeamConfig.MaxPlayers`, which the pak builder
# writes from the manifest's `rules` - and which the catalogue's `rules_override` can now set from Railway
# (docs/competitive.md). So "how many players before the match starts" is a deployed value: 10 for a real match, 2
# for a two-account test, without a new pak.
#
# IT FAILS OPEN, deliberately. A null ConfigDataAsset or an unset MaxPlayers breaks to 0, and `count >= 0` is true,
# so the gate disappears rather than holding a match shut for ever. The failure we can afford is a match that starts
# early; the one we cannot is ten people stuck in a lobby that never begins.
#
# WHY THE PROBE SHIPS WITH IT. Nothing here has been measured yet. The UHT dump gives signatures, not bodies - every
# one of these functions is an empty stub in it - so we do NOT know whether the native consults the predicate at all,
# nor whether a real ten-player match was ever starting short-handed in the first place. ShouldSpawnBots is the
# cautionary tale: it read `false` in the graph for a day while the cooked bytecode called the parent, and only
# disassembling the packed pak caught it. So OnMatchWaitingForPlayers and OnMatchStart both report - how many players
# were present, and what both predicates said at that moment - and the answer arrives from a real match rather than
# from a reading of a header.
PHASE_EVENT = "ch_bb5_phase"
CONFIG_DA = "/Script/Bodycam.GameModeConfigDataAsset"
TEAM_CONFIG = "/Script/Bodycam.TeamConfig"


def _roster_count(g, p):
    """`p`-prefixed nodes leaving the number of player states in the GameState on `<p>n.ReturnValue`.

    PlayerArray, not GetNumPlayers(): it is the roster the rest of this file already reads (the stats sweep walks
    the same array), so the gate counts exactly what the reports count."""
    g.call(p + "gs", GS_LIB, "GetGameState")
    g.get(p + "pa", "PlayerArray", GAMESTATE); g.link((p + "gs.ReturnValue", p + "pa.self"))
    g.call(p + "n", ARR, "Array_Length", array=True); g.link((p + "pa.PlayerArray", p + "n.TargetArray"))
    return p + "n.ReturnValue"


# THE FLOOR UNDER THE GATE. A lobby that reports 0 or 1 members is a lobby that is not ready to be
# asked - the attribute reads empty for the first half-minute of the match world, exactly as CH_MATCH
# does - and `roster >= 0` is TRUE, which would start a ranked match the instant the first player
# loaded in. Two is the smallest match anyone can play, so below it the gate simply stays shut and
# the native goes on waiting, which is what it did before this gate existed.
GATE_FLOOR = "2"


def _required_count(g, p):
    """`p`-prefixed nodes leaving HOW MANY PLAYERS THE LOBBY SAYS ARE COMING, on the returned pin.

    IT USED TO BE `TeamConfig.MaxPlayers`, and that number turned out to be load-bearing twice over
    (measured 2026-09-16/17, four live 1v1s). The game will not build `ABodycamGameState::Teams` at
    all unless TeamConfig.MaxPlayers is above 2 - with it at 2 the array is EMPTY, which kills the
    native score, the native ScoreLimit and therefore the match's own ending, and shows up in the
    probe ring as `ch_bb5_score_none teams=0`. But the same number was ALSO this gate's threshold,
    so raising it to 10 to get the teams back meant a two-player match was never full and looped
    warm-up for ever. One number, two jobs, and no value that does both.

    So the gate reads the LOBBY instead, which is a better answer to its own question anyway: the
    threshold should be how many people are actually coming, not how many the mode could hold. The
    match-world lobby reports `2/10` in a two-machine test and `10/10` in a real match (measured -
    ch_lobby_verify carries members/max in every match since 2026-09-15), and it is the same read the
    probe block has been doing every 45 s all along, so it costs nothing new.

    Sam, 2026-09-16, on the gate this feeds: the match must not start before everybody is in."""
    g.call(p + "info", ONLINE, "GetCurrentLobbyInfo")
    g.call(p + "mem", ONLINE_TYPES, "GetCurrentLobbyMembers"); g.link((p + "info.ReturnValue", p + "mem.LobbyInfo"))
    return p + "mem.ReturnValue"


def _gate_expr(g, p):
    """`(roster >= lobby members) AND (lobby members >= GATE_FLOOR)`, on the returned pin.

    PURE, because both predicates that use it are BlueprintPure and a pure graph has no exec pins to
    branch on. Both sides of the AND are therefore always evaluated; there is nothing to short out.
    """
    have = _roster_count(g, p)
    want = _required_count(g, p)
    g.call(p + "ge", MATH, "GreaterEqual_IntInt")
    g.link((have, p + "ge.A"), (want, p + "ge.B"))
    g.call(p + "floor", MATH, "GreaterEqual_IntInt", {"B": GATE_FLOOR})
    g.link((want, p + "floor.A"))
    g.call(p + "and", MATH, "BooleanAND")
    g.link((p + "ge.ReturnValue", p + "and.A"), (p + "floor.ReturnValue", p + "and.B"))
    return p + "and.ReturnValue"


def gm_min_players():
    """Override MinimalPlayerCountMeetCriteria() -> everyone the lobby is waiting on has arrived.

    A LINKED literal is not enough here and a linked expression is the whole point: see gm_shouldspawnbots for why
    an override that only sets the result node's default does nothing at all. The comparison drives ReturnValue
    directly, so the cooked bytecode has to read as the compare, not as a parent call - check it with
    tools/pak/kismet.py on the PACKED pak, never on the compile log."""
    return _start_predicate()


def gm_player_full():
    """Override PlayerCountIsFull() -> the same question, so neither predicate can hold the match.

    NEWLY OVERRIDDEN 2026-09-17. It was left native, and native reads the roster against
    `ABodycamGameState::MaxPlayers` - the same 10 that TeamConfig has to carry for the team array to
    exist. So with the teams fixed, `full` was false in a two-player match no matter what the minimum
    gate said, and the observed failure was a warm-up timer that counted to zero and started again.
    Which of the two the native actually consults is not documented anywhere we can read; answering
    both with the same expression means it does not matter.

    It is a widening, not a loosening: `full` now means "everyone the lobby is waiting on is here",
    which is true at 10/10 in a real match exactly as it was before."""
    return _start_predicate()


START_REQUEST = BB5_DIR + "/BP_BB5StartRequest.BP_BB5StartRequest_C"
START_URL = REPORT_URL + "/start-ready"
START_GI = "/Game/MenuSystemPro/Blueprints/Core/BodycamGI.BodycamGI_C"


def _start_key(g, p):
    # The existing per-match lobby pak stamps this BEFORE hosting/travel. Unlike lobby
    # membership this survives travel locally and cannot shrink as players load or leave.
    g.call(p + "gi", GS_LIB, "GetGameInstance")
    g.cast(p + "cast", START_GI, pure=True); g.link((p + "gi.ReturnValue", p + "cast.cast_object"))
    g.get(p + "key", "Session Name", START_GI); g.link((p + "cast.cast_result", p + "key.self"))
    g.call(p + "ranked", STR, "StartsWith", {"InPrefix": "chm-"})
    g.link((p + "key.Session Name", p + "ranked.SourceString"))
    return p + "key.Session Name", p + "ranked.ReturnValue"


def _start_predicate():
    """Both native predicates require a fresh approval for THIS exact current roster.

    Calls inside the function execute synchronously, including the complete roster scan.
    No network request or timer is allowed in a predicate. Ordinary non-ranked BB5 retains
    its old lobby-count rule; a chm- session can NEVER fall back to that count.
    """
    g = G(); g.entry(); g.result()
    key, ranked = _start_key(g, "key_")
    g.get("rule", "BB5BombRule")
    g.call("valid", SYS, "IsValid"); g.link(("rule.BB5BombRule", "valid.Object"))
    g.branch("ranked"); g.link((ranked, "ranked.condition")); g.chain("entry", "ranked")
    g.branch("valid_rule"); g.link(("valid.ReturnValue", "valid_rule.condition")); g.chain("ranked", "valid_rule")
    g.call("roster", RULE, "StartRoster"); g.link(("rule.BB5BombRule", "roster.self")); g.chain("valid_rule", "roster", "result")
    g.link(("ranked.else", "result.exec"), ("valid_rule.else", "result.exec"))
    for name in ("StartApprovedRoster", "StartApprovedMatch", "StartDeadline"):
        g.get(name, name, RULE); g.link(("rule.BB5BombRule", name + ".self"))
    g.call("same", STR, "EqualEqual_StrStr"); g.link(("roster.ReturnValue", "same.A"), ("StartApprovedRoster.StartApprovedRoster", "same.B"))
    g.call("match", STR, "EqualEqual_StrStr"); g.link((key, "match.A"), ("StartApprovedMatch.StartApprovedMatch", "match.B"))
    g.call("clock", SYS, "GetGameTimeInSeconds")
    g.call("clocki", MATH, "FTrunc"); g.link(("clock.ReturnValue", "clocki.A"))
    g.call("fresh", MATH, "Less_IntInt"); g.link(("clocki.ReturnValue", "fresh.A"), ("StartDeadline.StartDeadline", "fresh.B"))
    previous = "valid.ReturnValue"
    for i, check in enumerate(("same.ReturnValue", "match.ReturnValue", "fresh.ReturnValue")):
        name = "ok" + str(i)
        g.call(name, MATH, "BooleanAND"); g.link((previous, name + ".A"), (check, name + ".B")); previous = name + ".ReturnValue"
    legacy = _gate_expr(g, "legacy_")
    g.call("ranked_ok", MATH, "BooleanAND"); g.link((previous, "ranked_ok.A"), (ranked, "ranked_ok.B"))
    g.call("casual", MATH, "Not_PreBool"); g.link((ranked, "casual.A"))
    g.call("casual_ok", MATH, "BooleanAND"); g.link((legacy, "casual_ok.A"), ("casual.ReturnValue", "casual_ok.B"))
    g.call("choose", MATH, "BooleanOR"); g.link(("ranked_ok.ReturnValue", "choose.A"), ("casual_ok.ReturnValue", "choose.B"))
    g.link(("choose.ReturnValue", "result.ReturnValue"))
    return g.json()


def gm_start_gate(g):
    g.call("t_startgate", SYS, "K2_SetTimer", {"FunctionName": "StartGateTick", "Time": "2.0", "bLooping": "true"})
    g.selfnode("start_self"); g.link(("start_self.self", "t_startgate.Object"))
    g.existing("start_tick", "StartGateTick")
    key, ranked = _start_key(g, "start_")
    g.branch("start_branch"); g.link((ranked, "start_branch.condition")); g.chain("start_tick", "start_branch")
    g.get("start_rule", "BB5BombRule")
    g.call("start_transform", MATH, "MakeTransform")
    g.spawn("start_request", START_REQUEST); g.link(("start_transform.ReturnValue", "start_request.SpawnTransform")); g.chain("start_branch", "start_request")
    g.call("start_init", START_REQUEST, "BeginStartRequest")
    g.link(("start_request.ReturnValue", "start_init.self"), ("start_rule.BB5BombRule", "start_init.RuleComponent"), (key, "start_init.MatchKey"))
    g.chain("start_request", "start_init")


def start_request_events():
    g = G()
    g.custom("begin", "BeginStartRequest", [P("RuleComponent", "object", **{"class": RULE}), P("MatchKey", "string")])
    g.custom("reply", "OnStartReply", [P("bSuccess", "bool")])
    return g.json()


def start_request_logic():
    """A short-lived request owns its snapshot; late/duplicate replies cannot unlock a new roster."""
    g = G(); g.existing("begin", "BeginStartRequest")
    g.get("rule", "Rule"); g.get("key", "MatchKey"); g.get("snapshot", "Snapshot"); g.get("deadline", "Deadline"); g.get("phase", "Phase")
    g.set("capture_rule", "Rule"); g.link(("begin.RuleComponent", "capture_rule.Rule"))
    g.set("capture_key", "MatchKey"); g.link(("begin.MatchKey", "capture_key.MatchKey")); g.chain("begin", "capture_rule", "capture_key")
    g.call("roster", RULE, "StartRoster"); g.link(("rule.Rule", "roster.self")); g.chain("capture_key", "roster")
    g.set("capture_roster", "Snapshot"); g.link(("roster.ReturnValue", "capture_roster.Snapshot")); g.chain("roster", "capture_roster")
    g.call("clock", SYS, "GetGameTimeInSeconds"); g.call("clocki", MATH, "FTrunc"); g.link(("clock.ReturnValue", "clocki.A"))
    g.call("expiry", MATH, "Add_IntInt", {"B": "3"}); g.link(("clocki.ReturnValue", "expiry.A"))
    g.set("capture_expiry", "Deadline"); g.link(("expiry.ReturnValue", "capture_expiry.Deadline")); g.chain("capture_roster", "capture_expiry")
    g.set("pending", "Phase", defaults={"Phase": "1"})
    g.call("lifespan", ACTOR, "SetLifeSpan", {"InLifespan": "3.0"}); g.chain("capture_expiry", "pending", "lifespan")
    g.call("host", ONLINE, "GetPlatformUserNetId")
    _report_send(g, "ask", {"URL": START_URL, "IP": "",
           "EventName": "ch_start_ready", "FirstSessionTimestamp": "chstart-1", "IsFirstGameOpen": "false"})
    g.link(("host.ReturnValue", "ask.UserId"), ("key.MatchKey", "ask.Storefront"), ("snapshot.Snapshot", "ask.Platform"))
    g.n("delegate", "createevent", func="OnStartReply"); g.link(("delegate.OutputDelegate", "ask.OnResponse")); g.chain("lifespan", "ask")

    g.existing("reply", "OnStartReply")
    g.call("valid", SYS, "IsValid"); g.link(("rule.Rule", "valid.Object"))
    g.call("pending_ok", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("phase.Phase", "pending_ok.A"))
    g.call("fresh", MATH, "Less_IntInt"); g.link(("clocki.ReturnValue", "fresh.A"), ("deadline.Deadline", "fresh.B"))
    previous = "reply.bSuccess"
    for i, check in enumerate(("valid.ReturnValue", "pending_ok.ReturnValue", "fresh.ReturnValue")):
        name = "guard" + str(i); g.call(name, MATH, "BooleanAND"); g.link((previous, name + ".A"), (check, name + ".B")); previous = name + ".ReturnValue"
    g.branch("guard"); g.link((previous, "guard.condition")); g.chain("reply", "guard")
    g.set("consume", "Phase", defaults={"Phase": "2"}); g.chain("guard", "consume")
    # Recheck all identities, teams and controller presence after the HTTP callback.
    g.call("current", RULE, "StartRoster"); g.link(("rule.Rule", "current.self")); g.chain("consume", "current")
    g.call("same", STR, "EqualEqual_StrStr"); g.link(("current.ReturnValue", "same.A"), ("snapshot.Snapshot", "same.B"))
    current_key, _ = _start_key(g, "current_")
    g.call("same_match", STR, "EqualEqual_StrStr"); g.link((current_key, "same_match.A"), ("key.MatchKey", "same_match.B"))
    g.call("unchanged", MATH, "BooleanAND"); g.link(("same.ReturnValue", "unchanged.A"), ("same_match.ReturnValue", "unchanged.B"))
    g.branch("accept"); g.link(("unchanged.ReturnValue", "accept.condition")); g.chain("current", "accept")
    previous = "accept"
    for name, value in (("StartApprovedRoster", "snapshot.Snapshot"), ("StartApprovedMatch", "key.MatchKey"), ("StartDeadline", "deadline.Deadline")):
        g.set(name, name, RULE); g.link(("rule.Rule", name + ".self"), (value, name + "." + name)); g.chain(previous, name); previous = name
    g.call("gm", GS_LIB, "GetGameMode")
    g.cast("gmc", BC_GM, pure=True); g.link(("gm.ReturnValue", "gmc.cast_object"))
    g.call("refresh", BC_GM, "RefreshWaitingForPlayer"); g.link(("gmc.cast_result", "refresh.self")); g.chain(previous, "refresh")
    return g.json()


def gm_final_report(g):
    """After OnMatchEnded, batch all stats on the next tick and retry until saved.

    The native reply is only a bool: this endpoint returns 200 only after durable commit.
    Its delegate belongs to this GameMode/world; travelling destroys it and its callbacks.
    """
    g.call("t_final", SYS, "K2_SetTimer", {"FunctionName": "FinalTick", "Time": "2.0", "bLooping": "true"})
    g.selfnode("final_self"); g.link(("final_self.self", "t_final.Object"))
    g.existing("final_tick", "FinalTick")
    g.get("final_rule", "BB5BombRule")
    for name in ("FinalPending", "FinalAcknowledged"):
        g.get(name, name, RULE); g.link(("final_rule.BB5BombRule", name + ".self"))
    g.call("final_unacked", MATH, "Not_PreBool"); g.link(("FinalAcknowledged.FinalAcknowledged", "final_unacked.A"))
    g.call("final_waiting", MATH, "BooleanAND"); g.link(("FinalPending.FinalPending", "final_waiting.A"), ("final_unacked.ReturnValue", "final_waiting.B"))
    g.branch("final_branch"); g.link(("final_waiting.ReturnValue", "final_branch.condition")); g.chain("final_tick", "final_branch")
    g.get("final_captured", "FinalCaptured", RULE); g.link(("final_rule.BB5BombRule", "final_captured.self"))
    g.branch("final_capture_branch"); g.link(("final_captured.FinalCaptured", "final_capture_branch.condition")); g.chain("final_branch", "final_capture_branch")
    g.call("final_rows", RULE, "FinalRows"); g.link(("final_rule.BB5BombRule", "final_rows.self"), ("final_capture_branch.else", "final_rows.exec"))
    g.call("final_gs", GS_LIB, "GetGameState"); g.cast("final_gsc", BC_GS, pure=True); g.link(("final_gs.ReturnValue", "final_gsc.cast_object"))
    for name, fn in (("round", "GetCurrentRound"), ("limit", "GetScoreLimit")):
        g.call("final_" + name, BC_GS, fn); g.link(("final_gsc.cast_result", "final_" + name + ".self"))
    g.call("final_teams", BC_GS, "GetTeams"); g.link(("final_gsc.cast_result", "final_teams.self"))
    g.call("final_n", ARR, "Array_Length", array=True); g.link(("final_teams.ReturnValue", "final_n.TargetArray"))
    g.call("final_two", MATH, "GreaterEqual_IntInt", {"B": "2"}); g.link(("final_n.ReturnValue", "final_two.A"))
    g.branch("final_has_teams"); g.link(("final_two.ReturnValue", "final_has_teams.condition")); g.chain("final_rows", "final_has_teams")
    fields = ["final_round.ReturnValue", "final_limit.ReturnValue"]
    for i in (0, 1):
        name = "final_team" + str(i)
        g.call(name, ARR, "Array_Get", {"Index": str(i)}, array=True); g.link(("final_teams.ReturnValue", name + ".TargetArray"))
        g.brk(name + "data", V_TEAMDATA); g.link((name + ".Item", name + "data.in"))
        fields.extend((name + "data.TeamID", name + "data.TeamScore"))
    previous = None
    for i, value in enumerate(fields):
        name = "final_number" + str(i)
        g.call(name, STR, "Conv_IntToString"); g.link((value, name + ".InInt"))
        if previous:
            g.call(name + "sep", STR, "Concat_StrStr", {"B": ";"}); g.link((previous, name + "sep.A"))
            g.call(name + "cat", STR, "Concat_StrStr"); g.link((name + "sep.ReturnValue", name + "cat.A"), (name + ".ReturnValue", name + "cat.B"))
            previous = name + "cat.ReturnValue"
        else: previous = name + ".ReturnValue"
    key, _ = _start_key(g, "final_key_")
    import combat_graphs as combat
    g.get("final_combat", "CombatManager", RULE); g.link(("final_rule.BB5BombRule", "final_combat.self"))
    g.cast("final_manager", combat.GM, pure=True); g.link(("final_combat.CombatManager", "final_manager.cast_object"))
    for field in ("CombatEpoch", "CombatSeq"):
        g.get(field, field, combat.GM); g.link(("final_manager.cast_result", field + ".self"))
    g.call("final_seq", STR, "Conv_IntToString"); g.link(("CombatSeq.CombatSeq", "final_seq.InInt"))
    g.call("final_epoch_sep", STR, "Concat_StrStr", {"B": ";"}); g.link(("CombatEpoch.CombatEpoch", "final_epoch_sep.A"))
    g.call("final_combat_stamp", STR, "Concat_StrStr"); g.link(("final_epoch_sep.ReturnValue", "final_combat_stamp.A"), ("final_seq.ReturnValue", "final_combat_stamp.B"))
    after = "final_has_teams"
    for name, value in (("FinalBatch", "final_rows.ReturnValue"), ("FinalMeta", previous), ("FinalMatch", key), ("FinalCombat", "final_combat_stamp.ReturnValue")):
        g.set("freeze_" + name, name, RULE); g.link(("final_rule.BB5BombRule", "freeze_" + name + ".self"), (value, "freeze_" + name + "." + name))
        g.chain(after, "freeze_" + name); after = "freeze_" + name
        g.get("frozen_" + name, name, RULE); g.link(("final_rule.BB5BombRule", "frozen_" + name + ".self"))
    g.set("freeze_final", "FinalCaptured", RULE, defaults={"FinalCaptured": "true"}); g.link(("final_rule.BB5BombRule", "freeze_final.self")); g.chain(after, "freeze_final")
    g.call("final_host", ONLINE, "GetPlatformUserNetId")
    _report_send(g, "final_send", {"URL": REPORT_URL + "/final", "IP": "",
           "EventName": "ch_final_snapshot", "FirstSessionTimestamp": "chfinal-1", "IsFirstGameOpen": "false"})
    g.link(("frozen_FinalCombat.FinalCombat", "final_send.IP"), ("final_host.ReturnValue", "final_send.UserId"), ("frozen_FinalMatch.FinalMatch", "final_send.Storefront"), ("frozen_FinalBatch.FinalBatch", "final_send.Platform"), ("frozen_FinalMeta.FinalMeta", "final_send.Timestamp"))
    g.n("final_delegate", "createevent", func="OnFinalReply"); g.link(("final_delegate.OutputDelegate", "final_send.OnResponse")); g.chain("freeze_final", "final_send"); g.chain("final_capture_branch", "final_send")
    g.existing("final_reply", "OnFinalReply")
    g.call("final_accepted", MATH, "BooleanAND"); g.link(("final_reply.bSuccess", "final_accepted.A"), ("final_waiting.ReturnValue", "final_accepted.B"))
    g.branch("final_accept"); g.link(("final_accepted.ReturnValue", "final_accept.condition")); g.chain("final_reply", "final_accept")
    g.set("final_saved", "FinalAcknowledged", RULE, defaults={"FinalAcknowledged": "true"}); g.link(("final_rule.BB5BombRule", "final_saved.self")); g.chain("final_accept", "final_saved")
    g.call("final_exit", "/Game/GM/Gamemode/GM_BB5.GM_BB5_C", "MatchExit"); g.chain("final_saved", "final_exit")


def _phase_report(g, p, which, after):
    """One `ch_bb5_phase` send, chained off the event node `after`.

    Packed into the six string slots the probe has used since 2026-09-14 (see the PROBE block):
      UserId     the host's SteamID64, so the server can attribute it to a match
      EventName  ch_bb5_phase
      Platform   which moment this is: "waiting" or "start"
      Storefront the roster size at that moment - the number the whole question is about
      Timestamp  min=<MinimalPlayerCountMeetCriteria>;full=<PlayerCountIsFull>;want=<MaxPlayers>
    """
    count = _roster_count(g, p)
    want = _required_count(g, p)
    g.call(p + "cS", STR, "Conv_IntToString"); g.link((count, p + "cS.InInt"))
    g.call(p + "wS", STR, "Conv_IntToString"); g.link((want, p + "wS.InInt"))
    # Both predicates, as the game answers them at this instant. `min` is OUR override once it is in; reading it
    # back is how we learn the native actually calls it.
    g.call(p + "min", BC_GM, "MinimalPlayerCountMeetCriteria")
    g.call(p + "full", BC_GM, "PlayerCountIsFull")
    g.call(p + "minS", STR, "Conv_BoolToString"); g.link((p + "min.ReturnValue", p + "minS.InBool"))
    g.call(p + "fullS", STR, "Conv_BoolToString"); g.link((p + "full.ReturnValue", p + "fullS.InBool"))

    def cat(node, a_pin, b_pin=None, literal=None):
        g.call(node, STR, "Concat_StrStr", {} if b_pin else {"B": literal})
        g.link((a_pin, node + ".A"))
        if b_pin:
            g.link((b_pin, node + ".B"))
        return node + ".ReturnValue"

    row = cat(p + "r1", p + "minS.ReturnValue", literal=";full=")
    row = cat(p + "r2", row, b_pin=p + "fullS.ReturnValue")
    row = cat(p + "r3", row, literal=";want=")
    row = cat(p + "r4", row, b_pin=p + "wS.ReturnValue")

    g.call(p + "hps", GS_LIB, "GetPlayerState", {"PlayerStateIndex": "0"})
    g.call(p + "hid", ONLINE, "RetrievePlatformIdAsStringFromPlayerState")
    g.link((p + "hps.ReturnValue", p + "hid.PlayerState"))

    _report_send(g, p + "send", {
        "URL": PROBE_URL, "IP": "", "EventName": PHASE_EVENT,
        "Platform": which, "FirstSessionTimestamp": "", "IsFirstGameOpen": "false"})
    g.link((p + "hid.ReturnValue", p + "send.UserId"),
           (p + "cS.ReturnValue", p + "send.Storefront"),
           (row, p + "send.Timestamp"))
    g.chain(after, p + "send")


def gm_phase(g):
    """The two moments worth knowing about, each reporting the roster and both predicates.

    OnMatchWaitingForPlayers and OnMatchStart are BlueprintImplementableEvent on ABodycamGameMode, so implementing
    them in GM_BB5 is purely additive - the native decides when they fire and nothing here can hold them up.

    RefreshWaitingForPlayer() rides on the waiting event: with the gate in place the native has to be asked to look
    again once the roster grows, or a predicate that only flips after the last player arrives is never re-read."""
    g.event("wfp", "OnMatchWaitingForPlayers", BC_GM)
    _phase_report(g, "wf_", "waiting", "wfp")
    g.call("wf_refresh", BC_GM, "RefreshWaitingForPlayer")
    g.chain("wf_send", "wf_refresh")

    g.event("mst", "OnMatchStart", BC_GM)
    _phase_report(g, "ms_", "start", "mst")


def gm_shouldspawnbots():
    """Override ShouldSpawnBots() -> false (GM_BodyBomb does the same: no bots in Bodybomb).

    THIS OVERRIDE NEVER DID ANYTHING UNTIL NOW, in either direction (measured 2026-09-15). It used
    to set only the result node's ReturnValue default, which is dead: BodycamMirrorTools's
    OverrideFunction seeds an override graph with entry -> CallParentFunction -> result ALREADY
    WIRED, and BuildGraph REUSES the entry/result nodes rather than replacing them, so that parent
    link survives every build - and an input pin carrying a link ignores its default. The cooked
    bytecode read:

        ReturnValue = Super::ShouldSpawnBots()

    i.e. BB5 inherited whatever the native does and the word "false" was decoration. It was only
    caught by flipping it to true, rebuilding, and finding the pak byte-identical apart from node
    GUIDs - the compile log says "ok" either way, and so does the cook.

    A literal has to be LINKED in. An input pin takes one connection, so this replaces the
    parent's and the bytecode becomes `ReturnValue = MakeLiteralBool(False)`. Verify an override
    of this kind by disassembling the PACKED pak with tools/pak/kismet.py; nothing earlier in the
    pipeline will tell you.
    """
    g = G(); g.entry(); g.result()
    g.call("ssb_lit", SYS, "MakeLiteralBool", {"Value": "false"})
    g.link(("ssb_lit.ReturnValue", "result.ReturnValue"))
    return g.json()

if __name__ == "__main__":
    import sys
    graphs = {"inventory_events": inventory_events(), "rule_events": rule_events(), "rule_assign": rule_assign(), "rule_logic": rule_logic(),
              "gm_events": gm_events(), "gm_logic": gm_logic(), "gm_shouldspawnbots": gm_shouldspawnbots(),
              "gm_min_players": gm_min_players(), "gm_player_full": gm_player_full()}
    for f in INVENTORY_FUNCTIONS: graphs["inv_sig_" + f] = inventory_signature(f)
    for f in RULE_FUNCTIONS: graphs["rule_sig_" + f] = rule_signature(f); graphs["rule_fn_" + f] = RULE_FN_BODIES[f]()
    bad = 0
    for name, js in graphs.items():
        d = json.loads(js); ids = {n["id"] for n in d["nodes"]}
        for a, b in d["links"]:
            for ref in (a, b):
                if ref.split(".", 1)[0] not in ids: print(f"{name}: link to unknown node {ref}"); bad += 1
        print(f"{name}: {len(d['nodes'])} nodes, {len(d['links'])} links")
    sys.exit(1 if bad else 0)
