"""Этап 23: моментальные и точные статусы присутствия.

Раньше presence строился на догадках: «нода корректно остановилась» и
«машину выдернули из розетки» выглядели одинаково, возвращение рабочей
станции не объявлялось вообще (node_up требовал предшествующего node_down).
Здесь проверяются: штатный уход (node_leaving → пометка left, без ложного
node_down), возвращение любой ноды (node_returned) и их сочетания.
"""

from __future__ import annotations

from sa_home_bot.node.app import SeenEvents, _relay_peer_event
from sa_home_bot.node.kind import KIND_VPS, KIND_WORKSTATION
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.watch import (
    EVENT_NODE_DOWN,
    EVENT_NODE_RETURNED,
    EVENT_NODE_UP,
    PresenceWatcher,
)
from sa_home_bot.proto.messages import Address, make_event
from tests.unit.test_node_kind import FakeLink


def _watcher(node_id: str, *links: FakeLink, **kw) -> tuple[PresenceWatcher, list[tuple]]:
    events: list[tuple] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    kw.setdefault("down_after_s", 300.0)
    router = NodeRouter(node_id, peers={link.name: link for link in links})
    return PresenceWatcher(node_id, router, emit=emit, **kw), events


# --- Штатный уход: node_leaving подавляет аварию -----------------------------


async def test_left_нода_не_рождает_node_down_даже_для_сервера():
    link = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    link.left = True  # предупредила об уходе
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()
    assert events == []


async def test_relay_помечает_линк_left_по_node_leaving():
    router = NodeRouter("alfred")
    link = PeerLink("winpc", "tcp://winpc:8710")
    router.peers["winpc"] = link

    env = make_event(
        "node_leaving",
        {"node": "winpc", "kind": KIND_WORKSTATION},
        src=Address(node="winpc", service="node"),
    )
    await _relay_peer_event(
        env,
        node_id="alfred",
        router=router,
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=None,
        seen=SeenEvents(),
    )
    assert link.left is True
    assert link.downtime_s() is not None  # отсчёт простоя начался сразу


async def test_возвращение_сбрасывает_left():
    """После нового hello уход исчерпан: следующая пропажа — снова
    полноценный кандидат в аварии. Проверяем через peers_state()."""
    link = PeerLink("winpc", "tcp://winpc:8710")
    link.note_left()
    router = NodeRouter("alfred", peers={"winpc": link})
    assert router.peers_state()[0]["left"] is True


# --- Возвращение: node_returned для любого типа машины -----------------------


async def test_возвращение_рабочей_станции_объявляется():
    """До этапа 23 возвращение workstation не объявлялось никогда:
    node_up требовал предшествующего node_down, а node_down для неё
    не рождается by design."""
    link = FakeLink("winpc", alive=False, kind=KIND_WORKSTATION, down_s=100)
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()  # замечена недоступной
    assert events == []
    link.alive = True
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_RETURNED]
    assert events[0][1] == {"node": "winpc", "kind": KIND_WORKSTATION}


async def test_после_аварии_только_node_up_без_дубля_returned():
    link = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]
    link.alive = True
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN, EVENT_NODE_UP]


async def test_моргание_между_тиками_не_объявляется():
    link = FakeLink("winpc", alive=True, kind=KIND_WORKSTATION)
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()  # жива на обоих тиках — undown не замечен
    await watcher.check_once()
    assert events == []


async def test_возвращение_объявляет_только_объявитель():
    link = FakeLink("winpc", alive=False, kind=KIND_WORKSTATION, down_s=100)
    peer = FakeLink("alfred", alive=True)
    watcher, events = _watcher("zeta", link, peer)  # alfred < zeta — молчим
    await watcher.check_once()
    link.alive = True
    await watcher.check_once()
    assert events == []


# --- Сквозной путь: прощание доезжает до клиентов, линк помечен -------------


class _NodeService:
    def __init__(self, node: str) -> None:
        self._node = node

    def describe(self):
        from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo

        return ServiceDescription(
            info=ServiceInfo(node=self._node, service="node", version="0.44.0"),
            capabilities=(),
            actions=(ActionSpec(id="noop", title="Ничего"),),
        )

    async def get_state(self) -> dict:
        return {}

    async def run_command(self, action: str, args: dict) -> dict:
        return {}


async def test_node_leaving_доезжает_до_клиентов_и_метит_линк(tmp_path):
    """Регрессия: note_left рвёт то соединение, по которому событие пришло —
    если пометить линк ДО ретрансляции, отмена читающей задачи съедает
    рассылку, и бот прощания не видит. Проверяем весь путь на настоящих
    сокетах: A прощается → B ретранслирует своим клиентам И метит линк."""
    import asyncio

    from sa_home_bot.proto.client import ProtoClient
    from sa_home_bot.proto.server import ProtoServer

    a_ep = f"unix://{tmp_path / 'a.sock'}"
    b_ep = f"unix://{tmp_path / 'b.sock'}"
    server_a = ProtoServer(a_ep, _NodeService("A"))
    server_b = ProtoServer(b_ep, _NodeService("B"))
    await server_a.start()
    await server_b.start()

    router_b = NodeRouter("B")
    seen = SeenEvents()

    async def on_peer_event(env):
        await _relay_peer_event(
            env,
            node_id="B",
            router=router_b,
            state=NodeState(),
            state_path=str(tmp_path / "b-state.json"),
            make_peer_link=PeerLink,
            server=server_b,
            seen=seen,
        )

    link = PeerLink("A", a_ep, on_event=on_peer_event)
    router_b.peers["A"] = link
    await link.start()

    got: list = []

    async def on_client_event(env):
        got.append(env)

    client = ProtoClient(b_ep, on_event=on_client_event)
    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while not link.alive and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert link.alive
        await client.connect()

        await server_a.broadcast_event("node_leaving", {"node": "A", "kind": "server"})

        while not got and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert got, "клиент B не получил ретрансляцию node_leaving"
        assert got[0].payload["event"] == "node_leaving"
        assert got[0].src is not None and got[0].src.node == "A"
        assert link.left is True
        assert link.downtime_s() is not None
    finally:
        await client.close()
        await link.stop()
        await server_b.stop()
        await server_a.stop()
