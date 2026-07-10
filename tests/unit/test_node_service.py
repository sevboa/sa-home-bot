"""NodeService и nodectl-рендер: describe, get_state, команды, таблица статуса."""

import pytest

from sa_home_bot.node.service import NodeService
from sa_home_bot.node.supervisor import STOPPED, Supervisor
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
    assert "✅ running" in text and "⏹ stopped" in text
    assert "123" in text and "—" in text


def test_render_services_empty():
    assert "не назначены" in render_services([])


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
