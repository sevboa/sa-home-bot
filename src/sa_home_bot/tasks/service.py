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
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.llm_chat import run_chat_loop
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS, is_service_unavailable
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import undeclared_action
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol

log = logging.getLogger(__name__)

PREWAKE_LEAD_S = 600.0
POLL_INTERVAL_S = 30.0
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
        self._book = SubscriptionBook.from_config(
            settings.subscriptions, settings.guest_subscriptions
        )
        self._prewake_inflight: dict[int, asyncio.Task] = {}
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
                        ActionParam(
                            name="await_event",
                            type="string",
                            required=False,
                            title="Ждать событие {node, event_type} раньше due_at",
                        ),
                    ),
                ),
                ActionSpec(
                    id=protocol.ACTION_FIRE_NOW,
                    title="Разбудить задачу немедленно",
                    params=(
                        ActionParam(name="task_id", type="int", required=True, title="Id задачи"),
                    ),
                ),
                ActionSpec(
                    id=protocol.ACTION_MATCH_EVENT,
                    title="Сверить событие роя с ожидающими задачами",
                    params=(
                        ActionParam(name="node", type="string", required=True, title="Нода"),
                        ActionParam(
                            name="event_type", type="string", required=True, title="Тип события"
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
        if action == protocol.ACTION_FIRE_NOW:
            return await self._fire_now(args)
        if action == protocol.ACTION_MATCH_EVENT:
            return await self._match_event(args)
        raise undeclared_action(action)

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
        await_event = args.get("await_event")
        if await_event is not None:
            if (
                not isinstance(await_event, dict)
                or not isinstance(await_event.get("node"), str)
                or not await_event.get("node")
                or not isinstance(await_event.get("event_type"), str)
                or not await_event.get("event_type")
            ):
                raise ProtoError(
                    ERR_BAD_REQUEST,
                    "await_event должен быть {node: str, event_type: str}",
                )

        now = datetime.now(tz=UTC)
        task_id = await self._store.create_task(
            dst_node,
            dst_service,
            task_action,
            task_args or {},
            float(timeout_s) if timeout_s else self._settings.llm.request_timeout_s,
            meta or {},
            due_at.astimezone(UTC),
            now,
        )
        if await_event is not None:
            await self._store.add_event_waiter(
                task_id, await_event["node"], await_event["event_type"], now
            )
        return {"task_id": task_id}

    async def _fire_now(self, args: dict[str, Any]) -> dict[str, Any]:
        task_id = args.get("task_id")
        if not isinstance(task_id, int):
            raise ProtoError(ERR_BAD_REQUEST, "task_id обязателен и должен быть числом")
        row = await self._store.get_pending_task(task_id)
        if row is None:
            return {"fired": False, "reason": "нет такой задачи или уже сработала"}
        now = datetime.now(tz=UTC)
        await self._store.mark_task_fired(row["id"], now)
        await self._store.pop_event_waiter(row["id"])
        asyncio.create_task(self._fire_one_safe(row), name=f"task-fire-now-{row['id']}")
        return {"fired": True}

    async def _match_event(self, args: dict[str, Any]) -> dict[str, Any]:
        node = args.get("node")
        event_type = args.get("event_type")
        node_ok = isinstance(node, str) and bool(node)
        event_ok = isinstance(event_type, str) and bool(event_type)
        if not node_ok or not event_ok:
            raise ProtoError(ERR_BAD_REQUEST, "node и event_type обязательны")
        task_id = await self._store.pop_event_waiter_for(node, event_type)
        if task_id is None:
            return {"matched": False}
        fired = await self._fire_now({"task_id": task_id})
        return {"matched": bool(fired.get("fired")), "task_id": task_id}


    async def prewake_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._prewake_due()
            except Exception:
                log.exception("tasks: сбой прогрева")

    async def _prewake_due(self) -> None:
        deadline = datetime.now(tz=UTC) + timedelta(seconds=PREWAKE_LEAD_S)
        for row in await self._store.tasks_needing_prewake(deadline):
            if row["action"] == protocol.ACTION_TIMER:
                await self._store.mark_task_prewake_done(row["id"])
                continue
            await self._store.mark_task_prewake_done(row["id"])
            task_id = row["id"]
            job = asyncio.create_task(self._prewake_one_safe(row), name=f"tasks-prewake-{task_id}")
            self._prewake_inflight[task_id] = job
            job.add_done_callback(lambda _job, tid=task_id: self._prewake_inflight.pop(tid, None))

    async def _prewake_one_safe(self, row: dict) -> None:
        try:
            await self._prewake_one(row)
        except Exception:
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
            return

        await self._announce_waking(task_id, meta)
        outcome = await wake_core.ensure_service_ready(
            self._node_link,
            self._store,
            row["dst_node"],
            row["dst_service"],
            warmup_timeout_s=self._settings.llm.warmup_timeout_s,
        )
        log.info("tasks: прогрев задачи id=%s -> %s", task_id, outcome)
        if outcome == wake_core.READY:
            await self._emit(
                protocol.EVENT_TASK_PREWAKE, {"task_id": task_id, "meta": meta, "status": "ready"}
            )
            return
        log.warning(
            "tasks: прогрев задачи id=%s не удался (%s), повторим на сроке", task_id, outcome
        )

    async def _announce_waking(self, task_id: int, meta: dict) -> None:
        if task_id in self._waking_announced:
            return
        self._waking_announced.add(task_id)
        await self._emit(
            protocol.EVENT_TASK_PREWAKE, {"task_id": task_id, "meta": meta, "status": "waking"}
        )


    async def fire_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                await self._fire_due()
            except Exception:
                log.exception("tasks: сбой опроса очереди")

    async def _fire_due(self) -> None:
        now = datetime.now(tz=UTC)
        for row in await self._store.due_tasks(now):
            await self._store.mark_task_fired(row["id"], now)
            await self._store.pop_event_waiter(row["id"])
            asyncio.create_task(self._fire_one_safe(row), name=f"task-fire-{row['id']}")

    async def _fire_one_safe(self, row: dict) -> None:
        try:
            await self._fire_one(row)
        except Exception:
            log.exception("tasks: сбой срабатывания задачи id=%s", row["id"])
        finally:
            self._waking_announced.discard(row["id"])

    async def _fire_one(self, row: dict) -> None:
        dst = Address(node=row["dst_node"], service=row["dst_service"])
        meta = json.loads(row["meta_json"])

        job = self._prewake_inflight.get(row["id"])
        if job is not None and not job.done():
            log.info("tasks: задача id=%s ждёт свой идущий прогрев", row["id"])
            await asyncio.wait([job], timeout=FIRE_GRACE_S)

        if row["action"] == protocol.ACTION_CHAT_LOOP:
            await self._fire_chat_loop(row, dst, meta)
            return
        if row["action"] == protocol.ACTION_TIMER:
            await self._emit(
                protocol.EVENT_TASK_RESULT,
                {"task_id": row["id"], "meta": meta, "ok": True, "result": {}},
            )
            return

        task_args = json.loads(row["args_json"])
        try:
            result = await self._node_link.command(
                row["action"], task_args, dst=dst, timeout=row["timeout_s"]
            )
        except SERVICE_CALL_ERRORS as exc:
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
                wake_timeout_s=min(wake_core.WAKE_POLL_TIMEOUT_S, max(deadline - loop.time(), 0.0)),
                warmup_timeout_s=max(deadline - loop.time(), 0.0),
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
        awaited_node = meta.get("awaited_node")
        awaited_event = meta.get("awaited_event")
        tool_ctx = ai_tools.ToolContext(
            chat_id=chat_id,
            dialogue_id=meta.get("dialogue_id"),
            trigger_message_id=meta.get("trigger_message_id"),
            settings=self._settings,
            node_link=self._node_link,
            subscription=self._book.for_chat(chat_id) if isinstance(chat_id, int) else None,
            woken_by=(awaited_node, awaited_event)
            if isinstance(awaited_node, str) and isinstance(awaited_event, str)
            else None,
            message_thread_id=meta.get("message_thread_id"),
            book=self._book,
            emit=self._emit,
        )
        async def _emit_tool_call(name: str, call_args: dict[str, Any], result: str) -> None:
            await self._emit(
                protocol.EVENT_TOOL_CALL, {"name": name, "args": call_args, "result": result}
            )

        reason = task_args.get("reason")
        if not isinstance(reason, str):
            legacy = task_args.get("think", True)
            reason = "off" if legacy is False else "high"
        while True:
            try:
                raw = await run_chat_loop(
                    self._node_link,
                    dst,
                    row["timeout_s"],
                    messages,
                    tool_ctx,
                    reason,
                    telegram_chat_id=task_args.get("chat_id"),
                    log_chat_id=row["id"],
                    on_tool_call=_emit_tool_call,
                )
                break
            except SERVICE_CALL_ERRORS as exc:
                out_of_time = deadline - loop.time() <= FIRE_RETRY_INTERVAL_S
                if not is_service_unavailable(exc) or out_of_time:
                    log.warning(
                        "tasks: задача id=%s — запрос к модели не удался: %s", task_id, exc
                    )
                    await self._emit(
                        protocol.EVENT_TASK_RESULT,
                        {"task_id": task_id, "meta": meta, "ok": False, "error": str(exc)},
                    )
                    return
                log.warning(
                    "tasks: задача id=%s — модель недоступна (%s), повтор через %.0fс",
                    task_id,
                    exc,
                    FIRE_RETRY_INTERVAL_S,
                )
                await asyncio.sleep(FIRE_RETRY_INTERVAL_S)
        log.info("tasks: задача id=%s доставлена (%d символов)", task_id, len(raw))
        await self._emit(
            protocol.EVENT_TASK_RESULT,
            {"task_id": task_id, "meta": meta, "ok": True, "result": {"response": raw}},
        )
