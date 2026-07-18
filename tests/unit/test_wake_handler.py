"""/wake через рой: кэш реквизитов + отправка через живую ноду из той же LAN
(этап 19 п.6, IMPLEMENTATION_PLAN.md). Ручной путь ([wake] в конфиге) уже
покрыт test_swarm_view.py (кнопка) — здесь только оркестрация по рою."""

from __future__ import annotations

import pytest_asyncio

from sa_home_bot.bot import wake_state
from sa_home_bot.bot.handlers.wake import _wake_swarm_node
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ProtoError

ALFRED_WAKE = {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"}
WINPC_WAKE = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.50", "broadcast": "192.168.0.255"}

OWN_STATE = {
    "node": "alfred",
    "version": "0.25.0",
    "services": [],
    "wake": ALFRED_WAKE,
    "peers": [
        {"id": "arch-t480", "endpoint": "tcp://x:8710", "alive": True},
        {"id": "winpc", "endpoint": "tcp://y:8710", "alive": False},
    ],
}

ARCH_STATE = {"node": "arch-t480", "version": "0.25.0", "services": [], "wake": None}


class FakeMessage:
    def __init__(self) -> None:
        self.answers: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeNodeLink:
    display_name = "нода"

    def __init__(self, own, routes=None, command_error=None):
        self._own = own
        self._routes = routes or {}
        self.command_calls: list[tuple[str, dict, str | None]] = []
        self._command_error = command_error

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        key = f"{dst.node}:{dst.service}"
        if key in self._routes:
            return self._routes[key]
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None):
        self.command_calls.append((action, args, dst.node if dst else None))
        if self._command_error is not None:
            raise self._command_error
        return {"sent": True, "mac": (args or {}).get("mac")}


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def test_wake_swarm_node_without_cache_asks_to_wait(store):
    message = FakeMessage()
    link = FakeNodeLink(OWN_STATE, routes={"arch-t480:node": ARCH_STATE})
    await _wake_swarm_node(message, link, store, "winpc")
    assert "Нет данных о MAC" in message.answers[0]
    assert link.command_calls == []


async def test_wake_swarm_node_sends_via_matching_lan_peer(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    message = FakeMessage()
    link = FakeNodeLink(OWN_STATE, routes={"arch-t480:node": ARCH_STATE})

    await _wake_swarm_node(message, link, store, "winpc")

    assert link.command_calls == [("send_wol", {"mac": WINPC_WAKE["mac"]}, "alfred")]
    assert "через ноду «alfred»" in message.answers[0]


async def test_wake_swarm_node_no_matching_lan_peer(store):
    # Arch-t480 — Wi-Fi (wake=None), broadcast winpc ни с кем не совпадает.
    await wake_state.remember(store, "winpc", {**WINPC_WAKE, "broadcast": "10.0.0.255"})
    message = FakeMessage()
    link = FakeNodeLink(
        {**OWN_STATE, "wake": None}, routes={"arch-t480:node": ARCH_STATE}
    )

    await _wake_swarm_node(message, link, store, "winpc")

    assert "Некому отправить сигнал" in message.answers[0]
    assert link.command_calls == []


async def test_wake_swarm_node_waker_goes_unavailable_mid_flight(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    message = FakeMessage()
    link = FakeNodeLink(
        OWN_STATE,
        routes={"arch-t480:node": ARCH_STATE},
        command_error=ServiceUnavailableError("оборвалось"),
    )

    await _wake_swarm_node(message, link, store, "winpc")

    assert "перестала отвечать" in message.answers[0]


async def test_wake_swarm_node_proto_error_from_waker(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    message = FakeMessage()
    link = FakeNodeLink(
        OWN_STATE,
        routes={"arch-t480:node": ARCH_STATE},
        command_error=ProtoError("bad_request", "битый MAC"),
    )

    await _wake_swarm_node(message, link, store, "winpc")

    assert "битый MAC" in message.answers[0]
