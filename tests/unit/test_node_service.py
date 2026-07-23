"""NodeService и nodectl-рендер: describe, get_state, команды, таблица статуса."""

import pytest

from sa_home_bot.node.service import NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import STOPPED, SupervisedService, Supervisor
from sa_home_bot.nodectl import render_services, render_status
from sa_home_bot.proto.messages import ProtoError


class Recorded:
    def __init__(self) -> None:
        self.calls: list[str] = []


def _fake_supervisor() -> tuple[Supervisor, Recorded]:
    async def emit(event_type, data):
        pass

    sup = Supervisor(["monitor", "telegram-bot"], "config.toml", emit=emit)
    rec = Recorded()

    # Не запускаем реальные процессы: подменяем управление записью вызовов.
    for name, svc in sup.services.items():
        async def start(n=name):
            rec.calls.append(f"start:{n}")

        async def stop(n=name):
            rec.calls.append(f"stop:{n}")

        async def restart(n=name):
            rec.calls.append(f"restart:{n}")

        svc.start = start
        svc.stop = stop
        svc.restart = restart
    return sup, rec


def test_describe_declares_actions_with_name_param():
    sup, _ = _fake_supervisor()
    desc = NodeService(sup).describe()
    assert desc.info.service == "node"
    for action_id in ("start", "stop", "restart"):
        action = desc.find_action(action_id)
        assert action is not None
        assert action.params[0].name == "name"
        assert action.params[0].required


async def test_get_state_lists_services():
    sup, _ = _fake_supervisor()
    state = await NodeService(sup).get_state()
    names = [s["name"] for s in state["services"]]
    assert names == ["monitor", "telegram-bot"]
    assert all(s["status"] == STOPPED for s in state["services"])


async def test_commands_routed_to_service():
    sup, rec = _fake_supervisor()
    svc = NodeService(sup)
    await svc.run_command("start", {"name": "monitor"})
    await svc.run_command("restart", {"name": "telegram-bot"})
    await svc.run_command("stop", {"name": "monitor"})
    assert rec.calls == ["start:monitor", "restart:telegram-bot", "stop:monitor"]


async def test_unknown_service_is_bad_request():
    sup, _ = _fake_supervisor()
    with pytest.raises(ProtoError) as exc_info:
        await NodeService(sup).run_command("start", {"name": "ghost"})
    assert exc_info.value.code == "bad_request"
    assert "monitor" in exc_info.value.message  # подсказывает известные


async def test_power_actions_declared_and_scheduled(monkeypatch):
    import asyncio

    from sa_home_bot.node import service as service_module

    monkeypatch.setattr(service_module, "POWER_DELAY_S", 0.0)
    sup, _ = _fake_supervisor()
    ran: list[list[str]] = []

    async def fake_runner(argv):
        ran.append(argv)

    svc = NodeService(sup, power_runner=fake_runner)
    desc = svc.describe()
    assert "power" in desc.capabilities
    for action_id in ("poweroff", "reboot", "suspend"):
        action = desc.find_action(action_id)
        assert action is not None and not action.params

    result = await svc.run_command("poweroff", {})
    assert result["scheduled"] == "poweroff"
    await asyncio.sleep(0.05)  # дать отложенной задаче выполниться
    assert ran == [["systemctl", "poweroff"]]


async def test_restart_node_not_declared_without_callback():
    sup, _ = _fake_supervisor()
    desc = NodeService(sup).describe()
    assert desc.find_action("restart_node") is None


async def test_restart_node_declared_and_scheduled(monkeypatch):
    import asyncio

    from sa_home_bot.node import service as service_module

    monkeypatch.setattr(service_module, "POWER_DELAY_S", 0.0)
    sup, _ = _fake_supervisor()
    calls: list[str] = []

    svc = NodeService(sup, restart_node=lambda: calls.append("restart"))
    action = svc.describe().find_action("restart_node")
    assert action is not None and not action.params

    result = await svc.run_command("restart_node", {})
    assert result["scheduled"] == "restart_node"
    await asyncio.sleep(0.05)
    assert calls == ["restart"]


