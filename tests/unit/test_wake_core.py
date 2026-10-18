from __future__ import annotations

import pytest
import pytest_asyncio

from sa_home_bot import wake_core
from sa_home_bot.bot import wake_state
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, ProtoError

ALFRED_WAKE = {"mac": "7c:83:34:b4:59:ac", "ip": "192.168.0.100", "broadcast": "192.168.0.255"}
WINPC_WAKE = {"mac": "04:92:26:da:63:7c", "ip": "192.168.0.105", "broadcast": "192.168.0.255"}

OWN_STATE = {
    "node": "alfred",
    "wake": ALFRED_WAKE,
    "peers": [{"id": "winpc", "endpoint": "tcp://y:8710", "alive": False, "wake": WINPC_WAKE}],
}


class FakeNodeLink:
    display_name = "нода"

    def __init__(self, own, routes=None, *, wake_reveals=None):
        self._own = own
        self._routes = dict(routes or {})
        self._wake_reveals = wake_reveals or {}
        self.commands: list[tuple[str, dict, str | None]] = []
        self.requests: list[str] = []

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        key = f"{dst.node}:{dst.service}"
        self.requests.append(key)
        if key in self._routes:
            return self._routes[key]
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append((action, args or {}, dst.node if dst else None))
        if action == "send_wol":
            self._routes.update(self._wake_reveals)
            return {"sent": True}
        if action == "warmup":
            return {"asleep": False}
        raise ProtoError(ERR_UNKNOWN_ACTION, f"нет действия {action}")


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def test_resolve_wake_info_prefers_own_cache(store):
    await wake_state.remember(store, "winpc", WINPC_WAKE)
    link = FakeNodeLink({"node": "alfred", "peers": []})
    assert await wake_core.resolve_wake_info(link, store, "winpc") == WINPC_WAKE


async def test_resolve_wake_info_falls_back_to_swarm_and_persists(store):
    link = FakeNodeLink(OWN_STATE)
    assert await wake_core.resolve_wake_info(link, store, "winpc") == WINPC_WAKE
    assert await wake_state.cached(store, "winpc") == WINPC_WAKE


async def test_resolve_wake_info_none_for_unknown_node(store):
    link = FakeNodeLink(OWN_STATE)
    assert await wake_core.resolve_wake_info(link, store, "jeeves") is None


async def test_resolve_wake_info_none_when_peer_has_no_ethernet(store):
    link = FakeNodeLink({"node": "alfred", "peers": [{"id": "arch-t480", "wake": None}]})
    assert await wake_core.resolve_wake_info(link, store, "arch-t480") is None


async def test_ensure_service_ready_noop_when_already_warm(store):
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY
    assert link.commands == []


async def test_ensure_service_ready_warms_up_sleeping_service(store):
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": True}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY
    assert [c[0] for c in link.commands] == ["warmup"]


async def test_ensure_service_ready_wakes_unreachable_node(store):
    link = FakeNodeLink(OWN_STATE, wake_reveals={"winpc:llm": {"asleep": True}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY
    assert link.commands[0] == ("send_wol", {"mac": WINPC_WAKE["mac"]}, "alfred")
    assert link.commands[-1][0] == "warmup"


async def test_ensure_service_ready_unreachable_when_node_never_comes_up(store):
    link = FakeNodeLink(OWN_STATE)
    outcome = await wake_core.ensure_service_ready(link, store, "winpc", "llm", wake_timeout_s=0.0)
    assert outcome == wake_core.UNREACHABLE
    assert link.commands[0][0] == "send_wol"


async def test_ensure_service_ready_unreachable_without_wake_info(store):
    link = FakeNodeLink({"node": "alfred", "peers": []})
    outcome = await wake_core.ensure_service_ready(link, store, "winpc", "llm")
    assert outcome == wake_core.UNREACHABLE
    assert link.commands == []


async def test_try_warmup_tolerates_services_without_warmup(store):
    class NoWarmup(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            raise ProtoError(ERR_UNKNOWN_ACTION, "нет такого действия")

    link = NoWarmup(OWN_STATE, routes={"winpc:tasks": {"asleep": True}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "tasks") == wake_core.READY


class WarmupSpy(FakeNodeLink):
    def __init__(self, own, routes=None, *, outcome="ok"):
        super().__init__(own, routes)
        self._outcome = outcome
        self.warmup_timeouts: list[float | None] = []

    async def command(self, action, args=None, dst=None, timeout=None):
        if action == "warmup":
            self.commands.append((action, args or {}, dst.node if dst else None))
            self.warmup_timeouts.append(timeout)
            if self._outcome == "ok":
                return {}
            if self._outcome == "unknown":
                raise ProtoError(ERR_UNKNOWN_ACTION, "нет действия warmup")
            if self._outcome == "timeout":
                raise TimeoutError("не дождались")
            raise ProtoError("internal", "Ollama не поднялась после прогрева WSL/контейнера")
        return await super().command(action, args, dst, timeout)


async def test_warmup_budget_outlives_a_cold_model_load(store):
    assert wake_core.WARMUP_TIMEOUT_S >= 300.0

    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}})
    await wake_core.ensure_service_ready(link, store, "winpc", "llm")
    assert link.warmup_timeouts == [wake_core.WARMUP_TIMEOUT_S]


async def test_warmup_timeout_can_be_narrowed_by_caller(store):
    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}})
    await wake_core.ensure_service_ready(link, store, "winpc", "llm", warmup_timeout_s=42.0)
    assert link.warmup_timeouts == [42.0]


async def test_service_without_warmup_is_still_ready(store):
    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}}, outcome="unknown")
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY


@pytest.mark.parametrize("outcome", ["error", "timeout"])
async def test_failed_warmup_is_reported_with_its_reason(store, caplog, outcome):
    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}}, outcome=outcome)
    with caplog.at_level("WARNING"):
        result = await wake_core.ensure_service_ready(link, store, "winpc", "llm")
    assert result == wake_core.WARMUP_FAILED
    text = caplog.text
    assert "winpc" in text and "llm" in text
    assert ("Ollama" in text) or ("TimeoutError" in text)


async def test_find_lan_waker_picks_matching_broadcast(store):
    link = FakeNodeLink(OWN_STATE)
    waker = await wake_core.find_lan_waker(link, store, "winpc", "192.168.0.255")
    assert waker == "alfred"
    assert await wake_state.cached(store, "alfred") == ALFRED_WAKE


async def test_find_lan_waker_none_without_matching_subnet(store):
    link = FakeNodeLink(OWN_STATE)
    assert await wake_core.find_lan_waker(link, store, "winpc", "10.0.0.255") is None


async def test_wait_for_service_returns_true_immediately_when_reachable():
    link = FakeNodeLink({}, {"winpc:llm": {"asleep": False}})
    assert (
        await wake_core.wait_for_service(link, "winpc", "llm", timeout_s=5.0, interval_s=0.01)
        is True
    )
    assert link.requests == ["winpc:llm"]


async def test_wait_for_service_polls_until_available():
    calls = {"n": 0}
    link = FakeNodeLink({})

    async def flaky_get_state(dst=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ServiceUnavailableError("ещё нет")
        return {"asleep": False}

    link.get_state = flaky_get_state
    ok = await wake_core.wait_for_service(link, "winpc", "llm", timeout_s=5.0, interval_s=0.01)
    assert ok is True
    assert calls["n"] == 3


async def test_wait_for_service_gives_up_after_timeout():
    link = FakeNodeLink({})
    ok = await wake_core.wait_for_service(link, "winpc", "llm", timeout_s=0.05, interval_s=0.01)
    assert ok is False
