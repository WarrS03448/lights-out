"""Leaderboard screen — the Python half: its snapshot slice (and, one day, its bridge verbs).

OWNED BY THE LEADERBOARD SCREEN. This is the only Python screen module the leaderboard agent
edits. It contributes the ``leaderboard`` slice of the state snapshot that the JS screen
(static/screens/leaderboard.js) renders the Global/Friends tabs and the ranked table from.

  IMPORTANT — there is NO leaderboard / ranking service today. ``server/live.cjs`` exposes only
  the queue / match / party / live routes (``/api/queue/*``, ``/api/match/*``, ``/api/party/*``,
  ``/api/live``); there is no ``/api/leaderboard`` or ranking endpoint, and ``hub/competitive.py``
  says so outright ("there is no ranking service", ~line 3427 — the rank/Elo is invented locally
  in ``adopt_account``). So this screen does NOT fabricate standings: it ships an honest empty
  slice (``available: False``, ``rows: []``) and the JS renders a "not live yet" empty state.
  When a leaderboard endpoint is added server-side, wire it into ``_board_from_backend`` below and
  ``available`` flips to True with real rows — the JS already renders them.

New strings live HERE, per-language, and ride inside this screen's own snapshot slice
(``leaderboard.strings``); the JS reads them by key. i18n.py is NOT edited — the shared STRINGS
dict stays the single source of truth for the chrome, and this screen carries its own vocabulary
the same way a later plugin screen would (plan: i18n — ship the active language, no second store).
"""
from . import register_snapshot, register_verbs
from ... import i18n


