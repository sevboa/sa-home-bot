from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable

from aiohttp import web

from sa_home_bot.devlab.connect import local_tailscale_address
from sa_home_bot.metrics import protocol

log = logging.getLogger(__name__)

BIND_RETRY_S = 15.0
METRIC_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

Render = Callable[[], Awaitable[str]]


def _label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_label_value(value)}"' for key, value in labels.items())
    return "{" + body + "}"


def sample(name: str, labels: dict[str, str], value: float) -> str:
    return f"{name}{format_labels(labels)} {value:g}"


def gauge_family(name: str, title: str, samples: Iterable[str]) -> list[str]:
    return [f"# HELP {name} {title}", f"# TYPE {name} gauge", *samples]


def build_metrics_app(render: Render) -> web.Application:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(
            body=(await render()).encode(), headers={"Content-Type": protocol.CONTENT_TYPE}
        )

    app = web.Application()
    app.router.add_get(protocol.METRICS_PATH, handler)
    return app


class MetricsEndpoint:
    def __init__(
        self,
        render: Render,
        port: int,
        address: Callable[[], str | None] = local_tailscale_address,
        retry_s: float = BIND_RETRY_S,
    ) -> None:
        self._render = render
        self._port = port
        self._address = address
        self._retry_s = retry_s
        self._runner: web.AppRunner | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._bind_when_ready(), name="metrics-endpoint")

    async def _bind_when_ready(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            host = await loop.run_in_executor(None, self._address)
            if host:
                try:
                    await self._listen(host)
                except OSError as exc:
                    log.warning("метрики: не удалось слушать %s:%d: %s", host, self._port, exc)
                else:
                    log.info("Метрики: http://%s:%d%s", host, self._port, protocol.METRICS_PATH)
                    return
            else:
                log.warning("метрики: нет tailscale-адреса, повтор через %.0f с", self._retry_s)
            await asyncio.sleep(self._retry_s)

    async def _listen(self, host: str) -> None:
        runner = web.AppRunner(build_metrics_app(self._render), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, host, self._port).start()
        except OSError:
            await runner.cleanup()
            raise
        self._runner = runner

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._runner is not None:
            await self._runner.cleanup()
