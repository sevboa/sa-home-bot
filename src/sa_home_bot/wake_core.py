from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.wake_state import cached as cached_wake_info
from sa_home_bot.bot.wake_state import remember as remember_wake_info
from sa_home_bot.db.store import Store
from sa_home_bot.proto.addresses import MONITOR_SERVICE, NODE_SERVICE
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, Address, ProtoError

log = logging.getLogger(__name__)

PEER_TIMEOUT_S = 3.0


PRESENCE_TIMEOUT_S = 6.0

WAKE_POLL_TIMEOUT_S = 180.0
WAKE_POLL_INTERVAL_S = 3.0

WARMUP_TIMEOUT_S = 360.0

READY = "ready"
UNREACHABLE = "unreachable"
WARMUP_FAILED = "warmup_failed"
CRASHED = "crashed"

PHASE_WAKING = "waking"
PHASE_WOKEN = "woken"
PHASE_WARMING = "warming"


@dataclass
class NodeReport:
    node_id: str
    alive: bool
    state: dict | None = None
    monitor: dict | None = None
    kind: str = ""
    left: bool = False


async def fetch_state(
    node_link: ServiceLink, dst: Address | None, *, timeout_s: float = PEER_TIMEOUT_S
) -> dict | None:
    try:
        return await asyncio.wait_for(node_link.get_state(dst=dst), timeout_s)
    except (ServiceUnavailableError, ProtoError, TimeoutError):
        return None


async def collect_reports(
    node_link: ServiceLink, own_state: dict, *, with_monitor: bool = False
) -> list[NodeReport]:
    own = NodeReport(
        node_id=own_state.get("node", "?"),
        alive=True,
        state=own_state,
        kind=own_state.get("kind", ""),
    )
    reports = [own]
    for peer in own_state.get("peers", []):
        pid = peer.get("id", "?")
        reports.append(
            NodeReport(
                node_id=pid,
                alive=bool(peer.get("alive")),
                kind=str(peer.get("kind") or ""),
                left=bool(peer.get("left")),
            )
        )

    async def fill(report: NodeReport) -> None:
        if report.state is None:
            dst = Address(node=report.node_id, service=NODE_SERVICE)
            report.state = await fetch_state(node_link, dst, timeout_s=PEER_TIMEOUT_S)
        if with_monitor:
            report.monitor = await fetch_state(
                node_link,
                Address(node=report.node_id, service=MONITOR_SERVICE),
                timeout_s=PEER_TIMEOUT_S,
            )

    await asyncio.gather(*(fill(r) for r in reports if r.alive))
    return reports


async def _remember_all(store: Store, reports: list[NodeReport]) -> None:
    for r in reports:
        if r.state is not None:
            await remember_wake_info(store, r.node_id, r.state.get("wake"))


async def find_lan_waker(
    node_link: ServiceLink, store: Store, target_node_id: str, target_broadcast: str
) -> str | None:
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    dst = Address(node=node_id, service=service)
    while True:
        if await fetch_state(node_link, dst) is not None:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval_s)


async def resolve_wake_info(
    node_link: ServiceLink, store: Store, node_id: str
) -> dict[str, str] | None:
    info = await cached_wake_info(store, node_id)
    if info is not None:
        return info
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None
    for peer in own_state.get("peers", []):
        if peer.get("id") != node_id:
            continue
        await remember_wake_info(store, node_id, peer.get("wake"))
        return await cached_wake_info(store, node_id)
    return None


async def try_warmup(
    node_link: ServiceLink, dst: Address, timeout_s: float = WARMUP_TIMEOUT_S
) -> bool:
    try:
        await node_link.command("warmup", {}, dst=dst, timeout=timeout_s)
        return True
    except ProtoError as exc:
        if exc.code == ERR_UNKNOWN_ACTION:
            return True
        log.warning(
            "wake: %s/%s отказал в прогреве: %s — %s", dst.node, dst.service, exc.code, exc.message
        )
        return False
    except (ServiceUnavailableError, TimeoutError) as exc:
        log.warning(
            "wake: %s/%s не ответил на прогрев за %.0f с (%s)",
            dst.node, dst.service, timeout_s, type(exc).__name__,
        )
        return False


