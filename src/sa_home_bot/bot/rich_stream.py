from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InputRichBlockThinking, InputRichMessage, Message, ReplyParameters

from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_MD
from sa_home_bot.bot.notifier import TypingIndicator, send_with_retry

log = logging.getLogger(__name__)


KIND_MARKDOWN = "md"
KIND_THINKING = "think"
_KEEPALIVE_MAX_IDLE_S = 1800.0
_FAILURE_WARN_LIMIT = 3


@dataclass(frozen=True)
class RichDraftPolicy:
    streaming: bool = True
    min_interval_s: float = 6.0
    min_growth_chars: int = 120
    keepalive_idle_s: float = 18.0

    @classmethod
    def from_llm_config(cls, llm: Any) -> RichDraftPolicy:
        return cls(
            streaming=llm.rich_draft_streaming,
            min_interval_s=llm.rich_draft_min_interval_s,
            min_growth_chars=llm.rich_draft_min_growth_chars,
            keepalive_idle_s=llm.rich_draft_keepalive_idle_s,
        )


@dataclass
class _Draft:
    kind: str
    message: InputRichMessage
    text_len: int = 0


@dataclass
class DraftStats:
    text: int = 0
    status: int = 0
    keepalive: int = 0
    coalesced: int = 0
    retry_after: int = 0
    failures: int = 0

    def has_activity(self) -> bool:
        counters = (
            self.text,
            self.status,
            self.keepalive,
            self.coalesced,
            self.retry_after,
            self.failures,
        )
        return any(counters)


