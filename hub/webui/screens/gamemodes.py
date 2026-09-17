"""Gamemodes screen — the Python half: its snapshot slice and its bridge verbs.

OWNED BY THE GAMEMODES SCREEN. This is the only Python screen module the gamemodes agent edits.
It contributes the ``gamemodes`` slice of the state snapshot (the catalogue's gamemodes with their
installed / available / ranked state, versions, ruleset, maps and whether an update exists, plus
the game-ready / game-running flags) and registers the install / uninstall / update / ruleset verbs
the JS screen (static/screens/gamemodes.js) calls.

It mirrors the Tk gamemodes tab in hub/app.py: ``_installed`` / ``_entries`` (catalogue plus any
installed-but-unlisted "orphan"), the row Update button when the catalogue lists a newer version,
the "Ruleset" pop-up, and the "game running" guard enforced by ``ops.apply``. The long-running
work (``ops.apply``: download → build → atomic swap) runs on a WORKER thread exactly as
the Tk app runs it, hopping back onto the panel's single UI thread through ``panel.post`` to
mutate the screen's job state and re-emit the snapshot — never blocking the UI thread.

New screen strings live in ``GAMEMODES_STRINGS`` below (a per-language dict) and are shipped inside
this slice (``state.gamemodes.strings``); the JS reads them from the slice. i18n.py is NOT edited.
This module imports no pywebview and no Tk, so it stays testable headless.
"""
import threading

from . import register_snapshot, register_verbs
from ... import catalogue as cat
from ... import game as game_mod
from ... import ops
from ... import state as state_mod
from ... import i18n
from ...competitive import COMPETITIVE_MODE_ID


