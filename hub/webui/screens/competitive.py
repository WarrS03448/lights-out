"""Competitive screen — the Python half: its snapshot slice and its bridge verbs.

OWNED BY THE COMPETITIVE SCREEN. This is the only Python screen module the competitive agent
edits. It contributes the ``comp`` and ``party`` slices of the state snapshot and registers the
sign-in / matchmaking / party verbs the JS screen (static/screens/competitive.js) calls. Ported
from the pre-modular snapshot.py / bridge.py so the screen behaves identically.

Shared identity/chrome (``auth``, ``status``, ``view``, ``lang``, ``strings``) is assembled by the
core (hub/webui/snapshot.py); this file only adds the competitive-specific slices and verbs.
"""
from . import register_snapshot, register_verbs
from ...competitive import COMPETITIVE_MODE_ID
from ...activity import files_busy
from ...competitive import (MAX_PARTY, mask_party_code, VOTE_NEEDED, NO_SHOW_ELO,
                            NO_SHOW_BAN_SECONDS, format_duration, outdated_line, TIMED_STAGES)


# ---------------------------------------------------------------- snapshot slice
def _member(entry: dict, leader_id: str) -> dict:
    steam_id = str(entry.get("steam_id") or "")
    return {
        "steam_id": steam_id,
        "name": entry.get("name") or steam_id,
        # The Steam avatar URL. The page turns it into /avatar?u=... - it never loads it from
        # steamstatic.com itself, which the page's CSP would refuse anyway.
        "avatar": entry.get("avatar") or "",
        "level": entry.get("level"),
        "ping": entry.get("ping"),
        "is_leader": bool(leader_id) and steam_id == leader_id,
    }


def _invite_candidates(session, member_ids) -> list:
    """My friends, as rows the Invite picker can draw.

    EVERY friend is listed, not just the ones who can be invited: a picker that hides the offline
    half answers "where is she?" with nothing at all. The row carries why it cannot be pressed
    instead - offline, or already sitting in the party.
    """
    sent = set(getattr(session, "invite_sent", ()) or ())
    rows = []
    for friend in (getattr(session, "friends", ()) or ()):
        if not isinstance(friend, dict):
            continue
        steam_id = str(friend.get("steam_id") or "")
        if not steam_id:
            continue
        in_party = steam_id in member_ids
        rows.append({
            "steam_id": steam_id,
            "name": friend.get("persona") or steam_id,
            "online": bool(friend.get("online")),
            "in_party": in_party,
            "invited": steam_id in sent and not in_party,
            "can_invite": bool(friend.get("online")) and not in_party,
        })
    rows.sort(key=lambda r: (not r["can_invite"], r["name"].lower()))
    return rows


def _invites(session) -> list:
    """The inbox: what I have been offered. Straight off the stream, never assembled here."""
    rows = []
    for invite in (getattr(session, "party_invites", ()) or ()):
        if not isinstance(invite, dict):
            continue
        sender = invite.get("from") or {}
        steam_id = str(sender.get("steam_id") or "")
        if not steam_id:
            continue
        rows.append({
            "steam_id": steam_id,
            "name": sender.get("persona") or steam_id,
            "code": str(invite.get("code") or ""),
            "size": int(invite.get("size") or 0),
            "max": int(invite.get("max") or MAX_PARTY),
            "expires_in": int(invite.get("expires_in") or 0),
        })
    return rows


