"""Мультиадресность линков роя (IMPLEMENTATION_PLAN, этап 24).

Повод из эксплуатации: нода слушала и подключалась по ОДНОМУ жёстко
заданному tailscale-адресу. Отсюда две боли — нода не поднималась, пока
Tailscale не отдал адрес (40–60 с после загрузки), и без интернета домашний
рой не собирался вовсе: две машины в двух метрах друг от друга не видели
друг друга, хотя LAN исправна.

Здесь проверяется то, что чинит обе: нода слушает несколько адресов и не
ждёт отстающий; линк перебирает адреса соседа; адреса приезжают в hello и
переживают рестарт.
"""

import asyncio
import errno
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path

import pytest

from sa_home_bot.config import NodeConfig, SwarmNodeConfig
from sa_home_bot.node.app import _remember_peer, peer_candidates
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto import server as server_mod
from sa_home_bot.proto.endpoints import (
    RANK_LAN,
    RANK_OTHER,
    RANK_OVERLAY,
    TcpEndpoint,
    UnixEndpoint,
    advertisable,
    endpoint_rank,
    is_loopback,
    is_unspecified,
    is_virtual_iface,
    local_ipv4_addresses,
)
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo
from sa_home_bot.proto.server import ProtoServer


async def _emit(event_type, data):
    pass


class FakeService:
    """Служба соседа, называющая себя и свои адреса как велено тестом."""

    def __init__(
        self, node: str = "winpc", endpoints: tuple[str, ...] = (), node_kind: str = ""
    ) -> None:
        self.node = node
        self.endpoints = endpoints
        self.node_kind = node_kind

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(
                node=self.node,
                service="node",
                version="0.45.0",
                endpoints=self.endpoints,
                node_kind=self.node_kind,
            ),
            capabilities=(),
            actions=(ActionSpec(id="noop", title="Ничего"),),
        )

    async def get_state(self) -> dict:
        return {"node": self.node}

    async def run_command(self, action: str, args: dict) -> dict:
        return {"accepted": True}


@pytest.fixture
def sock_dir():
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path, ignore_errors=True)


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


# --- Классификация адресов ---


def test_локальная_сеть_пробуется_раньше_оверлея():
    """Ровно то, ради чего затевался этап: LAN — основной путь, tailscale —
    аварийный. 100.64.0.0/10 Python считает приватным, поэтому ранг у него
    свой, а не «как у 192.168»."""
    assert endpoint_rank(TcpEndpoint("192.168.0.100", 8710)) == RANK_LAN
    assert endpoint_rank(TcpEndpoint("100.73.52.92", 8710)) == RANK_OVERLAY
    assert endpoint_rank(TcpEndpoint("8.8.8.8", 8710)) == RANK_OTHER
    assert RANK_LAN < RANK_OVERLAY < RANK_OTHER


def test_loopback_и_0000_опознаются():
    assert is_loopback(TcpEndpoint("127.0.0.1", 8710))
    assert is_loopback(TcpEndpoint("localhost", 8710))
    assert is_loopback(UnixEndpoint(Path("/tmp/x.sock")))
    assert not is_loopback(TcpEndpoint("192.168.0.5", 8710))
    assert is_unspecified(TcpEndpoint("0.0.0.0", 8710))
    assert not is_unspecified(TcpEndpoint("192.168.0.5", 8710))


def test_объявляем_только_адреса_по_которым_к_нам_придут():
    """Unix-сокет и loopback соседу бесполезны: по ним он попадёт к себе."""
    advertised = advertisable(
        [
            UnixEndpoint(Path("/tmp/node.sock")),
            TcpEndpoint("127.0.0.1", 8710),
            TcpEndpoint("100.73.52.92", 8710),
            TcpEndpoint("192.168.0.100", 8710),
        ]
    )
    assert advertised == ["tcp://192.168.0.100:8710", "tcp://100.73.52.92:8710"]


def test_0000_раскрывается_в_адреса_интерфейсов(monkeypatch):
    """`listen = "tcp://0.0.0.0:8710"` — законная настройка, но объявить
    рою «ноль» нельзя: это не адрес, по которому к нам придут."""
    monkeypatch.setattr(
        "sa_home_bot.proto.endpoints.local_ipv4_addresses",
        lambda: ["192.168.0.100", "100.73.52.92"],
    )
    assert advertisable([TcpEndpoint("0.0.0.0", 8710)]) == [
        "tcp://192.168.0.100:8710",
        "tcp://100.73.52.92:8710",
    ]


