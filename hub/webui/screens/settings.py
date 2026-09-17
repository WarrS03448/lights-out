"""Settings screen — the Python half: its snapshot slice, its bridge verbs, and its strings.

OWNED BY THE SETTINGS SCREEN. This is the only Python screen module the settings agent edits. It
contributes the ``settings`` slice of the state snapshot and registers the verbs the JS screen
(static/screens/settings.js) calls. It reconciles the design deltas the plan calls out
(docs/ui-redesign-plan.md turn-2): the match-found sound, the Bodycam game path / Browse, the
language picker and Sign-out all live HERE now, and the old shooting-range warning is rendered as
an inline readiness checklist (Steam · Bodycam · Servers · Location).

Nothing here is authoritative and nothing new lives in the backend: every verb reuses an existing
session / game / state method on the UI thread (``panel.post``), exactly as the Tk panel did:
  * sound     -> the same ``comp_sound_volume`` state key the Tk CompetitivePanel used, played
                 through hub/sounds.py.
  * game path -> hub/game.py detection + validation, persisted to state.json (hub/state.py),
                 mirroring HubApp._on_browse.
  * language  -> the CORE ``set_language`` verb (bridge.py) — the JS calls it directly; we do not
                 re-implement it here.
  * sign-out  -> ``session.sign_out`` (which already refuses while a match is locked in, C14).
  * uninstall -> hub/uninstall.py, which is the one place that knows how to put the game back and
                 hand over to the Inno uninstaller. The verbs below only guard and dispatch.

New strings this screen needs but i18n.py does not carry are shipped per-language INSIDE this
slice (``settings.strings``) and read by the JS via a local lookup — i18n.py is the source of
truth for everything it already has and is NOT edited here (plan: i18n). The 7-language coverage
of the whole app is preserved because every language below has the same key set.

This module imports no pywebview and no Tk, so it stays testable headless (tests/test_hub.py,
tests/test_screen_settings.py) against a LiveSession driven by the fake live client.
"""
import os
import sys
import threading

from . import register_snapshot, register_verbs
from ... import game as game_mod
from ... import i18n
from ... import paths as paths_mod
from ... import sounds as sounds_mod
from ... import state as state_mod
from ... import uninstall as uninstall_mod