# ---------------------------------------------------------------- per-language strings
# One dict per shipped language (en de es fr pt ru zh — hub/i18n.CODES). English is the fallback
# and is filled in for any key a translation is missing, so the JS can look up any key by name.
STRINGS = {
    "en": {
        "search": "Player search",
        "search_hint": "Name or Steam ID",
        "all_tiers": "All tiers",
        "all_players": "All players",
        "ranked": "Ranked",
        "status": "Status",
        "reset": "Reset",
        "sort": "Sort",
        "sort_none": "None",
        "sort_highest": "Highest",
        "sort_lowest": "Lowest",
        "sort_az": "A–Z",
        "sort_za": "Z–A",
        "results": "{shown} of {total} loaded players · Search and filters apply to these standings.",
        "no_results": "No players match your search or filters.",
        "go_to_me": "Go to me",
        "title": "Leaderboard",
        "tab_global": "Global",
        "tab_friends": "Friends",
        "col_rank": "Rank",
        "col_player": "Player",
        "col_tier": "Tier",
        "placing": "Placing",
        "col_rr": "RR",
        "col_matches": "Matches",
        "col_winrate": "Win %",
        "you_label": "you",
        "empty_title": "Standings unavailable",
        "empty_global": "Unable to load standings. Check your connection and try again.",
        "empty_friends": "Unable to load standings. Check your connection and try again.",
    },
    "de": {
        "search": "Spielersuche",
        "search_hint": "Name oder Steam-ID",
        "all_tiers": "Alle Stufen",
        "all_players": "Alle Spieler",
        "ranked": "Eingestuft",
        "status": "Status",
        "reset": "Zurücksetzen",
        "sort": "Sortieren",
        "sort_none": "Keine",
        "sort_highest": "Höchste",
        "sort_lowest": "Niedrigste",
        "sort_az": "A–Z",
        "sort_za": "Z–A",
        "results": "{shown} von {total} geladenen Spielern · Suche und Filter gelten für diese Rangliste.",
        "no_results": "Keine Spieler entsprechen deiner Suche oder deinen Filtern.",
        "go_to_me": "Zu mir",
        "title": "Bestenliste",
        "tab_global": "Global",
        "tab_friends": "Freunde",
        "col_rank": "Rang",
        "col_player": "Spieler",
        "col_tier": "Stufe",
        "placing": "Platzierung",
        "col_rr": "RR",
        "col_matches": "Spiele",
        "col_winrate": "Siegrate",
        "you_label": "du",
        "empty_title": "Rangliste nicht verfügbar",
        "empty_global": "Die Rangliste konnte nicht geladen werden. Prüfe deine Verbindung und versuche es erneut.",
        "empty_friends": "Die Rangliste konnte nicht geladen werden. Prüfe deine Verbindung und versuche es erneut.",
    },
    "es": {
        "search": "Buscar jugador",
        "search_hint": "Nombre o ID de Steam",
        "all_tiers": "Todos los niveles",
        "all_players": "Todos los jugadores",
        "ranked": "Clasificado",
        "status": "Estado",
        "reset": "Restablecer",
        "sort": "Ordenar",
        "sort_none": "Ninguno",
        "sort_highest": "Mayor",
        "sort_lowest": "Menor",
        "sort_az": "A–Z",
        "sort_za": "Z–A",
        "results": "{shown} de {total} jugadores cargados · La búsqueda y los filtros se aplican a estas posiciones.",
        "no_results": "Ningún jugador coincide con la búsqueda o los filtros.",
        "go_to_me": "Ir a mi posición",
        "title": "Clasificación",
        "tab_global": "Global",
        "tab_friends": "Amigos",
        "col_rank": "Puesto",
        "col_player": "Jugador",
        "col_tier": "Nivel",
        "placing": "Colocación",
        "col_rr": "RR",
        "col_matches": "Partidas",
        "col_winrate": "% victorias",
        "you_label": "tú",
        "empty_title": "Clasificación no disponible",
        "empty_global": "No se pudo cargar la clasificación. Comprueba la conexión e inténtalo de nuevo.",
        "empty_friends": "No se pudo cargar la clasificación. Comprueba la conexión e inténtalo de nuevo.",
    },
    "fr": {
        "search": "Rechercher un joueur",
        "search_hint": "Nom ou identifiant Steam",
        "all_tiers": "Tous les paliers",
        "all_players": "Tous les joueurs",
        "ranked": "Classé",
        "status": "Statut",
        "reset": "Réinitialiser",
        "sort": "Trier",
        "sort_none": "Aucun",
        "sort_highest": "Plus élevé",
        "sort_lowest": "Plus bas",
        "sort_az": "A–Z",
        "sort_za": "Z–A",
        "results": "{shown} sur {total} joueurs chargés · La recherche et les filtres concernent ce classement.",
        "no_results": "Aucun joueur ne correspond à votre recherche ou aux filtres.",
        "go_to_me": "Aller à ma position",
        "title": "Classement",
        "tab_global": "Mondial",
        "tab_friends": "Amis",
        "col_rank": "Rang",
        "col_player": "Joueur",
        "col_tier": "Palier",
        "placing": "Placement",
        "col_rr": "RR",
        "col_matches": "Parties",
        "col_winrate": "% victoires",
        "you_label": "vous",
        "empty_title": "Classement indisponible",
        "empty_global": "Impossible de charger le classement. Vérifie ta connexion et réessaie.",
        "empty_friends": "Impossible de charger le classement. Vérifie ta connexion et réessaie.",
    },
    "pt": {
        "search": "Buscar jogador",
        "search_hint": "Nome ou ID Steam",
        "all_tiers": "Todos os níveis",
        "all_players": "Todos os jogadores",
        "ranked": "Classificado",
        "status": "Status",
        "reset": "Redefinir",
        "sort": "Ordenar",
        "sort_none": "Nenhum",
        "sort_highest": "Maior",
        "sort_lowest": "Menor",
        "sort_az": "A–Z",
        "sort_za": "Z–A",
        "results": "{shown} de {total} jogadores carregados · A busca e os filtros se aplicam a esta classificação.",
        "no_results": "Nenhum jogador corresponde à busca ou aos filtros.",
        "go_to_me": "Ir para minha posição",
        "title": "Classificação",
        "tab_global": "Global",
        "tab_friends": "Amigos",
        "col_rank": "Posição",
        "col_player": "Jogador",
        "col_tier": "Nível",
        "placing": "Colocação",
        "col_rr": "RR",
        "col_matches": "Partidas",
        "col_winrate": "% vitórias",
        "you_label": "você",
        "empty_title": "Classificação indisponível",
        "empty_global": "Não foi possível carregar a classificação. Verifica a ligação e tenta novamente.",
        "empty_friends": "Não foi possível carregar a classificação. Verifica a ligação e tenta novamente.",
    },
    "ru": {
        "search": "Поиск игрока",
        "search_hint": "Имя или Steam ID",
        "all_tiers": "Все ранги",
        "all_players": "Все игроки",
        "ranked": "С рангом",
        "status": "Статус",
        "reset": "Сбросить",
        "sort": "Сортировка",
        "sort_none": "Нет",
        "sort_highest": "По убыванию",
        "sort_lowest": "По возрастанию",
        "sort_az": "А–Я",
        "sort_za": "Я–А",
        "results": "{shown} из {total} загруженных игроков · Поиск и фильтры применяются к этой таблице.",
        "no_results": "Нет игроков, соответствующих поиску или фильтрам.",
        "go_to_me": "К моему месту",
        "title": "Таблица лидеров",
        "tab_global": "Общий",
        "tab_friends": "Друзья",
        "col_rank": "Место",
        "col_player": "Игрок",
        "col_tier": "Ранг",
        "placing": "Квалификация",
        "col_rr": "RR",
        "col_matches": "Матчи",
        "col_winrate": "% побед",
        "you_label": "вы",
        "empty_title": "Таблица рейтинга недоступна",
        "empty_global": "Не удалось загрузить рейтинг. Проверьте соединение и повторите попытку.",
        "empty_friends": "Не удалось загрузить рейтинг. Проверьте соединение и повторите попытку.",
    },
    "zh": {
        "search": "搜索玩家",
        "search_hint": "名称或 Steam ID",
        "all_tiers": "所有段位",
        "all_players": "所有玩家",
        "ranked": "已定级",
        "status": "状态",
        "reset": "重置",
        "sort": "排序",
        "sort_none": "无",
        "sort_highest": "从高到低",
        "sort_lowest": "从低到高",
        "sort_az": "A–Z",
        "sort_za": "Z–A",
        "results": "已加载 {total} 名玩家，显示 {shown} 名 · 搜索和筛选仅适用于这些排名。",
        "no_results": "没有符合搜索或筛选条件的玩家。",
        "go_to_me": "转到我的排名",
        "title": "排行榜",
        "tab_global": "全球",
        "tab_friends": "好友",
        "col_rank": "排名",
        "col_player": "玩家",
        "col_tier": "段位",
        "placing": "定级中",
        "col_rr": "RR",
        "col_matches": "场次",
        "col_winrate": "胜率",
        "you_label": "你",
        "empty_title": "排行榜暂不可用",
        "empty_global": "无法加载排行榜。请检查网络连接后重试。",
        "empty_friends": "无法加载排行榜。请检查网络连接后重试。",
    },
}