def party_snapshot(session, panel) -> dict:
    """The party card's state, straight from `session.party` (server-authoritative).

    The invite surface rides along here rather than in a slice of its own, because it is the same
    card: the picker is the empty seats, and the inbox is somebody else's.
    """
    hidden = bool(getattr(panel, "party_code_hidden", False))
    party = getattr(session, "party", None)
    invites = _invites(session)
    invite_error = getattr(session, "invite_error", "") or ""
    if not party:
        return {"in_party": False, "code": None, "hidden": hidden, "members": [],
                "size": 1, "max": MAX_PARTY, "is_leader": True,
                "error": getattr(session, "party_error", "") or "",
                # No party means no picker - the button only exists once one has been created
                # (Sam, 2026-09-15) - but an invite can still arrive, and it is how you get one.
                "can_invite": False, "friends": [], "invites": invites,
                "invite_error": invite_error}
    code = party.get("code") or ""
    leader_id = str(party.get("leader_id") or "")
    members = [_member(m, leader_id) for m in (party.get("members") or [])]
    member_ids = {m["steam_id"] for m in members}
    return {
        "in_party": True,
        "code": code,
        "code_masked": mask_party_code(code),
        "hidden": hidden,
        "leader_id": leader_id,
        "is_leader": session.is_party_leader(),
        "members": members,
        "size": len(members),
        "max": MAX_PARTY,
        "error": getattr(session, "party_error", "") or "",
        # ANY member may invite, not just the leader: an invite only offers a seat, and the person
        # who has to live with the answer is the one holding it. Full party, nothing to offer.
        "can_invite": len(members) < MAX_PARTY,
        "friends": _invite_candidates(session, member_ids),
        "invites": invites,
        "invite_error": invite_error,
    }


# ---------------------------------------------------------------- match-flow slices
# The live match path beyond "found": lobby (coin flip -> side/first-ban choice -> alternating
# map veto) -> connecting (the connect window) -> live (in-game + vote-to-cancel) -> result.
# These mirror what CompetitivePanel draws for each phase (competitive.py `_draw_lobby`,
# `_draw_connecting`, `_draw_live`, `_draw_result`) into the JSON the JS screen renders from.
# Nothing here is authoritative: the session/server own the flow; this only reflects its state.
def _enemy_aliases(session) -> dict:
    """{steam_id: call sign} while the pre-round lobby is hiding the other team, else {}.

    Guarded: a session type that predates the call signs simply shows real names rather than
    taking the whole state push down over a label."""
    try:
        return session.enemy_aliases() or {}
    except Exception:                     # noqa: BLE001 — a name must never break a push
        return {}


def _team_member(p: dict, captain_id: str, my_id: str, alias: str = "") -> dict:
    sid = str(p.get("steam_id") or "")
    return {
        "steam_id": sid,
        "name": alias or p.get("name") or sid,
        # HIDDEN MEANS ABSENT, not "drawn differently". The persona, the Steam picture and the
        # level all stay out of the snapshot for an anonymised enemy, so there is nothing in the
        # page for a determined player to read back out of it with the dev tools - and no way for
        # a future screen to render one by accident. The steam id stays: it is not a name, and
        # the Report button is hung off it.
        "avatar": "" if alias else (p.get("avatar") or ""),
        "level": None if alias else p.get("level"),
        "ping": p.get("ping"),
        "hidden": bool(alias),
        "is_captain": bool(captain_id) and sid == captain_id,
        "is_me": bool(my_id) and sid == my_id,
    }


def _roster_snapshot(session) -> list:
    """Everyone in the match as a flat list, for the screens that show people but not teams.

    The connect window and the result screen both draw a roster, and neither had steam ids in its
    slice - so there was nothing to hang a Report button on. `_teams_snapshot` already carries
    them for the lobby; this is the same data, un-teamed, for the phases where the teams are not
    what is being shown.
    """
    my_id = str((session.me or {}).get("steam_id") or "")
    # The report dialog names its target off this list, so it has to agree with the roster the
    # player is looking at: in the lobby that means call signs here too. Empty outside it.
    aliases = _enemy_aliases(session)
    seen, out = set(), []
    teams = getattr(session, "teams", None) or {}
    for n in (1, 2):
        for p in (teams.get(n) or []):
            sid = str(p.get("steam_id") or "")
            if sid and sid not in seen:
                seen.add(sid)
                out.append({"steam_id": sid, "name": aliases.get(sid) or p.get("name") or sid,
                            "avatar": "" if sid in aliases else (p.get("avatar") or ""),
                            "hidden": sid in aliases,
                            "is_me": sid == my_id, "team": n})
    for p in (getattr(session, "players", None) or []):
        sid = str(p.get("steam_id") or "")
        if sid and sid not in seen:
            seen.add(sid)
            out.append({"steam_id": sid, "name": aliases.get(sid) or p.get("name") or sid,
                        "avatar": "" if sid in aliases else (p.get("avatar") or ""),
                        "hidden": sid in aliases,
                        "is_me": sid == my_id, "team": 0})
    return out


