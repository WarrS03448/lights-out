"""Shared client copy for the two independent Bodybomb ladders."""
from . import i18n

_COPY = {
    "en": ("Concede", "Concede this match? You will receive a loss and your opponent a win.", "Solo queue only. Leave your party to enter 1v1.", "This ranked mode is unavailable while your ban is active."),
    "de": ("Aufgeben", "Dieses Match aufgeben? Du erhältst eine Niederlage und dein Gegner einen Sieg.", "Nur Solo-Suche. Verlasse deine Gruppe für 1v1.", "Dieser Ranglistenmodus ist während deiner Sperre nicht verfügbar."),
    "es": ("Rendirse", "¿Rendirte? Recibirás una derrota y tu rival una victoria.", "Solo jugadores individuales. Sal del grupo para jugar 1v1.", "Este modo clasificatorio no está disponible mientras dure tu sanción."),
    "fr": ("Abandonner", "Abandonner ce match ? Vous recevrez une défaite et votre adversaire une victoire.", "File solo uniquement. Quittez votre groupe pour jouer en 1v1.", "Ce mode classé est indisponible pendant votre bannissement."),
    "pt": ("Desistir", "Desistir da partida? Você receberá uma derrota e seu oponente uma vitória.", "Fila solo. Saia do grupo para jogar 1v1.", "Este modo ranqueado está indisponível durante seu banimento."),
    "ru": ("Сдаться", "Сдаться в этом матче? Вам будет засчитано поражение, а сопернику — победа.", "Только одиночная очередь. Покиньте группу для игры 1 на 1.", "Этот рейтинговый режим недоступен, пока действует блокировка."),
    "zh": ("认输", "确定认输吗？你将被判负，对手将获胜。", "仅限单人排队。请退出队伍后参加 1v1。", "封禁期间无法参加此排位模式。"),
}


def strings():
    return dict(zip(("concede", "concede_confirm", "solo_only", "banned"), _COPY.get(i18n.get_language(), _COPY["en"])))
