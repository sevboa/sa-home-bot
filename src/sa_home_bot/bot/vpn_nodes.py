"""Где в рое живёт служба ``vpn`` — динамически, вместо хардкода jeeves.

Этап 39, фаза 0 (39.0.2): у wooster теперь тоже белый IP, серверов
AmneziaWG может быть больше одного. Бот перестаёт слать команды на
``Address(node="jeeves", …)`` и находит ноды с запущенной службой ``vpn``
по их состоянию (``state["services"]``) — тем же приёмом, каким карточка
ноды берёт список служб для ``/nodes`` (bot/node_view.py).

Модуль намеренно без импорта aiogram: его тянет и bot/tools.py, а тот —
служба tasks (см. докстринг wake_core про тот же запрет).

Фанаут/merge по всем локациям (список подключений и usage с двух серверов)
— это следующий шаг 39.0.5; здесь только выбор ОДНОЙ ноды-адресата:
предпочтительной (держатель конкретного подключения, ``vpn_peers.server``)
либо первой живой.
"""

from __future__ import annotations

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
