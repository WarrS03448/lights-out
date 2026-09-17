"""UTF-8. Run: .venv/Scripts/python -m pytest tests/test_report_notes.py"""
from tests.test_screen_bugreport import _panel
from hub.webui.bridge import Api


def test_report_note_reaches_client_through_bridge():
    panel, session = _panel()
    calls = []
    session.client.report = lambda *args: (calls.append(args) or (200, {"ok": True}))
    session.open_report("76561198000999001", "saved", "Friend")
    Api(panel).report("76561198000999001", "other", "saved", "My explanation")
    assert calls == [("76561198000999001", "other", "saved", "My explanation")]
    assert not session.report_target


def test_failed_report_keeps_picker_open():
    panel, session = _panel()
    session.client.report = lambda *args: (503, {})
    session.open_report("76561198000999001", "saved", "Friend")
    session.report_player("76561198000999001", "other", "saved", "My explanation")
    assert session.report_target == "76561198000999001"
    assert session.report_error
