"""The Friends screen: your code, your list, and the requests either way.

Sam, 2026-09-15: "add a friend system ... have the user's unique friend code that works similar to
the party code where you can hide it, copy it, or generate a new friend code. another user can
enter this friend code and send a friend request where the requested user then has to accept. lets
keep the friend data tied to the user's steam account so if they log in somewhere else they still
have their friends list."

EVERYTHING HERE IS A VIEW. The server owns the list (server/live.cjs, keyed by SteamID64 in
Upstash so it follows the account rather than the machine); this module renders whatever the last
fetch returned and sends verbs. Nothing is decided locally, which is why there is no optimistic
update anywhere: a friendship has two sides and only the server can see both.

THE CODE IS HIDDEN BY DEFAULT. A player who is streaming has their hub on screen by definition,
and a visible friend code is an inbox anyone watching can fill. Copy still works while hidden -
the same rule the party code follows - so hiding it costs nothing.
"""
from . import register_snapshot, register_verbs
from ... import i18n
from ...competitive import MAX_PARTY

_STRINGS = {
    "en": {
        "title": "Friends",
        "minimize": "Minimize friends",
        "actions_for": "Actions for {name}",
        "signed_out": "Sign in to see your friends and share your friend code.",
        "code_label": "Your friend code",
        "code_hint": "Give this to someone so they can add you. It is not your Steam ID.",
        "show": "Show",
        "hide": "Hide",
        "copy": "Copy",
        "copied": "Copied",
        "new_code": "New code",
        "new_code_hint": "A new code stops the old one working straight away.",
        "add_label": "Add by friend code",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "Send request",
        "list": "Friends",
        "incoming": "Requests",
        "outgoing": "Sent",
        "accept": "Accept",
        "decline": "Decline",
        "cancel": "Cancel",
        "remove": "Remove",
        "invite": "Invite",
        "invited": "Invited",
        "in_party": "In your party",
        "online": "Online",
        "offline": "Offline",
        "empty": "No friends yet. Share your code, or add someone you just played with.",
        "no_incoming": "No pending requests.",
        "no_outgoing": "Nothing sent.",
        "count": "{n} of {max}",
        "refresh": "Refresh"
    },
    "de": {
        "title": "Freunde",
        "minimize": "Freunde minimieren",
        "actions_for": "Aktionen für {name}",
        "signed_out": "Melde dich an, um deine Freunde zu sehen und deinen Freundescode zu teilen.",
        "code_label": "Dein Freundescode",
        "code_hint": "Teile diesen Code, damit andere dich hinzufügen können. Er ist nicht deine Steam-ID.",
        "show": "Anzeigen",
        "hide": "Verbergen",
        "copy": "Kopieren",
        "copied": "Kopiert",
        "new_code": "Neuer Code",
        "new_code_hint": "Ein neuer Code macht den alten sofort ungültig.",
        "add_label": "Per Freundescode hinzufügen",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "Anfrage senden",
        "list": "Freunde",
        "incoming": "Anfragen",
        "outgoing": "Gesendet",
        "accept": "Annehmen",
        "decline": "Ablehnen",
        "cancel": "Abbrechen",
        "remove": "Entfernen",
        "invite": "Einladen",
        "invited": "Eingeladen",
        "in_party": "In deiner Gruppe",
        "online": "Online",
        "offline": "Offline",
        "empty": "Noch keine Freunde. Teile deinen Code oder füge jemanden aus deinem letzten Match hinzu.",
        "no_incoming": "Keine offenen Anfragen.",
        "no_outgoing": "Nichts gesendet.",
        "count": "{n} von {max}",
        "refresh": "Aktualisieren"
    },
    "es": {
        "title": "Amigos",
        "minimize": "Minimizar amigos",
        "actions_for": "Acciones para {name}",
        "signed_out": "Inicia sesión para ver a tus amigos y compartir tu código.",
        "code_label": "Tu código de amigo",
        "code_hint": "Compártelo para que puedan agregarte. No es tu ID de Steam.",
        "show": "Mostrar",
        "hide": "Ocultar",
        "copy": "Copiar",
        "copied": "Copiado",
        "new_code": "Nuevo código",
        "new_code_hint": "Un código nuevo invalida el anterior de inmediato.",
        "add_label": "Agregar por código de amigo",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "Enviar solicitud",
        "list": "Amigos",
        "incoming": "Solicitudes",
        "outgoing": "Enviadas",
        "accept": "Aceptar",
        "decline": "Rechazar",
        "cancel": "Cancelar",
        "remove": "Eliminar",
        "invite": "Invitar",
        "invited": "Invitado",
        "in_party": "En tu grupo",
        "online": "En línea",
        "offline": "Desconectado",
        "empty": "Aún no tienes amigos. Comparte tu código o agrega a alguien con quien acabas de jugar.",
        "no_incoming": "No hay solicitudes pendientes.",
        "no_outgoing": "No has enviado nada.",
        "count": "{n} de {max}",
        "refresh": "Actualizar"
    },
    "fr": {
        "title": "Amis",
        "minimize": "Réduire les amis",
        "actions_for": "Actions pour {name}",
        "signed_out": "Connecte-toi pour voir tes amis et partager ton code ami.",
        "code_label": "Ton code ami",
        "code_hint": "Partage-le pour que l’on puisse t’ajouter. Ce n’est pas ton identifiant Steam.",
        "show": "Afficher",
        "hide": "Masquer",
        "copy": "Copier",
        "copied": "Copié",
        "new_code": "Nouveau code",
        "new_code_hint": "Un nouveau code invalide immédiatement l’ancien.",
        "add_label": "Ajouter par code ami",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "Envoyer la demande",
        "list": "Amis",
        "incoming": "Demandes",
        "outgoing": "Envoyées",
        "accept": "Accepter",
        "decline": "Refuser",
        "cancel": "Annuler",
        "remove": "Retirer",
        "invite": "Inviter",
        "invited": "Invité",
        "in_party": "Dans ton groupe",
        "online": "En ligne",
        "offline": "Hors ligne",
        "empty": "Pas encore d’amis. Partage ton code ou ajoute une personne avec qui tu viens de jouer.",
        "no_incoming": "Aucune demande en attente.",
        "no_outgoing": "Aucun envoi.",
        "count": "{n} sur {max}",
        "refresh": "Actualiser"
    },
    "pt": {
        "title": "Amigos",
        "minimize": "Minimizar amigos",
        "actions_for": "Ações para {name}",
        "signed_out": "Entre para ver seus amigos e compartilhar seu código.",
        "code_label": "Seu código de amigo",
        "code_hint": "Compartilhe para que possam adicionar você. Não é seu ID do Steam.",
        "show": "Mostrar",
        "hide": "Ocultar",
        "copy": "Copiar",
        "copied": "Copiado",
        "new_code": "Novo código",
        "new_code_hint": "Um novo código invalida o anterior imediatamente.",
        "add_label": "Adicionar pelo código de amigo",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "Enviar solicitação",
        "list": "Amigos",
        "incoming": "Solicitações",
        "outgoing": "Enviadas",
        "accept": "Aceitar",
        "decline": "Recusar",
        "cancel": "Cancelar",
        "remove": "Remover",
        "invite": "Convidar",
        "invited": "Convidado",
        "in_party": "No seu grupo",
        "online": "Online",
        "offline": "Offline",
        "empty": "Ainda não há amigos. Compartilhe seu código ou adicione alguém com quem acabou de jogar.",
        "no_incoming": "Nenhuma solicitação pendente.",
        "no_outgoing": "Nada enviado.",
        "count": "{n} de {max}",
        "refresh": "Atualizar"
    },
    "ru": {
        "title": "Друзья",
        "minimize": "Свернуть список друзей",
        "actions_for": "Действия для {name}",
        "signed_out": "Войдите, чтобы увидеть друзей и поделиться кодом друга.",
        "code_label": "Ваш код друга",
        "code_hint": "Поделитесь им, чтобы вас могли добавить. Это не ваш Steam ID.",
        "show": "Показать",
        "hide": "Скрыть",
        "copy": "Копировать",
        "copied": "Скопировано",
        "new_code": "Новый код",
        "new_code_hint": "Новый код сразу делает прежний недействительным.",
        "add_label": "Добавить по коду друга",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "Отправить запрос",
        "list": "Друзья",
        "incoming": "Запросы",
        "outgoing": "Отправленные",
        "accept": "Принять",
        "decline": "Отклонить",
        "cancel": "Отменить",
        "remove": "Удалить",
        "invite": "Пригласить",
        "invited": "Приглашён",
        "in_party": "В вашей группе",
        "online": "В сети",
        "offline": "Не в сети",
        "empty": "Друзей пока нет. Поделитесь кодом или добавьте недавнего товарища по игре.",
        "no_incoming": "Нет ожидающих запросов.",
        "no_outgoing": "Ничего не отправлено.",
        "count": "{n} из {max}",
        "refresh": "Обновить"
    },
    "zh": {
        "title": "好友",
        "minimize": "收起好友",
        "actions_for": "{name} 的操作",
        "signed_out": "登录后可查看好友并分享好友代码。",
        "code_label": "你的好友代码",
        "code_hint": "分享此代码，让其他人添加你。这不是你的 Steam ID。",
        "show": "显示",
        "hide": "隐藏",
        "copy": "复制",
        "copied": "已复制",
        "new_code": "新代码",
        "new_code_hint": "生成新代码后，旧代码立即失效。",
        "add_label": "通过好友代码添加",
        "add_placeholder": "XXXXX-XXXXX",
        "add": "发送请求",
        "list": "好友",
        "incoming": "请求",
        "outgoing": "已发送",
        "accept": "接受",
        "decline": "拒绝",
        "cancel": "取消",
        "remove": "移除",
        "invite": "邀请",
        "invited": "已邀请",
        "in_party": "在你的队伍中",
        "online": "在线",
        "offline": "离线",
        "empty": "还没有好友。分享你的代码，或添加刚一起玩过的玩家。",
        "no_incoming": "没有待处理的请求。",
        "no_outgoing": "尚未发送。",
        "count": "{n}/{max}",
        "refresh": "刷新"
    }
}


