import asyncio

from sa_home_bot.bot import wake_state
from sa_home_bot.bot.node_view import NODE_DOWN_TEXT
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.swarm_view import REMOTE_STUB_TEXT, build_swarm_view
from sa_home_bot.config import WakeConfig
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.wake_core import PEER_TIMEOUT_S, find_lan_waker, wait_for_service
from tests.unit.support.fakes import FakeNodeLink as SupportNodeLink
from tests.unit.swarm_support import DeadNodeLink

OWN_STATE = {
    "node": "alfred",
    "version": "0.22.0",
    "services": [
        {"name": "monitor", "status": "running"},
        {"name": "telegram-bot", "status": "running"},
    ],
    "peers": [
        {"id": "arch-t480", "endpoint": "tcp://x:8710", "alive": True, "kind": "workstation"},
        {"id": "winpc", "endpoint": "tcp://y:8710", "alive": False, "kind": "workstation"},
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


class FakeNodeLink(SupportNodeLink):
    def __init__(self, own=None, routes=None, hang=()):
        super().__init__(own or OWN_STATE, routes=routes)
        self._hang = set(hang)
        self.requests: list[str] = []

    async def get_state(self, dst=None):
        key = f"{dst.node}:{dst.service}" if dst is not None else "own"
        self.requests.append(key)
        if key in self._hang:
            await asyncio.sleep(PEER_TIMEOUT_S + 30)
        return await super().get_state(dst)


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def _routes():
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
    assert "🟢 /node_arch_t480 · v0.21.0 · службы 1/1 · CPU 91°C · 🔔 1 · ⚠️" in text
    assert "⚪ /node_winpc — спит (это норма)" in text


async def test_swarm_offline_always_on_node_is_an_alarm():
    own = {
        **OWN_STATE,
        "peers": [{"id": "jeeves", "endpoint": "tcp://z:8710", "alive": False, "kind": "vps"}],
    }
    link = FakeNodeLink(own=own, routes={})
    text, _ = await build_swarm_view(link, _sub("nodes"))
    assert "🔴 /node_jeeves — не в сети" in text


async def test_swarm_orders_problem_active_asleep():
    own = {
        **OWN_STATE,
        "peers": [
            {"id": "arch-t480", "endpoint": "tcp://x:8710", "alive": True, "kind": "workstation"},
            {"id": "winpc", "endpoint": "tcp://y:8710", "alive": False, "kind": "workstation"},
            {"id": "jeeves", "endpoint": "tcp://z:8710", "alive": False, "kind": "vps"},
        ],
    }
    link = FakeNodeLink(own=own, routes=_routes())
    text, _ = await build_swarm_view(link, _sub("nodes"))
    lines = [ln for ln in text.splitlines() if ln[:1] in {"🟢", "🔴", "⚪"}]
    assert {ln.split()[1] for ln in lines[:2]} == {"/node_arch_t480", "/node_jeeves"}
    assert lines[2].startswith("🟢 /node_alfred")
    assert lines[3].startswith("⚪ /node_winpc")


async def test_swarm_dead_peer_gets_no_requests():
    link = FakeNodeLink(routes=_routes())
    await build_swarm_view(link, _sub("nodes"))
    assert not any(k.startswith("winpc:") for k in link.requests)


async def test_swarm_hung_peer_does_not_block(monkeypatch):
    monkeypatch.setattr("sa_home_bot.wake_core.PEER_TIMEOUT_S", 0.05)
    link = FakeNodeLink(routes=_routes(), hang=("arch-t480:node", "arch-t480:monitor"))
    text, _ = await asyncio.wait_for(build_swarm_view(link, _sub("nodes")), timeout=5)
    assert "/node_arch_t480 — не отвечает" in text


async def test_swarm_monitorless_node_marked():
    routes = {"alfred:monitor": OWN_MONITOR, "arch-t480:node": PEER_STATE}
    link = FakeNodeLink(routes=routes)
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
    text, keyboard = await build_swarm_view(DeadNodeLink(), _sub("nodes"))
    assert text == NODE_DOWN_TEXT
    assert keyboard is None


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


async def test_swarm_offline_vps_gets_no_wake_button(store):
    await wake_state.remember(store, "jeeves", WINPC_WAKE)
    own = {
        **OWN_STATE,
        "peers": [{"id": "jeeves", "endpoint": "tcp://z:8710", "alive": False, "kind": "vps"}],
    }
    link = FakeNodeLink(own=own, routes={})
    _, keyboard = await build_swarm_view(link, _sub("nodes", "wake"), store=store)
    codes = [b.callback_data for row in keyboard.inline_keyboard for b in row] if keyboard else []
    assert "st:wake:jeeves" not in codes


async def test_swarm_not_responding_node_gets_wake_button_when_cached(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    own = {**OWN_STATE, "peers": [{"id": "winpc", "endpoint": "tcp://y:8710", "alive": True}]}
    link = FakeNodeLink(own=own, routes={})
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
    assert await wake_state.cached(store, "alfred") == ALFRED_WAKE


async def test_find_lan_waker_none_without_matching_subnet(store):
    own = {**OWN_STATE, "wake": ALFRED_WAKE}
    link = FakeNodeLink(own=own, routes=_routes())
    assert await find_lan_waker(link, store, "winpc", "10.0.0.255") is None


async def test_wait_for_service_returns_true_immediately_when_reachable():
    link = FakeNodeLink(routes={"winpc:llm": {"asleep": False}})
    assert await wait_for_service(link, "winpc", "llm", timeout_s=5.0, interval_s=0.01) is True
    assert link.requests == ["winpc:llm"]


async def test_wait_for_service_polls_until_available():
    calls = {"n": 0}
    link = FakeNodeLink(routes={})

    async def flaky_get_state(dst=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServiceUnavailableError("ещё нет")
        return {"asleep": False}

    link.get_state = flaky_get_state
    ok = await wait_for_service(link, "winpc", "llm", timeout_s=5.0, interval_s=0.01)
    assert ok is True
    assert calls["n"] == 3


async def test_wait_for_service_gives_up_after_timeout():
    link = FakeNodeLink(routes={})
    ok = await wait_for_service(link, "winpc", "llm", timeout_s=0.05, interval_s=0.01)
    assert ok is False
