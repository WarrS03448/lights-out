#!/usr/bin/env python3.12
"""Headless tests for the Match history screen.  Run:  python3.12 tests/test_screen_history.py

The valuable half of the redesign is testable with no window (docs/ui-redesign-plan.md, "Testing"):
this covers the ``history`` snapshot slice the JS renders from, its bridge verbs, and its per-screen
string table. Nothing here imports pywebview or opens a Tk window; it drives the pure slice directly
and, once, through a real headless WebPanel to prove the slice merges into the whole snapshot.

The through-line is HONESTY: the backend leaves won/score/delta null until a gamemode reports a
scoreboard, so these tests assert the slice preserves those nulls rather than inventing a result.
"""
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from hub import i18n                                        # noqa: E402
from hub.webui.screens import history as H                  # noqa: E402
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
class FakeSession:
    """The handful of attributes history.snapshot reads off a session."""

    def __init__(self, history=None, loading=False, error="", seq=0):
        self.history = history
        self.history_loading = loading
        self.history_error = error
        self.history_seq = seq


class RecSession:
    """Records how the bridge verbs call load_history."""

    def __init__(self):
        self.calls = []

    def load_history(self, force=False):
        self.calls.append(force)


class RecPanel:
    """A panel whose post() runs immediately on this thread (like InlineScheduler)."""

    def __init__(self):
        self.session = RecSession()

    def post(self, fn):
        fn()


# Rows in the exact shape the server sends (server/live.cjs historyRow), newest first.
def sample_rows():
    return [
        {"id": "m1", "ended": 3000, "map": "Warehouse", "outcome": "played", "reason": "",
         "blamed": False, "team": 1, "side": "attack", "host": True, "players": 10,
         "won": None, "score": None, "delta": None, "elo": None, "connected": True},
        {"id": "m2", "ended": 2000, "map": "", "outcome": "cancelled", "reason": "no_show",
         "blamed": True, "team": 2, "side": "", "host": False, "players": 10,
         "won": None, "score": None, "delta": None, "elo": -25, "connected": False},
        {"id": "m3", "ended": 1000, "map": "Rooftops", "outcome": "cancelled", "reason": "",
         "blamed": False, "team": 1, "side": "defend", "host": False, "players": 10,
         "won": None, "score": None, "delta": None, "elo": None, "connected": True},
    ]


# ---------------------------------------------------------------- snapshot slice
def test_registered_under_history():
    assert "history" in SCREEN_SNAPSHOTS, "history did not register a snapshot contributor"
    assert SCREEN_SNAPSHOTS["history"] is H.snapshot


def test_never_asked_is_distinct_from_empty():
    # history None => we have not fetched / cannot reach the service: asked is False, no rows.
    slice_ = H.snapshot(FakeSession(history=None), None)["history"]
    assert slice_["asked"] is False and slice_["rows"] == []
    assert slice_["summary"]["recorded"] == 0
    # an EMPTY list is a real answer (a player with no matches): asked is True.
    slice2 = H.snapshot(FakeSession(history=[]), None)["history"]
    assert slice2["asked"] is True and slice2["rows"] == []


def test_rows_serialize_with_honest_nulls():
    slice_ = H.snapshot(FakeSession(history=sample_rows(), seq=7), None)["history"]
    assert slice_["seq"] == 7 and slice_["asked"] is True
    rows = slice_["rows"]
    assert len(rows) == 3

    played, no_show, cancelled = rows
    # a played match with no scoreboard yet: "played", not a fabricated win/loss, nulls preserved.
    assert played["result"] == "played"
    assert played["won"] is None and played["score"] is None and played["delta"] is None
    assert played["side"] == "attack" and played["host"] is True and played["map"] == "Warehouse"
    # the player's own no-show: at fault, and the RR debt is carried on the row.
    assert no_show["result"] == "fault" and no_show["reason"] == "no_show"
    assert no_show["blamed"] is True and no_show["elo"] == -25
    # a cancellation that was not this player's fault: just "cancelled", no debt.
    assert cancelled["result"] == "cancelled" and cancelled["elo"] is None


