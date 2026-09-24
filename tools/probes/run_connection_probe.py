"""Run owned UE5.5 loopback processes: python tools/probes/run_connection_probe.py.

This is a disposable engine-level test, not a Bodycam/Steam multiplayer claim.
Only child processes created here are terminated. No game-install writes.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "mirror" / "Bodycam"
EDITOR = Path(r"C:\Program Files\Epic Games\UE_5.5\Engine\Binaries\Win64\UnrealEditor-Cmd.exe")
RUN_ID = uuid.uuid4().hex[:12]
OUT = PROJECT / "Saved" / "RecoveryProof" / ("network-" + RUN_ID)
SAVES = PROJECT / "Saved" / "SaveGames"
CHILDREN = []
RECORDS = []


def available_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def launch(role, session, port):
    address = "/Game/RecoveryProof/ConnectionMap?listen" if role == "host" else f"127.0.0.1:{port}"
    log = OUT / f"{session}-{role}.log"
    args = [str(EDITOR), str(PROJECT / "Bodycam.uproject"), address, "-game", "-nullrhi", "-nosound",
            "-unattended", "-nopause", "-nosplash", "-NoSteam",
            f"-port={port}", "-MULTIHOME=127.0.0.1", f"-ProofRole={role}", f"-ProofSession={session}",
            "-abslog=" + str(log), f"-ProofSlot=RecoveryProof_{RUN_ID}_{role}"]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen(args, cwd=str(PROJECT), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW)
    CHILDREN.append(process)
    print(f"Started owned {session}/{role} process {process.pid}", flush=True)
    return process


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def path(role):
    return SAVES / (f"RecoveryProof_{RUN_ID}_{role}.sav")


def stable_bytes(role, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            raw = path(role).read_bytes()
            time.sleep(0.025)
            if len(raw) > 16 and raw.startswith(b"GVAS") and raw == path(role).read_bytes():
                return raw
        except OSError:
            pass
        time.sleep(0.025)
    raise OSError(f"no stable save for {role}")


def digest(role):
    try:
        return hashlib.sha256(stable_bytes(role, timeout=0.1)).hexdigest()
    except OSError:
        return None


def changes(role, old, process, count=3, timeout=35):
    end = time.monotonic() + timeout
    found = 0
    previous = old
    while time.monotonic() < end:
        assert process.poll() is None, f"owned {role} process exited before observation"
        current = digest(role)
        if current and current != previous:
            previous = current
            found += 1
            if found >= count:
                return current
        time.sleep(0.2)
    raise AssertionError(f"{role} did not produce {count} changed observations; inspect owned process logs")


def snapshot(stage, roles):
    for role in roles:
        slot = f"RecoveryProof_{RUN_ID}_{stage}_{role}"
        raw = stable_bytes(role)
        (SAVES / (slot + ".sav")).write_bytes(raw)
        (OUT / (slot + ".sav")).write_bytes(raw)
        RECORDS.append({"stage": stage, "role": role, "slot": slot,
                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})


def run():
    OUT.mkdir(parents=True, exist_ok=False)
    report = {"ok": False, "run_id": RUN_ID, "error": "run interrupted before completion", "records": RECORDS}
    try:
        before = {r: digest(r) for r in ("host", "client1", "client2")}
        port = available_port()
        host = launch("host", "A", port)
        changes("host", before["host"], host)
        one = launch("client1", "A", port)
        two = launch("client2", "A", port)
        changes("client1", before["client1"], one)
        changes("client2", before["client2"], two)
        snapshot("healthy", ("host", "client1", "client2"))
        stop(one)
        snapshot("client_stopped", ("client1",))
        dead = digest("client1")
        survivor = digest("client2")
        changes("client2", survivor, two)
        assert digest("client1") == dead, "terminated client's observation kept changing"
        snapshot("one_client_left", ("host", "client1", "client2"))
        assert two.poll() is None, "surviving client exited before host loss"
        stop(host)
        time.sleep(3)
        frozen = digest("client2")
        snapshot("host_lost", ("client2",))
        assert frozen is not None, "missing host-loss observation"
        start = time.monotonic()
        while time.monotonic() - start < 6:
            assert digest("client2") == frozen, "client observation changed after its host terminated"
            assert two.poll() is None, "client exited; cannot establish loss of pulses in a live client"
            time.sleep(0.1)
        unchanged_seconds = time.monotonic() - start
        snapshot("host_lost_stable", ("client2",))
        stop(two)
        previous = digest("client1")
        new_port = available_port()
        other_host = launch("host", "B", new_port)
        changes("host", digest("host"), other_host)
        other_client = launch("client1", "B", new_port)
        changes("client1", previous, other_client)
        snapshot("different_session", ("host", "client1"))
        stop(other_client)
        stop(other_host)
        report = {"ok": False, "network_ok": True, "run_id": RUN_ID,
                  "status": "awaiting_native_readback", "scope": "real Unreal loopback; no Bodycam/Steam game run",
                  "owned_pids": [p.pid for p in CHILDREN], "surviving_client_alive_after_host_loss": True,
                  "host_loss_grace_seconds": 3, "unchanged_observation_seconds": unchanged_seconds,
                  "records": RECORDS}
        (OUT / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        result = readback()
        assert result["ok"] is True and result["run_id"] == RUN_ID, "native readback failed or belongs to another run"
        report.update(ok=True, status="verified")
    except Exception as error:
        report.update(ok=False, status="failed", error=repr(error))
        raise
    finally:
        cleanup_errors = []
        for child in CHILDREN:
            try:
                stop(child)
            except Exception as error:
                cleanup_errors.append(f"owned process {child.pid}: {error!r}")
        if cleanup_errors:
            report.update(ok=False, status="cleanup_failed", cleanup_errors=cleanup_errors)
        (OUT / "run.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(str(OUT / "run.json"), flush=True)
        if cleanup_errors:
            raise RuntimeError("; ".join(cleanup_errors))
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2), flush=True)


def readback():
    env = dict(os.environ, RECOVERY_CONNECTION_RUN=str(OUT))
    with (OUT / "readback.log").open("w", encoding="utf-8") as log:
        reader = subprocess.Popen([str(EDITOR), str(PROJECT / "Bodycam.uproject"), "-run=pythonscript",
                                "-script=" + str(ROOT / "tools/probes/read_connection_probe.py"),
                                "-stdout", "-unattended", "-nopause", "-nosplash", "-nullrhi"],
                               cwd=str(PROJECT), env=env, stdout=log, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        CHILDREN.append(reader)
        try:
            code = reader.wait(timeout=120)
        finally:
            stop(reader)
    result = json.loads((OUT / "readback.json").read_text(encoding="utf-8"))
    assert code == 0 and result["ok"], "native readback/behavior check failed"
    return result


if __name__ == "__main__":
    run()
