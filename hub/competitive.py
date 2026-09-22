"""The Competitive tab — a FACEIT-style ranked home for Bodybomb 5v5.

Sam's design (docs/competitive-ideas.md, decided 2026-09-14):
  * Competitive is a separate tab and needs the Bodybomb 5v5 gamemode installed.
  * You sign in with STEAM, so the rank belongs to the account that actually plays.
  * Queue -> accept -> random captains -> a visible coin flip -> the toss winner takes
    the starting side OR the first ban -> alternating map bans down to one map -> play.
  * Levels 1-10 never show numbers, only arrows (1/2/3 green up, 1/2/3 red down).
    Level 10 opens BDR: 100/200/300 BDR = levels 11/12/13, top 500 above 13 = level 14.
  * No dedicated servers, so one of the ten players hosts - the one with the best ping.
  * Integrity (Sam, 2026-09-14): verify the game files against known-good hashes, allow
    nothing in ~mods but our own pak, require the hub to stay connected, watch the match
    stats server-side, and let 6 of 10 players void a match. NO process/module scanning -
    Sam ruled that out: the hub never looks outside the game folder.

This module is the WINDOW only. Everything it shows comes from a `Session` object, and
the session it uses today is `MockSession`: a local, offline stand-in that walks through
the whole flow on timers so the shape can be judged before a backend exists. Step 3 of
the plan replaces it with a WebSocket client that implements the same handful of methods;
nothing in the panel should need to change.

Threading: the panel is main-thread only, exactly like the rest of the hub. A real
session must post to app.q and let _pump() call panel.on_change().
"""
import copy
import random
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import font as tkfont

from . import auth as auth_mod
from . import avatars as avatars_mod
from . import censor as censor_mod
from . import game as game_mod
from . import match_cleanup
from . import i18n
from . import lobbypak as lobbypak_mod
from . import live as live_mod
from . import player_identity
from . import paths
from . import sounds as sounds_mod
from . import state as state_mod
from .i18n import t
from . import catalogue as catalogue_mod
from .version import HUB_VERSION
from .theme import (WHITE, BLACK, GREY, LINE, PANEL, PANEL_LINE, ACCENT, ACCENT_DARK,
                    GREEN, RED, AMBER, MUTED, SELECT_BG,
                    level_colour, level_text_colour, rank_colour, rank_text_colour, RANKS,
                    division_numeral, avatar_colour, initials)

# The gamemode Competitive runs on. Must match gamemodes/bb5/manifest.json "id".
COMPETITIVE_MODE_ID = "BB5"

# Every map the gamemode ships, in the manifest's order. The live list comes from the
# catalogue entry at runtime; this is the fallback until the catalogue has loaded.
DEFAULT_MAPS = ["Airsoft", "BombHouse", "Hospital", "Paintball", "Pool", "Rome", "Russian"]

# Ranked is Bodybomb 5v5, and the gamemode id is what prefixes the cooked level names (BB5_Rome,
# BB5_PublicPool). It is NOT interchangeable with the display names above: hub/lobbypak.py resolves
# "Pool" to BB5_PublicPool and "Russian" to BB5_RussianBuilding off the installed pak, because the
# manifest carries display names and opening a level that does not exist tears the host's world down.
HOST_GAMEMODE_ID = "BB5"

# Maps the gamemode ships but COMPETITIVE does not play (Sam, 2026-09-14: "lets remove
# paintball from the competitive pool"). They stay playable in a casual Bodybomb 5v5 match;
# this only trims the ranked veto. Add or remove a name here and the veto follows.
COMPETITIVE_EXCLUDED_MAPS = {"Paintball"}


def competitive_pool(maps):
    """The ranked pool: what the gamemode ships, minus the excluded maps, order kept.

    The veto bans down to one map whatever the size, but the arithmetic is worth knowing:
    an ODD pool (7 maps, 6 bans) splits the bans evenly 3/3, while an EVEN pool (6 maps,
    5 bans) gives the team that bans first one extra ban AND the final one. That is a real
    advantage, and it is deliberately the thing the coin-flip loser gets when the winner
    takes the starting side — see docs/competitive.md."""
    return [m for m in maps if m not in COMPETITIVE_EXCLUDED_MAPS]


def ban_first_team(pool_size, advantage_team):
    """Who bans FIRST so that the ban-advantage team bans LAST (Sam: the advantage IS the
    final ban, which controls the one map left standing).

    The veto alternates, so who bans last is pure parity of the number of bans (pool - 1):
    with an ODD number of bans the team that goes first also goes last, so the advantage team
    must lead; with an EVEN number the last ban falls to the second team, so the advantage
    team must go second. Getting this backwards hands the map to the wrong team, which is why
    the server and the mock share this one function rather than each guessing (mirrored in
    server/live.cjs banFirstTeam)."""
    bans_total = max(0, int(pool_size or 0) - 1)
    other = 2 if advantage_team == 1 else 1
    return advantage_team if bans_total % 2 == 1 else other


TEAM_SIZE = 5
TEAM_COUNT = 2
LOBBY_SIZE = TEAM_SIZE * TEAM_COUNT
BAN_SECONDS = 30                     # fallback only, same rule as ACCEPT_SECONDS below:
                                     # the lobby payload carries the server's value
# Every OTHER lobby stage is on a clock too (live.cjs PICK_SECONDS): the coin call, the
# side/last-ban choice and the attack/defend pick. Fallbacks, exactly like BAN_SECONDS - the
# lobby payload carries `stage_total_seconds` and that is what is drawn.
PICK_SECONDS = 30
FLIP_SECONDS = 4                     # how long the coin is in the air; a server-held stage now,
                                     # so all ten clients watch the same toss land
# The lobby stages a person can hold up, and so the ones that carry a countdown. "flipping" is in
# here because it IS clocked - by the server, as a presentation hold - even though nobody can
# stall it; "ready" is not, because it is waiting on the connect window rather than on anyone.
TIMED_STAGES = ("coin", "flipping", "choice", "side", "veto")
ACCEPT_SECONDS = 30                  # fallback only: match_found carries the server's own
                                     # value and that wins. Kept in step with
                                     # live.cjs ACCEPT_SECONDS so the two never disagree
                                     # on a screen the player is being penalised against.
VOTE_NEEDED = 6                      # 6 of 10 (Sam, 2026-09-21)

# The lobby's two chat logs. "team" is the four people you are playing with; "all" is the
# whole lobby, the other five included.
CHAT_CHANNELS = ("team", "all")

# What one chat line may be. The JS input already caps at 200, and the JS is the one part of this
# a player can edit, so the session enforces it too - which also bounds the work the slur filter
# is handed by anything that reaches the bridge.
MAX_CHAT_CHARS = 200
# How many lines one channel's log keeps. The service caps its transcript at COMP_CHAT_KEEP; this
# is the drawing side's own ceiling, because the log is fed by the network.
CHAT_KEEP = 300

# WHY THE OTHER TEAM HAS NO NAMES IN THE LOBBY (Sam, 2026-09-16). Between "match found" and
# the connect window there are a couple of minutes in which a player can read the enemy
# roster, decide they do not fancy playing THAT person, and leave - and a dodge there costs
# the other nine their match for nothing. So the pre-round lobby gives the enemy five call
# signs and a blank silhouette: you can talk to them, ban against them and report them, but
# you cannot look them up or recognise them.
#
# NATO order, because it is the one list of call signs everybody already knows and it
# extends past five on its own. They are proper nouns, so they are not translated - "Bravo"
# has to mean the same person in all seven languages for all chat to work at all.
ENEMY_CALLSIGNS = ("Alpha", "Bravo", "Charlie", "Delta", "Echo",
                   "Foxtrot", "Golf", "Hotel", "India", "Juliett")

# Timeouts (Sam, 2026-09-14). Two separate promises, and they lean on each other:
#
#   "lets add a timeout feature so people arent stuck in this screen forever" - every phase a
#   player cannot leave by themselves carries a deadline, so a lost packet or a server that
#   went away ends in an explanation rather than a frozen window. This is also what makes it
#   safe to take the sign-out button away during a match (see locked_in): the only reason to
#   reach for it mid-match was to escape a screen that had stopped moving, and now nothing
#   can stop moving for longer than the table below.
#
#   "if not everyone connects to the game after everything is decided for 3 minutes, the game
#   is cancelled nobody except the person who didnt connect loses anything. the person who
#   didnt connect will lose a medium size of elo and get a 5 [minute] queue ban" - the connect
#   window. It is the only timeout here that costs anyone anything, and it costs exactly one
#   person. These three numbers are the SERVER's defaults (COMP_CONNECT_SECONDS,
#   COMP_NO_SHOW_BAN_SECONDS, COMP_NO_SHOW_ELO); the server is the authority, and what it
#   sends overrides what is written here.
from . import version as _private_version
CONNECT_SECONDS = 900 if getattr(_private_version, 'ACCOUNT_TEST', False) else 300
NO_SHOW_BAN_SECONDS = 300
NO_SHOW_ELO = 25

# The non-host JOIN window. A joiner cannot even begin until the HOST's game has reported in: the
# lobby pak gets exactly one lobby search per launch, so a joiner opened before the host's lobby
# carries CH_MATCH finds an empty Steam and has spent its only shot (see _maybe_launch_game). So a
# non-host's real clock does NOT start when the connect window opens - it starts at host-ready, and
# runs for this long. The server owns the authoritative deadline (re-based to host-ready in
# live.cjs reportConnected); this mirror lets the preview and the pre-server screen behave the same.
#
# Host-ready is also what un-greys "Launch game" (Sam, 2026-09-16). The joiner's game is no longer
# opened for them at all - `launch_game` does it when they press the button, and this is the window
# they have to press it in.
JOIN_SECONDS = 300

# ---------------------------------------------------------------------------- closing the game
# Sam, 2026-09-15: "once the game ends and match data is confirmed to be collected, it completely
# closes the game ... the game must be closed anyways to queue so its much faster to just put the
# user back at base 1 rather than them having to manually close the game." And, the same day:
# "make sure the game only closes after flashbang registers the game as complete."
#
# That second sentence is the whole contract, so it is worth spelling out. The hub (Flashbang) is
# the ONLY thing that may decide a match is finished, and it decides it in exactly one place -
# register_match_complete(). Nothing else arms the close: not the game reporting a winner, not the
# scoreboard appearing, not a timer. The game is only ever closed AFTER this hub has written the
# match down as over, which means the match data has already been through us.
#
# WHY THE HUB AND NOT THE GAMEMODE. GM_BB5 could call QuitGame at match end, and that was the
# obvious route, but it is wrong twice over. A GameMode exists on the SERVER alone, so it would
# close the host's game and leave the other nine sitting in a dead match. And the host is the one
# machine that must NOT quit early: the host is the only reporter (docs/match-result.md hop 1), so
# a gamemode that quits the moment the match ends is a gamemode that can shoot the report in the
# head. The hub runs on all ten PCs and is the end of the reporting chain, so it is the only place
# that can both cover everybody and know the data is in.
#
# THE SCOREBOARD GRACE. Nobody wants the game yanked away mid-scoreboard, and the host's own exit
# path inside the match (bb5_graphs.gm_exit, EXIT_DELAY = 15 s) is still running online calls in
# that window - closing the process on top of one is how a clean exit turns into a dirty one. So
# the close waits out both.
CLOSE_GAME_AFTER_SECONDS = 20        # end-of-match screen the player gets before Bodycam goes away
CLOSE_GAME_GRACE_SECONDS = 25        # how long the close REQUEST is given before a kill (game.py)
# A /F kill looks exactly like a crash, and a crash while hosting has been measured to lock the
# account out of hosting for 20+ minutes. The host therefore gets the request and nothing else;
# everyone else gets killed rather than left with a window they have to close before they queue.
CLOSE_GAME_FORCE_HOST = False
CLOSE_GAME_FORCE_OTHERS = True

# How long the "close your game" cue stays up when Find match is pressed with Bodycam open.
CLOSE_GAME_CUE_MS = 3000

# A flat five minutes is farmable, so the ban climbs on repeats (Sam, 2026-09-14) as multiples
# of the first rung: 5 min, 15, 30, 1 h, then 1 h from then on (capped at an hour by Sam on
# 2026-09-16, softened from four). One rung comes off per clean day. The SERVER owns all of
# this; the numbers are mirrored here only so the preview behaves the same offline and so the
# connect screen has something to warn with before the server has said otherwise.
NO_SHOW_LADDER = (1, 3, 6, 12)


def no_show_ban_seconds(count: int) -> int:
    """What a player's `count`-th no-show costs them, 1-based."""
    i = min(max(1, int(count or 1)), len(NO_SHOW_LADDER)) - 1
    return NO_SHOW_BAN_SECONDS * NO_SHOW_LADDER[i]

# How long a phase - or, inside the lobby, a single stage - may sit unchanged before the tab
# gives the player their window back. Every limit is far longer than the step it guards, so
# reaching one always means something broke, never that somebody was thinking.
#
# EVERY PHASE IN LOCKED_PHASES MUST APPEAR HERE. Hiding sign-out during a phase and then
# leaving that phase with no way out turns a safeguard into a trap, and a test enforces it.
# Deliberately absent: "queued" (a long queue is normal and Cancel is right there), "idle",
# and "result" (the player is reading it, and can leave whenever they like).
PHASE_LIMITS = {
    "signing_in": 330,                 # auth.py gives up at 300; this is the belt to its braces
    "checking": 45,                    # three integrity steps, the join POST, and its retries
    "found": ACCEPT_SECONDS + 25,      # the server expires the accept window long before this
    # The connect window can legitimately hold for the base window (the host getting its game up)
    # AND a full JOIN_SECONDS after that (the join window re-based to host-ready, server-side), so
    # this backstop has to outlast both or it would abandon a match that is still starting normally.
    "connecting": CONNECT_SECONDS + JOIN_SECONDS + 30,
    # A match is as long as it is, but it is not INFINITE: a best-of-13 Bodybomb match runs
    # well under an hour, so three hours in "live" means the gamemode never reported back and
    # the player is sitting in a hub that will not let them sign out. Release them.
    "live": 3 * 60 * 60,
    # Waiting to be let back into a lobby that is running on the other nine clients. It has
    # to outlast a WHOLE lobby, because the rejoining player may have arrived at the start of
    # one; the server's own LOBBY_SECONDS expires the match long before this does.
    ("lobby", "rejoin"): 300,
    # Each of these now has a REAL clock on the server (live.cjs PICK_SECONDS / FLIP_SECONDS /
    # BAN_SECONDS), which decides for the absent captain and moves the stage on. So a stage that
    # sits here past its limit no longer means "somebody is stalling" - the server would have
    # broken that - it means the stage never advanced at all, which is our thread lost. Every
    # limit is left well clear of the server window it shadows, so the backstop can only fire
    # after the real clock has already had its chance and failed to produce anything.
    ("lobby", "coin"): 90,
    ("lobby", "flipping"): 30,
    ("lobby", "choice"): 90,
    ("lobby", "side"): 90,
    ("lobby", "veto"): 240,
    ("lobby", "ready"): 45,
}
STALL_TICK_SECONDS = 1

# How long after a stream (re)connects the hub waits for the service to hand a match back
# before it accepts that there is no match any more. The replay is written to the stream
# immediately after the `hello` (server/live.cjs handleStream), so this only has to cover the
# few packets between them - it is generous, not a guess at a round trip.
REJOIN_GRACE_SECONDS = 8

# Phases where the match has already been formed, so signing out would abandon nine other
# people. The header hides sign-out for exactly these (Sam, 2026-09-14: "have it so the sign
# out button is removed once a match is found so nobody could potentially abuse signing out
# and cancelling the match in the pre-round selection stages").
LOCKED_PHASES = ("found", "lobby", "connecting", "live")


def format_clock(seconds) -> str:
    """A running countdown: m:ss, or h:mm:ss once it is over an hour.

    The top of the ban ladder is four hours, and "240:00" is not a clock anybody reads."""
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def outdated_line(stale) -> str:
    """The one sentence that explains a queue shut by a version, wherever it is shown.

    `stale` is an `update_needed()` dict, or the server's own 426 body - they carry the same
    keys on purpose, so a refusal that came back from the service reads exactly like the one the
    hub worked out for itself. A missing key just leaves a blank in the sentence rather than
    raising, because this runs on the screen a player is already frustrated at."""
    stale = stale or {}
    # ABSENT, not behind. Only the server sets this (live.cjs versionProblem, the missing-pack
    # branch): "update it to 1.0.7" is nonsense to somebody who has never installed it, and the
    # gate screen's sentence is the one that names the right button. `who` means the refusal is
    # about SOMEBODY ELSE in the party, and "install Bodybomb 5v5" read by a leader who already
    # has it is a sentence about the wrong person.
    if stale.get("missing_mode") and not stale.get("who"):
        return t("comp_gate_body")
    what = str(stale.get("what") or "hub")
    # SAME VERSION, DIFFERENT RULES. "Update it to 1.0.15" read by somebody who already has 1.0.15
    # is a sentence that makes the hub look broken, and it is the exact case a rules override
    # produces - the number never moves. `mode_rules` is set by update_needed when the version is
    # level and only the settings differ.
    if what == "mode" and stale.get("mode_rules"):
        return t("comp_outdated_mode_rules")
    key = {"hub": "comp_outdated_hub", "mode": "comp_outdated_mode"}.get(what, "comp_outdated_both")
    return t(key, hub=str(stale.get("hub") or stale.get("need_hub") or "?"),
             mode=str(stale.get("mode") or stale.get("need_mode") or "?"))


def party_outdated_line(stale, who: str) -> str:
    """The same refusal, about SOMEBODY ELSE. `who` is already a name, not an id.

    The service shuts the queue for the whole party when any member is behind, and it marks the
    ones that are not about you with `who` (live.cjs: `...(mine ? {} : { who })`). Read without
    that, outdated_line tells a player who is perfectly up to date to update - and the Update
    button on that screen does nothing for them, because there is nothing of theirs to update.
    So this says whose hub it is, and asks for the only thing that will actually work."""
    stale = stale or {}
    if stale.get("missing_mode"):
        # Absent, not behind: their friend has never installed the mode, so "update it to 1.0.7"
        # is the wrong instruction to pass on.
        return t("comp_outdated_party_missing", who=who)
    what = str(stale.get("what") or "hub")
    key = {"hub": "comp_outdated_party_hub",
           "mode": "comp_outdated_party_mode"}.get(what, "comp_outdated_party_both")
    return t(key, who=who,
             hub=str(stale.get("hub") or stale.get("need_hub") or "?"),
             mode=str(stale.get("mode") or stale.get("need_mode") or "?"))


def format_duration(seconds) -> str:
    """A length of time INSIDE A SENTENCE: "5 min", "4 h". Not a clock.

    "you cannot queue for 4:00:00" is a countdown pretending to be an English sentence, so
    durations that are quoted rather than counted down get units instead."""
    seconds = max(0, int(seconds or 0))
    if seconds >= 3600:
        hours = seconds / 3600.0
        n = int(hours) if abs(hours - round(hours)) < 0.05 else round(hours, 1)
        return t("comp_dur_hour", n=n)
    # Floored at one minute: a real ban is never shorter, and "0.1 min" is not a sentence.
    return t("comp_dur_min", n=max(1, int(round(seconds / 60.0))))

# Parties (Sam, 2026-09-14). A party always lands on the SAME team, which is why the cap
# is a full team of five. There is NO skill-spread cap: the matchmaker balances on the
# AVERAGE Elo of each team instead ("take the average elo of the team and try to match it
# with a team of a similar average").
MAX_PARTY = TEAM_SIZE
# I, O, 0 and 1 are left out so a code read aloud or off a screen is never ambiguous.
PARTY_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PARTY_CODE_LEN = 6


def make_party_code() -> str:
    """Six characters, shown as ABCD-EF."""
    raw = "".join(random.choice(PARTY_CODE_ALPHABET) for _ in range(PARTY_CODE_LEN))
    return raw[:4] + "-" + raw[4:]


def mask_party_code(code: str) -> str:
    """The code with every character blanked, for streamers: ABCD-EF -> \u2022\u2022\u2022\u2022-\u2022\u2022.

    Sam, 2026-09-14: hide the code from a stream and still be able to copy it."""
    return "".join("\u2022" if c != "-" else "-" for c in str(code or ""))


def normalise_party_code(text: str) -> str:
    """Accept what a person actually types: spaces, dashes, lower case, quotes."""
    cleaned = "".join(c for c in str(text or "").upper() if c in PARTY_CODE_ALPHABET)
    if len(cleaned) != PARTY_CODE_LEN:
        return ""
    return cleaned[:4] + "-" + cleaned[4:]


# ====================================================================== bug reports
# Sam, 2026-09-16: "only allow the user to send 1 bug every 5 seconds".
#
# THE GUARD IS HERE, ON THE SESSION, not on the screen that draws the button. There are two UIs
# over this one session and the web one is a page that can be reloaded, so a cooldown kept in JS
# is a cooldown that a refresh clears. The server enforces the same five seconds again
# (live.cjs BUG_COOLDOWN_MS) because the hub is not the only thing that can reach the endpoint;
# this one exists so the player is told to wait instead of being refused after the fact.
BUG_COOLDOWN_SECONDS = 5.0
# Same ceiling the server truncates at, so the box can count down to it honestly rather than
# letting someone type 4000 characters and quietly keeping half.
BUG_TEXT_MAX = 2000


# ====================================================================== the profile
# How many of the most recent matches the form strip shows. The server keeps fifty
# (COMP_HISTORY_KEEP), which is the whole sample every number on the profile is drawn from;
# the strip is only the tail of it, short enough to read at a glance.
FORM_SHOWN = 10

# The outcomes the form strip can draw, worst last. Kept as plain strings rather than colours
# so profile_stats stays free of the theme and can be tested without a window.
FORM_WIN, FORM_LOSS, FORM_PLAYED, FORM_CANCELLED, FORM_FAULT = (
    "win", "loss", "played", "cancelled", "fault")


def _int_or(value, fallback=0):
    """A number off the wire. History rows are data, not promises: a string, a null or a
    dict in a numeric field must cost one statistic, never the whole profile."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def kd_ratio(kills, deaths):
    """K/D as every scoreboard in this app shows it, or None when either half is unknown.

    Shared rather than copied: the match-history detail, the post-match card and the Tk fallback
    all show this number, and three implementations of one ratio is three chances to disagree.

    Deaths of 0 is not a division by zero, it is a perfect record: the ratio is the kill count
    itself. Kills CAN be negative - the game's counter is a net score and a team kill takes one
    off - and a negative ratio is the honest reading of that rather than something to clamp.
    """
    if kills is None or deaths is None:
        return None
    if deaths <= 0:
        return float(kills)
    return round(kills / deaths, 2)


def profile_stats(rows):
    """What a player's own match history adds up to.

    PURE on purpose: no session, no Tk, no network, so the arithmetic behind every number on
    the profile can be tested on Sam's PC where there is no xvfb. `rows` are history rows
    exactly as the server sends them (server/live.cjs historyRow), newest first, already
    written from this player's point of view - `won`, `host` and `blamed` are theirs.

    Nothing here invents a number. `won` is null on every row until the gamemode reports a
    scoreboard, so `wins`/`losses` stay 0 and `win_rate` stays None, and the profile says
    that rather than printing a 0% that would read as a losing record.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    stats = {
        "recorded": len(rows), "played": 0, "cancelled": 0,
        "wins": 0, "losses": 0, "undecided": 0, "win_rate": None,
        "at_fault": 0, "no_show": 0, "declined": 0, "abandoned": 0,
        "hosted": 0, "attack": 0, "defend": 0,
        "maps": [], "top_map": "", "elo_lost": 0,
        "form": [], "last_played": 0,
    }
    maps = {}
    for row in rows:
        if row.get("voided") or row.get("outcome") == "voided":
            continue  # Recorded in history, without changing the played/rated record.
        cancelled = (row.get("outcome") or "") == "cancelled"
        blamed = bool(row.get("blamed"))
        won = row.get("won")
        if cancelled:
            stats["cancelled"] += 1
            if blamed:
                stats["at_fault"] += 1
                reason = str(row.get("reason") or "")
                if reason in ("no_show", "declined", "abandoned"):
                    stats[reason] += 1
        else:
            stats["played"] += 1
            if won is True:
                stats["wins"] += 1
            elif won is False:
                stats["losses"] += 1
            else:
                stats["undecided"] += 1
            # Only a match that was actually played says anything about the player's maps or
            # sides. A lobby that died in the veto has a map nobody ever loaded.
            name = str(row.get("map") or "")
            if name:
                maps[name] = maps.get(name, 0) + 1
            side = row.get("side") or ""
            if side == "attack":
                stats["attack"] += 1
            elif side == "defend":
                stats["defend"] += 1
            if row.get("host"):
                stats["hosted"] += 1
            ended = _int_or(row.get("ended"))
            if ended > stats["last_played"]:
                stats["last_played"] = ended
        # Elo is written as a negative number on the rows that cost the player something.
        elo = _int_or(row.get("elo"))
        if elo < 0:
            stats["elo_lost"] += -elo
        if len(stats["form"]) < FORM_SHOWN:
            stats["form"].append(
                (FORM_FAULT if blamed else FORM_CANCELLED) if cancelled
                else FORM_WIN if won is True
                else FORM_LOSS if won is False
                else FORM_PLAYED)
    decided = stats["wins"] + stats["losses"]
    if decided:
        stats["win_rate"] = int(round(stats["wins"] * 100.0 / decided))
    # Most played first, then alphabetically, so the same history always draws the same order.
    stats["maps"] = sorted(maps.items(), key=lambda kv: (-kv[1], kv[0]))
    stats["top_map"] = stats["maps"][0][0] if stats["maps"] else ""
    return stats


# ====================================================================== the panel protocol
# The session (Session/MockSession/LiveSession) is pure logic: it never imports tkinter and
# never assumes a view. It reaches its view through exactly FIVE methods, and any object that
# provides them can drive the whole flow. This is the seam the UI-redesign plan calls out
# (docs/ui-redesign-plan.md, "Logic <-> view separation"): CompetitivePanel (Tk) implements it,
# and so does hub/webui/panel.py:WebPanel (pywebview). Keep this list of five the ONLY coupling.
#
#   post(fn)            run fn on the UI thread (workers/SSE hop here before touching the view)
#   after(ms, fn)       schedule fn on the UI thread after ms milliseconds (the session's timers)
#   on_change()         the session state moved; (re)draw / (re)emit it
#   save_auth(payload)  persist (dict) or clear (None) the signed-in account in state.json
#   map_pool()          the ranked map pool (list[str]) for the veto
#
# post()/after() MUST marshal to the one UI thread; nothing else in the session touches a view.


# ====================================================================== the session
class Session:
    """What the panel needs from whatever drives it.

    A real implementation talks to the backend; MockSession fakes it locally. The panel
    only ever reads the attributes below and calls the methods below (see "the panel
    protocol" above for the five methods a panel must provide).

    phase: "signed_out" | "signing_in" | "idle" | "checking" | "queued" | "found"
           | "lobby" | "connecting" | "live" | "result"
    """
    phase = "signed_out"
    me = None                # {"name", "steam_id", "level", "elo", "bdr", "matches", "wins"}
    online = 0
    players_registered = None  # Unknown until a complete server count arrives.
    live_matches = 0         # matches in flight service-wide, off the same `stats` broadcast as
                             # `online` and `queue_size`. The top bar shows all three together.
    error = ""               # a line to show in red, or ""
    party = None             # None when solo, else {"code", "leader_id", "members": [player]}
    party_error = ""         # a line for the party card, or ""
    token = ""               # our own session token from server/auth.cjs (never a Steam one)
    link_url = ""            # during sign-in: the page the browser was sent to
    link_code = ""           # during sign-in: the short code that page is keyed by
    queue_position = 0       # while queued: our place, and how many are waiting
    queue_size = 0
    connected = True         # False while the live service is unreachable
    connect_left = 0         # while connecting: seconds left to get into the game
    connect_total = 0        # while connecting: how many players have to get in
    connected_ids = ()       # while connecting: who has reported in already
    i_connected = False      # while connecting: have WE reported in
    ladder = None            # the rank bands, straight off the server's hello (never guessed)
    penalties = None         # the penalty rules, likewise: the ban ladder in seconds, the RR an
                             # offence costs, the two windows and the team-kill dials. None until
                             # the stream says hello, and the screen draws nothing rather than
                             # quoting numbers it made up.
    stage_seconds = 0        # inside the lobby: seconds left on THIS stage (coin/choice/side/ban)
    stage_total_seconds = 0  # the full window, so the UI can draw a proportion
    _stage_ticking = False
    _clock_key = None        # (stage, ban_turn) the running countdown belongs to
    teams_status = ""        # while connecting: "" | "wait" | "mismatch". The server holds the
                             # start until the GAME has put everyone on the team the lobby chose
                             # (live.cjs goLiveIfReady); this is what that hold looks like here.
    host_ready = False       # while connecting: has the HOST's game reported in (host_id is in the
                             # match_connect roster)? A non-host's join window starts HERE, not when
                             # the connect window merely opened - there is nothing to join until then
    join_left = 0            # while connecting, non-host, after host_ready: seconds left in the
                             # JOIN_SECONDS window, Python-ticked exactly like connect_left
    launched = False         # fallback for a service that sends no match id; see reset_match
    launched_for = ""        # the match id Bodycam was opened for. NOT cleared by reset_match.
    match_id = ""            # the match the server currently has us in ("" when none)
    game_was_open = False    # the game was ALREADY running, so its one BeginPlay is spent
    game_opened_by_us = ""   # "host" (auto, at the connect window) or "player" (the joiner
                             # pressed Launch) - which rule opened it, for the screen
    host_pak_done = False
    pak_done = False        # the joiner's half of host_pak_done; see _prepare_joiner_pak    # while connecting: have we already swapped the host's lobby pak
    host_level = ""          # the cooked level we pointed it at; "" means the host must host by hand
    result_payload = None    # the whole match_result event (docs/match-result.md hop 2), or None
    # THE POST-MATCH CARD. A frozen copy of the match that just ended, or None when none is on
    # screen. It is NOT part of the match: reset_match() and leave_result() both wipe the teams,
    # the sides and the veto it was built from, and the card has to outlive them - the one thing
    # asked of it is that nothing but the player dismisses it. `close_postmatch` is the only
    # thing that clears it.
    postmatch = None
    match_complete = ""      # why this hub considers the match finished: "" until it does, then the
                             # source that told it ("match_result", "match_over", "cancelled", ...).
                             # THE GATE: Bodycam is never closed while this is "".
    game_close = ""          # the close's own state: "" | "armed" | "closing" | "closed" | "forced"
                             # | "failed" | "skipped" (nothing of ours was open to close)
    i_accepted = False       # while found: have WE accepted
    rejoined = False         # a one-shot flag: the service just handed a match back to us
    penalty_until = 0.0      # a queue ban as a wall-clock deadline; 0.0 when there is none
    penalty_reason = ""      # why, so the idle screen can say it
    penalty_count = 0        # how many no-shows are still on the record, decay applied
    penalty_next = NO_SHOW_BAN_SECONDS   # what the NEXT no-show would cost, in seconds
    # Match History (Sam, 2026-09-14). None means "never asked"; a list means the server has
    # answered, and an EMPTY list is a real answer - a player with no matches yet.
    history = None
    history_error = ""       # a line for the history list, or ""
    history_loading = False
    # FRIENDS. The server owns the list; this is the copy the screen draws, refetched whenever
    # the server says it changed (the `friend_update` nudge) rather than polled.
    friends = ()             # [{steam_id, persona, online}]
    friend_requests_in = ()
    friend_requests_out = ()
    friend_code = ""
    friend_code_hidden = True   # hidden by default: it is on screen while streaming, by definition
    friends_error = ""
    friends_loading = False
    friends_seq = 0          # bumped on every answer, so a redraw can tell one fetch from the next

    # PARTY INVITES. Incoming only: what I have been offered, as the server last pushed it
    # (`party_invites`). There is no outgoing list - an invite I sent is the other person's to
    # answer, and the only thing that would come back is a roster I can already see.
    party_invites = ()       # [{from: {steam_id, persona}, code, size, max, expires_in}]
    invite_error = ""        # a line for the invite/party card, or ""
    invite_sent = ()         # steam ids I have invited this session, so the row can say so

    # LEADERBOARD. The server builds it from a sorted-set index that fills itself as players get
    # ranked; this is the copy the screen draws.
    board_rows = ()
    board_you = None
    board_available = False
    board_loading = False
    board_error = ""
    board_seq = 0
    tournament_data = None
    tournament_loading = False
    tournament_error = ""
    tournament_received = 0.0
    tournament_ticket_seq = 0
    messages_data = None
    messages_loading = False
    messages_target = ""
    messages_thread = None
    messages_thread_loading = False
    messages_error = ""
    messages_sending = False
    messages_send_seq = 0
    messages_sent = None
    messages_pending = None
    messages_generation = 0

    # ONE MATCH, in full. `match_detail_id` is the row that is open ("" when none is); the record
    # itself is whatever the server last returned for it.
    match_detail_id = ""
    match_detail = None
    match_detail_loading = False
    match_detail_error = ""

    # REPORTS. `report_target` is the player whose reason-picker is open, "" when none is. The
    # reporter is told only that it was recorded (live.cjs reportPlayer): a count handed back is
    # a tool for deciding whether a pile-on is working.
    report_target = ""
    report_match = ""
    # The target's NAME as the screen that opened the picker had it. The picker used to look the
    # name up in the live roster, which is empty (or about somebody else entirely) when the report
    # was opened from match history - so it fell through to printing the raw steam id.
    report_name = ""
    report_sent = False
    report_error = ""

    # BUG REPORTS. Free text about the app, not about a person, so none of the above applies:
    # there is no target and no reason list. `bug_last_sent` is a MONOTONIC stamp (time.monotonic,
    # not time.time) because the only question asked of it is "how long since", and a wall clock
    # that steps backwards over a DST change or an NTP correction would answer that with a
    # cooldown lasting an hour.
    bug_text = ""            # what is in the box, so a re-render cannot lose it
    bug_sending = False
    bug_sent = False
    bug_error = ""
    bug_last_sent = 0.0
    # Bumped on every report that is actually FILED. The web UI puts it in the text box's id so
    # that a filed report leaves the box: the core restores an input's pre-render value by id
    # (core.js restoreInputs) to protect anything half-typed, which would otherwise put the sent
    # text straight back and let the same report be filed twice.
    bug_seq = 0

    def bug_cooldown_left(self) -> float:
        """Seconds still to wait before another bug report may be sent; 0.0 when it may."""
        if not self.bug_last_sent:
            return 0.0
        left = BUG_COOLDOWN_SECONDS - (time.monotonic() - self.bug_last_sent)
        return left if left > 0 else 0.0

    history_stale = False    # a match just ended: the next look should re-ask the server
    history_seq = 0          # bumped on every answer, so a redraw can tell one fetch from the
                             # next even when the rows are the same length and the same match

    # ---------------------------------------------------------------- the install gate
    def gamemode_installed(self) -> bool:
        """Is the gamemode ranked runs on actually in the pak?

        THIS IS A SESSION METHOD, not just a tab one. CompetitiveTab has its own copy and draws a
        gate screen instead of the whole tab when it says no (`_draw_gate`) - but the web UI is a
        different window over the SAME session, and it had no such screen, so a player whose very
        first act was opening Competitive could press Find match with nothing installed. Three
        separate places had written down "the tab cannot reach the queue without the gamemode"
        (here, hub/live.py `_stamp`, server/live.cjs `versionProblem`) and all three meant the Tk
        tab. `find_match` asks this now, so both windows are covered by one test.
        """
        app = getattr(self.panel, "app", None)
        installed = ((getattr(app, "state", None) or {}).get("installed") or {})
        return COMPETITIVE_MODE_ID in installed

    # ---------------------------------------------------------------- the version gate
    def update_needed(self):
        """What has to be updated before this hub may QUEUE, or None when nothing does.

        Ranked needs all ten players on the same build, so the service refuses a queue join from
        anything behind the catalogue it is publishing (server/live.cjs, the version gate). This
        is the same test, run here, for one reason: a disabled button that says why beats a
        button that works, spins, and comes back with a refusal.

        THE SERVER IS STILL THE AUTHORITY. This never lets anybody INTO the queue - `find_match`
        checks it and the server checks it again - and a hub whose catalogue has not loaded (or
        that is offline on a cached one) simply does not gate here and lets the service answer.

        NOTHING ELSE IS GATED. Being out of date does not touch a match already under way:
        accept, the coin flip, the veto, the connect window and the result all carry on, here and
        on the server, because the ten people in that match did nothing wrong when a release
        landed underneath them.

        Returns None, or:
            {"what": "hub" | "mode" | "both",
             "hub": <catalogue hub version>, "have_hub": HUB_VERSION,
             "mode": <catalogue gamemode version>, "have_mode": <installed version>}
        """
        app = getattr(self.panel, "app", None)
        catalogue = getattr(app, "catalogue", None) or {}
        if not catalogue:
            return None                      # nothing to compare against: let the server answer
        info = catalogue.get("hub") or {}
        want_hub = str(info.get("version") or "")
        entry = catalogue_mod.entry_by_id(catalogue, COMPETITIVE_MODE_ID) or {}
        want_mode = str(entry.get("version") or "")
        installed = ((getattr(app, "state", None) or {}).get("installed") or {})
        have_mode = str((installed.get(COMPETITIVE_MODE_ID) or {}).get("version") or "")

        hub_stale = bool(want_hub) and catalogue_mod.version_newer(want_hub, HUB_VERSION)
        # An UNINSTALLED gamemode is not "out of date" here: `gamemode_installed` is the test for
        # that, and calling it an update would send the player to the wrong button.
        installed_info = installed.get(COMPETITIVE_MODE_ID) or {}
        newer = bool(want_mode and have_mode) and catalogue_mod.version_newer(want_mode, have_mode)

        # THE SAME VERSION BUILT WITH DIFFERENT RULES IS A DIFFERENT PAK, and until 2026-09-17 this
        # gate could not see that. Only the version number was compared, so a rules change - which
        # is how the score limit, the round count and the team sizes actually reach the game - left
        # the queue wide open on a pak that plays something else entirely.
        #
        # It cost Sam four test matches in one evening. The catalogue said max_players 10 and the
        # pak on disk had been built with 2, which is the value that empties the game's team array:
        # no team score, so the match never ended, in the game OR in Lights Out. Nothing warned him,
        # because 1.0.15 was installed and 1.0.15 was on offer. Sam, 2026-09-17: "whenever there is
        # a gamemode change in anyway ... its guaranteed to require to update".
        #
        # `mode_update_available` is the one test both halves of the app already use for the Update
        # button, so the button and the gate can no longer disagree about what "up to date" means.
        rules_stale = bool(have_mode) and catalogue_mod.mode_update_available(entry, installed_info)
        mode_stale = newer or rules_stale
        if not hub_stale and not mode_stale:
            return None
        return {
            "what": "both" if (hub_stale and mode_stale) else ("hub" if hub_stale else "mode"),
            "hub": want_hub, "have_hub": HUB_VERSION,
            "mode": want_mode, "have_mode": have_mode,
            # Same number on both sides means the RULES moved, not the version - so the screen can
            # say "settings changed" instead of offering an update from 1.0.15 to 1.0.15.
            "mode_rules": bool(rules_stale and not newer),
        }


class MockSession(Session):
    """A local stand-in so the whole flow can be clicked through with no backend.

    Everything here is fake and marked as such in the window. It exists to settle the
    SHAPE of the tab with Sam; step 3 throws it away.
    """
    mock = True

    def __init__(self, panel):
        self.panel = panel
        self._account_epoch = 0
        self._signin_epoch = 0
        self._queue_epoch = 0
        self._invite_generation = {}
        self._party_transition = False
        self._lobby_pak_dir = ""
        self._pending_lobby_cleanup = set()
        self.phase = "signed_out"
        self.me = None
        self.online = 0
        self.error = ""
        self.token = ""              # our own session token, not a Steam credential
        self.link_url = ""           # the page the browser was sent to
        self.link_code = ""
        self._cancel_signin = False
        # the party outlives a match, so it is NOT touched by reset_match()
        self.party = None
        self.party_error = ""
        self.penalty_until = 0.0
        self.penalty_reason = ""
        self.penalty_count = 0
        self.penalty_next = NO_SHOW_BAN_SECONDS
        self._penalty_gen = 0           # bumped each time the ban-expiry one-shot is (re)armed
        # The post-match card outlives the match it is about - see Session.postmatch - so it is
        # set up here, beside history and the party, and never in reset_match().
        self.postmatch = None
        # Warning receipts are two-step: SSE queues visible text; only the browser's render callback
        # may move one into the service's acknowledged set. These sets prevent replayed SSE/verbs
        # from stacking warnings or posting the same receipt twice.
        self._combat_warning_pending = set()
        self._combat_warning_presented = set()
        self._combat_warning_acking = set()
        # history outlives a match too: it is the record OF matches, not part of one
        self.history = None
        self.history_error = ""
        self.history_loading = False
        self.history_stale = False
        self.history_seq = 0
        # The close-the-game state belongs to the LAUNCH, not to the match, so reset_match must
        # not touch it: a cancelled match resets itself and the game it opened is still running.
        self._game_ours = False       # we opened (or adopted) a Bodycam we owe a close
        self._close_gen = 0           # bumped per launch; a close armed for one game cannot
                                      # reach the next one
        self.reset_match()

    # ---------------------------------------------------------------- helpers
    def reset_match(self):
        # OUR LOBBY PAK GOES WITH THE MATCH IT WAS CUT FOR. It carries this match's map as a literal
        # and the graph opens it unconditionally at BeginPlay, so one left behind would send the
        # host's next cold launch into a match that is over. Done here rather than in _on_cancelled
        # because every way a match ends comes through this function, and a no-op when there is
        # nothing installed costs one stat call.
        if self.host_pak_done or self.pak_done:
            self._release_host_pak()
        self.players = []
        self.teams = {1: [], 2: []}
        self.captains = {1: None, 2: None}
        self.accepted = 0
        self.accept_total = LOBBY_SIZE   # the server's match size; 10 unless it says otherwise
        self.queue_seconds = 0
        self.accept_left = ACCEPT_SECONDS
        self.stage = ""            # inside "lobby": "coin"|"flipping"|"choice"|"side"|"veto"|"ready"
        self.coin_side = None      # the side the DESIGNATED captain called (heads/tails)
        self.coin_result = None    # "heads" | "tails"
        self.toss_winner = None    # team number
        self.coin_captain = None   # steam_id of the ONE captain who flips (server-set for live)
        self.advantage = None      # what the toss winner chose: "side" | "ban"
        self.side_picker = None    # the team that picks attack/defend (gets the side selector)
        self.ban_advantage = None  # the team that bans LAST (controls the final map)
        self.first_ban = None      # team number that bans first
        self.bans = []             # [(team, map_name)]
        self.ban_turn = None
        self.map = None
        self.lobby_pool = None     # the veto pool the SERVER decided (live); None -> panel pool
        self.sides = {1: "attack", 2: "defend"}
        self.host = None
        # TWO LOGS, ONE LOBBY: what your four team-mates read, and what all ten do. A line is
        # {"steam_id", "name", "text"} rather than (name, text) because ALL chat has to be
        # able to re-label a message the moment its sender is an anonymised enemy, and that
        # needs the id - the name typed under it is the one thing it must not show. A welcome
        # line has neither.
        self.chat = {ch: [] for ch in CHAT_CHANNELS}
        # The pregame snapshot has no server match id yet. Give its browser drafts a local
        # generation so reset_match also clears them, even if the lobby is off-screen.
        self.chat_epoch = getattr(self, "chat_epoch", 0) + 1
        self.chat_channel = "team"
        self.muted_chat_players = set()
        self._chat_order = 0
        self.vote = None           # {"caller": name, "yes": n, "no": n, "voted": bool}
        self.result = None         # {"won": bool, "score": (a, b), "delta": int, "voided": bool}
        self.result_payload = None # the whole match_result event, kept for the scoreboard screen
        self.check_step = 0
        self.rejoined = False
        # Whatever the last screen complained about belongs to the match that just ended. Every
        # caller that has something new to say sets it straight after this; the ones that do not
        # would otherwise carry a stale red line onto the next screen.
        self.error = ""
        self.connect_left = 0
        self.connect_total = 0
        self.connected_ids = set()
        self.i_connected = False
        self.teams_status = ""
        self.reconnect_waiting = []
        self.stage_seconds = 0
        self.stage_total_seconds = 0
        self._stage_ticking = False
        self._clock_key = None
        self.coin_auto = False     # did the CLOCK make each choice, rather than a captain
        self.advantage_auto = False
        self.side_auto = False
        self.host_ready = False
        self.join_left = 0
        self.i_accepted = False
        self.launched = False      # fallback only, for a service that sends no match id
        # DELIBERATELY NOT RESET: `launched_for` is the match we have already opened Bodycam for,
        # and it has to outlive this reset. reset_match() runs when our STREAM loses the thread
        # (_rejoin_gave_up), which says nothing about whether the match still exists on the server
        # - it usually does. Clearing the flag there is what made a reconnect reopen the game:
        # the service replays match_connecting to a hub that has just forgotten it launched, and
        # the guard reads a live match as a new one. Sam closed Bodycam and it opened again
        # immediately, repeatedly (2026-09-16); his stream was measured flapping on1/on0/on1.
        # A match id is unique, so keeping it costs nothing - the next match has a different one.
        self.game_was_open = False
        self.game_opened_by_us = ""
        self.host_pak_done = False
        # ONE FLAG PER ROLE, and the joiner's is separate because a player can be host in one match
        # and joiner in the next. Both are cleared per match, unlike `launched_for` - the pak has to
        # be re-stamped for every match because the TOKEN changes, where the launch must not repeat.
        self.pak_done = False
        self.host_level = ""
        self.report_token = ""       # private per-match capability; never placed in UI snapshots
        self.migration_token = ""
        self.host_epoch = 0
        self.match_complete = ""
        self.game_close = ""
        self._close_armed = False      # one close per match, whatever order the events arrive in
        self._cleanup_job = None
        self._game_launch_at = None

    def _later(self, ms, fn):
        self.panel.after(ms, fn)

    # ---------------------------------------------------------------- the lobby stage clock
    def _open_stage(self, stage):
        """Enter a lobby stage and put it on its clock. OFFLINE ONLY.

        A live lobby takes every one of these off the server (_apply_lobby), because the server is
        what actually decides when a turn is up. The preview has no server, so it keeps its own
        numbers - and it has to keep them, or the demo would be the one place in the hub where a
        stage sits with no countdown on it and looks like the bug this was built to fix."""
        self.stage = stage
        seconds = {"flipping": FLIP_SECONDS, "veto": BAN_SECONDS}.get(stage, PICK_SECONDS)
        self.stage_seconds = self.stage_total_seconds = seconds if stage in TIMED_STAGES else 0
        if self.stage_seconds:
            self._start_stage_tick()

    def _start_stage_tick(self):
        """Arm the lobby-stage countdown, at most once (mirrors _start_accept_tick)."""
        if self._stage_ticking:
            return
        self._stage_ticking = True
        self._later(1000, self._tick_stage)

    def _tick_stage(self):
        """Draw the stage clock down. It NEVER acts: when it reaches zero the server has already
        decided for the absent captain and the next lobby payload carries the result. A client that
        decided the turn was up would disagree with the server the moment either one lagged."""
        if self.phase != "lobby" or self.stage not in TIMED_STAGES:
            self._stage_ticking = False
            self.stage_seconds = 0
            return
        self.stage_seconds = max(0, int(getattr(self, "stage_seconds", 0) or 0) - 1)
        self._changed()
        if self.stage_seconds:
            self._later(1000, self._tick_stage)
        else:
            self._stage_ticking = False

    # ---------------------------------------------------------------- opening the game
    # WHY THE ORDER MATTERS, AND WHY IT IS NOT A PREFERENCE (docs/autojoin.md, step 4 result).
    # The pak gets ONE lobby search per launch: BeginPlay fires once per level load, fires the
    # search ~4 s later, and there is no clock in the lobby world to try again with. So a joiner
    # opened before the host has stamped CH_MATCH onto its lobby searches an empty Steam and has
    # spent its only shot - silently, with nothing to retry.
    #
    # Hence: the HOST opens as soon as the connect window does, and everyone else waits for the
    # host to report in. The release signal costs no new plumbing - `match_connect` already carries
    # the full `connected` roster to every client, so the host appearing in it IS the go-ahead.
    def _i_am_host(self) -> bool:
        mine = (self.me or {}).get("steam_id")
        return bool(mine) and (self.host or {}).get("steam_id") == mine

    def _game_dir(self):
        """The Bodycam install, as the app resolved it at startup (app.py:312-317). "" when the
        player has no game found - which is a normal state, not an error, and every caller here
        treats it as "cannot auto-host" rather than raising."""
        app = getattr(self.panel, "app", None)
        return getattr(app, "game_dir", "") or ""

    def _match_token(self):
        """The string that pairs this match's host with its joiners, or "" when we have no match.

        The HOST stamps it into BodycamGI "Session Name", which the game's own MakeCreateLobbyParams
        copies into the lobby's searchable `Name` attribute. A JOINER stamps the same string into
        "SessionToJoin (Client)", which makes GM_Host call JoiningByName(token) instead of hosting.
        One string, two paks, and the game does the search and the travel itself.

        It is derived rather than sent: match_id is already on both events where a role picks its
        pak, so there is no new field and no way for the two halves to disagree. Lowercase hex with
        one dash, so it can never trip UE's trailing _<digits> FName-number split."""
        mid = str(getattr(self, "match_id", "") or "")
        return ("chm-" + mid) if mid else ""

    def _prepare_joiner_pak(self):
        """Put the JOINER pak into ~mods, stamped with this match's token, before Steam launches.

        Same ordering rule as the host's, and for the same reason: paks mount at process start, so
        an install that lands after the launch does nothing at all. It is called from the release
        branch, immediately before _maybe_launch_game, because that guard is spent on its first
        call and anything after it would be too late.

        A failure here is not fatal to the match. The joiner simply boots normally and can still be
        told to join by hand - which is exactly what the pak degrading to "no token" does anyway."""
        if self.pak_done:
            return True
        game = self._game_dir()
        token = self._match_token()
        if not game or not self.map or not token:
            return False
        try:
            ready = lobbypak_mod.prepare(game, "BB5", self.map, log=None,
                                 host_id=player_identity.native_id(self.host),
                                 token=token, role="join", report_token=self.migration_token)
            self.pak_done = bool(ready)
            if ready:
                self._lobby_pak_dir = game
                self._pending_lobby_cleanup.discard(game)
            return self.pak_done
        except Exception:
            self._log_exception("prepare_joiner_pak")
            return False

    def _prepare_host_pak(self):
        """Put the lobby pak that opens THIS match's map into ~mods, before Steam is asked to launch.

        Ordering is the whole point: the pak is read at the game's startup, so it has to be right
        before the launch, not after. It runs on the Tk thread (server events are marshalled there),
        which costs the connect screen about half a second - acceptable against CONNECT_SECONDS, and
        the alternative is a host who launches into the wrong map.

        Guarded once per match because _on_connecting is re-entered verbatim on every replayed
        match_connecting event, and re-installing under a game that is already up would either fail
        on the lock or be ignored.

        `self.host_level` is "" when we could not prepare one - the gamemode pak is missing, the map
        is not among the levels this player has cooked, or the game is already running. The screen
        uses it to tell the host to open the map themselves; the manual "I'm in" button still
        releases the joiners, so the match is not lost. What it must never do is launch anyway."""
        if self.host_pak_done:
            return True
        game = self._game_dir()
        if not game or not self.map:
            self.host_level = ""
            return False
        try:
            # The host's own id goes in too: EVERY player's pak carries it, because the joiner
            # half of the graph needs to know which lobby among two dozen strangers' is this match.
            # The token goes in here too, and it is the half that makes the joiner possible: it is
            # stamped into BodycamGI "Session Name", which the game's own MakeCreateLobbyParams
            # copies into the lobby's searchable `Name` attribute. Without it the host advertises
            # nothing a joiner could ask for by name, and autojoin degrades to manual silently.
            self.host_level = lobbypak_mod.prepare(
                game, HOST_GAMEMODE_ID, self.map,
                host_id=player_identity.native_id(self.host),
                token=self._match_token(), report_token=self.report_token)
            self.host_pak_done = bool(self.host_level)
            if self.host_pak_done:
                self._lobby_pak_dir = game
                self._pending_lobby_cleanup.discard(game)
        except Exception:
            self._log_exception("prepare_host_pak")
            try:
                lobbypak_mod.remove(game)
            except Exception:
                pass
            self.host_level = ""
        return self.host_pak_done

    def _release_host_pak(self):
        """Take our lobby pak back out. A stale one sends the next cold launch to THIS match's map.

        Called when the match ends or dies. Not called when it goes live: the game is running then,
        and the pak has already been read."""
        game = self._lobby_pak_dir or self._game_dir()
        if not game:
            return
        try:
            if not lobbypak_mod.remove(game):
                self._pending_lobby_cleanup.add(game)
            else:
                self._pending_lobby_cleanup.discard(game)
        except Exception:
            self._pending_lobby_cleanup.add(game)
            self._log_exception("release_host_pak")

    def _prepare_launch(self):
        ready = self._prepare_host_pak() if self._i_am_host() else self._prepare_joiner_pak()
        if not ready and getattr(self, "live", False):
            self.error = t("comp_launch_prepare_failed")
            self._cue(self.error)
            self._changed()
            return False
        return True

    def _maybe_launch_game(self, why):
        """Open Bodycam once per match, if it is not already open.

        An ALREADY-RUNNING game is not a success here and must not be treated as one: that player
        spent their single BeginPlay before the match existed, there is no channel into a live
        Bodycam process to tell it otherwise, and launch_game() would be a no-op. They are marked
        so the screen can say so rather than leaving them to wonder why nothing happened."""
        if self.phase != "connecting":
            return
        # ONCE PER MATCH, KEYED ON THE MATCH - not once per session object. The service replays
        # match_connecting to any hub that reconnects mid-window (that is what it is for), so the
        # only thing that can safely mean "we already did this" is the id of the match itself.
        mid = str(getattr(self, "match_id", "") or "")
        if mid and self.launched_for == mid:
            return
        if not mid and self.launched:
            return                       # no id to key on: behave as before, once per reset
        self.launched = True
        self.launched_for = mid
        try:
            if game_mod.game_running():
                self.game_was_open = True          # the screen tells them to restart
                self._take_game_ownership()
                return
            self._game_launch_at = time.time()
            game_mod.launch_game()
            self.game_opened_by_us = why
            self._take_game_ownership()
        except Exception:
            self._log_exception("launch_game")

    def _take_game_ownership(self):
        """This hub is now responsible for closing the Bodycam this player is about to play in.

        Both branches of _maybe_launch_game count. A game that was ALREADY open is taken on too,
        and deliberately: that player's one BeginPlay is spent, so they are the person who most
        needs it closed at the end - they cannot queue again until it is.

        Bumping the generation here is what makes a stale close harmless. Any close armed for an
        earlier match is now looking at a game that no longer exists, and will decline to run."""
        self._game_ours = True
        self._close_gen += 1
        self._register_cleanup(self._close_gen, match_id=self.match_id)

    def _register_cleanup(self, generation, attempt=0, match_id=None):
        match_id = self.match_id if match_id is None else match_id
        if (generation != self._close_gen or not self._game_ours or self.match_complete
                or match_id != self.match_id):
            return
        try:
            self._cleanup_job = match_cleanup.register(
                self.match_id, str((self.me or {}).get("steam_id") or ""), self.token,
                self._game_dir(), launched_at=getattr(self, "_game_launch_at", None))
            if self._cleanup_job:
                self._watch_cleanup(self._cleanup_job, self._close_gen)
            elif attempt < 20:
                self._later(3000, lambda: self._register_cleanup(generation, attempt + 1, match_id))
        except Exception:
            self._log_exception("register_match_cleanup")
            if attempt < 20:
                self._later(3000, lambda: self._register_cleanup(generation, attempt + 1, match_id))

    def _watch_cleanup(self, job, generation):
        if generation != self._close_gen or job != self._cleanup_job:
            return
        status = match_cleanup.read_status(job)
        if status.get("state") not in match_cleanup.TERMINAL and time.time() - status.get("updated_at", 0) > 30:
            try:
                match_cleanup.ensure_worker(job)
            except OSError:
                self._log_exception("restart_match_cleanup")
        result = status.get("result")
        if (result and result.get("match_id") == self.match_id
                and not self.match_complete):
            self._on_result(result)
        # A result can attach a replacement game; this callback then belongs to the old one.
        if generation != self._close_gen or job != self._cleanup_job:
            return
        if status.get("state") in match_cleanup.TERMINAL:
            self._game_ours = False
            outcome = "closed" if status["state"] == "done" else "failed"
            if self.game_close != outcome:
                self.game_close = outcome
                self._log_close(outcome, self._i_am_host(), "cleanup_worker")
                self._changed()
            if self.phase in ("connecting", "live") and not self.match_complete:
                if not self._refresh_cleanup():
                    self._later(3000, lambda: self._watch_cleanup(job, generation))
            return
        self._later(1500, lambda: self._watch_cleanup(job, generation))

    def _refresh_cleanup(self):
        """Attach an already-running replacement to this match without launching anything.

        The original process may have crashed. Its worker deliberately never follows a new
        PID; only the active session (or its one completion callback) can register that game.
        No launch window is retained from the dead process, and no future process is awaited.
        """
        if not self._cleanup_job and not self._game_ours:
            return False
        try:
            status = match_cleanup.read_status(self._cleanup_job) if self._cleanup_job else {}
            if (status.get("result") or {}).get("match_id") == self.match_id:
                return False  # Already collected/closed; a later launch is not this match.
            job = match_cleanup.register(
                self.match_id, str((self.me or {}).get("steam_id") or ""), self.token,
                self._game_dir())
            if not job or job == self._cleanup_job:
                return False
            self._cleanup_job = job
            self._game_launch_at = None
            self._game_ours = True
            self.game_close = ""
            self._close_gen += 1
            self._log_close("reattached", self._i_am_host(), "replacement_process")
            self._watch_cleanup(job, self._close_gen)
            return True
        except Exception:
            self._log_exception("reattach_match_cleanup")
            return False

    def launch_game(self):
        """THE JOINER PRESSING "Launch game" (Sam, 2026-09-16): "instead of the joiners having
        their games auto launch, give them a button ... greyed out until its confirmed that the
        host is in the game".

        WHY IT IS A BUTTON NOW AND NOT AN EVENT HANDLER. The release rule has not changed - a
        joiner's pak gets ONE lobby search per launch, so opening before the host's lobby carries
        CH_MATCH spends it on an empty Steam (see _maybe_launch_game and gameReportedIn). What has
        changed is who pulls the trigger once the gate is open: the server says WHEN it MAY happen
        (`host_ready`), the player says when it DOES. A hub that yanked Steam open under someone
        who was still mid-sentence in Discord was the complaint this answers.

        `host_ready` is therefore a hard guard, not just a greyed button: a stale click from a JS
        snapshot taken a frame before the gate opened must not spend the search either.

        IT IS ALSO THIS PLAYER'S "I am in the game". That button is gone, and this is what took
        its place - pressing Launch is the honest, explicit act it always was, only now it is the
        one that actually does something. The start is still gated on reality rather than on the
        claim: goLiveIfReady holds the match until the GAME reports teams that match the lobby's,
        so reporting in here cannot start a match nobody is standing in."""
        if self.phase != "connecting" or self._i_am_host() or not self.host_ready:
            return
        # The pak FIRST, and this ordering is the whole feature: paks mount at process start, so
        # an install that lands after the launch does nothing at all.
        if not self._prepare_launch():
            return
        self._maybe_launch_game("player")
        self.report_connected()

    def relaunch_game(self):
        """Open the game after the player has closed it, without terminating a process."""
        if self.phase not in ("connecting", "live"):
            return
        try:
            if game_mod.game_running():
                self._game_launch_at = None
                self._take_game_ownership()
                self.error = t("comp_open_game_running")
                self._cue(self.error)
                self._changed()
                return
            if not self._prepare_launch():
                return
            self.error = ""
            self._game_launch_at = time.time()
            if game_mod.launch_game():
                self._take_game_ownership()
        except Exception:
            self._log_exception("relaunch_game")

    # ---------------------------------------------------------------- closing the game
    # The other half of _maybe_launch_game: the hub opened Bodycam for this match, so the hub
    # closes it again. Read the CLOSE_GAME_* block at the top of the file first - the gate below
    # is the whole of Sam's "only after flashbang registers the game as complete".
    def register_match_complete(self, source: str) -> bool:
        """THE GATE. Flashbang has written this match down as finished; Bodycam may now close.

        This is the ONLY door to _arm_game_close, and every caller is a point where the hub has
        already committed the ending to its own state - the result is on the session, the history
        is marked stale, the phase has moved. That ordering is the contract: we do not close the
        game and then record the match, we record the match and then close the game. A report that
        is still in flight cannot be cut off by a window we shut, because we have not been told it
        is finished yet and so we are not shutting anything.

        `source` is how we came to know ("match_result", "match_over", "cancelled", "preview"),
        kept on the session and written to the log so a game that closed can always be traced back
        to the thing that said it could.

        Idempotent: ten events can report the same ending and only the first one counts. Returns
        True when THIS call was the one that registered it.
        """
        source = str(source or "unknown")
        if self.match_complete:
            return False
        self.match_complete = source
        self._arm_game_close()
        return True

    def _arm_game_close(self):
        """Start the countdown to closing Bodycam, if there is a game of ours to close.

        WHAT COUNTS AS OURS. `_game_ours` is set when we launch, relaunch, or restore the game
        for a server-confirmed active match, so it is
        true exactly when this hub took this player into a match - whether it opened the game
        itself or found it already open (that player's spent BeginPlay is the strongest reason of
        all to shut it down, since they cannot queue again until it is). A match that died before
        the connect window never set it, and we close nothing: the player may well be doing
        something else in the game, and it was never ours to take away.

        WHY THE CLOSE IS SCOPED TO THE LAUNCH AND NOT TO THE MATCH. A cancelled match resets
        itself (reset_match) and can put the player straight back in the queue, so by the time
        this timer fires the session may be in a NEW match with a NEW game running. `_close_gen`
        is bumped on every launch and carried into the timer, so a close armed for one game can
        never reach the next one. Everything else it needs - who was hosting, what said the match
        was over - is captured here too, because reset_match will have taken all of it.
        """
        if self._close_armed:
            return
        self._close_armed = True
        if self.match_complete in ("match_result", "match_over"):
            if self._cleanup_job:
                match_cleanup.finish_launch(self._cleanup_job)
            # Completion can arrive before the old worker observes a crash. Capture only a
            # game already running now, then let its own durable receipt authorize the close.
            self._refresh_cleanup()
        # A durable receipt controls completed-match cleanup on every PC, including the host.
        # The detached worker survives UI exit and retains the exact process identity.
        if getattr(self, "_cleanup_job", None) and self.match_complete in ("match_result", "match_over"):
            self.game_close = "armed"
            match_cleanup.ensure_worker(self._cleanup_job)
            self._changed()
            return
        if (getattr(self, "live", False) and self.match_complete in ("match_result", "match_over")
                and len(self.match_id or "") == 16
                and all(c in "0123456789abcdef" for c in self.match_id)):
            # A production match requires exact process registration. Never replace a failed
            # registration with the legacy broad process-name close used by offline previews.
            self.game_close = "failed" if self._game_ours else "skipped"
            self._log_close(self.game_close, self._i_am_host(), "cleanup_unregistered")
            self._changed()
            return
        if not self._game_ours:
            self.game_close = "skipped"
            self._log_close("skipped", self._i_am_host(), self.match_complete)
            return
        gen, was_host, why = self._close_gen, self._i_am_host(), self.match_complete
        force = CLOSE_GAME_FORCE_HOST if was_host else CLOSE_GAME_FORCE_OTHERS
        self.game_close = "armed"
        match_id = self.match_id
        self._log_close("armed", was_host, why, match_id)
        self._changed()
        self._later(int(CLOSE_GAME_AFTER_SECONDS * 1000),
                    lambda: self._close_game_now(force, gen, was_host, why, match_id))

    def _close_game_now(self, force: bool, gen: int = 0, was_host: bool = False, why: str = "", match_id: str = ""):
        """Close Bodycam on a WORKER thread and report back on the main one.

        game.close_game blocks for as long as the grace lasts, which is most of a minute in the
        worst case; on the Tk thread that is a frozen window, and the frozen window would be the
        one telling the player what is going on.

        Clearing `_game_ours` FIRST is the de-duplication: two endings for the same match (a
        result and then a match_over, say) arm two timers, and only the first one that gets here
        owns the close."""
        if gen != self._close_gen or not self._game_ours:
            self._log_close("superseded", was_host, why, match_id)
            return                      # a newer launch owns the game now, or this is the second
        self.game_close = "closing"
        self._log_close("closing", was_host, why, match_id)
        self._changed()

        def done(outcome):
            if gen != self._close_gen:
                return
            if outcome in ("closed", "forced", "not-running"):
                self._game_ours = False
            self.game_close = outcome
            self._log_close(outcome, was_host, why, match_id)
            self._changed()
            if outcome == "failed":
                self._later(5000, lambda: self._close_game_now(force, gen, was_host, why, match_id))

        def work():
            try:
                outcome = game_mod.close_game(grace_seconds=CLOSE_GAME_GRACE_SECONDS, force=force)
            except Exception:           # noqa: BLE001 - a close that throws must not be silent
                self._log_exception("close_game")
                outcome = "failed"
            self.panel.post(lambda: done(outcome))

        self._run_off_thread(work)

    def _run_off_thread(self, work):
        """Run `work` on a daemon thread. A seam, so a test can watch the decision without
        having to race the thread that carries it out."""
        threading.Thread(target=work, daemon=True).start()

    def _log_close(self, outcome, was_host=False, why="", match_id=""):
        """One line per close, so "it closed my game" and "it did not" are both answerable.

        `why` and `was_host` are the values from ARM time: the player may have left the result
        screen by now, and reset_match would have taken both with it."""
        try:
            with open(paths.log_file(), "a", encoding="utf-8") as f:
                f.write("\n[%s] [competitive/close] %s after %s (host=%s match=%s)"
                        % (time.strftime("%Y-%m-%d %H:%M:%S"), outcome, why or "?",
                           bool(was_host), match_id or self.match_id or "?"))
        except Exception:               # noqa: BLE001
            pass

    def join_match(self):
        """PLACEHOLDER for the real non-host -> host connect (task 14, the lobby probe).

        Today a joiner reaches the host's lobby by hand - the screen tells them to accept the Steam
        invite or pick "Join game" - because the pak's single lobby search has no hub-driven trigger
        yet. This verb exists so the primary "Join" affordance is wired to a real, named endpoint
        instead of a dead null handler, and so the eventual implementation has an obvious home.

        It is DELIBERATELY a safe no-op. It must never fake a connect: reporting the player in
        without their game actually being in the lobby would let a no-show dodge the penalty check
        (`launch_game` is the one act that reports a joiner in, and it opens Bodycam to do it).
        TODO(join): drive the non-host connect once the pak exposes a hub-triggered lobby search."""
        return

    def _note_host_ready(self):
        """The host's game has reported in - host_id is now in the match_connect roster, which is
        the documented go-ahead (_maybe_launch_game): only now can a joiner's single lobby search
        find the host's stamped CH_MATCH lobby. For a NON-HOST this is where the visible, real
        5-minute join window begins; it is deliberately NOT begun on match_connecting, because until
        the host is up there is nothing to join and no clock should count against the player."""
        if self.host_ready:
            return
        self.host_ready = True
        if not self._i_am_host():
            self.join_left = JOIN_SECONDS
            self._later(1000, self._tick_join)
        self._changed()

    def _tick_join(self):
        """Draw the non-host join countdown. Like _tick_connect it only counts down and stops at
        zero: the SERVER owns the real deadline (re-based to host-ready in live.cjs) and decides
        when it has run out, so this waits to be told rather than acting on the client clock."""
        if self.phase != "connecting" or not self.host_ready:
            return
        self.join_left = max(0, self.join_left - 1)
        self._changed()
        if self.join_left:
            self._later(1000, self._tick_join)

    def _changed(self):
        self.panel.on_change()

    def _cue(self, text, ms=None):
        """A transient line (a toast), not state. Only the web panel can show one, and the Tk
        panel and the test harnesses have no such thing - so this is a no-op wherever it is not
        supported rather than a reason for any of them to grow a method.

        `ms` overrides how long the page holds it up; None leaves the page's own default. Returns
        whether the cue was actually delivered, so a caller whose whole message IS the cue can say
        it some other way on a panel that has no toast."""
        push = getattr(self.panel, "push_event", None)
        if not callable(push):
            return False
        event = {"type": "toast", "text": str(text)}
        if ms:
            event["ms"] = int(ms)
        try:
            push(event)
        except Exception:              # noqa: BLE001 — a cue is never worth an exception
            return False
        return True

    def _team_kill_warning(self, event):
        """Queue a localized warning without treating SSE receipt as player acknowledgement."""
        match_id = str(event.get("match_id") or self.match_id or "")
        warning_id = str(event.get("warning_id") or "")
        if not match_id or not warning_id:
            return
        if self.match_id and match_id != str(self.match_id):
            return
        key = (match_id, warning_id)
        if key in self._combat_warning_presented:
            return
        text = t("comp_teamkill_warning")
        push = getattr(self.panel, "push_event", None)
        if callable(push):
            try:
                push({"type": "combat_warning", "text": text, "ms": 8000,
                      "match_id": match_id, "warning_id": warning_id})
            except Exception:              # noqa: BLE001 — the warning still gets a visible fallback
                pass
            else:
                self._combat_warning_pending.add(key)
                self._combat_warning_presented.add(key)
                return
        # The classic panel has no transient-event channel. Its live screen already draws `error`,
        # so leave the warning there and deliberately send no receipt we cannot substantiate.
        self.error = text
        self._changed()

    def ack_combat_warning(self, match_id, warning_id):
        """Called by the web page after its warning toast exists in the rendered DOM."""
        key = (str(match_id or ""), str(warning_id or ""))
        if key not in self._combat_warning_pending or key in self._combat_warning_acking:
            return
        if not self.client:
            return
        self._combat_warning_acking.add(key)
        self._action(lambda client=self.client: client.ack_combat_warning(*key),
                     lambda status, body: self._combat_warning_ack_result(key, status))

    def _combat_warning_ack_result(self, key, status):
        self._combat_warning_acking.discard(key)
        if 200 <= int(status or 0) < 300:
            self._combat_warning_pending.discard(key)

    # ------------------------------------------------------------ the queue's one hard rule
    def game_is_open(self) -> bool:
        """Is Bodycam running right now? The EXACT probe, not the cached display value.

        This is a decision (may this player queue at all), and the cached answer is only
        refreshed when a snapshot is built - a hub sitting still on the Competitive screen can
        hold a minutes-old opinion, which is exactly the case this guard exists for: the player
        opens the game, then presses Find match. One `tasklist` on one deliberate press is a
        different cost from the three per redraw that made every click take three seconds
        (hub/game.py). It fails OPEN: if we cannot tell, the queue is not withheld."""
        try:
            return bool(game_mod.game_running())
        except Exception:              # noqa: BLE001 — a probe must never eat the button
            self._log_exception("game_running")
            return False

    def _close_game_cue(self):
        """Say "close your game" for three seconds and change nothing else.

        The web UI has a toast for exactly this. The classic Tk window has none, so there the
        same sentence goes on the idle screen's error line and is taken away again on a one-shot
        - otherwise its Find match button would look broken."""
        text = t("comp_close_game_queue")
        if self._cue(text, ms=CLOSE_GAME_CUE_MS):
            return
        self.error = text
        self._changed()
        self._later(CLOSE_GAME_CUE_MS, lambda: self._clear_close_game_cue(text))

    def _clear_close_game_cue(self, text):
        """Take the stand-in line back down, unless something else has since had its say."""
        if self.error != text:
            return
        self.error = ""
        self._changed()

    # ------------------------------------------------------------ the watchdog
    # One repeating tick for the whole session. It watches the phase (and, in the lobby, the
    # stage) and how long it has held still. Nothing else has to remember to set a timer, and
    # a phase added later is covered the moment it gets a line in PHASE_LIMITS.
    def _start_watchdog(self):
        self._watch_key = None
        self._watch_since = time.monotonic()
        self._later(STALL_TICK_SECONDS * 1000, self._watch_tick)

    def _phase_key(self):
        return (self.phase, self.stage) if self.phase == "lobby" else self.phase

    def _watch_tick(self):
        key = self._phase_key()
        now = time.monotonic()
        if key != self._watch_key:
            self._watch_key, self._watch_since = key, now
        else:
            limit = PHASE_LIMITS.get(key)
            if limit and now - self._watch_since > limit:
                self._watch_since = now        # do not fire again on the next tick
                try:
                    self.on_stalled(key)
                except Exception:              # noqa: BLE001 - the watchdog must never die
                    pass
        self._later(STALL_TICK_SECONDS * 1000, self._watch_tick)

    def on_stalled(self, key):
        """A phase outstayed its deadline. Hand the window back and say what happened.

        This is a backstop, not a rule: reaching it means our code, the service or the network
        lost the thread. So it never blames the player and never costs them anything."""
        if self.phase == "signing_in":
            self.cancel_sign_in()
            self.error = t("comp_timed_out")
            self._changed()
            return
        setting_up = self.phase in ("found", "lobby", "connecting")
        self.leave_everything()
        self.reset_match()
        self.phase = "idle" if self.me else "signed_out"
        self.error = t("comp_match_stalled") if setting_up else t("comp_timed_out")
        self._changed()

    def leave_everything(self):
        """Tell the service we are out of whatever we were in. Nothing to do offline."""

    # ------------------------------------------------------------ locking
    def locked_in(self) -> bool:
        """True once a match exists and walking away would strand nine other people.

        Sign-out is hidden for exactly this window. A player who really is stuck still gets
        out: every phase in LOCKED_PHASES has a deadline in PHASE_LIMITS."""
        return self.phase in LOCKED_PHASES

    def banned_left(self) -> int:
        """Seconds left on a queue ban, and it clears itself once served."""
        if not self.penalty_until:
            return 0
        left = int(round(self.penalty_until - time.time()))
        if left <= 0:
            self.penalty_until = 0.0
            self.penalty_reason = ""      # the COUNT stays: it is what the next rung is built on
            return 0
        return left

    def _arm_penalty_expiry(self):
        """Rebuild the snapshot exactly once, at the moment the queue ban runs out.

        The browser ticks the ban down every second on its own (webui competitive.js), so this
        is NOT a per-second Python ticker — it is a single one-shot so that can_find flips back
        true, and the un-banned hero shows, even when no other event happens to fire at expiry.
        Called from every site that sets penalty_until.

        The scheduler has no cancel, so overlapping bans are made safe with a generation token
        instead: every (re)arm bumps _penalty_gen, and a one-shot does its work only if its
        captured generation is still current — a stale timer from an earlier ban is a no-op."""
        self._penalty_gen = getattr(self, "_penalty_gen", 0) + 1
        if not self.penalty_until:
            return                            # cleared: the bump above already voids any pending one-shot
        gen = self._penalty_gen
        ms = int((self.penalty_until - time.time()) * 1000) + 50   # a hair past 0 so it has crossed
        if ms < 0:
            ms = 0

        def _fire():
            if gen != getattr(self, "_penalty_gen", 0):
                return                        # a newer arm (or a clear) supersedes this timer
            if self.banned_left() > 0:        # fired a touch early (clock rounding): try once more
                self._arm_penalty_expiry()
                return
            self._changed()                   # banned_left() cleared it: the un-banned UI rebuilds

        self._later(ms, _fire)

    def _fake_players(self):
        me = dict(self.me)
        me["ping"] = 24
        out = [me]
        for i in range(2, LOBBY_SIZE + 1):
            out.append({"name": t("comp_mock_player", n=i),
                        "steam_id": f"7656119800000000{i:02d}",
                        "level": random.randint(max(1, (self.me["level"] or 5) - 3),
                                                min(13, (self.me["level"] or 5) + 3)),
                        "ping": random.randint(12, 90)})
        return out

    # ---------------------------------------------------------------- sign in
    # This part is REAL, even though the queue above it is not: Steam OpenID through our own
    # service (hub/auth.py, server/auth.cjs). Everything after sign-in is still preview data.
    def sign_in(self):
        if self.phase != "signed_out":
            return
        self._signin_epoch += 1
        attempt = self._signin_epoch
        self.phase = "signing_in"
        self.error = ""
        self.link_url = ""
        self.link_code = ""
        self._cancel_signin = False
        self._changed()
        threading.Thread(target=lambda: self._sign_in_worker(attempt), daemon=True).start()

    def cancel_sign_in(self):
        self._signin_epoch += 1
        self._cancel_signin = True
        if self.phase == "signing_in":
            self.phase = "signed_out"
            self.error = t("comp_signin_cancelled")
            self._changed()

    def open_link_again(self):
        if self.link_url:
            webbrowser.open(self.link_url)

    def _deliver_signin(self, attempt, callback):
        if attempt == self._signin_epoch and self.phase == "signing_in":
            callback()

    def _sign_in_worker(self, attempt=None):
        """Worker thread. Never touches tkinter — everything goes back through panel.post."""
        attempt = self._signin_epoch if attempt is None else attempt
        try:
            started = auth_mod.start()
        except auth_mod.AuthError:
            self.panel.post(lambda: self._deliver_signin(attempt, lambda: self._sign_in_failed(t("comp_signin_offline"))))
            return
        self.panel.post(lambda: self._deliver_signin(attempt, lambda: self._sign_in_started(started)))
        try:
            webbrowser.open(started["url"])
        except Exception:        # noqa: BLE001 — no browser: the code is on screen anyway
            pass
        try:
            ready = auth_mod.wait_for(started["code"], should_stop=lambda: self._cancel_signin or attempt != self._signin_epoch)
        except auth_mod.AuthError as e:
            reason = str(e)
            if reason == "cancelled":
                return           # cancel_sign_in already moved the UI
            message = {"the sign-in link expired": t("comp_signin_expired"),
                       "timed out": t("comp_signin_timeout")}.get(reason,
                                                                  t("comp_signin_failed", reason=reason))
            self.panel.post(lambda: self._deliver_signin(attempt, lambda: self._sign_in_failed(message)))
            return
        self.panel.post(lambda: self._deliver_signin(attempt, lambda: self._sign_in_done(ready)))

    def _sign_in_started(self, started):
        if self.phase != "signing_in":
            return
        self.link_url = started.get("url", "")
        self.link_code = started.get("code", "")
        self._changed()

    def _sign_in_failed(self, message):
        self.phase = "signed_out"
        self.error = message
        self._changed()

    def _sign_in_done(self, account):
        if self.adopt_account(account, save=True) is False:
            return
        self.phase = "idle"
        self.error = ""
        self._changed()

    def adopt_account(self, account, save=False):
        """Take a verified account and become signed in as them.

        Rank fields are still invented — there is no backend to ask yet. The identity is
        real; everything numeric next to it is preview data (the tab says so)."""
        steam_id = str(account.get("steam_id") or "")
        persona = str(account.get("persona") or "")
        self.me = {"name": persona or steam_id, "steam_id": steam_id,
                   "avatar": str(account.get("avatar") or ""),
                   "level": 6, "elo": 1180, "bdr": None, "matches": 34, "wins": 19}
        self.online = random.randint(40, 180)
        self.queue_size = random.randint(0, 12)
        self.live_matches = random.randint(0, 9)
        self.token = str(account.get("token") or self.token or "")
        if save:
            self.panel.save_auth({"token": self.token, "steam_id": steam_id,
                                  "persona": persona, "avatar": self.me["avatar"]})

    def sign_out(self):
        # The header hides the button during a match; this is the other half of that promise,
        # so nothing else in the hub can sign a player out from under nine other people.
        if self.locked_in():
            self.error = t("comp_signout_locked")
            self._changed()
            return
        token = self.token
        try:
            pending = auth_mod.queue_revoke(token) if token else None
        except Exception:
            self.error = t("account_storage_failed")
            self._changed()
            return
        self._complete_sign_out(token, pending)

    def _complete_sign_out(self, token, pending):
        """The revocation record is already durable; this path cannot queue it again."""
        from . import telemetry
        telemetry.identify(None)
        if token:
            threading.Thread(target=lambda: auth_mod.revoke_session(token, pending=pending), daemon=True).start()
        self.token = ""
        self.me = None
        self.phase = "signed_out"
        self.party = None
        self.party_error = ""
        self.error = ""
        try:
            self.panel.save_auth(None)
        except Exception:
            # Revoked authority must remain detached even if persistence fails.
            pass
        self.reset_match()
        self._changed()

    # ---------------------------------------------------------------- party
    def party_size(self) -> int:
        return len(self.party["members"]) if self.party else 1

    def is_party_leader(self) -> bool:
        """Solo counts as leader: there is nobody else to wait for."""
        if not self.party:
            return True
        return self.party["leader_id"] == (self.me or {}).get("steam_id")

    def party_member_name(self, steam_id: str) -> str:
        """A party member's name, for a sentence about THEM. Falls back to the id: a refusal that
        names nobody is worse than one naming a number they can at least compare."""
        for p in (self.party or {}).get("members", []):
            if p.get("steam_id") == steam_id:
                return p.get("name") or steam_id
        return steam_id

    def party_leader_name(self) -> str:
        if not self.party:
            return (self.me or {}).get("name", "?")
        for p in self.party["members"]:
            if p.get("steam_id") == self.party["leader_id"]:
                return p.get("name", "?")
        return "?"

    def create_party(self):
        if self.party or not self.me or self.phase not in ("idle",):
            return
        self.party_error = ""
        self.party = {"code": make_party_code(), "leader_id": self.me["steam_id"],
                      "members": [dict(self.me)]}
        self._changed()
        self._later(2600, self._mock_friend_joins)      # preview only: somebody turns up

    def _mock_friend_joins(self):
        if not self.party or self.party_size() >= MAX_PARTY or self.phase != "idle":
            return
        n = self.party_size() + 1
        self.party["members"].append({
            "name": t("comp_mock_player", n=n), "steam_id": f"7656119800000001{n:02d}",
            "level": random.randint(3, 11), "ping": random.randint(12, 90)})
        self._changed()

    def join_party(self, code: str):
        """Mock: any well-formed code that is not your own joins a party of three."""
        if not self.me or self.phase != "idle":
            return
        code = normalise_party_code(code)
        if not code:
            self.party_error = t("comp_party_bad_code")
            self._changed()
            return
        if self.party and code == self.party["code"]:
            self.party_error = t("comp_party_own_code")
            self._changed()
            return
        leader = {"name": t("comp_mock_player", n=2), "steam_id": "76561198000000102",
                  "level": random.randint(3, 11), "ping": random.randint(12, 90)}
        other = {"name": t("comp_mock_player", n=3), "steam_id": "76561198000000103",
                 "level": random.randint(3, 11), "ping": random.randint(12, 90)}
        self.party_error = ""
        self.party = {"code": code, "leader_id": leader["steam_id"],
                      "members": [leader, other, dict(self.me)]}
        self._changed()

    def refresh_party_code(self):
        """Leader only: mint a new code. The old one stops working immediately, which is
        the point — it is what you press after the old one leaked (Sam, 2026-09-14)."""
        if not self.party or not self.is_party_leader():
            return
        self.party_error = ""
        self.party["code"] = make_party_code()
        self._changed()
        return self.party["code"]

    def leave_party(self):
        if not self.party:
            return
        members = [p for p in self.party["members"]
                   if p.get("steam_id") != (self.me or {}).get("steam_id")]
        # if the leader walks out, the party passes to whoever is next
        self.party = None
        self.party_error = ""
        self._changed()
        return members

    # ---------------------------------------------------------------- party invites
    # The preview has no server and therefore nobody to invite (and no friends list to fetch):
    # these exist so the screen's verbs are safe to call in every mode, not because the mock
    # models an invite.
    def refresh_friends(self):
        return None

    def invite_to_party(self, steam_id):
        return None

    def invite_friend_to_party(self, steam_id):
        return None

    def accept_party_invite(self, steam_id):
        return None

    def decline_party_invite(self, steam_id):
        return None

    # ---------------------------------------------------------------- queue
    def find_match(self):
        """Integrity check first (layers 1 and 2), then the queue.

        Only the party leader starts the search; everyone else waits (see the panel)."""
        from .activity import files_busy
        if getattr(getattr(self, "account_link", None), "active", False):
            self.error = t("account_link_finish")
            self._changed()
            return
        if files_busy(self.panel):
            self.error = t("comp_files_busy")
            self._changed()
            return
        if self.phase not in ("idle", "result"):
            return
        if getattr(self, "live", False) and not getattr(self.panel, "supports_matchmaking", True):
            self.error = t("comp_browser_required")
            self._changed()
            return
        if not self.is_party_leader():
            return
        # BODYCAM HAS TO BE CLOSED TO QUEUE. The hub opens the game itself when the match is
        # found and puts the player straight into the lobby; a game that is already running has
        # spent its one BeginPlay before the match existed, and there is no channel into a live
        # Bodycam process to put it into one. This used to be a paragraph under the button asking
        # the player to work that out for themselves. Now the button simply does nothing while the
        # game is up, and says why for three seconds.
        if self.game_is_open():
            self._close_game_cue()
            return
        left = self.banned_left()
        if left:
            self.error = t("comp_queue_banned", time=format_clock(left))   # a live countdown
            self._changed()
            return
        # NOTHING TO PLAY. The gamemode has to be installed before any of this means anything:
        # the queue is for a match in a mode this player does not have, and the connect window
        # would send them into a pak that is not there. The Tk tab never reaches this line
        # (the gate screen replaces the button) - the web UI's hero did, which is the bug.
        # Live only: the preview exists to be clicked through with nothing installed.
        if getattr(self, "live", False) and not self.gamemode_installed():
            self.error = t("comp_gate_body")
            self._changed()
            return
        # Behind the release the service is publishing: the join would be refused anyway, so say
        # it here rather than spending the integrity check first and refusing afterwards. The
        # idle screen's card has already said this; the button being disabled is the same test.
        stale = self.update_needed()
        if stale:
            self.error = outdated_line(stale)
            self._changed()
            return
        self.reset_match()
        self._queue_epoch += 1
        self._queue_ticking = False
        self.phase = "checking"
        self.check_step = 0
        self.error = ""
        self._changed()
        if getattr(self, "live", False):
            self._checks_passed()
        else:
            self._later(700, self._check_next)

    def _check_next(self):
        if self.phase != "checking":
            return
        self.check_step += 1
        if self.check_step >= 3:
            self._checks_passed()
            return
        self._changed()
        self._later(700, self._check_next)

    def _checks_passed(self):
        """The integrity check is done. The preview fakes a queue; LiveSession joins the real one."""
        self.phase = "queued"
        self.queue_seconds = 0
        self._changed()
        self._tick_queue()

    def _tick_queue(self):
        if self.phase != "queued":
            return
        self.queue_seconds += 1
        if self.queue_seconds >= 4:
            self.phase = "found"
            self.accepted = 0
            self.i_accepted = False
            self.accept_left = ACCEPT_SECONDS
            self.players = self._fake_players()
            self.accept_total = len(self.players)
            self._changed()
            self._tick_accept()
            return
        self._changed()
        self._later(1000, self._tick_queue)

    def cancel_queue(self):
        self.phase = "idle"
        self.reset_match()
        self._changed()

    # ---------------------------------------------------------------- accept
    def _tick_accept(self):
        if self.phase != "found":
            return
        self.accept_left -= 1
        if self.accept_left <= 0:
            self.phase = "idle"
            self.error = t("comp_declined")
            self.reset_match()
            self._changed()
            return
        self._changed()
        self._later(1000, self._tick_accept)

    def accept(self):
        if self.phase != "found" or self.i_accepted:
            return
        self.i_accepted = True
        self.accepted = 1
        self._changed()
        self._later(500, self._others_accept)

    def _others_accept(self):
        if self.phase != "found" or not self.accepted:
            return
        self.accepted += 1
        if self.accepted >= max(1, self.accept_total):
            self._start_lobby()
            return
        self._changed()
        self._later(260, self._others_accept)

    # ---------------------------------------------------------------- lobby
    def _start_lobby(self):
        """Split the roster into two teams. Must survive ANY size, not just ten.

        With COMP_MATCH_SIZE=1 on the server a match forms with one player, team 2 comes out
        empty, and the old random.choice(self.teams[2]) raised IndexError - which the UI pump
        swallowed, so the tab simply froze on "Match found" with nothing to go on (2026-09-14).
        Finding me by steam_id rather than assuming I am first also matters: the preview built
        that list itself, the server does not promise an order."""
        roster = list(self.players) or [dict(self.me or {})]
        my_id = (self.me or {}).get("steam_id")
        me = next((p for p in roster if p.get("steam_id") == my_id), roster[0])
        others = [p for p in roster if p is not me]
        random.shuffle(others)
        half = max(1, len(roster) // 2)          # 10 -> 5, 2 -> 1, 1 -> 1 (team 2 empty)
        # me on team 1 and captain of it, so every interaction is reachable in the preview
        self.teams = {1: [me] + others[:half - 1], 2: others[half - 1:]}
        self.captains = {1: me, 2: (self.teams[2][0] if self.teams[2] else me)}
        self.phase = "lobby"
        self._open_stage("coin")
        self.chat = self._fresh_chat()
        self._changed()

    def my_team(self) -> int:
        for n, members in self.teams.items():
            if any(p.get("steam_id") == (self.me or {}).get("steam_id") for p in members):
                return n
        return 1

    def enemy_team(self) -> int:
        return 2 if self.my_team() == 1 else 1

    # ------------------------------------------------- enemy anonymity (see ENEMY_CALLSIGNS)
    def hide_enemies(self) -> bool:
        """True exactly while the pre-round lobby is on screen.

        It lifts with the lobby, not with the match: from the connect window onwards, leaving
        is a no-show that costs elo and a queue ban (NO_SHOW_ELO), so the penalty is already
        doing the job the call signs were doing and the names cost nothing. Hiding them any
        longer would only make the scoreboard and the report list unreadable."""
        return getattr(self, "phase", "") == "lobby"

    def enemy_aliases(self) -> dict:
        """{steam_id: call sign} for the other team, or {} when nothing is being hidden.

        Ordered by steam id rather than by roster position, so that every client in the lobby
        calls the same person Bravo - including their own team-mates, who see them named in all
        chat. Roster order would agree too, right up to the first client that rebuilds its teams
        from a replayed lobby payload."""
        if not self.hide_enemies():
            return {}
        ids = sorted({str(p.get("steam_id") or "")
                      for p in (self.teams or {}).get(self.enemy_team()) or []} - {""})
        return {sid: (ENEMY_CALLSIGNS[i] if i < len(ENEMY_CALLSIGNS) else "Contact %d" % (i + 1))
                for i, sid in enumerate(ids)}

    def display_name(self, player) -> str:
        """What a screen should CALL this person: their persona, or their call sign while the
        lobby is hiding them. Everything the player reads goes through here."""
        player = player or {}
        sid = str(player.get("steam_id") or "")
        return self.enemy_aliases().get(sid) or player.get("name") or sid

    def is_hidden(self, steam_id) -> bool:
        return str(steam_id or "") in self.enemy_aliases()

    def captain_name(self, team) -> str:
        """A captain as the screen must say it - "Bravo is banning\u2026" for the other side."""
        return self.display_name((self.captains or {}).get(team) or {})

    def i_am_captain(self) -> bool:
        cap = self.captains.get(self.my_team()) or {}
        return cap.get("steam_id") == (self.me or {}).get("steam_id")

    def i_am_captain_of(self, team) -> bool:
        cap = (self.captains or {}).get(team) or {}
        return bool(team) and cap.get("steam_id") == (self.me or {}).get("steam_id")

    # ONE captain flips the coin for the whole match, not one per team, so all ten clients
    # agree who it is. The deterministic, server-owned rule is "the captain of team 1"; the
    # live session takes the same id off the server (self.coin_captain), the mock derives it.
    def coin_captain_id(self) -> str:
        if getattr(self, "coin_captain", None):
            return self.coin_captain
        cap = (self.captains or {}).get(1) or {}
        return str(cap.get("steam_id") or "")

    def i_am_coin_captain(self) -> bool:
        cid = self.coin_captain_id()
        return bool(cid) and cid == (self.me or {}).get("steam_id")

    def coin_captain_name(self) -> str:
        cid = self.coin_captain_id()
        # The flipping captain can be on the other side, and "waiting for <name>" is on screen
        # for the whole toss - which made it the one line that named an enemy every match.
        alias = self.enemy_aliases().get(cid)
        if alias:
            return alias
        for members in (self.teams or {}).values():
            for p in members:
                if p.get("steam_id") == cid:
                    return p.get("name") or ""
        for cap in (self.captains or {}).values():
            if cap and cap.get("steam_id") == cid:
                return cap.get("name") or ""
        return ""

    def pick_coin(self, side: str):
        # Only the single designated captain may flip; everyone else is waiting to be told.
        if self.stage != "coin" or self.coin_side or not self.i_am_coin_captain():
            return
        self.coin_side = side
        # The face is decided on the way UP, not on the way down, so the coin the screen spins is
        # already showing what it will land on. The live path does the same (live.cjs applyCoin)
        # because all ten clients have to watch the same toss; this one matches it so the offline
        # lobby is not a different animation with a different shape.
        self.coin_result = random.choice(["heads", "tails"])
        # The designated captain is team 1's, so the call is FOR team 1: a matching face wins
        # it for team 1, a miss for team 2. (LiveSession takes the server's result instead.)
        self.toss_winner = 1 if self.coin_result == self.coin_side else 2
        self._open_stage("flipping")
        self._changed()
        self._later(FLIP_SECONDS * 1000, self._coin_lands)

    def _coin_lands(self):
        if self.stage != "flipping":
            return
        self._open_stage("choice")
        self._changed()
        if self.toss_winner != self.my_team():
            self._later(1600, self._other_chooses)

    def _other_chooses(self):
        if self.stage != "choice":
            return
        self.choose(random.choice(["side", "ban"]), by=self.toss_winner)

    def choose(self, kind: str, by: int = None):
        """The toss winner takes ONE advantage: the starting SIDE or the last BAN.

        Whoever takes SIDE gets the attack/defend selector (a real choice, the "side" stage
        below); the other team gets the ban advantage. Whoever takes BAN bans LAST (controls
        the final map) and the other team gets the side selector. The ban ORDER is set here so
        the advantage team bans last whatever the pool size (ban_first_team).

        `by` is the acting team (the preview's bot passes it when the bot team won the toss); a
        real UI click comes in as the toss winner's captain."""
        if self.stage != "choice":
            return
        if by is None and not self.i_am_captain_of(self.toss_winner):
            return
        winner = self.toss_winner
        loser = 2 if winner == 1 else 1
        self.advantage = kind
        if kind == "side":
            self.side_picker = winner          # the winner picks attack/defend
            self.ban_advantage = loser         # the loser bans last
        else:
            self.side_picker = loser           # the loser picks attack/defend
            self.ban_advantage = winner        # the winner bans last
        self.first_ban = ban_first_team(len(self.panel.map_pool()), self.ban_advantage)
        self.ban_turn = self.first_ban
        # The side selector runs next, whoever holds it, before any map is banned.
        self._open_stage("side")
        self._changed()
        if self.side_picker != self.my_team():
            self._later(1500, self._bot_pick_side)

    def _bot_pick_side(self):
        if self.stage != "side" or self.side_picker == self.my_team():
            return
        self.choose_side(random.choice(["attack", "defend"]), by=self.side_picker)

    def choose_side(self, side: str, by: int = None):
        """The team that holds the side advantage picks attack or defend; the other gets the
        opposite. This is the selector that used to be missing — SIDE was auto-assigned.

        `by` is the team acting (the preview's bot passes it); a real UI click comes in as MY
        team's captain. Either way it must be the side_picker's turn."""
        if self.stage != "side" or side not in ("attack", "defend"):
            return
        team = by if by is not None else self.my_team()
        if team != self.side_picker or (by is None and not self.i_am_captain_of(self.side_picker)):
            return
        other = 2 if self.side_picker == 1 else 1
        self.sides = {self.side_picker: side, other: ("defend" if side == "attack" else "attack")}
        self._open_stage("veto")
        self._changed()
        self._maybe_bot_ban()

    def remaining_maps(self):
        pool = getattr(self, "lobby_pool", None) or self.panel.map_pool()
        banned = {m for _, m in self.bans}
        return [m for m in pool if m not in banned]

    def _maybe_bot_ban(self):
        if self.stage != "veto" or self.ban_turn == self.my_team():
            return
        self._later(1300, self._bot_ban)

    def _bot_ban(self):
        if self.stage != "veto" or self.ban_turn == self.my_team():
            return
        left = self.remaining_maps()
        if len(left) <= 1:
            return
        self.ban(random.choice(left), by=self.ban_turn)

    def ban(self, map_name: str, by: int = None):
        if self.stage != "veto":
            return
        team = by if by is not None else self.my_team()
        # A real UI click (by is None) must come from the banning team's captain; the preview's
        # bot passes `by` explicitly and skips the captain check.
        if team != self.ban_turn or map_name not in self.remaining_maps():
            return
        if by is None and not self.i_am_captain_of(self.ban_turn):
            return
        self.bans.append((team, map_name))
        left = self.remaining_maps()
        if len(left) == 1:
            self.map = left[0]
            self._open_stage("ready")      # decided: nothing left for anyone to hold up
            self._changed()
            self._later(2200, self._begin_connect)
            return
        self.ban_turn = 2 if self.ban_turn == 1 else 1
        self._open_stage("veto")           # the next captain's turn, on a clock of its own
        self._changed()
        self._maybe_bot_ban()

    def _fresh_chat(self) -> dict:
        """Start both audiences empty; the composer already labels Team and All."""
        return {"team": [], "all": []}

    def chat_log(self, channel: str) -> list:
        """One channel's lines. An unknown channel is the TEAM log, never the all one: the cost
        of getting that fallback backwards is a message meant for four people reaching ten."""
        return self.chat.setdefault(channel if channel in CHAT_CHANNELS else "team", [])

    def _append_chat(self, channel: str, line: dict):
        """Order both channels by receipt, without relying on clock precision."""
        self._chat_order += 1
        line = dict(line, order=self._chat_order)
        log = self.chat_log(channel)
        log.append(line)
        del log[:-CHAT_KEEP]

    def set_chat_channel(self, channel: str):
        self.chat_channel = channel if channel in CHAT_CHANNELS else "team"
        self._changed()

    def toggle_chat_mute(self, steam_id: str):
        """Local, per-match text mute. Never changes the relay or another player's view."""
        sid = str(steam_id or "")
        if self.phase != "lobby" or sid == str((self.me or {}).get("steam_id") or ""):
            return
        if not sid or not any(str(p.get("steam_id") or "") == sid
                              for team in self.teams.values() for p in team):
            return
        if sid in self.muted_chat_players:
            self.muted_chat_players.remove(sid)
        else:
            self.muted_chat_players.add(sid)
        self._changed()

    def send_chat(self, text: str, channel: str = "team"):
        text = (text or "").strip()[:MAX_CHAT_CHARS]
        if not text:
            return
        # FILTERED ON THE WAY IN, so no uncensored copy is ever in a log - including the sender's
        # own, which is the point: they see what everyone else would have seen. It is not
        # enforcement (a patched client can send whatever it likes, and when chat gains a relay the
        # SERVICE has to run the same rule before it broadcasts); it is what keeps the word off the
        # screen, and reporting is what deals with the person. See hub/censor.py.
        text = censor_mod.censor(text)
        me = self.me or {}
        self._append_chat(channel, {"steam_id": str(me.get("steam_id") or ""),
                                    "name": me.get("name", "?"), "text": text})
        self._changed()

    # ---------------------------------------------------------------- connect window
    # Everything is decided; now ten people actually have to be in the same game. This is the
    # step that used to be a 2.2 second fade into "live", and it is where a real match dies
    # most often: somebody alt-tabbed, somebody's game did not launch, somebody left.
    def _begin_connect(self):
        if self.phase != "lobby":
            return
        # the host is the best ping in the match; every client works this out the same way
        self.host = min(self.players, key=lambda p: p.get("ping", 999)) if self.players else dict(self.me or {})
        self.phase = "connecting"
        self.connect_left = CONNECT_SECONDS
        self.connect_total = max(1, len(self.players))
        self.connected_ids = set()
        self.i_connected = False
        self.teams_status = ""
        self._changed()
        self._later(1000, self._tick_connect)
        self._later(1800, self._others_connect)       # preview only; LiveSession drops this
        # Preview only: fake the host reporting in a moment later, so the mock walks the same
        # "waiting for the host" -> join-window transition a real non-host sees. When the invented
        # roster made US the host we also have to fake our own arrival, because the thing that
        # reports a real host in is the lobby probe (live.cjs gameReportedIn) and there is no game
        # here to send it - and the host has no Launch button to press instead.
        self._later(2500, self._note_host_ready)
        if self._i_am_host():
            self._later(2600, self.report_connected)

    def _tick_connect(self):
        if self.phase != "connecting":
            return
        self.connect_left = max(0, self.connect_left - 1)
        if not self.connect_left:
            self._connect_expired()
            return
        self._changed()
        self._later(1000, self._tick_connect)

    def _adopt_connect_seconds(self, seconds):
        """Re-base the visible connect countdown on the SERVER's deadline.

        WHY THIS EXISTS (Sam, 2026-09-16): "the time to connect timer is still ticking down and the
        timer could run out before the hoster respawns". The server gives the match a fresh window
        whenever the host's game proves it is on its way - when it asks for its travel permit, and
        again when it reports in from the match world - but the hub was only ever told the number
        once, at `match_connecting`. So ten screens counted down to 0:00 while the real deadline sat
        five minutes out, and the people watching sensibly assumed the match was dead.

        Only ever from a payload that carries the field: an older service sends none, and there the
        old behaviour is the right one. Zero is ignored too - a window that really has run out ends
        with `match_cancelled`, which is a different message and says so."""
        if self.phase != "connecting" or seconds is None:
            return
        try:
            left = int(seconds)
        except (TypeError, ValueError):
            return
        if left <= 0:
            return
        # A countdown that has already hit zero has stopped its own loop (LiveSession._tick_connect
        # stops at zero and waits to be told), so restarting it is part of adopting a new deadline.
        restart = self.connect_left <= 0
        self.connect_left = left
        if restart:
            self._later(1000, self._tick_connect)
        self._changed()

    def _connect_expired(self):
        """The window closed. Sam's rule exactly: the match dies, everyone who turned up loses
        nothing at all, and the people who did not pay for it.

        A JOINER WHOSE HOST NEVER CAME UP DID NOT MISS ANYTHING. `host_ready` is the moment the
        host's game reports in, and _note_host_ready is where a non-host's real join window starts:
        until then there is nothing to join and the Launch button is grey. So if it never arrived,
        the host is the only one at fault here, and we must not fine ourselves for their no-show.
        (The server decides this the same way in live.cjs expireConnect; this is the local mirror
        of it, which is what fires when the hub is running its own clock.)"""
        if self.phase != "connecting":
            return
        i_missed = not self.i_connected and (self.host_ready or self._i_am_host())
        self.reset_match()
        self.phase = "idle"
        if i_missed:
            self.penalty_count += 1
            seconds = no_show_ban_seconds(self.penalty_count)
            self.penalty_until = time.time() + seconds
            self.penalty_reason = "no_show"
            self._arm_penalty_expiry()
            self.penalty_next = no_show_ban_seconds(self.penalty_count + 1)
            self.error = t("comp_no_show_me", elo=NO_SHOW_ELO, time=format_duration(seconds))
        else:
            self.error = t("comp_no_show_others")
        self._changed()

    def report_connected(self):
        """The player says they are in the game.

        NO LONGER A BUTTON OF ITS OWN (Sam, 2026-09-16). For a JOINER it is what pressing "Launch
        game" does, on the way past; for the HOST it is the lobby probe (live.cjs gameReportedIn),
        which is proof of arrival rather than a claim about it. Nothing in the UI asserts it bare
        any more, so this is only ever reached through an act that actually opens the game."""
        if self.phase != "connecting" or self.i_connected:
            return
        self.i_connected = True
        self.connected_ids = set(self.connected_ids) | {(self.me or {}).get("steam_id")}
        self._changed()
        self._check_all_connected()

    def _others_connect(self):
        """Preview only: the invented players wander in one at a time."""
        if self.phase != "connecting":
            return
        mine = (self.me or {}).get("steam_id")
        pending = [p for p in self.players
                   if p.get("steam_id") not in self.connected_ids and p.get("steam_id") != mine]
        if pending:
            self.connected_ids = set(self.connected_ids) | {pending[0].get("steam_id")}
            self._changed()
            self._check_all_connected()
        if self.phase == "connecting":
            self._later(random.randint(900, 2400), self._others_connect)

    def _check_all_connected(self):
        if self.phase == "connecting" and len(self.connected_ids) >= max(1, self.connect_total):
            self._go_live()

    # ---------------------------------------------------------------- live
    def _go_live(self):
        if self.phase not in ("lobby", "connecting"):
            return
        if not self.host and not getattr(self, "live", False):
            self.host = (min(self.players, key=lambda p: p.get("ping", 999))
                         if self.players else dict(self.me or {}))
        self.phase = "live"
        self._changed()

    def start_vote(self):
        if getattr(self, "live", False) or self.phase != "live" or self.vote:
            return
        self.vote = {"caller": (self.me or {}).get("name", "?"), "yes": 0, "no": 0, "voted": False}
        self._changed()

    def cast_vote(self, yes: bool):
        if getattr(self, "live", False) or not self.vote or self.vote["voted"]:
            return
        self.vote["voted"] = True
        self.vote["yes" if yes else "no"] += 1
        self._changed()

    def finish(self, voided: bool = False):
        """Preview only: a real match ends when our GM_Bodybomb reports the scoreboard."""
        if getattr(self, "live", False) or self.phase not in ("live", "lobby"):
            return
        self.vote = None
        if voided:
            self.result = {"won": False, "score": (0, 0), "delta": 0, "voided": True}
        else:
            won = random.choice([True, False])
            score = (7, random.randint(2, 6)) if won else (random.randint(2, 6), 7)
            delta = random.choice([1, 2, 3]) * (1 if won else -1)
            # The size of RR move a live match pays, so the preview card reads like the real one.
            rr_delta = random.randint(18, 26) if won else -random.randint(14, 20)
            self.result = {"won": won, "score": score, "delta": delta, "rr_delta": rr_delta,
                           "voided": False}
            self.me["matches"] = (self.me.get("matches") or 0) + 1
            if won:
                self.me["wins"] = (self.me.get("wins") or 0) + 1
        self._record_local_match()
        # Taken BEFORE the phase moves, while the teams, the sides and the veto are still here:
        # this is the same picture the match-history detail draws, frozen at the moment it was
        # still true (see _postmatch_record).
        self._open_postmatch()
        self.phase = "result"
        # The match is now written down - result on the session, row in the local history. THAT
        # is what "registered as complete" means, and only now may the game be closed. Off
        # Windows and with nothing launched this is a no-op, so the preview stays harmless.
        self.register_match_complete("preview")
        self._changed()

    def leave_result(self):
        self.reset_match()
        self.phase = "idle"
        self._changed()

    # ---------------------------------------------------------------- the post-match card
    #
    # SAM, 2026-09-16: "an immediate post match Victory or Defeat screen ... that comes up after
    # the game ends and stays there until its closed manually". So it is state, not a screen
    # transition: it is raised over WHATEVER the player is looking at, and only the X, the
    # backdrop or Escape take it down. Nothing here is on a timer, nothing watches the phase, and
    # a later snapshot push cannot clear it - those are the three ways a card like this usually
    # disappears out from under somebody who was still reading it.
    #
    # It is also a FROZEN COPY rather than a live view of the session. A second after a match
    # ends the player can press Back (reset_match) or queue again, and both wipe the teams, the
    # sides and the veto the card is made of; a card that read them live would empty itself while
    # it was still on screen.
    def _open_postmatch(self, score=None):
        """Raise the card for the match that just ended. `score` is an explicit [team1, team2]
        for a caller that knows one the session does not."""
        record = self._postmatch_record(score)
        if record:
            self.postmatch = record

    def close_postmatch(self):
        """The ONLY way the card goes away. Idempotent: a double click on the X is one close."""
        if self.postmatch is None:
            return
        self.postmatch = None
        self._changed()

    def _postmatch_my_team(self) -> int:
        """My team number, or 0 when nothing here knows it.

        NOT `my_team()`, which answers 1 for a player it cannot place - fine for a lobby that is
        always populated, a lie on a card that marks one column as yours."""
        me_id = str((self.me or {}).get("steam_id") or "")
        if not me_id:
            return 0
        for n in (1, 2):
            for p in ((self.teams or {}).get(n) or []):
                if str(p.get("steam_id") or "") == me_id:
                    return n
        payload = self.result_payload if isinstance(self.result_payload, dict) else {}
        for p in (payload.get("players") or []):
            if str(p.get("steam_id") or "") == me_id:
                n = _int_or(p.get("team"))
                if n in (1, 2):
                    return n
        return 0

    def _postmatch_score(self, score=None):
        """The scoreline BY TEAM NUMBER ([team1, team2]), or None when the match reported none.

        `self.result["score"]` is ours:theirs - only this client knows which way round that is -
        so it is turned back here. A 0:0 is not a scoreline: it is the placeholder a voided match
        and a result that arrived without a score both carry, and the card says "no score" rather
        than printing a nil-all nobody played."""
        # A VOIDED MATCH HAS NO SCORE, whatever arrived alongside the void. The rounds were played
        # and then unplayed; printing the scoreline of a match that officially did not happen is
        # the one thing the card must not do, and the payload is not trusted to have dropped it.
        if (self.result or {}).get("voided"):
            return None
        payload = self.result_payload if isinstance(self.result_payload, dict) else {}
        for candidate in (score, payload.get("score")):
            if isinstance(candidate, (list, tuple)) and len(candidate) >= 2:
                return [_int_or(candidate[0]), _int_or(candidate[1])]
        ours = (self.result or {}).get("score")
        if not isinstance(ours, (list, tuple)) or len(ours) < 2:
            return None
        a, b = _int_or(ours[0]), _int_or(ours[1])
        if a == 0 and b == 0:
            return None
        return [b, a] if self._postmatch_my_team() == 2 else [a, b]

    def _postmatch_winner(self, mine: int):
        """Which team won, as a number, or None. Taken from the payload where the service said
        so, otherwise derived from this player's own result - which is the same fact, seen from
        one side. None when neither is known, so the card colours nothing rather than guessing."""
        payload = self.result_payload if isinstance(self.result_payload, dict) else {}
        n = _int_or(payload.get("winner"))
        if n in (1, 2):
            return n
        won = (self.result or {}).get("won")
        if isinstance(won, bool) and mine in (1, 2):
            return mine if won else (2 if mine == 1 else 1)
        return None

    def _postmatch_roster(self, mine: int) -> dict:
        """Both teams, {1: [...], 2: [...]}, from the lobby roster - or from the result payload
        for a hub that came back into a match it had no roster of its own for."""
        me_id = str((self.me or {}).get("steam_id") or "")
        teams = {1: [], 2: []}
        for n in (1, 2):
            for p in ((self.teams or {}).get(n) or []):
                sid = str(p.get("steam_id") or "")
                teams[n].append({"steam_id": sid, "name": p.get("name") or sid,
                                 "is_me": bool(me_id) and sid == me_id, "left": False})
        if teams[1] or teams[2]:
            return teams
        payload = self.result_payload if isinstance(self.result_payload, dict) else {}
        for p in (payload.get("players") or []):
            if not isinstance(p, dict):
                continue
            n = _int_or(p.get("team"))
            if n not in (1, 2):
                continue
            sid = str(p.get("steam_id") or "")
            teams[n].append({"steam_id": sid,
                             "name": p.get("name") or p.get("persona") or sid,
                             "is_me": bool(me_id) and sid == me_id,
                             "left": bool(p.get("left"))})
        return teams

    def _postmatch_record(self, score=None):
        """The finished match, in the shape the card draws - or None when there is no result to
        draw yet.

        WHAT IS HERE IS WHAT IS REAL, the same rule the match-history detail follows: the map,
        the scoreline, the two teams with the side each played, the veto in order, and - since
        the result event started carrying it - the per-player scoreboard. A figure the match did
        not report stays None and the card says so; a match the gamemode reported nothing about
        has an EMPTY board rather than a column of zeroes that would read as "everyone went 0-0".

        Assembled from what is already in hand, so the card is up the instant the match ends and
        never waits on a round trip."""
        r = self.result
        if not isinstance(r, dict) or not r:
            return None
        mine = self._postmatch_my_team()
        sides = self.sides or {}
        try:
            bans = [{"team": _int_or(team), "map": str(m)} for team, m in (self.bans or [])]
        except (TypeError, ValueError):      # a shape we did not expect costs the veto strip only
            bans = []
        payload = self.result_payload if isinstance(self.result_payload, dict) else {}
        return {
            "match_id": str(self.match_id or ""),
            "at": time.time() * 1000,
            "map": str(self.map or ""),
            "won": r.get("won") if isinstance(r.get("won"), bool) else None,
            "voided": bool(r.get("voided")),
            "void_reason": str(r.get("reason") or ""),
            "my_team": mine,
            "winner": self._postmatch_winner(mine),
            "score": self._postmatch_score(score),
            "delta": _int_or(r.get("delta")),
            # THE RR, and the two placement states that have none to show. None from a service
            # that predates `rr_delta`: the card then draws no rank line at all, rather than the
            # arrow count with "RR" after it that read as "only gaining and losing 1-3 RR".
            "rr_delta": r.get("rr_delta") if isinstance(r.get("rr_delta"), int) else None,
            "placing": bool(r.get("placing")),
            "placed": bool(r.get("placed")),
            "placements_left": _int_or(r.get("placements_left")),
            "placed_rank": str(r.get("placed_rank") or ""),
            "seconds": _int_or(payload.get("seconds")),
            "teams": self._postmatch_roster(mine),
            "sides": {1: str(sides.get(1) or ""), 2: str(sides.get(2) or "")},
            "bans": bans,
            # THE SCOREBOARD, straight off the result event. Passed through untouched - the card
            # shapes it - so that a payload from a newer service carrying more columns is not
            # trimmed here on its way past.
            "scoreboard": payload.get("scoreboard")
                          if isinstance(payload.get("scoreboard"), list) else [],
            "round_details": copy.deepcopy(payload.get("round_details"))
                             if isinstance(payload.get("round_details"), list) else [],
            "rounds_played": payload.get("rounds_played"),
        }

    # ---------------------------------------------------------------- match history
    def load_history(self, force=False):
        """Fetch the player's past matches. The offline preview has none but its own.

        `force` re-asks even when we already have an answer; the sub-tab uses it for the
        Refresh button and after a match ends, and leaves the cache alone otherwise so that
        flicking between Play and History mid-queue costs nothing."""
        if self.history is None:
            self.history = []
        self.history_error = ""
        self.history_loading = False

    def load_match(self, match_id, on_done):
        """One match in full, for the detail pop-up. Calls on_done(record_or_None)."""
        on_done(None)

    def _record_local_match(self):
        """The preview has no server to remember for it, so a finished mock match is put
        straight into the local list. Nothing here ever reaches Upstash."""
        if self.history is None:
            return
        r = self.result or {}
        self.history.insert(0, {
            "id": "preview-%d" % int(time.time() * 1000),
            "ended": time.time() * 1000,
            "map": self.map or "",
            "outcome": "played",
            "reason": "",
            "blamed": False,
            "team": self.my_team() or 0,
            "side": (self.sides or {}).get(self.my_team(), ""),
            "host": False,
            "players": len(self.players),
            "won": None if r.get("voided") else r.get("won"),
            "score": None if r.get("voided") else list(r.get("score") or ()),
            "delta": None if r.get("voided") else r.get("delta"),
            "rr_delta": None if r.get("voided") else r.get("rr_delta"),
            "placement": False,
            "elo": None,
            "connected": True,
            "accepted": True,
            "preview": True,
        })
        del self.history[50:]


class LiveSession(MockSession):
    """Server-authoritative competitive session.

    Every live event arrives on the reader thread and is bounced to the main thread through
    panel.post before it touches any of this state."""
    mock = False
    live = True
    stats_ready = False
    stats_queued = 0

    def __init__(self, panel):
        super().__init__(panel)
        self.client = None
        self.parent_token = ""
        self._proof_epoch = 0
        self._pending_game_account = None
        from .account_link import AccountLink
        self.account_link = AccountLink(self)
        # Exactly one queue countdown may ever be running. It is armed idempotently (the
        # `queued` event and the join POST's 200 race each other, and either may arrive first),
        # so this flag is what stops a second overlapping loop from double-counting the clock.
        self._queue_ticking = False
        self._cancel_queue_pending = False
        # Same guard for the accept countdown: a match_found replayed on reconnect (bug 3) must
        # not start a second loop on top of the one already running, or the timer runs double.
        self._accept_ticking = False
        # Set by every match event, and read once the grace below has run: together they are
        # how a reconnect tells "the service still has my match and just replayed it" from
        # "the service no longer has it" (see _rejoin_gave_up).
        self._match_replay_seen = False
        self._rejoin_deadline = 0.0      # monotonic; 0.0 means nothing is being waited for

    def _clear_account_state(self):
        self._account_epoch += 1
        self._signin_epoch += 1
        self._cancel_signin = True
        self._queue_epoch += 1
        self._invite_generation.clear()
        self._party_transition = False
        fields = ("history history_error history_loading history_stale friends friend_requests_in "
                  "friend_requests_out friend_code friend_code_hidden friends_error friends_loading "
                  "party_invites invite_error invite_sent board_rows board_you board_available board_loading "
                  "board_error tournament_data tournament_loading tournament_error tournament_received "
                  "messages_data messages_loading messages_target messages_thread messages_thread_loading "
                  "messages_error messages_sending messages_pending messages_sent "
                  "match_detail_id match_detail match_detail_loading match_detail_error "
                  "report_target report_match report_name report_sent report_error bug_text bug_sending "
                  "bug_sent bug_error bug_last_sent postmatch penalty_until penalty_reason penalty_count penalty_next")
        for name in fields.split():
            setattr(self, name, getattr(MockSession, name, None))
        for name in ("history_seq", "friends_seq", "board_seq", "report_seq", "bug_seq", "tournament_ticket_seq", "messages_send_seq", "messages_generation", "_penalty_gen"):
            setattr(self, name, getattr(self, name, 0) + 1)
        self.party = None
        self.party_error = ""
        self._queue_ticking = self._accept_ticking = False
        self.reset_match()

    # ---------------------------------------------------------------- connection
    def _revoke_login(self, token):
        if token:
            try:
                pending = auth_mod.queue_revoke(token)
            except Exception:
                return False
            threading.Thread(target=lambda: auth_mod.revoke_session(token, pending=pending), daemon=True).start()
        return True

    def _abandon_game_login(self):
        token = getattr(self, "parent_token", "")
        if token and not self._revoke_login(token):
            self.phase = "game_unavailable"
            self.error = t("account_storage_failed")
            self._changed()
            return False
        self.parent_token = self.token = ""
        self._pending_game_account = None
        self.me = None
        if token:
            self.panel.save_auth(None)
        return True

    def _cancel_account_login(self):
        self._proof_epoch = getattr(self, "_proof_epoch", 0) + 1
        self.account_busy = False
        self.account_step = ""
        self._login_challenge = ""
        self._recovery_challenge = ""
        self._reset_token = ""
        self._account_remember = False
        self.game_verifying = False
        self.error = ""

    def sign_in(self):
        if self.phase == "signed_out":
            self._cancel_account_login()
            super().sign_in()

    def cancel_sign_in(self):
        if self.phase in ("signed_out", "signing_in"):
            self._cancel_account_login()
            if self._abandon_game_login() is False:
                return
            super().cancel_sign_in()
            self._changed()

    def account_action(self, action, fields=None):
        """Email credentials/codes never enter snapshots, telemetry or saved state."""
        if action == "game/retry":
            if self.phase == "game_unavailable" and self._pending_game_account:
                self.error = ""
                self._prove_game_account(self._pending_game_account, False)
            return
        if action == "cancel":
            return self.cancel_sign_in()
        if self.me or self.phase != "signed_out" or getattr(self, "account_busy", False):
            return
        fields = fields if isinstance(fields, dict) else {}
        if action not in ("login", "login/verify", "register", "verify", "forgot-password", "forgot-password/verify", "reset-password"):
            return
        if action == "forgot-password/verify" and (getattr(self, "account_step", "") != "recovery_code" or not getattr(self, "_recovery_challenge", "")):
            return
        if action == "reset-password" and (getattr(self, "account_step", "") != "reset_password" or not getattr(self, "_reset_token", "")):
            return
        self.account_busy = True
        self.error = ""
        self._proof_epoch = getattr(self, "_proof_epoch", 0) + 1
        epoch = self._proof_epoch
        if action == "login":
            self._account_remember = fields.get("remember_me") is True
            payload = {key: fields.get(key) for key in ("email", "password", "remember_me")}
        elif action == "login/verify":
            payload = {"challenge": getattr(self, "_login_challenge", ""), "code": fields.get("code")}
        elif action == "register":
            payload = {"email": fields.get("email")}
        elif action == "forgot-password":
            self._reset_token = ""
            self._login_challenge = ""
            payload = {"email": fields.get("email"), "language": i18n.get_language()}
        elif action == "forgot-password/verify":
            payload = {"challenge": self._recovery_challenge, "code": fields.get("code")}
        elif action == "reset-password":
            payload = {"token": self._reset_token, "password": fields.get("password")}
        else:
            payload = {key: fields.get(key) for key in ("token", "password", "display_name")}
        self._changed()
        def work():
            try:
                result = auth_mod.account_request(action, payload)
                def apply():
                    if getattr(self, "_proof_epoch", 0) != epoch:
                        if action == "login/verify":
                            self._revoke_login(result.get("token"))
                        return
                    self.account_busy = False
                    if action == "login":
                        self._login_challenge = result["challenge"]
                        self.account_step = "login_code"
                    elif action == "register":
                        self.account_step = "register_code"
                    elif action == "verify":
                        self.account_step = "login"
                        self.error = t("account_created")
                    elif action == "forgot-password":
                        self._recovery_challenge = result["challenge"]
                        self.account_step = "recovery_code"
                    elif action == "forgot-password/verify":
                        self._recovery_challenge = ""
                        self._reset_token = result["reset_token"]
                        self.account_step = "reset_password"
                    elif action == "reset-password":
                        self._reset_token = self._recovery_challenge = ""
                        self.account_step = "login"
                        self.error = t("account_password_reset")
                    else:
                        self._login_challenge = ""
                        self.account_step = ""
                        remember = getattr(self, "_account_remember", False)
                        if not remember:
                            if self.panel.save_auth(None) is False:
                                self._revoke_login(result.get("token"))
                                self.error = t("account_storage_failed")
                                self._changed()
                                return
                        self.adopt_account({**result, "remember_me": remember}, save=remember)
                    self._changed()
                self.panel.post(apply)
            except Exception as exc:
                code = getattr(exc, "code", "")
                def failed():
                    if getattr(self, "_proof_epoch", 0) == epoch:
                        self.account_busy = False
                        self.error = t("account_request_failed")
                        if action in ("forgot-password", "forgot-password/verify", "reset-password"):
                            key = {"rate_limited": "account_recovery_limited", "invalid_code": "account_recovery_invalid",
                                   "invalid_email": "account_recovery_email", "invalid_password": "account_recovery_password"}.get(code)
                            self.error = t(key or ("account_reset_uncertain" if action == "reset-password" else "account_recovery_failed"))
                            if action == "reset-password" and code == "invalid_code":
                                self._reset_token = ""
                                self.account_step = "forgot_password"
                        self._changed()
                self.panel.post(failed)
            finally:
                payload.clear()
        threading.Thread(target=work, daemon=True).start()

    def adopt_account(self, account, save=False):
        account = {**(account.get("account") or {}), **account}
        account = player_identity.normalize(account)
        if str(account.get("token") or "").startswith("lo_"):
            return self._prove_game_account(account, save)
        steam_id = str(account.get("steam_id") or "")
        same_account = bool(self.me and self.me.get("steam_id") == steam_id)
        token = str(account.get("token") or self.token or "")
        if not same_account or token != self.token:
            self._disconnect()
        if not same_account:
            self._clear_account_state()
            # Live accounts start with identity only. Preview ranks and match counts must
            # never stand in for a rating that has not arrived from the server yet.
            self.me = {"steam_id": steam_id, "player_id": steam_id}
            self.online = self.live_matches = self.queue_size = self.stats_queued = 0
            self.stats_ready = False
        # Saved-login validation races the first rating/stats events. Refresh identity in
        # place so its later response cannot erase server progress on the existing stream.
        self.me.update({"name": str(account.get("persona") or steam_id),
                        "avatar": str(account.get("avatar") or ""),
                        "game_steam_id": player_identity.native_id(account)})
        self.token = token
        self.parent_token = str(account.get("parent_token") or token)
        linked = account.get("account") or {}
        self.me.update({"auth_method": "lightsout" if self.parent_token.startswith("lo_") else "steam",
                        "linked_account": bool(account.get("account_id") or linked.get("steam_id")),
                        "account_email": str(linked.get("email") or account.get("email") or "")})
        if save:
            saved = self.panel.save_auth({"token": self.parent_token, "player_id": steam_id,
                "steam_id": steam_id, "game_steam_id": self.me["game_steam_id"],
                "persona": self.me["name"], "avatar": self.me["avatar"],
                "remember_me": account.get("remember_me", True)})
            if saved is False:
                abandoned = self._abandon_game_login()
                self.phase = "signed_out" if abandoned else "game_unavailable"
                self.error = t("account_storage_failed")
                self._changed()
                return False
        self._connect()
        return True

    def _prove_game_account(self, account, save, renewal=False):
        from . import game_identity
        if not renewal:
            self._disconnect()
            if (self.me or {}).get("steam_id") != account.get("player_id"):
                self._clear_account_state()
            self.me = None
        self._proof_epoch = getattr(self, "_proof_epoch", 0) + 1
        epoch = self._proof_epoch
        self.parent_token = account["token"]
        self._pending_game_account = dict(account)
        parent = self.parent_token
        if not renewal and save and account.get("remember_me", False):
            if self.panel.save_auth(account) is False:
                abandoned = self._abandon_game_login()
                self.phase = "signed_out" if abandoned else "game_unavailable"
                self.error = t("account_storage_failed")
                self._changed()
                return False
        if not renewal:
            self.token = ""
            self.game_verifying = True
            self.phase = "signing_in"
            self._changed()
        def current():
            return self._proof_epoch == epoch and self.parent_token == parent
        def renew():
            if not current():
                return
            if self.phase != "idle":
                self._later(60000, renew)
            else:
                self._prove_game_account(account, False, renewal=True)
        def work():
            try:
                verified = game_identity.mint_session(parent, self._game_dir(), should_stop=lambda: not current())
                if verified.get("player_id") != account.get("player_id"):
                    raise game_identity.GameIdentityError()
                def apply():
                    if not current():
                        return
                    if renewal and self.phase != "idle":
                        self._later(60000, renew)
                        return
                    adopted = self.adopt_account({**account, **verified, "persona": account.get("persona") or account.get("display_name"),
                        "parent_token": parent}, save=save and account.get("remember_me", False))
                    self.game_verifying = False
                    if adopted is False:
                        return
                    self.phase = "idle"
                    self._changed()
                    # Prove in the background, and only replace the live connection while idle.
                    self._later(6 * 3600 * 1000, renew)
                self.panel.post(apply)
            except Exception:
                def failed():
                    if current():
                        if renewal:
                            self._later(60000, renew)
                            return
                        self.game_verifying = False
                        self.error = t("account_game_unavailable")
                        self.phase = "game_unavailable"
                        self._changed()
                self.panel.post(failed)
        threading.Thread(target=work, daemon=True).start()

    def restore_account(self, saved):
        if not saved.get("token"):
            return
        saved = dict(saved)
        self._proof_epoch = getattr(self, "_proof_epoch", 0) + 1
        epoch = self._proof_epoch
        account_epoch = self._account_epoch
        delays = (1000, 2000, 5000, 10000, 20000, 30000)
        def current():
            return (self._proof_epoch == epoch and self._account_epoch == account_epoch
                    and not getattr(self.panel, "_closed", False))
        def start(attempt=0):
            if current():
                threading.Thread(target=lambda: work(attempt), daemon=True).start()
        def work(attempt):
            if not current():
                return
            try:
                fresh = auth_mod.me(saved["token"])
            except auth_mod.AuthUnavailable:
                def retry():
                    if current():
                        self._later(delays[min(attempt, len(delays) - 1)],
                                    lambda: start(attempt + 1))
                self.panel.post(retry)
                return
            except auth_mod.AuthError:
                return
            def apply():
                if not current() or not fresh:
                    return
                self.adopt_account({**saved, **fresh, "token": saved["token"]})
                if not str(saved["token"]).startswith("lo_"):
                    self.phase = "idle"
                self._changed()
            self.panel.post(apply)
        start()

    def _connect(self):
        if self.client is not None or not self.token:
            return
        self.connected = False
        # A stopped reader may already have posted callbacks. Check its identity
        # on delivery so it cannot restore stale stats after signout/replacement.
        client = live_mod.LiveClient(
            self.token,
            on_event=lambda event: self.panel.post(
                lambda: self.on_live_event(event) if self.client is client else None),
            on_status=lambda ok, detail: self.panel.post(
                lambda: self._on_live_status(ok, detail) if self.client is client else None),
            versions=self._versions, network_config=self._network_config)
        self.client = client
        self.client.player_id = (self.me or {}).get("player_id")
        self.client.start()

    def _network_config(self):
        app = getattr(self.panel, "app", None)
        state = getattr(app, "state", None) or {}
        return {"game_dir": getattr(app, "game_dir", None) or state.get("game_dir") or "",
                "region": state.get("matchmaking_region") or "",
                "cross_region": state.get("matchmaking_cross_region") is True}

    def _versions(self):
        """What this hub is running, for the service's queue gate: our own version and the
        INSTALLED ranked pack's.

        A callable rather than a snapshot, because the pack's version changes underneath a
        running hub - press Update on the gamemode and the very next request reports the new
        number, with no restart and no reconnect. See hub/live.py `_stamp` for where it goes
        and why it goes on every request rather than just the join."""
        app = getattr(self.panel, "app", None)
        installed = ((getattr(app, "state", None) or {}).get("installed") or {})
        entry = installed.get(COMPETITIVE_MODE_ID) or {}
        return {"hub": HUB_VERSION, "mode": str(entry.get("version") or "")}

    def _disconnect(self):
        self.account_link.cancel(notify=False)
        self._account_epoch += 1
        self.stats_ready = False
        if self.client is not None:
            self.client.stop()
            self.client = None
        self.connected = True          # not "disconnected" once we are signed out; just idle

    def sign_out(self):
        if self.locked_in():
            super().sign_out()          # refuses, and says why
            return
        try:
            pending = match_cleanup.pending_for((self.me or {}).get("player_id") or (self.me or {}).get("steam_id"))
        except OSError:
            self.error = t("account_storage_failed")
            self._changed()
            return
        if pending:
            try:
                game_open = game_mod.game_running()
            except Exception:
                game_open = True  # Unknown process state cannot authorize credential revocation.
        else:
            game_open = False
        if game_open:
            self.error = t("account_wait_cleanup")
            self._changed()
            return
        try:
            token = getattr(self, "parent_token", "") or self.token
            pending = auth_mod.queue_revoke(token) if token else None
        except Exception:
            self.error = t("account_storage_failed")
            self._changed()
            return
        self._disconnect()
        self._clear_account_state()
        self._cancel_account_login()
        self.parent_token = ""
        self._pending_game_account = None
        self._complete_sign_out(token, pending)

    def finish_account_link(self):
        """Ownership changed or its final response was lost: require fresh authority.

        Normal sign-out may refuse for cleanup or disk errors. Revoked/uncertain
        credentials must still be detached, and must not remain in app memory.
        """
        token = self.parent_token or self.token
        try:
            pending = auth_mod.queue_revoke(token) if token else None
        except Exception:
            pending = None
        self._disconnect()
        self._clear_account_state()
        self._cancel_account_login()
        self.parent_token = ""
        self._pending_game_account = None
        state = getattr(getattr(self.panel, "app", None), "state", None)
        if isinstance(state, dict):
            state["auth"] = None
        self._complete_sign_out(token, pending)

    def _on_live_status(self, ok, detail):
        self.connected = bool(ok)
        if not ok:
            self.stats_ready = False
        if not ok and self.phase == "queued":
            # the server no longer knows we are queued; do not pretend we still are. A match
            # phase ("found"/"ready"/"connecting") is NOT dropped here: the server keeps the
            # player in the match across a transient stream drop and replays its state on
            # reconnect (server/live.cjs handleStream), so hard-dropping to idle would abandon
            # the accept window and let the server blame the player for declining (bug 3).
            self.phase = "idle"
            self.error = t("comp_live_lost")
        self._changed()

    # ---------------------------------------------------------------- actions
    def leave_everything(self):
        """The watchdog gave up on a phase. Do not just walk away from the window: tell the
        service, or it goes on holding a match that nobody is in any more."""
        if not self.client:
            return
        if self.phase in ("found", "lobby", "connecting", "live"):
            self._action(self.client.leave_match)
        elif self.phase in ("queued", "checking"):
            self._cancel_queue_pending = True
            cancel_pending = getattr(self.client, "cancel_pending_queue", None)
            if callable(cancel_pending):
                cancel_pending()
            self._action(self.client.leave_queue)

    def _action(self, call, on_result=None):
        """Run one POST on a WORKER thread and deliver the result to the main thread.

        These used to run inline, which froze the whole window for as long as the request
        took — up to the 15 s timeout if the service was slow."""
        epoch, client = self._account_epoch, self.client
        def current():
            return epoch == self._account_epoch and self.client is client
        def work():
            if not current():
                return
            status, body = call()
            if on_result is not None:
                self.panel.post(lambda: on_result(status, body) if current() else None)
        threading.Thread(target=work, daemon=True).start()

    # A deploy, a restart or a dropped packet should not end the attempt: these are the
    # statuses worth trying again. 404 is in the list because Railway's edge returns one
    # while a new container is taking over (2026-09-14: "Could not join the queue: Not found"
    # landed exactly during a redeploy).
    RETRY_STATUSES = (0, 404, 429, 500, 502, 503, 504)
    JOIN_RETRY_DELAYS = (1000, 3000, 6000)

    def _checks_passed(self):
        """Integrity passed: ask the real queue. Stay on the check screen until it answers,
        so the tab never claims to be queued before the server agrees."""
        if self.phase == "checking":
            self._cancel_queue_pending = False
        self._join_attempt(0)

    def _join_attempt(self, attempt):
        if self.phase == "queued":
            # A `queued` event landed while we were still checking (the server writes it to the
            # open stream before the POST answers). That IS success: abandon any pending retry
            # and make sure the countdown is running, rather than treating it as a failure.
            self._start_queue_tick()
            return
        if self.phase != "checking" or not self.client:
            if not self.client:
                self.phase = "idle"
                self.error = t("comp_queue_failed", reason="offline")
                self._changed()
            return
        prepare = getattr(self.client, "prepare_queue_join", None)
        action = prepare() if callable(prepare) else self.client.join_queue
        generation = self._queue_epoch
        self._action(action, lambda s, b: self._join_result(s, b, attempt) if generation == self._queue_epoch else None)

    def _join_result(self, status, body, attempt):
        if self.phase not in ("checking", "queued"):
            return                       # the player cancelled while we were asking
        if status == 200:
            # The `queued` event may have arrived first and already started the countdown; only
            # zero the clock when we are the ones putting the player into the queue.
            if self.phase == "checking":
                self.queue_seconds = 0
            self.phase = "queued"
            self.queue_position = int(body.get("position") or self.queue_position or 0)
            self.queue_size = int(body.get("size") or self.queue_size or 0)
            self.error = ""
            self._start_queue_tick()
            self._changed()
            return
        if self.phase == "queued":
            # The `queued` event already confirmed us; a late non-200 from the POST is stale and
            # must not knock us out of the queue or fire another retry.
            return
        if status == 426 or body.get("outdated"):
            # The service publishes a newer release than this hub (or than the pack it has), so
            # the queue is shut until it updates. A DECISION, not a failure: no retry could make
            # an old build current, and the same sentence the idle screen shows is used here so
            # a refusal from the service and one the hub worked out for itself read alike.
            #
            # It stops at the queue and nowhere else: a match already under way is untouched by
            # this, on both sides of the wire.
            self.phase = "idle"
            who = str(body.get("who") or "")
            self.error = (party_outdated_line(body, self.party_member_name(who))
                          if who and who != (self.me or {}).get("steam_id")
                          else outdated_line(body))
            self._changed()
            return
        if status == 403 and body.get("banned"):
            # a queue ban, which is a decision, not a failure: say what it is and how long
            #
            # `who` MEANS IT IS SOMEBODY ELSE'S BAN. The service refuses the join for the whole
            # party when any member is banned, and it sends the same `banned` body either way -
            # only the presence of `who` (live.cjs: `...(mine ? {} : { who })`) says whose it is.
            # Taking the seconds off that body regardless started a countdown on an INNOCENT
            # member's screen and told them, on the idle screen, that they were the one who never
            # loaded into a game. Nothing on the server says that: the ban belongs to their friend.
            seconds = int(body.get("seconds") or NO_SHOW_BAN_SECONDS)
            self.phase = "idle"
            who = str(body.get("who") or "")
            if who and who != (self.me or {}).get("steam_id"):
                self.error = t("comp_queue_banned_party", who=self.party_member_name(who),
                               time=format_clock(seconds))
                self._changed()
                return
            self.penalty_until = time.time() + seconds
            self.penalty_reason = str(body.get("reason") or "no_show")
            self._arm_penalty_expiry()
            self.penalty_count = int(body.get("count") or self.penalty_count)
            self.penalty_next = int(body.get("next_seconds") or self.penalty_next)
            self.error = t("comp_queue_banned", time=format_clock(seconds))
            self._changed()
            return
        if status in self.RETRY_STATUSES and attempt < len(self.JOIN_RETRY_DELAYS):
            generation = (self._account_epoch, self._queue_epoch)
            self._later(self.JOIN_RETRY_DELAYS[attempt], lambda: self._join_attempt(attempt + 1)
                        if generation == (self._account_epoch, self._queue_epoch) else None)
            return
        self.phase = "idle"
        self.error = t("comp_queue_failed", reason=str(body.get("error") or status or "offline"))
        self._changed()

    def _start_queue_tick(self):
        """Arm the queue countdown, at most once. Safe to call from every path that enters (or
        re-enters) the queue — the `queued` event, the join 200, a requeue after a cancelled
        match, a reconnect requeue — because the flag makes a second start a no-op."""
        if self._queue_ticking:
            return
        self._queue_ticking = True
        generation = (self._account_epoch, self._queue_epoch)
        self._later(1000, lambda: self._tick_queue(generation))

    def _tick_queue(self, generation=None):
        """Count the wait up. Unlike the preview, nothing here invents a match."""
        generation = generation or (self._account_epoch, self._queue_epoch)
        if generation != (self._account_epoch, self._queue_epoch):
            return
        if self.phase != "queued":
            self._queue_ticking = False   # left the queue: the loop stops and can be re-armed
            return
        self.queue_seconds += 1
        self._changed()
        self._later(1000, lambda: self._tick_queue(generation))

    def cancel_queue(self):
        self._cancel_queue_pending = True
        self._queue_epoch += 1
        self._queue_ticking = False
        if self.client:
            cancel_pending = getattr(self.client, "cancel_pending_queue", None)
            if callable(cancel_pending):
                cancel_pending()
            self._action(self.client.leave_queue)     # fire and forget: we are leaving either way
        super().cancel_queue()

    def accept(self):
        if self.phase != "found" or self.i_accepted or not self.client:
            return
        self.i_accepted = True            # optimistic: the event confirms it a moment later
        self._changed()
        self._action(self.client.accept, self._accept_result)

    def _accept_result(self, status, body):
        if status != 200 and self.phase == "found":
            self.i_accepted = False       # the POST failed: let the player try again
            self.error = str(body.get("error") or "")
            self._changed()

    def _start_accept_tick(self):
        """Arm the accept countdown, at most once (mirrors _start_queue_tick). Called from every
        match_found, including the one replayed on reconnect — the flag makes the replay a
        no-op if the loop is already running."""
        if self._accept_ticking:
            return
        self._accept_ticking = True
        self._later(1000, self._tick_accept)

    def _tick_accept(self):
        """Show the countdown, but the SERVER decides when it has run out."""
        if self.phase != "found":
            self._accept_ticking = False
            return
        self.accept_left = max(0, self.accept_left - 1)
        self._changed()
        if self.accept_left:
            self._later(1000, self._tick_accept)
        else:
            self._accept_ticking = False    # stopped at zero; a new match may re-arm it

    # ---------------------------------------------------------------- connect window
    CONNECT_RETRY_DELAYS = (700, 2000, 5000)

    def _begin_connect(self, attempt=0):
        """Ask the SERVER to open the window. Nothing moves until it says so, because it owns
        the clock and the penalty; this client only draws them."""
        if self.phase != "lobby" or not self.client:
            return
        # The server selected the lowest-average eligible host at formation. Displayed pings
        # are player-to-host estimates, so sorting them here would pick the host's zero again
        # or overwrite a correctly chosen host with a stale value. Old matches have no choice.
        if not self.host:
            self.host = self.players[0] if self.players else dict(self.me or {})
        map_name = self.map or ""
        host_id = (self.host or {}).get("steam_id") or ""
        # The lobby's outcome goes up with the same POST, so the match lands in everyone's
        # Match History with teams, sides and the veto rather than just a map name. The server
        # checks it against the roster and records it as client-reported.
        teams = {team: [p.get("steam_id") or "" for p in members]
                 for team, members in (self.teams or {}).items()}
        sides = dict(self.sides or {})
        bans = list(self.bans or ())
        self._action(lambda client=self.client: client.start_connect(map_name, host_id, teams, sides, bans),
                     lambda status, body: self._connect_started(status, body, attempt))

    def _connect_started(self, status, body, attempt):
        if self.phase != "lobby":
            return                        # the server's match_connecting already moved us
        if status == 200:
            return                        # the event will arrive and do the work
        if status in self.RETRY_STATUSES and attempt < len(self.CONNECT_RETRY_DELAYS):
            self._later(self.CONNECT_RETRY_DELAYS[attempt], lambda: self._begin_connect(attempt + 1))
            return
        # out of retries: say so and let the lobby watchdog release everyone
        self.error = str(body.get("error") or "")
        self._changed()

    def _tick_connect(self):
        """Draw the countdown. The SERVER decides when it has run out, exactly as with the
        accept window, so this stops at zero and waits to be told."""
        if self.phase != "connecting":
            return
        self.connect_left = max(0, self.connect_left - 1)
        self._changed()
        if self.connect_left:
            self._later(1000, self._tick_connect)

    def _others_connect(self):
        """Preview only. Real players report in through the service."""

    def report_connected(self):
        if self.phase != "connecting" or self.i_connected or not self.client:
            return
        self.i_connected = True           # optimistic: the event confirms it a moment later
        self.connected_ids = set(self.connected_ids) | {(self.me or {}).get("steam_id")}
        self._changed()
        self._action(self.client.report_connected)

    def leave_result(self):
        if self.client:
            self._action(self.client.leave_match)
        super().leave_result()

    # ---------------------------------------------------------------- server-authoritative lobby
    # The coin flip, the side/ban choice and the veto are decided by the SERVER now, not by each
    # client's own random (which is how ten players used to see ten different winners). These
    # overrides send the captain's actions to the service and set NOTHING locally except the brief
    # coin-flip animation; the real outcome arrives on the stream as a `lobby` event and
    # _apply_lobby writes it, so every client lands on the same state.
    def _apply_network(self, payload):
        connection = payload.get("network") or {}
        by_id = {str(p.get("steam_id")): p for p in (self.players or [])}
        for pid, ping in (connection.get("pings") or {}).items():
            if pid in by_id:
                by_id[pid]["ping"] = ping
        host_id = str(connection.get("host") or "")
        if host_id in by_id:
            self.host = dict(by_id[host_id])
            average = connection.get("average")
            self.host["ping"] = round(average) if isinstance(average, (int, float)) else None
            self.host["ping_estimated"] = True

    def _apply_lobby(self, payload):
        """Write the server's lobby state onto this client. The single source of truth for
        teams, the designated captain, the toss, the side/ban advantage and the veto.

        A payload with no lobby fields (an old server's bare match_ready, replayed on reconnect)
        is left alone: it must NOT null out a coin flip or veto we already have (bug 3). The real
        server always sends the full lobbyPayload, so a present `stage`/`teams` marks real state."""
        if not payload or ("stage" not in payload and "teams" not in payload):
            return
        if payload.get("match_id"):
            self.match_id = str(payload["match_id"])
        by_id = {str(p.get("steam_id")): p for p in (self.players or [])}
        self._apply_network(payload)

        def player(pid):
            pid = str(pid or "")
            return by_id.get(pid) or ({"name": pid, "steam_id": pid} if pid else None)

        teams = payload.get("teams") or {}
        built = {n: [player(i) for i in (teams.get(str(n)) or teams.get(n) or []) if str(i)]
                 for n in (1, 2)}
        if built[1] or built[2]:
            self.teams = built
        caps = payload.get("captains") or {}
        self.captains = {n: player(caps.get(str(n)) or caps.get(n) or "") for n in (1, 2)}
        self.coin_captain = str(payload.get("coin_captain") or "")
        self.coin_side = payload.get("coin_side")
        self.coin_result = payload.get("coin_result")
        self.toss_winner = payload.get("toss_winner")
        self.advantage = payload.get("advantage")
        self.side_picker = payload.get("side_picker")
        self.ban_advantage = payload.get("ban_advantage")
        sides = payload.get("sides") or {}
        self.sides = {n: (sides.get(str(n)) or sides.get(n) or "") for n in (1, 2)}
        self.first_ban = payload.get("first_ban")
        self.ban_turn = payload.get("ban_turn")
        self.coin_auto = bool(payload.get("coin_auto"))
        self.advantage_auto = bool(payload.get("advantage_auto"))
        self.side_auto = bool(payload.get("side_auto"))
        # THE BAN CLOCK comes off the server with every lobby payload, and the server is the only
        # thing that decides when a turn is up - this just draws it. Re-read on every payload
        # rather than only on a turn change, so a client that reconnects mid-turn shows the real
        # time left instead of a fresh full window.
        #
        # `ban_seconds` is the old name, and it is a real fallback rather than politeness: the
        # version gate ships LENIENT, so a hub can be talking to the PREVIOUS server, which clocks
        # the veto alone and calls it that. Reading both means the veto keeps its clock either way
        # and only the three new ones go missing against an old service.
        self.stage_seconds = _int_or(payload.get("stage_seconds"))
        self.stage_total_seconds = _int_or(payload.get("stage_total_seconds"))
        if not self.stage_seconds:
            self.stage_seconds = _int_or(payload.get("ban_seconds"))
            self.stage_total_seconds = self.stage_total_seconds or _int_or(payload.get("ban_total_seconds"))
        if not self.stage_total_seconds:
            self.stage_total_seconds = BAN_SECONDS if payload.get("stage") == "veto" else PICK_SECONDS
        pool = payload.get("pool") or []
        if pool:
            self.lobby_pool = [str(m) for m in pool]
        self.bans = [(_int_or(b.get("team")), str(b.get("map")))
                     for b in (payload.get("bans") or []) if isinstance(b, dict) and b.get("map")]
        if payload.get("map"):
            self.map = str(payload.get("map"))
        stage = payload.get("stage")
        if stage:
            self.stage = stage
        # Re-arm the countdown on anything that starts a NEW turn - a new stage, or the next
        # captain's ban inside the veto - and on a payload that arrived while nothing was ticking
        # (a reconnect mid-turn). Keyed on the pair rather than the stage alone because the veto is
        # many turns in one stage, and on neither alone because a ban and a stage change can land
        # in the same payload.
        key = (self.stage, self.ban_turn)
        if self.stage_seconds and (key != self._clock_key or not self._stage_ticking):
            self._clock_key = key
            self._start_stage_tick()

    def _enter_lobby(self, event):
        """A fresh match_ready. The server has already split the teams and named the one captain
        who flips; take all of it rather than shuffling our own (which is how the old client-side
        lobby put every client on 'team 1' and disagreed about the toss)."""
        players = [self._player_from(p) for p in (event.get("players") or [])]
        if players:
            self.players = players
        self.phase = "lobby"
        self.chat = self._fresh_chat()
        self.i_accepted = True
        self._apply_lobby(event)
        if not self.stage:
            self.stage = "coin"
        self._changed()

    def _lobby_advance(self, event):
        """A `lobby` event: the server moved the flip/choice/side/veto on. Apply it, and when the
        veto has decided the map open the connect window (idempotent server-side, so it does not
        matter that every client does this)."""
        if self.phase != "lobby":
            return
        self._apply_lobby(event)
        if self.stage == "ready" and self.map:
            self._begin_connect()
        self._changed()

    # ------------------------------------------------- chat
    # RELAYED, NOT ECHOED. MockSession appends to its own log because it has no service to talk
    # to; here the line goes to the server and comes back as a `chat` event, and THAT copy is what
    # is drawn. One source of truth, so the sender sees exactly what the other nine see - the
    # server's filtered text, under the name the server decided they are allowed to be told.
    def send_chat(self, text: str, channel: str = "team"):
        text = (text or "").strip()[:MAX_CHAT_CHARS]
        if not text or not self.client:
            return
        channel = channel if channel in CHAT_CHANNELS else "team"
        # Filtered here as well as at the service. Not belt-and-braces for its own sake: a hub can
        # be newer than the service it is talking to (the two ship separately), and this is the
        # half we know is current.
        text = censor_mod.censor(text)
        self._action(lambda client=self.client: client.send_chat(text, channel),
                     lambda status, body: self._chat_refused(channel, status, body))

    def _chat_refused(self, channel, status, body):
        """The service would not take a line. Say so IN the log it was typed into.

        Reachable mainly through the rate limit (one message every half second), and the reason it
        is handled at all: the JS clears the input the moment Send is pressed and _action throws
        the response away, so a refusal used to make the message quietly not exist. A player cannot
        tell that apart from the bug this feature replaced.

        The service's own words are used rather than a translated string, the way the party errors
        already do - it is the only copy that knows WHY - with a local line for the case where
        there is nothing to quote (a dropped request answers 0 and no body)."""
        if status == 200:
            return
        note = str((body or {}).get("error") or "").strip() or t("comp_chat_failed")
        log = self.chat_log(channel)
        # Replace a previous notice rather than stacking: somebody leaning on Enter should get one
        # line that stays put, not a screenful of them.
        if log and log[-1].get("notice"):
            log[-1]["text"] = note
        else:
            self._append_chat(channel, {"steam_id": "", "name": "", "text": note, "notice": True})
        self._changed()

    def _chat_line(self, event):
        """A line the service relayed. `name` is already resolved for US: an anonymised enemy
        arrives as their call sign, because their persona is never put on the wire."""
        channel = str(event.get("channel") or "team")
        channel = channel if channel in CHAT_CHANNELS else "team"
        self._append_chat(channel, {"steam_id": str(event.get("steam_id") or ""),
                                   "name": str(event.get("name") or ""),
                                   "text": str(event.get("text") or "")})
        self._changed()

    def pick_coin(self, side: str):
        if self.stage != "coin" or self.coin_side or not self.i_am_coin_captain() or not self.client:
            return
        self.coin_side = side
        # Start the toss on the press rather than on the reply, so the button feels instant. The
        # SERVER holds `flipping` for its own FLIP_SECONDS and its payload overwrites both of these
        # a round trip later; this is only what the captain sees in between. The clock has to be
        # set with it - a `flipping` stage showing 0 seconds left reads as a coin that has already
        # landed, and the screen would draw a still face on a result nobody has been told yet.
        self.stage = "flipping"
        self.stage_seconds = self.stage_total_seconds = FLIP_SECONDS
        self._start_stage_tick()
        self._changed()
        self._action(lambda client=self.client: client.flip_coin(side))

    def choose(self, kind: str, by: int = None):
        if self.stage != "choice" or not self.i_am_captain_of(self.toss_winner) or not self.client:
            return
        if kind not in ("side", "ban"):
            return
        self._action(lambda client=self.client: client.choose_advantage(kind))

    def choose_side(self, side: str, by: int = None):
        if self.stage != "side" or side not in ("attack", "defend") or not self.client:
            return
        if not self.i_am_captain_of(self.side_picker):
            return
        self._action(lambda client=self.client: client.pick_side(side))

    def ban(self, map_name: str, by: int = None):
        if self.stage != "veto" or not self.client:
            return
        if map_name not in self.remaining_maps() or not self.i_am_captain_of(self.ban_turn):
            return
        self._action(lambda client=self.client: client.ban_map(map_name))

    def start_vote(self):
        if self.phase != "live" or not self.client or not self.match_id or self.vote:
            return
        mid = self.match_id
        self._action(lambda client=self.client: client.void_vote(mid),
                     lambda status, body: self._void_vote_result(mid, status, body))

    def cast_vote(self, yes: bool):
        if (self.phase != "live" or not self.client or not self.match_id or not self.vote
                or self.vote.get("voted") or self.vote.get("pending") or not isinstance(yes, bool)):
            return
        mid = self.match_id
        self._action(lambda client=self.client: client.void_vote(mid, yes),
                     lambda status, body: self._void_vote_result(mid, status, body))

    def _apply_void_vote(self, vote):
        if not isinstance(vote, dict):
            self.vote = None
            return
        current = self.vote or {}
        # A POST response may arrive after a newer SSE tally. Never undo votes on screen.
        if (int(vote.get("yes") or 0) + int(vote.get("no") or 0)
                < int(current.get("yes") or 0) + int(current.get("no") or 0)):
            return
        self.vote = dict(vote)

    def _void_vote_result(self, mid, status, body):
        if self.phase != "live" or self.match_id != mid:
            return
        if status == 200:
            self.error = ""
            if "vote" in body:
                self._apply_void_vote(body["vote"])
        else:
            self.error = t("comp_vote_error")
        self._changed()

    # ---------------------------------------------------------------- events
    def on_live_event(self, event):
        event = player_identity.normalize(event)
        kind = event.get("type")
        if (kind in ("match_result", "match_over", "match_cancelled") and self.match_id
                and event.get("match_id") and event["match_id"] != self.match_id):
            # A leaver's old game can finish after they have entered another one.
            # Its result belongs in history and must never close the current game.
            self.history_stale = True
            return
        if kind == "network_status":
            self._changed()
            return
        if kind == "hello":
            self.connected = True
            if hasattr(getattr(self, "client", None), "messages"):
                self.refresh_messages()
            # THE RANK LADDER comes from the server, because every band is an environment dial
            # (server/rating.cjs ladder()). A hub shipping its own copy would keep explaining the
            # old bands the day one is retuned, and nothing would fail to make that visible.
            ladder = event.get("ladder")
            if isinstance(ladder, dict) and ladder.get("levels"):
                self.ladder = ladder
            # THE PENALTY RULES, on the same terms. `rungs` is the test: a block without the ban
            # ladder cannot answer the one question the screen exists to answer, and half a panel
            # is worse than none.
            penalties = event.get("penalties")
            if isinstance(penalties, dict) and penalties.get("rungs"):
                self.penalties = penalties
            # The stream just (re)connected. If we believe we are queued but the server has
            # been restarted or redeployed, it has forgotten us and we would sit watching a
            # timer climb forever. Ask to be put back; the server ignores a duplicate.
            if self.phase == "queued" and self.client:
                prepare = getattr(self.client, "prepare_queue_join", None)
                action = prepare() if callable(prepare) else self.client.join_queue
                self._action(action, self._requeue_result)
            elif self.locked_in():
                # We believe we are in a match. If the service still has it, it replays it on
                # THIS stream (server/live.cjs handleStream) and those events land within a
                # moment of the hello. If none does, the match is gone - a redeploy, or it
                # ended while we were away - and the alternative to noticing is a dead match
                # screen the player cannot leave until the watchdog fires hours later.
                #
                # No timer of its own: the watchdog already ticks once a second for exactly
                # this kind of deadline, and one more repeating job per reconnect is how a
                # session ends up with two accept countdowns running (bug 3).
                self._match_replay_seen = False
                self._rejoin_deadline = time.monotonic() + REJOIN_GRACE_SECONDS
        elif kind == "message_update":
            self.refresh_messages()
            if self.messages_target and not self.messages_thread_loading:
                self.open_messages(self.messages_target)
        elif kind in ("friend_update", "friend_request"):
            # A nudge, not a payload: the list is a GET and the server is the only thing that
            # knows what the other side did. Mirrors how history uses history_stale.
            self.refresh_friends()
        elif kind == "team_kill_warning":
            self._team_kill_warning(event)
        elif kind == "rating":
            # THE SERVER SENDS THIS THE MOMENT THE STREAM IS UP, and until now nothing read it -
            # so a signed-in player had no rank on screen until they finished a match, because
            # `level` was only ever written by match_result. That is why the hero had a sign-in
            # prompt and an empty badge where the rank belongs.
            if self.me is not None:
                # BOTH SHAPES, because a hub in the field outlives any one server deploy.
                #
                # The server now sends progress.publicProgress here, exactly as it does on
                # match_result: a flat block with `rank` as a plain 1-based integer beside a
                # top-level `rank_name`. It used to send rating.publicRating, whose `rank` was a
                # whole nested block - AND which named the rank the player's matchmaking rating deserved
                # rather than the one they had climbed to, so the hero showed one rank on connect
                # and a different one the moment a match settled.
                #
                # The nested form is only read for a server older than 2026-09-15 (or a rollback
                # to one). Unwrapping is all it needs: the ladder, the numbering and the null
                # division at the capstone are the same either way.
                block = event.get("rank") if isinstance(event.get("rank"), dict) else None
                src = dict(event)
                if block:
                    src.update(block)
                self.me["level"] = _int_or(src.get("level")) if src.get("level") is not None else None
                self.me["rank"] = src.get("rank") or None
                self.me["placing"] = bool(src.get("placing"))
                self.me["placements_left"] = _int_or(src.get("placements_left"))
                # THE LADDER THE SERVER ACTUALLY KEEPS. progress.publicProgress has sent these
                # since the ranks became eight tiers of three divisions, and the hub was dropping
                # every one of them on the floor - so a player finished a ranked match and saw a
                # number from the old 1-14 scale, or nothing at all.
                # THE TOP OF THE LADDER, which the server has to tell us about rather than
                # the hub deducing it: `top` is a SEAT on the leaderboard (the capstone) and
                # `counting` says the figure above has no ceiling, so neither can be worked out
                # from a rank number. Absent on an older server, where False is what they were.
                for key in ("counting", "top", "top_eligible"):
                    self.me[key] = bool(src.get(key))
                for key in ("rank_name", "division", "rr", "bdr"):
                    self.me[key] = (_int_or(src.get(key)) if key != "rank_name"
                                    else (src.get(key) or None))                         if src.get(key) is not None else None
                for key in ("matches", "wins", "losses"):
                    if event.get(key) is not None:
                        self.me[key] = _int_or(event.get(key))
                self._changed()
        elif kind == "stats":
            self.online = int(event.get("online") or 0)
            self.queue_size = int(event.get("queued") or 0)
            # Action responses can arrive after newer stats on the stream. Keep the
            # top-bar total separate from the queue card's position/size updates.
            self.stats_queued = self.queue_size
            # Absent on a server older than the top-bar counts; 0 is what it was then.
            self.live_matches = int(event.get("live_matches") or 0)
            registered = event.get("players_registered")
            self.players_registered = (registered if type(registered) is int
                                       and 0 <= registered < 2**53 else None)
            self.stats_ready = True
        elif kind == "queued":
            if getattr(self, "_cancel_queue_pending", False):
                return  # the serialized leave follows the in-flight join; do not resurrect it
            if self.phase != "queued":
                # entering the queue. This event may arrive before the join POST's 200 (the
                # server writes it to the already-open stream first), so entering here is what
                # starts the countdown; _join_result then just confirms position/size.
                self.phase = "queued"
                self.queue_seconds = 0
                self.queue_position = int(event.get("position") or 0)
                self.queue_size = int(event.get("size") or 0)
                self._start_queue_tick()
            else:
                # already queued: a position/size update. Do NOT reset the clock, and do NOT
                # start a second loop.
                self.queue_position = int(event.get("position") or self.queue_position)
                self.queue_size = int(event.get("size") or self.queue_size)
        elif kind == "unqueued":
            self._cancel_queue_pending = False
            if self.phase == "queued":
                self.phase = "idle"
                if event.get("network_unready"):
                    self.error = str(event.get("error") or "Connection measurements are unavailable.")
        elif kind == "match_found":
            self._match_replay_seen = True
            # A match_found REPLAYED on reconnect while we are already past the accept window
            # (in the client-local lobby, or connecting/live) must be ignored — otherwise it
            # would drag the player back to the accept screen and reset their lobby (bug 3).
            if self.phase in ("lobby", "connecting", "live", "result"):
                return
            self.players = [self._player_from(p) for p in (event.get("players") or [])]
            self.accept_total = max(1, int(event.get("total") or 0) or len(self.players))
            self.accept_left = int(event.get("accept_seconds") or ACCEPT_SECONDS)
            # The server includes who has already accepted so a client that (re)connects mid
            # accept window restores its own state from the server's truth rather than resetting
            # it (bug 3). On a fresh match_found the set is empty, so this is a no-op.
            accepted_ids = set(str(x) for x in (event.get("accepted_ids") or []))
            self.accepted = int(event.get("accepted") or len(accepted_ids) or 0)
            my_id = (self.me or {}).get("steam_id")
            self.i_accepted = bool(my_id) and my_id in accepted_ids
            self.phase = "found"
            # schedule the first tick rather than calling it: the countdown should SHOW the
            # full window for a second, not open already one second down. Idempotent, so a
            # match_found replayed on reconnect does not start a second loop (bug 3).
            self._start_accept_tick()
        elif kind == "match_accept":
            self.accepted = int(event.get("accepted") or 0)
            self.accept_total = max(1, int(event.get("total") or self.accept_total))
        elif kind == "match_ready":
            # The lobby now lives on the SERVER: match_ready carries the teams, the designated
            # captain and the (fresh or resumed) lobby state. Guard against a replay (bug 3): once
            # we are in the lobby (or past it) we re-apply the server state rather than resetting.
            self._match_replay_seen = True
            if self.phase not in ("lobby", "connecting", "live", "result"):
                # A RESUMED one is a lobby already running elsewhere; the server replays its real
                # state so we drop straight back into the right stage rather than waiting blind.
                if event.get("resumed"):
                    self._resume_lobby(event)
                else:
                    self._enter_lobby(event)
            elif self.phase == "lobby":
                self._apply_lobby(event)          # a refresh of the lobby we are already in
                self._changed()
            return
        elif kind == "chat":
            self._chat_line(event)
        elif kind == "lobby":
            self._lobby_advance(event)
            return
        elif kind == "match_connecting":
            self._match_replay_seen = True
            self._on_connecting(event)
            return
        elif kind == "match_connect":
            if event.get("match_id"):
                self.match_id = str(event.get("match_id"))
            self.connected_ids = set(event.get("connected") or [])
            self.connect_total = int(event.get("total") or self.connect_total or len(self.players) or 1)
            self.i_connected = (self.me or {}).get("steam_id") in self.connected_ids
            # HOW LONG THE SERVER SAYS IS LEFT. It re-arms the connect deadline whenever the host's
            # game proves it is on its way, and this is where that reaches the screen - without it
            # the countdown keeps falling towards a zero the server has already moved (see
            # _adopt_connect_seconds).
            self._adopt_connect_seconds(event.get("connect_seconds"))
            # THE RELEASE SIGNAL, and it is the HOST'S GAME rather than anybody's hub. The server
            # sets `stamped` when the host reports in from inside the match world, which is the
            # moment their lobby exists to be found (live.cjs gameReportedIn); a joiner released on
            # `connected` - a claim a hub makes - would spend its one lobby search on an empty
            # Steam. Older servers send no `stamped` field; fall back to the connected roster
            # there, which is the behaviour those servers were written for.
            host_id = (self.host or {}).get("steam_id")
            stamped = (bool(event.get("stamped")) if "stamped" in event
                       else bool(host_id and host_id in self.connected_ids))
            if host_id and stamped:
                # Host-ready OPENS THE GATE; it no longer walks through it (Sam, 2026-09-16). The
                # joiner's game used to be opened right here, which meant Steam took the screen
                # away from a player who had not asked for it. Now this only records that the
                # host's lobby is up and findable - which un-greys "Launch game" and starts the
                # visible 5-minute join window - and `launch_game` does the pak + the launch when
                # the player presses it. The pak-before-launch ordering moved there with it.
                #
                # For the host this still just records that the gate is open (their own countdown
                # is the connect window). _note_host_ready guards itself, so a replayed roster does
                # not restart the clock.
                self._note_host_ready()
        elif kind == "match_teams_wait":
            # Everyone is in, but the GAME has not put them on the lobby's teams yet. The server is
            # holding the start (live.cjs goLiveIfReady) rather than letting a scrambled match run.
            self.teams_status = "wait"
            self._changed()
        elif kind == "match_teams_mismatch":
            # The hold ran out and the server started it anyway - deliberately, because a gate that
            # can never end is worse. Say so instead of letting people wonder about the teams.
            self.teams_status = "mismatch"
            self._changed()
        elif kind == "match_reconnect":
            if event.get("match_id") != self.match_id:
                return
            self.reconnect_waiting = event.get("waiting") or []
            self._changed()
        elif kind == "match_void_vote":
            if self.phase != "live" or event.get("match_id") != self.match_id:
                return
            self._apply_void_vote(event.get("vote"))
            self._changed()
            return
        elif kind == "match_live":
            # the server archives a match the moment it goes live, so what we hold is stale
            self.history_stale = True
            self._match_replay_seen = True
            if event.get("match_id"):
                self.match_id = str(event["match_id"])
            self._apply_void_vote(event.get("vote"))
            self.reconnect_waiting = event.get("reconnect_waiting") or []
            self._update_match_authority(event)
            self._recover_running_game()
            if self.phase == "live":
                self._changed()               # a replay of the screen we are already showing
                return
            if event.get("map"):
                self.map = str(event.get("map"))
            if self.phase in ("lobby", "connecting"):
                self._go_live()
            else:
                self._resume_live(event)      # closed, updated or offline while it started
            return
        elif kind == "penalty":
            seconds = int(event.get("seconds") or 0)
            self.penalty_count = int(event.get("count") or 0)
            self.penalty_next = int(event.get("next_seconds") or NO_SHOW_BAN_SECONDS)
            if seconds > 0:
                self.penalty_until = time.time() + seconds
                self.penalty_reason = str(event.get("reason") or "")
                self._arm_penalty_expiry()
        elif kind == "match_result":
            self._match_replay_seen = True
            self._on_result(event)
            return
        elif kind == "match_cancelled":
            self._match_replay_seen = True
            self.history_stale = True     # a cancelled match is archived too, penalty and all
            self._on_cancelled(event)
            return
        elif kind == "match_over":
            self._match_replay_seen = True
            self.history_stale = True
            if self.phase not in ("idle", "signed_out"):
                self.phase = "idle"
            # The service has closed the books on this match. Whatever data was going to be
            # collected has been, because nothing more will ever arrive for a match the server
            # has finished with - so this is a complete match and the game may go. A
            # `match_result` for the same match usually got here first; the gate is idempotent.
            self.register_match_complete("match_over")
        elif kind == "party_invites":
            # THE WHOLE INBOX, every time, and never a patch: the server prunes expired invites
            # and ones whose party has gone as it builds this, so anything kept locally that is
            # not in the payload is something it has already decided I should not be offered.
            rows = event.get("invites")
            self.party_invites = tuple(r for r in (rows or []) if isinstance(r, dict))
            self.invite_error = ""
        elif kind == "party_invite":
            # The cue that rides alongside the list, so an invite is visible from whichever
            # screen the player is on rather than only on Competitive. The list itself came (or is
            # coming) as party_invites; this adds nothing to it.
            who = (event.get("from") or {}).get("persona") or t("comp_invite_someone")
            self._cue(t("comp_invite_toast", name=who))
        elif kind == "party_update":
            # The one source of truth for party membership. code:null means "you are solo now".
            code = event.get("code")
            pending = self._party_transition
            if pending and ((pending.get("code") and normalise_party_code(code) == pending["code"])
                            or (not pending.get("code") and code and code != pending.get("previous"))):
                self._party_transition = False
            # "Invited" is per-party: a different party (or none) is a different set of empty
            # seats, so the labels from the last one must not survive into it.
            if str(code or "") != str((self.party or {}).get("code") or ""):
                self.invite_sent = ()
            if not code:
                self.party = None
            else:
                self.party = {"code": code,
                              "leader_id": str(event.get("leader_id") or ""),
                              "members": [self._party_member(m) for m in (event.get("members") or [])]}
            present = {m.get("steam_id") for m in (self.party or {}).get("members", [])}
            self.invite_sent = tuple(sid for sid in self.invite_sent if sid not in present)
            self.party_error = ""
        self._changed()

    # ---------------------------------------------------------------- match history
    def load_history(self, force=False):
        """Ask the server for the player's last matches.

        Cached on purpose: the sub-tab is meant to be readable WHILE queued (Sam,
        2026-09-14), so flicking between Play and History must not fire a request every time.
        A match ending sets history_stale, which counts as a reason to re-ask."""
        if not self.client or not self.token:
            # Not connected, so we do not know that there are no matches - only that we cannot
            # ask. Leaving history as None makes the list say that instead of "No matches yet".
            self.history_error = t("comp_history_failed")
            self.history_loading = False
            return
        if self.history_loading:
            return
        if self.history is not None and not force and not self.history_stale:
            return
        self.history_loading = True
        self.history_error = ""
        self._changed()
        self._action(self.client.history, self._history_result)

    def _history_result(self, status, body):
        self.history_loading = False
        self.history_seq = (getattr(self, "history_seq", 0) + 1) % 1000000
        if status == 200 and isinstance(body, dict):
            rows = body.get("matches")
            self.history = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            self.history_error = ""
            self.history_stale = False
        elif status == 401:
            self.history_error = t("comp_history_signed_out")
        else:
            # Keep whatever we already had: a list on screen is better than a blank one, and
            # the error line says plainly that it may be out of date.
            self.history_error = t("comp_history_failed")
        self._changed()

    def load_match(self, match_id, on_done):
        """One match in full, for the detail pop-up."""
        if not self.client or not match_id:
            on_done(None)
            return

        def done(status, body):
            record = body.get("match") if (status == 200 and isinstance(body, dict)) else None
            on_done(record if isinstance(record, dict) else None)

        # Bound on THIS thread: sign-out can clear self.client while the request is in flight,
        # and an AttributeError on the worker would leave the pop-up on "Loading" forever.
        fetch = self.client.history
        self._action(lambda: fetch(match_id), done)

    def _on_connecting(self, event):
        """The window is open. Everything here comes from the server, including how much of it
        is left, so a hub that reconnects halfway through picks up the real clock."""
        was = self.phase
        # The match this payload is about. _maybe_launch_game keys its once-per-match guard on it,
        # so it has to be set BEFORE the launch below, not after.
        if event.get("match_id"):
            self.match_id = str(event.get("match_id"))
        # A hub that (re)connected straight into the connect window has no roster at all: the
        # server sends this payload and nothing else for a match in that state. Without the
        # names it cannot say who is hosting, and the host lookup below would quietly fall
        # back to the player themselves - so take the roster when we have none of our own.
        if not self.players and event.get("players"):
            self.players = [self._player_from(p) for p in event["players"]]
        by_id = {str(p.get("steam_id")): p for p in self.players}
        if event.get("teams"):
            self.teams = {n: [by_id[str(i)] for i in event["teams"].get(str(n), event["teams"].get(n, [])) if str(i) in by_id] for n in (1, 2)}
        for n in (1, 2):
            side = (event.get("sides") or {}).get(str(n), (event.get("sides") or {}).get(n))
            if side in ("attack", "defend"):
                self.sides[n] = side
        self.phase = "connecting"
        self.connect_left = int(event.get("connect_seconds") or CONNECT_SECONDS)
        self.connect_total = int(event.get("total") or len(self.players) or 1)
        self.connected_ids = set(event.get("connected") or [])
        self.i_connected = (self.me or {}).get("steam_id") in self.connected_ids
        if event.get("map"):
            self.map = str(event.get("map"))
        if event.get("no_show_seconds"):
            self.penalty_next = int(event.get("no_show_seconds"))
        host_id = str(event.get("host") or "")
        if host_id:
            self.host = next((p for p in self.players if p.get("steam_id") == host_id),
                             self.host or dict(self.me or {}))
        if self.host and event.get("host_game_steam_id"):
            self.host["game_steam_id"] = str(event["host_game_steam_id"])
        if event.get("stamped") and host_id in self.connected_ids:
            self._note_host_ready()
            self.join_left = self.connect_left
        self._update_match_authority(event)
        self._apply_network(event)
        # The host goes FIRST and alone: nobody else's game may open until this one has stamped
        # CH_MATCH onto its lobby, because their single search would find nothing and be spent.
        if self._i_am_host():
            token = str(event.get("report_token") or "")
            # The service generates 32 random bytes and sends them only on the authenticated
            # host's payload. Refuse a malformed field rather than baking arbitrary text into an
            # FName or silently launching a client the ranked route will reject.
            self.report_token = (token if len(token) == 64 and
                                 all(ch in "0123456789abcdef" for ch in token) else "")
            if self._prepare_launch():
                self._maybe_launch_game("host")
        self._changed()
        if was != "connecting":
            self._later(1000, self._tick_connect)

    def _update_match_authority(self, event):
        """Refresh host role on live replays without restarting an open game."""
        host_id = str(event.get("host") or "")
        old_host = str((self.host or {}).get("steam_id") or "")
        if host_id:
            self.host = next((p for p in self.players if p.get("steam_id") == host_id),
                             {"steam_id": host_id, "game_steam_id": event.get("host_game_steam_id", "")})
        token = str(event.get("migration_token") or "")
        if len(token) == 64 and all(c in "0123456789abcdef" for c in token):
            self.migration_token = token
        if self._i_am_host():
            self.report_token = self.migration_token or self.report_token
        self.host_epoch = int(event.get("host_epoch") or 0)
        if old_host and host_id and old_host != host_id:
            # The next launch must build for the new role. Never replace a mounted
            # pak: relaunch_game already checks that the process has exited.
            self.pak_done = False
            self.host_pak_done = False
            self.host_level = ""
            self.launched = False
            self.launched_for = ""

    def _on_result(self, event):
        """The match is over and the service has the scoreboard (docs/match-result.md hop 2).

        This is the ending the whole close-the-game feature is really waiting for: a
        `match_result` means the host's report reached the service, was checked and was accepted,
        so the match data IS collected. Everything here writes it down first and registers the
        match complete last, in that order, because that order is the promise - the game is only
        ever closed after this hub has the match.

        The payload is mapped onto the result dict the screen already draws. `score` arrives BY
        TEAM NUMBER and is turned into ours:theirs here, because only this client knows which side
        of it the player was on. `arrows` is the rank move below level 10; above it the service
        sends a real `bdr_delta` instead, and the screen picks whichever it was handed. `rr_delta`
        is the RR the match moved at every rank, and it is the number the web cards print. The full
        payload is kept on the session so the scoreboard screen can be built against it without
        another round trip."""
        self.history_stale = True
        self.vote = None
        self.result_payload = dict(event) if isinstance(event, dict) else {}
        # `you` is the documented shape (docs/match-result.md hop 2). A service that sends the
        # rank FLAT instead is read the same way rather than dropped on the floor: the fields are
        # identical either way, and the difference decided whether the badge moved at all.
        you = event.get("you")
        if not isinstance(you, dict) or not you:
            you = event if isinstance(event, dict) else {}
        voided = bool(event.get("voided"))
        if voided:
            self.result = {"won": False, "score": (0, 0), "delta": 0, "voided": True,
                           "reason": str(event.get("void_reason") or "")}
        else:
            mine = self.my_team() or 1
            raw_score = event.get("score") or (0, 0)
            score = ([raw_score.get("1", raw_score.get(1, 0)), raw_score.get("2", raw_score.get(2, 0))]
                     if isinstance(raw_score, dict) else list(raw_score)[:2])
            while len(score) < 2:
                score.append(0)
            ours, theirs = (score[0], score[1]) if mine == 1 else (score[1], score[0])
            winner = _int_or(event.get("winner"))
            # DID WE WIN, ASKED OF WHICHEVER FIELD THE SERVICE SENT. `winner` is the team number
            # the contract specifies (docs/match-result.md hop 2); `won` is this player's own
            # answer, and it is what live.cjs settles a match with today. Deriving it from
            # `winner` alone is False for everybody when no winner is on the wire - ten people
            # told they lost the same match - so the explicit flag wins where there is one.
            won_flag = event.get("won")
            won = won_flag if isinstance(won_flag, bool) else (winner in (1, 2) and winner == mine)
            # arrows below level 10, a real BDR move from level 10 up - never both, and the
            # service decides which by what it sends (docs/match-result.md, "the one rule in
            # the wire itself"). No threshold for this end to get wrong.
            # ...and the flat shape calls the arrows `delta`, which is the last thing tried
            # so a service sending only that still moves the card.
            delta = (you.get("bdr_delta") if you.get("bdr_delta") is not None
                     else you.get("arrows") if you.get("arrows") is not None
                     else event.get("delta")) or 0
            # THE RR THIS MATCH MOVED. Sam, 2026-09-16: "we are only gaining and losing 1-3 RR" -
            # the cards printed `delta` above, an arrow count, with "RR" after it. None from a
            # service too old to send it, so nothing downstream mistakes arrows for RR again.
            rr_delta = you.get("rr_delta") if you.get("rr_delta") is not None else event.get("rr_delta")
            # The match that FINISHED placements pays no RR either; it hands out a rank, and a card
            # should say which rather than "no rank change".
            placed_rank = ""
            if you.get("placed") and you.get("rank_name"):
                placed_rank = str(you.get("rank_name"))
                if you.get("division") is not None:
                    placed_rank += " %d" % _int_or(you.get("division"))
            self.result = {"won": won, "score": (ours, theirs),
                           "delta": _int_or(delta), "voided": False,
                           "rr_delta": None if rr_delta is None else _int_or(rr_delta, None),
                           "placing": bool(you.get("placing")),
                           "placed": bool(you.get("placed")),
                           "placements_left": _int_or(you.get("placements_left")),
                           "placed_rank": placed_rank}
            if you.get("level") is not None and self.me:
                self.me["level"] = _int_or(you.get("level")) or self.me.get("level")
            # the visible rank moves with the matchmaking rating, so a finished match is exactly when
            # the badge should change
            if you.get("rank") is not None and self.me:
                self.me["rank"] = you.get("rank") or None
            # ...and everything else the badge draws. A match result is exactly when a division
            # changes or a promotion lands, so dropping these here left the hero showing the rank
            # a player had BEFORE the match that just moved them.
            if self.me:
                for key in ("rank_name", "division", "rr", "bdr", "placing", "placements_left"):
                    if you.get(key) is None:
                        continue
                    if key == "rank_name":
                        self.me[key] = you.get(key) or None
                    elif key == "placing":
                        self.me[key] = bool(you.get(key))
                    else:
                        self.me[key] = _int_or(you.get(key))
                # THE CAPSTONE, which is the one rank whose fields go the other way: taking a
                # seat NULLS the division, and `you.get("division") is None` is skipped by the
                # loop above precisely because a missing field must not wipe a good one. Without
                # this a player who just took a Reaper seat wore "Reaper III" until they
                # restarted the hub. Only when the server said so - a payload with no `top` at
                # all is an older service, and nothing here should start guessing.
                if you.get("top") is not None:
                    self.me["top"] = bool(you.get("top"))
                    if self.me["top"]:
                        self.me["division"] = None
                if you.get("counting") is not None:
                    self.me["counting"] = bool(you.get("counting"))
                if you.get("top_eligible") is not None:
                    self.me["top_eligible"] = bool(you.get("top_eligible"))
            if you.get("matches") is not None and self.me:
                self.me["matches"] = _int_or(you.get("matches"))
            if you.get("wins") is not None and self.me:
                self.me["wins"] = _int_or(you.get("wins"))
        if event.get("map"):
            self.map = str(event.get("map"))
        # THE CARD, raised here and taken down by nobody but the player. Built before the phase
        # moves, while the teams, the sides and the veto this match was played with are still on
        # the session (see _postmatch_record).
        self._open_postmatch()
        self.phase = "result"
        # Written down. NOW the game may close.
        self.register_match_complete("match_result")
        self._changed()

    def _on_cancelled(self, event):
        """A match died. WHY decides what the player is told and what it cost them.

        Only `penalty` being present means this player is the one who did something: the
        server sends it to nobody else, which is Sam's rule that a no-show costs the no-show
        and nobody else a thing."""
        reason = str(event.get("reason") or "")
        penalty = event.get("penalty") or None
        requeued = bool(event.get("requeued"))
        self.reset_match()
        if penalty:
            seconds = int(penalty.get("seconds") or NO_SHOW_BAN_SECONDS)
            self.penalty_until = time.time() + seconds
            self.penalty_reason = str(penalty.get("reason") or "no_show")
            self._arm_penalty_expiry()
            self.penalty_count = int(penalty.get("count") or self.penalty_count + 1)
            self.penalty_next = int(penalty.get("next_seconds")
                                    or no_show_ban_seconds(self.penalty_count + 1))
            self.error = t("comp_no_show_me", elo=int(penalty.get("elo") or NO_SHOW_ELO),
                           time=format_duration(seconds))
        elif reason == "no_show":
            self.error = t("comp_no_show_others")
        elif reason == "stalled":
            self.error = t("comp_match_stalled")
        else:
            self.error = t("comp_declined")
        # Written down - why it died, what it cost, where the player goes next. NOW the game may
        # close: a match that died is as finished as one that was played (no report is coming for
        # it, ever) and the player still has a Bodycam open that they cannot queue with.
        # reset_match has already run above, which is why the close's own state is scoped to the
        # launch rather than to the match (_arm_game_close).
        self.register_match_complete("cancelled")
        if requeued:
            # the server put us back at the front of the queue, so the tab must agree with it
            self.phase = "queued"
            self.queue_seconds = 0
            self._changed()
            self._start_queue_tick()
            return
        self.phase = "idle"
        self._changed()

    # ---------------------------------------------------------------- friends
    def refresh_friends(self):
        """Ask the server for the list. Runs off the UI thread; the answer lands back on it."""
        if not self.client or self.friends_loading:
            return
        self.friends_loading = True
        self._changed()
        self._action(self.client.friends, self._friends_result)

    def _friends_result(self, status, body):
        self.friends_loading = False
        if status != 200 or not isinstance(body, dict) or not body.get("ok"):
            self.friends_error = str((body or {}).get("error") or "")
            self._changed()
            return
        self.friends_error = ""
        self.friends = tuple(body.get("friends") or ())
        self.friend_requests_in = tuple(body.get("incoming") or ())
        self.friend_requests_out = tuple(body.get("outgoing") or ())
        self.friend_code = str(body.get("code") or "")
        self.friends_seq += 1
        self._changed()

    def add_friend(self, code_or_id=""):
        """Send a request. A CODE is what a player types; an id is what a button next to someone
        already on screen sends. The server takes either and tells them apart."""
        text = str(code_or_id or "").strip()
        if not self.client or not text:
            return
        is_id = text.isdigit() and len(text) == 17
        self._action(
            lambda client=self.client: client.friend_request(code="" if is_id else text,
                                               target=text if is_id else ""),
            self._friend_action_result)

    def _friend_action_result(self, status, body):
        # Every friend verb refetches rather than patching the local copy: the server is the only
        # thing that knows what the other side did, and a list assembled from replies drifts.
        if status != 200:
            self.friends_error = str((body or {}).get("error") or "")
            self._changed()
            return
        self.friends_error = ""
        self.refresh_friends()

    def accept_friend(self, steam_id):
        if self.client and steam_id:
            self._action(lambda client=self.client: client.friend_accept(str(steam_id)), self._friend_action_result)

    def decline_friend(self, steam_id):
        if self.client and steam_id:
            self._action(lambda client=self.client: client.friend_decline(str(steam_id)), self._friend_action_result)

    def cancel_friend(self, steam_id):
        if self.client and steam_id:
            self._action(lambda client=self.client: client.friend_cancel(str(steam_id)), self._friend_action_result)

    def remove_friend(self, steam_id):
        if self.client and steam_id:
            self._action(lambda client=self.client: client.friend_remove(str(steam_id)), self._friend_action_result)

    def refresh_friend_code(self):
        """A new code. The old one stops working the moment this returns."""
        if self.client:
            self._action(self.client.friend_code_refresh, self._friend_action_result)

    def toggle_friend_code_hidden(self):
        self.friend_code_hidden = not self.friend_code_hidden
        self._changed()

    # ---------------------------------------------------------------- one match, in full
    def open_match(self, match_id):
        """Open a match from the history list and fetch its record.

        The row the list already has is a SUMMARY - who won, what map, what it cost. The detail is
        a separate GET because the record carries the whole roster, the veto and the round
        timeline, and pushing all of that into every history row would make the list heavy for the
        one row in fifty that gets opened.
        """
        mid = str(match_id or "")
        if not mid or not self.client:
            return
        if self.match_detail_id == mid and self.match_detail:
            return                                   # already open: reopening must not refetch
        self.match_detail_id = mid
        self.match_detail = None
        self.match_detail_error = ""
        self.match_detail_loading = True
        self._changed()
        self._action(lambda client=self.client: client.history(mid),
                     lambda st, bd: self._match_detail_result(mid, st, bd))

    def _match_detail_result(self, match_id, status, body):
        # A late answer for a match the player has already closed, or moved on from, must not
        # reopen it under them.
        if self.match_detail_id != match_id:
            return
        self.match_detail_loading = False
        if status != 200 or not isinstance(body, dict):
            self.match_detail_error = str((body or {}).get("error") or "")
            self._changed()
            return
        self.match_detail_error = ""
        self.match_detail = body.get("match") or body
        self._changed()

    def close_match(self):
        self.match_detail_id = ""
        self.match_detail = None
        self.match_detail_error = ""
        self._changed()

    # ---------------------------------------------------------------- leaderboard
    def refresh_messages(self):
        if not self.client or not self.me or self.messages_loading:
            return
        self.messages_loading = True
        self._action(self.client.messages, self._messages_result)

    def _messages_result(self, status, body):
        self.messages_loading = False
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            self.messages_data = body
            self.messages_error = ""
        else:
            self.messages_error = "unavailable"
        self._changed()

    def open_messages(self, target, before=None):
        if not self.client or not self.me:
            return
        target = str(target)
        self.messages_generation += 1
        generation = self.messages_generation
        if target != self.messages_target:
            self.messages_thread = None
        self.messages_target = target
        self.messages_thread_loading = True
        self.messages_error = ""
        self._changed()
        def result(status, body):
            if generation != self.messages_generation or target != self.messages_target:
                return
            self.messages_thread_loading = False
            if status == 200 and isinstance(body, dict) and body.get("ok"):
                old = (self.messages_thread or {}).get("messages", [])
                fresh = body.get("messages", [])
                if old and fresh and (before or fresh[0]["seq"] <= old[-1]["seq"] + 1):
                    combined = {m["seq"]: m for m in old + fresh}
                    newest = max(combined)
                    body["messages"] = [combined[k] for k in sorted(combined) if k > newest - 500]
                    if not before:
                        body["next_before"] = self.messages_thread.get("next_before")
                self.messages_thread = body
            else:
                self.messages_error = "unavailable"
            self._changed()
        self._action(lambda client=self.client: client.message_thread(target, before), result)

    def send_private_message(self, text):
        if not self.client or not self.me or self.messages_sending:
            return
        text = str(text).strip()
        target = self.messages_target
        if not target or not text or len(text) > 1000:
            return
        import uuid
        pending_by_target = self.messages_pending or {}
        pending = pending_by_target.get(target)
        if not pending or pending[:2] != (target, text):
            pending = (target, text, str(uuid.uuid4()))
            pending_by_target[target] = pending
            self.messages_pending = pending_by_target
        self.messages_sending = True
        self.messages_error = ""
        self._changed()
        def result(status, body):
            self.messages_sending = False
            if status == 200 and isinstance(body, dict) and body.get("ok"):
                self.messages_send_seq += 1
                self.messages_sent = {"target": target, "text": text, "id": pending[2]}
                pending_by_target.pop(target, None)
                self.messages_pending = pending_by_target or None
                if self.messages_target == target:
                    self.open_messages(target)
                self.refresh_messages()
            elif self.messages_target == target:
                code = body.get("error") if isinstance(body, dict) else ""
                self.messages_error = code if code in ("friends_only", "blocked", "rate_limited", "inbox_full") else "unavailable"
            self._changed()
        self._action(lambda client=self.client: client.send_message(*pending), result)

    def mark_messages_read(self, target, through_seq):
        if self.client and self.me:
            self._action(lambda client=self.client: client.read_messages(str(target), int(through_seq)), lambda status, body: self.refresh_messages() if status == 200 else None)

    def block_message_player(self, target, blocked=True):
        if not self.client or not self.me:
            return
        def result(status, body):
            if status == 200 and isinstance(body, dict) and body.get("ok"):
                self.refresh_friends()
                if self.messages_target == target:
                    self.open_messages(target)
            elif self.messages_target == target:
                self.messages_error = "unavailable"
                self._changed()
        self._action(lambda client=self.client: client.block_messages(str(target), bool(blocked)), result)

    def refresh_tournament(self):
        if not self.client or not self.me or self.tournament_loading:
            return
        self.tournament_loading = True
        self._changed()
        self._action(self.client.tournament, self._tournament_result)

    def _tournament_result(self, status, body):
        self.tournament_loading = False
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            self.tournament_data = body
            self.tournament_received = time.monotonic()
            self.tournament_error = ""
        else:
            self.tournament_error = "unavailable"
        self._changed()

    def register_tournament(self):
        if not self.client or not self.me or self.tournament_loading:
            return
        self.tournament_loading = True
        self.tournament_error = ""
        self._changed()
        self._action(self.client.register_tournament, self._tournament_registered)

    def _tournament_registered(self, status, body):
        self.tournament_loading = False
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            self.refresh_tournament()
        else:
            code = body.get("error") if isinstance(body, dict) else ""
            self.tournament_error = code if code in ("ended", "identity_conflict", "identity_required", "capacity") else "unavailable"
            self._changed()

    def send_tournament_support(self, category, match_id, message):
        if not self.client or not self.me or self.tournament_loading:
            return
        self.tournament_loading = True
        self.tournament_error = ""
        self._changed()
        self._action(lambda client=self.client: client.tournament_support(str(category), str(match_id), str(message)), self._tournament_ticket_result)

    def _tournament_ticket_result(self, status, body):
        if status == 200 and isinstance(body, dict) and body.get("ok"):
            self.tournament_ticket_seq += 1
        self._tournament_registered(status, body)

    def refresh_leaderboard(self):
        if not self.client or self.board_loading:
            return
        self.board_loading = True
        self._changed()
        self._action(self.client.leaderboard, self._board_result)

    def _board_result(self, status, body):
        self.board_loading = False
        if status != 200 or not isinstance(body, dict) or not body.get("ok"):
            self.board_error = str((body or {}).get("error") or "")
            self._changed()
            return
        self.board_error = ""
        # `available` is the SERVER's word, not ours. It says False when it has no index to read -
        # and an empty board and an unavailable one must not look the same, or the screen claims
        # nobody is ranked when the truth is that it could not ask.
        self.board_available = bool(body.get("available"))
        self.board_rows = tuple(body.get("rows") or ())
        self.board_you = body.get("you") or None
        self.board_seq += 1
        self._changed()

    # ---------------------------------------------------------------- reports
    def report_player(self, steam_id, reason, match_id="", note=""):
        """Report someone. The result is deliberately thin - see live.cjs reportPlayer for why the
        reporter is never told how many reports the target has."""
        if not self.client or not steam_id or not reason:
            return
        self._action(lambda client=self.client: client.report(str(steam_id), str(reason), str(match_id or ""), str(note or "")),
                     self._report_result)

    def _report_result(self, status, body):
        if status == 200 and (body or {}).get("ok"):
            self.report_sent = True
            self.report_error = ""
            self.report_target = ""
        else:
            self.report_sent = False
            self.report_error = str((body or {}).get("error") or i18n.tr(i18n.get_language(), "comp_report_failed"))
        self._changed()

    def open_report(self, steam_id, match_id="", name=""):
        """Open the reason picker against `steam_id`. `name` is who they are, as the surface that
        opened it already had them on screen - see `report_name`."""
        self.report_target = str(steam_id or "")
        self.report_seq = getattr(self, "report_seq", 0) + 1
        self.report_match = str(match_id or "")
        self.report_name = str(name or "")
        self.report_sent = False
        self.report_error = ""
        self._changed()

    def close_report(self):
        self.report_target = ""
        self.report_name = ""
        self._changed()

    # ---------------------------------------------------------------- bug reports
    def set_bug_text(self, text=""):
        """Remember what is in the box, so leaving the screen does not throw away a half-written
        report. The page syncs it on blur, not per keystroke, and this stays silent unless it has
        something to take back: a redraw here for every draft update is a redraw fighting the
        cursor for no gain."""
        self.bug_text = str(text or "")[:BUG_TEXT_MAX]
        # A new sentence means they are writing a second report, not still looking at the last
        # one's receipt. Clearing it here (rather than on send) is what makes "Sent" disappear
        # the moment it stops being true.
        if self.bug_sent or self.bug_error:
            self.bug_sent = False
            self.bug_error = ""
            self._changed()

    def send_bug_report(self, text=None):
        """File a bug. Refuses an empty box, a signed-out hub and a second report inside
        BUG_COOLDOWN_SECONDS - see that constant for why the guard is here and not in the page."""
        if text is not None:
            self.bug_text = str(text or "")[:BUG_TEXT_MAX]
        body = self.bug_text.strip()
        if self.bug_sending:
            return                       # already in flight; a double click must not send two
        if not body:
            self.bug_error = "empty"
            self.bug_sent = False
            self._changed()
            return
        if not self.client or not self.me:
            self.bug_error = "signed_out"
            self.bug_sent = False
            self._changed()
            return
        left = self.bug_cooldown_left()
        if left > 0:
            self.bug_error = "too_fast"
            self.bug_sent = False
            self._changed()
            # Re-emit when the wait is over: the snapshot is built on change, not on a clock, so
            # without this the page would sit on "wait 3s" until something else moved.
            self._later(int(left * 1000) + 50, self._changed)
            return
        self.bug_sending = True
        self.bug_error = ""
        self.bug_sent = False
        self._changed()
        self._action(lambda client=self.client: client.bug_report(body), self._bug_result)

    def _bug_result(self, status, body):
        self.bug_sending = False
        if status == 200 and (body or {}).get("ok"):
            self.bug_last_sent = time.monotonic()
            self.bug_sent = True
            self.bug_error = ""
            self.bug_text = ""           # filed: the box is empty for the next one
            self.bug_seq += 1            # ...and the page gets a NEW box (see bug_seq)
            self._changed()
            self._later(int(BUG_COOLDOWN_SECONDS * 1000) + 50, self._changed)
            return
        error = str((body or {}).get("error") or "")
        if status == 429 or error == "too_fast":
            # The server disagrees with our clock about the cooldown (a restart cleared ours, or
            # the same account filed one from a second machine). Its answer wins: adopt the wait
            # it gives us rather than offering a button that will be refused again.
            wait_ms = int((body or {}).get("retry_after_ms") or BUG_COOLDOWN_SECONDS * 1000)
            self.bug_last_sent = time.monotonic() - max(0.0, BUG_COOLDOWN_SECONDS - wait_ms / 1000.0)
            self.bug_error = "too_fast"
            self._changed()
            self._later(wait_ms + 50, self._changed)
            return
        # Anything else keeps the text: it is the only copy of what they wrote.
        self.bug_error = error or "failed"
        self._changed()

    # ---------------------------------------------------------------- coming back to a match
    # Three ways in, one promise: the hub was shut, the updater restarted it, or the network
    # went. In all three the MATCH never lived in this process - the service holds it, keeps
    # the player in it, and replays it the moment a stream comes back (server/live.cjs
    # handleStream). These take that replay and put the player back where they were.

    def _resume_lobby(self, event):
        """Back into a match whose LOBBY is still running.

        The lobby lives on the SERVER now, so the resumed match_ready carries its real state -
        the teams, the designated captain, the toss and whatever has been banned - and we drop
        straight back into the right stage instead of waiting blind for match_connecting."""
        players = [self._player_from(p) for p in (event.get("players") or [])]
        if players:
            self.players = players
        self.phase = "lobby"
        self.i_accepted = True          # the match exists, so we accepted it before we left
        self.rejoined = True
        self.error = ""
        self._apply_lobby(event)
        # No lobby state at all (an old server, or the payload was empty): fall back to the
        # visible wait, ended by match_connecting, rather than an empty coin screen.
        if not self.stage or self.stage not in ("coin", "flipping", "choice", "side", "veto", "ready"):
            self.stage = "rejoin"
        self._changed()

    def _recover_running_game(self):
        """Reattach cleanup to Bodycam after the server restores our active match.

        This only observes an existing process. Reopening the hub must never itself launch
        a game or repeat the auto-join sequence for a match already being played.
        """
        if self.match_id and not self._game_ours and game_mod.game_running():
            self._game_launch_at = None
            self._take_game_ownership()
            self.launched_for = self.match_id
            self.game_was_open = True

    def _resume_live(self, event):
        """Back into a match that is ALREADY being played.

        Everything the lobby decided is taken off the server's replay, because none of it
        exists in this process. Only what the server does not know - levels and pings - is
        invented, exactly the way _player_from always invents it."""
        players = [self._player_from(p) for p in (event.get("players") or [])]
        if players:
            self.players = players
        by_id = {str(p.get("steam_id")): p for p in self.players}
        teams = event.get("teams") or {}
        restored = {n: [by_id[str(i)] for i in (teams.get(str(n)) or []) if str(i) in by_id]
                    for n in (1, 2)}
        if restored[1] or restored[2]:
            self.teams = restored
        sides = event.get("sides") or {}
        for n in (1, 2):
            if sides.get(str(n)) in ("attack", "defend"):
                self.sides[n] = sides[str(n)]
        self.bans = [(_int_or(b.get("team")), str(b.get("map")))
                     for b in (event.get("bans") or [])
                     if isinstance(b, dict) and b.get("map")]
        self.host = by_id.get(str(event.get("host") or "")) or self.host or dict(self.me or {})
        if self._i_am_host():
            token = str(event.get("report_token") or "")
            if len(token) == 64 and all(ch in "0123456789abcdef" for ch in token):
                self.report_token = token
        self._update_match_authority(event)
        self._apply_network(event)
        # The match is live, which by definition means every one of them reported in.
        self.connect_total = len(self.players) or self.connect_total
        self.connected_ids = set(by_id)
        self.i_connected = True
        self.i_accepted = True
        self.host_ready = True
        self.phase = "live"
        self.error = ""
        self.rejoined = True
        self._changed()

    def _watch_tick(self):
        """The shared watchdog, plus the one deadline only a live session can be waiting on."""
        if self._pending_lobby_cleanup and not game_mod.game_running_cached(game_mod.SNAPSHOT_TTL_SECONDS):
            for game in tuple(self._pending_lobby_cleanup):
                if game == self._lobby_pak_dir and (self.host_pak_done or self.pak_done):
                    continue
                try:
                    if lobbypak_mod.remove(game):
                        self._pending_lobby_cleanup.discard(game)
                except Exception:
                    pass
        self._check_rejoin_deadline()
        super()._watch_tick()

    def _check_rejoin_deadline(self):
        """Has the grace after a reconnect run out with no match handed back?"""
        if not self._rejoin_deadline or time.monotonic() < self._rejoin_deadline:
            return
        if self._match_replay_seen or not self.locked_in() or not self.connected:
            # Answered, over, or dropped again before the replay could arrive. In the last
            # case the next `hello` starts a fresh grace, so nothing is decided here.
            self._rejoin_deadline = 0.0
            return
        self._rejoin_gave_up()

    def _rejoin_gave_up(self):
        """The grace after a `hello` ran out with no match event on the stream.

        So the service does not have the match we think we are in: it was restarted, or the
        match ended while we were away. Nobody did anything wrong here - this is our side
        losing the thread, the same as on_stalled - so it costs nothing and says so."""
        self._rejoin_deadline = 0.0
        self.reset_match()
        self.phase = "idle" if self.me else "signed_out"
        self.error = t("comp_match_stalled")
        self._changed()

    def _requeue_result(self, status, body):
        """Re-joining after a reconnect. A refusal here means the queue really is gone."""
        if self.phase != "queued":
            return
        if status == 200:
            self.queue_position = int(body.get("position") or self.queue_position)
            self.queue_size = int(body.get("size") or self.queue_size)
            # If the server had forgotten us (a restart) it now knows us again but sent no
            # `queued` event of its own; make sure the countdown is running either way.
            self._start_queue_tick()
        elif status not in self.RETRY_STATUSES:
            self.phase = "idle"
            self.error = t("comp_queue_failed", reason=str(body.get("error") or status))
        self._changed()

    def _player_from(self, entry):
        """Keep unavailable rank and latency values unknown until reported by the server."""
        entry = player_identity.normalize(entry)
        steam_id = str(entry.get("steam_id") or "")
        persona = str(entry.get("persona") or "")
        return {"name": persona or steam_id, "steam_id": steam_id, "player_id": steam_id,
                "game_steam_id": player_identity.native_id(entry),
                # The Steam avatar URL, straight through. Nothing here fetches it: the web UI asks
                # the hub's own bridge for the picture (hub/webui/httpbridge.py /avatar), which is
                # the only place that is allowed to go to Steam for one.
                "avatar": str(entry.get("avatar") or ""),
                "level": entry.get("level"), "ping": entry.get("ping")}

    # ---------------------------------------------------------------- party
    # Server-authoritative. Each of these calls the service and sets NOTHING locally except a
    # fast-fail error; the roster (and the code) arrive on the stream as a party_update, which
    # on_live_event turns into self.party. The mock fabricated all of this locally — the live
    # session must never, or two members would disagree about who is in the party.
    def create_party(self):
        if self.party or not self.me or self.phase not in ("idle",):
            return
        self.party_error = ""
        self._changed()
        if not self.client:
            self.party_error = t("comp_party_failed")
            self._changed()
            return
        self._action(self.client.create_party, self._party_result)

    def join_party(self, code):
        if self._party_transition:
            return
        from .activity import files_busy
        if files_busy(self.panel):
            self.party_error = t("comp_files_busy")
            self._changed()
            return
        if not self.me or self.phase != "idle":
            return
        code = normalise_party_code(code)
        if not code:
            # Instant feedback with no round trip, exactly as the mock does.
            self.party_error = t("comp_party_bad_code")
            self._changed()
            return
        if self.party and code == self.party["code"]:
            self.party_error = t("comp_party_own_code")
            self._changed()
            return
        self.party_error = ""
        self._changed()
        if not self.client:
            self.party_error = t("comp_party_failed")
            self._changed()
            return
        self._party_transition = {"code": code, "previous": (self.party or {}).get("code")}
        self._changed()
        self._action(lambda client=self.client: client.join_party(code), self._party_join_result)

    def _party_join_result(self, status, body):
        if status not in (200, 201, 204):
            self._party_transition = False
        self._party_result(status, body)

    def leave_party(self):
        # The roster clears when the server's {code:null} party_update arrives, not here.
        if not self.party or not self.client:
            return
        self._action(self.client.leave_party, self._party_result)

    def refresh_party_code(self):
        # Leader only: the guard mirrors the mock. The new code arrives on the stream, so unlike
        # the mock this CANNOT return it synchronously — callers must not depend on the return
        # value (the panel un-hides on the party_update instead; see _new_party_code).
        if not self.party or not self.is_party_leader() or not self.client:
            return None
        self.party_error = ""
        self._action(self.client.refresh_party_code, self._party_result)
        return None

    # ---------------------------------------------------------------- party invites
    # Server-authoritative in the same way the party is: the inbox arrives on the stream
    # (`party_invites`) and nothing here writes it. The one piece of local state is `invite_sent`,
    # which is cosmetic - it turns the button next to a friend I have just invited into "Invited"
    # so I do not send four in a row while they alt-tab.
    def invite_to_party(self, steam_id):
        """Offer a friend the seat next to me. Requires a party: the button only exists once one
        has been created, and the server refuses it anyway."""
        target = str(steam_id or "")
        if not self.client or not target or not self.party:
            return
        self.invite_error = ""
        if target not in self.invite_sent:
            self.invite_sent = self.invite_sent + (target,)
        self._changed()
        self._action(lambda client=self.client: client.invite_to_party(target),
                     lambda status, body: self._invite_result(status, body, target))

    def _invite_result(self, status, body, target=""):
        if status == 200:
            epoch, code = self._account_epoch, (self.party or {}).get("code")
            marker = object()
            self._invite_generation[target] = marker
            def expire():
                if (epoch == self._account_epoch and (self.party or {}).get("code") == code
                        and self._invite_generation.get(target) is marker):
                    self._invite_generation.pop(target, None)
                    self.invite_sent = tuple(i for i in self.invite_sent if i != target)
                    self._changed()
            self._later(max(1, int((body or {}).get("expires_in") or 120)) * 1000, expire)
            return
        # It did not go: take the "Invited" back, or the row lies about what happened.
        self.invite_sent = tuple(i for i in self.invite_sent if i != target)
        self.invite_error = str((body or {}).get("error") or "") or t("comp_invite_failed")
        self._changed()

    def invite_friend_to_party(self, steam_id):
        """Invite a friend from the FRIENDS list, where there may be no party yet.

        Sam, 2026-09-16: "if the inviter isnt already in a party, create a party and then send
        the invite." So this is one action with two halves, and the second half must not wait for
        the stream: the party_update that tells the rest of the hub about the new party is a
        broadcast, and hanging the invite off it would mean racing a socket to find out whether
        the thing we just created exists. The CREATE'S OWN 200 is the party (handlePartyCreate
        answers with the code, and is idempotent), so that answer is what the invite hangs off.

        The Competitive screen's picker still calls invite_to_party directly - it only exists
        once there is a party, so it has nothing to create.
        """
        target = str(steam_id or "")
        if not self.client or not target:
            return
        if self.party:
            self.invite_to_party(target)
            return
        # No party, and no party can be made from here while queued or in a match - the same
        # guard create_party makes, checked here so we do not fire a POST to be refused.
        if not self.me or self.phase != "idle":
            return
        self.invite_error = ""
        self.party_error = ""
        # Mark the row NOW, not after the create: two round trips is long enough for an
        # impatient second click, and the second invite is the one the server rate-limits.
        if target not in self.invite_sent:
            self.invite_sent = self.invite_sent + (target,)
        self._changed()
        self._action(self.client.create_party,
                     lambda status, body: self._party_then_invite(status, body, target))

    def _party_then_invite(self, status, body, target):
        """The party exists (or does not). Either way this is still the INVITE the player asked
        for, so a failed create is reported as a failed invite - the party card they never asked
        to see is not where they are looking."""
        if status not in (200, 201):
            self._invite_result(status, body, target)
            return
        self._action(lambda client=self.client: client.invite_to_party(target),
                     lambda s, b: self._invite_result(s, b, target))

    def accept_party_invite(self, steam_id):
        """Take the seat. The roster lands as a party_update; the inbox as a party_invites."""
        if self._party_transition:
            return
        from .activity import files_busy
        if files_busy(self.panel):
            self.invite_error = t("comp_files_busy")
            self._changed()
            return
        if not self.client or not str(steam_id or ""):
            return
        self.invite_error = ""
        self._party_transition = {"previous": (self.party or {}).get("code")}
        self._changed()
        self._action(lambda client=self.client: client.accept_party_invite(str(steam_id)),
                     self._party_invite_join_result)

    def _party_invite_join_result(self, status, body):
        if status != 200:
            self._party_transition = False
        self._invite_answer_result(status, body)

    def decline_party_invite(self, steam_id):
        if not self.client or not str(steam_id or ""):
            return
        self._action(lambda client=self.client: client.decline_party_invite(str(steam_id)),
                     self._invite_answer_result)

    def _invite_answer_result(self, status, body):
        if status == 200:
            return
        # The usual refusals are "That invite has expired." and "That party is gone." — both are
        # the server telling us our copy of the inbox is stale, and both arrive with a fresh
        # party_invites push, so there is nothing to do here but say what happened.
        self.invite_error = str((body or {}).get("error") or "") or t("comp_invite_failed")
        self._changed()

    def _party_result(self, status, body):
        """A party POST answered. On 2xx do nothing — the party_update event is the source of
        truth. On an error, surface it on the party card and redraw."""
        if status in (200, 201, 204):
            return
        if status == 404:
            self.party_error = t("comp_party_bad_code")
        elif status == 409:
            self.party_error = t("comp_party_is_full")
        else:
            self.party_error = t("comp_party_failed")
        self._changed()

    def _party_member(self, entry):
        """A party member from the server. UNLIKE _player_from this is honest: the persona is
        real and level/ping pass STRAIGHT THROUGH (they are None from the server — there is no
        rank service and no in-game ping). Never invent a number here."""
        entry = player_identity.normalize(entry)
        steam_id = str(entry.get("steam_id") or "")
        persona = str(entry.get("persona") or "")
        return {"name": persona or steam_id, "steam_id": steam_id, "player_id": steam_id,
                "game_steam_id": player_identity.native_id(entry),
                "avatar": str(entry.get("avatar") or ""),
                "level": entry.get("level"), "ping": entry.get("ping")}

    def _mock_friend_joins(self):
        """No-op in live mode: parties are server-driven, so no fake friend is ever injected.
        Belt-and-braces against a stray _later scheduled by a superclass path."""
        return


# ====================================================================== the panel
# The three views the tab can be showing, and the panel attribute holding each one's frame.
# Play is the only one that exists before sign-in, and the only one a locked phase allows.
VIEWS = ("play", "history", "profile")
VIEW_FRAMES = {"play": "body", "history": "history_frame", "profile": "profile_frame"}


class CompetitivePanel:
    supports_matchmaking = False  # Relay measurements require the browser interface.
    """Builds the Competitive tab into `parent` and keeps it in step with the session."""

    def __init__(self, app, parent):
        self.app = app
        self.root = app.root
        self.frame = tk.Frame(parent, bg=WHITE)
        self.body = None
        self.header = None
        self._timers = []            # before the session: it schedules its watchdog through us
        self._chat_entry = None
        self._avatar_images = {}         # (url, size) -> PhotoImage; Tk needs the reference kept
        self._avatar_tried = set()       # urls we have already gone to the network for
        self._avatar_failed = set()      # urls that did not work; never retried this session
        self.party_code_hidden = False   # the streamer toggle; Copy still works while hidden
        self._coin_canvas = None
        self._coin_job = None
        self._ban_label = None       # the queue-ban countdown, ticked in place
        self._ban_job = None
        self._last_phase_seen = None  # so the match-found cue fires on the EDGE, not every redraw
        # The sub-tabs (Sam, 2026-09-14: Match History has to be readable WHILE queued or
        # mid-veto "without disturbing anything"). So the view is a PANEL flag, never a session
        # phase: switching it does not touch the session, the queue, or a single timer. The two
        # views are separate frames that are packed and unpacked, which is also what lets the
        # history list keep its scroll position while the body behind it redraws every second.
        # Profile (Sam, 2026-09-14: click your picture and see your stats) is a third view
        # rather than a third sub-tab: it is reached from the avatar in the header, which sits
        # ABOVE the strip and is on screen in every phase.
        self.view = "play"           # "play" | "history" | "profile"
        self.views = None
        self.subtabs = None
        self.history_frame = None
        self._hist_alert = None      # the strip about the match going on behind: redrawn per tick
        self._hist_content = None    # the list itself: redrawn only when it actually changed
        self._scroll_canvas = None   # the scroll area most recently built, for _bind_wheel
        self._history_drawn = None   # fingerprint of what the list currently shows
        # Flicker guard (bug 2): the body is torn down and rebuilt only when the DRAWN state
        # actually changes. `stats` broadcasts and the once-a-second countdowns fire on_change
        # many times a second in live mode; those change only fast-moving values, which are
        # updated in place through `_live_labels` instead of rebuilding the whole panel.
        self._render_sig = None
        self._live_labels = []       # [update_fn]: called to refresh a value without a rebuild
        self.history_window = None   # the detail pop-up, kept for the tests
        self.profile_frame = None
        self._prof_alert = None      # the same strip as history's, for the same reason
        self._prof_content = None
        self._profile_drawn = None   # fingerprint of what the profile currently shows
        self._profile_from = "play"  # where Back goes: whichever view the avatar was clicked from
        self.session = LiveSession(self)
        self._fonts()
        self._restore_account()
        self._build()
        # Nothing in Competitive may hang forever (Sam, 2026-09-14). One tick watches them all.
        self.session._start_watchdog()

    # ---------------------------------------------------------------- plumbing
    def _fonts(self):
        base = tkfont.nametofont("TkDefaultFont")
        self.f_big = base.copy()
        self.f_big.configure(size=max(base.cget("size"), 9) + 6, weight="bold")
        self.f_head = base.copy()
        self.f_head.configure(weight="bold")
        self.f_sub = base.copy()
        self.f_sub.configure(size=max(base.cget("size"), 9) + 1, weight="bold")
        self.f_small = base.copy()
        self.f_small.configure(size=max(7, max(base.cget("size"), 9) - 1))

    def post(self, fn):
        """Call `fn` on the MAIN thread. Workers (sign-in, and the match backend later) use
        this instead of touching tkinter; app._pump drains it."""
        self.app.q.put(("comp", fn))

    def save_auth(self, payload):
        """Persist (or clear) the signed-in account in state.json."""
        try:
            state_mod.update_fields({"auth": payload})
            self.app.state["auth"] = payload
            return True
        except Exception:        # noqa: BLE001 — a read-only state dir must not break sign-in
            return False

    def _restore_account(self):
        self.session.restore_account(self.app.state.get("auth") or {})

    def after(self, ms, fn):
        """root.after that is cancelled when the panel goes away.

        The self-rescheduling ticks (the watchdog, the queue/accept/connect countdowns) call
        this once a second for the life of the session, so a fired job is pruned as it runs
        rather than left to pile up in _timers forever (bug 6). destroy() still cancels every
        job that has NOT yet fired."""
        def run():
            try:
                self._timers.remove(job)
            except ValueError:      # already pruned, or the list was cleared by destroy()
                pass
            fn()
        job = self.root.after(ms, run)
        self._timers.append(job)
        return job

    def destroy(self):
        self._closed = True
        session = getattr(self, "session", None)
        if session is not None and hasattr(session, "_disconnect"):
            try:
                session._disconnect()
            except Exception:    # noqa: BLE001
                pass
        for job in self._timers:
            try:
                self.root.after_cancel(job)
            except Exception:       # noqa: BLE001
                pass
        self._timers.clear()
        # These are Toplevels parented to the ROOT, so destroying self.frame does not take them
        # with it. A language change rebuilds the panel; without this the old window lingers in
        # the old language, wired to a dead session.
        for attr in ("history_window", "integrity_window", "party_window"):
            win = getattr(self, attr, None)
            if win is not None:
                try:
                    win.destroy()
                except Exception:   # noqa: BLE001
                    pass
                setattr(self, attr, None)
        try:
            self.frame.destroy()
        except Exception:           # noqa: BLE001
            pass

    def on_change(self):
        """The session moved; redraw the body (and the header, for rank changes).

        In live mode this fires many times a second (every `stats` broadcast, every countdown
        tick). A full teardown-and-rebuild each time is what made the panel flicker while the
        queue timer sat frozen (bug 2). So the header/sub-tabs/body are only rebuilt when the
        DRAWN state actually changes; when only a counter or a timer moved, the specific labels
        holding those values are updated in place instead (see `_live` / `_refresh_live`)."""
        try:
            if not self.frame.winfo_exists():
                return
        except Exception:           # noqa: BLE001
            return
        self._check_rejoin()
        self._phase_changed()
        if not self._subtabs_available() or self._history_locked():
            # Signed out, gamemode gone, or the accept window is open. The last of those is the
            # one that matters: _phase_changed only fires on the EDGE into "found", so without
            # this a player who opened history a second later would sit there reading while the
            # accept window ran out and the server blamed them for declining.
            self._set_view("play")
        sig = self._render_signature()
        rebuild = sig != self._render_sig
        self._render_sig = sig
        if rebuild:
            self._live_labels = []      # the widgets they point at are about to be destroyed
            self._draw_header()
            self._draw_subtabs()
        else:
            # Nothing structural moved: refresh the fast-changing values without tearing the
            # panel down. A `stats`-only change (online/queue size) lands here and does NOT
            # rebuild the body.
            self._refresh_live()
        # Match History and the profile are both drawn from the same history and both sit in
        # front of a running match, so both take this path whatever the render signature says:
        # each keeps its own fingerprint and redraws only when its own content moved.
        if self.view in ("history", "profile"):
            s = self.session
            # A match ended while the player was sitting here: ask for the new row. Both of
            # these views are built out of that history, so both want the fresh copy.
            if getattr(s, "history_stale", False) and not getattr(s, "history_loading", False):
                s.load_history()
            if self.view == "profile":
                self._redraw_view(self._prof_alert, self._draw_profile,
                                  self._profile_fingerprint, "_profile_drawn", "profile")
            else:
                self._redraw_view(self._hist_alert, self._draw_history_list,
                                  self._history_fingerprint, "_history_drawn", "history")
            return
        if rebuild:
            self._draw_body()

    def _force_redraw(self):
        """Rebuild the whole panel on the next on_change, even when the render signature has not
        moved. Needed when something OFF the session (a downloaded avatar cached on the panel)
        changed what should be drawn: invalidate the cached signature so on_change rebuilds."""
        self._render_sig = None
        self.on_change()

    # ---------------------------------------------------------------- flicker guard
    # The values that move every tick or on every `stats` broadcast, and so must NOT force a
    # rebuild: they are updated in place through _live_labels instead.
    _SIG_LIVE = ("online", "live_matches", "players_registered", "queue_size", "queue_seconds", "accept_left",
                 "connect_left")
    # Attributes that never reach the body (they belong to the header-less history view, or are
    # plumbing), so they must not drag the body into a needless rebuild.
    _SIG_SKIP = ("panel", "client", "history", "history_error", "history_loading",
                 "history_stale", "history_seq", "stats_queued", "stats_ready")

    def _sig_value(self, val):
        """A hashable, order-stable stand-in for one attribute, so signatures compare cleanly."""
        if isinstance(val, dict):
            return tuple(sorted((repr(k), self._sig_value(v)) for k, v in val.items()))
        if isinstance(val, (set, frozenset)):
            return tuple(sorted(repr(self._sig_value(v)) for v in val))
        if isinstance(val, (list, tuple)):
            return tuple(self._sig_value(v) for v in val)
        try:
            hash(val)
            return val
        except TypeError:
            return repr(val)

    def _render_signature(self):
        """A fingerprint of everything the header, sub-tabs and body branch on — minus the
        fast-moving counters, which are refreshed in place. Two renders with the same signature
        draw the same widgets, so the second one can be skipped."""
        s = self.session
        skip = set(self._SIG_LIVE) | set(self._SIG_SKIP)
        parts = [self.view, bool(self.gamemode_installed()), bool(getattr(self.app, "busy", False)),
                 self.sound_volume(), bool(self.party_code_hidden)]
        for key in sorted(vars(s)):
            if key in skip or key.startswith("_"):
                continue
            parts.append((key, self._sig_value(getattr(s, key))))
        return tuple(parts)

    def _live(self, widget, update_fn):
        """Register a widget whose text (or colour) changes without changing layout. When the
        render signature is unchanged, `update_fn` is called instead of rebuilding the body."""
        self._live_labels.append(update_fn)
        return widget

    def _refresh_live(self):
        for fn in self._live_labels:
            try:
                fn()
            except Exception:       # noqa: BLE001 - a stale widget must not kill the timer
                self._log_exception("live")

    def _redraw_view(self, alert_holder, draw, fingerprint, drawn_attr, where):
        """One tick of a view that sits in front of a running match.

        Only the alert strip is rebuilt every time. The content itself is left alone unless
        its fingerprint changed, so a player reading their history or their profile while the
        queue timer runs does not watch it flicker or lose their place.

        Wrapped, because on_change is what the once-a-second countdowns call: an exception
        escaping here would kill the timer that called it, and a frozen accept or connect
        countdown costs the player the match."""
        try:
            self._draw_alert(alert_holder)
            mark = fingerprint()
            if mark != getattr(self, drawn_attr, None):
                draw()
                setattr(self, drawn_attr, mark)     # only once it actually drew
        except Exception:           # noqa: BLE001
            setattr(self, drawn_attr, None)         # so the next tick tries again
            self._log_exception(where)

    # ---------------------------------------------------------------- sub-tabs
    def _subtabs_available(self) -> bool:
        """Play and Match History only make sense once there is an account behind them."""
        return bool(self.gamemode_installed() and self.session.me)

    def _history_locked(self) -> bool:
        """The one moment the player may not wander off: the accept window.

        Twenty seconds, and missing them cancels the match for ten people and blames this one.
        Everything else - the coin flip, the veto, the connect window - only warns."""
        return self.session.phase == "found"

    def _view_frame(self, name):
        """The frame behind a view name, or None.

        getattr with a default rather than an attribute: this runs from _phase_changed, which
        the tests drive against a panel that has no Tk at all."""
        return getattr(self, VIEW_FRAMES.get(name, "body"), None)

    def _set_view(self, name) -> bool:
        """Swap which view is packed, and nothing else. Returns True if it moved.

        Draws nothing on purpose: every caller is already on its way into a redraw, and this
        must stay cheap enough to call from _phase_changed."""
        name = name if name in VIEWS else "play"
        if name == self.view:
            return False
        going, coming = self._view_frame(self.view), self._view_frame(name)
        if coming is None:
            return False
        try:
            if going is not None:
                going.pack_forget()
            coming.pack(fill="both", expand=True)
            if name == "history":
                self._history_drawn = None       # the list has to be drawn at least once
            elif name == "profile":
                self._profile_drawn = None
        except Exception:           # noqa: BLE001 - the panel is going away
            # The flag is only moved once the layout actually moved. The two disagreeing is
            # how a player ends up looking at one view while the code redraws the other.
            return False
        self.view = name
        return True

    def _show_view(self, name):
        """A sub-tab, the header avatar, or Back was clicked."""
        if name in ("history", "profile") and self._history_locked():
            return                  # the strip already says so; do not fight the redraw
        if name == "profile" and hasattr(getattr(self.session, "client", None), "tournament"):
            self.session.refresh_tournament()
        if name == "profile" and self.view != "profile":
            self._profile_from = self.view      # so Back returns where the player came from
        # Both of these views are built out of the match history, so both ask for it. The
        # session caches it (load_history), so flicking between them costs nothing.
        if self._set_view(name) and self.view in ("history", "profile"):
            self.session.load_history()
        self.on_change()

    def _draw_subtabs(self):
        """Play | Match History, in the same shape as the window's own tab strip."""
        self._clear(self.subtabs)
        if not self._subtabs_available():
            return
        locked = self._history_locked()
        strip = tk.Frame(self.subtabs, bg=WHITE)
        strip.pack(fill="x", pady=(10, 0))
        for name, key in (("play", "comp_tab_play"), ("history", "comp_tab_history")):
            holder = tk.Frame(strip, bg=WHITE)
            holder.pack(side="left", padx=(0, 18))
            active = self.view == name
            # History is visibly unavailable during the accept window rather than silently
            # refusing the click: a greyed label explains itself, a dead one does not.
            off = locked and name == "history"
            label = tk.Label(holder, text=t(key), bg=WHITE,
                             fg=LINE if off else (BLACK if active else GREY),
                             cursor="arrow" if off else "hand2", font=self.f_head, pady=2)
            label.pack()
            rule = tk.Frame(holder, bg=ACCENT if active else WHITE, height=2)
            rule.pack(fill="x")
            if not off:
                label.bind("<Button-1>", lambda _e, n=name: self._show_view(n))
        tk.Frame(self.subtabs, bg=LINE, height=1).pack(fill="x", pady=(0, 10))

    def _phase_changed(self):
        """Fire anything that belongs to ENTERING a phase rather than being in it.

        on_change runs on every redraw - an accept tick, a chat line - so the cue has to hang off
        the transition or a player would hear it twenty times."""
        phase = self.session.phase
        if phase == self._last_phase_seen:
            return
        was, self._last_phase_seen = self._last_phase_seen, phase
        if phase == "found" and was != "found":
            # Sam, 2026-09-14: history is readable while queued and all through the pre-game
            # selection, but the ACCEPT WINDOW is the one screen a player must not read
            # through. Missing it costs the match and, for the connect window that follows it,
            # real Elo and a queue ban. So this one transition takes the view back by force.
            # Everything else only puts a banner on the history view and lets the player
            # decide - see _draw_history_alert.
            self._set_view("play")
        if phase == "found" and was != "found" and self.sound_enabled():
            try:
                sounds_mod.play_match_found(self.after, self.sound_volume())
            except Exception:       # noqa: BLE001 - no audio device must never cost a match
                # ...but say so somewhere. A bare pass here hid a TypeError the moment the cue
                # grew a volume argument, and the only symptom was silence (2026-09-14).
                self._log_exception("sound")

    def _check_rejoin(self):
        """The service just handed a match back (a reconnect, a restart, an update).

        Put it where the player can see it: the hub may well have opened on the gamemode list.
        It deliberately does NOT take focus - a rejoining player is usually inside Bodycam, and
        yanking the window in front of the game is the one thing worse than a missed line."""
        session = self.session
        if not getattr(session, "rejoined", False):
            return
        session.rejoined = False
        # Deferred, not done here: _show_tab redraws the panel, and doing that from inside
        # on_change would tear the widgets down underneath the redraw that is already running.
        self.after(0, self._surface_match)

    def _surface_match(self):
        try:
            self.app._show_tab("competitive")
            self.app._status(t("comp_rejoined"), revert_after=8000)
        except Exception:       # noqa: BLE001 — never let this be what breaks the match screen
            self._log_exception("rejoin")

    def _log_exception(self, where):
        """Swallowed exceptions go to the log rather than nowhere."""
        try:
            import traceback
            with open(paths.log_file(), "a", encoding="utf-8") as f:
                f.write("\n[competitive/%s] %s" % (where, traceback.format_exc()))
        except Exception:           # noqa: BLE001
            pass

    # ---------------------------------------------------------------- sound
    def sound_volume(self) -> int:
        """The slider, 0 to 100. Never set means the default, not silence.

        `None` rather than a missing key is what "never set" looks like: state.load() gives
        every key it knows about, so the test has to be against the VALUE. It cannot be a
        truth test either - 0 is a real answer, and the player who muted the cue must stay
        muted across restarts."""
        state = self.app.state or {}
        if state.get("comp_sound_volume") is not None:
            return sounds_mod.clamp_volume(state.get("comp_sound_volume"))
        # a hub that predates the slider carried a plain on/off flag; honour it once
        if state.get("comp_sound") is False:
            return 0
        return sounds_mod.DEFAULT_VOLUME

    def sound_enabled(self) -> bool:
        return self.sound_volume() > 0

    def set_sound_volume(self, value, save=True):
        volume = sounds_mod.clamp_volume(value)
        try:
            self.app.state["comp_sound_volume"] = volume
            # remember where the slider was, so the bell can put it back
            if volume > 0:
                self.app.state["comp_sound_last"] = volume
            if save:
                state_mod.update_fields({key: self.app.state[key] for key in ("comp_sound_volume", "comp_sound_last")})
        except Exception:           # noqa: BLE001 - a read-only state dir must not break the tab
            pass
        return volume

    def toggle_sound(self):
        """The header bell: straight to silent, and back to wherever the slider was."""
        if self.sound_volume() > 0:
            self.set_sound_volume(0)
        else:
            self.set_sound_volume((self.app.state or {}).get("comp_sound_last")
                                  or sounds_mod.DEFAULT_VOLUME)
        self.on_change()

    def test_sound(self):
        """Play the cue exactly as a found match would, at whatever the slider says now."""
        sounds_mod.play_match_found(self.after, self.sound_volume())

    def map_pool(self):
        """The ranked map pool: the gamemode's maps (from the catalogue when it lists them),
        minus COMPETITIVE_EXCLUDED_MAPS."""
        for e in ((self.app.catalogue or {}).get("gamemodes") or []):
            if e.get("id") == COMPETITIVE_MODE_ID:
                maps = e.get("maps") or (e.get("manifest") or {}).get("maps")
                if isinstance(maps, list) and len(maps) >= 3:
                    pool = competitive_pool([str(m) for m in maps])
                    if len(pool) >= 3:
                        return pool
        return competitive_pool(DEFAULT_MAPS)

    def gamemode_installed(self) -> bool:
        return COMPETITIVE_MODE_ID in (self.app.state.get("installed") or {})

    # ---------------------------------------------------------------- frame
    def _build(self):
        self.header = tk.Frame(self.frame, bg=WHITE)
        self.header.pack(fill="x")
        tk.Frame(self.frame, bg=LINE, height=1).pack(fill="x", pady=(8, 0))
        # Always packed, usually empty: an empty frame is zero pixels tall, and packing it
        # here means the strip can appear and disappear without ever landing below the views.
        self.subtabs = tk.Frame(self.frame, bg=WHITE)
        self.subtabs.pack(fill="x")
        self.views = tk.Frame(self.frame, bg=WHITE)
        self.views.pack(fill="both", expand=True)
        self.body = tk.Frame(self.views, bg=WHITE)
        self.body.pack(fill="both", expand=True)
        # Built once and kept: _draw_body tears its own frame down on every redraw, and the
        # history list must NOT be torn down with it or a queue tick would reset the scroll.
        self.history_frame = tk.Frame(self.views, bg=WHITE)
        self._hist_alert = tk.Frame(self.history_frame, bg=WHITE)
        self._hist_alert.pack(fill="x")
        self._hist_content = tk.Frame(self.history_frame, bg=WHITE)
        self._hist_content.pack(fill="both", expand=True)
        # The profile is built the same way and for the same reason: its content is redrawn
        # only when the numbers change, so a queue tick behind it does not make it flicker.
        self.profile_frame = tk.Frame(self.views, bg=WHITE)
        self._prof_alert = tk.Frame(self.profile_frame, bg=WHITE)
        self._prof_alert.pack(fill="x")
        self._prof_content = tk.Frame(self.profile_frame, bg=WHITE)
        self._prof_content.pack(fill="both", expand=True)
        self._draw_header()
        self._draw_subtabs()
        self._draw_body()

    def refresh(self):
        self.on_change()

    # ---------------------------------------------------------------- small widgets
    def _avatar(self, parent, name, size=34, bg=WHITE, url=""):
        """The player's Steam avatar when we have it, otherwise their initials.

        The real avatar needs STEAM_WEB_API_KEY on the server (that is where the URL comes
        from) and Pillow in the exe. Any of that missing just means initials, which is what
        every player saw before and is perfectly fine."""
        image = self._avatar_image(url, size)
        c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        if image is not None:
            c.create_image(size / 2, size / 2, image=image)
            return c
        c.create_oval(1, 1, size - 1, size - 1, fill=avatar_colour(name), outline="")
        c.create_text(size / 2, size / 2 + 1, text=initials(name), fill=WHITE,
                      font=self.f_small if size < 30 else self.f_head)
        return c

    def _avatar_image(self, url, size):
        """Cached image, or None — and the first time round, start fetching it."""
        if not url or url in self._avatar_failed or not avatars_mod.is_allowed(url):
            return None
        key = (url, size)
        if key in self._avatar_images:
            return self._avatar_images[key]
        image = avatars_mod.load(url, size)
        if image is not None:
            self._avatar_images[key] = image
            return image
        if url not in self._avatar_tried:
            self._avatar_tried.add(url)
            threading.Thread(target=self._fetch_avatar, args=(url,), daemon=True).start()
        return None

    def _fetch_avatar(self, url):
        """Worker thread: download once, then ask the main thread to redraw."""
        got = avatars_mod.fetch(url)

        def done():
            if got is None:
                self._avatar_failed.add(url)
                return                      # nothing changed on screen; no redraw needed
            # The image lands in a PANEL attribute (_avatar_images), not in session state, so the
            # render signature is unchanged and a plain on_change() would decide rebuild=False and
            # only refresh the live labels — which never recreates the avatar Canvas, so the real
            # avatar would never replace the initials. Force a full rebuild so it does.
            self._force_redraw()

        self.post(done)

    def _level_badge(self, parent, level, size=26, bg=WHITE, rank=None, division=None,
                     placing=False):
        """The rank badge.

        It draws the RANK now, not the old 1-14 level. The server's ladder is eight ranks of three
        divisions (progress.cjs), and this was clamping to a 14-point FACEIT scale - so rank 8 took
        rank 8's *level* colour, and every badge showed a bare number where a name and a division
        belong.

        `rank` is preferred; `level` is still accepted so any caller not yet passing a rank keeps
        working rather than losing its badge.
        """
        c = tk.Canvas(parent, width=size, height=size, bg=bg, highlightthickness=0, bd=0)
        if placing:
            # PLACING IS NOT RANK ZERO. A player mid-placements has a real rating and no rank yet,
            # and inventing a number for them is exactly what this badge must never do.
            c.create_rectangle(0, 0, size, size, fill=PANEL_LINE, outline="")
            c.create_text(size / 2, size / 2 + 1, text="?", fill=GREY, font=self.f_small)
            return c
        shown = rank if rank is not None else level
        if shown is None:
            # Unknown rank - a real party member the server has told us nothing about. A neutral
            # badge with a dash, NEVER an invented number.
            c.create_rectangle(0, 0, size, size, fill=PANEL_LINE, outline="")
            c.create_text(size / 2, size / 2 + 1, text="–", fill=GREY, font=self.f_small)
            return c
        shown = max(1, min(RANKS, int(shown or 1)))
        c.create_rectangle(0, 0, size, size, fill=rank_colour(shown), outline="")
        # The division, in roman, is what the badge says - the rank's NAME is on the line beside it.
        # The capstone has no division and shows its rank number instead.
        text = division_numeral(division) or str(shown)
        c.create_text(size / 2, size / 2 + 1, text=text,
                      fill=rank_text_colour(shown), font=self.f_small)
        return c

    def _rank_text(self, me):
        """'Operator II · 45 RR', or 'Reaper · 2140 BDR' at the capstone, or the placement line.

        One place, because the hero and the profile were each building this themselves out of
        whichever of `bdr` and `level` happened to be set - which is how the profile could say
        'Level 6' while the server had the player at Operator II.
        """
        if me.get("placing"):
            left = me.get("placements_left")
            return t("comp_placements_left", n=left) if left else t("comp_placements")
        name = me.get("rank_name")
        if not name:
            # No ladder from the server yet. Say nothing rather than a number from the old scale.
            return t("comp_level", n=me.get("level")) if me.get("level") else ""
        numeral = division_numeral(me.get("division"))
        head = f"{name} {numeral}".strip()
        # ONE CURRENCY, ALL THE WAY UP. This used to print BDR at the top of the ladder and RR
        # everywhere else, which read as a second currency arriving at the last rank; there is
        # only RR now, and the top of the ladder is where it stops resetting rather than where it
        # stops. `bdr` on the wire is the same figure under its older name, so it is the fallback.
        figure = me.get("rr")
        if figure is None:
            figure = me.get("bdr")
        if figure is not None:
            return f"{head}   ·   " + t("comp_rr", rr=figure)
        return head

    def _arrows(self, parent, delta, bg=WHITE):
        """Levels 1-10 show arrows, never numbers (Sam). 1/2/3 up green, 1/2/3 down red."""
        n = min(3, abs(int(delta or 0)))
        up = (delta or 0) > 0
        w, h = max(1, n) * 14 + 4, 18
        c = tk.Canvas(parent, width=w, height=h, bg=bg, highlightthickness=0, bd=0)
        colour = GREEN if up else RED
        for i in range(n):
            x = 4 + i * 14
            if up:
                c.create_polygon(x, 13, x + 5, 4, x + 10, 13, fill=colour, outline="")
            else:
                c.create_polygon(x, 5, x + 5, 14, x + 10, 5, fill=colour, outline="")
        return c

    def _button(self, parent, text, command, primary=False, state="normal", width=None, bg=WHITE):
        if primary:
            b = tk.Button(parent, text=text, command=command, state=state,
                          bg=ACCENT, fg=WHITE, activebackground=ACCENT_DARK, activeforeground=WHITE,
                          relief="flat", padx=20, pady=7, cursor="hand2",
                          font=self.f_head, disabledforeground="#BBBBBB")
        else:
            b = tk.Button(parent, text=text, command=command, state=state, bg=bg, fg=BLACK,
                          activebackground=bg, activeforeground=BLACK, padx=12, pady=3,
                          cursor="hand2")
        if width:
            b.configure(width=width)
        return b

    def _card(self, parent, **kw):
        f = tk.Frame(parent, bg=PANEL, highlightbackground=PANEL_LINE,
                     highlightthickness=1, bd=0, **kw)
        return f

    def _clear(self, widget):
        for child in widget.winfo_children():
            child.destroy()

    # ---------------------------------------------------------------- header
    def _draw_header(self):
        self._clear(self.header)
        s = self.session
        if not s.me:
            tk.Label(self.header, text=t("tab_competitive"), bg=WHITE, fg=BLACK,
                     font=self.f_sub, anchor="w").pack(side="left")
            return

        left = tk.Frame(self.header, bg=WHITE)
        left.pack(side="left")
        # Sam, 2026-09-14: clicking your picture opens your profile. The name goes with it -
        # a picture is a small target, and anyone who tries the picture will try the name.
        # Locked during the accept window for the same reason Match History is (_show_view
        # refuses it); the cursor says so rather than the click silently doing nothing.
        opens = not self._history_locked()
        face = self._avatar(left, s.me["name"], url=s.me.get("avatar") or "")
        face.configure(cursor="hand2" if opens else "arrow")
        face.pack(side="left", padx=(0, 8))
        who = tk.Frame(left, bg=WHITE)
        who.pack(side="left")
        name_row = tk.Frame(who, bg=WHITE)
        name_row.pack(anchor="w")
        self._level_badge(name_row, s.me.get("level"), rank=s.me.get("rank"),
                          division=s.me.get("division"), placing=s.me.get("placing")).pack(side="left", padx=(0, 6))
        name = tk.Label(name_row, text=s.me["name"], bg=WHITE, fg=BLACK, font=self.f_head,
                        cursor="hand2" if opens else "arrow")
        name.pack(side="left")
        if opens:
            for w in (face, name):
                w.bind("<Button-1>", lambda _e: self._show_view("profile"))
        played = s.me.get("matches") or 0
        wins = s.me.get("wins") or 0
        sub = self._rank_text(s.me)
        tk.Label(who, text=f"{sub}   ·   " + t("comp_record", played=played, wins=wins),
                 bg=WHITE, fg=GREY, anchor="w", font=self.f_small).pack(anchor="w")

        right = tk.Frame(self.header, bg=WHITE)
        right.pack(side="right")
        if s.locked_in():
            # Sam, 2026-09-14: the button goes away once a match is found, so nobody can sign
            # out to cancel one during the coin flip or the veto. A player who is genuinely
            # stuck still gets out: every locked phase has a deadline in PHASE_LIMITS.
            tk.Label(right, text=t("comp_signout_locked"), bg=WHITE, fg=GREY,
                     font=self.f_small).pack(side="right")
        else:
            self._button(right, t("comp_signout"), self.session.sign_out).pack(side="right")
        online = tk.Label(right, text=t("comp_online", n=s.online), bg=WHITE, fg=GREY,
                          font=self.f_small)
        online.pack(side="right", padx=(0, 12))
        # the online count changes on every presence/queue change (a `stats` broadcast); update
        # it in place so those do not rebuild the whole panel
        self._live(online, lambda: online.config(text=t("comp_online", n=self.session.online)))
        on = self.sound_enabled()
        bell = tk.Label(right, text=("\U0001F514 " if on else "\U0001F515 ") +
                        t("comp_sound_on" if on else "comp_sound_off"),
                        bg=WHITE, fg=GREY if on else LINE, cursor="hand2", font=self.f_small)
        bell.pack(side="right", padx=(0, 12))
        bell.bind("<Button-1>", lambda _e: self.toggle_sound())

    # ---------------------------------------------------------------- body
    def _draw_body(self):
        self._clear(self.body)
        if self._coin_job is not None:
            try:
                self.root.after_cancel(self._coin_job)
            except Exception:       # noqa: BLE001
                pass
            self._coin_job = None

        if not self.gamemode_installed():
            self._draw_gate()
            return

        phase = self.session.phase
        drawer = {
            "signed_out": self._draw_signed_out,
            "signing_in": self._draw_signing_in,
            "game_unavailable": self._draw_game_unavailable,
            "idle": self._draw_idle,
            "checking": self._draw_checking,
            "queued": self._draw_queued,
            "found": self._draw_found,
            "lobby": self._draw_lobby,
            "connecting": self._draw_connecting,
            "live": self._draw_live,
            "result": self._draw_result,
        }.get(phase, self._draw_idle)
        drawer()
        if getattr(self.session, "mock", False) and phase not in ("signed_out", "signing_in"):
            note = t("comp_preview")
            tk.Label(self.body, text=note, bg=WHITE, fg=AMBER,
                     font=self.f_small, anchor="w", wraplength=760, justify="left").pack(
                         side="bottom", fill="x", pady=(8, 0))
        if not getattr(self.session, "connected", True) and phase not in ("signed_out", "signing_in"):
            tk.Label(self.body, text=t("comp_live_lost"), bg=WHITE, fg=RED,
                     font=self.f_small, anchor="w", wraplength=760, justify="left").pack(
                         side="bottom", fill="x")

    def _centre(self):
        """A frame that keeps its content in the middle of the body."""
        wrap = tk.Frame(self.body, bg=WHITE)
        wrap.pack(fill="both", expand=True)
        inner = tk.Frame(wrap, bg=WHITE)
        inner.place(relx=0.5, rely=0.45, anchor="center")
        return inner

    # ------------------------------------------------- gate: gamemode not installed
    def _draw_gate(self):
        inner = self._centre()
        tk.Label(inner, text=t("comp_gate_title"), bg=WHITE, fg=BLACK,
                 font=self.f_sub).pack(pady=(0, 6))
        tk.Label(inner, text=t("comp_gate_body"), bg=WHITE, fg=GREY, wraplength=520,
                 justify="center").pack(pady=(0, 14))
        listed = any(e.get("id") == COMPETITIVE_MODE_ID
                     for e in ((self.app.catalogue or {}).get("gamemodes") or []))
        if not listed:
            tk.Label(inner, text=t("comp_gate_waiting"), bg=WHITE, fg=GREY).pack()
            return
        self._button(inner, t("comp_gate_install"), self._install_gamemode, primary=True,
                     state="disabled" if self.app.busy else "normal").pack()

    def _install_gamemode(self):
        if self.app.busy:
            return
        self.app._apply(set(self.app.state.get("installed") or {}) | {COMPETITIVE_MODE_ID})

    # ------------------------------------------------- signed out
    def _draw_signed_out(self):
        inner = self._centre()
        tk.Label(inner, text=t("comp_signin_title"), bg=WHITE, fg=BLACK,
                 font=self.f_sub).pack(pady=(0, 6))
        tk.Label(inner, text=t("comp_signin_body"), bg=WHITE, fg=GREY, wraplength=560,
                 justify="center").pack(pady=(0, 16))
        self._button(inner, t("comp_signin_button"), self.session.sign_in, primary=True).pack()
        if self.session.error:
            tk.Label(inner, text=self.session.error, bg=WHITE, fg=RED,
                     wraplength=560, justify="center").pack(pady=(12, 0))

    def _draw_signing_in(self):
        s = self.session
        inner = self._centre()
        tk.Label(inner, text=t("comp_signin_waiting"), bg=WHITE, fg=BLACK,
                 font=self.f_sub, wraplength=560, justify="center").pack()
        tk.Label(inner, text=t("comp_signin_browser"), bg=WHITE, fg=GREY,
                 wraplength=520, justify="center").pack(pady=(6, 0))
        if s.link_code:
            tk.Label(inner, text=t("comp_signin_code", code=s.link_code), bg=WHITE, fg=GREY,
                     font=self.f_small).pack(pady=(10, 0))
        row = tk.Frame(inner, bg=WHITE)
        row.pack(pady=(16, 0))
        if s.link_url:
            self._button(row, t("comp_signin_open_again"), s.open_link_again).pack(side="left")
        self._button(row, t("comp_cancel"), s.cancel_sign_in).pack(side="left", padx=(8, 0))

    def _draw_game_unavailable(self):
        inner = self._centre()
        tk.Label(inner, text=t("account_game_pending"), bg=WHITE, fg=BLACK,
                 font=self.f_sub, wraplength=560, justify="center").pack()
        self._button(inner, t("account_game_retry"),
                     lambda: self.session.account_action("game/retry"), primary=True).pack(pady=(12, 0))
        self._button(inner, t("comp_signout"), self.session.sign_out).pack(pady=(8, 0))
        if self.session.error:
            tk.Label(inner, text=self.session.error, bg=WHITE, fg=RED,
                     wraplength=560, justify="center").pack(pady=(12, 0))

    # ------------------------------------------------- idle
    def _draw_idle(self):
        if getattr(self.session, "live", False):
            tk.Label(self._centre(), text=t("comp_browser_required"), bg=WHITE, fg=GREY,
                     wraplength=560, justify="center").pack()
            return
        s = self.session
        inner = self._centre()
        if s.error:
            tk.Label(inner, text=s.error, bg=WHITE, fg=RED,
                     wraplength=560, justify="center").pack(pady=(0, 12))
        tk.Label(inner, text=t("comp_mode_line"), bg=WHITE, fg=GREY).pack(pady=(0, 12))

        banned = s.banned_left()
        if banned:
            self._draw_ban_card(inner, banned)

        # only the party leader starts the search; the rest wait for them
        if s.is_party_leader():
            self._button(inner, t("comp_find_match"), s.find_match, primary=True,
                         state="disabled" if banned else "normal").pack()
        else:
            tk.Label(inner, text=t("comp_party_waiting_leader", name=s.party_leader_name()),
                     bg=WHITE, fg=AMBER, wraplength=520, justify="center").pack(pady=(4, 4))

        # The paragraph about keeping Bodycam closed is gone from under the button: find_match
        # enforces that rule itself now and says so for three seconds when it bites. The waiting
        # screens keep their line - there the game genuinely is about to be opened for you.
        self._draw_sound_row(inner)

        link = tk.Label(inner, text=t("comp_check_what"), bg=WHITE, fg=ACCENT,
                        cursor="hand2", font=self.f_small)
        link.pack(pady=(14, 0))
        link.bind("<Button-1>", lambda _e: self._show_integrity_note())

        self._draw_party_card(inner)

    def _draw_ready_state(self, parent, short=False):
        """Where the player has to BE for any of this to work (Sam, 2026-09-14).

        The hub cannot pull somebody into a match from the desktop: the joining happens inside
        Bodycam, from the shooting range. That is not obvious from a window full of queue
        buttons, so it is said before they queue, while they wait, and again when it matters."""
        tk.Label(parent, text=t("comp_ready_state_short") if short else t("comp_ready_state"),
                 bg=WHITE, fg=AMBER, font=self.f_small, wraplength=560,
                 justify="center").pack(pady=(10, 0))

    def _draw_sound_row(self, parent):
        """Volume for the match-found cue, and a button to hear it (Sam, 2026-09-14).

        The slider has to be tried rather than guessed: the cue competes with whatever the player
        has in their other ear, so Test plays exactly what a found match plays, at exactly the
        level the slider is on right now."""
        row = tk.Frame(parent, bg=WHITE)
        row.pack(pady=(14, 0))
        tk.Label(row, text=t("comp_sound_label"), bg=WHITE, fg=GREY,
                 font=self.f_small).pack(side="left", padx=(0, 8))

        volume = self.sound_volume()
        readout = tk.Label(row, text=(t("comp_sound_muted") if volume == 0 else "%d%%" % volume),
                           bg=WHITE, fg=GREY if volume else LINE, font=self.f_small, width=8,
                           anchor="w")

        def show(v):
            readout.configure(text=(t("comp_sound_muted") if v == 0 else "%d%%" % v),
                              fg=GREY if v else LINE)

        def moved(value):
            # While dragging: the readout and the in-memory value only. Writing state.json on
            # every pixel of a drag would hammer the disk, so `settle` does the saving.
            v = sounds_mod.clamp_volume(value)
            self.set_sound_volume(v, save=False)
            show(v)

        def settle(_event=None):
            """The value the player actually landed on. Read from the WIDGET rather than trusting
            the drag callback: Tk does not fire a Scale's command when the widget sits on an
            unmapped tab, and the keyboard path (arrow keys, Home/End) has no drag at all."""
            v = sounds_mod.clamp_volume(slider.get())
            self.set_sound_volume(v)
            show(v)

        slider = tk.Scale(row, from_=0, to=100, orient="horizontal", showvalue=0,
                          length=150, sliderlength=14, width=10, bg=WHITE, fg=BLACK,
                          troughcolor=PANEL, highlightthickness=0, bd=0, relief="flat",
                          activebackground=ACCENT, command=moved)
        slider.set(volume)
        slider.pack(side="left")
        for sequence in ("<ButtonRelease-1>", "<KeyRelease>", "<MouseWheel>"):
            slider.bind(sequence, settle, add="+")
        readout.pack(side="left", padx=(8, 8))
        self._button(row, t("comp_sound_test"), self.test_sound).pack(side="left")

    def _draw_ban_card(self, parent, seconds):
        """A queue ban, with the clock running. Only the COUNTDOWN is redrawn each second:
        the idle screen carries a text entry for party codes, and rebuilding the whole body
        under it every tick would make the code impossible to type."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 14))
        inner = tk.Frame(card, bg=PANEL)
        inner.pack(fill="x", padx=14, pady=10)
        tk.Label(inner, text=t("comp_banned_title"), bg=PANEL, fg=RED,
                 font=self.f_head, anchor="w").pack(anchor="w")
        reason = {"no_show": t("comp_banned_no_show")}.get(self.session.penalty_reason, "")
        if reason:
            tk.Label(inner, text=reason, bg=PANEL, fg=GREY, anchor="w",
                     wraplength=520, justify="left").pack(anchor="w", pady=(2, 0))
        self._ban_label = tk.Label(inner, text=t("comp_banned_left", time=format_clock(seconds)),
                                   bg=PANEL, fg=BLACK, font=self.f_sub, anchor="w")
        self._ban_label.pack(anchor="w", pady=(6, 0))
        self._arm_ban_tick()

    def _arm_ban_tick(self):
        """Exactly one tick in flight. Redrawing the idle screen calls _draw_ban_card again,
        and without this each redraw would leave another loop running on the same label."""
        if self._ban_job is not None:
            try:
                self.root.after_cancel(self._ban_job)
            except Exception:           # noqa: BLE001
                pass
        self._ban_job = self.after(1000, self._tick_ban_label)

    def _tick_ban_label(self):
        self._ban_job = None
        label = self._ban_label
        if label is None:
            return
        try:
            if not label.winfo_exists():
                self._ban_label = None
                return
        except Exception:               # noqa: BLE001 - the panel went away
            self._ban_label = None
            return
        left = self.session.banned_left()
        if left <= 0:
            self._ban_label = None
            self.session._changed()     # the ban is served: redraw and re-enable the button
            return
        label.configure(text=t("comp_banned_left", time=format_clock(left)))
        self._arm_ban_tick()

    # ------------------------------------------------- party
    def _draw_party_card(self, parent):
        """Solo: two buttons. In a party: the code, who is in it, and Leave."""
        s = self.session
        card = self._card(parent)
        card.pack(fill="x", pady=(18, 0))
        head = tk.Frame(card, bg=PANEL)
        head.pack(fill="x", padx=10, pady=(8, 2))
        tk.Label(head, text=t("comp_party_title"), bg=PANEL, fg=BLACK,
                 font=self.f_head).pack(side="left")
        if s.party:
            tk.Label(head, text=t("comp_party_count", n=s.party_size(), max=MAX_PARTY),
                     bg=PANEL, fg=GREY, font=self.f_small).pack(side="right")

        if not s.party:
            tk.Label(card, text=t("comp_party_solo_note"), bg=PANEL, fg=GREY,
                     wraplength=440, justify="left", anchor="w").pack(fill="x", padx=10)
            row = tk.Frame(card, bg=PANEL)
            row.pack(padx=10, pady=(8, 10), anchor="w")
            self._button(row, t("comp_party_create"), s.create_party, bg=PANEL).pack(side="left")
            self._button(row, t("comp_party_join"), self._ask_party_code,
                         bg=PANEL).pack(side="left", padx=(8, 0))
            if s.party_error:
                tk.Label(card, text=s.party_error, bg=PANEL, fg=RED, anchor="w",
                         wraplength=440, justify="left").pack(fill="x", padx=10, pady=(0, 10))
            return

        # The code, big enough to read off a screen, with Copy / Hide / New code.
        # Hide blanks it for streaming; Copy keeps working while it is hidden, and New code
        # is the escape hatch once it has leaked (Sam, 2026-09-14).
        code_row = tk.Frame(card, bg=PANEL)
        code_row.pack(fill="x", padx=10, pady=(4, 2))
        shown = mask_party_code(s.party["code"]) if self.party_code_hidden else s.party["code"]
        tk.Label(code_row, text=shown, bg=PANEL, fg=BLACK, font=self.f_sub).pack(side="left")
        self.party_copy_button = self._button(code_row, t("comp_party_copy"),
                                              self._copy_party_code, bg=PANEL)
        self.party_copy_button.pack(side="left", padx=(10, 0))
        self._button(code_row,
                     t("comp_party_show") if self.party_code_hidden else t("comp_party_hide"),
                     self._toggle_party_code, bg=PANEL).pack(side="left", padx=(6, 0))
        if s.is_party_leader():
            self._button(code_row, t("comp_party_new_code"), self._new_party_code,
                         bg=PANEL).pack(side="left", padx=(6, 0))
        note = t("comp_party_share")
        if s.is_party_leader():
            note += " " + t("comp_party_new_code_note")
        tk.Label(card, text=note, bg=PANEL, fg=GREY, wraplength=440,
                 justify="left", anchor="w").pack(fill="x", padx=10, pady=(0, 6))

        for p in s.party["members"]:
            row = tk.Frame(card, bg=PANEL)
            row.pack(fill="x", padx=10, pady=1)
            self._level_badge(row, p.get("level"), size=18, bg=PANEL, rank=p.get("rank"),
                              division=p.get("division")).pack(side="left", padx=(0, 6))
            is_me = p.get("steam_id") == (s.me or {}).get("steam_id")
            tk.Label(row, text=p.get("name", "?"), bg=PANEL, fg=BLACK,
                     font=self.f_head if is_me else None, anchor="w").pack(side="left")
            if p.get("steam_id") == s.party["leader_id"]:
                tk.Label(row, text=t("comp_party_leader"), bg=PANEL, fg=ACCENT,
                         font=self.f_small).pack(side="left", padx=(6, 0))

        self._button(card, t("comp_party_leave"), s.leave_party,
                     bg=PANEL).pack(anchor="w", padx=10, pady=(8, 10))

    def _copy_party_code(self):
        s = self.session
        if not s.party:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(s.party["code"])
        except Exception:        # noqa: BLE001 — no clipboard (headless): the code is on screen anyway
            return
        btn = getattr(self, "party_copy_button", None)
        if btn is not None:
            try:
                btn.configure(text=t("comp_party_copied"))
            except Exception:    # noqa: BLE001
                pass

    def _toggle_party_code(self):
        """Blank the code on screen / show it again. Copy is unaffected."""
        self.party_code_hidden = not self.party_code_hidden
        self.on_change()

    def _new_party_code(self):
        """Mint a new code and reveal it, because the point is to read the new one.

        The mock returns the new code synchronously; the live session returns None and the new
        code arrives later on a party_update. So un-hide OPTIMISTICALLY here rather than gating
        on the return value, and let the event bring the code — the current code stays on screen
        until it does, which is harmless."""
        self.session.refresh_party_code()
        self.party_code_hidden = False
        self.on_change()

    def _ask_party_code(self):
        """A small modal: type the code a friend sent you."""
        s = self.session
        win = tk.Toplevel(self.root)
        win.title(t("comp_party_join_title"))
        win.configure(bg=WHITE)
        win.transient(self.root)
        tk.Label(win, text=t("comp_party_join_prompt"), bg=WHITE, fg=BLACK,
                 wraplength=360, justify="left").pack(padx=16, pady=(16, 8), anchor="w")
        entry = tk.Entry(win, bg=WHITE, fg=BLACK, relief="solid", bd=1, justify="center",
                         font=self.f_sub, width=12)
        entry.pack(padx=16)
        entry.focus_set()

        def submit(_e=None):
            code = entry.get()
            win.destroy()
            s.join_party(code)

        entry.bind("<Return>", submit)
        row = tk.Frame(win, bg=WHITE)
        row.pack(padx=16, pady=14, anchor="e")
        self._button(row, t("comp_party_join_button"), submit, primary=True).pack(side="left")
        self._button(row, t("close"), win.destroy).pack(side="left", padx=(8, 0))
        win.bind("<Escape>", lambda _e: win.destroy())
        self.party_window = win
        return win

    def _show_integrity_note(self):
        win = tk.Toplevel(self.root)
        win.title(t("comp_check_what"))
        win.configure(bg=WHITE)
        win.transient(self.root)
        tk.Label(win, text=t("comp_check_what_body"), bg=WHITE, fg=BLACK, wraplength=460,
                 justify="left").pack(padx=16, pady=16)
        tk.Button(win, text=t("close"), bg=WHITE, fg=BLACK, activebackground=WHITE,
                  padx=14, command=win.destroy).pack(pady=(0, 14))
        win.bind("<Escape>", lambda _e: win.destroy())
        self.integrity_window = win
        return win

    # ------------------------------------------------- integrity check
    def _draw_checking(self):
        inner = self._centre()
        tk.Label(inner, text=t("comp_check_title"), bg=WHITE, fg=BLACK,
                 font=self.f_sub).pack(pady=(0, 12))
        steps = [t("comp_check_files"), t("comp_check_mods"), t("comp_check_ok")]
        for i, text in enumerate(steps):
            done = i < self.session.check_step
            row = tk.Frame(inner, bg=WHITE)
            row.pack(anchor="w", pady=2)
            tk.Label(row, text="✓" if done else "·", bg=WHITE,
                     fg=GREEN if done else GREY, width=2).pack(side="left")
            tk.Label(row, text=text, bg=WHITE, fg=BLACK if done else GREY).pack(side="left")

    # ------------------------------------------------- queue
    def _draw_queued(self):
        inner = self._centre()
        secs = self.session.queue_seconds
        if self.session.error:
            # a requeue after a cancelled match lands here, and the player is owed the reason
            tk.Label(inner, text=self.session.error, bg=WHITE, fg=RED,
                     wraplength=560, justify="center").pack(pady=(0, 10))
        tk.Label(inner, text=t("comp_searching"), bg=WHITE, fg=BLACK, font=self.f_sub).pack()
        clock = tk.Label(inner, text=f"{secs // 60}:{secs % 60:02d}", bg=WHITE, fg=BLACK,
                         font=self.f_big)
        clock.pack(pady=(4, 2))
        # the queue clock ticks every second; keep it moving without a rebuild (bug 1 + bug 2)
        self._live(clock, lambda: clock.config(
            text=f"{self.session.queue_seconds // 60}:{self.session.queue_seconds % 60:02d}"))
        tk.Label(inner, text=t("comp_queue_mode"), bg=WHITE, fg=GREY).pack()
        if self.session.queue_position:
            pos = tk.Label(inner, text=t("comp_queue_position", n=self.session.queue_position,
                                         m=max(self.session.queue_size, self.session.queue_position)),
                           bg=WHITE, fg=GREY, font=self.f_small)
            pos.pack(pady=(2, 0))
            # queue size rides in on `stats`; refresh the count in place rather than rebuilding
            self._live(pos, lambda: pos.config(text=t("comp_queue_position",
                       n=self.session.queue_position,
                       m=max(self.session.queue_size, self.session.queue_position))))
        if self.session.party_size() > 1:
            tk.Label(inner, text=t("comp_party_queue_note", n=self.session.party_size()),
                     bg=WHITE, fg=ACCENT, font=self.f_small).pack(pady=(2, 0))
        tk.Frame(inner, bg=WHITE, height=12).pack()
        self._button(inner, t("comp_cancel"), self.session.cancel_queue).pack()
        self._draw_ready_state(inner)

    # ------------------------------------------------- match found
    def _draw_found(self):
        s = self.session
        inner = self._centre()
        tk.Label(inner, text=t("comp_found_title"), bg=WHITE, fg=BLACK, font=self.f_big).pack()
        total = max(1, getattr(s, "accept_total", 0) or len(s.players) or LOBBY_SIZE)
        tk.Label(inner, text=t("comp_accepted_count", n=s.accepted, total=total),
                 bg=WHITE, fg=GREY).pack(pady=(4, 10))

        # one pip per player, filling as people accept. NOT ten: the server's match size is
        # configurable and a screen that promises ten pips for a two-player test is a lie.
        pips = tk.Frame(inner, bg=WHITE)
        pips.pack(pady=(0, 14))
        for i in range(total):
            c = tk.Canvas(pips, width=16, height=16, bg=WHITE, highlightthickness=0, bd=0)
            c.create_oval(3, 3, 13, 13, fill=GREEN if i < s.accepted else WHITE,
                          outline=GREEN if i < s.accepted else LINE)
            c.pack(side="left", padx=2)

        if s.i_accepted:
            tk.Label(inner, text=t("comp_waiting_others"), bg=WHITE, fg=GREY).pack()
        else:
            accept_btn = self._button(inner, t("comp_accept") + f"  ({s.accept_left})",
                                      s.accept, primary=True)
            accept_btn.pack()
            # the accept countdown ticks every second; update the label in place (bug 2)
            self._live(accept_btn, lambda: accept_btn.config(
                text=t("comp_accept") + f"  ({self.session.accept_left})"))

    # ------------------------------------------------- connecting (the 3 minute window)
    def _draw_connecting(self):
        """Everything is decided and the clock is running: get into the game.

        This screen has to make the cost obvious BEFORE it is paid, which is why the warning
        line is on it the whole time rather than appearing once the damage is done."""
        s = self.session
        inner = self._centre()
        if getattr(s, "live", False) and (s._i_am_host() or s.i_connected):
            self._button(inner, t("comp_relaunch" if s._i_am_host() else "comp_reconnect"),
                         s.relaunch_game).pack(pady=(0, 8))
        if s.error:
            tk.Label(inner, text=s.error, bg=WHITE, fg=RED,
                     wraplength=520, justify="center").pack(pady=(0, 8))
        tk.Label(inner, text=t("comp_connect_title"), bg=WHITE, fg=BLACK, font=self.f_sub).pack()
        clock = tk.Label(inner, text=format_clock(s.connect_left), bg=WHITE,
                         fg=RED if s.connect_left <= 30 else BLACK, font=self.f_big)
        clock.pack(pady=(2, 0))
        timer_line = tk.Label(inner, text=t("comp_connect_timer", time=format_clock(s.connect_left)),
                              bg=WHITE, fg=GREY, font=self.f_small)
        timer_line.pack(pady=(0, 10))
        # the connect countdown ticks every second; update both lines (and the clock's colour,
        # which flips red in the last 30 s) in place rather than rebuilding the screen (bug 2)
        self._live(clock, lambda: clock.config(
            text=format_clock(self.session.connect_left),
            fg=RED if self.session.connect_left <= 30 else BLACK))
        self._live(timer_line, lambda: timer_line.config(
            text=t("comp_connect_timer", time=format_clock(self.session.connect_left))))

        if s.map:
            tk.Label(inner, text=t("comp_connect_map", map=s.map), bg=WHITE, fg=BLACK).pack()
        host = s.host or {}
        if host.get("name"):
            tk.Label(inner, text=t("comp_connect_host", name=host.get("name")),
                     bg=WHITE, fg=BLACK).pack(pady=(0, 10))

        total = max(1, s.connect_total or len(s.players) or 1)
        done = len(s.connected_ids)
        tk.Label(inner, text=t("comp_connect_count", n=done, total=total),
                 bg=WHITE, fg=GREY).pack()
        pips = tk.Frame(inner, bg=WHITE)
        pips.pack(pady=(6, 14))
        for i in range(total):
            c = tk.Canvas(pips, width=16, height=16, bg=WHITE, highlightthickness=0, bd=0)
            c.create_oval(3, 3, 13, 13, fill=GREEN if i < done else WHITE,
                          outline=GREEN if i < done else LINE)
            c.pack(side="left", padx=2)

        # LAUNCHING, not asserting. The "I am in the game" button is gone (Sam, 2026-09-16); the
        # host's game still opens by itself when the window does, and a JOINER now gets a Launch
        # button that stays disabled until the host's lobby is stamped - because a joiner opened
        # before then spends its one lobby search on an empty Steam.
        if s.i_connected:
            tk.Label(inner, text=t("comp_connect_waiting"), bg=WHITE, fg=GREY).pack()
        elif not s._i_am_host():
            ready = bool(getattr(s, "host_ready", False))
            self._button(inner, t("comp_launch"), s.launch_game, primary=True,
                         state="normal" if ready else "disabled").pack()
            if not ready:
                tk.Label(inner, text=t("comp_waiting_host"), bg=WHITE, fg=GREY).pack(pady=(6, 0))
                # The host's own connect clock (connect_left ticks for every client in the
                # window), so the wait reads as bounded rather than open-ended.
                tk.Label(inner, text=t("comp_host_join_timer", time=format_clock(s.connect_left)),
                         bg=WHITE, fg=GREY).pack()
            self._draw_ready_state(inner)
        else:
            self._draw_ready_state(inner)
        # The game was ALREADY running when the match came up, so it has spent the single
        # BeginPlay that autojoin rides on (docs/autojoin.md, step 4 result) and nothing we can
        # send will change that. Say so, in amber, instead of leaving them watching a clock run
        # down on a join that is never coming.
        if getattr(s, "game_was_open", False):
            tk.Label(inner, text=t("comp_game_was_open"), bg=WHITE, fg=AMBER,
                     font=self.f_small, wraplength=560, justify="center").pack(pady=(10, 0))
        # The start gate. Everyone can be in and the match still not start, which without a line
        # here is the most confusing screen in the app: a full set of green pips and nothing
        # happening. Both states are amber because both are "not what you expected, keep reading".
        status = getattr(s, "teams_status", "")
        if status in ("wait", "mismatch"):
            tk.Label(inner, text=t("comp_teams_wait" if status == "wait" else "comp_teams_mismatch"),
                     bg=WHITE, fg=AMBER, font=self.f_small, wraplength=560,
                     justify="center").pack(pady=(10, 0))
        # the number here is THIS player's next rung, not a constant: promising five minutes
        # to someone who is about to be banned for two hours would be a lie
        nxt = max(60, int(getattr(s, "penalty_next", 0) or NO_SHOW_BAN_SECONDS))
        tk.Label(inner, text=t("comp_connect_warn", time=format_duration(nxt)),
                 bg=WHITE, fg=AMBER, font=self.f_small, wraplength=560,
                 justify="center").pack(pady=(14, 0))

    # ------------------------------------------------- lobby (coin flip + veto + chat)
    def _draw_lobby(self):
        s = self.session
        if s.stage == "rejoin":
            self._draw_rejoin()
            return
        wrap = tk.Frame(self.body, bg=WHITE)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        wrap.columnconfigure(0, weight=3, uniform="lob")
        wrap.columnconfigure(1, weight=2, uniform="lob")
        wrap.rowconfigure(0, weight=1)

        left = tk.Frame(wrap, bg=WHITE)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = tk.Frame(wrap, bg=WHITE)
        right.grid(row=0, column=1, sticky="nsew")

        self._draw_teams(left)
        if s.stage in ("coin", "flipping"):
            self._draw_coin(left)
        elif s.stage == "choice":
            self._draw_choice(left)
        elif s.stage == "side":
            self._draw_side(left)
        else:
            self._draw_veto(left)
        # TEAM CHAT AT HALF WIDTH, ALL CHAT BESIDE IT, the same box built twice: neither reads
        # as the "real" one, and there is never any doubt about which log a line went into.
        right.columnconfigure(0, weight=1, uniform="chat")
        right.columnconfigure(1, weight=1, uniform="chat")
        right.rowconfigure(0, weight=1)
        team_col = tk.Frame(right, bg=WHITE)
        team_col.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        all_col = tk.Frame(right, bg=WHITE)
        all_col.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self._draw_chat(team_col, "team")
        self._draw_chat(all_col, "all")

    def _draw_rejoin(self):
        """Waiting to be let back into a match whose lobby is running on the other clients.

        There is nothing to click: the teams, the coin flip and the veto are all happening
        elsewhere, and `match_connecting` is what ends this screen."""
        inner = self._centre()
        tk.Label(inner, text=t("comp_rejoin_title"), bg=WHITE, fg=BLACK, font=self.f_sub).pack()
        tk.Label(inner, text=t("comp_rejoin_body"), bg=WHITE, fg=GREY, wraplength=520,
                 justify="center").pack(pady=(8, 0))

    def _draw_teams(self, parent):
        s = self.session
        cols = tk.Frame(parent, bg=WHITE)
        cols.pack(fill="x")
        cols.columnconfigure(0, weight=1, uniform="t")
        cols.columnconfigure(1, weight=1, uniform="t")
        for idx, team in enumerate((1, 2)):
            col = self._card(cols)
            col.grid(row=0, column=idx, sticky="nsew", padx=(0, 6) if idx == 0 else (6, 0))
            head = t("comp_team", n=team)
            if s.stage in ("veto", "ready") and s.sides:
                head += "  ·  " + (t("comp_side_attack") if s.sides.get(team) == "attack"
                                   else t("comp_side_defend"))
            tk.Label(col, text=head, bg=PANEL, fg=BLACK, font=self.f_head, anchor="w",
                     padx=8, pady=4).pack(fill="x")
            for p in s.teams.get(team, []):
                row = tk.Frame(col, bg=PANEL)
                row.pack(fill="x", padx=8, pady=1)
                self._level_badge(row, p.get("level"), size=18, bg=PANEL, rank=p.get("rank"),
                              division=p.get("division")).pack(side="left", padx=(0, 6))
                is_me = p.get("steam_id") == (s.me or {}).get("steam_id")
                tk.Label(row, text=s.display_name(p), bg=PANEL,
                         fg=GREY if s.is_hidden(p.get("steam_id")) else BLACK,
                         font=self.f_head if is_me else None, anchor="w").pack(side="left")
                if (s.captains.get(team) or {}).get("steam_id") == p.get("steam_id"):
                    tk.Label(row, text=t("comp_captain"), bg=PANEL, fg=ACCENT,
                             font=self.f_small).pack(side="left", padx=(6, 0))
            tk.Frame(col, bg=PANEL, height=6).pack(fill="x")

    def _draw_coin(self, parent):
        s = self.session
        box = tk.Frame(parent, bg=WHITE)
        box.pack(fill="x", pady=(14, 0))
        tk.Label(box, text=t("comp_coin_title"), bg=WHITE, fg=BLACK, font=self.f_head).pack()

        self._coin_canvas = tk.Canvas(box, width=90, height=90, bg=WHITE,
                                      highlightthickness=0, bd=0)
        self._coin_canvas.pack(pady=6)
        self._paint_coin(t("comp_heads")[:1].upper() if s.coin_result != "tails" else t("comp_tails")[:1].upper())

        if s.stage == "flipping":
            tk.Label(box, text=t("comp_coin_flipping"), bg=WHITE, fg=GREY).pack()
            self._spin_coin()
            return
        # The ONE designated captain flips for everyone; everyone else waits for their name.
        if not s.i_am_coin_captain():
            tk.Label(box, text=t("comp_coin_wait", name=s.coin_captain_name() or "?"),
                     bg=WHITE, fg=GREY).pack()
            return
        tk.Label(box, text=t("comp_coin_pick"), bg=WHITE, fg=BLACK).pack(pady=(0, 6))
        btns = tk.Frame(box, bg=WHITE)
        btns.pack()
        self._button(btns, t("comp_heads"), lambda: s.pick_coin("heads"), primary=True).pack(side="left", padx=4)
        self._button(btns, t("comp_tails"), lambda: s.pick_coin("tails"), primary=True).pack(side="left", padx=4)

    def _draw_side(self, parent):
        """The side advantage: whoever holds it picks attack or defend (the selector that used
        to be auto-assigned); the other team waits to be told which side it is on."""
        s = self.session
        box = tk.Frame(parent, bg=WHITE)
        box.pack(fill="x", pady=(14, 0))
        tk.Label(box, text=t("comp_side_title"), bg=WHITE, fg=BLACK, font=self.f_head).pack(pady=(0, 8))
        picker_name = s.captain_name(s.side_picker) or "?"
        # captain OF THE PICKING SIDE, not 'my team and I am a captain': one person can
        # captain both sides when a team is empty (buildLobby falls back to team1[0]).
        if not s.i_am_captain_of(s.side_picker):
            tk.Label(box, text=t("comp_side_wait", name=picker_name), bg=WHITE, fg=GREY).pack()
            return
        tk.Label(box, text=t("comp_side_pick"), bg=WHITE, fg=BLACK).pack(pady=(0, 6))
        btns = tk.Frame(box, bg=WHITE)
        btns.pack()
        self._button(btns, t("comp_side_attack"), lambda: s.choose_side("attack"), primary=True).pack(side="left", padx=4)
        self._button(btns, t("comp_side_defend"), lambda: s.choose_side("defend"), primary=True).pack(side="left", padx=4)

    def _paint_coin(self, letter):
        c = self._coin_canvas
        if c is None:
            return
        c.delete("all")
        c.create_oval(6, 6, 84, 84, fill="#E8C64F", outline="#B99A2E", width=3)
        c.create_text(45, 46, text=letter, font=self.f_big, fill="#6B5410")

    def _spin_coin(self):
        """Alternate the face while the toss is in the air."""
        faces = [t("comp_heads")[:1].upper(), t("comp_tails")[:1].upper()]
        state = {"i": 0}

        def step():
            if self.session.stage != "flipping":
                return
            try:
                if self._coin_canvas is None or not self._coin_canvas.winfo_exists():
                    return
            except Exception:       # noqa: BLE001
                return
            self._paint_coin(faces[state["i"] % 2])
            state["i"] += 1
            self._coin_job = self.root.after(130, step)

        step()

    def _draw_choice(self, parent):
        s = self.session
        box = tk.Frame(parent, bg=WHITE)
        box.pack(fill="x", pady=(14, 0))
        winner_name = s.captain_name(s.toss_winner) or "?"
        tk.Label(box, text=t("comp_coin_result", side=t("comp_" + (s.coin_result or "heads")),
                             name=winner_name),
                 bg=WHITE, fg=BLACK, font=self.f_head).pack(pady=(0, 8))
        # captain OF THE WINNING SIDE. One person captains both when a team is empty.
        if not s.i_am_captain_of(s.toss_winner):
            tk.Label(box, text=t("comp_choice_wait", name=winner_name), bg=WHITE, fg=GREY).pack()
            return
        tk.Label(box, text=t("comp_choice_title"), bg=WHITE, fg=BLACK).pack(pady=(0, 6))
        btns = tk.Frame(box, bg=WHITE)
        btns.pack()
        self._button(btns, t("comp_choose_side"), lambda: s.choose("side"), primary=True).pack(side="left", padx=4)
        self._button(btns, t("comp_choose_ban"), lambda: s.choose("ban"), primary=True).pack(side="left", padx=4)

    def _draw_veto(self, parent):
        s = self.session
        box = tk.Frame(parent, bg=WHITE)
        box.pack(fill="both", expand=True, pady=(14, 0))
        if s.stage == "ready":
            tk.Label(box, text=t("comp_veto_done", map=s.map), bg=WHITE, fg=BLACK,
                     font=self.f_sub).pack()
        else:
            mine = s.i_am_captain_of(s.ban_turn)
            head = t("comp_veto_your_turn") if mine else t(
                "comp_veto_turn", name=s.captain_name(s.ban_turn) or "?")
            tk.Label(box, text=t("comp_veto_title"), bg=WHITE, fg=BLACK, font=self.f_head).pack()
            tk.Label(box, text=head, bg=WHITE, fg=AMBER if mine else GREY).pack(pady=(0, 6))

        banned = {m: team for team, m in s.bans}
        grid = tk.Frame(box, bg=WHITE)
        grid.pack(fill="x")
        for i, name in enumerate(self.map_pool()):
            is_banned = name in banned
            is_final = s.map == name
            fg = MUTED if is_banned else BLACK
            row = tk.Frame(grid, bg=SELECT_BG if is_final else WHITE,
                           cursor="hand2" if (not is_banned and s.stage == "veto"
                                              and s.i_am_captain_of(s.ban_turn))
                           else "arrow")
            row.grid(row=i, column=0, sticky="ew", pady=1)
            grid.columnconfigure(0, weight=1)
            label = tk.Label(row, text=name, bg=row["bg"], fg=fg, anchor="w", padx=8, pady=3,
                             font=self.f_head if is_final else None)
            if is_banned:
                label.configure(text="\u00d7 " + name)   # banned: a cross, not a dash (Sam, 2026-09-14)
            label.pack(side="left", fill="x", expand=True)
            if is_banned:
                tk.Label(row, text=t("comp_banned_by", n=banned[name]), bg=row["bg"],
                         fg=MUTED, font=self.f_small, padx=8).pack(side="right")
            if not is_banned and s.stage == "veto" and s.i_am_captain_of(s.ban_turn):
                for w in (row, label):
                    w.bind("<Button-1>", lambda _e, m=name: s.ban(m))

    def _draw_chat(self, parent, channel="team"):
        """One chat box. `channel` is the only difference between the two: which log it reads,
        the title over it, and which log a line typed into it lands in."""
        s = self.session
        title = t("comp_chat_title") if channel == "team" else t("comp_chat_all_title")
        tk.Label(parent, text=title, bg=WHITE, fg=BLACK,
                 font=self.f_head, anchor="w").pack(fill="x")
        box = tk.Text(parent, bg=PANEL, fg=BLACK, relief="flat", wrap="word", height=12,
                      padx=6, pady=6, highlightbackground=PANEL_LINE, highlightthickness=1)
        box.pack(fill="both", expand=True, pady=(4, 6))
        for line in s.chat_log(channel):
            # display_name, not the name that was typed: an enemy speaking in all chat is
            # their call sign here as well, or the lobby's anonymity lasts one message.
            who = s.display_name(line) if line.get("name") else ""
            body = line.get("text") or ""
            box.insert("end", (f"{who}: {body}\n" if who else f"{body}\n"))
        box.configure(state="disabled")
        box.see("end")

        entry_row = tk.Frame(parent, bg=WHITE)
        entry_row.pack(fill="x")
        entry = tk.Entry(entry_row, bg=WHITE, fg=BLACK, relief="solid", bd=1)
        entry.pack(side="left", fill="x", expand=True, ipady=3)
        if channel == "team":
            self._chat_entry = entry

        def send(_e=None):
            s.send_chat(entry.get(), channel)
            entry.delete(0, "end")

        entry.bind("<Return>", send)
        self._button(entry_row, t("comp_chat_send"), send).pack(side="left", padx=(6, 0))

    # ------------------------------------------------- live
    def _draw_live(self):
        s = self.session
        inner = self._centre()
        if getattr(s, "live", False):
            self._button(inner, t("comp_relaunch" if s._i_am_host() else "comp_reconnect"),
                         s.relaunch_game).pack(pady=(0, 8))
            if s.error:
                tk.Label(inner, text=s.error, bg=WHITE, fg=RED,
                         wraplength=520, justify="center").pack(pady=(0, 8))
        tk.Label(inner, text=t("comp_live_title"), bg=WHITE, fg=BLACK, font=self.f_sub).pack()
        for waiting in getattr(s, "reconnect_waiting", []):
            remaining = max(0, int((float(waiting.get("deadline") or 0) - time.time() * 1000) / 1000))
            tk.Label(inner, text=t("comp_reconnect_wait", time=format_duration(remaining)), bg=WHITE, fg=AMBER).pack()
        tk.Label(inner, text=t("comp_map", map=s.map or "?"), bg=WHITE, fg=BLACK,
                 font=self.f_big).pack(pady=(2, 8))
        host_name = (s.host or {}).get("name", "?")
        host_ping = (s.host or {}).get("ping")
        tk.Label(inner, text=t("comp_host", name=host_name, ping=host_ping if host_ping is not None else "—"),
                 bg=WHITE, fg=GREY).pack()
        tk.Label(inner, text=t("comp_join_hint", name=host_name), bg=WHITE, fg=GREY,
                 wraplength=520, justify="center").pack(pady=(4, 14))
        if s.vote:
            self._draw_vote(inner)
        else:
            self._button(inner, t("comp_report"), s.start_vote).pack(pady=(16, 0))

        # preview only: a real match ends when the gamemode reports its scoreboard
        if getattr(s, "mock", False):
            self._button(inner, t("comp_preview_finish"), s.finish).pack(pady=(14, 0))

    def _draw_vote(self, parent):
        s = self.session
        card = self._card(parent)
        card.pack(fill="x", pady=(16, 0))
        tk.Label(card, text=t("comp_vote_title"), bg=PANEL, fg=BLACK,
                 font=self.f_head).pack(padx=10, pady=(8, 2))
        tk.Label(card, text=t("comp_vote_body"), bg=PANEL, fg=GREY, wraplength=460,
                 justify="center").pack(padx=10)
        tk.Label(card, text=t("comp_vote_count", n=s.vote["yes"], needed=VOTE_NEEDED),
                 bg=PANEL, fg=BLACK).pack(pady=(6, 4))
        if s.vote.get("pending"):
            tk.Label(card, text=t("comp_vote_pending"), bg=PANEL, fg=GREY).pack(pady=(0, 10))
        elif not s.vote["voted"]:
            btns = tk.Frame(card, bg=PANEL)
            btns.pack(pady=(0, 10))
            self._button(btns, t("comp_vote_yes"), lambda: s.cast_vote(True), bg=PANEL).pack(side="left", padx=4)
            self._button(btns, t("comp_vote_no"), lambda: s.cast_vote(False), bg=PANEL).pack(side="left", padx=4)
        else:
            tk.Label(card, text=t("comp_vote_cast"), bg=PANEL, fg=GREY).pack(pady=(0, 10))

    # ------------------------------------------------- result
    def _draw_result(self):
        s = self.session
        r = s.result or {}
        inner = self._centre()
        if r.get("voided"):
            tk.Label(inner, text=t("comp_result_void"), bg=WHITE, fg=AMBER, font=self.f_big).pack()
            tk.Label(inner, text=t("comp_result_void_body"), bg=WHITE, fg=GREY,
                     wraplength=520, justify="center").pack(pady=(4, 16))
        else:
            won = r.get("won")
            tk.Label(inner, text=t("comp_result_win") if won else t("comp_result_loss"),
                     bg=WHITE, fg=GREEN if won else RED, font=self.f_big).pack()
            a, b = r.get("score", (0, 0))
            tk.Label(inner, text=f"{a} : {b}", bg=WHITE, fg=BLACK, font=self.f_sub).pack(pady=(2, 10))
            # levels 1-10: arrows only, never a number (Sam)
            row = tk.Frame(inner, bg=WHITE)
            row.pack(pady=(0, 16))
            self._level_badge(row, s.me.get("level"), rank=s.me.get("rank"),
                              division=s.me.get("division"), placing=s.me.get("placing")).pack(side="left", padx=(0, 8))
            self._arrows(row, r.get("delta", 0)).pack(side="left")
        self._draw_game_close(inner)
        self._button(inner, t("comp_back"), s.leave_result, primary=True).pack()

    # The close is silent while it has nothing to say. "armed" draws nothing on purpose: the
    # player is reading a scoreboard, and a countdown to their game being shut is a distraction
    # from it - they find out when it happens, which is what they asked for.
    CLOSE_LINES = {"closing": ("comp_game_closing", GREY),
                   "closed": ("comp_game_closed", GREY),
                   "forced": ("comp_game_closed", GREY),
                   "failed": ("comp_game_close_failed", AMBER)}

    def _draw_game_close(self, parent):
        line = self.CLOSE_LINES.get(getattr(self.session, "game_close", ""))
        if not line:
            return
        key, colour = line
        tk.Label(parent, text=t(key), bg=WHITE, fg=colour, wraplength=520,
                 justify="center").pack(pady=(0, 12))

    # ================================================================ match history
    # Sam, 2026-09-14: "this tab should be something someone can look at while staying in the
    # searching for game queue or at any time during the pre-game selection phases without
    # disturbing anything with that functionality."
    #
    # That sentence is the whole design. Nothing below touches the session: no method here
    # queues, cancels, accepts, bans or leaves anything. The view is a panel flag, the list is
    # in a frame of its own that the per-second body redraw never reaches, and the only thing
    # that pulls a reader out of it is a found match (_phase_changed).

    def _history_fingerprint(self):
        """What the list is showing, boiled down. Redraw only when this changes.

        Deliberately NOT the whole list: on_change runs once a second while queued, and
        comparing fifty rows every tick to avoid a redraw would cost more than the redraw."""
        s = self.session
        rows = s.history or []
        return (s.history is None, bool(s.history_loading), s.history_error, len(rows),
                getattr(s, "history_seq", 0))

    def _history_when(self, row):
        """When a match was played. Numeric on purpose: a month name would need seven
        translations and still be ambiguous between en and de."""
        ended = row.get("ended")
        if not ended:
            return "?"             # unknown is unknown; 1970 is a lie the list would tell
        try:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ended) / 1000.0))
        except Exception:          # noqa: BLE001 - a malformed row must not blank the list
            return "?"

    def _history_outcome(self, row):
        """(headline, colour) for one match, from the player's own point of view."""
        if row.get("voided") or row.get("outcome") == "voided":
            return (t("comp_result_void"), AMBER)
        if (row.get("outcome") or "") == "cancelled":
            blamed = bool(row.get("blamed"))
            reason = row.get("reason") or ""
            if reason == "no_show":
                return ((t("comp_history_no_show"), RED) if blamed
                        else (t("comp_history_cancelled"), GREY))
            if reason == "declined":
                return ((t("comp_history_declined"), RED) if blamed
                        else (t("comp_history_cancelled"), GREY))
            if reason == "abandoned":
                return ((t("comp_history_abandoned"), RED) if blamed
                        else (t("comp_history_cancelled"), GREY))
            return (t("comp_history_cancelled"), GREY)
        won = row.get("won")
        if won is True:
            return (t("comp_result_win"), GREEN)
        if won is False:
            return (t("comp_result_loss"), RED)
        # Played, but no scoreboard reported it. Honest rather than blank: the match happened.
        return (t("comp_history_played"), BLACK)

    # ---------------------------------------------------------------- the alert strip
    def _history_alert(self):
        """What the match going on behind this view needs to say, or None.

        (text, colour, urgent). "found" is absent on purpose: that phase never gets here,
        because _phase_changed has already taken the player back to Play."""
        s = self.session
        phase = s.phase
        # Anything the player is OWED comes first, whatever phase it happened in. These lines
        # live on the play screens, which are not drawn while this view is up, so without this
        # a lost queue, a released match or a no-show penalty would simply blank the strip and
        # the player would go on believing they were still queued.
        if not getattr(s, "connected", True) and phase not in ("signed_out", "signing_in"):
            return (t("comp_live_lost"), RED, True)
        if s.error:
            return (s.error, RED, True)
        banned = s.banned_left()
        if banned:
            return (t("comp_banned_left", time=format_clock(banned)), RED, True)
        if phase in ("queued", "checking"):
            return (t("comp_history_alert_queued", time=format_clock(s.queue_seconds)), ACCENT, False)
        if phase == "lobby":
            # The coin flip, the side/ban choice and the veto each stall the match for all ten
            # exactly as hard, and each has its own deadline in PHASE_LIMITS. Warn whoever has to
            # act - the designated captain for the flip, the toss winner for the choice, the side
            # picker for the side, the banning captain for a veto turn.
            captain = s.i_am_captain()
            if captain and s.stage == "veto" and s.ban_turn == s.my_team():
                return (t("comp_history_alert_turn"), RED, True)
            if ((s.stage == "coin" and s.i_am_coin_captain())
                    or (captain and s.stage == "choice" and s.toss_winner == s.my_team())
                    or (captain and s.stage == "side" and s.side_picker == s.my_team())):
                return (t("comp_history_alert_action"), RED, True)
            return (t("comp_history_alert_lobby"), AMBER, False)
        if phase == "connecting":
            return (t("comp_history_alert_connect", time=format_clock(s.connect_left)), RED, True)
        if phase == "live":
            return (t("comp_history_alert_live"), AMBER, False)
        if phase == "result":
            return (t("comp_history_alert_result"), ACCENT, True)
        return None

    def _draw_alert(self, holder):
        """One strip, rebuilt every tick so its countdown is live. Everything else stays put.

        Drawn into whichever view is in front - Match History or the profile - because the
        thing it reports on (a match running behind this screen) is the same either way."""
        self._clear(holder)
        alert = self._history_alert()
        if alert is None:
            return
        text, colour, urgent = alert
        bar = tk.Frame(holder, bg=PANEL, highlightbackground=colour,
                       highlightthickness=1, bd=0)
        bar.pack(fill="x", pady=(0, 10))
        tk.Label(bar, text=text, bg=PANEL, fg=colour,
                 font=self.f_head if urgent else None, anchor="w").pack(
                     side="left", padx=10, pady=6)
        self._button(bar, t("comp_history_back_to_play"),
                     lambda: self._show_view("play"),
                     primary=urgent, bg=PANEL).pack(side="right", padx=8, pady=5)

    # ---------------------------------------------------------------- the list
    def _scroller(self, parent):
        """A vertical scroll area. Returns the frame to pack rows into."""
        holder = tk.Frame(parent, bg=WHITE)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=WHITE, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=WHITE)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        # The scroller most recently built, which is the one _bind_wheel is about to bind: the
        # two calls always come in that order, in whichever view is being drawn.
        self._scroll_canvas = canvas

        def resized(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(window, width=canvas.winfo_width())
            except Exception:       # noqa: BLE001 - mid-teardown
                pass

        inner.bind("<Configure>", resized)
        canvas.bind("<Configure>", resized)
        return inner

    def _bind_wheel(self, widget):
        """Tk does not bubble the wheel, so every row has to be told about it by hand.

        Bound per widget rather than with bind_all: bind_all would keep scrolling this list
        while the player is on a different tab entirely."""
        canvas = self._scroll_canvas
        if canvas is None:
            return

        def wheel(event):
            try:
                if not canvas.winfo_exists():
                    return
            except Exception:       # noqa: BLE001
                return
            if getattr(event, "num", 0) == 4:
                step = -1
            elif getattr(event, "num", 0) == 5:
                step = 1
            else:
                step = -1 if (getattr(event, "delta", 0) or 0) > 0 else 1
            canvas.yview_scroll(step, "units")

        stack = [widget]
        while stack:
            w = stack.pop()
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                w.bind(sequence, wheel)
            stack.extend(w.winfo_children())

    def _draw_history_list(self):
        self._clear(self._hist_content)
        self._scroll_canvas = None
        s = self.session
        rows = s.history or []

        head = tk.Frame(self._hist_content, bg=WHITE)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text=t("comp_history_title"), bg=WHITE, fg=BLACK,
                 font=self.f_sub, anchor="w").pack(side="left")
        self._button(head, t("comp_history_refresh"),
                     lambda: s.load_history(force=True)).pack(side="right")
        if rows:
            tk.Label(head, text=t("comp_history_count", n=len(rows)), bg=WHITE, fg=GREY,
                     font=self.f_small).pack(side="right", padx=(0, 10))

        if s.history_error:
            tk.Label(self._hist_content, text=s.history_error, bg=WHITE, fg=RED,
                     font=self.f_small, anchor="w", wraplength=760,
                     justify="left").pack(fill="x", pady=(0, 6))

        if s.history_loading and not rows:
            tk.Label(self._hist_content, text=t("comp_history_loading"), bg=WHITE,
                     fg=GREY).pack(pady=40)
            return
        if s.history is None:
            # Asked and never answered. The error line above already says why; do not follow it
            # with a spinner that will never stop, and do not claim the player has no matches.
            tk.Label(self._hist_content, text=t("comp_history_failed"), bg=WHITE,
                     fg=GREY, wraplength=460, justify="center").pack(pady=40)
            return

        if not rows:
            # The established "nothing here yet" idiom: grey, centred, no card and no alarm.
            box = tk.Frame(self._hist_content, bg=WHITE)
            box.pack(fill="both", expand=True)
            inner = tk.Frame(box, bg=WHITE)
            inner.place(relx=0.5, rely=0.4, anchor="center")
            tk.Label(inner, text=t("comp_history_empty"), bg=WHITE, fg=BLACK,
                     font=self.f_sub).pack(pady=(0, 6))
            tk.Label(inner, text=t("comp_history_empty_hint"), bg=WHITE, fg=GREY,
                     wraplength=460, justify="center").pack()
            return

        # The score and the Elo change do not exist yet: nothing reports a scoreboard (memory
        # section 3). Say so once, at the top, rather than leaving every row's dash unexplained.
        if any(r.get("won") is None and not r.get("preview") and r.get("outcome") != "voided" for r in rows):
            tk.Label(self._hist_content, text=t("comp_history_pending_note"), bg=WHITE, fg=AMBER,
                     font=self.f_small, anchor="w", wraplength=760,
                     justify="left").pack(fill="x", pady=(0, 8))

        list_frame = self._scroller(self._hist_content)
        for i, row in enumerate(rows):
            self._draw_history_row(list_frame, row, i)
        self._bind_wheel(list_frame)

    def _draw_history_row(self, parent, row, index):
        """One match: outcome, map, when, and what it cost. Click for the rest."""
        striped = PANEL if index % 2 else WHITE
        line = tk.Frame(parent, bg=striped, cursor="hand2")
        line.pack(fill="x", pady=1)

        text, colour = self._history_outcome(row)
        tk.Label(line, text=text, bg=striped, fg=colour, font=self.f_head, width=12,
                 anchor="w", padx=8, pady=5).pack(side="left")
        tk.Label(line, text=row.get("map") or t("comp_history_no_map"), bg=striped,
                 fg=BLACK if row.get("map") else MUTED, anchor="w", width=14).pack(side="left")

        team = row.get("team") or 0
        side = row.get("side") or ""
        bits = []
        if team:
            bits.append(t("comp_history_team", n=team))
        if side:
            bits.append(t("comp_history_side_attack") if side == "attack"
                        else t("comp_history_side_defend"))
        tk.Label(line, text="  ".join(bits), bg=striped, fg=GREY, font=self.f_small,
                 anchor="w", width=18).pack(side="left")

        # right-hand side, packed right to left
        try:
            elo = abs(int(row.get("elo") or 0))
        except (TypeError, ValueError):
            elo = 0                # server rows are data, not promises
        if elo:
            tk.Label(line, text=t("comp_history_elo", n=elo), bg=striped, fg=RED,
                     font=self.f_small, padx=8).pack(side="right")
        score = row.get("score")
        if isinstance(score, (list, tuple)) and len(score) == 2:
            tk.Label(line, text="%s : %s" % (score[0], score[1]), bg=striped, fg=BLACK,
                     font=self.f_head, padx=8).pack(side="right")
        elif not row.get("preview"):
            tk.Label(line, text=t("comp_history_pending"), bg=striped, fg=MUTED,
                     font=self.f_small, padx=8).pack(side="right")
        tk.Label(line, text=self._history_when(row), bg=striped, fg=GREY,
                 font=self.f_small, padx=8).pack(side="right")

        match_id = row.get("id") or ""
        for w in [line] + list(line.winfo_children()):
            w.bind("<Button-1>", lambda _e, m=match_id, r=row: self._show_match_detail(m, r))

    # ---------------------------------------------------------------- the detail pop-up
    def _show_match_detail(self, match_id, row=None):
        """Everything the server kept about one match, in the same small-window idiom as the
        Ruleset and integrity notes. Opens immediately and fills in when the fetch lands."""
        win = tk.Toplevel(self.root)
        win.title(t("comp_history_detail_title"))
        win.configure(bg=WHITE)
        win.transient(self.root)
        body = tk.Frame(win, bg=WHITE)
        body.pack(fill="both", expand=True, padx=16, pady=16)
        tk.Label(body, text=t("comp_history_loading"), bg=WHITE, fg=GREY).pack()
        tk.Button(win, text=t("close"), bg=WHITE, fg=BLACK, activebackground=WHITE,
                  padx=14, command=win.destroy).pack(pady=(0, 14))
        win.bind("<Escape>", lambda _e: win.destroy())
        self.history_window = win

        def filled(record):
            try:
                if not win.winfo_exists():
                    return
            except Exception:       # noqa: BLE001
                return
            self._clear(body)
            if not record:
                tk.Label(body, text=t("comp_history_detail_gone"), bg=WHITE, fg=GREY,
                         wraplength=420, justify="left").pack(anchor="w")
                return
            self._draw_match_detail(body, record, row or {})

        self.session.load_match(match_id, filled)
        return win

    @staticmethod
    def _history_kd(entry):
        """One player's "K / D" for the detail pop-up, or "" when we were never told.

        Kills are the game's own NET count and can be negative (a team kill takes one off), so
        nothing here clamps; team kills are appended only when a kill feed actually reached the
        match, because a "TK 0" we cannot back up is a claim rather than a reading.
        """
        if not isinstance(entry, dict):
            return ""
        kills, deaths = entry.get("kills"), entry.get("deaths")
        if not isinstance(kills, int) or not isinstance(deaths, int):
            return ""
        out = "%d / %d" % (kills, deaths)
        tk_count = entry.get("team_kills")
        if isinstance(tk_count, int) and tk_count > 0:
            out += "   TK %d" % tk_count
        return out

    def _draw_match_detail(self, parent, record, row):
        head_text, head_colour = self._history_outcome(row or {})
        tk.Label(parent, text=head_text, bg=WHITE, fg=head_colour,
                 font=self.f_sub, anchor="w").pack(anchor="w")
        line = " · ".join(x for x in (record.get("map") or t("comp_history_no_map"),
                                      self._history_when({"ended": record.get("ended")})) if x)
        tk.Label(parent, text=line, bg=WHITE, fg=GREY, anchor="w").pack(anchor="w", pady=(0, 10))

        # Everything below comes off the wire, and a record written by an older (or newer)
        # server is still in the store for months. Take only the shapes we can draw.
        sides = record.get("sides") if isinstance(record.get("sides"), dict) else {}
        teams = record.get("teams") if isinstance(record.get("teams"), dict) else {}
        players = [p for p in (record.get("players") or []) if isinstance(p, dict)]
        host_id = record.get("host") or ""
        mine = (self.session.me or {}).get("steam_id")
        # THE SCOREBOARD, keyed by steam id so a name row can pick up its own line. The record
        # carries it whenever the gamemode reported anything (server/live.cjs scoreboardOf); a
        # match it said nothing about has no board, and the note below says so rather than
        # drawing a column of zeroes.
        board = {}
        for entry in (record.get("scoreboard") or []):
            if isinstance(entry, dict) and entry.get("steam_id"):
                board[str(entry["steam_id"])] = entry

        cols = tk.Frame(parent, bg=WHITE)
        cols.pack(fill="x")
        # A match that died before the veto (a decline, an abandoned lobby) has no teams at
        # all, so grouping by team would show two empty columns and hide the very thing the
        # pop-up was opened for: who declined, and who never connected. Fall back to one list.
        grouped = [k for k in ("1", "2")
                   if any(str(p.get("team") or 0) == k for p in players) or teams.get(k)]
        for key in (grouped or [""]):
            members = ([p for p in players if str(p.get("team") or 0) == key] if key
                       else list(players))
            card = self._card(cols)
            card.pack(side="left", fill="both", expand=True, padx=(0, 8))
            side = sides.get(key) or ""
            title = t("comp_history_team", n=key) if key else t("comp_history_players")
            if side:
                title += "  ·  " + (t("comp_history_side_attack") if side == "attack"
                                    else t("comp_history_side_defend"))
            tk.Label(card, text=title, bg=PANEL, fg=BLACK, font=self.f_head,
                     anchor="w").pack(fill="x", padx=8, pady=(6, 4))
            for p in members:
                prow = tk.Frame(card, bg=PANEL)
                prow.pack(fill="x", padx=8, pady=1)
                is_me = p.get("steam_id") == mine
                tk.Label(prow, text=p.get("persona") or p.get("steam_id") or "?", bg=PANEL,
                         fg=BLACK, font=self.f_head if is_me else None,
                         anchor="w").pack(side="left")
                tags = []
                if p.get("steam_id") == host_id:
                    tags.append((t("comp_history_detail_host"), ACCENT))
                if p.get("blamed"):
                    tags.append((t("comp_history_detail_blamed"), RED))
                elif not p.get("connected") and record.get("outcome") == "cancelled":
                    tags.append((t("comp_history_detail_missing"), MUTED))
                for label, colour in tags:
                    tk.Label(prow, text=label, bg=PANEL, fg=colour,
                             font=self.f_small).pack(side="left", padx=(6, 0))
                # Their line, on the right. Packed last so it trails the tags, and absent
                # rather than zeroed when the sweep never reached this player - a "0/0" here
                # would read as a performance instead of as a gap in what we were told.
                line = self._history_kd(board.get(str(p.get("steam_id") or "")))
                if line:
                    tk.Label(prow, text=line, bg=PANEL, fg=GREY,
                             font=self.f_small).pack(side="right")
            tk.Frame(card, bg=PANEL, height=6).pack()

        bans = [b for b in (record.get("bans") or []) if isinstance(b, dict)]
        if bans:
            tk.Label(parent, text=t("comp_history_detail_veto"), bg=WHITE, fg=BLACK,
                     font=self.f_head, anchor="w").pack(anchor="w", pady=(12, 2))
            for entry in bans:
                by = entry.get("team")
                tk.Label(parent, text="× %s   %s" % (
                    entry.get("map") or "?",
                    t("comp_history_team", n=by) if by else ""), bg=WHITE, fg=MUTED,
                    font=self.f_small, anchor="w").pack(anchor="w")

        # Only when the gamemode reported nothing at all. Beside a board this line would
        # contradict the numbers directly above it.
        if record.get("score") is None and not board:
            tk.Label(parent, text=t("comp_history_pending_note"), bg=WHITE, fg=AMBER,
                     font=self.f_small, wraplength=420, justify="left",
                     anchor="w").pack(anchor="w", pady=(12, 0))
        if record.get("source") == "client":
            tk.Label(parent, text=t("comp_history_detail_source"), bg=WHITE, fg=GREY,
                     font=self.f_small, wraplength=420, justify="left",
                     anchor="w").pack(anchor="w", pady=(6, 0))

    # ================================================================== the profile view
    # Sam, 2026-09-14: "a ranked profile page where you can click on your user profile
    # picture to open a profile page that gives you stats".
    #
    # Two kinds of number live on this page and they must not be confused with each other.
    # The RANK (level, Elo) is still invented in adopt_account - there is no ranking service
    # yet - so it is drawn once, at the top, under a note that says so. Everything below the
    # rule is counted out of the real match history the server keeps, and nothing there is
    # ever filled in with a guess: a statistic the record cannot support is left out or said
    # to be missing, because a profile that rounds a blank up to zero is worse than none.
    def _profile_fingerprint(self):
        """What the profile is showing, boiled down. Redraw only when this changes.

        Same bargain as the history list: on_change runs once a second while queued, and this
        page has a scroll position worth keeping."""
        s = self.session
        me = s.me or {}
        return (me.get("steam_id"), me.get("name"), me.get("avatar"), me.get("level"),
                me.get("elo"), me.get("bdr"),
                s.history is None, bool(s.history_loading), s.history_error,
                len(s.history or []), getattr(s, "history_seq", 0),
                int(getattr(s, "penalty_count", 0) or 0), bool(s.banned_left()))

    def _draw_profile(self):
        self._clear(self._prof_content)
        self._scroll_canvas = None
        s = self.session
        me = s.me or {}
        if not me:
            # Signed out between the tick and the draw. on_change is already on its way to
            # putting the player back on Play; drawing a profile for nobody is not the fix.
            return

        page = self._scroller(self._prof_content)
        back = tk.Label(page, text="‹ " + t("comp_profile_back"), bg=WHITE, fg=ACCENT,
                        cursor="hand2", font=self.f_small, anchor="w")
        back.pack(anchor="w", pady=(0, 10))
        back.bind("<Button-1>", lambda _e: self._show_view(self._profile_from))

        self._draw_profile_head(page, me)
        tk.Frame(page, bg=LINE, height=1).pack(fill="x", pady=(14, 12))

        if s.history_error:
            tk.Label(page, text=s.history_error, bg=WHITE, fg=RED, font=self.f_small,
                     anchor="w", wraplength=700, justify="left").pack(fill="x", pady=(0, 8))
        if s.history is None:
            tk.Label(page, text=(t("comp_history_loading") if s.history_loading
                                 else t("comp_history_failed")),
                     bg=WHITE, fg=GREY, anchor="w").pack(anchor="w", pady=(0, 10))
        elif not s.history:
            tk.Label(page, text=t("comp_profile_empty"), bg=WHITE, fg=BLACK,
                     font=self.f_sub, anchor="w").pack(anchor="w")
            tk.Label(page, text=t("comp_profile_empty_hint"), bg=WHITE, fg=GREY,
                     anchor="w", wraplength=620, justify="left").pack(anchor="w", pady=(4, 0))
        else:
            self._draw_profile_stats(page, profile_stats(s.history))
        self._bind_wheel(page)

    def _draw_profile_head(self, parent, me):
        """Who this is, and the rank that does not mean anything yet."""
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x")
        self._avatar(row, me.get("name") or "?", size=72,
                     url=me.get("avatar") or "").pack(side="left", padx=(0, 14))
        who = tk.Frame(row, bg=WHITE)
        who.pack(side="left", anchor="n")

        name_row = tk.Frame(who, bg=WHITE)
        name_row.pack(anchor="w")
        self._level_badge(name_row, me.get("level"), size=30, rank=me.get("rank"),
                          division=me.get("division"), placing=me.get("placing")).pack(side="left", padx=(0, 8))
        tk.Label(name_row, text=me.get("name") or "?", bg=WHITE, fg=BLACK,
                 font=self.f_big).pack(side="left")

        rank = self._rank_text(me)
        tk.Label(who, text=rank, bg=WHITE, fg=GREY, anchor="w").pack(anchor="w", pady=(4, 0))

        steam_id = str(me.get("steam_id") or "")
        id_row = tk.Frame(who, bg=WHITE)
        id_row.pack(anchor="w", pady=(2, 0))
        if steam_id:
            tk.Label(id_row, text=t("comp_profile_id", id=steam_id), bg=WHITE, fg=MUTED,
                     font=self.f_small).pack(side="left", padx=(0, 10))
            # Only a SteamID64 is ever turned into a URL. The persona and the avatar arrive
            # over the network; the id is the one field Steam OpenID actually proved.
            if steam_id.isdigit():
                link = tk.Label(id_row, text=t("comp_profile_steam"), bg=WHITE, fg=ACCENT,
                                cursor="hand2", font=self.f_small)
                link.pack(side="left")
                link.bind("<Button-1>", lambda _e, i=steam_id: webbrowser.open(
                    "https://steamcommunity.com/profiles/" + i))

    def _draw_profile_stats(self, parent, stats):
        """Everything counted out of the real record."""
        tk.Label(parent, text=t("comp_profile_sample", n=stats["recorded"]), bg=WHITE, fg=GREY,
                 font=self.f_small, anchor="w").pack(anchor="w", pady=(0, 8))

        tiles = tk.Frame(parent, bg=WHITE)
        tiles.pack(fill="x", pady=(0, 12))
        for value, label in ((stats["played"], t("comp_profile_completed")),
                             (stats["cancelled"], t("comp_profile_cancelled")),
                             (stats["hosted"], t("comp_profile_hosted")),
                             (stats["at_fault"], t("comp_profile_at_fault"))):
            self._stat_tile(tiles, str(value), label)

        # The record. Every `won` is null until the gamemode reports a scoreboard, so this is
        # normally the honest blank rather than 0 - 0, which would read as a played record.
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 12))
        tk.Label(card, text=t("comp_profile_record"), bg=PANEL, fg=BLACK, font=self.f_head,
                 anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        if stats["win_rate"] is None:
            tk.Label(card, text=t("comp_profile_record_none"), bg=PANEL, fg=MUTED,
                     anchor="w", wraplength=680, justify="left").pack(fill="x", padx=10)
        else:
            line = tk.Frame(card, bg=PANEL)
            line.pack(fill="x", padx=10)
            tk.Label(line, text=str(stats["wins"]), bg=PANEL, fg=GREEN,
                     font=self.f_big).pack(side="left")
            tk.Label(line, text=" / ", bg=PANEL, fg=MUTED, font=self.f_big).pack(side="left")
            tk.Label(line, text=str(stats["losses"]), bg=PANEL, fg=RED,
                     font=self.f_big).pack(side="left")
            tk.Label(line, text="   " + t("comp_profile_winrate", n=stats["win_rate"]),
                     bg=PANEL, fg=BLACK, font=self.f_head).pack(side="left")
        if stats["undecided"]:
            tk.Label(card, text=t("comp_profile_unscored", n=stats["undecided"]), bg=PANEL,
                     fg=AMBER, font=self.f_small, anchor="w", wraplength=680,
                     justify="left").pack(fill="x", padx=10, pady=(2, 0))
        tk.Frame(card, bg=PANEL, height=8).pack()

        self._draw_profile_form(parent, stats["form"])
        self._draw_profile_conduct(parent, stats)
        self._draw_profile_maps(parent, stats)

    def _stat_tile(self, parent, value, label):
        card = self._card(parent)
        card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        tk.Label(card, text=value, bg=PANEL, fg=BLACK, font=self.f_big).pack(pady=(10, 0))
        tk.Label(card, text=label, bg=PANEL, fg=GREY, font=self.f_small,
                 wraplength=150, justify="center").pack(pady=(0, 10), padx=6)
        return card

    def _draw_profile_form(self, parent, form):
        """The last few matches as pips, newest first, with the legend spelled out.

        Colour alone would not survive a colour-blind player or a screenshot, so every pip
        that appears is named underneath it."""
        if not form:
            return
        colours = {FORM_WIN: GREEN, FORM_LOSS: RED, FORM_PLAYED: ACCENT,
                   FORM_CANCELLED: LINE, FORM_FAULT: AMBER}
        names = {FORM_WIN: t("comp_result_win"), FORM_LOSS: t("comp_result_loss"),
                 FORM_PLAYED: t("comp_profile_form_played"),
                 FORM_CANCELLED: t("comp_history_cancelled"),
                 FORM_FAULT: t("comp_profile_form_fault")}
        tk.Label(parent, text=t("comp_profile_form"), bg=WHITE, fg=BLACK, font=self.f_head,
                 anchor="w").pack(anchor="w")
        strip = tk.Canvas(parent, width=len(form) * 22, height=20, bg=WHITE,
                          highlightthickness=0, bd=0)
        strip.pack(anchor="w", pady=(4, 4))
        for i, kind in enumerate(form):
            x = i * 22
            strip.create_rectangle(x, 3, x + 16, 19, fill=colours.get(kind, LINE), outline="")
        legend = tk.Frame(parent, bg=WHITE)
        legend.pack(anchor="w", pady=(0, 12))
        for kind in (FORM_WIN, FORM_LOSS, FORM_PLAYED, FORM_CANCELLED, FORM_FAULT):
            if kind not in form:
                continue            # only explain the pips that are actually on screen
            key = tk.Frame(legend, bg=WHITE)
            key.pack(side="left", padx=(0, 14))
            swatch = tk.Canvas(key, width=10, height=10, bg=WHITE, highlightthickness=0, bd=0)
            swatch.create_rectangle(0, 0, 10, 10, fill=colours[kind], outline="")
            swatch.pack(side="left", padx=(0, 4))
            tk.Label(key, text=names[kind], bg=WHITE, fg=GREY,
                     font=self.f_small).pack(side="left")

    def _draw_profile_conduct(self, parent, stats):
        """What the player has cost other people, and what it has cost them.

        On the profile rather than buried in the history because the ladder it feeds (a 5 min
        ban, then 15, then 30) is invisible otherwise: a player who cannot see their own count
        cannot see the next rung coming."""
        card = self._card(parent)
        card.pack(fill="x", pady=(0, 12))
        tk.Label(card, text=t("comp_profile_conduct"), bg=PANEL, fg=BLACK, font=self.f_head,
                 anchor="w").pack(fill="x", padx=10, pady=(8, 4))

        banned = self.session.banned_left()
        if banned:
            tk.Label(card, text=t("comp_profile_banned", time=format_clock(banned)), bg=PANEL,
                     fg=RED, font=self.f_head, anchor="w").pack(fill="x", padx=10, pady=(0, 4))
        if not stats["at_fault"] and not stats["elo_lost"] and not banned:
            tk.Label(card, text=t("comp_profile_conduct_clean"), bg=PANEL, fg=GREEN,
                     anchor="w").pack(fill="x", padx=10)
        else:
            for count, label in ((stats["no_show"], t("comp_history_no_show")),
                                 (stats["declined"], t("comp_history_declined")),
                                 (stats["abandoned"], t("comp_history_abandoned"))):
                if count:
                    tk.Label(card, text="%d  %s" % (count, label), bg=PANEL, fg=RED,
                             anchor="w").pack(fill="x", padx=10)
            if stats["elo_lost"]:
                tk.Label(card, text=t("comp_profile_elo_lost", n=stats["elo_lost"]), bg=PANEL,
                         fg=RED, anchor="w").pack(fill="x", padx=10, pady=(2, 0))
        # The next rung is the server's number, not ours: penalty_next arrives with the
        # penalty event and is what the CURRENT count would actually cost.
        count = int(getattr(self.session, "penalty_count", 0) or 0)
        if count:
            nxt = int(getattr(self.session, "penalty_next", NO_SHOW_BAN_SECONDS) or
                      NO_SHOW_BAN_SECONDS)
            tk.Label(card, text=t("comp_profile_next_ban", time=format_duration(nxt)), bg=PANEL,
                     fg=AMBER, font=self.f_small, anchor="w", wraplength=680,
                     justify="left").pack(fill="x", padx=10, pady=(4, 0))
        tk.Frame(card, bg=PANEL, height=8).pack()

    def _draw_profile_maps(self, parent, stats):
        """Maps played, and the attack/defend split, both out of completed matches only."""
        sides = stats["attack"] + stats["defend"]
        if sides:
            row = tk.Frame(parent, bg=WHITE)
            row.pack(fill="x", pady=(0, 10))
            tk.Label(row, text=t("comp_profile_sides"), bg=WHITE, fg=BLACK, font=self.f_head,
                     anchor="w").pack(side="left", padx=(0, 10))
            tk.Label(row, text="%s %d   %s %d" % (t("comp_history_side_attack"),
                                                  stats["attack"],
                                                  t("comp_history_side_defend"),
                                                  stats["defend"]),
                     bg=WHITE, fg=GREY, anchor="w").pack(side="left")
        if not stats["maps"]:
            return
        tk.Label(parent, text=t("comp_profile_maps"), bg=WHITE, fg=BLACK, font=self.f_head,
                 anchor="w").pack(anchor="w", pady=(0, 4))
        top = stats["maps"][0][1] or 1
        for name, count in stats["maps"]:
            line = tk.Frame(parent, bg=WHITE)
            line.pack(fill="x", pady=1)
            tk.Label(line, text=name, bg=WHITE, fg=BLACK, width=14, anchor="w").pack(side="left")
            bar = tk.Canvas(line, width=220, height=12, bg=WHITE, highlightthickness=0, bd=0)
            bar.pack(side="left", padx=(0, 8))
            bar.create_rectangle(0, 2, max(4, int(220.0 * count / top)), 12,
                                 fill=ACCENT, outline="")
            tk.Label(line, text=str(count), bg=WHITE, fg=GREY,
                     font=self.f_small).pack(side="left")
