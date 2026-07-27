"""Живучесть линков роя (живой баг 2026-07-28).

Прод-симптом: winpc перезагрузилась, alfred этого не заметил — пир висел
«зелёным», а запросы к нему уходили в таймаут, пока ноду не перезапустили
руками. TCP keepalive не спасал: пробы шлются только на ПРОСТАИВАЮЩЕМ
соединении, а в Send-Q лежали неотправленные данные.

Здесь проверяются три слоя, которые чинят это независимо друг от друга:
heartbeat, немедленный реконнект и опознание вернувшегося соседа.
"""

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
    ActionSpec,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import ProtoServer


class FakePeerService:
    """Служба «соседа». Тесты подменяют её `describe`, чтобы сосед перестал
    отвечать, не рвя соединение (сокет жив, процесс завис — то, чего TCP не
    видит в принципе)."""

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="winpc", service="node", version="0.42.0"),
            capabilities=(),
            actions=(ActionSpec(id="noop", title="Ничего"),),
        )

    async def get_state(self) -> dict:
        return {"node": "winpc"}

    async def run_command(self, action: str, args: dict) -> dict:
        return {"accepted": True}


@pytest.fixture
def sock_dir():
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """Дождаться условия, не привязываясь к точным таймингам планировщика."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_heartbeat_рвёт_линк_когда_сосед_перестал_отвечать(sock_dir, monkeypatch):
    """Соединение живо, а ответов нет — линк обязан стать недоступным сам.

    Это тот случай, который не ловит ни keepalive, ни TCP_USER_TIMEOUT:
    на уровне сокета всё в порядке.
    """
    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_MISSES", 2)

    service = FakePeerService()
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=60.0)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive), "линк не поднялся"

        # hello обслуживается из describe() — вешаем именно его: соединение
        # при этом остаётся полностью исправным, отвечать перестаёт служба.
        def hanging_describe() -> ServiceDescription:
            raise TimeoutError("сосед не отвечает")

        service.describe = hanging_describe  # type: ignore[method-assign]

        assert await _wait_for(lambda: not link.alive), (
            "heartbeat не заметил, что сосед перестал отвечать"
        )
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_reconnect_now_поднимает_связь_заново(sock_dir):
    """Принудительный сброс линка не убивает его насовсем — он переподключается."""
    service = FakePeerService()
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        link.reconnect_now("тест")
        assert await _wait_for(lambda: not link.alive), "линк не сбросился"
        assert await _wait_for(lambda: link.alive), "линк не переподключился"
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_перезапустившийся_сосед_роняет_старое_соединение(monkeypatch):
    """Сосед пришёл с ДРУГОЙ инкарнацией (перезапустился) → сервер зовёт
    колбэк и закрывает его прошлое соединение (иначе они копятся: на проде
    их было четыре, по одному на каждый ребут winpc)."""
    connected: list[str] = []
    service = FakePeerService()
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
        assert await _wait_for(lambda: first.alive)
        # Первое соединение: сравнивать не с чем, дёргать нечего.
        assert connected == []
        assert server.connection_count == 1

        # Тот же узел, но процесс перезапустился — новая инкарнация.
        monkeypatch.setattr(peers_mod, "NODE_INCARNATION", "второй-запуск")
        second = PeerLink(
            "winpc", f"tcp://{addr.host}:{addr.port}", token="s3cret", self_node="alfred"
        )
        await second.start()
        assert await _wait_for(lambda: connected == ["alfred"])
        assert await _wait_for(lambda: server.connection_count == 1), (
            f"старое соединение не закрыто: {server.connection_count}"
        )
    finally:
        for link in (first, second):
            if link is not None:
                await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_переподключение_без_перезапуска_не_дёргает_колбэк():
    """Инкарнация та же — сосед просто переподключился.

    Живой баг 2026-07-28: реакция на КАЖДЫЙ auth породила взаимный цикл —
    alfred рвал линк к winpc, переподключался, чем заставлял winpc порвать
    свой линк к alfred, и так каждые 10 с по кругу.
    """
    connected: list[str] = []
    service = FakePeerService()
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
        assert await _wait_for(lambda: link.alive)
        for _ in range(3):
            link.reconnect_now("тест")
            assert await _wait_for(lambda: link.alive)
        assert connected == [], f"колбэк сработал на переподключении: {connected}"
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_клиент_без_self_node_не_ломает_auth():
    """Локальные службы и старые ноды не представляются — auth обязан
    работать как раньше, колбэк просто не зовётся."""
    connected: list[str] = []
    service = FakePeerService()
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


@pytest.mark.asyncio
async def test_сервер_ждёт_появления_своего_адреса(monkeypatch):
    """Адреса ещё нет (Tailscale не поднялся) — нода обязана дождаться его,
    а не упасть.

    Живой баг 2026-07-28: winpc не появлялась в рое после включения —
    `could not bind on any address out of [('100.78.225.83', 8710)]`.
    """
    from sa_home_bot.proto import server as server_mod

    monkeypatch.setattr(server_mod, "BIND_RETRY_INTERVAL_S", 0.01)
    real_start_server = asyncio.start_server
    attempts = {"n": 0}

    async def flaky_start_server(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:  # первые попытки — адреса ещё нет
            raise OSError("could not bind on any address out of [('100.99.99.99', 8710)]")
        return await real_start_server(*args, **kwargs)

    monkeypatch.setattr(server_mod.asyncio, "start_server", flaky_start_server)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), FakePeerService(), token="s3cret")
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


@pytest.mark.asyncio
async def test_занятый_порт_не_ждём(monkeypatch):
    """EADDRINUSE — чужой процесс на порту, ожидание не поможет: падаем сразу,
    а не висим три минуты."""
    import errno as errno_mod

    from sa_home_bot.proto import server as server_mod

    monkeypatch.setattr(server_mod, "BIND_RETRY_INTERVAL_S", 0.01)

    async def busy_start_server(*args, **kwargs):
        raise OSError(errno_mod.EADDRINUSE, "address already in use")

    monkeypatch.setattr(server_mod.asyncio, "start_server", busy_start_server)

    server = ProtoServer(TcpEndpoint("127.0.0.1", 9999), FakePeerService(), token="s3cret")
    with pytest.raises(OSError) as exc:
        await server.start()
    assert exc.value.errno == errno_mod.EADDRINUSE


@pytest.mark.asyncio
async def test_close_не_виснет_на_полумёртвом_сокете(sock_dir, monkeypatch):
    """`close()` обязан вернуться, даже если сокет не подтверждает закрытие.

    Живой баг 2026-07-28 (прод): `wait_closed()` без таймаута завис на
    полумёртвом TCP (данные в Send-Q не уходят, FIN не подтверждается) и
    подвесил весь цикл переподключения PeerLink — в логе «связь потеряна»
    и тишина три минуты, линк не восстановился сам.
    """
    from sa_home_bot.proto import client as client_mod

    monkeypatch.setattr(client_mod, "CLOSE_TIMEOUT_S", 0.05)

    service = FakePeerService()
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()
    client = ProtoClient(endpoint)
    await client.connect()

    async def never_closes():
        await asyncio.sleep(3600)

    monkeypatch.setattr(client._writer, "wait_closed", never_closes)
    try:
        # Уложиться обязаны в CLOSE_TIMEOUT_S, а не ждать вечно.
        await asyncio.wait_for(client.close(), timeout=2.0)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_одновременный_разрыв_и_heartbeat_не_убивают_линк(sock_dir, monkeypatch):
    """Гонка reconnect_now + heartbeat не должна вешать цикл переподключения.

    Живой баг 2026-07-28 (прод): forward() позвал reconnect_now, в ту же
    секунду heartbeat досчитал промахи — оба закрывали клиента параллельно,
    `wait_closed()` на полумёртвом сокете висел без таймаута, и линк не
    переподключался вообще («связь потеряна» и тишина три минуты).
    """
    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.02)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_TIMEOUT_S", 0.02)
    monkeypatch.setattr(peers_mod, "HEARTBEAT_MISSES", 1)

    service = FakePeerService()
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, service)
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        # Оба пути разрыва одновременно, несколько раз подряд.
        for _ in range(5):
            link.reconnect_now("гонка")
            link.reconnect_now("гонка ещё раз")
            await asyncio.sleep(0.03)
        assert await _wait_for(lambda: link.alive, timeout=10.0), (
            "линк не переподключился после гонки разрывов"
        )
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_цикл_переподключения_переживает_любую_ошибку(sock_dir, monkeypatch, caplog):
    """Неожиданное исключение не должно убивать задачу линка молча."""
    service = FakePeerService()
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
        assert await _wait_for(lambda: link.alive, timeout=10.0), (
            "линк умер на непредвиденной ошибке вместо повторной попытки"
        )
    finally:
        await link.stop()
        await server.stop()
