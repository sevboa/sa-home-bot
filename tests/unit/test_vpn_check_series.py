from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from sa_home_bot.config import Settings, VpnCheckConfig, VpnSeriesConfig
from sa_home_bot.node.vpn_probe_state import ProbeSlot
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn_check.service import VpnCheckService

PROBE_SERVER = "jeeves"
_IP_ECHO = "https://api.ipify.org"


def _slot(**overrides: Any) -> ProbeSlot:
    base = dict(
        server=PROBE_SERVER,
        transport="awg",
        netns="vpn-probe-jeeves-awg",
        veth_host="vprobe0h0",
        veth_ns="vprobe0n0",
        veth_host_addr="10.200.200.1/30",
        veth_ns_addr="10.200.200.2/30",
        subnet="10.200.200.0/30",
        iface="awg-probe0",
    )
    base.update(overrides)
    return ProbeSlot(**base)


def _reality_slot(**overrides: Any) -> ProbeSlot:
    base = dict(
        server=PROBE_SERVER,
        transport="reality",
        netns="vpn-probe-jeeves-reality",
        veth_host="vprobe1h0",
        veth_ns="vprobe1n0",
        veth_host_addr="10.200.200.5/30",
        veth_ns_addr="10.200.200.6/30",
        subnet="10.200.200.4/30",
        iface=None,
        socks_port=11081,
    )
    base.update(overrides)
    return ProbeSlot(**base)


