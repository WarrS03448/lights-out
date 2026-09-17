"""Run: python -m pytest tests/test_player_damage_display.py (UTF-8)."""
import pytest
from hub.webui.screens import history as H, postmatch as P
from tests.test_screen_history import _record

A, B = "1" * 17, "2" * 17
NAME = '<img src=x onerror="window.bad=1">'


@pytest.mark.parametrize("screen", ["history", "postmatch"])
def test_player_names_and_two_damage_directions_reach_both_details(screen):
    combat = {"status": "partial", "enemyDamage": 30, "damageTaken": 17,
              "playerStats": [{"steam_id": B, "name": "WRONG", "damageDealt": 30, "damageTaken": 17},
                              {"steam_id": "", "damageDealt": None, "damageTaken": 5}],
              "weaponStats": [{"weapon": "/BodycamAnimationFramework/Blueprints/Bodycam_Player.Bodycam_Player_C"}]}
    if screen == "history":
        record = _record()
        record["players"][1]["persona"] = NAME
        record["scoreboard"][0]["combat"] = combat
        mine = H._detail(record, A)["scoreboard"][0]
    else:
        record = {"teams": {1: [{"steam_id": A, "name": "me"}], 2: [{"steam_id": B, "name": NAME}]},
                  "scoreboard": [{"steam_id": A, "combat": combat}]}
        mine = P._card(record)["teams"]["1"][0]
    rows = mine["combat"]["playerStats"]
    assert rows[0] == {"steam_id": B, "name": NAME, "damageDealt": 30, "damageTaken": 17}
    assert rows[1] == {"steam_id": "", "name": "", "damageDealt": None, "damageTaken": 5}


@pytest.mark.parametrize("module", [H, P])
def test_old_summary_does_not_relabel_weapon_as_a_player(module):
    value = module._combat({"status": "partial", "weaponStats": [{"weapon": "Bodycam_Player_C"}]})
    assert value["playerStats"] == []


def test_bad_rows_and_numbers_cannot_turn_into_damage_or_display_names():
    from hub.webui.combat import player_damage_rows
    rows = player_damage_rows({"playerStats": [None, "bad", {"steam_id": "Bodycam_Player_C"},
        {"steam_id": B, "name": "fake", "damageDealt": float("inf"), "damageTaken": -8},
        {"steam_id": A, "damageDealt": True, "damageTaken": "12.5"}]}, {B: {"name": "friend"}})
    assert rows == [{"steam_id": B, "name": "friend", "damageDealt": None, "damageTaken": None},
                    {"steam_id": A, "name": A, "damageDealt": None, "damageTaken": 12.5}]


def test_new_damage_labels_match_in_both_screens_in_all_seven_languages():
    keys = ("player_damage", "damage_player", "damage_to", "damage_from", "unknown_source", "player_damage_unavailable")
    for lang in ("de", "en", "es", "fr", "pt", "ru", "zh"):
        for key in keys:
            assert H.strings_for(lang)[key] == P.strings_for(lang)[key]
            assert H.strings_for(lang)[key]
        if lang != "en":
            assert H.strings_for(lang)["damage_to"] != H.strings_for("en")["damage_to"]
