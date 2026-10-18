import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.node import peers as peers_mod
from sa_home_bot.node.peers import PeerLink
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import TcpEndpoint
from sa_home_bot.proto.messages import (
    ERR_TIMEOUT,
    MSG_COMMAND,
    ProtoError,
    ServiceDescription,
    make_request,
)
from sa_home_bot.proto.server import ProtoServer
from tests.unit.netsupport import StubService
from tests.unit.support.waiting import wait_for

pytest_plugins = ["tests.unit.sock_fixture"]


def sock_dir():
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def test_heartbeat_рвёт_линк_когда_сосед_перестал_отвечать(sock_dir, monkeypatch):
    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_MISSES", 2)

    service = StubService("winpc")
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=60.0, heartbeat_timeout=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive), "линк не поднялся"

        def hanging_describe() -> ServiceDescription:
            raise TimeoutError("сосед не отвечает")

        service.describe = hanging_describe  # type: ignore[method-assign]

        assert await wait_for(lambda: not link.alive), (
            "heartbeat не заметил, что сосед перестал отвечать"
        )
    finally:
        await link.stop()
        await server.stop()


async def test_reconnect_now_поднимает_связь_заново(sock_dir):
    service = StubService("winpc")
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        link.reconnect_now("тест")
        assert await wait_for(lambda: not link.alive), "линк не сбросился"
        assert await wait_for(lambda: link.alive), "линк не переподключился"
    finally:
        await link.stop()
        await server.stop()


async def test_перезапустившийся_сосед_роняет_старое_соединение(monkeypatch):
    connected: list[str] = []
    service = StubService("winpc")
    server = ProtoServer(
        TcpEndpoint("127.0.0.1", 0), service, token="s3cret", on_peer_connect=connected.append
    )
    await server.start()
    addr = server.endpoint
    first = second = None
    try:
        first = PeerLink(
            "winpc", f"tcp://{addr.host}:{addr.port}", token="s3cret", self_node="alfred"
        )
        await first.start()
        assert await wait_for(lambda: first.alive)
        assert connected == []
        assert server.connection_count == 1

        monkeypatch.setattr(peers_mod, "NODE_INCARNATION", "второй-запуск")
        second = PeerLink(
            "winpc", f"tcp://{addr.host}:{addr.port}", token="s3cret", self_node="alfred"
        )
        await second.start()
        assert await wait_for(lambda: connected == ["alfred"])
        assert await wait_for(lambda: server.connection_count == 1), (
            f"старое соединение не закрыто: {server.connection_count}"
        )
    finally:
        for link in (first, second):
            if link is not None:
                await link.stop()
        await server.stop()


async def test_переподключение_без_перезапуска_не_дёргает_колбэк():
    connected: list[str] = []
    service = StubService("winpc")
    server = ProtoServer(
        TcpEndpoint("127.0.0.1", 0), service, token="s3cret", on_peer_connect=connected.append
    )
    await server.start()
    addr = server.endpoint
    link = PeerLink(
        "winpc",
        f"tcp://{addr.host}:{addr.port}",
        token="s3cret",
        self_node="alfred",
        reconnect_delay=0.05,
    )
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        for _ in range(3):
            link.reconnect_now("тест")
            assert await wait_for(lambda: link.alive)
        assert connected == [], f"колбэк сработал на переподключении: {connected}"
    finally:
        await link.stop()
        await server.stop()


async def test_клиент_без_self_node_не_ломает_auth():
    connected: list[str] = []
    service = StubService("winpc")
    server = ProtoServer(
        TcpEndpoint("127.0.0.1", 0), service, token="s3cret", on_peer_connect=connected.append
    )
    await server.start()
    addr = server.endpoint
    client = ProtoClient(f"tcp://{addr.host}:{addr.port}", token="s3cret")
    try:
        await client.connect()
        info = await client.hello()
        assert info.node == "winpc"
        assert connected == []
    finally:
        await client.close()
        await server.stop()