def test_won_and_score_round_trip_when_present():
    # A locally-recorded preview win carries a real score and won=True.
    row = {"id": "p1", "ended": 5000, "map": "Depot", "outcome": "played", "reason": "",
           "blamed": False, "team": 1, "side": "attack", "host": False, "players": 10,
           "won": True, "score": [13, 9], "delta": 21, "elo": None, "preview": True}
    out = H.snapshot(FakeSession(history=[row]), None)["history"]["rows"][0]
    assert out["result"] == "win" and out["won"] is True
    assert out["score"] == [13, 9] and out["delta"] == 21 and out["preview"] is True


def test_the_services_score_string_reaches_the_row():
    """2.8.5: no real history row ever showed a score. server/live.cjs writes a row's score as a
    STRING, this player's side first (`${a}-${b}` in applyReceiptRow and the settlement's rows;
    its own test-result.mjs pins '7-4'), and _row kept lists only - so every settled match drew
    "no score" while the preview rows, which hold a list, looked fine."""
    assert H._row({"score": "7-4"})["score"] == [7, 4]
    assert H._row({"score": "4-7"})["score"] == [4, 7], "the player's own side stays first"
    assert H._row({"score": "13-0"})["score"] == [13, 0], "a real zero is a score, not a gap"
    assert H._row({"score": [13, 9]})["score"] == [13, 9], "a preview row's list still works"
    for junk in (None, "", "7", "7-", "-4", "a-b", "7-4-1", "undefined-undefined", 74, True):
        assert H._row({"score": junk})["score"] is None, junk
    # and through the whole slice, from a row in the shape the service sends
    row = {"id": "m7", "ended": 5000, "map": "Rome", "outcome": "played", "reason": "",
           "blamed": False, "team": 2, "side": "defend", "host": False, "players": 2,
           "won": False, "score": "4-7", "delta": -2, "rr_delta": -21, "placement": False,
           "elo": None, "connected": True}
    out = H.snapshot(FakeSession(history=[row]), None)["history"]["rows"][0]
    assert out["score"] == [4, 7] and out["result"] == "loss"
    json.dumps(out)


def test_the_rr_column_is_the_rr_the_match_moved_not_the_arrows():
    """SAM, 2026-09-16: "we are only gaining and losing 1-3 RR". The RR column printed `delta` - an
    arrow count, drawn from the matchmaking rating - with "RR" after it."""
    row = {"id": "m9", "ended": 5000, "map": "Rome", "outcome": "played", "reason": "",
           "blamed": False, "team": 1, "side": "attack", "host": False, "players": 2,
           "won": True, "score": "7-4", "delta": 2, "rr_delta": 23, "placement": False,
           "elo": None, "connected": True}
    out = H.snapshot(FakeSession(history=[row]), None)["history"]["rows"][0]
    assert out["rr_delta"] == 23 and out["delta"] == 2 and out["placement"] is False
    js = (ROOT / "hub" / "webui" / "static" / "screens" / "history.js").read_text(encoding="utf-8")
    assert "r.rr_delta" in js
    assert not re.search(r"value\s*=\s*r\.delta\b", js), "the RR cell must not print the arrow count"


def test_a_placement_row_is_labelled_and_old_rows_carry_no_rr():
    placement = {"id": "m8", "ended": 4000, "map": "Rome", "outcome": "played", "reason": "",
                 "blamed": False, "team": 1, "side": "attack", "host": False, "players": 2,
                 "won": False, "score": "4-7", "delta": 0, "rr_delta": 0, "placement": True,
                 "elo": None, "connected": True}
    rows = H.snapshot(FakeSession(history=[placement] + sample_rows()), None)["history"]["rows"]
    assert rows[0]["placement"] is True and rows[0]["rr_delta"] == 0
    # Rows the service wrote before `rr_delta` existed have no RR to show - None, not their arrows.
    assert all(r["rr_delta"] is None and r["placement"] is False for r in rows[1:])
    for code in i18n.CODES:
        assert H.strings_for(code)["rr_placement"].strip(), code