# ---------------------------------------------------------------- screen strings (own dict)
# Shipped inside the snapshot slice (state.gamemodes.strings) and read by gamemodes.js. These are
# the gamemodes-screen-specific strings; the shared tab/nav labels still come from i18n.STRINGS via
# the top-level snapshot. English is the fallback (gt() in JS falls back to the key).
GAMEMODES_STRINGS = {
    "en": {
        "heading": "Gamemodes",
        "intro": "Community modes install into your Bodycam folder. Open the ruleset for a mode's rules.",
        "section_installed": "Installed",
        "section_available": "Available",
        "ranked": "Ranked",
        "btn_install": "Install",
        "btn_uninstall": "Uninstall",
        "btn_update": "Update",
        "installed_label": "Installed",
        "ruleset": "Ruleset",
        "maps": "Maps",
        "maps_none": "Unavailable",
        "close": "Close",
        "version": "Version",
        "status": "Status",
        "status_installed": "Installed",
        "status_available": "Available",
        "yes": "Yes",
        "no": "No",
        "update_available": "Update available",
        "empty_none": "No gamemodes are listed yet.",
        "loading": "Loading gamemodes…",
        "no_game": "Set your Bodycam folder in Settings to install gamemodes.",
        "game_running": "Close Bodycam before installing or removing a gamemode.",
        "working": "Working…",
        "job_installing": "Installing {name}…",
        "job_uninstalling": "Removing {name}…",
        "job_updating": "Updating {name}…",
        "done_installed": "Installed.",
        "done_updated": "Updated.",
        "done_uninstalled": "Removed.",
        "footer": "Only ranked Bodybomb 5v5 matches affect your rank.",
    },
    "de": {
        "heading": "Spielmodi",
        "intro": "Community-Modi werden in deinen Bodycam-Ordner installiert. Öffne die Regeln eines Modus.",
        "section_installed": "Installiert",
        "section_available": "Verfügbar",
        "ranked": "Gewertet",
        "btn_install": "Installieren",
        "btn_uninstall": "Entfernen",
        "btn_update": "Aktualisieren",
        "installed_label": "Installiert",
        "ruleset": "Regeln",
        "maps": "Karten",
        "maps_none": "Nicht verfügbar",
        "close": "Schließen",
        "version": "Version",
        "status": "Status",
        "status_installed": "Installiert",
        "status_available": "Verfügbar",
        "yes": "Ja",
        "no": "Nein",
        "update_available": "Update verfügbar",
        "empty_none": "Es sind noch keine Spielmodi gelistet.",
        "loading": "Spielmodi werden geladen…",
        "no_game": "Lege deinen Bodycam-Ordner in den Einstellungen fest, um Spielmodi zu installieren.",
        "game_running": "Schließe Bodycam, bevor du einen Spielmodus installierst oder entfernst.",
        "working": "Arbeite…",
        "job_installing": "{name} wird installiert…",
        "job_uninstalling": "{name} wird entfernt…",
        "job_updating": "{name} wird aktualisiert…",
        "done_installed": "Installiert.",
        "done_updated": "Aktualisiert.",
        "done_uninstalled": "Entfernt.",
        "footer": "Nur gewertete Bodybomb-5v5-Matches beeinflussen deinen Rang.",
    },
    "es": {
        "heading": "Modos de juego",
        "intro": "Los modos de la comunidad se instalan en tu carpeta de Bodycam. Abre las reglas de un modo.",
        "section_installed": "Instalados",
        "section_available": "Disponibles",
        "ranked": "Clasificatorio",
        "btn_install": "Instalar",
        "btn_uninstall": "Desinstalar",
        "btn_update": "Actualizar",
        "installed_label": "Instalado",
        "ruleset": "Reglas",
        "maps": "Mapas",
        "maps_none": "No disponible",
        "close": "Cerrar",
        "version": "Versión",
        "status": "Estado",
        "status_installed": "Instalado",
        "status_available": "Disponible",
        "yes": "Sí",
        "no": "No",
        "update_available": "Actualización disponible",
        "empty_none": "Aún no hay modos de juego en la lista.",
        "loading": "Cargando modos de juego…",
        "no_game": "Elige tu carpeta de Bodycam en Ajustes para instalar modos.",
        "game_running": "Cierra Bodycam antes de instalar o quitar un modo.",
        "working": "Trabajando…",
        "job_installing": "Instalando {name}…",
        "job_uninstalling": "Quitando {name}…",
        "job_updating": "Actualizando {name}…",
        "done_installed": "Instalado.",
        "done_updated": "Actualizado.",
        "done_uninstalled": "Eliminado.",
        "footer": "Solo las partidas clasificatorias de Bodybomb 5v5 afectan a tu rango.",
    },
    "fr": {
        "heading": "Modes de jeu",
        "intro": "Les modes communautaires s'installent dans votre dossier Bodycam. Ouvrez les règles d'un mode.",
        "section_installed": "Installés",
        "section_available": "Disponibles",
        "ranked": "Classé",
        "btn_install": "Installer",
        "btn_uninstall": "Désinstaller",
        "btn_update": "Mettre à jour",
        "installed_label": "Installé",
        "ruleset": "Règles",
        "maps": "Cartes",
        "maps_none": "Indisponible",
        "close": "Fermer",
        "version": "Version",
        "status": "Statut",
        "status_installed": "Installé",
        "status_available": "Disponible",
        "yes": "Oui",
        "no": "Non",
        "update_available": "Mise à jour disponible",
        "empty_none": "Aucun mode de jeu n'est encore listé.",
        "loading": "Chargement des modes de jeu…",
        "no_game": "Définissez votre dossier Bodycam dans les paramètres pour installer des modes.",
        "game_running": "Fermez Bodycam avant d'installer ou de retirer un mode.",
        "working": "Traitement…",
        "job_installing": "Installation de {name}…",
        "job_uninstalling": "Suppression de {name}…",
        "job_updating": "Mise à jour de {name}…",
        "done_installed": "Installé.",
        "done_updated": "Mis à jour.",
        "done_uninstalled": "Supprimé.",
        "footer": "Seuls les matchs classés de Bodybomb 5v5 affectent ton rang.",
    },
    "pt": {
        "heading": "Modos de jogo",
        "intro": "Os modos da comunidade são instalados na sua pasta do Bodycam. Abra as regras de um modo.",
        "section_installed": "Instalados",
        "section_available": "Disponíveis",
        "ranked": "Ranqueado",
        "btn_install": "Instalar",
        "btn_uninstall": "Desinstalar",
        "btn_update": "Atualizar",
        "installed_label": "Instalado",
        "ruleset": "Regras",
        "maps": "Mapas",
        "maps_none": "Indisponível",
        "close": "Fechar",
        "version": "Versão",
        "status": "Estado",
        "status_installed": "Instalado",
        "status_available": "Disponível",
        "yes": "Sim",
        "no": "Não",
        "update_available": "Atualização disponível",
        "empty_none": "Ainda não há modos de jogo listados.",
        "loading": "Carregando modos de jogo…",
        "no_game": "Defina a sua pasta do Bodycam em Configurações para instalar modos.",
        "game_running": "Feche o Bodycam antes de instalar ou remover um modo.",
        "working": "Trabalhando…",
        "job_installing": "Instalando {name}…",
        "job_uninstalling": "Removendo {name}…",
        "job_updating": "Atualizando {name}…",
        "done_installed": "Instalado.",
        "done_updated": "Atualizado.",
        "done_uninstalled": "Removido.",
        "footer": "Apenas as partidas classificatórias de Bodybomb 5v5 afetam o teu rank.",
    },
    "ru": {
        "heading": "Режимы игры",
        "intro": "Моды сообщества устанавливаются в папку Bodycam. Откройте правила режима.",
        "section_installed": "Установлено",
        "section_available": "Доступно",
        "ranked": "Рейтинговый",
        "btn_install": "Установить",
        "btn_uninstall": "Удалить",
        "btn_update": "Обновить",
        "installed_label": "Установлено",
        "ruleset": "Правила",
        "maps": "Карты",
        "maps_none": "Недоступно",
        "close": "Закрыть",
        "version": "Версия",
        "status": "Статус",
        "status_installed": "Установлено",
        "status_available": "Доступно",
        "yes": "Да",
        "no": "Нет",
        "update_available": "Доступно обновление",
        "empty_none": "Пока нет режимов игры.",
        "loading": "Загрузка режимов…",
        "no_game": "Укажите папку Bodycam в настройках, чтобы устанавливать режимы.",
        "game_running": "Закройте Bodycam перед установкой или удалением режима.",
        "working": "Выполняется…",
        "job_installing": "Установка {name}…",
        "job_uninstalling": "Удаление {name}…",
        "job_updating": "Обновление {name}…",
        "done_installed": "Установлено.",
        "done_updated": "Обновлено.",
        "done_uninstalled": "Удалено.",
        "footer": "На ваш ранг влияют только рейтинговые матчи Bodybomb 5 на 5.",
    },
    "zh": {
        "heading": "游戏模式",
        "intro": "社区模式将安装到你的 Bodycam 文件夹。打开某个模式的规则。",
        "section_installed": "已安装",
        "section_available": "可用",
        "ranked": "排位",
        "btn_install": "安装",
        "btn_uninstall": "卸载",
        "btn_update": "更新",
        "installed_label": "已安装",
        "ruleset": "规则",
        "maps": "地图",
        "maps_none": "暂无数据",
        "close": "关闭",
        "version": "版本",
        "status": "状态",
        "status_installed": "已安装",
        "status_available": "可用",
        "yes": "是",
        "no": "否",
        "update_available": "有可用更新",
        "empty_none": "尚无游戏模式。",
        "loading": "正在加载游戏模式…",
        "no_game": "在设置中指定你的 Bodycam 文件夹以安装模式。",
        "game_running": "安装或移除模式前请先关闭 Bodycam。",
        "working": "处理中…",
        "job_installing": "正在安装 {name}…",
        "job_uninstalling": "正在移除 {name}…",
        "job_updating": "正在更新 {name}…",
        "done_installed": "已安装。",
        "done_updated": "已更新。",
        "done_uninstalled": "已移除。",
        "footer": "只有 Bodybomb 5v5 排位赛会影响你的段位。",
    },
}


