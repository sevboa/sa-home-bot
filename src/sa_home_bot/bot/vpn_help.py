"""«🙋 Позвать на помощь» в /vpn (этап 57.3a): человеку сказали «застрял» —
владельцу уходит одно сообщение с контекстом и кнопкой карточки гостя.

Одна функция отправки: ``send_help_request`` берёт готовый текст причины, так
что «⚠️ Сообщить о проблеме» (57.6b) добавит свои причины и «💬 Ответить»,
не трогая троттлинг и адресатов. Адресаты — те же, кому идут прочие админские
уведомления VPN (``notify_admins``: чаты с полным доступом).

Троттлинг — в памяти процесса, раз в ``THROTTLE_S`` на человека; после
рестарта бота счётчик обнуляется, это не страшно.
"""

from __future__ import annotations

import html
import time
from typing import Literal

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_admin_view
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


async def send_help_request(
    book,
    notifier: Notifier,
    *,
    chat_id: int,
    who: str,
    reason: str,
) -> Outcome:
    """Передать владельцу «человеку нужна помощь». ``who`` и ``reason`` —
    HTML-текст (экранирует вызывающий). ``throttled`` — уже передавали меньше
    ``THROTTLE_S`` назад; ``nobody`` — в книге нет ни одного админа."""
    if book is None or not any(WILDCARD in sub.allowed_commands for sub in book.all()):
        return "nobody"
    now = time.monotonic()
    last = _last_sent.get(chat_id)
    if last is not None and now - last < THROTTLE_S:
        return "throttled"
    _last_sent[chat_id] = now
    await notify_admins(
        book,
        notifier,
        f"🙋 VPN: {who} {reason}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👤 Карточка гостя",
                        callback_data=vpn_admin_view.guest_cb(chat_id),
                    )
                ]
            ]
        ),
    )
    return "sent"
