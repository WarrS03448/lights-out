"""make_blueprints.py — run inside the Unreal Editor (2_make_blueprints.bat) to create the stand-in parent Blueprint and the
CTF gamemode assets. Everything is logged with a [MB] prefix; blueprints.log / blueprints_summary.txt are read by Claude.

Stage 1 (2026-09-14, shipped as v16): the stand-in parent BP_BodycamGameModeAbstract (exact variable order of the game's
class), DA_CTF, and a logic-less GM_CTF child.
Stage 3 (2026-09-14): Bodybomb 5v5 — stand-ins Bombe + BP_InventoryComponent, AC_BB5BombRule (bomb dropped at the attacker
spawn), GE_BB5_DroneCooldown (x4), DA_BB5 (first to 7, 13 rounds, sides swap every 6), GM_BB5 (no bots). Graphs: bb5_graphs.py.
Stage 2: the real CTF logic — BP_CTF_Flag / BP_CTF_Base actors, GM_CTF events + spawn functions, a stub
HUD_Dot widget + icon texture at the game's paths (referenced, never shipped). Node graphs come from ctf_graphs.py and
are built by UBodycamMirrorTools.BuildGraph (C++). Three passes per Blueprint: variables/components -> compile;
events + function signatures -> compile; logic -> compile.
"""
import unreal, os, sys, traceback, importlib

LOG = []
SUMMARY = os.path.join(unreal.Paths.project_dir(), "blueprints_summary.txt")
try: open(SUMMARY, "w", encoding="utf-8").close()     # written line by line, so a crash still leaves the log up to that point
except Exception: pass

def _emit(line):
    LOG.append(line); print(line)
    try:
        with open(SUMMARY, "a", encoding="utf-8") as f: f.write(line + "\n")
    except Exception: pass

def log(msg):
    line = "[MB] " + str(msg); unreal.log(line); _emit(line)

def warn(msg):
    line = "[MB] WARNING " + str(msg); unreal.log_warning(line); _emit(line)

ASSET_TOOLS = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
BEL = unreal.BlueprintEditorLibrary
TOOLS = unreal.BodycamMirrorTools

SCRIPTS = os.path.join(unreal.Paths.project_dir(), "Scripts")
if SCRIPTS not in sys.path: sys.path.insert(0, SCRIPTS)
import ctf_graphs as CG
importlib.reload(CG)
import bb5_graphs as BG
import bodybomb_variant as BV
importlib.reload(BG)
import migration_graphs as MIGRATION
importlib.reload(MIGRATION)
import recovery_graphs as RECOVERY
importlib.reload(RECOVERY)
import recovery_restore_graphs as RESTORE
importlib.reload(RESTORE)
import combat_graphs as COMBAT
importlib.reload(COMBAT)
import combat_transport_graphs as TRANSPORT
importlib.reload(TRANSPORT)
import dom_graphs as DG
importlib.reload(DG)
import lobby_graphs as LG
importlib.reload(LG)

CTF_DIR = "/Game/GM/Gamemode/CTF"
BB5_DIR = "/Game/GM/Gamemode/BB5"
DOM_DIR = "/Game/GM/Gamemode/DOM"
DOT_ICON_DIR = "/Game/MenuSystemPro/FlatDarkGlowingGUI/textures/icons/normal_icon/512x512px"
FLAG_MESH_PATH = "/Game/MilitaryWarehouse/Meshes/SetDressing/SM_CaptureFlag04"   # the game's capture-flag prop (stub here, real in the game)
DRONE_COOLDOWN_FACTOR = 2.5   # Sam, 2026-09-14: drones allowed, cooldown x2.5 (GE_CTF_DroneCooldown); the mode text says [2.5X DRONE COOLDOWN]

def pin(category, sub_object=None, container=None):
    """Variable type description consumed by add_var (resolved in C++ by UBodycamMirrorTools.AddVariable)."""
    return (category, sub_object, container == "array")

def cname(c):
    """Name of a class given either as a Python type (unreal.BodycamGameMode) or a unreal.Class object."""
    try:
        return c.static_class().get_name() if isinstance(c, type) else c.get_name()
    except Exception:
        return str(c)

def make_blueprint(path, name, parent_class):
    full = f"{path}/{name}"
    if EAL.does_asset_exist(full):
        log(f"delete existing {full}"); EAL.delete_asset(full)
    factory = unreal.BlueprintFactory()
    factory.set_editor_property("parent_class", parent_class)
    bp = ASSET_TOOLS.create_asset(name, path, unreal.Blueprint, factory)
    if not bp: raise RuntimeError(f"could not create {full}")
    log(f"created {full} (parent {cname(parent_class)})")
    return bp

def make_widget_blueprint(path, name):
    full = f"{path}/{name}"
    if EAL.does_asset_exist(full):
        log(f"delete existing {full}"); EAL.delete_asset(full)
    factory = unreal.WidgetBlueprintFactory()
    factory.set_editor_property("parent_class", unreal.UserWidget)
    bp = ASSET_TOOLS.create_asset(name, path, unreal.WidgetBlueprint, factory)
    if not bp: raise RuntimeError(f"could not create widget {full}")
    log(f"created widget {full}")
    return bp

def make_stub_texture(path, name):
    full = f"{path}/{name}"
    if EAL.does_asset_exist(full):
        log(f"stub texture already exists: {full}"); return EAL.load_asset(full)
    tex = None
    try:
        tex = ASSET_TOOLS.create_asset(name, path, unreal.Texture2D, unreal.Texture2DFactoryNew())
    except Exception as e:
        warn(f"Texture2DFactoryNew failed ({e}); duplicating the engine default texture instead")
    if not tex:
        tex = EAL.duplicate_asset("/Engine/EngineResources/DefaultTexture", full)
    if not tex: raise RuntimeError(f"could not create stub texture {full}")
    EAL.save_loaded_asset(tex)
    log(f"created stub texture {full}")
    return tex

def make_stub_cue(full):
    """a stand-in SoundCue at one of the game's paths (references resolve to the real cue in the retail game)"""
    path, name = full.rsplit("/", 1)
    if EAL.does_asset_exist(full):
        log(f"stub cue already exists: {full}"); return EAL.load_asset(full)
    cue = ASSET_TOOLS.create_asset(name, path, unreal.SoundCue, unreal.SoundCueFactoryNew())
    if not cue: raise RuntimeError(f"could not create stub cue {full}")
    EAL.save_loaded_asset(cue)
    log(f"created stub cue {full}")
    return cue

def make_stub_mesh(full):
    """a stand-in StaticMesh at one of the game's asset paths (cooked references resolve to the real mesh in the retail game)"""
    if EAL.does_asset_exist(full):
        log(f"stub mesh already exists: {full}"); return EAL.load_asset(full)
    mesh = EAL.duplicate_asset("/Engine/BasicShapes/Cylinder", full)
    if not mesh: raise RuntimeError(f"could not create stub mesh {full}")
    EAL.save_loaded_asset(mesh)
    log(f"created stub mesh {full}")
    return mesh

def add_var(bp, name, pin_type):
    category, sub_object, is_array = pin_type
    if isinstance(sub_object, type): sub_object = sub_object.static_class()
    ok = TOOLS.add_variable(bp, name, category, sub_object, is_array)
    log(f"  var {name} ({category}{'[]' if is_array else ''}): {'ok' if ok else 'FAILED'}")
    return ok

def add_component(bp, comp_class, var_name):
    res = TOOLS.add_component(bp, comp_class.static_class() if isinstance(comp_class, type) else comp_class, var_name)
    if str(res).startswith("ERROR"): warn(f"  component {var_name}: {res}"); return None
    log(f"  component {var_name} ({cname(comp_class)}): {res}")
    return res

def replicate(bp, var, rep_notify=""):
    ok = TOOLS.set_variable_replicated(bp, var, True, rep_notify)
    log(f"  replicated {var}{' (RepNotify ' + rep_notify + ')' if rep_notify else ''}: {'ok' if ok else 'FAILED'}")

