"""Regression checks for probe evidence validation, not native game behavior."""
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/probes/read_connection_probe.py"
CASES = [
    ("healthy", "host", 20), ("healthy", "client1", 20), ("healthy", "client2", 20),
    ("client_stopped", "client1", 20),
    ("one_client_left", "host", 23), ("one_client_left", "client1", 20),
    ("one_client_left", "client2", 23),
    ("host_lost", "client2", 23), ("host_lost_stable", "client2", 23),
    ("different_session", "host", 17), ("different_session", "client1", 17),
]


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    run_id = "123456789abc"
    saves = tmp_path / "SaveGames"
    saves.mkdir()
    records, decoded = [], {}
    for stage, role, sequence in CASES:
        slot = f"RecoveryProof_{run_id}_{stage}_{role}"
        raw = f"decoded fixture for {slot}".encode()
        (saves / f"{slot}.sav").write_bytes(raw)
        (tmp_path / f"{slot}.sav").write_bytes(raw)
        records.append(dict(stage=stage, role=role, slot=slot,
                            sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)))
        session = "B" if stage == "different_session" else "A"
        decoded[slot] = dict(Session=f"RecoveryProof_{run_id}_host:{session}",
                             Sequence=sequence, SampleCount=4 if stage == "healthy" else 7,
                             Authority=role == "host", LocalControllerValid=True, World="ConnectionMap",
                             ObservationSlot=f"RecoveryProof_{run_id}_{role}")
    report = dict(ok=True, network_ok=True, run_id=run_id, records=records,
                  surviving_client_alive_after_host_loss=True)
    # This replaces only Unreal's decoder boundary. The real reader/validation,
    # file hashes, record set and final output are exercised unchanged.
    def load(slot, user):
        return SimpleNamespace(get_editor_property=lambda name: decoded[slot][name])
    monkeypatch.setitem(sys.modules, "unreal", SimpleNamespace(
        GameplayStatics=SimpleNamespace(load_game_from_slot=load),
        Paths=SimpleNamespace(project_saved_dir=lambda: str(tmp_path))))
    monkeypatch.setenv("RECOVERY_CONNECTION_RUN", str(tmp_path))
    def run():
        (tmp_path / "run.json").write_text(json.dumps(report), encoding="utf-8")
        return runpy.run_path(str(SCRIPT))
    return report, decoded, tmp_path, run


def test_complete_decoded_evidence_passes(evidence):
    _, _, folder, run = evidence
    run()
    assert json.loads((folder / "readback.json").read_text(encoding="utf-8"))["ok"] is True


@pytest.mark.parametrize("damage", ["missing_new_session", "duplicate", "modified_save", "wrong_hash"])
def test_incomplete_or_modified_evidence_is_rejected(evidence, damage):
    report, _, folder, run = evidence
    if damage == "missing_new_session":
        report["records"] = [r for r in report["records"] if r["stage"] != "different_session"]
    elif damage == "duplicate":
        report["records"].append(dict(report["records"][0]))
    elif damage == "modified_save":
        slot = report["records"][0]["slot"]
        (folder / "SaveGames" / f"{slot}.sav").write_bytes(b"changed since capture")
    else:
        report["records"][0]["sha256"] = "0" * 64
    with pytest.raises((AssertionError, ValueError)):
        run()
    assert json.loads((folder / "readback.json").read_text(encoding="utf-8"))["ok"] is False


@pytest.mark.parametrize("field,value", [("Session", "wrong-session"), ("ObservationSlot", "old-run")])
def test_wrong_session_or_old_run_is_rejected(evidence, field, value):
    report, decoded, _, run = evidence
    decoded[report["records"][0]["slot"]][field] = value
    with pytest.raises(AssertionError):
        run()


@pytest.fixture
def runner(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("probe_runner_audit", SCRIPT.with_name("run_connection_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT = tmp_path / "run"
    ticks = itertools.count()
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(module, "available_port", lambda: 12345)
    monkeypatch.setattr(module, "digest", lambda role: "same-file")
    monkeypatch.setattr(module, "changes", lambda *args: "changed-file")
    monkeypatch.setattr(module, "snapshot", lambda *args: None)
    def launch(*args):
        child = SimpleNamespace(pid=len(module.CHILDREN) + 1, ended=False)
        child.poll = lambda: 0 if child.ended else None
        child.terminate = lambda: setattr(child, "ended", True)
        child.wait = lambda **kwargs: 0
        module.CHILDREN.append(child)
        return child
    monkeypatch.setattr(module, "launch", launch)
    return module


def test_failed_native_readback_cannot_leave_success_report(runner, monkeypatch):
    def failed():
        raise RuntimeError("native reader failed")
    monkeypatch.setattr(runner, "readback", failed)
    with pytest.raises(RuntimeError, match="native reader failed"):
        runner.run()
    report = json.loads((runner.OUT / "run.json").read_text(encoding="utf-8"))
    assert report["network_ok"] is True and report["ok"] is False
    assert report["status"] == "failed"
    assert all(p.poll() is not None for p in runner.CHILDREN)


def test_cleanup_attempts_other_children_when_one_stop_fails(runner, monkeypatch):
    original = runner.stop
    def stop(child):
        if child.pid == 2:
            raise OSError("fixture stop failure")
        return original(child)
    monkeypatch.setattr(runner, "stop", stop)
    with pytest.raises(RuntimeError, match="fixture stop failure"):
        runner.run()
    report = json.loads((runner.OUT / "run.json").read_text(encoding="utf-8"))
    assert report["ok"] is False and report["status"] == "cleanup_failed"
    assert all(p.poll() is not None for p in runner.CHILDREN if p.pid != 2)