# ---------------------------------------------------------------- per-language strings
# Only the strings i18n.py does not already carry. Keys that DO exist in i18n (nav_settings,
# comp_sound_label, comp_sound_test, comp_sound_muted, comp_signout, comp_signout_locked, browse)
# are looked up by the JS through ctx.t() and are intentionally absent here. Every language has the
# same key set, so the app's 7-language coverage is unchanged.
_S = {
    "en": {
        "click_sound": "UI click sounds",
        "click_volume": "UI click volume",
        "click_hint": "Play a short sound when you click controls. Separate from the match-found alert.",
        "subtitle": "Changes save as you make them",
        "sec_audio": "Audio", "sec_game": "Game", "sec_account": "Account",
        "sec_readiness": "Readiness", "sec_network": "Matchmaking network",
        "sound_hint": "Plays over the game so you hear it from the shooting range.",
        "volume": "Volume",
        "path_name": "Bodycam install path",
        "path_detected": "Detected automatically from Steam",
        "path_manual": "Set your Bodycam folder manually",
        "path_placeholder": "Path to your Bodycam folder",
        "path_set": "Set",
        "lang_name": "Language",
        "acct_meta": "Steam ID {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "Servers",
        "ready_connected": "Connected", "ready_running": "Running", "ready_ok": "OK",
        "ready_offline": "Offline", "ready_not_running": "Not running",
        "ready_signed_out": "Signed out", "ready_closed": "Closed", "ready_close_it": "Close it before you queue",
        "ready_hint": "The hub puts you into the match from the shooting range. A match found "
                      "while you are elsewhere cannot start.",
        "network_region": "Region",
        "network_select": "Select region",
        "network_same_region": "Matches stay in your region by default.",
        "network_cross_short": "Cross-region",
        "network_cross_region": "Allow cross-region matches when ping is acceptable",
        "network_cross_hint": "Every affected player must opt in. The 120 ms limit always applies.",
        "network_preparing": "Preparing relay connection measurements…",
        "network_unavailable": "Latency estimates unavailable",
        "network_ready": "Latency estimates ready",
        "network_locked": "Region preferences cannot change during a match.",
        "region_NA": "North America", "region_SA": "South America", "region_EU": "Europe",
        "region_AS": "Asia", "region_OC": "Oceania", "region_AF": "Africa", "region_ME": "Middle East",
        "sec_uninstall": "Uninstall",
        "uninst_name": "Uninstall Lights Out",
        "uninst_hint": "Removes the app and takes our files back out of your Bodycam folder.",
        "uninst_btn": "Uninstall…",
        "uninst_title": "Uninstall Lights Out?",
        "uninst_body": "Lights Out closes, your Bodycam folder goes back to stock, and Windows "
                       "removes the app.",
        "uninst_files_label": "Removed from your Bodycam folder",
        "uninst_files_none": "Nothing of ours is in your Bodycam folder.",
        "uninst_keep": "Your settings, downloaded gamemodes and sign-in stay in {dir}, so "
                       "reinstalling picks up where you left off.",
        "uninst_wipe": "Also delete my settings, downloaded gamemodes and sign-in",
        "uninst_confirm": "Uninstall",
        "uninst_cancel": "Cancel",
        "uninst_working": "Uninstalling…",
        "uninst_locked": "You cannot uninstall while a match is locked in.",
        "uninst_err_not_installed": "This copy was not put here by the Lights Out installer, so "
                                    "there is nothing for Windows to uninstall.",
        "uninst_err_not_windows": "The uninstaller only exists on Windows.",
        "uninst_err_game_running": "Close Bodycam first — our files are in use while it runs.",
        "uninst_err_launch_failed": "Windows could not start the uninstaller.",
    },
    "de": {
        "sec_network": "Netzwerk für die Spielsuche",
        "network_region": "Region", "network_select": "Region auswählen",
        "network_same_region": "Spiele bleiben standardmäßig in deiner Region.",
        "network_cross_short": "Regionsübergreifend",
        "network_cross_region": "Regionsübergreifende Spiele bei akzeptablem Ping erlauben",
        "network_cross_hint": "Alle betroffenen Spieler müssen zustimmen. Das Limit von 120 ms gilt immer.",
        "network_preparing": "Relay-Verbindungsmessungen werden vorbereitet…",
        "network_unavailable": "Latenzschätzungen nicht verfügbar",
        "network_ready": "Latenzschätzungen bereit",
        "network_locked": "Regionseinstellungen können während eines Spiels nicht geändert werden.",
        "region_NA": "Nordamerika", "region_SA": "Südamerika", "region_EU": "Europa",
        "region_AS": "Asien", "region_OC": "Ozeanien", "region_AF": "Afrika", "region_ME": "Naher Osten",
        "click_sound": "Klickgeräusche",
        "click_volume": "Klicklautstärke",
        "click_hint": "Ein kurzer Ton beim Anklicken von Bedienelementen. Unabhängig vom Spiel-gefunden-Ton.",
        "subtitle": "Änderungen werden sofort gespeichert",
        "sec_audio": "Audio", "sec_game": "Spiel", "sec_account": "Konto",
        "sec_readiness": "Bereitschaft",
        "sound_hint": "Wird über dem Spiel abgespielt, damit du ihn im Schießstand hörst.",
        "volume": "Lautstärke",
        "path_name": "Bodycam-Installationspfad",
        "path_detected": "Automatisch über Steam erkannt",
        "path_manual": "Bodycam-Ordner manuell festlegen",
        "path_placeholder": "Pfad zu deinem Bodycam-Ordner",
        "path_set": "Setzen",
        "lang_name": "Sprache",
        "acct_meta": "Steam-ID {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "Server",
        "ready_connected": "Verbunden", "ready_running": "Läuft", "ready_ok": "OK",
        "ready_offline": "Offline", "ready_not_running": "Läuft nicht",
        "ready_signed_out": "Abgemeldet", "ready_closed": "Geschlossen", "ready_close_it": "Vor dem Anstellen schließen",
        "ready_hint": "Der Hub bringt dich aus dem Schießstand ins Match. Ein Match, das gefunden "
                      "wird, während du woanders bist, kann nicht starten.",
        "sec_uninstall": "Deinstallieren",
        "uninst_name": "Lights Out deinstallieren",
        "uninst_hint": "Entfernt die App und nimmt unsere Dateien wieder aus deinem Bodycam-Ordner.",
        "uninst_btn": "Deinstallieren…",
        "uninst_title": "Lights Out deinstallieren?",
        "uninst_body": "Lights Out schließt sich, dein Bodycam-Ordner ist wieder im Originalzustand, "
                       "und Windows entfernt die App.",
        "uninst_files_label": "Aus deinem Bodycam-Ordner entfernt",
        "uninst_files_none": "In deinem Bodycam-Ordner liegt nichts von uns.",
        "uninst_keep": "Deine Einstellungen, heruntergeladenen Spielmodi und die Anmeldung bleiben "
                       "in {dir}, damit eine Neuinstallation dort weitermacht, wo du aufgehört hast.",
        "uninst_wipe": "Einstellungen, heruntergeladene Spielmodi und Anmeldung ebenfalls löschen",
        "uninst_confirm": "Deinstallieren",
        "uninst_cancel": "Abbrechen",
        "uninst_working": "Wird deinstalliert…",
        "uninst_locked": "Solange ein Match feststeht, kannst du nicht deinstallieren.",
        "uninst_err_not_installed": "Diese Kopie stammt nicht vom Lights-Out-Installer, es gibt "
                                    "also nichts, was Windows deinstallieren könnte.",
        "uninst_err_not_windows": "Den Deinstaller gibt es nur unter Windows.",
        "uninst_err_game_running": "Schließe zuerst Bodycam — solange es läuft, sind unsere Dateien in Benutzung.",
        "uninst_err_launch_failed": "Windows konnte den Deinstaller nicht starten.",
    },
    "es": {
        "sec_network": "Red de emparejamiento",
        "network_region": "Región", "network_select": "Seleccionar región",
        "network_same_region": "Las partidas se mantienen en tu región por defecto.",
        "network_cross_short": "Entre regiones",
        "network_cross_region": "Permitir partidas entre regiones si el ping es aceptable",
        "network_cross_hint": "Todos los jugadores afectados deben activarlo. El límite de 120 ms siempre se aplica.",
        "network_preparing": "Preparando mediciones de conexión por retransmisión…",
        "network_unavailable": "Estimaciones de latencia no disponibles",
        "network_ready": "Estimaciones de latencia listas",
        "network_locked": "No puedes cambiar las preferencias de región durante una partida.",
        "region_NA": "América del Norte", "region_SA": "América del Sur", "region_EU": "Europa",
        "region_AS": "Asia", "region_OC": "Oceanía", "region_AF": "África", "region_ME": "Oriente Medio",
        "click_sound": "Sonidos de clic",
        "click_volume": "Volumen de clic",
        "click_hint": "Reproduce un sonido breve al pulsar los controles. Independiente del aviso de partida encontrada.",
        "subtitle": "Los cambios se guardan al instante",
        "sec_audio": "Audio", "sec_game": "Juego", "sec_account": "Cuenta",
        "sec_readiness": "Preparación",
        "sound_hint": "Se reproduce sobre el juego para que lo oigas desde el campo de tiro.",
        "volume": "Volumen",
        "path_name": "Ruta de instalación de Bodycam",
        "path_detected": "Detectada automáticamente desde Steam",
        "path_manual": "Define tu carpeta de Bodycam manualmente",
        "path_placeholder": "Ruta a tu carpeta de Bodycam",
        "path_set": "Fijar",
        "lang_name": "Idioma",
        "acct_meta": "ID de Steam {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "Servidores",
        "ready_connected": "Conectado", "ready_running": "En ejecución", "ready_ok": "OK",
        "ready_offline": "Sin conexión", "ready_not_running": "No está en ejecución",
        "ready_signed_out": "Sesión cerrada", "ready_closed": "Cerrado", "ready_close_it": "Ciérralo antes de entrar en cola",
        "ready_hint": "El hub te mete en la partida desde el campo de tiro. Una partida encontrada "
                      "mientras estás en otro sitio no puede empezar.",
        "sec_uninstall": "Desinstalar",
        "uninst_name": "Desinstalar Lights Out",
        "uninst_hint": "Elimina la app y saca nuestros archivos de tu carpeta de Bodycam.",
        "uninst_btn": "Desinstalar…",
        "uninst_title": "¿Desinstalar Lights Out?",
        "uninst_body": "Lights Out se cierra, tu carpeta de Bodycam vuelve a su estado original y "
                       "Windows elimina la app.",
        "uninst_files_label": "Eliminado de tu carpeta de Bodycam",
        "uninst_files_none": "No hay nada nuestro en tu carpeta de Bodycam.",
        "uninst_keep": "Tus ajustes, los modos descargados y tu sesión se quedan en {dir}, así que "
                       "al reinstalar seguirás donde lo dejaste.",
        "uninst_wipe": "Borrar también mis ajustes, los modos descargados y mi sesión",
        "uninst_confirm": "Desinstalar",
        "uninst_cancel": "Cancelar",
        "uninst_working": "Desinstalando…",
        "uninst_locked": "No puedes desinstalar mientras haya una partida confirmada.",
        "uninst_err_not_installed": "Esta copia no la puso aquí el instalador de Lights Out, así "
                                    "que no hay nada que Windows pueda desinstalar.",
        "uninst_err_not_windows": "El desinstalador solo existe en Windows.",
        "uninst_err_game_running": "Cierra Bodycam primero: mientras se ejecuta, nuestros archivos están en uso.",
        "uninst_err_launch_failed": "Windows no pudo iniciar el desinstalador.",
    },
    "fr": {
        "sec_network": "Réseau de matchmaking",
        "network_region": "Région", "network_select": "Choisir une région",
        "network_same_region": "Par défaut, les parties restent dans ta région.",
        "network_cross_short": "Interrégion",
        "network_cross_region": "Autoriser les parties entre régions si le ping est acceptable",
        "network_cross_hint": "Tous les joueurs concernés doivent l’activer. La limite de 120 ms s’applique toujours.",
        "network_preparing": "Préparation des mesures de connexion par relais…",
        "network_unavailable": "Estimations de latence indisponibles",
        "network_ready": "Estimations de latence prêtes",
        "network_locked": "Les préférences de région ne peuvent pas changer pendant une partie.",
        "region_NA": "Amérique du Nord", "region_SA": "Amérique du Sud", "region_EU": "Europe",
        "region_AS": "Asie", "region_OC": "Océanie", "region_AF": "Afrique", "region_ME": "Moyen-Orient",
        "click_sound": "Sons de clic",
        "click_volume": "Volume des clics",
        "click_hint": "Un son bref lorsque vous cliquez sur les commandes. Indépendant de l’alerte de partie trouvée.",
        "subtitle": "Les modifications sont enregistrées au fur et à mesure",
        "sec_audio": "Audio", "sec_game": "Jeu", "sec_account": "Compte",
        "sec_readiness": "Préparation",
        "sound_hint": "Se joue par-dessus le jeu pour que tu l'entendes depuis le stand de tir.",
        "volume": "Volume",
        "path_name": "Dossier d'installation de Bodycam",
        "path_detected": "Détecté automatiquement depuis Steam",
        "path_manual": "Définir ton dossier Bodycam manuellement",
        "path_placeholder": "Chemin vers ton dossier Bodycam",
        "path_set": "Définir",
        "lang_name": "Langue",
        "acct_meta": "ID Steam {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "Serveurs",
        "ready_connected": "Connecté", "ready_running": "En cours", "ready_ok": "OK",
        "ready_offline": "Hors ligne", "ready_not_running": "Pas lancé",
        "ready_signed_out": "Déconnecté", "ready_closed": "Fermé", "ready_close_it": "Fermez-le avant de rejoindre la file",
        "ready_hint": "Le hub te place dans le match depuis le stand de tir. Un match trouvé "
                      "pendant que tu es ailleurs ne peut pas démarrer.",
        "sec_uninstall": "Désinstaller",
        "uninst_name": "Désinstaller Lights Out",
        "uninst_hint": "Supprime l'application et retire nos fichiers de ton dossier Bodycam.",
        "uninst_btn": "Désinstaller…",
        "uninst_title": "Désinstaller Lights Out ?",
        "uninst_body": "Lights Out se ferme, ton dossier Bodycam revient à son état d'origine et "
                       "Windows supprime l'application.",
        "uninst_files_label": "Retiré de ton dossier Bodycam",
        "uninst_files_none": "Il n'y a rien à nous dans ton dossier Bodycam.",
        "uninst_keep": "Tes réglages, les modes téléchargés et ta connexion restent dans {dir} : "
                       "une réinstallation repart exactement d'où tu t'es arrêté.",
        "uninst_wipe": "Supprimer aussi mes réglages, les modes téléchargés et ma connexion",
        "uninst_confirm": "Désinstaller",
        "uninst_cancel": "Annuler",
        "uninst_working": "Désinstallation…",
        "uninst_locked": "Impossible de désinstaller tant qu'un match est confirmé.",
        "uninst_err_not_installed": "Cette copie n'a pas été posée là par l'installeur Lights Out : "
                                    "Windows n'a donc rien à désinstaller.",
        "uninst_err_not_windows": "Le désinstalleur n'existe que sous Windows.",
        "uninst_err_game_running": "Ferme d'abord Bodycam — tant qu'il tourne, nos fichiers sont utilisés.",
        "uninst_err_launch_failed": "Windows n'a pas pu lancer le désinstalleur.",
    },
    "pt": {
        "sec_network": "Rede de matchmaking",
        "network_region": "Região", "network_select": "Selecionar região",
        "network_same_region": "Por predefinição, as partidas ficam na tua região.",
        "network_cross_short": "Entre regiões",
        "network_cross_region": "Permitir partidas entre regiões se o ping for aceitável",
        "network_cross_hint": "Todos os jogadores envolvidos têm de ativar a opção. O limite de 120 ms aplica-se sempre.",
        "network_preparing": "A preparar medições de ligação por retransmissão…",
        "network_unavailable": "Estimativas de latência indisponíveis",
        "network_ready": "Estimativas de latência prontas",
        "network_locked": "Não podes alterar as preferências de região durante uma partida.",
        "region_NA": "América do Norte", "region_SA": "América do Sul", "region_EU": "Europa",
        "region_AS": "Ásia", "region_OC": "Oceânia", "region_AF": "África", "region_ME": "Médio Oriente",
        "click_sound": "Sons de clique",
        "click_volume": "Volume dos cliques",
        "click_hint": "Um som curto ao clicar nos controlos. Independente do aviso de partida encontrada.",
        "subtitle": "As alterações são guardadas à medida que as fazes",
        "sec_audio": "Áudio", "sec_game": "Jogo", "sec_account": "Conta",
        "sec_readiness": "Prontidão",
        "sound_hint": "Toca por cima do jogo para que o ouças a partir da carreira de tiro.",
        "volume": "Volume",
        "path_name": "Caminho de instalação do Bodycam",
        "path_detected": "Detetado automaticamente pelo Steam",
        "path_manual": "Define a tua pasta do Bodycam manualmente",
        "path_placeholder": "Caminho para a tua pasta do Bodycam",
        "path_set": "Definir",
        "lang_name": "Idioma",
        "acct_meta": "ID Steam {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "Servidores",
        "ready_connected": "Ligado", "ready_running": "Em execução", "ready_ok": "OK",
        "ready_offline": "Offline", "ready_not_running": "Não está em execução",
        "ready_signed_out": "Sessão terminada", "ready_closed": "Fechado", "ready_close_it": "Feche antes de entrar na fila",
        "ready_hint": "O hub coloca-te na partida a partir da carreira de tiro. Uma partida "
                      "encontrada enquanto estás noutro lado não pode começar.",
        "sec_uninstall": "Desinstalar",
        "uninst_name": "Desinstalar o Lights Out",
        "uninst_hint": "Remove a app e tira os nossos ficheiros da tua pasta do Bodycam.",
        "uninst_btn": "Desinstalar…",
        "uninst_title": "Desinstalar o Lights Out?",
        "uninst_body": "O Lights Out fecha, a tua pasta do Bodycam volta ao estado original e o "
                       "Windows remove a app.",
        "uninst_files_label": "Removido da tua pasta do Bodycam",
        "uninst_files_none": "Não há nada nosso na tua pasta do Bodycam.",
        "uninst_keep": "As tuas definições, os modos transferidos e a tua sessão ficam em {dir}, "
                       "por isso uma reinstalação continua onde paraste.",
        "uninst_wipe": "Apagar também as minhas definições, os modos transferidos e a sessão",
        "uninst_confirm": "Desinstalar",
        "uninst_cancel": "Cancelar",
        "uninst_working": "A desinstalar…",
        "uninst_locked": "Não podes desinstalar enquanto houver uma partida confirmada.",
        "uninst_err_not_installed": "Esta cópia não foi colocada aqui pelo instalador do Lights "
                                    "Out, por isso o Windows não tem nada para desinstalar.",
        "uninst_err_not_windows": "O desinstalador só existe no Windows.",
        "uninst_err_game_running": "Fecha primeiro o Bodycam — enquanto corre, os nossos ficheiros estão em uso.",
        "uninst_err_launch_failed": "O Windows não conseguiu iniciar o desinstalador.",
    },
    "ru": {
        "sec_network": "Сеть для подбора матчей",
        "network_region": "Регион", "network_select": "Выберите регион",
        "network_same_region": "По умолчанию матчи подбираются в вашем регионе.",
        "network_cross_short": "Между регионами",
        "network_cross_region": "Разрешить матчи между регионами при приемлемом пинге",
        "network_cross_hint": "Все участвующие игроки должны включить эту настройку. Ограничение 120 мс действует всегда.",
        "network_preparing": "Подготовка измерений соединения через ретранслятор…",
        "network_unavailable": "Оценки задержки недоступны",
        "network_ready": "Оценки задержки готовы",
        "network_locked": "Нельзя менять настройки региона во время матча.",
        "region_NA": "Северная Америка", "region_SA": "Южная Америка", "region_EU": "Европа",
        "region_AS": "Азия", "region_OC": "Океания", "region_AF": "Африка", "region_ME": "Ближний Восток",
        "click_sound": "Звуки нажатий",
        "click_volume": "Громкость нажатий",
        "click_hint": "Короткий звук при нажатии на элементы интерфейса. Отдельно от сигнала найденного матча.",
        "subtitle": "Изменения сохраняются сразу",
        "sec_audio": "Звук", "sec_game": "Игра", "sec_account": "Аккаунт",
        "sec_readiness": "Готовность",
        "sound_hint": "Проигрывается поверх игры, чтобы вы услышали его в тире.",
        "volume": "Громкость",
        "path_name": "Путь установки Bodycam",
        "path_detected": "Определено автоматически из Steam",
        "path_manual": "Указать папку Bodycam вручную",
        "path_placeholder": "Путь к папке Bodycam",
        "path_set": "Задать",
        "lang_name": "Язык",
        "acct_meta": "Steam ID {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "Серверы",
        "ready_connected": "Подключено", "ready_running": "Запущена", "ready_ok": "OK",
        "ready_offline": "Не в сети", "ready_not_running": "Не запущена",
        "ready_signed_out": "Выполнен выход", "ready_closed": "Закрыта", "ready_close_it": "Закройте её перед входом в очередь",
        "ready_hint": "Хаб переносит вас в матч из тира. Матч, найденный, пока вы в другом месте, "
                      "не может начаться.",
        "sec_uninstall": "Удаление",
        "uninst_name": "Удалить Lights Out",
        "uninst_hint": "Удаляет приложение и убирает наши файлы из папки Bodycam.",
        "uninst_btn": "Удалить…",
        "uninst_title": "Удалить Lights Out?",
        "uninst_body": "Lights Out закроется, папка Bodycam вернётся к исходному виду, и Windows "
                       "удалит приложение.",
        "uninst_files_label": "Удалено из папки Bodycam",
        "uninst_files_none": "В вашей папке Bodycam нет наших файлов.",
        "uninst_keep": "Ваши настройки, загруженные режимы и вход останутся в {dir}, поэтому "
                       "переустановка продолжится с того же места.",
        "uninst_wipe": "Также удалить мои настройки, загруженные режимы и вход",
        "uninst_confirm": "Удалить",
        "uninst_cancel": "Отмена",
        "uninst_working": "Удаление…",
        "uninst_locked": "Нельзя удалить, пока матч подтверждён.",
        "uninst_err_not_installed": "Эта копия установлена не программой установки Lights Out, "
                                    "поэтому Windows нечего удалять.",
        "uninst_err_not_windows": "Деинсталлятор есть только в Windows.",
        "uninst_err_game_running": "Сначала закройте Bodycam — пока игра запущена, наши файлы заняты.",
        "uninst_err_launch_failed": "Windows не удалось запустить деинсталлятор.",
    },
    "zh": {
        "sec_network": "匹配网络",
        "network_region": "地区", "network_select": "选择地区",
        "network_same_region": "默认只匹配同一地区的玩家。",
        "network_cross_short": "跨地区",
        "network_cross_region": "延迟合适时允许跨地区匹配",
        "network_cross_hint": "所有相关玩家都必须启用此选项。始终适用 120 毫秒的延迟上限。",
        "network_preparing": "正在准备中继连接测量…",
        "network_unavailable": "延迟估算不可用",
        "network_ready": "延迟估算已就绪",
        "network_locked": "比赛期间无法更改地区偏好。",
        "region_NA": "北美洲", "region_SA": "南美洲", "region_EU": "欧洲",
        "region_AS": "亚洲", "region_OC": "大洋洲", "region_AF": "非洲", "region_ME": "中东",
        "click_sound": "界面点击音效",
        "click_volume": "点击音量",
        "click_hint": "点击控件时播放短音效，与找到比赛的提示音分开设置。",
        "subtitle": "更改会即时保存",
        "sec_audio": "音频", "sec_game": "游戏", "sec_account": "账户",
        "sec_readiness": "就绪状态",
        "sound_hint": "会盖过游戏声音播放，让你在靶场也能听到。",
        "volume": "音量",
        "path_name": "Bodycam 安装路径",
        "path_detected": "已从 Steam 自动检测",
        "path_manual": "手动设置你的 Bodycam 文件夹",
        "path_placeholder": "你的 Bodycam 文件夹路径",
        "path_set": "设置",
        "lang_name": "语言",
        "acct_meta": "Steam ID {id}",
        "ready_steam": "Steam", "ready_bodycam": "Bodycam", "ready_servers": "服务器",
        "ready_connected": "已连接", "ready_running": "运行中", "ready_ok": "正常",
        "ready_offline": "离线", "ready_not_running": "未运行",
        "ready_signed_out": "已退出", "ready_closed": "已关闭", "ready_close_it": "排队前请先关闭",
        "ready_hint": "中心会把你从靶场带入对局。当你在别处时找到的对局无法开始。",
        "sec_uninstall": "卸载",
        "uninst_name": "卸载 Lights Out",
        "uninst_hint": "移除本程序，并把我们的文件从你的 Bodycam 文件夹里取出来。",
        "uninst_btn": "卸载…",
        "uninst_title": "要卸载 Lights Out 吗？",
        "uninst_body": "Lights Out 会关闭，你的 Bodycam 文件夹恢复原状，Windows 随后移除本程序。",
        "uninst_files_label": "已从你的 Bodycam 文件夹移除",
        "uninst_files_none": "你的 Bodycam 文件夹里没有我们的文件。",
        "uninst_keep": "你的设置、已下载的游戏模式和登录状态会保留在 {dir}，重新安装后可以接着用。",
        "uninst_wipe": "同时删除我的设置、已下载的游戏模式和登录状态",
        "uninst_confirm": "卸载",
        "uninst_cancel": "取消",
        "uninst_working": "正在卸载…",
        "uninst_locked": "已锁定对局期间无法卸载。",
        "uninst_err_not_installed": "这份程序不是由 Lights Out 安装程序放置的，Windows 没有可卸载的项目。",
        "uninst_err_not_windows": "卸载程序只存在于 Windows 上。",
        "uninst_err_game_running": "请先关闭 Bodycam —— 游戏运行时我们的文件正被占用。",
        "uninst_err_launch_failed": "Windows 无法启动卸载程序。",
    },
}


