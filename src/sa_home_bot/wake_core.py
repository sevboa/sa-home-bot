"""Общее ядро Wake-on-LAN через рой: раньше жило только в bot/swarm_view.py
и bot/handlers/wake.py (используется /wake и молчаливым прогревом перед
/ai), но оба этих модуля тянут aiogram на весь файл. Служба tasks (без
Telegram, живая находка 2026-07-24 при генерализации напоминаний в
отдельный сервис) тоже должна уметь будить спящую цель перед сроком
задачи — этот модуль ей это даёт без лишних зависимостей.

bot/swarm_view.py и bot/handlers/wake.py делегируют сюда (тонкие
реэкспорты), не дублируют логику — поведение /wake и /swarm не менялось.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.wake_state import cached as cached_wake_info
from sa_home_bot.bot.wake_state import remember as remember_wake_info
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError

PEER_TIMEOUT_S = 3.0


@dataclass
class NodeReport:
    node_id: str
    alive: bool
    state: dict | None = None  # get_state сервиса node (None — не ответил)


async def fetch_state(node_link: ServiceLink, dst: Address | None) -> dict | None:
    try:
        return await asyncio.wait_for(node_link.get_state(dst=dst), PEER_TIMEOUT_S)
    except (ServiceUnavailableError, ProtoError, TimeoutError):
        return None


async def collect_reports(node_link: ServiceLink, own_state: dict) -> list[NodeReport]:
    """Параллельный сбор состояний всех нод роя (своя — первой). Минимальная
    версия для нужд wake — без монитора (см. bot/swarm_view.py::_collect
    для полной сводки /swarm, которая дополнительно тянет monitor.get_state
    на каждую ноду)."""
    own = NodeReport(node_id=own_state.get("node", "?"), alive=True, state=own_state)
    reports = [own]
    for peer in own_state.get("peers", []):
        pid = peer.get("id", "?")
        reports.append(NodeReport(node_id=pid, alive=bool(peer.get("alive"))))

    async def fill(report: NodeReport) -> None:
        if report.state is None:
            dst = Address(node=report.node_id, service="node")
            report.state = await fetch_state(node_link, dst)

    await asyncio.gather(*(fill(r) for r in reports if r.alive))
    return reports


async def _remember_all(store: Store, reports: list[NodeReport]) -> None:
    for r in reports:
        if r.state is not None:
            await remember_wake_info(store, r.node_id, r.state.get("wake"))


async def find_lan_waker(
    node_link: ServiceLink, store: Store, target_node_id: str, target_broadcast: str
) -> str | None:
    """Живая нода в том же сегменте LAN, что и уснувшая ``target_node_id``
    (совпадает объявленный ею broadcast) — она отправит magic packet вместо
    того, кто зовёт (бот или служба tasks вполне может крутиться вне этой
    локалки). Заодно свежит кэш wake-реквизитов всех увиденных сейчас нод
    (в СВОЁМ store вызывающего — у бота и у tasks кэши независимые, см.
    докстринг модуля)."""
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None
    reports = await collect_reports(node_link, own_state)
    await _remember_all(store, reports)
    for r in reports:
        if not r.alive or r.node_id == target_node_id or r.state is None:
            continue
        candidate = r.state.get("wake")
        if candidate and candidate.get("broadcast") == target_broadcast:
            return r.node_id
    return None


async def wait_for_service(
    node_link: ServiceLink,
    node_id: str,
    service: str,
    timeout_s: float,
    interval_s: float = 3.0,
) -> bool:
    """Опрашивать get_state удалённой службы, пока не ответит или не истечёт
    ``timeout_s`` — дождаться, пока цель снова окажется на связи после WoL."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    dst = Address(node=node_id, service=service)
    while True:
        if await fetch_state(node_link, dst) is not None:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval_s)


@dataclass(frozen=True)
class WakeOutcome:
    """``detail`` — уже готовый HTML-текст для чата (эмодзи, как в /wake),
    чтобы бот мог переслать его без изменения поведения; служба tasks его
    просто не показывает никому (использует только ``ok``)."""

    ok: bool
    detail: str


async def wake_swarm_node_core(node_link: ServiceLink, store: Store, node_id: str) -> WakeOutcome:
    """Будим известную ноду по её кэшированным реквизитам — отправляет не
    сам вызывающий, а живая нода из той же LAN (см. докстринг модуля).
    Переиспользуется /wake, молчаливым прогревом перед /ai (bot/ai_flow.py)
    и прогревом задач (tasks/service.py)."""
    info = await cached_wake_info(store, node_id)
    if info is None:
        return WakeOutcome(
            False, f"⚙️ Нет данных о MAC «{node_id}» — нода ещё ни разу не была видна в рое."
        )

    waker = await find_lan_waker(node_link, store, node_id, info["broadcast"])
    if waker is None:
        return WakeOutcome(
            False, f"⚠️ Некому отправить сигнал: нет живой ноды в той же сети, что «{node_id}»."
        )

    dst = Address(node=waker, service="node")
    try:
        await node_link.command("send_wol", {"mac": info["mac"]}, dst=dst)
    except ServiceUnavailableError:
        return WakeOutcome(False, f"⚠️ Нода «{waker}» перестала отвечать во время отправки.")
    except ProtoError as exc:
        return WakeOutcome(False, f"❌ {waker}: {exc.message}")

    return WakeOutcome(
        True,
        f"🔌 Magic packet для «{node_id}» (<code>{info['mac']}</code>) отправлен через "
        f"ноду «{waker}». Появится в /nodes, как поднимется.",
    )
