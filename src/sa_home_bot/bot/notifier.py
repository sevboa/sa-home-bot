"""Notifier — обёртка над bot.send_message: ретраи 429, чанкование, reply-fallback."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import ReplyParameters

log = logging.getLogger(__name__)

MAX_LEN = 4096
MAX_RETRIES = 3


def _chunk(text: str, limit: int = MAX_LEN) -> list[str]:
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


class Notifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_direct(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> int | None:
        """Отправить сообщение. Вернуть message_id первого чанка или None при провале."""
        chunks = _chunk(text)
        first_message_id: int | None = None
        for i, chunk in enumerate(chunks):
            reply = (
                ReplyParameters(
                    message_id=reply_to_message_id, allow_sending_without_reply=True
                )
                if (i == 0 and reply_to_message_id is not None)
                else None
            )
            message_id = await self._send_one(chat_id, chunk, reply)
            if message_id is None:
                return first_message_id
            if first_message_id is None:
                first_message_id = message_id
        return first_message_id

    async def _send_one(
        self, chat_id: int, text: str, reply: ReplyParameters | None
    ) -> int | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await self._bot.send_message(
                    chat_id, text, reply_parameters=reply
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
