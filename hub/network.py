"""Thread-safe state for browser relay measurements. Never loads Steam libraries."""
import copy
import math
import re
import secrets
import threading
import time
from collections import deque

TRANSPORT = "webrtc-relay-v1"


def _number(v, low, high):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v) and low <= v <= high


class RelayNetworkProbe:
    """Python owns session state; WebView2 owns the relay-only data channels."""
    def __init__(self, _game_dir=""):
        self.generation = secrets.token_hex(16)
        self._lock = threading.RLock()
        self._closed = False
        self._ice, self._expires = [], 0
        self._seen, self._supported = 0, False
        self._browser_error = ""
        self._id, self._revision = "", ""
        self._peers, self._incoming, self._measurements = {}, {}, {}
        self._queued = False
        self._signals, self._sequence = deque(maxlen=128), 0
        self.error = None

    def needs_credentials(self):
        with self._lock:
            return self._expires <= time.time() * 1000 + 120000

    def configure(self, data):
        with self._lock:
            self._ice, self._expires = [], 0
            now = time.time() * 1000
            if (self._closed or not isinstance(data, dict) or data.get("transport") != TRANSPORT
                    or not _number(data.get("expires_at"), now, now + 3600000)):
                return False
            entries = data.get("iceServers")
            if not isinstance(entries, list) or not 1 <= len(entries) <= 8:
                return False
            ice = []
            for entry in entries:
                if not isinstance(entry, dict):
                    return False
                urls = entry.get("urls")
                urls = [urls] if isinstance(urls, str) else urls
                if (not isinstance(urls, list) or not 1 <= len(urls) <= 8
                        or not all(isinstance(u, str) and re.fullmatch(r"turns?:[A-Za-z0-9.\-:\[\]]+(?:\?transport=(?:tcp|udp))?", u) for u in urls)
                        or not all(isinstance(entry.get(k), str) and 0 < len(entry[k]) <= 2048 for k in ("username", "credential"))):
                    return False
                ice.append({"urls": list(urls), "username": entry["username"], "credential": entry["credential"]})
            self._ice, self._expires = ice, data["expires_at"]
            return True

    def profile(self):
        with self._lock:
            ready = (not self._closed and self._ice and self._expires > time.time() * 1000
                     and self._supported and time.monotonic() - self._seen < 15)
            return {"location": self.generation if ready else None, "age_seconds": 0, "transport": TRANSPORT,
                    "error": "" if ready else self._browser_error or
                    "Waiting for the relay connection checker in the desktop app."}

    def set_identity(self, steam_id, revision):
        with self._lock:
            if self._revision and self._revision != revision:
                self._peers.clear(); self._incoming.clear(); self._measurements.clear(); self._signals.clear()
            self._id, self._revision = str(steam_id or ""), str(revision or "")

    def set_peers(self, peers, queued):
        with self._lock:
            self._queued = bool(queued)
            self._peers = {p["steam_id"]: dict(p) for p in peers[:16] if isinstance(p, dict)
                           and isinstance(p.get("steam_id"), str) and isinstance(p.get("revision"), str)} if queued else {}
            if not queued:
                self._incoming.clear(); self._measurements.clear(); self._signals.clear()
            now = time.monotonic()
            self._incoming = {k: v for k, v in self._incoming.items() if now - v["at"] < 45}

    def receive_signal(self, event):
        with self._lock:
            if (self._closed or not self._revision or event.get("target_revision") != self._revision
                    or not isinstance(event.get("from"), str) or not isinstance(event.get("revision"), str)):
                return
            peer = event["from"]
            if len(self._incoming) >= 16 and peer not in self._incoming:
                return
            self._incoming[peer] = {"steam_id": peer, "revision": event["revision"], "at": time.monotonic()}
            self._sequence += 1
            self._signals.append({**event, "seq": self._sequence})

    def browser_state(self):
        with self._lock:
            known = {**self._incoming, **self._peers} if self._queued else {}
            return copy.deepcopy({"generation": self.generation, "iceServers": self._ice,
                "expires_at": self._expires, "steam_id": self._id, "revision": self._revision,
                "queued": self._queued, "peers": list(known.values()), "signals": list(self._signals)})

    def browser_input(self, body):
        with self._lock:
            if self._closed or not isinstance(body, dict) or body.get("generation") != self.generation:
                return False
            self._seen = time.monotonic()
            kind = body.get("type")
            if kind == "ready":
                self._supported = body.get("supported") is True
                if self._supported:
                    self._browser_error = ""
                ack = body.get("ack")
                if isinstance(ack, int) and not isinstance(ack, bool):
                    while self._signals and self._signals[0]["seq"] <= ack:
                        self._signals.popleft()
                return True
            if kind == "error":
                self._browser_error = str(body.get("error") or "Relay checker unavailable.")[:200]
                self._supported = False
                return True
            peer_id = body.get("peer")
            if not isinstance(peer_id, str):
                return False
            peer = self._peers.get(peer_id) or self._incoming.get(peer_id)
            if not self._queued or not peer or body.get("peer_revision") != peer["revision"]:
                return False
            valid_attempt = isinstance(body.get("attempt"), str) and re.fullmatch(r"[a-f0-9]{32}", body["attempt"])
            if kind == "signal":
                return bool(self._revision and valid_attempt)
            if (kind != "measurement" or not valid_attempt or not _number(body.get("ping"), 0, 10000)
                    or not isinstance(body.get("samples"), int) or isinstance(body["samples"], bool)
                    or not 5 <= body["samples"] <= 100 or not _number(body.get("age_seconds"), 0, 15)):
                return False
            self._measurements[body["peer"]] = {"steam_id": body["peer"], "revision": body["peer_revision"],
                "ping": body["ping"], "samples": body["samples"], "attempt": body["attempt"], "transport": TRANSPORT,
                "sampled": time.monotonic() - body["age_seconds"]}
            return True

    def take_measurements(self):
        with self._lock:
            result = []
            for row in self._measurements.values():
                age = time.monotonic() - row["sampled"]
                if age <= 15:
                    result.append({k: v for k, v in row.items() if k != "sampled"} | {"age_seconds": age})
            self._measurements.clear()
            return result

    def close(self):
        with self._lock:
            self._closed = True
            self._ice.clear(); self._signals.clear(); self._peers.clear(); self._incoming.clear(); self._measurements.clear()
            self._expires = 0