def test_summary_counts_are_honest():
    slice_ = H.snapshot(FakeSession(history=sample_rows()), None)["history"]
    sm = slice_["summary"]
    assert sm["recorded"] == 3 and sm["played"] == 1 and sm["cancelled"] == 2
    # nothing has been decided (won is null on every row), so wins/losses stay 0 and win_rate None.
    assert sm["wins"] == 0 and sm["losses"] == 0 and sm["win_rate"] is None


def test_loading_and_error_pass_through():
    s = H.snapshot(FakeSession(history=None, loading=True), None)["history"]
    assert s["loading"] is True
    s2 = H.snapshot(FakeSession(history=None, error="Could not reach the match service."),
                    None)["history"]
    assert s2["error"] == "Could not reach the match service."


def test_slice_is_json_serialisable():
    slice_ = H.snapshot(FakeSession(history=sample_rows(), seq=3), None)
    json.dumps(slice_)                                       # must never raise


def test_garbage_rows_do_not_crash_the_slice():
    # A row that is not a dict, or has junk in numeric fields, costs at most that row, never the push.
    rows = [None, "nope", {"id": 1, "ended": "x", "team": "y", "elo": "z", "outcome": "played"}]
    slice_ = H.snapshot(FakeSession(history=rows), None)["history"]
    assert len(slice_["rows"]) == 1                          # the two non-dicts are dropped
    r = slice_["rows"][0]
    assert r["ended"] == 0 and r["team"] == 0 and r["elo"] is None and r["id"] == "1"


# ---------------------------------------------------------------- bridge verbs
def test_verbs_map_to_load_history():
    assert "load_history" in SCREEN_VERBS and "refresh_history" in SCREEN_VERBS
    panel = RecPanel()
    SCREEN_VERBS["load_history"](panel)
    assert panel.session.calls[-1] is False                 # lazy load: does not force
    SCREEN_VERBS["refresh_history"](panel)
    assert panel.session.calls[-1] is True                  # Refresh re-asks the server


# ---------------------------------------------------------------- strings
def test_strings_complete_in_every_language():
    keys = set(H._EN)
    for code in i18n.CODES:
        merged = H.strings_for(code)
        missing = keys - set(merged)
        assert not missing, "history strings for %r are missing keys: %s" % (code, missing)
        # every value is a non-empty string (a blank label renders as a hole in the UI)
        blank = [k for k in keys if not str(merged.get(k, "")).strip()]
        assert not blank, "history strings for %r are blank: %s" % (code, blank)


def test_language_switch_changes_the_title():
    before = i18n.get_language()
    try:
        i18n.set_language("de")
        de = H.snapshot(FakeSession(history=[]), None)["history"]["strings"]
        assert de["title"] == "Matchverlauf"
        i18n.set_language("en")
        en = H.snapshot(FakeSession(history=[]), None)["history"]["strings"]
        assert en["title"] == "Match history"
    finally:
        i18n.set_language(before)


def test_js_references_only_shipped_string_keys():
    """Every literal hs("…") key the screen's JS references is shipped in _EN (the analogue of the
    core's i18n key-coverage test, for this screen's own string table). Guards a typo'd key
    rendering as the raw key on screen."""
    js = (ROOT / "hub" / "webui" / "static" / "screens" / "history.js").read_text(encoding="utf-8")
    keys = sorted(set(re.findall(r'\bhs\(\s*["\']([^"\']+)["\']', js)))
    assert keys, "no hs() keys found — the scan regex is wrong or the screen shipped a stub"
    missing = [k for k in keys if k not in H._EN]
    assert not missing, "history.js references string keys not shipped in _EN: %s" % missing