def _strings(lang: str) -> dict:
    """The active language's screen strings, English-filled so JS can look up any key."""
    merged = dict(GAMEMODES_STRINGS.get("en", {}))
    merged.update(GAMEMODES_STRINGS.get(lang, {}))
    return merged


# ---------------------------------------------------------------- game-running
# ops.apply is what actually enforces the guard (it raises "close the game"); the snapshot flag is
# advisory (the UI greys the buttons and shows a note). Read it live so the flag reflects the game
# starting/stopping at once — snapshots are de-duped and only built on a real state change, so this
# does not spawn `tasklist` on a tight loop.
def _game_running() -> bool:
    try:
        # CACHED: drawn on every snapshot. The Install button's own guard still
        # calls the exact game_running() (hub/ops.py), which is where it matters.
        return bool(game_mod.game_running_cached(game_mod.SNAPSHOT_TTL_SECONDS))
    except Exception:                # noqa: BLE001 — never let a probe crash the snapshot
        return False


# ---------------------------------------------------------------- catalogue helpers (mirror app.py)
def _installed(panel) -> dict:
    app = getattr(panel, "app", None)
    return (getattr(app, "state", {}) or {}).get("installed") or {}


def _catalogue(panel):
    return getattr(getattr(panel, "app", None), "catalogue", None)


