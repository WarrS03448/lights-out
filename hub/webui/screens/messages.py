"""Private inbox display; all sender, friendship and read permissions live on the server."""
from ... import i18n
from . import register_snapshot, register_verbs
_KEYS = "title unread empty select compose send refresh older block unblock official official_note friends_only blocked rate_limited inbox_full unavailable signin new_message retention you".split()
_VALUES = {
 "en": ["Messages", "unread", "No messages yet.", "Choose a conversation or message a friend.", "Write a message…", "Send", "Refresh", "Older messages", "Block player", "Unblock player", "Lights Out Admin", "Official message from Lights Out. For event questions, use Tournament support.", "You must be mutual friends to send messages.", "Messaging is blocked between these accounts.", "Too many messages. Please wait a minute.", "This inbox has reached its conversation limit.", "Messages are unavailable. Your draft is still here; try again.", "Sign in to view your messages.", "Message a friend", "The latest 500 messages per conversation are retained.", "You"],
 "de": ["Nachrichten", "ungelesen", "Noch keine Nachrichten.", "Wähle einen Chat oder schreibe einem Freund.", "Nachricht schreiben…", "Senden", "Aktualisieren", "Ältere Nachrichten", "Spieler blockieren", "Spieler entsperren", "Lights Out Admin", "Offizielle Nachricht von Lights Out. Bei Event-Fragen nutze den Turnier-Support.", "Zum Schreiben müsst ihr miteinander befreundet sein.", "Nachrichten zwischen diesen Konten sind blockiert.", "Zu viele Nachrichten. Bitte eine Minute warten.", "Das Limit für Unterhaltungen wurde erreicht.", "Nachrichten nicht verfügbar. Dein Entwurf bleibt erhalten; versuche es erneut.", "Melde dich an, um Nachrichten zu sehen.", "Freund anschreiben", "Die letzten 500 Nachrichten pro Unterhaltung werden gespeichert.", "Du"],
 "es": ["Mensajes", "sin leer", "Aún no hay mensajes.", "Elige una conversación o escribe a un amigo.", "Escribe un mensaje…", "Enviar", "Actualizar", "Mensajes anteriores", "Bloquear jugador", "Desbloquear jugador", "Administración de Lights Out", "Mensaje oficial de Lights Out. Para preguntas del evento, usa el soporte del torneo.", "Deben ser amigos mutuos para enviar mensajes.", "Los mensajes entre estas cuentas están bloqueados.", "Demasiados mensajes. Espera un minuto.", "Esta bandeja alcanzó el límite de conversaciones.", "Mensajes no disponibles. Tu borrador se conserva; inténtalo de nuevo.", "Inicia sesión para ver tus mensajes.", "Escribir a un amigo", "Se conservan los últimos 500 mensajes por conversación.", "Tú"],
 "fr": ["Messages", "non lus", "Aucun message pour le moment.", "Choisissez une conversation ou écrivez à un ami.", "Écrivez un message…", "Envoyer", "Actualiser", "Messages précédents", "Bloquer le joueur", "Débloquer le joueur", "Administration Lights Out", "Message officiel de Lights Out. Pour les questions sur l’événement, utilisez l’assistance du tournoi.", "Vous devez être amis pour échanger des messages.", "Les messages entre ces comptes sont bloqués.", "Trop de messages. Patientez une minute.", "La limite de conversations est atteinte.", "Messages indisponibles. Votre brouillon est conservé ; réessayez.", "Connectez-vous pour voir vos messages.", "Écrire à un ami", "Les 500 derniers messages de chaque conversation sont conservés.", "Vous"],
 "pt": ["Mensagens", "não lidas", "Nenhuma mensagem ainda.", "Escolha uma conversa ou escreva para um amigo.", "Escreva uma mensagem…", "Enviar", "Atualizar", "Mensagens anteriores", "Bloquear jogador", "Desbloquear jogador", "Administração Lights Out", "Mensagem oficial de Lights Out. Para dúvidas do evento, use o suporte do torneio.", "Vocês precisam ser amigos para enviar mensagens.", "As mensagens entre estas contas estão bloqueadas.", "Muitas mensagens. Aguarde um minuto.", "Esta caixa atingiu o limite de conversas.", "Mensagens indisponíveis. Seu rascunho foi mantido; tente novamente.", "Entre para ver suas mensagens.", "Escrever para um amigo", "As últimas 500 mensagens de cada conversa são mantidas.", "Você"],
 "ru": ["Сообщения", "непрочитанных", "Сообщений пока нет.", "Выберите беседу или напишите другу.", "Напишите сообщение…", "Отправить", "Обновить", "Ранние сообщения", "Заблокировать игрока", "Разблокировать игрока", "Администрация Lights Out", "Официальное сообщение Lights Out. По вопросам события используйте поддержку турнира.", "Для отправки сообщений вы должны быть друзьями.", "Сообщения между этими аккаунтами заблокированы.", "Слишком много сообщений. Подождите минуту.", "Достигнут лимит бесед.", "Сообщения недоступны. Черновик сохранён; попробуйте снова.", "Войдите, чтобы увидеть сообщения.", "Написать другу", "Хранятся последние 500 сообщений каждой беседы.", "Вы"],
 "zh": ["消息", "未读", "暂无消息。", "选择对话或给好友发消息。", "输入消息…", "发送", "刷新", "更早的消息", "屏蔽玩家", "取消屏蔽", "Lights Out 管理员", "来自 Lights Out 的官方消息。活动问题请使用锦标赛支持。", "只有互为好友才能发送消息。", "这些账号之间的消息已被屏蔽。", "发送过于频繁，请稍等一分钟。", "此收件箱已达到对话数量上限。", "消息暂不可用。草稿已保留，请重试。", "登录后查看消息。", "给好友发消息", "每个对话保留最近 500 条消息。", "你"],
}
STRINGS = {code: dict(zip(_KEYS, values, strict=True)) for code, values in _VALUES.items()}
_REFUNDS = {
    "en": "{amount} RR refunded in {mode} after a cheating ban for {cheaters}.",
    "de": "{amount} RR in {mode} nach einer Cheating-Sperre für {cheaters} erstattet.",
    "es": "Se devolvieron {amount} RR en {mode} tras la expulsión por trampas de {cheaters}.",
    "fr": "{amount} RR remboursés en {mode} après le bannissement pour triche de {cheaters}.",
    "pt": "{amount} RR devolvidos em {mode} após o banimento por trapaça de {cheaters}.",
    "ru": "Возвращено {amount} RR в {mode} после блокировки за читы: {cheaters}.",
    "zh": "因 {cheaters} 作弊被封禁，你在 {mode} 中获退 {amount} RR。",
}
for _code, _text in _REFUNDS.items():
    STRINGS[_code]["rr_refund"] = _text


