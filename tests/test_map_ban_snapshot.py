from tests.test_screen_matchflow import ME, P2, P3, P4, _panel, _srv_lobby
from hub.webui.snapshot import state_snapshot


def test_confirmed_bans_remain_available_after_leaving_the_lobby():
    panel, session = _panel()
    session.on_live_event({"type": "match_ready", **_srv_lobby("veto", match_id="map-ban-test")})
    assert state_snapshot(session, panel)["comp"]["match_id"] == "map-ban-test"
    bans = [{"team": 2, "map": "Airsoft"}]
    session.on_live_event({"type": "lobby", **_srv_lobby("veto", bans=bans)})
    comp = state_snapshot(session, panel)["comp"]
    assert comp["match_id"] == session.match_id
    assert comp["map_bans"] == bans
    session.on_live_event({"type": "match_connecting", "match_id": "map-ban-test", "map": "Hospital"})
    comp = state_snapshot(session, panel)["comp"]
    assert "lobby" not in comp
    assert comp["map_bans"] == bans


def test_recovered_lobby_retains_the_server_match_identity_and_existing_bans():
    panel, session = _panel()
    bans = [{"team": 1, "map": "Rome"}]
    session.on_live_event({"type": "match_ready", **_srv_lobby(
        "veto", match_id="recovered-match", resumed=True, bans=bans)})
    comp = state_snapshot(session, panel)["comp"]
    assert comp["match_id"] == "recovered-match"
    assert comp["map_bans"] == bans


def test_ban_turn_notification_is_addressed_only_to_the_current_captain():
    players = [ME, P2, P3, P4]
    for viewer in players:
        panel, session = _panel()
        session.me = dict(viewer)
        session.on_live_event({"type": "match_ready", "players": players,
                               **_srv_lobby("veto", match_id="captain-sound", ban_turn=2)})
        comp = state_snapshot(session, panel)["comp"]
        assert comp["lobby"]["my_turn"] is (viewer["steam_id"] == P3["steam_id"])
        session.on_live_event({"type": "lobby", **_srv_lobby(
            "veto", match_id="captain-sound", ban_turn=1,
            bans=[{"team": 2, "map": "Airsoft"}])})
        comp = state_snapshot(session, panel)["comp"]
        assert comp["lobby"]["my_turn"] is (viewer["steam_id"] == ME["steam_id"])