def test_js_does_not_route_new_strings_through_i18n_t():
    """The screen's NEW strings must NOT go through t()/ctx.t(): the core's completeness test scans
    all JS for t("…") keys and asserts they exist in hub/i18n.py, which this screen must not edit.
    So history.js must reference no t("…") key at all (it uses hs() instead)."""
    js = (ROOT / "hub" / "webui" / "static" / "screens" / "history.js").read_text(encoding="utf-8")
    # strip block and line comments so prose in the header does not trip the scan
    code = re.sub(r'/\*.*?\*/', '', js, flags=re.DOTALL)
    code = "\n".join(re.sub(r'//.*$', '', line) for line in code.splitlines())
    t_keys = re.findall(r'\bt\(\s*["\']([^"\']+)["\']', code)
    assert not t_keys, "history.js routes strings through t(); it must use hs(): %s" % t_keys


# ---------------------------------------------------------------- integration (real WebPanel)
def test_slice_merges_into_the_whole_snapshot():
    """Through a real headless WebPanel (no Tk, no pywebview, no window): state_snapshot merges the
    history slice under "history" while the shared view stays competitive, and the whole thing is
    JSON-serialisable — the same shape the bridge pushes to JS."""
    from hub import competitive as C
    from hub.webui.panel import WebPanel
    from hub.webui.scheduler import InlineScheduler
    from hub.webui.snapshot import state_snapshot

    class _FakeApp:
        def __init__(self):
            self.state = {"installed": {C.COMPETITIVE_MODE_ID: {"version": "1.0"}}, "auth": None}
            self.catalogue = None

    i18n.set_language("en")
    panel = WebPanel(_FakeApp(), scheduler=InlineScheduler(), window=None)
    s = panel.session
    s.me = {"name": "Sam", "steam_id": "76561198000999000", "level": 6, "elo": 1180,
            "matches": 34, "wins": 19}
    s.phase = "idle"
    s.history = sample_rows()
    s.history_seq = 2

    snap = state_snapshot(s, panel)
    json.dumps(snap)
    assert snap["view"] == "competitive"                    # nav still lives in the core
    assert "history" in snap and snap["history"]["asked"] is True
    assert len(snap["history"]["rows"]) == 3
    assert snap["history"]["strings"]["title"] == "Match history"
    assert snap["history"]["rows"][1]["result"] == "fault"


# ---------------------------------------------------------------- the scoreboard
def _record(**over):
    """An archived match record shaped as server/live.cjs fullRecord writes it."""
    rec = {
        "id": "m1", "map": "Rome", "outcome": "played", "created": 1, "ended": 2,
        "score": {"1": 7, "2": 3}, "won_team": 1,
        "teams": {"1": ["1" * 17], "2": ["2" * 17]},
        "sides": {"1": "attack", "2": "defend"},
        "players": [
            {"steam_id": "1" * 17, "persona": "me", "team": 1, "connected": True},
            {"steam_id": "2" * 17, "persona": "them", "team": 2, "connected": True},
        ],
        "rounds_played": 10,
        "scoreboard": [
            {"steam_id": "1" * 17, "team": 1, "reported": True, "kills": 14, "deaths": 6,
             "team_kills": 1, "rounds_won": 7, "clutches": 2, "ping": 34, "spawns": 10,
             "late_join": False, "spectator": False},
            {"steam_id": "2" * 17, "team": 2, "reported": True, "kills": 5, "deaths": 13,
             "team_kills": 0, "rounds_won": 3, "clutches": 0, "ping": 51, "spawns": 10,
             "late_join": False, "spectator": False},
        ],
    }
    rec.update(over)
    return rec


def _open(rec, me="1" * 17):
    """The `open` half of the slice, for a session sitting on this record."""
    session = FakeSession(history=[])
    session.match_detail_id = rec.get("id", "")
    session.match_detail = rec
    session.me = {"steam_id": me}
    return H.snapshot(session, None)["history"]["open"]


def test_the_board_is_serialised_with_names_and_teams():
    d = _open(_record())
    assert d["has_scoreboard"] is True
    assert d["rounds_played"] == 10
    board = d["scoreboard"]
    assert len(board) == 2
    mine = [r for r in board if r["steam_id"] == "1" * 17][0]
    assert mine["name"] == "me", "joined onto the roster the record already carries"
    assert mine["team"] == 1
    assert mine["is_me"] is True
    assert (mine["kills"], mine["deaths"]) == (14, 6)
    assert mine["team_kills"] == 1
    assert mine["ping"] == 34


