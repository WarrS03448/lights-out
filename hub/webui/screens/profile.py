"""Profile screen — the Python half: its snapshot slice, its strings and its one verb.

OWNED BY THE PROFILE SCREEN. This is the only Python screen module the profile agent edits. It
contributes the ``profile`` slice of the state snapshot and a single ``refresh_profile`` verb (a
1:1 passthrough to the session's history fetch). The screen renders from this slice in
``static/screens/profile.js``.

The numbers on this screen come from ONE pure function — ``hub.competitive.profile_stats(rows)`` —
fed the player's own match-history rows (``session.history``). That function is deliberate about
honesty: ``win_rate`` is ``None`` and ``top_map`` is ``""`` until a scoreboard exists, so the slice
below preserves those nulls/empties verbatim and the JS renders "Unranked" / "—" rather than
inventing a 0 % that would read as a losing record. Rank rating, peak rank and per-round stats
(K/D, ADR, headshots) have NO backend yet, so they are shipped as ``None`` and drawn as honest
"not tracked yet" placeholders — never as fabricated figures.

New user-facing strings live here (PROFILE_STRINGS, all seven languages) rather than in
hub/i18n.py: they are shipped inside this slice (``profile.strings``) and read by the JS from the
slice, so the shared i18n table is untouched. English fills any gap so every key always resolves.
"""
from . import register_snapshot, register_verbs
from ...competitive import profile_stats
from ... import i18n


# The ladder, in order, for the strip on this screen. docs/ranks.md owns it; this mirrors
# hub/webui/snapshot.py TIER_NAMES, imported lazily below to avoid an import cycle (snapshot.py
# imports this package at its top, before tier_for is defined).
#
# A FALLBACK ONLY. The strip is drawn from the ladder the SERVER sent whenever there is one
# (screens/profile.js), because a rank can be renamed on the service without a hub release; this
# list is what it falls back to offline. It used to be a different ladder altogether - Static,
# Witness, Responder, Operator, Enforcer, Nightwatch, Ghostframe, Blackout - so the strip
# contradicted the badge on the hero and the tier on the leaderboard.
TIER_ORDER = ["Rookie", "Private", "Soldier", "Veteran", "Operator",
              "Shadow", "Nightmare", "Spectre", "Reaper"]


