"""Notifier — обёртка над bot.send_message: ретраи 429, чанкование, reply-fallback."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, ReplyParameters

from sa_home_bot.subscriptions.models import WILDCARD

log = logging.getLogger(__name__)

MAX_LEN = 4096
MAX_RETRIES = 3
# Telegram сам гасит typing-action примерно через 5с — повторяем чуть чаще,
# чтобы индикатор не мигал видимым образом.
TYPING_KEEPALIVE_INTERVAL_S = 4.0


@contextlib.asynccontextmanager
async def typing_action(bot: Bot, chat_id: int, message_thread_id: int | None = None):
    """Держать индикатор «печатает» в чате, пока не выйдем из блока.

    Решение пользователя 2026-08-01: индикатор должен гореть не с момента,
    как человек написал, а именно пока модель готовит ответ — во время
    presence-проверки и прогрева контейнера/машины (wake-сценарий,
    bot/ai_flow.py::request_alfred) у пользователя уже есть свои текстовые
    «шаги»/«Агнольд», дублировать typing поверх них не нужно. Поэтому этим
    контекст-менеджером оборачивают именно вызов модели (``_ask()``), а не
    весь хендлер целиком."""

    async def _send() -> None:
        try:
            await bot.send_chat_action(chat_id, "typing", message_thread_id=message_thread_id)
        except TelegramAPIError:
            pass  # не критично — просто не обновится в этот раз

    async def _loop() -> None:
        while True:
            await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
            await _send()

    # Первый вызов — сразу и синхронно (внутри __aenter__), не через
    # create_task: если запрос к модели ни разу по-настоящему не
    # приостановится (нет реального I/O-ожидания на своём пути), фоновая
    # задача рискует не получить ни одного шанса выполниться до отмены в
    # finally — индикатор не загорелся бы вовсе. Цикл-повтор ниже нужен
    # только чтобы Telegram не погасил его сам примерно через 5с.
    await _send()
    task = asyncio.create_task(_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def chunk_text(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest:
        chunks.append(rest)
    return chunks


async def notify_admins(book, notifier: Notifier, text: str) -> None:
    """Служебное сообщение — в чаты с полным доступом (``*`` в
    allowed_commands), не пользователю.

    Сюда идут диагностика падений /ai (см. bot/ai_flow.py, где эта функция и
    жила раньше) и события приватного входа: кто вошёл по инвайту, кто
    ломится подбором (bot/invites.py). ``book`` не типизирован намеренно —
    иначе notifier зависел бы от подписок, которые зависят от конфига.
    """
    for sub in book.all():
        if WILDCARD in sub.allowed_commands:
            await notifier.send_direct(sub.chat_id, text)


class Notifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_direct(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int | None:
        """Отправить сообщение. Вернуть message_id первого чанка или None при провале.

        ``reply_markup`` вешается на ПЕРВЫЙ чанк: кнопка относится к
        сообщению целиком, а не к его хвосту (у длинных текстов чанков
        несколько, см. chunk_text)."""
        chunks = chunk_text(text)
        first_message_id: int | None = None
        for i, chunk in enumerate(chunks):
            reply = (
                ReplyParameters(
                    message_id=reply_to_message_id, allow_sending_without_reply=True
                )
                if (i == 0 and reply_to_message_id is not None)
                else None
            )
            message_id = await self._send_one(
                chat_id, chunk, reply, reply_markup if i == 0 else None
            )
            if message_id is None:
                return first_message_id
            if first_message_id is None:
                first_message_id = message_id
        return first_message_id

    async def _send_one(
        self,
        chat_id: int,
        text: str,
        reply: ReplyParameters | None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> int | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await self._bot.send_message(
                    chat_id, text, reply_parameters=reply, reply_markup=reply_markup
                )
                return msg.message_id
            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                log.warning("429 от Telegram (chat=%s), жду %ss", chat_id, wait)
                await asyncio.sleep(wait)
            except TelegramAPIError as exc:
                log.warning(
                    "Не удалось отправить в chat=%s (попытка %s/%s): %s",
                    chat_id,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                return None
        log.error("Исчерпаны ретраи отправки в chat=%s", chat_id)
        return None
