"""Match history screen — the Python half: its snapshot slice, verbs and strings.

OWNED BY THE HISTORY SCREEN. This is the only Python screen module the history agent edits. It
contributes the ``history`` slice of the state snapshot (the read-only record of the player's past
matches) and registers the load/refresh verbs the JS screen (static/screens/history.js) calls.

The record itself is produced by the backend (server/live.cjs ``historyRow``) and read into the
session by ``LiveSession.load_history`` / ``_history_result`` (hub/competitive.py). Nothing here is
authoritative: this module only *serialises* what the session already holds for display, keeping
the honest nulls the record carries (``won``/``score``/``delta`` stay null until a gamemode reports
a scoreboard; ``elo`` is a negative RR debt only on penalised rows). The per-row arithmetic that
the summary needs is the SAME pure function the Tk profile uses (``competitive.profile_stats``), so
the web and Tk views can never disagree about what a history adds up to.

Strings: per the redesign plan, screens ship NEW strings in their own snapshot slice rather than
editing hub/i18n.py. This screen ships its active-language strings under ``history.strings`` and the
JS reads them by key (its local ``hs()`` helper), so there is no second translation store to keep in
sync and the JS never references an i18n key that does not exist. This package imports no pywebview
and no Tk beyond what competitive already pulls, so the slice stays testable headless.
"""
import math

from . import register_snapshot, register_verbs
from ..combat import player_damage_rows, player_damage_strings
from ..rounds import round_details, round_strings
from ... import i18n
from ...competitive import kd_ratio, profile_stats


# ---------------------------------------------------------------- strings (this screen's own)
# English is the source of truth and every other language is merged over it, so an under-translated
# language still renders every key (mirrors i18n.strings_for). Use … (U+2026) for ellipses
# and sentence case labels.
_EN = {
    **round_strings("en"),
    **player_damage_strings("en"),
    "title": "Match history",
    "match_detail": "Match detail",
    "rounds": "Rounds",
    "team": "Team {n}",
    "veto": "Map veto",
    "auto_ban": "(auto)",
    "report": "Report",
    "no_scoreboard": "The gamemode reported no per-player stats for this match.",
    "scoreboard": "Scoreboard",
    "col_k": "K",
    "col_d": "D",
    "col_kd": "K/D",
    "col_tk": "TK",
    "col_player": "Player",
    "stat_none": "-",
    "rounds_played": "{n} rounds",
    "not_reported": "The gamemode never reported this player.",
    "kills_note": "Kills are the game's own net count. A team kill takes one off. TK is counted "
                  "separately from the kill feed.",
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
    "subtitle_recorded": "{n} matches",
    "subtitle_played": "{n} played",
    "subtitle_cancelled": "{n} cancelled",
    "subtitle_record": "{w}W · {l}L",
    "filter_all": "All",
    "filter_played": "Played",
    "filter_cancelled": "Cancelled",
    "res_win": "Win",
    "res_loss": "Loss",
    "res_played": "Played",
    "res_cancelled": "Cancelled",
    "res_fault": "At fault",
    "reason_no_show": "No show",
    "reason_declined": "Declined",
    "reason_abandoned": "Left lobby",
    "side_attack": "Attack",
    "side_defend": "Defense",
    "host_tag": "Host",
    "preview_tag": "Preview",
    "no_map": "No map",
    "col_result": "Result",
    "col_map": "Map",
    "col_score": "Score",
    "col_rr": "RR",
    "col_when": "When",
    "score_pending": "no score",
    "kda_label": "K/D",
    "kda_pending": "-",
    "rr_pending": "-",
    "rr_unit": "RR",
    "rr_placement": "Placement",
    "pending_note": "Some of these matches never reported a result, so they carry no score and "
                    "no RR. Everything else here is real.",
    "players": "{n} players",
    "empty_title": "No matches yet",
    "empty_hint": "Play a competitive match and it shows up here with the map, your team and what it "
                  "cost you.",
    "loading": "Loading your matches…",
    "refresh": "Refresh",
    "signed_out_cta": "Sign in to see your match history.",
    "when_now": "just now",
    "when_min": "{n} min ago",
    "when_hour": "{n} h ago",
    "when_day": "{n} d ago",
}