async def test_get_state_has_uptime():
    sup, _ = _fake_supervisor()
    state = await NodeService(sup).get_state()
    assert state["uptime_s"] >= 0
    # на Linux /proc/uptime всегда есть; поле не None и растёт от загрузки
    assert state["system_uptime_s"] > 0


def test_render_status_table():
    state = {
        "node": "alfred",
        "version": "0.8.0",
        "services": [
            {
                "name": "monitor",
                "status": "running",
                "pid": 123,
                "restarts": 0,
                "started_at": "2026-07-07T00:00:00+00:00",
            },
            {
                "name": "telegram-bot",
                "status": "stopped",
                "pid": None,
                "restarts": 2,
                "started_at": None,
            },
        ],
    }
    text = render_status(state)
    assert "Нода alfred (v0.8.0)" in text
    assert "🟢 running" in text and "🔴 stopped" in text
    assert "123" in text and "—" in text
    assert "Пиры" not in text  # без пиров секция не рисуется
    assert "Аптайм" not in text  # без полей аптайма строка не рисуется

    state["system_uptime_s"] = 90061.0  # 1д 1ч 1м 1с
    state["uptime_s"] = 65.0
    text = render_status(state)
    assert "Аптайм: система 1д 1ч 1м 1с · нода 1м 5с" in text

    state["peers"] = [
        {"id": "winpc", "endpoint": "tcp://192.168.0.50:8710", "alive": False},
    ]
    text = render_status(state)
    assert "Пиры:" in text and "🔴 winpc (tcp://192.168.0.50:8710)" in text


def test_render_services_empty():
    assert "не назначены" in render_services([])


# --- assign/unassign: назначения в рантайме (этап 17) ---


def test_describe_declares_assign_and_unassign():
    sup, _ = _fake_supervisor()
    desc = NodeService(sup).describe()
    assign = desc.find_action("assign")
    assert assign is not None
    # llm сознательно не в ASSIGNMENT_ARGS — не супервизируется (живая находка
    # 2026-07-23, node/supervisor.py::EXTERNALLY_MANAGED_ASSIGNMENTS).
    assert set(assign.params[0].choices) == {"monitor", "telegram-bot", "apps", "torrents"}
    unassign = desc.find_action("unassign")
    assert unassign is not None
    assert set(unassign.params[0].choices) == {"monitor", "telegram-bot"}  # только назначенные


async def test_assign_starts_service_and_persists(tmp_path, monkeypatch):
    started: list[str] = []

    async def fake_start(self):
        started.append(self.name)

    monkeypatch.setattr(SupervisedService, "start", fake_start)

    async def emit(event_type, data):
        pass

    sup = Supervisor([], None, emit=emit)
    state_path = tmp_path / "node-state.json"
    svc = NodeService(sup, state=NodeState(), state_path=str(state_path))

    result = await svc.run_command("assign", {"name": "apps"})
    assert result["service"]["name"] == "apps"
    assert started == ["apps"]
    assert "apps" in sup.services
    assert NodeState.load(state_path).assignments == ["apps"]


async def test_assign_idempotent_reuses_existing_service(monkeypatch):
    started: list[str] = []

    async def fake_start(self):
        started.append(self.name)

    monkeypatch.setattr(SupervisedService, "start", fake_start)

    async def emit(event_type, data):
        pass

    sup = Supervisor(["monitor"], None, emit=emit)
    original = sup.get("monitor")
    svc = NodeService(sup)

    await svc.run_command("assign", {"name": "monitor"})
    assert sup.get("monitor") is original  # не пересоздана
    assert started == ["monitor"]


async def test_assign_unknown_name_is_bad_request():
    sup, _ = _fake_supervisor()
    with pytest.raises(ProtoError) as exc_info:
        await NodeService(sup).run_command("assign", {"name": "no-such-service"})
    assert exc_info.value.code == "bad_request"


