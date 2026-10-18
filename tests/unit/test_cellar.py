from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from sa_home_bot.bot.handlers.interactives import cmd_interactives
from sa_home_bot.bot.interactives import cellar, keyboards, offers, places, radio, registry
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_OFFERED,
    InteractiveStore,
    Run,
    parse_iso,
)
from tests.unit.interactives_kit import (
    CANARY,
    DEFAULT_NOW,
    GUEST,
    KNOCK,
    Dice,
    act_reply,
    cellar_run,
    director_reply,
    make_service,
    reach_active,
    reach_hatch,
    reach_key_hunt,
    reach_offer,
    run_turn,
)
from tests.unit.support.fakes import Clock


@pytest.mark.parametrize(
    "text",
    [
        "Что у тебя в подвале?",
        "Альфред, а в подвал ты ходишь?",
        "Слышишь, стук снизу какой-то",
        "из-под пола доносится шум снизу",
        "Что-то стучит внизу",
        "Там под полом кто-то ходит",
        "в погребе холодно?",
    ],
)
def test_trigger_regex_matches_questions_about_cellar(text):
    assert cellar.CELLAR.trigger_re.search(text)


@pytest.mark.parametrize(
    "text",
    ["Какая погода?", "Подвалило работы", "Погребальный марш", "Расскажи про камин", "Привет"],
)
def test_trigger_regex_ignores_other_talk(text):
    assert not cellar.CELLAR.trigger_re.search(text)


def test_scenario_shape():
    scenario = cellar.CELLAR
    assert scenario.canary and scenario.trigger == "spontaneous"
    assert scenario.offer_text == "Снизу что-то стукнуло, сэр. Сходить проверить?"
    assert scenario.offer_buttons == ("Проверьте", "Не обращать внимания")
    assert scenario.decline_cooldown == timedelta(days=3)
    assert len(scenario.ladder) == 5 and scenario.last_stage == 4
    visible = " ".join([scenario.offer_text, *scenario.offer_buttons]).lower()
    for word in ("игр", "квест", "локаци", "карт", "сценк", "ведущ"):
        assert word not in visible
    assert "никогда не говори" in scenario.scene_frame


async def test_spontaneous_needs_three_plain_turns_and_the_roll(store):
    svc, notifier, link = make_service(store, dice=Dice(0.0))
    for _ in range(3):
        plan = await run_turn(svc)
        assert plan.scenario is None
    assert notifier.sent == []
    plan = await run_turn(svc)
    assert plan.offered and plan.scenario == cellar.SCENARIO_ID
    _, text, markup = notifier.sent_with_markup[-1]
    assert text == cellar.OFFER_TEXT
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["Проверьте", "Не обращать внимания"]
    assert link.director_inputs == []


async def test_spontaneous_roll_failure_keeps_rolling_each_turn(store):
    svc, notifier, _ = make_service(store, dice=Dice(0.9, 0.9, 0.0))
    for _ in range(3):
        await run_turn(svc)
    await run_turn(svc)
    await run_turn(svc)
    assert notifier.sent == []
    plan = await run_turn(svc)
    assert plan.offered


async def test_spontaneous_never_for_non_canary(store):
    svc, notifier, _ = make_service(store, dice=Dice(0.0))
    for _ in range(8):
        plan = await run_turn(svc, chat=GUEST, user=GUEST)
        assert plan.scenario is None
    assert notifier.sent == []
    assert await store.state_keys("") == []


async def test_spontaneous_only_in_private_and_not_when_opted_out(store):
    svc, notifier, _ = make_service(store, dice=Dice(0.0))
    for _ in range(6):
        await svc.before_turn(-100, CANARY, KNOCK, is_private=False)
    await svc.set_opted_out(CANARY, True)
    for _ in range(6):
        await run_turn(svc)
    assert notifier.sent == []


async def test_spontaneous_not_during_another_scene(store):
    svc, notifier, _ = make_service(store, dice=Dice(0.0))
    await run_turn(svc, "Альфред, ты картавишь!")
    await svc.handle_click(CANARY, CANARY, radio.RADIO.id, keyboards.BTN_PLAY)
    for _ in range(6):
        plan = await run_turn(svc)
        assert plan.scenario != cellar.SCENARIO_ID
    assert notifier.texts(CANARY) == [radio.RADIO.offer_text]