def strings_for(lang: str) -> dict:
    """This screen's strings. English is the source; a missing language falls back to it whole,
    the same rule the other screens use."""
    return dict(_STRINGS.get(lang) or _STRINGS["en"])


def _party_member_ids(session) -> set:
    """Who is already sitting with me. Off session.party, which is the server's roster."""
    party = getattr(session, "party", None) or {}
    ids = set()
    for member in (party.get("members") or ()):
        if isinstance(member, dict) and member.get("steam_id"):
            ids.add(str(member["steam_id"]))
    return ids


def _friend_rows(session) -> list:
    """The friends list, each row carrying whether it can be invited to a party RIGHT NOW.

    Sam, 2026-09-16: "add an invite button to the left of remove to invite them to a party. if
    the inviter isnt already in a party, create a party and then send the invite." So being solo
    is NOT a reason the button cannot be pressed - it is the ordinary case, and the party is made
    on the way. What does stop it: the friend is offline (the invite is delivered over their own
    stream and would evaporate), they are already in the party, the party is full, or we are
    queued or in a match, where no party can be minted at all.

    Every friend is still listed and the button is disabled rather than hidden, for the same
    reason the Competitive picker lists the offline half: a row that vanishes answers "where is
    she?" with nothing at all.
    """
    member_ids = _party_member_ids(session)
    sent = set(getattr(session, "invite_sent", ()) or ())
    party = getattr(session, "party", None)
    can_party = bool(party) or getattr(session, "phase", "") == "idle"
    full = bool(party) and len(member_ids) >= MAX_PARTY
    rows = []
    for friend in (getattr(session, "friends", ()) or ()):
        if not isinstance(friend, dict):
            continue
        row = dict(friend)
        steam_id = str(friend.get("steam_id") or "")
        in_party = steam_id in member_ids
        row["in_party"] = in_party
        row["invited"] = steam_id in sent and not in_party
        row["can_invite"] = (bool(steam_id) and bool(friend.get("online"))
                             and can_party and not in_party and not full)
        rows.append(row)
    return rows


