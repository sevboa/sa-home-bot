"""Живучесть линков на управляемой сети (стенд tests/netlab.py, этап 25 п. 1).

Те же классы отказов, что дала ночь 2026-07-28 на проде, но воспроизведённые
настоящим TCP-путём через прокси: half-open (заморозка без закрытия сокетов),
внезапная смерть без FIN, возвращение машины на тот же адрес. Существующие
tests/unit/test_link_recovery.py проверяют слои по отдельности (моками) —
здесь проверяется весь путь PeerLink → ProtoClient → сокет → сервер.
"""

import asyncio

import pytest

from sa_home_bot.node import peers as peers_mod
from sa_home_bot.node.peers import PeerLink
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import TcpEndpoint
from sa_home_bot.proto.messages import (
    MSG_GET_STATE,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
    make_request,
)
from sa_home_bot.proto.server import ProtoServer
from tests.netlab import NetLabProxy


class FakePeerService:
    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="winpc", service="node", version="0.43.0"),
            capabilities=(),
            actions=(ActionSpec(id="noop", title="Ничего"),),
        )

    async def get_state(self) -> dict:
        return {"node": "winpc"}

    async def run_command(self, action: str, args: dict) -> dict:
        return {"accepted": True}


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def stand(monkeypatch):
    """Сервер «соседа» + прокси перед ним. Тайминги ужаты, чтобы отказ
    обнаруживался за доли секунды, а не за прод-минуты."""
    from sa_home_bot.proto import client as client_mod

    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_MISSES", 2)
    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT_S", 0.05)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), FakePeerService(), token="s3cret")
    await server.start()
    proxy = NetLabProxy("127.0.0.1", server.endpoint.port)
    await proxy.start()
    link = PeerLink(
        "winpc", proxy.endpoint, token="s3cret", self_node="alfred", reconnect_delay=0.05
    )
    await link.start()
    assert await _wait_for(lambda: link.alive), "линк через прокси не поднялся"
    yield server, proxy, link
    await link.stop()
    await proxy.stop()
    await server.stop()


@pytest.mark.asyncio
async def test_прокси_прозрачен_для_протокола(stand):
    """Санити: через прокси проходит обычный RPC — стенд не искажает обмен."""
    server, proxy, link = stand
    env = make_request(MSG_GET_STATE)
    response = await link.forward(env)
    assert response.ok is True
    assert response.payload == {"node": "winpc"}


@pytest.mark.asyncio
async def test_half_open_обнаруживается_и_линк_воскресает(stand):
    """Прод-сценарий v0.42.0: сокет жив, ответы не приходят (half-open).

    Heartbeat обязан уронить линк, цикл переподключения — пережить закрытие
    полумёртвого сокета (v0.42.3) и восстановить связь, когда сеть ожила.
    """
    server, proxy, link = stand
    proxy.freeze()
    assert await _wait_for(lambda: not link.alive), "heartbeat не заметил half-open"
    proxy.unfreeze()
    assert await _wait_for(lambda: link.alive), "линк не воскрес после разморозки"


@pytest.mark.asyncio
async def test_внезапная_смерть_без_FIN_и_возвращение(stand):
    """«Машину выдернули из розетки»: ни FIN, ни RST, порт пропал.

    Линк обязан упасть сам, молотить переподключения без падений задачи и
    восстановиться, когда машина вернулась на тот же адрес.
    """
    server, proxy, link = stand
    await proxy.drop_silently()
    assert await _wait_for(lambda: not link.alive), "смерть соседа не замечена"
    # Несколько циклов «connection refused» — задача линка обязана выжить.
    await asyncio.sleep(0.3)
    await proxy.restore()
    assert await _wait_for(lambda: link.alive), "линк не восстановился после возвращения"


@pytest.mark.asyncio
async def test_запрос_в_замороженную_сеть_не_виснет(stand):
    """forward в half-open обязан завершиться быстрым честным отказом
    (unavailable) по таймауту конверта, а не висеть вечно."""
    server, proxy, link = stand
    proxy.freeze()
    env = make_request(MSG_GET_STATE, timeout_s=0.2)
    with pytest.raises((ProtoError, TimeoutError)):
        await asyncio.wait_for(link.forward(env), timeout=2.0)
    proxy.unfreeze()
    assert await _wait_for(lambda: link.alive), "линк не восстановился после отказа"


@pytest.mark.asyncio
async def test_медленная_сеть_не_ломает_обмен(stand):
    """Задержка пересылки меньше таймаутов — обмен работает, линк живёт."""
    server, proxy, link = stand
    proxy.delay_s = 0.02
    client = ProtoClient(proxy.endpoint, token="s3cret")
    await client.connect()
    try:
        info = await client.hello()
        assert info.node == "winpc"
    finally:
        await client.close()
    assert link.alive
