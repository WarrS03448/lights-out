"""Post-match card — the Python half: its snapshot slice, its one verb and its strings.

OWNED BY THE POST-MATCH CARD. It is not a view in the nav: it is an OVERLAY, raised over whatever
screen the player is on the moment a match ends, and it stays up until they close it. So this
module registers a snapshot slice and a verb like any other screen, and the JS half
(static/screens/postmatch.js) registers itself with the core as an overlay rather than a screen.

SAM, 2026-09-16: "an immediate post match Victory or Defeat screen in the app that comes up after
the game ends ... and stays there until its closed manually, either by clicking off the window or
hitting an X button. make it look similar to the match history match detail screen." All three
halves of that sentence are load-bearing:

  * IMMEDIATE — the card is built from what the session already holds when the match ends
    (hub/competitive.py ``_postmatch_record``), so it is up with the result and never waits on a
    fetch of the archived record.
  * UNTIL CLOSED — the session's ``postmatch`` is cleared by ``close_postmatch`` and by nothing
    else. Not a timer, not the phase moving back to idle, not the next snapshot push. This module
    therefore only ever *reflects* it; there is no dismissal logic here to get wrong.
  * LIKE THE MATCH DETAIL — the same sections in the same order (head, teams with sides, veto,
    the honest no-scoreboard note), which is why the fields below line up with history.py's
    ``_detail``.

Strings: per the redesign plan, a screen ships NEW strings in its own snapshot slice rather than
editing hub/i18n.py. The words this card shares with the existing result screen (Victory, Defeat,
Match voided, Team {n}, the side names, Close) already exist in seven languages in i18n, so they
are PULLED from there rather than translated a second time - two copies of "Victory" is two things
to keep in step, and the pair would drift the first time one was reworded.
"""
import math

from . import register_snapshot, register_verbs
from ..combat import player_damage_rows, player_damage_strings
from ..rounds import round_details, round_strings
from .history import _scoreboard
from ... import i18n
from ...competitive import kd_ratio
from ...competitive import format_duration


# ---------------------------------------------------------------- strings (this card's own)
# English is the source of truth and every other language is merged over it, so an
# under-translated language still renders every key (mirrors i18n.strings_for).
_EN = {
    **round_strings("en"),
    **player_damage_strings("en"),
    "title": "Match result",
    "no_map": "No map",
    "no_score": "No score reported",
    "veto": "Map veto",
    "duration": "Duration",
    "rr_unit": "RR",
    "no_rr": "No rank change",
    "placed": "Placed into {rank}",
    "you": "You",
    "note": "The scoreboard is not in the result the service sends — open this match in Match history to see it.",
    "col_k": "K",
    "col_d": "D",
    "col_kd": "K/D",
    "col_tk": "TK",
    "stat_none": "—",
    "kills_note": "Kills are the game's own net count — a team kill takes one off.",
    "combat_details": "Combat",
    "combat_complete": "Damage complete",
    "combat_partial": "Partial",
    "combat_unavailable": "Unavailable",
    "combat_partial_note": "Some combat data was not observed.",
    "combat_unavailable_note": "Combat data was not available for this player.",
    "col_damage": "Enemy damage",
    "col_adr": "ADR",
    "col_assists": "Assists",
    "col_headshots": "Observed headshots",
    "col_accuracy": "Accuracy",
    "weapons": "Damage sources",
    "col_weapon": "Source",
    "col_friendly_damage": "Friendly damage",
    "col_damage_taken": "Damage taken",
    "col_shots": "Shots",
    "col_hits": "Hits",
    "stays_hint": "This stays until you close it.",
    # Filled from i18n by strings_for(); these are the fallbacks, seen only if those keys go.
    "victory": "Victory",
    "defeat": "Defeat",
    "voided": "Match voided",
    "voided_body": "The match was ended by a player vote. Nobody gained or lost rank.",
    "team": "Team {n}",
    "side_attack": "Attackers",
    "side_defend": "Defenders",
    "close": "Close",
    "placements_left": "{n} more placement matches required for rank calibration",
}

