"""Кабинет Альфреда (Этап 49.2): Трансильвания, особенности от Ведущего, снимки."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from PIL import Image

from sa_home_bot.bot.interactives import cabinet, engine, radio, transylvania
from sa_home_bot.bot.interactives.base import Run
from sa_home_bot.bot.interactives.director import (
    FEATURES_SYSTEM,
    MOOD_PLAIN,
    MOODS,
    build_director_input,
    parse_decision,
)
from sa_home_bot.bot.interactives.engine import Interactives, photo_description
from sa_home_bot.bot.interactives.transylvania import Outside, Transylvania
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

GUEST = 601


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
        self.photos: list[tuple[int, object, str]] = []
        self.sent: list = []

    async def send_direct(self, chat_id, text, **_kw):
        self.sent.append((chat_id, text))
        return 1

    async def send_photo_ex(self, chat_id, photo, *, caption=None, **_kw):
        self.photos.append((chat_id, photo, caption))
        return 100 + len(self.photos), f"file-{len(self.photos)}"


class FakeLink:
    def __init__(self, features=("чучело совы на шкафу", "треснувший портрет")) -> None:
        self.features = list(features)
        self.calls: list[tuple[str, dict]] = []
        self.director_replies: list[str] = []

    inspect: dict | None = None

    async def command(self, action, args, dst=None, timeout=None):
        self.calls.append((action, args))
        if action == "chat" and args["system"] == FEATURES_SYSTEM:
            return {"response": json.dumps({"cabinet_add": self.features}, ensure_ascii=False)}
        if action == "chat":
            reply = self.director_replies.pop(0) if self.director_replies else "{}"
            return {"response": reply}
        if action == "generate_image":
            return {
                "png_b64": base64.b64encode(_png()).decode(),
                "width": 8,
                "height": 8,
                "prompt": "p",
                "seed": 7,
                **({"inspect": self.inspect} if self.inspect is not None else {}),
            }
        if action == "item_portrait":
            return {
                "png_b64": base64.b64encode(_png()).decode(),
                "width": 8,
                "height": 8,
                "seed": args["seed"],
                "full_prompt": args["prompt"],
                "seen": "Старое радио с микрофоном.",
                "missing": [],
            }
        raise AssertionError(action)

    def portraits(self) -> list[dict]:
        return [a for act, a in self.calls if act == "item_portrait"]

    def generated(self) -> list[dict]:
        return [a for act, a in self.calls if act == "generate_image"]


async def _night() -> int:
    return 3  # пасмурно


def _make(store, link):
    notifier = FakeNotifier()
    svc = Interactives(
        store,
        notifier,
        Settings(llm=LlmConfig(model="m")),
        lambda: link,
        now=lambda: datetime(2026, 9, 30, 21, 0, tzinfo=UTC),  # 00:00 в Бухаресте
        transylvania=Transylvania(fetch=_night),
    )
    return svc, notifier


async def _drain(svc) -> None:
    await asyncio.gather(*list(svc._photo_tasks))


# --- Трансильвания ---


def test_daylight_follows_the_season():
    def hours(d):
        rise, sset = transylvania.sun_times(d)
        return (sset - rise).total_seconds() / 3600

    assert hours(date(2026, 6, 21)) > 15 > 9 > hours(date(2026, 12, 21))
    rise, sset = transylvania.sun_times(date(2026, 9, 30))
    assert rise.astimezone(transylvania.TZ).hour == 7
    assert sset.astimezone(transylvania.TZ).hour == 19


def test_phase_by_local_time():
    at = lambda h: datetime(2026, 9, 30, h, 0, tzinfo=transylvania.TZ)  # noqa: E731
    assert transylvania.phase_at(at(2)) == transylvania.PHASE_NIGHT
    assert transylvania.phase_at(at(7)) == transylvania.PHASE_DAWN
    assert transylvania.phase_at(at(13)) == transylvania.PHASE_DAY
    assert transylvania.phase_at(at(19)) == transylvania.PHASE_DUSK


def test_rain_by_day_looks_like_evening():
    rainy = Outside(phase="day", weather="rain", local_time="13:00")
    assert rainy.light == "dusk"
    assert "rain" in rainy.en() and "gloomy" in rainy.en()
    assert "дождь" in rainy.ru()
    clear = Outside(phase="day", weather="clear", local_time="13:00")
    assert clear.light == "day" and clear.key != rainy.key


async def test_weather_is_cached_and_failure_is_just_no_weather():
    calls = []

    async def fetch():
        calls.append(1)
        if len(calls) > 1:
            raise OSError("нет сети")
        return 95

    t = [0.0]
    tr = Transylvania(fetch=fetch, clock=lambda: t[0])
    assert await tr.weather() == "storm"
    assert await tr.weather() == "storm" and len(calls) == 1
    t[0] = 4000
    assert await tr.weather() == "storm"  # сбой — прежнее значение
    assert await Transylvania(fetch=fetch).weather() is None


# --- особенности кабинета ---


def test_features_dedupe_and_limit():
    cab = cabinet.Cabinet(user_id=1)
    assert cab.add(["Сова на шкафу.", "сова на шкафу", "  ", "камин дымит"]) == [
        "Сова на шкафу",
        "камин дымит",
    ]
    cab.add([f"деталь {i}" for i in range(20)])
    assert len(cab.features) == cabinet.FEATURES_MAX
    assert cab.features[-1] == "деталь 19"


def test_director_parses_cabinet_add():
    raw = json.dumps({"stage": 0, "cabinet_add": ["липкий след на ковре", "", 5]})
    assert parse_decision(raw, 0).cabinet_add == ("липкий след на ковре",)
    assert parse_decision(json.dumps({"cabinet_add": "плесень"}), 0).cabinet_add == ("плесень",)
    assert parse_decision("{}", 0).cabinet_add == ()


def test_director_input_asks_for_first_features_and_forbids_contradictions():
    run = Run("radio", 1, 1)
    first = build_director_input(
        radio.RADIO, run, finale_allowed=False, place="Кабинет", outside="ночь", need_features=3
    )
    assert "придумай 3" in first and "cabinet_add" in first
    later = build_director_input(
        radio.RADIO, run, finale_allowed=False, place="Кабинет", outside="ночь"
    )
    assert "противоречь" in later
    assert "cabinet_add" not in build_director_input(radio.RADIO, run, finale_allowed=False)


async def test_scene_note_and_director_see_the_cabinet(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["жарко натоплено"]))
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="active"))
    plan = await svc.before_turn(GUEST, GUEST, "ну что там?", is_private=True)
    assert "Где ты:" in plan.note and "жарко натоплено" in plan.note
    assert "пасмурно" in plan.note
    link.director_replies = [json.dumps({"stage": 0, "cabinet_add": ["на ковре липкий след"]})]
    await svc.after_turn(plan, "Осматриваюсь", dialogue_id=1)
    director_input = link.calls[-1][1]["messages"][0]["content"]
    assert "жарко натоплено" in director_input
    cab = await cabinet.load(store, GUEST)
    # Добавленное по ходу сцены — след сцены, не постоянная особенность.
    assert cab.features == ["жарко натоплено"]
    assert cab.scene == ["на ковре липкий след"]
    assert "на ковре липкий след" in cab.describe_ru()


# --- снимок ---


async def test_first_photo_invents_features_then_same_state_is_reused(store):
    link = FakeLink()
    svc, notifier = _make(store, link)
    reply = await svc.tool_take_photo(GUEST, GUEST, {})
    assert "придёт" in reply
    await _drain(svc)
    cab = await cabinet.load(store, GUEST)
    assert cab.features == ["чучело совы на шкафу", "треснувший портрет"]
    (gen,) = link.generated()
    assert "чучело совы" in gen["description"] and "candlelight" in gen["description"]
    assert len(notifier.photos) == 1 and isinstance(notifier.photos[0][1], bytes)
    image = await store.image_by_id(next(iter(cab.photos.values())))
    assert image["purpose"] == engine.PHOTO_PURPOSE
    # Ничего не изменилось — тот же снимок по file_id, mycraft не трогаем.
    reply = await svc.tool_take_photo(GUEST, GUEST, {})
    assert "уже отправлен" in reply
    assert len(link.generated()) == 1
    assert notifier.photos[-1][1] == "file-1"
    # Новая особенность — новый снимок.
    cab.add(["на полу мокрые следы"])
    await cabinet.save(store, cab)
    await svc.tool_take_photo(GUEST, GUEST, {})
    await _drain(svc)
    assert len(link.generated()) == 2


async def test_focus_photo_is_always_new_and_uses_scene_mode(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "передатчик на столе"})
    await _drain(svc)
    (gen,) = link.generated()
    assert gen["mode"] == "scene" and gen["description"] == "передатчик на столе"
    assert "сова" not in gen["context"] and "fireplace" in gen["context"]


async def test_caption_names_the_subject_when_focus_is_empty(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    # Подпись о конкретном — крупный план, и он же сверяется на снимке.
    await svc.tool_take_photo(GUEST, GUEST, {"caption": "Вид из окна"})
    await _drain(svc)
    # Focus без expect — сверяем focus.
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "меч на стене"})
    await _drain(svc)
    # «Мой кабинет» — общий вид, как и раньше.
    await svc.tool_take_photo(GUEST, GUEST, {"caption": "Мой кабинет"})
    await _drain(svc)
    window, sword, room = link.generated()
    assert window["mode"] == "scene" and window["description"] == "Вид из окна"
    assert window["expect"] == ["Вид из окна"]
    assert sword["expect"] == ["меч на стене"]
    assert room["mode"] != "scene" and room["expect"] == []


async def test_one_photo_at_a_time_and_daily_limit(store, monkeypatch):
    link = FakeLink()
    svc, _ = _make(store, link)
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "камин"})
    assert await svc.tool_take_photo(GUEST, GUEST, {"focus": "окно"}) == cabinet.TOOL_PHOTO_BUSY
    await _drain(svc)
    monkeypatch.setattr(engine, "PHOTO_DAILY_LIMIT", 1)
    assert await svc.tool_take_photo(GUEST, GUEST, {"focus": "окно"}) == cabinet.TOOL_PHOTO_LIMIT


def test_overview_puts_features_first():
    cab = cabinet.Cabinet(user_id=1, features=[f"f{i}" for i in range(6)])
    outside = Outside(phase="night", weather=None, local_time="00:00")
    description, context = photo_description(cab, outside, "", None)
    assert description.startswith("Must be clearly visible: f4; f5.")
    assert "f3" not in description and context == ""
    description, _ = photo_description(cab, outside, "", "лампа вспыхнула")
    assert description.startswith("Main subject, just happened: лампа вспыхнула")


def test_smells_and_sounds_stay_out_of_cabinet_and_frame():
    cab = cabinet.Cabinet(user_id=1)
    assert cab.add(["Густой аромат лаванды", "за стеной хихиканье", "пятно сажи"]) == ["пятно сажи"]
    # Уже записанные до фильтра — остаются для Альфреда, но не идут в кадр.
    cab.features = ["сова", "пахнет полынью", "пятно сажи"]
    outside = Outside(phase="night", weather=None, local_time="00:00")
    description, _ = photo_description(cab, outside, "", None)
    assert "полын" not in description and "сова; пятно сажи" in description


def test_director_parses_photo():
    assert parse_decision(json.dumps({"photo": "передатчик"}), 0).photo == "передатчик"
    assert parse_decision(json.dumps({"photo": True}), 0).photo == ""
    assert parse_decision(json.dumps({"photo": None}), 0).photo is None
    assert parse_decision("{}", 0).photo is None
    run = Run("radio", 1, 1)
    text = build_director_input(radio.RADIO, run, finale_allowed=False, place="К", outside="ночь")
    assert '"photo"' in text and "запахи" in text


def test_director_parses_mood():
    assert parse_decision(json.dumps({"mood": "Жуть"}), 0).mood == "horror"
    assert parse_decision(json.dumps({"mood": "rot"}), 0).mood == "rot"
    assert parse_decision(json.dumps({"mood": "весело"}), 0).mood is None
    assert set(engine.MOOD_PRESETS) == set(MOODS.values()) - {MOOD_PLAIN}


async def test_mood_picks_model_and_lora_for_scene_frames(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="active"))
    await _scene_turn(svc, link, {"stage": 0, "mood": "гниль", "photo": "стол"})
    gen = link.generated()[-1]
    assert gen["model"] == "revanim" and gen["loras"] == [["rottech", 0.8]]
    # Настроение держится, пока Ведущий его не сменит — и в снимках Альфреда.
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "камин"})
    await _drain(svc)
    assert link.generated()[-1]["model"] == "revanim"
    await _scene_turn(svc, link, {"stage": 1, "mood": "обычно"})
    assert "model" not in link.generated()[-1]


async def _scene_turn(svc, link, reply: dict) -> None:
    plan = await svc.before_turn(GUEST, GUEST, "что там?", is_private=True)
    link.director_replies = [json.dumps(reply)]
    await svc.after_turn(plan, "Смотрю", dialogue_id=1)
    await _drain(svc)


async def test_director_frames_key_moments_not_too_often(store):
    link = FakeLink()
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="active"))
    # Ведущий просит кадр — снимаем крупно, с тем, что случилось.
    await _scene_turn(svc, link, {"stage": 0, "effect": "лампа вспыхнула", "photo": "лампа"})
    (gen,) = link.generated()
    assert gen["mode"] == "scene" and gen["description"] == "лампа"
    assert "лампа вспыхнула" in gen["context"]
    assert notifier.photos[-1][2] == engine.PHOTO_SCENE_CAPTION
    # Сразу ещё раз — рано, пропускаем.
    await _scene_turn(svc, link, {"stage": 0, "photo": "камин"})
    assert len(link.generated()) == 1
    # Переход стадии — кадр обязателен, даже без просьбы: общий вид.
    await _scene_turn(svc, link, {"stage": 1})
    assert len(link.generated()) == 2
    assert link.generated()[-1]["mode"] == "free"
    # Обычный ход без просьбы — без кадра.
    await _scene_turn(svc, link, {"stage": 1})
    assert len(link.generated()) == 2


@pytest.mark.parametrize("tool", ["take_photo"])
def test_tool_is_registered(tool):
    from sa_home_bot.bot import tools

    assert any(spec.name == tool for spec in tools.TOOLS)


# --- следы сцены (живая находка 2026-09-30) ---


async def test_scene_traces_leave_with_the_scene(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    cab = cabinet.Cabinet(user_id=GUEST, features=["сова на шкафу"])
    cab.add(["туман у стола", "из динамика шёпот", "пепельные пальцы"], scene=True)
    assert cab.scene == ["туман у стола", "пепельные пальцы"]  # звук — мимо
    await cabinet.save(store, cab)
    # Сцены нет (квест пройден) — снимок уже без её следов.
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="done"))
    reply = await svc.tool_take_photo(GUEST, GUEST, {})
    await _drain(svc)
    assert "туман" not in reply and "сова на шкафу" in reply
    assert (await cabinet.load(store, GUEST)).scene == []
    (gen,) = link.generated()
    assert "туман" not in gen["description"]


def test_describe_shows_few_visible_features():
    cab = cabinet.Cabinet(user_id=1, features=[f"деталь {i}" for i in range(10)])
    cab.features.append("в воздухе запах полыни")
    text = cab.describe_ru()
    assert "полыни" not in text
    assert text.count("деталь") == cabinet.DESCRIBE_MAX and "деталь 9" in text


# --- сверка снимка (Этап 49.2.1) ---


def test_photo_check_parse_counts_misses_from_answers():
    from sa_home_bot.llm import photo_check

    expect = ["собака у камина", "microphone :: a bare cable does NOT count", "портрет"]
    raw = json.dumps(
        {"description": "Стол,  камин.", "answers": {"1": "no", "2": "yes", "3": "maybe"}},
        ensure_ascii=False,
    )
    parsed = photo_check.parse(raw, expect)
    # Пункт без внятного ответа промахом не считается; в missing — название
    # без определения.
    assert parsed == {
        "description": "Стол, камин.",
        "missing": ["собака у камина"],
        "answers": {"собака у камина": False, "microphone": True},
    }
    raw = json.dumps({"description": "x", "answers": {"2": "no"}})
    assert photo_check.parse(raw, expect)["missing"] == ["microphone"]
    assert photo_check.parse("не json", ["x"]) is None
    assert photo_check.parse(json.dumps({"description": ""}), []) is None


def test_photo_check_question_numbers_items_with_definitions():
    from sa_home_bot.llm import photo_check

    q = photo_check.build_question(["radio", "mic :: a plug does NOT count"])
    assert "1. radio" in q and "2. mic — a plug does NOT count" in q


async def test_matching_photo_becomes_alfreds_note_only(store):
    link = FakeLink()
    link.inspect = {"description": "Камин, у огня спит рыжая собака.", "missing": []}
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "собака", "expect": ["собака у камина"]})
    await _drain(svc)
    (gen,) = link.generated()
    assert gen["expect"] == ["собака у камина"] and gen["keep_key"].startswith(f"snap-{GUEST}-")
    assert gen["negative_extra"] == cabinet.PHOTO_NEGATIVE_EN
    assert len(notifier.photos) == 1
    plan = await svc.before_turn(GUEST, GUEST, "а какого цвета собака?", is_private=True)
    assert "рыжая собака" in plan.note
    # Ведущему и кабинету — ничего.
    cab = await cabinet.load(store, GUEST)
    assert cab.features == ["сова"] and cab.scene == []


async def test_missing_subject_makes_alfred_offer_a_retake(store):
    link = FakeLink()
    link.inspect = {"description": "Пустой камин, собаки нет.", "missing": ["собака у камина"]}
    svc, notifier = _make(store, link)
    spoken: list[tuple[str, dict]] = []

    async def speak(chat_id, directive, where):
        spoken.append((directive, where))

    svc._speak = speak
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    args = {"expect": ["собака у камина"]}
    await svc.tool_take_photo(GUEST, GUEST, args, dialogue_id=55)
    await _drain(svc)
    assert len(notifier.photos) == 1  # снимок всё равно у гостя
    ((directive, where),) = spoken
    assert "собака у камина" in directive and "переснять" in directive
    assert where["dialogue_id"] == 55 and where["trigger_message_id"] == 101
    # Промах не становится общим видом кабинета.
    assert (await cabinet.load(store, GUEST)).photos == {}
    # Снова мимо — шутка про капризную плёнку, но переснять всё равно можно.
    await svc.tool_take_photo(GUEST, GUEST, args, dialogue_id=55)
    await _drain(svc)
    assert "капризн" in spoken[-1][0]
    first, second = link.generated()
    # Первый снимок — без упора, пересъёмка — с упором на пропущенное.
    assert "emphasize" not in first
    assert second["emphasize"] == ["собака у камина"]


def test_retake_emphasis_matches_rephrased_items():
    prev = {"missing": ["Собака у камина"]}
    assert engine.retake_emphasis(prev, ["собака", "кресло"]) == ["собака"]
    assert engine.retake_emphasis(prev, ["кресло"]) == []
    assert engine.retake_emphasis(None, ["собака"]) == []
    assert engine.retake_emphasis({"missing": []}, ["собака"]) == []
