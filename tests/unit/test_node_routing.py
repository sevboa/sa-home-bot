"""Межнодовая маршрутизация: «спроси любого» через NodeRouter + PeerLink.

Стенд: «удалённая нода» (winpc/monitor) на unix-сокете + «своя нода» (alfred)
с маршрутизатором. Клиент говорит только со своей нодой; адресация — dst
конверта.
"""

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_ACTION,
    ERR_UNKNOWN_DST,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import ProtoServer


class FakeRemoteMonitor:
    """Служба «удалённой» ноды."""

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="winpc", service="monitor", version="0.11.0"),
            capabilities=("temperature",),
            actions=(ActionSpec(id="scan_now", title="Скан"),),
        )

    async def get_state(self) -> dict:
        return {"node": "winpc", "cpu_c": 47.5}

    async def run_command(self, action: str, args: dict) -> dict:
        return {"accepted": True, "on": "winpc"}


class FakeLocalNode:
    """Сервис «своей» ноды (alfred) — то, что отвечает без маршрутизации."""

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="alfred", service="node", version="0.11.0"),
            capabilities=("supervisor",),
        )

    async def get_state(self) -> dict:
        return {"node": "alfred", "local": True}

    async def run_command(self, action: str, args: dict) -> dict:
        return {}


@pytest.fixture
async def swarm():
    tmpdir = Path(tempfile.mkdtemp(prefix="sa-swarm-"))
    # «Удалённая» нода winpc.
    remote = ProtoServer(tmpdir / "winpc.sock", FakeRemoteMonitor())
    await remote.start()
    # Линк alfred → winpc и маршрутизатор alfred'а.
    peer = PeerLink("winpc", tmpdir / "winpc.sock", reconnect_delay=0.1)
    await peer.start()
    router = NodeRouter("alfred", peers={"winpc": peer})
    local = ProtoServer(tmpdir / "alfred.sock", FakeLocalNode(), router=router.route)
    await local.start()

    relayed: list = []
    got_relayed = asyncio.Event()

    async def on_event(env):
        relayed.append(env)
        got_relayed.set()

    # Клиент (как бот/nodectl): говорит только со своей нодой alfred.
    client = ProtoClient(tmpdir / "alfred.sock", on_event=on_event, timeout=5.0)
    await client.connect()
    # Дождаться, пока линк до winpc поднимется.
    for _ in range(100):
        if peer.alive:
            break
        await asyncio.sleep(0.02)
    try:
        yield remote, peer, router, local, client, relayed, got_relayed
    finally:
        await client.close()
        await peer.stop()
        await local.stop()
        await remote.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_request_to_remote_node_is_forwarded(swarm):
    _, _, _, _, client, _, _ = swarm
    state = await client.get_state(dst=Address(node="winpc", service="monitor"))
    assert state == {"node": "winpc", "cpu_c": 47.5}

    result = await client.command(
        "scan_now", dst=Address(node="winpc", service="monitor")
    )
    assert result["on"] == "winpc"


async def test_remote_error_comes_back_as_is(swarm):
    _, _, _, _, client, _, _ = swarm
    with pytest.raises(ProtoError) as exc_info:
        await client.command("no_such", dst=Address(node="winpc", service="monitor"))
    assert exc_info.value.code == ERR_UNKNOWN_ACTION


async def test_local_dst_stays_local(swarm):
    _, _, _, _, client, _, _ = swarm
    # Без dst и с явным своим node — отвечает своя нода.
    assert (await client.get_state())["node"] == "alfred"
    state = await client.get_state(dst=Address(node="alfred", service="node"))
    assert state["local"] is True


async def test_unknown_node_rejected(swarm):
    _, _, _, _, client, _, _ = swarm
    with pytest.raises(ProtoError) as exc_info:
        await client.get_state(dst=Address(node="toaster", service="monitor"))
    assert exc_info.value.code == ERR_UNKNOWN_DST


async def test_dead_peer_fails_fast(swarm):
    remote, peer, _, _, client, _, _ = swarm
    await remote.stop()
    await asyncio.sleep(0.05)  # линк замечает обрыв
    with pytest.raises(ProtoError) as exc_info:
        await client.get_state(dst=Address(node="winpc", service="monitor"))
    assert exc_info.value.code == ERR_UNAVAILABLE


async def test_local_service_proxy(swarm):
    # dst.service=monitor на своей ноде → прокси к локальной службе.
    remote, peer, router, _, client, _, _ = swarm
    router.local_services["monitor"] = peer  # переиспользуем стенд как «локальный monitor»
    state = await client.get_state(dst=Address(service="monitor"))
    assert state["cpu_c"] == 47.5

    with pytest.raises(ProtoError) as exc_info:
        await client.get_state(dst=Address(service="jukebox"))
    assert exc_info.value.code == ERR_UNKNOWN_DST


async def test_peer_event_relayed_with_original_src():
    """Событие пира ретранслируется клиентам ноды; src оригинала сохраняется."""
    tmpdir = Path(tempfile.mkdtemp(prefix="sa-relay-"))
    try:
        remote = ProtoServer(tmpdir / "winpc.sock", FakeRemoteMonitor())
        await remote.start()
        local_server: ProtoServer | None = None

        async def on_peer_event(env):
            # как в node/app.py: своё эхо не ретранслируем
            if env.src is not None and env.src.node == "alfred":
                return
            await local_server.broadcast_envelope(env)

        peer = PeerLink(
            "winpc", tmpdir / "winpc.sock", on_event=on_peer_event, reconnect_delay=0.1
        )
        await peer.start()
        local_server = ProtoServer(tmpdir / "alfred.sock", FakeLocalNode())
        await local_server.start()

        got = asyncio.Event()
        events: list = []

        async def on_event(env):
            events.append(env)
            got.set()

        client = ProtoClient(tmpdir / "alfred.sock", on_event=on_event, timeout=5.0)
        await client.connect()
        for _ in range(100):
            if peer.alive:
                break
            await asyncio.sleep(0.02)

        await remote.broadcast_event("overheat_started", {"component_id": "cpu"})
        await asyncio.wait_for(got.wait(), timeout=5.0)
        assert events[0].payload["event"] == "overheat_started"
        assert events[0].src.node == "winpc"  # источник не переписан

        await client.close()
        await peer.stop()
        await local_server.stop()
        await remote.stop()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