async def test_unassign_stops_removes_and_persists(tmp_path, monkeypatch):
    stopped: list[str] = []

    async def fake_stop(self):
        stopped.append(self.name)

    monkeypatch.setattr(SupervisedService, "stop", fake_stop)

    async def emit(event_type, data):
        pass

    sup = Supervisor(["apps"], None, emit=emit)
    state_path = tmp_path / "node-state.json"
    state = NodeState(assignments=["apps"])
    svc = NodeService(sup, state=state, state_path=str(state_path))

    result = await svc.run_command("unassign", {"name": "apps"})
    assert result == {"unassigned": "apps"}
    assert "apps" not in sup.services
    assert stopped == ["apps"]
    assert NodeState.load(state_path).assignments == []


async def test_unassign_unknown_service_is_bad_request():
    sup, _ = _fake_supervisor()
    with pytest.raises(ProtoError) as exc_info:
        await NodeService(sup).run_command("unassign", {"name": "ghost"})
    assert exc_info.value.code == "bad_request"


# --- check_update/update: самообновление через pipx (без рестарта) ---


def test_update_actions_not_declared_without_update_source():
    sup, _ = _fake_supervisor()
    desc = NodeService(sup).describe()
    assert desc.find_action("check_update") is None
    assert desc.find_action("update") is None


def test_update_actions_declared_with_update_source():
    sup, _ = _fake_supervisor()
    desc = NodeService(sup, update_source="https://github.com/x/y.git").describe()
    assert desc.find_action("check_update") is not None
    update_action = desc.find_action("update")
    assert update_action is not None and not update_action.params


async def test_get_state_has_no_update_field_without_source():
    sup, _ = _fake_supervisor()
    state = await NodeService(sup).get_state()
    assert "update" not in state


async def test_get_state_reports_restart_required(monkeypatch):
    from sa_home_bot.node import service as service_module

    monkeypatch.setattr(service_module, "__version__", "0.21.0")
    monkeypatch.setattr(
        service_module.node_update, "installed_version", lambda: "0.22.0"
    )
    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    state = await svc.get_state()
    assert state["update"] == {
        "running": "0.21.0",
        "installed": "0.22.0",
        "restart_required": True,
        "last": None,
    }


async def test_get_state_no_restart_required_when_versions_match(monkeypatch):
    from sa_home_bot.node import service as service_module

    monkeypatch.setattr(service_module, "__version__", "0.22.0")
    monkeypatch.setattr(
        service_module.node_update, "installed_version", lambda: "0.22.0"
    )
    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    state = await svc.get_state()
    assert state["update"]["restart_required"] is False


async def test_check_update_reports_versions(monkeypatch):
    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        assert repo_url == "https://github.com/x/y.git"
        return "v0.22.0"

    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.21.0")

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    result = await svc.run_command("check_update", {})
    assert result == {
        "repo": "https://github.com/x/y.git",
        "running": svc.describe().info.version,
        "installed": "0.21.0",
        # "v"-префикс тега снят — сравнимо с installed/running (PEP 440, без "v")
        "latest": "0.22.0",
    }


async def test_check_update_network_failure_is_internal_error(monkeypatch):
    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        return None

    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("check_update", {})
    assert exc_info.value.code == "internal"


async def test_update_already_current_does_not_reinstall(monkeypatch):
    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        return "v0.22.0"

    called = []

    async def fake_reinstall(repo_url, ref):
        called.append((repo_url, ref))
        return True, "ok"

    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    # installed_version() — реальный importlib.metadata.version(), без "v" (PEP 440)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.22.0")
    monkeypatch.setattr(service_module.node_update, "pipx_reinstall", fake_reinstall)

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    result = await svc.run_command("update", {})

    assert result == {"up_to_date": True, "version": "0.22.0"}
    assert called == []  # pipx не звали


async def test_update_schedules_background_reinstall_and_emits_event(monkeypatch):
    import asyncio

    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        return "v0.22.0"

    async def fake_reinstall(repo_url, ref):
        assert (repo_url, ref) == ("https://github.com/x/y.git", "v0.22.0")
        return True, "installed ok"

    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.21.0")
    monkeypatch.setattr(service_module.node_update, "pipx_reinstall", fake_reinstall)

    events: list[tuple[str, dict]] = []

    async def emit(event_type, data):
        events.append((event_type, data))

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git", emit=emit)
    result = await svc.run_command("update", {})
    assert result == {"scheduled": True, "target_version": "0.22.0"}

    await asyncio.sleep(0.05)  # дать фоновой задаче выполниться
    assert events == [("update_finished", {"ok": True, "version": "0.22.0", "error": None})]
    assert svc._last_update == {"ok": True, "version": "0.22.0", "error": None}


