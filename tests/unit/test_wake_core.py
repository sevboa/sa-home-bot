"""Ядро пробуждения роя: откуда берутся реквизиты WoL и как служба доводится
до готовности отвечать (wake_core.py).

Живая находка 2026-07-27 (инцидент 19:34-19:39): служба tasks не могла
разбудить winpc НИ РАЗУ — её собственный кэш wake-реквизитов был пуст, а
наполнялся он только внутри find_lan_waker, куда wake_swarm_node_core
доходит лишь ПОСЛЕ успешного чтения того же кэша. Тесты ниже фиксируют
разрыв этой курицы-яйца и сценарий готовности целиком.
"""

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

# Так выглядит get_state своей ноды, когда winpc уже уснула: связи с ней нет
# (alive=False), но реквизиты для WoL известны с её последнего hello.
OWN_STATE = {
    "node": "alfred",
    "wake": ALFRED_WAKE,
    "peers": [{"id": "winpc", "endpoint": "tcp://y:8710", "alive": False, "wake": WINPC_WAKE}],
}


class FakeNodeLink:
    """``routes`` — что отвечает get_state для конкретных dst; ключ
    ``"winpc:llm"``. Отсутствие ключа = служба недоступна."""

    display_name = "нода"

    def __init__(self, own, routes=None, *, wake_reveals=None):
        self._own = own
        self._routes = dict(routes or {})
        # Что появляется в routes после успешной отправки magic packet —
        # так тест изображает проснувшуюся ноду.
        self._wake_reveals = wake_reveals or {}
        self.commands: list[tuple[str, dict, str | None]] = []

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        key = f"{dst.node}:{dst.service}"
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
    link = FakeNodeLink({"node": "alfred", "peers": []})  # рой ничего не подскажет
    assert await wake_core.resolve_wake_info(link, store, "winpc") == WINPC_WAKE


async def test_resolve_wake_info_falls_back_to_swarm_and_persists(store):
    # Кэш пуст (ровно случай службы tasks) — реквизиты берём у своей ноды,
    # которая помнит их с hello соседа, и сразу сохраняем.
    link = FakeNodeLink(OWN_STATE)
    assert await wake_core.resolve_wake_info(link, store, "winpc") == WINPC_WAKE
    assert await wake_state.cached(store, "winpc") == WINPC_WAKE


async def test_resolve_wake_info_none_for_unknown_node(store):
    link = FakeNodeLink(OWN_STATE)
    assert await wake_core.resolve_wake_info(link, store, "jeeves") is None


async def test_resolve_wake_info_none_when_peer_has_no_ethernet(store):
    # Wi-Fi-нода докладывает wake=None — будить нечем, это не ошибка.
    link = FakeNodeLink({"node": "alfred", "peers": [{"id": "arch-t480", "wake": None}]})
    assert await wake_core.resolve_wake_info(link, store, "arch-t480") is None


async def test_ensure_service_ready_noop_when_already_warm(store):
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": False}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY
    assert link.commands == []  # ни WoL, ни прогрева не потребовалось


async def test_ensure_service_ready_warms_up_sleeping_service(store):
    # Нода на связи, но модель выгружена по простою — это не повод слать WoL.
    link = FakeNodeLink(OWN_STATE, routes={"winpc:llm": {"asleep": True}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY
    assert [c[0] for c in link.commands] == ["warmup"]


async def test_ensure_service_ready_wakes_unreachable_node(store):
    # Главный сценарий инцидента: кэш пуст, нода спит — magic packet всё
    # равно должен уйти (реквизиты придут из роя), а после подъёма служба
    # должна быть прогрета.
    link = FakeNodeLink(OWN_STATE, wake_reveals={"winpc:llm": {"asleep": True}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY
    assert link.commands[0] == ("send_wol", {"mac": WINPC_WAKE["mac"]}, "alfred")
    assert link.commands[-1][0] == "warmup"


async def test_ensure_service_ready_unreachable_when_node_never_comes_up(store):
    # WoL ушёл, но служба так и не появилась — ждём не дольше бюджета.
    link = FakeNodeLink(OWN_STATE)
    outcome = await wake_core.ensure_service_ready(link, store, "winpc", "llm", wake_timeout_s=0.0)
    assert outcome == wake_core.UNREACHABLE
    assert link.commands[0][0] == "send_wol"


async def test_ensure_service_ready_unreachable_without_wake_info(store):
    # Ни кэша, ни подсказки от роя — будить нечем, но и падать нельзя.
    link = FakeNodeLink({"node": "alfred", "peers": []})
    outcome = await wake_core.ensure_service_ready(link, store, "winpc", "llm")
    assert outcome == wake_core.UNREACHABLE
    assert link.commands == []


async def test_try_warmup_tolerates_services_without_warmup(store):
    # Не всякая служба объявляет warmup — раз она отвечает, этого достаточно.
    class NoWarmup(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            raise ProtoError(ERR_UNKNOWN_ACTION, "нет такого действия")

    link = NoWarmup(OWN_STATE, routes={"winpc:tasks": {"asleep": True}})
    assert await wake_core.ensure_service_ready(link, store, "winpc", "tasks") == wake_core.READY


# --- прогрев: бюджет и наблюдаемость (живой сбой 2026-07-30) ----------------


class WarmupSpy(FakeNodeLink):
    """Служба на связи, но прогрев отвечает так, как задано в тесте."""

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
    """Замер 2026-07-30: холодный старт 35B-модели — 201 с, а прогрев ждал
    180 с и сдавался, хотя модель бы поднялась."""
    assert wake_core.WARMUP_TIMEOUT_S >= 300.0

    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}})
    await wake_core.ensure_service_ready(link, store, "winpc", "llm")
    assert link.warmup_timeouts == [wake_core.WARMUP_TIMEOUT_S]


async def test_warmup_timeout_can_be_narrowed_by_caller(store):
    """На сроке задачи ждать дольше остатка бюджета бессмысленно."""
    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}})
    await wake_core.ensure_service_ready(
        link, store, "winpc", "llm", warmup_timeout_s=42.0
    )
    assert link.warmup_timeouts == [42.0]


async def test_service_without_warmup_is_still_ready(store):
    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}}, outcome="unknown")
    assert await wake_core.ensure_service_ready(link, store, "winpc", "llm") == wake_core.READY


@pytest.mark.parametrize("outcome", ["error", "timeout"])
async def test_failed_warmup_is_reported_with_its_reason(store, caplog, outcome):
    """Раньше причина оставалась только в логах службы на другой машине —
    на alfred было видно лишь «прогрев не удался»."""
    link = WarmupSpy(OWN_STATE, routes={"winpc:llm": {"asleep": True}}, outcome=outcome)
    with caplog.at_level("WARNING"):
        result = await wake_core.ensure_service_ready(link, store, "winpc", "llm")
    assert result == wake_core.WARMUP_FAILED
    text = caplog.text
    assert "winpc" in text and "llm" in text
    # В логе не просто «не удалось», а что именно ответила служба.
    assert ("Ollama" in text) or ("TimeoutError" in text)
