from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.vpn import protocol as vpn_protocol

TCP_TIMEOUT_S = 5.0

TRANSPORT_AWG = "awg"
TRANSPORT_REALITY = "reality"

HandshakeFn = Callable[[], Awaitable[str | None]]


class RawVerdict(StrEnum):
    OK = "ok"
    TCP_BLOCKED = "tcp_blocked"
    PROTO_BLOCKED = "proto_blocked"


class RawTarget(BaseModel):
    host: str
    port: int


class RawProbeResult(BaseModel):
    server: str
    transport: str
    verdict: RawVerdict
    tcp_ok: bool | None
    tcp_error: str | None = None
    handshake_error: str | None = None


def reality_target(conf_text: str) -> RawTarget | None:
    try:
        doc: Any = json.loads(conf_text)
        vnext = doc["outbounds"][0]["settings"]["vnext"][0]
        return RawTarget(host=str(vnext["address"]), port=int(vnext["port"]))
    except (ValueError, KeyError, IndexError, TypeError):
        return None


async def tcp_connect(host: str, port: int, timeout: float = TCP_TIMEOUT_S) -> str | None:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except TimeoutError:
        return f"таймаут TCP {host}:{port}"
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return None


async def raw_probe(
    slot: ProbeSlot,
    target: RawTarget | None,
    do_handshake: HandshakeFn,
    *,
    tcp_timeout: float = TCP_TIMEOUT_S,
) -> RawProbeResult:
    tcp_ok: bool | None = None
    tcp_error: str | None = None
    if slot.transport in vpn_protocol.REALITY_PROBE_TRANSPORTS and target is not None:
        tcp_error = await tcp_connect(target.host, target.port, tcp_timeout)
        tcp_ok = tcp_error is None
        if not tcp_ok:
            return RawProbeResult(
                server=slot.server,
                transport=slot.transport,
                verdict=RawVerdict.TCP_BLOCKED,
                tcp_ok=False,
                tcp_error=tcp_error,
            )
    handshake_error = await do_handshake()
    return RawProbeResult(
        server=slot.server,
        transport=slot.transport,
        verdict=RawVerdict.OK if handshake_error is None else RawVerdict.PROTO_BLOCKED,
        tcp_ok=tcp_ok,
        handshake_error=handshake_error,
    )
