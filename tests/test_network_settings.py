"""Focused persistence and settings tests for regional matchmaking preferences."""
import json
import os
import tempfile

os.environ.setdefault("HUB_STATE_DIR", tempfile.mkdtemp(prefix="hub-network-settings-test-"))

from hub import state as state_mod
from hub.webui.screens import SCREEN_VERBS
from hub.webui.screens import settings as settings_mod


class _Client:
    def __init__(self, status=None):
        self.network_status = status or {}
        self.changed = 0

    def network_changed(self):
        self.changed += 1


class _Session:
    def __init__(self, phase="idle", locked=False, status=None):
        self.phase = phase
        self._locked = locked
        self.client = _Client(status)
        self.cancelled = 0
        self.me = None
        self.connected = True

    def locked_in(self):
        return self._locked

    def cancel_queue(self):
        self.cancelled += 1
        self.phase = "idle"


class _App:
    def __init__(self, state=None):
        self.state = state if state is not None else state_mod.default_state()
        self.game_dir = None


class _Panel:
    def __init__(self, session=None, state=None):
        self.session = session or _Session()
        self.app = _App(state)
        self.changes = 0
        self.window = None

    def post(self, fn):
        fn()

    def on_change(self):
        self.changes += 1


def test_network_preferences_round_trip_and_reject_invalid_types(tmp_path):
    path = tmp_path / "state.json"
    saved = state_mod.default_state()
    saved["matchmaking_region"] = "EU"
    saved["matchmaking_cross_region"] = True
    state_mod.save(saved, path)
    loaded = state_mod.load(path)
    assert loaded["matchmaking_region"] == "EU"
    assert loaded["matchmaking_cross_region"] is True

    path.write_text(json.dumps({
        "matchmaking_region": "XX",
        "matchmaking_cross_region": 1,
    }), encoding="utf-8")
    loaded = state_mod.load(path)
    assert loaded["matchmaking_region"] == ""
    assert loaded["matchmaking_cross_region"] is False

    path.write_text(json.dumps({
        "matchmaking_region": "eu",
        "matchmaking_cross_region": "true",
    }), encoding="utf-8")
    loaded = state_mod.load(path)
    assert loaded["matchmaking_region"] == ""
    assert loaded["matchmaking_cross_region"] is False


def test_network_snapshot_reports_preparing_unavailable_and_ready(monkeypatch):
    monkeypatch.setattr(settings_mod.game_mod, "game_running_cached", lambda *_: False)
    panel = _Panel(_Session(status={}), state_mod.default_state())
    network = settings_mod.snapshot(panel.session, panel)["settings"]["network"]
    assert network["status"] == "preparing"
    assert network["region"] == ""
    assert network["same_region_default"] is True
    assert network["options"][0] == {"code": "", "name": "Select region"}
    assert [option["code"] for option in network["options"]] == [
        "", "NA", "SA", "EU", "AS", "OC", "AF", "ME"
    ]

    panel.session.client.network_status = {"ready": False, "region": "EU", "error": "Steam unavailable"}
    network = settings_mod.snapshot(panel.session, panel)["settings"]["network"]
    assert network["status"] == "unavailable"
    assert network["suggested_region"] == "EU"
    assert network["error"] == "Steam unavailable"

    panel.session.client.network_status = {"ready": True, "region": "EU", "error": ""}
    network = settings_mod.snapshot(panel.session, panel)["settings"]["network"]
    assert network["status"] == "ready"
    assert network["suggested_region"] == "EU"


def test_network_settings_verbs_cancel_queue_persist_and_notify(monkeypatch):
    saves = []
    monkeypatch.setattr(settings_mod.state_mod, "save", lambda value: saves.append(dict(value)))
    session = _Session(phase="queued", status={"ready": True, "region": "NA", "error": ""})
    panel = _Panel(session, state_mod.default_state())

    SCREEN_VERBS["settings_set_matchmaking_region"](panel, "EU")
    assert session.cancelled == 1
    assert panel.app.state["matchmaking_region"] == "EU"
    assert session.client.changed == 1
    assert saves[-1]["matchmaking_region"] == "EU"

    SCREEN_VERBS["settings_set_matchmaking_cross_region"](panel, True)
    assert panel.app.state["matchmaking_cross_region"] is True
    assert session.client.changed == 2

    SCREEN_VERBS["settings_set_matchmaking_region"](panel, "XX")
    SCREEN_VERBS["settings_set_matchmaking_cross_region"](panel, 1)
    assert panel.app.state["matchmaking_region"] == "EU"
    assert panel.app.state["matchmaking_cross_region"] is True
    assert session.client.changed == 2


def test_network_settings_verbs_refuse_locked_match(monkeypatch):
    monkeypatch.setattr(settings_mod.state_mod, "save", lambda *_: (_ for _ in ()).throw(
        AssertionError("locked preference must not save")))
    for phase in ("found", "lobby", "connecting", "live"):
        session = _Session(phase=phase, locked=False)
        panel = _Panel(session, state_mod.default_state())
        SCREEN_VERBS["settings_set_matchmaking_region"](panel, "EU")
        SCREEN_VERBS["settings_set_matchmaking_cross_region"](panel, True)
        assert panel.app.state["matchmaking_region"] == ""
        assert panel.app.state["matchmaking_cross_region"] is False
        assert session.client.changed == 0

    session = _Session(phase="idle", locked=True)
    panel = _Panel(session, state_mod.default_state())
    SCREEN_VERBS["settings_set_matchmaking_region"](panel, "EU")
    assert panel.app.state["matchmaking_region"] == ""