def test_combat_contract_preserves_complete_partial_zero_and_absent():
    """Breaking _scoreboard's combat join must lose a real metric or turn unknown into zero."""
    players = [
        {"steam_id": str(n) * 17, "name": "p%s" % n, "team": 1 if n < 3 else 2,
         "is_me": n == 1, "left": False}
        for n in range(1, 5)
    ]
    rec = {"scoreboard": [
        {"steam_id": "1" * 17, "team": 1, "reported": True, "kills": 9, "deaths": 3,
         "combat": {
             "version": 1, "status": "complete",
             "coverage": {"damage": True, "shots": True, "objectives": True},
             "enemyDamage": 1234, "friendlyDamage": 12, "damageTaken": 880,
             "assists": 6, "headshots": 4, "shots": 80, "hits": 41,
             "adr": 123.4, "accuracy": 51.25,
             "weaponStats": [{"weapon": "Rifle", "enemyDamage": 900,
                              "friendlyDamage": 12, "kills": 7, "headshots": 3,
                              "shots": 60, "hits": 32}],
         }},
        {"steam_id": "2" * 17, "team": 1, "reported": True, "kills": 2, "deaths": 6,
         "combat": {
             "version": 1, "status": "partial",
             "coverage": {"damage": True, "shots": False, "objectives": False},
             "enemyDamage": 245, "friendlyDamage": None, "damageTaken": None,
             "assists": 0, "headshots": None, "shots": None, "hits": None,
             "adr": None, "accuracy": None, "weaponStats": [],
         }},
        {"steam_id": "3" * 17, "team": 2, "reported": True, "kills": 0, "deaths": 0,
         "combat": {
             "version": 1, "status": "complete",
             "coverage": {"damage": True, "shots": True, "objectives": True},
             "enemyDamage": 0, "friendlyDamage": 0, "damageTaken": 0,
             "assists": 0, "headshots": 0, "shots": 0, "hits": 0,
             "adr": 0, "accuracy": 0, "weaponStats": [],
         }},
        {"steam_id": "4" * 17, "team": 2, "reported": True, "kills": 1, "deaths": 1},
    ]}

    by_id = {row["steam_id"]: row for row in H._scoreboard(rec, players)}
    complete = by_id["1" * 17]["combat"]
    assert complete["enemyDamage"] == 1234 and complete["adr"] == 123.4
    assert complete["accuracy"] == 51.25 and complete["weaponStats"][0]["weapon"] == "Rifle"
    partial = by_id["2" * 17]["combat"]
    assert partial["status"] == "partial" and partial["enemyDamage"] == 245
    assert partial["adr"] is None and partial["assists"] == 0
    zero = by_id["3" * 17]["combat"]
    assert zero["status"] == "complete"
    assert all(zero[key] == 0 for key in ("enemyDamage", "friendlyDamage", "damageTaken",
                                          "assists", "headshots", "shots", "hits", "adr",
                                          "accuracy"))
    assert by_id["4" * 17]["combat"] is None, "an old match stays unavailable, not zero-filled"


def test_combat_strings_and_malicious_display_values_are_data():
    """Names stay text for the DOM; numeric junk/NaN cannot become a displayed statistic."""
    attack = '<img src=x onerror="window.__combatAttack=1">'
    rec = {"scoreboard": [{
        "steam_id": "x", "team": 1, "reported": True, "kills": 1, "deaths": 1,
        "combat": {
            "version": 1, "status": "complete", "coverage": {},
            "enemyDamage": attack, "friendlyDamage": 10 ** 10000, "damageTaken": None,
            "assists": 0, "headshots": 0, "shots": 1, "hits": 0,
            "adr": float("inf"), "accuracy": 0,
            "weaponStats": [{"weapon": attack, "enemyDamage": 0, "friendlyDamage": 0,
                             "kills": 0, "headshots": 0, "shots": 0, "hits": 0}],
        },
    }]}
    row = H._scoreboard(rec, [{"steam_id": "x", "name": attack, "team": 1,
                               "is_me": False, "left": False}])[0]
    assert row["name"] == attack and row["combat"]["weaponStats"][0]["weapon"] == attack
    assert row["combat"]["enemyDamage"] is None
    assert row["combat"]["friendlyDamage"] is None and row["combat"]["adr"] is None
    assert "NaN" not in json.dumps(row) and "Infinity" not in json.dumps(row)


