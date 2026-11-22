from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterable

from aiohttp import web

from sa_home_bot.db.store import Store
from sa_home_bot.devlab.connect import local_tailscale_address
from sa_home_bot.domain.host import METRICS, HostMetricState
from sa_home_bot.domain.models import HealthState
from sa_home_bot.metrics import protocol

log = logging.getLogger(__name__)

BIND_RETRY_S = 15.0
_METRIC_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    body = ",".join(f'{key}="{_label_value(value)}"' for key, value in labels.items())
    return "{" + body + "}"


def _sample(name: str, labels: dict[str, str], value: float) -> str:
    return f"{name}{_format_labels(labels)} {value:g}"


def render_metrics(
    health: Iterable[HealthState], host_states: Iterable[HostMetricState]
) -> str:
    lines = [
        f"# HELP {protocol.METRIC_UP} служба monitor жива",
        f"# TYPE {protocol.METRIC_UP} gauge",
        _sample(protocol.METRIC_UP, {}, 1),
    ]
    by_metric: dict[str, list[HostMetricState]] = {}
    for state in host_states:
        if _METRIC_NAME.match(state.metric):
            by_metric.setdefault(state.metric, []).append(state)
    for metric in sorted(by_metric):
        name = protocol.HOST_METRIC_PREFIX + metric
        spec = METRICS.get(metric)
        title = f"{spec.label}, {spec.unit}".rstrip(", ") if spec else metric
        lines.append(f"# HELP {name} {title}")
        lines.append(f"# TYPE {name} gauge")
        lines.extend(_sample(name, {}, state.value) for state in by_metric[metric])
    temperatures = sorted(health, key=lambda s: (s.kind, s.component_id))
    if temperatures:
        lines.append(f"# HELP {protocol.METRIC_TEMPERATURE} температура компонента, °C")
        lines.append(f"# TYPE {protocol.METRIC_TEMPERATURE} gauge")
        for state in temperatures:
            labels = {protocol.LABEL_KIND: state.kind, protocol.LABEL_COMPONENT: state.component_id}
            lines.append(_sample(protocol.METRIC_TEMPERATURE, labels, state.temperature_c))
    return "\n".join(lines) + "\n"


def build_metrics_app(store: Store) -> web.Application:
    async def handler(_request: web.Request) -> web.Response:
        body = render_metrics(await store.get_all_states(), await store.get_all_host_states())
        return web.Response(body=body.encode(), headers={"Content-Type": protocol.CONTENT_TYPE})

    app = web.Application()
    app.router.add_get(protocol.METRICS_PATH, handler)
    return app


class MetricsEndpoint:
    def __init__(
        self,
        store: Store,
        port: int,
        address: Callable[[], str | None] = local_tailscale_address,
        retry_s: float = BIND_RETRY_S,
    ) -> None:
        self._store = store
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
        runner = web.AppRunner(build_metrics_app(self._store))
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
