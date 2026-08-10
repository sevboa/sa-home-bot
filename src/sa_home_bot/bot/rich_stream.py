"""RichStreamSession — Telegram Bot API 10.1/10.2 Rich Messages для ответов
Альфреда (этап 34, Фаза 2, IMPLEMENTATION_PLAN.md).

Rich как основной режим ответа (config.py::LlmConfig.response_mode), не
плейсхолдер на потом — с первого коммита никакого смешивания с обычным
plain ``editMessageText`` внутри одного ответа: только
``sendRichMessageDraft`` (пока идёт генерация) и ``sendRichMessage`` (когда
текст окончательный, единственная точка, которая реально персистит
сообщение в историю чата). Настоящий текст ответа — готовый markdown целиком
(модель и так генерирует markdown, llm/prompt.py); статусные фразы (шаги/тул/
пробуждение) — блок ``InputRichBlockThinking`` (см. push_status ниже).

``sendRichMessageDraft`` платформенно ограничен приватным чатом (докстринг
``chat_id`` в aiogram/methods/send_rich_message_draft.py: "target private
chat") — это ограничение Bot API, не наш выбор. В группах/супергруппах эта
сессия используется БЕЗ вызовов on_partial (см. bot/handlers/ai.py) — тогда
единственное, что уезжает в чат, это финальный ``finalize()``: то же
форматирование (таблицы, код, списки), но без анимации стрима.
"""

from __future__ import annotations

import asyncio
import logging
import random

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputRichBlockThinking, InputRichMessage, Message, ReplyParameters

from sa_home_bot.bot.notifier import TypingIndicator, send_with_retry

log = logging.getLogger(__name__)

ALFRED_PREFIX_MD = "**Альфред:** "

# Живая находка 2026-08-11: докстринг aiogram (SendRichMessageDraft) — "the
# streamed draft is ephemeral and acts as a temporary 30-second preview".
# Долгие ожидания (wake_core.py: WAKE_POLL_TIMEOUT_S=180с на подъём машины,
# WARMUP_TIMEOUT_S=360с на прогрев модели) на порядок превышают этот TTL, а
# push_status/on_partial до сих пор шлют статус ОДИН раз и не освежают —
# черновик гас сам по себе задолго до персистентной реплики (Агнольд/финал
# ответа), из-за чего в чате был заметный разрыв, а следующий push_status
# после уже погасшего черновика читался пользователем как отдельное, не
# связанное с первым, новое сообщение. Интервал — тот же приём, что и
# TYPING_KEEPALIVE_INTERVAL_S в bot/notifier.py (Telegram сам гасит typing
# indicator, мы его периодически освежаем) — с запасом под 30-секундный TTL.
_KEEPALIVE_INTERVAL_S = 20.0
# Защитный потолок — не механизм по умолчанию (сессия должна всегда дойти
# до finalize()/finalize_status(), см. bot/ai_flow.py/bot/node_events.py),
# а подстраховка на случай, если когда-нибудь сессию всё же бросят, не
# закрыв: 90 тиков * 20с = 30 минут, с большим запасом покрывает самое
# долгое легитимное ожидание (WARMUP_TIMEOUT_S=360с), но не даёт фоновой
# задаче крутиться вечно на брошенной сессии.
_KEEPALIVE_MAX_TICKS = 90