def _teams_snapshot(session) -> list:
    """Both teams, with the side each is on and who each captain is — the lobby's roster."""
    my_id = str((session.me or {}).get("steam_id") or "")
    teams = getattr(session, "teams", None) or {}
    captains = getattr(session, "captains", None) or {}
    sides = getattr(session, "sides", None) or {}
    aliases = _enemy_aliases(session)
    out = []
    for n in (1, 2):
        cap = captains.get(n) or {}
        cap_id = str(cap.get("steam_id") or "")
        members = [_team_member(p, cap_id, my_id,
                               alias=aliases.get(str(p.get("steam_id") or "")) or "")
                   for p in (teams.get(n) or [])]
        out.append({"team": n, "side": sides.get(n) or "", "members": members})
    return out


def _chat_snapshot(session) -> dict:
    """Both lobby logs, every line already labelled the way the screen must say it.

    The aliasing happens HERE rather than when the line is stored, because a message is written
    once and read for the rest of the lobby - and it is the reader's side that decides whether
    the writer is an enemy. The sender's steam id is deliberately dropped on the way out: the
    page needs a label, not an identity."""
    aliases = _enemy_aliases(session)
    chat = getattr(session, "chat", None) or {}
    out = {}
    for channel in ("team", "all"):
        lines = chat.get(channel) or []
        out[channel] = [
            {"name": aliases.get(str(m.get("steam_id") or "")) or m.get("name") or "",
             "text": m.get("text") or ""}
            for m in lines]
    return out


def lobby_snapshot(session, panel) -> dict:
    """coin flip -> side/ban choice -> attack/defend selector -> map veto, plus chat and teams.

    Server-authoritative now: one designated captain flips, the server decides the winner, and
    the winner takes SIDE (gets the attack/defend selector) or the last BAN. This slice exposes
    who may act at each step so the JS shows the control to exactly one client and everyone else
    a "waiting for <name>" line."""
    stage = getattr(session, "stage", "") or ""
    my_team = session.my_team()
    i_am_captain = session.i_am_captain()
    ban_turn = getattr(session, "ban_turn", None)
    toss_winner = getattr(session, "toss_winner", None)
    side_picker = getattr(session, "side_picker", None)
    ban_advantage = getattr(session, "ban_advantage", None)
    captains = getattr(session, "captains", None) or {}

    # Every "waiting for <name>" line in the lobby goes through here, which is what stops the
    # coin flip and the veto from naming an enemy captain the roster is busy hiding.
    def cap_name(team):
        return session.captain_name(team) or ""

    # The veto pool is the SERVER's for a live match (session.lobby_pool); the panel's catalogue
    # pool is the fallback for the offline preview.
    pool = getattr(session, "lobby_pool", None)
    if not pool:
        try:
            pool = [str(m) for m in panel.map_pool()]
        except Exception:                 # noqa: BLE001 — a bad pool must not kill the push
            pool = []
    pool = [str(m) for m in pool]
    bans = [{"team": team, "map": m} for team, m in (getattr(session, "bans", None) or [])]
    return {
        "stage": stage,
        "my_team": my_team,
        "i_am_captain": bool(i_am_captain),
        # The single designated captain who flips the coin (team 1's), so the JS shows the
        # Heads/Tails control to exactly one client and everyone else "waiting for <name>".
        "i_am_coin_captain": bool(session.i_am_coin_captain()),
        "coin_captain_name": session.coin_captain_name(),
        "coin": {
            "side": getattr(session, "coin_side", None),
            "result": getattr(session, "coin_result", None),
            "toss_winner": toss_winner,
            "toss_winner_name": cap_name(toss_winner),
            # captain OF the winning side: see i_pick_side below for why this is not
            # `toss_winner == my_team`.
            "i_won_toss": bool(session.i_am_captain_of(toss_winner)),
            "my_captain_name": cap_name(my_team),
        },
        # Side/ban advantage. `advantage` is what the winner chose; `side_picker` gets the real
        # attack/defend selector; `ban_advantage` is the team that bans LAST (the final map).
        "advantage": getattr(session, "advantage", None),
        "side_picker": side_picker,
        "side_picker_name": cap_name(side_picker),
        # WHOSE TURN IT IS, ASKED THE RIGHT WAY (2026-09-15). This used to be
        # `side_picker == my_team and i_am_captain`, which quietly assumes the two captains are
        # two different people. They are not when a team is empty: buildLobby names captain 2 as
        # `team2[0] || team1[0]`, so a one-sided lobby has ONE person captaining both sides. The
        # server accepts their pick for either side - isLobbyCaptain compares against
        # captains[team] - and only this client-side derivation refused to offer it, which is how
        # a COMP_MATCH_SIZE=1 test lobby got stuck unable to ban its own maps.
        #
        # `i_am_captain_of(team)` asks the question the server actually answers, and is the same
        # thing in the normal ten-player case.
        "i_pick_side": bool(stage == "side" and session.i_am_captain_of(side_picker)),
        "ban_advantage": ban_advantage,
        "ban_advantage_name": cap_name(ban_advantage),
        "first_ban": getattr(session, "first_ban", None),
        "ban_turn": ban_turn,
        "ban_turn_name": cap_name(ban_turn),
        # The per-stage clock: the coin call, the side/last-ban choice, the attack/defend pick and
        # each ban turn all run on one. 0 on a stage that has no clock ("ready", and the rejoin
        # placeholder), so the JS can simply not draw it.
        "stage_seconds": (int(getattr(session, "stage_seconds", 0) or 0)
                          if stage in TIMED_STAGES else 0),
        "stage_total_seconds": int(getattr(session, "stage_total_seconds", 0) or 0),
        # Which of the choices the CLOCK made instead of a captain, so the screen can say so
        # rather than letting nine people blame a teammate for a pick they never made.
        "coin_auto": bool(getattr(session, "coin_auto", False)),
        "advantage_auto": bool(getattr(session, "advantage_auto", False)),
        "side_auto": bool(getattr(session, "side_auto", False)),
        # True only when it is MY turn to click a map (the JS enables the list on this). See
        # i_pick_side above for why this asks i_am_captain_of rather than comparing teams.
        "my_turn": bool(stage == "veto" and session.i_am_captain_of(ban_turn)),
        "map": getattr(session, "map", None),
        "veto": {"pool": pool, "bans": bans},
        "teams": _teams_snapshot(session),
        "roster": _roster_snapshot(session),
        "chat": _chat_snapshot(session),
        "messages": _combined_chat_snapshot(session),
        "chat_epoch": getattr(session, "chat_epoch", 0),
        "chat_channel": getattr(session, "chat_channel", "team"),
        "muted_players": sorted(getattr(session, "muted_chat_players", set())),
    }