def _game_dir(panel):
    return getattr(getattr(panel, "app", None), "game_dir", None)


def _entries(panel) -> list:
    """Catalogue gamemodes, plus any installed id the catalogue no longer lists (so the player can
    still see and uninstall it) — the web analogue of HubApp._entries."""
    catalogue = _catalogue(panel)
    entries = list((catalogue or {}).get("gamemodes", []))
    known = {e.get("id") for e in entries}
    for mode_id, info in _installed(panel).items():
        if mode_id not in known:
            entries.append({"id": mode_id, "title": info.get("title", mode_id),
                            "titles": info.get("titles") or {},
                            "version": info.get("version", ""), "_orphan": True})
    return entries


def _known_ids(panel) -> set:
    return {e.get("id") for e in (_catalogue(panel) or {}).get("gamemodes", []) if e.get("id")}


def _ruleset_text(entry: dict) -> str:
    """The gamemode's rules in the hub's language, else English, else the first one there is."""
    rs = entry.get("rulesets") or {}
    if not isinstance(rs, dict):
        return ""
    for code in (i18n.get_language(), "en"):
        if rs.get(code):
            return str(rs[code])
    return str(next((v for v in rs.values() if v), ""))


def _ruleset_lines(text: str, name: str) -> list:
    """Split the ruleset blob into display lines: drop blanks, drop a leading title line that just
    repeats the mode name, and strip a leading bullet glyph (the JS renders its own bullet)."""
    out = []
    for raw in str(text or "").split("\n"):
        s = raw.strip()
        if not s:
            continue
        if not out and s == name:
            continue                          # the header line duplicates the title shown above
        if s[:1] in ("•", "-", "*", "–"):
            s = s[1:].strip()
        out.append(s)
    return out


def _maps(entry: dict) -> list:
    maps = entry.get("maps") or (entry.get("manifest") or {}).get("maps") or []
    if not isinstance(maps, list):
        return []
    return [str(m) for m in maps if m]


# ---------------------------------------------------------------- snapshot slice
def _mode(entry: dict, panel, installed: dict, ready: bool, running: bool, busy: bool) -> dict:
    mode_id = entry.get("id")
    is_installed = mode_id in installed
    orphan = bool(entry.get("_orphan"))
    ranked = (mode_id == COMPETITIVE_MODE_ID)
    cat_version = str(entry.get("version", "") or "")
    have_version = str((installed.get(mode_id) or {}).get("version", "") or "") if is_installed else ""
    update_available = bool(is_installed and
                            cat.mode_update_available(entry, installed.get(mode_id) or {}))
    text = _ruleset_text(entry)
    name = cat.display_title(entry)
    from ...activity import match_active
    actionable = ready and not running and not busy and not match_active(panel)
    return {
        "id": mode_id,
        "name": name,
        "description": str(entry.get("description", "") or ""),
        "version": cat_version,
        "installed": is_installed,
        "installed_version": have_version or None,
        "ranked": ranked,
        "orphan": orphan,
        "update_available": update_available,
        "ruleset": text,
        "ruleset_lines": _ruleset_lines(text, name),
        "maps": _maps(entry),
        # what the row's buttons may do right now (JS still guards, this is the authority)
        "can_install": bool(actionable and (not is_installed) and (not orphan)),
        "can_uninstall": bool(actionable and is_installed),
        "can_update": bool(actionable and update_available),
    }


