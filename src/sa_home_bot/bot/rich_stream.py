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

Этап 34.3 (2026-09-30) — щадящий стрим. Живая находка: частая смена
черновика в личке вешает клиент Telegram (особенно Android), а у
sendRichMessageDraft недокументированный лимит строже editMessageText (при
~1.3 с между черновиками — 429 с retry_after 3–10 с). Раньше сессия слала
черновик на каждый тик опроса (раз в секунду, llm_chat.py::_poll_partial),
каждый статус тула, плюс безусловный keep-alive раз в 20 с поверх активного
стрима и typing раз в 4 с параллельно, а 429 глотались молча в log.debug.
Теперь все отправки черновика сессии идут через один ограничитель
(RichDraftPolicy): не чаще раза в ``min_interval_s``, текст — только при
приросте на ``min_growth_chars``, статусы сворачиваются (последний побеждает),
keep-alive — только в простое, retry_after выдерживается, typing при
черновике не горит. Отложенное досылает одна фоновая задача сессии
(``_pump``), она же и keep-alive.
"""

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

from sa_home_bot.bot.notifier import TypingIndicator, send_with_retry

log = logging.getLogger(__name__)

ALFRED_PREFIX_MD = "**Альфред:** "

# Защитный потолок фоновой задачи сессии — не механизм по умолчанию (сессия
# должна всегда дойти до finalize()/finalize_status()/aclose(), см.
# bot/handlers/ai.py и bot/node_events.py), а подстраховка на случай, если
# её всё же бросят, не закрыв. Считается от ПОСЛЕДНЕГО обновления от
# вызывающего (push_status/on_partial), не от старта задачи: 30 минут тишины
# с большим запасом покрывают самое долгое легитимное ожидание
# (wake_core.py::WARMUP_TIMEOUT_S=360с), но не дают задаче крутиться вечно.
#
# История (2026-08-11): черновик эфемерен — докстринг aiogram
# SendRichMessageDraft: "temporary 30-second preview", — а долгие ожидания
# пробуждения/прогрева на порядок больше; без переотправки черновик гас
# задолго до персистентной реплики. Отсюда keep-alive (см.
# RichDraftPolicy.keepalive_idle_s).
_KEEPALIVE_MAX_IDLE_S = 1800.0
# Сколько сбоев черновика одного вида (429 / прочие) за сессию пишем в лог
# на уровне warning — дальше только debug, итог всё равно попадёт в
# итоговую строку сессии (см. aclose). Сеть до Telegram может лежать
# минутами (SOCKS-прокси), warning на каждый тик забил бы журнал.
_FAILURE_WARN_LIMIT = 3


@dataclass(frozen=True)
class RichDraftPolicy:
    """Как часто сессия имеет право трогать черновик (этап 34.3,
    2026-09-30). Поля и почему такие дефолты — config.py::LlmConfig
    (rich_draft_*); дефолты здесь совпадают с конфигом, чтобы сессия без
    явной политики (тесты, голосовые пути) вела себя так же, как в проде.

    - ``streaming`` — False: черновика нет вовсе, только «печатает».
    - ``min_interval_s`` — минимум между ЛЮБЫМИ двумя отправками черновика
      сессии (текст, статус, keep-alive — лимит Telegram общий).
    - ``min_growth_chars`` — минимальный прирост текста для нового
      черновика текста (первый кусок текста — без условия).
    - ``keepalive_idle_s`` — после стольких секунд без отправок черновик
      освежается (TTL ~30 с)."""

    streaming: bool = True
    min_interval_s: float = 6.0
    min_growth_chars: int = 120
    keepalive_idle_s: float = 18.0

    @classmethod
    def from_llm_config(cls, llm: Any) -> RichDraftPolicy:
        """Из config.py::LlmConfig — отдельная фабрика, а не импорт
        Settings: модуль используется и службой бота, и тестами с
        заглушками конфига."""
        return cls(
            streaming=llm.rich_draft_streaming,
            min_interval_s=llm.rich_draft_min_interval_s,
            min_growth_chars=llm.rich_draft_min_growth_chars,
            keepalive_idle_s=llm.rich_draft_keepalive_idle_s,
        )


@dataclass
class _Draft:
    """Черновик, ждущий отправки: ``kind`` — "md" (текст ответа) или
    "think" (статус), ``text_len`` — длина тела текста без префикса (для
    условия прироста), у статуса 0."""

    kind: str
    message: InputRichMessage
    text_len: int = 0


@dataclass
class DraftStats:
    """Счётчики одной сессии — одна INFO-строка при её закрытии (aclose),
    чтобы по журналу было видно, сколько обновлений реально получил клиент
    и не упирались ли мы в лимит Telegram."""

    text: int = 0
    status: int = 0
    keepalive: int = 0
    coalesced: int = 0
    retry_after: int = 0
    failures: int = 0

    def any(self) -> bool:
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
        policy: RichDraftPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._draft_id = random.randint(1, 2**31 - 1)
        self._policy = policy or RichDraftPolicy()
        self._clock = clock
        # Сигнатура последнего ПРИНЯТОГО к показу контента — ("md",
        # markdown) или ("think", text): дедуп подряд идущих одинаковых
        # обновлений. Пара, не голая строка: markdown-путь и thinking-путь
        # шлют структурно разные пейлоады (blocks vs markdown), совпадение
        # сырого текста между ними не должно гасить обновление.
        self._last_sent: tuple[str, str] | None = None
        # Ждёт своей очереди у ограничителя (этап 34.3) — всегда самое
        # свежее: новый push заменяет неотправленный (статусы тулов
        # сворачиваются, последний побеждает).
        self._pending: _Draft | None = None
        # Последний реально отправленный черновик — то, что keep-alive
        # освежает в простое. None — активного черновика нет (сессия только
        # создана или уже финализирована persisted-сообщением).
        self._active_message: InputRichMessage | None = None
        self._active_kind: str | None = None
        # Длина тела текста в последнем показанном черновике текста — от
        # неё меряется прирост (RichDraftPolicy.min_growth_chars).
        self._shown_text_len = 0
        # Время последней ПОПЫТКИ отправки черновика (успешной или нет —
        # лимит Telegram считает запросы, не успехи). None — ещё не было.
        self._last_send_at: float | None = None
        # До какого момента Telegram просил не слать (TelegramRetryAfter).
        self._blocked_until = 0.0
        self._last_push_at = self._clock()
        # Все отправки черновика и persisted-финал — под одним замком:
        # черновик из фоновой задачи не может проскочить между остановкой
        # стрима и sendRichMessage (гонка финала, этап 34.3).
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._pump_task: asyncio.Task | None = None
        self._stats = DraftStats()
        self._summary_logged = False
        # Решение пользователя 2026-08-11 + этап 34.3: typing при черновике
        # не горит вовсе (черновик сам показывает, что ответ идёт) — только
        # при policy.streaming=False, где черновика нет. См. TypingIndicator.
        self._typing = TypingIndicator(bot, chat_id, message_thread_id)

    # --- ограничитель ---

    def _time_ok(self, now: float) -> bool:
        if now < self._blocked_until:
            return False
        if self._last_send_at is None:
            return True  # самый первый черновик сессии — сразу
        return now - self._last_send_at >= self._policy.min_interval_s

    def _content_ready(self, draft: _Draft) -> bool:
        """Достаточно ли нового в тексте, чтобы тратить на него отправку.
        Статус — всегда (он и так редкий, а после свёртки — последний).
        Первый кусок текста (до него не было черновика текста) — всегда.
        Текст стал короче показанного (новый раунд после тула) — тоже."""
        if draft.kind != "md" or self._active_kind != "md":
            return True
        growth = draft.text_len - self._shown_text_len
        return growth < 0 or growth >= self._policy.min_growth_chars

    def _next_due(self) -> float | None:
        """Момент, когда фоновой задаче есть что делать: отложенный
        черновик, прошедший условие прироста, — как только разрешит
        интервал; иначе keep-alive в простое. None — черновика нет вовсе."""
        if self._pending is None and self._active_message is None:
            return None
        base = self._last_send_at if self._last_send_at is not None else self._clock()
        due = base + self._policy.keepalive_idle_s
        if self._pending is not None and self._content_ready(self._pending):
            due = min(due, base + self._policy.min_interval_s)
        # Любая отправка, keep-alive тоже, подчиняется общему интервалу и
        # паузе после 429 — единый ограничитель на сессию.
        return max(due, base + self._policy.min_interval_s, self._blocked_until)

    async def _send_draft(self, rich_message: InputRichMessage, counter: str) -> bool:
        """Одна попытка sendRichMessageDraft. False — Telegram попросил
        подождать (429): черновик не показан, вызывающий вернёт его в
        очередь. Прочие сбои — как раньше, best-effort: считаем показанным,
        keep-alive попробует снова в простое."""
        self._last_send_at = self._clock()
        try:
            await self._bot.send_rich_message_draft(
                chat_id=self._chat_id,
                draft_id=self._draft_id,
                rich_message=rich_message,
                message_thread_id=self._message_thread_id,
            )
        except TelegramRetryAfter as exc:
            # Этап 34.3: раньше 429 тонул в log.debug, а следующий тик
            # опроса через секунду слал снова — клиент получал поток
            # запросов, Telegram — повод продлить бан. Теперь выдерживаем
            # retry_after целиком для ВСЕХ черновиков сессии.
            self._stats.retry_after += 1
            self._blocked_until = self._clock() + float(exc.retry_after)
            self._warn(
                self._stats.retry_after,
                "rich_stream: 429 на черновике (chat=%s) — пауза %s с",
                self._chat_id,
                exc.retry_after,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — best-effort по докстрингу метода
            # Живая находка 2026-08-29: ловили только TelegramAPIError, но
            # реальный сбой (зависший SOCKS-прокси до Telegram, см. память
            # telegram-bot-api-proxy) приходит как aiohttp_socks.
            # ProxyTimeoutError — обычный Exception, не подкласс
            # TelegramAPIError. Черновик — превью, не критично потерять
            # один тик (в отличие от finalize() ниже, где потерять
            # сообщение молча нельзя) — но ПРЕЖДЕ эта ошибка улетала наверх
            # и рушила весь /ai-ответ ещё до обращения к модели.
            # asyncio.CancelledError не Exception с 3.8 — отмена задачи
            # хендлера по-прежнему проходит сквозь этот except.
            # Этап 34.3: warning вместо debug (с потолком на сессию).
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
        """Под self._lock. Отправить отложенный черновик и запомнить его
        как показанный; на 429 — вернуть в очередь, если его ещё не
        вытеснил более свежий."""
        draft = self._pending
        if draft is None:
            return
        self._pending = None
        counter = "text" if draft.kind == "md" else "status"
        if not await self._send_draft(draft.message, counter):
            if self._pending is None:
                self._pending = draft
            return
        self._active_message = draft.message
        self._active_kind = draft.kind
        self._shown_text_len = draft.text_len if draft.kind == "md" else 0

    async def _flush_due(self) -> None:
        """Под self._lock — ход фоновой задачи: отложенный черновик, если
        его пора слать; иначе, если настал простой, — keep-alive: самое
        свежее, что есть (отложенный хвост текста ниже порога прироста
        тоже доезжает здесь), либо повтор показанного."""
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
        """Фоновая задача сессии: досылает отложенное и освежает черновик
        в простое. Спит ровно до следующего момента, когда есть что делать
        (_next_due), и просыпается раньше по self._wake на новый push —
        не опрашивает по таймеру."""
        while True:
            now = self._clock()
            idle_left = self._last_push_at + _KEEPALIVE_MAX_IDLE_S - now
            if idle_left <= 0:
                return  # брошенная сессия — см. _KEEPALIVE_MAX_IDLE_S
            due = self._next_due()
            if due is None:
                return  # финализировано — черновика больше нет
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
                # Страховка от холостого цикла: сделать было нечего (срок
                # посчитан по состоянию, которое успело смениться) — ждём
                # следующего push или секунду, а не крутимся впустую.
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=1.0)

    def _ensure_pump(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())
        self._wake.set()

    async def _offer(self, draft: _Draft) -> None:
        """Новое содержимое от вызывающего: встаёт в очередь (вытесняя
        неотправленное), уходит сразу, если ограничитель разрешает, иначе
        его дошлёт фоновая задача."""
        self._last_push_at = self._clock()
        async with self._lock:
            if self._pending is not None:
                self._stats.coalesced += 1
            self._pending = draft
            if self._time_ok(self._clock()) and self._content_ready(draft):
                await self._send_pending()
        self._ensure_pump()

    async def _stop_background(self) -> None:
        """Остановить фоновую задачу (ДОЖДАВШИСЬ её — отменённая задача
        могла быть посреди отправки черновика) и typing."""
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
        """Гарантированная очистка на случай, что сессию бросили, не дойдя ни
        до finalize()/finalize_status(), ни до другого штатного выхода —
        необработанное исключение или отмена задачи хендлера (живая находка
        при аудите механизма пробуждения 2026-08-26, Приложение 5 плана:
        раньше keep-alive в таких случаях просто тикал сам по себе до
        защитного потолка — до получаса устаревшего статуса в чате после
        того, как ход уже фактически завершился с ошибкой где-то ещё).

        Не шлёт никакого сообщения — вызывающий (см. bot/handlers/ai.py::
        _do_ask_and_reply) уже отправил или отправит своё собственное
        объяснение сбоя; здесь только останавливаем фоновую задачу/typing.
        Идемпотентна — безопасно звать даже если сессия уже штатно
        финализирована (тогда это no-op, кроме итоговой строки в лог —
        она пишется один раз, этап 34.3)."""
        await self._stop_background()
        self._clear_draft_state()
        self._log_summary()

    def _log_summary(self) -> None:
        if self._summary_logged:
            return
        if not self._stats.any() and self._typing.sends == 0:
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
        """Черновик настоящего текста ответа (on_partial) — дедуп по
        последнему принятому, дальше ограничитель (см. _offer).

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
        if not self._policy.streaming:
            # Без черновика единственный знак, что ответ идёт, — typing.
            await self._typing.start()
            return
        markdown = ALFRED_PREFIX_MD + markdown_body if with_prefix else markdown_body
        await self._offer(_Draft("md", InputRichMessage(markdown=markdown), len(markdown_body)))

    async def _push_thinking(self, text: str) -> None:
        signature = ("think", text)
        if not text or signature == self._last_sent:
            return
        self._last_sent = signature
        if not self._policy.streaming:
            # Статусы без черновика не показываем (любой черновик в личке —
            # то, что вешает клиент), но ответ уже в работе — typing.
            await self._typing.start()
            return
        message = InputRichMessage(blocks=[InputRichBlockThinking(text=text)])
        await self._offer(_Draft("think", message))

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
        # Этап 34.3 — гонка финала: раньше keep-alive и typing гасились
        # ПОСЛЕ sendRichMessage, и тик keep-alive, успевший начаться во
        # время финала, мог всплыть устаревшим черновиком уже после
        # реплики (или лишним «печатает»). Теперь сначала фоновая задача
        # останавливается (с ожиданием отменённой — она могла быть посреди
        # отправки) и гаснет typing, и только потом, под тем же замком, что
        # и черновики, уходит реплика.
        await self._stop_background()
        async with self._lock:
            # Реальное сообщение вытесняет активный черновик той же сессии
            # (платформенная семантика — см. push_status), поэтому
            # сигнатура последнего показанного черновика больше не отражает
            # экран: следующий push_status/on_partial не должен считать её
            # актуальной; отложенный черновик тоже устарел.
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
