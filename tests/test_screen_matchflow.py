#!/usr/bin/env python3.12
"""Headless tests for the competitive MATCH-FLOW phases of the web UI.  Run:

    python3.12 tests/test_screen_matchflow.py

Covers the slices and verbs added by hub/webui/screens/competitive.py for the live match path
beyond "found": lobby (coin flip -> side/first-ban choice -> map veto), the connect window, the
live/vote panel and the result. Same shape as the webui tests in tests/test_hub.py — plain
asserts on the JSON snapshot dict and on the js_api verb -> session-verb mapping, no Tk, no
pywebview, no network (the LiveSession runs with a fake live client and the inline scheduler).
"""
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Point the hub's state at a scratch dir BEFORE importing it (as tests/test_hub.py does).
_STATE = tempfile.mkdtemp(prefix="hub-mf-state-")
os.environ.setdefault("HUB_STATE_DIR", _STATE)
sys.path.insert(0, str(REPO))

from hub import competitive as C          # noqa: E402
from hub import i18n                       # noqa: E402
from hub.webui.bridge import Api           # noqa: E402
from hub import sounds as sounds_mod        # noqa: E402
from hub.webui import panel as panel_mod    # noqa: E402
from hub.webui.panel import WebPanel       # noqa: E402
from hub.webui.scheduler import InlineScheduler  # noqa: E402
from hub.webui.snapshot import state_snapshot    # noqa: E402

RESULTS = []


# ---------------------------------------------------------------- fakes / harness
class _FakeApp:
    """The minimal `app` a WebPanel reads: persisted state (dict) and the catalogue."""

    def __init__(self, installed=None, catalogue=None):
        self.state = {"installed": installed or {}, "auth": None}
        self.catalogue = catalogue


class _FakeLiveClient:
    """A no-op live client so LiveSession never touches the network in these tests. It records
    the lobby actions the session POSTs, so a test can assert that the coin/choice/side/ban were
    sent to the SERVER rather than decided locally."""

    def __init__(self):
        self.calls = []

    def start(self): pass
    def stop(self): pass
    def leave_match(self): return (200, {})
    def report_connected(self): return (200, {})
    def flip_coin(self, side): self.calls.append(("flip_coin", side)); return (200, {})
    def choose_advantage(self, kind): self.calls.append(("choose_advantage", kind)); return (200, {})
    def pick_side(self, side): self.calls.append(("pick_side", side)); return (200, {})
    def ban_map(self, name): self.calls.append(("ban_map", name)); return (200, {})
    # chat_result is what the service is pretending to answer, so a test can be the rate limiter
    chat_result = (200, {})
    def send_chat(self, text, channel="team"):
        self.calls.append(("send_chat", text, channel))
        return self.chat_result
    def start_connect(self, *a, **k): self.calls.append(("start_connect", a)); return (200, {})
    def ack_combat_warning(self, match_id, warning_id):
        self.calls.append(("ack_combat_warning", match_id, warning_id))
        return (200, {"ok": True})


class _CallRecorder:
    """Stands in for a session: records which verb the bridge called, with what args."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def rec(*args, **kw):
            self.calls.append((name, args, kw))
        return rec


ME = {"name": "Sam", "steam_id": "76561198000999000", "level": 6, "elo": 1180,
      "bdr": None, "matches": 34, "wins": 19, "ping": 24}
P2 = {"name": "Ravi", "steam_id": "2", "level": 5, "ping": 40}
P3 = {"name": "Wario", "steam_id": "3", "level": 7, "ping": 33}
P4 = {"name": "Toad", "steam_id": "4", "level": 4, "ping": 50}


def _panel():
    """A headless WebPanel driving a LiveSession with the fake client and the inline scheduler."""
    panel = WebPanel(_FakeApp(installed={C.COMPETITIVE_MODE_ID: {"version": "1.0"}}),
                     scheduler=InlineScheduler(), window=None)
    s = panel.session
    s._action = lambda call, on_result=None: (lambda r: on_result(*r) if on_result else None)(call())
    s.me = dict(ME)
    s.token = "tok"
    s.client = _FakeLiveClient()
    s.phase = "idle"
    return panel, s


def _seat_a_lobby(s, stage):
    """Put the session into a lobby at the given stage with a known two-per-team roster.

    Me is on team 1 and its captain, so every captain-only control is reachable — the same
    invariant _start_lobby keeps in the live/mock flow."""
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.teams = {1: [dict(ME), dict(P2)], 2: [dict(P3), dict(P4)]}
    s.captains = {1: dict(ME), 2: dict(P3)}
    s.sides = {1: "attack", 2: "defend"}
    s.phase = "lobby"
    s.stage = stage
    s.chat = s._fresh_chat()


# ---------------------------------------------------------------- snapshot shape
def test_idle_snapshot_carries_no_match_slice():
    """Off the match path the comp slice has none of lobby/connect/live/result — only the
    active phase's detail rides along (smaller snapshots, stable de-dup)."""
    i18n.set_language("en")
    panel, s = _panel()
    snap = state_snapshot(s, panel)
    comp = snap["comp"]
    assert comp["phase"] == "idle"
    for key in ("lobby", "connect", "live", "result"):
        assert key not in comp, "idle snapshot should not carry %r" % key
    json.dumps(snap)


