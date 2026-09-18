"""Isolated Steam ticket proof. No game launch, login linking, or install writes.

The installed, pinned Valve DLL runs only in a short-lived child process. Tickets
travel over private inherited pipes and stay alive until the server verifies them.
"""
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time

APP_ID = 2406770
DLL_PATH = Path("Bodycam/Plugins/SteamCorePro_5.5/Source/ThirdParty/SteamLibrary/redistributable_bin/win64/steam_api64.dll")
DLL_SHA256 = "670d654aa3255c5061cf0236a1cdbb2f5076cbd5ae44f613b6f4921558e83e2d"
MAX_FRAME = 8192
_spawn_lock = threading.Lock()


class GameIdentityError(Exception):
    """Messages contain no provider diagnostics or credentials."""


def read_frame(stream):
    def exact(size):
        out = b""
        while len(out) < size:
            chunk = stream.read(size - len(out))
            if not chunk:
                raise GameIdentityError("Game verification ended.")
            out += chunk
        return out
    size = struct.unpack("!I", exact(4))[0]
    if not 1 <= size <= MAX_FRAME:
        raise GameIdentityError("Invalid game verification response.")
    try:
        value = json.loads(exact(size))
    except (ValueError, UnicodeError):
        raise GameIdentityError("Invalid game verification response.") from None
    if not isinstance(value, dict):
        raise GameIdentityError("Invalid game verification response.")
    return value


def write_frame(stream, value):
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    if not 1 <= len(data) <= MAX_FRAME:
        raise GameIdentityError("Invalid game verification request.")
    remaining = memoryview(struct.pack("!I", len(data)) + data)
    while remaining:
        count = stream.write(remaining)
        if not count:
            raise GameIdentityError("Game verification ended.")
        remaining = remaining[count:]
    stream.flush()


def _timed_frame(stream, seconds):
    results = queue.Queue(maxsize=1)
    def read():
        try:
            results.put((True, read_frame(stream)))
        except Exception:
            results.put((False, None))
    threading.Thread(target=read, daemon=True).start()
    try:
        ok, result = results.get(timeout=seconds)
    except queue.Empty:
        raise GameIdentityError("Game verification timed out.") from None
    if not ok:
        raise GameIdentityError("Game verification ended.")
    return result


def _timed_write(stream, value, seconds=3):
    result = queue.Queue(maxsize=1)
    def write():
        try:
            write_frame(stream, value)
            result.put(True)
        except Exception:
            result.put(False)
    threading.Thread(target=write, daemon=True).start()
    try:
        if result.get(timeout=seconds):
            return
    except queue.Empty:
        pass
    raise GameIdentityError("Game verification ended.")