async def test_update_concurrent_call_is_bad_request(monkeypatch):
    import asyncio

    from sa_home_bot.node import service as service_module

    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_latest(repo_url):
        return "v0.22.0"

    async def slow_reinstall(repo_url, ref):
        started.set()
        await release.wait()
        return True, "ok"

    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.21.0")
    monkeypatch.setattr(service_module.node_update, "pipx_reinstall", slow_reinstall)

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    await svc.run_command("update", {})
    await started.wait()

    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("update", {})
    assert exc_info.value.code == "bad_request"

    release.set()
    await asyncio.sleep(0.05)


# --- update на win32: дёргаем задачу планировщика, а не pipx в процессе ---


async def test_update_on_win32_triggers_scheduled_task_not_pipx(monkeypatch):
    import asyncio

    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        return "v0.22.0"

    async def fake_pipx_reinstall(repo_url, ref):
        raise AssertionError("на win32 pipx_reinstall звать нельзя")

    triggered = []

    async def fake_trigger_task():
        triggered.append(True)
        return True, "SUCCESS: Attempted to run the scheduled task"

    monkeypatch.setattr(service_module.sys, "platform", "win32")
    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.21.0")
    monkeypatch.setattr(service_module.node_update, "pipx_reinstall", fake_pipx_reinstall)
    monkeypatch.setattr(service_module.node_update, "trigger_scheduled_task", fake_trigger_task)

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    result = await svc.run_command("update", {})
    assert result == {"scheduled": True, "target_version": "0.22.0", "via": "scheduled_task"}

    await asyncio.sleep(0.05)  # дать фоновой задаче выполниться
    assert triggered == [True]
    assert svc._updating is False


async def test_update_on_win32_failure_emits_event(monkeypatch):
    import asyncio

    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        return "v0.22.0"

    async def fake_trigger_task():
        return False, "задача не найдена"

    monkeypatch.setattr(service_module.sys, "platform", "win32")
    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.21.0")
    monkeypatch.setattr(service_module.node_update, "trigger_scheduled_task", fake_trigger_task)

    events: list[tuple[str, dict]] = []

    async def emit(event_type, data):
        events.append((event_type, data))

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git", emit=emit)
    await svc.run_command("update", {})

    await asyncio.sleep(0.05)
    assert events == [
        ("update_finished", {"ok": False, "version": "0.22.0", "error": "задача не найдена"})
    ]


async def test_update_on_win32_already_current_does_not_trigger_task(monkeypatch):
    from sa_home_bot.node import service as service_module

    async def fake_latest(repo_url):
        return "v0.22.0"

    triggered = []

    async def fake_trigger_task():
        triggered.append(True)
        return True, "ok"

    monkeypatch.setattr(service_module.sys, "platform", "win32")
    monkeypatch.setattr(service_module.node_update, "latest_tag", fake_latest)
    monkeypatch.setattr(service_module.node_update, "installed_version", lambda: "0.22.0")
    monkeypatch.setattr(service_module.node_update, "trigger_scheduled_task", fake_trigger_task)

    sup, _ = _fake_supervisor()
    svc = NodeService(sup, update_source="https://github.com/x/y.git")
    result = await svc.run_command("update", {})

    assert result == {"up_to_date": True, "version": "0.22.0"}
    assert triggered == []


# --- send_wol / get_state()["wake"]: рой сам отправляет WoL (этап 19 п.6) ---


def test_describe_declares_send_wol_with_mac_param():
    sup, _ = _fake_supervisor()
    action = NodeService(sup).describe().find_action("send_wol")
    assert action is not None
    assert action.params[0].name == "mac"
    assert action.params[0].required


