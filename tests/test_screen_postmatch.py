#!/usr/bin/env python3.12
"""Headless tests for the post-match card.  Run:  python3.12 tests/test_screen_postmatch.py

SAM, 2026-09-16: "an immediate post match Victory or Defeat screen in the app that comes up after
the game ends ... and stays there until its closed manually, either by clicking off the window or
hitting an X button. make it look similar to the match history match detail screen."

Three of those clauses are testable with no window, and all three are here:

  * IMMEDIATE — the card exists the moment `match_result` lands, built from what the session
    already holds. No fetch, no second event.
  * STAYS — the hard one, and the one a later change is most likely to undo by accident. The card
    survives leaving the result screen, resetting the match, queueing again and any number of
    snapshot pushes. ONLY close_postmatch takes it down.
  * LIKE THE MATCH DETAIL — the slice carries the same sections history.py's `_detail` does (map,
    scoreline, both teams with their sides, the veto), with the same honest nulls.

Nothing here imports pywebview or opens a Tk window.
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hub import i18n                                          # noqa: E402
from hub import competitive as C                              # noqa: E402
from hub.webui.screens import postmatch as P                  # noqa: E402
from hub.webui.screens import SCREEN_SNAPSHOTS, SCREEN_VERBS  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = []


def test(fn):
    try:
        fn()
    except Exception:
        import traceback
        RESULTS.append((fn.__name__, False))
        print("FAIL  " + fn.__name__)
        traceback.print_exc()
    else:
        RESULTS.append((fn.__name__, True))
        print("ok    " + fn.__name__)


# ---------------------------------------------------------------- fakes
class _StubPanel:
    """The handful of panel calls a session makes, with timers that never fire on their own."""

    def __init__(self):
        self.changes = 0

    def after(self, ms, fn):
        return "t%d" % ms

    def after_cancel(self, handle):
        pass

    def on_change(self):
        self.changes += 1

    def map_pool(self):
        return ["Rome", "Warehouse", "Rooftops"]

    def session_changed(self):
        self.changes += 1


class RecPanel:
    """A panel whose post() runs immediately on this thread (like InlineScheduler)."""

    def __init__(self, session):
        self.session = session

    def post(self, fn):
        fn()


def _session():
    """A live session sitting in a finished-looking match: ten players, two teams, a veto."""
    s = C.LiveSession(_StubPanel())
    s._action = lambda call, on_result=None: (lambda r: on_result(*r) if on_result else None)(call())
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "matches": 34, "wins": 19}
    s.token = "tok"
    s.phase = "live"
    s.map = "Rome"
    s.match_id = "ch-1a2b3c"
    s.teams = {
        1: [{"steam_id": "76561198000999000", "name": "Sam"},
            {"steam_id": "76561198000000002", "name": "Vex"}],
        2: [{"steam_id": "76561198000000003", "name": "Nyx"},
            {"steam_id": "76561198000000004", "name": "Ivo"}],
    }
    s.sides = {1: "attack", 2: "defend"}
    s.bans = [(1, "Warehouse"), (2, "Rooftops")]
    return s


def _result_event(**over):
    """A `match_result` in the shape docs/match-result.md hop 2 specifies."""
    event = {"type": "match_result", "match_id": "ch-1a2b3c", "map": "Rome",
             "winner": 1, "score": [7, 4], "seconds": 2280, "voided": False,
             "you": {"arrows": 2, "rank": 5, "matches": 35, "wins": 20}}
    event.update(over)
    return event


def _card(session):
    return P.snapshot(session, None)["postmatch"]


# ---------------------------------------------------------------- it comes up
def test_registered_under_postmatch():
    assert "postmatch" in SCREEN_SNAPSHOTS, "postmatch did not register a snapshot contributor"
    assert SCREEN_SNAPSHOTS["postmatch"] is P.snapshot


def test_nothing_is_open_before_a_match_ends():
    s = _session()
    slice_ = _card(s)
    assert slice_["open"] is False and slice_["card"] is None


def test_a_win_raises_the_card_on_the_result_event():
    """IMMEDIATE: one event in, a complete card out. No second event, no fetch."""
    s = _session()
    s.on_live_event(_result_event())
    slice_ = _card(s)
    assert slice_["open"] is True
    card = slice_["card"]
    assert card["won"] is True and card["voided"] is False
    assert card["map"] == "Rome" and card["match_id"] == "ch-1a2b3c"
    assert card["score"] == [7, 4] and card["winner"] == 1 and card["my_team"] == 1
    assert card["delta"] == 2
    assert card["duration"]                                   # 2280 s, formatted in Python


def test_a_loss_reads_from_the_losing_side():
    """The same match, seen by someone on team 2: Defeat, and the scoreline stays BY TEAM so the
    two columns underneath it still line up."""
    s = _session()
    s.me = {"name": "Nyx", "steam_id": "76561198000000003"}
    s.on_live_event(_result_event())
    card = _card(s)["card"]
    assert card["won"] is False and card["my_team"] == 2 and card["winner"] == 1
    assert card["score"] == [7, 4]                            # team1 : team2, not ours : theirs


def test_an_explicit_won_flag_beats_a_missing_winner():
    """THE SHAPE THE SERVER ACTUALLY SENDS TODAY (server/live.cjs settle()): `won` per player and
    no `winner` at all. Derived from `winner` alone, every one of the ten clients reads False -
    the whole match told it lost - so the explicit flag has to win where there is one."""
    s = _session()
    s.on_live_event({"type": "match_result", "match_id": "ch-1a2b3c", "won": True, "delta": 2,
                     "you": {"arrows": 2}})
    card = _card(s)["card"]
    assert card["won"] is True
    assert card["winner"] == 1                                # derived from our own result
    assert card["score"] is None                              # nothing reported one: say so


def test_a_voided_match_claims_neither_victory_nor_defeat():
    s = _session()
    s.on_live_event(_result_event(voided=True, void_reason="vote"))
    card = _card(s)["card"]
    assert card["voided"] is True and card["won"] is False
    assert card["score"] is None, "0:0 is a placeholder, not a scoreline"
    assert card["delta"] == 0


# ---------------------------------------------------------------- the rank line
def test_the_card_carries_the_rr_the_match_moved():
    """SAM, 2026-09-16: "we are only gaining and losing 1-3 RR". The card printed `delta` - the
    arrow count - with "RR" after it. It carries `rr_delta` now, the RR the service moved."""
    s = _session()
    s.on_live_event(_result_event(you={"arrows": 2, "rr_delta": 23, "rank": 4, "division": 2,
                                       "rank_name": "Veteran", "placing": False}))
    card = _card(s)["card"]
    assert card["rr_delta"] == 23, card
    assert card["delta"] == 2, "the arrows are still carried for whatever draws arrows"
    assert card["placing"] is False and card["placed"] is False


def test_a_service_without_rr_delta_gets_no_rr_figure():
    """An older service sends only the arrows, and the card must not print those as RR again."""
    s = _session()
    s.on_live_event(_result_event())
    assert _card(s)["card"]["rr_delta"] is None


def test_a_placement_match_says_how_many_are_left():
    s = _session()
    s.on_live_event(_result_event(you={"arrows": 0, "rr_delta": 0, "placing": True,
                                       "placements_left": 3}))
    card = _card(s)["card"]
    assert card["placing"] is True and card["placements_left"] == 3
    for code in i18n.CODES:
        assert "{n}" in P.strings_for(code)["placements_left"], code


def test_the_match_that_places_you_names_the_rank():
    s = _session()
    s.on_live_event(_result_event(you={"arrows": 0, "rr_delta": 0, "placing": False, "placed": True,
                                       "rank": 4, "rank_name": "Veteran", "division": 2}))
    card = _card(s)["card"]
    assert card["placed"] is True and card["placed_rank"] == "Veteran 2"
    for code in i18n.CODES:
        assert "{rank}" in P.strings_for(code)["placed"], code


def test_the_rank_line_is_drawn_from_rr_delta_not_the_arrows():
    """The JS half of the same fix: the figure comes from `rr_delta`, and the arrow count is not
    printed on the card at all."""
    js = (ROOT / "hub" / "webui" / "static" / "screens" / "postmatch.js").read_text(encoding="utf-8")
    assert "card.rr_delta" in js
    assert "card.delta" not in js, "the arrow count must not be printed with an RR unit"


def test_the_preview_finish_raises_the_card_too():
    """The offline preview's End match ends a match as far as the player is concerned, so it gets
    the same card - which is also how this is clicked through without a server."""
    s = C.MockSession(_StubPanel())
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "matches": 0, "wins": 0}
    s.phase = "live"
    s.map = "Rome"
    s.teams = {1: [{"steam_id": "76561198000999000", "name": "Sam"}], 2: []}
    s.finish()
    card = _card(s)["card"]
    assert card is not None and isinstance(card["won"], bool)
    assert card["score"] and sum(card["score"]) > 0


# ---------------------------------------------------------------- it stays up
def test_the_card_outlives_the_result_screen_and_the_next_match():
    """THE CLAUSE THAT MATTERS: "stays there until its closed manually".

    Leaving the result screen wipes the teams, the sides and the veto the card was built from
    (reset_match), and queueing again moves the phase. Neither may take the card down, and the
    card must still be whole afterwards - which is why it is a frozen copy rather than a live
    view of the session."""
    s = _session()
    s.on_live_event(_result_event())
    s.leave_result()                                          # "Back" on the result screen
    card = _card(s)["card"]
    assert _card(s)["open"] is True
    assert card["map"] == "Rome" and card["score"] == [7, 4]
    assert [p["name"] for p in card["teams"]["1"]] == ["Sam", "Vex"]
    assert [b["map"] for b in card["bans"]] == ["Warehouse", "Rooftops"]
    assert s.phase == "idle"                                  # the app moved on; the card did not

    # ...and a hundred snapshot pushes later it is still there. Nothing about rendering it
    # consumes it.
    for _ in range(100):
        assert _card(s)["open"] is True


def test_only_close_postmatch_takes_it_down():
    s = _session()
    s.on_live_event(_result_event())
    assert _card(s)["open"] is True
    s.close_postmatch()
    assert _card(s)["open"] is False and _card(s)["card"] is None
    s.close_postmatch()                                       # idempotent: a double-click is one close
    assert _card(s)["open"] is False


def test_the_verb_closes_it():
    assert "close_postmatch" in SCREEN_VERBS
    s = _session()
    s.on_live_event(_result_event())
    SCREEN_VERBS["close_postmatch"](RecPanel(s))
    assert _card(s)["open"] is False


def test_a_new_match_raises_a_new_card():
    s = _session()
    s.on_live_event(_result_event())
    s.close_postmatch()
    s.teams = {1: [{"steam_id": "76561198000999000", "name": "Sam"}], 2: []}
    s.map = "Warehouse"
    s.on_live_event(_result_event(map="Warehouse", score=[3, 7], winner=2))
    card = _card(s)["card"]
    assert card["map"] == "Warehouse" and card["won"] is False and card["winner"] == 2


# ---------------------------------------------------------------- it looks like the detail panel
def test_the_card_carries_the_match_detail_sections():
    """Same sections as history.py's `_detail`, so the two panels can look alike: the map, the
    scoreline, both teams with the side each played, and the veto in the order it happened."""
    s = _session()
    s.on_live_event(_result_event())
    card = _card(s)["card"]
    assert card["sides"] == {"1": "attack", "2": "defend"}
    assert card["teams"]["2"][0]["name"] == "Nyx"
    assert card["teams"]["1"][0]["is_me"] is True
    assert card["bans"] == [{"team": 1, "map": "Warehouse"}, {"team": 2, "map": "Rooftops"}]
    # This fixture's event carries no `scoreboard`, which is what a match the gamemode never
    # reported on looks like — so the card still points at Match history rather than drawing an
    # empty table. The event CAN carry one now; see the scoreboard tests below.
    assert card["has_scoreboard"] is False, "no board on this event, so no table"


def test_a_roster_the_session_never_saw_comes_off_the_payload():
    """A hub that came back into a match mid-flight has no roster of its own. The result payload
    carries one; without this fallback the card would draw two empty columns."""
    s = _session()
    s.teams = {1: [], 2: []}
    s.on_live_event(_result_event(players=[
        {"steam_id": "76561198000999000", "name": "Sam", "team": 1},
        {"steam_id": "76561198000000003", "persona": "Nyx", "team": 2, "left": True},
    ]))
    card = _card(s)["card"]
    assert [p["name"] for p in card["teams"]["1"]] == ["Sam"]
    assert card["teams"]["2"][0]["name"] == "Nyx" and card["teams"]["2"][0]["left"] is True
    assert card["my_team"] == 1



# ---------------------------------------------------------------- the scoreboard on the card
def _board_event(**over):
    """A result event carrying per-player rows, as live.cjs settleMatch now sends them."""
    rows = over.pop("scoreboard", None)
    if rows is None:
        rows = [
            {"steam_id": "76561198000999000", "persona": "Sam", "team": 1, "reported": True,
             "kills": 21, "deaths": 11, "team_kills": 1},
            {"steam_id": "76561198000000003", "persona": "Nyx", "team": 2, "reported": True,
             "kills": 8, "deaths": 19, "team_kills": 0},
        ]
    return _result_event(scoreboard=rows, **over)


def test_the_card_draws_the_board_when_the_event_carries_one():
    """The whole point of the change: no fetch, no round trip, the numbers are on the event."""
    s = _session()
    s.on_live_event(_board_event())
    card = _card(s)["card"]
    assert card["has_scoreboard"] is True
    mine = card["teams"]["1"][0]
    assert (mine["kills"], mine["deaths"]) == (21, 11)
    assert mine["kd"] == 1.91, "the same ratio the match-history panel shows"
    assert mine["team_kills"] == 1


def test_card_combat_contract_preserves_complete_partial_zero_and_absent():
    combat = {
        "version": 1, "status": "complete",
        "coverage": {"damage": True, "shots": True, "objectives": True},
        "enemyDamage": 777, "friendlyDamage": 8, "damageTaken": 500,
        "assists": 4, "headshots": 3, "shots": 40, "hits": 20,
        "adr": 77.7, "accuracy": 50,
        "weaponStats": [{"weapon": "SMG", "enemyDamage": 600, "friendlyDamage": 8,
                         "kills": 6, "headshots": 2, "shots": 32, "hits": 17}],
    }
    partial = {**combat, "status": "partial", "adr": None, "accuracy": None,
               "coverage": {"damage": True, "shots": False, "objectives": False},
               "weaponStats": []}
    zero = {**combat, "enemyDamage": 0, "friendlyDamage": 0, "damageTaken": 0,
            "assists": 0, "headshots": 0, "shots": 0, "hits": 0, "adr": 0,
            "accuracy": 0, "weaponStats": []}
    # The live result can carry combat on its player rows, while older integrations put it on the
    # joined scoreboard row. The card accepts both during the compatibility window.
    record = {
        "teams": {
            1: [{"steam_id": "76561198000999000", "name": "Sam", "combat": combat},
                {"steam_id": "76561198000000002", "name": "Vex"}],
            2: [{"steam_id": "76561198000000003", "name": "Nyx"},
                {"steam_id": "76561198000000004", "name": "Ivo"}],
        },
        "scoreboard": [
            {"steam_id": "76561198000999000", "reported": True, "kills": 7, "deaths": 2,
             "team_kills": 0},
            {"steam_id": "76561198000000002", "reported": True, "kills": 2, "deaths": 4,
             "team_kills": 0, "combat": partial},
            {"steam_id": "76561198000000003", "reported": True, "kills": 0, "deaths": 0,
             "team_kills": 0, "combat": zero},
            {"steam_id": "76561198000000004", "reported": True, "kills": 1, "deaths": 1,
             "team_kills": 0},
        ],
    }
    rows = {p["name"]: p for team in P._card(record)["teams"].values() for p in team}
    assert rows["Sam"]["combat"]["enemyDamage"] == 777
    assert rows["Vex"]["combat"]["status"] == "partial"
    assert rows["Vex"]["combat"]["adr"] is None
    assert rows["Nyx"]["combat"]["accuracy"] == 0
    assert rows["Ivo"]["combat"] is None


def test_card_combat_rejects_non_finite_metrics_but_preserves_display_text():
    attack = '<svg onload="window.__combatAttack=1">'
    record = {
        "teams": {1: [{"steam_id": "x", "name": attack, "combat": {
            "version": 1, "status": "complete", "coverage": {},
            "enemyDamage": float("nan"), "friendlyDamage": 10 ** 10000, "damageTaken": None,
            "assists": 0, "headshots": 0, "shots": 0, "hits": 0, "adr": float("inf"),
            "accuracy": 0, "weaponStats": [{"weapon": attack, "enemyDamage": 0,
                                               "friendlyDamage": 0, "kills": 0,
                                               "headshots": 0, "shots": 0, "hits": 0}],
        }}], 2: []},
        "scoreboard": [{"steam_id": "x", "reported": True, "kills": 0, "deaths": 0}],
    }
    player = P._card(record)["teams"]["1"][0]
    assert player["name"] == attack and player["combat"]["weaponStats"][0]["weapon"] == attack
    assert player["combat"]["enemyDamage"] is None and player["combat"]["adr"] is None
    assert player["combat"]["friendlyDamage"] is None
    assert "NaN" not in json.dumps(player) and "Infinity" not in json.dumps(player)


def test_combat_only_result_still_opens_the_scoreboard_rows():
    combat = {"version": 1, "status": "partial", "coverage": {"damage": True},
              "enemyDamage": 120, "friendlyDamage": 0, "damageTaken": None,
              "assists": None, "headshots": None, "shots": None, "hits": None,
              "adr": None, "accuracy": None, "weaponStats": []}
    card = P._card({
        "teams": {1: [{"steam_id": "x", "name": "Observed"}], 2: []},
        "scoreboard": [{"steam_id": "x", "reported": False, "kills": None,
                        "deaths": None, "team_kills": None, "combat": combat}],
    })
    assert card["has_scoreboard"] is True, "combat evidence is a scoreboard even without K/D"
    assert card["teams"]["1"][0]["combat"]["enemyDamage"] == 120


def test_the_board_is_joined_onto_the_roster_not_drawn_beside_it():
    """The card already lists both teams; a second list of the same ten people underneath would
    be the same information twice. So the stats ride on the player rows."""
    s = _session()
    s.on_live_event(_board_event())
    card = _card(s)["card"]
    assert "scoreboard" not in card, "no second list — the numbers are on the team rows"
    assert card["teams"]["2"][0]["kills"] == 8


def test_a_player_the_gamemode_never_reported_keeps_nulls():
    """A dash, not a zero: a 0 in a kills column reads as "they went 0-0"."""
    s = _session()
    s.on_live_event(_board_event(scoreboard=[
        {"steam_id": "76561198000999000", "persona": "Sam", "team": 1, "reported": True,
         "kills": 21, "deaths": 11, "team_kills": 0},
        {"steam_id": "76561198000000003", "persona": "Nyx", "team": 2, "reported": False,
         "kills": None, "deaths": None, "team_kills": None},
    ]))
    theirs = _card(s)["card"]["teams"]["2"][0]
    assert theirs["reported"] is False
    assert theirs["kills"] is None and theirs["deaths"] is None and theirs["kd"] is None
    assert theirs["name"] == "Nyx", "and they are still on the board"


def test_tk_only_earns_a_column_when_a_feed_arrived():
    """`team_kills` is null when no kill feed reached the match, and a column of dashes teaches
    nobody anything."""
    s = _session()
    s.on_live_event(_board_event(scoreboard=[
        {"steam_id": "76561198000999000", "persona": "Sam", "team": 1, "reported": True,
         "kills": 21, "deaths": 11, "team_kills": None},
    ]))
    assert _card(s)["card"]["has_team_kills"] is False

    s2 = _session()
    s2.on_live_event(_board_event())
    assert _card(s2)["card"]["has_team_kills"] is True, "a real 0 is a reading, and earns the column"


def test_an_empty_board_keeps_the_honest_note():
    """A match the gamemode said nothing about must not get an empty table."""
    s = _session()
    s.on_live_event(_board_event(scoreboard=[]))
    card = _card(s)["card"]
    assert card["has_scoreboard"] is False
    assert card["teams"]["1"][0]["kills"] is None


def test_a_garbled_board_costs_the_card_nothing():
    """A row shape we did not expect must not take the card down with it — it comes up for ten
    people the instant a match ends."""
    s = _session()
    s.on_live_event(_board_event(scoreboard=["nonsense", None, 7,
                                             {"steam_id": "76561198000999000", "kills": "many",
                                              "deaths": 11, "reported": True}]))
    card = _card(s)["card"]
    mine = card["teams"]["1"][0]
    assert mine["kills"] is None, "'many' is not a number"
    assert mine["deaths"] == 11
    json.dumps(card)


def test_the_board_and_the_history_panel_agree_on_kd():
    """One ratio, one implementation. Three scoreboards (card, match detail, Tk) that each round
    their own way is exactly the kind of disagreement nobody notices until a player does."""
    from hub.competitive import kd_ratio
    from hub.webui.screens import history as H
    s = _session()
    s.on_live_event(_board_event())
    card_kd = _card(s)["card"]["teams"]["1"][0]["kd"]
    assert card_kd == kd_ratio(21, 11)
    # And the history panel builds its own rows with the same function.
    detail = H._scoreboard({"scoreboard": [{"steam_id": "x", "kills": 21, "deaths": 11,
                                            "reported": True, "team": 1}]},
                           [{"steam_id": "x", "name": "Sam", "team": 1}])
    assert detail[0]["kd"] == card_kd

def test_slice_is_json_serialisable():
    s = _session()
    s.on_live_event(_result_event())
    json.dumps(P.snapshot(s, None))                           # must never raise


def test_a_garbage_record_costs_the_card_not_the_push():
    class Junk:
        postmatch = "not a dict"
    assert _card(Junk())["open"] is False


# ---------------------------------------------------------------- strings
def test_strings_complete_in_every_language():
    keys = set(P._EN)
    semantic_labels = {
        "en": ("Damage complete", "Enemy damage", "Observed headshots", "Damage sources", "Source", "Damage taken"),
        "de": ("Schaden vollständig", "Gegnerschaden", "Beobachtete Kopftreffer", "Schadensquellen", "Quelle", "Erlittener Schaden"),
        "es": ("Daño completo", "Daño al enemigo", "Tiros a la cabeza observados", "Fuentes de daño", "Fuente", "Daño recibido"),
        "fr": ("Dégâts complets", "Dégâts ennemis", "Tirs à la tête observés", "Sources de dégâts", "Source", "Dégâts subis"),
        "pt": ("Dano completo", "Dano ao inimigo", "Tiros na cabeça observados", "Fontes de dano", "Fonte", "Dano recebido"),
        "ru": ("Урон учтён полностью", "Урон врагам", "Зафиксированные попадания в голову", "Источники урона", "Источник", "Полученный урон"),
        "zh": ("伤害数据完整", "敌方伤害", "已观测爆头", "伤害来源", "来源", "承受伤害"),
    }
    for code in i18n.CODES:
        merged = P.strings_for(code)
        missing = keys - set(merged)
        assert not missing, "postmatch strings for %r are missing keys: %s" % (code, missing)
        blank = [k for k in keys if not str(merged.get(k, "")).strip()]
        assert not blank, "postmatch strings for %r are blank: %s" % (code, blank)
        assert tuple(merged[key] for key in ("combat_complete", "col_damage", "col_headshots",
                                             "weapons", "col_weapon", "col_damage_taken")) \
               == semantic_labels[code]


def test_the_shared_words_come_from_i18n_not_a_second_copy():
    """Victory, Defeat and friends already exist in seven languages. The card pulls them rather
    than translating them again, so a reword in hub/i18n.py cannot leave two spellings behind."""
    before = i18n.get_language()
    try:
        for code in i18n.CODES:
            i18n.set_language(code)
            got = P.strings_for(code)
            for key, source in P._SHARED.items():
                expected = (i18n.STRINGS.get(code) or {}).get(source) \
                    or (i18n.STRINGS.get(i18n.DEFAULT) or {}).get(source)
                assert got[key] == expected, \
                    "%s/%s is a second copy of %s" % (code, key, source)
    finally:
        i18n.set_language(before)


def test_language_switch_changes_the_card():
    before = i18n.get_language()
    try:
        i18n.set_language("de")
        assert P.strings_for("de")["victory"] == "Sieg"
        assert P.strings_for("de")["title"] == "Matchergebnis"
        i18n.set_language("en")
        assert P.strings_for("en")["victory"] == "Victory"
    finally:
        i18n.set_language(before)


def test_the_note_does_not_claim_the_stats_are_uncollected():
    """The scoreboard landed on main while this was in flight. Per-player stats ARE collected now
    - the archive has them and the match-detail panel draws them - they are simply not on the
    `match_result` event the card is built from. A note saying they are "not collected yet" would
    contradict the panel one click away, in every language."""
    for code in i18n.CODES:
        assert P.strings_for(code)["note"].strip(), code
    en = P.strings_for("en")["note"]
    assert "not collected" not in en.lower(), en
    assert "history" in en.lower(), "the note must say where the scoreboard actually is"


def test_js_references_only_shipped_string_keys():
    """Every literal ps("…") key the card's JS references is shipped in _EN."""
    js = (ROOT / "hub" / "webui" / "static" / "screens" / "postmatch.js").read_text(encoding="utf-8")
    keys = sorted(set(re.findall(r'\bps\(\s*["\']([^"\']+)["\']', js)))
    assert keys, "no ps() keys found — the scan regex is wrong"
    missing = [k for k in keys if k not in P._EN]
    assert not missing, "postmatch.js references string keys it does not ship: %s" % missing


