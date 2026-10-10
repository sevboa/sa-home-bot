"""Канарейка Этапа 59 (59.C, решение владельца 2026-10-10): новое видят только
гости из ``llm.interactives_canary_user_ids``, а для остальных поведение бота
прежнее — тесты ниже держат это «байт-в-байт»."""

from __future__ import annotations

import dataclasses
import re

import pytest
import pytest_asyncio

from sa_home_bot.bot.interactives import engine, radio
from sa_home_bot.bot.interactives.base import STATUS_OFFERED, InteractiveStore, Run
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.interactives.transylvania import Transylvania
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

CANARY = 701
GUEST = 702
GHOST_ID = "ghost"
GHOST_TEXT = "Альфред, тут призрак?"


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, **_kw):
        self.sent.append((chat_id, text))
        return 1000 + len(self.sent)


class FakeLink:
    async def command(self, action, args, dst=None, timeout=None):
        return {"response": "{}"}


async def _clear() -> int:
    return 0


def _make(store, *, canary=(CANARY,)):
    svc = Interactives(
        store,
        FakeNotifier(),
        Settings(llm=LlmConfig(model="m", interactives_canary_user_ids=list(canary))),
        lambda: FakeLink(),
        transylvania=Transylvania(fetch=_clear),
    )
    return svc


@pytest.fixture
def ghost(monkeypatch):
    """Канареечный сценарий-пустышка в REGISTRY (cellar появится позже)."""
    scenario = dataclasses.replace(
        radio.RADIO,
        id=GHOST_ID,
        canary=True,
        offer_text="Вы слышите шорох?",
        trigger_re=re.compile("призрак"),
    )
    monkeypatch.setitem(engine.REGISTRY, GHOST_ID, scenario)
    return scenario


async def _dump(store) -> dict[str, str | None]:
    return {key: await store.get_state(key) for key in await store.state_keys("")}


def test_config_default_has_no_canaries():
    cfg = LlmConfig(model="m")
    assert cfg.interactives_canary_user_ids == []
    assert cfg.interactives_return_idle_h == 2.0
    assert cfg.interactives_spontaneous_chance == pytest.approx(1 / 8)


async def test_canary_ok_only_for_listed_users(store):
    svc = _make(store)
    assert svc.canary_ok(CANARY)
    assert not svc.canary_ok(GUEST)
    assert not svc.canary_ok(None)
    assert not _make(store, canary=()).canary_ok(CANARY)


async def test_canary_scenario_exists_only_for_canaries(store, ghost):
    svc = _make(store)
    assert [s.id for s in svc.scenarios_for(CANARY)] == [radio.SCENARIO_ID, GHOST_ID]
    assert [s.id for s in svc.scenarios_for(GUEST)] == [radio.SCENARIO_ID]


async def test_canary_scenario_not_offered_to_others(store, ghost):
    svc = _make(store)
    plan = await svc.before_turn(GUEST, GUEST, GHOST_TEXT, is_private=True)
    assert plan.scenario is None and not plan.offered
    assert await svc._state.load_run(GUEST, GHOST_ID) is None
    plan = await svc.before_turn(CANARY, CANARY, GHOST_TEXT, is_private=True)
    assert plan.offered and plan.scenario == GHOST_ID
    assert (await svc._state.load_run(CANARY, GHOST_ID)).status == STATUS_OFFERED


async def test_forged_click_on_canary_scenario_is_refused(store, ghost):
    svc = _make(store)
    run = Run(scenario=GHOST_ID, chat_id=GUEST, user_id=GUEST, status=STATUS_OFFERED)
    await InteractiveStore(store).save_run(run)
    answer, text, drop = await svc.handle_click(GUEST, GUEST, GHOST_ID, engine.BTN_PLAY)
    assert (answer, text, drop) == ("Эта форма не для вас.", None, False)
    assert (await svc._state.load_run(GUEST, GHOST_ID)).status == STATUS_OFFERED


async def test_radio_complaint_unchanged_for_everyone(store, ghost):
    """Жалоба на речь предлагает радио и гостю вне списка, и канарейке —
    ровно прежними ключами app_state."""
    for user in (GUEST, CANARY):
        svc = _make(store)
        plan = await svc.before_turn(user, user, "Альфред, ты картавишь!", is_private=True)
        assert plan.offered and plan.scenario == radio.SCENARIO_ID
    keys = await store.state_keys("")
    assert keys == sorted(
        [
            f"interactive_run:{GUEST}:radio",
            f"interactive_run:{CANARY}:radio",
        ]
    )


async def test_plain_turn_for_non_canary_writes_nothing(store, ghost):
    svc = _make(store)
    before = await _dump(store)
    for text in ("Какая завтра погода?", GHOST_TEXT, "Как дела?", "Расскажи анекдот"):
        plan = await svc.before_turn(GUEST, GUEST, text, is_private=True)
        assert plan.scenario is None and plan.note is None
    assert await _dump(store) == before == {}


def test_run_json_without_world_is_the_old_format():
    run = Run(scenario="radio", chat_id=1, user_id=1)
    assert "world" not in run.to_json()
    run.world["x"] = 1
    assert Run.from_json(run.to_json()).world == {"x": 1}
    # Старый JSON (до Этапа 59) читается: поля world в нём нет.
    old = Run.from_json(Run(scenario="radio", chat_id=1, user_id=1).to_json())
    assert old.world == {}
