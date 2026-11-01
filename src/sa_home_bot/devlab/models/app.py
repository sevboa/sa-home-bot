from __future__ import annotations

import asyncio
import contextlib
import logging

from aiohttp import web

from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.app import tailscale_address
from sa_home_bot.devlab.models.backends import build_backend
from sa_home_bot.devlab.models.gateway import Gateway
from sa_home_bot.devlab.models.hubclient import HEARTBEAT_INTERVAL_S, HubClient
from sa_home_bot.devlab.models.ledger import Ledger
from sa_home_bot.devlab.models.limits import RateWindow
from sa_home_bot.devlab.models.slice import SliceControl
from sa_home_bot.devlab.neighbor import NeighborGate, start_following, stop_following
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def heartbeat_loop(
    gateway: Gateway, hub: HubClient, interval_s: float = HEARTBEAT_INTERVAL_S
) -> None:
    while True:
        try:
            await hub.heartbeat(await gateway.heartbeat_body())
        except Exception:
            log.exception("Heartbeat шлюза не удался")
        await asyncio.sleep(interval_s)


async def run_models(settings: Settings) -> None:
    cfg = settings.devlab_models
    if not cfg.hub_url or not cfg.hub_token or not cfg.entries:
        raise RuntimeError(
            "devlab_models: нужны hub_url, hub_token и хотя бы одна [devlab_models.<имя>]"
        )
    host = cfg.bind_host or await tailscale_address()
    control = SliceControl()
    backends = {
        name: build_backend(name, spec, cfg, control) for name, spec in cfg.entries.items()
    }
    local = any(spec.kind == protocol.MODEL_KIND_LLAMACPP for spec in cfg.entries.values())
    gate = NeighborGate(
        freeze=control.freeze,
        thaw=control.thaw,
        freeze_max_s=cfg.freeze_max_s,
        on_night=control.set_night,
    )
    neighbor = (
        start_following(gate, cfg.neighbor_socket or settings.llm.socket) if local else None
    )
    windows = {name: RateWindow(spec.rpm) for name, spec in cfg.entries.items()}
    ledger = Ledger(cfg.data_dir)
    await ledger.open()
    hub = HubClient(cfg.hub_url, cfg.hub_token)
    gateway = Gateway(backends, ledger, hub, windows, url=f"http://{host}:{cfg.port}")
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    runner = web.AppRunner(gateway.build_app())
    await runner.setup()
    await web.TCPSite(runner, host, cfg.port).start()
    log.info(
        "Служба devlab_models запущена: http://%s:%d, моделей %d", host, cfg.port, len(backends)
    )
    beat = asyncio.create_task(heartbeat_loop(gateway, hub))
    try:
        await lifespan.wait()
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat
        if neighbor is not None:
            await stop_following(neighbor, gate)
        log.info("Останов службы devlab_models...")
        await runner.cleanup()
        for backend in backends.values():
            await backend.close()
        await hub.close()
        await ledger.close()
        log.info("Служба devlab_models остановлена чисто")