_TRANSLATIONS = {
    "de": {
        "col_k": "K",
        "col_d": "T",
        "col_kd": "K/T",
        "col_tk": "TK",
        "stat_none": "—",
        "kills_note": "Kills sind die Nettozahl des Spiels — ein Teamkill zieht einen ab.",
        "combat_details": "Kampf", "combat_complete": "Schaden vollständig",
        "combat_partial": "Teilweise", "combat_unavailable": "Nicht verfügbar",
        "combat_partial_note": "Einige Kampfdaten wurden nicht erfasst.",
        "combat_unavailable_note": "Für diesen Spieler sind keine Kampfdaten verfügbar.",
        "col_damage": "Gegnerschaden", "col_adr": "ADR", "col_assists": "Vorlagen",
        "col_headshots": "Beobachtete Kopftreffer", "col_accuracy": "Genauigkeit", "weapons": "Schadensquellen",
        "col_weapon": "Quelle", "col_friendly_damage": "Teamschaden", "col_damage_taken": "Erlittener Schaden",
        "col_shots": "Schüsse", "col_hits": "Treffer",
        "title": "Matchergebnis",
        "no_map": "Keine Karte",
        "no_score": "Kein Ergebnis gemeldet",
        "veto": "Map-Veto",
        "duration": "Dauer",
        "rr_unit": "RR",
        "no_rr": "Keine Rangänderung",
        "placed": "Eingestuft: {rank}",
        "you": "Du",
        "note": "Die Anzeigetafel ist nicht im Ergebnis, das der Dienst sendet — öffne dieses Match im Matchverlauf, um sie zu sehen.",
        "stays_hint": "Das bleibt, bis du es schließt.",
    },
    "es": {
        "col_k": "B",
        "col_d": "M",
        "col_kd": "B/M",
        "col_tk": "BA",
        "stat_none": "—",
        "kills_note": "Las bajas son el recuento neto del juego — una baja aliada resta una.",
        "combat_details": "Combate", "combat_complete": "Daño completo",
        "combat_partial": "Parcial", "combat_unavailable": "No disponible",
        "combat_partial_note": "Algunos datos de combate no se observaron.",
        "combat_unavailable_note": "No hay datos de combate disponibles para este jugador.",
        "col_damage": "Daño al enemigo", "col_adr": "ADR", "col_assists": "Asistencias",
        "col_headshots": "Tiros a la cabeza observados", "col_accuracy": "Precisión", "weapons": "Fuentes de daño",
        "col_weapon": "Fuente", "col_friendly_damage": "Daño aliado", "col_damage_taken": "Daño recibido",
        "col_shots": "Disparos", "col_hits": "Impactos",
        "title": "Resultado de la partida",
        "no_map": "Sin mapa",
        "no_score": "Sin marcador reportado",
        "veto": "Veto de mapas",
        "duration": "Duración",
        "rr_unit": "RR",
        "no_rr": "Sin cambio de rango",
        "placed": "Clasificado en {rank}",
        "you": "Tú",
        "note": "El marcador no viene en el resultado que envía el servicio — abre esta partida en el historial para verlo.",
        "stays_hint": "Esto se queda hasta que lo cierres.",
    },
    "fr": {
        "col_k": "É",
        "col_d": "M",
        "col_kd": "É/M",
        "col_tk": "TÉ",
        "stat_none": "—",
        "kills_note": "Les éliminations sont le compte net du jeu — un tir ami en retire une.",
        "combat_details": "Combat", "combat_complete": "Dégâts complets",
        "combat_partial": "Partiel", "combat_unavailable": "Indisponible",
        "combat_partial_note": "Certaines données de combat n'ont pas été observées.",
        "combat_unavailable_note": "Les données de combat ne sont pas disponibles pour ce joueur.",
        "col_damage": "Dégâts ennemis", "col_adr": "DMR", "col_assists": "Assistances",
        "col_headshots": "Tirs à la tête observés", "col_accuracy": "Précision", "weapons": "Sources de dégâts",
        "col_weapon": "Source", "col_friendly_damage": "Dégâts alliés", "col_damage_taken": "Dégâts subis",
        "col_shots": "Tirs", "col_hits": "Touches",
        "title": "Résultat du match",
        "no_map": "Aucune carte",
        "no_score": "Aucun score rapporté",
        "veto": "Veto de cartes",
        "duration": "Durée",
        "rr_unit": "RR",
        "no_rr": "Aucun changement de rang",
        "placed": "Classé {rank}",
        "you": "Vous",
        "note": "Le tableau des scores n'est pas dans le résultat envoyé par le service — "
                "ouvrez ce match dans l'historique pour le voir.",
        "stays_hint": "Ceci reste affiché jusqu'à ce que vous le fermiez.",
    },
    "pt": {
        "col_k": "A",
        "col_d": "M",
        "col_kd": "A/M",
        "col_tk": "AE",
        "stat_none": "—",
        "kills_note": "Abates são a contagem líquida do jogo — um abate de equipa tira um.",
        "combat_details": "Combate", "combat_complete": "Dano completo",
        "combat_partial": "Parcial", "combat_unavailable": "Indisponível",
        "combat_partial_note": "Alguns dados de combate não foram observados.",
        "combat_unavailable_note": "Os dados de combate não estão disponíveis para este jogador.",
        "col_damage": "Dano ao inimigo", "col_adr": "DMR", "col_assists": "Assistências",
        "col_headshots": "Tiros na cabeça observados", "col_accuracy": "Precisão", "weapons": "Fontes de dano",
        "col_weapon": "Fonte", "col_friendly_damage": "Dano aliado", "col_damage_taken": "Dano recebido",
        "col_shots": "Disparos", "col_hits": "Acertos",
        "title": "Resultado da partida",
        "no_map": "Sem mapa",
        "no_score": "Nenhum placar reportado",
        "veto": "Veto de mapas",
        "duration": "Duração",
        "rr_unit": "RR",
        "no_rr": "Sem mudança de rank",
        "placed": "Colocado em {rank}",
        "you": "Você",
        "note": "O placar não vem no resultado que o serviço envia — abra esta partida no histórico para vê-lo.",
        "stays_hint": "Isto fica aqui até você fechar.",
    },
    "ru": {
        "col_k": "У",
        "col_d": "С",
        "col_kd": "У/С",
        "col_tk": "УС",
        "stat_none": "—",
        "kills_note": "Убийства — чистый счёт игры: убийство союзника вычитает одно.",
        "combat_details": "Бой", "combat_complete": "Урон учтён полностью",
        "combat_partial": "Неполные данные", "combat_unavailable": "Недоступно",
        "combat_partial_note": "Часть боевых данных не была зафиксирована.",
        "combat_unavailable_note": "Боевые данные этого игрока недоступны.",
        "col_damage": "Урон врагам", "col_adr": "Урон/раунд", "col_assists": "Помощь",
        "col_headshots": "Зафиксированные попадания в голову", "col_accuracy": "Точность", "weapons": "Источники урона",
        "col_weapon": "Источник", "col_friendly_damage": "Урон союзникам", "col_damage_taken": "Полученный урон",
        "col_shots": "Выстрелы", "col_hits": "Попадания",
        "title": "Итог матча",
        "no_map": "Нет карты",
        "no_score": "Счёт не передан",
        "veto": "Бан карт",
        "duration": "Длительность",
        "rr_unit": "RR",
        "no_rr": "Ранг не изменился",
        "placed": "Ранг после калибровки: {rank}",
        "you": "Вы",
        "note": "Таблицы нет в результате, который присылает сервис — откройте этот матч в истории, чтобы её увидеть.",
        "stays_hint": "Это останется на экране, пока вы не закроете.",
    },
    "zh": {
        "col_k": "击杀",
        "col_d": "死亡",
        "col_kd": "K/D",
        "col_tk": "误杀",
        "stat_none": "—",
        "kills_note": "击杀数是游戏自身的净计数——误杀队友会扣除一次。",
        "combat_details": "战斗数据", "combat_complete": "伤害数据完整",
        "combat_partial": "部分", "combat_unavailable": "不可用",
        "combat_partial_note": "部分战斗数据未被记录。",
        "combat_unavailable_note": "没有这名玩家的战斗数据。",
        "col_damage": "敌方伤害", "col_adr": "回合均伤", "col_assists": "助攻",
        "col_headshots": "已观测爆头", "col_accuracy": "命中率", "weapons": "伤害来源",
        "col_weapon": "来源", "col_friendly_damage": "友军伤害", "col_damage_taken": "承受伤害",
        "col_shots": "射击", "col_hits": "命中",
        "title": "对局结果",
        "no_map": "无地图",
        "no_score": "未上报比分",
        "veto": "地图禁用",
        "duration": "时长",
        "rr_unit": "RR",
        "no_rr": "段位未变化",
        "placed": "定级结果：{rank}",
        "you": "你",
        "note": "服务发送的结果中不含记分板——在对局历史中打开这场对局即可查看。",
        "stays_hint": "在你关闭之前，它会一直留在这里。",
    },
}