def test_lobby_coin_stage():
    """The coin stage exposes who is captain, the toss face, and both teams' rosters."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["stage"] == "coin"
    assert lb["my_team"] == 1 and lb["i_am_captain"] is True
    assert lb["coin"]["my_captain_name"] == "Sam"
    teams = lb["teams"]
    assert [t["team"] for t in teams] == [1, 2]
    me_cell = next(m for m in teams[0]["members"] if m["is_me"])
    assert me_cell["is_captain"] is True and me_cell["name"] == "Sam"
    # Both audiences start empty, without explanatory messages in the chat feed.
    assert lb["chat"] == {"team": [], "all": []}
    assert lb["messages"] == []


# ---------------------------------------------------------------- chat is relayed, not echoed
# The bug these exist for: chat used to append to the sender's own log and go nowhere, so two
# people in one lobby each saw a conversation with themselves and nothing looked broken.
def test_a_chat_line_is_sent_to_the_service_and_not_echoed():
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    before = {ch: len(s.chat_log(ch)) for ch in ("team", "all")}

    s.send_chat("gl hf", "all")
    assert s.client.calls[-1] == ("send_chat", "gl hf", "all")
    s.send_chat("ban rome", "team")
    assert s.client.calls[-1] == ("send_chat", "ban rome", "team")
    # NOTHING was added locally: the service's copy is what gets drawn, so there is no local echo
    # to disagree with it.
    assert {ch: len(s.chat_log(ch)) for ch in ("team", "all")} == before

    s.send_chat("   ", "all")
    s.send_chat("", "team")
    assert s.client.calls[-1] == ("send_chat", "ban rome", "team"), "a blank line is not sent"


def test_an_unknown_channel_is_sent_as_team():
    """Never `all`: the cost of that fallback being backwards is a message meant for four people
    reaching ten. The service applies the same rule; this is the half a patched hub cannot skip."""
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.send_chat("oops", "enemy")
    assert s.client.calls[-1] == ("send_chat", "oops", "team")


def test_an_outgoing_line_is_filtered_before_it_leaves():
    """The service filters too, and its copy is authoritative - but a hub can be newer than the
    service it is talking to, and this is the half we know is current."""
    from hub import censor
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    word = censor.words()[0]
    s.send_chat("hey %s" % word, "all")
    sent = s.client.calls[-1][1]
    assert word not in sent and censor.MASK in sent


def test_a_relayed_chat_event_lands_in_the_right_log():
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.on_live_event({"type": "chat", "channel": "all", "steam_id": "3",
                     "name": "Alpha", "text": "gl hf"})
    s.on_live_event({"type": "chat", "channel": "team", "steam_id": "2",
                     "name": "Ravi", "text": "ban rome"})
    assert s.chat_log("all")[-1] == {"steam_id": "3", "name": "Alpha", "text": "gl hf", "order": 1}
    assert s.chat_log("team")[-1] == {"steam_id": "2", "name": "Ravi", "text": "ban rome", "order": 2}

    chat = state_snapshot(s, panel)["comp"]["lobby"]["chat"]
    assert chat["all"][-1] == {"name": "Alpha", "text": "gl hf"}
    assert chat["team"][-1] == {"name": "Ravi", "text": "ban rome"}


def test_a_relayed_line_with_a_junk_channel_goes_to_the_team_log():
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    before = len(s.chat_log("all"))
    s.on_live_event({"type": "chat", "channel": "", "steam_id": "2", "name": "Ravi", "text": "hi"})
    assert s.chat_log("team")[-1]["text"] == "hi"
    assert len(s.chat_log("all")) == before


def test_the_drawn_log_is_bounded():
    """It is fed by the network, so it gets a ceiling."""
    from hub import competitive as C
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    for n in range(C.CHAT_KEEP + 50):
        s.on_live_event({"type": "chat", "channel": "all", "steam_id": "2",
                         "name": "Ravi", "text": "line %d" % n})
    log = s.chat_log("all")
    assert len(log) == C.CHAT_KEEP
    assert log[-1]["text"] == "line %d" % (C.CHAT_KEEP + 49), "the NEWEST lines are kept"


def test_a_refused_line_says_so_where_it_was_typed():
    """The rate limit is one message every half second, and the JS clears the input the moment
    Send is pressed - so a refusal the hub ignored would make the message quietly not exist. That
    is indistinguishable from the bug this whole feature replaced."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.client.chat_result = (409, {"ok": False, "error": "Slow down."})

    s.send_chat("one", "all")
    assert s.chat_log("all")[-1]["text"] == "Slow down."
    assert s.chat_log("all")[-1]["name"] == "", "a notice is not attributed to anybody"

    # leaning on Enter replaces the notice rather than stacking a screenful of them
    s.send_chat("two", "all")
    s.send_chat("three", "all")
    notices = [m for m in s.chat_log("all") if m.get("notice")]
    assert len(notices) == 1, notices
    assert s.chat_log("team") == [m for m in s.chat_log("team") if not m.get("notice")], \
        "the notice belongs to the channel it was typed into"


def test_a_refusal_with_nothing_to_quote_still_says_something():
    """A dropped request answers 0 with no body - the player still has to be told."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.client.chat_result = (0, {})
    s.send_chat("hello", "team")
    assert s.chat_log("team")[-1]["text"] == i18n.t("comp_chat_failed")


def test_a_line_the_service_accepted_adds_nothing_locally():
    """200 means it is coming back off the stream; anything added here would be a duplicate."""
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    before = len(s.chat_log("all"))
    s.send_chat("gl hf", "all")
    assert len(s.chat_log("all")) == before


# ---------------------------------------------------------------- enemy anonymity
# The pre-round lobby hides the other team so that nobody can dodge a specific person (Sam,
# 2026-09-16). These check the thing that actually protects them: that the persona and the Steam
# picture are not in the snapshot at all, rather than merely not drawn.
def test_lobby_hides_the_enemy_team_behind_call_signs():
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    teams = state_snapshot(s, panel)["comp"]["lobby"]["teams"]
    mine = next(tm for tm in teams if tm["team"] == 1)
    theirs = next(tm for tm in teams if tm["team"] == 2)

    assert [m["name"] for m in mine["members"]] == ["Sam", "Ravi"], "my own team is never hidden"
    assert all(m["hidden"] is False for m in mine["members"])

    assert [m["name"] for m in theirs["members"]] == ["Alpha", "Bravo"]
    for m in theirs["members"]:
        assert m["hidden"] is True
        assert m["avatar"] == "" and m["level"] is None
        assert m["steam_id"], "the id stays: it is not a name, and Report hangs off it"
    blob = json.dumps(state_snapshot(s, panel))
    assert "Wario" not in blob and "Toad" not in blob, "a hidden persona must not reach the page"


def test_call_signs_are_stable_and_agree_across_clients():
    """Ordered by steam id, not by roster position: the enemy's own team-mates read the same
    name in all chat, and a replayed lobby payload that reorders the roster cannot rename them
    halfway through the veto."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    first = s.enemy_aliases()
    s.teams[2] = list(reversed(s.teams[2]))
    assert s.enemy_aliases() == first
    assert first == {"3": "Alpha", "4": "Bravo"}


def test_the_lobby_never_names_an_enemy_captain():
    """Every 'waiting for <name>' line goes through the same aliasing as the roster - the coin
    flip and the veto were the two places that named the other side every single match."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    s.coin_captain = "3"
    s.toss_winner = s.side_picker = s.ban_advantage = s.ban_turn = 2
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["coin_captain_name"] == "Alpha"
    assert lb["coin"]["toss_winner_name"] == "Alpha"
    for key in ("side_picker_name", "ban_advantage_name", "ban_turn_name"):
        assert lb[key] == "Alpha", key


def test_an_enemy_speaking_in_all_chat_speaks_as_their_call_sign():
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.chat["all"].append({"steam_id": "3", "name": "Wario", "text": "gl"})
    s.chat["team"].append({"steam_id": "2", "name": "Ravi", "text": "gl team"})
    chat = state_snapshot(s, panel)["comp"]["lobby"]["chat"]
    assert chat["all"][-1] == {"name": "Alpha", "text": "gl"}
    assert chat["team"][-1] == {"name": "Ravi", "text": "gl team"}, "team-mates keep their names"


def test_the_names_come_back_when_the_lobby_ends():
    """The call signs lift with the LOBBY, not with the match. From the connect window on,
    leaving is a no-show that costs elo and a queue ban, so the penalty is already doing the
    job - and a scoreboard of call signs would be unreadable."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "ready")
    assert s.enemy_aliases()
    s.phase = "connecting"
    assert s.enemy_aliases() == {}
    teams = state_snapshot(s, panel)["comp"]["connect"]["teams"]
    theirs = next(tm for tm in teams if tm["team"] == 2)
    assert [m["name"] for m in theirs["members"]] == ["Wario", "Toad"]