_TRANSLATIONS = {
    "de": {
        "title": "Matchverlauf",
        "match_detail": "Matchdetails",
        "rounds": "Runden",
        "team": "Team {n}",
        "veto": "Map-Veto",
        "auto_ban": "(auto)",
        "report": "Melden",
        "no_scoreboard": "Der Spielmodus hat für dieses Match keine Spielerstatistiken gemeldet.",
        "scoreboard": "Punktetafel",
        "col_k": "K",
        "col_d": "T",
        "col_kd": "K/T",
        "col_tk": "TK",
        "col_player": "Spieler",
        "stat_none": "-",
        "rounds_played": "{n} Runden",
        "not_reported": "Der Spielmodus hat diesen Spieler nie gemeldet.",
        "kills_note": "Kills sind die Nettozahl des Spiels. Ein Teamkill zieht einen ab. TK wird separat aus dem Kill-Feed gezählt.",
        "combat_details": "Kampf", "combat_complete": "Schaden vollständig",
        "combat_partial": "Teilweise", "combat_unavailable": "Nicht verfügbar",
        "combat_partial_note": "Einige Kampfdaten wurden nicht erfasst.",
        "combat_unavailable_note": "Für diesen Spieler sind keine Kampfdaten verfügbar.",
        "col_damage": "Gegnerschaden", "col_adr": "ADR", "col_assists": "Vorlagen",
        "col_headshots": "Beobachtete Kopftreffer", "col_accuracy": "Genauigkeit",
        "weapons": "Schadensquellen", "col_weapon": "Quelle",
        "col_friendly_damage": "Teamschaden", "col_damage_taken": "Erlittener Schaden", "col_shots": "Schüsse",
        "col_hits": "Treffer",
        "kda_label": "K/T",
        "subtitle_recorded": "{n} Matches", "subtitle_played": "{n} gespielt",
        "subtitle_cancelled": "{n} abgebrochen", "subtitle_record": "{w}S · {l}N",
        "filter_all": "Alle", "filter_played": "Gespielt", "filter_cancelled": "Abgebrochen",
        "res_win": "Sieg", "res_loss": "Niederlage", "res_played": "Gespielt",
        "res_cancelled": "Abgebrochen", "res_fault": "Verschuldet",
        "reason_no_show": "Nicht erschienen", "reason_declined": "Abgelehnt",
        "reason_abandoned": "Lobby verlassen", "side_attack": "Angriff",
        "side_defend": "Verteidigung", "host_tag": "Host", "preview_tag": "Vorschau",
        "no_map": "Keine Karte", "col_result": "Ergebnis", "col_map": "Karte",
        "col_score": "Punkte", "col_rr": "RR", "col_when": "Wann",
        "score_pending": "kein Ergebnis", "rr_placement": "Platzierung",
        "pending_note": "Einige dieser Matches haben nie ein Ergebnis gemeldet, daher haben sie "
                        "weder Punkte noch RR. Alles andere hier ist echt.",
        "players": "{n} Spieler", "empty_title": "Noch keine Matches",
        "empty_hint": "Spiele ein Wettkampfmatch und es erscheint hier mit Karte, deinem Team "
                      "und dem, was es dich gekostet hat.",
        "loading": "Deine Matches werden geladen…", "refresh": "Aktualisieren",
        "signed_out_cta": "Melde dich an, um deinen Matchverlauf zu sehen.",
        "when_now": "gerade eben", "when_min": "vor {n} Min.", "when_hour": "vor {n} Std.",
        "when_day": "vor {n} T.",
    },
    "es": {
        "title": "Historial de partidas",
        "match_detail": "Detalle de la partida",
        "rounds": "Rondas",
        "team": "Equipo {n}",
        "veto": "Veto de mapas",
        "auto_ban": "(auto)",
        "report": "Reportar",
        "no_scoreboard": "El modo de juego no informó estadísticas por jugador para esta partida.",
        "scoreboard": "Marcador",
        "col_k": "B",
        "col_d": "M",
        "col_kd": "B/M",
        "col_tk": "BA",
        "col_player": "Jugador",
        "stat_none": "-",
        "rounds_played": "{n} rondas",
        "not_reported": "El modo de juego nunca informó de este jugador.",
        "kills_note": "Las bajas son el recuento neto del juego. Una baja aliada resta una. BA se cuenta aparte, desde el registro de bajas.",
        "combat_details": "Combate", "combat_complete": "Daño completo",
        "combat_partial": "Parcial", "combat_unavailable": "No disponible",
        "combat_partial_note": "Algunos datos de combate no se observaron.",
        "combat_unavailable_note": "No hay datos de combate disponibles para este jugador.",
        "col_damage": "Daño al enemigo", "col_adr": "ADR", "col_assists": "Asistencias",
        "col_headshots": "Tiros a la cabeza observados", "col_accuracy": "Precisión",
        "weapons": "Fuentes de daño", "col_weapon": "Fuente", "col_friendly_damage": "Daño aliado",
        "col_damage_taken": "Daño recibido",
        "col_shots": "Disparos", "col_hits": "Impactos",
        "kda_label": "B/M",
        "subtitle_recorded": "{n} partidas", "subtitle_played": "{n} jugadas",
        "subtitle_cancelled": "{n} canceladas", "subtitle_record": "{w}V · {l}D",
        "filter_all": "Todas", "filter_played": "Jugadas", "filter_cancelled": "Canceladas",
        "res_win": "Victoria", "res_loss": "Derrota", "res_played": "Jugada",
        "res_cancelled": "Cancelada", "res_fault": "Culpa tuya",
        "reason_no_show": "No apareció", "reason_declined": "Rechazada",
        "reason_abandoned": "Salió del lobby", "side_attack": "Ataque",
        "side_defend": "Defensa", "host_tag": "Anfitrión", "preview_tag": "Vista previa",
        "no_map": "Sin mapa", "col_result": "Resultado", "col_map": "Mapa",
        "col_score": "Marcador", "col_rr": "RR", "col_when": "Cuándo",
        "score_pending": "sin marcador", "rr_placement": "Colocación",
        "pending_note": "Algunas de estas partidas nunca informaron un resultado, así que no "
                        "tienen marcador ni RR. Todo lo demás aquí es real.",
        "players": "{n} jugadores", "empty_title": "Aún no hay partidas",
        "empty_hint": "Juega una partida competitiva y aparecerá aquí con el mapa, tu equipo y lo "
                      "que te costó.",
        "loading": "Cargando tus partidas…", "refresh": "Actualizar",
        "signed_out_cta": "Inicia sesión para ver tu historial de partidas.",
        "when_now": "ahora mismo", "when_min": "hace {n} min", "when_hour": "hace {n} h",
        "when_day": "hace {n} d",
    },
    "fr": {
        "title": "Historique des matchs",
        "match_detail": "Détail du match",
        "rounds": "Manches",
        "team": "Équipe {n}",
        "veto": "Veto de cartes",
        "auto_ban": "(auto)",
        "report": "Signaler",
        "no_scoreboard": "Le mode de jeu n'a rapporté aucune statistique par joueur pour ce match.",
        "scoreboard": "Tableau des scores",
        "col_k": "É",
        "col_d": "M",
        "col_kd": "É/M",
        "col_tk": "TÉ",
        "col_player": "Joueur",
        "stat_none": "-",
        "rounds_played": "{n} manches",
        "not_reported": "Le mode de jeu n'a jamais rapporté ce joueur.",
        "kills_note": "Les éliminations sont le compte net du jeu. Un tir ami en retire une. TÉ est compté à part, depuis le journal des éliminations.",
        "combat_details": "Combat", "combat_complete": "Dégâts complets",
        "combat_partial": "Partiel", "combat_unavailable": "Indisponible",
        "combat_partial_note": "Certaines données de combat n'ont pas été observées.",
        "combat_unavailable_note": "Les données de combat ne sont pas disponibles pour ce joueur.",
        "col_damage": "Dégâts ennemis", "col_adr": "DMR", "col_assists": "Assistances",
        "col_headshots": "Tirs à la tête observés", "col_accuracy": "Précision",
        "weapons": "Sources de dégâts", "col_weapon": "Source", "col_friendly_damage": "Dégâts alliés",
        "col_damage_taken": "Dégâts subis",
        "col_shots": "Tirs", "col_hits": "Touches",
        "kda_label": "É/M",
        "subtitle_recorded": "{n} matchs", "subtitle_played": "{n} joués",
        "subtitle_cancelled": "{n} annulés", "subtitle_record": "{w}V · {l}D",
        "filter_all": "Tous", "filter_played": "Joués", "filter_cancelled": "Annulés",
        "res_win": "Victoire", "res_loss": "Défaite", "res_played": "Joué",
        "res_cancelled": "Annulé", "res_fault": "En faute",
        "reason_no_show": "Absent", "reason_declined": "Refusé",
        "reason_abandoned": "Lobby quitté", "side_attack": "Attaque",
        "side_defend": "Défense", "host_tag": "Hôte", "preview_tag": "Aperçu",
        "no_map": "Aucune carte", "col_result": "Résultat", "col_map": "Carte",
        "col_score": "Score", "col_rr": "RR", "col_when": "Quand",
        "score_pending": "aucun score", "rr_placement": "Placement",
        "pending_note": "Certains de ces matchs n'ont jamais rapporté de résultat : ils n'ont "
                        "donc ni score ni RR. Tout le reste ici est réel.",
        "players": "{n} joueurs", "empty_title": "Aucun match pour l'instant",
        "empty_hint": "Jouez un match compétitif et il apparaîtra ici avec la carte, votre équipe et "
                      "ce qu'il vous a coûté.",
        "loading": "Chargement de vos matchs…", "refresh": "Actualiser",
        "signed_out_cta": "Connectez-vous pour voir votre historique de matchs.",
        "when_now": "à l'instant", "when_min": "il y a {n} min", "when_hour": "il y a {n} h",
        "when_day": "il y a {n} j",
    },
    "pt": {
        "title": "Histórico de partidas",
        "match_detail": "Detalhe da partida",
        "rounds": "Rounds",
        "team": "Equipa {n}",
        "veto": "Veto de mapas",
        "auto_ban": "(auto)",
        "report": "Denunciar",
        "no_scoreboard": "O modo de jogo não reportou estatísticas por jogador para esta partida.",
        "scoreboard": "Placar",
        "col_k": "A",
        "col_d": "M",
        "col_kd": "A/M",
        "col_tk": "AE",
        "col_player": "Jogador",
        "stat_none": "-",
        "rounds_played": "{n} rounds",
        "not_reported": "O modo de jogo nunca reportou este jogador.",
        "kills_note": "Abates são a contagem líquida do jogo. Um abate de equipa tira um. AE é contado à parte, a partir do registo de abates.",
        "combat_details": "Combate", "combat_complete": "Dano completo",
        "combat_partial": "Parcial", "combat_unavailable": "Indisponível",
        "combat_partial_note": "Alguns dados de combate não foram observados.",
        "combat_unavailable_note": "Os dados de combate não estão disponíveis para este jogador.",
        "col_damage": "Dano ao inimigo", "col_adr": "DMR", "col_assists": "Assistências",
        "col_headshots": "Tiros na cabeça observados", "col_accuracy": "Precisão",
        "weapons": "Fontes de dano", "col_weapon": "Fonte", "col_friendly_damage": "Dano aliado",
        "col_damage_taken": "Dano recebido",
        "col_shots": "Disparos", "col_hits": "Acertos",
        "kda_label": "A/M",
        "subtitle_recorded": "{n} partidas", "subtitle_played": "{n} jogadas",
        "subtitle_cancelled": "{n} canceladas", "subtitle_record": "{w}V · {l}D",
        "filter_all": "Todas", "filter_played": "Jogadas", "filter_cancelled": "Canceladas",
        "res_win": "Vitória", "res_loss": "Derrota", "res_played": "Jogada",
        "res_cancelled": "Cancelada", "res_fault": "Sua culpa",
        "reason_no_show": "Não compareceu", "reason_declined": "Recusada",
        "reason_abandoned": "Saiu do lobby", "side_attack": "Ataque",
        "side_defend": "Defesa", "host_tag": "Anfitrião", "preview_tag": "Prévia",
        "no_map": "Sem mapa", "col_result": "Resultado", "col_map": "Mapa",
        "col_score": "Placar", "col_rr": "RR", "col_when": "Quando",
        "score_pending": "sem placar", "rr_placement": "Colocação",
        "pending_note": "Algumas destas partidas nunca reportaram um resultado, por isso não "
                        "têm placar nem RR. Todo o resto aqui é real.",
        "players": "{n} jogadores", "empty_title": "Ainda sem partidas",
        "empty_hint": "Jogue uma partida competitiva e ela aparece aqui com o mapa, seu time e o que "
                      "ela te custou.",
        "loading": "Carregando suas partidas…", "refresh": "Atualizar",
        "signed_out_cta": "Entre para ver seu histórico de partidas.",
        "when_now": "agora mesmo", "when_min": "há {n} min", "when_hour": "há {n} h",
        "when_day": "há {n} d",
    },
    "ru": {
        "title": "История матчей",
        "match_detail": "Подробности матча",
        "rounds": "Раунды",
        "team": "Команда {n}",
        "veto": "Бан карт",
        "auto_ban": "(авто)",
        "report": "Пожаловаться",
        "no_scoreboard": "Режим игры не сообщил статистику по игрокам для этого матча.",
        "scoreboard": "Таблица счёта",
        "col_k": "У",
        "col_d": "С",
        "col_kd": "У/С",
        "col_tk": "УС",
        "col_player": "Игрок",
        "stat_none": "-",
        "rounds_played": "{n} раундов",
        "not_reported": "Режим игры ни разу не сообщил об этом игроке.",
        "kills_note": "Число убийств отражает чистый счёт самой игры: убийство союзника вычитает одно. УС считается отдельно, по ленте убийств.",
        "combat_details": "Бой", "combat_complete": "Урон учтён полностью",
        "combat_partial": "Неполные данные", "combat_unavailable": "Недоступно",
        "combat_partial_note": "Часть боевых данных не была зафиксирована.",
        "combat_unavailable_note": "Боевые данные этого игрока недоступны.",
        "col_damage": "Урон врагам", "col_adr": "Урон/раунд", "col_assists": "Помощь",
        "col_headshots": "Зафиксированные попадания в голову", "col_accuracy": "Точность",
        "weapons": "Источники урона", "col_weapon": "Источник", "col_friendly_damage": "Урон союзникам",
        "col_damage_taken": "Полученный урон",
        "col_shots": "Выстрелы", "col_hits": "Попадания",
        "kda_label": "У/С",
        "subtitle_recorded": "матчей: {n}", "subtitle_played": "сыграно: {n}",
        "subtitle_cancelled": "отменено: {n}", "subtitle_record": "{w}П · {l}П",
        "filter_all": "Все", "filter_played": "Сыграны", "filter_cancelled": "Отменены",
        "res_win": "Победа", "res_loss": "Поражение", "res_played": "Сыграно",
        "res_cancelled": "Отменён", "res_fault": "Ваша вина",
        "reason_no_show": "Не явился", "reason_declined": "Отклонён",
        "reason_abandoned": "Покинул лобби", "side_attack": "Атака",
        "side_defend": "Защита", "host_tag": "Хост", "preview_tag": "Превью",
        "no_map": "Нет карты", "col_result": "Итог", "col_map": "Карта",
        "col_score": "Счёт", "col_rr": "RR", "col_when": "Когда",
        "score_pending": "нет счёта", "rr_placement": "Калибровка",
        "pending_note": "Некоторые из этих матчей так и не сообщили результат, поэтому у них "
                        "нет ни счёта, ни RR. Всё остальное здесь настоящее.",
        "players": "игроков: {n}", "empty_title": "Пока нет матчей",
        "empty_hint": "Сыграйте рейтинговый матч, и он появится здесь: карта, ваша команда и "
                      "чего он вам стоил.",
        "loading": "Загрузка ваших матчей…", "refresh": "Обновить",
        "signed_out_cta": "Войдите, чтобы увидеть историю матчей.",
        "when_now": "только что", "when_min": "{n} мин назад", "when_hour": "{n} ч назад",
        "when_day": "{n} дн назад",
    },
    "zh": {
        "title": "对局历史",
        "match_detail": "比赛详情",
        "rounds": "回合",
        "team": "队伍 {n}",
        "veto": "地图禁用",
        "auto_ban": "（自动）",
        "report": "举报",
        "no_scoreboard": "该游戏模式未上报本场对局的玩家数据。",
        "scoreboard": "计分板",
        "col_k": "击杀",
        "col_d": "死亡",
        "col_kd": "K/D",
        "col_tk": "误杀",
        "col_player": "玩家",
        "stat_none": "-",
        "rounds_played": "{n} 回合",
        "not_reported": "该游戏模式从未上报这名玩家。",
        "kills_note": "击杀数是游戏自身的净计数，误杀队友会扣除一次。误杀数另行按击杀记录统计。",
        "combat_details": "战斗数据", "combat_complete": "伤害数据完整",
        "combat_partial": "部分", "combat_unavailable": "不可用",
        "combat_partial_note": "部分战斗数据未被记录。",
        "combat_unavailable_note": "没有这名玩家的战斗数据。",
        "col_damage": "敌方伤害", "col_adr": "回合均伤", "col_assists": "助攻",
        "col_headshots": "已观测爆头", "col_accuracy": "命中率",
        "weapons": "伤害来源", "col_weapon": "来源", "col_friendly_damage": "友军伤害",
        "col_damage_taken": "承受伤害",
        "col_shots": "射击", "col_hits": "命中",
        "kda_label": "K/D",
        "subtitle_recorded": "{n} 场对局", "subtitle_played": "已进行 {n} 场",
        "subtitle_cancelled": "已取消 {n} 场", "subtitle_record": "{w}胜 · {l}负",
        "filter_all": "全部", "filter_played": "已进行", "filter_cancelled": "已取消",
        "res_win": "胜利", "res_loss": "失败", "res_played": "已进行",
        "res_cancelled": "已取消", "res_fault": "你的责任",
        "reason_no_show": "未出现", "reason_declined": "已拒绝",
        "reason_abandoned": "离开大厅", "side_attack": "进攻",
        "side_defend": "防守", "host_tag": "房主", "preview_tag": "预览",
        "no_map": "无地图", "col_result": "结果", "col_map": "地图",
        "col_score": "比分", "col_rr": "RR", "col_when": "时间",
        "score_pending": "无比分", "rr_placement": "定级赛",
        "pending_note": "其中部分对局从未上报结果，因此没有比分和 RR。此处其他信息均为真实数据。",
        "players": "{n} 名玩家", "empty_title": "还没有对局",
        "empty_hint": "进行一场竞技对局后即会在此显示地图、你的队伍以及它对你的影响。",
        "loading": "正在加载你的对局…", "refresh": "刷新",
        "signed_out_cta": "登录后可查看你的对局历史。",
        "when_now": "刚刚", "when_min": "{n} 分钟前", "when_hour": "{n} 小时前",
        "when_day": "{n} 天前",
    },
}


