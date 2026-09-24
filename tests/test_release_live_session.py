"""Real matches cannot be completed by the local preview controls."""
import copy

import pytest

from test_screen_matchflow import _panel
from hub.webui.bridge import Api
from hub.webui.screens.competitive import live_snapshot


def test_live_match_does_not_expose_preview_finish():
    panel, session = _panel()
    session.phase = "live"
    assert session.mock is False
    assert live_snapshot(session)["can_finish"] is False


@pytest.mark.parametrize("through_bridge", [False, True])
def test_preview_finish_cannot_fabricate_a_live_result(through_bridge, monkeypatch):
    panel, session = _panel()
    session.phase = "live"
    session.history = []
    before = copy.deepcopy(session.me)
    completed = []
    monkeypatch.setattr(session, "register_match_complete", completed.append)
    if through_bridge:
        Api(panel).finish_match()
    else:
        session.finish()
    assert session.phase == "live"
    assert not session.result
    assert not session.history
    assert session.me == before
    assert not completed


def test_live_votes_do_not_invent_other_players_decisions():
    panel, session = _panel()
    session.phase = "live"
    session.match_id = "0123456789abcdef"
    sent = []
    session.client.void_vote = lambda mid, yes=None: (sent.append((mid, yes)) or (200, {"ok": True}))
    Api(panel).start_vote()
    assert session.vote is None
    session.vote = {"caller": "Player", "yes": 0, "no": 0, "voted": False}
    Api(panel).cast_vote(True)
    assert session.vote == {"caller": "Player", "yes": 0, "no": 0, "voted": False}
    assert sent == [(session.match_id, None), (session.match_id, True)]


def test_live_roster_uses_only_reported_rank_and_ping():
    _, session = _panel()
    player = session._player_from({"steam_id": "76561198000000001", "persona": "Player"})
    assert player["level"] is None and player["ping"] is None
    player = session._player_from({"steam_id": "76561198000000001", "level": 4, "ping": 23})
    assert player["level"] == 4 and player["ping"] == 23


def test_live_queue_does_not_run_simulated_integrity_steps(monkeypatch):
    _, session = _panel()
    for name, value in (("banned_left", 0),
                        ("gamemode_installed", True), ("update_needed", None)):
        monkeypatch.setattr(session, name, lambda v=value: v)
    passed = []
    timers = []
    monkeypatch.setattr(session, "_checks_passed", lambda: passed.append(True))
    monkeypatch.setattr(session, "_later", lambda ms, fn: timers.append(fn))
    session.find_match()
    assert session.phase == "checking"
    assert passed == [True]
    assert session._check_next not in timers


def test_classic_recovery_interface_refuses_ranked_entry():
    panel, session = _panel()
    panel.supports_matchmaking = False
    session.find_match()
    assert session.phase == 'idle'
    assert session.error


def test_open_game_explains_running_game_without_requesting_launch(monkeypatch):
    from hub import competitive
    _, session = _panel()
    session.phase = 'live'
    launches = []
    monkeypatch.setattr(competitive.game_mod, 'game_running', lambda: True)
    monkeypatch.setattr(competitive.game_mod, 'launch_game', lambda: launches.append(True))
    session.relaunch_game()
    assert not launches
    assert session.error
