"""MonitorLink: запросы, недоступный монитор, переподключение, события."""

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.bot.monitor_link import MonitorLink, MonitorUnavailableError
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo
from sa_home_bot.proto.server import ProtoServer


class FakeMonitor:
    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="monitor", version="0.7.0"),
            capabilities=("temperature",),
            actions=(ActionSpec(id="scan_now", title="Запустить скан"),),
        )

    async def get_state(self) -> dict:
        return {"service": "monitor", "health": []}

    async def run_command(self, action: str, args: dict) -> dict:
        return {"sensor_queued": True, "smart_queued": True}


@pytest.fixture
def sock_dir():
    # Свой короткий tempdir: путь unix-сокета ограничен ~108 байтами.
    tmpdir = Path(tempfile.mkdtemp(prefix="sa-link-"))
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


async def _wait_connected(link: MonitorLink, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not link.connected:
            await asyncio.sleep(0.02)


async def test_requests_and_unavailable(sock_dir):
    socket_path = sock_dir / "m.sock"
    link = MonitorLink(socket_path, reconnect_delay=0.1)
    await link.start()
    try:
        # Монитора нет — запрос отваливается сразу и понятно.
        with pytest.raises(MonitorUnavailableError):
            await link.get_state()

        server = ProtoServer(socket_path, FakeMonitor())
        await server.start()
        await _wait_connected(link)

        assert (await link.get_state())["service"] == "monitor"
        assert (await link.command("scan_now"))["sensor_queued"] is True
        await server.stop()
    finally:
        await link.stop()


async def test_reconnects_and_receives_events(sock_dir):
    socket_path = sock_dir / "m.sock"
    handler = FakeMonitor()
    server = ProtoServer(socket_path, handler)
    await server.start()

    events: list = []
    got_event = asyncio.Event()

    async def on_event(env):
        events.append(env.payload)
        got_event.set()

    link = MonitorLink(socket_path, on_event=on_event, reconnect_delay=0.1)
    await link.start()
    try:
        await _wait_connected(link)

        # Монитор перезапустился.
        await server.stop()
        async with asyncio.timeout(5.0):
            while link.connected:
                await asyncio.sleep(0.02)

        server2 = ProtoServer(socket_path, handler)
        await server2.start()
        await _wait_connected(link)

        # После реконнекта события снова доходят.
        delivered = await server2.broadcast_event("overheat_started", {"component_id": "x"})
        assert delivered == 1
        async with asyncio.timeout(5.0):
            await got_event.wait()
        assert events[0]["event"] == "overheat_started"
        await server2.stop()
    finally:
        await link.stop()
