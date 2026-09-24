"""Throwaway UE5.5 probe: run with UnrealEditor-Cmd -run=pythonscript -script=<this file>.

Builds an authored replicated actor and a local test map. Proves engine plumbing
only until these assets are exercised inside an actual private Bodycam match.
"""
import json
from pathlib import Path
import sys
import traceback
import unreal

ROOT = Path(unreal.Paths.project_dir()).resolve()
OUT = ROOT / "Saved" / "RecoveryProof"
sys.path.insert(0, str(ROOT / "Scripts"))
from ctf_graphs import G, SYS, MATH, GS_LIB, STR, ACTOR

FOLDER = "/Game/RecoveryProof"
ACTOR_PATH = FOLDER + "/BP_ConnectionPulse.BP_ConnectionPulse_C"
SAVE_PATH = FOLDER + "/BP_ConnectionObservation.BP_ConnectionObservation_C"
T = unreal.BodycamMirrorTools
E = unreal.EditorAssetLibrary
LOG = []


def checked(result):
    text = str(result)
    LOG.append(text)
    if "ERROR" in text or "BS_Error" in text or "WARNING" in text:
        raise RuntimeError(text)
    return result


def compile_bp(bp):
    checked(T.compile_and_report(bp))


def blueprint(name, parent):
    path = FOLDER + "/" + name
    if E.does_asset_exist(path):
        assert E.delete_asset(path)
    f = unreal.BlueprintFactory()
    f.set_editor_property("parent_class", parent)
    bp = unreal.AssetToolsHelpers.get_asset_tools().create_asset(name, FOLDER, unreal.Blueprint, f)
    assert bp
    return bp


def variable(bp, name, category, cls=None):
    assert T.add_variable(bp, name, category, cls, False), name


