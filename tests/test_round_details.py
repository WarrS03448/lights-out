from hub.webui.screens import history as H, postmatch as P
from tests.test_screen_history import _record
from tests.test_screen_postmatch import _session, _board_event


def round_record():
    return {"n": 2, "won": 2, "score": [1, 1], "seconds": 64,
            "scoreboard": [{"steam_id": "1" * 17, "team": 1, "reported": True,
                            "kills": -1, "deaths": 1, "team_kills": 1,
                            "combat": {"status": "partial", "enemyDamage": 35,
                                       "playerStats": [{"steam_id": "2" * 17,
                                                        "damageDealt": 35, "damageTaken": None}]}}]}


def test_round_keeps_player_detail_without_using_match_totals_for_missing_players():
    rec = _record(round_details=[round_record()])
    rec["players"][1]["combat"] = {"status": "complete", "enemyDamage": 900}
    detail = H._detail(rec, "1" * 17)
    round_ = detail["round_details"][1]
    assert round_["n"] == 2 and round_["score"] == [1, 1]
    mine = next(p for p in round_["scoreboard"] if p["is_me"])
    assert (mine["kills"], mine["deaths"], mine["kd"]) == (-1, 1, -1)
    assert mine["combat"]["playerStats"][0]["name"] == "them"
    theirs = next(p for p in round_["scoreboard"] if not p["is_me"])
    assert theirs["kills"] is None and theirs["combat"] is None
    assert detail["scoreboard"][0]["kills"] == 14


def test_legacy_rounds_remain_selectable_without_inventing_stats_or_batch_order():
    detail = H._detail(_record(rounds=[{"won": 1, "steps": 2, "1": 2, "2": 1},
                                        {"won": 2, "steps": 1, "1": 2, "2": 1}]), "1" * 17)
    assert len(detail["round_details"]) == 10
    assert all(r["won"] is None for r in detail["round_details"][:3])
    assert all(p["kills"] is None for r in detail["round_details"] for p in r["scoreboard"])


def test_round_details_survive_immediate_result_and_match_reset():
    s = _session()
    r = round_record()
    r["scoreboard"][0]["steam_id"] = "76561198000999000"
    s.on_live_event(_board_event(round_details=[r], rounds_played=2, score=[1, 1]))
    card = P.snapshot(s, None)["postmatch"]["card"]
    assert len(card["round_details"]) == 2
    assert card["round_details"][1]["scoreboard"][0]["kills"] == -1
    assert card["teams"]["1"][0]["kills"] == 21
    frozen = s.postmatch
    s.reset_match()
    assert s.postmatch is frozen
    assert P.snapshot(s, None)["postmatch"]["card"]["round_details"] == card["round_details"]
