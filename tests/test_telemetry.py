"""Run: python -m pytest tests/test_telemetry.py. Uses only an isolated temp outbox."""
import json
import urllib.error
import pytest
from hub import telemetry

class Response:
    status = 200
    def __init__(self, body): self.body = json.dumps(body).encode("utf-8")
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.body

def make(tmp_path, sender=None, **limits):
    return telemetry._Telemetry(tmp_path / "telemetry.json", open_url=sender, background=False, **limits)

def rows(tmp_path):
    return json.loads((tmp_path / "telemetry.json").read_text(encoding="utf-8"))["events"]

def test_inactive_then_unclean_restart_preserves_ids(tmp_path):
    a = make(tmp_path)
    assert a.emit("ui.action", action="find_match") is False
    a.start(); a.identify("same-token"); a.emit("ui.action", action="find_match")
    ids = {r["id"] for r in rows(tmp_path)}
    b = make(tmp_path); b.start()
    assert ids <= {r["id"] for r in rows(tmp_path)}
    assert any(r["type"] == "session.previous_unclean" for r in rows(tmp_path))

def test_clean_exit_retains_end_event(tmp_path):
    a = make(tmp_path); a.start(); a.identify("same-token"); a.shutdown()
    assert json.loads((tmp_path / "telemetry.json").read_text(encoding="utf-8"))["open_session"] is None
    b = make(tmp_path); b.start()
    assert any(r["type"] == "session.end" for r in rows(tmp_path))
    assert not any(r["type"] == "session.previous_unclean" for r in rows(tmp_path))

def test_privacy_and_nonfinite_fields(tmp_path):
    a = make(tmp_path); a.start(); a.identify("private-token")
    a.emit("request.outcome", action="queue", count=2, duration_ms=float("nan"), reason="C:/Users/private", token="private-token", message="private words")
    assert rows(tmp_path)[-1]["data"] == {"action": "queue", "count": 2}
    assert "private-token" not in (tmp_path / "telemetry.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("address", ["203.0.113.10", "2001:db8::1"])
def test_ip_addresses_are_absent_from_disk_outbox_and_uploaded_batch(tmp_path, address):
    uploaded = []
    def sender(req, timeout):
        payload = json.loads(req.data)
        uploaded.append(payload)
        return Response({"ok": True, "accepted": [row["id"] for row in payload["events"]]})
    a = make(tmp_path, sender)
    a.start(); a.identify("local-test-account")
    a.emit("request.outcome", action="queue", status=200, reason=address,
           error_class=address, ip=address, client_ip=address,
           headers={"x-forwarded-for": address}, url="https://example.test/?ip=" + address)
    assert rows(tmp_path)[-1]["data"] == {"action": "queue", "status": 200}
    assert address not in (tmp_path / "telemetry.json").read_text(encoding="utf-8")
    assert a._flush_once() is True
    assert uploaded and address not in json.dumps(uploaded)

def test_retry_ack_only_and_account_isolation(tmp_path):
    sent = []
    def sender(req, timeout):
        assert timeout == 5
        events = json.loads(req.data)["events"]; sent.append((req.get_header("Authorization"), events))
        if len(sent) == 1: raise urllib.error.URLError("offline")
        return Response({"ok": True, "accepted": [events[0]["id"]]})
    a = make(tmp_path, sender); a.start(); a.identify("old-token")
    a.emit("ui.action", action="old_action")
    old = rows(tmp_path)[-1]["id"]
    assert a._flush_once() is False
    assert old in {r["id"] for r in rows(tmp_path)}
    a.identify("new-token"); a.emit("ui.action", action="new_action")
    new = rows(tmp_path)[-1]["id"]
    assert a._flush_once() is True
    assert sent[-1][0] == "Bearer new-token"
    assert old not in {r["id"] for r in sent[-1][1]}
    assert old not in {r["id"] for r in rows(tmp_path)}
    assert any(r["type"] == "telemetry.gap" for r in sent[-1][1])
    assert "_owner" not in sent[-1][1][0]

def test_batch_and_disk_bounds(tmp_path):
    sent = []
    def sender(req, timeout):
        events = json.loads(req.data)["events"]; sent.append(events)
        return Response({"ok": True, "accepted": [r["id"] for r in events]})
    a = make(tmp_path, sender); a.start(); a.identify("token")
    for _ in range(48): a.emit("ui.action", action="queue")
    a._flush_once(); assert len(sent[0]) == 40
    a = make(tmp_path, max_events=6, max_bytes=1800); a.start(); a.identify("token")
    for _ in range(20): a.emit("ui.action", action="x"*80)
    assert len(rows(tmp_path)) <= 6
    assert (tmp_path / "telemetry.json").stat().st_size <= 1800
    assert any(r["type"] == "telemetry.gap" for r in rows(tmp_path))

def test_disk_failure_does_not_escape(tmp_path):
    blocked = tmp_path / "blocked"; blocked.write_text("file", encoding="utf-8")
    a = telemetry._Telemetry(blocked / "outbox.json", background=False)
    a.start(); a.identify("token"); a.emit("ui.action", action="queue"); a.shutdown()


def test_ui_emit_does_not_write_disk_and_prose_is_excluded(tmp_path, monkeypatch):
    a = make(tmp_path); a.start()
    a._background = True
    writes = []
    monkeypatch.setattr(a, "_save_locked", lambda: writes.append(True))
    a.emit("app.error", reason="Connection reset by peer", error_class="private error words", code="uncaught_thread", action="combat-warning", phase="phase2")
    assert writes == []
    assert a._events[-1]["data"] == {"code": "uncaught_thread", "action":"combat-warning", "phase":"phase2"}


def test_previous_anonymous_rows_are_not_adopted(tmp_path):
    a = make(tmp_path); a.start(); a.emit("ui.action", action="old_anonymous")
    old = a._events[-1]["id"]
    b = make(tmp_path); b.start(); b.identify("account")
    assert old not in {r["id"] for r in b._events}
    assert any(r["type"] == "session.previous_unclean" for r in b._events)


def test_worker_coalesces_burst_and_uploads_persisted_events(tmp_path):
    import threading
    signal = threading.Event(); sent = []
    def sender(req, timeout):
        events = json.loads(req.data)["events"]; sent.append(events); signal.set()
        return Response({"ok": True, "accepted": [r["id"] for r in events]})
    a = telemetry._Telemetry(tmp_path / "telemetry.json", open_url=sender)
    try:
        a.start(); a.identify("account")
        for _ in range(20): a.emit("ui.action", action="queue")
        assert signal.wait(3)
        assert len(sent) == 1 and len(sent[0]) == 21
    finally:
        a.shutdown(); a._thread.join(1)