def test_lobby_veto_turn_and_bans():
    """The veto slice carries the map pool, the running bans, and whether it is MY turn to click."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    s.first_ban = 1
    s.ban_turn = 1
    s.bans = [(2, "Rome")]
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["stage"] == "veto"
    pool = lb["veto"]["pool"]
    assert isinstance(pool, list) and len(pool) >= 3 and "Rome" in pool
    assert lb["veto"]["bans"] == [{"team": 2, "map": "Rome"}]
    assert lb["my_turn"] is True                      # my captain, my turn
    # once it is the other captain's turn, my_turn is false and the JS disables the list
    s.ban_turn = 2
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["my_turn"] is False and lb["ban_turn_name"] == "Alpha"   # Wario, anonymised
    # both teams show their side once the veto is under way
    assert {t["side"] for t in lb["teams"]} == {"attack", "defend"}


def test_a_one_sided_lobby_can_still_ban_its_own_maps():
    """One player captains BOTH sides, so every turn is their turn.

    COMP_MATCH_SIZE=1 is the supported way to exercise the whole flow alone (live.cjs says so), and
    buildLobby names captain 2 as `team2[0] || team1[0]` - so a lobby with an empty side has one
    person captaining both. The SERVER already accepts their pick for either side: isLobbyCaptain
    compares against captains[team]. Only the client refused to offer it, because it asked "is it my
    TEAM's turn and am I a captain" instead of "am I the captain whose turn it is" - and a solo test
    lobby got stuck unable to ban its own maps (2026-09-15).

    The same hole would strand a real match whose entire second team walked out.
    """
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    # collapse it to one side: I am on team 1 and captain of both
    s.teams = {1: [dict(ME)], 2: []}
    s.captains = {1: dict(ME), 2: dict(ME)}
    s.first_ban = 1
    s.bans = []

    for turn in (1, 2, 1, 2):
        s.ban_turn = turn
        lb = state_snapshot(s, panel)["comp"]["lobby"]
        assert lb["my_turn"] is True, "turn %d was not offered to the only captain there is" % turn

    # ...and the two earlier stages have the same shape
    s.stage = "side"
    s.side_picker = 2
    assert state_snapshot(s, panel)["comp"]["lobby"]["i_pick_side"] is True
    s.stage = "coin"
    s.toss_winner = 2
    assert state_snapshot(s, panel)["comp"]["lobby"]["coin"]["i_won_toss"] is True


def test_a_normal_lobby_still_hands_the_turn_to_one_side():
    """The fix must not let the other team's captain ban on my turn."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    s.first_ban = 1
    s.ban_turn = 2                 # the OTHER captain
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["my_turn"] is False
    s.ban_turn = 1
    assert state_snapshot(s, panel)["comp"]["lobby"]["my_turn"] is True


def test_the_veto_shows_a_ban_clock_and_only_draws_it():
    """The per-turn clock reaches the UI, and the client never decides the turn is up.

    Sam, 2026-09-15: "add a timer for each ban selection so someone cant stall the lobby out
    forever. add a visible timer in the ui as well". The SERVER owns the deadline and bans for an
    absent captain; this is the half the players watch."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    s.first_ban = 1
    s.ban_turn = 1
    s.bans = []
    s.stage_seconds = 24
    s.stage_total_seconds = 30

    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["stage_seconds"] == 24 and lb["stage_total_seconds"] == 30

    # it ticks DOWN and stops at zero without acting: no ban is invented client-side
    s._tick_stage()
    assert s.stage_seconds == 23 and len(s.bans) == 0
    s.stage_seconds = 1
    s._tick_stage()
    assert s.stage_seconds == 0 and len(s.bans) == 0, "the client must never ban for the server"

    # ...and on a stage with no clock there is nothing to draw at all
    s.stage = "ready"
    s.stage_seconds = 24
    assert state_snapshot(s, panel)["comp"]["lobby"]["stage_seconds"] == 0


def test_every_stalling_stage_carries_a_clock():
    """Sam, 2026-09-16: "too many points where a user could infinitly stall the lobby".

    The coin call, the side/last-ban choice and the attack/defend pick were all unbounded - only
    the veto had a clock. Each of the four now reaches the screen with a countdown on it, because a
    clock the player cannot see is one they will not believe in."""
    i18n.set_language("en")
    panel, s = _panel()
    for stage in ("coin", "flipping", "choice", "side", "veto"):
        _seat_a_lobby(s, stage)
        s.stage_seconds = 19
        s.stage_total_seconds = 30
        lb = state_snapshot(s, panel)["comp"]["lobby"]
        assert lb["stage_seconds"] == 19, "%s reached the screen with no clock" % stage
        # and the tick keeps running on it, rather than stopping outside the veto
        s._tick_stage()
        assert s.stage_seconds == 18, "the clock does not tick during %s" % stage


def test_the_stage_clock_comes_off_the_server_payload():
    """A client that reconnects mid-turn shows the REAL time left, not a fresh full window."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    s._apply_lobby(_srv_lobby("veto", ban_turn=2, first_ban=1, stage_seconds=7,
                              stage_total_seconds=30, bans=[{"team": 1, "map": "Rome"}]))
    assert s.stage_seconds == 7, "the client invented its own window instead of taking the server's"
    assert s.stage_total_seconds == 30


def test_an_older_server_still_gets_its_veto_clock_drawn():
    """`ban_seconds` is the previous release's name for the same field, and the version gate ships
    LENIENT - so this hub can be talking to a server that only clocks the veto and calls it that.
    Reading both names means the veto keeps its clock, and only the three new ones go missing."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    old = _srv_lobby("veto", ban_turn=2, first_ban=1, ban_seconds=11, ban_total_seconds=30)
    old.pop("stage_seconds", None)
    old.pop("stage_total_seconds", None)
    s._apply_lobby(old)
    assert s.stage_seconds == 11, "a lobby from the previous server lost its clock entirely"
    assert s.stage_total_seconds == 30


def test_the_hub_says_when_the_clock_chose_instead_of_a_captain():
    """A teammate who looks up to find the side already picked should be told the CLOCK did it.

    Without this the only reading available is that their captain made the pick, and nine people
    spend the match blaming someone for a choice they never made."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "side")
    s._apply_lobby(_srv_lobby("side", advantage="ban", side_picker=1, coin_auto=True,
                              advantage_auto=True))
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["coin_auto"] is True and lb["advantage_auto"] is True
    assert lb["side_auto"] is False, "a flag nobody set must not be invented"


def _srv_lobby(stage, **over):
    """A server `lobby` payload (server/live.cjs lobbyPayload): team 1 = ME+P2, team 2 = P3+P4,
    the designated captain is team 1's (ME)."""
    base = {
        "teams": {"1": [ME["steam_id"], P2["steam_id"]], "2": [P3["steam_id"], P4["steam_id"]]},
        "captains": {"1": ME["steam_id"], "2": P3["steam_id"]},
        "coin_captain": ME["steam_id"],
        "stage": stage,
        "coin_side": None, "coin_result": None, "toss_winner": None,
        "advantage": None, "side_picker": None, "ban_advantage": None,
        "sides": {"1": "", "2": ""},
        "first_ban": None, "ban_turn": None, "bans": [],
        "pool": ["Airsoft", "BombHouse", "Hospital", "Pool", "Rome", "Russian"],
        "map": None,
    }
    base.update(over)
    return base


