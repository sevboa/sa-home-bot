from __future__ import annotations

import html
import logging
import time
from collections.abc import Sequence
from typing import Literal

from sa_home_bot.bot import vpn_admin_view, vpn_callbacks, vpn_nodes, vpn_report
from sa_home_bot.bot.notifier import Notifier, notify_admins
from sa_home_bot.subscriptions.models import WILDCARD

log = logging.getLogger(__name__)

THROTTLE_S = 600.0

Outcome = Literal["sent", "throttled", "nobody"]

_last_sent: dict[int, float] = {}


def reset_throttle() -> None:
    _last_sent.clear()


def who_text(name: str, username: str | None) -> str:
    text = f"<b>{html.escape(name)}</b>"
    if username:
        text += f" (@{html.escape(username)})"
    return text


def has_admins(book) -> bool:
    return book is not None and any(WILDCARD in sub.allowed_commands for sub in book.all())


def is_throttled(chat_id: int) -> bool:
    last = _last_sent.get(chat_id)
    return last is not None and time.monotonic() - last < THROTTLE_S


def blocked_outcome(book, chat_id: int) -> Outcome | None:
    if vpn_callbacks.is_probe(chat_id) or not has_admins(book):
        return "nobody"
    if is_throttled(chat_id):
        return "throttled"
    return None


def context_or_empty(servers: list[dict], answered: list[dict], device_key: str) -> list[str]:
    try:
        down = vpn_nodes.unavailable_servers(answered)
        return vpn_report.context_lines(servers, down, device_key)
    except Exception:
        log.warning("Контекст заявки по VPN не собрался", exc_info=True)
        return []


async def send_help_request(
    book,
    notifier: Notifier,
    *,
    chat_id: int,
    who: str,
    reason: str,
    context: Sequence[str] = (),
) -> Outcome:
    blocked = blocked_outcome(book, chat_id)
    if blocked is not None:
        return blocked
    _last_sent[chat_id] = time.monotonic()
    text = "\n".join([f"⚠️ VPN: проблема у {who}", f"Причина: {reason}", *context])
    await notify_admins(
        book,
        notifier,
        text,
        reply_markup=vpn_report.owner_keyboard(chat_id, vpn_admin_view.guest_cb(chat_id)),
    )
    return "sent"