def test_адреса_виртуальных_сетей_не_объявляются(monkeypatch):
    """Найдено при деплое этапа 24 на winpc: WSL и Hyper-V дают хосту адреса
    172.x, снаружи недостижимые НИКОГДА. Объявленные рою, они становятся
    мёртвыми кандидатами впереди настоящего LAN-адреса — сосед тратит на
    каждый полный CONNECT_TIMEOUT_S, и «LAN как основной путь» не работает."""
    class FakeAddr:
        def __init__(self, address: str) -> None:
            self.family = socket.AF_INET
            self.address = address

    fake_ifaces = {
        "vEthernet (WSL)": [FakeAddr("172.18.64.1")],
        "vEthernet (Default Switch)": [FakeAddr("172.26.48.1")],
        "docker0": [FakeAddr("172.17.0.1")],
        "Ethernet": [FakeAddr("192.168.0.105")],
        "Tailscale": [FakeAddr("100.78.225.83")],
    }
    # local_ipv4_addresses импортирует psutil внутри функции — подменяем модуль.
    monkeypatch.setitem(
        sys.modules, "psutil", types.SimpleNamespace(net_if_addrs=lambda: fake_ifaces)
    )

    assert local_ipv4_addresses() == ["192.168.0.105", "100.78.225.83"]


def test_настоящий_мост_и_оверлей_под_фильтр_не_попадают():
    """`br0` — обычно РЕАЛЬНЫЙ мост LAN, `br-<hash>` — docker; tailscale0 —
    рабочий путь, просто медленнее LAN."""
    assert not is_virtual_iface("br0")
    assert not is_virtual_iface("tailscale0")
    assert not is_virtual_iface("enp1s0")
    assert is_virtual_iface("br-1a2b3c4d")
    assert is_virtual_iface("vEthernet (WSL)")


# --- Нода слушает несколько адресов ---


def test_listen_принимает_и_строку_и_список():
    """Конфиги, написанные до этапа 24, продолжают работать как есть."""
    assert NodeConfig(listen="tcp://100.1.2.3:8710").listen == ["tcp://100.1.2.3:8710"]
    assert NodeConfig(listen="").listen == []
    assert NodeConfig(listen=["tcp://a:1", "tcp://b:2"]).listen == ["tcp://a:1", "tcp://b:2"]


@pytest.mark.asyncio
async def test_нода_поднимается_на_доступном_адресе_не_дожидаясь_отстающего(
    sock_dir, monkeypatch
):
    """Главный смысл этапа: LAN есть сразу, tailscale-адреса ещё нет —
    нода обязана работать уже сейчас, а не через минуту."""
    monkeypatch.setattr(server_mod, "BIND_RETRY_INTERVAL_S", 0.01)
    real_start_server = asyncio.start_server
    attempts = {"n": 0}

    async def flaky_start_server(*args, **kwargs):
        # Адрес «tailscale» (порт 0 с этим хостом) не поднимается первые разы.
        if kwargs.get("host") == "127.0.0.2":
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("could not bind on any address")
        return await real_start_server(*args, **kwargs)

    monkeypatch.setattr(server_mod.asyncio, "start_server", flaky_start_server)

    server = ProtoServer(
        [f"unix://{sock_dir / 'node.sock'}", TcpEndpoint("127.0.0.2", 0)],
        FakeService(),
        token="s3cret",
    )
    await server.start()
    try:
        # start() вернулся, хотя второй адрес ещё не поднялся.
        assert len(server.bound_endpoints) == 1
        assert await _wait_for(lambda: len(server.bound_endpoints) == 2), (
            "отстающий адрес так и не добрали фоном"
        )
        # Объявлять рою нечего: unix-сокет и loopback (а 127.0.0.2 — он же)
        # соседу бесполезны, по ним он попадёт к себе.
        assert server.advertised_endpoints() == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_занятый_порт_среди_нескольких_адресов_не_маскируется(sock_dir, monkeypatch):
    """EADDRINUSE — чужой процесс на порту: молча работать «наполовину»
    нельзя, это не «адрес ещё не приехал»."""

    async def busy_start_server(*args, **kwargs):
        raise OSError(errno.EADDRINUSE, "address already in use")

    monkeypatch.setattr(server_mod.asyncio, "start_server", busy_start_server)
    server = ProtoServer(
        [f"unix://{sock_dir / 'node.sock'}", TcpEndpoint("127.0.0.1", 9999)],
        FakeService(),
        token="s3cret",
    )
    with pytest.raises(OSError) as exc:
        await server.start()
    assert exc.value.errno == errno.EADDRINUSE
    await server.stop()