async def test_get_state_reports_wake_info_when_ethernet(monkeypatch):
    from sa_home_bot import wol
    from sa_home_bot.node import service as service_module

    info = wol.LocalWakeInfo(mac="aa:bb:cc:dd:ee:ff", ip="192.168.0.100", broadcast="192.168.0.255")
    monkeypatch.setattr(service_module.wol, "detect_local_wake_info", lambda: info)

    sup, _ = _fake_supervisor()
    state = await NodeService(sup).get_state()
    assert state["wake"] == {
        "mac": "aa:bb:cc:dd:ee:ff",
        "ip": "192.168.0.100",
        "broadcast": "192.168.0.255",
    }


async def test_get_state_wake_none_without_ethernet(monkeypatch):
    from sa_home_bot.node import service as service_module

    monkeypatch.setattr(service_module.wol, "detect_local_wake_info", lambda: None)
    sup, _ = _fake_supervisor()
    state = await NodeService(sup).get_state()
    assert state["wake"] is None


async def test_send_wol_normalizes_mac_and_binds_to_own_ip(monkeypatch):
    from sa_home_bot import wol
    from sa_home_bot.node import service as service_module

    info = wol.LocalWakeInfo(mac="7c:83:34:b4:59:ac", ip="192.168.0.100", broadcast="192.168.0.255")
    monkeypatch.setattr(service_module.wol, "detect_local_wake_info", lambda: info)
    sent = {}

    def fake_send(mac, broadcast="255.255.255.255", port=9, repeats=3, bind_ip=""):
        sent.update(mac=mac, bind_ip=bind_ip)

    monkeypatch.setattr(service_module.wol, "send_magic_packet", fake_send)

    sup, _ = _fake_supervisor()
    result = await NodeService(sup).run_command("send_wol", {"mac": "AA-BB-CC-DD-EE-FF"})
    assert result == {"sent": True, "mac": "aa:bb:cc:dd:ee:ff"}
    assert sent == {"mac": "aa:bb:cc:dd:ee:ff", "bind_ip": "192.168.0.100"}


async def test_send_wol_invalid_mac_is_bad_request():
    sup, _ = _fake_supervisor()
    with pytest.raises(ProtoError) as exc_info:
        await NodeService(sup).run_command("send_wol", {"mac": "не мак"})
    assert exc_info.value.code == "bad_request"


def test_resolve_endpoint_relative_to_config_dir(tmp_path):
    import argparse
    from pathlib import Path

    from sa_home_bot.nodectl import _resolve_endpoint
    from sa_home_bot.proto.endpoints import TcpEndpoint, UnixEndpoint

    config = tmp_path / "config.toml"
    config.write_text('[node]\nsocket = "./data/node.sock"\n[swarm]\ntoken = "t"\n')
    args = argparse.Namespace(socket=None, config=str(config))
    # Относительный сокет из конфига — относительно каталога конфига, не CWD.
    endpoint, token = _resolve_endpoint(args)
    assert endpoint == UnixEndpoint(tmp_path.resolve() / "data/node.sock")
    assert token == "t"

    # Явный --socket всегда важнее конфига (и понимает tcp://).
    args = argparse.Namespace(socket="/run/x.sock", config=str(config))
    assert _resolve_endpoint(args)[0] == UnixEndpoint(Path("/run/x.sock"))
    args = argparse.Namespace(socket="tcp://127.0.0.1:8710", config=str(config))
    assert _resolve_endpoint(args)[0] == TcpEndpoint("127.0.0.1", 8710)


def test_resolve_endpoint_falls_back_to_xdg_config(tmp_path, monkeypatch):
    import argparse

    from sa_home_bot.nodectl import _resolve_endpoint
    from sa_home_bot.proto.endpoints import TcpEndpoint

    # CWD без config.toml, зато конфиг лежит в ~/.config/sa-home-bot/
    # (нода, установленная через pipx) — nodectl должен найти его сам.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    xdg = tmp_path / ".config" / "sa-home-bot"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text(
        '[node]\nsocket = "tcp://100.64.0.1:8710"\n[swarm]\ntoken = "t"\n'
    )
    args = argparse.Namespace(socket=None, config=None)
    endpoint, token = _resolve_endpoint(args)
    assert endpoint == TcpEndpoint("100.64.0.1", 8710)
    assert token == "t"