class BuildFailed(RuntimeError):
    """a graph or a compile had errors: the run stops here (no RESULT: OK -> 3_cook.bat and the pak builder refuse to continue)"""

def compile_report(bp, label):
    rep = TOOLS.compile_and_report(bp)
    log(f"compile {label}: {'CLEAN' if not rep else chr(10) + rep}")
    if any(l.startswith("ERROR:") or l.startswith("STATUS: BS_Error") for l in rep.splitlines()):
        raise BuildFailed(f"compile {label} has errors (see above)")

def layout(bp, label):
    gen = BEL.generated_class(bp)
    log(f"layout of {label} ({gen.get_name()}):\n" + TOOLS.describe_class_layout(gen, True))

def build(bp, graph, js, label):
    """BuildGraph + log; the first line of the report says how many errors there were"""
    log(f"building {label} [{graph}] ...")
    rep = TOOLS.build_graph(bp, graph, js)
    first = rep.splitlines()[0] if rep else rep
    bad = first.startswith("ERROR") or ("error(s)" in first and not first.rstrip().endswith(" 0 error(s)"))
    (warn if bad else log)(f"build {label} [{graph}]: {rep.rstrip()}" if bad else f"build {label} [{graph}]: {first}")
    if not bad:
        for line in rep.splitlines()[1:]:
            if line.strip(): log("    " + line)
    if bad:
        # a graph error means a pin was left unlinked and the compiler silently used its default (v20: both flags at the
        # world origin because a HitResult "break" node had no output pins) -> never let such a run reach the cook
        raise BuildFailed(f"graph {label} [{graph}] has errors (see above)")

def gen_class(bp): return BEL.generated_class(bp)

def cls_of(path):
    c = unreal.load_object(None, path)
    if not c: warn(f"class not found: {path}")
    return c

def pre_clean():
    """Delete authored mirror assets together, including mutually referring request actors."""
    paths = ["/Game/GM/Gamemode/GM_BB1"] + [
            "/Game/GM/Gamemode/BB1/" + asset for asset in (
                "BP_BB1RestoreRequest", "BP_BB1Recovery", "BP_BB1MigrationRequest", "BP_BB1StartRequest", "BP_BB1TeamRequest",
                "BP_CHCombatRequest", "BP_CHCombatObserver", "BP_CHCombatManager",
                "AC_BB1BombRule", "GE_BB1_DroneCooldown")] + ["/Game/GM/DATA/DataAsset/DA_BB1"]
    paths += ["/Game/GM/Gamemode/GM_DOM", DOM_DIR + "/BP_DOM_Point", DOM_DIR + "/GE_DOM_DroneCooldown", "/Game/GM/DATA/DataAsset/DA_DOM",
                 "/Game/GM/Gamemode/GM_CTF", CTF_DIR + "/BP_CTF_Base", CTF_DIR + "/BP_CTF_Flag", CTF_DIR + "/GE_CTF_NoPerk", CTF_DIR + "/GE_CTF_DroneCooldown", "/Game/MenuSystemPro/INGAME/HUD_Dot",
                 "/Game/GM/DATA/DataAsset/DA_CTF",
                 BB5_DIR + "/BP_BB5RestoreRequest", BB5_DIR + "/BP_BB5Recovery", BB5_DIR + "/BP_BB5MigrationRequest", "/Game/GM/Gamemode/GM_BB5", BB5_DIR + "/BP_BB5StartRequest", BB5_DIR + "/BP_BB5TeamRequest", BB5_DIR + "/AC_BB5BombRule", BB5_DIR + "/GE_BB5_DroneCooldown", "/Game/GM/DATA/DataAsset/DA_BB5",
                 BG.INV_PKG, BG.BOMBE_PKG,
                 "/Game/GM/Gamemode/GM_CHLobby", LG.HOST_PKG,
                 LG.GI_PKG,
                 "/Game/GM/Gamemode/BP_BodycamGameModeAbstract"]
    # The combat manager/observer/request also refer to one another.
    paths += [BB5_DIR + '/' + name for name in ('BP_CHCombatRequest','BP_CHCombatObserver','BP_CHCombatManager')]
    assets = [EAL.load_asset(path) for path in paths if EAL.does_asset_exist(path)]
    if assets and not EAL.delete_loaded_assets(assets):
        raise BuildFailed('Could not remove stale authored mirror assets')
    log(f'deleted {len(assets)} authored mirror assets as one dependency group')

def make_drone_cooldown_ge(path, name, factor):
    """infinite GameplayEffect: CharacterAttributeSet.GadgetCooldown x factor, stack limit 1 (the GM re-applies it every 2 s)"""
    ge = make_blueprint(path, name, unreal.GameplayEffect)
    compile_report(ge, name)
    gecdo = unreal.get_default_object(gen_class(ge))
    for prop, val in (("duration_policy", unreal.GameplayEffectDurationType.INFINITE), ("stacking_type", unreal.GameplayEffectStackingType.AGGREGATE_BY_TARGET), ("stack_limit_count", 1)):
        try: gecdo.set_editor_property(prop, val); log(f"  GE default {prop} = {val}")
        except Exception as e: raise BuildFailed(f"GE: could not set {prop}: {e}")
    rep = TOOLS.add_gameplay_effect_modifier(ge, unreal.CharacterAttributeSet.static_class(), "GadgetCooldown", "Multiply", factor)
    log("  GE modifier: " + rep)
    if not rep.startswith("ok") or "attribute valid=1" not in rep: raise BuildFailed(f"{name} modifier not set: " + rep)
    compile_report(ge, f"{name} (final)")
    EAL.save_loaded_asset(ge)
    return ge

def make_config_asset(name, phase, scoring, team, note):
    da_path = f"/Game/GM/DATA/DataAsset/{name}"
    if EAL.does_asset_exist(da_path): EAL.delete_asset(da_path)
    daf = unreal.DataAssetFactory()
    daf.set_editor_property("data_asset_class", unreal.GameModeConfigDataAsset)
    da = ASSET_TOOLS.create_asset(name, "/Game/GM/DATA/DataAsset", unreal.GameModeConfigDataAsset, daf)
    ph = unreal.PhaseConfig(); sc = unreal.ScoringConfig(); tm = unreal.TeamConfig()
    for obj, vals in ((ph, phase), (sc, scoring), (tm, team)):
        for k, v in vals.items(): obj.set_editor_property(k, v)
    da.set_editor_property("phase_config", ph); da.set_editor_property("scoring_config", sc); da.set_editor_property("team_config", tm)
    EAL.save_loaded_asset(da)
    log(f"created {da_path}: {note}")
    return da

