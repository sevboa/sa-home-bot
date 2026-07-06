"""Пара клиент↔сервер протокола v0 на unix-сокете во временной директории."""

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_UNKNOWN_ACTION,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import ProtoServer


class FakeMonitor:
    """Мини-служба для тестов: пара действий, счётчик команд."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict]] = []

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="monitor", version="0.7.0"),
            capabilities=("temperature",),
            actions=(
                ActionSpec(id="scan_now", title="Запустить скан"),
                ActionSpec(
                    id="set_threshold",
                    title="Порог",
                    params=(ActionParam(name="value", type="float"),),
                ),
                ActionSpec(id="boom", title="Падает"),
            ),
        )

    async def get_state(self) -> dict:
        return {"cpu": {"temperature_c": 41.0}, "status": "ok"}

    async def run_command(self, action: str, args: dict) -> dict:
        if action == "boom":
            raise RuntimeError("внутренняя поломка")
        self.commands.append((action, args))
        return {"accepted": True}


@pytest.fixture
async def rpc():
    # Свой короткий tempdir: путь unix-сокета ограничен ~108 байтами.
    tmpdir = Path(tempfile.mkdtemp(prefix="sa-proto-"))
    socket_path = tmpdir / "monitor.sock"
    handler = FakeMonitor()
    server = ProtoServer(socket_path, handler)
    await server.start()
    events: list = []
    got_event = asyncio.Event()

    async def on_event(env):
        events.append(env)
        got_event.set()

    client = ProtoClient(socket_path, on_event=on_event, timeout=5.0)
    await client.connect()
    try:
        yield handler, server, client, events, got_event
    finally:
        await client.close()
        await server.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_hello_and_describe(rpc):
    _, _, client, _, _ = rpc
    info = await client.hello()
    assert (info.node, info.service) == ("alfred", "monitor")

    desc = await client.describe()
    assert "temperature" in desc.capabilities
    assert desc.find_action("scan_now") is not None


async def test_get_state(rpc):
    _, _, client, _, _ = rpc
    state = await client.get_state()
    assert state["cpu"]["temperature_c"] == 41.0


async def test_command_dispatched_to_handler(rpc):
    handler, _, client, _, _ = rpc
    result = await client.command("scan_now")
    assert result == {"accepted": True}
    assert handler.commands == [("scan_now", {})]


async def test_unknown_action_rejected(rpc):
    handler, _, client, _, _ = rpc
    with pytest.raises(ProtoError) as exc_info:
        await client.command("format_disk")
    assert exc_info.value.code == ERR_UNKNOWN_ACTION
    assert handler.commands == []


async def test_missing_required_param_rejected(rpc):
    _, _, client, _, _ = rpc
    with pytest.raises(ProtoError) as exc_info:
        await client.command("set_threshold")
    assert exc_info.value.code == ERR_BAD_REQUEST
    # с параметром — проходит
    result = await client.command("set_threshold", {"value": 85.0})
    assert result == {"accepted": True}


async def test_handler_crash_returns_internal_error(rpc):
    _, _, client, _, _ = rpc
    with pytest.raises(ProtoError) as exc_info:
        await client.command("boom")
    assert exc_info.value.code == ERR_INTERNAL
    # соединение живо — следующий запрос работает
    assert (await client.get_state())["status"] == "ok"


async def test_event_broadcast_reaches_client(rpc):
    _, server, _, events, got_event = rpc
    await server.broadcast_event("overheat_started", {"component_id": "cpu:package"})
    await asyncio.wait_for(got_event.wait(), timeout=5.0)
    assert events[0].payload["event"] == "overheat_started"
    assert events[0].src.service == "monitor"


async def test_event_reaches_all_clients(rpc):
    _, server, client, _, _ = rpc
    events2: list = []
    got2 = asyncio.Event()

    async def on_event2(env):
        events2.append(env)
        got2.set()

    client2 = ProtoClient(client._path, on_event=on_event2, timeout=5.0)
    await client2.connect()
    try:
        # дождаться регистрации второго подключения на сервере
        await client2.hello()
        await server.broadcast_event("smart_degraded", {})
        await asyncio.wait_for(got2.wait(), timeout=5.0)
        assert events2[0].payload["event"] == "smart_degraded"
    finally:
        await client2.close()


async def test_server_survives_garbage_line(rpc):
    _, _, client, _, _ = rpc
    # мусор напрямую в сокет — сервер должен ответить ошибкой и не упасть
    client._writer.write(b"garbage\n")
    await client._writer.drain()
    assert (await client.get_state())["status"] == "ok"


async def test_server_stop_fails_pending_and_closes_client(rpc):
    _, server, client, _, _ = rpc
    await server.stop()
    await asyncio.sleep(0.1)  # дать читателю клиента увидеть EOF
    with pytest.raises((ProtoError, ConnectionError, OSError)):
        await client.get_state()