def _combined_chat_snapshot(session) -> list:
    """One chronological feed, with audience labels and local mutes applied before rendering."""
    aliases = _enemy_aliases(session)
    muted = getattr(session, "muted_chat_players", set())
    lines = [(channel, line) for channel in ("team", "all")
             for line in (getattr(session, "chat", {}).get(channel) or [])]
    lines.sort(key=lambda item: item[1].get("order", 0))
    return [{"channel": channel,
             "name": aliases.get(str(line.get("steam_id") or "")) or line.get("name") or "",
             "text": line.get("text") or ""}
            for channel, line in lines if str(line.get("steam_id") or "") not in muted]


def _is_host(session) -> bool:
    """True when THIS client is the match host (best ping, computed identically on every client).
    Guarded so a session type without the helper never breaks the snapshot."""
    try:
        return bool(session._i_am_host())
    except Exception:
        return False


def connect_snapshot(session) -> dict:
    """The connect window: the countdown, who is in, and the cost of missing it."""
    host = getattr(session, "host", None) or {}
    connected = getattr(session, "connected_ids", None) or set()
    players = getattr(session, "players", None) or []
    total = max(1, int(getattr(session, "connect_total", 0) or len(players) or 1))
    nxt = max(60, int(getattr(session, "penalty_next", 0) or NO_SHOW_BAN_SECONDS))
    return {
        "left": int(getattr(session, "connect_left", 0) or 0),
        "total": total,
        "done": len(connected),
        "i_connected": bool(getattr(session, "i_connected", False)),
        "map": getattr(session, "map", None),
        "host": {"name": host.get("name") or "", "ping": host.get("ping"), "estimated": bool(host.get("ping_estimated"))},
        # HOST-GATED LAUNCH: whether this client is the host, whether the host's game has reported
        # in, and the non-host join countdown. The JS gates the connect UI on these - a non-host
        # shows "waiting for the host" with NO countdown and a GREYED "Launch game" until
        # host_ready, then the join_left clock and a live Launch button. `i_connected` is what
        # flips that button to Relaunch + Join, because pressing Launch is now what reports the
        # player in (the old "I am in the game" button is gone).
        "is_host": _is_host(session),
        "host_ready": bool(getattr(session, "host_ready", False)),
        "join_left": int(getattr(session, "join_left", 0) or 0),
        # THE START GATE. Everyone can be in and the match still not start: the server holds it
        # until the GAME has put players on the teams the lobby chose (live.cjs goLiveIfReady).
        # "" normally, "wait" while it is held, "mismatch" once it gave up and started anyway.
        # Without this the screen is a full set of green pips and nothing happening.
        "teams_status": str(getattr(session, "teams_status", "") or ""),
        # the warn line is formatted in Python (JS has no format_duration): pass the pieces
        "warn_elo": NO_SHOW_ELO,
        "warn_time": format_duration(nxt),
        "teams": _teams_snapshot(session),
        "roster": _roster_snapshot(session),
    }