def strings_for(lang: str) -> dict:
    """This screen's active-language strings, English-filled so JS can look up any key."""
    merged = dict(_EN)
    merged.update(_TRANSLATIONS.get(lang, {}))
    merged.update(player_damage_strings(lang))
    merged.update(round_strings(lang))
    merged["res_voided"] = i18n.STRINGS.get(lang, i18n.STRINGS["en"])["comp_result_void"]
    return merged


# ---------------------------------------------------------------- row serialisation
def _result(row: dict) -> str:
    """The row's display category, mirroring competitive.profile_stats' FORM_* buckets exactly so
    the badge, the colour and the arithmetic never disagree. Honest with nulls: a played match whose
    scoreboard has not been reported yet is "played", not a fabricated win or loss."""
    if row.get("voided") or row.get("outcome") == "voided":
        return "voided"
    cancelled = (row.get("outcome") or "") == "cancelled"
    if cancelled:
        return "fault" if row.get("blamed") else "cancelled"
    won = row.get("won")
    if won is True:
        return "win"
    if won is False:
        return "loss"
    return "played"


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _number_or_none(value):
    """A finite JSON number, preserving real zero and rejecting booleans/junk."""
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
    """Trim the optional public combat summary without inventing missing evidence."""
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
def _row(row: dict) -> dict:
    """One history row, trimmed to what the list draws. Fields the backend leaves null (``won``,
    ``score``, ``delta`` until a scoreboard exists) stay null so JS can show an honest placeholder
    rather than a 0 that would read as a real result."""
    side = row.get("side") or ""
    score = row.get("score")
    return {
        "id": str(row.get("id") or ""),
        "ended": _int_or_none(row.get("ended")) or 0,
        "map": str(row.get("map") or ""),
        "outcome": str(row.get("outcome") or ""),
        "reason": str(row.get("reason") or ""),
        "blamed": bool(row.get("blamed")),
        "team": _int_or_none(row.get("team")) or 0,
        "side": side if side in ("attack", "defend") else "",
        "host": bool(row.get("host")),
        "players": _int_or_none(row.get("players")) or 0,
        "won": row.get("won") if isinstance(row.get("won"), bool) else None,
        "score": list(score) if isinstance(score, (list, tuple)) else None,
        "delta": _int_or_none(row.get("delta")),
        # THE RR THE MATCH MOVED - the RR column's number. `delta` above is an arrow count, and
        # printing it as RR is what made every match read "+1 RR" (2026-09-16). Null on rows the
        # service wrote before it sent this, and on matches that never settled.
        "rr_delta": _int_or_none(row.get("rr_delta")),
        # A match played while placing, which moves no RR by design.
        "placement": bool(row.get("placement")),
        "elo": _int_or_none(row.get("elo")),
        # THIS PLAYER'S OWN K/D. Null until the gamemode has reported a row for them, which is a
        # different thing from 0 - see _scoreboard.
        "kills": _int_or_none(row.get("kills")),
        "deaths": _int_or_none(row.get("deaths")),
        "team_kills": _int_or_none(row.get("team_kills")),
        "result": _result(row),
        "preview": bool(row.get("preview")),
    }