class RichStreamSession:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        message_thread_id: int | None = None,
        policy: RichDraftPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._draft_id = random.randint(1, 2**31 - 1)
        self._policy = policy or RichDraftPolicy()
        self._clock = clock
        self._last_sent: tuple[str, str] | None = None
        self._pending: _Draft | None = None
        self._active_message: InputRichMessage | None = None
        self._active_kind: str | None = None
        self._shown_text_len = 0
        self._last_send_at: float | None = None
        self._blocked_until = 0.0
        self._last_push_at = self._clock()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._pump_task: asyncio.Task | None = None
        self._stats = DraftStats()
        self._summary_logged = False
        self._typing = TypingIndicator(bot, chat_id, message_thread_id)


    def _time_ok(self, now: float) -> bool:
        if now < self._blocked_until:
            return False
        if self._last_send_at is None:
            return True
        return now - self._last_send_at >= self._policy.min_interval_s

    def _content_ready(self, draft: _Draft) -> bool:
        if draft.kind != KIND_MARKDOWN or self._active_kind != KIND_MARKDOWN:
            return True
        growth = draft.text_len - self._shown_text_len
        return growth < 0 or growth >= self._policy.min_growth_chars

    def _next_due(self) -> float | None:
        if self._pending is None and self._active_message is None:
            return None
        base = self._last_send_at if self._last_send_at is not None else self._clock()
        due = base + self._policy.keepalive_idle_s
        if self._pending is not None and self._content_ready(self._pending):
            due = min(due, base + self._policy.min_interval_s)
        return max(due, base + self._policy.min_interval_s, self._blocked_until)

    async def _send_draft(self, rich_message: InputRichMessage, counter: str) -> bool:
        self._last_send_at = self._clock()
        try:
            await self._bot.send_rich_message_draft(
                chat_id=self._chat_id,
                draft_id=self._draft_id,
                rich_message=rich_message,
                message_thread_id=self._message_thread_id,
            )
        except TelegramRetryAfter as exc:
            self._stats.retry_after += 1
            self._blocked_until = self._clock() + float(exc.retry_after)
            self._warn(
                self._stats.retry_after,
                "rich_stream: 429 на черновике (chat=%s) — пауза %s с",
                self._chat_id,
                exc.retry_after,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            self._stats.failures += 1
            self._warn(
                self._stats.failures,
                "rich_stream: не удалось обновить черновик (chat=%s): %r",
                self._chat_id,
                exc,
            )
        else:
            setattr(self._stats, counter, getattr(self._stats, counter) + 1)
        return True

    def _warn(self, count: int, msg: str, *args: Any) -> None:
        if count <= _FAILURE_WARN_LIMIT:
            log.warning(msg, *args)
        else:
            log.debug(msg, *args)

    async def _send_pending(self) -> None:
        draft = self._pending
        if draft is None:
            return
        self._pending = None
        counter = "text" if draft.kind == KIND_MARKDOWN else "status"
        if not await self._send_draft(draft.message, counter):
            if self._pending is None:
                self._pending = draft
            return
        self._active_message = draft.message
        self._active_kind = draft.kind
        self._shown_text_len = draft.text_len if draft.kind == KIND_MARKDOWN else 0

    async def _flush_due(self) -> None:
        now = self._clock()
        if not self._time_ok(now):
            return
        if self._pending is not None and self._content_ready(self._pending):
            await self._send_pending()
            return
        base = self._last_send_at if self._last_send_at is not None else now
        if now - base < self._policy.keepalive_idle_s:
            return
        if self._pending is not None:
            await self._send_pending()
        elif self._active_message is not None:
            await self._send_draft(self._active_message, "keepalive")

    async def _pump(self) -> None:
        while True:
            now = self._clock()
            idle_left = self._last_push_at + _KEEPALIVE_MAX_IDLE_S - now
            if idle_left <= 0:
                return
            due = self._next_due()
            if due is None:
                return
            delay = due - now
            if delay > 0:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=min(delay, idle_left))
                continue
            before = self._last_send_at
            async with self._lock:
                await self._flush_due()
            if self._last_send_at == before:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)

    def _ensure_pump(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())
        self._wake.set()

    async def _offer(self, draft: _Draft) -> None:
        self._last_push_at = self._clock()
        async with self._lock:
            if self._pending is not None:
                self._stats.coalesced += 1
            self._pending = draft
            if self._time_ok(self._clock()) and self._content_ready(draft):
                await self._send_pending()
        self._ensure_pump()

    async def _stop_background(self) -> None:
        task = self._pump_task
        self._pump_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._typing.stop()

    def _clear_draft_state(self) -> None:
        self._pending = None
        self._active_message = None
        self._active_kind = None
        self._shown_text_len = 0
        self._last_sent = None

    async def aclose(self) -> None:
        await self._stop_background()
        self._clear_draft_state()
        self._log_summary()

    def _log_summary(self) -> None:
        if self._summary_logged:
            return
        if not self._stats.has_activity() and self._typing.sends == 0:
            return
        self._summary_logged = True
        s = self._stats
        log.info(
            "rich_stream: итог сессии chat=%s: текст=%d статусы=%d keep-alive=%d "
            "свёрнуто=%d typing=%d 429=%d сбоев=%d стрим=%s",
            self._chat_id,
            s.text,
            s.status,
            s.keepalive,
            s.coalesced,
            self._typing.sends,
            s.retry_after,
            s.failures,
            "да" if self._policy.streaming else "нет",
        )

    async def _push_markdown(self, markdown_body: str, *, with_prefix: bool = True) -> None:
        signature = (KIND_MARKDOWN, markdown_body)
        if not markdown_body or signature == self._last_sent:
            return
        self._last_sent = signature
        if not self._policy.streaming:
            await self._typing.start()
            return
        markdown = ALFRED_PREFIX_MD + markdown_body if with_prefix else markdown_body
        draft_message = InputRichMessage(markdown=markdown)
        await self._offer(_Draft(KIND_MARKDOWN, draft_message, len(markdown_body)))

    async def _push_thinking(self, text: str) -> None:
        signature = (KIND_THINKING, text)
        if not text or signature == self._last_sent:
            return
        self._last_sent = signature
        if not self._policy.streaming:
            await self._typing.start()
            return
        message = InputRichMessage(blocks=[InputRichBlockThinking(text=text)])
        await self._offer(_Draft(KIND_THINKING, message))

    async def on_partial(self, text: str, done: bool) -> None:
        await self._push_markdown(text)

    async def push_status(self, text: str) -> None:
        await self._push_thinking(text)

    async def _send_persisted(
        self, markdown: str, *, reply_to_message_id: int | None = None
    ) -> Message | None:
        reply = (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id is not None
            else None
        )
        rich_message = InputRichMessage(markdown=markdown)
        await self._stop_background()
        async with self._lock:
            self._clear_draft_state()
            sent = await send_with_retry(
                self._chat_id,
                "rich-сообщение",
                lambda: self._bot.send_rich_message(
                    chat_id=self._chat_id,
                    rich_message=rich_message,
                    reply_parameters=reply,
                    message_thread_id=self._message_thread_id,
                ),
            )
        return sent

    async def finalize(self, raw: str, reply_to_message_id: int | None = None) -> Message | None:
        return await self._send_persisted(
            ALFRED_PREFIX_MD + raw.strip(), reply_to_message_id=reply_to_message_id
        )

    async def finalize_status(
        self, markdown: str, *, reply_to_message_id: int | None = None
    ) -> Message | None:
        return await self._send_persisted(markdown, reply_to_message_id=reply_to_message_id)


async def push_status_or_answer(
    message: Message, rich_session: RichStreamSession | None, plain: str, html: str
) -> None:
    if rich_session is not None:
        await rich_session.push_status(plain)
    else:
        await message.answer(html)


async def finalize_status_or_answer(
    message: Message, rich_session: RichStreamSession | None, md: str, html: str
) -> None:
    if rich_session is not None:
        await rich_session.finalize_status(md)
    else:
        await message.answer(html)