# ----------------------------------------------------------------------------------------------------------------------
def stage1():
    """stand-in parent + DA_CTF + GM_CTF shell (same as v16, plus the parent's event stubs for 'call parent')"""
    parent = make_blueprint("/Game/GM/Gamemode", "BP_BodycamGameModeAbstract", unreal.BodycamGameMode)
    add_var(parent, "GamemodeForCompatibility", pin("byte"))                                                    # the game uses enum GameMode; a plain byte has the same cooked layout
    add_var(parent, "HUDClassCommonUILevelOverrides", pin("struct", unreal.Vector.static_struct(), "array"))   # placeholder struct type (never serialized by children)
    add_var(parent, "HUDClassCommonUI", pin("class", unreal.HUD.static_class()))
    add_var(parent, "HMS_bBotsMethod", pin("bool"))
    add_var(parent, "bTriggerWeatherTransition", pin("bool"))
    add_component(parent, unreal.BodycamTimeLimitRuleSetComponent, "BodycamTimeLimitRuleSet")
    log("  begin play: " + TOOLS.add_begin_play_print(parent, "BP_BodycamGameModeAbstract stub"))
    build(parent, "EventGraph", CG.parent_events(), "parent event stubs")
    compile_report(parent, "BP_BodycamGameModeAbstract")
    expect = ["UberGraphFrame", "BodycamTimeLimitRuleSet", "DefaultSceneRoot", "GamemodeForCompatibility", "HUDClassCommonUILevelOverrides",
              "HUDClassCommonUI", "HMS_bBotsMethod", "bTriggerWeatherTransition", "OnPlayerKilled"]
    got = [line.split()[2] for line in TOOLS.describe_class_layout(gen_class(parent), True).splitlines()[:len(expect)]]
    log(("LAYOUT MATCHES the game's BP_BodycamGameModeAbstract_C" if got == expect else f"LAYOUT MISMATCH: got {got}, expected {expect}"))
    EAL.save_loaded_asset(parent)

    da = make_config_asset("DA_CTF", {"round_warmup_duration": 6.0, "respawn_delay": 7.0}, {"score_limit": 3, "max_phases": 0},
                           {"team_max_size": 10, "max_players": 20, "team_switch_interval": 0}, "score limit 3, 10 per team, 20 players, respawn 7 s")

    gm = make_blueprint("/Game/GM/Gamemode", "GM_CTF", gen_class(parent))
    add_component(gm, unreal.BodycamCaptureRuleSetComponent, "BodycamCaptureRuleSet")
    compile_report(gm, "GM_CTF (shell)")
    return parent, da, gm

# ----------------------------------------------------------------------------------------------------------------------
def stage2(parent, da, gm):
    # ---------- stubs at the game's paths (referenced by our Blueprints, never shipped) ----------
    hud = make_widget_blueprint("/Game/MenuSystemPro/INGAME", "HUD_Dot")
    add_var(hud, "Image", pin("object", unreal.Image))   # the retail HUD_Dot's Image widget variable (read by the flag's marker fix-up)
    build(hud, "EventGraph", CG.huddot_events(), "HUD_Dot stub events")
    compile_report(hud, "HUD_Dot stub")
    EAL.save_loaded_asset(hud)
    make_stub_texture(DOT_ICON_DIR, "t_radio_512x512")
    make_stub_texture("/Game/MenuSystemPro/Textures/Menu", "T_UI_Frame_Setting_Divider")
    make_stub_mesh(FLAG_MESH_PATH)
    for cue in CG.SND_ALL:   # the graphs' cue paths -> stubs at the same paths (never drift)
        make_stub_cue(cue.split(".")[0])

    # ---------- GE_CTF_DroneCooldown: infinite effect, CharacterAttributeSet.GadgetCooldown x 2.5 -> "[2.5X DRONE COOLDOWN]" ----------
    # The game's GA_Character_ApplyPerkCooldown builds the cooldown as remaining + GadgetCooldown (attribute CURRENT value, read when the
    # perk is used), so a multiplicative modifier scales every drone cooldown; the ASC lives on the pawn, so the GM re-applies it every
    # 2 s (stack limit 1 -> never compounds). Replaces GE_CTF_NoPerk (v20/v21: tags that blocked the perk entirely).
    make_drone_cooldown_ge(CTF_DIR, "GE_CTF_DroneCooldown", DRONE_COOLDOWN_FACTOR)

    # ---------- pass 1: actors, variables, components, replication ----------
    flag = make_blueprint(CTF_DIR, "BP_CTF_Flag", unreal.Actor)
    add_component(flag, unreal.StaticMeshComponent, "Mesh")
    add_component(flag, unreal.SphereComponent, "Trigger")
    add_component(flag, unreal.BodycamWidgetComponent, "Dot")
    log("  Dot defaults: " + TOOLS.set_widget_component_defaults(flag, "Dot", gen_class(hud), True))
    add_var(flag, "TeamID", pin("int")); add_var(flag, "Side", pin("byte")); add_var(flag, "FlagState", pin("byte"))
    add_var(flag, "Carrier", pin("object", unreal.Pawn)); add_var(flag, "HomeLocation", pin("struct", unreal.Vector.static_struct()))
    add_var(flag, "GM", pin("object", gen_class(gm)))
    add_var(flag, "StaticMat", pin("object", unreal.MaterialInterface)); add_var(flag, "LastEvent", pin("byte")); add_var(flag, "EventSeq", pin("int"))
    add_var(flag, "bCapturedNext", pin("bool")); add_var(flag, "DotState", pin("byte"))   # DotState: 0 no marker yet, 1 own (green), 2 enemy (red)
    add_var(flag, "LastLocalPawn", pin("object", unreal.Pawn))   # RefreshDot: a new local pawn re-creates the marker (HUD_Dot captures ControlledPawn once)
    add_var(flag, "EventTeam", pin("int"))                        # MarkEvent: the team the event is good news for (team-relative sound)
    replicate(flag, "Carrier"); replicate(flag, "LastEvent"); replicate(flag, "EventTeam")
    replicate(flag, "Side", "OnRep_Side"); replicate(flag, "TeamID", "OnRep_TeamID"); replicate(flag, "FlagState", "OnRep_FlagState"); replicate(flag, "EventSeq", "OnRep_EventSeq")
    for fn in ("OnRep_Side", "OnRep_TeamID", "OnRep_FlagState", "OnRep_EventSeq"): log(f"  {fn} graph: " + TOOLS.ensure_function_graph(flag, fn))
    compile_report(flag, "BP_CTF_Flag (vars)")

    base = make_blueprint(CTF_DIR, "BP_CTF_Base", unreal.Actor)
    add_component(base, unreal.SphereComponent, "Trigger")
    add_component(base, unreal.StaticMeshComponent, "Pad")
    add_var(base, "TeamID", pin("int")); add_var(base, "Side", pin("byte"))
    add_var(base, "OwnFlag", pin("object", gen_class(flag))); add_var(base, "EnemyFlag", pin("object", gen_class(flag)))
    add_var(base, "GM", pin("object", gen_class(gm)))
    for v in ("TeamID", "Side"): replicate(base, v)
    compile_report(base, "BP_CTF_Base (vars)")

    for v in ("TeamA", "TeamB"): add_var(gm, v, pin("int"))
    for v in ("bTeamsReady", "bSidesReady", "bOccupied"): add_var(gm, v, pin("bool"))
    add_var(gm, "bFlagsPlaced", pin("bool"))
    for v in ("Base1Starts", "Base2Starts", "AllStarts", "SpawnCandidates", "Pawns", "EnemyPawns"): add_var(gm, v, pin("object", unreal.Actor, "array"))
    add_var(gm, "KnownTeamIds", pin("int", None, "array"))
    for v in ("Center1", "Center2", "Axis", "Anchor1", "Anchor2", "FlagHome1", "FlagHome2"): add_var(gm, v, pin("struct", unreal.Vector.static_struct()))
    for v in ("AxisLenSq", "TmpBest", "TmpDist"): add_var(gm, v, pin("float"))
    add_var(gm, "TmpLocs", pin("struct", unreal.Vector.static_struct(), "array"))
    for v in ("ChosenStart", "TmpBestActor"): add_var(gm, v, pin("object", unreal.Actor))
    for v in ("Flag1", "Flag2"): add_var(gm, v, pin("object", gen_class(flag)))
    for v in ("Base1", "Base2"): add_var(gm, v, pin("object", gen_class(base)))
    compile_report(gm, "GM_CTF (vars)")

    # ---------- pass 2: custom events + function signatures, so every Blueprint can call every other one ----------
    build(flag, "EventGraph", CG.flag_events(), "Flag events")
    build(base, "EventGraph", CG.base_events(), "Base events")
    build(gm, "EventGraph", CG.gm_events(), "GM events")
    for fn in CG.GM_FUNCTIONS: build(gm, fn, CG.gm_signature(fn), f"GM function signature {fn}")
    log("  override FindPlayerStart: " + TOOLS.override_function(gm, "FindPlayerStart"))
    log("  override ChoosePlayerStart: " + TOOLS.override_function(gm, "ChoosePlayerStart"))
    for bp, label in ((flag, "BP_CTF_Flag (events)"), (base, "BP_CTF_Base (events)"), (gm, "GM_CTF (events)")): compile_report(bp, label)

    # ---------- pass 3: logic ----------
    build(flag, "EventGraph", CG.flag_logic(), "Flag logic")
    build(flag, "OnRep_Side", CG.flag_onrep_side(), "Flag OnRep_Side")
    build(flag, "OnRep_TeamID", CG.flag_onrep_teamid(), "Flag OnRep_TeamID")
    build(flag, "OnRep_FlagState", CG.flag_onrep_flagstate(), "Flag OnRep_FlagState")
    build(flag, "OnRep_EventSeq", CG.flag_onrep_eventseq(), "Flag OnRep_EventSeq")
    build(base, "EventGraph", CG.base_logic(), "Base logic")
    build(gm, "EventGraph", CG.gm_logic(), "GM logic")
    for fn in CG.GM_FUNCTIONS: build(gm, fn, CG.GM_FN_BODIES[fn](), f"GM function {fn}")
    build(gm, "FindPlayerStart", CG.gm_findplayerstart(), "GM FindPlayerStart override")
    build(gm, "ChoosePlayerStart", CG.gm_chooseplayerstart(), "GM ChoosePlayerStart override")
    for bp, label in ((flag, "BP_CTF_Flag (logic)"), (base, "BP_CTF_Base (logic)"), (gm, "GM_CTF (logic)")): compile_report(bp, label)

    # ---------- class defaults ----------
    for bp, label in ((flag, "BP_CTF_Flag"), (base, "BP_CTF_Base")):
        cdo = unreal.get_default_object(gen_class(bp))
        for prop, val in (("replicates", True), ("replicate_movement", True), ("always_relevant", True)):
            try: cdo.set_editor_property(prop, val); log(f"  {label} default {prop} = {val}")
            except Exception as e: warn(f"  {label}: could not set {prop}: {e}")
    cdo = unreal.get_default_object(gen_class(gm))
    try:
        cdo.set_editor_property("config_data_asset", da); log("  GM_CTF class default ConfigDataAsset -> DA_CTF")
    except Exception as e:
        warn(f"  could not set ConfigDataAsset: {e}")
    for bp, label in ((flag, "BP_CTF_Flag (final)"), (base, "BP_CTF_Base (final)"), (gm, "GM_CTF (final)")): compile_report(bp, label)
    for bp, label in ((flag, "BP_CTF_Flag"), (base, "BP_CTF_Base"), (gm, "GM_CTF")): layout(bp, label)
    for bp in (flag, base, gm): EAL.save_loaded_asset(bp)
    log("stage 2 done")

