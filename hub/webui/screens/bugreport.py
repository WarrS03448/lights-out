"""Bug report screen — the Python half: its snapshot slice, its bridge verbs, and its strings.

OWNED BY THE BUG REPORT SCREEN. Sam, 2026-09-16: "the settings button at the top - let's put
another button that's called Bug report, and it opens a screen that enables the user to report a
bug. That bug will then get sent to the admin console with their steam id and name. Only allow the
user to send 1 bug every 5 seconds."

WHO IT IS FROM IS NOT A FIELD. The steam id and the persona come off the signed-in account
(``session.me``), travel with the bearer token, and are stamped onto the record by the SERVER
(server/live.cjs submitBugReport) - the page never sends them. A box labelled "your Steam ID" is a
box to type somebody else's into, and the one thing a bug report has to be good for is going back
to the person who filed it. The screen shows who it will be sent as, so nobody is surprised.

THE FIVE-SECOND LIMIT IS NOT DRAWN HERE. It is enforced in the SESSION
(hub/competitive.py send_bug_report / bug_cooldown_left) and again on the server, and this slice
only reports how much of it is left so the page can say so. A limit that lives in the page is a
limit a reload clears, and there are two UIs over this one session.

Nothing here is authoritative: the verbs are 1:1 passthroughs onto the session on the UI thread,
exactly like every other screen in this package. No pywebview and no Tk, so it stays testable
headless (tests/test_screen_bugreport.py).
"""
from . import register_snapshot, register_verbs
from ... import competitive as comp
from ... import i18n


