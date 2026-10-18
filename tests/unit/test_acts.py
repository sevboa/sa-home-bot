from __future__ import annotations

import json

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.interactives import (
    acts,
    cellar,
    director,
    keyboards,
    photo_text,
    places,
    underground,
)
from sa_home_bot.bot.interactives.base import STATUS_ACTIVE, STATUS_DONE, Run
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from tests.unit.test_cellar import (
    CANARY,
    GUEST,
    Dice,
    _act_reply,
    _active,
    _dir,
    _make,
    _run,
    _to_hatch,
    _to_key_hunt,
    _turn,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def _begin(svc, text):
    return await svc.before_turn(CANARY, CANARY, text, is_private=True)


async def test_chain_of_acts_moves_stage_immediately_and_logs_each(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    plan = await _begin(svc, "Да, снимайте доски")
    link.director_replies = [
        _act_reply(
            "Доски поддаются, под ними тяжёлый ржавый замок.",
            stage=1,
            facts_add=["доски сняты", "замок заперт"],
        ),
        _act_reply("Замок неподвижен, как ни дёргай.", stage=1),
    ]
    first = await svc.tool_act(CANARY, CANARY, "сорвать доски с двери")
    assert "Доски поддаются" in first
    run = await _run(store)
    assert run.stage == 1 and run.world["facts"] == ["доски сняты", "замок заперт"]
    sent = link.director_inputs[0]
    assert "Да, снимайте доски" in sent and "Текущая стадия: 0" in sent
    assert "сорвать доски с двери" in sent
    second = await svc.tool_act(CANARY, CANARY, "дёрнуть замок")
    assert "Замок неподвижен" in second
    assert "доски сняты" in link.director_inputs[1]
    assert "Текущая стадия: 1" in link.director_inputs[1]
    assert (await _run(store)).transcript[-5:] == [
        "Гость: Да, снимайте доски",
        "Действие: сорвать доски с двери",
        "Итог: Доски поддаются, под ними тяжёлый ржавый замок.",
        "Действие: дёрнуть замок",
        "Итог: Замок неподвижен, как ни дёргай.",
    ]
    link.director_replies = [_dir(stage=1, effect="Придуманное после хода.")]
    await svc.after_turn(plan, "Доски сняты, сэр.", dialogue_id=1)
    run = await _run(store)
    assert run.transcript.count("Гость: Да, снимайте доски") == 1
    assert run.transcript[-1] == "Альфред: Доски сняты, сэр."
    assert run.pending_effect is None
    assert "Придуманное после хода." not in " ".join(run.transcript)
    assert "effect null" in link.director_inputs[-1]


async def test_go_during_key_hunt_is_an_act_through_the_director(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    await _begin(svc, "Сходи на кухню")
    link.director_replies = [
        _act_reply("Альфред дошёл до кухни."),
        _act_reply("Дверь подвала всё так же заперта."),
    ]
    assert (await svc.tool_go(CANARY, CANARY, "kitchen")).startswith(
        "Итог: Альфред дошёл до кухни."
    )
    assert "пойти: kitchen" in link.director_inputs[0]
    assert (await svc.tool_go(CANARY, CANARY, "cellar")).startswith("Итог: Дверь подвала")
    assert "пойти к двери подвала" in link.director_inputs[1]
    assert await svc.tool_go(CANARY, CANARY, "cabinet") == underground.TOOL_GO_HOME
    assert (await _run(store)).status == STATUS_ACTIVE


async def test_stage_never_jumps_more_than_one_by_act(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    await _begin(svc, "Снимай")
    link.director_replies = [_act_reply("Доски упали.", stage=4)]
    await svc.tool_act(CANARY, CANARY, "сорвать доски")
    assert (await _run(store)).stage == 1


async def test_stage_change_by_act_blocks_a_second_jump_in_the_same_turn(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    plan = await _begin(svc, "Снимай")
    link.director_replies = [_act_reply("Доски упали.", stage=1)]
    await svc.tool_act(CANARY, CANARY, "сорвать доски")
    link.director_replies = [_dir(stage=2)]
    await svc.after_turn(plan, "Готово.", dialogue_id=1)
    assert (await _run(store)).stage == 1


async def test_act_result_is_quoted_back_to_the_model(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    await _begin(svc, "Снимай")
    link.director_replies = [_act_reply("Доски упали.")]
    text = await svc.tool_act(CANARY, CANARY, "сорвать доски")
    assert text.startswith("Итог: Доски упали.") and "ничего не добавляя" in text


async def test_search_like_act_in_key_hunt_takes_its_outcome_from_the_code(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    await _begin(svc, "Поищи в кладовой")
    link.director_replies = [_act_reply("В ящике лежит ключ!", stage=3)]
    await svc.tool_act(CANARY, CANARY, "порыскать в шкафу кладовой")
    run = await _run(store)
    assert "ключа здесь нет" in link.director_inputs[-1].lower()
    assert run.stage == 2 and not run.world.get("key_found")
    assert run.world["key_searches"] == 1
    assert "Действие: порыскать: шкафу кладовой" in run.transcript[-2]


async def test_look_over_is_not_a_search_on_stage_zero(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    await _begin(svc, "Осмотри дверь")
    link.director_replies = [_act_reply("Дверь как дверь.")]
    await svc.tool_act(CANARY, CANARY, "осмотреть дверь")
    assert "key_searches" not in (await _run(store)).world


async def test_look_over_is_a_search_on_key_hunt_stage(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    await _begin(svc, "Осмотри буфет")
    link.director_replies = [_act_reply("Буфет пуст.")]
    await svc.tool_act(CANARY, CANARY, "осмотреть буфет")
    assert (await _run(store)).world["key_searches"] == 1


async def test_key_found_by_search_act_moves_to_stage_three_at_once(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    run = await _run(store)
    run.world["key_searches"] = 4
    await svc._state.save_run(run)
    svc._rng = Dice(0.5)
    await _begin(svc, "Ищи дальше")
    link.director_replies = [_act_reply("Ключ ржавеет на гвозде.")]
    await svc.tool_act(CANARY, CANARY, "обыскать полку")
    run = await _run(store)
    assert run.stage == 3 and run.world["key_found"]
    assert await svc._state.get_effect("cellar_unlocked", CANARY) == "1"
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR
    assert len(await store.items_of(CANARY, "wine")) == 1


async def test_fifth_act_in_a_turn_stops_without_asking_the_director(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    await _begin(svc, "Работай")
    link.director_replies = [_act_reply(f"Итог {i}.") for i in range(1, 5)]
    for i in range(4):
        out = await svc.tool_act(CANARY, CANARY, f"действие {i}")
        assert f"Итог {i + 1}." in out
    asked = len(link.director_inputs)
    out = await svc.tool_act(CANARY, CANARY, "ещё действие")
    assert out == acts.TOOL_ACT_LIMIT and "спроси" in out
    assert len(link.director_inputs) == asked
    await _begin(svc, "Дальше")
    link.director_replies = [_act_reply("Снова можно.")]
    assert "Снова можно." in await svc.tool_act(CANARY, CANARY, "действие")


async def test_facts_are_capped_at_twelve_and_removed_on_request(store):
    run = Run(scenario="cellar", chat_id=1, user_id=1)
    for i in range(15):
        director.update_facts(run, (f"факт {i}",), ())
    assert run.world["facts"] == [f"факт {i}" for i in range(3, 15)]
    director.update_facts(run, ("новый",), ("факт 5",))
    assert "факт 5" not in run.world["facts"] and run.world["facts"][-1] == "новый"
    assert len(run.world["facts"]) == 12
    director.update_facts(run, (), tuple(run.world["facts"]))
    assert "facts" not in run.world


async def test_facts_are_seen_by_alfred_the_director_and_the_snapshot(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    await _begin(svc, "Снимай")
    link.director_replies = [_act_reply("Доски упали.", stage=1, facts_add=["доски сняты"])]
    await svc.tool_act(CANARY, CANARY, "сорвать доски")
    plan = await _begin(svc, "Ну?")
    assert "Что вокруг уже так (не противоречь): доски сняты." in plan.note
    link.director_replies = [_dir(stage=1)]
    await svc.after_turn(plan, "Ну?", dialogue_id=1)
    assert "- доски сняты" in link.director_inputs[-1]
    description, _ = photo_text.photo_description(
        await svc._room(CANARY, CANARY),
        await svc._transylvania.outside(svc._now()),
        "",
        None,
        state=await svc._scene_facts(CANARY, CANARY),
    )
    assert "Current state: доски сняты." in description


async def test_scene_without_facts_changes_nothing_in_notes_or_snapshots(store):
    svc, _, _ = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    plan = await _begin(svc, "Ну?")
    assert "уже так" not in plan.note
    assert await svc._scene_facts(CANARY, CANARY) is None


async def test_move_to_by_act_on_key_hunt_stage(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    await _begin(svc, "Иди на кухню")
    kitchen = {
        "name": "кухня",
        "where": "на кухне",
        "canon_ru": "Большая холодная кухня замка.",
        "canon_en": "large castle kitchen",
    }
    link.director_replies = [_act_reply("Ты на кухне.", move_to=kitchen)]
    await svc.tool_act(CANARY, CANARY, "пойти на кухню")
    assert places.is_temp((await places.get_at(store, CANARY)).place)


async def test_finale_by_act_sets_flags_now_and_finishes_next_turn(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_hatch(svc, store, link)
    run = await _run(store)
    run.turns_total = 10
    await svc._state.save_run(run)
    plan = await _begin(svc, "Поищи ещё вина")
    link.director_replies = [
        _act_reply("За бочкой зияет лаз.", stage=4, finale=True, finale_fault="лаз за бочкой")
    ]
    await svc.tool_act(CANARY, CANARY, "отодвинуть бочку")
    run = await _run(store)
    assert run.finale and run.finale_fault == "лаз за бочкой"
    assert await svc._state.is_completed("cellar", CANARY)
    assert await svc._state.get_effect("catacombs_open", CANARY) == "1"
    link.director_replies = [_dir(stage=4)]
    await svc.after_turn(plan, "Лаз, сэр.", dialogue_id=1)
    assert (await _run(store)).status == STATUS_ACTIVE
    plan = await _begin(svc, "И что?")
    assert "лаз за бочкой" in plan.note
    link.director_replies = [_dir(stage=4)]
    await svc.after_turn(plan, "Лаз.", dialogue_id=1)
    assert (await _run(store)).status == STATUS_DONE


async def test_turn_without_act_keeps_the_directors_surroundings_effect(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    link.director_replies = [_dir(stage=0, effect="Где-то капает вода.")]
    await _turn(svc, "Ну что там?")
    run = await _run(store)
    assert run.pending_effect == "Где-то капает вода."
    assert "САМО" in link.director_inputs[-1]


async def test_without_a_director_the_act_gets_a_neutral_result(store):
    svc, _, _ = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    svc._get_node_link = lambda: None
    await _begin(svc, "Снимай")
    out = await svc.tool_act(CANARY, CANARY, "сорвать доски")
    assert acts.TOOL_ACT_NO_DIRECTOR in out
    assert (await _run(store)).stage == 0


async def test_act_refused_for_non_canary_and_group_chats(store):
    svc, _, _ = _make(store)
    assert await svc.tool_act(GUEST, GUEST, "открыть") == acts.TOOL_ACT_UNAVAILABLE
    assert await svc.tool_act(-100, CANARY, "открыть") == acts.TOOL_ACT_UNAVAILABLE
    assert await store.state_keys("") == []


async def test_act_in_the_study_outside_a_scene_has_nothing_to_do(store):
    svc, _, link = _make(store)
    assert await svc.tool_act(CANARY, CANARY, "открыть ящик") == acts.TOOL_ACT_CABINET
    assert link.director_inputs == []


async def test_act_during_the_radio_scene_is_not_for_the_director(store):
    svc, _, link = _make(store, dice=Dice(0.99))
    await _turn(svc, "Альфред, ты картавишь!")
    await svc.handle_click(CANARY, CANARY, "radio", keyboards.BTN_PLAY)
    await _begin(svc, "Что с радио?")
    assert await svc.tool_act(CANARY, CANARY, "покрутить ручку") == acts.TOOL_ACT_CABINET
    assert link.director_inputs == []


async def test_act_empty_action(store):
    svc, _, _ = _make(store)
    assert await svc.tool_act(CANARY, CANARY, "  ") == acts.TOOL_ACT_NO_ACTION


async def test_act_in_the_cellar_after_the_scene_asks_the_director_without_saving(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await svc._move_to(CANARY, places.PLACE_CELLAR)
    await _begin(svc, "Подвинь бочку")
    link.director_replies = [_act_reply("Бочка тяжёлая, но сдвигается.", stage=3)]
    out = await svc.tool_act(CANARY, CANARY, "подвинуть бочку")
    assert "Бочка тяжёлая" in out
    assert "dark" in link.director_inputs[-1].lower() or "подвал" in link.director_inputs[-1]
    assert await store.state_keys("interactive_run:") == []


async def test_act_in_catacombs_scene_uses_the_director_and_keeps_stage_zero(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await svc._move_to(CANARY, places.PLACE_CELLAR)
    await svc._start_descent(CANARY, CANARY)
    await svc._move_to(CANARY, "catacombs")
    await _begin(svc, "Подвинь плиту")
    link.director_replies = [_act_reply("Плита сдвинулась.", stage=2, facts_add=["плита сдвинута"])]
    out = await svc.tool_act(CANARY, CANARY, "сдвинуть плиту")
    assert "Плита сдвинулась." in out
    run = await svc._state.load_run(CANARY, "catacombs")
    assert run.stage == 0 and run.world["facts"] == ["плита сдвинута"]


def test_declaration_and_registration():
    function = acts.ACT_DECLARATION["function"]
    assert function["name"] == "act"
    assert function["parameters"]["required"] == ["action"]
    assert "act" in ai_tools.CANARY_TOOLS and "act" in ai_tools.SERIAL_TOOLS
    assert {"act", "search"} <= ai_tools.SERIAL_TOOLS


def test_scene_frames_demand_acts_for_physical_actions():
    assert "только инструментом act" in cellar.CELLAR.scene_frame
    assert "только инструментом act" in underground.CATACOMBS.scene_frame
    assert "замок" not in cellar.CELLAR.ladder[0].lower()
    assert "РЖАВЫЙ ЗАПЕРТЫЙ ЗАМОК" in cellar.CELLAR.act_prompt


def test_parse_act_requires_a_result():
    assert director.parse_act("{}", 0) is None
    parsed = director.parse_act(json.dumps({"result": " Ок. ", "stage": "x"}), 2)
    assert parsed is not None and parsed.result == "Ок." and parsed.stage == 2