@register_snapshot("gamemodes")
def snapshot(session, panel) -> dict:
    """Contribute the ``gamemodes`` slice: the catalogue modes with their state, the ready/running
    guards, the running job's progress, the last notice, and this screen's strings."""
    lang = i18n.get_language()
    catalogue = _catalogue(panel)
    game_dir = _game_dir(panel)
    installed = _installed(panel)
    ready = bool(game_dir) and game_mod.is_game_dir(game_dir)
    running = _game_running()
    job = getattr(panel, "_gamemodes_job", None)
    busy = bool(job and job.get("busy"))
    notice = getattr(panel, "_gamemodes_notice", None)

    modes = [_mode(e, panel, installed, ready, running, busy) for e in _entries(panel)]
    installed_modes = [m for m in modes if m["installed"]]
    available_modes = [m for m in modes if not m["installed"]]
    # ranked first in each column (matches the design's emphasis on Bodybomb 5v5)
    installed_modes.sort(key=lambda m: (not m["ranked"], m["name"].lower()))
    available_modes.sort(key=lambda m: (not m["ranked"], m["name"].lower()))

    return {
        "gamemodes": {
            "strings": _strings(lang),
            "ready": ready,
            "running": running,
            "catalogue_loaded": catalogue is not None,
            "busy": busy,
            "job": dict(job) if job else None,
            "notice": dict(notice) if notice else None,
            "open_ruleset": getattr(panel, "_gamemodes_ruleset", None),
            "installed": installed_modes,
            "available": available_modes,
        },
        # ...and the same news as a STRIP, over whatever screen they are on.
        #
        # Sam, 2026-09-17: "similar to how the lightsoff app auto detects an update without having
        # to restart or go to a different page, do the same with gamemodes". The hub's own update
        # has had that since 2.0.22 - state.update, drawn in the core chrome by core.js - and a
        # gamemode's did not: it was a button on one row of one tab, so the only way to find out was
        # to go and look. That is how a whole evening of testing ran on a pak whose rules had been
        # replaced hours earlier.
        #
        # ONE mode, not a list: two strips stacked over the screen is worse than a second trip to
        # the tab, and in practice there is one ranked mode. Ranked first when both are stale, since
        # it is the one that stops you queueing.
        "gamemode_update": _update_strip(installed_modes, running, busy, ready),
    }


def _update_strip(installed_modes, running, busy, ready):
    """The gamemode-update strip's slice, or None when there is nothing to say.

    `can_update` carries the same authority the row's button does, and the strip says WHY when it is
    false - a button that does nothing when Bodycam is open is the version of this that gets
    reported as broken."""
    stale = [m for m in installed_modes if m.get("update_available")]
    if not stale:
        return None
    stale.sort(key=lambda m: (not m.get("ranked"), str(m.get("name") or "").lower()))
    mode = stale[0]
    latest = str(mode.get("version") or "")
    current = str(mode.get("installed_version") or "")
    return {
        "id": mode.get("id"),
        "name": mode.get("name"),
        "latest": latest,
        "current": current,
        # The version did not move, so the RULES did - the case that used to be invisible from
        # here AND from the version number. Kept for a service older than the build numbering.
        "rules_only": bool(latest and current and latest == current),
        "can_update": bool(mode.get("can_update")),
        "running": bool(running),
        "busy": bool(busy),
        "ready": bool(ready),
        "more": len(stale) - 1,
    }


# ---------------------------------------------------------------- install worker
# Spawns the long ops.apply on a worker thread (tests replace this to run synchronously). Mirrors
# HubApp._apply: the worker calls ops.apply and reports progress/done/error back onto the UI thread.
def run_async(fn):
    threading.Thread(target=fn, daemon=True).start()