def _strings(lang: str) -> dict:
    """This screen's strings for the active language, English-filled so a missing key can never
    render as the raw key (the same English-fallback rule i18n.py uses)."""
    merged = dict(_S.get(i18n.DEFAULT, {}))
    merged.update(_S.get(lang, {}))
    return merged


# ---------------------------------------------------------------- snapshot helpers
def _mask_steam_id(steam_id: str) -> str:
    """`7656119…8142` — enough to recognise your own account, not the whole 64-bit id."""
    s = str(steam_id or "")
    if len(s) <= 11:
        return s
    return s[:7] + "…" + s[-4:]


def _game_slice(session, panel) -> dict:
    from ...activity import files_locked
    app = getattr(panel, "app", None)
    state = getattr(app, "state", {}) or {}
    # The real WebApp tracks the resolved dir on `app.game_dir`; fall back to state.json, then a
    # fresh detection — the same precedence HubApp/WebApp use at startup.
    path = getattr(app, "game_dir", None) or state.get("game_dir") or ""
    detected = bool(path) and game_mod.is_game_dir(path)
    # "auto" = we found it ourselves (Steam), not a path the user typed/kept. Best-effort: if the
    # persisted dir equals what detection returns now, call it auto-detected.
    auto = False
    if detected:
        try:
            auto = os.path.normpath(str(path)) == (game_mod.find_game_dir() or "")
        except Exception:               # noqa: BLE001 — detection must never crash the snapshot
            auto = False
    window = getattr(panel, "window", None)
    can_browse = window is not None and hasattr(window, "create_file_dialog")
    return {
        "path": str(path),
        "detected": detected,
        "auto_detected": auto,
        # CACHED: this is a label, and the exact answer costs a tasklist spawn (~1 s here).
        "running": bool(game_mod.game_running_cached(game_mod.SNAPSHOT_TTL_SECONDS)),
        "can_browse": can_browse,
        "locked": files_locked(panel),
        "error": getattr(panel, "settings_game_error", "") or "",
    }