def test_pick_coin_only_designated_captain_and_is_server_sent():
    """Only the ONE designated captain (team 1's) may flip, and the flip is SENT to the server
    rather than resolved by a local random — pick_coin only shows the animation and POSTs."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.coin_captain = ME["steam_id"]
    s.coin_side = None; s.coin_result = None; s.toss_winner = None
    # the team-1 NON-captain cannot flip
    s.me = dict(P2)
    s.pick_coin("heads")
    assert s.stage == "coin" and not s.client.calls, "a non-captain must not flip"
    # the designated captain can, and it goes to the server (no local coin_result invented)
    s.me = dict(ME)
    s.pick_coin("heads")
    assert s.stage == "flipping" and s.coin_result is None
    assert ("flip_coin", "heads") in s.client.calls
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["i_am_coin_captain"] is True and lb["coin"]["side"] == "heads"


def test_coin_result_comes_from_server_event():
    """The winner is whatever the server broadcast — applied from the `lobby` event, the SAME on
    every client, never a per-client random.choice."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "coin")
    s.stage = "flipping"
    s.on_live_event({"type": "lobby", **_srv_lobby("choice", coin_side="heads",
                                                   coin_result="tails", toss_winner=2)})
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["stage"] == "choice"
    assert lb["coin"]["result"] == "tails" and lb["coin"]["toss_winner"] == 2
    assert lb["coin"]["i_won_toss"] is False and lb["coin"]["toss_winner_name"] == "Alpha"


def test_side_choice_gives_a_real_attack_defend_selector():
    """Winner picks SIDE -> the winner gets the real attack/defend selector and the loser gets
    the ban advantage (bans last). The side stage shows the selector to the picker only."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "choice")
    s.on_live_event({"type": "lobby", **_srv_lobby("side", coin_result="heads", toss_winner=1,
                                                   advantage="side", side_picker=1,
                                                   ban_advantage=2, first_ban=2, ban_turn=2)})
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert lb["stage"] == "side" and lb["advantage"] == "side"
    assert lb["i_pick_side"] is True                       # I hold the real selector
    assert lb["ban_advantage"] == 2 and lb["ban_advantage_name"] == "Alpha"
    s.choose_side("attack")                                # routes to the server, not local state
    assert ("pick_side", "attack") in s.client.calls


def test_ban_reflects_server_event_and_opens_connect():
    """A ban is SENT to the server; the running bans and the final map come back on a `lobby`
    event, and once the veto is decided the client opens the connect window."""
    i18n.set_language("en")
    panel, s = _panel()
    _seat_a_lobby(s, "veto")
    s.on_live_event({"type": "lobby", **_srv_lobby("veto", sides={"1": "attack", "2": "defend"},
                                                   ban_advantage=1, first_ban=1, ban_turn=1)})
    s.ban("Rome")                                          # my captain bans -> to the server
    assert ("ban_map", "Rome") in s.client.calls
    s.on_live_event({"type": "lobby", **_srv_lobby("veto", sides={"1": "attack", "2": "defend"},
                                                   ban_advantage=1, first_ban=1, ban_turn=2,
                                                   bans=[{"team": 1, "map": "Rome"}])})
    lb = state_snapshot(s, panel)["comp"]["lobby"]
    assert {"team": 1, "map": "Rome"} in lb["veto"]["bans"]
    assert lb["ban_turn"] == 2 and lb["my_turn"] is False
    s.on_live_event({"type": "lobby", **_srv_lobby("ready", sides={"1": "attack", "2": "defend"},
                                                   map="Hospital",
                                                   bans=[{"team": 1, "map": "Rome"}])})
    assert s.map == "Hospital"
    assert any(c[0] == "start_connect" for c in s.client.calls), "the decided veto opens connect"


def test_decided_map_and_sides_reach_launch():
    """The map and sides the lobby decided reach the host pak and the connect payload, so the
    launched game matches the lobby: _prepare_host_pak cooks the lobby pak for the vetoed map."""
    import hub.lobbypak as lp
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.teams = {1: [dict(ME), dict(P2)], 2: [dict(P3), dict(P4)]}
    s.sides = {1: "attack", 2: "defend"}
    s.map = "Hospital"
    s.host = dict(ME)
    s.phase = "connecting"
    prepared = {}
    s._game_dir = lambda: "C:/game"
    orig = lp.prepare
    # *a/**kw on purpose: this stub pins WHICH MAP the host pak is cooked for, which is what the
    # test is about. Spelling the signature out again would mean this test failing every time
    # prepare() grows a parameter - as it did when the per-match join token was threaded through.
    lp.prepare = lambda game, mode, mp, *a, **kw: (prepared.setdefault("map", mp), "BB5_%s" % mp)[1]
    try:
        s._prepare_host_pak()
    finally:
        lp.prepare = orig
    assert prepared.get("map") == "Hospital", "the host pak is cooked for the vetoed map"
    assert s.host_level == "BB5_Hospital"
    c = state_snapshot(s, panel)["comp"]["connect"]
    assert c["map"] == "Hospital"
    assert {tm["side"] for tm in c["teams"]} == {"attack", "defend"}


def test_connect_window_snapshot():
    """The connect slice carries the countdown, the roster progress, host/map, and the pre-computed
    no-show warning (elo + human duration formatted in Python, since JS has no format_duration)."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.teams = {1: [dict(ME), dict(P2)], 2: [dict(P3), dict(P4)]}
    s.captains = {1: dict(ME), 2: dict(P3)}
    s.sides = {1: "attack", 2: "defend"}
    s.map = "Hospital"
    s.host = dict(P3)
    s.phase = "connecting"
    s.connect_left = 175
    s.connect_total = 4
    s.connected_ids = {ME["steam_id"], "2"}
    s.i_connected = True
    c = state_snapshot(s, panel)["comp"]["connect"]
    assert c["left"] == 175 and c["total"] == 4 and c["done"] == 2
    assert c["i_connected"] is True
    assert c["map"] == "Hospital" and c["host"]["name"] == "Wario"
    assert c["warn_elo"] == C.NO_SHOW_ELO and isinstance(c["warn_time"], str) and c["warn_time"]
    assert len(c["teams"]) == 2
    json.dumps(state_snapshot(s, panel))


def test_a_waiting_joiner_can_see_the_hosts_clock():
    """A joiner waiting on the host sees how long the HOST has left, not an open-ended wait.

    Sam, 2026-09-16: "The host has (ticking timer here) to join the match." Before host-ready the
    joiner's own clock has not started, so the screen showed a bare "waiting for the host" line and
    no sign the wait was bounded. `left` is the host's connect clock and it ticks on every client in
    the window, so the slice already carries what that line needs - this pins it.
    """
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.map = "Hospital"
    s.host = dict(P3)                                  # NOT me: this client is a joiner
    s.phase = "connecting"
    s.connect_total = 4
    s.connect_left = 148
    s.host_ready = False                               # nothing to join yet
    c = state_snapshot(s, panel)["comp"]["connect"]
    assert c["is_host"] is False and c["host_ready"] is False
    assert c["join_left"] == 0                         # the joiner's own window has not begun
    assert c["left"] == 148                            # ...but the host's clock is there to show
    assert i18n.t("comp_host_join_timer", time="2:28") == "The host has 2:28 to join the match."


def test_the_connect_clock_is_re_based_by_the_server():
    """The countdown follows the SERVER's deadline, not the number it was handed at the start.

    Sam, 2026-09-16: "the time to connect timer is still ticking down and the timer could run out
    before the hoster respawns". The server re-arms the connect window whenever the host's game
    proves it is on its way - asking for its travel permit, then reporting in from the match world -
    and every match_connect now carries what is left. Before this, ten screens counted down to 0:00
    against a deadline that had quietly moved five minutes out, and people left a live match.
    """
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.map = "Hospital"
    s.host = dict(P3)
    s.phase = "connecting"
    s.connect_total = 4
    s.connect_left = 12                                # nearly out, with the host still booting
    s.on_live_event({"type": "match_connect", "connected": [], "total": 4,
                     "connect_seconds": 300})
    assert s.connect_left == 300
    assert state_snapshot(s, panel)["comp"]["connect"]["left"] == 300

    # A payload without the field is an older service: leave the clock exactly as it was.
    s.on_live_event({"type": "match_connect", "connected": [], "total": 4})
    assert s.connect_left == 300
    # ...and so is a zero. A window that has really run out ends with match_cancelled, which says so.
    s.on_live_event({"type": "match_connect", "connected": [], "total": 4, "connect_seconds": 0})
    assert s.connect_left == 300


