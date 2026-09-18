#!/usr/bin/env python3.12
"""Headless tests for STEAM AVATARS in the web UI.  Run:  python3.12 tests/test_screen_avatars.py

Sam, 2026-09-16: "put the user's steam image next to their name in the profile" and "in parties and
in lobbies also show the user's steam image instead of just default text." Three halves have to
line up for that, and each is tested here:

  * THE SERVER hands out an avatar URL wherever it already hands out a persona (party members,
    match rosters) - checked as a source scan, since the live service is a node process;
  * THE SESSION carries it through to the snapshot without fetching anything itself;
  * THE PAGE turns it into `/avatar?u=...` on the hub's own bridge, because the page's CSP allows
    `img-src 'self'` and would refuse steamstatic.com - and the bridge only ever fetches https
    Steam hosts, so a payload cannot point the hub at an arbitrary URL.

The fallback is the point of the last one: an account with no avatar, a hub that is offline and a
cold cache must all leave the initials plate exactly as it was.
"""
import os
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_STATE = tempfile.mkdtemp(prefix="hub-avatars-state-")
os.environ.setdefault("HUB_STATE_DIR", _STATE)
sys.path.insert(0, str(REPO))

from hub import avatars as avatars_mod          # noqa: E402
from hub.webui import httpbridge                # noqa: E402
from hub.webui.screens import competitive as SCREEN   # noqa: E402

RESULTS = []
STATIC = REPO / "hub" / "webui" / "static"
UI_JS = (STATIC / "ui.js").read_text(encoding="utf-8")
COMP_JS = (STATIC / "screens" / "competitive.js").read_text(encoding="utf-8")
PROF_JS = (STATIC / "screens" / "profile.js").read_text(encoding="utf-8")
LIVE_CJS = (REPO / "server" / "live.cjs").read_text(encoding="utf-8")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
STEAM_URL = "https://avatars.steamstatic.com/abcdef_full.jpg"


# ---------------------------------------------------------------- the bridge route
def test_only_steam_urls_are_ever_fetched():
    """The URL arrives over the wire, so it is not followed blindly (hub/avatars.py). A hub that
    fetched whatever a payload told it to is a hub that can be pointed at anything."""
    for bad in ("", None, "http://avatars.steamstatic.com/x.jpg",      # not https
                "https://evil.example/x.jpg",                          # not Steam
                "https://avatars.steamstatic.com.evil.example/x.jpg",  # not Steam either
                "file:///c:/windows/win.ini", "javascript:alert(1)"):
        assert httpbridge.avatar_bytes(bad) is None, bad


def test_a_cached_avatar_is_served_with_its_real_type():
    """The file on disk is whatever Steam sent, so the type is sniffed rather than assumed."""
    path = avatars_mod.cache_path(STEAM_URL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG)
    hit = httpbridge.avatar_bytes(STEAM_URL)
    assert hit is not None, "a cached Steam avatar must be served"
    body, ctype = hit
    assert body == PNG and ctype == "image/png", ctype


def test_a_file_that_is_not_an_image_is_not_served():
    """This route exists to show avatars, not to be a file server for the cache directory."""
    url = "https://avatars.steamstatic.com/not-an-image_full.jpg"
    path = avatars_mod.cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<!doctype html><html>nope</html>")
    assert httpbridge.avatar_bytes(url) is None