def _language_slice() -> dict:
    current = i18n.get_language()
    return {
        "current": current,
        "current_name": i18n.name_of(current),
        "options": [{"code": c, "name": n} for c, n in i18n.LANGUAGES],
    }


def _sound_volume(state: dict) -> int:
    """The slider, 0..100 — sounds.volume_for_state, which is the one rule every view reads
    (absent == default, the legacy on/off flag honoured once) so this screen, the web panel's
    match-found cue and the old Tk panel can never disagree about one persisted setting."""
    return sounds_mod.volume_for_state(state)


def _sound_slice(panel) -> dict:
    state = (getattr(getattr(panel, "app", None), "state", {}) or {})
    vol = _sound_volume(state)
    return {
        "volume": vol,
        "enabled": vol > 0,
        "default": sounds_mod.DEFAULT_VOLUME,
        "can_play": bool(sounds_mod.WINDOWS),
    }


def _account_slice(session) -> dict:
    me = getattr(session, "me", None) or {}
    signed_in = bool(me)
    return {
        "signed_in": signed_in,
        "persona": me.get("name") or "",
        "steam_id": me.get("steam_id") or "",
        "steam_id_masked": _mask_steam_id(me.get("steam_id") or ""),
        "avatar": me.get("avatar") or "",
        "locked": bool(session.locked_in()),
    }


