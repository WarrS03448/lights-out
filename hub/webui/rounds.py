"""Shared, read-only round selector data and translations for both match views."""
import math


_LABELS = {
    "en": ("All rounds", "Round {n}", "Select a round to see its player stats and combat details.", "Team {n} won", "Round result unavailable", "Per-round stats were not recorded for this round.", "Duration", "Score after round"),
    "de": ("Alle Runden", "Runde {n}", "Wähle eine Runde für Spielerstatistiken und Kampfdetails.", "Team {n} hat gewonnen", "Rundenergebnis nicht verfügbar", "Für diese Runde wurden keine Spielerstatistiken aufgezeichnet.", "Dauer", "Stand nach der Runde"),
    "es": ("Todas las rondas", "Ronda {n}", "Selecciona una ronda para ver estadísticas y detalles de combate.", "Ganó el equipo {n}", "Resultado de ronda no disponible", "No se registraron estadísticas de jugadores para esta ronda.", "Duración", "Marcador tras la ronda"),
    "fr": ("Toutes les manches", "Manche {n}", "Sélectionnez une manche pour voir les statistiques et les détails de combat.", "Victoire de l’équipe {n}", "Résultat de la manche indisponible", "Aucune statistique de joueur enregistrée pour cette manche.", "Durée", "Score après la manche"),
    "pt": ("Todos os rounds", "Round {n}", "Selecione um round para ver estatísticas e detalhes de combate.", "A equipa {n} venceu", "Resultado do round indisponível", "Não foram registadas estatísticas de jogadores para este round.", "Duração", "Placar após o round"),
    "ru": ("Все раунды", "Раунд {n}", "Выберите раунд, чтобы увидеть статистику игроков и боевые данные.", "Команда {n} победила", "Результат раунда недоступен", "Статистика игроков за этот раунд не была записана.", "Длительность", "Счёт после раунда"),
    "zh": ("全部回合", "第 {n} 回合", "选择回合以查看玩家统计和战斗详情。", "队伍 {n} 获胜", "回合结果不可用", "未记录此回合的玩家统计。", "时长", "回合结束比分"),
}
_KEYS = ("all_rounds", "round_label", "round_hint", "round_winner", "round_unknown", "round_no_stats", "round_duration", "round_score")


def round_strings(lang):
    return dict(zip(_KEYS, _LABELS.get(lang, _LABELS["en"])))


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def round_details(record, players, board_factory):
    """Keep missing rounds visible, without substituting match totals for round stats."""
    rows = record.get("round_details")
    by_round = {r["n"]: r for r in rows if isinstance(r, dict)
                and isinstance(r.get("n"), int) and 1 <= r["n"] <= 99} if isinstance(rows, list) else {}
    total = _number(record.get("rounds_played")) or 0
    score = record.get("score")
    values = [score.get("1", score.get(1)), score.get("2", score.get(2))] if isinstance(score, dict) else score
    if isinstance(values, (list, tuple)) and len(values) == 2 and all(_number(v) is not None for v in values):
        total = max(total, sum(values))
    # Old score reports can span multiple rounds. Only unambiguous, single-side
    # increases reveal winners; two sides changing never reveals their order.
    previous = [0, 0]
    legacy = record.get("rounds")
    for row in legacy if isinstance(legacy, list) else []:
        if not isinstance(row, dict):
            continue
        score = [row.get("1", row.get(1)), row.get("2", row.get(2))]
        if any(_number(v) is None or v < 0 for v in score) or any(a < b for a, b in zip(score, previous)):
            continue
        gains = [score[i] - previous[i] for i in (0, 1)]
        start, end = int(sum(previous)), int(sum(score))
        if end > 99:
            continue
        if (gains[0] > 0) != (gains[1] > 0):
            side = 1 if gains[0] else 2
            for n in range(start + 1, end + 1):
                running = previous.copy()
                running[side - 1] += n - start
                by_round.setdefault(n, {"n": n, "won": side, "score": running})
        total = max(total, end)
        previous = score
    total = min(99, max(int(total), max(by_round, default=0)))
    # A missing round row must not inherit a player's full-match combat summary.
    roster = [{**p, "combat": None} for p in players]
    output = []
    for n in range(1, total + 1):
        row = by_round.get(n, {})
        raw = row.get("scoreboard")
        stats = {str(p.get("steam_id")): p for p in raw if isinstance(p, dict)} if isinstance(raw, list) else {}
        board = board_factory({"scoreboard": [stats.get(p["steam_id"], {"steam_id": p["steam_id"], "team": p["team"]})
                                                for p in roster]}, roster)
        score = row.get("score")
        output.append({"n": n, "won": row.get("won") if row.get("won") in (1, 2) else None,
                       "score": score if isinstance(score, list) and len(score) == 2
                       and all(_number(v) is not None for v in score) else None,
                       "seconds": _number(row.get("seconds")), "scoreboard": board,
                       "has_stats": any(p["reported"] or (p.get("combat") or {}).get("status") in ("partial", "complete") for p in board)})
    return output
