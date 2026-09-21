"""Python -> JS state: the single snapshot the web UI renders from.

The Tk panel fingerprints the session and rebuilds only when the drawn state changed
(hub/competitive.py `_render_signature`). The web UI keeps that idea but the "drawing" is a
JSON object: on every `on_change()`, WebPanel builds ONE snapshot with `state_snapshot()` and
pushes it to JS as `window.__hub.onState(<json>)`; JS reconciles the DOM. One snapshot, not
many messages, so a reconnect or a language switch is just a fresh snapshot (plan: "The JS <->
Python bridge contract").

MODULAR ASSEMBLER: this file owns only the SHARED base of the snapshot — ``view``, ``lang``,
``strings`` and the identity/chrome slices ``auth`` and ``status`` that the core nav and status
bar render from. Each SCREEN contributes its own slice through a per-screen module in
``hub/webui/screens/`` (registered via ``register_snapshot``); ``state_snapshot`` merges every
registered contributor into the state dict. Adding a screen is a new file in that package — it is
NOT an edit to this function body, so two agents adding different screens never touch the same
lines.

Everything here is pure and pywebview-free, so it is tested headless against a LiveSession
driven by the fake live client (tests/test_hub.py), exactly like the existing
`test_live_session_*` tests but asserting on the dict instead of Tk widgets.
"""
from .. import i18n
from . import screens as screens_pkg


# The named ranks, in order: docs/ranks.md, and the same list both server modules ship as
# COMP_RANK_NAMES + COMP_RANK_TOP. The placeholder set (Static/Witness/Responder/Operator/
# Enforcer/Nightwatch/Ghostframe/Blackout) is gone - it was a second ladder, with a different
# shape as well as different names, and the hub was showing both at once.
#
# Nine entries: eight ranks of three divisions (I-III, 100 RR each), then Reaper, the
# capstone - one band, no divisions, and the figure above it just keeps counting. See
# docs/ranks.md and docs/valuation.md.
#
# THIS LIST IS A FALLBACK, not the source of truth. Anything drawing a rank the server has named
# should use that name; the server sends the whole ladder with `hello` precisely so a rename does
# not need a hub release. What is left reading this is `tier_for`, which turns a bare level
# number into a word when that is all we have.
TIER_NAMES = ["Rookie", "Private", "Soldier", "Veteran", "Operator",
              "Shadow", "Nightmare", "Spectre", "Reaper"]


def _rank_block(me) -> dict | None:
    """The hero's rank, as the page expects it: {rank_name, division, rr, top} or None.

    THE PAGE HAS ALWAYS READ A DICT - competitive.js does `rank.rank_name`, `rank.division`,
    `rank.rr`, `rank.top` - and this was handing it `me["rank"]`, which is the server's rank NUMBER
    (progress.publicProgress sends `rank: rankOf(p)`, an integer 1-9). Reading `.rank_name` off an
    integer is undefined, so the emblem drew nothing at all: the whole visible ladder was one type
    mismatch away from working.

    None while placing, which is the page's cue to show the placement line instead: a player
    mid-placements has a real rating and no rank, and a number invented for them would be a lie.

    `top` means the capstone - Reaper, which has no divisions and no ceiling. The page shows
    the figure alone there rather than a division and a bar.

    `counting` means the figure has no ceiling either way: from Spectre 1 up, RR stops resetting
    at every division and runs as one count (server/progress.cjs), so Spectre 2 is 100 RR and
    Spectre 3 is 200+ with nothing above it. A page that drew a 0-100 bar for that would show
    every Spectre 3 in the world a full bar for ever.
    """
    if not me or me.get("placing"):
        return None
    name = me.get("rank_name")
    if not name:
        return None                      # no ladder from the server yet: say nothing, invent nothing
    division = me.get("division")
    # `top` off the server's own flag, falling back to the shape of the payload for a service
    # that has not shipped it yet: the capstone is the one rank with no division.
    top = bool(me.get("top")) if me.get("top") is not None else division is None
    # The running count, under either of the names the server has used for it. `rr` is the one
    # that matters now - it IS the count from the counting band up - and `bdr` is the older name
    # for the same figure, kept because the match result card still switches on it.
    rr = me.get("rr")
    if rr is None:
        rr = me.get("bdr")
    return {
        "rank": me.get("rank"),
        "rank_name": name,
        "division": division,
        "rr": rr,
        "counting": bool(me.get("counting")) or top,
        # Enough RR for the capstone, but the seats are full. The page can then say WHY the badge
        # has not changed, instead of looking like it has stopped reading the server.
        "top_eligible": bool(me.get("top_eligible")),
        "top": top,
    }


