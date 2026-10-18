import asyncio
import errno
import socket

import pytest

from sa_home_bot.node.app import MAX_RELAY_HOPS, SeenEvents, _relay_peer_event
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.state import NodeState
from sa_home_bot.proto import server as server_mod
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import TcpEndpoint
from sa_home_bot.proto.messages import (
    Address,
    decode,
    encode,
    make_event,
)
from sa_home_bot.proto.server import ProtoServer
from tests.unit.netsupport import StubService
from tests.unit.support.waiting import wait_for

pytest_plugins = ["tests.unit.sock_fixture"]


def test_hops_переживает_кодирование():
    env = make_event("node_down", {"node": "winpc"}, src=Address(node="alfred"))
    assert env.hops == 0
    bumped = decode(encode(env))
    assert bumped.hops == 0

    import dataclasses

    with_hops = dataclasses.replace(env, hops=3)
    assert decode(encode(with_hops)).hops == 3


def test_hops_отсутствие_в_json_равно_нулю():
    env = make_event("x", {})
    raw = encode(env)
    assert b"hops" not in raw


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
    assert broadcasts[0].id == env.id


async def test_событие_с_превышением_hops_дропается():
    import dataclasses

    class _FakeServer:
        async def broadcast_envelope(self, env):
            raise AssertionError("циркулирующее событие не должно ретранслироваться")

    env = dataclasses.replace(
        make_event("update_finished", {}, src=Address(node="A", service="node")),
        hops=MAX_RELAY_HOPS,
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


async def test_broadcast_не_виснет_на_полумёртвом_клиенте(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "SEND_TIMEOUT_S", 0.1)

    endpoint = f"unix://{tmp_path / 'node.sock'}"
    server = ProtoServer(endpoint, StubService("alfred"))
    await server.start()

    healthy = ProtoClient(endpoint)
    stuck = ProtoClient(endpoint)
    await healthy.connect()
    await stuck.connect()
    try:
        assert await wait_for(lambda: server.connection_count == 2)
        victim = next(iter(server._connections))

        async def hanging_send(env):
            await asyncio.sleep(3600)

        victim.send = hanging_send  # type: ignore[method-assign]

        delivered = await asyncio.wait_for(server.broadcast_event("ping", {}), timeout=2.0)
        assert delivered == 1
        assert victim not in server._connections
    finally:
        await healthy.close()
        await stuck.close()
        await server.stop()


async def test_молчащее_соединение_закрывается_по_auth_таймауту(monkeypatch):
    monkeypatch.setattr(server_mod, "AUTH_TIMEOUT_S", 0.1)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), StubService("alfred"), token="s3cret")
    await server.start()
    addr = server.endpoint
    try:
        reader, writer = await asyncio.open_connection(addr.host, addr.port)
        eof = await asyncio.wait_for(reader.read(1), timeout=2.0)
        assert eof == b""
        writer.close()
    finally:
        await server.stop()


async def test_принятый_сокет_получает_keepalive():
    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), StubService("alfred"), token="s3cret")
    await server.start()
    addr = server.endpoint
    client = ProtoClient(f"tcp://{addr.host}:{addr.port}", token="s3cret")
    try:
        await client.connect()
        assert await wait_for(lambda: server.connection_count == 1)
        conn = next(iter(server._connections))
        raw = conn.writer.get_extra_info("socket")
        assert raw.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
    finally:
        await client.close()
        await server.stop()


async def test_нода_поднимается_на_доступном_адресе_не_дожидаясь_отстающего(
    sock_dir, monkeypatch
):
    monkeypatch.setattr(server_mod, "BIND_RETRY_INTERVAL_S", 0.01)
    real_start_server = asyncio.start_server
    attempts = {"n": 0}

    async def flaky_start_server(*args, **kwargs):
        if kwargs.get("host") == "127.0.0.2":
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("could not bind on any address")
        return await real_start_server(*args, **kwargs)

    monkeypatch.setattr(server_mod.asyncio, "start_server", flaky_start_server)

    server = ProtoServer(
        [f"unix://{sock_dir / 'node.sock'}", TcpEndpoint("127.0.0.2", 0)],
        StubService(),
        token="s3cret",
    )
    await server.start()
    try:
        assert len(server.bound_endpoints) == 1
        assert await wait_for(lambda: len(server.bound_endpoints) == 2), (
            "отстающий адрес так и не добрали фоном"
        )
        assert server.advertised_endpoints() == []
    finally:
        await server.stop()


async def test_занятый_порт_среди_нескольких_адресов_не_маскируется(sock_dir, monkeypatch):
    async def busy_start_server(*args, **kwargs):
        raise OSError(errno.EADDRINUSE, "address already in use")

    monkeypatch.setattr(server_mod.asyncio, "start_server", busy_start_server)
    server = ProtoServer(
        [f"unix://{sock_dir / 'node.sock'}", TcpEndpoint("127.0.0.1", 9999)],
        StubService(),
        token="s3cret",
    )
    with pytest.raises(OSError) as exc:
        await server.start()
    assert exc.value.errno == errno.EADDRINUSE
    await server.stop()