async def test_сервер_ждёт_появления_своего_адреса(monkeypatch):
    from sa_home_bot.proto import server as server_mod

    monkeypatch.setattr(server_mod, "BIND_RETRY_INTERVAL_S", 0.01)
    real_start_server = asyncio.start_server
    attempts = {"n": 0}

    async def flaky_start_server(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("could not bind on any address out of [('100.99.99.99', 8710)]")
        return await real_start_server(*args, **kwargs)

    monkeypatch.setattr(server_mod.asyncio, "start_server", flaky_start_server)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), StubService("winpc"), token="s3cret")
    await server.start()
    try:
        assert attempts["n"] == 3, "сервер не дождался появления адреса"
        addr = server.endpoint
        client = ProtoClient(f"tcp://{addr.host}:{addr.port}", token="s3cret")
        await client.connect()
        assert (await client.hello()).node == "winpc"
        await client.close()
    finally:
        await server.stop()


async def test_занятый_порт_не_ждём(monkeypatch):
    import errno as errno_mod

    from sa_home_bot.proto import server as server_mod

    monkeypatch.setattr(server_mod, "BIND_RETRY_INTERVAL_S", 0.01)

    async def busy_start_server(*args, **kwargs):
        raise OSError(errno_mod.EADDRINUSE, "address already in use")

    monkeypatch.setattr(server_mod.asyncio, "start_server", busy_start_server)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 9999), StubService("winpc"), token="s3cret")
    with pytest.raises(OSError) as exc:
        await server.start()
    assert exc.value.errno == errno_mod.EADDRINUSE


async def test_close_не_виснет_на_полумёртвом_сокете(sock_dir, monkeypatch):
    from sa_home_bot.proto import client as client_mod

    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT_S", 0.05)

    service = StubService("winpc")
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()
    client = ProtoClient(endpoint)
    await client.connect()

    async def never_closes():
        await asyncio.sleep(3600)

    monkeypatch.setattr(client._writer, "wait_closed", never_closes)
    try:
        await asyncio.wait_for(client.close(), timeout=2.0)
    finally:
        await server.stop()


async def test_одновременный_разрыв_и_heartbeat_не_убивают_линк(sock_dir, monkeypatch):
    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.02)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_MISSES", 1)

    service = StubService("winpc")
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05, heartbeat_timeout=0.02)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        for _ in range(5):
            link.reconnect_now("гонка")
            link.reconnect_now("гонка ещё раз")
            await asyncio.sleep(0.03)
        assert await wait_for(lambda: link.alive, timeout=10.0), (
            "линк не переподключился после гонки разрывов"
        )
    finally:
        await link.stop()
        await server.stop()


async def test_цикл_переподключения_переживает_любую_ошибку(sock_dir, monkeypatch, caplog):
    service = StubService("winpc")
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    calls = {"n": 0}
    real_hello = ProtoClient.hello

    async def exploding_hello(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("что-то совсем неожиданное")
        return await real_hello(self, *args, **kwargs)

    monkeypatch.setattr(ProtoClient, "hello", exploding_hello)

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive, timeout=10.0), (
            "линк умер на непредвиденной ошибке вместо повторной попытки"
        )
    finally:
        await link.stop()
        await server.stop()


async def test_медленный_запрос_не_рвёт_линк(sock_dir):
    service = StubService("winpc")
    release = asyncio.Event()

    async def slow_command(action: str, args: dict) -> dict:
        await release.wait()
        return {"accepted": True}

    service.run_command = slow_command  # type: ignore[method-assign]
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=60.0)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        env = make_request(
            MSG_COMMAND, {"action": "noop", "args": {}}, src=None, dst=None, timeout_s=0.1
        )
        with pytest.raises(ProtoError) as info:
            await link.forward(env)
        assert info.value.code == ERR_TIMEOUT
        assert link.alive, "таймаут одного запроса уронил линк"
    finally:
        release.set()
        await link.stop()
        await server.stop()


async def test_линк_перебирает_адреса_до_рабочего(sock_dir):
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, StubService())
    await server.start()

    link = PeerLink(
        "winpc",
        [f"unix://{sock_dir / 'nowhere.sock'}", endpoint],
        reconnect_delay=0.05,
    )
    await link.start()
    try:
        assert await wait_for(lambda: link.alive), "линк не нашёл живой адрес"
        assert str(link.endpoint) == str(sock_dir / "peer.sock")
        assert link.endpoints[0] == str(sock_dir / "peer.sock")
    finally:
        await link.stop()
        await server.stop()


