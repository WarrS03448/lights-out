"""Browser relay controller regressions; no native Steam or real credentials."""
import ctypes
import time
from types import SimpleNamespace

from hub import network
from hub import relaytransport
from hub.live import LiveClient
from tests.test_network_transport import _TransportClient, _wait_for


def probe():
    cls = getattr(network, "RelayNetworkProbe", None)
    assert cls is not None, "Production needs a non-Steam relay controller"
    return cls("")


def config():
    return {"transport": "webrtc-relay-v1", "expires_at": int(time.time() * 1000) + 600000,
            "iceServers": [{"urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
                            "username": "temporary-user", "credential": "temporary-secret"}]}


def ready(p):
    p.configure(config())
    p.browser_input({"generation": p.generation, "type": "ready", "supported": True})
    p.set_identity("1", "ours")
    p.set_peers([{"steam_id": "2", "revision": "theirs", "location": "b" * 32}], True)


def test_production_factory_never_initializes_steam(monkeypatch, tmp_path):
    (tmp_path / "steam_api64.dll").write_bytes(b"not a library")
    loaded = []
    def forbidden(path):
        loaded.append(path)
        raise AssertionError("Steam initialization is forbidden")
    monkeypatch.setattr(ctypes, "WinDLL", forbidden, raising=False)
    monkeypatch.setattr(network, "_load_library", forbidden, raising=False)
    p = LiveClient._make_network_probe(str(tmp_path))
    try:
        assert loaded == [], "Opening Lights Out must not register a Steam game"
        assert type(p).__name__ == "RelayNetworkProbe"
        ready(p)
        assert p.profile()["location"] == p.generation
        assert p.browser_input({"generation": p.generation, "type": "measurement", "peer": "2",
                                "peer_revision": "theirs", "ping": 35, "samples": 8,
                                "age_seconds": 0, "attempt": "a" * 32})
        rows = p.take_measurements()
        assert len(rows) == 1 and rows[0]["transport"] == "webrtc-relay-v1"
        assert loaded == [], "Relay configure/readiness/report paths must stay independent of Steam"
    finally:
        p.close()


def test_requires_browser_and_unexpired_relay_credentials():
    p = probe()
    assert p.profile()["location"] is None
    ready(p)
    assert p.profile()["location"] == p.generation
    expired = config(); expired["expires_at"] = 1
    p.configure(expired)
    assert p.profile()["location"] is None


def test_stale_browser_generation_cannot_report_or_restore_readiness():
    p = probe(); ready(p)
    assert not p.browser_input({"generation": "old", "type": "ready", "supported": True})
    assert not p.browser_input({"generation": "old", "type": "measurement", "peer": "2",
                               "peer_revision": "theirs", "ping": 35, "samples": 8,
                               "age_seconds": 0, "attempt": "a" * 32})
    assert p.take_measurements() == []


def test_measurement_is_revision_bound_and_consumed_once():
    p = probe(); ready(p)
    body = {"generation": p.generation, "type": "measurement", "peer": "2",
            "peer_revision": "theirs", "ping": 35.5, "samples": 8,
            "age_seconds": 1, "attempt": "a" * 32}
    assert p.browser_input(body)
    rows = p.take_measurements()
    assert len(rows) == 1 and rows[0]["ping"] == 35.5
    assert 1 <= rows[0]["age_seconds"] < 2
    assert rows[0]["transport"] == "webrtc-relay-v1"
    assert p.take_measurements() == [], "Cached readings cannot acquire fresh server timestamps"
    body["peer_revision"] = "old"
    assert not p.browser_input(body)


def test_incomplete_bad_or_old_measurements_are_not_zero():
    p = probe(); ready(p)
    base = {"generation": p.generation, "type": "measurement", "peer": "2",
            "peer_revision": "theirs", "ping": 35, "samples": 8,
            "age_seconds": 0, "attempt": "a" * 32}
    for update in ({"ping": None}, {"ping": True}, {"ping": -1}, {"ping": float("nan")},
                   {"samples": 4}, {"samples": True}, {"age_seconds": 16}, {"peer": []}, {"peer": {}}):
        assert not p.browser_input({**base, **update})
    assert p.take_measurements() == []


