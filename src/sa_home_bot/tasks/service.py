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
или спит, пробуем разбудить его заранее (тот же WoL-путь, что /wake, см.
wake_core.py) — единственная попытка; если к моменту fire_loop dst всё ещё
не тёплый, задача сразу считается неудачной, без второй попытки на самом
сроке (решение пользователя 2026-07-24: холодный старт до ~150с не должен
растягивать сам момент срабатывания)."""

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
    ERR_UNKNOWN_ACTION,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.tasks import protocol

log = logging.getLogger(__name__)

PREWAKE_LEAD_S = 300.0
POLL_INTERVAL_S = 30.0
_PRESENCE_TIMEOUT_S = 3.0
WAKE_POLL_TIMEOUT_S = 90.0
WAKE_POLL_INTERVAL_S = 3.0
# Прогрев самой цели (например ACTION_WARMUP службы llm) — не все службы
# такое объявляют, отсутствие поддержки (ERR_UNKNOWN_ACTION) — не сбой.
WARMUP_TIMEOUT_S = 180.0

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
            float(timeout_s) if timeout_s else 60.0,
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
            asyncio.create_task(self._prewake_one_safe(row), name=f"tasks-prewake-{row['id']}")

    async def _prewake_one_safe(self, row: dict) -> None:
        try:
            await self._prewake_one(row)
        except Exception:  # noqa: BLE001 — фоновая задача, сбой не должен пропасть молча
            log.exception("tasks: сбой прогрева задачи id=%s", row["id"])

    async def _prewake_one(self, row: dict) -> None:
        dst = Address(node=row["dst_node"], service=row["dst_service"])
        meta = json.loads(row["meta_json"])
        try:
            state = await asyncio.wait_for(self._node_link.get_state(dst=dst), _PRESENCE_TIMEOUT_S)
        except (ServiceUnavailableError, ProtoError, TimeoutError):
            state = None
        if state is not None and not state.get("asleep"):
            return  # уже тёплая — тихо, показывать нечего

        await self._emit(
            protocol.EVENT_TASK_PREWAKE, {"task_id": row["id"], "meta": meta, "status": "waking"}
        )
        if state is None:
            outcome = await wake_core.wake_swarm_node_core(
                self._node_link, self._store, row["dst_node"]
            )
            became = outcome.ok and await wake_core.wait_for_service(
                self._node_link,
                row["dst_node"],
                row["dst_service"],
                WAKE_POLL_TIMEOUT_S,
                WAKE_POLL_INTERVAL_S,
            )
            if not became:
                await self._emit(
                    protocol.EVENT_TASK_PREWAKE,
                    {
                        "task_id": row["id"],
                        "meta": meta,
                        "status": "failed",
                        "reason": "unreachable",
                    },
                )
                return

        warmed = await self._try_warmup(dst)
        if warmed:
            await self._emit(
                protocol.EVENT_TASK_PREWAKE, {"task_id": row["id"], "meta": meta, "status": "ready"}
            )
        else:
            await self._emit(
                protocol.EVENT_TASK_PREWAKE,
                {"task_id": row["id"], "meta": meta, "status": "failed", "reason": "warmup_failed"},
            )

    async def _try_warmup(self, dst: Address) -> bool:
        """Лучшее из возможного: дёргаем условное действие ``warmup`` у
        цели (llm/service.py его объявляет) — отсутствие поддержки
        (ERR_UNKNOWN_ACTION) не ошибка, просто у этой службы нет отдельного
        прогрева (dst и так уже отвечает — этого достаточно)."""
        try:
            await self._node_link.command("warmup", {}, dst=dst, timeout=WARMUP_TIMEOUT_S)
            return True
        except ProtoError as exc:
            return exc.code == ERR_UNKNOWN_ACTION
        except ServiceUnavailableError:
            return False

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

    async def _fire_one(self, row: dict) -> None:
        dst = Address(node=row["dst_node"], service=row["dst_service"])
        meta = json.loads(row["meta_json"])

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
        # За PREWAKE_LEAD_S уже была одна полноценная попытка прогрева (см.
        # _prewake_one) — второй раз ждать здесь ещё 90-150с не будем
        # (решение пользователя 2026-07-24), сразу проверяем и, если не
        # готова, докладываем неудачу.
        try:
            state = await asyncio.wait_for(self._node_link.get_state(dst=dst), _PRESENCE_TIMEOUT_S)
        except (ServiceUnavailableError, ProtoError, TimeoutError):
            state = None
        if state is None or state.get("asleep"):
            await self._emit(
                protocol.EVENT_TASK_RESULT,
                {
                    "task_id": row["id"],
                    "meta": meta,
                    "ok": False,
                    "error": "цель недоступна/не прогрета к моменту срабатывания",
                },
            )
            return

        task_args = json.loads(row["args_json"])
        messages = list(task_args.get("messages") or [])
        tool_ctx = ai_tools.ToolContext(
            chat_id=meta.get("chat_id"),
            dialogue_id=meta.get("dialogue_id"),
            trigger_message_id=meta.get("trigger_message_id"),
            settings=self._settings,
            node_link=self._node_link,
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
            await self._emit(
                protocol.EVENT_TASK_RESULT,
                {"task_id": row["id"], "meta": meta, "ok": False, "error": str(exc)},
            )
            return
        await self._emit(
            protocol.EVENT_TASK_RESULT,
            {"task_id": row["id"], "meta": meta, "ok": True, "result": {"response": raw}},
        )