async def test_адрес_с_чужой_нодой_уступает_правильному(sock_dir):
    wrong = f"unix://{sock_dir / 'wrong.sock'}"
    right = f"unix://{sock_dir / 'right.sock'}"
    other = ProtoServer(wrong, StubService(node="alfred"))
    ours = ProtoServer(right, StubService(node="winpc"))
    await other.start()
    await ours.start()

    link = PeerLink("winpc", [wrong, right], reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        assert str(link.endpoint) == str(sock_dir / "right.sock"), (
            "линк остался на адресе, где отвечает чужая нода"
        )
    finally:
        await link.stop()
        await ours.stop()
        await other.stop()


async def test_единственный_адрес_с_чужим_именем_всё_же_работает(sock_dir, caplog):
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, StubService(node="desktop-ie953ua"))
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive), "линк отказался от единственного адреса"
    finally:
        await link.stop()
        await server.stop()


async def test_адрес_отвечающий_нами_самими_уступает_правильному(sock_dir):
    stale = f"unix://{sock_dir / 'stale.sock'}"
    right = f"unix://{sock_dir / 'right.sock'}"
    ourselves = ProtoServer(stale, StubService(node="alfred"))
    neighbour = ProtoServer(right, StubService(node="mycraft"))
    await ourselves.start()
    await neighbour.start()

    link = PeerLink("mycraft", [stale, right], reconnect_delay=0.05, self_node="alfred")
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        assert str(link.endpoint) == str(sock_dir / "right.sock"), (
            "линк остался на адресе, где отвечаем мы сами"
        )
    finally:
        await link.stop()
        await neighbour.stop()
        await ourselves.stop()


async def test_единственный_адрес_отвечающий_нами_самими_не_считается_живым(sock_dir):
    stale = f"unix://{sock_dir / 'stale.sock'}"
    ourselves = ProtoServer(stale, StubService(node="alfred"))
    await ourselves.start()

    link = PeerLink("mycraft", stale, reconnect_delay=0.05, self_node="alfred")
    await link.start()
    try:
        await asyncio.sleep(0.2)
        assert not link.alive, "линк принял ответ от самого себя за соседа"
    finally:
        await link.stop()
        await ourselves.stop()


async def test_устаревший_адрес_не_копится_после_свежего_hello(sock_dir):
    stale = f"unix://{sock_dir / 'stale.sock'}"
    right = f"unix://{sock_dir / 'right.sock'}"
    service = StubService(node="mycraft", endpoints=("tcp://192.168.0.103:8710",))
    server = ProtoServer(right, service)
    await server.start()

    link = PeerLink("mycraft", [stale, right], reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        assert await wait_for(lambda: stale not in link.endpoints), link.endpoints
        assert "tcp://192.168.0.103:8710" in link.endpoints
    finally:
        await link.stop()
        await server.stop()


async def test_мисматч_чужого_соседа_не_портит_kind_и_адреса(sock_dir):
    wrong = f"unix://{sock_dir / 'wrong.sock'}"
    other = ProtoServer(
        wrong,
        StubService(
            node="winpc", endpoints=("tcp://192.168.0.104:8710",), node_kind="workstation"
        ),
    )
    await other.start()

    link = PeerLink("mycraft", wrong, reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        await asyncio.sleep(0.1)
        assert link.node_kind == ""
        assert "tcp://192.168.0.104:8710" not in link.endpoints
    finally:
        await link.stop()
        await other.stop()


async def test_линк_учит_адреса_из_hello_и_сообщает_их_наверх(sock_dir):
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    service = StubService(endpoints=("tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"))
    server = ProtoServer(endpoint, service)
    await server.start()

    learned: list[tuple[str, list[str]]] = []
    link = PeerLink(
        "winpc",
        endpoint,
        on_endpoints=lambda n, e: learned.append((n, e)),
        reconnect_delay=0.05,
    )
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        assert await wait_for(lambda: len(link.endpoints) == 3), link.endpoints
        assert link.endpoints[1:] == ["tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"]
        assert learned and learned[-1][0] == "winpc"
    finally:
        await link.stop()
        await server.stop()


async def test_линк_не_учит_loopback_соседа(sock_dir):
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, StubService(endpoints=("tcp://127.0.0.1:8710",)))
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        await asyncio.sleep(0.1)
        assert link.endpoints == [str(sock_dir / "peer.sock")]
    finally:
        await link.stop()
        await server.stop()