# ---------------------------------------------------------------- i18n (screen-local, 7 languages)
# English is the source and the fallback. Keys are read in profile.js from state.profile.strings.
PROFILE_STRINGS = {
    "en": {
        "profile_overview": "Overview",
        "profile_signed_in_via": "Signed in via Steam",
        "profile_signed_out": "Sign in to see your profile.",
        "profile_unranked": "Unranked",
        "profile_rank_rating": "Rank rating",
        "profile_not_tracked": "Unavailable",
        "profile_peak": "Peak",
        "profile_rank_ladder": "Rank ladder",
        "profile_back_to_profile": "Back to profile",
        "profile_ranks_open": "View all ranks",
        "profile_ranks_intro": "All ranks, from lowest to highest. Each division has its own badge.",
        "profile_ranks_your_rank": "Your current rank",
        "profile_ranks_here": "You are here",
        "profile_ranks_no_divisions": "No divisions",
        "profile_ranks_progress": "Below {rank}, each division runs from 0–{max} RR. Reach {step} RR to move up; any excess RR carries into the next division.",
        "profile_ranks_counting": "From {rank} 1, RR keeps counting across divisions. {rank} {divisions} has no RR ceiling.",
        "profile_ranks_top_qualify": "Reach at least {rr} RR in {rank} and hold a top-{slots} position on the leaderboard to become {top}. RR keeps counting; there are no divisions.",
        "profile_ranks_top_keep": "There are up to {slots} seats. If fewer players qualify, all of them get a seat. You lose {top} if you fall below {rr} RR or outside the top {slots}. Other players can overtake you even when you have not lost RR.",
        "profile_ranks_unavailable": "Rank details are not available yet. Connect to the service to load the rank ladder.",
        "profile_ladder_note": "Three divisions per rank (I–III), 100 RR each. From Spectre 1 your RR stops resetting and simply counts up; Reaper has no divisions and belongs to the 150 highest RR on the leaderboard.",
        "profile_recent_form": "Recent form",
        "profile_recent_matches": "Recent matches",
        "profile_view_all": "View all",
        "profile_no_matches": "No ranked matches yet",
        "profile_no_matches_body": "Play a ranked Bodybomb 5v5 match to start your record.",
        "profile_stat_matches": "Matches",
        "profile_stat_played": "Played",
        "profile_stat_wins": "Wins",
        "profile_stat_losses": "Losses",
        "profile_stat_winrate": "Win rate",
        "profile_stat_topmap": "Top map",
        "profile_stat_cancelled": "Cancelled",
        "profile_ranked_only": "Ranked Bodybomb 5v5 only. Unranked community modes do not affect RR.",
        "profile_refresh": "Refresh",
        "profile_loading": "Loading…",
        "profile_form_win": "Win",
        "profile_form_loss": "Loss",
        "profile_form_played": "Played",
        "profile_form_cancelled": "Cancelled",
        "profile_form_fault": "At fault",
    },
    "de": {
        "profile_overview": "Übersicht",
        "profile_signed_in_via": "Angemeldet über Steam",
        "profile_signed_out": "Melde dich an, um dein Profil zu sehen.",
        "profile_unranked": "Ohne Rang",
        "profile_rank_rating": "Rangwertung",
        "profile_not_tracked": "Nicht verfügbar",
        "profile_peak": "Höchstwert",
        "profile_rank_ladder": "Rangleiter",
        "profile_back_to_profile": "Zurück zum Profil",
        "profile_ranks_open": "Alle Ränge ansehen",
        "profile_ranks_intro": "Alle Ränge, vom niedrigsten zum höchsten. Jede Stufe hat ihr eigenes Abzeichen.",
        "profile_ranks_your_rank": "Dein aktueller Rang",
        "profile_ranks_here": "Du bist hier",
        "profile_ranks_no_divisions": "Keine Stufen",
        "profile_ranks_progress": "Unterhalb von {rank} reicht jede Stufe von 0–{max} RR. Bei {step} RR steigst du auf; überschüssige RR werden in die nächste Stufe übernommen.",
        "profile_ranks_counting": "Ab {rank} 1 zählt RR über die Stufen hinweg weiter. {rank} {divisions} hat keine RR-Obergrenze.",
        "profile_ranks_top_qualify": "Erreiche mindestens {rr} RR in {rank} und einen Platz unter den besten {slots} der Bestenliste, um {top} zu werden. RR zählt weiter; es gibt keine Stufen.",
        "profile_ranks_top_keep": "Es gibt bis zu {slots} Plätze. Qualifizieren sich weniger Spieler, erhalten alle einen Platz. Du verlierst {top}, wenn du unter {rr} RR oder aus den besten {slots} fällst. Andere können dich überholen, auch wenn du keine RR verloren hast.",
        "profile_ranks_unavailable": "Rangdetails sind noch nicht verfügbar. Verbinde dich mit dem Dienst, um die Rangleiter zu laden.",
        "profile_ladder_note": "Drei Stufen pro Rang (I–III), je 100 RR. Ab Spectre 1 wird RR nicht mehr zurückgesetzt, sondern zählt weiter; Reaper hat keine Stufen und gehört den 150 höchsten RR der Bestenliste.",
        "profile_recent_form": "Aktuelle Form",
        "profile_recent_matches": "Letzte Spiele",
        "profile_view_all": "Alle anzeigen",
        "profile_no_matches": "Noch keine gewerteten Spiele",
        "profile_no_matches_body": "Spiele ein gewertetes Bodybomb 5v5, um deine Bilanz zu starten.",
        "profile_stat_matches": "Spiele",
        "profile_stat_played": "Gespielt",
        "profile_stat_wins": "Siege",
        "profile_stat_losses": "Niederlagen",
        "profile_stat_winrate": "Siegquote",
        "profile_stat_topmap": "Top-Karte",
        "profile_stat_cancelled": "Abgebrochen",
        "profile_ranked_only": "Nur gewertetes Bodybomb 5v5. Ungewertete Community-Modi beeinflussen die RR nicht.",
        "profile_refresh": "Aktualisieren",
        "profile_loading": "Wird geladen…",
        "profile_form_win": "Sieg",
        "profile_form_loss": "Niederlage",
        "profile_form_played": "Gespielt",
        "profile_form_cancelled": "Abgebrochen",
        "profile_form_fault": "Verschuldet",
    },
    "es": {
        "profile_overview": "Resumen",
        "profile_signed_in_via": "Sesión iniciada con Steam",
        "profile_signed_out": "Inicia sesión para ver tu perfil.",
        "profile_unranked": "Sin rango",
        "profile_rank_rating": "Puntos de rango",
        "profile_not_tracked": "No disponible",
        "profile_peak": "Máximo",
        "profile_rank_ladder": "Escala de rangos",
        "profile_back_to_profile": "Volver al perfil",
        "profile_ranks_open": "Ver todos los rangos",
        "profile_ranks_intro": "Todos los rangos, de menor a mayor. Cada división tiene su propia insignia.",
        "profile_ranks_your_rank": "Tu rango actual",
        "profile_ranks_here": "Estás aquí",
        "profile_ranks_no_divisions": "Sin divisiones",
        "profile_ranks_progress": "Por debajo de {rank}, cada división va de 0 a {max} RR. Al llegar a {step} RR, subes de división; el RR sobrante pasa a la siguiente división.",
        "profile_ranks_counting": "Desde {rank} 1, el RR sigue acumulándose entre divisiones. {rank} {divisions} no tiene límite de RR.",
        "profile_ranks_top_qualify": "Alcanza al menos {rr} RR en {rank} y mantente entre los primeros {slots} de la clasificación para ser {top}. El RR sigue acumulándose; no hay divisiones.",
        "profile_ranks_top_keep": "Hay hasta {slots} plazas. Si se clasifican menos jugadores, todos obtienen una plaza. Pierdes {top} si bajas de {rr} RR o sales de los primeros {slots}. Otros jugadores pueden adelantarte aunque no hayas perdido RR.",
        "profile_ranks_unavailable": "Los detalles de los rangos aún no están disponibles. Conéctate al servicio para cargar la escala de rangos.",
        "profile_ladder_note": "Tres divisiones por rango (I–III), 100 RR cada una. Desde Spectre 1 el RR deja de reiniciarse y sigue sumando; Reaper no tiene divisiones y es de los 150 RR más altos de la clasificación.",
        "profile_recent_form": "Forma reciente",
        "profile_recent_matches": "Partidas recientes",
        "profile_view_all": "Ver todo",
        "profile_no_matches": "Aún no hay partidas clasificatorias",
        "profile_no_matches_body": "Juega un Bodybomb 5v5 clasificatorio para empezar tu historial.",
        "profile_stat_matches": "Partidas",
        "profile_stat_played": "Jugadas",
        "profile_stat_wins": "Victorias",
        "profile_stat_losses": "Derrotas",
        "profile_stat_winrate": "% de victorias",
        "profile_stat_topmap": "Mapa top",
        "profile_stat_cancelled": "Canceladas",
        "profile_ranked_only": "Solo Bodybomb 5v5 clasificatorio. Los modos comunitarios sin clasificar no afectan al RR.",
        "profile_refresh": "Actualizar",
        "profile_loading": "Cargando…",
        "profile_form_win": "Victoria",
        "profile_form_loss": "Derrota",
        "profile_form_played": "Jugada",
        "profile_form_cancelled": "Cancelada",
        "profile_form_fault": "Culpa propia",
    },
    "fr": {
        "profile_overview": "Vue d’ensemble",
        "profile_signed_in_via": "Connecté via Steam",
        "profile_signed_out": "Connecte-toi pour voir ton profil.",
        "profile_unranked": "Sans rang",
        "profile_rank_rating": "Points de rang",
        "profile_not_tracked": "Indisponible",
        "profile_peak": "Record",
        "profile_rank_ladder": "Échelle des rangs",
        "profile_back_to_profile": "Retour au profil",
        "profile_ranks_open": "Voir tous les rangs",
        "profile_ranks_intro": "Tous les rangs, du plus bas au plus haut. Chaque division a son propre insigne.",
        "profile_ranks_your_rank": "Ton rang actuel",
        "profile_ranks_here": "Tu es ici",
        "profile_ranks_no_divisions": "Sans divisions",
        "profile_ranks_progress": "En dessous de {rank}, chaque division va de 0 à {max} RR. À {step} RR, tu montes d’une division ; le RR excédentaire est conservé dans la suivante.",
        "profile_ranks_counting": "À partir de {rank} 1, le RR continue de s’accumuler entre les divisions. {rank} {divisions} n’a pas de plafond de RR.",
        "profile_ranks_top_qualify": "Atteins au moins {rr} RR en {rank} et reste parmi les {slots} premiers du classement pour devenir {top}. Le RR continue de s’accumuler ; il n’y a pas de divisions.",
        "profile_ranks_top_keep": "Il y a jusqu’à {slots} places. Si moins de joueurs remplissent les critères, chacun obtient une place. Tu perds {top} si tu passes sous {rr} RR ou sors des {slots} premiers. D’autres joueurs peuvent te dépasser même si tu n’as pas perdu de RR.",
        "profile_ranks_unavailable": "Les détails des rangs ne sont pas encore disponibles. Connecte-toi au service pour charger l’échelle des rangs.",
        "profile_ladder_note": "Trois divisions par rang (I–III), 100 RR chacune. À partir de Spectre 1, le RR ne se réinitialise plus et continue de monter ; Reaper n'a pas de divisions et revient aux 150 meilleurs RR du classement.",
        "profile_recent_form": "Forme récente",
        "profile_recent_matches": "Parties récentes",
        "profile_view_all": "Tout voir",
        "profile_no_matches": "Aucune partie classée",
        "profile_no_matches_body": "Joue un Bodybomb 5v5 classé pour démarrer ton historique.",
        "profile_stat_matches": "Parties",
        "profile_stat_played": "Jouées",
        "profile_stat_wins": "Victoires",
        "profile_stat_losses": "Défaites",
        "profile_stat_winrate": "% de victoires",
        "profile_stat_topmap": "Carte favorite",
        "profile_stat_cancelled": "Annulées",
        "profile_ranked_only": "Bodybomb 5v5 classé uniquement. Les modes communautaires non classés n'affectent pas le RR.",
        "profile_refresh": "Actualiser",
        "profile_loading": "Chargement…",
        "profile_form_win": "Victoire",
        "profile_form_loss": "Défaite",
        "profile_form_played": "Jouée",
        "profile_form_cancelled": "Annulée",
        "profile_form_fault": "Faute",
    },
    "pt": {
        "profile_overview": "Visão geral",
        "profile_signed_in_via": "Sessão iniciada via Steam",
        "profile_signed_out": "Inicie sessão para ver o seu perfil.",
        "profile_unranked": "Sem classificação",
        "profile_rank_rating": "Pontos de rank",
        "profile_not_tracked": "Indisponível",
        "profile_peak": "Máximo",
        "profile_rank_ladder": "Escala de ranks",
        "profile_back_to_profile": "Voltar ao perfil",
        "profile_ranks_open": "Ver todos os ranks",
        "profile_ranks_intro": "Todos os ranks, do mais baixo ao mais alto. Cada divisão tem o seu próprio emblema.",
        "profile_ranks_your_rank": "O seu rank atual",
        "profile_ranks_here": "Está aqui",
        "profile_ranks_no_divisions": "Sem divisões",
        "profile_ranks_progress": "Abaixo de {rank}, cada divisão vai de 0 a {max} RR. Aos {step} RR, sobe de divisão; o RR excedente passa para a divisão seguinte.",
        "profile_ranks_counting": "A partir de {rank} 1, o RR continua a acumular entre divisões. {rank} {divisions} não tem limite de RR.",
        "profile_ranks_top_qualify": "Alcance pelo menos {rr} RR em {rank} e mantenha-se entre os primeiros {slots} da tabela para chegar a {top}. O RR continua a acumular; não há divisões.",
        "profile_ranks_top_keep": "Há até {slots} lugares. Se menos jogadores cumprirem os requisitos, todos recebem um lugar. Perde {top} se ficar abaixo de {rr} RR ou fora dos primeiros {slots}. Outros jogadores podem ultrapassá-lo mesmo sem ter perdido RR.",
        "profile_ranks_unavailable": "Os detalhes dos ranks ainda não estão disponíveis. Ligue-se ao serviço para carregar a escala de ranks.",
        "profile_ladder_note": "Três divisões por rank (I–III), 100 RR cada. A partir de Spectre 1 o RR deixa de reiniciar e continua a contar; Reaper não tem divisões e pertence aos 150 RR mais altos da tabela.",
        "profile_recent_form": "Forma recente",
        "profile_recent_matches": "Partidas recentes",
        "profile_view_all": "Ver tudo",
        "profile_no_matches": "Ainda sem partidas ranqueadas",
        "profile_no_matches_body": "Jogue um Bodybomb 5v5 ranqueado para começar o seu histórico.",
        "profile_stat_matches": "Partidas",
        "profile_stat_played": "Jogadas",
        "profile_stat_wins": "Vitórias",
        "profile_stat_losses": "Derrotas",
        "profile_stat_winrate": "% de vitórias",
        "profile_stat_topmap": "Mapa favorito",
        "profile_stat_cancelled": "Canceladas",
        "profile_ranked_only": "Apenas Bodybomb 5v5 ranqueado. Modos comunitários não ranqueados não afetam o RR.",
        "profile_refresh": "Atualizar",
        "profile_loading": "A carregar…",
        "profile_form_win": "Vitória",
        "profile_form_loss": "Derrota",
        "profile_form_played": "Jogada",
        "profile_form_cancelled": "Cancelada",
        "profile_form_fault": "Culpa própria",
    },
    "ru": {
        "profile_overview": "Обзор",
        "profile_signed_in_via": "Вход через Steam",
        "profile_signed_out": "Войдите, чтобы увидеть свой профиль.",
        "profile_unranked": "Без ранга",
        "profile_rank_rating": "Рейтинг ранга",
        "profile_not_tracked": "Недоступно",
        "profile_peak": "Пик",
        "profile_rank_ladder": "Лестница рангов",
        "profile_back_to_profile": "Вернуться в профиль",
        "profile_ranks_open": "Все ранги",
        "profile_ranks_intro": "Все ранги, от низшего к высшему. У каждого подранга свой значок.",
        "profile_ranks_your_rank": "Ваш текущий ранг",
        "profile_ranks_here": "Вы здесь",
        "profile_ranks_no_divisions": "Без подрангов",
        "profile_ranks_progress": "Ниже {rank} каждый подранг охватывает 0–{max} RR. При {step} RR вы повышаетесь; лишние RR переносятся в следующий подранг.",
        "profile_ranks_counting": "Начиная с {rank} 1, RR накапливается без сброса при переходе между подрангами. У {rank} {divisions} нет верхнего предела RR.",
        "profile_ranks_top_qualify": "Наберите не менее {rr} RR в {rank} и войдите в число первых {slots} игроков таблицы лидеров, чтобы получить {top}. RR продолжает накапливаться; подрангов нет.",
        "profile_ranks_top_keep": "Доступно до {slots} мест. Если требованиям соответствует меньше игроков, место получает каждый. Вы теряете {top}, если RR опустится ниже {rr} или вы выйдете из первых {slots}. Другие игроки могут обойти вас, даже если вы не потеряли RR.",
        "profile_ranks_unavailable": "Сведения о рангах пока недоступны. Подключитесь к сервису, чтобы загрузить лестницу рангов.",
        "profile_ladder_note": "Три подранга в ранге (I–III), по 100 RR каждый. С Spectre 1 RR больше не обнуляется, а растёт дальше; у Reaper нет подрангов, его получают 150 игроков с наибольшим RR в таблице лидеров.",
        "profile_recent_form": "Недавняя форма",
        "profile_recent_matches": "Недавние матчи",
        "profile_view_all": "Показать все",
        "profile_no_matches": "Рейтинговых матчей пока нет",
        "profile_no_matches_body": "Сыграйте рейтинговый Bodybomb 5v5, чтобы начать историю.",
        "profile_stat_matches": "Матчи",
        "profile_stat_played": "Сыграно",
        "profile_stat_wins": "Победы",
        "profile_stat_losses": "Поражения",
        "profile_stat_winrate": "Процент побед",
        "profile_stat_topmap": "Топ-карта",
        "profile_stat_cancelled": "Отменено",
        "profile_ranked_only": "Только рейтинговый Bodybomb 5v5. Нерейтинговые режимы сообщества не влияют на RR.",
        "profile_refresh": "Обновить",
        "profile_loading": "Загрузка…",
        "profile_form_win": "Победа",
        "profile_form_loss": "Поражение",
        "profile_form_played": "Сыграно",
        "profile_form_cancelled": "Отменено",
        "profile_form_fault": "Ваша вина",
    },
    "zh": {
        "profile_overview": "概览",
        "profile_signed_in_via": "已通过 Steam 登录",
        "profile_signed_out": "登录以查看你的个人资料。",
        "profile_unranked": "无排名",
        "profile_rank_rating": "段位积分",
        "profile_not_tracked": "暂无数据",
        "profile_peak": "最高",
        "profile_rank_ladder": "段位阶梯",
        "profile_back_to_profile": "返回个人资料",
        "profile_ranks_open": "查看所有段位",
        "profile_ranks_intro": "所有段位按从低到高排列。每个小段都有自己的徽章。",
        "profile_ranks_your_rank": "你当前的段位",
        "profile_ranks_here": "你在这里",
        "profile_ranks_no_divisions": "无小段",
        "profile_ranks_progress": "在 {rank} 以下，每个小段的范围为 0–{max} RR。达到 {step} RR 后晋升，多出的 RR 会带入下一小段。",
        "profile_ranks_counting": "从 {rank} 1 起，RR 在小段之间持续累加。{rank} {divisions} 的 RR 没有上限。",
        "profile_ranks_top_qualify": "在 {rank} 达到至少 {rr} RR，并保持排行榜前 {slots} 名，即可获得 {top}。RR 持续累加，没有小段。",
        "profile_ranks_top_keep": "最多有 {slots} 个席位。如果符合条件的玩家较少，每人都能获得席位。低于 {rr} RR 或跌出前 {slots} 名就会失去 {top}。即使你没有损失 RR，其他玩家也可能超过你。",
        "profile_ranks_unavailable": "段位详情暂不可用。连接服务以加载段位阶梯。",
        "profile_ladder_note": "每个段位三个小段（I–III），各 100 RR。从 Spectre 1 起 RR 不再清零，而是持续累加；Reaper 没有小段，属于排行榜 RR 最高的 150 名玩家。",
        "profile_recent_form": "近期战绩",
        "profile_recent_matches": "最近对局",
        "profile_view_all": "查看全部",
        "profile_no_matches": "暂无排位对局",
        "profile_no_matches_body": "打一场排位 Bodybomb 5v5 来开始你的战绩。",
        "profile_stat_matches": "对局",
        "profile_stat_played": "已进行",
        "profile_stat_wins": "胜场",
        "profile_stat_losses": "负场",
        "profile_stat_winrate": "胜率",
        "profile_stat_topmap": "常用地图",
        "profile_stat_cancelled": "已取消",
        "profile_ranked_only": "仅排位 Bodybomb 5v5。非排位社区模式不影响 RR。",
        "profile_refresh": "刷新",
        "profile_loading": "加载中…",
        "profile_form_win": "胜利",
        "profile_form_loss": "失败",
        "profile_form_played": "已进行",
        "profile_form_cancelled": "已取消",
        "profile_form_fault": "自身责任",
    },
}


