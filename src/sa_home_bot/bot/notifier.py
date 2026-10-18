from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, ReplyParameters

from sa_home_bot.db.store import OUTBOX_KIND_INTERACTIVE
from sa_home_bot.subscriptions.models import WILDCARD

if TYPE_CHECKING:
    from sa_home_bot.db.store import Store

_T = TypeVar("_T")

log = logging.getLogger(__name__)

MAX_LEN = 4096
MAX_RETRIES = 3
_TRANSIENT_BACKOFF_S = (0.0, 2.0, 5.0)
TYPING_KEEPALIVE_INTERVAL_S = 4.0
TYPING_MAX_TICKS = 450


class TypingIndicator:
    def __init__(self, bot: Bot, chat_id: int, message_thread_id: int | None = None) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._task: asyncio.Task | None = None
        self.sends = 0

    async def _send(self) -> None:
        self.sends += 1
        try:
            await self._bot.send_chat_action(
                self._chat_id, "typing", message_thread_id=self._message_thread_id
            )
        except Exception:  # noqa: BLE001
            pass

    async def _loop(self) -> None:
        for _ in range(TYPING_MAX_TICKS):
            await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
            await self._send()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        await self._send()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None


@contextlib.asynccontextmanager
async def typing_action(bot: Bot, chat_id: int, message_thread_id: int | None = None):
    indicator = TypingIndicator(bot, chat_id, message_thread_id)
    await indicator.start()
    try:
        yield
    finally:
        await indicator.stop()


def is_permanent_send_error(exc: BaseException) -> bool:
    return isinstance(exc, (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound))


async def _wait_and_report_if_retryable(
    exc: BaseException, action: str, chat_id: int, attempt: int
) -> bool:
    if is_permanent_send_error(exc):
        log.warning(
            "Не удалось отправить %s в chat=%s (перманентный сбой, без ретрая): %s",
            action, chat_id, exc,
        )
        return False
    log.warning(
        "Не удалось отправить %s в chat=%s (попытка %s/%s): %s",
        action, chat_id, attempt, MAX_RETRIES, exc,
    )
    if attempt >= MAX_RETRIES:
        return False
    await asyncio.sleep(_TRANSIENT_BACKOFF_S[min(attempt - 1, len(_TRANSIENT_BACKOFF_S) - 1)])
    return True


async def send_with_retry(
    chat_id: int,
    action: str,
    call: Callable[[], Awaitable[_T]],
    outcome: dict | None = None,
) -> _T | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            wait = exc.retry_after + 1
            log.warning("429 от Telegram (chat=%s, %s), жду %ss", chat_id, action, wait)
            await asyncio.sleep(wait)
        except Exception as exc:  # noqa: BLE001
            if not await _wait_and_report_if_retryable(exc, action, chat_id, attempt):
                if outcome is not None and not is_permanent_send_error(exc):
                    outcome["transient"] = True
                return None
    if outcome is not None:
        outcome["transient"] = True
    log.error("Исчерпаны ретраи отправки %s в chat=%s", action, chat_id)
    return None


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


async def broadcast(subs: Iterable[Any], notifier: Notifier, text: str, **send_kwargs: Any) -> int:
    delivered = 0
    for sub in subs:
        if await notifier.send_direct(sub.chat_id, text, **send_kwargs) is not None:
            delivered += 1
    return delivered


