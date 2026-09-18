"""Служба vpn_check: describe, запуск проверки, пуш результата в vpn,
обработка HTTP-ошибок и сбоев самого curl."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sa_home_bot.config import Settings, VpnCheckConfig
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn_check.service import VpnCheckService

PROBE_SERVER = "jeeves"


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _FakeNodeLink:
    # Своя нода не держит vpn (пробник живёт на alfred) — отчёт фанаутится
    # на живые vpn-инстансы роя (vpn_nodes.fanout), не в одну через
    # resolve_vpn_dst.
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
    kwargs.setdefault("probe_server", PROBE_SERVER)
    return Settings(node=node, vpn_check=VpnCheckConfig(netns="vpn-probe", **kwargs))


_IP_ECHO = "https://api.ipify.org"


def _patch_curl(
    monkeypatch,
    results: dict[str, tuple[bytes, bytes, int]],
    *,
    route_dev: str | None = "awg-probe0",
    netns_ip: str = "203.0.113.7",
    host_ip: str = "198.51.100.9",
) -> list[tuple]:
    """``results``: target url -> (stdout, stderr, code) для curl к целям.
    Гейт _egress_gate по умолчанию «здоровый»: маршрут из netns идёт через
    ``route_dev`` (None → `ip route get` падает), внешний IP из netns
    (``netns_ip``) отличается от IP хоста (``host_ip``)."""
    calls: list[tuple] = []

    async def fake_create_subprocess_exec(*cmd, stdout=None, stderr=None):
        calls.append(cmd)
        if "route" in cmd and "get" in cmd:
            if route_dev is None:
                return _FakeProc(b"", b"RTNETLINK answers: Network is unreachable", 2)
            return _FakeProc(f"1.1.1.1 dev {route_dev} src 10.9.0.14\n".encode(), b"", 0)
        if cmd[-1] == _IP_ECHO:
            ip = netns_ip if "netns" in cmd else host_ip
            return _FakeProc(ip.encode(), b"", 0)
        out, err, code = results[cmd[-1]]
        return _FakeProc(out, err, code)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return calls


def _target_curl_calls(calls: list[tuple]) -> list[tuple]:
    """Только вызовы curl к целям проверки — без гейта (route get / ip-echo)."""
    return [
        c for c in calls if "curl" in c and c[-1] != _IP_ECHO and not ("route" in c and "get" in c)
    ]


def _result_for(node_link: _FakeNodeLink, target: str, *, call_index: int = 0) -> dict:
    results = node_link.calls[call_index]["args"]["results"]
    return next(r for r in results if r["target"] == target)


def test_describe_declares_check_action():
    desc = VpnCheckService(_settings(), _FakeNodeLink()).describe()
    assert desc.info.service == "vpn_check"
    assert desc.find_action("check") is not None


async def test_check_accepts_and_returns_immediately(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = VpnCheckService(_settings(), _FakeNodeLink())
    result = await service.run_command(
        "check", {"server": PROBE_SERVER, "targets": ["https://1.1.1.1"]}
    )
    assert result == {"accepted": True, "server": PROBE_SERVER, "targets": ["https://1.1.1.1"]}


async def test_check_without_targets_is_bad_request():
    service = VpnCheckService(_settings(), _FakeNodeLink())
    with pytest.raises(ProtoError) as excinfo:
        await service.run_command("check", {"server": PROBE_SERVER})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_check_without_server_is_bad_request():
    service = VpnCheckService(_settings(), _FakeNodeLink())
    with pytest.raises(ProtoError) as excinfo:
        await service.run_command("check", {"targets": ["https://1.1.1.1"]})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_unknown_action_raises_value_error():
    with pytest.raises(ValueError):
        await VpnCheckService(_settings(), _FakeNodeLink()).run_command("fetch", {})


async def test_self_check_is_excluded(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    # Своя нода (socket.gethostname()) совпадает с проверяемым сервером.
    result = await service.run_command(
        "check", {"server": service._node, "targets": ["https://1.1.1.1"]}
    )
    assert result["skipped"] == "self-check исключён"
    assert node_link.calls == []
    assert calls == []


async def test_check_for_server_without_local_tunnel_is_skipped(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(probe_server="wooster"), node_link)
    result = await service.run_command(
        "check", {"server": "jeeves", "targets": ["https://1.1.1.1"]}
    )
    assert "нет локально настроенного тоннеля" in result["skipped"]
    assert node_link.calls == []
    assert calls == []


async def test_check_skipped_when_probe_server_not_configured(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(probe_server=""), node_link)
    result = await service.run_command(
        "check", {"server": "jeeves", "targets": ["https://1.1.1.1"]}
    )
    assert "нет локально настроенного тоннеля" in result["skipped"]
    assert calls == []


async def test_report_fans_out_to_all_live_vpn_nodes(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    await VpnCheckService(_settings(), node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    assert node_link.calls[0]["dst"].node == "wooster"


async def test_report_is_dropped_when_no_vpn_in_swarm(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    node_link.state = {"node": "alfred", "peers": [], "services": []}
    await VpnCheckService(_settings(), node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    assert node_link.calls == []  # некому слать — и не пытаемся


async def test_run_and_report_pushes_ok_result(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    assert len(node_link.calls) == 1
    call = node_link.calls[0]
    assert call["action"] == "report_check"
    assert call["args"]["node"]
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["server"] == PROBE_SERVER
    assert res["transport"] == "awg"
    assert res["ok"] is True
    assert res["error"] is None
    assert isinstance(res["ms"], int)


async def test_run_and_report_marks_http_error_as_failed(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"503", b"", 0)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "503" in res["error"]


async def test_run_and_report_marks_curl_failure(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"", b"connection refused", 7)})
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "connection refused" in res["error"]


async def test_check_runs_curl_inside_probe_netns(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = VpnCheckService(_settings(), _FakeNodeLink())
    await service._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    cmd = _target_curl_calls(calls)[0]
    assert "netns" in cmd
    assert cmd[cmd.index("netns") + 1] == "exec"
    assert cmd[cmd.index("netns") + 2] == "vpn-probe"
    assert "curl" in cmd


async def test_gate_fails_all_targets_when_route_bypasses_tunnel(monkeypatch):
    # route_dev != iface → пробник не в туннеле → все цели падают одной
    # ошибкой, curl к самим целям даже не зовётся.
    calls = _patch_curl(
        monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)}, route_dev="vprobe-veth1"
    )
    node_link = _FakeNodeLink()
    await VpnCheckService(_settings(), node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "не в туннеле" in res["error"]
    assert _target_curl_calls(calls) == []


async def test_gate_fails_when_exit_ip_equals_host_ip(monkeypatch):
    # Маршрут вроде через туннель, но внешний IP из netns == IP хоста →
    # трафик всё равно идёт мимо VPN.
    calls = _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        netns_ip="198.51.100.9",
        host_ip="198.51.100.9",
    )
    node_link = _FakeNodeLink()
    await VpnCheckService(_settings(), node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "мимо VPN" in res["error"]
    assert _target_curl_calls(calls) == []


async def test_gate_skips_exit_ip_check_on_vpn_exit_node(monkeypatch):
    # На самой VPN-ноде (назначение "vpn") IP туннеля == IP хоста — это
    # норма, сверку exit-IP не делаем, проверки идут как обычно.
    from sa_home_bot.config import NodeConfig

    _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        netns_ip="198.51.100.9",
        host_ip="198.51.100.9",
    )
    node_link = _FakeNodeLink()
    settings = _settings(node=NodeConfig(assignments=["vpn", "vpn_check"]))
    await VpnCheckService(settings, node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is True


async def test_gate_reports_missing_sudoers_hint(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)}, route_dev=None)

    async def fail_perm(*cmd, stdout=None, stderr=None):
        if "route" in cmd and "get" in cmd:
            return _FakeProc(b"", b"sudo: a password is required", 1)
        raise AssertionError("гейт не должен идти дальше маршрута")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_perm)
    node_link = _FakeNodeLink()
    await VpnCheckService(_settings(), node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "nodectl fix" in res["error"]


async def test_gate_detects_localized_sudo_password_prompt(monkeypatch):
    # Русская локаль ноды: `sudo -n` пишет «sudo: требуется указать пароль».
    async def ru_locale(*cmd, stdout=None, stderr=None):
        if "route" in cmd and "get" in cmd:
            return _FakeProc(b"", "sudo: требуется указать пароль".encode(), 1)
        raise AssertionError("гейт не должен идти дальше маршрута")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", ru_locale)
    node_link = _FakeNodeLink()
    await VpnCheckService(_settings(), node_link)._run_and_report(PROBE_SERVER, ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "nodectl fix" in res["error"] and "нет прав" in res["error"]


async def test_run_and_report_multiple_targets(monkeypatch):
    _patch_curl(
        monkeypatch,
        {
            "https://1.1.1.1": (b"200", b"", 0),
            "https://api.telegram.org": (b"", b"timed out", 28),
        },
    )
    node_link = _FakeNodeLink()
    service = VpnCheckService(_settings(), node_link)
    await service._run_and_report(PROBE_SERVER, ["https://1.1.1.1", "https://api.telegram.org"])
    assert _result_for(node_link, "https://1.1.1.1")["ok"] is True
    assert _result_for(node_link, "https://api.telegram.org")["ok"] is False
