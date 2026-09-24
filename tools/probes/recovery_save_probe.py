"""Throwaway proof: use run_save_probe.py for fresh, ordered UE5.5 processes.

This tests real Unreal SaveGame serialization with a synthetic, nonempty HMS
structure. It does NOT test Bodycam's native capture, restore, or host migration.
Uses only the existing Unreal 5.5 runtime; no additional packages.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import traceback

import unreal


PHASE = os.environ.get("RECOVERY_PROBE_PHASE")
RUN_ID = os.environ.get("RECOVERY_SAVE_RUN", "")
if PHASE not in ("capture", "reload") or not re.fullmatch(r"[a-f0-9]{12}", RUN_ID):
    raise ValueError("Explicit capture/reload phase and fresh 12-hex RECOVERY_SAVE_RUN required")
ROOT = Path(unreal.Paths.project_dir()).resolve()
OUT = ROOT / "Saved" / "RecoveryProof" / ("save-" + RUN_ID)
ASSET = "/Game/RecoveryProof/BP_RecoveryCheckpoint"
SLOT = "LightsOutRecoveryProof_" + RUN_ID


def ue_type(native):
    key = native.replace("_", "").lower()
    names = [n for n in dir(unreal) if n.replace("_", "").lower() == key]
    if len(names) != 1:
        raise RuntimeError(f"Cannot resolve {native}: {[n for n in dir(unreal) if 'hms' in n.lower()]}")
    return getattr(unreal, names[0])


def prop(obj, name):
    return obj.get_editor_property(name)


def assign(obj, **values):
    for name, value in values.items():
        obj.set_editor_property(name, value)
    return obj


def buffer(data):
    return assign(ue_type("HMS_ByteBuffer")(), buffer=data)


def buffers(**values):
    return assign(ue_type("HMS_StringToByteBufferMap")(), map={k: buffer(v) for k, v in values.items()})


def make_sample():
    # Values deliberately include embedded zeroes, high bytes, Unicode strings,
    # 64-bit actor identifiers and nested component/property maps.
    actor = assign(
        ue_type("HMS_ActorSave")(),
        actor_class=unreal.Actor.static_class(),
        actor_transform=unreal.Transform(
            location=unreal.Vector(123.25, -456.5, 78.75),
            rotation=unreal.Rotator(17, 31, -9),
            scale=unreal.Vector(1.5, 2.0, 0.5),
        ),
        actor_id=9007199254740993,
        priority=17,
        property_buffers=buffers(Health=[0, 1, 127, 128, 255], Label=[65, 0, 66, 0]),
        components_property_buffers={"ObjectiveTimer": buffers(Remaining=[23, 42, 0, 255])},
    )
    reference = assign(
        ue_type("HMS_ActorReference")(), actor_id=9007199254740993,
        actor_class=unreal.Actor.static_class(),
    )
    assert prop(actor, "actor_id") == 9007199254740993, "actor ID lost precision on assignment"
    assert prop(reference, "actor_id") == 9007199254740993, "reference ID lost precision on assignment"
    player = assign(
        ue_type("HMS_PlayerSave")(), player_id="synthetic-player-A", priority=3,
        possessed_pawn_actor_reference=reference,
        player_state_property_buffers=buffers(Score=[5, 0, 0, 0], Team=[1]),
        player_controller_property_buffers=buffers(Controller=[0, 254, 255]),
    )
    return assign(
        ue_type("HMS_GameSave")(), level_name="RecoveryProof_место_地图",
        player_saves=[player], actor_saves=[actor],
        game_mode_property_buffers=buffers(Round=[9, 0, 0, 0], Scores=[5, 3]),
        game_mode_components_property_buffers={"Rules": buffers(Sides=[1, 0], Timer=[120])},
        game_state_property_buffers=buffers(Phase=[1, 2, 3, 0]),
        custom_game_save_data_object_property_buffers=buffers(Custom=[255, 0, 128, 64], Challenge=list(bytes.fromhex(RUN_ID))),
    )


def map_data(value):
    return {str(key): list(prop(data, "buffer")) for key, data in prop(value, "map").items()}


def describe(value):
    actors = []
    for actor in prop(value, "actor_saves"):
        transform = prop(actor, "actor_transform")
        position = prop(transform, "translation")
        rotation = prop(transform, "rotation")
        scale = prop(transform, "scale3d")
        actors.append({
            "class": prop(actor, "actor_class").get_path_name(),
            "id": prop(actor, "actor_id"), "priority": prop(actor, "priority"),
            "transform": [[position.x, position.y, position.z],
                          [rotation.x, rotation.y, rotation.z, rotation.w],
                          [scale.x, scale.y, scale.z]],
            "properties": map_data(prop(actor, "property_buffers")),
            "components": {str(k): map_data(v) for k, v in prop(actor, "components_property_buffers").items()},
        })
    players = []
    for player in prop(value, "player_saves"):
        reference = prop(player, "possessed_pawn_actor_reference")
        players.append({
            "id": prop(player, "player_id"), "priority": prop(player, "priority"),
            "pawn_id": prop(reference, "actor_id"),
            "pawn_class": prop(reference, "actor_class").get_path_name(),
            "state": map_data(prop(player, "player_state_property_buffers")),
            "controller": map_data(prop(player, "player_controller_property_buffers")),
        })
    return {
        "level": prop(value, "level_name"), "players": players, "actors": actors,
        "mode": map_data(prop(value, "game_mode_property_buffers")),
        "mode_components": {str(k): map_data(v) for k, v in prop(value, "game_mode_components_property_buffers").items()},
        "state": map_data(prop(value, "game_state_property_buffers")),
        "custom": map_data(prop(value, "custom_game_save_data_object_property_buffers")),
    }


def create_wrapper():
    eal = unreal.EditorAssetLibrary
    if PHASE == "reload":
        bp = eal.load_asset(ASSET)
        assert bp, "captured wrapper asset missing"
        return unreal.BlueprintEditorLibrary.generated_class(bp)
    if eal.does_asset_exist(ASSET):
        bp = eal.load_asset(ASSET)
        for name in ("Payload", "ProbeVersion", "CaptureProcess", "MatchKey"):
            unreal.BlueprintEditorLibrary.set_blueprint_variable_instance_editable(bp, name, True)
        report = unreal.BodycamMirrorTools.compile_and_report(bp)
        assert not any(word in report for word in ("ERROR", "BS_Error", "WARNING")), report
        assert eal.save_loaded_asset(bp)
        (OUT / "wrapper-compile.txt").write_text(f"Run: {RUN_ID}\n" + report + "\nRESULT: OK\n", encoding="utf-8")
        return unreal.BlueprintEditorLibrary.generated_class(bp)
    factory = unreal.BlueprintFactory()
    factory.set_editor_property("parent_class", unreal.SaveGame)
    bp = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
        "BP_RecoveryCheckpoint", "/Game/RecoveryProof", unreal.Blueprint, factory,
    )
    assert bp, "wrapper creation failed"
    tools = unreal.BodycamMirrorTools
    assert tools.add_variable(bp, "Payload", "struct", ue_type("HMS_GameSave").static_struct(), False)
    assert tools.add_variable(bp, "ProbeVersion", "int", None, False)
    assert tools.add_variable(bp, "CaptureProcess", "int", None, False)
    assert tools.add_variable(bp, "MatchKey", "string", None, False)
    for name in ("Payload", "ProbeVersion", "CaptureProcess", "MatchKey"):
        assert tools.set_variable_save_game(bp, name), name
        unreal.BlueprintEditorLibrary.set_blueprint_variable_instance_editable(bp, name, True)
    report = tools.compile_and_report(bp)
    if any(word in report for word in ("ERROR", "BS_Error", "WARNING")):
        raise RuntimeError(report)
    assert eal.save_loaded_asset(bp), "wrapper save failed"
    (OUT / "wrapper-compile.txt").write_text(f"Run: {RUN_ID}\n" + report + "\nRESULT: OK\n", encoding="utf-8")
    return unreal.BlueprintEditorLibrary.generated_class(bp)


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    stats = unreal.GameplayStatics
    cls = create_wrapper()
    defaults = unreal.get_default_object(cls)
    assert prop(defaults, "ProbeVersion") == 0 and prop(defaults, "CaptureProcess") == 0
    assert prop(defaults, "MatchKey") == ""
    assert describe(prop(defaults, "Payload")) == describe(ue_type("HMS_GameSave")()), "payload leaked into asset defaults"
    expected_path = OUT / "expected.json"
    path = ROOT / "Saved" / "SaveGames" / (SLOT + ".sav")
    if PHASE == "capture":
        assert not expected_path.exists() and not path.exists(), "capture run/slot already exists"
        payload = make_sample()
        save = stats.create_save_game_object(cls)
        assign(save, Payload=payload, ProbeVersion=1, CaptureProcess=os.getpid(), MatchKey="synthetic-" + RUN_ID)
        assert prop(save, "ProbeVersion") == 1
        assert prop(unreal.get_default_object(cls), "ProbeVersion") == 0, "instance altered class defaults"
        assert describe(prop(save, "Payload")) == describe(payload), "instance assignment changed payload"
        expected = {"data": describe(payload), "capture_pid": os.getpid(), "run_id": RUN_ID}
        assert stats.save_game_to_slot(save, SLOT, 0), "SaveGameToSlot returned false"
        assert stats.does_save_game_exist(SLOT, 0), "save not found after successful write"
        raw = path.read_bytes()
        assert raw.startswith(b"GVAS") and len(raw) > 16, "disk save missing or empty"
        expected.update(save_bytes=len(raw), save_sha256=hashlib.sha256(raw).hexdigest())
        expected_path.write_text(json.dumps(expected, indent=2, ensure_ascii=False), encoding="utf-8")
        result = {"ok": True, "phase": PHASE, "pid": os.getpid(), "run_id": RUN_ID, "scope": "synthetic HMS capture via real Unreal SaveGame"}
    elif PHASE == "reload":
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        capture = json.loads((OUT / "capture.json").read_text(encoding="utf-8"))
        assert expected["run_id"] == RUN_ID and capture["run_id"] == RUN_ID, "stale run manifest"
        assert capture["ok"] is True and capture["pid"] == expected["capture_pid"], "capture did not succeed"
        raw = path.read_bytes()
        assert len(raw) == expected["save_bytes"] == capture["save_bytes"], "checkpoint size changed"
        assert hashlib.sha256(raw).hexdigest() == expected["save_sha256"] == capture["save_sha256"], "checkpoint hash changed"
        assert expected["capture_pid"] != os.getpid(), "reload must use a separate process"
        assert prop(unreal.get_default_object(cls), "ProbeVersion") == 0, "capture leaked into asset defaults"
        save = stats.load_game_from_slot(SLOT, 0)
        assert save is not None, "LoadGameFromSlot returned None"
        assert save.get_class() == cls, "loaded wrong SaveGame class"
        assert prop(save, "ProbeVersion") == 1
        assert prop(save, "MatchKey") == "synthetic-" + RUN_ID, "checkpoint belongs to an older run"
        assert prop(save, "CaptureProcess") == expected["capture_pid"]
        actual = describe(prop(save, "Payload"))
        (OUT / "actual.json").write_text(json.dumps(actual, indent=2, ensure_ascii=False), encoding="utf-8")
        assert actual == expected["data"], "nested data changed across restart; compare expected/actual JSON"
        result = {"ok": True, "phase": PHASE, "run_id": RUN_ID, "capture_pid": expected["capture_pid"], "reload_pid": os.getpid(),
                  "scope": "synthetic HMS structure persisted across real Unreal process restart",
                  "native_bodycam_capture_or_restore_proven": False}
    else:
        raise ValueError("Unknown phase")
    result.update(save_bytes=len(raw), save_sha256=hashlib.sha256(raw).hexdigest())
    (OUT / (PHASE + ".json")).write_text(json.dumps(result, indent=2), encoding="utf-8")
    unreal.log("RECOVERY_PROOF " + json.dumps(result))


try:
    run()
except Exception:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / (PHASE + ".json")).write_text(json.dumps({"ok": False, "phase": PHASE, "run_id": RUN_ID, "error": traceback.format_exc()}, indent=2), encoding="utf-8")
    raise