def _scoreboard(rec, players):
    """The per-player board, joined onto the roster the record already carries.

    The server ships `scoreboard` as a list of stat rows keyed by steam id (live.cjs
    ``scoreboardOf``); the names, teams and "did they walk out" live on ``players``. Joining them
    here rather than in JS keeps one shape for both UIs and keeps the honest nulls intact: a field
    the gamemode never reported stays None, because a 0 in a kills column reads as "they went 0-0"
    rather than "we were never told".

    Returns [] when the record has no board at all — a match that ended before the gamemode said
    anything — which is what ``has_scoreboard`` is False for.
    """
    rows = rec.get("scoreboard")
    if not isinstance(rows, list) or not rows:
        return []
    by_id = {}
    for p in players:
        by_id[p["steam_id"]] = p
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("steam_id") or "")
        who = by_id.get(sid) or {}
        kills = _int_or_none(r.get("kills"))
        deaths = _int_or_none(r.get("deaths"))
        out.append({
            "steam_id": sid,
            "name": who.get("name") or sid,
            "team": int(r.get("team") or who.get("team") or 0),
            "is_me": bool(who.get("is_me")),
            "left": bool(who.get("left")),
            # False when the stat sweep never reached them. The row is still drawn — the board has
            # to match the two teams — but every number on it is a dash.
            "reported": bool(r.get("reported")),
            "kills": kills,
            "deaths": deaths,
            "kd": kd_ratio(kills, deaths),
            "team_kills": _int_or_none(r.get("team_kills")),
            "rounds_won": _int_or_none(r.get("rounds_won")),
            "clutches": _int_or_none(r.get("clutches")),
            "ping": _int_or_none(r.get("ping")),
            "late_join": bool(r.get("late_join")),
            "combat": _combat(r.get("combat") if isinstance(r.get("combat"), dict)
                               else who.get("combat"), by_id),
        })
    # Best first, and a player with no numbers sinks to the bottom rather than tying at zero.
    out.sort(key=lambda r: (r["kills"] is None, -(r["kills"] or 0), r["deaths"] or 0))
    return out


