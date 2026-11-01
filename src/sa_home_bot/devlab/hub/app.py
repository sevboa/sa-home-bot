from __future__ import annotations

import asyncio
import contextlib
import logging
import socket

from aiohttp import web

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.devlab.hub import connect_http, executor_api, git_http, mcp_http
from sa_home_bot.devlab.hub.http import build_app
from sa_home_bot.devlab.hub.nodes import NodeDirectory, SwarmNodes
from sa_home_bot.devlab.hub.queue import HubQueue
from sa_home_bot.devlab.hub.service import HubService
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.devlab.hub.waker import WAKE_STEP_INTERVAL_S, ExecutorWaker
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.utils.process import run

log = logging.getLogger(__name__)

TAILSCALE_IP_TIMEOUT_S = 10.0
PURGE_INTERVAL_S = 24 * 3600.0
WAKE_DB_NAME = "wake.sqlite"


async def purge_loop(
    hub_queue: HubQueue, retention_days: int, interval_s: float = PURGE_INTERVAL_S
) -> None:
    while True:
        try:
            removed = await hub_queue.purge(retention_days)
            log.info("Очистка ящика: удалено сообщений %d", removed)
        except Exception:
            log.exception("Очистка ящика не удалась")
        await asyncio.sleep(interval_s)


async def stall_loop(store: HubStore, threshold_s: float, interval_s: float) -> None:
    while True:
        try:
            flagged = await store.queue.flag_stalled(threshold_s, await store.executor_alive())
            for message in flagged:
                log.warning("Задание %s без вестей: %s", message.job_id, message.payload)
        except Exception:
            log.exception("Проверка застоя заданий не удалась")
        await asyncio.sleep(interval_s)


async def wake_loop(waker: ExecutorWaker, interval_s: float = WAKE_STEP_INTERVAL_S) -> None:
    while True:
        try:
            await waker.step()
        except Exception:
            log.exception("Шаг будильника исполнителя не удался")
        await asyncio.sleep(interval_s)


async def tailscale_address() -> str:
    try:
        result = await run(["tailscale", "ip", "-4"], timeout=TAILSCALE_IP_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        raise RuntimeError(f"не удалось спросить адрес tailscale0: {exc}") from exc
    words = result.stdout_text.split()
    if result.returncode != 0 or not words:
        raise RuntimeError(f"tailscale ip -4 не дал адреса: {result.stderr_text}")
    return words[0]


def build_hub_app(store: HubStore, nodes: NodeDirectory, swarm_token: str = "") -> web.Application:
    app = build_app(store, nodes, swarm_token)
    connect_http.add_routes(app)
    git_http.add_routes(app)
    executor_api.add_routes(app)
    mcp_http.add_routes(app)
    return app


async def run_hub(settings: Settings) -> None:
    cfg = settings.devlab_hub
    host = cfg.bind_host or await tailscale_address()
    store = HubStore(cfg.data_dir)
    await store.open()
    wake_db = Database(cfg.data_dir / WAKE_DB_NAME)
    await wake_db.open()
    await apply_migrations(wake_db)
    node_link = ServiceLink(
        settings.node.socket, token=settings.swarm.token, display_name="нода (devlab_hub)"
    )
    await node_link.start()
    waker = ExecutorWaker(store, node_link, Store(wake_db), cfg.executor_node)
    nodes = SwarmNodes(node_link.get_state, settings.node.id or socket.gethostname(), host)
    server = ProtoServer(cfg.socket, HubService(store, nodes), token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    runner = web.AppRunner(build_hub_app(store, nodes, settings.swarm.token))
    await runner.setup()
    await web.TCPSite(runner, host, cfg.port).start()
    log.info("Служба devlab_hub запущена: http://%s:%d, данные %s", host, cfg.port, cfg.data_dir)
    purge_task = asyncio.create_task(purge_loop(store.queue, cfg.mailbox_retention_days))
    wake_task = asyncio.create_task(wake_loop(waker))
    stall_task = asyncio.create_task(
        stall_loop(store, cfg.stalled_after_min * 60.0, cfg.stall_check_s)
    )
    try:
        await lifespan.wait()
    finally:
        for task in (purge_task, wake_task, stall_task):
            task.cancel()
        for task in (purge_task, wake_task, stall_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await node_link.stop()
        await wake_db.close()
        log.info("Останов службы devlab_hub...")
        await runner.cleanup()
        await server.stop()
        await store.close()
        log.info("Служба devlab_hub остановлена чисто")