# ----------------------------------------------------------------------------------------------------------------------
def stage3_bb5(parent, mode_id="BB5"):
    """Bodybomb 5v5 (Sam, 2026-09-14). Stock Bodybomb minus the spectator drone (pak builder: DefaultDroneClass), plus: the bomb
    dropped at the centre of the attacker spawn instead of equipped, 4x drone cooldown, first to 7 with a side swap every 6 rounds,
    no bots. See bb5_graphs.py for the graphs."""
    # Each mode owns its authored assets; the established graph logic is shared.
    def variant_name(value):
        return BV.remap(value, mode_id)
    def build(bp, graph, js, label):
        return globals()["build"](bp, graph, BV.graph(js, mode_id), variant_name(label))
    def make_blueprint(path, asset, parent_class):
        return globals()["make_blueprint"](variant_name(path), variant_name(asset), parent_class)
    def make_config_asset(asset, phase, scoring, team, note):
        return globals()["make_config_asset"](variant_name(asset), phase, scoring, team, variant_name(note))
    def make_drone_cooldown_ge(path, asset, factor):
        return globals()["make_drone_cooldown_ge"](variant_name(path), variant_name(asset), factor)
    def add_component(bp, cls, variable):
        return globals()["add_component"](bp, cls, variant_name(variable))
    # ---------- stand-ins at the game's paths (referenced by our component, never shipped) ----------
    if mode_id == "BB5":
        bombe = make_blueprint(BG.BOMBE_PKG.rsplit("/", 1)[0], "Bombe", unreal.Actor)
        compile_report(bombe, "Bombe stub"); EAL.save_loaded_asset(bombe)
        inv = make_blueprint(BG.INV_PKG.rsplit("/", 1)[0], "BP_InventoryComponent", unreal.ActorComponent)
        build(inv, "EventGraph", BG.inventory_events(), "BP_InventoryComponent stub events")
        for fn in BG.INVENTORY_FUNCTIONS: build(inv, fn, BG.inventory_signature(fn), f"BP_InventoryComponent stub function {fn}")
        compile_report(inv, "BP_InventoryComponent stub"); EAL.save_loaded_asset(inv)

    make_drone_cooldown_ge(BB5_DIR, "GE_BB5_DroneCooldown", (3.0 if mode_id == "BB1" else BG.DRONE_COOLDOWN_FACTOR))
    # the game's DA_BodyBomb values (PhaseDuration 180, warm-up 6, 5 per team / 10 players) with Sam's round format; the pak
    # builder also applies these values from each manifest: BB1 first to 5 of 9; BB5 first to 7 of 13.
    da = make_config_asset("DA_BB5", {"phase_duration": (120.0 if mode_id == "BB1" else 180.0), "round_warmup_duration": 6.0}, {"score_limit": (5 if mode_id == "BB1" else 7), "max_phases": (9 if mode_id == "BB1" else 13)},
                           {"team_max_size": 5, "max_players": 10, "team_switch_interval": (1 if mode_id == "BB1" else 6)}, ("first to 5 of 9, sides swap every round, 120 s rounds" if mode_id == "BB1" else "first to 7 of 13, sides swap every 6, 180 s rounds"))

    # ---------- AC_BB5BombRule: pass 1 variables ----------
    rule = make_blueprint(BB5_DIR, "AC_BB5BombRule", unreal.ObjectiveRuleSetComponent)
    add_var(rule, "BombClass", pin("class", unreal.Actor)); add_var(rule, "Attacker", pin("object", unreal.PlayerState))
    add_var(rule, "CurrentBomb", pin("object", unreal.Actor))
    add_var(rule, "DropTries", pin("int")); add_var(rule, "TeamPawns", pin("object", unreal.Actor, "array"))
    add_var(rule, "TmpLocs", pin("struct", unreal.Vector.static_struct(), "array")); add_var(rule, "SpawnCentre", pin("struct", unreal.Vector.static_struct()))
    # the bomb carrier's own pawn location, and whether we have one: PlaceBomb averages only the team-mates standing near him,
    # so one player who already left the spawn cannot drag the bomb out of it (2026-09-16)
    add_var(rule, "AnchorLoc", pin("struct", unreal.Vector.static_struct())); add_var(rule, "bHaveAnchor", pin("bool"))
    # team-base spawns (2026-09-16). This state is on the COMPONENT and not on GM_BB5 on purpose: the pak builder writes the
    # gamemode CDO by property index across the hierarchy, so a gamemode may not declare variables. See bb5_graphs.py.
    add_var(rule, "Base1Starts", pin("object", unreal.Actor, "array")); add_var(rule, "Base2Starts", pin("object", unreal.Actor, "array"))
    add_var(rule, "SpawnCandidates", pin("object", unreal.Actor, "array")); add_var(rule, "ChosenStart", pin("object", unreal.Actor))
    add_var(rule, "bStartsReady", pin("bool")); add_var(rule, "bStartTaken", pin("bool")); add_var(rule, "bLatched", pin("bool"))
    add_var(rule, "LatchTeam", pin("int")); add_var(rule, "LatchSide", pin("int"))
    add_var(rule, "LatchObjectiveTeam", pin("int"))
    # RecountTeams' running count (2026-09-16): the game's score-limit check only sees teams with PlayerCount > 0, and our
    # TeamID writes bypass the only thing that counts them. See bb5_graphs.py, THE TEAM HEAD-COUNT.
    add_var(rule, "TeamTally", pin("int"))
    for name in ("StartScratch", "StartApprovedRoster", "StartApprovedMatch", "FinalScratch", "FinalBatch", "FinalMeta", "FinalMatch", "FinalCombat"):
        add_var(rule, name, pin("string"))
    add_var(rule, "StartDeadline", pin("int"))
    add_var(rule, "PresenceScratch", pin("string")); add_var(rule, "PresenceCount", pin("int"))
    add_var(rule, "PresenceDirty", pin("bool"))
    for name in ("StartApprovedHumans", "StartCurrentHumans"):
        add_var(rule, name, pin("string", None, "array"))
    add_var(rule, "FinalPending", pin("bool")); add_var(rule, "FinalAcknowledged", pin("bool"))
    add_var(rule, "FinalCaptured", pin("bool"))
    add_var(rule, "CompetitiveStarted", pin("bool"))
    add_var(rule, "CombatManager", pin("object", unreal.Actor))
    for name in ("GuardSeen", "GuardNext"):
        add_var(rule, name, pin("string"))
    add_var(rule, "GuardDispatched", pin("bool"))
    add_var(rule, "HostEpoch", pin("int"))
    add_var(rule, "RecoveryPending", pin("bool"))
    add_var(rule, "RecoveryApplied", pin("bool"))
    add_var(rule, "MigrationPending", pin("bool"))
    add_var(rule, "MigrationCallbackSeen", pin("bool"))
    add_var(rule, "MigrationCandidate", pin("string"))
    for name in MIGRATION.DURABLE:
        if not TOOLS.set_variable_save_game(rule, name):
            raise BuildFailed("SaveGame flag missing: " + name)
    for name in ("RecoveryPending", "RecoveryApplied"):
        if not TOOLS.set_variable_save_game(rule, name):
            raise BuildFailed("SaveGame flag missing: " + name)
    compile_report(rule, "AC_BB5BombRule (vars)")
    # pass 2: events + signatures + the native override
    build(rule, "EventGraph", BG.rule_events(), "AC_BB5BombRule events")
    for fn in BG.RULE_FUNCTIONS: build(rule, fn, BG.rule_signature(fn), f"AC_BB5BombRule function signature {fn}")
    log("  override AssignPlayerToObjective: " + TOOLS.override_function(rule, "AssignPlayerToObjective"))
    compile_report(rule, "AC_BB5BombRule (events)")
    # pass 3: logic
    build(rule, "EventGraph", BG.rule_logic(), "AC_BB5BombRule logic")
    build(rule, "AssignPlayerToObjective", BG.rule_assign(), "AC_BB5BombRule AssignPlayerToObjective override")
    for fn in BG.RULE_FUNCTIONS: build(rule, fn, BG.RULE_FN_BODIES[fn](), f"AC_BB5BombRule function {fn}")
    compile_report(rule, "AC_BB5BombRule (logic)")
    EAL.save_loaded_asset(rule)

    # Each transaction owns its player and delegates. An ordinary Actor has a real world
    # context and lifespan; it adds no indexed properties to the retargeted GameMode.
    request = make_blueprint(BB5_DIR, "BP_BB5TeamRequest", unreal.Actor)
    add_var(request, "Subject", pin("object", unreal.BodycamPlayerState))
    add_var(request, "HostId", pin("string")); add_var(request, "SubjectId", pin("string"))
    add_var(request, "Rule", pin("object", gen_class(rule)))
    add_var(request, "Deadline", pin("int")); add_var(request, "Phase", pin("int"))
    compile_report(request, "BP_BB5TeamRequest (vars)")
    build(request, "EventGraph", BG.team_request_events(), "BP_BB5TeamRequest events")
    compile_report(request, "BP_BB5TeamRequest (events)")
    build(request, "EventGraph", BG.team_request_logic(), "BP_BB5TeamRequest logic")
    compile_report(request, "BP_BB5TeamRequest (logic)")
    EAL.save_loaded_asset(request)

    start_request = make_blueprint(BB5_DIR, "BP_BB5StartRequest", unreal.Actor)
    add_var(start_request, "Presence", pin("bool"))
    add_var(start_request, "Rule", pin("object", gen_class(rule)))
    for name in ("MatchKey", "Snapshot"):
        add_var(start_request, name, pin("string"))
    for name in ("Deadline", "Phase", "Epoch"):
        add_var(start_request, name, pin("int"))
    compile_report(start_request, "BP_BB5StartRequest (vars)")
    build(start_request, "EventGraph", BG.start_request_events(), "BP_BB5StartRequest events")
    compile_report(start_request, "BP_BB5StartRequest (events)")
    build(start_request, "EventGraph", BG.start_request_logic(), "BP_BB5StartRequest logic")
    compile_report(start_request, "BP_BB5StartRequest (logic)")
    EAL.save_loaded_asset(start_request)
    migration_request = make_blueprint(BB5_DIR, "BP_BB5MigrationRequest", unreal.Actor)
    add_var(migration_request, "Rule", pin("object", gen_class(rule)))
    add_var(migration_request, "MatchKey", pin("string"))
    add_var(migration_request, "Epoch", pin("int"))
    add_var(migration_request, "Activate", pin("bool"))
    build(migration_request, "EventGraph", MIGRATION.request_events(), "migration request events")
    compile_report(migration_request, "migration request signatures")
    # Passive observer state lives on ordinary Actors, preserving GM_BB5's indexed CDO layout.
    manager = make_blueprint(BB5_DIR, "BP_CHCombatManager", unreal.Actor)
    observer = make_blueprint(BB5_DIR, "BP_CHCombatObserver", unreal.Actor)
    compile_report(manager, "BP_CHCombatManager (shell)")
    compile_report(observer, "BP_CHCombatObserver (shell)")
    request = make_blueprint(BB5_DIR, "BP_CHCombatRequest", unreal.Actor)
    add_var(request, "Manager", pin("object", gen_class(manager))); add_var(request, "Attempt", pin("int"))
    build(request, "EventGraph", TRANSPORT.request_events(), "combat request events")
    compile_report(request, "BP_CHCombatRequest (events)")
    for bp, specs in ((manager, COMBAT.GM_VARIABLES + TRANSPORT.VARIABLES), (observer, COMBAT.OBSERVER_VARIABLES)):
        for name, category, cls, container in specs:
            if not add_var(bp, name, pin(category, unreal.load_class(None, variant_name(cls)) if cls else None, container)):
                raise BuildFailed("combat variable " + name)
        compile_report(bp, bp.get_name() + " (vars)")
    build(manager, "EventGraph", COMBAT.gm_events(), "combat manager events")
    for name, signature, logic in COMBAT.FUNCTIONS:
        build(manager, name, signature(), "combat signature " + name)
    compile_report(manager, "BP_CHCombatManager (events)")
    build(observer, "EventGraph", COMBAT.observer_events(), "combat observer events")
    compile_report(observer, "BP_CHCombatObserver (events)")
    build(observer, "EventGraph", COMBAT.observer_logic(), "combat observer logic")
    compile_report(observer, "BP_CHCombatObserver (logic)")
    for name, signature, logic in COMBAT.FUNCTIONS:
        build(manager, name, logic(), "combat function " + name)
    build(manager, "EventGraph", COMBAT.gm_logic(), "combat manager logic")
    build(manager, "EventGraph", TRANSPORT.manager_logic(), "acknowledged batch transport")
    build(request, "EventGraph", TRANSPORT.request_logic(), "authenticated combat request")
    compile_report(request, "BP_CHCombatRequest (logic)")
    compile_report(manager, "BP_CHCombatManager (logic)")
    for bp in (manager, observer, request):
        layout(bp, bp.get_name())
        EAL.save_loaded_asset(bp)

    recovery = make_blueprint(BB5_DIR, "BP_BB5Recovery", unreal.Actor)
    add_var(recovery, "Rule", pin("object", gen_class(rule)))
    add_var(recovery, "Session", pin("string"))
    add_var(recovery, "SeedSession", pin("string"))
    add_var(recovery, "RestoreDeadline", pin("int"))
    for name in ("Rows", "Meta"):
        add_var(recovery, name, pin("string"))
    add_var(recovery, "HasCapture", pin("bool"))
    add_var(recovery, "SnapshotRosterValid", pin("bool"))
    add_var(recovery, "SnapshotIds", pin("string", None, "array"))
    add_var(recovery, "Parts", pin("string", None, "array"))
    for name in ("Epoch", "Sequence", "Ticks", "Score0", "Score1", "CapturedRound", "CapturedEpoch", "SeedEpoch", "Stage"):
        add_var(recovery, name, pin("int"))
    for name in ("Session", "Epoch", "Sequence"):
        if not TOOLS.set_variable_replicated(recovery, name, True, "OnRep_Sequence" if name == "Sequence" else ""):
            raise BuildFailed("recovery replication flag: " + name)
    TOOLS.ensure_function_graph(recovery, "OnRep_Sequence")
    TOOLS.ensure_function_graph(recovery, "BuildSnapshot")
    TOOLS.ensure_function_graph(recovery, "ApplySnapshot")
    compile_report(recovery, "recovery vars")
    build(recovery, "EventGraph", RECOVERY.events(), "recovery events")
    compile_report(recovery, "recovery signatures")
    restore_request = make_blueprint(BB5_DIR, "BP_BB5RestoreRequest", unreal.Actor)
    add_var(restore_request, "Manager", pin("object", gen_class(recovery)))
    add_var(restore_request, "Consumed", pin("bool"))
    for name in ("Meta", "Rows", "Session"):
        add_var(restore_request, name, pin("string"))
    for name in ("Stage", "Epoch", "Deadline"):
        add_var(restore_request, name, pin("int"))
    build(restore_request, "EventGraph", RESTORE.request_events(), "restore request signatures")
    compile_report(restore_request, "restore request signatures")
    # Recovery's bounded failure path calls the shared authored session exit.
    # Create its signature before compiling graphs that reference it.
    gm = make_blueprint("/Game/GM/Gamemode", "GM_BB5", gen_class(parent))
    add_component(gm, gen_class(rule), "BB5BombRule")
    compile_report(gm, "GM_BB5 (shell)")
    build(gm, "EventGraph", BG.gm_events(), "GM_BB5 events")
    compile_report(gm, "GM_BB5 signatures")
    build(recovery, "OnRep_Sequence", RECOVERY.on_rep(), "recovery RepNotify")
    build(recovery, "BuildSnapshot", RECOVERY.snapshot(), "coherent round snapshot")
    build(recovery, "ApplySnapshot", RESTORE.apply_snapshot(), "restore native round fields")
    build(recovery, "EventGraph", RECOVERY.logic(), "recovery host tick")
    build(recovery, "EventGraph", RECOVERY.pulse_logic(), "recovery participant pulse")
    build(recovery, "EventGraph", RECOVERY.checkpoint_logic(), "durable checkpoint transport")
    build(recovery, "EventGraph", RESTORE.warmup_event(), "hold warmup for restore validation")
    build(restore_request, "EventGraph", RESTORE.request_logic(), "restore acknowledgement")
    compile_report(restore_request, "restore request logic")
    EAL.save_loaded_asset(restore_request)
    cdo = unreal.get_default_object(gen_class(recovery))
    cdo.set_editor_property("replicates", True)
    cdo.set_editor_property("always_relevant", True)
    cdo.set_editor_property("net_update_frequency", 5.0)
    compile_report(recovery, "recovery logic")
    EAL.save_loaded_asset(recovery)

    # ---------- GM_BB5 ----------
    log("  override ShouldSpawnBots: " + TOOLS.override_function(gm, "ShouldSpawnBots"))
    # The player-count gate. The override graph has to be CREATED from the parent's signature first, or BuildGraph's
    # FindGraph(create=true) makes a plain new function of the same name whose result node has no ReturnValue pin -
    # which is exactly what the first run of this did, and it fails loudly rather than shipping a dead override.
    log("  override MinimalPlayerCountMeetCriteria: " + TOOLS.override_function(gm, "MinimalPlayerCountMeetCriteria"))
    log("  override PlayerCountIsFull: " + TOOLS.override_function(gm, "PlayerCountIsFull"))
    log("  override FindPlayerStart: " + TOOLS.override_function(gm, "FindPlayerStart"))
    log("  override ChoosePlayerStart: " + TOOLS.override_function(gm, "ChoosePlayerStart"))
    compile_report(gm, "GM_BB5 (events)")
    build(migration_request, "EventGraph", MIGRATION.request_logic(), "migration request logic")
    compile_report(migration_request, "migration request logic")
    EAL.save_loaded_asset(migration_request)
    build(gm, "EventGraph", BG.gm_logic(), "GM_BB5 logic")
    build(gm, "ShouldSpawnBots", BG.gm_shouldspawnbots(), "GM_BB5 ShouldSpawnBots override")
    # Ranked starts require server approval for the exact current roster and teams.
    build(gm, "MinimalPlayerCountMeetCriteria", BG.gm_min_players(), "GM_BB5 minimum-player gate")
    build(gm, "PlayerCountIsFull", BG.gm_player_full(), "GM_BB5 full-lobby gate")
    build(gm, "FindPlayerStart", BG.gm_findplayerstart(), "GM_BB5 FindPlayerStart override")
    build(gm, "ChoosePlayerStart", BG.gm_chooseplayerstart(), "GM_BB5 ChoosePlayerStart override")
    compile_report(gm, "GM_BB5 (logic)")
    cdo = unreal.get_default_object(gen_class(gm))
    try:
        cdo.set_editor_property("config_data_asset", da); log("  GM_BB5 class default ConfigDataAsset -> DA_BB5")
    except Exception as e:
        warn(f"  could not set ConfigDataAsset: {e}")
    compile_report(gm, "GM_BB5 (final)")
    for bp, label in ((rule, "AC_BB5BombRule"), (gm, "GM_BB5")): layout(bp, label)
    EAL.save_loaded_asset(gm)
    log("stage 3 (BB5) done")
