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
    result = await client.command("set_threshold", {"value": 85.0})
    assert result == {"accepted": True}


async def test_handler_crash_returns_internal_error(rpc):
    _, _, client, _, _ = rpc
    with pytest.raises(ProtoError) as exc_info:
        await client.command("boom")
    assert exc_info.value.code == ERR_INTERNAL
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

    client2 = ProtoClient(client._endpoint, on_event=on_event2, timeout=5.0)
    await client2.connect()
    try:
        await client2.hello()
        await server.broadcast_event("smart_degraded", {})
        await asyncio.wait_for(got2.wait(), timeout=5.0)
        assert events2[0].payload["event"] == "smart_degraded"
    finally:
        await client2.close()


async def test_server_survives_garbage_line(rpc):
    _, _, client, _, _ = rpc
    client._writer.write(b"garbage\n")
    await client._writer.drain()
    assert (await client.get_state())["status"] == "ok"


async def test_timeout_s_overrides_default_per_request(tmp_path):
    class SlowMonitor(FakeMonitor):
        def describe(self) -> ServiceDescription:
            base = super().describe()
            return ServiceDescription(
                info=base.info,
                capabilities=base.capabilities,
                actions=(*base.actions, ActionSpec(id="slow", title="Медленно")),
            )

        async def run_command(self, action: str, args: dict) -> dict:
            if action == "slow":
                await asyncio.sleep(0.3)
                return {"accepted": True}
            return await super().run_command(action, args)

    socket_path = tmp_path / "slow.sock"
    server = ProtoServer(socket_path, SlowMonitor())
    await server.start()
    client = ProtoClient(socket_path, timeout=0.05)
    await client.connect()
    try:
        with pytest.raises(TimeoutError):
            await client.command("slow")
        result = await client.command("slow", timeout=2.0)
        assert result == {"accepted": True}
    finally:
        await client.close()
        await server.stop()


async def test_server_stop_fails_pending_and_closes_client(rpc):
    _, server, client, _, _ = rpc
    await server.stop()
    await asyncio.sleep(0.1)
    with pytest.raises((ProtoError, ConnectionError, OSError)):
        await client.get_state()


async def _client_with_handler(server, on_event):
    client = ProtoClient(server.endpoint, on_event=on_event, timeout=1.0)
    await client.connect()
    await client.hello()
    return client


async def test_event_handler_can_query_the_same_link(rpc):
    _, server, _, _, _ = rpc
    answered: list = []
    done = asyncio.Event()
    holder: dict = {}

    async def on_event(env):
        answered.append(await holder["client"].get_state())
        done.set()

    holder["client"] = client = await _client_with_handler(server, on_event)
    try:
        await server.broadcast_event("update_finished", {})
        await asyncio.wait_for(done.wait(), timeout=2.0)
        assert answered[0]["status"] == "ok"
    finally:
        await client.close()


async def test_slow_event_handler_does_not_hold_responses(rpc):
    _, server, _, _, _ = rpc
    release = asyncio.Event()

    async def on_event(env):
        await release.wait()

    client = await _client_with_handler(server, on_event)
    try:
        await server.broadcast_event("tool_call", {})
        assert (await asyncio.wait_for(client.get_state(), timeout=0.5))["status"] == "ok"
    finally:
        release.set()
        await client.close()


async def test_events_are_handled_one_at_a_time_in_order(rpc):
    _, server, _, _, _ = rpc
    seen: list[str] = []
    done = asyncio.Event()

    async def on_event(env):
        name = env.payload["event"]
        if name == "task_prewake":
            await asyncio.sleep(0.05)
        seen.append(name)
        if len(seen) == 2:
            done.set()

    client = await _client_with_handler(server, on_event)
    try:
        await server.broadcast_event("task_prewake", {})
        await server.broadcast_event("task_result", {})
        await asyncio.wait_for(done.wait(), timeout=2.0)
        assert seen == ["task_prewake", "task_result"]
    finally:
        await client.close()


async def test_join_delivers_event_sent_right_before_disconnect(rpc):
    _, server, _, _, _ = rpc
    seen: list[str] = []

    async def on_event(env):
        await asyncio.sleep(0.05)
        seen.append(env.payload["event"])

    client = await _client_with_handler(server, on_event)
    try:
        await server.broadcast_event("node_leaving", {})
        await server.stop()
        await asyncio.wait_for(client.join(), timeout=2.0)
        assert seen == ["node_leaving"]
    finally:
        await client.close()