class TicketProcess:
    def __init__(self, game_dir, identity):
        self.game_dir, self.identity = str(game_dir), identity
        self.process = self.reader = self.writer = self.temporary = None

    def __enter__(self):
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8 or len(self.game_dir.encode("utf-8")) > 768 or \
                not re.fullmatch(r"[A-Za-z0-9]{24}", self.identity or ""):
            raise GameIdentityError("Game verification is unavailable.")
        import msvcrt
        incoming, write_in = os.pipe()
        read_out, outgoing = os.pipe()
        child_handles = [msvcrt.get_osfhandle(incoming), msvcrt.get_osfhandle(outgoing)]
        self.reader, self.writer = os.fdopen(read_out, "rb", buffering=0), os.fdopen(write_in, "wb", buffering=0)
        self.temporary = tempfile.TemporaryDirectory(prefix="lightsout-proof-")
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve().parent.parent / "hub_entry.py"))
        command += ["--game-identity", *map(str, child_handles)]
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {"handle_list": child_handles}
        try:
            with _spawn_lock:
                try:
                    for handle in child_handles:
                        os.set_handle_inheritable(handle, True)
                    self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                        startupinfo=startup, creationflags=subprocess.CREATE_NO_WINDOW,
                        cwd=self.temporary.name)
                finally:
                    for handle in child_handles:
                        os.set_handle_inheritable(handle, False)
            os.close(incoming); incoming = None
            os.close(outgoing); outgoing = None
            _timed_write(self.writer, {"v": 1, "op": "acquire", "identity": self.identity, "game_dir": self.game_dir})
            result = _timed_frame(self.reader, 20)
            if result.get("v") != 1 or result.get("ok") is not True or \
                    not re.fullmatch(r"(?:[a-f0-9]{2}){1,2560}", result.get("ticket", "")) or \
                    not re.fullmatch(r"\d{17}", result.get("local_steam_id", "")):
                raise GameIdentityError("Open Steam with Bodycam available, then try again.")
            return result
        except Exception:
            self.__exit__(None, None, None)
            raise GameIdentityError("Open Steam with Bodycam available, then try again.") from None
        finally:
            if incoming is not None:
                os.close(incoming)
            if outgoing is not None:
                os.close(outgoing)

    def __exit__(self, *args):
        failed = False
        try:
            if self.process:
                _timed_write(self.writer, {"v": 1, "op": "release"})
                if _timed_frame(self.reader, 3) != {"v": 1, "released": True} or self.process.wait(timeout=3) != 0:
                    failed = True
        except Exception:
            failed = True
        finally:
            if self.process:
                try:
                    if self.process.poll() is None:
                        self.process.kill()
                    self.process.wait(timeout=3)
                except Exception:
                    failed = True
            for stream in [self.writer, self.reader]:
                try:
                    if stream:
                        stream.close()
                except Exception:
                    failed = True
            if self.temporary:
                try:
                    self.temporary.cleanup()
                except Exception:
                    failed = True
            self.process = self.reader = self.writer = self.temporary = None
        if failed and not (args and args[0]):
            raise GameIdentityError("Game verification could not finish.")


def mint_session(parent_token, game_dir, should_stop=None, request=None, ticket_factory=TicketProcess):
    if request is None:
        from .auth import account_request
        request = account_request
    def cancelled():
        if should_stop and should_stop():
            raise GameIdentityError("Game verification cancelled.")
    cancelled()
    challenge = request("game/challenge", {}, parent_token)
    cancelled()
    with ticket_factory(game_dir, challenge.get("identity")) as proof:
        cancelled()
        verified = request("game/verify", {"challenge": challenge.get("challenge"), "ticket": proof["ticket"]}, parent_token)
        cancelled()
        if not re.fullmatch(r"lg_[A-Za-z0-9_-]{43}", verified.get("token", "")) or \
                verified.get("game_steam_id") != proof["local_steam_id"] or \
                not re.fullmatch(r"(?:\d{17}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", verified.get("player_id", "")):
            raise GameIdentityError("Your game account could not be verified.")
        return verified


def _api(dll, name, args, result):
    fn = getattr(dll, name)
    fn.argtypes, fn.restype = args, result
    return fn


class _Callback(C.Structure):
    _fields_ = [("user", C.c_int32), ("kind", C.c_int32), ("data", C.c_void_p), ("size", C.c_int32)]


class _Ticket(C.Structure):
    _fields_ = [("handle", C.c_uint32), ("result", C.c_int32), ("length", C.c_int32), ("data", C.c_ubyte * 2560)]