# ---------------------------------------------------------------- per-language strings
# Only the strings i18n.py does not already carry; `nav_bugreport` DOES live in i18n.py (the core
# nav draws it) and is looked up by the JS through ctx.t(). Every language below has the same key
# set, so the app's 7-language coverage is unchanged.
_S = {
    "en": {
        "subtitle": "Something broken? Tell us what happened.",
        "label": "What went wrong?",
        "placeholder": "What were you doing, and what did Lights Out do instead?",
        "send": "Send report",
        "sending": "Sending…",
        "wait": "Wait {n}s",
        "sent": "Thanks — your report is with us.",
        "sent_again": "Send another",
        "as": "Sent as {name}",
        "as_id": "Steam ID {id}",
        "privacy": "We receive your Steam name and ID, your hub version and what you write. "
                   "Nothing else.",
        "left": "{n} characters left",
        "limit": "One report every {n} seconds.",
        "err_empty": "Write what went wrong first.",
        "err_signed_out": "Sign in with Steam to send a bug report.",
        "err_too_fast": "One report every {n} seconds — try again in a moment.",
        "err_failed": "That could not be sent. Try again in a moment.",
    },
    "de": {
        "subtitle": "Etwas kaputt? Sag uns, was passiert ist.",
        "label": "Was ist schiefgelaufen?",
        "placeholder": "Was hast du gemacht, und was hat Lights Out stattdessen getan?",
        "send": "Melden",
        "sending": "Wird gesendet…",
        "wait": "Noch {n}s",
        "sent": "Danke — deine Meldung ist bei uns.",
        "sent_again": "Weitere senden",
        "as": "Gesendet als {name}",
        "as_id": "Steam-ID {id}",
        "privacy": "Wir erhalten deinen Steam-Namen und deine ID, deine Hub-Version und deinen "
                   "Text. Sonst nichts.",
        "left": "Noch {n} Zeichen",
        "limit": "Eine Meldung alle {n} Sekunden.",
        "err_empty": "Schreib zuerst, was schiefgelaufen ist.",
        "err_signed_out": "Melde dich mit Steam an, um einen Fehler zu melden.",
        "err_too_fast": "Eine Meldung alle {n} Sekunden — versuch es gleich noch einmal.",
        "err_failed": "Das konnte nicht gesendet werden. Versuch es gleich noch einmal.",
    },
    "es": {
        "subtitle": "¿Algo va mal? Cuéntanos qué ha pasado.",
        "label": "¿Qué ha fallado?",
        "placeholder": "¿Qué estabas haciendo y qué hizo Lights Out en su lugar?",
        "send": "Enviar informe",
        "sending": "Enviando…",
        "wait": "Espera {n}s",
        "sent": "Gracias: ya tenemos tu informe.",
        "sent_again": "Enviar otro",
        "as": "Enviado como {name}",
        "as_id": "ID de Steam {id}",
        "privacy": "Recibimos tu nombre e ID de Steam, tu versión del hub y lo que escribas. "
                   "Nada más.",
        "left": "Quedan {n} caracteres",
        "limit": "Un informe cada {n} segundos.",
        "err_empty": "Escribe primero qué ha fallado.",
        "err_signed_out": "Inicia sesión con Steam para enviar un informe.",
        "err_too_fast": "Un informe cada {n} segundos: inténtalo en un momento.",
        "err_failed": "No se ha podido enviar. Inténtalo en un momento.",
    },
    "fr": {
        "subtitle": "Quelque chose ne va pas ? Dis-nous ce qui s'est passé.",
        "label": "Qu'est-ce qui a échoué ?",
        "placeholder": "Que faisais-tu, et qu'a fait Lights Out à la place ?",
        "send": "Envoyer",
        "sending": "Envoi…",
        "wait": "Encore {n}s",
        "sent": "Merci — ton signalement nous est parvenu.",
        "sent_again": "En envoyer un autre",
        "as": "Envoyé en tant que {name}",
        "as_id": "ID Steam {id}",
        "privacy": "Nous recevons ton nom et ton ID Steam, ta version du hub et ce que tu écris. "
                   "Rien d'autre.",
        "left": "{n} caractères restants",
        "limit": "Un signalement toutes les {n} secondes.",
        "err_empty": "Écris d'abord ce qui n'a pas marché.",
        "err_signed_out": "Connecte-toi avec Steam pour signaler un bug.",
        "err_too_fast": "Un signalement toutes les {n} secondes — réessaie dans un instant.",
        "err_failed": "Impossible d'envoyer. Réessaie dans un instant.",
    },
    "pt": {
        "subtitle": "Algo avariado? Diz-nos o que aconteceu.",
        "label": "O que correu mal?",
        "placeholder": "O que estavas a fazer e o que fez o Lights Out em vez disso?",
        "send": "Enviar relatório",
        "sending": "A enviar…",
        "wait": "Espera {n}s",
        "sent": "Obrigado — o teu relatório chegou.",
        "sent_again": "Enviar outro",
        "as": "Enviado como {name}",
        "as_id": "ID Steam {id}",
        "privacy": "Recebemos o teu nome e ID de Steam, a tua versão do hub e o que escreveres. "
                   "Mais nada.",
        "left": "Faltam {n} caracteres",
        "limit": "Um relatório a cada {n} segundos.",
        "err_empty": "Escreve primeiro o que correu mal.",
        "err_signed_out": "Inicia sessão com a Steam para enviar um relatório.",
        "err_too_fast": "Um relatório a cada {n} segundos — tenta daqui a pouco.",
        "err_failed": "Não foi possível enviar. Tenta daqui a pouco.",
    },
    "ru": {
        "subtitle": "Что-то сломалось? Расскажите, что случилось.",
        "label": "Что пошло не так?",
        "placeholder": "Что вы делали и что вместо этого сделал Lights Out?",
        "send": "Отправить",
        "sending": "Отправка…",
        "wait": "Ещё {n} с",
        "sent": "Спасибо — отчёт получен.",
        "sent_again": "Отправить ещё",
        "as": "Отправлено от имени {name}",
        "as_id": "Steam ID {id}",
        "privacy": "Мы получаем ваше имя и ID в Steam, версию хаба и ваш текст. Больше ничего.",
        "left": "Осталось символов: {n}",
        "limit": "Один отчёт раз в {n} с.",
        "err_empty": "Сначала напишите, что пошло не так.",
        "err_signed_out": "Войдите через Steam, чтобы отправить отчёт.",
        "err_too_fast": "Один отчёт раз в {n} с — попробуйте через мгновение.",
        "err_failed": "Отправить не удалось. Попробуйте через мгновение.",
    },
    "zh": {
        "subtitle": "出问题了？告诉我们发生了什么。",
        "label": "哪里出错了？",
        "placeholder": "你当时在做什么，Lights Out 又做了什么？",
        "send": "发送报告",
        "sending": "发送中…",
        "wait": "还需 {n} 秒",
        "sent": "谢谢，我们已收到你的报告。",
        "sent_again": "再发一条",
        "as": "以 {name} 的身份发送",
        "as_id": "Steam ID {id}",
        "privacy": "我们会收到你的 Steam 名称和 ID、你的 hub 版本以及你写的内容，仅此而已。",
        "left": "还可输入 {n} 个字符",
        "limit": "每 {n} 秒只能发送一条。",
        "err_empty": "请先写下出了什么问题。",
        "err_signed_out": "请先用 Steam 登录再发送报告。",
        "err_too_fast": "每 {n} 秒只能发送一条 — 请稍候再试。",
        "err_failed": "发送失败，请稍候再试。",
    },
}


