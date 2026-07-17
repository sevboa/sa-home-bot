"""Присоединение к рою по токену (этап 18): swarm_join/join, замыкание сетки."""

import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.node.app import SeenEvents, _relay_peer_event
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


# --- _swarm_join: валидация (без реальной сети — свои проверки) ---


async def test_swarm_join_requires_node_id_and_endpoint():
    svc = _bare_service(own_endpoint="tcp://a:8710")
    with pytest.raises(ProtoError) as exc_info:
        await svc.run_command("swarm_join", {"node_id": "b"})  # нет endpoint
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


# --- Реальный round-trip: две ноды, unix-сокеты, без токена ---


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

    # B знает A напрямую (только что присоединился).
    assert "A" in router_b.peers
    assert NodeState.load(path_b).peers[0].id == "A"
    # A тоже узнал B (swarm_join регистрирует пришедшего).
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
    await svc_b.join(str(a_listen))  # повторно — тот же endpoint
    assert router_a.peers["B"] is first_link  # не пересоздан


# --- Замыкание сетки: node_joined от пира → авто-подключение (_relay_peer_event) ---


async def test_relay_peer_event_auto_adds_new_peer(tmp_path):
    router = NodeRouter("B")
    added: list[tuple[str, str]] = []

    async def fake_add_peer(link: PeerLink) -> None:
        added.append((link.name, str(link.endpoint)))
        router.peers[link.name] = link  # без реальной сети

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
        token="",
        on_peer_event=lambda e: None,
        server=None,
        seen=SeenEvents(),
    )

    assert added == [("C", "tcp://c:8710")]
    assert NodeState.load(state_path).peers[0].id == "C"


async def test_relay_peer_event_ignores_own_echo():
    router = NodeRouter("A")
    calls = []
    router.add_peer = lambda link: calls.append(link)  # не должно вызваться

    env = make_event(
        "node_joined",
        {"node_id": "C", "endpoint": "tcp://c:8710"},
        src=Address(node="A", service="node"),  # эхо самой себя
    )
    await _relay_peer_event(
        env,
        node_id="A",
        router=router,
        state=NodeState(),
        state_path="/dev/null",
        token="",
        on_peer_event=lambda e: None,
        server=None,
        seen=SeenEvents(),
    )
    assert calls == []


async def test_relay_peer_event_skips_already_known_peer():
    router = NodeRouter("B")
    router.peers["C"] = PeerLink("C", "tcp://c:8710")  # уже известен
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
        token="",
        on_peer_event=lambda e: None,
        server=None,
        seen=SeenEvents(),
    )
    assert calls == []  # уже в router.peers — не трогаем


# --- Дедуп ретрансляции: живой инцидент 2026-07-17, шторм в связном рое ---


async def test_relay_peer_event_deduplicates_by_envelope_id():
    """В полносвязном рое одно событие приходит несколькими путями —
    вторая (и дальнейшие) копия ТОГО ЖЕ env.id не должна ретранслироваться
    заново, иначе это лавина (см. SeenEvents docstring)."""
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
    for _ in range(3):  # три "копии" одного и того же события (разные пути)
        await _relay_peer_event(
            env,
            node_id="B",
            router=router,
            state=NodeState(),
            state_path="/dev/null",
            token="",
            on_peer_event=lambda e: None,
            server=_FakeServer(),
            seen=seen,
        )

    assert broadcasts == [env.id]  # разослано ровно один раз


async def test_seen_events_bounded_size():
    from sa_home_bot.node.app import SeenEvents as _SeenEvents

    seen = _SeenEvents(maxsize=3)
    for i in range(5):
        assert seen.seen(f"id{i}") is False
    # старые id вытеснены — id0/id1 больше не считаются виденными
    assert seen.seen("id0") is False
    assert seen.seen("id4") is True  # свежий всё ещё в наборе