async def test_spontaneous_at_most_once_a_day(store):
    clock = Clock(DEFAULT_NOW)
    svc, notifier, _ = make_service(store, dice=Dice(0.0), clock=clock)
    await reach_offer(svc, store)
    clock.now += timedelta(hours=3)
    for _ in range(8):
        await run_turn(svc)
    assert notifier.texts(CANARY).count(cellar.OFFER_TEXT) == 1
    clock.now += timedelta(hours=25)
    plan = await run_turn(svc)
    assert plan.offered
    assert notifier.texts(CANARY).count(cellar.OFFER_TEXT) == 2


async def test_direct_question_offers_without_roll_or_turn_count(store):
    svc, notifier, _ = make_service(store, dice=Dice(0.99))
    plan = await run_turn(svc, "Альфред, а что там у тебя в подвале?")
    assert plan.offered and plan.scenario == cellar.SCENARIO_ID
    assert notifier.texts(CANARY) == [cellar.OFFER_TEXT]


async def test_direct_question_ignored_for_non_canary(store):
    svc, notifier, _ = make_service(store)
    plan = await run_turn(svc, "Альфред, а что там у тебя в подвале?", chat=GUEST, user=GUEST)
    assert plan.scenario is None and notifier.sent == []


async def test_refusal_cools_down_for_three_days(store):
    clock = Clock(DEFAULT_NOW)
    svc, notifier, _ = make_service(store, dice=Dice(0.0), clock=clock)
    await reach_offer(svc, store)
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, keyboards.BTN_NEVER)
    assert answer[1] == cellar.OFFER_TEXT + "\n<i>— Не обращать внимания</i>"
    run = await cellar_run(store)
    assert run.status == STATUS_DECLINED
    clock.now += timedelta(days=2, hours=23)
    for _ in range(6):
        plan = await run_turn(svc)
        assert plan.scenario is None
    assert not (await run_turn(svc, "А что в подвале?")).offered
    clock.now += timedelta(hours=2)
    assert (await run_turn(svc, "А что в подвале?")).offered


async def test_yes_button_text_and_alfred_goes_to_the_cellar_door(store):
    svc, _, _ = make_service(store, dice=Dice(0.0))
    await reach_offer(svc, store)
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, keyboards.BTN_PLAY)
    assert answer == ("Хорошо.", cellar.OFFER_TEXT + "\n<i>— Проверьте</i>", True)
    at = await places.get_at(store, CANARY)
    assert at is not None and at.place == places.PLACE_CELLAR_DOOR
    directive, _ = svc.spoken[-1]
    assert "заколочена досками" in directive and "снимать ли" in directive


async def test_yes_in_words_starts_the_scene_this_turn(store):
    svc, notifier, _ = make_service(store)
    await reach_offer(svc, store)
    offer_id = (1000 + len(notifier.sent))
    plan = await svc.before_turn(CANARY, CANARY, "Да, проверь, что там внизу", is_private=True)
    assert plan.scene and plan.scenario == cellar.SCENARIO_ID
    assert offers.AGREED_BY_WORDS_NOTE in plan.note
    assert (await cellar_run(store)).status == STATUS_ACTIVE
    at = await places.get_at(store, CANARY)
    assert at is not None and at.place == places.PLACE_CELLAR_DOOR
    assert notifier.edited == [(CANARY, offer_id, cellar.OFFER_TEXT + "\n<i>— Проверьте</i>")]


async def test_no_in_words_declines_with_cooldown(store):
    svc, notifier, _ = make_service(store)
    await reach_offer(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Нет, не надо", is_private=True)
    assert not plan.scene
    run = await cellar_run(store)
    assert run.status == STATUS_DECLINED
    assert parse_iso(run.declined_until) > svc._now() + timedelta(days=2)
    assert notifier.edited[-1][2].endswith("<i>— Не обращать внимания</i>")


async def test_other_talk_keeps_the_offer_and_its_buttons(store):
    svc, notifier, _ = make_service(store)
    await reach_offer(svc, store)
    await run_turn(svc, "Разошли всем уведомление про VPN")
    assert (await cellar_run(store)).status == STATUS_OFFERED
    assert notifier.markup_edits == [] and notifier.edited == []
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, keyboards.BTN_PLAY)
    assert answer[0] == "Хорошо."


async def test_offer_still_expires_after_an_hour(store):
    clock = Clock(DEFAULT_NOW)
    svc, notifier, _ = make_service(store, clock=clock)
    await reach_offer(svc, store)
    clock.now += timedelta(hours=1, minutes=1)
    plan = await svc.before_turn(CANARY, CANARY, "Да, проверь", is_private=True)
    assert not plan.scene
    assert (await cellar_run(store)).status == STATUS_DECLINED
    assert notifier.markup_edits


