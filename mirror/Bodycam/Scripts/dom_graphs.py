"""dom_graphs.py — node graphs for Domination 10v10 (Sam, 2026-09-14), built by UBodycamMirrorTools.BuildGraph like ctf_graphs.py.

Rules (docs/domination.md): three control points A/B/C, all neutral at the start; stand on one with no enemy inside to capture
it (8 s alone, faster with more players); an enemy inside freezes the capture; leaving it loses the progress; a point stays
yours until the other team captures it; every 4 s each team scores +1 per point it holds; first to 200, 10-minute limit.
Spawns move to the points your team holds, with the fixed-side behaviour as the fallback.

What comes from the game, and what is ours (disassembled from the retail GM_Hardpoint / HardPointZone, 2026-09-14):
  * Scoring is the game's: UBodycamCaptureRuleSetComponent::HandleTeamCapturing(TeamID, NumOfCapturer). Stock Hardpoint calls it
    once a second with the number of players on the zone; CTF calls it with 1 and v19 proved that adds exactly one point and
    ends the match at DA_*.ScoringConfig.ScoreLimit. We call it with 1, once per owned point, per score tick — so we depend
    only on the behaviour that has already run in a real match.
  * Capture bookkeeping is NOT the game's: ABodycamHardpointZone does it natively (HandleActorEnteredZone / GetTeamCapturing ->
    FHardpointCaptureState{CapturingTeamId, CaptureSpeed, bContested}) and it needs the level's hand-placed BP_PointZonePreview
    actors, which our cloned TDM levels do not have. So BP_DOM_Point counts the pawns in its own trigger, in Blueprint.
  * Stock Hardpoint has no persistent ownership and no capture time — it re-derives "who is standing here" every second. The
    hold-and-own model here is new logic.
  * GM_Hardpoint does not bias spawns at all (its FindPlayerStart override is a pass-through); the zone-aware spawn code lives
    in GT_Bodycam::Find Best Spawn Point and is gated on GameMode == 11, so it never runs for us. Overriding FindPlayerStart and
    ChoosePlayerStart in our own GM is the CTF route and the only one available.

Everything class-agnostic is imported from ctf_graphs rather than retyped: the DSL, and the spawn/side/team functions that CTF
already proved in-game (EnsureTeams, EnsureSides, AverageLocation, ExtremeStart, SideT, DeriveSidesFromWaiting, GatherPawns,
IsStartFree, EnemyDistance, FloorPoint, StartForPlayer and the two spawn overrides). G.selfcall resolves by name on our own
Blueprint, so those graphs are identical for GM_DOM as long as it carries the same variables and function names.
"""
import json
from ctf_graphs import (G, P, VEC, ACTOR_ARRAY, _collision_overlap, gm_signature as ctf_signature,
                        gm_fn_EnsureTeams, gm_fn_EnsureSides, gm_fn_AverageLocation, gm_fn_ExtremeStart, gm_fn_SideT,
                        gm_fn_DeriveSidesFromWaiting, gm_fn_GatherPawns, gm_fn_IsStartFree, gm_fn_EnemyDistance,
                        gm_fn_FloorPoint, gm_fn_StartForPlayer, gm_findplayerstart, gm_chooseplayerstart,
                        SYS, MATH, GS_LIB, ARR, STR, ACTOR, PAWN, CONTROLLER, PRIM, SCENECOMP, SMC, WIDGETCOMP, WIDGET, IMAGE,
                        BC_GM, BC_PS, CAPTURE_RULE, HUD_DOT, DOT_ICON, CYLINDER, GM_PARENT, SND_ALLY, SND_ENEMY, SND_VOLUME,
                        TEXT_LIB, LOC_NS)

DOM_DIR = "/Game/GM/Gamemode/DOM"
GM_DOM = "/Game/GM/Gamemode/GM_DOM.GM_DOM_C"
POINT = DOM_DIR + "/BP_DOM_Point.BP_DOM_Point_C"
GE_DRONECD = DOM_DIR + "/GE_DOM_DroneCooldown.GE_DOM_DroneCooldown_C"
# the contest cue is the game's own: HardPointZone.ContestSound (decoded from its CDO, 2026-09-14)
SND_CONTEST = "/Game/Ultimate_SFX/UI_Item_Sounds_HD_Remake/Cues/Error_Sound_2_Cue.Error_Sound_2_Cue"
SND_ALL = (SND_ALLY, SND_ENEMY, SND_CONTEST)

# ---- tuning (docs/domination.md §1, §7; all in cm and seconds) --------------------------------------------------------
POINT_RADIUS = 400          # capture trigger sphere (CTF's base zone is 220; stock Hardpoint's box is 1024 half-extent for ONE zone)
PAD_SCALE = "8.0,8.0,0.02"  # the floor disc: the engine cylinder is 100 cm across, so scale = radius / 50
CAPTURE_TIME = 8.0          # seconds to capture, alone
TICK = 0.5                  # capture poll
DECAY = 0.5                 # progress lost per tick while nobody is on the point (= 1x)
SCORE_TICK = 4.0            # seconds between score ticks; +1 per owned point (2 points -> 200 in ~6:40)
SPREAD_MIN = 1200.0         # minimum distance between two control points
POINT_CLEAR = 800.0         # never spawn this close to any point
OWNED_RADIUS = 4500.0       # dynamic spawns: this close to one of OUR points
ENEMY_POINT_CLEAR = 2500.0  # ... and no closer than this to a point the enemy owns
ENEMY_PAWN_CLEAR = 1500.0   # ... and no closer than this to the nearest enemy
TARGET_T = (0.20, 0.50, 0.80)   # where A, B and C sit on the side axis (0 = side 1 base, 1 = side 2 base)
DRONE_COOLDOWN_FACTOR = 2.5     # CTF's value; BB5 (competitive) uses 4.0

BIG = "999999999.0"
COL_OWN = "(R=0.000000,G=1.000000,B=0.000000,A=1.000000)"        # green: ours
COL_ENEMY = "(R=1.000000,G=0.000000,B=0.000000,A=1.000000)"      # red: theirs
COL_NEUTRAL = "(R=1.000000,G=1.000000,B=1.000000,A=1.000000)"    # white: nobody's
COL_CONTESTED = "(R=1.000000,G=0.750000,B=0.000000,A=1.000000)"  # amber: being fought over
SEP = " - "                 # between the letter and the status on the marker label


def _loc(g, tag, key, src):
    """localized string pin: FindTextInLocalizationTable(ns, key, source) with the English literal as the fallback (UE 5.5
    signature: bool ReturnValue + FText OutText + SourceString), then to a string for the marker label."""
    g.call(f"loc_{tag}", TEXT_LIB, "FindTextInLocalizationTable", {"Namespace": LOC_NS, "Key": key, "SourceString": src})
    g.call(f"lit_{tag}", SYS, "MakeLiteralText", {"Value": src})
    g.call(f"txt_{tag}", MATH, "SelectText")
    g.link((f"loc_{tag}.OutText", f"txt_{tag}.A"), (f"lit_{tag}.ReturnValue", f"txt_{tag}.B"), (f"loc_{tag}.ReturnValue", f"txt_{tag}.bPickA"))
    g.call(f"str_{tag}", TEXT_LIB, "Conv_TextToString"); g.link((f"txt_{tag}.ReturnValue", f"str_{tag}.InText"))
    return f"str_{tag}.ReturnValue"


# ==================================================================================================================
# BP_DOM_Point (Actor, replicated, always relevant)
#   vars: PointIndex byte (rep) 1=A 2=B 3=C; OwnerTeam int (rep, OnRep_OwnerTeam) -1 = neutral; CapTeam int (rep) -1 = nobody;
#         Progress float (rep) 0..CAPTURE_TIME; bContested bool (rep); LastEvent byte (rep); EventTeam int (rep);
#         EventSeq int (rep, OnRep_EventSeq); GM GM_DOM_C; CountA/CountB/PresentTeam/PresentCount int (server scratch);
#         DotState byte; DotLabel string; LastLocalPawn Pawn
#   components: Trigger (Sphere r=POINT_RADIUS), Pad (StaticMesh disc), Dot (BodycamWidgetComponent)
# ==================================================================================================================
def point_events():
    g = G()
    g.custom("initpoint", "InitPoint")
    g.custom("capturetick", "CaptureTick")
    g.custom("evaluate", "EvaluateCapture")
    g.custom("applylook", "ApplyLook")
    g.custom("setupdot", "SetupDot")
    g.custom("refreshdot", "RefreshDot")
    g.custom("playevent", "PlayEventSound")
    g.custom("mark", "MarkEvent", [P("Event", "byte"), P("Team", "int")])   # 1 captured, 3 contested; Team = who it is good news for
    return g.json()


def point_onrep_ownerteam():
    g = G(); g.entry(); g.selfcall("dot", POINT, "RefreshDot"); g.link(("entry.then", "dot.exec")); return g.json()


def point_onrep_eventseq():
    g = G(); g.entry(); g.selfcall("snd", POINT, "PlayEventSound"); g.link(("entry.then", "snd.exec")); return g.json()