def strings_for(lang: str) -> dict:
    """The active language's leaderboard strings, English-filled so any key resolves in JS."""
    merged = dict(STRINGS.get(i18n.DEFAULT, {}))
    merged.update(STRINGS.get(lang, {}))
    return merged


# ---------------------------------------------------------------- backend read (none yet)
def _row(entry: dict, placing_label: str) -> dict:
    """One server row in the shape the JS table renders.

    `tier` is the VISIBLE rank - "Spectre 3", or just the name for the capstone, which has no
    divisions. `rr` is progress through the division lower down, and the running count from the
    top rank up - which is what the board is actually ordered by, and why that band stops
    resetting the figure (server/progress.cjs). Both come from the server so the board can never
    disagree with the badge on the player's own hero.
    """
    name = str(entry.get("rank_name") or "")
    div = int(entry.get("division") or 0)
    if not name:
        tier = placing_label
    elif entry.get("top") or not div:
        tier = name
    else:
        tier = "%s %d" % (name, div)
    return {
        "rank": entry.get("rank"),
        "steam_id": str(entry.get("steam_id") or ""),
        "name": entry.get("persona") or str(entry.get("steam_id") or ""),
        "avatar": str(entry.get("avatar") or ""),
        "level": None,          # the numeric ladder is not what players are shown any more
        "tier": tier,
        # The badge beside the tier. Sent as the server's own three fields rather than as a
        # resolved icon index, because the INDEX is a rank's position in the ladder that arrived
        # with `hello` (docs/ranks.md) and only the page holds both halves - the board is fetched
        # over HTTP and knows nothing about the ladder. A placing player has no name here and so
        # gets no badge, which is the same answer the hero gives.
        "rank_name": name or None,
        "division": div or None,
        "top": bool(entry.get("top")),
        "rr": entry.get("rr"),
        "matches": entry.get("matches"),
        "win_rate": ("%d%%" % int(entry.get("win_rate") or 0)) if entry.get("matches") else "-",
        "is_you": bool(entry.get("is_you")),
    }


def _board_from_backend(session):
    """(rows, you) from the last fetch, or (None, None) when there is nothing to show.

    None is not the same as []: None renders the "not live yet" empty state, [] renders an empty
    TABLE. The difference matters - a board that could not be read must not claim that nobody is
    ranked.
    """
    if not getattr(session, "board_available", False):
        return None, None
    placing = strings_for(i18n.get_language()).get("placing", "Placing")
    rows = [_row(r, placing) for r in (getattr(session, "board_rows", ()) or ())]
    you = getattr(session, "board_you", None)
    return rows, (_row(you, placing) if you else None)


# ---------------------------------------------------------------- snapshot slice
@register_snapshot("leaderboard")
def snapshot(session, panel) -> dict:
    """Contribute the ``leaderboard`` slice.

    Honest by construction: with no rank service, ``available`` is False and ``rows`` is empty, so
    the JS renders a "not live yet" empty state instead of inventing standings. The tabs
    (``scopes``) and the per-language ``strings`` are always shipped so the screen renders fully."""
    lang = i18n.get_language()
    rows, you = _board_from_backend(session)
    available = rows is not None
    return {
        "leaderboard": {
            "mode": getattr(session, "board_mode", "BB5"),
            "available": bool(available),
            "rows": rows or [],
            "you": you,
            "scope": "global",
            "scopes": ["global", "friends"],
            "season": None,             # e.g. "Season 1" once a season exists server-side
            "strings": strings_for(lang),
        }
    }


register_verbs("leaderboard", {
    "leaderboard_refresh": lambda panel: panel.post(panel.session.refresh_leaderboard),
    "leaderboard_mode": lambda panel, mode: panel.post(lambda: panel.session.select_board_mode(str(mode))),
})