def _readiness_slice(session, panel) -> list:
    """The inline checklist that replaced the shooting-range warning (plan turn-2). Each item is
    {key, state, label_key, value_key}; JS resolves the labels/values through this slice's strings.
    ``state`` is "ok" (green) | "warn" (gold, advisory) | "bad" (accent, blocking)."""
    signed_in = bool(getattr(session, "me", None))
    connected = bool(getattr(session, "connected", True))
    # CACHED: a checklist row, redrawn on every state change. See game.game_running_cached.
    running = bool(game_mod.game_running_cached(game_mod.SNAPSHOT_TTL_SECONDS))
    return [
        {"key": "steam", "label_key": "ready_steam",
         "state": "ok" if signed_in else "bad",
         "value_key": "ready_connected" if signed_in else "ready_signed_out"},
        # CLOSED is the ready state, not running (Sam, 2026-09-15). The hub launches Bodycam
        # itself at the right moment, and it has to: the lobby pak gets exactly ONE lobby search
        # per launch, because BeginPlay fires once per level load and the lobby world has no clock
        # to retry with. A game that was already open has spent that shot, which is the
        # `game_was_open` branch in _maybe_launch_game and the worst screen in the tab. Telling
        # the player "Running - OK" was steering them into it.
        {"key": "bodycam", "label_key": "ready_bodycam",
         "state": "warn" if running else "ok",
         "value_key": "ready_close_it" if running else "ready_closed"},
        {"key": "servers", "label_key": "ready_servers",
         "state": "ok" if connected else "bad",
         "value_key": "ready_ok" if connected else "ready_offline"},
    ]


