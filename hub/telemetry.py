"""Bounded, privacy-safe diagnostics. Transport is independent of LiveClient."""
import atexit
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
import uuid
import sys
from pathlib import Path
from . import paths, transport
from .version import API_BASE, HUB_VERSION

MAX_EVENTS, MAX_BYTES, BATCH_SIZE = 1000, 1024 * 1024, 40
_EVENT = re.compile(r"^(app|session|ui|request|connection|operation|update|launch|telemetry|auth)\.[a-z0-9_.]{1,50}$")
_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$")
_TOKENS = frozenset(("action", "status", "code", "screen", "reason", "error_class", "phase"))

def _safe_data(fields):
    data = {}
    for key in _TOKENS:
        v = fields.get(key)
        if isinstance(v, str) and _CODE.fullmatch(v) and not re.search(r"bearer|token|password|secret", v, re.I):
            data[key] = v
    for key in ("duration_ms", "count", "status"):
        v = fields.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1e12:
            data[key] = v
    for key in ("connected", "gap"):
        if isinstance(fields.get(key), bool): data[key] = fields[key]
    return data

class _Telemetry:
    def __init__(self, outbox_path=None, open_url=None, background=True, max_events=MAX_EVENTS, max_bytes=MAX_BYTES):
        self._path = Path(outbox_path) if outbox_path is not None else None
        self._open_url = open_url or transport.urlopen
        self._background = background
        self._max_events = max(1, min(int(max_events), MAX_EVENTS))
        self._max_bytes = max(512, min(int(max_bytes), MAX_BYTES))
        self._lock = threading.RLock()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread = None
        self._events = []
        self._open_session = self.session_id = None
        self._started = self._ever_identified = False
        self._token = self._owner = None
        self._dirty = False

    def _outbox_path(self):
        if self._path is None:
            try: self._path = paths.state_dir() / "telemetry.json"
            except Exception: return None
        return self._path

    def start(self):
        try:
            with self._lock:
                if self._started: return False
                self._load_locked()
                previous = self._open_session
                anonymous = sum(r["_owner"] is None for r in self._events)
                self._events = [r for r in self._events if r["_owner"] is not None]
                self.session_id = str(uuid.uuid4())
                self._open_session = self.session_id
                self._started = True
                self._stop.clear()
                if previous: self._append_locked(self._event("session.previous_unclean", {"severity": "warn", "reason": "unclean_exit"}))
                self._append_locked(self._event("session.start", {}))
                if anonymous: self._append_locked(self._event("telemetry.gap", {"severity": "warn", "reason": "anonymous_expired", "count": anonymous}))
                self._save_locked()
            if self._background:
                self._thread = threading.Thread(target=self._run, name="hub-telemetry", daemon=True)
                self._thread.start()
            return True
        except Exception: return False

    def identify(self, token, player_id=None):
        try:
            from .player_identity import valid_player
            clean = token if isinstance(token, str) and 0 < len(token) <= 8192 else None
            owner_key = ("player:" + player_id) if valid_player(player_id) else clean
            owner = hashlib.sha256(owner_key.encode("utf-8")).hexdigest() if clean else None
            with self._lock:
                if not self._started: return False
                if clean and not self._ever_identified:
                    for row in self._events:
                        if row.get("_owner") is None and row.get("session_id") == self.session_id: row["_owner"] = owner
                    self._ever_identified = True
                if self._token == clean: return True
                self._token, self._owner = clean, owner
                if owner:
                    discarded = sum(r["_owner"] not in (None, owner) for r in self._events)
                    self._events = [r for r in self._events if r["_owner"] in (None, owner)]
                    if discarded: self._append_locked(self._event("telemetry.gap", {"severity": "warn", "reason": "identity_changed", "count": discarded}))
                self._trim_locked()
                self._save_locked()
            self._wake.set()
            return True
        except Exception: return False

    def emit(self, event_type, **fields):
        try:
            with self._lock:
                if not self._started or not isinstance(event_type, str) or not _EVENT.fullmatch(event_type): return False
                self._append_locked(self._event(event_type, fields))
                self._dirty = True
                if not self._background: self._save_locked()
            self._wake.set()
            return True
        except Exception: return False

    def shutdown(self):
        try:
            with self._lock:
                if not self._started: return
                self._stop.set(); self._wake.set()
                self._append_locked(self._event("session.end", {"status": "clean"}))
                self._open_session = None
                self._started = False
                self._save_locked()
                self._token = self._owner = None
        except Exception: pass

    def _event(self, kind, fields):
        severity = fields.get("severity", "info")
        return {"id": str(uuid.uuid4()), "type": kind, "at": int(time.time()*1000), "session_id": self.session_id,
                "version": HUB_VERSION, "severity": severity if severity in ("info", "warn", "error") else "info",
                "data": _safe_data(fields), "_owner": self._owner}

    def _append_locked(self, event):
        self._events.append(event)
        self._trim_locked()

    def _encoded_locked(self):
        return json.dumps({"schema": 1, "open_session": self._open_session, "events": self._events},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _trim_locked(self):
        cutoff = time.time()*1000 - 30*86400000
        expired = sum(r["at"] < cutoff for r in self._events)
        self._events = [r for r in self._events if r["at"] >= cutoff]
        dropped = expired
        while self._events and (len(self._events)>self._max_events or len(self._encoded_locked())>self._max_bytes):
            index = next((i for i,r in enumerate(self._events) if r["type"]!="telemetry.gap" and r["severity"]=="info"), 0)
            self._events.pop(index); dropped += 1
        if not dropped or not self.session_id: return
        gap = next((r for r in self._events if r["type"]=="telemetry.gap" and r["session_id"]==self.session_id and r["_owner"]==self._owner), None)
        if gap: gap["data"]["count"] += dropped
        else:
            gap = self._event("telemetry.gap", {"severity": "warn", "reason": "outbox_expired" if expired else "outbox_limit", "gap": True, "count": dropped})
            self._events.append(gap)
        while self._events and (len(self._events)>self._max_events or len(self._encoded_locked())>self._max_bytes):
            index = next((i for i,r in enumerate(self._events) if r is not gap), None)
            if index is None: self._events.clear(); break
            self._events.pop(index); gap["data"]["count"] += 1

    def _save_locked(self):
        target = self._outbox_path()
        if target is None: return False
        temp = target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "wb") as handle:
                handle.write(self._encoded_locked()); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, target)
            self._dirty = False
            return True
        except (OSError, ValueError):
            try: temp.unlink(missing_ok=True)
            except OSError: pass
            return False

    def _load_locked(self):
        target = self._outbox_path()
        try:
            if target is None or target.stat().st_size > MAX_BYTES: return
            doc = json.loads(target.read_text(encoding="utf-8"))
            self._events = [r for r in doc.get("events", [])[:MAX_EVENTS] if self._valid_row(r)]
            value = doc.get("open_session")
            self._open_session = value if isinstance(value, str) and re.fullmatch(r"[a-f0-9-]{36}", value) else None
        except (OSError, ValueError, TypeError, AttributeError): self._events, self._open_session = [], None

    @staticmethod
    def _valid_row(r):
        if not isinstance(r, dict) or set(r)!={"id", "type", "at", "session_id", "version", "severity", "data", "_owner"}: return False
        return (isinstance(r["id"], str) and bool(re.fullmatch(r"[a-f0-9-]{36}", r["id"])) and
                isinstance(r["session_id"], str) and bool(re.fullmatch(r"[a-f0-9-]{36}", r["session_id"])) and
                isinstance(r["type"], str) and bool(_EVENT.fullmatch(r["type"])) and isinstance(r["at"], int) and
                isinstance(r["version"], str) and bool(re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", r["version"])) and
                r["severity"] in ("info", "warn", "error") and isinstance(r["data"], dict) and r["data"]==_safe_data(r["data"]) and
                (r["_owner"] is None or isinstance(r["_owner"], str) and bool(re.fullmatch(r"[a-f0-9]{64}", r["_owner"]))))

    def _flush_once(self):
        with self._lock:
            self._trim_locked()
            token, owner = self._token, self._owner
            if not token or not owner: return False
            batch = [{k:v for k,v in r.items() if k!="_owner"} for r in self._events if r["_owner"]==owner][:BATCH_SIZE]
        if not batch: return False
        try:
            request = urllib.request.Request(API_BASE.rstrip("/")+"/api/telemetry", data=json.dumps({"events": batch}).encode("utf-8"),
                headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": "Bearer "+token}, method="POST")
            with self._open_url(request, timeout=5) as response:
                if not 200<=int(response.status)<300: return False
                body = json.loads(response.read().decode("utf-8"))
            if body.get("ok") is not True or not isinstance(body.get("accepted"), list): return False
            accepted = {v for v in body["accepted"] if isinstance(v, str)} & {r["id"] for r in batch}
            if not accepted: return False
            with self._lock:
                self._events = [r for r in self._events if not (r["_owner"]==owner and r["id"] in accepted)]
                self._save_locked()
            return True
        except Exception: return False

    def _run(self):
        failures, next_send = 0, 0
        while not self._stop.is_set():
            self._wake.wait(min(5, max(.25, next_send-time.monotonic()))); self._wake.clear()
            if self._stop.is_set(): break
            if self._stop.wait(.25): break
            with self._lock:
                if self._dirty: self._save_locked()
                sendable = bool(self._token and any(r["_owner"]==self._owner for r in self._events))
            if not sendable or time.monotonic()<next_send: continue
            if self._flush_once():
                failures = 0
                next_send = time.monotonic()+2.5
            else:
                next_send = time.monotonic()+(2.5, 5, 10, 30, 60)[min(failures, 4)]
                failures += 1

_client = _Telemetry()
def start():
    if not _client.start(): return False
    previous = sys.excepthook
    def app_exception(kind, value, traceback):
        emit("app.error", severity="error", code="uncaught_exception", error_class=kind.__name__)
        previous(kind, value, traceback)
    sys.excepthook = app_exception
    previous_thread = threading.excepthook
    def thread_exception(args):
        emit("app.error", severity="error", code="uncaught_thread", error_class=args.exc_type.__name__)
        previous_thread(args)
    threading.excepthook = thread_exception
    return True
def identify(token, player_id=None): return _client.identify(token, player_id=player_id)
def emit(kind, **fields): return _client.emit(kind, **fields)
def shutdown(): return _client.shutdown()
atexit.register(shutdown)
