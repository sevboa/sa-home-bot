import asyncio
import shutil
import tempfile
from pathlib import Path

from sa_home_bot.node import supervisor as supervisor_mod
from sa_home_bot.node.peers import PeerLink
from sa_home_bot.node.supervisor import SupervisedService
from sa_home_bot.proto import server as server_mod
from sa_home_bot.proto.server import ProtoServer, _Connection
from sa_home_bot.utils import lifespan as lifespan_mod
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.utils.shutdown import ShutdownBudget
from tests.unit.netsupport import StubService

pytest_plugins = ["tests.unit.sock_fixture"]


class HangingWriter:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()

    def get_extra_info(self, name: str, default=None):
        return default


def sock_dir():
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def test_бюджет_бросает_зависший_шаг_и_идёт_дальше():
    budget = ShutdownBudget(total_s=5.0, step_s=0.2)

    assert await budget.step("быстрый", asyncio.sleep(0)) is True
    assert await budget.step("зависший", asyncio.Event().wait()) is False
    assert await budget.step("после зависшего", asyncio.sleep(0)) is True
    assert budget.overdue == ["зависший"]


async def test_бюджет_не_даёт_шагам_сложиться_за_общий_дедлайн():
    budget = ShutdownBudget(total_s=0.3, step_s=5.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for i in range(5):
        await budget.step(f"линк {i}", asyncio.Event().wait())
    assert loop.time() - started < 2.0, "общий дедлайн не ограничил сумму шагов"
    assert len(budget.overdue) == 5


async def test_proto_сервер_останавливается_с_полумёртвым_клиентом(sock_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "CLOSE_TIMEOUT_S", 0.05)
    server = ProtoServer(f"unix://{sock_dir / 'node.sock'}", StubService("peer"))
    await server.start()
    server._connections.add(_Connection(HangingWriter(), authenticated=True))

    await asyncio.wait_for(server.stop(), timeout=10.0)


async def test_погашенный_линк_не_переподключается(sock_dir):
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, StubService("peer"))
    await server.start()

    link = PeerLink("peer", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not link.alive and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert link.alive, "линк не поднялся"

        link._dropping = True
        await asyncio.wait_for(link.stop(), timeout=5.0)
        assert not link.alive

        await asyncio.sleep(0.3)
        assert not link.alive, "погашенный линк поднялся заново"
        assert server.connection_count == 0, "погашенный линк держит соединение"
    finally:
        await server.stop()


async def test_служба_останавливается_с_зависшей_задачей_наблюдения(monkeypatch):
    monkeypatch.setattr(supervisor_mod, "SUPERVISE_EXIT_TIMEOUT_S", 0.05)

    async def emit(event_type: str, data: dict) -> None:
        return None

    svc = SupervisedService("fake", [], emit=emit, stop_timeout_s=1.0)
    svc._desired_running = True
    svc._task = asyncio.create_task(asyncio.Event().wait(), name="supervise-fake")

    await asyncio.wait_for(svc.stop(), timeout=10.0)
    assert svc._task is None


async def test_lifespan_не_держат_зависшие_колбэки(monkeypatch):
    monkeypatch.setattr(lifespan_mod, "CALLBACK_TIMEOUT_S", 0.05)
    done: list[str] = []

    async def hangs() -> None:
        await asyncio.Event().wait()

    async def closes_db() -> None:
        done.append("db")

    lifespan = Lifespan()
    lifespan.push(closes_db)
    lifespan.push(hangs)

    await asyncio.wait_for(lifespan.shutdown(), timeout=5.0)
    assert done == ["db"], "зависший колбэк не должен отменять остальные"
