import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.config import Settings, SwarmNodeConfig
from sa_home_bot.node.app import (
    SeenEvents,
    _relay_peer_event,
    build_router,
    make_link_factories,
    peer_candidates,
    peer_kinds,
)
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import Address, ProtoError, make_event
from sa_home_bot.proto.server import ProtoServer


async def _emit(event_type, data):
    pass


def _bare_service(**kw) -> NodeService:
    sup = Supervisor([], None, emit=_emit)
    return NodeService(sup, **kw)


async def test_swarm_join_requires_node_id_and_endpoint():
    svc = _bare_service(own_endpoint="tcp://a:8710")
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("swarm_join", {"node_id": "b"})
    assert exc_info.value.code == "bad_request"


async def test_swarm_join_rejects_self_address():
    svc = NodeService(Supervisor([], None, emit=_emit), node_id="alfred", own_endpoint="x")
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command(
            "swarm_join", {"node_id": "alfred", "endpoint": "tcp://alfred:8710"}
        )
    assert exc_info.value.code == "bad_request"


def test_swarm_join_and_join_declared_only_with_own_endpoint():
    without = _bare_service().describe()
    assert without.find_action("swarm_join") is None
    assert without.find_action("join") is None

    with_endpoint = _bare_service(own_endpoint="tcp://a:8710").describe()
    assert with_endpoint.find_action("swarm_join") is not None
    assert with_endpoint.find_action("join") is not None


async def test_join_requires_endpoint():
    svc = _bare_service(own_endpoint="tcp://a:8710")
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("join", {})
    assert exc_info.value.code == "bad_request"


@pytest.fixture
async def two_nodes():
    tmpdir = Path(tempfile.mkdtemp(prefix="sa-join-"))

    def build(node_id: str, listen: Path):
        router = NodeRouter(node_id)
        state = NodeState()
        state_path = tmpdir / f"{node_id}-state.json"
        svc = NodeService(
            Supervisor([], None, emit=_emit),
            router,
            node_id=node_id,
            state=state,
            state_path=str(state_path),
            own_endpoint=str(listen),
        )
        server = ProtoServer([listen], svc, router=router.route)
        return router, state, state_path, svc, server

    a_listen = tmpdir / "a.sock"
    b_listen = tmpdir / "b.sock"
    router_a, state_a, path_a, svc_a, server_a = build("A", a_listen)
    router_b, state_b, path_b, svc_b, server_b = build("B", b_listen)
    await server_a.start()
    await server_b.start()
    try:
        yield {
            "a": (router_a, state_a, path_a, svc_a, server_a, a_listen),
            "b": (router_b, state_b, path_b, svc_b, server_b, b_listen),
        }
    finally:
        for router, _, _, _, server, _ in (
            (router_a, None, None, None, server_a, None),
            (router_b, None, None, None, server_b, None),
        ):
            for link in router.peers.values():
                await link.stop()
            await server.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_join_connects_both_directions_and_persists(two_nodes):
    router_a, state_a, path_a, _, _, a_listen = two_nodes["a"]
    router_b, state_b, path_b, svc_b, _, b_listen = two_nodes["b"]

    result = await svc_b.join(str(a_listen))
    assert result["peers_added"] == ["A"]

    assert "A" in router_b.peers
    assert NodeState.load(path_b).peers[0].id == "A"
    assert "B" in router_a.peers
    assert NodeState.load(path_a).peers[0].id == "B"


async def test_join_unavailable_neighbor_raises_unavailable():
    svc = NodeService(
        Supervisor([], None, emit=_emit), node_id="B", own_endpoint="unix:/tmp/nope-b.sock"
    )
    with pytest.raises(ProtoError) as exc_info:
        await svc.join("unix:/tmp/definitely-not-there.sock")
    assert exc_info.value.code == "unavailable"


async def test_swarm_join_idempotent_second_call_same_endpoint(two_nodes):
    router_a, _, _, _, _, a_listen = two_nodes["a"]
    _, _, _, svc_b, _, _ = two_nodes["b"]

    await svc_b.join(str(a_listen))
    first_link = router_a.peers["B"]
    await svc_b.join(str(a_listen))
    assert router_a.peers["B"] is first_link


