"""Shared combat display rows. Player names come from the match roster (UTF-8)."""
import math
import re

_LABELS = {
    "en": ("Damage by player", "Player", "Damage dealt", "Damage taken", "Unknown source", "Damage by player is unavailable for this match."),
    "de": ("Schaden nach Spieler", "Spieler", "Verursachter Schaden", "Erlittener Schaden", "Unbekannte Quelle", "Schaden nach Spieler ist für dieses Match nicht verfügbar."),
    "es": ("Daño por jugador", "Jugador", "Daño infligido", "Daño recibido", "Fuente desconocida", "El daño por jugador no está disponible para esta partida."),
    "fr": ("Dégâts par joueur", "Joueur", "Dégâts infligés", "Dégâts subis", "Source inconnue", "Les dégâts par joueur ne sont pas disponibles pour ce match."),
    "pt": ("Dano por jogador", "Jogador", "Dano causado", "Dano recebido", "Fonte desconhecida", "O dano por jogador não está disponível para esta partida."),
    "ru": ("Урон по игрокам", "Игрок", "Нанесённый урон", "Полученный урон", "Неизвестный источник", "Данные об уроне по игрокам для этого матча недоступны."),
    "zh": ("按玩家统计伤害", "玩家", "造成伤害", "受到伤害", "未知来源", "本场对局没有按玩家统计的伤害数据。"),
}


def player_damage_strings(lang):
    return dict(zip(("player_damage", "damage_player", "damage_to", "damage_from", "unknown_source", "player_damage_unavailable"),
                    _LABELS.get(lang, _LABELS["en"])))


def player_damage_rows(value, players=None):
    """Keep missing directions unknown and never use a causer class as a player."""
    rows = value.get("playerStats")
    if not isinstance(rows, list):
        return []
    players = players or {}

    def number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            n = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return (int(n) if n.is_integer() else n) if math.isfinite(n) and n >= 0 else None

    out = []
    for row in rows[:65]:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("steam_id") or "")
        if sid and not re.fullmatch(r"[0-9]{17}", sid):
            continue
        who = players.get(sid) or {}
        out.append({"steam_id": sid, "name": str(who.get("name") or who.get("persona") or sid),
                    "damageDealt": number(row.get("damageDealt")),
                    "damageTaken": number(row.get("damageTaken"))})
    return out