def live_snapshot(session) -> dict:
    """In-game: the map/host to join, and the vote-to-cancel if one is running."""
    host = getattr(session, "host", None) or {}
    vote = getattr(session, "vote", None)
    v = None
    if vote:
        v = {"caller": vote.get("caller") or "", "yes": int(vote.get("yes") or 0),
             "no": int(vote.get("no") or 0), "needed": VOTE_NEEDED, "voted": bool(vote.get("voted"))}
    return {
        "map": getattr(session, "map", None),
        "host": {"name": host.get("name") or "", "ping": host.get("ping"), "estimated": bool(host.get("ping_estimated"))},
        "vote": v,
        # host-gated join (additive), same fields as the connect slice: in `live` everyone has
        # connected so host_ready is already true, but the JS still reads is_host to decide whether
        # to show the host's Relaunch or the joiner's Relaunch + Join.
        "is_host": _is_host(session),
        "host_ready": bool(getattr(session, "host_ready", False)),
        # the preview (mock/live-preview) can force a result; a real match ends on the scoreboard
        "can_finish": bool(getattr(session, "mock", False)) and not getattr(session, "live", False),
        "teams": _teams_snapshot(session),
        "roster": _roster_snapshot(session),
    }


def result_snapshot(session) -> dict:
    """The outcome: win / loss / voided, the score, and the rank delta."""
    r = getattr(session, "result", None) or {}
    score = r.get("score") or (0, 0)
    try:
        score = [int(score[0]), int(score[1])]
    except (TypeError, ValueError, IndexError):
        score = [0, 0]
    return {
        "won": r.get("won"),
        "voided": bool(r.get("voided")),
        "score": score,
        "delta": int(r.get("delta") or 0),
        # The RR the match moved (None from a service that does not send it), and the placement
        # states that move none. `delta` above is an arrow count and is not printed as a number.
        "rr_delta": r.get("rr_delta") if isinstance(r.get("rr_delta"), int) else None,
        "placing": bool(r.get("placing")),
        "placements_left": int(r.get("placements_left") or 0),
        "map": getattr(session, "map", None),
        # What became of the player's Bodycam. "" and "armed" say nothing on screen - they are
        # reading a scoreboard, and a countdown to their game being shut is a distraction from
        # it. See the CLOSE_GAME_* block in competitive.py.
        "game_close": str(getattr(session, "game_close", "") or ""),
        # The post-game roster. It was not here before, which is why there was no way to report
        # (or add) the people you had just played with from the one screen where you know whether
        # you want to.
        "roster": _roster_snapshot(session),
    }


def _mode_listed(panel) -> bool:
    """Is the ranked gamemode in the catalogue this hub has loaded? False also when the catalogue
    has not arrived yet, which is what the gate's "waiting" line is for."""
    catalogue = getattr(getattr(panel, "app", None), "catalogue", None) or {}
    return any(e.get("id") == COMPETITIVE_MODE_ID
               for e in (catalogue.get("gamemodes") or []))


