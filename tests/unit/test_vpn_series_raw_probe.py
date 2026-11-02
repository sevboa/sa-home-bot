from __future__ import annotations

import asyncio
import json

import pytest

from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.vpn_series import raw_probe
from sa_home_bot.vpn_series.raw_probe import RawTarget, RawVerdict


def _slot(transport: str) -> ProbeSlot:
    return ProbeSlot(
        server="jeeves",
        transport=transport,
        netns=f"vpn-probe-jeeves-{transport}",
        veth_host="h",
        veth_ns="n",
        veth_host_addr="10.200.200.1/30",
        veth_ns_addr="10.200.200.2/30",
        subnet="10.200.200.0/30",
    )


def _handshake(result: str | None):
    calls: list[int] = []

    async def fn() -> str | None:
        calls.append(1)
        return result

    return fn, calls


def _tcp(monkeypatch: pytest.MonkeyPatch, error: str | None) -> None:
    async def fake(host: str, port: int, timeout: float = 0) -> str | None:
        return error

    monkeypatch.setattr(raw_probe, "tcp_connect", fake)


TARGET = RawTarget(host="203.0.113.7", port=443)


@pytest.mark.parametrize(
    ("tcp_error", "hs_error", "verdict", "hs_called"),
    [
        ("refused", None, RawVerdict.TCP_BLOCKED, False),
        (None, "туннель не поднялся", RawVerdict.PROTO_BLOCKED, True),
        (None, None, RawVerdict.OK, True),
    ],
)
def test_reality_verdicts(monkeypatch, tcp_error, hs_error, verdict, hs_called) -> None:
    _tcp(monkeypatch, tcp_error)
    fn, calls = _handshake(hs_error)
    result = asyncio.run(raw_probe.raw_probe(_slot("reality"), TARGET, fn))
    assert result.verdict is verdict
    assert bool(calls) is hs_called
    assert result.tcp_ok is (tcp_error is None)
    assert result.handshake_error == hs_error


@pytest.mark.parametrize(
    ("hs_error", "verdict"),
    [(None, RawVerdict.OK), ("нет handshake", RawVerdict.PROTO_BLOCKED)],
)
def test_awg_skips_tcp(monkeypatch, hs_error, verdict) -> None:
    _tcp(monkeypatch, "не должно вызываться")
    fn, _ = _handshake(hs_error)
    result = asyncio.run(raw_probe.raw_probe(_slot("awg"), None, fn))
    assert result.verdict is verdict
    assert result.tcp_ok is None


def test_tcp_connect_real_sockets() -> None:
    async def scenario() -> tuple[str | None, str | None]:
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        opened = await raw_probe.tcp_connect("127.0.0.1", port, 2.0)
        server.close()
        await server.wait_closed()
        closed = await raw_probe.tcp_connect("127.0.0.1", port, 2.0)
        return opened, closed

    opened, closed = asyncio.run(scenario())
    assert opened is None
    assert closed is not None


def test_reality_target_reads_only_address_and_port() -> None:
    conf = json.dumps(
        {
            "outbounds": [
                {"settings": {"vnext": [{"address": "h.example", "port": 8443, "users": []}]}}
            ]
        }
    )
    assert raw_probe.reality_target(conf) == RawTarget(host="h.example", port=8443)
    assert raw_probe.reality_target("{}") is None
    assert raw_probe.reality_target("не json") is None
