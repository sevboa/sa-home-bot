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
    """Соединение живо, а ответов нет — линк обязан стать недоступным сам.

    Это тот случай, который не ловит ни keepalive, ни TCP_USER_TIMEOUT:
    на уровне сокета всё в порядке.
    """
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

        # hello обслуживается из describe() — вешаем именно его: соединение
        # при этом остаётся полностью исправным, отвечать перестаёт служба.
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
    """Принудительный сброс линка не убивает его насовсем — он переподключается."""
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
    """Сосед пришёл с ДРУГОЙ инкарнацией (перезапустился) → сервер зовёт
    колбэк и закрывает его прошлое соединение (иначе они копятся: на проде
    их было четыре, по одному на каждый ребут winpc)."""
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
        # Первое соединение: сравнивать не с чем, дёргать нечего.
        assert connected == []
        assert server.connection_count == 1

        # Тот же узел, но процесс перезапустился — новая инкарнация.
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
    """Инкарнация та же — сосед просто переподключился.

    Живой баг 2026-07-28: реакция на КАЖДЫЙ auth породила взаимный цикл —
    alfred рвал линк к winpc, переподключался, чем заставлял winpc порвать
    свой линк к alfred, и так каждые 10 с по кругу.
    """
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
    """Локальные службы и старые ноды не представляются — auth обязан
    работать как раньше, колбэк просто не зовётся."""
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
    """EADDRINUSE — чужой процесс на порту, ожидание не поможет: падаем сразу,
    а не висим три минуты."""
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
    """`close()` обязан вернуться, даже если сокет не подтверждает закрытие.

    Живой баг 2026-07-28 (прод): `wait_closed()` без таймаута завис на
    полумёртвом TCP (данные в Send-Q не уходят, FIN не подтверждается) и
    подвесил весь цикл переподключения PeerLink — в логе «связь потеряна»
    и тишина три минуты, линк не восстановился сам.
    """
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
        # Уложиться обязаны в CLOSE_TIMEOUT_S, а не ждать вечно.
        await asyncio.wait_for(client.close(), timeout=2.0)
    finally:
        await server.stop()


async def test_одновременный_разрыв_и_heartbeat_не_убивают_линк(sock_dir, monkeypatch):
    """Гонка reconnect_now + heartbeat не должна вешать цикл переподключения.

    Живой баг 2026-07-28 (прод): forward() позвал reconnect_now, в ту же
    секунду heartbeat досчитал промахи — оба закрывали клиента параллельно,
    `wait_closed()` на полумёртвом сокете висел без таймаута, и линк не
    переподключался вообще («связь потеряна» и тишина три минуты).
    """
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
        # Оба пути разрыва одновременно, несколько раз подряд.
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
    """Неожиданное исключение не должно убивать задачу линка молча."""
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
    """Живая находка 2026-09-25: SearXNG думал дольше таймаута web_search —
    forward() ронял всё соединение, и следующий раунд chat по тому же линку
    получал «недоступно». Таймаут одного запроса — это медленная служба, а не
    мёртвый линк: запрос получает ошибку timeout, линк остаётся жив."""
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
    """Первый адрес мёртв (машина сменила сеть) — связь всё равно есть."""
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
        # Удачный адрес встал первым — следующий перебор начнётся с него.
        assert link.endpoints[0] == str(sock_dir / "peer.sock")
    finally:
        await link.stop()
        await server.stop()


async def test_адрес_с_чужой_нодой_уступает_правильному(sock_dir):
    """Сосед мог назвать адрес, который в НАШЕЙ сети занят кем-то другим:
    связь по нему — это запросы не туда. Пока есть другие адреса, берём их."""
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
    """Имя в [[swarm.nodes]] человек пишет как ему удобно и оно законно может
    не совпасть с hostname соседа: отказываться от единственного адреса
    из-за этого нельзя — так вело себя и до этапа 24."""
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
    """Живая находка 2026-08-04: DHCP без резервации отдал IP, когда-то
    выученный как адрес соседа, нам самим. Ответ «это я» — не «сосед иначе
    называется» (RANK-фолбэк из теста выше), а гарантированно мёртвый
    кандидат: пока есть настоящий адрес соседа, брать его."""
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
    """В отличие от «просто другое имя в конфиге» (тест выше), ответ
    буквально нашим собственным node_id никогда не годится даже как
    последняя надежда — иначе линк подключается сам к себе и врёт
    PresenceWatcher, что сосед «вернулся в строй»."""
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
    """Живая находка 2026-08-04: раньше выученное только прирастало «в
    надежде, что адрес вернётся к старому» — стухший кандидат простаивал
    в списке навсегда. Свежий hello ЗАМЕНЯЕТ список, а не дополняет."""
    stale = f"unix://{sock_dir / 'stale.sock'}"  # никто не слушает
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
    """Найдено 2026-08-04: «запасной» ответ от РЕАЛЬНОГО, но другого
    соседа (устаревший адрес переехал на него, не на нас — тот случай
    гонит тест выше) раньше приписывал нашему линку чужие kind/endpoints.
    Мисматч годится только для связи, не для памяти о соседе."""
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
    """Сосед знает свои адреса точнее, чем чужой конфиг, — рассказывает их
    в hello, а нода сохраняет (иначе выученное терялось бы на рестарте)."""
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
        # Сначала — путь, по которому связь уже есть, затем LAN, затем оверлей.
        assert link.endpoints[1:] == ["tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"]
        assert learned and learned[-1][0] == "winpc"
    finally:
        await link.stop()
        await server.stop()


async def test_линк_не_учит_loopback_соседа(sock_dir):
    """По 127.0.0.1 мы попадём не к соседу, а к себе — такой адрес не берём
    ни при каких обстоятельствах."""
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