def tier_for(level) -> str:
    """A named tier for an integer level (display only).

    `level` IS the rank index, 1-based, exactly as ``progress.levelOf`` returns it - so 9 is the
    capstone. It used to be a 1..14 FACEIT-style level that this halved to get a tier, and that
    mapping was wrong against the ladder in two ways at once: the wrong arithmetic AND the wrong
    list of names.

    NO LEVEL MEANS NO NAME. A player mid-placements has no rank, and this used to clamp their
    missing level up to 1 and hand back "Rookie" - a rank they have not earned, printed under their
    persona while the hero right above it said they were still placing. It only looked harmless
    while this list was a different ladder from the one the badge came from.
    """
    try:
        lvl = int(level or 0)
    except (TypeError, ValueError):
        return ""
    if lvl < 1:
        return ""
    return TIER_NAMES[min(len(TIER_NAMES), lvl) - 1]


def strings_for(lang: str) -> dict:
    """The active language's strings, English-filled so JS can look up any key by name.

    The 7-language STRINGS dict (hub/i18n.py) stays the single source of truth; the snapshot
    just ships the active language, so there is no second translation store in JS to keep in
    sync (plan: i18n)."""
    merged = dict(i18n.STRINGS.get(i18n.DEFAULT, {}))
    merged.update(i18n.STRINGS.get(lang, {}))
    return merged


def _auth(session) -> dict:
    """Shared identity slice (the hero header, and whatever later screens show of the player)."""
    me = session.me or {}
    signed_in = bool(session.me)
    return {
        "signed_in": signed_in,
        "phase": session.phase,
        "persona": me.get("name") or "",
        "steam_id": me.get("steam_id") or "",
        "player_id": me.get("player_id") or me.get("steam_id") or "",
        "game_steam_id": me.get("game_steam_id") or "",
        "account_step": getattr(session, "account_step", ""),
        "account_busy": bool(getattr(session, "account_busy", False)),
        "game_verifying": bool(getattr(session, "game_verifying", False)),
        "avatar": me.get("avatar") or "",
        "level": me.get("level"),
        "tier": tier_for(me.get("level")) if signed_in else "",
        # THE VISIBLE RANK: {rank_name, division, rr, top} or None while placing. The hero draws
        # from this; `level` is the old numeric ladder and stays only for the surfaces that have
        # not been converted yet (party rows, history).
        "rank": _rank_block(me) if signed_in else None,
        "placing": bool(me.get("placing")),
        "placements_left": me.get("placements_left"),
        "elo": me.get("elo"),
        "bdr": me.get("bdr"),
        "matches": me.get("matches"),
        "wins": me.get("wins"),
        # sign-in hand-off (browser OpenID): the page/code so JS can show "open again"/the code
        "link_url": getattr(session, "link_url", "") or "",
        "link_code": getattr(session, "link_code", "") or "",
    }


def _status(session, panel) -> dict:
    """Shared chrome slice: live counts, registration inventory and ranked installation.

    `online`, `queued` and `live_matches` all come off the SAME `stats` broadcast, so the three
    figures in the top bar are always the same moment - a count of people online taken now next
    to a count of matches taken a minute ago is how you get "4 online, 9 live games".
    Registration inventory is refreshed independently by the server; None means unavailable.
    """
    return {
        "connected": bool(getattr(session, "connected", True)
                          and getattr(session, "stats_ready", False)),
        "online": int(getattr(session, "online", 0) or 0),
        # Everyone searching service-wide, not our own place in the queue (that is the
        # competitive slice's queue_position/queue_size pair, drawn on the queue card).
        "queued": int(getattr(session, "stats_queued", 0) or 0),
        "live_matches": int(getattr(session, "live_matches", 0) or 0),
        "players_registered": getattr(session, "players_registered", None),
        "locked_in": session.locked_in(),
        # Whether the ranked gamemode is installed (the Tk tab gates on this). The web UI
        # only needs to know; it does not draw the gate screen in Phase 1.
        "gamemode_installed": bool(_gamemode_installed(panel)),
    }