def _network_slice(session, panel) -> dict:
    """Saved regional preferences plus the worker's honest current readiness.

    An empty saved region means the player must choose. Steam relay markers are opaque, so the
    worker never silently invents or replaces the preference in state.json.
    """
    state = (getattr(getattr(panel, "app", None), "state", {}) or {})
    saved_region = state.get("matchmaking_region", "")
    if saved_region not in ("",) + state_mod.MATCHMAKING_REGIONS:
        saved_region = ""
    cross_region = state.get("matchmaking_cross_region", False)
    if not isinstance(cross_region, bool):
        cross_region = False

    client = getattr(session, "client", None)
    raw = getattr(client, "network_status", None) if client is not None else None
    status = raw if isinstance(raw, dict) else {}
    suggested = status.get("region")
    if suggested not in state_mod.MATCHMAKING_REGIONS:
        suggested = ""
    error = str(status.get("error") or "")
    if client is None:
        readiness = "unavailable"
    elif error:
        readiness = "unavailable"
    elif bool(status.get("ready")):
        readiness = "ready"
    else:
        readiness = "preparing"

    strings = _strings(i18n.get_language())
    return {
        "region": saved_region,
        "cross_region": cross_region,
        "suggested_region": suggested,
        "options": ([{"code": "", "name": strings["network_select"]}] +
                    [{"code": code, "name": strings[f"region_{code}"]}
                     for code in state_mod.MATCHMAKING_REGIONS]),
        "same_region_default": True,
        "status": readiness,
        "error": error,
        "locked": bool(session.locked_in() or
                       str(getattr(session, "phase", "") or "") in
                       ("found", "lobby", "connecting", "live")),
    }