def test_every_language_translates_combat_details():
    keys = {"combat_details", "combat_complete", "combat_partial", "combat_unavailable",
            "combat_partial_note", "combat_unavailable_note", "col_damage", "col_adr",
            "col_assists", "col_headshots", "col_accuracy", "weapons", "col_weapon",
            "col_friendly_damage", "col_damage_taken", "col_shots", "col_hits"}
    semantic_labels = {
        "en": ("Damage complete", "Enemy damage", "Observed headshots", "Damage sources", "Source", "Damage taken"),
        "de": ("Schaden vollständig", "Gegnerschaden", "Beobachtete Kopftreffer", "Schadensquellen", "Quelle", "Erlittener Schaden"),
        "es": ("Daño completo", "Daño al enemigo", "Tiros a la cabeza observados", "Fuentes de daño", "Fuente", "Daño recibido"),
        "fr": ("Dégâts complets", "Dégâts ennemis", "Tirs à la tête observés", "Sources de dégâts", "Source", "Dégâts subis"),
        "pt": ("Dano completo", "Dano ao inimigo", "Tiros na cabeça observados", "Fontes de dano", "Fonte", "Dano recebido"),
        "ru": ("Урон учтён полностью", "Урон врагам", "Зафиксированные попадания в голову", "Источники урона", "Источник", "Полученный урон"),
        "zh": ("伤害数据完整", "敌方伤害", "已观测爆头", "伤害来源", "来源", "承受伤害"),
    }
    assert keys <= set(H._EN)
    for code in i18n.CODES:
        if code != "en":
            assert keys <= set(H._TRANSLATIONS.get(code, {})), code
        strings = H.strings_for(code)
        assert tuple(strings[key] for key in ("combat_complete", "col_damage", "col_headshots",
                                               "weapons", "col_weapon", "col_damage_taken")) \
               == semantic_labels[code]


def test_kd_is_a_ratio_and_zero_deaths_is_not_a_crash():
    # Lives in competitive.py now, shared with the post-match card and the Tk fallback so three
    # scoreboards cannot disagree about one ratio.
    from hub.competitive import kd_ratio
    assert kd_ratio(14, 6) == 2.33
    assert kd_ratio(5, 0) == 5.0, "no deaths is a perfect record, not a division by zero"
    assert kd_ratio(0, 4) == 0.0
    assert kd_ratio(-1, 2) == -0.5, "kills are a NET count and can be negative"
    assert kd_ratio(None, 4) is None
    assert kd_ratio(4, None) is None


def test_a_player_the_sweep_missed_keeps_nulls_not_zeroes():
    rec = _record()
    rec["scoreboard"][1] = {"steam_id": "2" * 17, "team": 2, "reported": False,
                            "kills": None, "deaths": None, "team_kills": None}
    theirs = [r for r in _open(rec)["scoreboard"] if r["steam_id"] == "2" * 17][0]
    assert theirs["reported"] is False
    assert theirs["kills"] is None, "0 would read as 'they went 0-0'"
    assert theirs["deaths"] is None
    assert theirs["kd"] is None
    assert theirs["name"] == "them", "and they are still on the board"


def test_the_board_sorts_best_first_and_sinks_the_unreported():
    rec = _record()
    rec["scoreboard"] = [
        {"steam_id": "3" * 17, "team": 1, "reported": False, "kills": None, "deaths": None},
        {"steam_id": "2" * 17, "team": 2, "reported": True, "kills": 5, "deaths": 13},
        {"steam_id": "1" * 17, "team": 1, "reported": True, "kills": 14, "deaths": 6},
    ]
    order = [r["steam_id"] for r in _open(rec)["scoreboard"]]
    assert order == ["1" * 17, "2" * 17, "3" * 17]