# ----------------------------------------------------------------------------------------------------------------------
def stage4_dom(parent):
    """Domination 10v10 (Sam, 2026-09-14). Three control points spawned from the map's own PlayerStarts (there are no
    objective markers in the stock levels), hold-to-own capture, +1 per owned point every 4 s through the game's own
    BodycamCaptureRuleSetComponent, and spawns that follow the points a team holds with the fixed-side pool as the
    fallback. Graphs: dom_graphs.py. Design and the Hardpoint disassembly behind it: docs/domination.md."""
    make_drone_cooldown_ge(DOM_DIR, "GE_DOM_DroneCooldown", DG.DRONE_COOLDOWN_FACTOR)
    da = make_config_asset("DA_DOM", {"round_warmup_duration": 6.0, "respawn_delay": 7.0}, {"score_limit": 200, "max_phases": 0},
                           {"team_max_size": 10, "max_players": 20, "team_switch_interval": 0}, "score limit 200, 10 per team, 20 players, respawn 7 s")
    hud = EAL.load_asset("/Game/MenuSystemPro/INGAME/HUD_Dot")      # the stand-in stage 2 creates (referenced, never shipped)
    for cue in DG.SND_ALL:   # the graphs' cue paths -> stubs at the same paths (never drift). The two capture cues already
        make_stub_cue(cue.split(".")[0])   # exist from stage 2; the contest cue (Error_Sound_2_Cue) is new to this mode.

    # the GM shell comes first: BP_DOM_Point keeps a typed reference back to it
    gm = make_blueprint("/Game/GM/Gamemode", "GM_DOM", gen_class(parent))
    add_component(gm, unreal.BodycamCaptureRuleSetComponent, "BodycamCaptureRuleSet")
    compile_report(gm, "GM_DOM (shell)")

    # ---------- pass 1: BP_DOM_Point components, variables, replication ----------
    point = make_blueprint(DOM_DIR, "BP_DOM_Point", unreal.Actor)
    add_component(point, unreal.SphereComponent, "Trigger")
    add_component(point, unreal.StaticMeshComponent, "Pad")
    add_component(point, unreal.BodycamWidgetComponent, "Dot")
    log("  Dot defaults: " + TOOLS.set_widget_component_defaults(point, "Dot", gen_class(hud), True))
    add_var(point, "PointIndex", pin("byte"))        # 1 = A, 2 = B, 3 = C
    add_var(point, "OwnerTeam", pin("int"))          # -1 = neutral
    add_var(point, "CapTeam", pin("int"))            # -1 = nobody is capturing
    add_var(point, "Progress", pin("float"))         # 0 .. CAPTURE_TIME seconds
    add_var(point, "bContested", pin("bool"))
    for v in ("LastEvent", "DotState"): add_var(point, v, pin("byte"))
    for v in ("EventTeam", "EventSeq", "CountA", "CountB", "PresentTeam", "PresentCount"): add_var(point, v, pin("int"))
    add_var(point, "GM", pin("object", gen_class(gm)))
    add_var(point, "DotLabel", pin("string"))        # the marker text, so RefreshDot only re-creates it when it changed
    add_var(point, "LastLocalPawn", pin("object", unreal.Pawn))   # HUD_Dot stores ControlledPawn once: a respawn needs a new marker
    for v in ("PointIndex", "CapTeam", "Progress", "bContested", "LastEvent", "EventTeam"): replicate(point, v)
    replicate(point, "OwnerTeam", "OnRep_OwnerTeam"); replicate(point, "EventSeq", "OnRep_EventSeq")
    for fn in ("OnRep_OwnerTeam", "OnRep_EventSeq"): log(f"  {fn} graph: " + TOOLS.ensure_function_graph(point, fn))
    compile_report(point, "BP_DOM_Point (vars)")

    # ---------- pass 1b: GM_DOM variables (CTF's exact set, because CTF's team/side/spawn graphs are reused verbatim) ----------
    for v in ("TeamA", "TeamB"): add_var(gm, v, pin("int"))
    for v in ("bTeamsReady", "bSidesReady", "bOccupied", "bPointsPlaced"): add_var(gm, v, pin("bool"))
    for v in ("Base1Starts", "Base2Starts", "AllStarts", "SpawnCandidates", "Pawns", "EnemyPawns"): add_var(gm, v, pin("object", unreal.Actor, "array"))
    add_var(gm, "KnownTeamIds", pin("int", None, "array"))
    for v in ("Center1", "Center2", "Axis", "Anchor1", "Anchor2", "ZoneHome1", "ZoneHome2", "ZoneHome3"): add_var(gm, v, pin("struct", unreal.Vector.static_struct()))
    for v in ("AxisLenSq", "TmpBest", "TmpDist"): add_var(gm, v, pin("float"))
    add_var(gm, "TmpLocs", pin("struct", unreal.Vector.static_struct(), "array"))
    for v in ("ChosenStart", "TmpBestActor", "PickedStart"): add_var(gm, v, pin("object", unreal.Actor))
    for v in ("Point1", "Point2", "Point3"): add_var(gm, v, pin("object", gen_class(point)))
    add_var(gm, "Points", pin("object", gen_class(point), "array"))
    compile_report(gm, "GM_DOM (vars)")

    # ---------- pass 2: custom events + function signatures, so every graph can call every other ----------
    build(point, "EventGraph", DG.point_events(), "BP_DOM_Point events")
    build(gm, "EventGraph", DG.gm_events(), "GM_DOM events")
    for fn in DG.GM_FUNCTIONS: build(gm, fn, DG.gm_signature(fn), f"GM_DOM function signature {fn}")
    log("  override FindPlayerStart: " + TOOLS.override_function(gm, "FindPlayerStart"))
    log("  override ChoosePlayerStart: " + TOOLS.override_function(gm, "ChoosePlayerStart"))
    for bp, label in ((point, "BP_DOM_Point (events)"), (gm, "GM_DOM (events)")): compile_report(bp, label)

    # ---------- pass 3: logic ----------
    build(point, "EventGraph", DG.point_logic(), "BP_DOM_Point logic")
    build(point, "OnRep_OwnerTeam", DG.point_onrep_ownerteam(), "BP_DOM_Point OnRep_OwnerTeam")
    build(point, "OnRep_EventSeq", DG.point_onrep_eventseq(), "BP_DOM_Point OnRep_EventSeq")
    build(gm, "EventGraph", DG.gm_logic(), "GM_DOM logic")
    for fn in DG.GM_FUNCTIONS: build(gm, fn, DG.GM_FN_BODIES[fn](), f"GM_DOM function {fn}")
    build(gm, "FindPlayerStart", DG.gm_findplayerstart(), "GM_DOM FindPlayerStart override")
    build(gm, "ChoosePlayerStart", DG.gm_chooseplayerstart(), "GM_DOM ChoosePlayerStart override")
    for bp, label in ((point, "BP_DOM_Point (logic)"), (gm, "GM_DOM (logic)")): compile_report(bp, label)

    # ---------- class defaults ----------
    cdo = unreal.get_default_object(gen_class(point))
    for prop, val in (("replicates", True), ("replicate_movement", True), ("always_relevant", True), ("OwnerTeam", -1), ("CapTeam", -1)):
        try: cdo.set_editor_property(prop, val); log(f"  BP_DOM_Point default {prop} = {val}")
        except Exception as e: warn(f"  BP_DOM_Point: could not set {prop}: {e}")
    gcdo = unreal.get_default_object(gen_class(gm))
    try:
        gcdo.set_editor_property("config_data_asset", da); log("  GM_DOM class default ConfigDataAsset -> DA_DOM")
    except Exception as e:
        warn(f"  could not set ConfigDataAsset: {e}")
    for bp, label in ((point, "BP_DOM_Point (final)"), (gm, "GM_DOM (final)")): compile_report(bp, label)
    for bp, label in ((point, "BP_DOM_Point"), (gm, "GM_DOM")): layout(bp, label)
    for bp in (point, gm): EAL.save_loaded_asset(bp)
    log("stage 4 (Domination) done")



