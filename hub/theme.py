"""The hub's colours and small drawing helpers, in one place.

app.py used to own the palette; the Competitive tab needs the same colours, so they
live here and app.py re-exports the original names (WHITE, BLACK, GREY, DARK_RED,
SELECT_BG) so nothing that imported them from hub.app has to change.

Deliberately flat and light: white ground, black text, one blue for selection and
links, and a small set of accents that only the competitive screens use.
"""

# ---------------------------------------------------------------- the original five
WHITE = "#FFFFFF"
BLACK = "#000000"
GREY = "#777777"
DARK_RED = "#8B0000"
SELECT_BG = "#CCE8FF"          # the blue of a selected row (Windows Explorer's selection blue)

# ---------------------------------------------------------------- competitive additions
LINE = "#DDDDDD"               # hairline rules and box borders
PANEL = "#F5F7FA"              # the very light card ground
PANEL_LINE = "#E3E7ED"
ACCENT = "#1B6AC9"             # links, the active tab, the primary button
ACCENT_DARK = "#14508F"
GREEN = "#1F7A3D"              # a win, an accepted player, rank gained
RED = "#B32828"                # a loss, a decline, rank lost
AMBER = "#B07A00"              # waiting / your turn
MUTED = "#9AA3AE"              # struck-out map names, placeholder text

# FACEIT-style level colours, 1-14. 14 is the top-500 tier (Sam, 2026-09-14).
LEVEL_COLOURS = {
    1: "#EEEEEE",
    2: "#1FBF5F", 3: "#1FBF5F",
    4: "#FFC115", 5: "#FFC115", 6: "#FFC115", 7: "#FFC115",
    8: "#FF6309", 9: "#FF6309",
    10: "#FE3B1F",
    11: "#7B5CFF", 12: "#7B5CFF", 13: "#7B5CFF",
    14: "#D4AF37",
}
# levels whose badge needs dark text (everything else gets white)
LEVEL_DARK_TEXT = {1, 4, 5, 6, 7, 14}

# a stable colour per player, so the same name always draws the same avatar
AVATAR_COLOURS = ["#3D6FB4", "#4C8C6B", "#A05C3E", "#7A5AA8", "#B0803A",
                  "#396E7E", "#8A4A62", "#5C6BA0", "#6E8A3C", "#9A5040"]


# RANK COLOURS, 1-9, for the ladder the server keeps (progress.cjs RANK_NAMES + RANK_TOP:
# Rookie, Private, Soldier, Veteran, Operator, Shadow, Nightmare, Spectre, Reaper).
#
# TAKEN FROM THE BADGE ART, not picked here: each pair is the plate and the ink of that rank's
# icon in hub/webui/static/ranks.svg, converted out of OKLCH. The Tk panel and the web UI draw
# the same ladder, and two hand-picked palettes for one ladder is how the hub ends up with a
# green badge beside a violet one for the same rank.
#
# It CLIMBS INTO THE DARK (docs/ranks.md): the plate loses lightness and the ink loses chroma all
# the way up, so Reaper is the quietest thing on screen rather than the loudest. The old
# table did the opposite - it ran grey, green, blue, violet, yellow, orange, red, gold, brightest
# at the top - which is the convention this ladder was written against.
#
# The LEVEL_COLOURS table above is the older 1-14 FACEIT scale and is kept only for anything still
# drawing a level.
# The redrawn 3.0.0 badges (tools/rank-art): each rank's division-1 plate and ink, converted
# from the sprite's oklch. Ranks 1-4 are a soldier's career in military colours; from Operator
# the ladder turns into night. Keep these in step with tools/rank-art/ranks/r<N>.js.
RANK_COLOURS = {
    1: "#423219",   # Rookie - training sand
    2: "#2E3416",   # Private - olive drab
    3: "#182F1C",   # Soldier - field green
    4: "#1F2730",   # Veteran - gunmetal
    5: "#062322",   # Operator - night-vision teal
    6: "#13192F",   # Shadow - indigo
    7: "#1A0506",   # Nightmare - blood-black
    8: "#030A12",   # Spectre - cold blue-black (sprite #030C15, one step darker so the plates
                    # still darken by luma past Nightmare's red-black)
    9: "#04060A",   # Reaper - the capstone: no divisions, and the figure keeps counting
}
# The glyph ink for each rank, which is what a numeral on that plate is drawn in. Light on dark
# the whole way up, so nothing needs a black/white decision per rank.
RANK_INK = {
    1: "#EAD1A4",   # Rookie
    2: "#D0D2A3",   # Private
    3: "#AABD9D",   # Soldier
    4: "#D1B695",   # Veteran - brass
    5: "#91BFB8",   # Operator
    6: "#A4ACD1",   # Shadow
    7: "#C6B8A7",   # Nightmare - bone
    8: "#98A7AF",   # Spectre
    9: "#A1A4AC",   # Reaper
}
RANKS = len(RANK_COLOURS)

# Roman numerals for the division inside a rank. Three divisions by default; the list is longer
# than it needs to be so a server configured with more does not fall off the end.
DIVISION_NUMERALS = ["I", "II", "III", "IV", "V"]


def rank_colour(rank: int) -> str:
    """The plate a rank's badge is drawn on."""
    return RANK_COLOURS.get(max(1, min(RANKS, int(rank or 1))), RANK_COLOURS[1])


def rank_text_colour(rank: int) -> str:
    """The ink on that plate - the badge's own glyph colour, not a black/white choice."""
    return RANK_INK.get(max(1, min(RANKS, int(rank or 1))), RANK_INK[1])


def division_numeral(division) -> str:
    """'II' for division 2. Empty for the apex ranks, which have no divisions."""
    try:
        n = int(division)
    except (TypeError, ValueError):
        return ""
    if n < 1 or n > len(DIVISION_NUMERALS):
        return ""
    return DIVISION_NUMERALS[n - 1]


def level_colour(level: int) -> str:
    return LEVEL_COLOURS.get(max(1, min(14, int(level or 1))), LEVEL_COLOURS[1])


def level_text_colour(level: int) -> str:
    return BLACK if max(1, min(14, int(level or 1))) in LEVEL_DARK_TEXT else WHITE


def avatar_colour(name: str) -> str:
    return AVATAR_COLOURS[sum(ord(c) for c in (name or "?")) % len(AVATAR_COLOURS)]


def initials(name: str) -> str:
    """Up to two letters for the avatar placeholder (until Steam avatars are wired up)."""
    parts = [p for p in str(name or "?").replace("_", " ").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()
