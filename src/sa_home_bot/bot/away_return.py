"""«Альфред в городе» (Этап 51): проход раз в минуту и возвращение.

``AwayRunner`` — периодическая задача бота (не таймер в памяти: вся
арифметика сроков идёт по записи ``app_state["alfred_away"]``, которую
правят и бот, и CLI): сообщения владельцу от CLI, напоминание за 15 минут до
срока, принудительное возвращение по потолку 24 ч и разбор очереди, когда
возвращение объявлено (``/back``, кнопка, потолок).

``AwayReturn`` — сам разбор очереди (IMPLEMENTATION_PLAN.md, этап 51,
«Возвращение»):

1. ждём готовности LLM на mycraft (существующая логика пробуждения,
   wake_core.ensure_service_ready); пока не готова — гости видят «вот-вот
   вернётся», следующий проход повторит попытку;
2. чаты из ``pending`` — строго по одному (у Ollama одна очередь);
3. в чате: отложенные голосовые по порядку через voice_stt (транскрипт
   подменяет заглушку в той же записи ai_turns), затем ОДИН запрос к Альфреду
   через обычный путь (bot/handlers/ai.py::_ask_and_reply) с system-вставкой
   «ты вернулся из города»; ответ — реплаем на последнее сообщение;
4. чат снимается с ``pending`` только после доставленного ответа; состояние
   разбора — в БД (ai_turns, away_media, запись alfred_away), поэтому падение
   на середине продолжается с места остановки.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import Chat, Message, User

from sa_home_bot import wake_core
from sa_home_bot.bot import ai_flow, dialogue_context, voice_stt
from sa_home_bot.bot.away import (
    MAX_VOICE_SECONDS_PER_CHAT,
    MAX_VOICES_PER_CHAT,
    MEDIA_FAILED_TEXT,
    MEDIA_SKIPPED_TEXT,
    PHASE_AWAY,
    PHASE_RETURNING,
    REMIND_BEFORE,
    AwayService,
    AwayState,
    is_media_stub,
    local_tz,
    reminder_keyboard,
    status_text,
)
from sa_home_bot.bot.notifier import Notifier, notify_admins
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm.prompt import wrap_context_note
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)

TICK_S = 60.0

# System-вставка перед последней репликой собеседника — в образе, текст из плана.
RETURN_NOTE = (
    "Ты только что вернулся в замок из города, куда спускался по делам; пока "
    "тебя не было, тебе написали — ответь на всё разом, в образе, можно коротко "
    "извиниться за ожидание."
)

CEILING_TEXT = "Прошли сутки с отъезда Альфреда — возвращаю принудительно (потолок 24 ч)."
DROPPED_TEXT = (
    "Не вышло ответить чату {chat_id} после возвращения (несколько попыток) — "
    "снял его с очереди. Сообщения остались в истории."
)


def build_message(bot: Bot | None, chat_id: int, entry: dict[str, Any]) -> Message:
    """Синтетическое «последнее сообщение» чата из записи очереди — чтобы
    вернуться по тому же пути ``_ask_and_reply``, что и живой диалог (ответ
    реплаем на это сообщение, топик, собеседник для заметки Альфреду)."""
    user = entry.get("user")
    thread_id = entry.get("thread_id")
    message = Message(
        message_id=int(entry["last_message_id"]),
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id, type=entry.get("chat_type") or "private"),
        from_user=(
            User(
                id=int(user["id"]),
                is_bot=False,
                first_name=user.get("first_name") or "",
                last_name=user.get("last_name"),
                username=user.get("username"),
            )
            if user
            else None
        ),
        text=entry.get("text") or None,
        message_thread_id=thread_id,
        is_topic_message=True if entry.get("is_topic") else None,
    )
    return message.as_(bot) if bot is not None else message


class AwayReturn:
    """Разбор очереди ``pending`` после возвращения Альфреда."""

    def __init__(
        self,
        service: AwayService,
        *,
        get_node_link: Callable[[], ServiceLink | None],
        store: Store,
        config: Settings,
        book: Any,
        notifier: Notifier,
        active_ai_chats: ai_flow.ActiveAiChats,
        tool_calls: Any,
        pending_actions: Any | None = None,
        interactives: Any | None = None,
    ) -> None:
        self._service = service
        self._get_node_link = get_node_link
        self._store = store
        self._config = config
        self._book = book
        self._notifier = notifier
        self._active_ai_chats = active_ai_chats
        self._tool_calls = tool_calls
        self._pending_actions = pending_actions
        self._interactives = interactives

    async def run_pass(self) -> int:
        """Один проход по очереди. Возвращает число чатов, снятых с очереди."""
        state = await self._service.load()
        if state is None or state.phase != PHASE_RETURNING:
            return 0
        node_link = self._get_node_link()
        if node_link is None:
            return 0
        if not state.pending:
            await self._service.clear()
            return 0
        outcome = await wake_core.ensure_service_ready(
            node_link,
            self._store,
            ai_flow.LLM_NODE,
            ai_flow.LLM_SERVICE,
            warmup_timeout_s=self._config.llm.warmup_timeout_s,
        )
        if outcome != wake_core.READY:
            log.info("away: LLM ещё не готова (%s) — повторю на следующем проходе", outcome)
            return 0

        done = 0
        failed: set[str] = set()  # чат, на который не вышло ответить, — до следующего прохода
        # Пока есть прогресс: пока отвечали одному чату, в другой (или в тот же)
        # могли написать ещё — тогда он остался в очереди и идёт на следующий круг.
        while True:
            state = await self._service.load()
            if state is None or state.phase != PHASE_RETURNING:
                return done
            order = sorted(
                (k for k in state.pending if k not in failed),
                key=lambda k: state.pending[k].get("since", ""),
            )
            progressed = False
            for key in order:
                if await self._handle_chat(node_link, int(key)):
                    done += 1
                    progressed = True
                else:
                    failed.add(key)
            if not progressed:
                return done

    async def _handle_chat(self, node_link: ServiceLink, chat_id: int) -> bool:
        """Ответить одному чату. True — чат снят с очереди с ответом;
        False — ответа нет (попытка записана)."""
        state = await self._service.load()
        entry = state.pending.get(str(chat_id)) if state else None
        if entry is None:
            return False
        try:
            # Новые голосовые могли прийти, пока разбирали прежние.
            for _ in range(3):
                if not await self._transcribe_media(node_link, chat_id):
                    break
            state = await self._service.load()
            entry = state.pending.get(str(chat_id)) if state else None
            if entry is None:
                return False
            last_id = int(entry["last_message_id"])
            dialogue_id = int(entry["dialogue_id"])
            history = await dialogue_context.load_history(
                self._store, self._config, chat_id, dialogue_id
            )
            if not history or history[-1].get("role") != "user":
                # Отвечать не на что — просто снять чат.
                await self._service.finish_chat(chat_id, last_id)
                return True
            history.insert(len(history) - 1, wrap_context_note(RETURN_NOTE))
            message = build_message(self._notifier.bot, chat_id, entry)
            raw = await self._ask(message, node_link, dialogue_id, history)
        except Exception:  # noqa: BLE001 — один чат не должен остановить остальные
            log.exception("away: возвращение — чат %s не разобран", chat_id)
            raw = None
            last_id = 0
        if raw is None:
            if await self._service.record_failure(chat_id):
                await notify_admins(
                    self._book, self._notifier, DROPPED_TEXT.format(chat_id=chat_id)
                )
            return False
        await self._service.finish_chat(chat_id, last_id)
        return True

    async def _ask(
        self,
        message: Message,
        node_link: ServiceLink,
        dialogue_id: int,
        history: list[dict[str, Any]],
    ) -> str | None:
        # Импорт здесь: хендлер сам импортирует bot/away.py.
        from sa_home_bot.bot.handlers import ai as ai_handler  # noqa: PLC0415

        return await ai_handler._ask_and_reply(
            message,
            node_link,
            self._store,
            self._config,
            self._book,
            self._notifier,
            dialogue_id,
            history,
            self._active_ai_chats,
            self._tool_calls,
            ai_handler._rich_session_for(message, self._config),
            pending_actions=self._pending_actions,
            interactives=self._interactives,
        )

    # --- голосовые ---

    async def _transcribe_media(self, node_link: ServiceLink, chat_id: int) -> bool:
        """Распознать ждущие файлы чата по порядку, подменяя заглушки в ai_turns.
        True — что-то обработано (стоит проверить, не пришло ли ещё)."""
        rows = await self._store.away_media_for_chat(chat_id)
        handled = False
        used_n = 0
        used_s = 0
        for row in rows:
            message_id = int(row["message_id"])
            turn = await self._store.ai_turn(chat_id, message_id)
            if turn is None or not is_media_stub(str(turn["content"])):
                # Уже подменено до падения — осталось убрать след.
                await self._drop_media(row)
                continue
            handled = True
            duration = int(row["duration_s"] or 0)
            if used_n >= MAX_VOICES_PER_CHAT or used_s + duration > MAX_VOICE_SECONDS_PER_CHAT:
                content = MEDIA_SKIPPED_TEXT
            elif duration > self._config.llm.stt_max_duration_s:
                content = MEDIA_FAILED_TEXT
            else:
                used_n += 1
                used_s += duration
                content = await self._recognize(node_link, chat_id, row) or MEDIA_FAILED_TEXT
            await self._store.set_ai_turn_content(chat_id, message_id, content)
            await self._drop_media(row)
        return handled

    async def _recognize(
        self, node_link: ServiceLink, chat_id: int, row: dict[str, Any]
    ) -> str:
        """Транскрипт файла; пустая строка — не вышло (текст пометки выбирает вызывающий)."""
        raw = await self._read_media(row)
        if raw is None:
            return ""
        try:
            return await voice_stt.transcribe_bytes(node_link, raw, chat_id, self._config)
        except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
            log.warning("away: голосовое не распознано (chat=%s): %s", chat_id, exc)
            return ""

    async def _read_media(self, row: dict[str, Any]) -> bytes | None:
        """Файл с диска alfred; нет его — по запасному file_id из Telegram."""
        path = row.get("path")
        if path:
            with contextlib.suppress(OSError):
                return Path(path).read_bytes()
        file_id = row.get("file_id")
        if file_id:
            try:
                buf = await self._notifier.bot.download(file_id)
                return buf.read() if buf is not None else None
            except Exception:  # noqa: BLE001
                log.warning("away: файл не скачать по file_id", exc_info=True)
        return None

    async def _drop_media(self, row: dict[str, Any]) -> None:
        """Файл и строка away_media не нужны после подмены заглушки."""
        path = row.get("path")
        if path:
            with contextlib.suppress(OSError):
                Path(path).unlink()
        await self._store.delete_away_media(int(row["chat_id"]), int(row["message_id"]))


class AwayRunner:
    """Проход раз в минуту: владельцу — сообщения CLI, напоминание, потолок;
    при возвращении — разбор очереди."""

    def __init__(
        self,
        service: AwayService,
        returner: AwayReturn,
        book: Any,
        notifier: Notifier,
        *,
        tick_s: float = TICK_S,
    ) -> None:
        self._service = service
        self._returner = returner
        self._book = book
        self._notifier = notifier
        self._tick_s = tick_s
        self._kick = asyncio.Event()

    def kick(self) -> None:
        """Не ждать минуту: ``/back`` хочет начать разбор сразу."""
        self._kick.set()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — задача живёт до останова бота
                log.exception("away: проход не удался")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), self._tick_s)
            self._kick.clear()

    async def tick(self) -> None:
        for text in await self._service.take_notices():
            await notify_admins(self._book, self._notifier, text)
        state = await self._service.load()
        if state is None:
            return
        now = self._service.now()
        if state.phase == PHASE_AWAY:
            if now >= state.ceiling:
                if await self._service.mark_phase_returning() is not None:
                    await notify_admins(self._book, self._notifier, CEILING_TEXT)
            elif now >= state.until - REMIND_BEFORE and await self._service.mark_reminded():
                await notify_admins(
                    self._book,
                    self._notifier,
                    self._reminder_text(state, now),
                    reply_markup=reminder_keyboard(),
                )
        await self._returner.run_pass()

    @staticmethod
    def _reminder_text(state: AwayState, now: datetime) -> str:
        return (
            status_text(state, now, local_tz())
            + " Продлить или вернуть?"
        )