def _update(panel) -> dict:
    """App self-update slice (the top-of-window update strip renders from this).

    `available`/`forced`/`latest` are DERIVED from the catalogue each snapshot: HUB_VERSION vs
    catalogue hub.version via the catalogue module's `version_newer`, exactly the offer/force test
    the Tk path uses (hub/app.py _check_hub_update). The download/launch progress
    (`status`/`progress`/`error`) is the panel's live runtime state, set by start_hub_update.

    Self-update is only OFFERED from the installed exe (`update.own_exe()` is the running exe when
    frozen, else None): a dev checkout has no installed hub for the installer to replace, so we do
    not offer an update that could not be applied — the same reason `clean_old_versions` only
    touches the beside-exe files when frozen.

    `forced` IS SUPPRESSED WHILE A MATCH IS RUNNING (Sam, 2026-09-15). A forced update is a
    blocking full-screen overlay (core.js renderUpdate, .updatebar.forced), and a player who is
    in a competitive match needs the screen underneath it: accept, the veto, the connect window,
    "I am in the game". Taking it away mid-match does not make them update any sooner, it makes
    them a no-show and costs them Elo and a queue ban for a release THEY did not push. So during
    a locked phase it degrades to the ordinary dismissable strip, and goes back to being forced
    the moment the match is over — which is the same rule the Tk window follows
    (hub/app.py _check_hub_update). What stops them QUEUEING again on an old build is the
    version gate, not this overlay."""
    from ..version import HUB_VERSION
    from .. import update as update_mod
    from ..catalogue import version_newer
    info = ((getattr(getattr(panel, "app", None), "catalogue", None) or {}).get("hub")) or {}
    latest = str(info.get("version") or "")
    frozen = update_mod.own_exe() is not None
    newer = bool(latest and info.get("download_url") and version_newer(latest, HUB_VERSION))
    available = bool(frozen and newer)
    session = getattr(panel, "session", None)
    try:
        locked = bool(session is not None and session.locked_in())
    except Exception:            # noqa: BLE001 — never let this be what stops an update
        locked = False
    rt = getattr(panel, "update_state", None) or {}
    return {
        "available": available,
        "current": HUB_VERSION,
        "latest": latest,
        "forced": bool(available and info.get("required") and not locked),
        # True only while a forced update is being HELD BACK by a live match, so the strip can
        # say why it is not the usual "Later" offer and will become one again.
        "forced_deferred": bool(available and info.get("required") and locked),
        "download_url": info.get("download_url", "") if available else "",
        "size": info.get("size") if available else None,
        "sha256": info.get("sha256", "") if available else "",
        "status": rt.get("status", "idle"),
        "progress": int(rt.get("progress", 0) or 0),
        "error": rt.get("error", "") or "",
    }


def state_snapshot(session, panel) -> dict:
    """The whole state the UI needs, as a JSON-serialisable dict: the shared base plus every
    registered screen's slice merged in.

    ``view`` is the panel's active screen (the nav switches it via set_view); nav lives in Python so
    it survives i18n/state (plan). Each screen contributor returns a dict merged at the top level;
    keys are disjoint by convention (competitive owns ``comp``/``party``, other screens namespace
    under their own name), so merge order does not matter."""
    lang = i18n.get_language()
    state = {
        "view": getattr(panel, "view", "competitive"),
        "lang": lang,
        "strings": strings_for(lang),
        "auth": _auth(session),
        "status": _status(session, panel),
        "update": _update(panel),
    }
    for name, contribute in screens_pkg.SCREEN_SNAPSHOTS.items():
        try:
            slice_ = contribute(session, panel)
        except Exception:                # noqa: BLE001 — one bad screen must not kill the push
            continue
        if slice_:
            state.update(slice_)
    return state


def _gamemode_installed(panel) -> bool:
    try:
        app = getattr(panel, "app", None)
        installed = (getattr(app, "state", {}) or {}).get("installed") or {}
        from ..competitive import COMPETITIVE_MODE_ID
        return COMPETITIVE_MODE_ID in installed
    except Exception:            # noqa: BLE001 — never let the snapshot crash the push
        return False
