"""Заявка владельцу из /vpn: «🙋 Позвать на помощь» мастера (57.3a) и
«⚠️ Сообщить о проблеме» (57.6b) — один механизм.

Одна функция отправки: ``send_help_request`` берёт готовый текст причины и
строки контекста, шлёт владельцу «⚠️ VPN: проблема у …» с кнопками
«💬 Ответить» и «👤 Карточка гостя». Адресаты — те же, кому идут прочие админские
уведомления VPN (``notify_admins``: чаты с полным доступом).

Троттлинг — в памяти процесса, раз в ``THROTTLE_S`` на человека; после
рестарта бота счётчик обнуляется, это не страшно.
"""

from __future__ import annotations

import html
import time
from collections.abc import Sequence
from typing import Literal

from sa_home_bot.bot import vpn_admin_view, vpn_report
from sa_home_bot.bot.notifier import Notifier, notify_admins
from sa_home_bot.subscriptions.models import WILDCARD

THROTTLE_S = 600.0

Outcome = Literal["sent", "throttled", "nobody"]

_last_sent: dict[int, float] = {}


def reset_throttle() -> None:
    _last_sent.clear()


def who_text(name: str, username: str | None) -> str:
    """«Алексей (@alex)» — уже экранировано под HTML."""
    text = f"<b>{html.escape(name)}</b>"
    if username:
        text += f" (@{html.escape(username)})"
    return text


def has_admins(book) -> bool:
    return book is not None and any(WILDCARD in sub.allowed_commands for sub in book.all())


def is_throttled(chat_id: int) -> bool:
    last = _last_sent.get(chat_id)
    return last is not None and time.monotonic() - last < THROTTLE_S


async def send_help_request(
    book,
    notifier: Notifier,
    *,
    chat_id: int,
    who: str,
    reason: str,
    context: Sequence[str] = (),
) -> Outcome:
    """Передать владельцу «человеку нужна помощь» (формат этапа 57.6b). ``who``,
    ``reason`` и ``context`` — HTML-текст (экранирует вызывающий). ``throttled`` —
    уже передавали меньше ``THROTTLE_S`` назад (общий счётчик «🙋 Позвать на
    помощь» и «⚠️ Сообщить о проблеме»); ``nobody`` — в книге нет ни одного
    админа. Пробник (chat_id=0) — не человек, ему не отвечают."""
    if chat_id == 0 or not has_admins(book):
        return "nobody"
    if is_throttled(chat_id):
        return "throttled"
    _last_sent[chat_id] = time.monotonic()
    text = "\n".join([f"⚠️ VPN: проблема у {who}", f"Причина: {reason}", *context])
    await notify_admins(
        book,
        notifier,
        text,
        reply_markup=vpn_report.owner_keyboard(chat_id, vpn_admin_view.guest_cb(chat_id)),
    )
    return "sent"
