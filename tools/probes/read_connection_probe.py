"""Read owned engine probe SaveGames in UE5.5; RECOVERY_CONNECTION_RUN selects the report.

Native Unreal decoding verifies fields; raw file changes alone do not prove
replication or the claimed session identity. No external dependencies.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import traceback
import unreal

OUT = Path(os.environ["RECOVERY_CONNECTION_RUN"])


def run():
    result = json.loads((OUT / "run.json").read_text(encoding="utf-8"))
    assert result["network_ok"] is True, result
    assert re.fullmatch(r"[a-f0-9]{12}", result["run_id"]), "invalid run marker"
    expected = {
        ("healthy", "host"), ("healthy", "client1"), ("healthy", "client2"),
        ("client_stopped", "client1"),
        ("one_client_left", "host"), ("one_client_left", "client1"), ("one_client_left", "client2"),
        ("host_lost", "client2"), ("host_lost_stable", "client2"),
        ("different_session", "host"), ("different_session", "client1"),
    }
    keys = [(r["stage"], r["role"]) for r in result["records"]]
    assert len(keys) == len(expected) and set(keys) == expected, "missing or duplicate observations"
    saves = Path(unreal.Paths.project_saved_dir()).resolve() / "SaveGames"
    rows = []
    for record in result["records"]:
        assert record["slot"] == f"RecoveryProof_{result['run_id']}_{record['stage']}_{record['role']}", record
        for folder in (saves, OUT):
            raw = (folder / (record["slot"] + ".sav")).read_bytes()
            assert len(raw) == record["bytes"] and hashlib.sha256(raw).hexdigest() == record["sha256"], "snapshot changed"
        saved = unreal.GameplayStatics.load_game_from_slot(record["slot"], 0)
        assert saved, record
        row = {**record, **{name: saved.get_editor_property(name) for name in
                           ("Session", "Sequence", "SampleCount", "Authority", "LocalControllerValid", "World", "ObservationSlot")}}
        assert row["ObservationSlot"] == f"RecoveryProof_{result['run_id']}_{row['role']}", row
        assert row["Sequence"] > 0 and row["SampleCount"] >= 3, row
        assert row["World"] == "ConnectionMap", row
        assert row["Authority"] == (row["role"] == "host"), row
        assert row["LocalControllerValid"], row
        session = "B" if row["stage"] == "different_session" else "A"
        assert row["Session"] == f"RecoveryProof_{result['run_id']}_host:{session}", row
        rows.append(row)
    by = {(r["stage"], r["role"]): r for r in rows}
    assert by["one_client_left", "client2"]["Sequence"] > by["healthy", "client2"]["Sequence"]
    assert by["one_client_left", "client1"]["Sequence"] == by["client_stopped", "client1"]["Sequence"]
    assert result["surviving_client_alive_after_host_loss"]
    assert by["host_lost", "client2"]["Sequence"] == by["host_lost_stable", "client2"]["Sequence"]
    (OUT / "readback.json").write_text(json.dumps({"ok": True, "run_id": result["run_id"], "rows": rows,
        "scope": "real Unreal loopback replication and local SaveGame observation",
        "bodycam_gameplay_or_steam_verified": False}, indent=2), encoding="utf-8")


try:
    run()
except Exception:
    (OUT / "readback.json").write_text(json.dumps({"ok": False, "error": traceback.format_exc()}, indent=2), encoding="utf-8")
    raise
