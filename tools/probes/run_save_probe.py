"""Fresh engine save/restart and rejection checks: python tools/probes/run_save_probe.py."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from run_connection_probe import EDITOR, PROJECT, ROOT, stop

RUN_ID = uuid.uuid4().hex[:12]
OUT = PROJECT / "Saved/RecoveryProof" / ("save-" + RUN_ID)
SAVES = PROJECT / "Saved/SaveGames"
CHILDREN = []


def phase(run_id, action):
    folder = PROJECT / "Saved/RecoveryProof" / ("save-" + run_id)
    env = dict(os.environ, RECOVERY_SAVE_RUN=run_id, RECOVERY_PROBE_PHASE=action)
    with (folder / (action + ".log")).open("w", encoding="utf-8") as log:
        process = subprocess.Popen([
            str(EDITOR), str(PROJECT / "Bodycam.uproject"), "-run=pythonscript",
            "-script=" + str(ROOT / "tools/probes/recovery_save_probe.py"),
            "-stdout", "-unattended", "-nopause", "-nosplash", "-nullrhi",
        ], cwd=str(PROJECT), env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        CHILDREN.append(process)
        try:
            code = process.wait(timeout=120)
        finally:
            stop(process)
    result = json.loads((folder / (action + ".json")).read_text(encoding="utf-8"))
    assert result["run_id"] == run_id and result["phase"] == action
    return code, process.pid, result


def run():
    OUT.mkdir(parents=True, exist_ok=False)
    report = {"ok": False, "run_id": RUN_ID, "status": "incomplete",
              "native_bodycam_capture_or_restore_proven": False}
    try:
        code, captured_pid, capture = phase(RUN_ID, "capture")
        assert code == 0 and capture["ok"] is True and capture["pid"] == captured_pid, "capture failed"
        # phase() waited for the exact capture Popen to exit before reload begins.
        code, reloaded_pid, reload = phase(RUN_ID, "reload")
        assert code == 0 and reload["ok"] is True and reload["reload_pid"] == reloaded_pid
        assert reload["capture_pid"] == captured_pid
        assert reload["save_sha256"] == capture["save_sha256"] and reload["save_bytes"] == capture["save_bytes"]
        slot = SAVES / ("LightsOutRecoveryProof_" + RUN_ID + ".sav")
        raw = slot.read_bytes()
        expected = json.loads((OUT / "expected.json").read_text(encoding="utf-8"))
        negative = []
        for damage, message in (("missing_save", "FileNotFoundError"),
                                ("corrupt_save", "checkpoint hash changed"),
                                ("stale_manifest", "stale run manifest"),
                                ("old_save", "checkpoint belongs to an older run")):
            bad_id = uuid.uuid4().hex[:12]
            folder = PROJECT / "Saved/RecoveryProof" / ("save-" + bad_id)
            folder.mkdir(exist_ok=False)
            bad_expected = dict(expected, run_id=bad_id)
            bad_capture = dict(capture, run_id=bad_id)
            if damage == "stale_manifest":
                bad_expected["run_id"] = RUN_ID
            (folder / "expected.json").write_text(json.dumps(bad_expected), encoding="utf-8")
            (folder / "capture.json").write_text(json.dumps(bad_capture), encoding="utf-8")
            if damage != "missing_save":
                content = raw if damage != "corrupt_save" else raw[:-1] + bytes([raw[-1] ^ 1])
                (SAVES / ("LightsOutRecoveryProof_" + bad_id + ".sav")).write_bytes(content)
            code, _, rejected = phase(bad_id, "reload")
            assert code != 0 and rejected["ok"] is False and message in rejected["error"], (damage, code, rejected)
            negative.append({"case": damage, "run_id": bad_id, "rejected": True})
        assert hashlib.sha256(slot.read_bytes()).hexdigest() == capture["save_sha256"], "original evidence changed"
        report.update(ok=True, status="verified", capture_pid=captured_pid, reload_pid=reloaded_pid,
                      capture_exited_before_reload=True, save_bytes=capture["save_bytes"],
                      save_sha256=capture["save_sha256"], rejection_checks=negative)
    except Exception as error:
        report.update(ok=False, status="failed", error=repr(error))
        raise
    finally:
        errors = []
        for child in CHILDREN:
            try:
                stop(child)
            except Exception as error:
                errors.append(f"owned process {child.pid}: {error!r}")
        report["owned_pids"] = [p.pid for p in CHILDREN]
        if errors:
            report.update(ok=False, status="cleanup_failed", cleanup_errors=errors)
        (OUT / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(str(OUT / "run.json"), flush=True)
        if errors:
            raise RuntimeError("; ".join(errors))
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    run()