def _summary(rows) -> dict:
    """The header's honest counts, from the same pure function the Tk profile uses."""
    stats = profile_stats(rows)
    return {
        "recorded": stats["recorded"],
        "played": stats["played"],
        "cancelled": stats["cancelled"],
        "wins": stats["wins"],
        "losses": stats["losses"],
        "undecided": stats["undecided"],
        "win_rate": stats["win_rate"],
    }


# ---------------------------------------------------------------- snapshot slice
@register_snapshot("history")
def snapshot(session, panel) -> dict:
    """The read-only match record, serialised for the list.

    ``asked`` distinguishes "never fetched / cannot reach the service" (session.history is None)
    from "the server answered, and the honest answer may be an empty list" — the JS shows a
    different thing for each. ``seq`` lets JS tell one answer from the next even when the rows are
    identical (session.history_seq), and ``error`` carries the session's already-localised line."""
    rows = getattr(session, "history", None)
    asked = rows is not None
    serialised = [_row(r) for r in rows if isinstance(r, dict)] if asked else []
    return {"history": {
        "asked": asked,
        "loading": bool(getattr(session, "history_loading", False)),
        "error": getattr(session, "history_error", "") or "",
        "seq": int(getattr(session, "history_seq", 0) or 0),
        "rows": serialised,
        "summary": _summary(rows if asked else []),
        "strings": strings_for(i18n.get_language()),
        # THE OPEN MATCH. "" when none is. The detail is fetched per match rather than shipped
        # with every row: the record carries the whole roster, the veto and the round timeline,
        # and one row in fifty gets opened.
        "open_id": str(getattr(session, "match_detail_id", "") or ""),
        "open_loading": bool(getattr(session, "match_detail_loading", False)),
        "open_error": str(getattr(session, "match_detail_error", "") or ""),
        "open": _detail(getattr(session, "match_detail", None),
                        str((getattr(session, "me", None) or {}).get("steam_id") or "")),
    }}