def test_queue_exit_and_close_clear_peer_state_and_secrets():
    p = probe(); ready(p)
    assert p.browser_state()["peers"]
    p.set_peers([], False)
    assert p.browser_state()["peers"] == []
    p.close()
    assert p.browser_state()["iceServers"] == []
    assert p.profile()["location"] is None


def test_worker_sends_relay_profile_and_consumes_browser_reports():
    p = probe(); ready(p)
    class Client(_TransportClient):
        def _post(self, path, body=None):
            status, result = super()._post(path, body)
            if path == "/api/network/profile" and not body.get("unavailable"):
                result["steam_id"] = "1"
            return status, result
    client = Client("token", network_config=lambda: {"region": "NA"}, network_probe_factory=lambda _: p,
                    peer_pages=[{"revision": "ours-r1", "queued": True, "peers": [
                        {"steam_id": "2", "revision": "theirs", "location": "b" * 32}], "next_offset": None}])
    client.start()
    try:
        _wait_for(lambda: client.network_status.get("ready"))
        posted = next(body for path, body in client.posts if path == "/api/network/profile")
        assert posted["transport"] == "webrtc-relay-v1"
        assert client.network_browser_state()["queued"] is True
        assert client.network_browser_input({"generation": "old", "type": "ready"})[0] == 409
    finally:
        client.stop()


def test_measurement_uploads_are_bounded_reaged_and_stop_after_generation_change(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(relaytransport, "time", SimpleNamespace(monotonic=lambda: clock[0]),
                        raising=False)
    measurements = [{"steam_id": str(index), "revision": "peer-r", "ping": 30 + index,
                     "samples": 5, "attempt": f"{index:032x}",
                     "transport": "webrtc-relay-v1", "age_seconds": 1.0}
                    for index in range(33)]

    class Probe:
        generation = "generation"
        def needs_credentials(self): return False
        def profile(self): return {"location": self.generation, "age_seconds": 0}
        def set_identity(self, _steam_id, _revision): pass
        def set_peers(self, _peers, _queued): pass
        def take_measurements(self): return [dict(row) for row in measurements]

    class Client:
        def __init__(self, session, change_after_first=False):
            self.session = session
            self.change_after_first = change_after_first
            self.pings = []
        def _network_active(self, session, change):
            return session is self.session and session["change"] == change
        def _get(self, path):
            assert path == "/api/network/peers?offset=0"
            return 200, {"revision": "ours-r1", "queued": True, "peers": []}
        def _post(self, path, body=None):
            if path == "/api/network/profile":
                return 200, {"ready": True, "revision": "ours-r1", "steam_id": "1"}
            assert path == "/api/network/pings"
            self.pings.append(body)
            clock[0] += 1
            if self.change_after_first and len(self.pings) == 1:
                self.session["change"] += 1
            return 200, {"ok": True}
        def _network_response_error(self, _body, fallback): return fallback
        def _network_mark_unavailable(self, *_args): raise AssertionError("unexpected unavailable")
        def _network_set_status(self, *_args, **_kwargs): pass

    def run(change_after_first=False):
        clock[0] = 100.0
        session = {"change": 0, "retry_delay": 0, "unavailable_sent": False}
        client = Client(session, change_after_first)
        relaytransport.refresh(client, session, 0, {"region": "NA", "cross_region": False}, Probe())
        return client.pings

    posts = run()
    assert [len(post["peers"]) for post in posts] == [16, 16, 1]
    assert [[row["age_seconds"] for row in post["peers"]] for post in posts] == [
        [1.0] * 16, [2.0] * 16, [3.0]]
    stopped = run(change_after_first=True)
    assert [len(post["peers"]) for post in stopped] == [16]


def test_unstarted_client_has_no_browser_credentials():
    client = LiveClient("private-token", network_config=lambda: {"region": "NA"})
    assert hasattr(client, "network_browser_state"), "Browser bridge needs an explicit safe state interface"
    assert client.network_browser_state() is None