def comp_snapshot(session, panel) -> dict:
    """The find-match / queue / accept state the hero renders from, plus the live match path
    (lobby / connecting / live / result) when the session has moved into it."""
    banned_left = session.banned_left()
    # Behind the release the service is publishing? The queue is shut until this is None
    # (hub/competitive.py Session.update_needed, and server/live.cjs enforces it for real).
    # A match ALREADY UNDER WAY is never gated by it, here or on the server, so this only ever
    # reaches the idle screen's Find match button.
    try:
        outdated = session.update_needed()
    except Exception:            # noqa: BLE001 — never let this be what kills a push
        outdated = None
    comp = {
        "phase": session.phase,
        "match_id": str(getattr(session, "match_id", "") or ""),
        # Keep confirmed bans outside the lobby-only slice: the final ban can arrive
        # in the same poll as the transition to connecting, on any active screen.
        "map_bans": [{"team": team, "map": name}
                     for team, name in (getattr(session, "bans", None) or [])],
        "stage": getattr(session, "stage", "") or "",
        "error": getattr(session, "error", "") or "",
        "locked_in": session.locked_in(),
        "is_leader": session.is_party_leader(),
        "is_captain": bool(session.i_am_captain()),
        # The rank ladder for the info panel: the server's numbers or nothing. None means the
        # stream has not said hello yet, and the JS hides the panel rather than inventing bands.
        "ladder": getattr(session, "ladder", None),
        "rank_info_open": bool(getattr(panel, "rank_info_open", False)),
        # THE PENALTY RULES, on the same terms as the ladder above: the server's dials or
        # nothing. None means the stream has not said hello yet, and the JS hides the button
        # rather than opening a panel with blanks where the times should be.
        "penalties": getattr(session, "penalties", None),
        "penalties_open": bool(getattr(panel, "penalties_open", False)),
        # THE REPORT PICKER. `report_target` is the steam id whose reason list is open, "" when
        # none is. One at a time and server-side state, because core.js rebuilds the DOM on every
        # change and a half-made accusation must not survive that as a stray local variable.
        #
        # IT IS DRAWN BY THE CORE, NOT BY THIS SCREEN (static/core.js registerOverlay, and the
        # registration at the foot of static/screens/competitive.js). The button exists on the
        # history tab too, and a picker only competitive.js could draw meant pressing it there
        # opened nothing until the player switched tabs (Sam, 2026-09-16). This slice is in the
        # snapshot on every view, so the box now follows the state instead of the tab.
        "report_target": str(getattr(session, "report_target", "") or ""),
        "report_seq": int(getattr(session, "report_seq", 0)),
        "report_match": str(getattr(session, "report_match", "") or ""),
        # Who they are, from whoever opened the picker: the live roster cannot name a player from
        # a match that finished weeks ago.
        "report_name": str(getattr(session, "report_name", "") or ""),
        "report_sent": bool(getattr(session, "report_sent", False)),
        "report_error": str(getattr(session, "report_error", "") or ""),
        "report_reasons": ["cheating", "text_abuse", "voice_abuse", "afk", "griefing",
                           "team_killing", "smurfing", "other"],
        # IS THE GAMEMODE EVEN THERE? The Tk tab answers this by replacing its whole body with a
        # gate screen (hub/competitive.py CompetitiveTab._draw_gate), so its Find match button
        # cannot be reached. This screen had no such gate: a player who had just installed the hub
        # and opened Competitive first could queue for a mode they did not own. The JS puts the
        # install button in Find match's place from this flag; can_find kills it either way.
        "installed": bool(session.gamemode_installed()),
        "mode_id": COMPETITIVE_MODE_ID,
        # Does the catalogue still describe the mode? The gate's Install button builds the pak
        # from catalogue entries, and ops.apply drops any id the catalogue does not list - so
        # offering the button before the list has arrived would run an install that installs
        # nothing. Same test, same reason, as the Tk gate screen.
        "mode_listed": _mode_listed(panel),
        "can_find": (session.is_party_leader() and session.gamemode_installed()
                     and not files_busy(panel)
                     and not banned_left and not outdated),
        # None, or {what, hub, have_hub, mode, have_mode} plus the finished sentence. The JS
        # renders the sentence rather than assembling one, so the hub, the Tk window and a
        # refusal that came back from the service all say the same thing.
        "outdated": (dict(outdated, line=outdated_line(outdated))
                     if outdated else None),
        "party_size": session.party_size(),
        "queue": {
            "seconds": int(getattr(session, "queue_seconds", 0) or 0),
            "position": int(getattr(session, "queue_position", 0) or 0),
            "size": int(getattr(session, "queue_size", 0) or 0),
        },
        "found": {
            "accepted": int(getattr(session, "accepted", 0) or 0),
            "total": int(getattr(session, "accept_total", 0) or 0),
            "accept_left": int(getattr(session, "accept_left", 0) or 0),
            "i_accepted": bool(getattr(session, "i_accepted", False)),
        },
        "penalty": {
            "left": int(banned_left or 0),
            # the absolute epoch-seconds deadline, so the browser can tick the clock down every
            # second between snapshots (webui competitive.js); 0 when there is no live ban.
            # Read AFTER banned_left() above, which zeroes penalty_until the moment it expires.
            "until": float(getattr(session, "penalty_until", 0.0) or 0.0),
            "reason": getattr(session, "penalty_reason", "") or "",
        },
    }
    # Only the active phase's detail rides along — smaller snapshots, and the de-dup in
    # WebPanel.on_change stays stable across the periodic stats/online ticks.
    phase = session.phase
    if phase == "lobby":
        comp["lobby"] = lobby_snapshot(session, panel)
    elif phase == "connecting":
        comp["connect"] = connect_snapshot(session)
    elif phase == "live":
        comp["live"] = live_snapshot(session)
    elif phase == "result":
        comp["result"] = result_snapshot(session)
    return comp


