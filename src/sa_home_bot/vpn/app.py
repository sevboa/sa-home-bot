"""Сборка и жизненный цикл службы vpn (отдельный процесс, обычно на jeeves).

По образцу tasks/app.py: proto-сервер поверх VpnService плюс своя БД (общая
схема проекта). Фоновый цикл — сэмплер трафика (usage_loop). При старте
пробуем один раз свести интерфейс с БД (reconcile) — после рестарта jeeves
awg0 пуст, а БД помнит всех активных пиров; неудача (sudoers ещё не
поставлен через ``nodectl fix``) не должна мешать службе подняться —
`get_state`/`describe` и хотя бы `issue` дальше сработают попыткой, а
диагноз ``ERR_NEEDS_PRIVILEGE`` объяснит, что делать.
"""

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

    # AwgBackend конструируется без побочных эффектов (хранит имя интерфейса);
    # реально к `awg` служба ходит только когда awg среди транспортов ноды.
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

    # Клиент к своей же локальной ноде — для рассылки проверок доступности
    # (vpn/service.py::_dispatch_checks → node/service.py::ACTION_TRIGGER_PEERS
    # → vpn_check на нодах из [vpn].check_nodes).
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
    # Бэкап снапшота БД у напарника (39.0.8(c)): выключен без [backup].partner.
    partner = settings.backup.partner.strip()

    async def ask_partner(action: str, args: dict) -> dict:
        return await node_link.command(
            action, args, dst=Address(node=partner, service=SERVICE_NAME), timeout=20.0
        )

    service.backup = build_backup(settings, lambda: db.conn, settings.node.id, ask_partner)
    if service.backup is not None:
        await service.backup.start()
    await service.backfill_server()
    # Строго до reconcile ниже: иначе первый же реконсайл снял бы с интерфейса
    # пиры всех, кому допуск ещё не проставлен.
    await service.backfill_access()
    server = ProtoServer(settings.vpn.socket, service, token=settings.swarm.token)
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()

    with contextlib.suppress(Exception):
        await service.reconcile()
    with contextlib.suppress(Exception):
        await service.check_restore()

    # Подписка Hiddify и https-страница (57.10). Не вышло поднять — служба
    # живёт без неё (get_subscription вернёт понятную ошибку).
    sub_web: SubscriptionWeb | None = None
    if reality_backend is not None and settings.vpn.sub_port > 0:
        sub_web = SubscriptionWeb(settings.vpn, service.resolve_subscription)
        service.sub_web = sub_web
        try:
            await sub_web.start()
        except Exception:
            log.exception("vpn: страница подписки не запустилась")

    usage_task = asyncio.create_task(service.usage_loop(), name="vpn-usage-loop")
    # Проверки доступности (vpn_check) — только для awg-нод: у reality-only
    # ноды нет netns-пробника AmneziaWG.
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