def _serve(reader, writer):
    import msvcrt
    from ctypes import wintypes as W
    request = _timed_frame(reader, 5)
    if request.get("v") != 1 or request.get("op") != "acquire" or \
            not re.fullmatch(r"[A-Za-z0-9]{24}", request.get("identity", "")):
        raise GameIdentityError("Invalid request.")
    path = (Path(request["game_dir"]).resolve() / DLL_PATH).resolve(strict=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    create = _api(kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
    handle = create(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == C.c_void_p(-1).value:
        raise GameIdentityError("Game library unavailable.")
    # Sharing only READ prevents replacement between verification and native use.
    with os.fdopen(msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY), "rb") as pinned:
        if hashlib.file_digest(pinned, "sha256").hexdigest() != DLL_SHA256:
            raise GameIdentityError("Game library changed.")
        os.environ["SteamAppId"] = os.environ["SteamGameId"] = str(APP_ID)
        dll = C.CDLL(str(path), winmode=0x100 | 0x1000)
        if not _api(dll, "SteamAPI_InitSafe", [], C.c_bool)():
            raise GameIdentityError("Steam unavailable.")
        ticket_handle, user, cancel = 0, None, None
        try:
            user = _api(dll, "SteamAPI_SteamUser_v023", [], C.c_void_p)()
            utils = _api(dll, "SteamAPI_SteamUtils_v010", [], C.c_void_p)()
            if not user or not utils or _api(dll, "SteamAPI_ISteamUtils_GetAppID", [C.c_void_p], C.c_uint32)(utils) != APP_ID:
                raise GameIdentityError("Wrong app context.")
            steam_id = str(_api(dll, "SteamAPI_ISteamUser_GetSteamID", [C.c_void_p], C.c_uint64)(user))
            _api(dll, "SteamAPI_ManualDispatch_Init", [], None)()
            pipe = _api(dll, "SteamAPI_GetHSteamPipe", [], C.c_int32)()
            frame = _api(dll, "SteamAPI_ManualDispatch_RunFrame", [C.c_int32], None)
            poll = _api(dll, "SteamAPI_ManualDispatch_GetNextCallback", [C.c_int32, C.POINTER(_Callback)], C.c_bool)
            free = _api(dll, "SteamAPI_ManualDispatch_FreeLastCallback", [C.c_int32], None)
            cancel = _api(dll, "SteamAPI_ISteamUser_CancelAuthTicket", [C.c_void_p, C.c_uint32], None)
            identity_bytes = request["identity"].encode("ascii")
            ticket_handle = _api(dll, "SteamAPI_ISteamUser_GetAuthTicketForWebApi", [C.c_void_p, C.c_char_p], C.c_uint32)(user, identity_bytes)
            deadline = time.monotonic() + 15
            ticket_hex = None
            while ticket_handle and time.monotonic() < deadline and ticket_hex is None:
                frame(pipe)
                msg = _Callback()
                while poll(pipe, C.byref(msg)):
                    try:
                        if msg.kind == 168 and msg.data and msg.size == C.sizeof(_Ticket):
                            ticket = _Ticket.from_buffer_copy(C.string_at(msg.data, msg.size))
                            if ticket.handle == ticket_handle and ticket.result == 1 and 0 < ticket.length <= 2560:
                                ticket_hex = bytes(ticket.data[:ticket.length]).hex()
                    finally:
                        free(pipe)
                time.sleep(.03)
            if not ticket_hex:
                raise GameIdentityError("Ticket unavailable.")
            _timed_write(writer, {"v": 1, "ok": True, "ticket": ticket_hex, "local_steam_id": steam_id})
            # Parent verifies over HTTPS while this API session and ticket stay alive.
            release = _timed_frame(reader, 30)
            if release != {"v": 1, "op": "release"}:
                raise GameIdentityError("Invalid release.")
        finally:
            if ticket_handle and user and cancel:
                cancel(user, ticket_handle)
            _api(dll, "SteamAPI_Shutdown", [], None)()
        _timed_write(writer, {"v": 1, "released": True})


def dispatch(argv):
    if len(argv) < 2 or argv[1] != "--game-identity":
        return
    if os.name != "nt" or len(argv) != 4:
        raise SystemExit(1)
    import msvcrt
    try:
        incoming, outgoing = (int(value) for value in argv[2:])
        with os.fdopen(msvcrt.open_osfhandle(incoming, os.O_RDONLY | os.O_BINARY), "rb", buffering=0) as reader, \
                os.fdopen(msvcrt.open_osfhandle(outgoing, os.O_WRONLY | os.O_BINARY), "wb", buffering=0) as writer:
            try:
                _serve(reader, writer)
            except Exception:
                _timed_write(writer, {"v": 1, "ok": False})
                raise SystemExit(1) from None
    except Exception:
        raise SystemExit(1) from None
    raise SystemExit(0)
