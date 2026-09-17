"""Run: python -m pytest tests/test_lobby_chat.py -q (UTF-8 sources)."""
from tests.test_screen_matchflow import _panel, _seat_a_lobby
from hub.webui.bridge import Api
from hub.webui.snapshot import state_snapshot


def lobby(panel, session):
    return state_snapshot(session, panel)["comp"]["lobby"]


def relay(session, channel, sid, text):
    session._chat_line({"channel": channel, "steam_id": sid, "name": "Wario", "text": text})


def test_combined_chat_keeps_arrival_order_and_audience_without_leaking_enemy_names():
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    relay(s, "all", "3", "first")
    relay(s, "team", "2", "second")
    relay(s, "all", "3", "third")
    lines = lobby(panel, s)["messages"][-3:]
    assert [m["text"] for m in lines] == ["first", "second", "third"]
    assert [m["channel"] for m in lines] == ["all", "team", "all"]
    assert lines[0]["name"] == lines[2]["name"] == "Alpha"


def test_mute_hides_old_and_new_messages_from_both_channels_and_unmute_restores_them():
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    relay(s, "team", "2", "old team")
    relay(s, "all", "2", "old all")
    Api(panel).toggle_chat_mute("2")
    relay(s, "team", "2", "new team")
    relay(s, "all", "3", "visible")
    lb = lobby(panel, s)
    assert [m["text"] for m in lb["messages"] if m["name"]] == ["visible"]
    assert lb["muted_players"] == ["2"]
    Api(panel).toggle_chat_mute("2")
    assert [m["text"] for m in lobby(panel, s)["messages"] if m["name"]] == [
        "old team", "old all", "new team", "visible"]


def test_chat_controls_default_to_team_and_reset_for_next_match():
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    api = Api(panel)
    assert lobby(panel, s)["chat_channel"] == "team"
    epoch = lobby(panel, s)["chat_epoch"]
    api.set_chat_channel("all")
    api.toggle_chat_mute("3")
    assert lobby(panel, s)["chat_channel"] == "all"
    assert lobby(panel, s)["muted_players"] == ["3"]
    api.set_chat_channel("typo")
    assert lobby(panel, s)["chat_channel"] == "team"
    api.toggle_chat_mute(s.me["steam_id"])
    api.toggle_chat_mute("stranger")
    assert lobby(panel, s)["muted_players"] == ["3"]
    s.reset_match()
    _seat_a_lobby(s, "coin")
    assert lobby(panel, s)["chat_channel"] == "team"
    assert lobby(panel, s)["muted_players"] == []
    assert lobby(panel, s)["chat_epoch"] != epoch, "new match must discard browser drafts"


def test_failed_send_notice_keeps_its_place_in_combined_chat():
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    relay(s, "all", "3", "first")
    s._chat_refused("team", 429, {"error": "slow down"})
    relay(s, "all", "3", "last")
    assert [m["text"] for m in lobby(panel, s)["messages"][-3:]] == [
        "first", "slow down", "last"]