@register_snapshot("competitive")
def snapshot(session, panel) -> dict:
    """Merge the competitive slices into the top-level snapshot (kept flat: JS reads state.comp /
    state.party, and these are also the fields the existing headless tests assert on)."""
    return {
        "comp": comp_snapshot(session, panel),
        "party": party_snapshot(session, panel),
    }


# ---------------------------------------------------------------- bridge verbs
# Each verb is a 1:1 passthrough to a session verb (or a view-only panel toggle), run on the UI
# thread via panel.post — exactly as the pre-modular Api methods did. fn(panel, *args).
def _toggle_penalties(panel):
    """View-only toggle for the penalties explainer - the same shape, and on the panel for the
    same reason, as _toggle_rank_info below it.

    THE TWO ARE MUTUALLY EXCLUSIVE. They are both modals over the same hero, and opening one
    from behind the other would stack two dialogs whose Escape handlers both close to the same
    screen. Opening either closes the other."""
    def apply_():
        panel.penalties_open = not bool(getattr(panel, "penalties_open", False))
        if panel.penalties_open:
            panel.rank_info_open = False
        panel.on_change()
    panel.post(apply_)


def _toggle_rank_info(panel):
    """View-only toggle for the ranked explainer, the same shape as gamemodes' open_ruleset.

    It lives on the PANEL rather than in a JS variable because core.js rebuilds the whole DOM on
    every state change - and the competitive tab changes state every second while a clock is
    running - so anything the player opened has to be remembered somewhere that survives a
    rebuild."""
    def apply_():
        panel.rank_info_open = not bool(getattr(panel, "rank_info_open", False))
        if panel.rank_info_open:
            panel.penalties_open = False      # see _toggle_penalties: one modal at a time
        panel.on_change()
    panel.post(apply_)