class RichStreamSession:
    """Одна сессия — один ответ Альфреда. ``draft_id`` генерируется случайно
    на сессию (не константа на чат!) — ``ActiveAiChats`` (bot/ai_flow.py)
    только хранит task для отмены при остановке бота, НЕ гарантирует, что в
    одном чате не может идти два /ai одновременно (два быстрых сообщения
    подряд диспетчеризуются aiogram каждое своей задачей) — общий draft_id
    у двух параллельных стримов означал бы, что Telegram "анимирует" один
    черновик поверх другого (докстринг SendRichMessageDraft: "changes to
    drafts with the same identifier are animated")."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        message_thread_id: int | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._draft_id = random.randint(1, 2**31 - 1)
        # Сигнатура последнего отправленного контента — ("md", markdown) или
        # ("think", text). Пара, не голая строка: markdown-путь и
        # thinking-путь шлют структурно разные пейлоады (blocks vs markdown),
        # совпадение сырого текста между ними не должно гасить обновление.
        self._last_sent: tuple[str, str] | None = None
        # Последний реально отправленный ЧЕРНОВИК (не сигнатура выше) — то,
        # что keep-alive пересылает повторно, пока идёт долгое ожидание
        # (см. _KEEPALIVE_INTERVAL_S). None — активного черновика нет
        # (сессия только создана или уже финализирована persисted-сообщением).
        self._active_message: InputRichMessage | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Решение пользователя 2026-08-11: typing («печатает») горит только
        # пока реально льётся текст ответа (_push_markdown), не во время
        # статусов/мышления/тулов (_push_thinking) — см. TypingIndicator.
        self._typing = TypingIndicator(bot, chat_id, message_thread_id)

    async def _send_draft(self, rich_message: InputRichMessage) -> None:
        try:
            await self._bot.send_rich_message_draft(
                chat_id=self._chat_id,
                draft_id=self._draft_id,
                rich_message=rich_message,
                message_thread_id=self._message_thread_id,
            )
        except TelegramAPIError as exc:
            # Best-effort: черновик — превью, не критично потерять один
            # тик, следующий подтянет уже более свежий текст (в отличие от
            # finalize() ниже, где потерять сообщение молча нельзя).
            log.debug(
                "rich_stream: не удалось обновить черновик (chat=%s): %s", self._chat_id, exc
            )
        # Вне зависимости от успеха — это теперь то, что (предположительно)
        # на экране; keep-alive пересылает именно это, включая свои
        # собственные повторы (см. _keepalive_loop) — сбой одного тика не
        # должен прерывать цикл, следующий тик попробует снова.
        self._active_message = rich_message

    def _ensure_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _keepalive_loop(self) -> None:
        for _ in range(_KEEPALIVE_MAX_TICKS):
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            if self._active_message is None:
                return  # финализировано (finalize/finalize_status) — черновика больше нет
            await self._send_draft(self._active_message)

    def _stop_keepalive(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        self._active_message = None

    async def _push_markdown(self, markdown_body: str, *, with_prefix: bool = True) -> None:
        """Черновик настоящего текста ответа (on_partial) — дедуп по
        последнему отправленному, best-effort (см. _send_draft).

        ``with_prefix=False`` — живой баг 2026-08-10: статусные фразы
        сами называли Альфреда в третьем лице ("Альфред сёрфит...") —
        с ALFRED_PREFIX_MD получалось видимое дублирование "Альфред:
        Альфред сёрфит...". Настоящий ответ модели (on_partial) имени не
        содержит — ему префикс по-прежнему нужен. Параметр остался только
        для on_partial: статусы теперь идут через _push_thinking, где
        такой проблемы нет вовсе (thinking-блок — не markdown)."""
        signature = ("md", markdown_body)
        if not markdown_body or signature == self._last_sent:
            return
        self._last_sent = signature
        await self._typing.start()
        markdown = ALFRED_PREFIX_MD + markdown_body if with_prefix else markdown_body
        await self._send_draft(InputRichMessage(markdown=markdown))
        self._ensure_keepalive()

    async def _push_thinking(self, text: str) -> None:
        signature = ("think", text)
        if not text or signature == self._last_sent:
            return
        self._last_sent = signature
        # Статус — не набор текста: "печатает" тут неправда, за пользователя
        # уже говорит сам thinking-блок (серая анимация "печатает мысль").
        await self._typing.stop()
        await self._send_draft(InputRichMessage(blocks=[InputRichBlockThinking(text=text)]))
        self._ensure_keepalive()

    async def on_partial(self, text: str, done: bool) -> None:
        """Колбэк для llm_chat.py::run_chat_loop(on_partial=...).

        ``done`` игнорируется здесь: финализация идёт отдельным вызовом
        finalize() с уже постобработанным текстом (strip_math_notation +
        SpeechTherapist, llm/service.py), не с последним куском стрима —
        превью может чуть разойтись с финальным текстом в последний
        момент, это не проблема (черновик эфемерен, реальный текст в чат
        уходит один раз через finalize)."""
        await self._push_markdown(text)

    async def push_status(self, text: str) -> None:
        """Статус (мышление/шаги/тул/пробуждение узла — bot/ai_flow.py) —
        блок ``InputRichBlockThinking`` в тот же черновик, что on_partial:
        Telegram сам рисует серую анимацию "печатает мысль" для этого типа
        блока (нативный примитив Bot API, не наше форматирование).
        Эфемерная реплика: её сменит либо следующий статус, либо начало
        настоящего текста ответа (on_partial), либо finalize() —
        платформенная семантика черновика уже даёт "заменяется" бесплатно,
        без отдельной логики очистки.

        ``InputRichBlockThinking`` допустим ТОЛЬКО в sendRichMessageDraft
        (докстринг aiogram: "can't be received in messages") — в finalize()
        поэтому по-прежнему обычный markdown, не блоки.

        ``text`` — обычный текст без разметки, фраза уже называет Альфреда
        сама (см. _push_markdown про дубль имени у markdown-пути)."""
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
        # Реальное сообщение вытесняет активный черновик той же сессии
        # (платформенная семантика — см. push_status), поэтому сигнатура
        # последнего показанного черновика больше не отражает экран:
        # следующий push_status/on_partial не должен считать её актуальной.
        self._last_sent = None
        # Черновика больше нет — keep-alive (см. _KEEPALIVE_INTERVAL_S)
        # больше нечего пересылать, останавливаем фоновую задачу. typing
        # тоже: реплика уже дошла до чата, ответ более не "печатается".
        self._stop_keepalive()
        await self._typing.stop()
        return sent

    async def finalize(self, raw: str, reply_to_message_id: int | None = None) -> Message | None:
        """Персистит настоящий ответ Альфреда — с ретраем на 429
        (bot/notifier.py::send_with_retry), как и обычная отправка
        сообщений. Работает одинаково для приватных чатов (после серии
        on_partial) и для групп (единственный вызов, без единого
        on_partial до этого) — группам streaming-черновик недоступен
        платформенно, не по нашему выбору (см. докстринг модуля)."""
        return await self._send_persisted(
            ALFRED_PREFIX_MD + raw.strip(), reply_to_message_id=reply_to_message_id
        )

    async def finalize_status(
        self, markdown: str, *, reply_to_message_id: int | None = None
    ) -> Message | None:
        """Персистит реплику ДРУГОГО персонажа, не Альфреда (например
        Агнольда при пробуждении узла — bot/ai_flow.py) — без
        ALFRED_PREFIX_MD (текст сам называет говорящего). ``reply_to_
        message_id`` по умолчанию не задан (не ответ на конкретное
        сообщение, отдельная сюжетная реплика) — но служба tasks
        (bot/node_events.py) передаёт его для «Альбегта» при провале
        отложенной задачи: там реплика как раз ссылается на исходную
        просьбу, которая могла прозвучать давно (задача самоплан на потом),
        и явная стрелка-реплай в Telegram — единственная подсказка, к чему
        она относится.

        Живая находка 2026-08-10 (третий заход): раньше такая реплика шла
        через message.answer() — совсем отдельный от rich-механики Bot API
        метод (обычный sendMessage). Активный черновик ("шаги") от этого
        никак не менялся — просто истекал сам по себе через несколько
        секунд, а реплика всплывала отдельным сообщением с заметным
        разрывом между ними. sendRichMessage (та же механика, что и
        finalize()) вытесняет черновик той же сессии напрямую — без
        зазора, статус буквально подменяется репликой, при этом реплика
        остаётся в истории чата насовсем (в отличие от push_status)."""
        return await self._send_persisted(markdown, reply_to_message_id=reply_to_message_id)