def _begin(panel, action: str, target: str, desired: set):
    """UI thread: mark the screen busy, push, then hand the blocking work to a worker thread."""
    from ...activity import files_locked
    if files_locked(panel):
        panel._gamemodes_notice = {"kind": "error", "text": i18n.t("comp_files_locked")}
        panel.on_change()
        return                                # one job at a time (as the Tk tab: self.busy)
    from ... import telemetry
    telemetry.emit("operation.start", action=action)
    catalogue = _catalogue(panel)
    game_dir = _game_dir(panel)
    entry = cat.entry_by_id(catalogue or {}, target)
    name = cat.display_title(entry) if entry else target
    key = {"install": "job_installing", "uninstall": "job_uninstalling",
           "update": "job_updating"}.get(action, "working")
    strings = _strings(i18n.get_language())
    panel._gamemodes_job = {
        "busy": True, "action": action, "target": target,
        "progress": None, "message": strings.get(key, "").replace("{name}", name),
        "error": "",
    }
    panel._gamemodes_notice = None
    panel.on_change()

    # Only ids the catalogue still describes can be built into the pak (as HubApp._apply filters).
    desired = {i for i in desired if i in _known_ids(panel)}

    def work():
        try:
            st = state_mod.load()
            res = ops.apply(
                desired, catalogue, st, game_dir,
                log=lambda m: None,
                progress=lambda f, m: panel.post(lambda f=f, m=m: _progress(panel, f, m)),
            )
            panel.post(lambda: _finish(panel, action, res))
        except Exception as exc:              # noqa: BLE001 — surface, do not crash the worker
            msg = str(exc)
            panel.post(lambda: _fail(panel, msg))

    run_async(work)


def _reload_state(panel):
    """UI thread: re-read what ops.apply persisted, so the next snapshot is drawn from it.

    ops.apply is handed its OWN state dict (`st` above) and saves that; `app.state` is a different
    object and still describes the world as it was before the install. Every row on the screen is
    drawn from it — installed vs available, the version under the name, which buttons the row gets
    — so until this runs, a finished install redraws as if it never happened.

    IT HAS TO RUN HERE, ON THE UI THREAD, BEFORE THE PUSH. It used to be the worker's parting shot
    in a `finally` after `panel.post(_finish)` had already queued the redraw, which made it a race
    between a thread waking up and a JSON file being parsed — and the redraw usually won. The
    banner said "Installed." over a row that still offered to install it, and it stayed that way
    until something else re-emitted the snapshot, which in practice meant leaving the tab and
    coming back (Sam, 2026-09-16)."""
    app = getattr(panel, "app", None)
    if app is None:
        return
    try:
        saved = state_mod.load()
        app.state.update({key: saved[key] for key in state_mod.INSTALL_FIELDS})
    except Exception:                         # noqa: BLE001 — a read-only state dir must not
        pass                                  # cost us the notice the player is waiting for


def _progress(panel, fraction, message):
    job = getattr(panel, "_gamemodes_job", None)
    if not job or not job.get("busy"):
        return
    if fraction is not None:
        try:
            job["progress"] = max(0.0, min(1.0, float(fraction)))
        except (TypeError, ValueError):
            job["progress"] = None
    else:
        job["progress"] = None
    if message:
        job["message"] = str(message)
    panel.on_change()


def _finish(panel, action, res):
    from ... import telemetry
    telemetry.emit("operation.outcome", action=action, status="complete")
    res = res or {}
    _reload_state(panel)
    strings = _strings(i18n.get_language())
    if res.get("removed"):
        kind, key = "uninstalled", "done_uninstalled"
    elif action == "update":
        kind, key = "updated", "done_updated"
    else:
        kind, key = "installed", "done_installed"
    panel._gamemodes_job = None
    panel._gamemodes_notice = {"kind": kind, "text": strings.get(key, "")}
    panel.on_change()


