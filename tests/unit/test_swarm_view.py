"""Сводка роя (/swarm): fan-out, агрегатная шапка, строки нод, wake-кнопка."""

import asyncio

import pytest_asyncio

from sa_home_bot.bot import wake_state
from sa_home_bot.bot.node_view import NODE_DOWN_TEXT
from sa_home_bot.bot.swarm_view import (
    PEER_TIMEOUT_S,
    REMOTE_STUB_TEXT,
    build_swarm_view,
    find_lan_waker,
)
from sa_home_bot.config import WakeConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription

OWN_STATE = {
    "node": "alfred",
    "version": "0.22.0",
    "services": [
        {"name": "monitor", "status": "running"},
        {"name": "telegram-bot", "status": "running"},
    ],
    "peers": [
        {"id": "arch-t480", "endpoint": "tcp://x:8710", "alive": True},
        {"id": "winpc", "endpoint": "tcp://y:8710", "alive": False},
    ],
}

PEER_STATE = {
    "node": "arch-t480",
    "version": "0.21.0",
    "services": [{"name": "monitor", "status": "running"}],
}

OWN_MONITOR = {
    "health": [
        {"component_id": "cpu:pkg", "kind": "cpu", "status": "ok", "temperature_c": 48.0},
        {"component_id": "disk:/dev/sda", "kind": "disk", "status": "ok", "temperature_c": 31.0},
    ],
    "last_outage": {
        "kind": "unexpected",
        "boot_at": "2026-07-09T10:00:00+00:00",
        "down_at": "2026-07-09T09:00:00+00:00",
        "up_at": "2026-07-09T10:00:00+00:00",
        "down_approx": True,
    },
    "requirements": [],
}

PEER_MONITOR = {
    "health": [
        {"component_id": "cpu:pkg", "kind": "cpu", "status": "alerting", "temperature_c": 91.0},
    ],
    "last_outage": {
        "kind": "clean",
        "boot_at": "2026-07-12T10:00:00+00:00",
        "down_at": "2026-07-12T09:00:00+00:00",
        "up_at": None,
        "down_approx": False,
    },
    "requirements": [{"id": "smartctl", "status": "needs_privilege", "hint": "nodectl fix"}],
}


class FakeNodeLink:
    """Маршрутизация get_state по dst, как её видит бот через свою ноду."""

    display_name = "нода"

    def __init__(self, own=None, routes=None, hang=()):  # hang — dst-ключи, что виснут
        self._own = own or OWN_STATE
        self._routes = routes or {}
        self._hang = set(hang)
        self.requests: list[str] = []

    async def get_state(self, dst=None):
        key = f"{dst.node}:{dst.service}" if dst is not None else "own"
        self.requests.append(key)
        if key in self._hang:
            await asyncio.sleep(PEER_TIMEOUT_S + 30)
        if key == "own":
            return self._own
        if key in self._routes:
            return self._routes[key]
        from sa_home_bot.bot.service_link import ServiceUnavailableError

        raise ServiceUnavailableError("нет связи")


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def _routes():
    # Свой монитор адресуется явным id своей ноды — NodeRouter трактует
    # dst.node == свой id как локальную маршрутизацию.
    return {
        "alfred:monitor": OWN_MONITOR,
        "arch-t480:node": PEER_STATE,
        "arch-t480:monitor": PEER_MONITOR,
    }


async def test_swarm_header_counts_and_versions():
    link = FakeNodeLink(routes=_routes())
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "3 нод" in text and "в сети 2" in text
    assert "свежая v0.22.0" in text
    assert "отстаёт arch-t480 (v0.21.0)" in text


async def test_swarm_versions_all_equal():
    peer = {**PEER_STATE, "version": "0.22.0"}
    link = FakeNodeLink(routes={**_routes(), "arch-t480:node": peer})
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "ПО: v0.22.0 у всех" in text


async def test_swarm_last_failure_freshest_unexpected_only():
    # У alfred сбой (unexpected) 09.07, у arch последнее отключение штатное —
    # в шапке сбой alfred; clean не считается сбоем.
    link = FakeNodeLink(routes=_routes())
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "Последний сбой: alfred" in text


async def test_swarm_no_failure_line_when_no_unexpected():
    own_monitor = {**OWN_MONITOR, "last_outage": None}
    link = FakeNodeLink(routes={**_routes(), "alfred:monitor": own_monitor})
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "Последний сбой" not in text


async def test_swarm_node_lines_links_and_facts():
    link = FakeNodeLink(routes=_routes())
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "🟢 /node_alfred · v0.22.0 · службы 2/2 · CPU 48°C" in text
    # У arch: алерт (🔔 1) и ⚠️ requirements.
    assert "🟢 /node_arch_t480 · v0.21.0 · службы 1/1 · CPU 91°C · 🔔 1 · ⚠️" in text
    assert "🔴 /node_winpc — не в сети" in text


