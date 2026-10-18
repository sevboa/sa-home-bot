from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import re

import pytest

from sa_home_bot.bot.interactives import cellar, keyboards, radio, registry
from sa_home_bot.bot.interactives.base import STATUS_OFFERED, InteractiveStore, Run
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.interactives.transylvania import Transylvania
from sa_home_bot.config import LlmConfig, Settings
from tests.unit.interactives_kit import clear_sky
from tests.unit.support.factories import png_bytes
from tests.unit.support.fakes import FakeNotifier

CANARY = 701
GUEST = 702
GHOST_ID = "ghost"
GHOST_TEXT = "Альфред, тут призрак?"


class FakeLink:
    async def command(self, action, args, dst=None, timeout=None):
        return {"response": "{}"}


def _make(store, *, canary=(CANARY,)):
    svc = Interactives(
        store,
        FakeNotifier(),
        Settings(llm=LlmConfig(model="m", interactives_canary_user_ids=list(canary))),
        lambda: FakeLink(),
        transylvania=Transylvania(fetch=clear_sky),
    )
    return svc


@pytest.fixture
def ghost(monkeypatch):
    scenario = dataclasses.replace(
        radio.RADIO,
        id=GHOST_ID,
        canary=True,
        offer_text="Вы слышите шорох?",
        trigger_re=re.compile("призрак"),
    )
    monkeypatch.setitem(registry.REGISTRY, GHOST_ID, scenario)
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
    assert [s.id for s in svc.scenarios_for(CANARY)] == [
        radio.SCENARIO_ID,
        cellar.SCENARIO_ID,
        "catacombs",
        GHOST_ID,
    ]
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
    answer, text, drop = await svc.handle_click(GUEST, GUEST, GHOST_ID, keyboards.BTN_PLAY)
    assert (answer, text, drop) == ("Эта форма не для вас.", None, False)
    assert (await svc._state.load_run(GUEST, GHOST_ID)).status == STATUS_OFFERED


async def test_radio_complaint_unchanged_for_everyone(store, ghost):
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
    old = Run.from_json(Run(scenario="radio", chat_id=1, user_id=1).to_json())
    assert old.world == {}


class SceneLink:
    def __init__(self) -> None:
        self.generated: list[dict] = []
        self.chat_roles: list[object] = []

    async def command(self, action, args, dst=None, timeout=None):
        if action == "chat":
            self.chat_roles.append(args.get("role"))
            if args.get("role") == "director":
                return {
                    "response": json.dumps(
                        {
                            "active": True,
                            "stage": 1,
                            "effect": "В эфире шорох.",
                            "cabinet_add": ["чучело совы на шкафу"],
                            "photo": True,
                        },
                        ensure_ascii=False,
                    )
                }
            return {"response": "Готово, сэр."}
        if action == "generate_image":
            self.generated.append(args)
            return {
                "png_b64": base64.b64encode(png_bytes()).decode(),
                "width": 8,
                "height": 8,
                "prompt": "p",
                "seed": 7,
            }
        if action == "chat_progress":
            return {"partial": "draw", "done": True}
        if action == "item_portrait":
            return {
                "png_b64": base64.b64encode(png_bytes()).decode(),
                "width": 8,
                "height": 8,
                "seed": args["seed"],
                "full_prompt": args["prompt"],
                "seen": "Старое радио.",
                "missing": [],
            }
        raise AssertionError(action)


NEW_KEYS_PREFIXES = (
    "alfred_at:",
    "location_searched:",
    "interactives_plain:",
    "user_effect:cellar_unlocked",
    "user_effect:catacombs_open",
    "catacombs:",
)


async def test_radio_and_cabinet_flow_for_non_canary_is_the_old_one(store):
    notifier, link = FakeNotifier(), SceneLink()
    svc = Interactives(
        store,
        notifier,
        Settings(llm=LlmConfig(model="m", interactives_canary_user_ids=[CANARY])),
        lambda: link,
        transylvania=Transylvania(fetch=clear_sky),
        rng=lambda: 0.0,
    )

    async def speak(chat_id, directive, where):
        return None

    svc._speak = speak
    plan = await svc.before_turn(GUEST, GUEST, "Альфред, ты картавишь!", is_private=True)
    assert plan.offered and plan.scenario == radio.SCENARIO_ID
    await svc.after_turn(plan, "Я говогю чисто.", dialogue_id=77)
    await svc.flush_forms(GUEST, plan, dialogue_id=77)
    _, text, markup = notifier.sent_with_markup[-1]
    assert text == radio.RADIO.offer_text
    assert [b.text for row in markup.inline_keyboard for b in row] == ["Да", "Нет"]
    await svc.handle_click(GUEST, GUEST, "radio", keyboards.BTN_PLAY)
    plan = await svc.before_turn(GUEST, GUEST, "Слышу «пгивет»", is_private=True)
    assert plan.scene and "За окном" not in plan.note
    await svc.after_turn(plan, "Проверю антенну.", dialogue_id=77)
    await asyncio.gather(*list(svc._photo_tasks))
    shot = link.generated[-1]
    assert "gothic study" in shot["description"] and shot["light"]
    assert await svc.tool_take_photo(GUEST, GUEST, {"focus": "полки"})
    await asyncio.gather(*list(svc._photo_tasks))
    assert "study" in link.generated[-1]["context"]
    assert len(notifier.photos) == 2
    keys = await store.state_keys("")
    assert not [k for k in keys if k.startswith(NEW_KEYS_PREFIXES)]
    assert [k for k in keys if k.startswith("location:")] == [f"location:cabinet:{GUEST}"]
    assert "world" not in await store.get_state(f"interactive_run:{GUEST}:radio")
    room = await svc._room(GUEST, GUEST)
    assert type(room).__name__ == "Cabinet"
    assert await svc._where_ru(room, GUEST, in_scene=True) == room.describe_ru()


async def test_old_run_json_without_new_fields_still_loads(store):
    old = {
        "scenario": "radio",
        "chat_id": GUEST,
        "user_id": GUEST,
        "status": "active",
        "stage": 2,
        "turns_total": 5,
    }
    await store.set_state(f"interactive_run:{GUEST}:radio", json.dumps(old))
    run = await InteractiveStore(store).load_run(GUEST, "radio")
    assert run is not None and run.stage == 2 and run.world == {}