def _strings_for(lang: str) -> dict:
    """The active language's screen strings, English-filled so JS can look up any key by name."""
    merged = dict(PROFILE_STRINGS["en"])
    merged.update(PROFILE_STRINGS.get(lang, {}))
    return merged


def _tier_for(level):
    """The player's named tier for an integer level (display only). Lazy import of the shared
    mapping avoids the snapshot.py <-> screens package import cycle."""
    try:
        from ..snapshot import tier_for
    except Exception:            # noqa: BLE001 — never let a display label crash the snapshot
        return ""
    return tier_for(level)


def _ladder(current_tier: str) -> list:
    """The eight named tiers in order, with the player's current one flagged for the strip."""
    return [{"name": name, "current": bool(current_tier) and name == current_tier}
            for name in TIER_ORDER]


def _recent(rows, limit=5) -> list:
    """A short, honest recent-matches list straight from the player's own history rows.

    Nothing is invented: ``won`` stays True/False/None exactly as the row carries it, so the JS
    draws a win, a loss, or a neutral "played" mark accordingly. ``elo`` may be null (the preview
    and unscored matches carry none)."""
    out = []
    for row in (rows or [])[:limit]:
        if not isinstance(row, dict):
            continue
        cancelled = (row.get("outcome") or "") == "cancelled"
        out.append({
            "id": str(row.get("id") or ""),
            "map": str(row.get("map") or ""),
            "won": row.get("won"),                    # True / False / None — never coerced
            "cancelled": cancelled,
            "blamed": bool(row.get("blamed")),
            "elo": row.get("elo"),                    # may be null
            "ended": row.get("ended"),                # ms epoch or null; JS formats it
        })
    return out