def test_no_board_says_so_rather_than_showing_zeroes():
    d = _open(_record(scoreboard=[], rounds_played=None))
    assert d["has_scoreboard"] is False
    assert d["scoreboard"] == []
    assert d["rounds_played"] is None
    # And a record from before the board existed at all must not crash the slice.
    rec = _record()
    del rec["scoreboard"]
    del rec["rounds_played"]
    assert _open(rec)["has_scoreboard"] is False


def test_a_garbled_board_does_not_crash_the_slice():
    d = _open(_record(scoreboard=["nonsense", None, 7,
                                  {"steam_id": "1" * 17, "kills": "many", "deaths": 3}]))
    assert len(d["scoreboard"]) == 1, "the unreadable entries are dropped, not the whole board"
    row = d["scoreboard"][0]
    assert row["kills"] is None, "'many' is not a number"
    assert row["deaths"] == 3
    json.dumps(d)


def test_the_row_carries_the_players_own_line():
    rows = [{"id": "m1", "ended": 1, "map": "Rome", "outcome": "played", "team": 1,
             "won": True, "score": [7, 3], "kills": 14, "deaths": 6, "team_kills": 1}]
    out = H.snapshot(FakeSession(history=rows), None)["history"]["rows"][0]
    assert out["kills"] == 14, "so the list can draw a K/D without a detail fetch"
    assert out["deaths"] == 6
    assert out["team_kills"] == 1
    # And a row from a match with no board keeps the honest nulls.
    bare = H.snapshot(FakeSession(history=[{"id": "m2", "outcome": "played"}]),
                      None)["history"]["rows"][0]
    assert bare["kills"] is None and bare["deaths"] is None


def test_the_kd_label_names_the_column_it_actually_holds():
    # No assist is collected anywhere (docs/match-data.md tier 1a), so "K/D/A" was a promise the
    # data cannot keep. Every language has to agree, or one of them claims an assist column.
    for lang in ["en"] + sorted(H._TRANSLATIONS):
        label = H.strings_for(lang)["kda_label"]
        assert "/" in label, lang
        assert label.count("/") == 1, "%s still labels a third column: %r" % (lang, label)


def main():
    for fn in [
        test_registered_under_history,
        test_never_asked_is_distinct_from_empty,
        test_rows_serialize_with_honest_nulls,
        test_won_and_score_round_trip_when_present,
        test_the_services_score_string_reaches_the_row,
        test_the_rr_column_is_the_rr_the_match_moved_not_the_arrows,
        test_a_placement_row_is_labelled_and_old_rows_carry_no_rr,
        test_summary_counts_are_honest,
        test_loading_and_error_pass_through,
        test_slice_is_json_serialisable,
        test_garbage_rows_do_not_crash_the_slice,
        test_verbs_map_to_load_history,
        test_strings_complete_in_every_language,
        test_language_switch_changes_the_title,
        test_js_references_only_shipped_string_keys,
        test_js_does_not_route_new_strings_through_i18n_t,
        test_slice_merges_into_the_whole_snapshot,
        test_the_board_is_serialised_with_names_and_teams,
        test_combat_contract_preserves_complete_partial_zero_and_absent,
        test_combat_strings_and_malicious_display_values_are_data,
        test_every_language_translates_combat_details,
        test_kd_is_a_ratio_and_zero_deaths_is_not_a_crash,
        test_a_player_the_sweep_missed_keeps_nulls_not_zeroes,
        test_the_board_sorts_best_first_and_sinks_the_unreported,
        test_no_board_says_so_rather_than_showing_zeroes,
        test_a_garbled_board_does_not_crash_the_slice,
        test_the_row_carries_the_players_own_line,
        test_the_kd_label_names_the_column_it_actually_holds,
    ]:
        test(fn)

    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