async def test_relay_peer_event_auto_adds_new_peer(tmp_path):
    router = NodeRouter("B")
    added: list[tuple[str, str]] = []

    async def fake_add_peer(link: PeerLink) -> None:
        added.append((link.name, str(link.endpoint)))
        router.peers[link.name] = link

    router.add_peer = fake_add_peer  # type: ignore[method-assign]

    state = NodeState()
    state_path = tmp_path / "state.json"
    env = make_event(
        "node_joined",
        {"node_id": "C", "endpoint": "tcp://c:8710"},
        src=Address(node="A", service="node"),
    )

    await _relay_peer_event(
        env,
        node_id="B",
        router=router,
        state=state,
        state_path=str(state_path),
        make_peer_link=PeerLink,
        server=None,
        seen=SeenEvents(),
    )

    assert added == [("C", "tcp://c:8710")]
    assert NodeState.load(state_path).peers[0].id == "C"


async def test_relay_peer_event_ignores_own_echo():
    router = NodeRouter("A")
    calls = []
    router.add_peer = lambda link: calls.append(link)

    env = make_event(
        "node_joined",
        {"node_id": "C", "endpoint": "tcp://c:8710"},
        src=Address(node="A", service="node"),
    )
    await _relay_peer_event(
        env,
        node_id="A",
        router=router,
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=None,
        seen=SeenEvents(),
    )
    assert calls == []


async def test_relay_peer_event_local_service_same_node_id_is_not_echo():
    router = NodeRouter("alfred")
    broadcasts: list[str] = []

    class _FakeServer:
        async def broadcast_envelope(self, env):
            broadcasts.append(env.id)

    env = make_event(
        "task_result",
        {"task_id": 1, "ok": True},
        src=Address(node="alfred", service="tasks"),
    )
    await _relay_peer_event(
        env,
        node_id="alfred",
        router=router,
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=_FakeServer(),
        seen=SeenEvents(),
        is_local=True,
    )
    assert broadcasts == [env.id]


async def test_relay_peer_event_peer_same_node_id_still_treated_as_echo():
    router = NodeRouter("alfred")

    class _FakeServer:
        async def broadcast_envelope(self, env):
            raise AssertionError("не должно быть вызвано — это эхо")

    env = make_event(
        "task_result",
        {"task_id": 1, "ok": True},
        src=Address(node="alfred", service="tasks"),
    )
    await _relay_peer_event(
        env,
        node_id="alfred",
        router=router,
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=_FakeServer(),
        seen=SeenEvents(),
    )


async def test_build_router_wires_local_services_to_on_local_event():
    settings = Settings()

    def on_peer_event(e):
        pass

    def on_local_event(e):
        pass

    make_peer_link, make_local_link = make_link_factories(
        settings, "alfred", on_peer_event, on_local_event
    )
    router = build_router(settings, "alfred", ["tasks"], [], make_peer_link, make_local_link)
    assert "tasks" in router.local_services
    assert router.local_services["tasks"]._on_event is on_local_event
    assert router.local_services["tasks"]._on_event is not on_peer_event


def test_peer_kinds_state_wins_over_toml():
    nodes = [SwarmNodeConfig(id="jeeves", endpoint="tcp://a:8710", kind="server")]
    extra = [SwarmNodeConfig(id="jeeves", endpoint="tcp://a:8710", kind="vps")]
    assert peer_kinds(nodes, extra) == {"jeeves": "vps"}
    assert peer_kinds([SwarmNodeConfig(id="x", endpoint="tcp://b:8710")], []) == {}


async def test_build_router_seeds_peer_kind_from_persistent_state():
    settings = Settings()
    make_peer_link, make_local_link = make_link_factories(
        settings, "alfred", lambda e: None, lambda e: None
    )
    extra = [SwarmNodeConfig(id="jeeves", endpoint="tcp://z:8710", kind="vps")]
    router = build_router(settings, "alfred", [], extra, make_peer_link, make_local_link)
    assert router.peers["jeeves"].node_kind == "vps"
    assert router.peers_state()[0]["kind"] == "vps"


