"""Notifier — обёртка над bot.send_message: ретраи 429, чанкование, reply-fallback."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, ReplyParameters

from sa_home_bot.subscriptions.models import WILDCARD

_T = TypeVar("_T")

log = logging.getLogger(__name__)

MAX_LEN = 4096
MAX_RETRIES = 3
# Telegram сам гасит typing-action примерно через 5с — повторяем чуть чаще,
# чтобы индикатор не мигал видимым образом.
TYPING_KEEPALIVE_INTERVAL_S = 4.0
# Защитный потолок для TypingIndicator.start() без последующего stop() —
# тот же приём, что и _KEEPALIVE_MAX_TICKS в rich_stream.py: сессия должна
# всегда дойти до места, которое останавливает индикатор (push_status/
# finalize/finalize_status), это подстраховка на случай, если её всё же
# бросят, не закрыв. 450 тиков * 4с = 30 минут — тот же запас, что и у
# keep-alive черновика.
TYPING_MAX_TICKS = 450


class TypingIndicator:
    """Индикатор «печатает» с независимыми start()/stop() — для мест, где
    его нужно включать/выключать по ходу дела, а не одним блоком на весь
    запрос (см. typing_action() ниже про такой блок).

    Решение пользователя 2026-08-11: индикатор должен гореть ровно пока
    в чат реально льётся текст ответа, а не пока модель «думает» или «идёт»
    (router-проход, статусы тулов, ожидание пробуждения, распознавание
    голосового) — там уже есть свой thinking-блок со статусом
    (RichStreamSession.push_status), дублировать typing поверх него не
    нужно. RichStreamSession включает этот индикатор из _push_markdown
    (реальный кусок текста) и выключает из _push_thinking/_send_persisted
    (статус или финал)."""

    def __init__(self, bot: Bot, chat_id: int, message_thread_id: int | None = None) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._task: asyncio.Task | None = None

    async def _send(self) -> None:
        try:
            await self._bot.send_chat_action(
                self._chat_id, "typing", message_thread_id=self._message_thread_id
            )
        except Exception:  # noqa: BLE001 — не критично, просто не обновится в этот раз
            # Живая находка 2026-08-29 (см. rich_stream.py::_send_draft):
            # сетевые сбои вроде aiohttp_socks.ProxyTimeoutError не
            # подклассы TelegramAPIError и раньше улетали наверх, хотя
            # индикатор — некритичный побочный эффект, не сам ответ.
            pass

    async def _loop(self) -> None:
        for _ in range(TYPING_MAX_TICKS):
            await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_S)
            await self._send()

    async def start(self) -> None:
        """No-op, если уже горит — вызывающие (RichStreamSession) зовут это
        на каждый кусок текста, не только на первый."""
        if self._task is not None and not self._task.done():
            return
        # Первый вызов — сразу и синхронно, не через create_task: если
        # дальнейший код ни разу по-настоящему не приостановится (нет
        # реального I/O-ожидания на своём пути), фоновая задача рискует не
        # получить ни одного шанса выполниться до stop() — индикатор не
        # загорелся бы вовсе. Цикл-повтор ниже нужен только чтобы Telegram
        # не погасил его сам примерно через 5с.
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
    """Держать индикатор «печатает» в чате на весь блок — для путей без
    покускового стрима (группы/фолбэк без rich-сессии), где нет отдельного
    сигнала "текст реально льётся" и лучшее приближение — вся генерация
    ответа целиком (см. bot/ai_flow.py::_typing_while_asking)."""
    indicator = TypingIndicator(bot, chat_id, message_thread_id)
    await indicator.start()
    try:
        yield
    finally:
        await indicator.stop()


async def send_with_retry(
    chat_id: int,
    action: str,
    call: Callable[[], Awaitable[_T]],
) -> _T | None:
    """Общий цикл ретраев на 429/ошибки Telegram (MAX_RETRIES попыток,
    полноценный sleep(retry_after+1) на TelegramRetryAfter) — вынесен для
    bot/rich_stream.py (этап 34, Фаза 2), чтобы не копировать цикл
    ``Notifier._send_one`` под sendRichMessage. ``action`` — короткое
    описание для логов ("rich-сообщение" и т.п.). ``Notifier._send_one``/
    ``send_document``/``send_photo`` были написаны раньше и на этот хелпер
    не переведены — рефакторинг их не входит в эту задачу."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            wait = exc.retry_after + 1
            log.warning("429 от Telegram (chat=%s, %s), жду %ss", chat_id, action, wait)
            await asyncio.sleep(wait)
        except Exception as exc:  # noqa: BLE001 — см. живую находку 2026-08-29 ниже
            # Сетевые сбои (напр. aiohttp_socks.ProxyTimeoutError зависшего
            # SOCKS-прокси до Telegram) — не подклассы TelegramAPIError и
            # раньше улетали наверх мимо этого же ретрай-цикла, задуманного
            # именно чтобы не ронять вызывающего на временных сбоях связи
            # (см. rich_stream.py::_send_draft, тот же класс бага).
            log.warning(
                "Не удалось отправить %s в chat=%s (попытка %s/%s): %s",
                action,
                chat_id,
                attempt,
                MAX_RETRIES,
                exc,
            )
            return None
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