def test_the_sniffer_knows_the_types_steam_actually_serves():
    assert httpbridge._image_type(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "image/jpeg"
    assert httpbridge._image_type(b"GIF89a" + b"\x00" * 8) == "image/gif"
    assert httpbridge._image_type(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"
    assert httpbridge._image_type(b"RIFF\x00\x00\x00\x00WAVE") is None, "a RIFF is not a picture"
    assert httpbridge._image_type(b"") is None


# ---------------------------------------------------------------- the snapshot
def test_party_members_and_team_rosters_carry_the_avatar():
    member = SCREEN._member({"steam_id": "76561198000000102", "name": "Wario",
                             "avatar": STEAM_URL, "level": None, "ping": None}, "")
    assert member["avatar"] == STEAM_URL

    team = SCREEN._team_member({"steam_id": "76561198000000103", "name": "Peach",
                                "avatar": STEAM_URL}, "", "")
    assert team["avatar"] == STEAM_URL

    # missing on the wire (an older service, or nobody of theirs connected) -> "", never None:
    # the page tests it for truth, and a null would render as the string "null" in an <img> src.
    assert SCREEN._member({"steam_id": "1", "name": "x"}, "")["avatar"] == ""
    assert SCREEN._team_member({"steam_id": "1", "name": "x"}, "", "")["avatar"] == ""


def test_the_session_passes_the_url_through_without_fetching_it():
    """`_party_member` and `_player_from` are pure translations of a wire payload. Nothing in the
    session may go to the network for a picture - that is the bridge's job, off the UI thread."""
    from hub.competitive import LiveSession
    entry = {"steam_id": "76561198000000102", "persona": "Wario", "avatar": STEAM_URL}
    assert LiveSession._party_member(None, entry)["avatar"] == STEAM_URL
    assert LiveSession._player_from(None, entry)["avatar"] == STEAM_URL


# ---------------------------------------------------------------- the server
def test_the_service_hands_out_an_avatar_wherever_it_hands_out_a_name():
    """Party rosters and match rosters. Without this the hub knows every player's name and only
    its own face, which is exactly what the screens looked like."""
    assert "avatar: account.avatar || ''" in LIVE_CJS, "the live client must remember the face"
    assert "function avatarOf(steamId)" in LIVE_CJS
    assert "persona: personaOf(id), avatar: avatarOf(id)" in LIVE_CJS, "party roster"
    # Every roster the HUB draws from: match_found, match_ready, the connect window, the live
    # panel - and the rejoin replay, which is the whole roster a hub that dropped gets back. Miss
    # that last one and reconnecting turns everybody's face back into initials.
    rosters = LIVE_CJS.count("player_id: p.player_id, game_steam_id: identity.gameFor(match, p.player_id), persona: p.persona, avatar: p.avatar || ''")
    rosters += LIVE_CJS.count("player_id: q.player_id, game_steam_id: identity.gameFor(match, q.player_id), persona: q.persona, avatar: q.avatar || ''")
    rosters += LIVE_CJS.count("player_id: p.player_id, game_steam_id: identity.gameFor(rejoin, p.player_id), persona: p.persona, avatar: p.avatar || ''")
    assert rosters == 5, "expected five hub-facing rosters to carry avatars, found %d" % rosters
    assert "avatar: (anyClient && anyClient.avatar) || avatarOf(steamId)" in LIVE_CJS, \
        "the match must carry the face from the moment it is formed, not look it up later"


# ---------------------------------------------------------------- the page
def test_the_shared_plate_shows_a_picture_and_falls_back_to_the_letters():
    """One helper, so every plate in the app gains this at once - and the <img> removes itself on
    error, uncovering the initials that were always underneath it."""
    assert '"/avatar?u=" + encodeURIComponent(opts.url)' in UI_JS
    assert 'img.addEventListener("error"' in UI_JS
    assert "img.parentNode.removeChild(img)" in UI_JS


def test_the_three_places_sam_named_pass_a_url():
    """Profile, party card, lobby/connect/live rosters."""
    assert "url: prof.avatar || auth.avatar" in PROF_JS, "profile"
    assert COMP_JS.count("url: m.avatar") == 2, "the party card and the team rosters"
    # ...and neither of them prints a level number any more
    assert "m.level ? m.level : initials(m.name)" not in COMP_JS
    assert "(m.level === null || m.level === undefined) ? initials(m.name) : m.level" not in COMP_JS


def _run(fn):
    try:
        fn()
        RESULTS.append((fn.__name__, True))
        print("ok   %s" % fn.__name__)
    except Exception:                       # noqa: BLE001
        RESULTS.append((fn.__name__, False))
        print("FAIL %s\n%s" % (fn.__name__, traceback.format_exc()))


def main():
    for fn in [
        test_only_steam_urls_are_ever_fetched,
        test_a_cached_avatar_is_served_with_its_real_type,
        test_a_file_that_is_not_an_image_is_not_served,
        test_the_sniffer_knows_the_types_steam_actually_serves,
        test_party_members_and_team_rosters_carry_the_avatar,
        test_the_session_passes_the_url_through_without_fetching_it,
        test_the_service_hands_out_an_avatar_wherever_it_hands_out_a_name,
        test_the_shared_plate_shows_a_picture_and_falls_back_to_the_letters,
        test_the_three_places_sam_named_pass_a_url,
    ]:
        _run(fn)
    failed = [n for n, ok in RESULTS if not ok]
    print("\n%d/%d passed" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
