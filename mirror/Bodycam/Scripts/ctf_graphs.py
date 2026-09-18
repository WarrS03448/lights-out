"""ctf_graphs.py — node graphs for the CTF Blueprints, as data consumed by UBodycamMirrorTools.BuildGraph (C++).

Schema (one BuildGraph call = one graph):
  {"nodes": [ {"id": "...", "type": T, ...}, ... ], "links": [["nodeId.pin", "nodeId.pin"], ...]}
  node types: event{class,name} | customevent{name,params[{name,category,class|struct,array}]} | existing{name}
              call{class,func,array?} | callparent{class,func} | get/set{var,class?} | cast{class,pure?}
              branch | sequence{count} | foreach | makearray{count} | self | spawn{class} | break/make{struct}
              adddelegate{delegate,class?} | entry{params?} | result{params?}
  node "defaults": {"PinName": value}  (object/class pins take an asset path)
  pin aliases: exec, then, else, condition, self/target, return, cast_object, cast_result, cast_ok, cast_fail, in, out
Passes per Blueprint (make_blueprints.py): 1 variables/components, compile; 2 EVENTS (custom events + function
signatures), compile; 3 LOGIC (bodies), compile. Pure nodes (const/BlueprintPure) have no exec pins.

CTF rules (memory.md §5.2): touch enemy flag = pick up; capture = bring it to your own base while your own flag is home;
dropped flag returns when a teammate touches it, else after 30 s; sides are fixed; spawns = own base starts first, then
own-half starts no closer to mid than a quarter of the way to the enemy flag, never the enemy base.
"""
import json

# ---- class / asset paths ------------------------------------------------------------------------------------------
GM_PARENT = "/Game/GM/Gamemode/BP_BodycamGameModeAbstract.BP_BodycamGameModeAbstract_C"
GM_CTF = "/Game/GM/Gamemode/GM_CTF.GM_CTF_C"
FLAG = "/Game/GM/Gamemode/CTF/BP_CTF_Flag.BP_CTF_Flag_C"
BASE = "/Game/GM/Gamemode/CTF/BP_CTF_Base.BP_CTF_Base_C"
HUD_DOT = "/Game/MenuSystemPro/INGAME/HUD_Dot.HUD_Dot_C"
DOT_ICON = "/Game/MenuSystemPro/Textures/Menu/T_UI_Frame_Setting_Divider.T_UI_Frame_Setting_Divider"   # the plain frame outline HUD_ItemDetection (dropped bomb) and W_PlayerIndicator (allies) use
BC_GM = "/Script/Bodycam.BodycamGameMode"
BC_GS = "/Script/Bodycam.BodycamGameState"
BC_PS = "/Script/Bodycam.BodycamPlayerState"
CAPTURE_RULE = "/Script/Bodycam.BodycamCaptureRuleSetComponent"
GS_LIB = "/Script/Engine.GameplayStatics"
SYS = "/Script/Engine.KismetSystemLibrary"
MATH = "/Script/Engine.KismetMathLibrary"
TEXT_LIB = "/Script/Engine.KismetTextLibrary"
LOC_NS = "CommunityGamemodes"        # every in-game string of a community gamemode: (namespace, key) in gamemodes/<id>/loc.json,
                                      # merged into the player's Game.locres by the hub. Blueprints fetch them at runtime with
                                      # FindTextInLocalizationTable(ns, key, source) -> plain string pins, nothing for the
                                      # editor's text-key machinery to re-key. Keys: "<ID>.HUD.<Name>"; source = the English.
ARR = "/Script/Engine.KismetArrayLibrary"
STR = "/Script/Engine.KismetStringLibrary"
ACTOR = "/Script/Engine.Actor"
PAWN = "/Script/Engine.Pawn"
CONTROLLER = "/Script/Engine.Controller"
PLAYERSTART = "/Script/Engine.PlayerStart"
PRIM = "/Script/Engine.PrimitiveComponent"
SCENECOMP = "/Script/Engine.SceneComponent"
SPHERE = "/Script/Engine.SphereComponent"
SMC = "/Script/Engine.StaticMeshComponent"
WIDGETCOMP = "/Script/UMG.WidgetComponent"
WIDGET = "/Script/UMG.Widget"
IMAGE = "/Script/UMG.Image"
USERWIDGET = "/Script/UMG.UserWidget"
CYLINDER = "/Engine/BasicShapes/Cylinder.Cylinder"
FLAG_MESH = "/Game/MilitaryWarehouse/Meshes/SetDressing/SM_CaptureFlag04.SM_CaptureFlag04"
CONE = "/Engine/BasicShapes/Cone.Cone"
V_TEAMDATA = "/Script/Bodycam.BodycamTeamData"
WIDGET_MAT = "/Engine/EngineMaterials/Widget3DPassThrough_Translucent.Widget3DPassThrough_Translucent"   # translucent; TintColorAndOpacity (0,0,0,0) + OpacityFromTexture 0 -> renders nothing (v22: NaniteHiddenSectionMaterial is purple on a non-Nanite mesh; only Nanite "hides" it)
MID = "/Script/Engine.MaterialInstanceDynamic"
# Sam (2026-09-14): every flag event plays the Hardpoint zone sound, team-relative: the "ally in the hardpoint" cue when the acting
# team is the listener's, the "enemy in the hardpoint" cue otherwise. Decoded from HardPointZone: PlaySpecificSoundByTeam(EarnPointSound,
# LoosingSound, TeamCapturing) with EarnPointSound = Simple_Click_Sound_HardPoint_Cue and LoosingSound = ..._HardPointNoPlayers_Cue1.
SND_ALLY = "/Game/UI/Sfx/Cues/Simple_Click_Sound_HardPoint_Cue.Simple_Click_Sound_HardPoint_Cue"
SND_ENEMY = "/Game/UI/Sfx/Cues/Simple_Click_Sound_HardPointNoPlayers_Cue1.Simple_Click_Sound_HardPointNoPlayers_Cue1"
SND_ALL = (SND_ALLY, SND_ENEMY)
SND_VOLUME = "3.0"   # Sam: loud enough to be heard over gunfire
GE_DRONECD = "/Game/GM/Gamemode/CTF/GE_CTF_DroneCooldown.GE_CTF_DroneCooldown_C"   # GadgetCooldown x2.5 (infinite, stack limit 1)
MATINT = "/Script/Engine.MaterialInterface"
VECTOR = "/Script/CoreUObject.Vector"
NATIVE_BREAK = {"/Script/Engine.HitResult": "GameplayStatics.BreakHitResult"}   # see G.brk

# ---- tiny DSL --------------------------------------------------------------------------------------------------------
class G:
    def __init__(self): self.nodes = []; self.links = []
    def n(self, id, type, **kw):
        assert all(n["id"] != id for n in self.nodes), f"duplicate node id {id}"
        d = {"id": id, "type": type}; d.update(kw); self.nodes.append(d); return id
    def event(self, id, name, cls=None): return self.n(id, "event", name=name, **({"class": cls} if cls else {}))
    def custom(self, id, name, params=()): return self.n(id, "customevent", name=name, params=list(params))
    def existing(self, id, name): return self.n(id, "existing", name=name)
    def call(self, id, cls, func, defaults=None, array=False, alt=None):
        d = {"func": func}
        if cls: d["class"] = cls
        if defaults: d["defaults"] = defaults
        if array: d["array"] = True
        if alt: d["alt"] = list(alt)
        return self.n(id, "call", **d)
    def callparent(self, id, cls, func, defaults=None): return self.n(id, "callparent", **{"class": cls, "func": func, **({"defaults": defaults} if defaults else {})})
    def get(self, id, var, cls=None): return self.n(id, "get", var=var, **({"class": cls} if cls else {}))
    def set(self, id, var, cls=None, defaults=None): return self.n(id, "set", var=var, **({"class": cls} if cls else {}), **({"defaults": defaults} if defaults else {}))
    def cast(self, id, cls, pure=False): return self.n(id, "cast", **{"class": cls, "pure": pure})
    def branch(self, id): return self.n(id, "branch")
    def seq(self, id, count): return self.n(id, "sequence", count=count)
    def foreach(self, id): return self.n(id, "foreach")
    def spawn(self, id, cls): return self.n(id, "spawn", **{"class": cls}, defaults={"CollisionHandlingOverride": "AlwaysSpawn"})
    def brk(self, id, struct):
        # structs whose fields are not Blueprint-visible get NO output pins from the generic break node (the editor uses a native
        # "break" function for them instead); the link would fail and the compiler would silently use the pin default (v20 bug)
        assert struct not in NATIVE_BREAK, f"{id}: break of {struct} has no output pins; call {NATIVE_BREAK[struct]} instead"
        return self.n(id, "break", struct=struct)
    def selfnode(self, id): return self.n(id, "self")
    def adddelegate(self, id, delegate, cls=None): return self.n(id, "adddelegate", delegate=delegate, **({"class": cls} if cls else {}))
    def entry(self, id="entry", params=()): return self.n(id, "entry", params=list(params))
    def result(self, id="result", params=()): return self.n(id, "result", params=list(params))
    def link(self, *pairs):
        for a, b in pairs: self.links.append([a, b])
    def chain(self, *ids):
        """exec chain: a.then -> b.exec -> ..."""
        for a, b in zip(ids, ids[1:]): self.links.append([f"{a}.then", f"{b}.exec"])
    def json(self): return json.dumps({"nodes": self.nodes, "links": self.links})
    # -- small helpers used everywhere --
    def selfcall(self, id, cls, func, defaults=None):
        """call one of our own custom events / functions: no class -> the builder resolves it on the Blueprint's own (skeleton)
        class and the node's hidden self pin defaults to Self, exactly like a node placed in the editor"""
        return self.call(id, None, func, defaults)
    def pawn_team(self, prefix, pawn_pin):
        """pure chain: pawn -> player state -> BodycamPlayerState.TeamID ; returns (teamid_pin, cast_ok_condition_pin)"""
        self.get(f"{prefix}_ps", "PlayerState", PAWN); self.link((pawn_pin, f"{prefix}_ps.self"))       # APawn.PlayerState (the "Get Player State" node is this property)
        self.cast(f"{prefix}_bps", BC_PS, pure=True); self.link((f"{prefix}_ps.PlayerState", f"{prefix}_bps.cast_object"))
        self.get(f"{prefix}_team", "TeamID", BC_PS); self.link((f"{prefix}_bps.cast_result", f"{prefix}_team.self"))
        return f"{prefix}_team.TeamID"

P = lambda name, category, **kw: {"name": name, "category": category, **kw}
ACTOR_ARRAY = lambda name: P(name, "object", **{"class": ACTOR, "array": True})
VEC = lambda name: P(name, "struct", struct=VECTOR)

def _collision_overlap(g, comp_getter_id, comp_pin, radius, first_exec_from):
    """Trigger sphere: radius, query-only, overlap everything, generate overlap events. Returns the last node id."""
    g.call("radius", SPHERE, "SetSphereRadius", {"InSphereRadius": str(radius), "bUpdateOverlaps": "true"})
    g.call("colen", PRIM, "SetCollisionEnabled", {"NewType": "QueryOnly"})
    g.call("colresp", PRIM, "SetCollisionResponseToAllChannels", {"NewResponse": "ECR_Overlap"})
    g.call("colovl", PRIM, "SetGenerateOverlapEvents", {"bInGenerateOverlapEvents": "true"})
    g.link((first_exec_from, "radius.exec")); g.chain("radius", "colen", "colresp", "colovl")
    for c in ("radius", "colen", "colresp", "colovl"): g.link((f"{comp_getter_id}.{comp_pin}", f"{c}.self"))
    return "colovl"

# ==================================================================================================================
# BP_CTF_Flag (Actor, replicated)
#   vars: TeamID int (rep), Side byte (rep, OnRep_Side), FlagState byte (rep: 0 home, 1 carried, 2 dropped), Carrier Pawn (rep),
#         HomeLocation vector, GM object(GM_CTF_C)
#   components: Mesh (StaticMesh), Trigger (Sphere), Dot (BodycamWidgetComponent)
# ==================================================================================================================
def flag_events():
    g = G()
    g.custom("initflag", "InitFlag")
    g.custom("pickup", "PickUp", [P("NewCarrier", "object", **{"class": PAWN})])
    g.custom("dropat", "DropAt", [VEC("Location")])
    g.custom("returnhome", "ReturnHome")
    g.custom("setupdot", "SetupDot")
    g.custom("refreshdot", "RefreshDot")
    g.custom("applylook", "ApplyLook")
    g.custom("checkcarrier", "CheckCarrier")
    g.custom("carriedlook", "ApplyCarriedLook")
    g.custom("playevent", "PlayEventSound")
    g.custom("mark", "MarkEvent", [P("Event", "byte"), P("Team", "int")])   # Team = the team the event is good news for
    return g.json()

def flag_onrep_side():
    g = G()
    g.entry()
    g.selfcall("look", FLAG, "ApplyLook")
    g.link(("entry.then", "look.exec"))
    return g.json()

def flag_onrep_teamid():
    # the team ids can be corrected after the flag exists (solo lobby -> second team): redo the marker colour
    g = G(); g.entry(); g.selfcall("dot", FLAG, "RefreshDot"); g.link(("entry.then", "dot.exec")); return g.json()

def flag_onrep_flagstate():
    g = G(); g.entry(); g.selfcall("cl", FLAG, "ApplyCarriedLook"); g.link(("entry.then", "cl.exec")); return g.json()

def flag_onrep_eventseq():
    g = G(); g.entry(); g.selfcall("snd", FLAG, "PlayEventSound"); g.link(("entry.then", "snd.exec")); return g.json()

