"""Живучесть серверной стороны + предохранитель hops (аудит 2026-07-28).

До v0.43 принятые сокеты не получали keepalive, рассылка шла последовательно
и без таймаута (один полумёртвый клиент вешал emit(), а с ним супервизор и
аренду), молчащее соединение без auth жило вечно, а защита от шторма
ретрансляции держалась на единственном дедуп-наборе.
"""

import asyncio
import socket

from sa_home_bot.node.app import MAX_EVENT_HOPS, SeenEvents, _relay_peer_event
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.state import NodeState
from sa_home_bot.proto import server as server_mod
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import TcpEndpoint
from sa_home_bot.proto.messages import (
    ActionSpec,
    Address,
    ServiceDescription,
    ServiceInfo,
    decode,
    encode,
    make_event,
)
from sa_home_bot.proto.server import ProtoServer


class FakeService:
    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="node", version="0.43.0"),
            capabilities=(),
            actions=(ActionSpec(id="noop", title="Ничего"),),
        )

    async def get_state(self) -> dict:
        return {}

    async def run_command(self, action: str, args: dict) -> dict:
        return {}


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# --- hops: второй предохранитель от шторма ---


def test_hops_переживает_кодирование():
    env = make_event("node_down", {"node": "winpc"}, src=Address(node="alfred"))
    assert env.hops == 0
    bumped = decode(encode(env))
    assert bumped.hops == 0  # поле не пишется, пока ноль — совместимость

    import dataclasses

    with_hops = dataclasses.replace(env, hops=3)
    assert decode(encode(with_hops)).hops == 3


def test_hops_отсутствие_в_json_равно_нулю():
    env = make_event("x", {})
    raw = encode(env)
    assert b"hops" not in raw  # старая нода увидит привычный конверт


async def test_ретрансляция_инкрементит_hops():
    broadcasts = []

    class _FakeServer:
        async def broadcast_envelope(self, env):
            broadcasts.append(env)

    env = make_event("update_finished", {}, src=Address(node="A", service="node"))
    await _relay_peer_event(
        env,
        node_id="B",
        router=NodeRouter("B"),
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=_FakeServer(),
        seen=SeenEvents(),
    )
    assert [e.hops for e in broadcasts] == [1]
    assert broadcasts[0].id == env.id  # id не меняется — дедуп работает как раньше


async def test_событие_с_превышением_hops_дропается():
    import dataclasses

    class _FakeServer:
        async def broadcast_envelope(self, env):
            raise AssertionError("циркулирующее событие не должно ретранслироваться")

    env = dataclasses.replace(
        make_event("update_finished", {}, src=Address(node="A", service="node")),
        hops=MAX_EVENT_HOPS,
    )
    await _relay_peer_event(
        env,
        node_id="B",
        router=NodeRouter("B"),
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=_FakeServer(),
        seen=SeenEvents(),
    )


# --- broadcast: полумёртвый клиент не тормозит остальных ---


async def test_broadcast_не_виснет_на_полумёртвом_клиенте(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "SEND_TIMEOUT_S", 0.1)

    endpoint = f"unix://{tmp_path / 'node.sock'}"
    server = ProtoServer(endpoint, FakeService())
    await server.start()

    healthy = ProtoClient(endpoint)
    stuck = ProtoClient(endpoint)
    await healthy.connect()
    await stuck.connect()
    try:
        assert await _wait_for(lambda: server.connection_count == 2)
        # Одно из соединений «залипает»: его send больше никогда не завершается.
        victim = next(iter(server._connections))

        async def hanging_send(env):
            await asyncio.sleep(3600)

        victim.send = hanging_send  # type: ignore[method-assign]

        delivered = await asyncio.wait_for(server.broadcast_event("ping", {}), timeout=2.0)
        # Здоровый получатель обслужен, залипший — выброшен из соединений.
        assert delivered == 1
        assert victim not in server._connections
    finally:
        await healthy.close()
        await stuck.close()
        await server.stop()


# --- TCP: auth-дедлайн и keepalive на принятой стороне ---


async def test_молчащее_соединение_закрывается_по_auth_таймауту(monkeypatch):
    monkeypatch.setattr(server_mod, "AUTH_TIMEOUT_S", 0.1)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), FakeService(), token="s3cret")
    await server.start()
    addr = server.endpoint
    try:
        reader, writer = await asyncio.open_connection(addr.host, addr.port)
        # Ничего не шлём. Сервер обязан закрыть соединение сам.
        eof = await asyncio.wait_for(reader.read(1), timeout=2.0)
        assert eof == b""
        writer.close()
    finally:
        await server.stop()


async def test_принятый_сокет_получает_keepalive():
    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), FakeService(), token="s3cret")
    await server.start()
    addr = server.endpoint
    client = ProtoClient(f"tcp://{addr.host}:{addr.port}", token="s3cret")
    try:
        await client.connect()
        assert await _wait_for(lambda: server.connection_count == 1)
        conn = next(iter(server._connections))
        raw = conn.writer.get_extra_info("socket")
        assert raw.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
    finally:
        await client.close()
        await server.stop()
