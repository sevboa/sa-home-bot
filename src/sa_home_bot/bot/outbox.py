"""Флаш постоянной очереди исходящих (Этап 52).

Notifier кладёт в таблицу outbox текст, не доставленный из-за транзиентного
сбоя связи с Telegram (см. bot/notifier.py::Notifier.send_direct). Здесь —
досылка: по событию «выход восстановлен» и тиком раз в минуту.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, ReplyParameters

from sa_home_bot.bot.notifier import MAX_LEN, Notifier, is_permanent_send_error
from sa_home_bot.db.store import (  # noqa: F401 — реэкспорт для вызывающих
    OUTBOX_KIND_ALERT,
    OUTBOX_KIND_DELIVER,
    OUTBOX_KIND_INTERACTIVE,
    OUTBOX_KIND_TASK,
    OUTBOX_TTL,
    Store,
)

log = logging.getLogger(__name__)

# Старше этого — к тексту добавляем пометку о задержке.
LATE_MARK_AFTER = timedelta(seconds=60)
# Сколько раз подряд пережидаем 429 на одной записи, прежде чем отложить флаш.
_MAX_RETRY_AFTER = 3

_flush_lock = asyncio.Lock()


def _with_late_mark(text: str, created_at: datetime) -> str:
    mark = f"⏳ с задержкой, {created_at.astimezone().strftime('%H:%M')}"
    if len(text) + len(mark) + 2 > MAX_LEN:
        return text
    return f"{text}\n\n{mark}"


async def flush_outbox(
    notifier: Notifier,
    store: Store,
    *,
    now: Callable[[], datetime] | datetime | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Доставить due-записи outbox по порядку created_at; вернуть число
    доставленных. Один флаш одновременно (параллельный вызов сразу вернёт 0).

    - 429: ждём retry_after+1 и повторяем ту же запись;
    - транзиентный сбой: attempts+1 и остановка флаша (связи нет);
    - перманентный (бот заблокирован, чат не найден и т.п.): запись удаляется
      с warning;
    - протухшее (TTL по виду) удаляется до отправки.

    ``now`` — datetime или фабрика (для тестов); по умолчанию текущее UTC."""
    if _flush_lock.locked():
        return 0
    async with _flush_lock:
        current = now() if callable(now) else (now or datetime.now(UTC))
        expired = await store.outbox_expire(current)
        if expired:
            log.info("outbox: протухло и удалено записей: %s", expired)
        sent = 0
        for row in await store.outbox_due(current):
            payload = row["payload"]
            text = payload.get("text", "")
            if (current - row["created_at"]) > LATE_MARK_AFTER:
                text = _with_late_mark(text, row["created_at"])
            reply = (
                ReplyParameters(
                    message_id=payload["reply_to_message_id"], allow_sending_without_reply=True
                )
                if payload.get("reply_to_message_id") is not None
                else None
            )
            markup = (
                InlineKeyboardMarkup.model_validate(payload["reply_markup"])
                if payload.get("reply_markup")
                else None
            )
            retries = 0
            while True:
                try:
                    await notifier.bot.send_message(
                        row["chat_id"],
                        text,
                        reply_parameters=reply,
                        reply_markup=markup,
                        message_thread_id=row["message_thread_id"],
                    )
                except TelegramRetryAfter as exc:
                    retries += 1
                    if retries > _MAX_RETRY_AFTER:
                        return sent
                    wait = exc.retry_after + 1
                    log.warning("outbox: 429 от Telegram, жду %ss", wait)
                    await sleep(wait)
                    continue
                except Exception as exc:  # noqa: BLE001
                    if is_permanent_send_error(exc):
                        log.warning(
                            "outbox: запись %s (chat=%s) не доставить, удаляю: %s",
                            row["id"], row["chat_id"], exc,
                        )
                        await store.outbox_delete(row["id"])
                        break
                    log.warning("outbox: связи нет, флаш прерван (запись %s): %s", row["id"], exc)
                    await store.outbox_bump_attempts(row["id"])
                    return sent
                await store.outbox_delete(row["id"])
                sent += 1
                break
        return sent