class _FakeNodeLink:
    state: dict = {
        "node": "wooster",
        "kind": "vps",
        "peers": [],
        "services": [{"name": "vpn", "service": "vpn", "status": "running"}],
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_state(self, dst=None):
        return self.state

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append({"action": action, "args": args, "dst": dst, "timeout": timeout})
        return {"accepted": True}


def _settings(**kwargs) -> Settings:
    from sa_home_bot.config import NodeConfig

    node = kwargs.pop("node", NodeConfig(assignments=[]))
    return Settings(
        node=node,
        vpn_check=VpnCheckConfig(**kwargs),
        vpn_series=VpnSeriesConfig(enabled=False),
    )


def _service(node_link, slots=None, **settings_kwargs) -> VpnCheckService:
    return VpnCheckService(
        _settings(**settings_kwargs), node_link, slots=[_slot()] if slots is None else slots
    )


async def _run_with_timeout(coro, timeout=2.0):
    return await asyncio.wait_for(coro, timeout=timeout)


def test_describe_contains_series_actions():
    desc = _service(_FakeNodeLink()).describe()
    assert desc.info.service == "vpn_check"
    assert desc.find_action("check") is not None
    assert desc.find_action("series") is not None
    assert desc.find_action("series_status") is not None
    caps = set(desc.capabilities)
    assert "series" in caps
    assert "series_status" in caps


async def test_series_unknown_mode_returns_error(monkeypatch):
    service = _service(_FakeNodeLink())
    with pytest.raises(ProtoError) as exc:
        await service.run_command("series", {"server": PROBE_SERVER, "mode": "unknown"})
    assert exc.value.code == ERR_BAD_REQUEST
    assert "неизвестный mode" in str(exc.value)


async def test_series_self_check_skipped(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "gethostname", lambda: "wooster")
    service = _service(_FakeNodeLink())
    result = await service.run_command("series", {"server": "wooster", "mode": "single"})
    assert result == {"accepted": True, "server": "wooster", "skipped": "self-check исключён"}


async def test_series_no_slot_skipped(monkeypatch):
    service = _service(_FakeNodeLink(), slots=[_slot(server="other")])
    result = await service.run_command("series", {"server": PROBE_SERVER, "mode": "single"})
    assert result == {
        "accepted": True,
        "server": PROBE_SERVER,
        "skipped": "нет локально настроенного тоннеля к этому серверу",
    }


async def test_series_single_tunnel_up_down_once(monkeypatch):
    tunnel_calls: list[str] = []
    series_results: list[Any] = []

    async def fake_tunnel_up(self, slot):
        tunnel_calls.append("up")
        return None

    async def fake_tunnel_down(self, slot):
        tunnel_calls.append("down")

    async def fake_egress_gate(self, slot):
        return None

    async def fake_run_single(slot, cfg, *, spawn=None):
        series_results.append("single")
        from sa_home_bot.vpn_series.measure import SeriesResult
        return SeriesResult(
            bytes_total=1000000,
            duration_s=1.0,
            stalled_at_bytes=None,
            max_idle_s=0.1,
            kbps=8000.0,
            verdict="ok",
        )

    monkeypatch.setattr(VpnCheckService, "_tunnel_up", fake_tunnel_up)
    monkeypatch.setattr(VpnCheckService, "_tunnel_down", fake_tunnel_down)
    monkeypatch.setattr(VpnCheckService, "_egress_gate", fake_egress_gate)
    monkeypatch.setattr("sa_home_bot.vpn_check.service.run_single", fake_run_single)

    service = _service(_FakeNodeLink())
    await _run_with_timeout(
        service.run_command("series", {"server": PROBE_SERVER, "mode": "single"})
    )

    await asyncio.sleep(0.1)

    assert tunnel_calls == ["up", "down"]
    assert series_results == ["single"]
    key = f"{PROBE_SERVER}/awg"
    assert key in service._series_last
    assert service._series_last[key]["mode"] == "single"
    assert service._series_last[key]["raw"]["verdict"] == "ok"
    assert len(service._series_last[key]["series"]) == 1


async def test_series_reality_tcp_blocked_no_tunnel(monkeypatch):
    tunnel_calls: list[str] = []

    async def fake_tunnel_up(self, slot):
        tunnel_calls.append("up")
        return None

    async def fake_tunnel_down(self, slot):
        tunnel_calls.append("down")

    async def fake_egress_gate(self, slot):
        return None

    async def fake_tcp_connect(host, port, timeout=5.0):
        return "connection refused"

    def fake_reality_conf_path(slot):
        return Path("/fake/path.json")

    import sa_home_bot.node.vpn_probe_state as vpn_probe_state_module
    monkeypatch.setattr(vpn_probe_state_module, "reality_conf_path", fake_reality_conf_path)

    def fake_read_text(self):
        return '{"outbounds": [{"settings": {"vnext": [{"address": "1.2.3.4", "port": 443}]}}]}'

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    monkeypatch.setattr(VpnCheckService, "_tunnel_up", fake_tunnel_up)
    monkeypatch.setattr(VpnCheckService, "_tunnel_down", fake_tunnel_down)
    monkeypatch.setattr(VpnCheckService, "_egress_gate", fake_egress_gate)
    monkeypatch.setattr("sa_home_bot.vpn_series.raw_probe.tcp_connect", fake_tcp_connect)

    async def fake_run_single(slot, cfg, *, spawn=None):
        from sa_home_bot.vpn_series.measure import SeriesResult
        return SeriesResult(
            bytes_total=1000000,
            duration_s=1.0,
            stalled_at_bytes=None,
            max_idle_s=0.1,
            kbps=8000.0,
            verdict="ok",
        )
    monkeypatch.setattr("sa_home_bot.vpn_check.service.run_single", fake_run_single)

    service = _service(_FakeNodeLink(), slots=[_reality_slot()])
    await _run_with_timeout(
        service.run_command("series", {"server": PROBE_SERVER, "mode": "single"})
    )

    await asyncio.sleep(0.1)

    assert tunnel_calls == []
    key = f"{PROBE_SERVER}/reality"
    assert key in service._series_last
    assert service._series_last[key]["raw"]["verdict"] == "tcp_blocked"
    assert service._series_last[key]["series"] == []


async def test_series_lock_prevents_concurrent_check_and_series(monkeypatch):
    active = 0
    max_active = 0

    async def fake_tunnel_up(self, slot):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        return None

    async def fake_tunnel_down(self, slot):
        nonlocal active
        await asyncio.sleep(0.01)
        active -= 1

    async def fake_egress_gate(self, slot):
        await asyncio.sleep(0.01)
        return None

    async def fake_check_one(self, slot, target):
        await asyncio.sleep(0.01)
        return {"ok": True, "ms": 10, "error": None}

    async def fake_run_single(slot, cfg, *, spawn=None):
        await asyncio.sleep(0.01)
        from sa_home_bot.vpn_series.measure import SeriesResult
        return SeriesResult(
            bytes_total=1000000,
            duration_s=1.0,
            stalled_at_bytes=None,
            max_idle_s=0.1,
            kbps=8000.0,
            verdict="ok",
        )

    monkeypatch.setattr(VpnCheckService, "_tunnel_up", fake_tunnel_up)
    monkeypatch.setattr(VpnCheckService, "_tunnel_down", fake_tunnel_down)
    monkeypatch.setattr(VpnCheckService, "_egress_gate", fake_egress_gate)
    monkeypatch.setattr(VpnCheckService, "_check_one", fake_check_one)
    monkeypatch.setattr("sa_home_bot.vpn_check.service.run_single", fake_run_single)

    service = _service(_FakeNodeLink())

    async def do_check():
        return await service.run_command("check", {"server": PROBE_SERVER, "targets": ["https://1.1.1.1"]})

    async def do_series():
        return await service.run_command("series", {"server": PROBE_SERVER, "mode": "single"})

    await _run_with_timeout(asyncio.gather(do_check(), do_series()))

    await asyncio.sleep(0.1)

    assert max_active == 1


async def test_series_status_returns_last_result(monkeypatch):
    async def fake_tunnel_up(self, slot):
        return None

    async def fake_tunnel_down(self, slot):
        pass

    async def fake_egress_gate(self, slot):
        return None

    async def fake_run_single(slot, cfg, *, spawn=None):
        from sa_home_bot.vpn_series.measure import SeriesResult
        return SeriesResult(
            bytes_total=1000000,
            duration_s=1.0,
            stalled_at_bytes=None,
            max_idle_s=0.1,
            kbps=8000.0,
            verdict="ok",
        )

    monkeypatch.setattr(VpnCheckService, "_tunnel_up", fake_tunnel_up)
    monkeypatch.setattr(VpnCheckService, "_tunnel_down", fake_tunnel_down)
    monkeypatch.setattr(VpnCheckService, "_egress_gate", fake_egress_gate)
    monkeypatch.setattr("sa_home_bot.vpn_check.service.run_single", fake_run_single)

    service = _service(_FakeNodeLink())
    await _run_with_timeout(
        service.run_command("series", {"server": PROBE_SERVER, "mode": "single"})
    )

    await asyncio.sleep(0.1)

    status = await service.run_command("series_status", {})
    assert "results" in status
    key = f"{PROBE_SERVER}/awg"
    assert key in status["results"]
    assert status["results"][key]["mode"] == "single"