def test_a_clock_that_hit_zero_starts_again_when_the_server_re_bases_it():
    """LiveSession's tick stops at zero and waits to be told - so adopting a new deadline has to
    restart the loop, or the screen would freeze on the number it was given and never move again."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2)]
    s.host = dict(P2)
    s.phase = "connecting"
    s.connect_total = 2
    s.connect_left = 0                                 # the loop has already stopped itself
    s.on_live_event({"type": "match_connect", "connected": [], "total": 2,
                     "connect_seconds": 240})
    assert s.connect_left == 240
    s._tick_connect()                                  # the loop is running again
    assert s.connect_left == 239


def test_a_joiner_is_not_fined_when_the_host_never_loaded_in():
    """Sam, 2026-09-16, from a live test: he let the connect window run out as the host and "i got
    the rr penalty and the timer, but so did the joiner". A joiner cannot begin to join until the
    host's game is up (host_ready), so a window that closes before that never counted against them.
    The server decides the same way (live.cjs expireConnect); this is the hub's local mirror."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2)]
    s.map = "Hospital"
    s.host = dict(P2)                                  # somebody else hosts; ME is the joiner
    s.phase = "connecting"
    s.connect_total = 2
    s.host_ready = False                               # their game never came up
    s.i_connected = False
    s._connect_expired()
    assert s.penalty_until == 0 and s.penalty_reason == ""
    assert s.error == C.t("comp_no_show_others")

    # ...but once the host WAS up, a joiner who still never loaded in is a real no-show.
    panel, s = _panel()
    s.players = [dict(ME), dict(P2)]
    s.host = dict(P2)
    s.phase = "connecting"
    s.connect_total = 2
    s.host_ready = True
    s.i_connected = False
    s._connect_expired()
    assert s.penalty_reason == "no_show" and s.penalty_until > 0


def test_a_host_who_never_loaded_in_still_pays():
    """The other half of the same rule: the host has nobody to wait for, so their window closing
    with them not in it is exactly the offence the penalty exists for."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2)]
    s.host = dict(ME)
    s.phase = "connecting"
    s.connect_total = 2
    s.host_ready = False                               # it never came up - because WE never did
    s.i_connected = False
    assert s._i_am_host() is True
    s._connect_expired()
    assert s.penalty_reason == "no_show" and s.penalty_until > 0


def test_connect_host_gating_non_host():
    """A NON-HOST does not get a join countdown when the connect window merely opens: the slice
    says host_ready is False and is_host is False, so the JS shows "waiting for the host". Once the
    host appears in a match_connect roster, host_ready flips and the 5-minute join window starts."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.teams = {1: [dict(ME), dict(P2)], 2: [dict(P3), dict(P4)]}
    s.captains = {1: dict(ME), 2: dict(P3)}
    s.map = "Hospital"
    s.host = dict(P3)                                  # Wario hosts; ME (Sam) is a non-host
    s.phase = "connecting"
    s.connect_total = 4
    s.connected_ids = set()
    assert s._i_am_host() is False
    c = state_snapshot(s, panel)["comp"]["connect"]
    assert c["is_host"] is False and c["host_ready"] is False and c["join_left"] == 0

    # the host's game reports in -> host_id lands in the connected roster
    s.on_live_event({"type": "match_connect", "connected": [P3["steam_id"]], "total": 4})
    assert s.host_ready is True
    c = state_snapshot(s, panel)["comp"]["connect"]
    assert c["host_ready"] is True and c["join_left"] == C.JOIN_SECONDS
    json.dumps(state_snapshot(s, panel))