def profile_snapshot(session, panel) -> dict:
    """The profile screen's slice: honest stats + identity + the ladder + this screen's strings.

    ``session.history`` is the player's own match rows (newest first), or ``None`` when we have
    never successfully asked the server. ``profile_stats`` treats ``None`` as "no data" and returns
    zeros / ``None`` / ``""`` accordingly, which we pass through verbatim — the screen shows those
    as placeholders, never as invented numbers."""
    lang = i18n.get_language()
    me = getattr(session, "me", None) or {}
    signed_in = bool(getattr(session, "me", None))
    history = getattr(session, "history", None)

    stats = profile_stats(history)
    # ``maps`` comes back as a list of (name, count) tuples; normalise to JSON-friendly objects so
    # the wire shape is explicit (json.dumps would otherwise emit two-element arrays).
    stats["maps"] = [{"map": name, "count": count} for name, count in stats.get("maps", [])]

    tier = _tier_for(me.get("level")) if signed_in else ""

    return {
        "profile": {
            "signed_in": signed_in,
            # identity — real, straight from the signed-in account
            "persona": me.get("name") or "",
            "avatar": me.get("avatar") or "",
            "steam_id": me.get("steam_id") or "",
            "level": me.get("level"),               # may be null
            "tier": tier or None,                   # named tier label, or null when signed out
            # rank rating / peak / region have NO backend yet — honest nulls, drawn as "not tracked"
            "rr": None,
            "rr_max": None,
            "peak_tier": None,
            "region": None,
            "ladder": _ladder(tier),
            # whether we actually have the history yet (None = never fetched, [] = fetched, empty)
            "history_loaded": history is not None,
            "history_loading": bool(getattr(session, "history_loading", False)),
            "history_error": getattr(session, "history_error", "") or "",
            "stats": stats,                         # profile_stats() output, nulls/empties intact
            "recent": _recent(history),
            "strings": _strings_for(lang),
        }
    }


@register_snapshot("profile")
def snapshot(session, panel) -> dict:
    """Merge the profile slice into the top-level snapshot (namespaced under ``profile``)."""
    return profile_snapshot(session, panel)


# ---------------------------------------------------------------- bridge verbs
# One action: (re)fetch the player's match history so the stats reflect the latest matches. It is a
# 1:1 passthrough to the session's own cached fetch (force=True re-asks), run on the UI thread via
# panel.post exactly like the competitive verbs. Nothing here is authoritative — the next onState
# push re-syncs the screen.
register_verbs("profile", {
    "refresh_profile": lambda panel: panel.post(lambda: panel.session.load_history(force=True)),
})
