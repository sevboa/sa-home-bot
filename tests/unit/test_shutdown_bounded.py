"""Останов не имеет права висеть (этап 29).

Живой сбой на jeeves 2026-07-30: нода приняла `restart_node`, написала
«Останов ноды...» и осталась работать — процесс дожил до `TimeoutStopSec` и был
добит SIGKILL. Причина не одна: последовательность останова состояла из голых
`await`, каждый из которых полагался на добрую волю собеседника.

Здесь проверяются места, где останов мог не закончиться НИКОГДА, — каждое
отдельно, без сети и без таймингов планировщика.
"""

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.node.peers import PeerLink
from sa_home_bot.node.supervisor import SupervisedService
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo
from sa_home_bot.proto.server import ProtoServer, _Connection
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.utils.shutdown import ShutdownBudget


class FakeService:
    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="peer", service="node", version="0.0.0"),
            capabilities=(),
            actions=(ActionSpec(id="noop", title="Ничего"),),
        )

    async def get_state(self) -> dict:
        return {}

    async def run_command(self, action: str, args: dict) -> dict:
        return {}


class HangingWriter:
    """Полумёртвый сокет: closed(), но закрытие не подтверждается никогда.

    Так выглядит соединение, у которого данные лежат в Send-Q и не уходят:
    ни FIN, ни ошибка — `wait_closed()` просто не возвращается.
    """

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        await asyncio.Event().wait()

    def get_extra_info(self, name: str, default=None):
        return default


@pytest.fixture
def sock_dir():
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.mark.asyncio
async def test_бюджет_бросает_зависший_шаг_и_идёт_дальше():
    budget = ShutdownBudget(total_s=5.0, step_s=0.2)

    assert await budget.step("быстрый", asyncio.sleep(0)) is True
    assert await budget.step("зависший", asyncio.Event().wait()) is False
    # Просрочка не отменяет остальные шаги: соседей предупредить, детей
    # погасить и сокеты закрыть надо в любом случае.
    assert await budget.step("после зависшего", asyncio.sleep(0)) is True
    assert budget.overdue == ["зависший"]


@pytest.mark.asyncio
async def test_бюджет_не_даёт_шагам_сложиться_за_общий_дедлайн():
    """Пиров в рое много; каждый по потолку — это уже минуты."""
    budget = ShutdownBudget(total_s=0.3, step_s=5.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    for i in range(5):
        await budget.step(f"линк {i}", asyncio.Event().wait())
    assert loop.time() - started < 2.0, "общий дедлайн не ограничил сумму шагов"
    assert len(budget.overdue) == 5


@pytest.mark.asyncio
async def test_proto_сервер_останавливается_с_полумёртвым_клиентом(sock_dir):
    """Именно здесь останов ноды мог зависнуть навсегда: `wait_closed()` у
    клиента с забитым Send-Q не возвращается ни через минуту, ни через час."""
    server = ProtoServer(f"unix://{sock_dir / 'node.sock'}", FakeService())
    await server.start()
    server._connections.add(_Connection(HangingWriter(), authenticated=True))

    await asyncio.wait_for(server.stop(), timeout=10.0)


@pytest.mark.asyncio
async def test_погашенный_линк_не_переподключается(sock_dir):
    """Окно между `reconnect_now()` и пробуждением `_serve`: отмена от stop()
    в этот момент неотличима от нашего собственного сброса связи — её
    проглатывают, и линк продолжает переподключаться, а `stop()` ждёт задачу,
    которая уже никогда не завершится.

    Окно узкое и снаружи невоспроизводимо детерминированно, поэтому проверяем
    не гонку, а инвариант: после `stop()` линк лежит и связь не поднимает,
    в каком бы состоянии его ни застали.
    """
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, FakeService())
    await server.start()

    link = PeerLink("peer", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not link.alive and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert link.alive, "линк не поднялся"

        # Ровно состояние того окна: соединение сбрасываем мы сами, и в этот
        # момент линк гасят.
        link._dropping = True
        await asyncio.wait_for(link.stop(), timeout=5.0)
        assert not link.alive

        # Цикл переподключения, переживший stop(), успел бы поднять связь
        # заново — reconnect_delay здесь 0.05 с.
        await asyncio.sleep(0.3)
        assert not link.alive, "погашенный линк поднялся заново"
        assert server.connection_count == 0, "погашенный линк держит соединение"
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_служба_останавливается_с_зависшей_задачей_наблюдения():
    """`await self._task` без потолка: задача наблюдения могла спать перед
    перезапуском или писать событие полумёртвому клиенту."""

    async def emit(event_type: str, data: dict) -> None:
        return None

    svc = SupervisedService("fake", [], emit=emit, stop_timeout_s=1.0)
    svc._desired_running = True
    svc._task = asyncio.create_task(asyncio.Event().wait(), name="supervise-fake")

    await asyncio.wait_for(svc.stop(), timeout=10.0)
    assert svc._task is None


@pytest.mark.asyncio
async def test_lifespan_не_держат_зависшие_колбэки():
    done: list[str] = []

    async def hangs() -> None:
        await asyncio.Event().wait()

    async def closes_db() -> None:
        done.append("db")

    lifespan = Lifespan()
    lifespan.push(closes_db)
    lifespan.push(hangs)

    # Потолок колбэка — 10 с; общий срок теста заметно больше, чтобы падение
    # означало настоящее зависание, а не медленную машину.
    await asyncio.wait_for(lifespan.shutdown(), timeout=30.0)
    assert done == ["db"], "зависший колбэк не должен отменять остальные"