def test_connect_host_gating_host_has_no_join_window():
    """The HOST sees host_ready flip too (the gate is open) but gets NO separate join countdown -
    their clock is the connect window, and Join is a non-host affordance."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.map = "Hospital"
    s.host = dict(ME)                                  # ME (Sam) hosts
    s.phase = "connecting"
    s.connect_total = 4
    s.connected_ids = set()
    assert s._i_am_host() is True
    s.on_live_event({"type": "match_connect", "connected": [ME["steam_id"]], "total": 4})
    assert s.host_ready is True
    c = state_snapshot(s, panel)["comp"]["connect"]
    assert c["is_host"] is True and c["host_ready"] is True and c["join_left"] == 0


def test_the_launch_button_replaced_the_in_game_button():
    """"I am in the game" is gone from the screen, and the joiner's "Launch game" took its place
    (Sam, 2026-09-16).

    Both halves matter. The BUTTON had to go: it asserted a fact the app could not see, and the
    thing that now knows it is either the lobby probe (the host) or the act of launching (a
    joiner). And the replacement has to be GREYED rather than absent until host_ready, because a
    joiner's pak gets one lobby search per launch - a button that could be pressed early would
    spend it on a Steam that has nothing to find yet."""
    js = JS_PATH.read_text(encoding="utf-8")
    assert 'call("report_connected")' not in js, (
        'the "I am in the game" button is still wired up in the screen')
    assert 't("comp_connect_button")' not in js, (
        'the "I am in the game" label is still rendered')
    assert 'call("launch_game")' in js, "the joiner has no Launch button"
    # the gate, spelled exactly: the button is built disabled until the host is ready
    assert re.search(r't\("comp_launch"\)[\s\S]{0,160}disabled:\s*!ready', js), (
        "Launch game must be disabled until host_ready")


def test_launch_game_is_gated_on_the_host_and_is_safe_everywhere_else():
    """The greyed button is a hint; the session verb is the guard. A joiner who clicks on a stale
    snapshot - or a host, who has no such button at all - must not spend the lobby search."""
    from hub import game as game_mod
    calls = []
    real = game_mod.launch_game
    game_mod.launch_game = lambda: (calls.append("launch"), True)[1]
    try:
        panel, s = _panel()
        s.players = [dict(ME), dict(P2)]
        s.host = dict(P2)                              # ME is a joiner
        s.phase = "idle"
        s.launch_game()                                # not in a match
        assert calls == [] and s.i_connected is False

        s.phase = "connecting"
        s.host_ready = False
        s.launch_game()                                # the gate is shut
        assert calls == [], "a stale click must not launch before host-ready: %r" % (calls,)
        assert s.i_connected is False

        s.host = dict(ME)                              # I am the host: my game opened itself
        s.host_ready = True
        s.launch_game()
        assert calls == [], "the host has no Launch button and must not get one: %r" % (calls,)
    finally:
        game_mod.launch_game = real


def test_relaunch_game_guarded_and_safe():
    """relaunch_game only runs while a match is up, and is a safe no-op elsewhere (and off Windows,
    launch_game() itself is a no-op)."""
    panel, s = _panel()
    s.phase = "idle"
    s.relaunch_game()                                  # not in a match: does nothing, does not raise
    s.phase = "connecting"
    s.relaunch_game()                                  # in a match: calls launch_game() (no-op here)
    s.join_match()                                     # placeholder no-op, must never raise


def test_live_snapshot_with_and_without_vote():
    """The live slice exposes map/host and, when a vote-to-cancel is running, its tally."""
    i18n.set_language("en")
    panel, s = _panel()
    s.players = [dict(ME), dict(P2), dict(P3), dict(P4)]
    s.teams = {1: [dict(ME), dict(P2)], 2: [dict(P3), dict(P4)]}
    s.captains = {1: dict(ME), 2: dict(P3)}
    s.sides = {1: "attack", 2: "defend"}
    s.map = "Pool"
    s.host = dict(P3)
    s.phase = "live"
    s.vote = None
    lv = state_snapshot(s, panel)["comp"]["live"]
    assert lv["map"] == "Pool" and lv["host"]["name"] == "Wario"
    assert lv["vote"] is None
    # LiveSession is a mock/live-preview, so the preview finish cue is offered
    assert lv["can_finish"] is False
    s.vote = {"caller": "Sam", "yes": 3, "no": 1, "voted": True}
    lv = state_snapshot(s, panel)["comp"]["live"]
    assert lv["vote"]["yes"] == 3 and lv["vote"]["no"] == 1
    assert lv["vote"]["needed"] == C.VOTE_NEEDED and lv["vote"]["voted"] is True


def test_result_snapshot_win_loss_void():
    """The result slice carries the outcome, the score and the rank delta for each ending."""
    i18n.set_language("en")
    panel, s = _panel()
    s.phase = "result"
    s.map = "Rome"
    s.result = {"won": True, "score": (7, 4), "delta": 2, "voided": False}
    r = state_snapshot(s, panel)["comp"]["result"]
    assert r["won"] is True and r["score"] == [7, 4] and r["delta"] == 2 and r["voided"] is False
    assert r["map"] == "Rome"
    s.result = {"won": False, "score": (0, 0), "delta": 0, "voided": True}
    r = state_snapshot(s, panel)["comp"]["result"]
    assert r["voided"] is True and r["delta"] == 0


def test_result_snapshot_carries_the_rr_not_just_the_arrows():
    """SAM, 2026-09-16: "we are only gaining and losing 1-3 RR" - the result screen printed the
    arrow count as a bare "+2". The slice carries the RR the match moved, and None when the service
    did not send it, so the arrows are never mistaken for it."""
    panel, s = _panel()
    s.phase = "result"
    s.result = {"won": True, "score": (7, 4), "delta": 2, "rr_delta": 23, "voided": False}
    r = state_snapshot(s, panel)["comp"]["result"]
    assert r["rr_delta"] == 23 and r["delta"] == 2 and r["placing"] is False
    s.result = {"won": True, "score": (7, 4), "delta": 2, "voided": False}
    assert state_snapshot(s, panel)["comp"]["result"]["rr_delta"] is None
    s.result = {"won": False, "score": (4, 7), "delta": 0, "rr_delta": 0, "voided": False,
                "placing": True, "placements_left": 2}
    r = state_snapshot(s, panel)["comp"]["result"]
    assert r["placing"] is True and r["placements_left"] == 2
    js = (REPO / "hub" / "webui" / "static" / "screens" / "competitive.js").read_text(encoding="utf-8")
    assert "r.rr_delta" in js and "var d = r.delta" not in js


def test_result_snapshot_carries_what_became_of_the_game():
    """The hub closes Bodycam once it has registered the match complete, so the result screen has
    to be able to say so. "armed" is deliberately indistinguishable from nothing on screen: the
    player is reading a scoreboard, not watching a countdown to their game being shut."""
    panel, s = _panel()
    s.phase = "result"
    s.result = {"won": True, "score": (7, 4), "delta": 2, "voided": False}
    for state in ("", "armed", "closing", "closed", "forced", "failed", "skipped"):
        s.game_close = state
        assert state_snapshot(s, panel)["comp"]["result"]["game_close"] == state, state


# ---------------------------------------------------------------- verb mapping
def test_matchflow_verbs_map_to_session():
    """Each new js_api verb is a 1:1 passthrough to the matching session verb (run on the UI
    thread via panel.post), forwarding the JS arguments unchanged."""
    panel, _ = _panel()
    rec = _CallRecorder()
    panel.session = rec                                # record what the bridge calls
    api = Api(panel)

    api.pick_coin("heads");     assert rec.calls[-1] == ("pick_coin", ("heads",), {})
    api.choose("side");         assert rec.calls[-1] == ("choose", ("side",), {})
    api.choose_side("attack");  assert rec.calls[-1] == ("choose_side", ("attack",), {})
    api.ban_map("Rome");        assert rec.calls[-1] == ("ban", ("Rome",), {})
    api.send_chat("gl hf");     assert rec.calls[-1] == ("send_chat", ("gl hf", "team"), {})
    api.send_chat("hi", "all");  assert rec.calls[-1] == ("send_chat", ("hi", "all"), {})
    api.launch_game();          assert rec.calls[-1][0] == "launch_game"
    api.relaunch_game();        assert rec.calls[-1][0] == "relaunch_game"
    api.join_match();           assert rec.calls[-1][0] == "join_match"
    api.start_vote();           assert rec.calls[-1][0] == "start_vote"
    api.cast_vote(True);        assert rec.calls[-1] == ("cast_vote", (True,), {})
    api.cast_vote(False);       assert rec.calls[-1] == ("cast_vote", (False,), {})
    api.finish_match();         assert rec.calls[-1][0] == "finish"
    api.leave_match();          assert rec.calls[-1][0] == "leave_result"


def test_matchflow_strings_exist_in_every_language():
    """Every i18n key the match-flow phases render is present in all seven languages, so no phase
    ever falls back to a raw key. (The JS-scan test in tests/test_hub.py guards English; this
    guards the rest for the specific keys these phases use.)"""
    keys = [
        "comp_coin_title", "comp_coin_pick", "comp_coin_wait", "comp_coin_flipping",
        "comp_coin_result", "comp_coin_landed", "comp_heads", "comp_tails",
        "comp_auto_pick", "comp_veto_clock",
        "comp_choice_title", "comp_choice_wait", "comp_choose_side", "comp_choose_ban",
        "comp_side_title", "comp_side_pick", "comp_side_wait", "comp_veto_last_ban",
        "comp_veto_title", "comp_veto_your_turn", "comp_veto_turn", "comp_veto_done",
        "comp_banned_by", "comp_team", "comp_captain", "comp_side_attack", "comp_side_defend",
        "comp_chat_title", "comp_chat_send", "comp_chat_all_title", "comp_chat_all_welcome",
        "comp_hidden_note",
        "comp_connect_title", "comp_connect_timer", "comp_connect_map", "comp_connect_host",
        "comp_connect_count", "comp_launch", "comp_connect_waiting", "comp_connect_warn",
        "comp_waiting_host", "comp_relaunch",
        "comp_live_title", "comp_map", "comp_host", "comp_join", "comp_join_hint", "comp_report",
        "comp_vote_title", "comp_vote_body", "comp_vote_count", "comp_vote_yes", "comp_vote_no",
        "comp_vote_cast", "comp_result_win", "comp_result_loss", "comp_result_void",
        "comp_result_void_body", "comp_back", "comp_preview_finish",
        "comp_game_closing", "comp_game_closed", "comp_game_close_failed",
    ]
    missing = {}
    for lang, table in i18n.STRINGS.items():
        gaps = [k for k in keys if k not in table]
        if gaps:
            missing[lang] = gaps
    assert not missing, "match-flow i18n keys missing per language: %s" % missing


# ---------------------------------------------------------------- the match-found cue
def _capture_cue(panel):
    """Swap the panel module's sounds for a recorder. Returns (rings, restore).

    Patched on `panel_mod.sounds_mod` rather than on the panel, because the whole point is to
    prove the PANEL reaches the sound module at all - a test that stubbed panel.play_match_found
    would pass with the bug in place."""
    rings = []
    real = panel_mod.sounds_mod.play_match_found
    panel_mod.sounds_mod.play_match_found = lambda schedule, volume=100: rings.append(volume) or True

    def restore():
        panel_mod.sounds_mod.play_match_found = real
    return rings, restore


def test_found_rings_once_per_entry():
    """The cue fires on the EDGE into "found" - once - and again on a later match.

    The web UI shipped without this: WebPanel had no _phase_changed, so the hub went silent on a
    found match while Settings' Test button still worked (Sam, 2026-09-15)."""
    panel, s = _panel()
    rings, restore = _capture_cue(panel)
    try:
        s.phase = "idle"
        panel.on_change()
        assert rings == [], "no cue while idle"
        s.phase = "found"
        panel.on_change()
        assert rings == [sounds_mod.DEFAULT_VOLUME], "entering found rings once, at the default"
        # on_change runs on every accept tick and every accept; none of them may ring again
        s.accept_left = 9
        panel.on_change()
        s.accept_left = 8
        panel.on_change()
        assert len(rings) == 1, "a redraw inside the accept window must not ring again"
        s.phase = "lobby"
        panel.on_change()
        s.phase = "idle"
        panel.on_change()
        s.phase = "found"
        panel.on_change()
        assert len(rings) == 2, "the next match rings again"
    finally:
        restore()


