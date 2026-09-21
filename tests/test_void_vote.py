"""UTF-8. Live voting and the normal result/cleanup flow, without launching a game."""
from test_screen_matchflow import _panel, ME
from hub.webui.screens.competitive import live_snapshot


def test_live_vote_uses_server_and_rejects_stale_replies():
    panel, session = _panel()
    session.phase = "live"
    session.match_id = "0123456789abcdef"
    calls = []
    session.client.void_vote = lambda mid, yes=None: (calls.append((mid, yes)) or (200, {
        "ok": True, "match_id": mid, "vote": {"yes": 0, "no": 0, "voted": False}}))
    session.start_vote()
    assert calls == [(session.match_id, None)]
    assert session.vote["yes"] == 0
    session.cast_vote(True)
    assert calls[-1] == (session.match_id, True)
    session.on_live_event({"type": "match_void_vote", "match_id": session.match_id,
                           "vote": {"yes": 6, "no": 0, "voted": True}})
    session.cast_vote(True)
    assert len(calls) == 2
    session.on_live_event({"type": "match_void_vote", "match_id": "fedcba9876543210",
                           "vote": {"yes": 7, "voted": False}})
    session._void_vote_result(session.match_id, 200, {"vote": {"yes": 1, "no": 0, "voted": False}})
    assert session.vote["yes"] == 6 and session.vote["voted"] is True
    assert session.phase == "live"
    assert live_snapshot(session)["can_vote"] is True


def test_void_result_opens_persistent_overlay_and_arms_existing_cleanup(monkeypatch):
    panel, session = _panel()
    session.phase = "live"
    session.match_id = "0123456789abcdef"
    session.vote = {"yes": 6, "no": 0, "voted": True}
    original_rank = dict(session.me)
    completed = []
    monkeypatch.setattr(session, "register_match_complete", completed.append)
    session.on_live_event({"type": "match_result", "match_id": session.match_id,
                           "voided": True, "void_reason": "vote", "rr_delta": 0,
                           "close_allowed": True, "data_collected": True})
    assert session.vote is None
    assert completed == ["match_result"]
    assert session.postmatch["voided"] is True
    assert session.postmatch["score"] is None
    assert session.me == original_rank
    session.on_live_event({"type": "match_over", "match_id": session.match_id})
    assert session.postmatch["voided"] is True


def test_preview_never_generates_other_peoples_votes():
    from test_screen_postmatch import _StubPanel
    from hub.competitive import MockSession
    session = MockSession(_StubPanel())
    session.phase = "live"
    session.me = dict(ME)
    callbacks = []
    session._later = lambda delay, callback: callbacks.append(callback)
    session.start_vote()
    session.cast_vote(True)
    for callback in callbacks:
        callback()
    assert session.vote["yes"] == 1
    assert session.phase == "live"


def test_void_history_is_final_without_a_win_loss_or_score():
    from hub.webui.screens.history import _row
    row = _row({"id": "0123456789abcdef", "outcome": "voided", "voided": True,
                "won": None, "score": None, "rr_delta": 0})
    assert row["result"] == "voided"
    assert row["score"] is None and row["won"] is None
    from hub.competitive import profile_stats
    stats = profile_stats([row])
    assert stats["recorded"] == 1
    assert stats["played"] == stats["undecided"] == stats["wins"] == stats["losses"] == 0