async def notify_admins(
    book, notifier: Notifier, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Служебное сообщение — в чаты с полным доступом (``*`` в
    allowed_commands), не пользователю.

    Сюда идут диагностика падений /ai (см. bot/ai_flow.py, где эта функция и
    жила раньше), события приватного входа: кто вошёл по инвайту, кто
    ломится подбором (bot/invites.py), и блокировка автовыключения ноды
    открытой SSH-сессией (bot/node_events.py, ``reply_markup`` — кнопка
    «закрыть сессии и выключить»). ``book`` не типизирован намеренно —
    иначе notifier зависел бы от подписок, которые зависят от конфига.
    """
    for sub in book.all():
        if WILDCARD in sub.allowed_commands:
            await notifier.send_direct(sub.chat_id, text, reply_markup=reply_markup)


class Notifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    @property
    def bot(self) -> Bot:
        """Экземпляр aiogram Bot — нужен местам, которым нужна не сама
        отправка обычного sendMessage (send_direct ниже), а другая механика
        Bot API поверх того же бота (RichStreamSession, bot/rich_stream.py —
        см. bot/node_events.py::_handle_task_prewake, где служба tasks шлёт
        «шаги»/«Агнольд» тем же rich-путём, что и живой /ai)."""
        return self._bot

    async def send_direct(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        """Отправить сообщение. Вернуть message_id первого чанка или None при провале.

        ``reply_markup`` вешается на ПЕРВЫЙ чанк: кнопка относится к
        сообщению целиком, а не к его хвосту (у длинных текстов чанков
        несколько, см. chunk_text). ``message_thread_id`` — топик личного
        чата (Private Chat Topics, bot/handlers/ai.py): без него
        проактивная отправка (не ответ на сообщение) уезжает в общий топик,
        а не туда, где пользователь реально смотрит переписку (живой баг
        vpn: конфиг/QR/APK терялись из вида в топике — 2026-08-03)."""
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
                chat_id,
                chunk,
                reply,
                reply_markup if i == 0 else None,
                message_thread_id=message_thread_id,
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
        message_thread_id: int | None = None,
    ) -> int | None:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await self._bot.send_message(
                    chat_id,
                    text,
                    reply_parameters=reply,
                    reply_markup=reply_markup,
                    message_thread_id=message_thread_id,
                )
                return msg.message_id
            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                log.warning("429 от Telegram (chat=%s), жду %ss", chat_id, wait)
                await asyncio.sleep(wait)
            except Exception as exc:  # noqa: BLE001 — см. send_with_retry про сетевые сбои
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

    async def send_document(
        self,
        chat_id: int,
        document: BufferedInputFile | bytes | str,
        *,
        filename: str | None = None,
        caption: str | None = None,
        message_thread_id: int | None = None,
    ) -> tuple[int, str] | None:
        """Отправить документ: байты (``BufferedInputFile`` или голый
        ``bytes`` + ``filename`` — второе нужно вызывающим без aiogram,
        см. bot/tools.py) или уже загруженный в Telegram ``file_id``
        (строка) — тогда байты никуда не едут, Telegram переиспользует
        прежнюю загрузку.

        Возвращает ``(message_id, file_id)`` — второе нужно вызывающему,
        чтобы запомнить file_id и не гонять файл повторно (см.
        bot/vpn_apk.py::deliver_apk). ``message_thread_id`` — см.
        send_direct."""
        if isinstance(document, bytes):
            document = BufferedInputFile(document, filename=filename or "file")
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await self._bot.send_document(
                    chat_id,
                    document,
                    caption=caption,
                    message_thread_id=message_thread_id,
                )
                file_id = msg.document.file_id if msg.document else ""
                return msg.message_id, file_id
            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                log.warning("429 от Telegram (chat=%s), жду %ss", chat_id, wait)
                await asyncio.sleep(wait)
            except Exception as exc:  # noqa: BLE001 — см. send_with_retry про сетевые сбои
                log.warning(
                    "Не удалось отправить документ в chat=%s (попытка %s/%s): %s",
                    chat_id,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                return None
        log.error("Исчерпаны ретраи отправки документа в chat=%s", chat_id)
        return None

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
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await self._bot.send_photo(
                    chat_id, photo, caption=caption, message_thread_id=message_thread_id
                )
                return msg.message_id
            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                log.warning("429 от Telegram (chat=%s), жду %ss", chat_id, wait)
                await asyncio.sleep(wait)
            except Exception as exc:  # noqa: BLE001 — см. send_with_retry про сетевые сбои
                log.warning(
                    "Не удалось отправить фото в chat=%s (попытка %s/%s): %s",
                    chat_id,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                return None
        log.error("Исчерпаны ретраи отправки фото в chat=%s", chat_id)
        return None

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
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                msg = await self._bot.send_voice(
                    chat_id, voice, caption=caption, message_thread_id=message_thread_id
                )
                return msg.message_id
            except TelegramRetryAfter as exc:
                wait = exc.retry_after + 1
                log.warning("429 от Telegram (chat=%s), жду %ss", chat_id, wait)
                await asyncio.sleep(wait)
            except Exception as exc:  # noqa: BLE001 — см. send_with_retry про сетевые сбои
                log.warning(
                    "Не удалось отправить голосовое в chat=%s (попытка %s/%s): %s",
                    chat_id,
                    attempt,
                    MAX_RETRIES,
                    exc,
                )
                return None
        log.error("Исчерпаны ретраи отправки голосового в chat=%s", chat_id)
        return None

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(Exception):  # noqa: BLE001 — необязательная уборка
            await self._bot.delete_message(chat_id, message_id)