def test_found_cue_follows_the_slider():
    """Muted never reaches the audio device; a set level is what plays."""
    panel, s = _panel()
    rings, restore = _capture_cue(panel)
    try:
        panel.app.state["comp_sound_volume"] = 0
        s.phase = "found"
        panel.on_change()
        assert rings == [], "muted means muted - not a silent WAV played at the device"
        panel.app.state["comp_sound_volume"] = 35
        s.phase = "idle"
        panel.on_change()
        s.phase = "found"
        panel.on_change()
        assert rings == [35]
        # the legacy on/off flag a hub older than the slider wrote is still honoured
        panel.app.state["comp_sound_volume"] = None
        panel.app.state["comp_sound"] = False
        s.phase = "idle"
        panel.on_change()
        s.phase = "found"
        panel.on_change()
        assert rings == [35], "the pre-slider off flag still mutes"
    finally:
        restore()


def test_a_real_match_found_event_rings():
    """End to end through the session: the server's match_found is what the player hears."""
    panel, s = _panel()
    rings, restore = _capture_cue(panel)
    try:
        s.phase = "idle"
        panel.on_change()
        s.on_live_event({"type": "match_found", "match_id": "m1", "accept_seconds": 12,
                         "players": [dict(ME), dict(P2), dict(P3), dict(P4)]})
        assert s.phase == "found"
        assert rings == [sounds_mod.DEFAULT_VOLUME], "a found match rings"
    finally:
        restore()


def test_teamkill_warning_waits_for_the_render_acknowledgement():
    """SSE receipt queues visible text; only the UI-render verb acknowledges it upstream."""
    before = i18n.get_language()
    try:
        for code in i18n.CODES:
            assert "comp_teamkill_warning" in i18n.STRINGS[code], code
            assert i18n.tr(code, "comp_teamkill_warning").strip(), code
        i18n.set_language("en")
        panel, s = _panel()
        s.phase = "live"
        s.match_id = "m-warning"
        s.on_live_event({"type": "team_kill_warning", "match_id": "m-warning",
                         "warning_id": "w-1", "flags": ["continued"], "enforced": True})

        events = panel.events_since(0)
        assert len(events) == 1 and events[0]["type"] == "combat_warning"
        assert events[0]["text"] == i18n.t("comp_teamkill_warning")
        assert events[0]["match_id"] == "m-warning" and events[0]["warning_id"] == "w-1"
        assert s.client.calls == [], "receiving SSE is not proof that the player saw the warning"

        # A replay cannot stack the same visible warning or create a second pending receipt.
        s.on_live_event({"type": "team_kill_warning", "match_id": "m-warning",
                         "warning_id": "w-1", "flags": [], "enforced": True})
        assert len(panel.events_since(0)) == 1

        s.ack_combat_warning("m-warning", "w-1")
        assert s.client.calls == [("ack_combat_warning", "m-warning", "w-1")]
        # A forged or already-completed browser call cannot acknowledge arbitrary evidence.
        s.ack_combat_warning("m-warning", "not-presented")
        s.ack_combat_warning("m-warning", "w-1")
        assert len(s.client.calls) == 1
    finally:
        i18n.set_language(before)


def test_a_broken_audio_device_never_costs_a_match():
    """play_match_found raising must not stop the snapshot that draws the accept window."""
    panel, s = _panel()
    real = panel_mod.sounds_mod.play_match_found

    def boom(schedule, volume=100):
        raise RuntimeError("no audio device")

    panel_mod.sounds_mod.play_match_found = boom
    try:
        s.phase = "found"
        panel.on_change()
        assert panel.last_state["comp"]["phase"] == "found", "the accept window still drew"
    finally:
        panel_mod.sounds_mod.play_match_found = real


# ---------------------------------------------------------------- the looping animations
CSS_PATH = REPO / "hub" / "webui" / "static" / "screens" / "competitive.css"
JS_PATH = REPO / "hub" / "webui" / "static" / "screens" / "competitive.js"


def _css_duration_ms(selector):
    """The animation duration on `selector` in competitive.css, in ms."""
    css = CSS_PATH.read_text(encoding="utf-8")
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert rule, "no %r rule in competitive.css" % selector
    dur = re.search(r"animation:[^;]*?(\d*\.?\d+)s", rule.group(1))
    assert dur, "no animation duration on %r: %r" % (selector, rule.group(1))
    return round(float(dur.group(1)) * 1000)


def _js_const(name):
    m = re.search(r"\bvar\s+" + name + r"\s*=\s*(\d+)\s*;", JS_PATH.read_text(encoding="utf-8"))
    assert m, "no `var %s = <n>;` in competitive.js" % name
    return int(m.group(1))


def _css_rule(selector):
    """The declaration block of `selector` in competitive.css, comments stripped."""
    css = re.sub(r"/\*.*?\*/", "", CSS_PATH.read_text(encoding="utf-8"), flags=re.S)
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert rule, "no %r rule in competitive.css" % selector
    return rule.group(1)


def test_the_animation_durations_match_the_css():
    """The JS phase constants and the CSS durations are two halves of one number.

    heldPhase() hands each rebuilt element a negative animation-delay modulo these, so a drift
    between them does not fail loudly — it just makes the loop stutter at the wrap, which is the
    original bug wearing a different hat."""
    assert _js_const("SWEEP_MS") == _css_duration_ms(".search-bar > i"), \
        "SWEEP_MS must equal the hubslide duration on .search-bar > i"
    assert _js_const("FLIP_MS") == _css_duration_ms(".coin.flip"), \
        "FLIP_MS must equal the hubflip duration on .coin.flip"