# This card's key -> the i18n key it is the same word as. Pulled at snapshot time so a reword in
# hub/i18n.py reaches the card too, and so the seven translations of "Victory" live in one place.
_SHARED = {
    "victory": "comp_result_win",
    "defeat": "comp_result_loss",
    "voided": "comp_result_void",
    "voided_body": "comp_result_void_body",
    "team": "comp_team",
    "side_attack": "comp_side_attack",
    "side_defend": "comp_side_defend",
    "close": "close",
    "placements_left": "comp_placements_left",
}


def strings_for(lang: str) -> dict:
    """This card's active-language strings, English-filled so JS can look up any key."""
    merged = dict(_EN)
    merged.update(_TRANSLATIONS.get(lang, {}))
    merged.update(player_damage_strings(lang))
    merged.update(round_strings(lang))
    shared = i18n.STRINGS.get(lang) or {}
    base = i18n.STRINGS.get(i18n.DEFAULT) or {}
    for key, source in _SHARED.items():
        merged[key] = shared.get(source) or base.get(source) or merged[key]
    return merged


# ---------------------------------------------------------------- the record, serialised
def _int_or_none(value):
    """int(value), or None when it is absent or unreadable.

    Never 0 as a stand-in for unknown — the whole point of the card's honesty rule is that a
    column of zeroes reads as "everyone went 0-0" rather than "we were not told".
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


def _combat(value, players=None):
    """The optional public combat summary; absent and unknown fields stay absent/null."""
    if not isinstance(value, dict):
        return None
    status = str(value.get("status") or "unavailable")
    if status not in ("complete", "partial", "unavailable"):
        status = "unavailable"
    coverage = value.get("coverage") if isinstance(value.get("coverage"), dict) else {}
    weapons = []
    for row in value.get("weaponStats") if isinstance(value.get("weaponStats"), list) else []:
        if not isinstance(row, dict):
            continue
        weapons.append({
            "weapon": str(row.get("weapon") or ""),
            "enemyDamage": _number_or_none(row.get("enemyDamage")),
            "friendlyDamage": _number_or_none(row.get("friendlyDamage")),
            "kills": _number_or_none(row.get("kills")),
            "headshots": _number_or_none(row.get("headshots")),
            "shots": _number_or_none(row.get("shots")),
            "hits": _number_or_none(row.get("hits")),
        })
    return {
        "version": _int_or_none(value.get("version")) or 1,
        "status": status,
        "coverage": {key: coverage.get(key) is True
                     for key in ("damage", "shots", "objectives")},
        "enemyDamage": _number_or_none(value.get("enemyDamage")),
        "friendlyDamage": _number_or_none(value.get("friendlyDamage")),
        "damageTaken": _number_or_none(value.get("damageTaken")),
        "assists": _number_or_none(value.get("assists")),
        "headshots": _number_or_none(value.get("headshots")),
        "shots": _number_or_none(value.get("shots")),
        "hits": _number_or_none(value.get("hits")),
        "adr": _number_or_none(value.get("adr")),
        "accuracy": _number_or_none(value.get("accuracy")),
        "weaponStats": weapons,
        "playerStats": player_damage_rows(value, players),
    }
def _player(p: dict, players=None) -> dict:
    sid = str(p.get("steam_id") or "")
    return {
        "steam_id": sid,
        "name": str(p.get("name") or sid),
        "is_me": bool(p.get("is_me")),
        "left": bool(p.get("left")),
        # Their line, joined on by _card. All None when the gamemode reported nothing about them,
        # which the card draws as dashes rather than as a real 0.
        "reported": bool(p.get("reported")),
        "kills": p.get("kills"),
        "deaths": p.get("deaths"),
        "kd": p.get("kd"),
        "team_kills": p.get("team_kills"),
        "combat": _combat(p.get("combat"), players),
    }


def _card(record) -> dict | None:
    """The frozen match, trimmed to what the card draws, or None when none is up.

    Honest with nulls exactly as history.py's ``_detail`` is: ``won`` is None for a match nobody
    reported a winner for, ``score`` is None when no scoreline arrived, and the card prints a
    placeholder for each rather than a fabricated 0 that would read as a real result.

    ``has_scoreboard`` is False and stays False. Per-player stats DO exist now - the archive
    carries them and the match-detail panel draws them - but they are not on the `match_result`
    event this card is built from, and the card's whole point is that it is up the instant the
    match ends rather than after a fetch. So it says where the scoreboard is instead of showing
    an empty table or, worse, claiming the stats do not exist."""
    if not isinstance(record, dict):
        return None
    teams = record.get("teams") or {}
    sides = record.get("sides") or {}

    def side_of(n):
        return str(sides.get(n) or sides.get(str(n)) or "")

    # THE BOARD, indexed so each roster row can pick up its own line. Joined here rather than
    # drawn as a separate table: the card already lists both teams, and a second list of the same
    # ten people underneath it would be the same information twice.
    board = {}
    for entry in (record.get("scoreboard") or []):
        if isinstance(entry, dict) and entry.get("steam_id"):
            board[str(entry["steam_id"])] = entry

    players = {str(p.get("steam_id") or ""): p
               for n in (1, 2) for p in (teams.get(n) or teams.get(str(n)) or [])
               if isinstance(p, dict)}

    def roster(n):
        rows = teams.get(n) or teams.get(str(n)) or []
        out = []
        for p in rows:
            if not isinstance(p, dict):
                continue
            stat = board.get(str(p.get("steam_id") or "")) or {}
            kills = _int_or_none(stat.get("kills"))
            deaths = _int_or_none(stat.get("deaths"))
            out.append(_player({**p,
                                "reported": stat.get("reported"),
                                "kills": kills,
                                "deaths": deaths,
                                "kd": kd_ratio(kills, deaths),
                                "team_kills": _int_or_none(stat.get("team_kills")),
                                "combat": stat.get("combat") if isinstance(stat.get("combat"), dict)
                                else p.get("combat")}, players))
        return out

    score = record.get("score")
    seconds = int(record.get("seconds") or 0)
    my_team = int(record.get("my_team") or 0)
    team_rows = {"1": roster(1), "2": roster(2)}
    has_combat = any(isinstance(r.get("combat"), dict) for r in board.values())
    if not has_combat:
        has_combat = any(p.get("combat") is not None
                         for rows in team_rows.values() for p in rows)
    return {
        "match_id": str(record.get("match_id") or ""),
        "at": float(record.get("at") or 0),
        "map": str(record.get("map") or ""),
        # THE HEADLINE, and the only three it can be. None is a real answer - a match that ended
        # without the service naming a winner - and the card leads with the map instead of
        # claiming a victory or a defeat nobody reported.
        "won": record.get("won") if isinstance(record.get("won"), bool) else None,
        "voided": bool(record.get("voided")),
        "void_reason": str(record.get("void_reason") or ""),
        "my_team": my_team if my_team in (1, 2) else 0,
        "winner": record.get("winner") if record.get("winner") in (1, 2) else None,
        "score": [int(score[0]), int(score[1])] if isinstance(score, (list, tuple))
                 and len(score) >= 2 else None,
        "delta": int(record.get("delta") or 0),
        # THE RR THE MATCH MOVED - what the card prints, not `delta` above, which is an arrow count.
        # None when the result did not carry it, and then the card draws no rank line at all.
        "rr_delta": _int_or_none(record.get("rr_delta")),
        # Placements pay no RR, so a card for one says how many are left, or where they landed.
        "placing": bool(record.get("placing")),
        "placed": bool(record.get("placed")),
        "placements_left": _int_or_none(record.get("placements_left")) or 0,
        "placed_rank": str(record.get("placed_rank") or ""),
        # Formatted in Python: JS has no format_duration, and the same sentence is already
        # written in seven languages there. "" when the match did not report a length.
        "duration": format_duration(seconds) if seconds > 0 else "",
        "teams": team_rows,
        "round_details": round_details(record, [{**p, "team": int(n)} for n, rows in team_rows.items() for p in rows], _scoreboard),
        "sides": {"1": side_of(1), "2": side_of(2)},
        "bans": [{"team": int(b.get("team") or 0), "map": str(b.get("map") or "")}
                 for b in (record.get("bans") or []) if isinstance(b, dict)],
        # True once the gamemode has reported ANY player's line. False keeps the old honest
        # note, which is still the right answer for a match it said nothing about.
        "has_scoreboard": has_combat or any(r.get("reported") for r in board.values()),
        # Only worth a footnote when there is a TK column to explain.
        "has_team_kills": any(r.get("team_kills") is not None for r in board.values()),
    }


# ---------------------------------------------------------------- snapshot slice
@register_snapshot("postmatch")
def snapshot(session, panel) -> dict:
    """The card, or ``open: False`` when there is none.

    Note what is NOT here: no timer, no phase, no "seen" flag. The card's whole lifetime is the
    session's ``postmatch`` being non-None, and only ``close_postmatch`` ends it - so a snapshot
    pushed for something else entirely (a stats tick, a language switch, the next queue) cannot
    take it off the screen mid-read."""
    card = _card(getattr(session, "postmatch", None))
    return {"postmatch": {
        "open": card is not None,
        "card": card,
        "strings": strings_for(i18n.get_language()),
    }}


# ---------------------------------------------------------------- bridge verbs
# ONE verb, and it is the dismissal. The X, a backdrop click and Escape all land here (ui.modal
# wires all three to the one onClose), so there is a single way out in the code as well as on
# screen.
register_verbs("postmatch", {
    "close_postmatch": lambda panel: panel.post(panel.session.close_postmatch),
})
