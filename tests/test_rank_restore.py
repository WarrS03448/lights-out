"""Rank state must survive the background validation of a saved Steam login."""
import pytest

from test_hub import _live_session
from hub.webui.snapshot import _auth


@pytest.mark.parametrize("rating", [
    {"placing": True, "placements_left": 3, "matches": 2, "wins": 1, "level": None},
    {"placing": False, "placements_left": 0, "matches": 5, "wins": 3,
     "level": 4, "rank": 4, "rank_name": "Veteran", "division": 2, "rr": 35},
])
def test_background_account_validation_preserves_live_rank(rating):
    session, panel = _live_session()
    session.on_live_event({"type": "rating", **rating})
    session.on_live_event({"type": "stats", "online": 7, "queued": 2, "live_matches": 1})
    before = _auth(session)
    client = session.client
    session.adopt_account({"steam_id": session.me["steam_id"], "persona": "Fresh name",
                           "avatar": "fresh-avatar", "token": session.token}, save=True)
    after = _auth(session)
    for key in ("rank", "placing", "placements_left", "matches", "wins", "level"):
        assert after[key] == before[key], key
    assert after["persona"] == "Fresh name"
    assert after["avatar"] == "fresh-avatar"
    assert session.client is client
    assert session.online == 7 and session.stats_ready


def test_new_live_identity_does_not_inherit_preview_or_previous_rank(monkeypatch):
    session, panel = _live_session()
    session.on_live_event({"type": "rating", "placing": False, "rank_name": "Veteran",
                           "level": 4, "matches": 5})
    old_client = session.client
    monkeypatch.setattr(session, "_connect", lambda: None)
    session.adopt_account({"steam_id": "76561198000000001", "persona": "Other", "token": "new"})
    assert _auth(session)["rank"] is None
    assert session.me.get("level") is None
    assert session.me.get("matches") is None
    assert "stop" in old_client.calls
