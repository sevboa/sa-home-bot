"""Где в рое живёт служба ``vpn`` — динамически, вместо хардкода jeeves.

Этап 39, фаза 0 (39.0.2): у wooster теперь тоже белый IP, серверов
AmneziaWG может быть больше одного. Бот перестаёт слать команды на
``Address(node="jeeves", …)`` и находит ноды с запущенной службой ``vpn``
по их состоянию (``state["services"]``) — тем же приёмом, каким карточка
ноды берёт список служб для ``/nodes`` (bot/node_view.py).

Модуль намеренно без импорта aiogram: его тянет и bot/tools.py, а тот —
служба tasks (см. докстринг wake_core про тот же запрет).

``resolve_vpn_dst`` выбирает ОДНУ ноду-адресата (держателя конкретного
подключения, ``vpn_peers.server``, либо первую живую), а ``fanout`` (39.0.5)
собирает ответы со ВСЕХ живых серверов — карточка ``/vpn`` показывает
подключения и квоту каждой локации отдельно.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sa_home_bot import wake_core
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.vpn.protocol import SERVICE_NAME


def _carries_vpn(state: dict) -> bool:
    return any(
        svc.get("service") == SERVICE_NAME and svc.get("status") == "running"
        for svc in state.get("services", [])
    )


async def live_vpn_nodes(node_link: ServiceLink) -> list[str]:
    """Id нод с запущенной службой ``vpn``, своя — первой. Пустой список —
    ни одна нода роя сейчас VPN не держит либо рой недоступен."""
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return []
    reports = await wake_core.collect_reports(node_link, own_state)
    nodes: list[str] = []
    for report in reports:
        if not report.alive or report.state is None:
            continue
        if _carries_vpn(report.state) and report.node_id not in nodes:
            nodes.append(report.node_id)
    return nodes


async def resolve_vpn_dst(
    node_link: ServiceLink, *, server: str | None = None
) -> Address | None:
    """Адрес службы ``vpn`` для команды. ``server`` — предпочтительная нода
    (``vpn_peers.server`` конкретного подключения); если она сейчас не на
    связи или VPN не держит — берётся первая живая. ``None`` — VPN в рое
    нет ни на одной ноде."""
    nodes = await live_vpn_nodes(node_link)
    if not nodes:
        return None
    chosen = server if server in nodes else nodes[0]
    return Address(node=chosen, service=SERVICE_NAME)


async def live_vpn_servers(node_link: ServiceLink) -> list[dict]:
    """``[{node, label, transports}]`` по всем живым VPN-серверам — дёшево
    (``get_state`` службы), чтобы нарисовать выбор локации перед выдачей."""
    nodes = await live_vpn_nodes(node_link)
    if not nodes:
        return []

    async def _one(node_id: str) -> dict | None:
        try:
            state = await node_link.get_state(dst=Address(node=node_id, service=SERVICE_NAME))
        except (ServiceUnavailableError, ProtoError):
            return None
        return {
            "node": node_id,
            "label": state.get("label") or "",
            "transports": state.get("transports") or [],
            # Индикатор доступности по транспортам (39.0.7(f)) — пикеру
            # локации есть что показать рядом с кнопкой. Старая нода поля не
            # шлёт: пустой список читается как «проверок нет», без индикатора.
            "check": state.get("check") or [],
        }

    states = await asyncio.gather(*(_one(node_id) for node_id in nodes))
    return [state for state in states if state is not None]


@dataclass(frozen=True, order=True)
class ProbeTarget:
    """Одна пара (сервер, транспорт), которую пробнику (39.0.7(d)) стоит
    проверять — из чего именно строится netns/veth/имена, решает
    node/fixups.py, здесь только сам список пар."""

    server: str
    transport: str


async def probe_targets(node_link: ServiceLink, *, exclude: str) -> list[ProbeTarget]:
    """Все живые (сервер, транспорт) кроме ``exclude`` (своя нода —
    self-check исключён по решению владельца 2026-09-18, см.
    vpn_check/service.py). Раскладывает ``live_vpn_servers()`` (по одной
    записи на сервер) в плоский список пар — по одной на каждый транспорт
    сервера. Отсортирован (детерминизм для node/fixups.py: индекс пары в
    этом списке участвует в именовании netns/veth/подсетей)."""
    servers = await live_vpn_servers(node_link)
    targets = [
        ProbeTarget(server=s["node"], transport=t)
        for s in servers
        if s["node"] != exclude
        for t in s["transports"]
    ]
    return sorted(targets)


async def fanout(node_link: ServiceLink, action: str, args: dict) -> list[dict]:
    """Позвать ``action`` на всех живых vpn-нодах, вернуть ответы ответивших.

    Нода, отвалившаяся между опросом живых и самим вызовом, просто выпадает из
    результата: карточка с одним живым сервером полезнее отказа целиком. Пустой
    список — VPN в рое не держит никто (или рой недоступен)."""
    targets = await live_vpn_nodes(node_link)
    if not targets:
        return []

    async def _one(node_id: str) -> dict | None:
        try:
            return await node_link.command(
                action, args, dst=Address(node=node_id, service=SERVICE_NAME)
            )
        except (ServiceUnavailableError, ProtoError):
            return None

    results = await asyncio.gather(*(_one(node_id) for node_id in targets))
    return [result for result in results if result is not None]