def _refund_text(context, fallback):
    if not isinstance(context, dict) or context.get("type") != "rr_refund":
        return fallback
    mode, amount, names = context.get("mode"), context.get("amount"), context.get("cheaters")
    if mode not in ("BB1", "BB5") or type(amount) is not int or amount <= 0 or not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        return fallback
    return _REFUNDS.get(i18n.get_language(), _REFUNDS["en"]).format(
        amount=amount, mode="1v1 Bodybomb" if mode == "BB1" else "5v5 Bodybomb", cheaters=", ".join(names))


def _localize(data, rows_key, context_key, text_key):
    if not isinstance(data, dict):
        return data
    return {**data, rows_key: [{**row, text_key: _refund_text(row.get(context_key), row.get(text_key, ""))}
                              for row in data.get(rows_key, [])]}

@register_snapshot("messages")
def snapshot(session, panel):
    return {"messages": {"signed_in": bool(getattr(session, "me", None)),
        "identity": (getattr(session, "me", None) or {}).get("player_id") or (getattr(session, "me", None) or {}).get("steam_id") or "",
        "data": _localize(getattr(session, "messages_data", None), "threads", "last_context", "last_text"), "target": getattr(session, "messages_target", ""),
        "thread": _localize(getattr(session, "messages_thread", None), "messages", "context", "text"), "loading": getattr(session, "messages_loading", False),
        "thread_loading": getattr(session, "messages_thread_loading", False), "sending": getattr(session, "messages_sending", False),
        "error": getattr(session, "messages_error", ""), "seq": getattr(session, "messages_send_seq", 0),
        "sent": getattr(session, "messages_sent", None),
        "strings": STRINGS.get(i18n.get_language(), STRINGS["en"])}}

register_verbs("messages", {
    "messages_refresh": lambda panel: panel.post(panel.session.refresh_messages),
    "messages_open": lambda panel, target, before=None: panel.post(lambda: panel.session.open_messages(target, before)),
    "messages_send": lambda panel, text: panel.post(lambda: panel.session.send_private_message(text)),
    "messages_read": lambda panel, target, through: panel.post(lambda: panel.session.mark_messages_read(target, through)),
    "messages_block": lambda panel, target, blocked=True: panel.post(lambda: panel.session.block_message_player(target, blocked)),
})