async def test_swarm_dead_peer_gets_no_requests():
    link = FakeNodeLink(routes=_routes())
    await build_swarm_view(link, _sub("nodes"))
    assert not any(k.startswith("winpc:") for k in link.requests)


async def test_swarm_hung_peer_does_not_block(monkeypatch):
    # Зависший (но «живой») пир упирается в таймаут — сводка выходит,
    # его строка честно говорит «не отвечает».
    monkeypatch.setattr("sa_home_bot.bot.swarm_view.PEER_TIMEOUT_S", 0.05)
    link = FakeNodeLink(routes=_routes(), hang=("arch-t480:node", "arch-t480:monitor"))
    text, _ = await asyncio.wait_for(build_swarm_view(link, _sub("nodes")), timeout=5)
    assert "/node_arch_t480 — не отвечает" in text


async def test_swarm_monitorless_node_marked():
    routes = {"alfred:monitor": OWN_MONITOR, "arch-t480:node": PEER_STATE}
    link = FakeNodeLink(routes=routes)  # монитора arch нет в маршрутах
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "/node_arch_t480 · v0.21.0 · службы 1/1 · монитор не отвечает" in text


async def test_swarm_wake_stub_and_button():
    link = FakeNodeLink(routes=_routes())
    wake = WakeConfig(mac="AA:BB:CC:DD:EE:FF")
    text, keyboard = await build_swarm_view(link, _sub("nodes", "wake"), wake)
    assert REMOTE_STUB_TEXT in text
    codes = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert codes == ["st:wake"]


async def test_swarm_node_down():
    class DeadLink:
        display_name = "нода"

        async def get_state(self, dst=None):
            from sa_home_bot.bot.service_link import ServiceUnavailableError

            raise ServiceUnavailableError("нет связи")

    text, keyboard = await build_swarm_view(DeadLink(), _sub("nodes"))
    assert text == NODE_DOWN_TEXT
    assert keyboard is None


# --- wake через рой: кэширование реквизитов + кнопка на уснувшую ноду (этап 19 п.6) ---


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


ALFRED_WAKE = {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"}
WINPC_WAKE = {"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.0.50", "broadcast": "192.168.0.255"}


async def test_swarm_view_caches_wake_info_from_alive_nodes(store):
    own = {**OWN_STATE, "wake": ALFRED_WAKE}
    link = FakeNodeLink(own=own, routes=_routes())
    await build_swarm_view(link, _sub("nodes"), store=store)
    assert await wake_state.cached(store, "alfred") == ALFRED_WAKE


async def test_swarm_offline_node_gets_wake_button_when_cached(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    link = FakeNodeLink(routes=_routes())
    _, keyboard = await build_swarm_view(link, _sub("nodes", "wake"), store=store)
    codes = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "st:wake:winpc" in codes


async def test_swarm_not_responding_node_gets_wake_button_when_cached(store):
    # alive=True (PeerLink формально ещё не заметил обрыв — TCP keepalive
    # не мгновенный), но get_state зависает/недоступен — "не отвечает",
    # не "не в сети". Кнопка нужна и тут (живая находка 2026-07-20).
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    own = {**OWN_STATE, "peers": [{"id": "winpc", "endpoint": "tcp://y:8710", "alive": True}]}
    link = FakeNodeLink(own=own, routes={})  # нет маршрута winpc:node → state=None
    text, keyboard = await build_swarm_view(link, _sub("nodes", "wake"), store=store)
    assert "/node_winpc — не отвечает" in text
    codes = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "st:wake:winpc" in codes


async def test_swarm_offline_node_no_button_without_cache(store):
    link = FakeNodeLink(routes=_routes())
    _, keyboard = await build_swarm_view(link, _sub("nodes", "wake"), store=store)
    codes = [b.callback_data for row in keyboard.inline_keyboard for b in row] if keyboard else []
    assert not any(c.startswith("st:wake:") for c in codes)


async def test_find_lan_waker_picks_matching_broadcast(store):
    own = {**OWN_STATE, "wake": ALFRED_WAKE}
    link = FakeNodeLink(own=own, routes=_routes())
    waker = await find_lan_waker(link, store, "winpc", "192.168.0.255")
    assert waker == "alfred"
    # Заодно освежил кэш живых нод, увиденных в этом же fan-out.
    assert await wake_state.cached(store, "alfred") == ALFRED_WAKE


async def test_find_lan_waker_none_without_matching_subnet(store):
    own = {**OWN_STATE, "wake": ALFRED_WAKE}
    link = FakeNodeLink(own=own, routes=_routes())
    assert await find_lan_waker(link, store, "winpc", "10.0.0.255") is None
