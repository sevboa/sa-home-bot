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


class FakeLink:
    def __init__(self, name: str, *, alive: bool, kind: str = "", down_s: float | None = None):
        self.name = name
        self.endpoint = f"tcp://{name}:8710"
        self.endpoints = [self.endpoint]
        self.alive = alive
        self.node_kind = kind
        self.left = False
        self._down_s = down_s

    def downtime_s(self) -> float | None:
        return None if self.alive else self._down_s


def _watcher(node_id: str, *links: FakeLink, **kw) -> tuple[PresenceWatcher, list[tuple]]:
    events: list[tuple] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    kw.setdefault("down_after_s", 300.0)
    router = NodeRouter(node_id, peers={link.name: link for link in links})
    return PresenceWatcher(node_id, router, emit=emit, **kw), events


async def test_left_нода_не_рождает_node_down_даже_для_сервера():
    link = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    link.left = True
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
    assert link.downtime_s() is not None


async def test_возвращение_сбрасывает_left():
    link = PeerLink("winpc", "tcp://winpc:8710")
    link.note_left()
    router = NodeRouter("alfred", peers={"winpc": link})
    assert router.peers_state()[0]["left"] is True


async def test_возвращение_рабочей_станции_объявляется():
    link = FakeLink("winpc", alive=False, kind=KIND_WORKSTATION, down_s=100)
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()
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
    await watcher.check_once()
    await watcher.check_once()
    assert events == []


async def test_возвращение_объявляет_только_объявитель():
    link = FakeLink("winpc", alive=False, kind=KIND_WORKSTATION, down_s=100)
    peer = FakeLink("alfred", alive=True)
    watcher, events = _watcher("zeta", link, peer)
    await watcher.check_once()
    link.alive = True
    await watcher.check_once()
    assert events == []


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


async def test_sleeping_workstation_raises_no_alarm():
    watcher, events = _watcher(
        "alfred", FakeLink("winpc", alive=False, kind=KIND_WORKSTATION, down_s=9999)
    )
    await watcher.check_once()
    assert events == []


async def test_missing_always_on_node_raises_node_down():
    watcher, events = _watcher("alfred", FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600))
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]
    assert events[0][1]["node"] == "jeeves"


async def test_node_down_waits_for_the_threshold():
    watcher, events = _watcher(
        "alfred", FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=120), down_after_s=300.0
    )
    await watcher.check_once()
    assert events == []


async def test_node_down_is_reported_once_then_node_up_on_return():
    link = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]
    link.alive = True
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN, EVENT_NODE_UP]


async def test_only_the_lowest_id_alive_node_announces():
    down = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    peer = FakeLink("winpc", alive=True, kind=KIND_WORKSTATION)

    quiet, quiet_events = _watcher("zeta", down, peer)
    await quiet.check_once()
    assert quiet_events == []

    loud, loud_events = _watcher("alfred", down, peer)
    await loud.check_once()
    assert [e[0] for e in loud_events] == [EVENT_NODE_DOWN]


async def test_kind_of_an_unreachable_peer_is_remembered(tmp_path):
    state_path = tmp_path / "node-state.json"
    state = NodeState()
    link = FakeLink("jeeves", alive=True, kind=KIND_VPS)
    watcher, _ = _watcher("alfred", link, state=state, state_path=str(state_path))
    await watcher.check_once()

    reloaded = NodeState.load(state_path)
    assert [(p.id, p.kind) for p in reloaded.peers] == [("jeeves", KIND_VPS)]

    link.alive = False
    link.node_kind = ""
    link._down_s = 600
    watcher2, events = _watcher(
        "alfred", link, state=reloaded, state_path=str(state_path)
    )
    await watcher2.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]