def test_the_animated_elements_carry_a_held_phase():
    """Both looping animations are handed a phase when they are (re)built.

    Without this the element is new once a second — the queue timer rewrites the snapshot — and
    its CSS animation restarts from zero, so a 1.4s sweep never got past ~69%."""
    js = JS_PATH.read_text(encoding="utf-8")
    assert "animationDelay = heldPhase(SWEEP_MS)" in js, "the search bar must hold its phase"
    assert "animationDelay = heldPhase(FLIP_MS)" in js, "the coin must hold its phase"


# Layout and redraw behavior are exercised in test_lobby_chat_browser.cjs.


def test_a_scroll_position_survives_the_rebuild():
    """The other half of the same bug, and the half that made it hopeless rather than awkward.

    render() does `app.innerHTML = ""` and draws the screen again for every snapshot that differs
    from the last - and in a veto the ban clock differs EVERY SECOND. A scroll container built by
    that render starts at scrollTop 0, so scrolling down to the chat was not slow, it was
    impossible: the pane snapped back to the top a second later, every time. Measured in a browser
    against a ticking veto snapshot - strip dragged to 348, three redraws later it read 0.

    keepScroll is the same answer as heldPhase: nothing preserves the old NODE, so a module-level
    record outlives it. `stick` is the chat log's extra rule - follow the newest line, unless the
    reader has scrolled up into the history, in which case their place is what is kept. Verified
    the same way afterwards: strip left at 200 is still 200 after three rebuilds, a log scrolled
    to the top stays there, and the untouched log is still pinned to its last line."""
    js = JS_PATH.read_text(encoding="utf-8")
    render_at = js.index("function render(root, state, ctx)")
    at = re.search(r"\bvar\s+scrollMemory\s*=", js)
    assert at and at.start() < render_at, (
        "scrollMemory must be assigned at MODULE scope, above render() - inside it it is born "
        "empty on the render that needs to read it, which is the bug it exists to fix")
    assert js.index("function keepScroll") < render_at, "keepScroll belongs at module scope too"

    assert 'keepScroll(top, "lobby-top", false)' in js, \
        "the teams/veto strip must keep its place, or the map you scrolled to is gone in a second"
    assert "keepScroll(log, logId, true)" in js, \
        "a chat log sticks to the newest line - `true` is that rule"
    assert "document.getElementById(logId); if (l) { l.scrollTop = l.scrollHeight; }" not in js, \
        "the unconditional pin yanked a reader out of the history once a second"


def test_the_phase_constants_are_declared_before_render():
    """MODULE scope, above render() — the whole point.

    These first sat next to searchBox, which is inside render() and hundreds of lines below the
    call that reads it. `var` hoists the declaration but not the assignment, so SWEEP_MS was
    undefined at the only moment it was read; `Date.now() % undefined` is NaN, and a browser
    discards an invalid animation-delay silently. The fix ran perfectly and did nothing. The same
    trap already cost a shipped release once — see the CLOSE_LINES comment in competitive.js."""
    js = JS_PATH.read_text(encoding="utf-8")
    render_at = js.index("function render(root, state, ctx)")
    for name in ("SWEEP_MS", "FLIP_MS"):
        at = re.search(r"\bvar\s+" + name + r"\s*=", js)
        assert at and at.start() < render_at, (
            "%s must be assigned at module scope, above render() - inside it it is still "
            "undefined when heroAction reads it" % name)
    assert js.index("function heldPhase") < render_at, "heldPhase belongs at module scope too"



def test_the_idle_hero_no_longer_lectures_about_the_game_being_open():
    """The paragraph under Find match is gone, and the toast it was replaced by can be held for
    three seconds (Sam, 2026-09-16).

    Both halves matter. The TEXT had to go: it asked every player, every time, to remember a rule
    that only bites the few who have Bodycam open - and it was advice, so nothing stopped them
    queueing anyway. The replacement is a refusal (C.Session.find_match), and it needs a cue that
    stays up long enough to read: the toast took one fixed 1.8 s before this, so the duration
    travels with the event."""
    js = JS_PATH.read_text(encoding="utf-8")
    assert 't("comp_ready_state")' not in js, (
        "the 'you do not need Bodycam open' paragraph is still under Find match")
    core = (REPO / "hub" / "webui" / "static" / "core.js").read_text(encoding="utf-8")
    assert "HubUI.toast(ev.text, ev.ms)" in core, "the cue's duration never reaches the toast"
    ui = (REPO / "hub" / "webui" / "static" / "ui.js").read_text(encoding="utf-8")
    assert "function toast(text, ms)" in ui and "+ms > 0 ? +ms : 1800" in ui, (
        "toast must honour a per-cue duration and keep its 1.8 s default")

# ---------------------------------------------------------------- runner
def _run(fn):
    name = fn.__name__
    try:
        fn()
        RESULTS.append((name, True))
        print("ok   " + name)
    except Exception:                                  # noqa: BLE001 — report, do not abort
        RESULTS.append((name, False))
        print("FAIL " + name)
        traceback.print_exc()


def main():
    for fn in [
        test_idle_snapshot_carries_no_match_slice,
        test_lobby_coin_stage,
        test_lobby_veto_turn_and_bans,
        test_a_one_sided_lobby_can_still_ban_its_own_maps,
        test_a_normal_lobby_still_hands_the_turn_to_one_side,
        test_the_veto_shows_a_ban_clock_and_only_draws_it,
        test_pick_coin_only_designated_captain_and_is_server_sent,
        test_coin_result_comes_from_server_event,
        test_side_choice_gives_a_real_attack_defend_selector,
        test_ban_reflects_server_event_and_opens_connect,
        test_decided_map_and_sides_reach_launch,
        test_connect_window_snapshot,
        test_connect_host_gating_non_host,
        test_connect_host_gating_host_has_no_join_window,
        test_the_launch_button_replaced_the_in_game_button,
        test_launch_game_is_gated_on_the_host_and_is_safe_everywhere_else,
        test_relaunch_game_guarded_and_safe,
        test_live_snapshot_with_and_without_vote,
        test_result_snapshot_win_loss_void,
        test_result_snapshot_carries_the_rr_not_just_the_arrows,
        test_result_snapshot_carries_what_became_of_the_game,
        test_found_rings_once_per_entry,
        test_found_cue_follows_the_slider,
        test_a_real_match_found_event_rings,
        test_teamkill_warning_waits_for_the_render_acknowledgement,
        test_a_broken_audio_device_never_costs_a_match,
        test_every_stalling_stage_carries_a_clock,
        test_the_stage_clock_comes_off_the_server_payload,
        test_an_older_server_still_gets_its_veto_clock_drawn,
        test_the_hub_says_when_the_clock_chose_instead_of_a_captain,
        test_the_animation_durations_match_the_css,
        test_the_animated_elements_carry_a_held_phase,
        test_a_scroll_position_survives_the_rebuild,
        test_the_phase_constants_are_declared_before_render,
        test_the_idle_hero_no_longer_lectures_about_the_game_being_open,
        test_matchflow_verbs_map_to_session,
        test_matchflow_strings_exist_in_every_language,
    ]:
        _run(fn)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