# --- Перебор адресов линком ---


@pytest.mark.asyncio
async def test_линк_перебирает_адреса_до_рабочего(sock_dir):
    """Первый адрес мёртв (машина сменила сеть) — связь всё равно есть."""
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, FakeService())
    await server.start()

    link = PeerLink(
        "winpc",
        [f"unix://{sock_dir / 'nowhere.sock'}", endpoint],
        reconnect_delay=0.05,
    )
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive), "линк не нашёл живой адрес"
        assert str(link.endpoint) == str(sock_dir / "peer.sock")
        # Удачный адрес встал первым — следующий перебор начнётся с него.
        assert link.endpoints[0] == str(sock_dir / "peer.sock")
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_адрес_с_чужой_нодой_уступает_правильному(sock_dir):
    """Сосед мог назвать адрес, который в НАШЕЙ сети занят кем-то другим:
    связь по нему — это запросы не туда. Пока есть другие адреса, берём их."""
    wrong = f"unix://{sock_dir / 'wrong.sock'}"
    right = f"unix://{sock_dir / 'right.sock'}"
    other = ProtoServer(wrong, FakeService(node="alfred"))
    ours = ProtoServer(right, FakeService(node="winpc"))
    await other.start()
    await ours.start()

    link = PeerLink("winpc", [wrong, right], reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        assert str(link.endpoint) == str(sock_dir / "right.sock"), (
            "линк остался на адресе, где отвечает чужая нода"
        )
    finally:
        await link.stop()
        await ours.stop()
        await other.stop()


@pytest.mark.asyncio
async def test_единственный_адрес_с_чужим_именем_всё_же_работает(sock_dir, caplog):
    """Имя в [[swarm.nodes]] человек пишет как ему удобно и оно законно может
    не совпасть с hostname соседа: отказываться от единственного адреса
    из-за этого нельзя — так вело себя и до этапа 24."""
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, FakeService(node="desktop-ie953ua"))
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive), "линк отказался от единственного адреса"
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_адрес_отвечающий_нами_самими_уступает_правильному(sock_dir):
    """Живая находка 2026-08-04: DHCP без резервации отдал IP, когда-то
    выученный как адрес соседа, нам самим. Ответ «это я» — не «сосед иначе
    называется» (RANK-фолбэк из теста выше), а гарантированно мёртвый
    кандидат: пока есть настоящий адрес соседа, брать его."""
    stale = f"unix://{sock_dir / 'stale.sock'}"
    right = f"unix://{sock_dir / 'right.sock'}"
    ourselves = ProtoServer(stale, FakeService(node="alfred"))
    neighbour = ProtoServer(right, FakeService(node="mycraft"))
    await ourselves.start()
    await neighbour.start()

    link = PeerLink("mycraft", [stale, right], reconnect_delay=0.05, self_node="alfred")
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        assert str(link.endpoint) == str(sock_dir / "right.sock"), (
            "линк остался на адресе, где отвечаем мы сами"
        )
    finally:
        await link.stop()
        await neighbour.stop()
        await ourselves.stop()


@pytest.mark.asyncio
async def test_единственный_адрес_отвечающий_нами_самими_не_считается_живым(sock_dir):
    """В отличие от «просто другое имя в конфиге» (тест выше), ответ
    буквально нашим собственным node_id никогда не годится даже как
    последняя надежда — иначе линк подключается сам к себе и врёт
    PresenceWatcher, что сосед «вернулся в строй»."""
    stale = f"unix://{sock_dir / 'stale.sock'}"
    ourselves = ProtoServer(stale, FakeService(node="alfred"))
    await ourselves.start()

    link = PeerLink("mycraft", stale, reconnect_delay=0.05, self_node="alfred")
    await link.start()
    try:
        await asyncio.sleep(0.2)
        assert not link.alive, "линк принял ответ от самого себя за соседа"
    finally:
        await link.stop()
        await ourselves.stop()