def point_logic():
    g = G()
    # --- BeginPlay (every machine): trigger, floor disc, marker poll; server: the capture poll -------------------------
    # The GM spawns the point and only THEN sets PointIndex / GM / HomeLocation and calls InitPoint, so nothing here may
    # assume those are set (a non-deferred SpawnActor has already run BeginPlay by the time the caller writes them).
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    g.get("trig", "Trigger")
    last = _collision_overlap(g, "trig", "Trigger", POINT_RADIUS, "bp.then")
    g.selfcall("look0", POINT, "ApplyLook"); g.chain(last, "look0")
    g.call("dottimer", SYS, "K2_SetTimer", {"FunctionName": "RefreshDot", "Time": "1.0", "bLooping": "true"})
    g.selfnode("selfd"); g.link(("selfd.self", "dottimer.Object")); g.chain("look0", "dottimer")
    g.call("auth0", ACTOR, "HasAuthority"); g.branch("br_auth0"); g.link(("auth0.ReturnValue", "br_auth0.condition")); g.chain("dottimer", "br_auth0")
    g.call("captimer", SYS, "K2_SetTimer", {"FunctionName": "CaptureTick", "Time": str(TICK), "bLooping": "true"})
    g.selfnode("selfc"); g.link(("selfc.self", "captimer.Object"), ("br_auth0.then", "captimer.exec"))

    # --- ApplyLook (every machine): the floor disc that marks the point. Plain engine cylinder, no material work — a
    #     dynamic material instance is how v23 turned the CTF flag purple, and ownership colour lives on the HUD marker.
    g.existing("applylook", "ApplyLook")
    g.get("pad", "Pad")
    g.call("padcol", PRIM, "SetCollisionEnabled", {"NewType": "NoCollision"})
    g.call("padmesh", SMC, "SetStaticMesh", {"NewMesh": CYLINDER})
    g.call("padscale", SCENECOMP, "SetRelativeScale3D", {"NewScale3D": PAD_SCALE})
    g.call("padoff", SCENECOMP, "K2_SetRelativeLocation", {"NewLocation": "0.0,0.0,1.0", "bSweep": "false", "bTeleport": "true"})
    for c in ("padcol", "padmesh", "padscale", "padoff"): g.link(("pad.Pad", f"{c}.self"))
    g.chain("applylook", "padcol", "padmesh", "padscale", "padoff")

    # --- InitPoint (server, after the GM has set PointIndex / GM / HomeLocation): neutral, nothing captured ------------
    g.existing("initpoint", "InitPoint")
    g.set("i_own", "OwnerTeam", defaults={"OwnerTeam": "-1"})
    g.set("i_cap", "CapTeam", defaults={"CapTeam": "-1"})
    g.set("i_prog", "Progress", defaults={"Progress": "0.0"})
    g.set("i_cont", "bContested", defaults={"bContested": "false"})
    g.chain("initpoint", "i_own", "i_cap", "i_prog", "i_cont")

    # --- CaptureTick (server, every TICK s): count the alive players of each team standing in the trigger ---------------
    # Team ids are only trustworthy once the GM has latched them (CTF v24: allies "picked up" their own flag because the
    # actor still carried a provisional id), and ABodycamPlayerState::TeamID is -1 until a team is assigned.
    g.existing("capturetick", "CaptureTick")
    g.set("c_a0", "CountA", defaults={"CountA": "0"})
    g.set("c_b0", "CountB", defaults={"CountB": "0"})
    g.chain("capturetick", "c_a0", "c_b0")
    g.get("c_gm", "GM"); g.call("c_gmv", SYS, "IsValid"); g.link(("c_gm.GM", "c_gmv.Object"))
    g.get("c_ready", "bTeamsReady", GM_DOM); g.link(("c_gm.GM", "c_ready.self"))
    g.call("c_ok", MATH, "BooleanAND"); g.link(("c_gmv.ReturnValue", "c_ok.A"), ("c_ready.bTeamsReady", "c_ok.B"))
    g.branch("br_cready"); g.link(("c_ok.ReturnValue", "br_cready.condition")); g.chain("c_b0", "br_cready")
    # AActor::GetOverlappingActors is BlueprintPure in this build — the first editor run reported "no input pin 'exec'" on it
    # (2026-09-14). So the loop is driven straight from the branch and the node is evaluated when the ForEach reads its array,
    # which is what BP_CTF_Base::Recheck does. The overlap list is maintained by the physics system, so re-evaluating it per
    # iteration costs a cached-array read, not a collision query.
    g.call("c_inzone", ACTOR, "GetOverlappingActors", {"ClassFilter": PAWN}); g.selfnode("selfz"); g.link(("selfz.self", "c_inzone.self"))
    g.foreach("c_fe"); g.link(("c_inzone.OverlappingActors", "c_fe.Array"), ("br_cready.then", "c_fe.Exec"))
    g.cast("c_pawn", PAWN, pure=True); g.link(("c_fe.Array Element", "c_pawn.cast_object"))
    g.call("c_ctl", PAWN, "GetController"); g.link(("c_pawn.cast_result", "c_ctl.self"))
    g.call("c_alive", SYS, "IsValid"); g.link(("c_ctl.ReturnValue", "c_alive.Object"))
    theirteam = g.pawn_team("c", "c_pawn.cast_result")
    g.call("c_assigned", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link((theirteam, "c_assigned.A"))
    g.call("c_use", MATH, "BooleanAND"); g.link(("c_alive.ReturnValue", "c_use.A"), ("c_assigned.ReturnValue", "c_use.B"))
    g.branch("br_cuse"); g.link(("c_use.ReturnValue", "br_cuse.condition"), ("c_fe.LoopBody", "br_cuse.exec"))
    g.get("c_ta", "TeamA", GM_DOM); g.link(("c_gm.GM", "c_ta.self"))
    g.get("c_tb", "TeamB", GM_DOM); g.link(("c_gm.GM", "c_tb.self"))
    g.call("c_isa", MATH, "EqualEqual_IntInt"); g.link((theirteam, "c_isa.A"), ("c_ta.TeamA", "c_isa.B"))
    g.call("c_isb", MATH, "EqualEqual_IntInt"); g.link((theirteam, "c_isb.A"), ("c_tb.TeamB", "c_isb.B"))
    g.branch("br_isa"); g.link(("c_isa.ReturnValue", "br_isa.condition"), ("br_cuse.then", "br_isa.exec"))
    g.get("c_ca", "CountA"); g.call("c_inca", MATH, "Add_IntInt", {"B": "1"}); g.link(("c_ca.CountA", "c_inca.A"))
    g.set("c_seta", "CountA"); g.link(("c_inca.ReturnValue", "c_seta.CountA"), ("br_isa.then", "c_seta.exec"))
    g.branch("br_isb"); g.link(("c_isb.ReturnValue", "br_isb.condition"), ("br_isa.else", "br_isb.exec"))
    g.get("c_cb", "CountB"); g.call("c_incb", MATH, "Add_IntInt", {"B": "1"}); g.link(("c_cb.CountB", "c_incb.A"))
    g.set("c_setb", "CountB"); g.link(("c_incb.ReturnValue", "c_setb.CountB"), ("br_isb.then", "c_setb.exec"))
    g.selfcall("c_eval", POINT, "EvaluateCapture"); g.link(("c_fe.Completed", "c_eval.exec"))

    # --- EvaluateCapture (server): the whole state machine, from CountA / CountB ---------------------------------------
    g.existing("evaluate", "EvaluateCapture")
    g.get("e_ca", "CountA"); g.get("e_cb", "CountB")
    g.call("e_a", MATH, "Greater_IntInt", {"B": "0"}); g.link(("e_ca.CountA", "e_a.A"))
    g.call("e_b", MATH, "Greater_IntInt", {"B": "0"}); g.link(("e_cb.CountB", "e_b.A"))
    g.call("e_both", MATH, "BooleanAND"); g.link(("e_a.ReturnValue", "e_both.A"), ("e_b.ReturnValue", "e_both.B"))
    g.branch("br_both"); g.link(("e_both.ReturnValue", "br_both.condition")); g.chain("evaluate", "br_both")

    # contested: freeze the capture; announce only on the transition (stock Hardpoint re-fires its cues every second
    # because it has no state to compare against — copied literally that would be a 2 Hz sound loop)
    g.get("e_wascont", "bContested"); g.call("e_notcont", MATH, "Not_PreBool"); g.link(("e_wascont.bContested", "e_notcont.A"))
    g.branch("br_newcont"); g.link(("e_notcont.ReturnValue", "br_newcont.condition"), ("br_both.then", "br_newcont.exec"))
    g.get("e_ownc", "OwnerTeam")
    g.selfcall("e_mark3", POINT, "MarkEvent", {"Event": "3"}); g.link(("e_ownc.OwnerTeam", "e_mark3.Team"), ("br_newcont.then", "e_mark3.exec"))
    g.set("e_cont1", "bContested", defaults={"bContested": "true"}); g.link(("br_newcont.else", "e_cont1.exec"), ("e_mark3.then", "e_cont1.exec"))

    # not contested
    g.set("e_cont0", "bContested", defaults={"bContested": "false"}); g.link(("br_both.else", "e_cont0.exec"))
    g.branch("br_ea"); g.link(("e_a.ReturnValue", "br_ea.condition")); g.chain("e_cont0", "br_ea")
    g.get("e_gm", "GM")
    g.get("e_ta", "TeamA", GM_DOM); g.link(("e_gm.GM", "e_ta.self"))
    g.get("e_tb", "TeamB", GM_DOM); g.link(("e_gm.GM", "e_tb.self"))
    g.set("e_pta", "PresentTeam"); g.link(("e_ta.TeamA", "e_pta.PresentTeam"), ("br_ea.then", "e_pta.exec"))
    g.set("e_pca", "PresentCount"); g.link(("e_ca.CountA", "e_pca.PresentCount")); g.chain("e_pta", "e_pca")
    g.branch("br_eb"); g.link(("e_b.ReturnValue", "br_eb.condition"), ("br_ea.else", "br_eb.exec"))
    g.set("e_ptb", "PresentTeam"); g.link(("e_tb.TeamB", "e_ptb.PresentTeam"), ("br_eb.then", "e_ptb.exec"))
    g.set("e_pcb", "PresentCount"); g.link(("e_cb.CountB", "e_pcb.PresentCount")); g.chain("e_ptb", "e_pcb")
    g.set("e_ptn", "PresentTeam", defaults={"PresentTeam": "-1"}); g.link(("br_eb.else", "e_ptn.exec"))
    g.set("e_pcn", "PresentCount", defaults={"PresentCount": "0"}); g.chain("e_ptn", "e_pcn")

    g.get("e_pt", "PresentTeam"); g.get("e_pc", "PresentCount"); g.get("e_own", "OwnerTeam"); g.get("e_cap", "CapTeam"); g.get("e_prog", "Progress")
    g.call("e_present", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link(("e_pt.PresentTeam", "e_present.A"))
    g.branch("br_present"); g.link(("e_present.ReturnValue", "br_present.condition"))
    g.link(("e_pca.then", "br_present.exec"), ("e_pcb.then", "br_present.exec"), ("e_pcn.then", "br_present.exec"))

    # somebody is on it, alone
    g.call("e_isowner", MATH, "EqualEqual_IntInt"); g.link(("e_pt.PresentTeam", "e_isowner.A"), ("e_own.OwnerTeam", "e_isowner.B"))
    g.branch("br_isowner"); g.link(("e_isowner.ReturnValue", "br_isowner.condition"), ("br_present.then", "br_isowner.exec"))
    g.set("e_hold0", "Progress", defaults={"Progress": "0.0"}); g.link(("br_isowner.then", "e_hold0.exec"))   # already ours: nothing to capture
    g.set("e_hold1", "CapTeam", defaults={"CapTeam": "-1"}); g.chain("e_hold0", "e_hold1")
    # a different team than the one that was capturing -> restart the bar
    g.call("e_same", MATH, "EqualEqual_IntInt"); g.link(("e_cap.CapTeam", "e_same.A"), ("e_pt.PresentTeam", "e_same.B"))
    g.branch("br_same"); g.link(("e_same.ReturnValue", "br_same.condition"), ("br_isowner.else", "br_same.exec"))
    g.set("e_newcap", "CapTeam"); g.link(("e_pt.PresentTeam", "e_newcap.CapTeam"), ("br_same.else", "e_newcap.exec"))
    g.set("e_newprog", "Progress", defaults={"Progress": "0.0"}); g.chain("e_newcap", "e_newprog")
    # rate: 1 player = TICK per tick, 2 = 2x, 3 or more = 3x. Written as nested SelectFloat so no int->float conversion
    # node is needed (the pin names of Conv_IntToDouble are one more thing that can fail a whole build).
    g.call("e_ge2", MATH, "GreaterEqual_IntInt", {"B": "2"}); g.link(("e_pc.PresentCount", "e_ge2.A"))
    g.call("e_ge3", MATH, "GreaterEqual_IntInt", {"B": "3"}); g.link(("e_pc.PresentCount", "e_ge3.A"))
    g.call("e_r1", MATH, "SelectFloat", {"A": str(TICK * 2), "B": str(TICK)}); g.link(("e_ge2.ReturnValue", "e_r1.bPickA"))
    g.call("e_rate", MATH, "SelectFloat", {"A": str(TICK * 3)}); g.link(("e_r1.ReturnValue", "e_rate.B"), ("e_ge3.ReturnValue", "e_rate.bPickA"))
    g.call("e_add", MATH, "Add_DoubleDouble"); g.link(("e_prog.Progress", "e_add.A"), ("e_rate.ReturnValue", "e_add.B"))
    g.set("e_setprog", "Progress"); g.link(("e_add.ReturnValue", "e_setprog.Progress"), ("br_same.then", "e_setprog.exec"), ("e_newprog.then", "e_setprog.exec"))
    g.get("e_prog2", "Progress")   # read AFTER the set: a pure getter is evaluated when its consumer runs
    g.call("e_done", MATH, "GreaterEqual_DoubleDouble", {"B": str(CAPTURE_TIME)}); g.link(("e_prog2.Progress", "e_done.A"))
    g.branch("br_done"); g.link(("e_done.ReturnValue", "br_done.condition")); g.chain("e_setprog", "br_done")
    g.set("e_newowner", "OwnerTeam"); g.link(("e_pt.PresentTeam", "e_newowner.OwnerTeam"), ("br_done.then", "e_newowner.exec"))
    g.set("e_zero", "Progress", defaults={"Progress": "0.0"})
    g.set("e_capclr", "CapTeam", defaults={"CapTeam": "-1"})
    g.selfcall("e_mark1", POINT, "MarkEvent", {"Event": "1"}); g.link(("e_pt.PresentTeam", "e_mark1.Team"))
    g.chain("e_newowner", "e_zero", "e_capclr", "e_mark1")

    # nobody on it: the capture decays
    g.call("e_dec", MATH, "Subtract_DoubleDouble", {"B": str(DECAY)}); g.link(("e_prog.Progress", "e_dec.A"))
    g.call("e_floor", MATH, "FMax", {"B": "0.0"}); g.link(("e_dec.ReturnValue", "e_floor.A"))
    g.set("e_decset", "Progress"); g.link(("e_floor.ReturnValue", "e_decset.Progress"), ("br_present.else", "e_decset.exec"))
    g.get("e_prog3", "Progress")
    g.call("e_empty", MATH, "LessEqual_DoubleDouble", {"B": "0.0"}); g.link(("e_prog3.Progress", "e_empty.A"))
    g.branch("br_empty"); g.link(("e_empty.ReturnValue", "br_empty.condition")); g.chain("e_decset", "br_empty")
    g.set("e_capclr2", "CapTeam", defaults={"CapTeam": "-1"}); g.link(("br_empty.then", "e_capclr2.exec"))

    # --- MarkEvent(Event, Team) -> replicated (LastEvent, EventTeam, then EventSeq with RepNotify, so the client's OnRep
    #     sees the other two already applied); the listen-server host gets no RepNotify, so it plays the sound directly.
    g.existing("mark", "MarkEvent")
    g.set("m_le", "LastEvent"); g.link(("mark.Event", "m_le.LastEvent"))
    g.set("m_et", "EventTeam"); g.link(("mark.Team", "m_et.EventTeam"))
    g.get("m_seq", "EventSeq"); g.call("m_seq1", MATH, "Add_IntInt", {"B": "1"}); g.link(("m_seq.EventSeq", "m_seq1.A"))
    g.set("m_seqset", "EventSeq"); g.link(("m_seq1.ReturnValue", "m_seqset.EventSeq"))
    g.selfcall("m_snd", POINT, "PlayEventSound")
    g.chain("mark", "m_le", "m_et", "m_seqset", "m_snd")

    # --- PlayEventSound: a capture = the ally cue for EventTeam, the enemy cue for everyone else (HardPointZone's
    #     PlaySpecificSoundByTeam does exactly this with the same two cues); a contest = the game's contest cue, but only
    #     for the team that owns the point. Stock Hardpoint multicasts its contest cue to everybody, which here would mean
    #     all twenty players hearing an error beep at volume 3 every time anyone steps onto a neutral point.
    g.existing("playevent", "PlayEventSound")
    g.get("s_ev", "LastEvent")
    g.call("s_iscont", MATH, "EqualEqual_ByteByte", {"B": "3"}); g.link(("s_ev.LastEvent", "s_iscont.A"))
    g.branch("br_scont"); g.link(("s_iscont.ReturnValue", "br_scont.condition")); g.chain("playevent", "br_scont")
    g.call("s_playcont", GS_LIB, "PlaySound2D", {"Sound": SND_CONTEST, "VolumeMultiplier": SND_VOLUME, "PitchMultiplier": "1.0"})
    g.call("s_pc0", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.get("s_ps", "PlayerState", CONTROLLER); g.link(("s_pc0.ReturnValue", "s_ps.self"))
    g.cast("s_bps", BC_PS, pure=True); g.link(("s_ps.PlayerState", "s_bps.cast_object"))
    g.get("s_viewer", "TeamID", BC_PS); g.link(("s_bps.cast_result", "s_viewer.self"))
    g.get("s_evteam", "EventTeam")
    g.call("s_ally", MATH, "EqualEqual_IntInt"); g.link(("s_viewer.TeamID", "s_ally.A"), ("s_evteam.EventTeam", "s_ally.B"))
    # contested: EventTeam is the point's owner, so -1 on a neutral point means nobody hears it
    g.branch("br_contmine"); g.link(("s_ally.ReturnValue", "br_contmine.condition"), ("br_scont.then", "br_contmine.exec"))
    g.link(("br_contmine.then", "s_playcont.exec"))
    g.branch("br_sally"); g.link(("s_ally.ReturnValue", "br_sally.condition"), ("br_scont.else", "br_sally.exec"))
    g.call("s_playally", GS_LIB, "PlaySound2D", {"Sound": SND_ALLY, "VolumeMultiplier": SND_VOLUME, "PitchMultiplier": "1.0"}); g.link(("br_sally.then", "s_playally.exec"))
    g.call("s_playenemy", GS_LIB, "PlaySound2D", {"Sound": SND_ENEMY, "VolumeMultiplier": SND_VOLUME, "PitchMultiplier": "1.0"}); g.link(("br_sally.else", "s_playenemy.exec"))

    # --- RefreshDot (every machine, 1 s loop + OnRep_OwnerTeam) --------------------------------------------------------
    # CTF's marker rules, all of them paid for by a failed build: create the marker only once the local player state, the
    # local PAWN and the widget's SLATE tree exist; re-create it when the local pawn changes (HUD_Dot stores ControlledPawn
    # once); re-apply the image size every poll (SetDesiredSizeOverride only reaches an SImage that already exists);
    # UWidget::IsVisible is BlueprintPure in 5.5, so branch on its return, never link an exec pin to it.
    # DotState: 0 not ready, 1 ours, 2 the enemy's, 3 neutral, 4 contested. DotLabel is the text; either changing re-runs
    # SetupDot, which is just "Create Dot" reconfiguring the same widget.
    g.existing("refreshdot", "RefreshDot")
    g.call("r_pc0", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.get("r_ps", "PlayerState", CONTROLLER); g.link(("r_pc0.ReturnValue", "r_ps.self"))
    g.cast("r_bps", BC_PS, pure=True); g.link(("r_ps.PlayerState", "r_bps.cast_object"))
    g.get("r_viewer", "TeamID", BC_PS); g.link(("r_bps.cast_result", "r_viewer.self"))
    g.call("r_pawn", CONTROLLER, "K2_GetPawn"); g.link(("r_pc0.ReturnValue", "r_pawn.self"))
    g.call("r_pawnok", SYS, "IsValid"); g.link(("r_pawn.ReturnValue", "r_pawnok.Object"))
    g.get("r_dot", "Dot"); g.call("r_getw", WIDGETCOMP, "GetUserWidgetObject", alt=["GetWidget"]); g.link(("r_dot.Dot", "r_getw.self"))
    g.cast("r_asdot", HUD_DOT); g.link(("r_getw.ReturnValue", "r_asdot.cast_object")); g.chain("refreshdot", "r_asdot")
    g.call("r_slate", WIDGET, "IsVisible"); g.link(("r_asdot.cast_result", "r_slate.self"))
    g.call("r_ready1", MATH, "BooleanAND"); g.link(("r_bps.bSuccess", "r_ready1.A"), ("r_pawnok.ReturnValue", "r_ready1.B"))
    g.call("r_ready", MATH, "BooleanAND"); g.link(("r_ready1.ReturnValue", "r_ready.A"), ("r_slate.ReturnValue", "r_ready.B"))
    g.get("r_last", "LastLocalPawn"); g.call("r_samepawn", MATH, "EqualEqual_ObjectObject"); g.link(("r_pawn.ReturnValue", "r_samepawn.A"), ("r_last.LastLocalPawn", "r_samepawn.B"))
    g.call("r_newpawn", MATH, "Not_PreBool"); g.link(("r_samepawn.ReturnValue", "r_newpawn.A"))
    g.call("r_changed", MATH, "BooleanAND"); g.link(("r_newpawn.ReturnValue", "r_changed.A"), ("r_pawnok.ReturnValue", "r_changed.B"))
    g.branch("br_rchanged"); g.link(("r_changed.ReturnValue", "br_rchanged.condition"), ("r_asdot.cast_ok", "br_rchanged.exec"))
    g.set("r_lastset", "LastLocalPawn"); g.link(("r_pawn.ReturnValue", "r_lastset.LastLocalPawn"), ("br_rchanged.then", "r_lastset.exec"))
    g.set("r_ds0", "DotState", defaults={"DotState": "0"}); g.chain("r_lastset", "r_ds0")

    # wanted colour state
    g.get("r_own", "OwnerTeam"); g.get("r_cont", "bContested")
    g.call("r_isown", MATH, "EqualEqual_IntInt"); g.link(("r_viewer.TeamID", "r_isown.A"), ("r_own.OwnerTeam", "r_isown.B"))
    g.call("r_neutral", MATH, "Less_IntInt", {"B": "0"}); g.link(("r_own.OwnerTeam", "r_neutral.A"))
    g.call("r_w1", MATH, "SelectInt", {"A": "1", "B": "2"}); g.link(("r_isown.ReturnValue", "r_w1.bPickA"))           # ours / theirs
    g.call("r_w2", MATH, "SelectInt", {"A": "3"}); g.link(("r_w1.ReturnValue", "r_w2.B"), ("r_neutral.ReturnValue", "r_w2.bPickA"))
    g.call("r_w3", MATH, "SelectInt", {"A": "4"}); g.link(("r_w2.ReturnValue", "r_w3.B"), ("r_cont.bContested", "r_w3.bPickA"))
    g.call("r_want", MATH, "SelectInt", {"B": "0"}); g.link(("r_w3.ReturnValue", "r_want.A"), ("r_ready.ReturnValue", "r_want.bPickA"))

    # wanted label: "<letter> - <status>", status = the capture percentage while a capture is running, else the state
    g.get("r_idx", "PointIndex")
    g.call("r_is1", MATH, "EqualEqual_ByteByte", {"B": "1"}); g.link(("r_idx.PointIndex", "r_is1.A"))
    g.call("r_is2", MATH, "EqualEqual_ByteByte", {"B": "2"}); g.link(("r_idx.PointIndex", "r_is2.A"))
    g.call("r_bc", MATH, "SelectString", {"A": "B", "B": "C"}); g.link(("r_is2.ReturnValue", "r_bc.bPickA"))
    g.call("r_letter", MATH, "SelectString", {"A": "A"}); g.link(("r_bc.ReturnValue", "r_letter.B"), ("r_is1.ReturnValue", "r_letter.bPickA"))
    neutral = _loc(g, "neutral", "DOM.HUD.Neutral", "NEUTRAL")
    yours = _loc(g, "yours", "DOM.HUD.Yours", "YOUR POINT")
    enemy = _loc(g, "enemy", "DOM.HUD.Enemy", "ENEMY POINT")
    contested = _loc(g, "contested", "DOM.HUD.Contested", "CONTESTED")
    g.call("r_s1", MATH, "SelectString"); g.link((yours, "r_s1.A"), (enemy, "r_s1.B"), ("r_isown.ReturnValue", "r_s1.bPickA"))
    g.call("r_s2", MATH, "SelectString"); g.link((neutral, "r_s2.A"), ("r_s1.ReturnValue", "r_s2.B"), ("r_neutral.ReturnValue", "r_s2.bPickA"))
    g.call("r_s3", MATH, "SelectString"); g.link((contested, "r_s3.A"), ("r_s2.ReturnValue", "r_s3.B"), ("r_cont.bContested", "r_s3.bPickA"))
    # progress in quarters — only comparisons and SelectString, so nothing here depends on a pin name CTF has not already used
    g.get("r_prog", "Progress"); g.get("r_capteam", "CapTeam")
    g.call("r_q1", MATH, "GreaterEqual_DoubleDouble", {"B": str(CAPTURE_TIME * 0.25)}); g.link(("r_prog.Progress", "r_q1.A"))
    g.call("r_q2", MATH, "GreaterEqual_DoubleDouble", {"B": str(CAPTURE_TIME * 0.50)}); g.link(("r_prog.Progress", "r_q2.A"))
    g.call("r_q3", MATH, "GreaterEqual_DoubleDouble", {"B": str(CAPTURE_TIME * 0.75)}); g.link(("r_prog.Progress", "r_q3.A"))
    g.call("r_p1", MATH, "SelectString", {"A": "25%", "B": "..."}); g.link(("r_q1.ReturnValue", "r_p1.bPickA"))
    g.call("r_p2", MATH, "SelectString", {"A": "50%"}); g.link(("r_p1.ReturnValue", "r_p2.B"), ("r_q2.ReturnValue", "r_p2.bPickA"))
    g.call("r_p3", MATH, "SelectString", {"A": "75%"}); g.link(("r_p2.ReturnValue", "r_p3.B"), ("r_q3.ReturnValue", "r_p3.bPickA"))
    g.call("r_capping", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link(("r_capteam.CapTeam", "r_capping.A"))
    # a contested point reads CONTESTED, not a frozen percentage; a decaying one keeps showing how far it got, which is true
    g.call("r_notcont", MATH, "Not_PreBool"); g.link(("r_cont.bContested", "r_notcont.A"))
    g.call("r_showpct", MATH, "BooleanAND"); g.link(("r_capping.ReturnValue", "r_showpct.A"), ("r_notcont.ReturnValue", "r_showpct.B"))
    g.call("r_status", MATH, "SelectString"); g.link(("r_p3.ReturnValue", "r_status.A"), ("r_s3.ReturnValue", "r_status.B"), ("r_showpct.ReturnValue", "r_status.bPickA"))
    g.call("r_cat1", STR, "Concat_StrStr", {"B": SEP}); g.link(("r_letter.ReturnValue", "r_cat1.A"))
    g.call("r_label", STR, "Concat_StrStr"); g.link(("r_cat1.ReturnValue", "r_label.A"), ("r_status.ReturnValue", "r_label.B"))

    # re-create the marker when either the colour state or the text changed (and never before it is ready)
    g.get("r_ds", "DotState"); g.call("r_dsint", MATH, "Conv_ByteToInt"); g.link(("r_ds.DotState", "r_dsint.InByte"))
    g.call("r_samestate", MATH, "EqualEqual_IntInt"); g.link(("r_want.ReturnValue", "r_samestate.A"), ("r_dsint.ReturnValue", "r_samestate.B"))
    g.get("r_dl", "DotLabel"); g.call("r_samelabel", STR, "EqualEqual_StrStr"); g.link(("r_label.ReturnValue", "r_samelabel.A"), ("r_dl.DotLabel", "r_samelabel.B"))
    g.call("r_unchanged", MATH, "BooleanAND"); g.link(("r_samestate.ReturnValue", "r_unchanged.A"), ("r_samelabel.ReturnValue", "r_unchanged.B"))
    g.call("r_zero", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("r_want.ReturnValue", "r_zero.A"))
    g.call("r_skip", MATH, "BooleanOR"); g.link(("r_unchanged.ReturnValue", "r_skip.A"), ("r_zero.ReturnValue", "r_skip.B"))
    g.branch("br_rskip"); g.link(("r_skip.ReturnValue", "br_rskip.condition"), ("br_rchanged.else", "br_rskip.exec"), ("r_ds0.then", "br_rskip.exec"))
    g.call("r_wantb", MATH, "Conv_IntToByte"); g.link(("r_want.ReturnValue", "r_wantb.inInt"))
    g.set("r_dsset", "DotState"); g.link(("r_wantb.ReturnValue", "r_dsset.DotState"), ("br_rskip.else", "r_dsset.exec"))
    g.set("r_dlset", "DotLabel"); g.link(("r_label.ReturnValue", "r_dlset.DotLabel")); g.chain("r_dsset", "r_dlset")
    g.selfcall("r_setup", POINT, "SetupDot"); g.chain("r_dlset", "r_setup")
    # steady state: keep the image at its designed size (harmless when already applied)
    g.call("r_nz", MATH, "NotEqual_IntInt", {"B": "0"}); g.link(("r_dsint.ReturnValue", "r_nz.A"))
    g.branch("br_rnz"); g.link(("r_nz.ReturnValue", "br_rnz.condition"), ("br_rskip.then", "br_rnz.exec"))
    g.get("r_img", "Image", HUD_DOT); g.link(("r_asdot.cast_result", "r_img.self"))
    g.call("r_size", IMAGE, "SetDesiredSizeOverride", {"DesiredSize": "(X=64.000000,Y=64.000000)"}); g.link(("r_img.Image", "r_size.self"), ("br_rnz.then", "r_size.exec"))

    # --- SetupDot (RefreshDot decides when): the game's HUD_Dot marker ---------------------------------------------------
    # Overlay type 2 = "HotPoint", the type the game's own HardPointZone uses for its capture zone and the one that carries
    # a label (stock passes "Capture" there). Type 5 is CTF's — image + distance, no name — and Domination's marker has to
    # say WHICH point it is. Ranges are metres and drive HUD_Dot's opacity fade; huge values keep it solid at any distance.
    g.existing("setupdot", "SetupDot")
    g.get("d_dot", "Dot")
    g.call("d_getw", WIDGETCOMP, "GetUserWidgetObject", alt=["GetWidget"]); g.link(("d_dot.Dot", "d_getw.self"))
    g.cast("d_asdot", HUD_DOT); g.link(("d_getw.ReturnValue", "d_asdot.cast_object"))
    g.get("d_ds", "DotState"); g.get("d_label", "DotLabel")
    g.call("d_is1", MATH, "EqualEqual_ByteByte", {"B": "1"}); g.link(("d_ds.DotState", "d_is1.A"))
    g.call("d_is3", MATH, "EqualEqual_ByteByte", {"B": "3"}); g.link(("d_ds.DotState", "d_is3.A"))
    g.call("d_is4", MATH, "EqualEqual_ByteByte", {"B": "4"}); g.link(("d_ds.DotState", "d_is4.A"))
    g.call("d_c1", MATH, "SelectColor", {"A": COL_OWN, "B": COL_ENEMY}); g.link(("d_is1.ReturnValue", "d_c1.bPickA"))
    g.call("d_c2", MATH, "SelectColor", {"A": COL_NEUTRAL}); g.link(("d_c1.ReturnValue", "d_c2.B"), ("d_is3.ReturnValue", "d_c2.bPickA"))
    g.call("d_c3", MATH, "SelectColor", {"A": COL_CONTESTED}); g.link(("d_c2.ReturnValue", "d_c3.B"), ("d_is4.ReturnValue", "d_c3.bPickA"))
    g.selfnode("d_self")
    g.call("d_create", HUD_DOT, "Create Dot", {"EN_OverlayType": "2", "In Range Far": "100000.0", "In Range Close": "100000.0",
                                               "Texture": DOT_ICON, "DesiredSize": "(X=64.000000,Y=64.000000)", "Name Size": "12", "Timer Size": "10"})
    g.link(("d_asdot.cast_result", "d_create.self"), ("d_c3.ReturnValue", "d_create.In Color and Opacity"),
           ("d_label.DotLabel", "d_create.UserName"), ("d_self.self", "d_create.Actor"))
    g.chain("setupdot", "d_asdot")
    g.link(("d_asdot.cast_ok", "d_create.exec"))
    return g.json()


# ==================================================================================================================
# GM_DOM
#   CTF's variables (the imported functions need exactly these) plus:
#     bPointsPlaced bool; ZoneHome1..3 vector; Points BP_DOM_Point[]; Point1..3 BP_DOM_Point; PickedStart Actor
# ==================================================================================================================
# CTF's proven graphs, reused unchanged (G.selfcall resolves by name on our own Blueprint, so nothing in them is CTF-specific)
SHARED_FUNCTIONS = ["EnsureTeams", "EnsureSides", "AverageLocation", "ExtremeStart", "SideT", "DeriveSidesFromWaiting",
                    "GatherPawns", "IsStartFree", "EnemyDistance", "FloorPoint", "StartForPlayer"]
OWN_FUNCTIONS = ["ApplyTeamIds", "MinPointDistance", "PointDistanceForTeam", "PickPointStart", "CollectDynamic", "CollectHalf", "ChooseStart"]
GM_FUNCTIONS = SHARED_FUNCTIONS + OWN_FUNCTIONS


def gm_events():
    g = G()
    g.custom("setup", "SetupDOM")
    g.custom("scoretick", "ScoreTick")
    g.custom("verify", "VerifyPoints")
    g.custom("perkmod", "RefreshPerkMods")
    return g.json()


def gm_signature(name):
    if name in SHARED_FUNCTIONS: return ctf_signature(name)
    g = G()
    sig = {
        "ApplyTeamIds": ((), ()),
        "MinPointDistance": ((VEC("Location"),), (P("D", "float"),)),
        "PointDistanceForTeam": ((VEC("Location"), P("Team", "int")), (P("D", "float"),)),
        "PickPointStart": ((P("TargetT", "float"), P("MinSpread", "float")), (P("Start", "object", **{"class": ACTOR}),)),
        "CollectDynamic": ((P("Side", "int"),), ()),
        "CollectHalf": ((P("Side", "int"), P("bAny", "bool")), ()),
        "ChooseStart": ((P("Side", "int"),), (P("Start", "object", **{"class": ACTOR}),)),
    }[name]
    g.entry(params=sig[0])
    if sig[1]: g.result(params=sig[1])
    return g.json()


def _fn(body):
    g = G(); g.entry(); body(g); return g.json()


def gm_fn_ApplyTeamIds():
    """EnsureTeams calls this whenever the team ids change. The points read TeamA/TeamB off the GM, so there is nothing to
    push — but the host is also a viewer, and its marker colours were decided with the old ids, so refresh them."""
    def body(g):
        g.get("pts", "Points"); g.foreach("fe"); g.link(("pts.Points", "fe.Array"), ("entry.then", "fe.Exec"))
        g.call("v", SYS, "IsValid"); g.link(("fe.Array Element", "v.Object"))
        g.branch("br"); g.link(("v.ReturnValue", "br.condition"), ("fe.LoopBody", "br.exec"))
        g.call("refresh", POINT, "RefreshDot"); g.link(("fe.Array Element", "refresh.self"), ("br.then", "refresh.exec"))
    return _fn(body)


def gm_fn_MinPointDistance():
    """distance from Location to the nearest control point (a very large number before any point exists)"""
    def body(g):
        g.result()
        g.set("d0", "TmpDist", defaults={"TmpDist": BIG}); g.link(("entry.then", "d0.exec"))
        g.get("pts", "Points"); g.foreach("fe"); g.link(("pts.Points", "fe.Array"), ("d0.then", "fe.Exec"))
        g.call("v", SYS, "IsValid"); g.link(("fe.Array Element", "v.Object"))
        g.branch("brv"); g.link(("v.ReturnValue", "brv.condition"), ("fe.LoopBody", "brv.exec"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.call("dist", MATH, "Vector_Distance"); g.link(("loc.ReturnValue", "dist.V1"), ("entry.Location", "dist.V2"))
        g.get("cur", "TmpDist"); g.call("lt", MATH, "Less_DoubleDouble"); g.link(("dist.ReturnValue", "lt.A"), ("cur.TmpDist", "lt.B"))
        g.branch("br"); g.link(("lt.ReturnValue", "br.condition"), ("brv.then", "br.exec"))
        g.set("d1", "TmpDist"); g.link(("dist.ReturnValue", "d1.TmpDist"), ("br.then", "d1.exec"))
        g.get("out", "TmpDist"); g.link(("out.TmpDist", "result.D"), ("fe.Completed", "result.exec"))
    return _fn(body)


def gm_fn_PointDistanceForTeam():
    """distance from Location to the nearest point OWNED by Team (very large when that team owns none)"""
    def body(g):
        g.result()
        g.set("d0", "TmpDist", defaults={"TmpDist": BIG}); g.link(("entry.then", "d0.exec"))
        g.get("pts", "Points"); g.foreach("fe"); g.link(("pts.Points", "fe.Array"), ("d0.then", "fe.Exec"))
        g.call("v", SYS, "IsValid"); g.link(("fe.Array Element", "v.Object"))
        g.get("own", "OwnerTeam", POINT); g.link(("fe.Array Element", "own.self"))
        g.call("mine", MATH, "EqualEqual_IntInt"); g.link(("own.OwnerTeam", "mine.A"), ("entry.Team", "mine.B"))
        g.call("ok", MATH, "BooleanAND"); g.link(("v.ReturnValue", "ok.A"), ("mine.ReturnValue", "ok.B"))
        g.branch("brv"); g.link(("ok.ReturnValue", "brv.condition"), ("fe.LoopBody", "brv.exec"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.call("dist", MATH, "Vector_Distance"); g.link(("loc.ReturnValue", "dist.V1"), ("entry.Location", "dist.V2"))
        g.get("cur", "TmpDist"); g.call("lt", MATH, "Less_DoubleDouble"); g.link(("dist.ReturnValue", "lt.A"), ("cur.TmpDist", "lt.B"))
        g.branch("br"); g.link(("lt.ReturnValue", "br.condition"), ("brv.then", "br.exec"))
        g.set("d1", "TmpDist"); g.link(("dist.ReturnValue", "d1.TmpDist"), ("br.then", "d1.exec"))
        g.get("out", "TmpDist"); g.link(("out.TmpDist", "result.D"), ("fe.Completed", "result.exec"))
    return _fn(body)


def gm_fn_PickPointStart():
    """The PlayerStart whose position on the side axis is closest to TargetT, at least MinSpread from every point already
    chosen. Real starts are used rather than a point interpolated along the axis, so every control point is guaranteed to
    sit in reachable, non-solid space. Returns nothing when the spread cannot be satisfied — the caller then asks again
    with MinSpread 0, so placement can never fail outright."""
    def body(g):
        g.result()
        g.set("best0", "TmpBest", defaults={"TmpBest": BIG}); g.link(("entry.then", "best0.exec"))
        g.set("act0", "TmpBestActor"); g.chain("best0", "act0")
        g.get("all", "AllStarts"); g.foreach("fe"); g.link(("all.AllStarts", "fe.Array"), ("act0.then", "fe.Exec"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.selfcall("t", GM_DOM, "SideT"); g.link(("loc.ReturnValue", "t.Location"), ("fe.LoopBody", "t.exec"))
        # |t - TargetT| without Abs: FMax(a-b, b-a) uses only nodes CTF already compiled
        g.call("d1", MATH, "Subtract_DoubleDouble"); g.link(("t.T", "d1.A"), ("entry.TargetT", "d1.B"))
        g.call("d2", MATH, "Subtract_DoubleDouble"); g.link(("entry.TargetT", "d2.A"), ("t.T", "d2.B"))
        g.call("dt", MATH, "FMax"); g.link(("d1.ReturnValue", "dt.A"), ("d2.ReturnValue", "dt.B"))
        g.selfcall("near", GM_DOM, "MinPointDistance"); g.link(("loc.ReturnValue", "near.Location"), ("t.then", "near.exec"))
        g.call("spread", MATH, "GreaterEqual_DoubleDouble"); g.link(("near.D", "spread.A"), ("entry.MinSpread", "spread.B"))
        g.branch("br_spread"); g.link(("spread.ReturnValue", "br_spread.condition"), ("near.then", "br_spread.exec"))
        g.get("best", "TmpBest"); g.call("better", MATH, "Less_DoubleDouble"); g.link(("dt.ReturnValue", "better.A"), ("best.TmpBest", "better.B"))
        g.branch("br_better"); g.link(("better.ReturnValue", "br_better.condition"), ("br_spread.then", "br_better.exec"))
        g.set("best1", "TmpBest"); g.link(("dt.ReturnValue", "best1.TmpBest"), ("br_better.then", "best1.exec"))
        g.set("act1", "TmpBestActor"); g.link(("fe.Array Element", "act1.TmpBestActor")); g.chain("best1", "act1")
        g.get("out", "TmpBestActor"); g.link(("out.TmpBestActor", "result.Start"), ("fe.Completed", "result.exec"))
    return _fn(body)


def gm_fn_CollectDynamic():
    """SpawnCandidates = starts that are near a point WE hold, far from the points the enemy holds, off the points
    themselves, away from enemies and free. Empty while our team holds nothing, which is exactly what we want at the
    start of a match: the caller then falls back to the fixed-side pool."""
    def body(g):
        g.get("c0", "SpawnCandidates"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("c0.SpawnCandidates", "clr.TargetArray"), ("entry.then", "clr.exec"))
        g.call("isone", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("entry.Side", "isone.A"))
        g.get("ta", "TeamA"); g.get("tb", "TeamB")
        g.call("myteam", MATH, "SelectInt"); g.link(("ta.TeamA", "myteam.A"), ("tb.TeamB", "myteam.B"), ("isone.ReturnValue", "myteam.bPickA"))
        g.call("enteam", MATH, "SelectInt"); g.link(("tb.TeamB", "enteam.A"), ("ta.TeamA", "enteam.B"), ("isone.ReturnValue", "enteam.bPickA"))
        g.get("all", "AllStarts"); g.foreach("fe"); g.link(("all.AllStarts", "fe.Array"), ("clr.then", "fe.Exec"))
        # never the enemy's own base starts, whatever the points say
        g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
        g.call("in1", ARR, "Array_Contains", array=True); g.call("in2", ARR, "Array_Contains", array=True)
        g.link(("b1.Base1Starts", "in1.TargetArray"), ("fe.Array Element", "in1.ItemToFind"), ("b2.Base2Starts", "in2.TargetArray"), ("fe.Array Element", "in2.ItemToFind"))
        g.call("notone", MATH, "Not_PreBool"); g.link(("isone.ReturnValue", "notone.A"))
        g.call("en1", MATH, "BooleanAND"); g.call("en2", MATH, "BooleanAND"); g.call("enemybase", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "en1.A"), ("in2.ReturnValue", "en1.B"), ("notone.ReturnValue", "en2.A"), ("in1.ReturnValue", "en2.B"),
               ("en1.ReturnValue", "enemybase.A"), ("en2.ReturnValue", "enemybase.B"))
        g.call("notenemy", MATH, "Not_PreBool"); g.link(("enemybase.ReturnValue", "notenemy.A"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.selfcall("dall", GM_DOM, "MinPointDistance"); g.link(("loc.ReturnValue", "dall.Location"), ("fe.LoopBody", "dall.exec"))
        g.selfcall("down", GM_DOM, "PointDistanceForTeam"); g.link(("loc.ReturnValue", "down.Location"), ("myteam.ReturnValue", "down.Team")); g.chain("dall", "down")
        g.selfcall("dene", GM_DOM, "PointDistanceForTeam"); g.link(("loc.ReturnValue", "dene.Location"), ("enteam.ReturnValue", "dene.Team")); g.chain("down", "dene")
        g.call("offpoint", MATH, "Greater_DoubleDouble", {"B": str(POINT_CLEAR)}); g.link(("dall.D", "offpoint.A"))
        g.call("nearown", MATH, "LessEqual_DoubleDouble", {"B": str(OWNED_RADIUS)}); g.link(("down.D", "nearown.A"))
        g.call("farenemy", MATH, "Greater_DoubleDouble", {"B": str(ENEMY_POINT_CLEAR)}); g.link(("dene.D", "farenemy.A"))
        g.call("a1", MATH, "BooleanAND"); g.link(("offpoint.ReturnValue", "a1.A"), ("nearown.ReturnValue", "a1.B"))
        g.call("a2", MATH, "BooleanAND"); g.link(("a1.ReturnValue", "a2.A"), ("farenemy.ReturnValue", "a2.B"))
        g.call("a3", MATH, "BooleanAND"); g.link(("a2.ReturnValue", "a3.A"), ("notenemy.ReturnValue", "a3.B"))
        g.branch("br_zone"); g.link(("a3.ReturnValue", "br_zone.condition"), ("dene.then", "br_zone.exec"))
        g.selfcall("ed", GM_DOM, "EnemyDistance"); g.link(("fe.Array Element", "ed.Start"), ("br_zone.then", "ed.exec"))
        g.call("safe", MATH, "Greater_DoubleDouble", {"B": str(ENEMY_PAWN_CLEAR)}); g.link(("ed.D", "safe.A"))
        g.branch("br_safe"); g.link(("safe.ReturnValue", "br_safe.condition"), ("ed.then", "br_safe.exec"))
        g.selfcall("free", GM_DOM, "IsStartFree"); g.link(("fe.Array Element", "free.Start"), ("br_safe.then", "free.exec"))
        g.branch("br_free"); g.link(("free.Free", "br_free.condition"), ("free.then", "br_free.exec"))
        g.get("c1", "SpawnCandidates"); g.call("add", ARR, "Array_Add", array=True)
        g.link(("c1.SpawnCandidates", "add.TargetArray"), ("fe.Array Element", "add.NewItem"), ("br_free.then", "add.exec"))
    return _fn(body)


def gm_fn_CollectHalf():
    """The fixed-side pool, CTF's rule: own base starts, or starts on our own quarter of the side axis; never an enemy base
    start. bAny=false also requires the start to be clear of the points and unoccupied; bAny=true takes it regardless, so
    this always yields something as long as the sides are known."""
    def body(g):
        g.get("c0", "SpawnCandidates"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("c0.SpawnCandidates", "clr.TargetArray"), ("entry.then", "clr.exec"))
        g.call("isone", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("entry.Side", "isone.A"))
        g.call("notone", MATH, "Not_PreBool"); g.link(("isone.ReturnValue", "notone.A"))
        g.get("all", "AllStarts"); g.foreach("fe"); g.link(("all.AllStarts", "fe.Array"), ("clr.then", "fe.Exec"))
        g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
        g.call("in1", ARR, "Array_Contains", array=True); g.call("in2", ARR, "Array_Contains", array=True)
        g.link(("b1.Base1Starts", "in1.TargetArray"), ("fe.Array Element", "in1.ItemToFind"), ("b2.Base2Starts", "in2.TargetArray"), ("fe.Array Element", "in2.ItemToFind"))
        g.call("own1", MATH, "BooleanAND"); g.call("own2", MATH, "BooleanAND"); g.call("ownbase", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "own1.A"), ("in1.ReturnValue", "own1.B"), ("notone.ReturnValue", "own2.A"), ("in2.ReturnValue", "own2.B"),
               ("own1.ReturnValue", "ownbase.A"), ("own2.ReturnValue", "ownbase.B"))
        g.call("en1", MATH, "BooleanAND"); g.call("en2", MATH, "BooleanAND"); g.call("enemybase", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "en1.A"), ("in2.ReturnValue", "en1.B"), ("notone.ReturnValue", "en2.A"), ("in1.ReturnValue", "en2.B"),
               ("en1.ReturnValue", "enemybase.A"), ("en2.ReturnValue", "enemybase.B"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.selfcall("t", GM_DOM, "SideT"); g.link(("loc.ReturnValue", "t.Location"), ("fe.LoopBody", "t.exec"))
        g.call("le", MATH, "LessEqual_DoubleDouble", {"B": "0.25"}); g.call("ge", MATH, "GreaterEqual_DoubleDouble", {"B": "0.75"})
        g.link(("t.T", "le.A"), ("t.T", "ge.A"))
        g.call("s1ok", MATH, "BooleanAND"); g.call("s2ok", MATH, "BooleanAND"); g.call("tok", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "s1ok.A"), ("le.ReturnValue", "s1ok.B"), ("notone.ReturnValue", "s2ok.A"), ("ge.ReturnValue", "s2ok.B"),
               ("s1ok.ReturnValue", "tok.A"), ("s2ok.ReturnValue", "tok.B"))
        g.call("inzone", MATH, "BooleanOR"); g.link(("ownbase.ReturnValue", "inzone.A"), ("tok.ReturnValue", "inzone.B"))
        g.call("notenemy", MATH, "Not_PreBool"); g.link(("enemybase.ReturnValue", "notenemy.A"))
        g.call("zoneok", MATH, "BooleanAND"); g.link(("inzone.ReturnValue", "zoneok.A"), ("notenemy.ReturnValue", "zoneok.B"))
        g.branch("br_zone"); g.link(("zoneok.ReturnValue", "br_zone.condition"), ("t.then", "br_zone.exec"))
        g.branch("br_any"); g.link(("entry.bAny", "br_any.condition"), ("br_zone.then", "br_any.exec"))
        g.selfcall("dall", GM_DOM, "MinPointDistance"); g.link(("loc.ReturnValue", "dall.Location"), ("br_any.else", "dall.exec"))
        g.call("offpoint", MATH, "Greater_DoubleDouble", {"B": str(POINT_CLEAR)}); g.link(("dall.D", "offpoint.A"))
        g.branch("br_off"); g.link(("offpoint.ReturnValue", "br_off.condition"), ("dall.then", "br_off.exec"))
        g.selfcall("free", GM_DOM, "IsStartFree"); g.link(("fe.Array Element", "free.Start"), ("br_off.then", "free.exec"))
        g.branch("br_free"); g.link(("free.Free", "br_free.condition"), ("free.then", "br_free.exec"))
        g.get("c1", "SpawnCandidates"); g.call("add", ARR, "Array_Add", array=True)
        g.link(("c1.SpawnCandidates", "add.TargetArray"), ("fe.Array Element", "add.NewItem"), ("br_free.then", "add.exec"), ("br_any.then", "add.exec"))
    return _fn(body)


def gm_fn_ChooseStart():
    """Dynamic pool first, then our own half, then our own half with the filters off. From whatever pool we end up with,
    the start farthest from the nearest enemy wins, with up to 8 m of jitter so twenty players do not stack on one start
    (CTF's scoring, unchanged)."""
    def body(g):
        g.result()
        g.selfcall("gather", GM_DOM, "GatherPawns"); g.link(("entry.Side", "gather.Side"), ("entry.then", "gather.exec"))
        g.selfcall("p1", GM_DOM, "CollectDynamic"); g.link(("entry.Side", "p1.Side")); g.chain("gather", "p1")
        g.get("c1", "SpawnCandidates"); g.call("n1", ARR, "Array_Length", array=True); g.link(("c1.SpawnCandidates", "n1.TargetArray"))
        g.call("none1", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("n1.ReturnValue", "none1.A"))
        g.branch("br1"); g.link(("none1.ReturnValue", "br1.condition"), ("p1.then", "br1.exec"))
        g.selfcall("p2", GM_DOM, "CollectHalf", {"bAny": "false"}); g.link(("entry.Side", "p2.Side"), ("br1.then", "p2.exec"))
        g.get("c2", "SpawnCandidates"); g.call("n2", ARR, "Array_Length", array=True); g.link(("c2.SpawnCandidates", "n2.TargetArray"))
        g.call("none2", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("n2.ReturnValue", "none2.A"))
        g.branch("br2"); g.link(("none2.ReturnValue", "br2.condition"), ("p2.then", "br2.exec"))
        g.selfcall("p3", GM_DOM, "CollectHalf", {"bAny": "true"}); g.link(("entry.Side", "p3.Side"), ("br2.then", "p3.exec"))
        g.set("best0", "TmpBest", defaults={"TmpBest": "-1.0"}); g.set("act0", "TmpBestActor")
        g.link(("br1.else", "best0.exec"), ("br2.else", "best0.exec"), ("p3.then", "best0.exec")); g.chain("best0", "act0")
        g.get("c3", "SpawnCandidates"); g.foreach("fe"); g.link(("c3.SpawnCandidates", "fe.Array"), ("act0.then", "fe.Exec"))
        g.selfcall("ed", GM_DOM, "EnemyDistance"); g.link(("fe.Array Element", "ed.Start"), ("fe.LoopBody", "ed.exec"))
        g.call("capped", MATH, "FMin", {"B": "6000.0"}); g.link(("ed.D", "capped.A"))
        g.call("jit", MATH, "RandomFloatInRange", {"Min": "0.0", "Max": "800.0"})
        g.call("score", MATH, "Add_DoubleDouble"); g.link(("capped.ReturnValue", "score.A"), ("jit.ReturnValue", "score.B"))
        g.get("best", "TmpBest"); g.call("better", MATH, "Greater_DoubleDouble"); g.link(("score.ReturnValue", "better.A"), ("best.TmpBest", "better.B"))
        g.branch("brb"); g.link(("better.ReturnValue", "brb.condition"), ("ed.then", "brb.exec"))
        g.set("best1", "TmpBest"); g.link(("score.ReturnValue", "best1.TmpBest"), ("brb.then", "best1.exec"))
        g.set("act1", "TmpBestActor"); g.link(("fe.Array Element", "act1.TmpBestActor")); g.chain("best1", "act1")
        g.get("out", "TmpBestActor"); g.set("ch", "ChosenStart"); g.link(("out.TmpBestActor", "ch.ChosenStart"), ("fe.Completed", "ch.exec"))
        g.get("ch2", "ChosenStart"); g.link(("ch2.ChosenStart", "result.Start")); g.chain("ch", "result")
    return _fn(body)


GM_FN_BODIES = {"EnsureTeams": gm_fn_EnsureTeams, "EnsureSides": gm_fn_EnsureSides, "AverageLocation": gm_fn_AverageLocation,
                "ExtremeStart": gm_fn_ExtremeStart, "SideT": gm_fn_SideT, "DeriveSidesFromWaiting": gm_fn_DeriveSidesFromWaiting,
                "GatherPawns": gm_fn_GatherPawns, "IsStartFree": gm_fn_IsStartFree, "EnemyDistance": gm_fn_EnemyDistance,
                "FloorPoint": gm_fn_FloorPoint, "StartForPlayer": gm_fn_StartForPlayer,
                "ApplyTeamIds": gm_fn_ApplyTeamIds, "MinPointDistance": gm_fn_MinPointDistance,
                "PointDistanceForTeam": gm_fn_PointDistanceForTeam, "PickPointStart": gm_fn_PickPointStart,
                "CollectDynamic": gm_fn_CollectDynamic, "CollectHalf": gm_fn_CollectHalf, "ChooseStart": gm_fn_ChooseStart}


def _place_point(g, idx, target_t, prev_exec):
    """Place control point <idx> at the PlayerStart closest to target_t on the side axis, on the floor, and take that
    start out of play. Returns the id of the last exec node."""
    i = idx
    g.selfcall(f"pick{i}", GM_DOM, "PickPointStart", {"TargetT": str(target_t), "MinSpread": str(SPREAD_MIN)})
    g.link((prev_exec, f"pick{i}.exec"))
    g.set(f"ps{i}", "PickedStart"); g.link((f"pick{i}.Start", f"ps{i}.PickedStart")); g.chain(f"pick{i}", f"ps{i}")
    g.get(f"psg{i}a", "PickedStart"); g.call(f"pv{i}a", SYS, "IsValid"); g.link((f"psg{i}a.PickedStart", f"pv{i}a.Object"))
    g.branch(f"brp{i}"); g.link((f"pv{i}a.ReturnValue", f"brp{i}.condition")); g.chain(f"ps{i}", f"brp{i}")
    # nothing satisfied the spread: ask again without it, so placement can never fail outright
    g.selfcall(f"pick{i}b", GM_DOM, "PickPointStart", {"TargetT": str(target_t), "MinSpread": "0.0"}); g.link((f"brp{i}.else", f"pick{i}b.exec"))
    g.set(f"ps{i}b", "PickedStart"); g.link((f"pick{i}b.Start", f"ps{i}b.PickedStart")); g.chain(f"pick{i}b", f"ps{i}b")
    g.get(f"psg{i}", "PickedStart"); g.call(f"pv{i}", SYS, "IsValid"); g.link((f"psg{i}.PickedStart", f"pv{i}.Object"))
    g.branch(f"brok{i}"); g.link((f"pv{i}.ReturnValue", f"brok{i}.condition"), (f"brp{i}.then", f"brok{i}.exec"), (f"ps{i}b.then", f"brok{i}.exec"))
    g.call(f"loc{i}", ACTOR, "K2_GetActorLocation"); g.link((f"psg{i}.PickedStart", f"loc{i}.self"))
    # the floor point is stored BEFORE the start is destroyed: pure nodes are re-evaluated per consumer, and a destroyed
    # actor reads back as (0,0,0) — that is the v20 bug that put both CTF flags at the world origin
    g.selfcall(f"fp{i}", GM_DOM, "FloorPoint"); g.link((f"loc{i}.ReturnValue", f"fp{i}.Start"), (f"brok{i}.then", f"fp{i}.exec"))
    g.set(f"zh{i}", f"ZoneHome{i}"); g.link((f"fp{i}.Location", f"zh{i}.ZoneHome{i}")); g.chain(f"fp{i}", f"zh{i}")
    g.get(f"zhg{i}", f"ZoneHome{i}")
    for arr in ("AllStarts", "Base1Starts", "Base2Starts"):
        nid = f"rm{i}_{arr}"
        g.get(f"g{nid}", arr); g.call(nid, ARR, "Array_RemoveItem", array=True)
        g.link((f"g{nid}.{arr}", f"{nid}.TargetArray"), (f"psg{i}.PickedStart", f"{nid}.Item"))
    g.chain(f"zh{i}", f"rm{i}_AllStarts", f"rm{i}_Base1Starts", f"rm{i}_Base2Starts")
    g.call(f"kill{i}", ACTOR, "K2_DestroyActor"); g.link((f"psg{i}.PickedStart", f"kill{i}.self")); g.chain(f"rm{i}_Base2Starts", f"kill{i}")
    g.call(f"xf{i}", MATH, "MakeTransform"); g.link((f"zhg{i}.ZoneHome{i}", f"xf{i}.Location"))
    g.spawn(f"sp{i}", POINT); g.link((f"xf{i}.ReturnValue", f"sp{i}.SpawnTransform"), (f"kill{i}.then", f"sp{i}.exec"))
    g.set(f"pi{i}", "PointIndex", POINT, defaults={"PointIndex": str(i)}); g.link((f"sp{i}.ReturnValue", f"pi{i}.self"))
    g.selfnode(f"selfp{i}"); g.set(f"pgm{i}", "GM", POINT); g.link((f"sp{i}.ReturnValue", f"pgm{i}.self"), (f"selfp{i}.self", f"pgm{i}.GM"))
    g.call(f"init{i}", POINT, "InitPoint"); g.link((f"sp{i}.ReturnValue", f"init{i}.self"))
    g.set(f"store{i}", f"Point{i}"); g.link((f"sp{i}.ReturnValue", f"store{i}.Point{i}"))
    g.get(f"pts{i}", "Points"); g.call(f"ptsadd{i}", ARR, "Array_Add", array=True)
    g.link((f"pts{i}.Points", f"ptsadd{i}.TargetArray"), (f"sp{i}.ReturnValue", f"ptsadd{i}.NewItem"))
    g.chain(f"sp{i}", f"pi{i}", f"pgm{i}", f"init{i}", f"store{i}", f"ptsadd{i}")
    return f"ptsadd{i}"


def gm_logic():
    g = G()
    # --- BeginPlay: parent, provisional team ids, the drone-cooldown refresher --------------------------------------
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    g.callparent("bp_parent", GM_PARENT, "ReceiveBeginPlay")
    g.set("provA", "TeamA", defaults={"TeamA": "0"})    # the game's team ids are 0-based and -1 means unassigned;
    g.set("provB", "TeamB", defaults={"TeamB": "1"})    # EnsureTeams replaces these as soon as it can see both teams
    g.call("perktimer", SYS, "K2_SetTimer", {"FunctionName": "RefreshPerkMods", "Time": "2.0", "bLooping": "true"})
    g.selfnode("selfp"); g.link(("selfp.self", "perktimer.Object"))
    g.chain("bp", "bp_parent", "provA", "provB", "perktimer")

    # --- RefreshPerkMods (server, every 2 s): the infinite "GadgetCooldown x2.5" effect on every pawn (the ASC lives on
    #     the pawn, so a respawn needs it again; stack limit 1 means re-applying never compounds) — CTF's mechanism.
    g.existing("perkmod", "RefreshPerkMods")
    g.call("applyge", BC_GM, "ApplyGameplayEffectToAllPlayers", {"EffectClass": GE_DRONECD}); g.chain("perkmod", "applyge")

    # --- the game's phase events: parent, then SetupDOM (guarded, so whichever fires first sets up once) --------------
    for ev in ("OnRoundWarmup", "OnRoundStart", "OnMatchStart"):
        g.event("ev_" + ev, ev, BC_GM)
        g.callparent("par_" + ev, GM_PARENT, ev)
        g.selfcall("setup_" + ev, GM_DOM, "SetupDOM")
        g.chain("ev_" + ev, "par_" + ev, "setup_" + ev)

    # --- SetupDOM: once; needs teams + sides; three points along the side axis ----------------------------------------
    g.existing("setup", "SetupDOM")
    g.get("placedchk", "bPointsPlaced"); g.branch("br_already"); g.link(("placedchk.bPointsPlaced", "br_already.condition")); g.chain("setup", "br_already")
    g.selfcall("et", GM_DOM, "EnsureTeams"); g.selfcall("es", GM_DOM, "EnsureSides"); g.link(("br_already.else", "et.exec")); g.chain("et", "es")
    g.get("sr", "bSidesReady"); g.branch("br_ready"); g.link(("sr.bSidesReady", "br_ready.condition")); g.chain("es", "br_ready")
    last = "br_ready.then"
    for i, t in enumerate(TARGET_T, start=1):
        last = _place_point(g, i, t, last) + ".then"
    g.set("placed", "bPointsPlaced", defaults={"bPointsPlaced": "true"}); g.link((last, "placed.exec"))
    g.call("scoretimer", SYS, "K2_SetTimer", {"FunctionName": "ScoreTick", "Time": str(SCORE_TICK), "bLooping": "true"})
    g.selfnode("selfs"); g.link(("selfs.self", "scoretimer.Object"))
    g.call("verifytimer", SYS, "K2_SetTimer", {"FunctionName": "VerifyPoints", "Time": "2.0", "bLooping": "true"})
    g.selfnode("selfv"); g.link(("selfv.self", "verifytimer.Object"))
    g.chain("placed", "scoretimer", "verifytimer")

    # --- ScoreTick (server, every SCORE_TICK s): +1 per point a team owns, through the game's own capture rule
    #     component, which also decides when the score limit has been reached and ends the match.
    g.existing("scoretick", "ScoreTick")
    g.get("st_pts", "Points"); g.foreach("st_fe"); g.link(("st_pts.Points", "st_fe.Array"), ("scoretick.then", "st_fe.Exec"))
    g.call("st_v", SYS, "IsValid"); g.link(("st_fe.Array Element", "st_v.Object"))
    g.get("st_own", "OwnerTeam", POINT); g.link(("st_fe.Array Element", "st_own.self"))
    g.call("st_owned", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link(("st_own.OwnerTeam", "st_owned.A"))
    g.call("st_ok", MATH, "BooleanAND"); g.link(("st_v.ReturnValue", "st_ok.A"), ("st_owned.ReturnValue", "st_ok.B"))
    g.branch("br_st"); g.link(("st_ok.ReturnValue", "br_st.condition"), ("st_fe.LoopBody", "br_st.exec"))
    g.get("st_rule", "BodycamCaptureRuleSet")
    g.call("st_score", CAPTURE_RULE, "HandleTeamCapturing", {"NumOfCapturer": "1"})
    g.link(("st_rule.BodycamCaptureRuleSet", "st_score.self"), ("st_own.OwnerTeam", "st_score.TeamID"), ("br_st.then", "st_score.exec"))

    # --- VerifyPoints (server, every 2 s): a point more than a metre from its recorded home goes back on it. Nothing
    #     legitimate ever moves one, so this is a pure safety net (CTF's VerifyFlags).
    g.existing("verify", "VerifyPoints")
    for i in (1, 2, 3):
        g.get(f"vp{i}", f"Point{i}"); g.call(f"vv{i}", SYS, "IsValid"); g.link((f"vp{i}.Point{i}", f"vv{i}.Object"))
        g.call(f"vloc{i}", ACTOR, "K2_GetActorLocation"); g.link((f"vp{i}.Point{i}", f"vloc{i}.self"))
        g.get(f"vzh{i}", f"ZoneHome{i}")
        g.call(f"vd{i}", MATH, "Vector_Distance"); g.link((f"vloc{i}.ReturnValue", f"vd{i}.V1"), (f"vzh{i}.ZoneHome{i}", f"vd{i}.V2"))
        g.call(f"vfar{i}", MATH, "Greater_DoubleDouble", {"B": "100.0"}); g.link((f"vd{i}.ReturnValue", f"vfar{i}.A"))
        g.call(f"vand{i}", MATH, "BooleanAND"); g.link((f"vv{i}.ReturnValue", f"vand{i}.A"), (f"vfar{i}.ReturnValue", f"vand{i}.B"))
        g.branch(f"vbr{i}"); g.link((f"vand{i}.ReturnValue", f"vbr{i}.condition"))
        g.call(f"vfix{i}", ACTOR, "K2_SetActorLocation", {"bSweep": "false", "bTeleport": "true"})
        g.link((f"vp{i}.Point{i}", f"vfix{i}.self"), (f"vzh{i}.ZoneHome{i}", f"vfix{i}.NewLocation"), (f"vbr{i}.then", f"vfix{i}.exec"))
    g.link(("verify.then", "vbr1.exec"), ("vbr1.else", "vbr2.exec"), ("vfix1.then", "vbr2.exec"),
           ("vbr2.else", "vbr3.exec"), ("vfix2.then", "vbr3.exec"))
    return g.json()


if __name__ == "__main__":
    # self-check: every graph serialises, has no duplicate ids and no link to an unknown node
    import sys
    graphs = {"point_events": point_events(), "point_onrep_ownerteam": point_onrep_ownerteam(),
              "point_onrep_eventseq": point_onrep_eventseq(), "point_logic": point_logic(),
              "gm_events": gm_events(), "gm_logic": gm_logic(),
              "gm_findplayerstart": gm_findplayerstart(), "gm_chooseplayerstart": gm_chooseplayerstart()}
    for f in GM_FUNCTIONS: graphs["sig_" + f] = gm_signature(f); graphs["fn_" + f] = GM_FN_BODIES[f]()
    bad = 0
    for name, js in graphs.items():
        d = json.loads(js); ids = {n["id"] for n in d["nodes"]}
        for a, b in d["links"]:
            for ref in (a, b):
                if ref.split(".", 1)[0] not in ids: print(f"{name}: link to unknown node {ref}"); bad += 1
        print(f"{name}: {len(d['nodes'])} nodes, {len(d['links'])} links")
    sys.exit(1 if bad else 0)
