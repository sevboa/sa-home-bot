"""TasksService — генерализованные отложенные задачи роя (служба tasks).

Замена старого тула remind (писал готовый текст прямо в БД бота, доставка
— константная фраза, живая находка 2026-07-24: пользователь явно попросил
отдельный сервис роя, а не доработку внутри бота — задачи может ставить
любой узел по протоколу, не только тул remind из /ai).

Задача = due_at + произвольная протокольная команда (dst_node/dst_service/
action/args) + timeout_s + непрозрачные meta — эта служба их не читает,
только хранит и возвращает целиком в событии ``task_result``/``task_prewake``
тому, кто задачу создал (обычно бот, bot/node_events.py — доставка в
Telegram и запись в ai_turns остаются у него: у этой службы нет доступа ни
к Telegram, ни к БД бота, только к своей очереди, как и у остальных служб
роя).

Один специальный ``action`` — ``chat_loop`` (tasks/protocol.py) — не
форвардится как обычная команда, а прогоняется через полный цикл
tool-calling поверх llm.chat (sa_home_bot.llm_chat.run_chat_loop): это
единственный сейчас существующий «богатый» тип задачи, ради которого весь
этот сервис и был выделен — модель может поставить такую задачу САМА СЕБЕ
через тул remind (self-scheduling, инструменту исполнения всё равно, кто
создал задачу — человек через /ai или модель во время собственного ответа).

Прогрев (prewake_loop) — за PREWAKE_LEAD_S до due_at, если dst не отвечает
или спит, будим его заранее (тот же WoL-путь, что /wake, см. wake_core.py),
чтобы ответ пришёл ровно в срок.

Живая находка 2026-07-27 (инцидент 19:34-19:39, напоминание не доставлено).
Прежняя схема — единственная попытка прогрева и мгновенный отказ на самом
сроке, если цель не тёплая (решение пользователя 2026-07-24: холодный старт
не должен растягивать момент срабатывания) — на практике дала худшее из
двух: WoL из этой службы не уходил ВООБЩЕ (пустой кэш wake-реквизитов, см.
wake_core.resolve_wake_info), 5 минут форы пропали впустую, а на сроке
задача сдалась за 6 секунд. Новая схема (решение пользователя 2026-07-27):
фора увеличена до 10 минут, а на сроке служба сама будит цель и повторяет
попытки до FIRE_GRACE_S после due_at — лучше напоминание с опозданием в
пару минут, чем «пропущено»."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sa_home_bot import __version__, wake_core
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm_chat import run_chat_loop
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol

log = logging.getLogger(__name__)

# За сколько до срока начинать будить цель. Решение пользователя 2026-07-27:
# 10 минут — холодный старт winpc плюс загрузка 35B-модели в память заведомо
# укладываются, и ответ приходит ровно в срок, а не после него.
PREWAKE_LEAD_S = 600.0
POLL_INTERVAL_S = 30.0
# Сколько после due_at ещё пытаться, если цель так и не поднялась (решение
# пользователя 2026-07-27). Дальше — честное "пропущено": напоминание,
# опоздавшее на десятки минут, уже вреднее молчания.
FIRE_GRACE_S = 300.0
FIRE_RETRY_INTERVAL_S = 30.0

EventEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _noop_emit(event_type: str, data: dict[str, Any]) -> None:
    pass


class TasksService:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        node_link: ServiceLink,
        *,
        emit: EventEmitter = _noop_emit,
    ) -> None:
        self._settings = settings
        self._store = store
        self._node_link = node_link
        self._emit = emit
        self._node = socket.gethostname()
        # Права заказчика задачи. Читаются из того же конфига, что у бота
        # (подписки — часть Settings), потому что БД бота этой службе
        # недоступна. Резолв — в МОМЕНТ СРАБАТЫВАНИЯ по meta["chat_id"], а не
        # снимком в meta при создании: если право отобрали, пока задача ждала
        # в очереди, отложенный ответ им уже не воспользуется. Не
        # «оптимизировать» в снимок — это и есть смысл конструкции.
        self._book = SubscriptionBook.from_config(settings.subscriptions)
        # Идущие прогревы: fire_loop дожидается прогрева СВОЕЙ задачи, а не
        # читает presence посреди него. Живая находка 2026-07-27: циклы
        # независимы, и задача со сроком меньше PREWAKE_LEAD_S получала
        # прогрев и срабатывание практически одновременно — presence видела
        # ещё не поднятую ноду и задача падала при живом идущем прогреве.
        self._prewake_inflight: dict[int, asyncio.Task] = {}
        # Кому уже показали «шаги» (EVENT_TASK_PREWAKE waking) — чтобы не
        # повторять то же сообщение на срабатывании.
        self._waking_announced: set[int] = set()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=protocol.SERVICE_NAME, version=__version__),
            capabilities=(),
            actions=(
                ActionSpec(
                    id=protocol.ACTION_CREATE,
                    title="Поставить отложенную задачу",
                    params=(
                        ActionParam(
                            name="due_at", type="string", required=True, title="Когда (ISO 8601)"
                        ),
                        ActionParam(
                            name="dst_node", type="string", required=True, title="Нода-адресат"
                        ),
                        ActionParam(
                            name="dst_service",
                            type="string",
                            required=True,
                            title="Служба-адресат",
                        ),
                        ActionParam(
                            name="action",
                            type="string",
                            required=True,
                            title="Действие (или chat_loop — см. protocol.py)",
                        ),
                        ActionParam(
                            name="args", type="string", required=False, title="Аргументы действия"
                        ),
                        ActionParam(
                            name="timeout_s",
                            type="float",
                            required=False,
                            title="Таймаут исполнения (сек)",
                        ),
                        ActionParam(
                            name="meta",
                            type="string",
                            required=False,
                            title="Непрозрачные метаданные заказчика",
                        ),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {"node": self._node, "service": protocol.SERVICE_NAME}

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == protocol.ACTION_CREATE:
            return await self._create(args)
        raise ValueError(f"необъявленное действие: {action}")

    async def _create(self, args: dict[str, Any]) -> dict[str, Any]:
        due_raw = args.get("due_at")
        dst_node = args.get("dst_node")
        dst_service = args.get("dst_service")
        task_action = args.get("action")
        if not all(isinstance(v, str) and v for v in (due_raw, dst_node, dst_service, task_action)):
            raise ProtoError(
                ERR_BAD_REQUEST,
                "due_at/dst_node/dst_service/action обязательны и должны быть непустыми строками",
            )
        try:
            due_at = datetime.fromisoformat(due_raw)
        except ValueError as exc:
            raise ProtoError(ERR_BAD_REQUEST, f"due_at не в формате ISO 8601: {exc}") from exc
        if due_at.tzinfo is None:
            due_at = due_at.astimezone()
        task_args = args.get("args")
        if task_args is not None and not isinstance(task_args, dict):
            raise ProtoError(ERR_BAD_REQUEST, "args должен быть объектом")
        meta = args.get("meta")
        if meta is not None and not isinstance(meta, dict):
            raise ProtoError(ERR_BAD_REQUEST, "meta должен быть объектом")
        timeout_s = args.get("timeout_s")
        if timeout_s is not None and not isinstance(timeout_s, int | float):
            raise ProtoError(ERR_BAD_REQUEST, "timeout_s должен быть числом")

        now = datetime.now(tz=UTC)
        task_id = await self._store.create_task(
            dst_node,
            dst_service,
            task_action,
            task_args or {},
            # Дефолт — бюджет запроса к модели: единственный «богатый» тип
            # задачи (chat_loop) заведомо не укладывается в прежние 60с, а
            # обычным командам такой запас не мешает.
            float(timeout_s) if timeout_s else self._settings.llm.request_timeout_s,
            meta or {},
            due_at.astimezone(UTC),
            now,
        )
        return {"task_id": task_id}

    # --- прогрев заранее ---

    async def prewake_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._prewake_due()
            except Exception:  # noqa: BLE001 — сбой одного тика не должен ронять цикл
                log.exception("tasks: сбой прогрева")

    async def _prewake_due(self) -> None:
        deadline = datetime.now(tz=UTC) + timedelta(seconds=PREWAKE_LEAD_S)
        for row in await self._store.tasks_needing_prewake(deadline):
            # Сразу помечаем — иначе следующий тик (через 30с) попробует
            # снова, пока предыдущая попытка (прогрев может занять минуты)
            # ещё идёт.
            await self._store.mark_task_prewake_done(row["id"])
            task_id = row["id"]
            job = asyncio.create_task(self._prewake_one_safe(row), name=f"tasks-prewake-{task_id}")
            self._prewake_inflight[task_id] = job
            job.add_done_callback(lambda _job, tid=task_id: self._prewake_inflight.pop(tid, None))

    async def _prewake_one_safe(self, row: dict) -> None:
        try:
            await self._prewake_one(row)
        except Exception:  # noqa: BLE001 — фоновая задача, сбой не должен пропасть молча
            log.exception("tasks: сбой прогрева задачи id=%s", row["id"])

    async def _prewake_one(self, row: dict) -> None:
        task_id = row["id"]
        meta = json.loads(row["meta_json"])
        dst = Address(node=row["dst_node"], service=row["dst_service"])

        state = await wake_core.fetch_state(
            self._node_link, dst, timeout_s=wake_core.PRESENCE_TIMEOUT_S
        )
        if state is not None and not state.get("asleep"):
            log.info("tasks: задача id=%s — цель уже тёплая, прогрев не нужен", task_id)
            return  # тихо: показывать пользователю нечего

        await self._announce_waking(task_id, meta)
        outcome = await wake_core.ensure_service_ready(
            self._node_link, self._store, row["dst_node"], row["dst_service"]
        )
        log.info("tasks: прогрев задачи id=%s -> %s", task_id, outcome)
        if outcome == wake_core.READY:
            await self._emit(
                protocol.EVENT_TASK_PREWAKE, {"task_id": task_id, "meta": meta, "status": "ready"}
            )
            return
        # Не сдаёмся насовсем: на сроке будет ещё одна серия попыток
        # (_fire_chat_loop), поэтому здесь НЕ эмитим failed — иначе
        # пользователь получил бы «не смог» от ещё не проигранной задачи.
        log.warning(
            "tasks: прогрев задачи id=%s не удался (%s), повторим на сроке", task_id, outcome
        )

    async def _announce_waking(self, task_id: int, meta: dict) -> None:
        """«Шаги» показываем один раз на задачу — прогрев и срабатывание
        могут оба упереться в спящую ноду, дублировать сообщение незачем."""
        if task_id in self._waking_announced:
            return
        self._waking_announced.add(task_id)
        await self._emit(
            protocol.EVENT_TASK_PREWAKE, {"task_id": task_id, "meta": meta, "status": "waking"}
        )

    # --- срабатывание ---

    async def fire_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._fire_due()
            except Exception:  # noqa: BLE001
                log.exception("tasks: сбой опроса очереди")

    async def _fire_due(self) -> None:
        now = datetime.now(tz=UTC)
        for row in await self._store.due_tasks(now):
            # Списываем сразу — исполнение теперь небыстрое (живой запрос к
            # модели, до row["timeout_s"]) и не должно попасть под
            # повторный тик опроса, пока предыдущая попытка ещё идёт.
            await self._store.mark_task_fired(row["id"], now)
            asyncio.create_task(self._fire_one_safe(row), name=f"task-fire-{row['id']}")

    async def _fire_one_safe(self, row: dict) -> None:
        try:
            await self._fire_one(row)
        except Exception:  # noqa: BLE001
            log.exception("tasks: сбой срабатывания задачи id=%s", row["id"])
        finally:
            # Задача отыграна — её пометка о показанных «шагах» больше не
            # нужна (иначе множество росло бы всю жизнь процесса).
            self._waking_announced.discard(row["id"])

    async def _fire_one(self, row: dict) -> None:
        dst = Address(node=row["dst_node"], service=row["dst_service"])
        meta = json.loads(row["meta_json"])

        # Прогрев этой же задачи может быть ещё в работе (срок ближе, чем
        # PREWAKE_LEAD_S) — дождаться его, а не гонять с ним наперегонки.
        job = self._prewake_inflight.get(row["id"])
        if job is not None and not job.done():
            log.info("tasks: задача id=%s ждёт свой идущий прогрев", row["id"])
            await asyncio.wait([job], timeout=FIRE_GRACE_S)

        if row["action"] == protocol.ACTION_CHAT_LOOP:
            await self._fire_chat_loop(row, dst, meta)
            return

        task_args = json.loads(row["args_json"])
        try:
            result = await self._node_link.command(
                row["action"], task_args, dst=dst, timeout=row["timeout_s"]
            )
        except (ServiceUnavailableError, ProtoError) as exc:
            await self._emit(
                protocol.EVENT_TASK_RESULT,
                {"task_id": row["id"], "meta": meta, "ok": False, "error": str(exc)},
            )
            return
        await self._emit(
            protocol.EVENT_TASK_RESULT,
            {"task_id": row["id"], "meta": meta, "ok": True, "result": result},
        )

    async def _fire_chat_loop(self, row: dict, dst: Address, meta: dict) -> None:
        # Решение пользователя 2026-07-27: на сроке служба сама доводит цель
        # до готовности (WoL + ожидание + прогрев) и повторяет попытки до
        # FIRE_GRACE_S. Прежний вариант — presence на 6с и мгновенный отказ —
        # означал, что спящая машина гарантированно съедала напоминание
        # (инцидент 19:34, см. докстринг модуля).
        task_id = row["id"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + FIRE_GRACE_S
        outcome = wake_core.UNREACHABLE
        while True:
            outcome = await wake_core.ensure_service_ready(
                self._node_link,
                self._store,
                row["dst_node"],
                row["dst_service"],
                # Не выходить за общий бюджет задачи ради одного ожидания WoL.
                wake_timeout_s=min(wake_core.WAKE_POLL_TIMEOUT_S, max(deadline - loop.time(), 0.0)),
            )
            if outcome == wake_core.READY:
                break
            await self._announce_waking(task_id, meta)
            if deadline - loop.time() <= FIRE_RETRY_INTERVAL_S:
                break
            log.warning(
                "tasks: задача id=%s — цель не готова (%s), повтор через %.0fс",
                task_id,
                outcome,
                FIRE_RETRY_INTERVAL_S,
            )
            await asyncio.sleep(FIRE_RETRY_INTERVAL_S)

        if outcome != wake_core.READY:
            log.warning("tasks: задача id=%s провалена: цель так и не поднялась", task_id)
            await self._emit(
                protocol.EVENT_TASK_RESULT,
                {
                    "task_id": task_id,
                    "meta": meta,
                    "ok": False,
                    "error": "цель недоступна/не прогрета к моменту срабатывания",
                },
            )
            return

        task_args = json.loads(row["args_json"])
        messages = list(task_args.get("messages") or [])
        chat_id = meta.get("chat_id")
        tool_ctx = ai_tools.ToolContext(
            chat_id=chat_id,
            dialogue_id=meta.get("dialogue_id"),
            trigger_message_id=meta.get("trigger_message_id"),
            settings=self._settings,
            node_link=self._node_link,
            # См. self._book: права на момент срабатывания, не на момент
            # создания задачи. Подписки уже нет → None → тулы без прав.
            subscription=self._book.for_chat(chat_id) if isinstance(chat_id, int) else None,
        )
        try:
            raw = await run_chat_loop(
                self._node_link,
                dst,
                row["timeout_s"],
                messages,
                tool_ctx,
                think=bool(task_args.get("think", True)),
                telegram_chat_id=task_args.get("chat_id"),
                log_chat_id=row["id"],
            )
        except (ServiceUnavailableError, ProtoError) as exc:
            log.warning("tasks: задача id=%s — запрос к модели не удался: %s", task_id, exc)
            await self._emit(
                protocol.EVENT_TASK_RESULT,
                {"task_id": task_id, "meta": meta, "ok": False, "error": str(exc)},
            )
            return
        log.info("tasks: задача id=%s доставлена (%d символов)", task_id, len(raw))
        await self._emit(
            protocol.EVENT_TASK_RESULT,
            {"task_id": task_id, "meta": meta, "ok": True, "result": {"response": raw}},
        )