def _game_dir(panel) -> str:
    """The resolved Bodycam folder, with the same precedence _game_slice uses."""
    app = getattr(panel, "app", None)
    state = getattr(app, "state", {}) or {}
    return str(getattr(app, "game_dir", None) or state.get("game_dir") or "")


def _uninstall_slice(session, panel) -> dict:
    """Everything the Uninstall card and its confirm modal need, and nothing it has to guess.

    `game_files` is the honest list of what leaving would take out of the player's Bodycam folder
    (basenames — the folder itself is already on this screen), so the modal shows the actual
    consequence rather than a promise. `reason` is empty when uninstalling is possible and
    otherwise names why not, which is what disables the button: a dev checkout and an unzipped
    dist folder both have no Add/Remove-Programs entry to hand over to."""
    from ...activity import files_locked
    path, reason = uninstall_mod.availability()
    return {
        "available": bool(path),
        "reason": reason,
        "program_dir": uninstall_mod.program_dir(),
        "state_dir": str(paths_mod.state_dir()),
        "game_files": [os.path.basename(p) for p in uninstall_mod.game_files(_game_dir(panel))],
        "confirming": bool(getattr(panel, "settings_uninstall_open", False)),
        "busy": bool(getattr(panel, "settings_uninstall_busy", False)),
        "locked": files_locked(panel),
        "error": getattr(panel, "settings_uninstall_error", "") or "",
    }


@register_snapshot("settings")
def snapshot(session, panel) -> dict:
    """The ``settings`` slice: the detailed controls (game path, language, sound, sign-in identity)
    plus the readiness checklist, the uninstall card and this screen's per-language strings."""
    lang = i18n.get_language()
    return {
        "settings": {
            "game": _game_slice(session, panel),
            "language": _language_slice(),
            "sound": _sound_slice(panel),
            "click_sound": _click_sound_slice(panel),
            "account": _account_slice(session),
            "network": _network_slice(session, panel),
            "readiness": _readiness_slice(session, panel),
            "uninstall": _uninstall_slice(session, panel),
            "strings": _strings(lang),
        }
    }


# ---------------------------------------------------------------- verb implementations
def _apply_game_path(panel, picked: str):
    """Validate and persist a chosen folder, mirroring HubApp._on_browse: accept the game folder
    itself OR a steamapps\\common parent (we look one level down). Runs on the UI thread."""
    from ...activity import files_locked
    if files_locked(panel):
        panel.settings_game_error = i18n.t("comp_files_locked")
        panel.on_change()
        return
    app = getattr(panel, "app", None)
    if app is None or not picked:
        return
    picked = os.path.normpath(str(picked))
    for cand in (picked, os.path.join(picked, "Bodycam")):
        if game_mod.is_game_dir(cand):
            game_dir = os.path.normpath(cand)
            try:
                app.game_dir = game_dir
            except Exception:            # noqa: BLE001 — some app stand-ins are read-only
                pass
            try:
                app.state["game_dir"] = game_dir
                state_mod.reconcile(app.state, game_dir)
                state_mod.update_fields({key: app.state[key] for key in state_mod.INSTALL_FIELDS})
            except Exception:            # noqa: BLE001 — a read-only state dir must not break it
                pass
            panel.settings_game_error = ""
            panel.on_change()
            return
    # not a Bodycam install: surface the same message the Tk browse used, already localized
    panel.settings_game_error = i18n.t("not_game_folder")
    panel.on_change()


def _set_game_path(panel, path=""):
    """Manual-entry fallback (used when no native folder dialog is available): validate a typed
    path on the UI thread."""
    panel.post(lambda: _apply_game_path(panel, str(path or "")))


def _browse_game_path(panel):
    """Open the native folder picker (pywebview) on a worker thread so it never blocks the UI
    thread, then apply the result on the UI thread. A no-op headless (no window)."""
    window = getattr(panel, "window", None)
    if window is None or not hasattr(window, "create_file_dialog"):
        return                           # JS shows the manual path field instead (see can_browse)

    def worker():
        try:
            import webview
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
        except Exception:                # noqa: BLE001 — dialog cancelled / unavailable
            return
        if not result:
            return
        picked = result[0] if isinstance(result, (list, tuple)) else result
        panel.post(lambda: _apply_game_path(panel, picked))

    threading.Thread(target=worker, name="hub-browse", daemon=True).start()


def _set_sound_volume(panel, value=0):
    """Persist the slider and re-render — the exact rule CompetitivePanel.set_sound_volume uses,
    writing the same ``comp_sound_volume`` state key so both views share one setting."""
    def apply():
        vol = sounds_mod.clamp_volume(value)
        app = getattr(panel, "app", None)
        if app is not None:
            try:
                app.state["comp_sound_volume"] = vol
                if vol > 0:
                    app.state["comp_sound_last"] = vol
                state_mod.update_fields({key: app.state[key] for key in ("comp_sound_volume", "comp_sound_last")})
            except Exception:            # noqa: BLE001 — read-only state dir must not break it
                pass
        panel.on_change()
    panel.post(apply)


def _network_change_allowed(session) -> bool:
    """Match commitment freezes network policy; a queue may be cancelled and edited."""
    if session is None:
        return False
    return not (session.locked_in() or
                str(getattr(session, "phase", "") or "") in
                ("found", "lobby", "connecting", "live"))


def _persist_network_preference(panel, key, value):
    def apply():
        session = getattr(panel, "session", None)
        app = getattr(panel, "app", None)
        if app is None or not _network_change_allowed(session):
            return
        if app.state.get(key, "" if key == "matchmaking_region" else False) == value:
            return
        # Changing eligibility while queued must invalidate the current search before the new
        # preference is advertised. LiveSession.cancel_queue is the established leave method.
        if str(getattr(session, "phase", "") or "") in ("checking", "queued"):
            cancel = getattr(session, "cancel_queue", None)
            if callable(cancel):
                cancel()
        app.state[key] = value
        state_mod.update_fields({key: value})
        changed = getattr(getattr(session, "client", None), "network_changed", None)
        if callable(changed):
            changed()
        panel.on_change()
    panel.post(apply)