def _fail(panel, message):
    from ... import telemetry
    telemetry.emit("operation.outcome", action=(getattr(panel, "_gamemodes_job", None) or {}).get("action", "install"), status="failed", severity="error")
    # A failure is not proof that nothing changed: ops.apply persists the new state as its LAST
    # step, so anything that went wrong after that point leaves state.json ahead of app.state.
    # Re-read before drawing the error, for the same reason _finish does.
    _reload_state(panel)
    panel._gamemodes_job = None
    panel._gamemodes_notice = {"kind": "error", "text": str(message or "")}
    panel.on_change()


def rebuild_for_rules_override(panel) -> bool:
    """Rebuild the pak, unasked, when the catalogue's rules override stopped matching the one the
    installed pak was built with. Returns True when a rebuild was started.

    THIS IS THE HALF THAT MAKES A DEPLOY LAND. Changing COMP_GAME_RULES_OVERRIDE changes what the
    catalogue says the rounds are, but the pak on disk is whatever it was built with - so without
    this a tester would have to notice the Update button and press it before every session, which
    is the chore the override existed to remove. The catalogue is re-read on a timer
    (webui/shell.py), so "hit deploy, keep playing" is the whole loop.

    Deliberately narrow:
      * only when an override CHANGED (production ships none, so this never runs there),
      * never a version bump - taking a new gamemode version is the player's choice, as before,
      * never while the game is running (the pak is locked), another job is in flight, or they are
        queued or in a match.
    It runs as an ordinary "update" job, so the screen shows the same progress bar and the same
    "Updated." notice as pressing the button would.
    """
    if getattr(panel, "_gamemodes_job", None):
        return False
    if not _game_dir(panel) or _game_running():
        return False
    # Not while they are queued or in a match: the build takes 30-60 s and a match can pop in the
    # middle of it, so one more match on the old rules is the better trade.
    session = getattr(panel, "session", None)
    try:
        phase = str(getattr(session, "phase", "") or "")
        if phase in ("queued", "checking") or (session is not None and session.locked_in()):
            return False
    except Exception:                 # noqa: BLE001 - a session that cannot answer is not a reason
        pass                          # to leave a tester on the wrong rules for ever
    installed = _installed(panel)
    catalogue = _catalogue(panel)
    for mode_id, info in installed.items():
        entry = cat.entry_by_id(catalogue or {}, mode_id)
        if not entry:
            continue
        if cat.rules_override(entry) != ((info or {}).get("rules_override") or {}):
            _begin(panel, "update", mode_id, set(installed))
            return True
    return False


# ---------------------------------------------------------------- verbs (JS -> Python)
def _install(panel, mode_id):
    mode_id = str(mode_id or "")
    if not mode_id:
        return
    panel.post(lambda: _begin(panel, "install", mode_id, set(_installed(panel)) | {mode_id}))


def _uninstall(panel, mode_id):
    mode_id = str(mode_id or "")
    if not mode_id:
        return
    panel.post(lambda: _begin(panel, "uninstall", mode_id, set(_installed(panel)) - {mode_id}))


def _update(panel, mode_id):
    """The row Update button: keep everything installed, refetch this mode at the catalogue version
    (HubApp._on_update). ops.apply rebuilds the pak from the newest packs for the whole set."""
    mode_id = str(mode_id or "")
    if not mode_id:
        return
    if mode_id not in _installed(panel):
        return
    panel.post(lambda: _begin(panel, "update", mode_id, set(_installed(panel))))


def _open_ruleset(panel, mode_id):
    """View-only toggle: remember which mode's ruleset modal is open, re-emit (like party hide)."""
    def apply_():
        panel._gamemodes_ruleset = str(mode_id or "") or None
        panel.on_change()
    panel.post(apply_)


def _close_ruleset(panel):
    def apply_():
        panel._gamemodes_ruleset = None
        panel.on_change()
    panel.post(apply_)


register_verbs("gamemodes", {
    "gamemode_install":   _install,
    "gamemode_uninstall": _uninstall,
    "gamemode_update":    _update,
    "open_ruleset":       _open_ruleset,
    "close_ruleset":      _close_ruleset,
})