@register_snapshot("friends")
def snapshot(session, panel) -> dict:
    lang = i18n.get_language()
    hidden = bool(getattr(session, "friend_code_hidden", True))
    code = str(getattr(session, "friend_code", "") or "")
    friends = _friend_rows(session)
    return {
        "friends": {
            "strings": strings_for(lang),
            # The code is sent even while hidden, because Copy has to work without revealing it -
            # the JS masks it for display only. That is the same trade the party code makes: the
            # value is already on this machine, and the thing being protected is the SCREEN.
            "code": code,
            "code_hidden": hidden,
            "code_masked": ("-".join("•" * len(part) for part in code.split("-"))
                            if code else ""),
            "list": friends,
            "incoming": list(getattr(session, "friend_requests_in", ()) or ()),
            "outgoing": list(getattr(session, "friend_requests_out", ()) or ()),
            "loading": bool(getattr(session, "friends_loading", False)),
            "error": str(getattr(session, "friends_error", "") or ""),
            # The invite's own line. Separate from `error` because a refused invite says nothing
            # about the list, and the list's error must not be cleared by one.
            "invite_error": str(getattr(session, "invite_error", "") or ""),
            "seq": int(getattr(session, "friends_seq", 0) or 0),
            "max": 200,
            "signed_in": bool(getattr(session, "me", None)),
        }
    }


register_verbs("friends", {
    "friends_refresh":     lambda panel: panel.post(panel.session.refresh_friends),
    "friend_add":          lambda panel, code="": panel.post(lambda: panel.session.add_friend(str(code or ""))),
    "friend_accept":       lambda panel, sid="": panel.post(lambda: panel.session.accept_friend(str(sid or ""))),
    "friend_decline":      lambda panel, sid="": panel.post(lambda: panel.session.decline_friend(str(sid or ""))),
    "friend_cancel":       lambda panel, sid="": panel.post(lambda: panel.session.cancel_friend(str(sid or ""))),
    "friend_remove":       lambda panel, sid="": panel.post(lambda: panel.session.remove_friend(str(sid or ""))),
    # One verb for both halves: the session mints the party when there is none (see
    # LiveSession.invite_friend_to_party). The screen must not do it in two calls - the second
    # would have to guess when the first had landed.
    "friend_invite":       lambda panel, sid="": panel.post(lambda: panel.session.invite_friend_to_party(str(sid or ""))),
    "friend_code_new":     lambda panel: panel.post(panel.session.refresh_friend_code),
    "friend_code_toggle":  lambda panel: panel.post(panel.session.toggle_friend_code_hidden),
})