async def notify_admins(
    book, notifier: Notifier, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    admins = (sub for sub in book.all() if WILDCARD in sub.allowed_commands)
    await broadcast(admins, notifier, text, reply_markup=reply_markup)


class Notifier:
    def __init__(self, bot: Bot, outbox: Store | None = None) -> None:
        self._bot = bot
        self._outbox = outbox

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send_direct(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        message_thread_id: int | None = None,
        outbox_kind: str = OUTBOX_KIND_INTERACTIVE,
        dedup_key: str | None = None,
    ) -> int | None:
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
            outcome: dict = {}
            message_id = await self._send_chunk(
                chat_id,
                chunk,
                reply,
                reply_markup if i == 0 else None,
                message_thread_id=message_thread_id,
                outcome=outcome,
            )
            if message_id is None:
                if outcome.get("transient") and self._outbox is not None:
                    await self._enqueue_undelivered_chunks(
                        chat_id, chunks[i:], i == 0, reply_to_message_id,
                        reply_markup if i == 0 else None, message_thread_id,
                        outbox_kind, dedup_key, i,
                    )
                return first_message_id
            if first_message_id is None:
                first_message_id = message_id
        return first_message_id

    async def _enqueue_undelivered_chunks(
        self,
        chat_id: int,
        chunks: list[str],
        with_reply: bool,
        reply_to_message_id: int | None,
        reply_markup: InlineKeyboardMarkup | None,
        message_thread_id: int | None,
        kind: str,
        dedup_key: str | None,
        first_index: int,
    ) -> None:
        now = datetime.now(UTC)
        try:
            for n, chunk in enumerate(chunks):
                payload: dict = {"text": chunk}
                if n == 0 and with_reply and reply_to_message_id is not None:
                    payload["reply_to_message_id"] = reply_to_message_id
                if n == 0 and reply_markup is not None:
                    payload["reply_markup"] = reply_markup.model_dump(
                        mode="json", exclude_none=True
                    )
                key = dedup_key
                if key is not None and (len(chunks) > 1 or first_index > 0):
                    key = f"{dedup_key}#{first_index + n}"
                await self._outbox.outbox_enqueue(
                    chat_id, kind, payload, now=now, dedup_key=key,
                    message_thread_id=message_thread_id,
                )
            log.warning(
                "Сообщение для chat=%s поставлено в outbox (%s, чанков: %s)",
                chat_id, kind, len(chunks),
            )
        except Exception:  # noqa: BLE001
            log.exception("Не удалось поставить сообщение в outbox (chat=%s)", chat_id)

    async def _send_chunk(
        self,
        chat_id: int,
        text: str,
        reply: ReplyParameters | None,
        reply_markup: InlineKeyboardMarkup | None = None,
        message_thread_id: int | None = None,
        outcome: dict | None = None,
    ) -> int | None:
        msg = await send_with_retry(
            chat_id,
            "сообщение",
            lambda: self._bot.send_message(
                chat_id,
                text,
                reply_parameters=reply,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            ),
            outcome,
        )
        return msg.message_id if msg is not None else None

    async def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        msg = await send_with_retry(
            chat_id,
            "правка сообщения",
            lambda: self._bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            ),
        )
        return msg is not None

    async def edit_markup(
        self, chat_id: int, message_id: int, reply_markup: InlineKeyboardMarkup | None
    ) -> bool:
        msg = await send_with_retry(
            chat_id,
            "правка кнопок",
            lambda: self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=reply_markup
            ),
        )
        return msg is not None

    async def edit_caption(
        self,
        chat_id: int,
        message_id: int,
        caption: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        photo: bool = True,
    ) -> bool:
        if not photo:
            return await self.edit_text(chat_id, message_id, caption, reply_markup=reply_markup)
        msg = await send_with_retry(
            chat_id,
            "правка подписи",
            lambda: self._bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id, caption=caption, reply_markup=reply_markup
            ),
        )
        return msg is not None

    async def send_document(
        self,
        chat_id: int,
        document: BufferedInputFile | bytes | str,
        *,
        filename: str | None = None,
        caption: str | None = None,
        message_thread_id: int | None = None,
    ) -> tuple[int, str] | None:
        if isinstance(document, bytes):
            document = BufferedInputFile(document, filename=filename or "file")
        msg = await send_with_retry(
            chat_id,
            "документ",
            lambda: self._bot.send_document(
                chat_id, document, caption=caption, message_thread_id=message_thread_id
            ),
        )
        if msg is None:
            return None
        return msg.message_id, msg.document.file_id if msg.document else ""

    async def send_photo(
        self,
        chat_id: int,
        photo: BufferedInputFile | bytes,
        *,
        filename: str | None = None,
        caption: str | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        if isinstance(photo, bytes):
            photo = BufferedInputFile(photo, filename=filename or "photo.png")
        msg = await send_with_retry(
            chat_id,
            "фото",
            lambda: self._bot.send_photo(
                chat_id, photo, caption=caption, message_thread_id=message_thread_id
            ),
        )
        return msg.message_id if msg is not None else None

    async def send_photo_ex(
        self,
        chat_id: int,
        photo: bytes | str,
        *,
        filename: str = "image.png",
        caption: str | None = None,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
        has_spoiler: bool = False,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> tuple[int, str] | None:
        media = BufferedInputFile(photo, filename=filename) if isinstance(photo, bytes) else photo
        extra = {"reply_markup": reply_markup} if reply_markup is not None else {}
        reply = (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id is not None
            else None
        )
        msg = await send_with_retry(
            chat_id,
            "фото",
            lambda: self._bot.send_photo(
                chat_id,
                media,
                caption=caption,
                message_thread_id=message_thread_id,
                reply_parameters=reply,
                has_spoiler=has_spoiler,
                **extra,
            ),
        )
        if msg is None or not msg.photo:
            return None
        return msg.message_id, msg.photo[-1].file_id

    async def send_voice(
        self,
        chat_id: int,
        voice: BufferedInputFile | bytes,
        *,
        filename: str | None = None,
        caption: str | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        if isinstance(voice, bytes):
            voice = BufferedInputFile(voice, filename=filename or "voice.ogg")
        msg = await send_with_retry(
            chat_id,
            "голосовое",
            lambda: self._bot.send_voice(
                chat_id, voice, caption=caption, message_thread_id=message_thread_id
            ),
        )
        return msg.message_id if msg is not None else None

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(Exception):  # noqa: BLE001
            await self._bot.delete_message(chat_id, message_id)
