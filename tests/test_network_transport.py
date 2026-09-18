"""Focused transport and lifecycle tests for Steam network measurements."""
import threading
import time

from hub import live as live_mod


def _wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for network worker")


class _Probe:
    def __init__(self, game_dir, profile=None, estimates=None, error=""):
        self.game_dir = game_dir
        self._profile = profile
        self.estimates = estimates or {}
        self.error = error
        self.closed = False

    def profile(self):
        return self._profile

    def estimate(self, marker):
        return self.estimates.get(marker)

    def close(self):
        self.closed = True


class _TransportClient(live_mod.LiveClient):
    def __init__(self, *args, peer_pages=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.posts = []
        self.gets = []
        self.peer_pages = list(peer_pages or [])

    def _run(self):
        self._stop.wait()

    def _post(self, path, body=None):
        self.posts.append((path, body))
        if path == "/api/network/profile" and body != {"unavailable": True}:
            return 200, {"ok": True, "ready": True, "region": body["region"],
                         "revision": "ours-r1"}
        return 200, {"ok": True}

    def _get(self, path):
        self.gets.append(path)
        if self.peer_pages:
            return 200, self.peer_pages.pop(0)
        return 200, {"ok": True, "revision": "ours-r1", "peers": [],
                     "next_offset": None}


def test_combat_warning_ack_posts_both_evidence_ids():
    client = _TransportClient("token")
    assert client.ack_combat_warning("match-7", "warning-3") == (200, {"ok": True})
    assert client.posts == [("/api/match/combat-warning", {
        "match_id": "match-7", "warning_id": "warning-3",
    })]


def test_legacy_client_does_not_start_network_worker():
    client = _TransportClient("token")
    client.start()
    try:
        assert client.network_status == {}
        assert not any(t.name.startswith("hub-network-") for t in threading.enumerate())
    finally:
        client.stop()


def test_worker_uploads_profile_and_paginated_revision_bound_estimates():
    peers_1 = [
        {"player_id": "a1111111-1111-4111-8111-111111111111", "steam_id": "76561198000000002", "location": "marker-2", "revision": "peer-r2"},
        {"steam_id": "3", "location": "missing", "revision": "peer-r3"},
    ]
    peers_2 = [{"steam_id": str(i), "location": "marker-4", "revision": "peer-r4"}
               for i in range(4, 404)]
    probe = _Probe("game-a", {"location": "mine", "age_seconds": 2.5, "region": "NA"},
                   {"marker-2": 42, "marker-4": 77})
    events = []
    client = _TransportClient(
        "token", on_event=events.append,
        network_config=lambda: {"game_dir": "game-a", "region": "EU", "cross_region": True},
        network_probe_factory=lambda path: probe,
        peer_pages=[
            {"ok": True, "revision": "ours-r1", "peers": peers_1, "next_offset": 2},
            {"ok": True, "revision": "ours-r1", "peers": peers_2, "next_offset": None},
        ])
    client.start()
    try:
        _wait_for(lambda: client.network_status.get("ready") is True)
    finally:
        client.stop()

    assert client.gets[:2] == ["/api/network/peers?offset=0", "/api/network/peers?offset=2"]
    assert client.posts[0] == ("/api/network/profile", {
        "region": "EU", "cross_region": True, "location": "mine", "age_seconds": 2.5,
    })
    ping_posts = [body for path, body in client.posts if path == "/api/network/pings"]
    assert ping_posts[0] == {"revision": "ours-r1", "peers": [
        {"player_id": "a1111111-1111-4111-8111-111111111111", "revision": "peer-r2", "ping": 42},
    ]}
    assert len(ping_posts[1]["peers"]) == 400
    assert ping_posts[1]["revision"] == "ours-r1"
    assert client.network_status == {"ready": True, "region": "EU", "error": ""}
    assert events[-1] == {"type": "network_status", "ready": True,
                          "region": "EU", "error": ""}
    assert probe.closed is True


def test_missing_profile_invalidates_server_once_and_reports_probe_error(monkeypatch):
    monkeypatch.setattr(live_mod, "NETWORK_REFRESH_SECONDS", 0.01)
    probe = _Probe("game-a", profile={"location": None, "age_seconds": None, "region": None,
                                      "error": "Steam relay API unavailable"})
    events = []
    client = _TransportClient(
        "token", on_event=events.append,
        network_config=lambda: {"game_dir": "game-a", "region": "NA", "cross_region": False},
        network_probe_factory=lambda path: probe)
    client.start()
    try:
        _wait_for(lambda: sum(path == "/api/network/profile" for path, _ in client.posts) == 1)
        time.sleep(0.05)
    finally:
        client.stop()

    assert [call for call in client.posts if call[0] == "/api/network/profile"] == [
        ("/api/network/profile", {"unavailable": True})]
    assert client.network_status == {
        "ready": False, "region": "NA", "error": "Steam relay API unavailable",
    }
    assert events[-1]["type"] == "network_status"


def test_warming_probe_is_reused_until_steam_produces_a_marker(monkeypatch):
    monkeypatch.setattr(live_mod, "NETWORK_WARMUP_SECONDS", 0.01)

    class _WarmingProbe(_Probe):
        def __init__(self, game_dir):
            super().__init__(game_dir)
            self.reads = 0

        def profile(self):
            self.reads += 1
            if self.reads == 1:
                return {"location": None, "age_seconds": None, "region": None,
                        "error": "Steam relay measurements are not ready yet."}
            return {"location": "mine", "age_seconds": 0.2, "region": None}

    probes = []

    def factory(game_dir):
        probes.append(_WarmingProbe(game_dir))
        return probes[-1]

    client = _TransportClient(
        "token", network_config=lambda: {"game_dir": "game", "region": "NA",
                                          "cross_region": False},
        network_probe_factory=factory)
    client.start()
    try:
        _wait_for(lambda: client.network_status.get("ready") is True)
    finally:
        client.stop()

    assert len(probes) == 1
    assert probes[0].reads >= 2
    assert [body for path, body in client.posts if path == "/api/network/profile"][0] == {
        "unavailable": True,
    }


def test_fatally_initialized_probe_is_recreated_on_normal_refresh(monkeypatch):
    monkeypatch.setattr(live_mod, "NETWORK_REFRESH_SECONDS", 0.01)
    probes = []

    def factory(game_dir):
        if not probes:
            probe = _Probe(game_dir, {
                "location": None, "age_seconds": None, "region": None,
                "error": "The installed game does not contain steam_api64.dll.",
            }, error="The installed game does not contain steam_api64.dll.")
        else:
            probe = _Probe(game_dir, {
                "location": "mine", "age_seconds": 0.1, "region": None,
            })
        probes.append(probe)
        return probe

    client = _TransportClient(
        "token", network_config=lambda: {"game_dir": "game", "region": "NA",
                                          "cross_region": False},
        network_probe_factory=factory)
    client.start()
    try:
        _wait_for(lambda: client.network_status.get("ready") is True)
    finally:
        client.stop()

    assert len(probes) == 2
    assert probes[0].closed is True


def test_failed_unavailable_invalidation_retries_until_acknowledged(monkeypatch):
    monkeypatch.setattr(live_mod, "NETWORK_REFRESH_SECONDS", 0.01)

    class _RetryClient(_TransportClient):
        def _post(self, path, body=None):
            self.posts.append((path, body))
            if path == "/api/network/profile" and body == {"unavailable": True}:
                attempts = sum(call == (path, body) for call in self.posts)
                return ((0, {"error": "offline"}) if attempts == 1
                        else (200, {"ok": True, "ready": False}))
            return super()._post(path, body)

    probe = _Probe("game", {
        "location": None, "age_seconds": None, "region": None,
        "error": "Steam unavailable",
    })
    client = _RetryClient(
        "token", network_config=lambda: {"game_dir": "game", "region": "NA",
                                          "cross_region": False},
        network_probe_factory=lambda path: probe)
    client.start()
    try:
        _wait_for(lambda: sum(call == ("/api/network/profile", {"unavailable": True})
                              for call in client.posts) >= 2)
        time.sleep(0.04)
    finally:
        client.stop()

    unavailable = [call for call in client.posts
                   if call == ("/api/network/profile", {"unavailable": True})]
    assert len(unavailable) == 2


def test_network_changed_invalidates_readiness_and_recreates_probe_for_new_game_dir():
    config = {"game_dir": "game-a", "region": "NA", "cross_region": False}
    probes = []

    def make_probe(game_dir):
        probe = _Probe(game_dir, {"location": "mine-" + game_dir,
                                  "age_seconds": 0, "region": "NA"})
        probes.append(probe)
        return probe

    client = _TransportClient("token", network_config=lambda: dict(config),
                              network_probe_factory=make_probe)
    client.start()
    try:
        _wait_for(lambda: client.network_status.get("ready") is True)
        config["game_dir"] = "game-b"
        client.network_changed()
        assert client.network_status["ready"] is False
        _wait_for(lambda: len(probes) == 2 and client.network_status.get("ready") is True)
    finally:
        client.stop()

    assert [probe.game_dir for probe in probes] == ["game-a", "game-b"]
    assert all(probe.closed for probe in probes)


def test_network_changed_discards_inflight_snapshot_before_it_can_upload():
    config = {"game_dir": "game", "region": "NA", "cross_region": False}
    entered = threading.Event()
    release = threading.Event()

    class _SlowProfile(_Probe):
        def profile(self):
            if not entered.is_set():
                entered.set()
                release.wait(2)
            return {"location": "mine", "age_seconds": 0, "region": None}

    client = _TransportClient("token", network_config=lambda: dict(config),
                              network_probe_factory=lambda path: _SlowProfile(path))
    client.start()
    assert entered.wait(1)
    config["region"] = "EU"
    client.network_changed()
    release.set()
    try:
        _wait_for(lambda: client.network_status.get("ready") is True)
    finally:
        client.stop()

    profiles = [body for path, body in client.posts
                if path == "/api/network/profile" and body != {"unavailable": True}]
    assert profiles
    assert all(body["region"] == "EU" for body in profiles)


def test_enabled_client_refuses_queue_until_network_is_ready():
    client = _TransportClient(
        "token", network_config=lambda: {"game_dir": "game", "region": "NA",
                                          "cross_region": False},
        network_probe_factory=lambda path: _Probe(path, None, error="Steam is not running"))
    status, body = client.join_queue()
    assert status == 409
    assert body == {"ok": False, "error": "Network measurements are not ready."}
    assert client.posts == []

    client.network_status = {"ready": False, "region": "NA", "error": "Steam is not running"}
    status, body = client.join_queue()
    assert status == 409
    assert body == {"ok": False, "error": "Steam is not running"}


def test_probe_region_never_substitutes_for_explicit_configured_region():
    probe = _Probe("game", {"location": "mine", "age_seconds": 0, "region": "EU"})
    client = _TransportClient(
        "token", network_config=lambda: {"game_dir": "game", "region": "",
                                          "cross_region": False},
        network_probe_factory=lambda path: probe)
    client.start()
    try:
        _wait_for(lambda: client.network_status.get("error") ==
                  "Choose a matchmaking region.")
    finally:
        client.stop()

    available_profiles = [body for path, body in client.posts
                          if path == "/api/network/profile" and body != {"unavailable": True}]
    assert available_profiles == []
    assert client.network_status["region"] == ""


def test_prepared_join_is_cancelled_before_scheduled_action_executes():
    client = _TransportClient("token")
    scheduled_join = client.prepare_queue_join()
    client.cancel_pending_queue()

    status, body = scheduled_join()
    assert status == 409
    assert body["cancelled"] is True
    assert client.posts == []


def test_leave_waits_for_inflight_join_before_posting():
    join_entered = threading.Event()
    release_join = threading.Event()

    class _OrderedClient(_TransportClient):
        def _post(self, path, body=None):
            self.posts.append((path, body))
            if path == "/api/queue/join":
                join_entered.set()
                release_join.wait(2)
            return 200, {"ok": True}

    client = _OrderedClient("token")
    join_thread = threading.Thread(target=client.join_queue)
    join_thread.start()
    assert join_entered.wait(1)

    client.cancel_pending_queue()
    leave_thread = threading.Thread(target=client.leave_queue)
    leave_thread.start()
    time.sleep(0.02)
    assert [path for path, _ in client.posts] == ["/api/queue/join"]

    release_join.set()
    join_thread.join(1)
    leave_thread.join(1)
    assert [path for path, _ in client.posts] == ["/api/queue/join", "/api/queue/leave"]


def test_stop_waits_for_inflight_cycle_and_old_generation_cannot_write_after_restart():
    entered = threading.Event()
    release = threading.Event()

    class _BlockingClient(_TransportClient):
        def _get(self, path):
            entered.set()
            release.wait(2)
            return super()._get(path)

    client = _BlockingClient(
        "token", network_config=lambda: {"game_dir": "game", "region": "NA",
                                          "cross_region": False},
        network_probe_factory=lambda path: _Probe(
            path, {"location": "mine", "age_seconds": 0, "region": "NA"}))
    client.start()
    assert entered.wait(1)

    stopped = threading.Event()
    stopper = threading.Thread(target=lambda: (client.stop(), stopped.set()))
    stopper.start()
    time.sleep(0.02)
    assert not stopped.is_set(), "stop returned while a network request was still in flight"
    release.set()
    stopper.join(1)
    assert stopped.is_set()
    writes_at_stop = len(client.posts)
    time.sleep(0.02)
    assert len(client.posts) == writes_at_stop

    client.start()
    try:
        _wait_for(lambda: client.network_status.get("ready") is True)
    finally:
        client.stop()