def flag_logic():
    g = G()
    # --- BeginPlay (all machines): trigger collision, mesh without collision, look, dot; server: carrier watchdog ---
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    g.get("trig", "Trigger")
    last = _collision_overlap(g, "trig", "Trigger", 150, "bp.then")
    g.get("mesh0", "Mesh"); g.call("meshcol", PRIM, "SetCollisionEnabled", {"NewType": "NoCollision"}); g.link(("mesh0.Mesh", "meshcol.self"))
    g.chain(last, "meshcol")
    g.selfcall("look0", FLAG, "ApplyLook"); g.chain("meshcol", "look0")
    g.call("dotdelay", SYS, "K2_SetTimer", {"FunctionName": "RefreshDot", "Time": "1.0", "bLooping": "true"})   # polls: the viewer's team and the flag's TeamID can both change after spawn
    g.selfnode("self0"); g.link(("self0.self", "dotdelay.Object")); g.chain("look0", "dotdelay")
    g.call("auth0", ACTOR, "HasAuthority"); g.branch("br_auth0"); g.link(("auth0.ReturnValue", "br_auth0.condition")); g.chain("dotdelay", "br_auth0")
    g.call("watch", SYS, "K2_SetTimer", {"FunctionName": "CheckCarrier", "Time": "1.0", "bLooping": "true"})
    g.selfnode("selfw"); g.link(("selfw.self", "watch.Object"), ("br_auth0.then", "watch.exec"))

    # --- ApplyLook (all machines; also from OnRep_Side): mesh by side (1 = cylinder, 2 = cone), size, offset ---
    g.existing("applylook", "ApplyLook")
    g.get("mesh", "Mesh")
    g.call("mesh1", SMC, "SetStaticMesh", {"NewMesh": FLAG_MESH}); g.link(("mesh.Mesh", "mesh1.self"))       # the game's own capture-flag prop
    g.call("scale", SCENECOMP, "SetRelativeScale3D", {"NewScale3D": "1.0,1.0,1.0"}); g.link(("mesh.Mesh", "scale.self"))
    g.call("moff", SCENECOMP, "K2_SetRelativeLocation", {"NewLocation": "0.0,0.0,0.0", "bSweep": "false", "bTeleport": "true"}); g.link(("mesh.Mesh", "moff.self"))   # the actor is placed on the floor (line trace in SetupCTF)
    g.call("smat", PRIM, "GetMaterial", {"ElementIndex": "0"}); g.link(("mesh.Mesh", "smat.self"))
    g.set("keepmat", "StaticMat"); g.link(("smat.ReturnValue", "keepmat.StaticMat"))
    g.selfcall("cl0", FLAG, "ApplyCarriedLook")
    g.chain("applylook", "mesh1", "scale", "moff", "keepmat", "cl0")

    # --- ApplyCarriedLook (all machines; also OnRep_FlagState): carried = pole + sandbags (material slot 0) hidden, only the cloth ---
    g.existing("carriedlook", "ApplyCarriedLook")
    g.get("cmesh", "Mesh"); g.get("cst", "FlagState"); g.call("cl_is1", MATH, "EqualEqual_ByteByte", {"B": "1"}); g.link(("cst.FlagState", "cl_is1.A"))
    g.branch("br_cl"); g.link(("cl_is1.ReturnValue", "br_cl.condition")); g.chain("carriedlook", "br_cl")
    g.call("hide0", PRIM, "CreateDynamicMaterialInstance", {"ElementIndex": "0", "SourceMaterial": WIDGET_MAT}); g.link(("cmesh.Mesh", "hide0.self"), ("br_cl.then", "hide0.exec"))
    g.call("hide0a", MID, "SetVectorParameterValue", {"ParameterName": "TintColorAndOpacity", "Value": "(R=0.000000,G=0.000000,B=0.000000,A=0.000000)"}); g.link(("hide0.ReturnValue", "hide0a.self"))
    g.call("hide0b", MID, "SetScalarParameterValue", {"ParameterName": "OpacityFromTexture", "Value": "0.0"}); g.link(("hide0.ReturnValue", "hide0b.self"))
    g.chain("hide0", "hide0a", "hide0b")
    g.get("smat2", "StaticMat"); g.call("show0", PRIM, "SetMaterial", {"ElementIndex": "0"}); g.link(("cmesh.Mesh", "show0.self"), ("smat2.StaticMat", "show0.Material"), ("br_cl.else", "show0.exec"))

    # --- MarkEvent(Event, Team): 1 pick-up, 2 drop, 3 return, 4 capture; Team = the team it is good news for -> replicated (LastEvent,
    #     EventTeam, then EventSeq with RepNotify -> the client's OnRep sees the other two already applied), sound on every machine ---
    g.existing("mark", "MarkEvent")
    g.set("le", "LastEvent"); g.link(("mark.Event", "le.LastEvent"))
    g.set("et", "EventTeam"); g.link(("mark.Team", "et.EventTeam"))
    g.get("seq", "EventSeq"); g.call("seq1", MATH, "Add_IntInt", {"B": "1"}); g.link(("seq.EventSeq", "seq1.A"))
    g.set("seqset", "EventSeq"); g.link(("seq1.ReturnValue", "seqset.EventSeq"))
    g.selfcall("snd0", FLAG, "PlayEventSound")      # the listen-server host gets no RepNotify
    g.chain("mark", "le", "et", "seqset", "snd0")

    # --- PlayEventSound: the listener's team == EventTeam -> "ally in the hardpoint" cue, else the "enemy in the hardpoint" cue ---
    g.existing("playevent", "PlayEventSound")
    g.call("spc0", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.get("spcps", "PlayerState", CONTROLLER); g.link(("spc0.ReturnValue", "spcps.self"))
    g.cast("sviewerps", BC_PS, pure=True); g.link(("spcps.PlayerState", "sviewerps.cast_object"))
    g.get("sviewerteam", "TeamID", BC_PS); g.link(("sviewerps.cast_result", "sviewerteam.self"))
    g.get("sevteam", "EventTeam")
    g.call("sally", MATH, "EqualEqual_IntInt"); g.link(("sviewerteam.TeamID", "sally.A"), ("sevteam.EventTeam", "sally.B"))
    g.branch("br_sally"); g.link(("sally.ReturnValue", "br_sally.condition")); g.chain("playevent", "br_sally")
    g.call("playally", GS_LIB, "PlaySound2D", {"Sound": SND_ALLY, "VolumeMultiplier": SND_VOLUME, "PitchMultiplier": "1.0"}); g.link(("br_sally.then", "playally.exec"))
    g.call("playenemy", GS_LIB, "PlaySound2D", {"Sound": SND_ENEMY, "VolumeMultiplier": SND_VOLUME, "PitchMultiplier": "1.0"}); g.link(("br_sally.else", "playenemy.exec"))

    # --- InitFlag (server, after the GM set TeamID/Side/HomeLocation): state home + look ---
    g.existing("init", "InitFlag")
    g.set("st0", "FlagState", defaults={"FlagState": "0"})
    g.selfcall("look1", FLAG, "ApplyLook")
    g.chain("init", "st0", "look1")

    # --- CheckCarrier (server, every second): carried but the carrier is gone -> drop where the flag is ---
    g.existing("chk", "CheckCarrier")
    g.get("cstate", "FlagState"); g.call("ciscar", MATH, "EqualEqual_ByteByte", {"B": "1"}); g.link(("cstate.FlagState", "ciscar.A"))
    g.get("ccar", "Carrier"); g.call("cvalid", SYS, "IsValid"); g.link(("ccar.Carrier", "cvalid.Object"))
    g.call("cnot", MATH, "Not_PreBool"); g.link(("cvalid.ReturnValue", "cnot.A"))
    g.call("cand", MATH, "BooleanAND"); g.link(("ciscar.ReturnValue", "cand.A"), ("cnot.ReturnValue", "cand.B"))
    g.branch("br_lost"); g.link(("cand.ReturnValue", "br_lost.condition")); g.chain("chk", "br_lost")
    g.call("cloc", ACTOR, "K2_GetActorLocation"); g.selfnode("selfc"); g.link(("selfc.self", "cloc.self"))
    g.selfcall("cdrop", FLAG, "DropAt"); g.link(("cloc.ReturnValue", "cdrop.Location"), ("br_lost.then", "cdrop.exec"))

    # --- overlap (server): alive pawn only; teammate returns a dropped flag; enemy picks up a flag that is not carried ---
    g.event("ovl", "ReceiveActorBeginOverlap", ACTOR)
    g.call("auth", ACTOR, "HasAuthority"); g.branch("br_auth"); g.link(("auth.ReturnValue", "br_auth.condition"))
    g.chain("ovl", "br_auth")
    g.cast("aspawn", PAWN); g.link(("ovl.OtherActor", "aspawn.cast_object"), ("br_auth.then", "aspawn.exec"))
    g.call("octl", PAWN, "GetController"); g.link(("aspawn.cast_result", "octl.self"))
    g.call("oalive", SYS, "IsValid"); g.link(("octl.ReturnValue", "oalive.Object"))
    theirteam = g.pawn_team("o", "aspawn.cast_result")
    # v24: off spawn, allies "picked up" their own flag: the flag still carried the provisional team id while the players had the real
    # one. Touches count only once the GM's ids are final (bTeamsReady) and the toucher has an assigned team (>= 0).
    g.get("ogm", "GM"); g.get("oready", "bTeamsReady", GM_CTF); g.link(("ogm.GM", "oready.self"))
    g.call("oassigned", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link((theirteam, "oassigned.A"))
    g.call("ook1", MATH, "BooleanAND"); g.link(("oalive.ReturnValue", "ook1.A"), ("oready.bTeamsReady", "ook1.B"))
    g.call("ook", MATH, "BooleanAND"); g.link(("ook1.ReturnValue", "ook.A"), ("oassigned.ReturnValue", "ook.B"))
    g.branch("br_alive"); g.link(("ook.ReturnValue", "br_alive.condition"), ("aspawn.cast_ok", "br_alive.exec"))
    g.get("myteam", "TeamID")
    g.call("sameteam", MATH, "EqualEqual_IntInt"); g.link((theirteam, "sameteam.A"), ("myteam.TeamID", "sameteam.B"))
    g.branch("br_team"); g.link(("sameteam.ReturnValue", "br_team.condition"), ("br_alive.then", "br_team.exec"))
    g.get("state1", "FlagState"); g.call("isdropped", MATH, "EqualEqual_ByteByte", {"B": "2"}); g.link(("state1.FlagState", "isdropped.A"))
    g.branch("br_dropped"); g.link(("isdropped.ReturnValue", "br_dropped.condition"), ("br_team.then", "br_dropped.exec"))
    g.selfcall("ret1", FLAG, "ReturnHome"); g.link(("br_dropped.then", "ret1.exec"))
    g.get("state2", "FlagState"); g.call("iscarried", MATH, "EqualEqual_ByteByte", {"B": "1"}); g.link(("state2.FlagState", "iscarried.A"))
    g.branch("br_carried"); g.link(("iscarried.ReturnValue", "br_carried.condition"), ("br_team.else", "br_carried.exec"))
    g.selfcall("pick1", FLAG, "PickUp"); g.link(("aspawn.cast_result", "pick1.NewCarrier"), ("br_carried.else", "pick1.exec"))

    # --- PickUp(NewCarrier): attach above the carrier's head ---
    g.existing("pickup", "PickUp")
    g.set("st1", "FlagState", defaults={"FlagState": "1"}); g.set("carrier", "Carrier"); g.link(("pickup.NewCarrier", "carrier.Carrier"))
    g.call("attach", ACTOR, "K2_AttachToActor", {"LocationRule": "SnapToTarget", "RotationRule": "SnapToTarget", "ScaleRule": "KeepWorld"})
    g.link(("pickup.NewCarrier", "attach.ParentActor"))
    g.call("rel", ACTOR, "K2_SetActorRelativeLocation", {"NewRelativeLocation": "0.0,-40.0,-90.0", "bSweep": "false", "bTeleport": "true"})
    g.call("cleartimer", SYS, "K2_ClearTimer", {"FunctionName": "ReturnHome"}); g.selfnode("self3"); g.link(("self3.self", "cleartimer.Object"))
    pteam = g.pawn_team("pk", "pickup.NewCarrier")   # pick-up is good news for the carrier's team
    g.selfcall("cl1", FLAG, "ApplyCarriedLook"); g.selfcall("ev1", FLAG, "MarkEvent", {"Event": "1"}); g.link((pteam, "ev1.Team"))
    g.chain("pickup", "st1", "carrier", "attach", "rel", "cleartimer", "cl1", "ev1")

    # --- DropAt(Location): detach, lie there, auto-return after 30 s ---
    g.existing("dropat", "DropAt")
    g.call("detach1", ACTOR, "K2_DetachFromActor", {"LocationRule": "KeepWorld", "RotationRule": "KeepWorld", "ScaleRule": "KeepWorld"})
    g.call("setloc1", ACTOR, "K2_SetActorLocation", {"bSweep": "false", "bTeleport": "true"}); g.link(("dropat.Location", "setloc1.NewLocation"))
    g.set("st2", "FlagState", defaults={"FlagState": "2"}); g.set("carrier0", "Carrier")
    g.call("rettimer", SYS, "K2_SetTimer", {"FunctionName": "ReturnHome", "Time": "30.0", "bLooping": "false"}); g.selfnode("self4"); g.link(("self4.self", "rettimer.Object"))
    g.get("dteam", "TeamID")   # a drop is good news for the flag's own team
    g.selfcall("cl2", FLAG, "ApplyCarriedLook"); g.selfcall("ev2", FLAG, "MarkEvent", {"Event": "2"}); g.link(("dteam.TeamID", "ev2.Team"))
    g.chain("dropat", "detach1", "setloc1", "st2", "carrier0", "rettimer", "cl2", "ev2")

    # --- ReturnHome: back on the stand, then tell the GM (a teammate may be waiting in the base with the enemy flag) ---
    g.existing("returnhome", "ReturnHome")
    # the acting team, decided while Carrier is still set: capture -> the carrier's team, plain return -> the flag's own team
    rteam = g.pawn_team("rt", "rcar.Carrier"); g.get("rcar", "Carrier"); g.get("rown", "TeamID"); g.get("rcap", "bCapturedNext")
    g.call("rpick", MATH, "SelectInt"); g.link((rteam, "rpick.A"), ("rown.TeamID", "rpick.B"), ("rcap.bCapturedNext", "rpick.bPickA"))
    g.set("evteam", "EventTeam"); g.link(("rpick.ReturnValue", "evteam.EventTeam"))
    g.call("detach2", ACTOR, "K2_DetachFromActor", {"LocationRule": "KeepWorld", "RotationRule": "KeepWorld", "ScaleRule": "KeepWorld"})
    g.get("home", "HomeLocation"); g.call("setloc2", ACTOR, "K2_SetActorLocation", {"bSweep": "false", "bTeleport": "true"}); g.link(("home.HomeLocation", "setloc2.NewLocation"))
    g.set("st3", "FlagState", defaults={"FlagState": "0"}); g.set("carrier1", "Carrier")
    g.call("cleartimer2", SYS, "K2_ClearTimer", {"FunctionName": "ReturnHome"}); g.selfnode("self5"); g.link(("self5.self", "cleartimer2.Object"))
    g.get("gm", "GM"); g.call("gmvalid", SYS, "IsValid"); g.link(("gm.GM", "gmvalid.Object"))
    g.branch("br_gm"); g.link(("gmvalid.ReturnValue", "br_gm.condition"))
    g.call("notify", GM_CTF, "OnFlagReturned"); g.selfnode("self7"); g.link(("gm.GM", "notify.self"), ("self7.self", "notify.Flag"))
    g.selfcall("cl3", FLAG, "ApplyCarriedLook")
    g.get("cap", "bCapturedNext"); g.call("evcode", MATH, "SelectInt", {"A": "4", "B": "3"}); g.link(("cap.bCapturedNext", "evcode.bPickA"))
    g.call("evbyte", MATH, "Conv_IntToByte"); g.link(("evcode.ReturnValue", "evbyte.inInt"))
    g.get("evteam2", "EventTeam")
    g.selfcall("ev3", FLAG, "MarkEvent"); g.link(("evbyte.ReturnValue", "ev3.Event"), ("evteam2.EventTeam", "ev3.Team"))
    g.set("capreset", "bCapturedNext", defaults={"bCapturedNext": "false"})
    g.chain("returnhome", "evteam", "detach2", "setloc2", "st3", "carrier1", "cleartimer2", "cl3", "ev3", "capreset", "br_gm"); g.link(("br_gm.then", "notify.exec"))

    # --- RefreshDot (every machine, 1 s loop + OnRep_TeamID) ---
    # The marker is (re)created only when everything it captures exists: the local player state (team), the local PAWN (HUD_Dot's
    # "Create Dot" stores ControlledPawn once and its distance/opacity timer is dead without it) and the widget's SLATE tree
    # (UWidget.IsVisible() is false until the screen layer has built it; UImage.SetDesiredSizeOverride only reaches an existing
    # SImage, so a marker created earlier keeps the brush's tiny native size = v22 Trenches). A new local pawn (respawn) re-creates
    # the marker so ControlledPawn is current, and the 64x64 size is re-applied every poll on top of that.
    # want = 0 (not ready) / 1 own (green) / 2 enemy (red) vs DotState; on change: SetupDot ("Create Dot" reconfigures the same widget).
    g.existing("refreshdot", "RefreshDot")
    g.call("rpc0", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.get("rpcps", "PlayerState", CONTROLLER); g.link(("rpc0.ReturnValue", "rpcps.self"))
    g.cast("rviewerps", BC_PS, pure=True); g.link(("rpcps.PlayerState", "rviewerps.cast_object"))
    g.get("rviewerteam", "TeamID", BC_PS); g.link(("rviewerps.cast_result", "rviewerteam.self"))
    g.call("rpawn", CONTROLLER, "K2_GetPawn"); g.link(("rpc0.ReturnValue", "rpawn.self"))
    g.call("rpawnok", SYS, "IsValid"); g.link(("rpawn.ReturnValue", "rpawnok.Object"))
    g.get("rdot", "Dot"); g.call("rgetw", WIDGETCOMP, "GetUserWidgetObject", alt=["GetWidget"]); g.link(("rdot.Dot", "rgetw.self"))
    g.cast("rasdot", HUD_DOT); g.link(("rgetw.ReturnValue", "rasdot.cast_object")); g.chain("refreshdot", "rasdot")
    g.call("rslate", WIDGET, "IsVisible"); g.link(("rasdot.cast_result", "rslate.self"))   # BlueprintPure in 5.5: no exec pins
    g.call("rready1", MATH, "BooleanAND"); g.link(("rviewerps.bSuccess", "rready1.A"), ("rpawnok.ReturnValue", "rready1.B"))
    g.call("rready", MATH, "BooleanAND"); g.link(("rready1.ReturnValue", "rready.A"), ("rslate.ReturnValue", "rready.B"))
    # a new local pawn -> forget the current marker (DotState 0) so it is created again with the current ControlledPawn
    g.get("rlast", "LastLocalPawn"); g.call("rsame_pawn", MATH, "EqualEqual_ObjectObject"); g.link(("rpawn.ReturnValue", "rsame_pawn.A"), ("rlast.LastLocalPawn", "rsame_pawn.B"))
    g.call("rnewpawn", MATH, "Not_PreBool"); g.link(("rsame_pawn.ReturnValue", "rnewpawn.A"))
    g.call("rchanged", MATH, "BooleanAND"); g.link(("rnewpawn.ReturnValue", "rchanged.A"), ("rpawnok.ReturnValue", "rchanged.B"))
    g.branch("br_rchanged"); g.link(("rchanged.ReturnValue", "br_rchanged.condition"), ("rasdot.cast_ok", "br_rchanged.exec"))
    g.set("rlastset", "LastLocalPawn"); g.link(("rpawn.ReturnValue", "rlastset.LastLocalPawn"), ("br_rchanged.then", "rlastset.exec"))
    g.set("rds0", "DotState", defaults={"DotState": "0"}); g.chain("rlastset", "rds0")
    # want vs DotState
    g.get("rmyteam", "TeamID")
    g.call("risown", MATH, "EqualEqual_IntInt"); g.link(("rviewerteam.TeamID", "risown.A"), ("rmyteam.TeamID", "risown.B"))
    g.call("rwant1", MATH, "SelectInt", {"A": "1", "B": "2"}); g.link(("risown.ReturnValue", "rwant1.bPickA"))
    g.call("rwant", MATH, "SelectInt", {"B": "0"}); g.link(("rwant1.ReturnValue", "rwant.A"), ("rready.ReturnValue", "rwant.bPickA"))
    g.get("rds", "DotState"); g.call("rdsint", MATH, "Conv_ByteToInt"); g.link(("rds.DotState", "rdsint.InByte"))
    g.call("rsame", MATH, "EqualEqual_IntInt"); g.link(("rwant.ReturnValue", "rsame.A"), ("rdsint.ReturnValue", "rsame.B"))
    g.call("rzero", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("rwant.ReturnValue", "rzero.A"))
    g.call("rskip", MATH, "BooleanOR"); g.link(("rsame.ReturnValue", "rskip.A"), ("rzero.ReturnValue", "rskip.B"))
    g.branch("br_rskip"); g.link(("rskip.ReturnValue", "br_rskip.condition"), ("br_rchanged.else", "br_rskip.exec"), ("rds0.then", "br_rskip.exec"))
    g.call("rwantb", MATH, "Conv_IntToByte"); g.link(("rwant.ReturnValue", "rwantb.inInt"))
    g.set("rdsset", "DotState"); g.link(("rwantb.ReturnValue", "rdsset.DotState"), ("br_rskip.else", "rdsset.exec"))
    g.selfcall("rsetup", FLAG, "SetupDot"); g.chain("rdsset", "rsetup")
    # steady state (marker exists): keep the image at its designed size every poll (harmless if already applied)
    g.call("rnz", MATH, "NotEqual_IntInt", {"B": "0"}); g.link(("rdsint.ReturnValue", "rnz.A"))
    g.branch("br_rnz"); g.link(("rnz.ReturnValue", "br_rnz.condition"), ("br_rskip.then", "br_rnz.exec"))
    g.get("rimg", "Image", HUD_DOT); g.link(("rasdot.cast_result", "rimg.self"))
    g.call("rsize", IMAGE, "SetDesiredSizeOverride", {"DesiredSize": "(X=64.000000,Y=64.000000)"}); g.link(("rimg.Image", "rsize.self"), ("br_rnz.then", "rsize.exec"))

    # --- SetupDot (RefreshDot decides when): the game's HUD_Dot marker, green "YOUR FLAG" / red "ENEMY FLAG" ---
    # Both markers use overlay type 5 (image + distance readout) — Sam liked the distance on the enemy marker (v24) and wants it on both.
    g.existing("setupdot", "SetupDot")
    g.get("dot", "Dot")
    g.call("getw", WIDGETCOMP, "GetUserWidgetObject", alt=["GetWidget"]); g.link(("dot.Dot", "getw.self"))
    g.cast("asdot", HUD_DOT); g.link(("getw.ReturnValue", "asdot.cast_object"))
    g.call("pc0", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.get("pcps", "PlayerState", CONTROLLER); g.link(("pc0.ReturnValue", "pcps.self"))
    g.cast("viewerps", BC_PS, pure=True); g.link(("pcps.PlayerState", "viewerps.cast_object"))
    g.get("viewerteam", "TeamID", BC_PS); g.link(("viewerps.cast_result", "viewerteam.self"))
    g.get("myteam2", "TeamID")
    g.call("isown", MATH, "EqualEqual_IntInt"); g.link(("viewerteam.TeamID", "isown.A"), ("myteam2.TeamID", "isown.B"))
    g.call("color", MATH, "SelectColor", {"A": "(R=0.000000,G=1.000000,B=0.000000,A=1.000000)", "B": "(R=1.000000,G=0.000000,B=0.000000,A=1.000000)"})
    # localized labels (loc.json CTF.HUD.YourFlag / CTF.HUD.EnemyFlag), looked up by key at runtime. UE 5.5 signature (checked in
    # KismetTextLibrary.h): bool FindTextInLocalizationTable(Namespace, Key, FText& OutText, SourceString) -> when the entry is
    # missing, ReturnValue is false and the English literal is used instead, so the marker never goes blank.
    for tag, key, src in (("your", "CTF.HUD.YourFlag", "YOUR FLAG"), ("enemy", "CTF.HUD.EnemyFlag", "ENEMY FLAG")):
        g.call(f"loc_{tag}", TEXT_LIB, "FindTextInLocalizationTable", {"Namespace": LOC_NS, "Key": key, "SourceString": src})
        g.call(f"lit_{tag}", SYS, "MakeLiteralText", {"Value": src})
        g.call(f"txt_{tag}", MATH, "SelectText")
        g.link((f"loc_{tag}.OutText", f"txt_{tag}.A"), (f"lit_{tag}.ReturnValue", f"txt_{tag}.B"), (f"loc_{tag}.ReturnValue", f"txt_{tag}.bPickA"))
    g.call("labeltxt", MATH, "SelectText"); g.link(("txt_your.ReturnValue", "labeltxt.A"), ("txt_enemy.ReturnValue", "labeltxt.B"))
    g.call("label", TEXT_LIB, "Conv_TextToString"); g.link(("labeltxt.ReturnValue", "label.InText"))
    g.link(("isown.ReturnValue", "color.bPickA"), ("isown.ReturnValue", "labeltxt.bPickA"))
    g.selfnode("self6")
    # overlay type 5 = "PointDistance": the frame + the distance in metres, no name/timer (EN_OverlayType: 0 Player, 1 Tips, 2 HotPoint,
    # 3 Lobby, 4 Point, 5 PointDistance, 6 Image). Ranges are metres and drive the opacity fade (MapRangeClamped inside HUD_Dot); huge = always solid.
    g.call("create", HUD_DOT, "Create Dot", {"EN_OverlayType": "5", "In Range Far": "100000.0", "In Range Close": "100000.0",
                                             "Texture": DOT_ICON, "DesiredSize": "(X=64.000000,Y=64.000000)", "Name Size": "12", "Timer Size": "10"})
    g.link(("asdot.cast_result", "create.self"), ("color.ReturnValue", "create.In Color and Opacity"), ("label.ReturnValue", "create.UserName"), ("self6.self", "create.Actor"))
    g.chain("setupdot", "asdot")
    g.link(("asdot.cast_ok", "create.exec"))
    return g.json()

# ==================================================================================================================
# BP_CTF_Base (Actor, replicated)  vars: TeamID int (rep), Side byte (rep), OwnFlag, EnemyFlag (Flag), GM (GM_CTF_C)
#   components: Trigger (Sphere r=220), Pad (StaticMesh: flat cylinder on the floor)
# ==================================================================================================================
def base_events():
    g = G()
    g.custom("try", "TryCapture", [P("Runner", "object", **{"class": PAWN})])
    g.custom("recheck", "Recheck")
    return g.json()

def base_logic():
    g = G()
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    g.get("trig", "Trigger")
    last = _collision_overlap(g, "trig", "Trigger", 220, "bp.then")
    g.get("pad", "Pad")
    g.call("padcol", PRIM, "SetCollisionEnabled", {"NewType": "NoCollision"})
    g.call("padmesh", SMC, "SetStaticMesh", {"NewMesh": CYLINDER})
    g.call("padscale", SCENECOMP, "SetRelativeScale3D", {"NewScale3D": "4.4,4.4,0.02"})
    g.call("padoff", SCENECOMP, "K2_SetRelativeLocation", {"NewLocation": "0.0,0.0,1.0", "bSweep": "false", "bTeleport": "true"})
    for c in ("padcol", "padmesh", "padscale", "padoff"): g.link(("pad.Pad", f"{c}.self"))
    g.chain(last, "padcol", "padmesh", "padscale", "padoff")

    g.event("ovl", "ReceiveActorBeginOverlap", ACTOR)
    g.call("auth", ACTOR, "HasAuthority"); g.branch("br_auth"); g.link(("auth.ReturnValue", "br_auth.condition"))
    g.chain("ovl", "br_auth")
    g.cast("aspawn", PAWN); g.link(("ovl.OtherActor", "aspawn.cast_object"), ("br_auth.then", "aspawn.exec"))
    g.selfcall("try0", BASE, "TryCapture"); g.link(("aspawn.cast_result", "try0.Runner"), ("aspawn.cast_ok", "try0.exec"))

    # --- TryCapture(Runner): own alive player, carrying the enemy flag, own flag at home -> score + return the enemy flag ---
    g.existing("try", "TryCapture")
    g.call("rvalid", SYS, "IsValid"); g.link(("try.Runner", "rvalid.Object"))
    g.call("rctl", PAWN, "GetController"); g.link(("try.Runner", "rctl.self"))
    g.call("ralive", SYS, "IsValid"); g.link(("rctl.ReturnValue", "ralive.Object"))
    g.call("rok", MATH, "BooleanAND"); g.link(("rvalid.ReturnValue", "rok.A"), ("ralive.ReturnValue", "rok.B"))
    g.branch("br_alive"); g.link(("rok.ReturnValue", "br_alive.condition")); g.chain("try", "br_alive")
    theirteam = g.pawn_team("r", "try.Runner")
    g.get("myteam", "TeamID")
    g.call("sameteam", MATH, "EqualEqual_IntInt"); g.link((theirteam, "sameteam.A"), ("myteam.TeamID", "sameteam.B"))
    g.branch("br_team"); g.link(("sameteam.ReturnValue", "br_team.condition"), ("br_alive.then", "br_team.exec"))
    g.get("enemyflag", "EnemyFlag"); g.call("efvalid", SYS, "IsValid"); g.link(("enemyflag.EnemyFlag", "efvalid.Object"))
    g.branch("br_ef"); g.link(("efvalid.ReturnValue", "br_ef.condition"), ("br_team.then", "br_ef.exec"))
    g.get("efcarrier", "Carrier", FLAG); g.link(("enemyflag.EnemyFlag", "efcarrier.self"))
    g.call("iscarrier", MATH, "EqualEqual_ObjectObject"); g.link(("efcarrier.Carrier", "iscarrier.A"), ("try.Runner", "iscarrier.B"))
    g.branch("br_carrier"); g.link(("iscarrier.ReturnValue", "br_carrier.condition"), ("br_ef.then", "br_carrier.exec"))
    g.get("ownflag", "OwnFlag"); g.get("ofstate", "FlagState", FLAG); g.link(("ownflag.OwnFlag", "ofstate.self"))
    g.call("ishome", MATH, "EqualEqual_ByteByte", {"B": "0"}); g.link(("ofstate.FlagState", "ishome.A"))
    g.branch("br_home"); g.link(("ishome.ReturnValue", "br_home.condition"), ("br_carrier.then", "br_home.exec"))
    g.get("gm", "GM"); g.call("score", GM_CTF, "ScoreCapture"); g.link(("gm.GM", "score.self"), ("myteam.TeamID", "score.Team"))
    g.set("capflag", "bCapturedNext", FLAG, defaults={"bCapturedNext": "true"}); g.link(("enemyflag.EnemyFlag", "capflag.self"))
    g.call("ret", FLAG, "ReturnHome"); g.link(("enemyflag.EnemyFlag", "ret.self"))
    g.link(("br_home.then", "score.exec")); g.chain("score", "capflag", "ret")

    # --- Recheck: run TryCapture for every pawn already standing in the zone ---
    g.existing("recheck", "Recheck")
    g.call("inzone", ACTOR, "GetOverlappingActors", {"ClassFilter": PAWN}); g.selfnode("selfz"); g.link(("selfz.self", "inzone.self"))
    g.foreach("fe"); g.link(("inzone.OverlappingActors", "fe.Array"), ("recheck.then", "fe.Exec"))
    g.cast("zpawn", PAWN, pure=True); g.link(("fe.Array Element", "zpawn.cast_object"))
    g.selfcall("try1", BASE, "TryCapture"); g.link(("zpawn.cast_result", "try1.Runner"), ("fe.LoopBody", "try1.exec"))
    return g.json()

# ==================================================================================================================
# GM_CTF
#   vars: TeamA, TeamB int; bTeamsReady, bSidesReady, bOccupied bool; Base1Starts, Base2Starts, AllStarts, SpawnCandidates, Pawns Actor[];
#         Center1, Center2, Axis, Anchor1, Anchor2 vector; AxisLenSq, TmpBest float; TmpLocs vector[]; ChosenStart, TmpBestActor Actor;
#         Flag1, Flag2 (Flag); Base1, Base2 (Base)
# ==================================================================================================================
GM_FUNCTIONS = ["EnsureTeams", "ApplyTeamIds", "EnsureSides", "AverageLocation", "ExtremeStart", "DeriveSidesFromWaiting", "SideT", "GatherPawns",
                "IsStartFree", "EnemyDistance", "NearFlag", "CollectCandidates", "CollectFallback", "ChooseStart", "StartForPlayer", "FloorPoint"]

def gm_events():
    g = G()
    g.custom("score", "ScoreCapture", [P("Team", "int")])
    g.custom("killed", "HandleKill", [P("InstigatorController", "object", **{"class": CONTROLLER}), P("VictimController", "object", **{"class": CONTROLLER})])
    g.custom("setup", "SetupCTF")
    g.custom("returned", "OnFlagReturned", [P("Flag", "object", **{"class": FLAG})])
    g.custom("perkmod", "RefreshPerkMods")
    g.custom("verify", "VerifyFlags")
    return g.json()

# function signatures (pass 2): entry/result pins only
def gm_signature(name):
    g = G()
    sig = {
        "EnsureTeams": ((), ()),
        "ApplyTeamIds": ((), ()),
        "EnsureSides": ((), ()),
        "AverageLocation": ((ACTOR_ARRAY("Starts"),), (VEC("Avg"),)),
        "ExtremeStart": ((ACTOR_ARRAY("Starts"), VEC("Point"), P("bFarthest", "bool")), (P("Start", "object", **{"class": ACTOR}),)),
        "DeriveSidesFromWaiting": ((), ()),
        "SideT": ((VEC("Location"),), (P("T", "float"),)),
        "GatherPawns": ((P("Side", "int"),), ()),
        "IsStartFree": ((P("Start", "object", **{"class": ACTOR}),), (P("Free", "bool"),)),
        "EnemyDistance": ((P("Start", "object", **{"class": ACTOR}),), (P("D", "float"),)),
        "NearFlag": ((VEC("Location"),), (P("Near", "bool"),)),
        "CollectCandidates": ((P("Side", "int"),), ()),
        "CollectFallback": ((P("Side", "int"), P("bAny", "bool")), ()),
        "ChooseStart": ((P("Side", "int"),), (P("Start", "object", **{"class": ACTOR}),)),
        "StartForPlayer": ((P("Player", "object", **{"class": CONTROLLER}),), (P("Start", "object", **{"class": ACTOR}),)),
        "FloorPoint": ((VEC("Start"),), (VEC("Location"),)),
    }[name]
    g.entry(params=sig[0])
    if sig[1]: g.result(params=sig[1])
    return g.json()

def _fn(name, body):
    """helper: function graph with the existing entry/result nodes"""
    g = G(); g.entry(); body(g); return g.json()

def gm_fn_EnsureTeams():
    # Team ids come from GameState.GetTeams(), which only lists teams that exist so far (a lobby without bots has ONE entry).
    # Provisional ids 1/2 (set in BeginPlay) let flags spawn and spawns work before both teams exist; once both are known the
    # provisional side-1 team is kept if it is one of them (no side flip), else min/max. ApplyTeamIds pushes ids to flags/bases.
    def body(g):
        g.get("ready", "bTeamsReady"); g.branch("br"); g.link(("ready.bTeamsReady", "br.condition"), ("entry.then", "br.exec"))
        g.call("gs", GS_LIB, "GetGameState"); g.cast("asgs", BC_GS, pure=True); g.link(("gs.ReturnValue", "asgs.cast_object"))
        # KnownTeamIds = ids from GetTeams() + ids of every player state (a solo lobby may list no team at all)
        g.get("k0", "KnownTeamIds"); g.call("kclr", ARR, "Array_Clear", array=True); g.link(("k0.KnownTeamIds", "kclr.TargetArray"), ("br.else", "kclr.exec"))
        g.call("teams", BC_GS, "GetTeams"); g.link(("asgs.cast_result", "teams.self"))
        g.foreach("fet"); g.link(("teams.ReturnValue", "fet.Array"), ("kclr.then", "fet.Exec"))
        g.brk("btx", V_TEAMDATA); g.link(("fet.Array Element", "btx.in"))
        g.get("k1", "KnownTeamIds"); g.call("kadd1", ARR, "Array_AddUnique", array=True); g.link(("k1.KnownTeamIds", "kadd1.TargetArray"), ("btx.TeamID", "kadd1.NewItem"), ("fet.LoopBody", "kadd1.exec"))
        g.get("parr", "PlayerArray", "/Script/Engine.GameStateBase"); g.link(("asgs.cast_result", "parr.self"))
        g.foreach("fep"); g.link(("parr.PlayerArray", "fep.Array"), ("fet.Completed", "fep.Exec"))
        g.cast("pbps", BC_PS, pure=True); g.link(("fep.Array Element", "pbps.cast_object"))
        g.get("pteam", "TeamID", BC_PS); g.link(("pbps.cast_result", "pteam.self"))
        g.call("passigned", MATH, "GreaterEqual_IntInt", {"B": "0"}); g.link(("pteam.TeamID", "passigned.A"))   # ABodycamPlayerState::TeamID starts at -1 (unassigned): never a team
        g.branch("brp"); g.link(("passigned.ReturnValue", "brp.condition"), ("fep.LoopBody", "brp.exec"))
        g.get("k2", "KnownTeamIds"); g.call("kadd2", ARR, "Array_AddUnique", array=True); g.link(("k2.KnownTeamIds", "kadd2.TargetArray"), ("pteam.TeamID", "kadd2.NewItem"), ("brp.then", "kadd2.exec"))
        g.get("kn", "KnownTeamIds"); g.call("n", ARR, "Array_Length", array=True); g.link(("kn.KnownTeamIds", "n.TargetArray"))
        g.call("t0", ARR, "Array_Get", {"Index": "0"}, array=True); g.call("t1", ARR, "Array_Get", {"Index": "1"}, array=True)
        g.link(("kn.KnownTeamIds", "t0.TargetArray"), ("kn.KnownTeamIds", "t1.TargetArray"))
        g.call("two", MATH, "GreaterEqual_IntInt", {"B": "2"}); g.link(("n.ReturnValue", "two.A"))
        g.branch("br2"); g.link(("two.ReturnValue", "br2.condition"), ("fep.Completed", "br2.exec"))
        # --- both known: keep the provisional side-1 team if it is one of them ---
        g.get("ta", "TeamA")
        g.call("eqa", MATH, "EqualEqual_IntInt"); g.call("eqb", MATH, "EqualEqual_IntInt")
        g.link(("ta.TeamA", "eqa.A"), ("t0.Item", "eqa.B"), ("ta.TeamA", "eqb.A"), ("t1.Item", "eqb.B"))
        g.call("keep", MATH, "BooleanOR"); g.link(("eqa.ReturnValue", "keep.A"), ("eqb.ReturnValue", "keep.B"))
        g.branch("brk"); g.link(("keep.ReturnValue", "brk.condition"), ("br2.then", "brk.exec"))
        g.call("other", MATH, "SelectInt"); g.link(("t1.Item", "other.A"), ("t0.Item", "other.B"), ("eqa.ReturnValue", "other.bPickA"))
        g.set("setB1", "TeamB"); g.link(("other.ReturnValue", "setB1.TeamB"), ("brk.then", "setB1.exec"))
        g.call("mn", MATH, "Min"); g.call("mx", MATH, "Max")
        g.link(("t0.Item", "mn.A"), ("t1.Item", "mn.B"), ("t0.Item", "mx.A"), ("t1.Item", "mx.B"))
        g.set("setA2", "TeamA"); g.set("setB2", "TeamB"); g.link(("mn.ReturnValue", "setA2.TeamA"), ("mx.ReturnValue", "setB2.TeamB"), ("brk.else", "setA2.exec")); g.chain("setA2", "setB2")
        g.set("setR", "bTeamsReady", defaults={"bTeamsReady": "true"}); g.link(("setB1.then", "setR.exec"), ("setB2.then", "setR.exec"))
        g.selfcall("apply1", GM_CTF, "ApplyTeamIds"); g.chain("setR", "apply1")
        # --- one known (solo lobby, or the first spawn before the other team exists): it is side 1, the other side gets the other
        #     0-based id (0 <-> 1); ready as well, so flags/bases accept touches (gate) and ids stop moving under the players ---
        g.call("one", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("n.ReturnValue", "one.A"))
        g.branch("br1"); g.link(("one.ReturnValue", "br1.condition"), ("br2.else", "br1.exec"))
        g.call("iszero", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("t0.Item", "iszero.A"))
        g.call("otherid", MATH, "SelectInt", {"A": "1", "B": "0"}); g.link(("iszero.ReturnValue", "otherid.bPickA"))
        g.set("setA3", "TeamA"); g.set("setB3", "TeamB"); g.link(("t0.Item", "setA3.TeamA"), ("otherid.ReturnValue", "setB3.TeamB"), ("br1.then", "setA3.exec")); g.chain("setA3", "setB3")
        g.set("setR3", "bTeamsReady", defaults={"bTeamsReady": "true"})
        g.selfcall("apply2", GM_CTF, "ApplyTeamIds"); g.chain("setB3", "setR3", "apply2")
    return _fn("EnsureTeams", body)

def gm_fn_ApplyTeamIds():
    # flags/bases carry the current TeamA/TeamB (called whenever the ids change)
    def body(g):
        g.get("f1", "Flag1"); g.call("v", SYS, "IsValid"); g.link(("f1.Flag1", "v.Object"))
        g.branch("br"); g.link(("v.ReturnValue", "br.condition"), ("entry.then", "br.exec"))
        g.get("ta", "TeamA"); g.get("tb", "TeamB"); g.get("f2", "Flag2"); g.get("b1", "Base1"); g.get("b2", "Base2")
        g.set("s1", "TeamID", FLAG); g.set("s2", "TeamID", FLAG); g.set("s3", "TeamID", BASE); g.set("s4", "TeamID", BASE)
        g.link(("f1.Flag1", "s1.self"), ("ta.TeamA", "s1.TeamID"), ("f2.Flag2", "s2.self"), ("tb.TeamB", "s2.TeamID"),
               ("b1.Base1", "s3.self"), ("ta.TeamA", "s3.TeamID"), ("b2.Base2", "s4.self"), ("tb.TeamB", "s4.TeamID"))
        g.link(("br.then", "s1.exec")); g.chain("s1", "s2", "s3", "s4")
    return _fn("ApplyTeamIds", body)

def gm_fn_AverageLocation():
    def body(g):
        g.result()
        g.get("tmp", "TmpLocs"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("tmp.TmpLocs", "clr.TargetArray"), ("entry.then", "clr.exec"))
        g.foreach("fe"); g.link(("entry.Starts", "fe.Array"), ("clr.then", "fe.Exec"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.get("tmp2", "TmpLocs"); g.call("add", ARR, "Array_Add", array=True); g.link(("tmp2.TmpLocs", "add.TargetArray"), ("loc.ReturnValue", "add.NewItem"), ("fe.LoopBody", "add.exec"))
        g.get("tmp3", "TmpLocs"); g.call("avg", MATH, "GetVectorArrayAverage"); g.link(("tmp3.TmpLocs", "avg.Vectors"))
        g.link(("avg.ReturnValue", "result.Avg"), ("fe.Completed", "result.exec"))
    return _fn("AverageLocation", body)

def gm_fn_ExtremeStart():
    # nearest (bFarthest = false) or farthest (true) start from Point
    def body(g):
        g.result()
        g.call("init", MATH, "SelectFloat", {"A": "-1.0", "B": "999999999.0"}); g.link(("entry.bFarthest", "init.bPickA"))
        g.set("best0", "TmpBest"); g.link(("init.ReturnValue", "best0.TmpBest"), ("entry.then", "best0.exec"))
        g.set("act0", "TmpBestActor"); g.chain("best0", "act0")
        g.foreach("fe"); g.link(("entry.Starts", "fe.Array"), ("act0.then", "fe.Exec"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.call("dist", MATH, "Vector_Distance"); g.link(("loc.ReturnValue", "dist.V1"), ("entry.Point", "dist.V2"))
        g.get("best", "TmpBest")
        g.call("gt", MATH, "Greater_DoubleDouble"); g.call("lt", MATH, "Less_DoubleDouble")
        g.link(("dist.ReturnValue", "gt.A"), ("best.TmpBest", "gt.B"), ("dist.ReturnValue", "lt.A"), ("best.TmpBest", "lt.B"))
        g.call("nf", MATH, "Not_PreBool"); g.link(("entry.bFarthest", "nf.A"))
        g.call("a1", MATH, "BooleanAND"); g.call("a2", MATH, "BooleanAND"); g.call("better", MATH, "BooleanOR")
        g.link(("entry.bFarthest", "a1.A"), ("gt.ReturnValue", "a1.B"), ("nf.ReturnValue", "a2.A"), ("lt.ReturnValue", "a2.B"), ("a1.ReturnValue", "better.A"), ("a2.ReturnValue", "better.B"))
        g.branch("br"); g.link(("better.ReturnValue", "br.condition"), ("fe.LoopBody", "br.exec"))
        g.set("best1", "TmpBest"); g.link(("dist.ReturnValue", "best1.TmpBest"), ("br.then", "best1.exec"))
        g.set("act1", "TmpBestActor"); g.link(("fe.Array Element", "act1.TmpBestActor")); g.chain("best1", "act1")
        g.get("out", "TmpBestActor"); g.link(("out.TmpBestActor", "result.Start"), ("fe.Completed", "result.exec"))
    return _fn("ExtremeStart", body)

def gm_fn_SideT():
    # 0 at our side-1 base centre, 1 at the side-2 base centre
    def body(g):
        g.result()
        g.get("c1", "Center1"); g.get("axis", "Axis"); g.get("len", "AxisLenSq")
        g.call("sub", MATH, "Subtract_VectorVector"); g.link(("entry.Location", "sub.A"), ("c1.Center1", "sub.B"))
        g.call("dot", MATH, "Dot_VectorVector"); g.link(("sub.ReturnValue", "dot.A"), ("axis.Axis", "dot.B"))
        g.call("safe", MATH, "FMax", {"B": "1.0"}); g.link(("len.AxisLenSq", "safe.A"))
        g.call("div", MATH, "Divide_DoubleDouble"); g.link(("dot.ReturnValue", "div.A"), ("safe.ReturnValue", "div.B"))
        g.link(("div.ReturnValue", "result.T"), ("entry.then", "result.exec"))
    return _fn("SideT", body)

def gm_fn_DeriveSidesFromWaiting():
    # maps without team-tagged starts (The Backrooms): the two starts farthest apart anchor the sides; deep starts = base starts
    def body(g):
        g.get("all", "AllStarts"); g.call("n", ARR, "Array_Length", array=True); g.link(("all.AllStarts", "n.TargetArray"))
        g.call("enough", MATH, "GreaterEqual_IntInt", {"B": "2"}); g.link(("n.ReturnValue", "enough.A"))
        g.branch("br"); g.link(("enough.ReturnValue", "br.condition"), ("entry.then", "br.exec"))
        g.selfcall("avg", GM_CTF, "AverageLocation"); g.link(("all.AllStarts", "avg.Starts"), ("br.then", "avg.exec"))
        g.selfcall("far1", GM_CTF, "ExtremeStart", {"bFarthest": "true"}); g.link(("all.AllStarts", "far1.Starts"), ("avg.Avg", "far1.Point"))
        g.call("loc1", ACTOR, "K2_GetActorLocation"); g.link(("far1.Start", "loc1.self"))
        g.set("an1", "Anchor1"); g.link(("loc1.ReturnValue", "an1.Anchor1"))
        g.selfcall("far2", GM_CTF, "ExtremeStart", {"bFarthest": "true"}); g.link(("all.AllStarts", "far2.Starts"), ("loc1.ReturnValue", "far2.Point"))
        g.call("loc2", ACTOR, "K2_GetActorLocation"); g.link(("far2.Start", "loc2.self"))
        g.set("an2", "Anchor2"); g.link(("loc2.ReturnValue", "an2.Anchor2"))
        g.chain("avg", "far1", "an1", "far2", "an2")
        # temporary axis between the anchors (Center/Axis are recomputed from the base sets by EnsureSides afterwards)
        g.get("a1", "Anchor1"); g.get("a2", "Anchor2")
        g.call("ax", MATH, "Subtract_VectorVector"); g.link(("a2.Anchor2", "ax.A"), ("a1.Anchor1", "ax.B"))
        g.set("setax", "Axis"); g.link(("ax.ReturnValue", "setax.Axis"))
        g.call("axd", MATH, "Dot_VectorVector"); g.link(("ax.ReturnValue", "axd.A"), ("ax.ReturnValue", "axd.B"))
        g.set("setlen", "AxisLenSq"); g.link(("axd.ReturnValue", "setlen.AxisLenSq"))
        g.set("setc1", "Center1"); g.link(("a1.Anchor1", "setc1.Center1"))
        g.chain("an2", "setax", "setlen", "setc1")
        g.foreach("fe"); g.link(("all.AllStarts", "fe.Array"), ("setc1.then", "fe.Exec"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.selfcall("t", GM_CTF, "SideT"); g.link(("loc.ReturnValue", "t.Location"), ("fe.LoopBody", "t.exec"))
        g.call("deep1", MATH, "LessEqual_DoubleDouble", {"B": "0.35"}); g.call("deep2", MATH, "GreaterEqual_DoubleDouble", {"B": "0.65"})
        g.link(("t.T", "deep1.A"), ("t.T", "deep2.A"))
        g.branch("br1"); g.branch("br2"); g.link(("deep1.ReturnValue", "br1.condition"), ("deep2.ReturnValue", "br2.condition"))
        g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
        g.call("add1", ARR, "Array_Add", array=True); g.call("add2", ARR, "Array_Add", array=True)
        g.link(("b1.Base1Starts", "add1.TargetArray"), ("fe.Array Element", "add1.NewItem"), ("b2.Base2Starts", "add2.TargetArray"), ("fe.Array Element", "add2.NewItem"))
        g.link(("t.then", "br1.exec"), ("br1.then", "add1.exec"), ("br1.else", "br2.exec"), ("br2.then", "add2.exec"))
    return _fn("DeriveSidesFromWaiting", body)

def gm_fn_EnsureSides():
    def body(g):
        g.get("ready", "bSidesReady"); g.branch("br"); g.link(("ready.bSidesReady", "br.condition"), ("entry.then", "br.exec"))
        # collect starts by tag (Drone starts are skipped)
        for i, v in enumerate(("AllStarts", "Base1Starts", "Base2Starts")):
            g.get(f"g{i}", v); g.call(f"clr{i}", ARR, "Array_Clear", array=True); g.link((f"g{i}.{v}", f"clr{i}.TargetArray"))
        g.link(("br.else", "clr0.exec")); g.chain("clr0", "clr1", "clr2")
        g.call("starts", GS_LIB, "GetAllActorsOfClass", {"ActorClass": PLAYERSTART}); g.chain("clr2", "starts")
        g.foreach("fe"); g.link(("starts.OutActors", "fe.Array"), ("starts.then", "fe.Exec"))
        g.cast("asps", PLAYERSTART, pure=True); g.link(("fe.Array Element", "asps.cast_object"))
        g.get("pstag", "PlayerStartTag", PLAYERSTART); g.link(("asps.cast_result", "pstag.self"))
        g.call("isdrone", MATH, "EqualEqual_NameName", {"B": "Drone"}); g.link(("pstag.PlayerStartTag", "isdrone.A"))
        g.branch("brd"); g.link(("isdrone.ReturnValue", "brd.condition"), ("fe.LoopBody", "brd.exec"))
        g.get("all2", "AllStarts"); g.call("adda", ARR, "Array_Add", array=True); g.link(("all2.AllStarts", "adda.TargetArray"), ("fe.Array Element", "adda.NewItem"), ("brd.else", "adda.exec"))
        g.call("is1", MATH, "EqualEqual_NameName", {"B": "1"}); g.call("is2", MATH, "EqualEqual_NameName", {"B": "2"})
        g.link(("pstag.PlayerStartTag", "is1.A"), ("pstag.PlayerStartTag", "is2.A"))
        g.branch("br1"); g.branch("br2"); g.link(("is1.ReturnValue", "br1.condition"), ("is2.ReturnValue", "br2.condition"))
        g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
        g.call("add1", ARR, "Array_Add", array=True); g.call("add2", ARR, "Array_Add", array=True)
        g.link(("b1.Base1Starts", "add1.TargetArray"), ("fe.Array Element", "add1.NewItem"), ("b2.Base2Starts", "add2.TargetArray"), ("fe.Array Element", "add2.NewItem"))
        g.link(("adda.then", "br1.exec"), ("br1.then", "add1.exec"), ("br1.else", "br2.exec"), ("br2.then", "add2.exec"))
        # no tagged bases (Backrooms) -> derive them
        g.get("b1n", "Base1Starts"); g.call("n1", ARR, "Array_Length", array=True); g.link(("b1n.Base1Starts", "n1.TargetArray"))
        g.get("b2n", "Base2Starts"); g.call("n2", ARR, "Array_Length", array=True); g.link(("b2n.Base2Starts", "n2.TargetArray"))
        g.call("e1", MATH, "EqualEqual_IntInt", {"B": "0"}); g.call("e2", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("n1.ReturnValue", "e1.A"), ("n2.ReturnValue", "e2.A"))
        g.call("missing", MATH, "BooleanOR"); g.link(("e1.ReturnValue", "missing.A"), ("e2.ReturnValue", "missing.B"))
        g.branch("brm"); g.link(("missing.ReturnValue", "brm.condition"), ("fe.Completed", "brm.exec"))
        g.selfcall("derive", GM_CTF, "DeriveSidesFromWaiting"); g.link(("brm.then", "derive.exec"))
        # still missing -> give up (bSidesReady stays false, spawns fall back to the game's own logic)
        g.get("b1m", "Base1Starts"); g.call("m1", ARR, "Array_Length", array=True); g.link(("b1m.Base1Starts", "m1.TargetArray"))
        g.get("b2m", "Base2Starts"); g.call("m2", ARR, "Array_Length", array=True); g.link(("b2m.Base2Starts", "m2.TargetArray"))
        g.call("f1", MATH, "Greater_IntInt", {"B": "0"}); g.call("f2", MATH, "Greater_IntInt", {"B": "0"}); g.link(("m1.ReturnValue", "f1.A"), ("m2.ReturnValue", "f2.A"))
        g.call("both", MATH, "BooleanAND"); g.link(("f1.ReturnValue", "both.A"), ("f2.ReturnValue", "both.B"))
        g.branch("brok"); g.link(("both.ReturnValue", "brok.condition"), ("derive.then", "brok.exec"), ("brm.else", "brok.exec"))
        # centres + axis
        g.get("b1c", "Base1Starts"); g.selfcall("c1", GM_CTF, "AverageLocation"); g.link(("b1c.Base1Starts", "c1.Starts"), ("brok.then", "c1.exec"))
        g.set("setc1", "Center1"); g.link(("c1.Avg", "setc1.Center1"))
        g.get("b2c", "Base2Starts"); g.selfcall("c2", GM_CTF, "AverageLocation"); g.link(("b2c.Base2Starts", "c2.Starts"))
        g.set("setc2", "Center2"); g.link(("c2.Avg", "setc2.Center2"))
        g.chain("c1", "setc1", "c2", "setc2")
        g.get("gc1", "Center1"); g.get("gc2", "Center2")
        g.call("ax", MATH, "Subtract_VectorVector"); g.link(("gc2.Center2", "ax.A"), ("gc1.Center1", "ax.B"))
        g.set("setax", "Axis"); g.link(("ax.ReturnValue", "setax.Axis"))
        g.call("axd", MATH, "Dot_VectorVector"); g.link(("ax.ReturnValue", "axd.A"), ("ax.ReturnValue", "axd.B"))
        g.set("setlen", "AxisLenSq"); g.link(("axd.ReturnValue", "setlen.AxisLenSq"))
        g.set("setready", "bSidesReady", defaults={"bSidesReady": "true"})
        g.chain("setc2", "setax", "setlen", "setready")
    return _fn("EnsureSides", body)

def gm_fn_GatherPawns():
    # Pawns = pawns that currently have a controller (alive players and bots); EnemyPawns = those not on Side's team
    def body(g):
        g.get("ta", "TeamA"); g.get("tb", "TeamB"); g.call("isone", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("entry.Side", "isone.A"))
        g.call("myteam", MATH, "SelectInt"); g.link(("ta.TeamA", "myteam.A"), ("tb.TeamB", "myteam.B"), ("isone.ReturnValue", "myteam.bPickA"))
        g.get("p0", "Pawns"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("p0.Pawns", "clr.TargetArray"), ("entry.then", "clr.exec"))
        g.get("e0", "EnemyPawns"); g.call("clre", ARR, "Array_Clear", array=True); g.link(("e0.EnemyPawns", "clre.TargetArray")); g.chain("clr", "clre")
        g.call("all", GS_LIB, "GetAllActorsOfClass", {"ActorClass": PAWN}); g.chain("clre", "all")
        g.foreach("fe"); g.link(("all.OutActors", "fe.Array"), ("all.then", "fe.Exec"))
        g.cast("asp", PAWN, pure=True); g.link(("fe.Array Element", "asp.cast_object"))
        g.call("ctl", PAWN, "GetController"); g.link(("asp.cast_result", "ctl.self"))
        g.call("alive", SYS, "IsValid"); g.link(("ctl.ReturnValue", "alive.Object"))
        g.branch("br"); g.link(("alive.ReturnValue", "br.condition"), ("fe.LoopBody", "br.exec"))
        g.get("p1", "Pawns"); g.call("add", ARR, "Array_Add", array=True); g.link(("p1.Pawns", "add.TargetArray"), ("fe.Array Element", "add.NewItem"), ("br.then", "add.exec"))
        team = g.pawn_team("gp", "asp.cast_result")
        g.call("same", MATH, "EqualEqual_IntInt"); g.link((team, "same.A"), ("myteam.ReturnValue", "same.B"))
        g.branch("bre"); g.link(("same.ReturnValue", "bre.condition"), ("add.then", "bre.exec"))
        g.get("e1", "EnemyPawns"); g.call("adde", ARR, "Array_Add", array=True); g.link(("e1.EnemyPawns", "adde.TargetArray"), ("fe.Array Element", "adde.NewItem"), ("bre.else", "adde.exec"))
    return _fn("GatherPawns", body)

def gm_fn_IsStartFree():
    # free = no controlled pawn within 120 cm
    def body(g):
        g.result()
        g.set("occ0", "bOccupied", defaults={"bOccupied": "false"}); g.link(("entry.then", "occ0.exec"))
        g.call("sloc", ACTOR, "K2_GetActorLocation"); g.link(("entry.Start", "sloc.self"))
        g.get("pawns", "Pawns"); g.foreach("fe"); g.link(("pawns.Pawns", "fe.Array"), ("occ0.then", "fe.Exec"))
        g.call("ploc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "ploc.self"))
        g.call("dist", MATH, "Vector_Distance"); g.link(("ploc.ReturnValue", "dist.V1"), ("sloc.ReturnValue", "dist.V2"))
        g.call("near", MATH, "Less_DoubleDouble", {"B": "120.0"}); g.link(("dist.ReturnValue", "near.A"))
        g.branch("br"); g.link(("near.ReturnValue", "br.condition"), ("fe.LoopBody", "br.exec"))
        g.set("occ1", "bOccupied", defaults={"bOccupied": "true"}); g.link(("br.then", "occ1.exec"))
        g.get("occ", "bOccupied"); g.call("free", MATH, "Not_PreBool"); g.link(("occ.bOccupied", "free.A"))
        g.link(("free.ReturnValue", "result.Free"), ("fe.Completed", "result.exec"))
    return _fn("IsStartFree", body)

def gm_fn_EnemyDistance():
    # distance from Start to the nearest enemy pawn (a very large number when there is none)
    def body(g):
        g.result()
        g.set("d0", "TmpDist", defaults={"TmpDist": "999999999.0"}); g.link(("entry.then", "d0.exec"))
        g.call("sloc", ACTOR, "K2_GetActorLocation"); g.link(("entry.Start", "sloc.self"))
        g.get("ep", "EnemyPawns"); g.foreach("fe"); g.link(("ep.EnemyPawns", "fe.Array"), ("d0.then", "fe.Exec"))
        g.call("ploc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "ploc.self"))
        g.call("dist", MATH, "Vector_Distance"); g.link(("ploc.ReturnValue", "dist.V1"), ("sloc.ReturnValue", "dist.V2"))
        g.get("cur", "TmpDist"); g.call("lt", MATH, "Less_DoubleDouble"); g.link(("dist.ReturnValue", "lt.A"), ("cur.TmpDist", "lt.B"))
        g.branch("br"); g.link(("lt.ReturnValue", "br.condition"), ("fe.LoopBody", "br.exec"))
        g.set("d1", "TmpDist"); g.link(("dist.ReturnValue", "d1.TmpDist"), ("br.then", "d1.exec"))
        g.get("out", "TmpDist"); g.link(("out.TmpDist", "result.D"), ("fe.Completed", "result.exec"))
    return _fn("EnemyDistance", body)

def gm_fn_NearFlag():
    # within 400 cm of either flag stand (false until the flags are placed)
    def body(g):
        g.result()
        g.get("placed", "bFlagsPlaced")
        g.get("h1", "FlagHome1"); g.get("h2", "FlagHome2")
        g.call("d1", MATH, "Vector_Distance"); g.call("d2", MATH, "Vector_Distance")
        g.link(("entry.Location", "d1.V1"), ("h1.FlagHome1", "d1.V2"), ("entry.Location", "d2.V1"), ("h2.FlagHome2", "d2.V2"))
        g.call("n1", MATH, "Less_DoubleDouble", {"B": "400.0"}); g.call("n2", MATH, "Less_DoubleDouble", {"B": "400.0"})
        g.link(("d1.ReturnValue", "n1.A"), ("d2.ReturnValue", "n2.A"))
        g.call("either", MATH, "BooleanOR"); g.link(("n1.ReturnValue", "either.A"), ("n2.ReturnValue", "either.B"))
        g.call("near", MATH, "BooleanAND"); g.link(("placed.bFlagsPlaced", "near.A"), ("either.ReturnValue", "near.B"))
        g.link(("near.ReturnValue", "result.Near"), ("entry.then", "result.exec"))
    return _fn("NearFlag", body)

def gm_fn_CollectCandidates():
    # SpawnCandidates = every start that is (own base start OR on our quarter of the map), not an enemy base start,
    # not within 4 m of a flag, and free. Base starts and "slightly ahead of base" starts form ONE pool (Sam, 2026-09-14).
    def body(g):
        g.get("c0", "SpawnCandidates"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("c0.SpawnCandidates", "clr.TargetArray"), ("entry.then", "clr.exec"))
        g.call("isone", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("entry.Side", "isone.A"))
        g.call("notone", MATH, "Not_PreBool"); g.link(("isone.ReturnValue", "notone.A"))
        g.get("all", "AllStarts"); g.foreach("fe"); g.link(("all.AllStarts", "fe.Array"), ("clr.then", "fe.Exec"))
        g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
        g.call("in1", ARR, "Array_Contains", array=True); g.call("in2", ARR, "Array_Contains", array=True)
        g.link(("b1.Base1Starts", "in1.TargetArray"), ("fe.Array Element", "in1.ItemToFind"), ("b2.Base2Starts", "in2.TargetArray"), ("fe.Array Element", "in2.ItemToFind"))
        g.call("own1", MATH, "BooleanAND"); g.call("own2", MATH, "BooleanAND"); g.call("ownbase", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "own1.A"), ("in1.ReturnValue", "own1.B"), ("notone.ReturnValue", "own2.A"), ("in2.ReturnValue", "own2.B"), ("own1.ReturnValue", "ownbase.A"), ("own2.ReturnValue", "ownbase.B"))
        g.call("en1", MATH, "BooleanAND"); g.call("en2", MATH, "BooleanAND"); g.call("enemybase", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "en1.A"), ("in2.ReturnValue", "en1.B"), ("notone.ReturnValue", "en2.A"), ("in1.ReturnValue", "en2.B"), ("en1.ReturnValue", "enemybase.A"), ("en2.ReturnValue", "enemybase.B"))
        g.call("loc", ACTOR, "K2_GetActorLocation"); g.link(("fe.Array Element", "loc.self"))
        g.selfcall("t", GM_CTF, "SideT"); g.link(("loc.ReturnValue", "t.Location"), ("fe.LoopBody", "t.exec"))
        g.call("le", MATH, "LessEqual_DoubleDouble", {"B": "0.25"}); g.call("ge", MATH, "GreaterEqual_DoubleDouble", {"B": "0.75"})
        g.link(("t.T", "le.A"), ("t.T", "ge.A"))
        g.call("s1ok", MATH, "BooleanAND"); g.call("s2ok", MATH, "BooleanAND"); g.call("tok", MATH, "BooleanOR")
        g.link(("isone.ReturnValue", "s1ok.A"), ("le.ReturnValue", "s1ok.B"), ("notone.ReturnValue", "s2ok.A"), ("ge.ReturnValue", "s2ok.B"), ("s1ok.ReturnValue", "tok.A"), ("s2ok.ReturnValue", "tok.B"))
        g.call("inzone", MATH, "BooleanOR"); g.link(("ownbase.ReturnValue", "inzone.A"), ("tok.ReturnValue", "inzone.B"))
        g.call("notenemy", MATH, "Not_PreBool"); g.link(("enemybase.ReturnValue", "notenemy.A"))
        g.call("zoneok", MATH, "BooleanAND"); g.link(("inzone.ReturnValue", "zoneok.A"), ("notenemy.ReturnValue", "zoneok.B"))
        g.branch("brz"); g.link(("zoneok.ReturnValue", "brz.condition"), ("t.then", "brz.exec"))
        g.selfcall("nf", GM_CTF, "NearFlag"); g.link(("loc.ReturnValue", "nf.Location"), ("brz.then", "nf.exec"))
        g.branch("brn"); g.link(("nf.Near", "brn.condition"), ("nf.then", "brn.exec"))
        g.selfcall("free", GM_CTF, "IsStartFree"); g.link(("fe.Array Element", "free.Start"), ("brn.else", "free.exec"))
        g.branch("brf"); g.link(("free.Free", "brf.condition"), ("free.then", "brf.exec"))
        g.get("c1", "SpawnCandidates"); g.call("add", ARR, "Array_Add", array=True); g.link(("c1.SpawnCandidates", "add.TargetArray"), ("fe.Array Element", "add.NewItem"), ("brf.then", "add.exec"))
    return _fn("CollectCandidates", body)

def gm_fn_CollectFallback():
    # SpawnCandidates = own base starts; bAny=false keeps only those not within 4 m of a flag (occupancy ignored)
    def body(g):
        g.get("c0", "SpawnCandidates"); g.call("clr", ARR, "Array_Clear", array=True); g.link(("c0.SpawnCandidates", "clr.TargetArray"), ("entry.then", "clr.exec"))
        g.call("isone", MATH, "EqualEqual_IntInt", {"B": "1"}); g.link(("entry.Side", "isone.A"))
        g.branch("brs"); g.link(("isone.ReturnValue", "brs.condition"), ("clr.then", "brs.exec"))
        g.get("b1", "Base1Starts"); g.get("b2", "Base2Starts")
        g.foreach("fe1"); g.foreach("fe2"); g.link(("b1.Base1Starts", "fe1.Array"), ("b2.Base2Starts", "fe2.Array"), ("brs.then", "fe1.Exec"), ("brs.else", "fe2.Exec"))
        for i in (1, 2):
            g.call(f"loc{i}", ACTOR, "K2_GetActorLocation"); g.link((f"fe{i}.Array Element", f"loc{i}.self"))
            g.selfcall(f"nf{i}", GM_CTF, "NearFlag"); g.link((f"loc{i}.ReturnValue", f"nf{i}.Location"), (f"fe{i}.LoopBody", f"nf{i}.exec"))
            g.call(f"notnear{i}", MATH, "Not_PreBool"); g.link((f"nf{i}.Near", f"notnear{i}.A"))
            g.call(f"ok{i}", MATH, "BooleanOR"); g.link(("entry.bAny", f"ok{i}.A"), (f"notnear{i}.ReturnValue", f"ok{i}.B"))
            g.branch(f"br{i}"); g.link((f"ok{i}.ReturnValue", f"br{i}.condition"), (f"nf{i}.then", f"br{i}.exec"))
            g.get(f"c{i}", "SpawnCandidates"); g.call(f"add{i}", ARR, "Array_Add", array=True)
            g.link((f"c{i}.SpawnCandidates", f"add{i}.TargetArray"), (f"fe{i}.Array Element", f"add{i}.NewItem"), (f"br{i}.then", f"add{i}.exec"))
    return _fn("CollectFallback", body)

def gm_fn_ChooseStart():
    # pool = CollectCandidates; empty -> own base starts away from the flag; empty -> any own base start.
    # pick = the candidate farthest from the nearest enemy, with up to 8 m of random jitter so spawns still vary (anti spawn-camping)
    def body(g):
        g.result()
        g.selfcall("gather", GM_CTF, "GatherPawns"); g.link(("entry.Side", "gather.Side"), ("entry.then", "gather.exec"))
        g.selfcall("p1", GM_CTF, "CollectCandidates"); g.link(("entry.Side", "p1.Side")); g.chain("gather", "p1")
        g.get("c1", "SpawnCandidates"); g.call("n1", ARR, "Array_Length", array=True); g.link(("c1.SpawnCandidates", "n1.TargetArray"))
        g.call("none1", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("n1.ReturnValue", "none1.A"))
        g.branch("br1"); g.link(("none1.ReturnValue", "br1.condition"), ("p1.then", "br1.exec"))
        g.selfcall("p2", GM_CTF, "CollectFallback", {"bAny": "false"}); g.link(("entry.Side", "p2.Side"), ("br1.then", "p2.exec"))
        g.get("c2", "SpawnCandidates"); g.call("n2", ARR, "Array_Length", array=True); g.link(("c2.SpawnCandidates", "n2.TargetArray"))
        g.call("none2", MATH, "EqualEqual_IntInt", {"B": "0"}); g.link(("n2.ReturnValue", "none2.A"))
        g.branch("br2"); g.link(("none2.ReturnValue", "br2.condition"), ("p2.then", "br2.exec"))
        g.selfcall("p3", GM_CTF, "CollectFallback", {"bAny": "true"}); g.link(("entry.Side", "p3.Side"), ("br2.then", "p3.exec"))
        # scoring loop over whatever pool we ended with
        g.set("best0", "TmpBest", defaults={"TmpBest": "-1.0"}); g.set("act0", "TmpBestActor")
        g.link(("br1.else", "best0.exec"), ("br2.else", "best0.exec"), ("p3.then", "best0.exec")); g.chain("best0", "act0")
        g.get("c3", "SpawnCandidates"); g.foreach("fe"); g.link(("c3.SpawnCandidates", "fe.Array"), ("act0.then", "fe.Exec"))
        g.selfcall("ed", GM_CTF, "EnemyDistance"); g.link(("fe.Array Element", "ed.Start"), ("fe.LoopBody", "ed.exec"))
        g.call("capped", MATH, "FMin", {"B": "6000.0"}); g.link(("ed.D", "capped.A"))
        g.call("jit", MATH, "RandomFloatInRange", {"Min": "0.0", "Max": "800.0"})
        g.call("score", MATH, "Add_DoubleDouble"); g.link(("capped.ReturnValue", "score.A"), ("jit.ReturnValue", "score.B"))
        g.get("best", "TmpBest"); g.call("better", MATH, "Greater_DoubleDouble"); g.link(("score.ReturnValue", "better.A"), ("best.TmpBest", "better.B"))
        g.branch("brb"); g.link(("better.ReturnValue", "brb.condition"), ("ed.then", "brb.exec"))
        g.set("best1", "TmpBest"); g.link(("score.ReturnValue", "best1.TmpBest"), ("brb.then", "best1.exec"))
        g.set("act1", "TmpBestActor"); g.link(("fe.Array Element", "act1.TmpBestActor")); g.chain("best1", "act1")
        g.get("out", "TmpBestActor"); g.set("ch", "ChosenStart"); g.link(("out.TmpBestActor", "ch.ChosenStart"), ("fe.Completed", "ch.exec"))
        g.get("ch2", "ChosenStart"); g.link(("ch2.ChosenStart", "result.Start")); g.chain("ch", "result")
    return _fn("ChooseStart", body)

def gm_fn_StartForPlayer():
    # our side-based choice for a controller, or null when teams/sides are not known yet (callers then use the game's logic)
    def body(g):
        g.result()
        g.selfcall("et", GM_CTF, "EnsureTeams"); g.selfcall("es", GM_CTF, "EnsureSides"); g.link(("entry.then", "et.exec")); g.chain("et", "es")
        g.get("sr", "bSidesReady")
        g.call("pvalid", SYS, "IsValid"); g.link(("entry.Player", "pvalid.Object"))
        g.call("ok", MATH, "BooleanAND"); g.link(("sr.bSidesReady", "ok.A"), ("pvalid.ReturnValue", "ok.B"))
        g.branch("br"); g.link(("ok.ReturnValue", "br.condition")); g.chain("es", "br")
        g.get("pps", "PlayerState", CONTROLLER); g.link(("entry.Player", "pps.self"))
        g.cast("asbps", BC_PS, pure=True); g.link(("pps.PlayerState", "asbps.cast_object"))
        g.get("pteam", "TeamID", BC_PS); g.link(("asbps.cast_result", "pteam.self"))
        g.get("teamA", "TeamA"); g.call("isA", MATH, "EqualEqual_IntInt"); g.link(("pteam.TeamID", "isA.A"), ("teamA.TeamA", "isA.B"))
        g.call("side", MATH, "SelectInt", {"A": "1", "B": "2"}); g.link(("isA.ReturnValue", "side.bPickA"))
        g.selfcall("choose", GM_CTF, "ChooseStart"); g.link(("side.ReturnValue", "choose.Side"), ("br.then", "choose.exec"))
        g.set("ch1", "ChosenStart"); g.link(("choose.Start", "ch1.ChosenStart")); g.chain("choose", "ch1")
        g.set("ch0", "ChosenStart"); g.link(("br.else", "ch0.exec"))
        g.get("ch", "ChosenStart"); g.link(("ch.ChosenStart", "result.Start"), ("ch1.then", "result.exec"), ("ch0.then", "result.exec"))
    return _fn("StartForPlayer", body)

def gm_fn_FloorPoint():
    # Where the flag stands for a PlayerStart at Start: a Visibility trace from 50 cm above Start to 300 cm below it. The hit is
    # trusted only if the trace did not begin inside geometry and the hit point is BELOW Start (a player standing on the start
    # would otherwise put the flag on their head); then hit + 2 cm. Anything else -> Start - 90 cm (a PlayerStart sits ~92 cm
    # above its floor; that is the v19 placement). Every path yields a point within 3 m of Start, never the world origin.
    # NOTE: FHitResult has no Blueprint-visible fields, so the generic "break" node has NO output pins (the v20 bug: the link
    # failed and the compiler used (0,0,0)); the hit is read through GameplayStatics.BreakHitResult, the editor's own node.
    def body(g):
        g.result()
        g.call("tr_s", MATH, "Add_VectorVector", {"B": "0.0,0.0,50.0"}); g.link(("entry.Start", "tr_s.A"))
        g.call("tr_e", MATH, "Subtract_VectorVector", {"B": "0.0,0.0,300.0"}); g.link(("entry.Start", "tr_e.A"))
        g.call("trace", SYS, "LineTraceSingle", {"TraceChannel": "TraceTypeQuery1", "bTraceComplex": "false", "DrawDebugType": "None", "bIgnoreSelf": "true"})
        g.link(("tr_s.ReturnValue", "trace.Start"), ("tr_e.ReturnValue", "trace.End"), ("entry.then", "trace.exec"))
        g.call("hit", GS_LIB, "BreakHitResult"); g.link(("trace.OutHit", "hit.Hit"))
        g.call("hz", MATH, "BreakVector"); g.link(("hit.Location", "hz.InVec"))
        g.call("sz", MATH, "BreakVector"); g.link(("entry.Start", "sz.InVec"))
        g.call("below", MATH, "Less_DoubleDouble"); g.link(("hz.Z", "below.A"), ("sz.Z", "below.B"))
        g.call("notpen", MATH, "Not_PreBool"); g.link(("hit.bInitialOverlap", "notpen.A"))
        g.call("ok1", MATH, "BooleanAND"); g.link(("trace.ReturnValue", "ok1.A"), ("notpen.ReturnValue", "ok1.B"))
        g.call("ok", MATH, "BooleanAND"); g.link(("ok1.ReturnValue", "ok.A"), ("below.ReturnValue", "ok.B"))
        g.call("floor", MATH, "Add_VectorVector", {"B": "0.0,0.0,2.0"}); g.link(("hit.Location", "floor.A"))
        g.call("guess", MATH, "Subtract_VectorVector", {"B": "0.0,0.0,90.0"}); g.link(("entry.Start", "guess.A"))
        g.call("pick", MATH, "SelectVector"); g.link(("floor.ReturnValue", "pick.A"), ("guess.ReturnValue", "pick.B"), ("ok.ReturnValue", "pick.bPickA"))
        g.link(("pick.ReturnValue", "result.Location"), ("trace.then", "result.exec"))    # the pure chain reads the trace's stored outputs and the Start parameter only
    return _fn("FloorPoint", body)

GM_FN_BODIES = {"EnsureTeams": gm_fn_EnsureTeams, "ApplyTeamIds": gm_fn_ApplyTeamIds, "EnsureSides": gm_fn_EnsureSides, "AverageLocation": gm_fn_AverageLocation,
                "ExtremeStart": gm_fn_ExtremeStart, "DeriveSidesFromWaiting": gm_fn_DeriveSidesFromWaiting, "SideT": gm_fn_SideT,
                "GatherPawns": gm_fn_GatherPawns, "IsStartFree": gm_fn_IsStartFree, "EnemyDistance": gm_fn_EnemyDistance, "NearFlag": gm_fn_NearFlag,
                "CollectCandidates": gm_fn_CollectCandidates, "CollectFallback": gm_fn_CollectFallback, "ChooseStart": gm_fn_ChooseStart,
                "StartForPlayer": gm_fn_StartForPlayer, "FloorPoint": gm_fn_FloorPoint}

def gm_findplayerstart():
    """Override FindPlayerStart(Player, IncomingName) -> Actor: our side-based choice, else the game's own logic."""
    g = G()
    g.entry(); g.result()
    g.selfcall("ours", GM_CTF, "StartForPlayer"); g.link(("entry.Player", "ours.Player"), ("entry.then", "ours.exec"))
    g.call("valid", SYS, "IsValid"); g.link(("ours.Start", "valid.Object"))
    g.branch("brv"); g.link(("valid.ReturnValue", "brv.condition"), ("ours.then", "brv.exec"))
    g.set("chosen_a", "ChosenStart"); g.link(("ours.Start", "chosen_a.ChosenStart"), ("brv.then", "chosen_a.exec"))
    g.callparent("parent", GM_PARENT, "FindPlayerStart"); g.link(("entry.Player", "parent.Player"), ("entry.IncomingName", "parent.IncomingName"), ("brv.else", "parent.exec"))
    g.set("chosen_b", "ChosenStart"); g.link(("parent.ReturnValue", "chosen_b.ChosenStart")); g.chain("parent", "chosen_b")
    g.get("chosen", "ChosenStart"); g.link(("chosen.ChosenStart", "result.ReturnValue"))
    g.link(("chosen_a.then", "result.exec"), ("chosen_b.then", "result.exec"))
    return g.json()

def gm_chooseplayerstart():
    """Override ChoosePlayerStart(Player) -> Actor: same choice (covers callers that skip FindPlayerStart, e.g. bot spawning)."""
    g = G()
    g.entry(); g.result()
    g.selfcall("ours", GM_CTF, "StartForPlayer"); g.link(("entry.Player", "ours.Player"), ("entry.then", "ours.exec"))
    g.call("valid", SYS, "IsValid"); g.link(("ours.Start", "valid.Object"))
    g.branch("brv"); g.link(("valid.ReturnValue", "brv.condition"), ("ours.then", "brv.exec"))
    g.set("chosen_a", "ChosenStart"); g.link(("ours.Start", "chosen_a.ChosenStart"), ("brv.then", "chosen_a.exec"))
    g.callparent("parent", GM_PARENT, "ChoosePlayerStart"); g.link(("entry.Player", "parent.Player"), ("brv.else", "parent.exec"))
    g.set("chosen_b", "ChosenStart"); g.link(("parent.ReturnValue", "chosen_b.ChosenStart")); g.chain("parent", "chosen_b")
    g.get("chosen", "ChosenStart"); g.link(("chosen.ChosenStart", "result.ReturnValue"))
    g.link(("chosen_a.then", "result.exec"), ("chosen_b.then", "result.exec"))
    return g.json()

def _spawn_pair(g, idx, starts_var, center_var, flag_var, base_var, team_var, prev_exec):
    """spawn flag + base for one side at the base start nearest to that side's centre; returns the last exec node id"""
    g.get(f"bs{idx}", starts_var); g.get(f"ce{idx}", center_var)
    g.selfcall(f"near{idx}", GM_CTF, "ExtremeStart", {"bFarthest": "false"})
    g.link((f"bs{idx}.{starts_var}", f"near{idx}.Starts"), (f"ce{idx}.{center_var}", f"near{idx}.Point"), (prev_exec, f"near{idx}.exec"))
    g.call(f"home{idx}", ACTOR, "K2_GetActorLocation"); g.link((f"near{idx}.Start", f"home{idx}.self"))
    # where the flag stands: FloorPoint (validated floor trace, else 90 cm under the start) -> saved in FlagHome{idx} BEFORE the start is destroyed
    g.selfcall(f"fp{idx}", GM_CTF, "FloorPoint"); g.link((f"home{idx}.ReturnValue", f"fp{idx}.Start"), (f"near{idx}.then", f"fp{idx}.exec"))
    g.set(f"fh{idx}", f"FlagHome{idx}"); g.link((f"fp{idx}.Location", f"fh{idx}.FlagHome{idx}"), (f"fp{idx}.then", f"fh{idx}.exec"))
    # the start the flag stands on leaves play: out of our lists and destroyed, so not even the game's own spawner can use it
    g.get(f"rs{idx}", starts_var); g.call(f"rm{idx}", ARR, "Array_RemoveItem", array=True); g.link((f"rs{idx}.{starts_var}", f"rm{idx}.TargetArray"), (f"near{idx}.Start", f"rm{idx}.Item"))
    g.get(f"ra{idx}", "AllStarts"); g.call(f"rma{idx}", ARR, "Array_RemoveItem", array=True); g.link((f"ra{idx}.AllStarts", f"rma{idx}.TargetArray"), (f"near{idx}.Start", f"rma{idx}.Item"))
    g.call(f"kill{idx}", ACTOR, "K2_DestroyActor"); g.link((f"near{idx}.Start", f"kill{idx}.self"))
    g.chain(f"fh{idx}", f"rm{idx}", f"rma{idx}", f"kill{idx}")
    # pure nodes are re-evaluated by every consumer: after DestroyActor the start's location reads as (0,0,0), so everything
    # below reads the saved FlagHome variable instead (v18 bug: both flags spawned at the world origin)
    g.get(f"fhg{idx}", f"FlagHome{idx}")
    g.call(f"xf{idx}", MATH, "MakeTransform"); g.link((f"fhg{idx}.FlagHome{idx}", f"xf{idx}.Location"))
    g.spawn(f"spf{idx}", FLAG); g.link((f"xf{idx}.ReturnValue", f"spf{idx}.SpawnTransform"), (f"kill{idx}.then", f"spf{idx}.exec"))
    g.get(f"team{idx}", team_var)
    g.set(f"fteam{idx}", "TeamID", FLAG); g.link((f"spf{idx}.ReturnValue", f"fteam{idx}.self"), (f"team{idx}.{team_var}", f"fteam{idx}.TeamID"))
    g.set(f"fside{idx}", "Side", FLAG, defaults={"Side": str(idx)}); g.link((f"spf{idx}.ReturnValue", f"fside{idx}.self"))
    g.set(f"fhome{idx}", "HomeLocation", FLAG); g.link((f"spf{idx}.ReturnValue", f"fhome{idx}.self"), (f"fhg{idx}.FlagHome{idx}", f"fhome{idx}.HomeLocation"))
    g.selfnode(f"selfg{idx}"); g.set(f"fgm{idx}", "GM", FLAG); g.link((f"spf{idx}.ReturnValue", f"fgm{idx}.self"), (f"selfg{idx}.self", f"fgm{idx}.GM"))
    g.call(f"finit{idx}", FLAG, "InitFlag"); g.link((f"spf{idx}.ReturnValue", f"finit{idx}.self"))
    g.set(f"storef{idx}", flag_var); g.link((f"spf{idx}.ReturnValue", f"storef{idx}.{flag_var}"))
    g.spawn(f"spb{idx}", BASE); g.link((f"xf{idx}.ReturnValue", f"spb{idx}.SpawnTransform"))
    g.set(f"bteam{idx}", "TeamID", BASE); g.link((f"spb{idx}.ReturnValue", f"bteam{idx}.self"), (f"team{idx}.{team_var}", f"bteam{idx}.TeamID"))
    g.set(f"bside{idx}", "Side", BASE, defaults={"Side": str(idx)}); g.link((f"spb{idx}.ReturnValue", f"bside{idx}.self"))
    g.selfnode(f"selfb{idx}"); g.set(f"bgm{idx}", "GM", BASE); g.link((f"spb{idx}.ReturnValue", f"bgm{idx}.self"), (f"selfb{idx}.self", f"bgm{idx}.GM"))
    g.set(f"storeb{idx}", base_var); g.link((f"spb{idx}.ReturnValue", f"storeb{idx}.{base_var}"))
    g.chain(f"spf{idx}", f"fteam{idx}", f"fside{idx}", f"fhome{idx}", f"fgm{idx}", f"finit{idx}", f"storef{idx}", f"spb{idx}", f"bteam{idx}", f"bside{idx}", f"bgm{idx}", f"storeb{idx}")
    return f"storeb{idx}"

def gm_logic():
    g = G()
    # --- BeginPlay: parent, then OnPlayerKilled -> HandleKill ---
    g.event("bp", "ReceiveBeginPlay", ACTOR)
    g.callparent("bp_parent", GM_PARENT, "ReceiveBeginPlay")
    g.existing("killed", "HandleKill")
    g.adddelegate("bind", "OnPlayerKilled"); g.link(("killed.OutputDelegate", "bind.Delegate"))
    g.set("provA", "TeamA", defaults={"TeamA": "0"}); g.set("provB", "TeamB", defaults={"TeamB": "1"})   # provisional until EnsureTeams knows better (the game's ids are 0-based; -1 = unassigned)
    g.call("perktimer", SYS, "K2_SetTimer", {"FunctionName": "RefreshPerkMods", "Time": "2.0", "bLooping": "true"}); g.selfnode("selfp"); g.link(("selfp.self", "perktimer.Object"))
    g.chain("bp", "bp_parent", "provA", "provB", "bind", "perktimer")

    # --- RefreshPerkMods (server, every 2 s): every pawn carries the infinite "GadgetCooldown x2.5" effect (the ASC lives on the pawn,
    #     so a respawn needs it again; stack limit 1 -> re-applying never compounds). The game's cooldown ability reads the attribute
    #     when the drone is used, so the HUD timer shows the extended time too. ---
    g.existing("perkmod", "RefreshPerkMods")
    g.call("applyge", BC_GM, "ApplyGameplayEffectToAllPlayers", {"EffectClass": GE_DRONECD}); g.chain("perkmod", "applyge")

    # --- the game's phase events: parent, then SetupCTF (guarded, so whichever comes first sets things up once) ---
    for ev in ("OnRoundWarmup", "OnRoundStart", "OnMatchStart"):
        g.event("ev_" + ev, ev, BC_GM)
        g.callparent("par_" + ev, GM_PARENT, ev)
        g.selfcall("setup_" + ev, GM_CTF, "SetupCTF")
        g.chain("ev_" + ev, "par_" + ev, "setup_" + ev)

    # --- SetupCTF: once; needs teams + sides; flags and bases at the base start nearest each side's centre ---
    g.existing("setup", "SetupCTF")
    g.get("flag1chk", "Flag1"); g.call("already", SYS, "IsValid"); g.link(("flag1chk.Flag1", "already.Object"))
    g.branch("br_already"); g.link(("already.ReturnValue", "br_already.condition")); g.chain("setup", "br_already")
    g.selfcall("et", GM_CTF, "EnsureTeams"); g.selfcall("es", GM_CTF, "EnsureSides"); g.link(("br_already.else", "et.exec")); g.chain("et", "es")
    g.get("sr", "bSidesReady")
    g.branch("br_ready"); g.link(("sr.bSidesReady", "br_ready.condition")); g.chain("es", "br_ready")
    last = _spawn_pair(g, 1, "Base1Starts", "Center1", "Flag1", "Base1", "TeamA", "br_ready.then")
    last = _spawn_pair(g, 2, "Base2Starts", "Center2", "Flag2", "Base2", "TeamB", f"{last}.then")
    g.get("f1", "Flag1"); g.get("f2", "Flag2"); g.get("b1", "Base1"); g.get("b2", "Base2")
    g.set("b1own", "OwnFlag", BASE); g.set("b1en", "EnemyFlag", BASE); g.set("b2own", "OwnFlag", BASE); g.set("b2en", "EnemyFlag", BASE)
    g.link(("b1.Base1", "b1own.self"), ("f1.Flag1", "b1own.OwnFlag"), ("b1.Base1", "b1en.self"), ("f2.Flag2", "b1en.EnemyFlag"),
           ("b2.Base2", "b2own.self"), ("f2.Flag2", "b2own.OwnFlag"), ("b2.Base2", "b2en.self"), ("f1.Flag1", "b2en.EnemyFlag"))
    g.set("placed", "bFlagsPlaced", defaults={"bFlagsPlaced": "true"})
    g.call("verifytimer", SYS, "K2_SetTimer", {"FunctionName": "VerifyFlags", "Time": "2.0", "bLooping": "true"}); g.selfnode("selfv"); g.link(("selfv.self", "verifytimer.Object"))
    g.chain(last, "b1own", "b1en", "b2own", "b2en", "placed", "verifytimer")

    # --- VerifyFlags (server, every 2 s): a flag that is at home (state 0) but more than 1 m from its stand is put back on it.
    #     Nothing legitimate moves a home flag (ReturnHome sets it to HomeLocation = FlagHome), so this is a pure safety net. ---
    g.existing("verify", "VerifyFlags")
    for i in (1, 2):
        g.get(f"vf{i}", f"Flag{i}"); g.call(f"vv{i}", SYS, "IsValid"); g.link((f"vf{i}.Flag{i}", f"vv{i}.Object"))
        g.get(f"vst{i}", "FlagState", FLAG); g.link((f"vf{i}.Flag{i}", f"vst{i}.self"))
        g.call(f"vhome{i}", MATH, "EqualEqual_ByteByte", {"B": "0"}); g.link((f"vst{i}.FlagState", f"vhome{i}.A"))
        g.call(f"vloc{i}", ACTOR, "K2_GetActorLocation"); g.link((f"vf{i}.Flag{i}", f"vloc{i}.self"))
        g.get(f"vfh{i}", f"FlagHome{i}")
        g.call(f"vd{i}", MATH, "Vector_Distance"); g.link((f"vloc{i}.ReturnValue", f"vd{i}.V1"), (f"vfh{i}.FlagHome{i}", f"vd{i}.V2"))
        g.call(f"vfar{i}", MATH, "Greater_DoubleDouble", {"B": "100.0"}); g.link((f"vd{i}.ReturnValue", f"vfar{i}.A"))
        g.call(f"vand{i}", MATH, "BooleanAND"); g.link((f"vv{i}.ReturnValue", f"vand{i}.A"), (f"vhome{i}.ReturnValue", f"vand{i}.B"))
        g.call(f"vand{i}b", MATH, "BooleanAND"); g.link((f"vand{i}.ReturnValue", f"vand{i}b.A"), (f"vfar{i}.ReturnValue", f"vand{i}b.B"))
        g.branch(f"vbr{i}"); g.link((f"vand{i}b.ReturnValue", f"vbr{i}.condition"))
        g.call(f"vfix{i}", ACTOR, "K2_SetActorLocation", {"bSweep": "false", "bTeleport": "true"})
        g.link((f"vf{i}.Flag{i}", f"vfix{i}.self"), (f"vfh{i}.FlagHome{i}", f"vfix{i}.NewLocation"), (f"vbr{i}.then", f"vfix{i}.exec"))
    g.link(("verify.then", "vbr1.exec"), ("vbr1.else", "vbr2.exec"), ("vfix1.then", "vbr2.exec"))

    # --- ScoreCapture(Team): the game's capture rule component adds the point and checks the score limit ---
    g.existing("score", "ScoreCapture")
    g.get("rule", "BodycamCaptureRuleSet"); g.call("addscore", CAPTURE_RULE, "HandleTeamCapturing", {"NumOfCapturer": "1"})
    g.link(("rule.BodycamCaptureRuleSet", "addscore.self"), ("score.Team", "addscore.TeamID")); g.chain("score", "addscore")

    # --- HandleKill: drop the flag the victim carried (flag valid, carried, carrier valid, carrier's controller == victim) ---
    for i in (1, 2):
        g.get(f"kf{i}", f"Flag{i}"); g.call(f"kvalid{i}", SYS, "IsValid"); g.link((f"kf{i}.Flag{i}", f"kvalid{i}.Object"))
        g.get(f"kcar{i}", "Carrier", FLAG); g.link((f"kf{i}.Flag{i}", f"kcar{i}.self"))
        g.call(f"kcv{i}", SYS, "IsValid"); g.link((f"kcar{i}.Carrier", f"kcv{i}.Object"))
        g.call(f"kctl{i}", PAWN, "GetController"); g.link((f"kcar{i}.Carrier", f"kctl{i}.self"))
        g.call(f"keq{i}", MATH, "EqualEqual_ObjectObject"); g.link((f"kctl{i}.ReturnValue", f"keq{i}.A"), ("killed.VictimController", f"keq{i}.B"))
        g.call(f"kand{i}", MATH, "BooleanAND"); g.link((f"kvalid{i}.ReturnValue", f"kand{i}.A"), (f"kcv{i}.ReturnValue", f"kand{i}.B"))
        g.call(f"kand{i}b", MATH, "BooleanAND"); g.link((f"kand{i}.ReturnValue", f"kand{i}b.A"), (f"keq{i}.ReturnValue", f"kand{i}b.B"))
        g.branch(f"kbr{i}"); g.link((f"kand{i}b.ReturnValue", f"kbr{i}.condition"))
        g.call(f"kloc{i}", ACTOR, "K2_GetActorLocation"); g.link((f"kcar{i}.Carrier", f"kloc{i}.self"))
        g.call(f"kdrop{i}", FLAG, "DropAt"); g.link((f"kf{i}.Flag{i}", f"kdrop{i}.self"), (f"kloc{i}.ReturnValue", f"kdrop{i}.Location"))
        g.link((f"kbr{i}.then", f"kdrop{i}.exec"))
    g.link(("killed.then", "kbr1.exec"), ("kbr1.else", "kbr2.exec"), ("kdrop1.then", "kbr2.exec"))

    # --- OnFlagReturned(Flag): the base that owns it re-checks the players already standing in its zone ---
    g.existing("returned", "OnFlagReturned")
    for i in (1, 2):
        g.get(f"rb{i}", f"Base{i}"); g.call(f"rbv{i}", SYS, "IsValid"); g.link((f"rb{i}.Base{i}", f"rbv{i}.Object"))
        g.get(f"rown{i}", "OwnFlag", BASE); g.link((f"rb{i}.Base{i}", f"rown{i}.self"))
        g.call(f"req{i}", MATH, "EqualEqual_ObjectObject"); g.link((f"rown{i}.OwnFlag", f"req{i}.A"), ("returned.Flag", f"req{i}.B"))
        g.call(f"rand{i}", MATH, "BooleanAND"); g.link((f"rbv{i}.ReturnValue", f"rand{i}.A"), (f"req{i}.ReturnValue", f"rand{i}.B"))
        g.branch(f"rbr{i}"); g.link((f"rand{i}.ReturnValue", f"rbr{i}.condition"))
        g.call(f"rchk{i}", BASE, "Recheck"); g.link((f"rb{i}.Base{i}", f"rchk{i}.self"), (f"rbr{i}.then", f"rchk{i}.exec"))
    g.link(("returned.then", "rbr1.exec"), ("rbr1.else", "rbr2.exec"), ("rchk1.then", "rbr2.exec"))
    return g.json()

# ==================================================================================================================
# HUD_Dot stub (WidgetBlueprint): only the events our Blueprints call, with the game's exact signatures. NOT shipped.
# ==================================================================================================================
def huddot_events():
    g = G()
    g.custom("create", "Create Dot", [P("UserName", "string"), P("EN_OverlayType", "byte"), P("In Range Far", "float"), P("In Range Close", "float"),
                                       P("In Color and Opacity", "struct", struct="/Script/CoreUObject.LinearColor"), P("Actor", "object", **{"class": ACTOR}),
                                       P("Texture", "object", **{"class": "/Script/Engine.Texture2D"}), P("DesiredSize", "struct", struct="/Script/CoreUObject.Vector2D"),
                                       P("Name Size", "int"), P("Timer Size", "int")])
    g.custom("upd", "UpdateDistance")
    return g.json()

# ==================================================================================================================
# stand-in parent: the events the game's BP_BodycamGameModeAbstract implements (so "call parent" resolves to it)
# ==================================================================================================================
def parent_events():
    g = G()
    for name in ("OnRoundWarmup", "OnRoundStart", "OnRoundEnded", "OnMatchWaitingForPlayers", "OnMatchStart", "OnMatchEnded"):
        g.event("ev_" + name, name, BC_GM)
    g.event("ev_HMS_OnGameRehosted", "HMS_OnGameRehosted", "/Script/HostMigrationSystem.HMS_GameMode")
    g.event("ev_HMS_OnCreateGameSaveTaskComplete", "HMS_OnCreateGameSaveTaskComplete", "/Script/HostMigrationSystem.HMS_GameMode")
    g.event("ev_K2_OnRestartPlayer", "K2_OnRestartPlayer", "/Script/Engine.GameModeBase")
    return g.json()

if __name__ == "__main__":
    # self-check: every graph serialises and has no duplicate ids / dangling links
    import sys
    graphs = {"flag_events": flag_events(), "flag_onrep_side": flag_onrep_side(), "flag_logic": flag_logic(), "base_events": base_events(),
              "base_logic": base_logic(), "gm_events": gm_events(), "gm_logic": gm_logic(), "gm_findplayerstart": gm_findplayerstart(), "gm_chooseplayerstart": gm_chooseplayerstart(),
              "huddot_events": huddot_events(), "parent_events": parent_events()}
    for f in GM_FUNCTIONS: graphs["sig_" + f] = gm_signature(f); graphs["fn_" + f] = GM_FN_BODIES[f]()
    bad = 0
    for name, js in graphs.items():
        d = json.loads(js); ids = {n["id"] for n in d["nodes"]}
        for a, b in d["links"]:
            for ref in (a, b):
                if ref.split(".", 1)[0] not in ids: print(f"{name}: link to unknown node {ref}"); bad += 1
        print(f"{name}: {len(d['nodes'])} nodes, {len(d['links'])} links")
    sys.exit(1 if bad else 0)