def _detail(rec, my_id: str):
    """One archived match, shaped for the detail view. None when nothing is open or loaded.

    WHAT IS HERE IS WHAT IS REAL. The record carries teams, sides, the veto, how the score got to
    where it did and - since the board landed - what each player did in it. A match whose gamemode
    reported nothing still has no board, and says so, rather than showing a column of zeroes that
    looks like everyone went 0-0.
    """
    if not isinstance(rec, dict):
        return None
    teams = rec.get("teams") or {}
    sides = rec.get("sides") or {}

    def side_of(n):
        return str(sides.get(str(n)) or sides.get(n) or "")

    players = []
    for p in (rec.get("players") or []):
        sid = str(p.get("steam_id") or "")
        players.append({
            "steam_id": sid,
            "name": p.get("persona") or sid,
            "team": int(p.get("team") or 0),
            "is_me": bool(my_id) and sid == my_id,
            "left": bool(p.get("left")),
            "connected": bool(p.get("connected")),
            # Only present when a penalty was applied; None means "nothing happened to them".
            "elo": p.get("elo"),
            "combat": _combat(p.get("combat")),
        })

    detail_rounds = round_details(rec, players, _scoreboard)
    rounds = [{"n": r["n"], "won": r["won"], "score": r["score"]} for r in detail_rounds]

    board = _scoreboard(rec, players)
    return {
        "id": str(rec.get("id") or ""),
        "map": rec.get("map") or "",
        "outcome": rec.get("outcome") or "",
        "reason": rec.get("reason") or "",
        "created": rec.get("created") or 0,
        "ended": rec.get("ended") or 0,
        "score": rec.get("score") or None,
        "winner": rec.get("winner"),
        "teams": {"1": [str(x) for x in (teams.get("1") or teams.get(1) or [])],
                  "2": [str(x) for x in (teams.get("2") or teams.get(2) or [])]},
        "sides": {"1": side_of(1), "2": side_of(2)},
        "bans": [{"team": int(b.get("team") or 0), "map": str(b.get("map") or ""),
                  "auto": bool(b.get("auto"))} for b in (rec.get("bans") or [])],
        "rounds": rounds,
        "round_details": detail_rounds,
        "players": players,
        # The board, and whether there is one at all. A match the gamemode never reported on gets
        # the honest line instead of an empty table.
        "scoreboard": board,
        "has_scoreboard": bool(board),
        # The match's own round count, which is the only honest denominator on the board. None
        # when the gamemode never said.
        "rounds_played": _int_or_none(rec.get("rounds_played")),
    }


# ---------------------------------------------------------------- bridge verbs
# 1:1 passthroughs to LiveSession.load_history, run on the UI thread via panel.post — exactly the
# shape competitive.py's verbs use. The JS calls load_history lazily on first view (cache-friendly:
# LiveSession keeps the answer so flicking between screens costs nothing) and refresh_history for
# the Refresh button (force=True re-asks the server).
register_verbs("history", {
    "load_history":    lambda panel: panel.post(lambda: panel.session.load_history(force=False)),
    "refresh_history": lambda panel: panel.post(lambda: panel.session.load_history(force=True)),
})


register_verbs("history_detail", {
    "open_match":  lambda panel, mid="": panel.post(lambda: panel.session.open_match(str(mid or ""))),
    "close_match": lambda panel: panel.post(panel.session.close_match),
})
