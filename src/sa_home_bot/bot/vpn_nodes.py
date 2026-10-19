from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sa_home_bot import wake_core
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.vpn.protocol import (
    ACTION_DEVICE_ONLINE,
    ACTION_USAGE,
    SERVICE_NAME,
    TRANSPORT_AWG,
    TRANSPORT_REALITY,
)

log = logging.getLogger(__name__)

KNOWN_SERVERS_PATH = Path("./data/vpn_known_servers.json")


def _carries_vpn(state: dict) -> bool:
    return any(
        svc.get("service") == SERVICE_NAME and svc.get("status") == "running"
        for svc in state.get("services", [])
    )


async def live_vpn_nodes(node_link: ServiceLink) -> list[str]:
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


async def resolve_vpn_dst(node_link: ServiceLink, *, server: str | None = None) -> Address | None:
    nodes = await live_vpn_nodes(node_link)
    if not nodes:
        return None
    chosen = server if server in nodes else nodes[0]
    return Address(node=chosen, service=SERVICE_NAME)


async def live_vpn_servers(node_link: ServiceLink) -> list[dict]:
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
            "check": state.get("check") or [],
        }

    states = await asyncio.gather(*(_one(node_id) for node_id in nodes))
    return [state for state in states if state is not None]


@dataclass(frozen=True, order=True)
class ProbeTarget:
    server: str
    transport: str


async def probe_targets(node_link: ServiceLink, *, exclude: str) -> list[ProbeTarget]:
    servers = await live_vpn_servers(node_link)
    targets = [
        ProbeTarget(server=s["node"], transport=t)
        for s in servers
        if s["node"] != exclude
        for t in s["transports"]
    ]
    return sorted(targets)


async def fanout(node_link: ServiceLink, action: str, args: dict) -> list[dict]:
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


def _load_known() -> dict[str, str]:
    try:
        raw = json.loads(KNOWN_SERVERS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def remember_servers(answered: list[dict]) -> None:
    known = _load_known()
    updated = dict(known)
    for server in answered:
        node = server.get("node")
        if node:
            updated[node] = server.get("label") or updated.get(node, "")
    if updated == known:
        return
    try:
        KNOWN_SERVERS_PATH.parent.mkdir(parents=True, exist_ok=True)
        KNOWN_SERVERS_PATH.write_text(json.dumps(updated, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("vpn: не удалось сохранить список известных серверов", exc_info=True)


def known_server_label(node: str) -> str:
    return _load_known().get(node, "")


def unavailable_servers(answered: list[dict]) -> list[dict]:
    seen = {server.get("node") for server in answered}
    return [
        {"node": node, "label": label, "unavailable": True}
        for node, label in sorted(_load_known().items())
        if node not in seen
    ]


async def fanout_usage(node_link: ServiceLink, chat_id: int) -> list[dict]:
    return await fanout(node_link, ACTION_USAGE, {"chat_id": chat_id})


async def device_online(node_link: ServiceLink, chat_id: int, label: str) -> list[dict]:
    return await fanout(
        node_link, ACTION_DEVICE_ONLINE, {"chat_id": chat_id, "device_label": label}
    )


async def usage_for_chat(node_link: ServiceLink, chat_id: int) -> list[dict]:
    answered = await fanout_usage(node_link, chat_id)
    remember_servers(answered)
    return answered


def is_allowed(server: dict) -> bool:
    return bool(server.get("allowed", True))


def allowed_servers(servers: list[dict]) -> list[dict]:
    return [server for server in servers if is_allowed(server)]


def server_label(server: dict) -> str:
    return server.get("label") or server.get("node") or "сервер"


def _open_with_transport(servers: list[dict], transport: str) -> list[dict]:
    return [s for s in servers if is_allowed(s) and transport in (s.get("transports") or [])]


def has_reality_server(servers: list[dict]) -> bool:
    return bool(_open_with_transport(servers, TRANSPORT_REALITY))


def awg_servers(servers: list[dict]) -> list[dict]:
    return [s for s in _open_with_transport(servers, TRANSPORT_AWG) if s.get("node")]


def reality_servers(servers: list[dict]) -> list[dict]:
    return [s for s in _open_with_transport(servers, TRANSPORT_REALITY) if s.get("node")]
