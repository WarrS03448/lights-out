"""Persistent match-scoped cleanup. Runs without the UI or Steam initialization."""
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import uuid
from ctypes import wintypes as W
from pathlib import Path
from urllib.parse import urlencode

from . import auth, game, paths, state
from .version import GAME_EXE

POLL_SECONDS, CAPTURE_SECONDS, GRACE_SECONDS = 3, 120, 25
TERMINAL = {"done", "superseded", "capture_expired"}
_APIS = None


def _apis():
    global _APIS
    if _APIS is not None:
        return _APIS
    if sys.platform != "win32":
        raise RuntimeError("Cleanup requires Windows")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u = ctypes.WinDLL("user32", use_last_error=True)
    callback = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    for dll, name, args, result in (
        (k, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        (k, "CloseHandle", [W.HANDLE], W.BOOL),
        (k, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
        (k, "GetProcessTimes", [W.HANDLE] + [ctypes.POINTER(W.FILETIME)] * 4, W.BOOL),
        (k, "QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)], W.BOOL),
        (k, "TerminateProcess", [W.HANDLE, W.UINT], W.BOOL),
        (k, "CreateMutexW", [W.LPVOID, W.BOOL, W.LPCWSTR], W.HANDLE),
        (u, "EnumWindows", [callback, W.LPARAM], W.BOOL),
        (u, "GetWindowThreadProcessId", [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
        (u, "PostMessageW", [W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL),
    ):
        fn = getattr(dll, name); fn.argtypes, fn.restype = args, result
    _APIS = k, u, callback
    return _APIS


def _directory():
    folder = paths.state_dir() / "cleanup"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _job_path(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid cleanup job")
    return _directory() / (job_id + ".json")


def _read(path):
    with open(path, encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("Invalid cleanup record")
    return value


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(temporary, "x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_status(job_id):
    try:
        return _read(_job_path(job_id).with_suffix(".status.json"))
    except (OSError, ValueError):
        return {"state": "pending"}


def _status(job_id, state, **fields):
    value = {**read_status(job_id), "state": state, "updated_at": time.time(), **fields}
    _write(_job_path(job_id).with_suffix(".status.json"), value)


def _normal_path(value):
    return os.path.normcase(os.path.abspath(value))


def _open_process(pid):
    handle = _apis()[0].OpenProcess(0x00100000 | 0x1000 | 0x0001, False, int(pid))
    if handle:
        return handle
    error = ctypes.get_last_error()
    if error == 87:  # PID no longer exists; access denied remains an error.
        return None
    raise ctypes.WinError(error)


def _exited(handle):
    result = _apis()[0].WaitForSingleObject(handle, 0)
    if result == 0:
        return True
    if result == 258:
        return False
    raise ctypes.WinError(ctypes.get_last_error())


def _identity(handle, pid):
    k, _, _ = _apis()
    times = [W.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    size = W.DWORD(32768); buffer = ctypes.create_unicode_buffer(size.value)
    if not k.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"pid": int(pid), "created": (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            "exe": _normal_path(buffer.value)}


def capture_identity(pid, expected_exe):
    handle = _open_process(pid)
    if handle is None:
        return None
    try:
        if _exited(handle):
            return None
        identity = _identity(handle, pid)
        return identity if identity["exe"] == _normal_path(expected_exe) else None
    finally:
        _apis()[0].CloseHandle(handle)


def register(match_id, steam_id, token, game_dir, launched_at=None):
    """Adopt an existing game or capture the process from a bounded Steam launch window."""
    if sys.platform != "win32" or not re.fullmatch(r"[0-9a-f]{16}", match_id or "") or not token:
        return None
    expected = str(Path(game_dir) / "Bodycam" / "Binaries" / "Win64" / GAME_EXE)
    if not Path(expected).is_file():
        return None
    candidates = [identity for pid in game.game_pids() if (identity := capture_identity(pid, expected))]
    if launched_at is not None:
        candidates = [i for i in candidates if launched_at <= i['created'] / 10_000_000 - 11_644_473_600 <= launched_at + CAPTURE_SECONDS]
    identity = candidates[0] if len(candidates) == 1 else None
    if identity is None and launched_at is None:
        return None
    for path in _directory().glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
            continue
        old = _read(path)
        if (old.get("match_id") == match_id and old.get("steam_id") == steam_id
                and read_status(path.stem).get("state") not in TERMINAL
                and ((identity is not None and old.get("identity") == identity)
                     or (old.get("identity") is None and old.get("launched_at") == launched_at))):
            ensure_worker(path.stem)
            return path.stem
    job_id = uuid.uuid4().hex
    from .credentials import seal
    _write(_job_path(job_id), {"schema": 2, "job_id": job_id, "match_id": match_id,
        "steam_id": steam_id, "credential": seal({"token": token}), "expected_exe": _normal_path(expected),
        "launched_at": launched_at, "identity": identity})
    ensure_worker(job_id)
    return job_id


def ensure_worker(job_id):
    _job_path(job_id)
    args = [sys.executable]; options = {}
    if not getattr(sys, "frozen", False):
        args += ["-m", "hub"]
        options["cwd"] = str(Path(__file__).resolve().parent.parent)
    args += ["--match-cleanup", job_id]
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW; startup.wShowWindow = 0
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, startupinfo=startup,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, **options)


def resume_pending_jobs():
    if sys.platform != "win32":
        return
    for path in _directory().glob("*.json"):
        if re.fullmatch(r"[0-9a-f]{32}", path.stem) and read_status(path.stem).get("state") not in TERMINAL:
            try:
                ensure_worker(path.stem)
            except OSError:
                pass  # Job remains available for the next startup.


def pending_for(player_id):
    """Do not revoke a receipt credential while its detached worker still needs it."""
    if not player_id:
        return False
    for path in _directory().glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
            continue
        try:
            job = _read(path)
            if job.get("steam_id") == player_id and read_status(path.stem).get("state") not in TERMINAL:
                return True
        except (OSError,ValueError):
            continue
    return False


def finish_launch(job_id):
    """Once the match ends, this launch may capture only a process that already exists.

    A separate atomic record avoids racing the worker's persisted process identity.
    """
    path = _job_path(job_id).with_suffix(".capture.json")
    if not path.exists():
        _write(path, {"until": time.time()})


def _capture_deadline(job):
    deadline = float(job["launched_at"]) + CAPTURE_SECONDS
    try:
        return min(deadline, float(_read(_job_path(job["job_id"]).with_suffix(".capture.json"))["until"]))
    except FileNotFoundError:
        return deadline


def _capture_launched(job):
    start = float(job["launched_at"])
    candidates = []
    for pid in game.game_pids():
        identity = capture_identity(pid, job["expected_exe"])
        if identity:
            born = identity["created"] / 10_000_000 - 11_644_473_600
            if start <= born <= start + CAPTURE_SECONDS:
                candidates.append(identity)
    # Completion can seal the launch while process enumeration is in progress.
    deadline = _capture_deadline(job)
    candidates = [i for i in candidates if i["created"] / 10_000_000 - 11_644_473_600 <= deadline]
    return candidates[0] if len(candidates) == 1 else None


def receipt_allows_close(job, receipt):
    return (isinstance(receipt, dict) and receipt.get("ok") is True
            and receipt.get("match_id") == job["match_id"]
            and receipt.get("data_collected") is True and receipt.get("close_allowed") is True)


def _receipt(job):
    try:
        from .credentials import open_sealed, CredentialError
        # A match cleanup worker has only its protected, expiring gameplay token.
        # It never falls back to another account's remembered parent credential.
        token = open_sealed(job["credential"])["token"]
        status, value = auth._request("/api/match/completion?" + urlencode({"id": job["match_id"]}),
                                      token=token, timeout=10)
        return value if status == 200 and receipt_allows_close(job, value) else None
    except (OSError, ValueError, KeyError, CredentialError, auth.AuthError):
        return None


def _graceful_close(handle, pid):
    _, user, callback = _apis()
    @callback
    def visit(window, _):
        owner = W.DWORD(); user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and not _exited(handle):
            user.PostMessageW(window, 0x0010, 0, 0)
        return True
    user.EnumWindows(visit, 0)


def run_worker(job_id):
    path = _job_path(job_id); k, _, _ = _apis()
    ctypes.set_last_error(0)
    mutex = k.CreateMutexW(None, False, "Local\\LightsOutCleanup-v1-" + job_id)
    already_running = ctypes.get_last_error() == 183
    if not mutex:
        return 1
    if already_running:
        k.CloseHandle(mutex); return 0
    handle = None
    try:
        job = _read(path)
        if job.get("schema") not in (1, 2) or job.get("job_id") != job_id:
            return 1
        if read_status(job_id).get("state") in TERMINAL:
            return 0
        grace_started = None; receipt = None; attempts = 0
        while True:
            try:
                if job.get("schema") == 1:
                    from .credentials import seal
                    if not job.get("token"):
                        return 1
                    migrated = {**job, "schema": 2, "credential": seal({"token": job["token"]})}
                    del migrated["token"]
                    _write(path, migrated)
                    job = migrated
                identity = job.get("identity")
                if identity is None:
                    identity = _capture_launched(job)
                    if identity is None:
                        if time.time() > _capture_deadline(job):
                            _status(job_id, "capture_expired"); return 0
                        _status(job_id, "awaiting_process"); time.sleep(POLL_SECONDS); continue
                    captured = {**job, "identity": identity}
                    _write(path, captured); job = captured
                if handle is None:
                    handle = _open_process(identity["pid"])
                    if handle is None:
                        _status(job_id, "done", reason="verified_process_absent"); return 0
                    try:
                        actual = _identity(handle, identity["pid"])
                    except OSError:
                        k.CloseHandle(handle); handle = None
                        raise
                    if actual != identity:
                        _status(job_id, "superseded", reason="process_identity_changed"); return 0
                if _exited(handle):
                    _status(job_id, "done", reason="verified_exit", attempts=attempts); return 0
                if receipt is None:
                    receipt = _receipt(job)
                    if receipt is None:
                        _status(job_id, "waiting_for_data"); time.sleep(POLL_SECONDS); continue
                    _status(job_id, "data_collected", result=receipt.get("result"))
                if time.time() * 1000 < receipt.get("close_after", 0):
                    time.sleep(POLL_SECONDS); continue
                if grace_started is None:
                    _graceful_close(handle, identity["pid"]); grace_started = time.monotonic()
                    _status(job_id, "closing_gracefully")
                elif time.monotonic() - grace_started >= GRACE_SECONDS:
                    attempts += 1
                    if not k.TerminateProcess(handle, 0) and not _exited(handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                    _status(job_id, "verifying_exit", attempts=attempts)
            except OSError as error:
                _status(job_id, "retrying", attempts=attempts, winerror=getattr(error, "winerror", None))
            time.sleep(POLL_SECONDS)
    finally:
        if handle:
            k.CloseHandle(handle)
        k.CloseHandle(mutex)


def dispatch(argv):
    if "--match-cleanup" not in argv:
        return False
    index = argv.index("--match-cleanup")
    if index + 1 >= len(argv):
        raise SystemExit(2)
    raise SystemExit(run_worker(argv[index + 1]))
