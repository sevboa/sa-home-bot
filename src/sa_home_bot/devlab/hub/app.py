from __future__ import annotations

import asyncio
import contextlib
import logging

from aiohttp import web

from sa_home_bot.config import Settings
from sa_home_bot.devlab.hub import executor_api, git_http
from sa_home_bot.devlab.hub.http import build_app
from sa_home_bot.devlab.hub.queue import HubQueue
from sa_home_bot.devlab.hub.service import HubService
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.utils.process import run

log = logging.getLogger(__name__)

TAILSCALE_IP_TIMEOUT_S = 10.0
PURGE_INTERVAL_S = 24 * 3600.0


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


async def tailscale_address() -> str:
    try:
        result = await run(["tailscale", "ip", "-4"], timeout=TAILSCALE_IP_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        raise RuntimeError(f"не удалось спросить адрес tailscale0: {exc}") from exc
    words = result.stdout_text.split()
    if result.returncode != 0 or not words:
        raise RuntimeError(f"tailscale ip -4 не дал адреса: {result.stderr_text}")
    return words[0]


async def run_hub(settings: Settings) -> None:
    cfg = settings.devlab_hub
    host = cfg.bind_host or await tailscale_address()
    store = HubStore(cfg.data_dir)
    await store.open()
    server = ProtoServer(cfg.socket, HubService(store), token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    app = build_app(store)
    git_http.add_routes(app)
    executor_api.add_routes(app)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, cfg.port).start()
    log.info("Служба devlab_hub запущена: http://%s:%d, данные %s", host, cfg.port, cfg.data_dir)
    purge_task = asyncio.create_task(purge_loop(store.queue, cfg.mailbox_retention_days))
    try:
        await lifespan.wait()
    finally:
        purge_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purge_task
        log.info("Останов службы devlab_hub...")
        await runner.cleanup()
        await server.stop()
        await store.close()
        log.info("Служба devlab_hub остановлена чисто")