# ---------------------------------------------------------------- the front end wiring
def test_the_card_is_an_overlay_the_core_draws_over_every_screen():
    """It is not a view: the nav has no entry for it, and a match can end while the player is on
    any screen. So the core must draw registered overlays on every render, AFTER the screen (or
    clearOverlays, which runs before both, would eat the one just appended)."""
    core = (ROOT / "hub" / "webui" / "static" / "core.js").read_text(encoding="utf-8")
    lines = core.splitlines()

    def line_of(needle, start=0):
        for i in range(start, len(lines)):
            if needle in lines[i]:
                return i
        return -1

    assert "registerOverlay" in core, "core.js has no overlay registry"
    render_at = line_of("function render() {")
    clear_at = line_of("clearOverlays();", render_at)
    screen_at = line_of("renderScreen();", render_at)
    overlay_at = line_of("renderOverlays();", render_at)
    assert overlay_at > screen_at > clear_at, \
        "overlays must render after the screen, and after the previous render's were cleared"

    js = (ROOT / "hub" / "webui" / "static" / "screens" / "postmatch.js").read_text(encoding="utf-8")
    assert 'registerOverlay("postmatch"' in js, "the card must register as an overlay"
    assert "registerScreen" not in js, "the card is not a view"


def test_nothing_in_the_card_dismisses_itself():
    """The whole feature in one assertion: no timer, anywhere in this file. A setTimeout that
    closed the card - or a close on the next push - is exactly what Sam asked not to have, and it
    would look like a bug in the session rather than a line in the JS."""
    js = (ROOT / "hub" / "webui" / "static" / "screens" / "postmatch.js").read_text(encoding="utf-8")
    for line in js.splitlines():
        code = line.strip()
        if code.startswith("*") or code.startswith("//") or code.startswith("/*"):
            continue                                          # the rule written down is not a timer
        if "setTimeout" in code or "setInterval" in code:
            assert "close" not in code, "the card must never dismiss itself: " + code
    assert "close_postmatch" in js, "...and the manual dismissal must be wired"
    # ui.modal gives the X, the backdrop and Escape one shared onClose; the card must use it
    # rather than hand-rolling a close control (tests/test_hub.py enforces that for every modal).
    assert "ui.modal(" in js and "onClose" in js
    assert "ui-modal-x" not in js