async def test_link_factories_wire_events_and_identity():
    settings = Settings()

    def on_peer_event(e):
        pass

    def on_local_event(e):
        pass

    make_peer_link, make_local_link = make_link_factories(
        settings, "alfred", on_peer_event, on_local_event
    )
    peer = make_peer_link("winpc", "tcp://w:8710")
    assert peer._on_event is on_peer_event
    assert peer._src is not None and peer._src.node == "alfred"

    local = make_local_link("tasks", "unix:/tmp/tasks.sock")
    assert local._on_event is on_local_event


async def test_swarm_join_and_join_use_peer_link_factory(two_nodes):
    router_a, _, _, svc_a, _, a_listen = two_nodes["a"]
    router_b, _, _, svc_b, _, _ = two_nodes["b"]

    events_a: list = []
    events_b: list = []

    async def on_peer_event_a(env):
        events_a.append(env)

    async def on_peer_event_b(env):
        events_b.append(env)

    svc_a._make_peer_link = lambda pid, ep: PeerLink(
        pid, ep, on_event=on_peer_event_a, self_node="A"
    )
    svc_b._make_peer_link = lambda pid, ep: PeerLink(
        pid, ep, on_event=on_peer_event_b, self_node="B"
    )

    await svc_b.join(str(a_listen))
    assert router_b.peers["A"]._on_event is on_peer_event_b
    assert router_a.peers["B"]._on_event is on_peer_event_a


async def test_relay_peer_event_skips_already_known_peer():
    router = NodeRouter("B")
    router.peers["C"] = PeerLink("C", "tcp://c:8710")
    calls = []
    router.add_peer = lambda link: calls.append(link)

    env = make_event(
        "node_joined",
        {"node_id": "C", "endpoint": "tcp://c:8710"},
        src=Address(node="A", service="node"),
    )
    await _relay_peer_event(
        env,
        node_id="B",
        router=router,
        state=NodeState(),
        state_path="/dev/null",
        make_peer_link=PeerLink,
        server=None,
        seen=SeenEvents(),
    )
    assert calls == []


async def test_relay_peer_event_deduplicates_by_envelope_id():
    router = NodeRouter("B")
    broadcasts: list[str] = []

    class _FakeServer:
        async def broadcast_envelope(self, env):
            broadcasts.append(env.id)

    env = make_event(
        "update_finished",
        {"ok": True},
        src=Address(node="A", service="node"),
    )
    seen = SeenEvents()
    for _ in range(3):
        await _relay_peer_event(
            env,
            node_id="B",
            router=router,
            state=NodeState(),
            state_path="/dev/null",
            make_peer_link=PeerLink,
            server=_FakeServer(),
            seen=seen,
        )

    assert broadcasts == [env.id]


async def test_seen_events_bounded_size():
    from sa_home_bot.node.app import SeenEvents as _SeenEvents

    seen = _SeenEvents(maxsize=3)
    for i in range(5):
        assert seen.seen(f"id{i}") is False
    assert seen.seen("id0") is False
    assert seen.seen("id4") is True


def test_адреса_пира_берутся_из_состояния_вперёд_конфига():
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


def _service(**kw) -> NodeService:
    return NodeService(Supervisor([], None, emit=_emit), node_id="alfred", **kw)


def test_нода_называет_рою_то_что_реально_слушает():
    live = ["tcp://192.168.0.100:8710"]
    svc = _service(own_endpoint="tcp://100.73.52.92:8710", advertise=lambda: list(live))
    assert svc.describe().info.endpoints == ("tcp://192.168.0.100:8710",)
    live.append("tcp://100.73.52.92:8710")
    assert len(svc.describe().info.endpoints) == 2


def test_без_живого_списка_объявляется_настроенный_адрес():
    svc = _service(own_endpoint="tcp://100.73.52.92:8710", advertise=lambda: [])
    assert svc.describe().info.endpoints == ("tcp://100.73.52.92:8710",)


async def test_swarm_join_возит_все_адреса_обеих_сторон():
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


async def test_swarm_join_от_ноды_старой_версии_работает_как_раньше():
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