def _click_sound_slice(panel):
    state = getattr(getattr(panel, "app", None), "state", {}) or {}
    return {"volume": sounds_mod.clamp_volume(state.get("ui_click_volume", 35)),
            "enabled": state.get("ui_click_enabled") is not False}


def _set_click_preference(panel, key, value):
    def apply():
        app = getattr(panel, "app", None)
        if app is not None:
            app.state[key] = value
            try:
                state_mod.update_fields({key: value})
            except OSError:
                pass
        panel.on_change()
    panel.post(apply)


def _set_matchmaking_region(panel, region=""):
    region = str(region or "")
    if region != "" and region not in state_mod.MATCHMAKING_REGIONS:
        return
    _persist_network_preference(panel, "matchmaking_region", region)


def _set_matchmaking_cross_region(panel, enabled=False):
    if not isinstance(enabled, bool):
        return
    _persist_network_preference(panel, "matchmaking_cross_region", enabled)


def _set_click_volume(panel, value=35):
    _set_click_preference(panel, "ui_click_volume", sounds_mod.clamp_volume(value))


def _set_click_enabled(panel, value=True):
    if isinstance(value, bool):
        _set_click_preference(panel, "ui_click_enabled", value)


def _test_sound(panel):
    """Play the cue exactly as a found match would, at the current slider level.

    Literally the same call the found-match edge makes (WebPanel.play_match_found), so Test can
    never again be the only thing in the hub that makes a sound."""
    def play():
        play_it = getattr(panel, "play_match_found", None)
        if callable(play_it):
            play_it()
            return
        state = (getattr(getattr(panel, "app", None), "state", {}) or {})
        sounds_mod.play_match_found(panel.after, _sound_volume(state))
    panel.post(play)


def _sign_out(panel):
    """Sign out via the session verb (which already refuses while a match is locked in, C14)."""
    panel.post(panel.session.sign_out)


# ---------------------------------------------------------------- uninstall
def _open_uninstall(panel):
    """Show the confirm modal. View-only, and it clears a previous error so a second attempt does
    not open onto the last one's message."""
    def apply():
        panel.settings_uninstall_open = True
        panel.settings_uninstall_error = ""
        panel.on_change()
    panel.post(apply)


def _close_uninstall(panel):
    def apply():
        panel.settings_uninstall_open = False
        panel.settings_uninstall_error = ""
        panel.on_change()
    panel.post(apply)


def _uninstall_done(panel, result):
    """Back on the UI thread with hub/uninstall.py's verdict.

    Failure stays in the modal with the reason: nothing has been handed over, so the player is owed
    the message and a second try. Success ends the hub — the Windows uninstaller is already running
    and would otherwise have to force-close us to get at the program folder."""
    panel.settings_uninstall_busy = False
    if not result.get("ok"):
        panel.settings_uninstall_error = result.get("error") or "launch_failed"
        panel.on_change()
        return
    panel.settings_uninstall_open = False
    panel.on_change()
    quit_it = getattr(panel, "quit", None)
    took = bool(quit_it and quit_it())
    # Frozen with no window to close (should not happen) we still must go: the uninstaller is
    # mid-flight. Headless in a test `quit()` returns False and we deliberately stay alive.
    if not took and getattr(sys, "frozen", False):
        os._exit(0)


def _uninstall(panel, wipe_data=False):
    """Uninstall Lights Out: clean the Bodycam folder, hand over to Windows, quit.

    The guards run on the UI thread (they read session state); the work runs on a worker, because
    game_running() spawns tasklist and rmtree of the packs folder is real disk work, and freezing
    the window at the exact moment it is being taken away is the worst possible time for it."""
    def go():
        if getattr(panel, "settings_uninstall_busy", False):
            return                       # already going; a double click must not start two
        session = getattr(panel, "session", None)
        from ...activity import files_locked
        if files_locked(panel):
            # Same rule as sign-out (C14): a match the player is committed to outranks this, and
            # leaving mid-match would strand the other nine.
            panel.settings_uninstall_error = "locked"
            panel.on_change()
            return
        _path, reason = uninstall_mod.availability()
        if reason:
            panel.settings_uninstall_error = reason
            panel.on_change()
            return
        panel.settings_uninstall_busy = True
        panel.settings_uninstall_error = ""
        panel.on_change()
        game_dir, wipe = _game_dir(panel), bool(wipe_data)

        def worker():
            try:
                result = uninstall_mod.perform(game_dir, wipe_data=wipe)
            except Exception:            # noqa: BLE001 — never leave the modal spinning
                result = {"ok": False, "error": "launch_failed"}
            panel.post(lambda: _uninstall_done(panel, result))

        threading.Thread(target=worker, name="hub-uninstall", daemon=True).start()
    panel.post(go)


# Verbs are 1:1 passthroughs run on the UI thread. Names are settings-scoped to avoid colliding
# with the core verbs (set_language is the core's; the JS calls it directly for the language
# picker) or another screen's verbs.
register_verbs("settings", {
    "settings_browse_game_path": _browse_game_path,
    "settings_set_game_path":    _set_game_path,
    "settings_set_sound_volume": _set_sound_volume,
    "settings_set_matchmaking_region": _set_matchmaking_region,
    "settings_set_matchmaking_cross_region": _set_matchmaking_cross_region,
    "settings_test_sound":       _test_sound,
    "settings_set_click_volume": _set_click_volume,
    "settings_set_click_enabled": _set_click_enabled,
    "settings_sign_out":         _sign_out,
    "settings_open_uninstall":   _open_uninstall,
    "settings_close_uninstall":  _close_uninstall,
    "settings_uninstall":        _uninstall,
})