def test_the_card_is_styled_and_loaded():
    static = ROOT / "hub" / "webui" / "static"
    index = (static / "index.html").read_text(encoding="utf-8")
    assert "screens/postmatch.css" in index and "screens/postmatch.js" in index
    css = (static / "screens" / "postmatch.css").read_text(encoding="utf-8")
    for cls in (".pm-headline", ".pm-score", ".pm-teams", ".pm-veto", ".pm-overlay .ui-modal"):
        assert cls in css, "postmatch.css has no rule for %s" % cls
    # The card must not hard-code colour: the shared tokens are what keep the app consistent.
    assert "var(--green)" in css and "var(--accent)" in css
    assert not re.search(r":\s*#[0-9a-fA-F]{3,6}\b", css), "postmatch.css hard-codes a colour"


def test_slice_merges_into_the_whole_snapshot():
    """Through a real headless WebPanel, so the card is proven to reach the page - not just the
    slice function."""
    from hub.webui.panel import WebPanel
    from hub.webui.scheduler import InlineScheduler
    from hub.webui.snapshot import state_snapshot

    class _App:
        state = {"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0"}}}
        catalogue = {"gamemodes": [{"id": C.COMPETITIVE_MODE_ID}]}

    panel = WebPanel(_App(), scheduler=InlineScheduler(), window=None)
    s = panel.session
    s._action = lambda call, on_result=None: (lambda r: on_result(*r) if on_result else None)(call())
    s.me = {"name": "Sam", "steam_id": "76561198000999000"}
    s.token = "tok"
    s.phase = "live"
    s.map = "Rome"
    s.teams = {1: [{"steam_id": "76561198000999000", "name": "Sam"}], 2: []}
    s.on_live_event(_result_event())

    snap = state_snapshot(s, panel)
    json.dumps(snap)
    assert snap["postmatch"]["open"] is True
    assert snap["postmatch"]["card"]["won"] is True
    assert snap["postmatch"]["strings"]["victory"] == "Victory"

    # ...and the player on another screen still gets it: the slice does not depend on the view.
    panel.view = "profile"
    assert state_snapshot(s, panel)["postmatch"]["open"] is True