@pytest.mark.parametrize("text", ["не проверяй", "Какая погода?", "проверка связи", "данные"])
def test_words_regexes_do_not_misread(text):
    assert not offers.OFFER_YES_WORDS_RE.match(text)


@pytest.mark.parametrize("text", ["да", "Ну давай", "Проверьте", "сходи глянь", "пойдём вниз"])
def test_yes_words(text):
    assert offers.OFFER_YES_WORDS_RE.match(text) and not offers.OFFER_NO_WORDS_RE.match(text)


async def test_spontaneous_knock_not_on_a_photo_request(store):
    svc, notifier, _ = make_service(store, dice=Dice(0.0))
    for _ in range(3):
        await run_turn(svc)
    plan = await run_turn(svc, "хорошо, покажи что за окном")
    assert not plan.offered and notifier.sent == []
    assert (await run_turn(svc)).offered


async def test_scene_note_places_alfred_at_the_boarded_door(store):
    svc, _, _ = make_service(store, dice=Dice(0.0))
    await reach_active(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Ну что там?", is_private=True)
    assert plan.scene and plan.scenario == cellar.SCENARIO_ID
    assert cellar.CELLAR.scene_frame in plan.note
    assert "заколочена" in plan.note
    assert cellar.CELLAR.ladder[0] in plan.note
    assert "Где ты: Узкая каменная лестница" in plan.note
    assert "за окном" not in plan.note.lower()


async def test_stage_one_only_after_guest_agrees_and_director_says_so(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_active(svc, store)
    link.director_replies = [
        director_reply(stage=0),
        director_reply(stage=1, effect="Доски падают."),
    ]
    await run_turn(svc, "А что там за дверью?")
    assert (await cellar_run(store)).stage == 0
    await run_turn(svc, "Да, снимайте доски")
    run = await cellar_run(store)
    assert run.stage == 1 and run.pending_effect == "Доски падают."
    at = await places.get_at(store, CANARY)
    assert at.place == places.PLACE_CELLAR_DOOR


async def test_director_cannot_skip_the_key_hunt(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_active(svc, store)
    link.director_replies = [
        director_reply(stage=1),
        director_reply(stage=2),
        director_reply(stage=3),
        director_reply(stage=3),
    ]
    for _ in range(4):
        await run_turn(svc)
    run = await cellar_run(store)
    assert run.stage == 2
    assert not run.world.get("key_found")


def _kitchen():
    return {
        "name": "кухня",
        "where": "на кухне",
        "canon_ru": "Большая холодная кухня замка: очаг, медные кастрюли, длинный стол.",
        "canon_en": "large medieval castle kitchen, copper pots, stone hearth",
    }


async def test_move_to_puts_alfred_into_a_temp_room_kept_in_run_world(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_key_hunt(svc, store, link)
    link.director_replies = [director_reply(stage=2, move_to=_kitchen())]
    await run_turn(svc, "Поищи на кухне")
    at = await places.get_at(store, CANARY)
    assert places.is_temp(at.place)
    run = await cellar_run(store)
    assert run.world["rooms"][at.place]["name"] == "кухня"
    assert [k for k in await store.state_keys("location:")] == []
    room = await svc._room(CANARY, CANARY)
    assert room.temp and "кухня" in room.describe_ru().lower() or "кухн" in room.describe_ru()


async def test_move_to_ignored_outside_key_hunt_stage(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_active(svc, store)
    link.director_replies = [director_reply(stage=0, move_to=_kitchen())]
    await run_turn(svc, "Ну что там?")
    at = await places.get_at(store, CANARY)
    assert at.place == places.PLACE_CELLAR_DOOR


def test_key_chances_follow_the_plan():
    assert [cellar.key_chance(n) for n in (1, 2, 3, 4)] == [0.0, 0.25, 0.40, 0.60]
    assert cellar.key_chance(5) == 1.0 and cellar.key_chance(9) == 1.0


def test_first_search_never_finds_the_key_even_with_a_lucky_roll():
    run = Run(scenario="cellar", chat_id=1, user_id=1)
    assert cellar.roll_key(run, lambda: 0.0) is False
    assert run.world["key_searches"] == 1 and "key_found" not in run.world


def test_fifth_search_always_finds_the_key():
    run = Run(scenario="cellar", chat_id=1, user_id=1)
    run.world["key_searches"] = 4
    assert cellar.roll_key(run, lambda: 0.999) is True
    assert run.world["key_found"] is True


async def test_search_tool_in_key_hunt_asks_the_director_synchronously(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_key_hunt(svc, store, link)
    link.director_replies = [act_reply("Шкаф пуст, лишь пыль и паутина.")]
    answer = await svc.tool_search(CANARY, CANARY, "шкаф в кладовой")
    assert "Шкаф пуст, лишь пыль и паутина." in answer
    assert "ключа здесь нет" in link.director_inputs[-1].lower()
    assert "Исход определён кодом" in link.director_inputs[-1]
    run = await cellar_run(store)
    assert not run.world.get("key_found")
    assert "search_outcome" not in run.world
    assert run.transcript[-2].startswith("Действие: порыскать: шкаф в кладовой")
    assert run.transcript[-1] == "Итог: Шкаф пуст, лишь пыль и паутина."
    assert await svc._state.get_effect("cellar_unlocked", CANARY) is None
    link.director_replies = [director_reply(stage=2, effect="Придуманный исход.")]
    await run_turn(svc, "Ну?")
    run = await cellar_run(store)
    assert run.pending_effect is None and run.last_effect != "Придуманный исход."


async def test_second_search_with_good_roll_finds_the_key_and_opens_the_cellar(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_key_hunt(svc, store, link)
    link.director_replies = [act_reply("Пусто."), act_reply("В ящике блестит ключ.")]
    await svc.tool_search(CANARY, CANARY, "шкаф в кладовой")
    answer = await svc.tool_search(CANARY, CANARY, "ящик буфета")
    assert "В ящике блестит ключ." in answer
    assert "НАХОДИТ ключ" in link.director_inputs[-1]
    run = await cellar_run(store)
    assert run.world["key_found"] is True
    assert await svc._state.get_effect("cellar_unlocked", CANARY) == "1"
    assert run.stage == 3
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR


async def test_search_without_director_uses_the_code_outcome(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_key_hunt(svc, store, link)
    svc._get_node_link = lambda: None
    await svc.tool_search(CANARY, CANARY, "шкаф")
    answer = await svc.tool_search(CANARY, CANARY, "ящик буфета")
    assert "ключ" in answer
    assert (await cellar_run(store)).stage == 3


async def test_repeat_search_in_same_place_gives_nothing_and_is_not_an_attempt(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_key_hunt(svc, store, link)
    link.director_replies = [act_reply("Пусто."), act_reply("Всё то же.")]
    await svc.tool_search(CANARY, CANARY, "ящик стола")
    await svc.tool_search(CANARY, CANARY, "В ящик стола!")
    run = await cellar_run(store)
    assert run.world["key_searches"] == 1
    assert "ничего нового" in link.director_inputs[-1]


async def test_fifth_new_place_guarantees_the_key(store):
    svc, _, link = make_service(store, dice=Dice(0.99))
    await reach_key_hunt(svc, store, link)
    svc._rng = Dice(0.99)
    for i, where in enumerate(["шкаф", "ящик", "полка", "сундук", "ниша"], start=1):
        svc._acts_new_turn(CANARY, "ищи")
        await svc.tool_search(CANARY, CANARY, where)
        assert bool((await cellar_run(store)).world.get("key_found")) == (i == 5)


async def test_search_by_non_canary_is_refused(store):
    svc, _, _ = make_service(store)
    assert await svc.tool_search(GUEST, GUEST, "шкаф") == places.TOOL_SEARCH_UNAVAILABLE
    assert await store.state_keys("") == []


async def test_stage_three_gives_the_bottle_as_an_item(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_hatch(svc, store, link)
    wine = await store.items_of(CANARY, "wine")
    assert len(wine) == 1 and wine[0]["origin"] == "stage"
    assert "found_item" in link.director_inputs[-1]


async def test_finale_sets_flags_then_finishes_on_the_next_turn(store):
    svc, notifier, link = make_service(store, dice=Dice(0.0))
    await reach_hatch(svc, store, link)
    run = await cellar_run(store)
    run.turns_total = 10
    await svc._state.save_run(run)
    link.director_replies = [director_reply(stage=4, finale=True, finale_fault="лаз за бочкой")]
    await run_turn(svc, "Давай ещё поищем")
    run = await cellar_run(store)
    assert run.finale and run.status == STATUS_ACTIVE
    assert run.pending_effect == "Обнаруживается лаз: лаз за бочкой."
    assert await svc._state.is_completed("cellar", CANARY)
    assert await svc._state.get_effect("catacombs_open", CANARY) == "1"
    assert all("радиостанци" not in t for t in notifier.texts(CANARY))
    plan = await svc.before_turn(CANARY, CANARY, "И что?", is_private=True)
    assert not plan.force_swap_form
    assert "лаз за бочкой" in plan.note
    link.director_replies = [director_reply(stage=4)]
    await svc.after_turn(plan, "Лаз, сэр.", dialogue_id=77)
    assert (await cellar_run(store)).status == STATUS_DONE
    room = await svc._room(CANARY, CANARY)
    where = await svc._where_ru(room, CANARY, in_scene=False)
    assert "лаз в катакомбы" in where
    for _ in range(6):
        assert not (await run_turn(svc, "А что в подвале?")).offered


async def test_finale_not_before_stage_four(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_key_hunt(svc, store, link)
    run = await cellar_run(store)
    run.turns_total = 20
    await svc._state.save_run(run)
    link.director_replies = [director_reply(stage=2, finale=True, finale_fault="лаз")]
    await run_turn(svc)
    assert not (await cellar_run(store)).finale


async def test_director_idle_returns_alfred_to_the_cabinet_with_a_note(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_active(svc, store)
    link.director_replies = [director_reply(active=False)]
    await run_turn(svc, "Кстати, расскажи анекдот")
    assert (await cellar_run(store)).status == STATUS_IDLE
    at = await places.get_at(store, CANARY)
    assert at.in_cabinet and at.returned_from == "от двери подвала"
    plan = await svc.before_turn(CANARY, CANARY, "Ну что, анекдот?", is_private=True)
    assert "вернулся от двери подвала к себе в кабинет" in plan.note
    plan = await svc.before_turn(CANARY, CANARY, "Ещё", is_private=True)
    assert "вернулся" not in (plan.note or "")
    plan = await svc.before_turn(CANARY, CANARY, "Так что там в подвале?", is_private=True)
    assert plan.scene and "вернулся" not in plan.note
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR_DOOR


async def test_guest_silent_longer_than_the_threshold_brings_alfred_home(store):
    clock = Clock(DEFAULT_NOW)
    svc, _, _ = make_service(store, dice=Dice(0.0), clock=clock)
    await reach_active(svc, store)
    clock.now += timedelta(hours=1, minutes=50)
    plan = await svc.before_turn(CANARY, CANARY, "Я тут", is_private=True)
    assert plan.scene
    clock.now += timedelta(hours=2, minutes=1)
    plan = await svc.before_turn(CANARY, CANARY, "Я снова тут", is_private=True)
    assert not plan.scene
    assert "вернулся от двери подвала к себе в кабинет" in plan.note
    assert (await cellar_run(store)).status == STATUS_IDLE


async def test_take_photo_in_the_cellar_draws_the_cellar_not_the_study(store):
    svc, notifier, link = make_service(store, dice=Dice(0.0))
    await reach_active(svc, store)
    link.director_replies = [director_reply(stage=1)]
    await run_turn(svc)
    await asyncio.gather(*list(svc._photo_tasks))
    door = link.generated[-1]
    assert "boarded up" in door["description"] and "study" not in door["description"]
    await svc._move_to(CANARY, places.PLACE_CELLAR)
    started = await svc.tool_take_photo(CANARY, CANARY, {})
    assert started
    await asyncio.gather(*list(svc._photo_tasks))
    request = link.generated[-1]
    assert "dark stone cellar" in request["description"]
    assert "study" not in request["description"] and "fireplace" not in request["description"]
    assert request["light"] == places.UNDERGROUND_LIGHT_EN
    assert "light_move" not in request and "paste" not in request
    assert notifier.photos[-1][2].endswith("Готово, сэр.") or notifier.photos[-1][2] == "Подвал"


async def test_take_photo_for_non_canary_ignores_alfred_at(store):
    svc, notifier, link = make_service(store)
    await places.set_at(store, GUEST, places.new_at(places.PLACE_CELLAR, svc._now()))
    assert await svc.tool_take_photo(GUEST, GUEST, {})
    await asyncio.gather(*list(svc._photo_tasks))
    request = link.generated[-1]
    assert "study" in request["description"] and "cellar" not in request["description"]
    assert request["light"] != places.UNDERGROUND_LIGHT_EN


class FakeMessage:
    def __init__(self, user_id, chat_id=None):
        self.chat = type("Chat", (), {"id": chat_id or user_id})()
        self.from_user = type("User", (), {"id": user_id})()
        self.answers: list[str] = []

    async def answer(self, text, **_kw):
        self.answers.append(text)


class FakeCommand:
    def __init__(self, args):
        self.args = args


async def test_debug_cellar_knocks_right_now_for_canary(store):
    svc, notifier, _ = make_service(store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("cellar"), svc)
    assert message.answers == ["Стук отправлен."]
    assert notifier.texts(CANARY) == [cellar.OFFER_TEXT]
    assert (await cellar_run(store)).status == STATUS_OFFERED
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("cellar"), svc)
    assert "уже на ходу" in message.answers[0]


async def test_debug_commands_for_others_show_the_old_usage(store):
    svc, notifier, _ = make_service(store)
    for args in ("cellar", "reset all", "where", "что-то"):
        message = FakeMessage(GUEST)
        await cmd_interactives(message, FakeCommand(args), svc)
        assert message.answers == ["Использование: /interactives on | off"]
    assert notifier.sent == [] and await store.state_keys("") == []


async def test_debug_where_shows_raw_alfred_at(store):
    svc, _, _ = make_service(store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("where"), svc)
    assert "в кабинете" in message.answers[0]
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CELLAR, svc._now()))
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("where"), svc)
    assert json.loads(message.answers[0])["place"] == "cellar"


async def test_debug_reset_erases_stage_59_progress_but_not_the_cabinet(store):
    svc, _, link = make_service(store, dice=Dice(0.0))
    await reach_hatch(svc, store, link)
    from sa_home_bot.bot.interactives import cabinet

    cab = await cabinet.load(store, CANARY)
    cab.add(["чучело совы на шкафу"])
    await cabinet.save(store, cab)
    await places.save_room(store, await places.load_room(store, CANARY, places.PLACE_CELLAR))
    await store.set_state(f"catacombs:{CANARY}", "{}")
    await svc._state.mark_completed("cellar", CANARY)
    await svc._state.set_effect("catacombs_open", CANARY, "1")
    await svc._state.set_effect("speech_clear", CANARY, "1")
    await InteractiveStore(store).save_run(Run(scenario="radio", chat_id=CANARY, user_id=CANARY))
    other = Run(scenario="cellar", chat_id=GUEST, user_id=GUEST)
    await InteractiveStore(store).save_run(other)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("reset all"), svc)
    assert message.answers[0].startswith("Сброшено (all)")
    keys = await store.state_keys("")
    assert not [k for k in keys if "cellar" in k and str(GUEST) not in k]
    assert not [k for k in keys if k.startswith(("alfred_at", "catacombs", "location:cellar"))]
    assert f"location:cabinet:{CANARY}" in keys
    assert f"user_effect:speech_clear:{CANARY}" in keys
    assert f"interactive_run:{CANARY}:radio" in keys
    assert f"interactive_run:{GUEST}:cellar" in keys
    assert not [k for k in keys if k.startswith("user_effect:catacombs_open")]


async def test_debug_reset_unknown_scope_hints(store):
    svc, _, _ = make_service(store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("reset"), svc)
    assert "reset cellar | catacombs | all" in message.answers[0]


async def test_radio_scene_pulls_a_canary_back_to_the_study_but_canary_scenes_keep_place(store):
    import dataclasses
    import re

    svc, _, _ = make_service(store)
    ghost = dataclasses.replace(
        cellar.CELLAR,
        id="ghost",
        trigger_re=re.compile("призрак"),
        stage_places=(),
        start_place=None,
    )
    registry.REGISTRY["ghost"] = ghost
    try:
        await svc._state.save_run(
            Run(scenario="ghost", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE)
        )
        await svc._move_to(CANARY, places.PLACE_CELLAR)
        plan = await svc.before_turn(CANARY, CANARY, "Ну как?", is_private=True)
        assert plan.scene and plan.scenario == "ghost"
        assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR
        await svc._state.save_run(
            Run(scenario="ghost", chat_id=CANARY, user_id=CANARY, status=STATUS_IDLE)
        )
        radio_run = Run(scenario="radio", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE)
        await svc._state.save_run(radio_run)
        plan = await svc.before_turn(CANARY, CANARY, "Слышно плохо", is_private=True)
        assert plan.scenario == "radio"
        assert (await places.get_at(store, CANARY)).in_cabinet
    finally:
        registry.REGISTRY.pop("ghost", None)