def _strings(lang: str) -> dict:
    """This screen's strings for the active language, English-filled so a missing key can never
    render as the raw key (the same English-fallback rule i18n.py uses)."""
    merged = dict(_S.get(i18n.DEFAULT, {}))
    merged.update(_S.get(lang, {}))
    return merged


def _cooldown_ms(session) -> int:
    """How much of the five seconds is left, in whole milliseconds.

    Read from the SESSION, never recomputed here: hub/competitive.py owns the rule and the server
    owns the last word on it."""
    left = getattr(session, "bug_cooldown_left", None)
    try:
        return int(max(0.0, float(left())) * 1000) if callable(left) else 0
    except Exception:                    # noqa: BLE001 — a clock must never break the snapshot
        return 0


@register_snapshot("bugreport")
def snapshot(session, panel) -> dict:
    """The ``bugreport`` slice: who the report goes as, what is in the box, and where the send
    stands (in flight / filed / refused, and how long the cooldown has left to run)."""
    me = getattr(session, "me", None) or {}
    return {
        "bugreport": {
            "signed_in": bool(me),
            "persona": me.get("name") or "",
            "steam_id": me.get("steam_id") or "",
            # The draft lives on the session rather than in the page, so switching to Settings and
            # back does not throw away what somebody has half-written.
            "text": str(getattr(session, "bug_text", "") or ""),
            # Which box this is. It changes only when a report is FILED, and the page hangs the
            # textarea's id off it so the filed text cannot be restored into the empty box.
            "seq": int(getattr(session, "bug_seq", 0) or 0),
            "max": comp.BUG_TEXT_MAX,
            "sending": bool(getattr(session, "bug_sending", False)),
            "sent": bool(getattr(session, "bug_sent", False)),
            # "" | empty | signed_out | too_fast | failed | whatever the server called it
            "error": str(getattr(session, "bug_error", "") or ""),
            "cooldown_ms": _cooldown_ms(session),
            "cooldown_seconds": int(comp.BUG_COOLDOWN_SECONDS),
            "strings": _strings(i18n.get_language()),
        }
    }


# ---------------------------------------------------------------- verb implementations
def _set_text(panel, text=""):
    """Keep the draft. Called on blur/change rather than per keystroke - see the JS for why."""
    def apply():
        setter = getattr(panel.session, "set_bug_text", None)
        if callable(setter):
            setter(text)
    panel.post(apply)


def _send(panel, text=""):
    """Send it. Every guard (empty, signed out, too soon) lives in the session method."""
    def apply():
        send = getattr(panel.session, "send_bug_report", None)
        if callable(send):
            send(text)
    panel.post(apply)


# Verb names are screen-scoped so they cannot collide with the core verbs or another screen's.
register_verbs("bugreport", {
    "bug_set_text": _set_text,
    "bug_send":     _send,
})
