"""Сценарий «Стук снизу» (Этап 59.2): спонтанный триггер, форма, стадии,
поиск ключа (решает код), финал, отладочные подкоманды."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from PIL import Image

from sa_home_bot.bot.handlers.interactives import cmd_interactives
from sa_home_bot.bot.interactives import cellar, engine, places, radio
from sa_home_bot.bot.interactives.base import (
    STATUS_ACTIVE,
    STATUS_DECLINED,
    STATUS_DONE,
    STATUS_IDLE,
    STATUS_OFFERED,
    InteractiveStore,
    Run,
)
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.interactives.transylvania import Transylvania
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

CANARY = 801
GUEST = 802
KNOCK = "Какая сегодня погода?"


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "brown").save(buf, format="PNG")
    return buf.getvalue()


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.photos: list[tuple[int, object, str | None]] = []
        self._next = 1000

    async def send_direct(self, chat_id, text, reply_markup=None, **_kw):
        self._next += 1
        self.sent.append((chat_id, text, reply_markup))
        return self._next

    async def send_photo_ex(self, chat_id, photo, *, caption=None, **_kw):
        self.photos.append((chat_id, photo, caption))
        return 500 + len(self.photos), f"file-{len(self.photos)}"

    def texts(self, chat_id: int) -> list[str]:
        return [t for c, t, _ in self.sent if c == chat_id]


class FakeLink:
    def __init__(self) -> None:
        self.director_replies: list[str] = []
        self.director_inputs: list[str] = []
        self.generated: list[dict] = []

    async def command(self, action, args, dst=None, timeout=None):
        if action == "chat" and args.get("role") == "director":
            self.director_inputs.append(args["messages"][0]["content"])
            return {"response": self.director_replies.pop(0) if self.director_replies else "{}"}
        if action == "chat":
            return {"response": "Готово, сэр."}  # подпись к снимку
        if action == "generate_image":
            self.generated.append(args)
            return {
                "png_b64": base64.b64encode(_png()).decode(),
                "width": 8,
                "height": 8,
                "prompt": "p",
                "seed": 7,
            }
        if action == "chat_progress":
            return {"partial": "draw", "done": True}
        raise AssertionError(action)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Dice:
    """Подставной rng: значения по очереди, затем последнее."""

    def __init__(self, *values: float) -> None:
        self.values = list(values) or [0.99]

    def __call__(self) -> float:
        return self.values.pop(0) if len(self.values) > 1 else self.values[0]


async def _clear() -> int:
    return 0


def _make(store, *, canary=(CANARY,), dice=None, clock=None, chance=1 / 8):
    notifier, link = FakeNotifier(), FakeLink()
    svc = Interactives(
        store,
        notifier,
        Settings(
            llm=LlmConfig(
                model="m",
                interactives_canary_user_ids=list(canary),
                interactives_spontaneous_chance=chance,
            )
        ),
        lambda: link,
        now=clock or Clock(),
        choose=lambda options: options[0],
        rng=dice or Dice(0.99),
        transylvania=Transylvania(fetch=_clear),
    )
    svc.spoken = []

    async def speak(chat_id, directive, where):
        svc.spoken.append((directive, where))

    svc._speak = speak
    return svc, notifier, link


async def _turn(svc, text=KNOCK, reply="Ответ Альфреда", *, chat=CANARY, user=CANARY):
    plan = await svc.before_turn(chat, user, text, is_private=True)
    await svc.after_turn(plan, reply, dialogue_id=77)
    await svc.flush_forms(chat, plan, dialogue_id=77)
    return plan


async def _run(store, chat=CANARY) -> Run | None:
    return await InteractiveStore(store).load_run(chat, cellar.SCENARIO_ID)


def _dir(**fields) -> str:
    data = {"active": True, "stage": 0, "effect": None, "directive": None, "finale": False}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


async def _offered(svc, store):
    """Канарейка: три обычных хода и четвёртый с форм (бросок удался)."""
    for _ in range(3):
        await _turn(svc)
    svc._rng = Dice(0.0)
    plan = await _turn(svc)
    assert plan.offered and plan.scenario == cellar.SCENARIO_ID
    assert (await _run(store)).status == STATUS_OFFERED


async def _active(svc, store):
    await _offered(svc, store)
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, engine.BTN_PLAY)
    assert answer[0] == "Хорошо."
    assert (await _run(store)).status == STATUS_ACTIVE


# --- триггер ---


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
    # Гость видит только форму: в ней слов «игра/квест/локация/карта» нет.
    visible = " ".join([scenario.offer_text, *scenario.offer_buttons]).lower()
    for word in ("игр", "квест", "локаци", "карт", "сценк", "ведущ"):
        assert word not in visible
    # Альфреду эти слова названы только как запрещённые.
    assert "никогда не говори" in scenario.scene_frame


# --- спонтанный триггер ---


async def test_spontaneous_needs_three_plain_turns_and_the_roll(store):
    svc, notifier, link = _make(store, dice=Dice(0.0))
    for _ in range(3):
        plan = await _turn(svc)
        assert plan.scenario is None  # первые три хода — только счёт
    assert notifier.sent == []
    plan = await _turn(svc)
    assert plan.offered and plan.scenario == cellar.SCENARIO_ID
    _, text, markup = notifier.sent[-1]
    assert text == cellar.OFFER_TEXT
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["Проверьте", "Не обращать внимания"]
    assert link.director_inputs == []  # Ведущий до согласия не зовётся


async def test_spontaneous_roll_failure_keeps_rolling_each_turn(store):
    svc, notifier, _ = _make(store, dice=Dice(0.9, 0.9, 0.0))
    for _ in range(3):
        await _turn(svc)
    await _turn(svc)  # бросок 0.9 >= 1/8
    await _turn(svc)  # 0.9
    assert notifier.sent == []
    plan = await _turn(svc)  # 0.0
    assert plan.offered


async def test_spontaneous_never_for_non_canary(store):
    svc, notifier, _ = _make(store, dice=Dice(0.0))
    for _ in range(8):
        plan = await _turn(svc, chat=GUEST, user=GUEST)
        assert plan.scenario is None
    assert notifier.sent == []
    assert await store.state_keys("") == []  # и ни одной записи


async def test_spontaneous_only_in_private_and_not_when_opted_out(store):
    svc, notifier, _ = _make(store, dice=Dice(0.0))
    for _ in range(6):
        await svc.before_turn(-100, CANARY, KNOCK, is_private=False)
    await svc.set_opted_out(CANARY, True)
    for _ in range(6):
        await _turn(svc)
    assert notifier.sent == []


async def test_spontaneous_not_during_another_scene(store):
    svc, notifier, _ = _make(store, dice=Dice(0.0))
    await _turn(svc, "Альфред, ты картавишь!")  # радио предложено
    for _ in range(6):
        plan = await _turn(svc)
        assert plan.scenario != cellar.SCENARIO_ID
    assert notifier.texts(CANARY) == [radio.RADIO.offer_text]


async def test_spontaneous_at_most_once_a_day(store):
    clock = Clock()
    svc, notifier, _ = _make(store, dice=Dice(0.0), clock=clock)
    await _offered(svc, store)
    # Гость молча проигнорировал форму: пишет дальше — кнопки сняты.
    clock.now += timedelta(hours=3)
    for _ in range(8):
        await _turn(svc)
    assert notifier.texts(CANARY).count(cellar.OFFER_TEXT) == 1
    # Форма без ответа протухла (час), кулдаун протухшей — сутки; после них
    # новое предложение возможно.
    clock.now += timedelta(hours=25)
    plan = await _turn(svc)
    assert plan.offered
    assert notifier.texts(CANARY).count(cellar.OFFER_TEXT) == 2


async def test_direct_question_offers_without_roll_or_turn_count(store):
    svc, notifier, _ = _make(store, dice=Dice(0.99))
    plan = await _turn(svc, "Альфред, а что там у тебя в подвале?")
    assert plan.offered and plan.scenario == cellar.SCENARIO_ID
    assert notifier.texts(CANARY) == [cellar.OFFER_TEXT]


async def test_direct_question_ignored_for_non_canary(store):
    svc, notifier, _ = _make(store)
    plan = await _turn(svc, "Альфред, а что там у тебя в подвале?", chat=GUEST, user=GUEST)
    assert plan.scenario is None and notifier.sent == []


async def test_refusal_cools_down_for_three_days(store):
    clock = Clock()
    svc, notifier, _ = _make(store, dice=Dice(0.0), clock=clock)
    await _offered(svc, store)
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, engine.BTN_NEVER)
    assert answer[1] == cellar.OFFER_TEXT + "\n<i>— Не обращать внимания</i>"
    run = await _run(store)
    assert run.status == STATUS_DECLINED
    clock.now += timedelta(days=2, hours=23)
    for _ in range(6):
        plan = await _turn(svc)
        assert plan.scenario is None
    # Прямой вопрос тоже не пробивает кулдаун.
    assert not (await _turn(svc, "А что в подвале?")).offered
    clock.now += timedelta(hours=2)
    assert (await _turn(svc, "А что в подвале?")).offered


async def test_yes_button_text_and_alfred_goes_to_the_cellar_door(store):
    svc, _, _ = _make(store, dice=Dice(0.0))
    await _offered(svc, store)
    answer = await svc.handle_click(CANARY, CANARY, cellar.SCENARIO_ID, engine.BTN_PLAY)
    assert answer == ("Хорошо.", cellar.OFFER_TEXT + "\n<i>— Проверьте</i>", True)
    at = await places.get_at(store, CANARY)
    assert at is not None and at.place == places.PLACE_CELLAR_DOOR
    directive, _ = svc.spoken[-1]
    assert "заколочена досками" in directive and "снимать ли" in directive


# --- сцена: стадии ---


async def test_scene_note_places_alfred_at_the_boarded_door(store):
    svc, _, _ = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    plan = await svc.before_turn(CANARY, CANARY, "Ну что там?", is_private=True)
    assert plan.scene and plan.scenario == cellar.SCENARIO_ID
    assert cellar.CELLAR.scene_frame in plan.note
    assert "заколочена" in plan.note  # «Где ты» — лестница к двери подвала
    assert cellar.CELLAR.ladder[0] in plan.note
    assert "Где ты: Узкая каменная лестница" in plan.note
    assert "за окном" not in plan.note.lower()  # погоды снаружи под землёй нет


async def test_stage_one_only_after_guest_agrees_and_director_says_so(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    link.director_replies = [_dir(stage=0), _dir(stage=1, effect="Доски падают.")]
    await _turn(svc, "А что там за дверью?")
    assert (await _run(store)).stage == 0
    await _turn(svc, "Да, снимайте доски")
    run = await _run(store)
    assert run.stage == 1 and run.pending_effect == "Доски падают."
    at = await places.get_at(store, CANARY)
    assert at.place == places.PLACE_CELLAR_DOOR


async def test_director_cannot_skip_the_key_hunt(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    link.director_replies = [_dir(stage=1), _dir(stage=2), _dir(stage=3), _dir(stage=3)]
    for _ in range(4):
        await _turn(svc)
    run = await _run(store)
    assert run.stage == 2  # в стадию 3 — только когда код нашёл ключ
    assert not run.world.get("key_found")


async def _to_key_hunt(svc, store, link):
    await _active(svc, store)
    link.director_replies = [_dir(stage=1), _dir(stage=2)]
    await _turn(svc)
    await _turn(svc)
    assert (await _run(store)).stage == 2


def _kitchen():
    return {
        "name": "кухня",
        "where": "на кухне",
        "canon_ru": "Большая холодная кухня замка: очаг, медные кастрюли, длинный стол.",
        "canon_en": "large medieval castle kitchen, copper pots, stone hearth",
    }


async def test_move_to_puts_alfred_into_a_temp_room_kept_in_run_world(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    link.director_replies = [_dir(stage=2, move_to=_kitchen())]
    await _turn(svc, "Поищи на кухне")
    at = await places.get_at(store, CANARY)
    assert places.is_temp(at.place)
    run = await _run(store)
    assert run.world["rooms"][at.place]["name"] == "кухня"
    # Комната в хранилище location:* не попала.
    assert [k for k in await store.state_keys("location:")] == []
    room = await svc._room(CANARY, CANARY)
    assert room.temp and "кухня" in room.describe_ru().lower() or "кухн" in room.describe_ru()


async def test_move_to_ignored_outside_key_hunt_stage(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    link.director_replies = [_dir(stage=0, move_to=_kitchen())]
    await _turn(svc, "Ну что там?")
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


async def test_search_tool_in_key_hunt_hands_the_outcome_to_the_director(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    # 1-й обыск: ключа нет, как бы ни выпал кубик.
    answer = await svc.tool_search(CANARY, CANARY, "шкаф в кладовой")
    assert "не выдумывай" in answer and "шкаф в кладовой" in answer
    run = await _run(store)
    assert "ключа здесь нет" in run.world["search_outcome"].lower()
    assert not run.world.get("key_found")
    assert await svc._state.get_effect("cellar_unlocked", CANARY) is None
    link.director_replies = [_dir(stage=2)]
    await _turn(svc, "Ну?")
    assert "Исход обыска" in link.director_inputs[-1]
    assert "ключа здесь нет" in link.director_inputs[-1].lower()
    assert "search_outcome" not in (await _run(store)).world  # ушёл и забыт


async def test_second_search_with_good_roll_finds_the_key_and_opens_the_cellar(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    await svc.tool_search(CANARY, CANARY, "шкаф в кладовой")
    await svc.tool_search(CANARY, CANARY, "ящик буфета")  # 2-й: шанс 25%, rng 0.0
    run = await _run(store)
    assert run.world["key_found"] is True
    assert await svc._state.get_effect("cellar_unlocked", CANARY) == "1"
    assert "НАХОДИТ ключ" in run.world["search_outcome"]
    # Ход: код сам переводит на стадию 3 и ставит Альфреда в подвал.
    link.director_replies = [_dir(stage=2, effect="Ключ блестит в ящике.")]
    await _turn(svc, "Ну что?")
    run = await _run(store)
    assert run.stage == 3
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR


async def test_repeat_search_in_same_place_gives_nothing_and_is_not_an_attempt(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    await svc.tool_search(CANARY, CANARY, "ящик стола")
    await svc.tool_search(CANARY, CANARY, "В ящик стола!")
    run = await _run(store)
    assert run.world["key_searches"] == 1
    assert "ничего нового" in run.world["search_outcome"]


async def test_fifth_new_place_guarantees_the_key(store):
    svc, _, link = _make(store, dice=Dice(0.99))
    await _to_key_hunt(svc, store, link)
    svc._rng = Dice(0.99)  # _offered подменил кубик на «всегда везёт»
    for i, where in enumerate(["шкаф", "ящик", "полка", "сундук", "ниша"], start=1):
        await svc.tool_search(CANARY, CANARY, where)
        assert bool((await _run(store)).world.get("key_found")) == (i == 5)


async def test_search_by_non_canary_is_refused(store):
    svc, _, _ = _make(store)
    assert await svc.tool_search(GUEST, GUEST, "шкаф") == places.TOOL_SEARCH_UNAVAILABLE
    assert await store.state_keys("") == []


async def _to_hatch(svc, store, link):
    await _to_key_hunt(svc, store, link)
    run = await _run(store)
    run.world["key_found"] = True
    await svc._state.save_run(run)
    link.director_replies = [_dir(stage=3, effect="Замок поддался; у входа бутылка вина.")]
    await _turn(svc, "Ну?")
    assert (await _run(store)).stage == 3
    link.director_replies = [_dir(stage=4)]
    await _turn(svc, "Поищи ещё вина")
    assert (await _run(store)).stage == 4


async def test_stage_three_mentions_the_bottle_but_it_is_not_an_item_yet(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_hatch(svc, store, link)
    assert "бутылк" in link.director_inputs[-2].lower()  # TODO 59.3 в промпте Ведущего
    assert await store.items_of(CANARY) == []


async def test_finale_sets_flags_then_finishes_on_the_next_turn(store):
    svc, notifier, link = _make(store, dice=Dice(0.0))
    await _to_hatch(svc, store, link)
    # Ходы набирают минимум для финала.
    run = await _run(store)
    run.turns_total = 10
    await svc._state.save_run(run)
    link.director_replies = [_dir(stage=4, finale=True, finale_fault="лаз за бочкой")]
    await _turn(svc, "Давай ещё поищем")
    run = await _run(store)
    assert run.finale and run.status == STATUS_ACTIVE
    assert run.pending_effect == "Обнаруживается лаз: лаз за бочкой."
    assert await svc._state.is_completed("cellar", CANARY)
    assert await svc._state.get_effect("catacombs_open", CANARY) == "1"
    # Формы замены радиостанции у этой сцены нет.
    assert all("радиостанци" not in t for t in notifier.texts(CANARY))
    plan = await svc.before_turn(CANARY, CANARY, "И что?", is_private=True)
    assert not plan.force_swap_form
    assert "лаз за бочкой" in plan.note
    link.director_replies = [_dir(stage=4)]
    await svc.after_turn(plan, "Лаз, сэр.", dialogue_id=77)
    assert (await _run(store)).status == STATUS_DONE
    # Подвал — постоянная комната: в «Где ты» отсюда лаз в катакомбы.
    room = await svc._room(CANARY, CANARY)
    where = await svc._where_ru(room, CANARY, in_scene=False)
    assert "лаз в катакомбы" in where
    # И сцена больше не предлагается.
    for _ in range(6):
        assert not (await _turn(svc, "А что в подвале?")).offered


async def test_finale_not_before_stage_four(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_key_hunt(svc, store, link)
    run = await _run(store)
    run.turns_total = 20
    await svc._state.save_run(run)
    link.director_replies = [_dir(stage=2, finale=True, finale_fault="лаз")]
    await _turn(svc)
    assert not (await _run(store)).finale


# --- уход Ведущего в idle и возврат в кабинет ---


async def test_director_idle_returns_alfred_to_the_cabinet_with_a_note(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    link.director_replies = [_dir(active=False)]
    await _turn(svc, "Кстати, расскажи анекдот")
    assert (await _run(store)).status == STATUS_IDLE
    at = await places.get_at(store, CANARY)
    assert at.in_cabinet and at.returned_from == "от двери подвала"
    plan = await svc.before_turn(CANARY, CANARY, "Ну что, анекдот?", is_private=True)
    assert "вернулся от двери подвала к себе в кабинет" in plan.note
    # Один раз: дальше записи о возврате нет.
    plan = await svc.before_turn(CANARY, CANARY, "Ещё", is_private=True)
    assert "вернулся" not in (plan.note or "")
    # Гость вернулся к теме — сцена продолжается, Альфред снова у двери.
    plan = await svc.before_turn(CANARY, CANARY, "Так что там в подвале?", is_private=True)
    assert plan.scene and "вернулся" not in plan.note  # не «поднялся», а снова у двери
    assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR_DOOR


async def test_guest_silent_longer_than_the_threshold_brings_alfred_home(store):
    clock = Clock()
    svc, _, _ = _make(store, dice=Dice(0.0), clock=clock)
    await _active(svc, store)
    clock.now += timedelta(hours=1, minutes=50)
    plan = await svc.before_turn(CANARY, CANARY, "Я тут", is_private=True)
    assert plan.scene  # ещё в подвале
    clock.now += timedelta(hours=2, minutes=1)
    plan = await svc.before_turn(CANARY, CANARY, "Я снова тут", is_private=True)
    assert not plan.scene
    assert "вернулся от двери подвала к себе в кабинет" in plan.note
    assert (await _run(store)).status == STATUS_IDLE


# --- фото и «Где ты» ---


async def test_take_photo_in_the_cellar_draws_the_cellar_not_the_study(store):
    svc, notifier, link = _make(store, dice=Dice(0.0))
    await _active(svc, store)
    link.director_replies = [_dir(stage=1)]
    await _turn(svc)  # переход стадии — кадр сцены у двери подвала
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
    svc, notifier, link = _make(store)
    await places.set_at(store, GUEST, places.new_at(places.PLACE_CELLAR, svc._now()))
    assert await svc.tool_take_photo(GUEST, GUEST, {})
    await asyncio.gather(*list(svc._photo_tasks))
    request = link.generated[-1]
    assert "study" in request["description"] and "cellar" not in request["description"]
    assert request["light"] != places.UNDERGROUND_LIGHT_EN


# --- отладка ---


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
    svc, notifier, _ = _make(store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("cellar"), svc)
    assert message.answers == ["Стук отправлен."]
    assert notifier.texts(CANARY) == [cellar.OFFER_TEXT]
    assert (await _run(store)).status == STATUS_OFFERED
    # Второй раз — уже на ходу.
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("cellar"), svc)
    assert "уже на ходу" in message.answers[0]


async def test_debug_commands_for_others_show_the_old_usage(store):
    svc, notifier, _ = _make(store)
    for args in ("cellar", "reset all", "where", "что-то"):
        message = FakeMessage(GUEST)
        await cmd_interactives(message, FakeCommand(args), svc)
        assert message.answers == ["Использование: /interactives on | off"]
    assert notifier.sent == [] and await store.state_keys("") == []


async def test_debug_where_shows_raw_alfred_at(store):
    svc, _, _ = _make(store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("where"), svc)
    assert "в кабинете" in message.answers[0]
    await places.set_at(store, CANARY, places.new_at(places.PLACE_CELLAR, svc._now()))
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("where"), svc)
    assert json.loads(message.answers[0])["place"] == "cellar"


async def test_debug_reset_erases_stage_59_progress_but_not_the_cabinet(store):
    svc, _, link = _make(store, dice=Dice(0.0))
    await _to_hatch(svc, store, link)
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
    assert f"location:cabinet:{CANARY}" in keys  # кабинет цел
    assert f"user_effect:speech_clear:{CANARY}" in keys  # прочие флаги целы
    assert f"interactive_run:{CANARY}:radio" in keys
    assert f"interactive_run:{GUEST}:cellar" in keys  # чужой прогресс цел
    assert not [k for k in keys if k.startswith("user_effect:catacombs_open")]


async def test_debug_reset_unknown_scope_hints(store):
    svc, _, _ = _make(store)
    message = FakeMessage(CANARY)
    await cmd_interactives(message, FakeCommand("reset"), svc)
    assert "reset cellar | catacombs | all" in message.answers[0]


async def test_radio_scene_pulls_a_canary_back_to_the_study_but_canary_scenes_keep_place(store):
    import dataclasses
    import re

    svc, _, _ = _make(store)
    ghost = dataclasses.replace(
        cellar.CELLAR,
        id="ghost",
        trigger_re=re.compile("призрак"),
        stage_places=(),
        start_place=None,
    )
    engine.REGISTRY["ghost"] = ghost
    try:
        await svc._state.save_run(
            Run(scenario="ghost", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE)
        )
        await svc._move_to(CANARY, places.PLACE_CELLAR)
        plan = await svc.before_turn(CANARY, CANARY, "Ну как?", is_private=True)
        assert plan.scene and plan.scenario == "ghost"
        assert (await places.get_at(store, CANARY)).place == places.PLACE_CELLAR
        # А радио идёт в кабинете, где бы Альфред ни был.
        await svc._state.save_run(
            Run(scenario="ghost", chat_id=CANARY, user_id=CANARY, status=STATUS_IDLE)
        )
        radio_run = Run(scenario="radio", chat_id=CANARY, user_id=CANARY, status=STATUS_ACTIVE)
        await svc._state.save_run(radio_run)
        plan = await svc.before_turn(CANARY, CANARY, "Слышно плохо", is_private=True)
        assert plan.scenario == "radio"
        assert (await places.get_at(store, CANARY)).in_cabinet
    finally:
        engine.REGISTRY.pop("ghost", None)