@pytest.mark.asyncio
async def test_устаревший_адрес_не_копится_после_свежего_hello(sock_dir):
    """Живая находка 2026-08-04: раньше выученное только прирастало «в
    надежде, что адрес вернётся к старому» — стухший кандидат простаивал
    в списке навсегда. Свежий hello ЗАМЕНЯЕТ список, а не дополняет."""
    stale = f"unix://{sock_dir / 'stale.sock'}"  # никто не слушает
    right = f"unix://{sock_dir / 'right.sock'}"
    service = FakeService(node="mycraft", endpoints=("tcp://192.168.0.103:8710",))
    server = ProtoServer(right, service)
    await server.start()

    link = PeerLink("mycraft", [stale, right], reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        assert await _wait_for(lambda: stale not in link.endpoints), link.endpoints
        assert "tcp://192.168.0.103:8710" in link.endpoints
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_мисматч_чужого_соседа_не_портит_kind_и_адреса(sock_dir):
    """Найдено 2026-08-04: «запасной» ответ от РЕАЛЬНОГО, но другого
    соседа (устаревший адрес переехал на него, не на нас — тот случай
    гонит тест выше) раньше приписывал нашему линку чужие kind/endpoints.
    Мисматч годится только для связи, не для памяти о соседе."""
    wrong = f"unix://{sock_dir / 'wrong.sock'}"
    other = ProtoServer(
        wrong,
        FakeService(
            node="winpc", endpoints=("tcp://192.168.0.104:8710",), node_kind="workstation"
        ),
    )
    await other.start()

    link = PeerLink("mycraft", wrong, reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        await asyncio.sleep(0.1)
        assert link.node_kind == ""
        assert "tcp://192.168.0.104:8710" not in link.endpoints
    finally:
        await link.stop()
        await other.stop()


@pytest.mark.asyncio
async def test_линк_учит_адреса_из_hello_и_сообщает_их_наверх(sock_dir):
    """Сосед знает свои адреса точнее, чем чужой конфиг, — рассказывает их
    в hello, а нода сохраняет (иначе выученное терялось бы на рестарте)."""
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    service = FakeService(endpoints=("tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"))
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
        assert await _wait_for(lambda: link.alive)
        assert await _wait_for(lambda: len(link.endpoints) == 3), link.endpoints
        # Сначала — путь, по которому связь уже есть, затем LAN, затем оверлей.
        assert link.endpoints[1:] == ["tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"]
        assert learned and learned[-1][0] == "winpc"
    finally:
        await link.stop()
        await server.stop()


@pytest.mark.asyncio
async def test_линк_не_учит_loopback_соседа(sock_dir):
    """По 127.0.0.1 мы попадём не к соседу, а к себе — такой адрес не берём
    ни при каких обстоятельствах."""
    endpoint = f"unix://{sock_dir / 'peer.sock'}"
    server = ProtoServer(endpoint, FakeService(endpoints=("tcp://127.0.0.1:8710",)))
    await server.start()

    link = PeerLink("winpc", endpoint, reconnect_delay=0.05)
    await link.start()
    try:
        assert await _wait_for(lambda: link.alive)
        await asyncio.sleep(0.1)
        assert link.endpoints == [str(sock_dir / "peer.sock")]
    finally:
        await link.stop()
        await server.stop()


# --- Known-good адрес переживает рестарт ---


def test_адреса_пира_берутся_из_состояния_вперёд_конфига():
    """В состоянии — последний удачный путь и всё выученное; в TOML — один
    адрес, вписанный руками. Пропадать не должно ни то, ни другое."""
    toml = [SwarmNodeConfig(id="winpc", endpoint="tcp://100.78.225.83:8710")]
    state = [
        SwarmNodeConfig(
            id="winpc",
            endpoint="tcp://192.168.0.105:8710",
            endpoints=["tcp://100.78.225.83:8710"],
        )
    ]
    assert peer_candidates(toml, state) == {
        "winpc": ["tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"]
    }


def test_пир_только_из_конфига_работает_как_раньше():
    toml = [SwarmNodeConfig(id="winpc", endpoint="tcp://100.78.225.83:8710")]
    assert peer_candidates(toml, []) == {"winpc": ["tcp://100.78.225.83:8710"]}


# --- Обмен адресами в hello и swarm_join ---


def _service(**kw) -> NodeService:
    return NodeService(Supervisor([], None, emit=_emit), node_id="alfred", **kw)


def test_нода_называет_рою_то_что_реально_слушает():
    """Не адрес из конфига, а живой список: он меняется, пока адреса
    доезжают (tailscale поднимается десятки секунд после старта)."""
    live = ["tcp://192.168.0.100:8710"]
    svc = _service(own_endpoint="tcp://100.73.52.92:8710", advertise=lambda: list(live))
    assert svc.describe().info.endpoints == ("tcp://192.168.0.100:8710",)
    live.append("tcp://100.73.52.92:8710")
    assert len(svc.describe().info.endpoints) == 2


def test_без_живого_списка_объявляется_настроенный_адрес():
    """Пока сервер не поднялся (и в сборках без него) единственная правда о
    себе — конфиг."""
    svc = _service(own_endpoint="tcp://100.73.52.92:8710", advertise=lambda: [])
    assert svc.describe().info.endpoints == ("tcp://100.73.52.92:8710",)


@pytest.mark.asyncio
async def test_swarm_join_возит_все_адреса_обеих_сторон():
    """Присоединяющийся называет все свои адреса, принимающий — свои и
    известные ему адреса соседей."""
    router = NodeRouter("alfred")
    created: list[tuple[str, list[str]]] = []

    async def fake_add_peer(link: PeerLink) -> None:
        created.append((link.name, link.endpoints))
        router.peers[link.name] = link

    router.add_peer = fake_add_peer  # type: ignore[method-assign]
    svc = NodeService(
        Supervisor([], None, emit=_emit),
        router,
        node_id="alfred",
        own_endpoint="tcp://100.73.52.92:8710",
        advertise=lambda: ["tcp://192.168.0.100:8710", "tcp://100.73.52.92:8710"],
    )

    result = await svc.run_command(
        "swarm_join",
        {
            "node_id": "winpc",
            "endpoint": "tcp://192.168.0.105:8710",
            "endpoints": ["tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"],
        },
    )

    assert created == [
        ("winpc", ["tcp://192.168.0.105:8710", "tcp://100.78.225.83:8710"])
    ]
    assert result["peers"][0]["endpoints"] == [
        "tcp://192.168.0.100:8710",
        "tcp://100.73.52.92:8710",
    ]


@pytest.mark.asyncio
async def test_обновление_адресов_не_стирает_тип_соседа():
    """Тип узнаётся из hello и нужен именно тогда, когда соседа уже не
    спросить: потерять его на записи адресов — значит разучиться отличать
    «сервер пропал» от «рабочая станция уснула»."""
    state = NodeState(
        peers=[SwarmNodeConfig(id="winpc", endpoint="tcp://old:8710", kind="workstation")]
    )
    _remember_peer(state, "winpc", "tcp://192.168.0.105:8710", ["tcp://192.168.0.105:8710"])
    assert state.peers[0].kind == "workstation"
    assert state.peers[0].endpoint == "tcp://192.168.0.105:8710"


@pytest.mark.asyncio
async def test_swarm_join_от_ноды_старой_версии_работает_как_раньше():
    """Поле endpoints появилось в этапе 24 — без него всё как было."""
    router = NodeRouter("alfred")
    created: list[list[str]] = []

    async def fake_add_peer(link: PeerLink) -> None:
        created.append(link.endpoints)
        router.peers[link.name] = link

    router.add_peer = fake_add_peer  # type: ignore[method-assign]
    svc = NodeService(
        Supervisor([], None, emit=_emit), router, node_id="alfred", own_endpoint="tcp://a:8710"
    )
    await svc.run_command("swarm_join", {"node_id": "winpc", "endpoint": "tcp://old:8710"})
    assert created == [["tcp://old:8710"]]