async def ensure_service_ready(
    node_link: ServiceLink,
    store: Store,
    node_id: str,
    service: str,
    *,
    wake_timeout_s: float | None = None,
    wake_poll_interval_s: float = WAKE_POLL_INTERVAL_S,
    warmup_timeout_s: float = WARMUP_TIMEOUT_S,
    on_phase_change: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    if wake_timeout_s is None:
        wake_timeout_s = WAKE_POLL_TIMEOUT_S
    dst = Address(node=node_id, service=service)

    async def _finish_warmup() -> str:
        if await try_warmup(node_link, dst, warmup_timeout_s):
            return READY
        log.warning("wake: прогрев %s/%s не удался", node_id, service)
        return WARMUP_FAILED

    state = await fetch_state(node_link, dst, timeout_s=PRESENCE_TIMEOUT_S)

    if state is None:
        node_alive = service != NODE_SERVICE and (
            await fetch_state(
                node_link, Address(node=node_id, service=NODE_SERVICE), timeout_s=PRESENCE_TIMEOUT_S
            )
            is not None
        )
        if node_alive:
            log.warning(
                "wake: нода «%s» жива, но служба %s не отвечает — WoL тут бесполезен",
                node_id, service,
            )
            return CRASHED
        if on_phase_change is not None:
            await on_phase_change(PHASE_WAKING)
        outcome = await wake_swarm_node_core(node_link, store, node_id)
        if not outcome.ok:
            log.warning("wake: не удалось разбудить «%s»: %s", node_id, outcome.detail)
            return UNREACHABLE
        log.info("wake: «%s» разбужена, ждём %s до %.0f с", node_id, service, wake_timeout_s)
        if not await wait_for_service(
            node_link, node_id, service, wake_timeout_s, wake_poll_interval_s
        ):
            log.warning(
                "wake: «%s» не подняла службу %s за %.0f с", node_id, service, wake_timeout_s
            )
            return UNREACHABLE
        if on_phase_change is not None:
            await on_phase_change(PHASE_WOKEN)
            await on_phase_change(PHASE_WARMING)
        return await _finish_warmup()

    if state.get("asleep"):
        if on_phase_change is not None:
            await on_phase_change(PHASE_WARMING)
        return await _finish_warmup()

    return READY


@dataclass(frozen=True)
class WakeOutcome:
    ok: bool
    detail: str


async def wake_swarm_node_core(node_link: ServiceLink, store: Store, node_id: str) -> WakeOutcome:
    info = await resolve_wake_info(node_link, store, node_id)
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


_CHECK_UPDATE_ACTION = "check_update"


async def check_updates_summary(node_link: ServiceLink) -> str:
    own = await fetch_state(node_link, None)
    if own is None:
        return "недоступно: своя нода не отвечает"
    try:
        own_check = await node_link.command(_CHECK_UPDATE_ACTION, {}, dst=None)
    except ProtoError as exc:
        return f"не вышло проверить обновления: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: своя нода не ответила ({exc})"
    latest = own_check.get("latest")
    if not latest:
        return "не удалось узнать последнюю версию (сеть?)"

    reports = await collect_reports(node_link, own, with_monitor=False)
    behind: list[str] = []
    restart_only: list[str] = []
    unreachable: list[str] = []
    for r in reports:
        if not r.alive or r.state is None:
            unreachable.append(r.node_id)
            continue
        running = r.state.get("version")
        if running and running != latest:
            behind.append(r.node_id)
            continue
        if (r.state.get("update") or {}).get("restart_required"):
            restart_only.append(r.node_id)

    parts = [f"Последняя версия: v{latest}."]
    if behind:
        parts.append("Нужен update: " + ", ".join(sorted(behind)) + ".")
    if restart_only:
        parts.append(
            "Update уже на диске, нужен только restart_node: "
            + ", ".join(sorted(restart_only))
            + "."
        )
    if unreachable:
        parts.append("Не отвечают, не проверить: " + ", ".join(sorted(unreachable)) + ".")
    if not behind and not restart_only:
        parts.append("Все доступные ноды на последней версии.")
    return " ".join(parts)