register_verbs("competitive", {
    "toggle_rank_info": _toggle_rank_info,
    "toggle_penalties": _toggle_penalties,
    # This verb only exists so core.js can prove the localized warning reached the rendered DOM
    # before the service records an acknowledgement. The session rejects unknown/replayed ids.
    "ack_combat_warning": lambda panel, mid="", wid="": panel.post(
        lambda: panel.session.ack_combat_warning(str(mid or ""), str(wid or ""))),
    # Reports. open/close are view-only toggles; `report` is the one that writes.
    "open_report":  lambda panel, sid="", mid="", name="": panel.post(
                        lambda: panel.session.open_report(str(sid or ""), str(mid or ""), str(name or ""))),
    "close_report": lambda panel: panel.post(panel.session.close_report),
    "report":       lambda panel, sid="", reason="", mid="", note="": panel.post(
                        lambda: panel.session.report_player(str(sid or ""), str(reason or ""), str(mid or ""), str(note or ""))),
    # sign in / out
    "sign_in":          lambda panel: panel.post(panel.session.sign_in),
    "sign_out":         lambda panel: panel.post(panel.session.sign_out),
    "open_link_again":  lambda panel: panel.post(panel.session.open_link_again),
    "cancel_sign_in":   lambda panel: panel.post(panel.session.cancel_sign_in),
    # matchmaking
    "find_match":       lambda panel: panel.post(panel.session.find_match),
    "cancel_search":    lambda panel: panel.post(panel.session.cancel_queue),   # the verb it maps to
    "accept":           lambda panel: panel.post(panel.session.accept),
    # match flow: lobby (coin flip -> side/first-ban choice -> map veto), connect, live, result.
    # Each is a 1:1 passthrough to the session verb, run on the UI thread; the session guards the
    # phase/stage/turn so a stale click from JS is a safe no-op (the next onState re-syncs).
    "pick_coin":        lambda panel, side="": panel.post(lambda: panel.session.pick_coin(str(side or ""))),
    "choose":           lambda panel, kind="": panel.post(lambda: panel.session.choose(str(kind or ""))),
    "choose_side":      lambda panel, side="": panel.post(lambda: panel.session.choose_side(str(side or ""))),
    "ban_map":          lambda panel, map_name="": panel.post(lambda: panel.session.ban(str(map_name or ""))),
    "send_chat":        lambda panel, text="", channel="team": panel.post(
        lambda: panel.session.send_chat(str(text or ""), str(channel or "team"))),
    "set_chat_channel": lambda panel, channel="team": panel.post(
        lambda: panel.session.set_chat_channel(str(channel or "team"))),
    "toggle_chat_mute": lambda panel, steam_id="": panel.post(
        lambda: panel.session.toggle_chat_mute(str(steam_id or ""))),
    # host-gated join / launch (connect + live phases). `launch_game` is the JOINER's "Launch
    # game": it opens their Bodycam and reports them in, and the session refuses it until the
    # host's lobby is stamped (the JS greys the button on the same fact). It replaced the old
    # "I am in the game" / report_connected button, which no longer exists in the UI. `relaunch`
    # re-runs the game (guarded, safe if it is already up); join_match is the placeholder for the
    # real non-host -> host connect.
    "launch_game":      lambda panel: panel.post(panel.session.launch_game),
    "relaunch_game":    lambda panel: panel.post(panel.session.relaunch_game),
    "join_match":       lambda panel: panel.post(panel.session.join_match),
    "start_vote":       lambda panel: panel.post(panel.session.start_vote),
    "cast_vote":        lambda panel, yes=False: panel.post(lambda: panel.session.cast_vote(bool(yes))),
    "finish_match":     lambda panel: panel.post(panel.session.finish),          # preview-only cue
    "leave_match":      lambda panel: panel.post(panel.session.leave_result),
    # party
    "create_party":     lambda panel: panel.post(panel.session.create_party),
    "join_party":       lambda panel, code="": panel.post(lambda: panel.session.join_party(str(code or ""))),
    "leave_party":      lambda panel: panel.post(panel.session.leave_party),
    "refresh_party_code": lambda panel: panel.post(panel.session.refresh_party_code),
    # party invites. `open_invites` is the picker being opened: the friends list is fetched when
    # the Friends screen is opened and nowhere else, so a player who has not been there yet would
    # otherwise open an empty picker.
    "open_invites":     lambda panel: panel.post(panel.session.refresh_friends),
    "invite_friend":    lambda panel, steam_id="": panel.post(
        lambda: panel.session.invite_to_party(str(steam_id or ""))),
    "invite_accept":    lambda panel, steam_id="": panel.post(
        lambda: panel.session.accept_party_invite(str(steam_id or ""))),
    "invite_decline":   lambda panel, steam_id="": panel.post(
        lambda: panel.session.decline_party_invite(str(steam_id or ""))),
    # a view-only toggle (which member sees the code): lives on the panel, not the session
    "toggle_hide_code": lambda panel: panel.post(panel.toggle_hide_code),
})