def make_bodycam_gi_stub():
    """Create the private GameInstance surface used by both match and lobby graphs.

    GM_BB5 reads Search String for the report capability, so this stand-in must exist before
    stage3 compiles that graph.  It is an import-only compile dependency and is never shipped.
    """
    gi = make_blueprint(LG.GI_PKG.rsplit("/", 1)[0], "BodycamGI", unreal.GameInstance)
    add_var(gi, "Selected Level Name", ("name", None, False))
    add_var(gi, "Session Name", ("string", None, False))
    add_var(gi, "SessionToJoin (Client)", ("string", None, False))
    add_var(gi, "Search String", ("string", None, False))
    add_var(gi, "MostFillServer", ("int", None, False))
    for _fn in ("GetCurrentLevel", "UpdateGamemode", "HostPartyInLobby",
                "UpdateSessionAndTravelToMap"):
        build(gi, _fn, LG.gi_stub_signature(_fn),
              "BodycamGI stand-in function signature %s" % _fn)
    compile_report(gi, "BodycamGI stand-in"); EAL.save_loaded_asset(gi)
    return gi


def stage5_lobby(gi):
    """GM_CHLobby: the gamemode that runs while a player stands in the shooting range.

    The lobby level's World Settings select GM_Host_C; our pak repoints them at this class, so this
    is where Competitive's autojoin will live (docs/autojoin.md). Today its BeginPlay does one
    thing: call the parent, then fire a single probe.

    THE STAND-IN. GM_Host_C is one of the game's Blueprints, so it cannot be picked as a parent
    here. We create an EMPTY GM_Host at the game's own path - parented to AGameModeBase, which is
    what the real one derives from (verified by dumping the cooked class) - and make GM_CHLobby its
    child. The cooked child then imports /Game/GM/GM_Host.GM_Host_C as its super and the REAL class
    answers to that at runtime. Same trick as BB5's Bombe and BP_InventoryComponent.
    **The stand-in is never shipped**; the lobby pak carries GM_CHLobby and the level, nothing else.

    NO VARIABLES, NO CHANGED DEFAULTS. A cooked CDO is written by property index across the whole
    hierarchy: the stand-in has 0 properties and the real GM_Host_C has 12, so any stored default
    would land in the wrong slot. Storing nothing is what makes the mismatch harmless.
    """
    host = make_blueprint(LG.HOST_PKG.rsplit("/", 1)[0], "GM_Host", unreal.GameModeBase)
    build(host, "EventGraph", LG.host_stub_events(), "GM_Host stand-in ReceiveBeginPlay")
    compile_report(host, "GM_Host stand-in"); EAL.save_loaded_asset(host)

    gm = make_blueprint("/Game/GM/Gamemode", "GM_CHLobby", gen_class(host))
    compile_report(gm, "GM_CHLobby (shell)")
    # Two passes, the same shape the other stages use: the custom events have to EXIST before the
    # logic pass binds them, because a "createevent" node resolves its target by name.
    build(gm, "EventGraph", LG.chlobby_events(), "GM_CHLobby response events")
    compile_report(gm, "GM_CHLobby (events)")
    build(gm, "EventGraph", LG.chlobby_logic(), "GM_CHLobby BeginPlay probe")
    compile_report(gm, "GM_CHLobby (logic)")
    # the rule above, asserted rather than trusted: a variable added here corrupts the CDO at runtime
    own = [v.get_editor_property("variable_name") for v in (BEL.get_blueprint_variables(gm) or [])] \
        if hasattr(BEL, "get_blueprint_variables") else []
    if own:
        warn(f"  GM_CHLobby declares variables {own} - it must declare NONE (see docs/autojoin.md)")
    layout(gm, "GM_CHLobby")
    EAL.save_loaded_asset(gm)

    # ----------------------------------------------------------------------------------------
    # GM_CHPeek: the measurement class, built alongside and NEVER shipped in the same pak.
    #
    # It exists because GM_CHLobby travels and GM_CHPeek does not. No online or lobby call may be
    # outstanding when OpenLevel fires - chlobby-29 crashed that way, the FindLobbies still inside
    # the EOS SDK when OpenLevel destroyed the world that owned it - so the diagnostic search
    # cannot share a build with the host arm at any price. This class opens no level at all.
    #
    # Building it here costs nothing and ships nothing: the pak tool picks ONE class, so
    #     build_lobby_override.py --class-pkg /Game/GM/Gamemode/GM_CHPeek --class GM_CHPeek_C
    # produces the peek pak and the existing GM_CHLobby invocation still produces the host pak.
    # The two are never installed together. See docs/autojoin-peek.md.
    peek = make_blueprint("/Game/GM/Gamemode", "GM_CHPeek", gen_class(host))
    compile_report(peek, "GM_CHPeek (shell)")
    # two passes, same reason as above: `createevent` resolves its target by NAME, so the custom
    # events have to exist before the logic pass binds them
    build(peek, "EventGraph", LG.chpeek_events(), "GM_CHPeek response events")
    compile_report(peek, "GM_CHPeek (events)")
    build(peek, "EventGraph", LG.chpeek_logic(), "GM_CHPeek BeginPlay peek")
    compile_report(peek, "GM_CHPeek (logic)")
    # rule 3 applies here exactly as it does to GM_CHLobby: same stand-in parent, same cooked CDO
    # written by property index, so a variable here corrupts it at runtime just the same
    own = [v.get_editor_property("variable_name") for v in (BEL.get_blueprint_variables(peek) or [])] \
        if hasattr(BEL, "get_blueprint_variables") else []
    if own:
        warn(f"  GM_CHPeek declares variables {own} - it must declare NONE (see docs/autojoin.md)")
    layout(peek, "GM_CHPeek")
    EAL.save_loaded_asset(peek)

    # ----------------------------------------------------------------------------------------
    # GM_CHJoin: the joiner. Packed for the machines that are NOT hosting, so like GM_CHPeek it
    # never shares a build with the host arm:
    #     build_lobby_override.py --class-pkg /Game/GM/Gamemode/GM_CHJoin --class GM_CHJoin_C
    # It identifies its host by CH_MATCH - our own tag, measured written and persisting on the
    # shipping build - rather than by PlayerSteamId, whose format has never been compared. See
    # docs/autojoin-joiner.md.
    join = make_blueprint("/Game/GM/Gamemode", "GM_CHJoin", gen_class(host))
    compile_report(join, "GM_CHJoin (shell)")
    build(join, "EventGraph", LG.chjoin_events(), "GM_CHJoin response events")
    compile_report(join, "GM_CHJoin (events)")
    build(join, "EventGraph", LG.chjoin_logic(), "GM_CHJoin BeginPlay join")
    compile_report(join, "GM_CHJoin (logic)")
    own = [v.get_editor_property("variable_name") for v in (BEL.get_blueprint_variables(join) or [])]         if hasattr(BEL, "get_blueprint_variables") else []
    if own:
        warn(f"  GM_CHJoin declares variables {own} - it must declare NONE (see docs/autojoin.md)")
    layout(join, "GM_CHJoin")
    EAL.save_loaded_asset(join)

    # ----------------------------------------------------------------------------------------
    # GM_CHTJoin: the stranger-join TEST. Built alongside, shipped in its own pak, never with the
    # others - the pak tool picks one class:
    #     build_lobby_override.py --class-pkg /Game/GM/Gamemode/GM_CHTJoin --class GM_CHTJoin_C
    # It joins the first public, idle lobby a search returns, to prove the whole find -> read ->
    # pick -> join chain works between strangers. See docs/autojoin-joiner.md.
    tj = make_blueprint("/Game/GM/Gamemode", "GM_CHTJoin", gen_class(host))
    compile_report(tj, "GM_CHTJoin (shell)")
    build(tj, "EventGraph", LG.chtjoin_events(), "GM_CHTJoin response events")
    compile_report(tj, "GM_CHTJoin (events)")
    build(tj, "EventGraph", LG.chtjoin_logic(), "GM_CHTJoin BeginPlay join")
    compile_report(tj, "GM_CHTJoin (logic)")
    own = [v.get_editor_property("variable_name") for v in (BEL.get_blueprint_variables(tj) or [])]         if hasattr(BEL, "get_blueprint_variables") else []
    if own:
        warn(f"  GM_CHTJoin declares variables {own} - it must declare NONE (see docs/autojoin.md)")
    layout(tj, "GM_CHTJoin")
    EAL.save_loaded_asset(tj)
    log("stage 5 (lobby) done")
# ----------------------------------------------------------------------------------------------------------------------
def main():
    log("make_blueprints.py starting; engine " + unreal.SystemLibrary.get_engine_version())
    ok = False
    try:
        pre_clean()
        parent, da, gm = stage1()
        stage2(parent, da, gm)
        gi = make_bodycam_gi_stub()
        stage3_bb5(parent)
        stage3_bb5(parent, "BB1")
        stage4_dom(parent)
        stage5_lobby(gi)
        ok = True
    except BuildFailed as e:
        warn(f"BUILD FAILED: {e}")
    except Exception:
        warn("EXCEPTION:\n" + traceback.format_exc())
    finally:
        # the last line of the summary is the verdict: 2_make_blueprints.bat / 3_cook.bat / the pak builder all look for "RESULT: OK"
        log(f"summary written to {SUMMARY}")
        _emit("RESULT: OK" if ok else "RESULT: FAILED (see the WARNING/ERROR lines above; do not cook, do not build a pak)")

main()
