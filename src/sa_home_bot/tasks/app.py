"""Сборка и жизненный цикл службы tasks (отдельный процесс, обычно на той
же ноде, что бот — см. tasks/protocol.py::NODE_ID).

Как llm/app.py — минимальный proto-сервер (входящие create от кого угодно
в рое) плюс свой ServiceLink-клиент к локальной ноде (исходящие команды к
dst задач — llm.chat, WoL и т.п., см. tasks/service.py) и два фоновых
цикла (прогрев заранее, срабатывание по due_at)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.tasks.service import TasksService
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_tasks(settings: Settings) -> None:
    db = Database(settings.tasks.db_path)
    await db.open()
    await apply_migrations(db)
    store = Store(db)

    node_link = ServiceLink(
        settings.node.socket, token=settings.swarm.token, display_name="нода (tasks)"
    )
    await node_link.start()

    # server создаётся после service (тот же приём, что node/app.py::run_node
    # и llm/app.py::run_llm) — emit замыкается на переменную server, реально
    # дёргается уже после server.start().
    server: ProtoServer | None = None

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    service = TasksService(settings, store, node_link, emit=emit)
    server = ProtoServer(settings.tasks.socket, service, token=settings.swarm.token)
    await server.start()

    prewake_task = asyncio.create_task(service.prewake_loop(), name="tasks-prewake-loop")
    fire_task = asyncio.create_task(service.fire_loop(), name="tasks-fire-loop")

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info("Служба tasks запущена: сокет %s", settings.tasks.socket)

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы tasks...")
        prewake_task.cancel()
        fire_task.cancel()
        for task in (prewake_task, fire_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await server.stop()
        await node_link.stop()
        await db.close()
        log.info("Служба tasks остановлена чисто")
