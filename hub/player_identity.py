"""Canonical UI actor IDs with a separate native game Steam ID.

The existing renderer calls its comparison key steam_id. This adapter keeps that
private alias while every native pak lookup uses game_steam_id explicitly.
"""
import re


def valid_player(value):
    return isinstance(value, str) and bool(re.fullmatch(r"(?:\d{17}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})", value))


def normalize(value):
    if isinstance(value, list):
        return [normalize(child) for child in value]
    if not isinstance(value, dict):
        return value
    out = {key: normalize(child) for key, child in value.items()}
    player = out.get("player_id") or out.get("steam_id")
    if valid_player(player):
        game = out.get("game_steam_id")
        if not game and re.fullmatch(r"\d{17}", str(out.get("steam_id") or "")):
            game = out["steam_id"]
        out["player_id"] = player
        out["steam_id"] = player
        if game:
            out["game_steam_id"] = game
    return out


def native_id(row):
    if not isinstance(row, dict):
        return ""
    game = row.get("game_steam_id")
    # Old server rosters have no player_id. This is the only legacy fallback.
    if not game and "player_id" not in row:
        game = row.get("steam_id")
    return game if isinstance(game, str) and re.fullmatch(r"\d{17}", game) else ""