def main():
    for fn in [
        test_registered_under_postmatch,
        test_nothing_is_open_before_a_match_ends,
        test_a_win_raises_the_card_on_the_result_event,
        test_a_loss_reads_from_the_losing_side,
        test_an_explicit_won_flag_beats_a_missing_winner,
        test_a_voided_match_claims_neither_victory_nor_defeat,
        test_the_card_carries_the_rr_the_match_moved,
        test_a_service_without_rr_delta_gets_no_rr_figure,
        test_a_placement_match_says_how_many_are_left,
        test_the_match_that_places_you_names_the_rank,
        test_the_rank_line_is_drawn_from_rr_delta_not_the_arrows,
        test_the_preview_finish_raises_the_card_too,
        test_the_card_outlives_the_result_screen_and_the_next_match,
        test_only_close_postmatch_takes_it_down,
        test_the_verb_closes_it,
        test_a_new_match_raises_a_new_card,
        test_the_card_carries_the_match_detail_sections,
        test_the_card_draws_the_board_when_the_event_carries_one,
        test_card_combat_contract_preserves_complete_partial_zero_and_absent,
        test_card_combat_rejects_non_finite_metrics_but_preserves_display_text,
        test_combat_only_result_still_opens_the_scoreboard_rows,
        test_the_board_is_joined_onto_the_roster_not_drawn_beside_it,
        test_a_player_the_gamemode_never_reported_keeps_nulls,
        test_tk_only_earns_a_column_when_a_feed_arrived,
        test_an_empty_board_keeps_the_honest_note,
        test_a_garbled_board_costs_the_card_nothing,
        test_the_board_and_the_history_panel_agree_on_kd,
        test_a_roster_the_session_never_saw_comes_off_the_payload,
        test_slice_is_json_serialisable,
        test_a_garbage_record_costs_the_card_not_the_push,
        test_strings_complete_in_every_language,
        test_the_shared_words_come_from_i18n_not_a_second_copy,
        test_language_switch_changes_the_card,
        test_the_note_does_not_claim_the_stats_are_uncollected,
        test_js_references_only_shipped_string_keys,
        test_the_card_is_an_overlay_the_core_draws_over_every_screen,
        test_nothing_in_the_card_dismisses_itself,
        test_the_card_is_styled_and_loaded,
        test_slice_merges_into_the_whole_snapshot,
    ]:
        test(fn)

    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
