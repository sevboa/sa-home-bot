import asyncio

import pytest

from sa_home_bot.node import peers as peers_mod
from sa_home_bot.node.peers import PeerLink
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import TcpEndpoint
from sa_home_bot.proto.messages import (
    MSG_GET_STATE,
    ProtoError,
    make_request,
)
from sa_home_bot.proto.server import ProtoServer
from tests.netlab import NetLabProxy
from tests.unit.netsupport import StubService
from tests.unit.support.waiting import wait_for


@pytest.fixture
async def stand(monkeypatch):
    from sa_home_bot.proto import client as client_mod

    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_MISSES", 2)
    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT_S", 0.05)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), StubService("winpc"), token="s3cret")
    await server.start()
    proxy = NetLabProxy("127.0.0.1", server.endpoint.port)
    await proxy.start()
    link = PeerLink(
        "winpc", proxy.endpoint, token="s3cret", self_node="alfred", reconnect_delay=0.05,
        heartbeat_timeout=0.05,
    )
    await link.start()
    assert await wait_for(lambda: link.alive), "линк через прокси не поднялся"
    yield server, proxy, link
    await link.stop()
    await proxy.stop()
    await server.stop()


async def test_прокси_прозрачен_для_протокола(stand):
    server, proxy, link = stand
    env = make_request(MSG_GET_STATE)
    response = await link.forward(env)
    assert response.ok is True
    assert response.payload == {"node": "winpc"}


async def test_half_open_обнаруживается_и_линк_воскресает(stand):
    server, proxy, link = stand
    proxy.freeze()
    assert await wait_for(lambda: not link.alive), "heartbeat не заметил half-open"
    proxy.unfreeze()
    assert await wait_for(lambda: link.alive), "линк не воскрес после разморозки"


async def test_внезапная_смерть_без_FIN_и_возвращение(stand):
    server, proxy, link = stand
    await proxy.drop_silently()
    assert await wait_for(lambda: not link.alive), "смерть соседа не замечена"
    await asyncio.sleep(0.3)
    await proxy.restore()
    assert await wait_for(lambda: link.alive), "линк не восстановился после возвращения"


async def test_запрос_в_замороженную_сеть_не_виснет(stand):
    server, proxy, link = stand
    proxy.freeze()
    env = make_request(MSG_GET_STATE, timeout_s=0.2)
    with pytest.raises((ProtoError, TimeoutError)):
        await asyncio.wait_for(link.forward(env), timeout=2.0)
    proxy.unfreeze()
    assert await wait_for(lambda: link.alive), "линк не восстановился после отказа"


async def test_медленная_сеть_не_ломает_обмен(stand):
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
