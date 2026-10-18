from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.backup.snapshot import build_backup
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import Address
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.reality.xray import RealXrayBackend
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.vpn.awg import RealAwgBackend
from sa_home_bot.vpn.protocol import SERVICE_NAME, TRANSPORT_AWG, TRANSPORT_REALITY
from sa_home_bot.vpn.service import VpnService
from sa_home_bot.vpn.subweb import SubscriptionWeb

log = logging.getLogger(__name__)


async def run_vpn(settings: Settings) -> None:
    db = Database(settings.vpn.db_path)
    await db.open()
    await apply_migrations(db)

    transports = settings.vpn.transports or [TRANSPORT_AWG]
    awg_on = TRANSPORT_AWG in transports

    backend = RealAwgBackend(settings.vpn.interface)

    reality_backend = None
    if TRANSPORT_REALITY in transports:
        if settings.vpn.reality is None:
            log.warning(
                "vpn: транспорт 'reality' в [vpn].transports, но нет секции "
                "[vpn.reality] — транспорт отключён"
            )
        else:
            reality_backend = RealXrayBackend(
                settings.vpn.reality.api_addr,
                settings.vpn.reality.inbound_tag,
                settings.vpn.reality.port,
            )

    node_link = ServiceLink(
        settings.node.socket, token=settings.swarm.token, display_name="нода (vpn)"
    )
    await node_link.start()

    server: ProtoServer | None = None

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    service = VpnService(
        settings, db, backend, emit, node_link=node_link, reality_backend=reality_backend
    )
    partner = settings.backup.partner.strip()

    async def ask_partner(action: str, args: dict) -> dict:
        return await node_link.command(
            action, args, dst=Address(node=partner, service=SERVICE_NAME), timeout=20.0
        )

    service.backup = build_backup(settings, lambda: db.conn, settings.node.id, ask_partner)
    if service.backup is not None:
        await service.backup.start()
    await service.backfill_server()
    await service.backfill_access()
    server = ProtoServer(settings.vpn.socket, service, token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()

    with contextlib.suppress(Exception):
        await service.reconcile()
    with contextlib.suppress(Exception):
        await service.check_restore()

    sub_web: SubscriptionWeb | None = None
    if reality_backend is not None and settings.vpn.sub_port > 0:
        sub_web = SubscriptionWeb(settings.vpn, service.resolve_subscription, service.web_issue_awg)
        service.sub_web = sub_web
        try:
            await sub_web.start()
        except Exception:
            log.exception("vpn: страница подписки не запустилась")

    usage_task = asyncio.create_task(service.usage_loop(), name="vpn-usage-loop")
    tasks = [usage_task]
    if awg_on:
        tasks.append(asyncio.create_task(service.check_loop(), name="vpn-check-loop"))
    log.info(
        "Служба vpn запущена: транспорты %s, сокет %s",
        ",".join(transports),
        settings.vpn.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы vpn...")
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if sub_web is not None:
            await sub_web.stop()
        if service.backup is not None:
            await service.backup.stop()
        await server.stop()
        await node_link.stop()
        await db.close()
        log.info("Служба vpn остановлена чисто")