def build(bp, graph, data):
    checked(T.build_graph(bp, graph, data.json()))


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    save = blueprint("BP_ConnectionObservation", unreal.SaveGame)
    for name, category in (("Session", "string"), ("Sequence", "int"),
                           ("SampleCount", "int"), ("Authority", "bool"),
                           ("LocalControllerValid", "bool"), ("World", "string"), ("ObservationSlot", "string")):
        variable(save, name, category)
        assert T.set_variable_save_game(save, name)
    compile_bp(save)
    assert E.save_loaded_asset(save)

    pulse = blueprint("BP_ConnectionPulse", unreal.Actor)
    for name, category in (("Session", "string"), ("Sequence", "int"), ("SampleCount", "int")):
        variable(pulse, name, category)
    assert T.set_variable_replicated(pulse, "Session", True, "")
    assert T.set_variable_replicated(pulse, "Sequence", True, "OnRep_Sequence")
    checked(T.ensure_function_graph(pulse, "OnRep_Sequence"))
    compile_bp(pulse)
    event = G()
    event.event("begin", "ReceiveBeginPlay", ACTOR)
    event.custom("tick", "PulseTick")
    build(pulse, "EventGraph", event)
    compile_bp(pulse)

    g = G()
    g.event("begin", "ReceiveBeginPlay", ACTOR)
    g.selfnode("self")
    g.call("authority", ACTOR, "HasAuthority")
    g.branch("server")
    g.link(("authority.ReturnValue", "server.condition"))
    g.chain("begin", "server")
    g.call("cmd", SYS, "GetCommandLine")
    g.call("other_session", STR, "Contains", {"Substring": "ProofSession=B", "bUseCase": "true"})
    g.link(("cmd.ReturnValue", "other_session.SearchIn"))
    g.call("session_choice", MATH, "SelectString", {"A": ":B", "B": ":A"})
    g.link(("other_session.ReturnValue", "session_choice.bPickA"))
    # Replicate the owned host's run-specific marker. The client's own command
    # line is not evidence that it joined this run's server.
    g.call("host_slot_arg", STR, "Split", {"InStr": "-ProofSlot="})
    g.link(("cmd.ReturnValue", "host_slot_arg.SourceString"))
    g.call("host_slot", STR, "TrimTrailing")
    g.link(("host_slot_arg.RightS", "host_slot.SourceString"))
    g.call("full_session", STR, "Concat_StrStr")
    g.link(("host_slot.ReturnValue", "full_session.A"), ("session_choice.ReturnValue", "full_session.B"))
    g.set("session", "Session")
    g.link(("full_session.ReturnValue", "session.Session"))
    g.call("timer", SYS, "K2_SetTimer", {"FunctionName": "PulseTick", "Time": "1.0", "bLooping": "true"})
    g.link(("self.self", "timer.Object"))
    g.chain("server", "cmd", "session", "timer")
    g.existing("tick", "PulseTick")
    g.get("seq", "Sequence")
    g.call("increment", MATH, "Add_IntInt", {"B": "1"})
    g.link(("seq.Sequence", "increment.A"))
    g.set("set_seq", "Sequence")
    g.link(("increment.ReturnValue", "set_seq.Sequence"))
    g.chain("tick", "set_seq")
    build(pulse, "EventGraph", g)

    g = G()
    g.entry()
    g.get("session", "Session")
    g.call("has_session", STR, "NotEqual_StrStr", {"B": ""})
    g.link(("session.Session", "has_session.A"))
    g.branch("valid")
    g.link(("has_session.ReturnValue", "valid.condition"))
    g.chain("entry", "valid")
    g.get("count", "SampleCount")
    g.call("inc_count", MATH, "Add_IntInt", {"B": "1"})
    g.link(("count.SampleCount", "inc_count.A"))
    g.set("set_count", "SampleCount")
    g.link(("inc_count.ReturnValue", "set_count.SampleCount"))
    g.chain("valid", "set_count")
    g.call("create", GS_LIB, "CreateSaveGameObject", {"SaveGameClass": SAVE_PATH})
    g.cast("cast", SAVE_PATH)
    g.link(("create.ReturnValue", "cast.cast_object"))
    g.chain("set_count", "cmd", "world", "create", "cast")
    g.get("seq", "Sequence")
    g.call("authority", ACTOR, "HasAuthority")
    g.call("controller", GS_LIB, "GetPlayerController", {"PlayerIndex": "0"})
    g.call("controller_valid", SYS, "IsValid")
    g.link(("controller.ReturnValue", "controller_valid.Object"))
    g.call("world", GS_LIB, "GetCurrentLevelName", {"bRemovePrefixString": "true"})
    previous = "cast.cast_ok"
    for name, pin in (("Session", "session.Session"), ("Sequence", "seq.Sequence"),
                      ("SampleCount", "count.SampleCount"), ("Authority", "authority.ReturnValue"),
                      ("LocalControllerValid", "controller_valid.ReturnValue"), ("World", "world.ReturnValue"),
                      ("ObservationSlot", "slot.ReturnValue")):
        node = "save_" + name
        g.set(node, name, SAVE_PATH)
        g.link(("cast.cast_result", node + ".self"), (pin, node + "." + name), (previous, node + ".exec"))
        previous = node + ".then"
    g.call("cmd", SYS, "GetCommandLine")
    # Runner supplies this as its final argument. Unique per run and role;
    # readback requires the same marker, so an older writer cannot pass.
    g.call("slot_arg", STR, "Split", {"InStr": "-ProofSlot="})
    g.link(("cmd.ReturnValue", "slot_arg.SourceString"))
    g.call("slot", STR, "TrimTrailing")
    g.link(("slot_arg.RightS", "slot.SourceString"))
    g.call("write", GS_LIB, "SaveGameToSlot", {"UserIndex": "0"})
    g.link((previous, "write.exec"), ("cast.cast_result", "write.SaveGameObject"), ("slot.ReturnValue", "write.SlotName"))
    build(pulse, "OnRep_Sequence", g)
    compile_bp(pulse)
    cdo = unreal.get_default_object(unreal.BlueprintEditorLibrary.generated_class(pulse))
    for name, value in (("replicates", True), ("always_relevant", True), ("net_update_frequency", 10.0)):
        cdo.set_editor_property(name, value)
    compile_bp(pulse)
    assert E.save_loaded_asset(pulse)
    level = unreal.EditorLevelLibrary
    if E.does_asset_exist(FOLDER + "/ConnectionMap"):
        assert E.delete_asset(FOLDER + "/ConnectionMap")
    assert level.new_level(FOLDER + "/ConnectionMap")
    actor = level.spawn_actor_from_class(unreal.BlueprintEditorLibrary.generated_class(pulse), unreal.Vector(0, 0, 100))
    assert actor
    assert level.spawn_actor_from_class(unreal.PlayerStart, unreal.Vector(0, 0, 300))
    assert level.save_current_level()
    (OUT / "connection-build.txt").write_text("\n".join(LOG) + "\nRESULT: OK\n", encoding="utf-8")
    (OUT / "connection-build.json").write_text(json.dumps({"ok": True, "scope": "authored engine probe assets; not Bodycam runtime proof"}, indent=2), encoding="utf-8")


try:
    run()
except Exception:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "connection-build.txt").write_text("\n".join(LOG) + "\nERROR\n" + traceback.format_exc(), encoding="utf-8")
    raise
