"""Служба vpn_check: describe, запуск проверки, эфемерный подъём/останов
туннеля, пуш результата в vpn, обработка HTTP-ошибок и сбоев curl/awg-quick.

39.0.7(d): служба больше не хранит единственный netns/iface в конфиге —
слоты (что реально настроено локально) передаются явно в конструктор (в
проде — ``node/vpn_probe_state.py::load()``, здесь — фикстуры ниже)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sa_home_bot.config import Settings, VpnCheckConfig
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


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProc:
    """Двойник asyncio.subprocess.Process. Два режима:

    - «завершившийся» (``alive=False``, по умолчанию) — как раньше, только
      ``communicate()`` (awg-quick up/down, curl, ip route get — все
      короткоживущие команды через ``_run``).
    - «живой» (``alive=True``) — для xray-пробника (39.0.7(e)): долгоживущий
      процесс, ``returncode`` остаётся ``None`` пока не позвали
      ``terminate()``/``kill()``, ``wait()`` возвращает управление сразу
      после (в реальном asyncio она ждала бы фактического выхода, здесь
      это ни к чему — событийный цикл в тестах никто не крутит отдельно)."""

    def __init__(
        self, stdout: bytes = b"", stderr: bytes = b"", returncode: int | None = 0, *, alive=False
    ) -> None:
        self._stdout = stdout
        self._stderr_bytes = stderr
        self.returncode = None if alive else returncode
        self.stderr = _FakeStderr(stderr)
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr_bytes

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


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
    return Settings(node=node, vpn_check=VpnCheckConfig(**kwargs))


def _service(node_link, slots=None, **settings_kwargs) -> VpnCheckService:
    return VpnCheckService(
        _settings(**settings_kwargs), node_link, slots=[_slot()] if slots is None else slots
    )


def _patch_curl(
    monkeypatch,
    results: dict[str, tuple[bytes, bytes, int]],
    *,
    route_dev: str | None = "awg-probe0",
    route_err: bytes = b"RTNETLINK answers: Network is unreachable",
    netns_ip: str = "203.0.113.7",
    host_ip: str = "198.51.100.9",
    tunnel_up_ok: bool = True,
    tunnel_up_err: bytes = b"",
    reality_up_ok: bool = True,
    reality_up_err: bytes = b"",
) -> list[tuple]:
    """``results``: target url -> (stdout, stderr, code) для curl к целям.
    Гейт по умолчанию «здоровый»: ``awg-quick up``/xray-пробник проходят,
    маршрут из netns идёт через ``route_dev`` (None → `ip route get`
    падает, только для awg), внешний IP из netns (``netns_ip``) отличается
    от IP хоста (``host_ip``). ``asyncio.sleep`` замокан на no-op — иначе
    ретраи `_wait_for_route`/пауза после запуска xray реально ждали бы
    секунды в каждом тесте."""
    calls: list[tuple] = []

    async def fake_create_subprocess_exec(*cmd, stdout=None, stderr=None):
        calls.append(cmd)
        if any("awg-quick" in c for c in cmd):
            if "up" in cmd:
                return (
                    _FakeProc(b"", b"", 0)
                    if tunnel_up_ok
                    else _FakeProc(b"", tunnel_up_err, 1)
                )
            return _FakeProc(b"", b"", 0)  # down — best-effort, всегда «ок» в тестах
        if any("xray" in c for c in cmd):
            return (
                _FakeProc(alive=True)
                if reality_up_ok
                else _FakeProc(stderr=reality_up_err, returncode=1, alive=False)
            )
        if "route" in cmd and "get" in cmd:
            if route_dev is None:
                return _FakeProc(b"", route_err, 2)
            return _FakeProc(f"1.1.1.1 dev {route_dev} src 10.9.0.14\n".encode(), b"", 0)
        if cmd[-1] == _IP_ECHO:
            ip = netns_ip if "netns" in cmd else host_ip
            return _FakeProc(ip.encode(), b"", 0)
        out, err, code = results[cmd[-1]]
        return _FakeProc(out, err, code)

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    return calls


def _target_curl_calls(calls: list[tuple]) -> list[tuple]:
    """Только вызовы curl к целям проверки — без гейта/тоннеля (route get /
    ip-echo / awg-quick up|down)."""
    return [
        c
        for c in calls
        if "curl" in c and c[-1] != _IP_ECHO and not ("route" in c and "get" in c)
    ]


def _tunnel_calls(calls: list[tuple], verb: str) -> list[tuple]:
    return [c for c in calls if any("awg-quick" in tok for tok in c) and verb in c]


def _xray_calls(calls: list[tuple]) -> list[tuple]:
    return [c for c in calls if any("xray" in tok for tok in c)]


def _result_for(node_link: _FakeNodeLink, target: str, *, call_index: int = 0) -> dict:
    results = node_link.calls[call_index]["args"]["results"]
    return next(r for r in results if r["target"] == target)


def test_describe_declares_check_action():
    desc = _service(_FakeNodeLink()).describe()
    assert desc.info.service == "vpn_check"
    assert desc.find_action("check") is not None


async def test_check_accepts_and_returns_immediately(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = _service(_FakeNodeLink())
    result = await service.run_command(
        "check", {"server": PROBE_SERVER, "targets": ["https://1.1.1.1"]}
    )
    assert result == {"accepted": True, "server": PROBE_SERVER, "targets": ["https://1.1.1.1"]}


async def test_check_without_targets_is_bad_request():
    service = _service(_FakeNodeLink())
    with pytest.raises(ProtoError) as excinfo:
        await service.run_command("check", {"server": PROBE_SERVER})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_check_without_server_is_bad_request():
    service = _service(_FakeNodeLink())
    with pytest.raises(ProtoError) as excinfo:
        await service.run_command("check", {"targets": ["https://1.1.1.1"]})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_unknown_action_raises_value_error():
    with pytest.raises(ValueError):
        await _service(_FakeNodeLink()).run_command("fetch", {})


async def test_self_check_is_excluded(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = _service(node_link)
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
    service = _service(node_link, slots=[_slot(server="wooster")])
    result = await service.run_command(
        "check", {"server": "jeeves", "targets": ["https://1.1.1.1"]}
    )
    assert "нет локально настроенного тоннеля" in result["skipped"]
    assert node_link.calls == []
    assert calls == []


async def test_check_skipped_when_no_slots_configured(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = _service(node_link, slots=[])
    result = await service.run_command(
        "check", {"server": "jeeves", "targets": ["https://1.1.1.1"]}
    )
    assert "нет локально настроенного тоннеля" in result["skipped"]
    assert calls == []


async def test_report_fans_out_to_all_live_vpn_nodes(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    assert node_link.calls[0]["dst"].node == "wooster"


async def test_report_is_dropped_when_no_vpn_in_swarm(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    node_link.state = {"node": "alfred", "peers": [], "services": []}
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    assert node_link.calls == []  # некому слать — и не пытаемся


async def test_run_and_report_pushes_ok_result(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    service = _service(node_link)
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
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
    service = _service(node_link)
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "503" in res["error"]


async def test_run_and_report_marks_curl_failure(monkeypatch):
    _patch_curl(monkeypatch, {"https://1.1.1.1": (b"", b"connection refused", 7)})
    node_link = _FakeNodeLink()
    service = _service(node_link)
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "connection refused" in res["error"]


async def test_check_runs_curl_inside_probe_netns(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = _service(_FakeNodeLink())
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    cmd = _target_curl_calls(calls)[0]
    assert "netns" in cmd
    assert cmd[cmd.index("netns") + 1] == "exec"
    assert cmd[cmd.index("netns") + 2] == "vpn-probe-jeeves-awg"
    assert "curl" in cmd


async def test_ephemeral_tunnel_brought_up_and_torn_down_around_checks(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    service = _service(_FakeNodeLink())
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    up_calls = _tunnel_calls(calls, "up")
    down_calls = _tunnel_calls(calls, "down")
    assert len(up_calls) == 1 and "awg-probe0" in up_calls[0]
    assert len(down_calls) == 1 and "awg-probe0" in down_calls[0]
    # Порядок: up идёт раньше проверки целей, down — после.
    up_idx = calls.index(up_calls[0])
    down_idx = calls.index(down_calls[0])
    target_idx = calls.index(_target_curl_calls(calls)[0])
    assert up_idx < target_idx < down_idx


async def test_tunnel_torn_down_even_when_checks_raise(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)}, route_dev=None)
    service = _service(_FakeNodeLink())
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    assert len(_tunnel_calls(calls, "down")) == 1


async def test_tunnel_up_failure_fails_all_targets_without_curling(monkeypatch):
    calls = _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        tunnel_up_ok=False,
        tunnel_up_err=b"amneziawg-go: no such device",
    )
    node_link = _FakeNodeLink()
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "awg-quick up" in res["error"]
    assert _target_curl_calls(calls) == []
    # up "не прошёл", но down всё равно best-effort вызывается.
    assert len(_tunnel_calls(calls, "down")) == 1


async def test_tunnel_up_permission_error_hints_nodectl_fix(monkeypatch):
    _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        tunnel_up_ok=False,
        tunnel_up_err=b"sudo: a password is required",
    )
    node_link = _FakeNodeLink()
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert "nodectl fix" in res["error"]


async def test_two_slots_for_same_server_are_both_checked(monkeypatch):
    # Сервер несёт оба транспорта, и оба провижинены на этой ноде — один
    # dispatch-запрос ("проверьте jeeves") должен прогнать оба слота и
    # вернуть по строке результата на каждый.
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    slots = [_slot(), _reality_slot()]
    await _service(node_link, slots=slots)._run_and_report(
        PROBE_SERVER, slots, ["https://1.1.1.1"]
    )
    results = node_link.calls[0]["args"]["results"]
    transports = {r["transport"] for r in results}
    assert transports == {"awg", "reality"}
    assert all(r["ok"] is True for r in results)
    # Оба туннеля поднимались/гасились независимо друг от друга.
    assert len(_tunnel_calls(calls, "up")) == 1
    assert len(_xray_calls(calls)) == 1


async def test_unsupported_transport_reports_soft_error_without_crashing(monkeypatch):
    # Задел на будущее: слот с транспортом, для которого ещё нет ни одной
    # реализации в этой службе (ни awg, ни reality) — не должен ронять
    # проверку соседних слотов, только сам себе как мягкая ошибка.
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    unknown_slot = _slot(transport="openvpn", netns="vpn-probe-jeeves-openvpn", iface=None)
    await _service(node_link, slots=[unknown_slot])._run_and_report(
        PROBE_SERVER, [unknown_slot], ["https://1.1.1.1"]
    )
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "openvpn" in res["error"]
    assert _target_curl_calls(calls) == []


async def test_reality_tunnel_brought_up_and_torn_down_around_checks(monkeypatch):
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})
    node_link = _FakeNodeLink()
    slot = _reality_slot()
    await _service(node_link, slots=[slot])._run_and_report(
        PROBE_SERVER, [slot], ["https://1.1.1.1"]
    )
    xray_calls = _xray_calls(calls)
    assert len(xray_calls) == 1
    assert "vpn-probe-jeeves-reality" in xray_calls[0]
    target_calls = _target_curl_calls(calls)
    assert len(target_calls) == 1
    assert "--socks5" in target_calls[0]
    assert f"127.0.0.1:{slot.socks_port}" in target_calls[0]
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is True


async def test_reality_tunnel_up_failure_fails_targets_without_curling(monkeypatch):
    calls = _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        reality_up_ok=False,
        reality_up_err=b"xray: failed to parse config",
    )
    node_link = _FakeNodeLink()
    slot = _reality_slot()
    await _service(node_link, slots=[slot])._run_and_report(
        PROBE_SERVER, [slot], ["https://1.1.1.1"]
    )
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "xray" in res["error"]
    assert _target_curl_calls(calls) == []


async def test_reality_tunnel_up_permission_error_hints_nodectl_fix(monkeypatch):
    _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        reality_up_ok=False,
        reality_up_err=b"sudo: a password is required",
    )
    node_link = _FakeNodeLink()
    slot = _reality_slot()
    await _service(node_link, slots=[slot])._run_and_report(
        PROBE_SERVER, [slot], ["https://1.1.1.1"]
    )
    res = _result_for(node_link, "https://1.1.1.1")
    assert "nodectl fix" in res["error"]


async def test_reality_gate_uses_socks_exit_ip_not_route(monkeypatch):
    # Reality не переписывает default route — гейт не должен дёргать
    # `ip route get` вообще, только сверку exit-IP через сам SOCKS.
    calls = _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        netns_ip="198.51.100.9",
        host_ip="198.51.100.9",  # совпадает с хостом — трафик мимо VPN
    )
    node_link = _FakeNodeLink()
    slot = _reality_slot()
    await _service(node_link, slots=[slot])._run_and_report(
        PROBE_SERVER, [slot], ["https://1.1.1.1"]
    )
    assert not any("route" in c and "get" in c for c in calls)
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "мимо VPN" in res["error"]


async def test_gate_fails_all_targets_when_route_bypasses_tunnel(monkeypatch):
    # route_dev != iface → пробник не в туннеле → все цели падают одной
    # ошибкой, curl к самим целям даже не зовётся.
    calls = _patch_curl(
        monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)}, route_dev="vprobe-veth1"
    )
    node_link = _FakeNodeLink()
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
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
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
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
    service = _service(node_link, node=NodeConfig(assignments=["vpn", "vpn_check"]))
    await service._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is True


async def test_gate_reports_missing_sudoers_hint(monkeypatch):
    _patch_curl(
        monkeypatch,
        {"https://1.1.1.1": (b"200", b"", 0)},
        route_dev=None,
        route_err=b"sudo: a password is required",
    )
    node_link = _FakeNodeLink()
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
    res = _result_for(node_link, "https://1.1.1.1")
    assert res["ok"] is False
    assert "nodectl fix" in res["error"]


async def test_gate_detects_localized_sudo_password_prompt(monkeypatch):
    # Русская локаль ноды: `sudo -n` пишет «sudo: требуется указать пароль».
    calls = _patch_curl(monkeypatch, {"https://1.1.1.1": (b"200", b"", 0)})

    async def ru_locale(*cmd, stdout=None, stderr=None):
        calls.append(cmd)
        if any("awg-quick" in c for c in cmd):
            return _FakeProc(b"", b"", 0)
        if "route" in cmd and "get" in cmd:
            return _FakeProc(b"", "sudo: требуется указать пароль".encode(), 1)
        raise AssertionError("гейт не должен идти дальше маршрута")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", ru_locale)
    node_link = _FakeNodeLink()
    await _service(node_link)._run_and_report(PROBE_SERVER, [_slot()], ["https://1.1.1.1"])
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
    service = _service(node_link)
    await service._run_and_report(
        PROBE_SERVER, [_slot()], ["https://1.1.1.1", "https://api.telegram.org"]
    )
    assert _result_for(node_link, "https://1.1.1.1")["ok"] is True
    assert _result_for(node_link, "https://api.telegram.org")["ok"] is False
