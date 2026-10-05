"""Кабинет Альфреда (Этап 49.2): Трансильвания, особенности от Ведущего, снимки."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import UTC, date, datetime, timedelta

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
        if action == "chat" and "system" not in args:
            # Реплика Альфреда — подпись к снимку (engine._photo_line).
            return {"response": "Готово, сэр."}
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
        if action == "chat_progress":
            return {"partial": "draw", "done": False}
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

    def lines(self) -> list[str]:
        """Директивы подписей к снимкам."""
        return [
            a["messages"][0]["content"]
            for act, a in self.calls
            if act == "chat" and "system" not in a
        ]


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
    # Реплики Альфреда (служба tasks) — в список: (директива, куда).
    svc.spoken = []

    async def speak(chat_id, directive, where):
        svc.spoken.append((directive, where))

    svc._speak = speak
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
    assert reply == cabinet.TOOL_PHOTO_STARTED
    await _drain(svc)
    cab = await cabinet.load(store, GUEST)
    assert cab.features == ["чучело совы на шкафу", "треснувший портрет"]
    (gen,) = link.generated()
    assert "чучело совы" in gen["description"] and "candlelight" in gen["description"]
    assert len(notifier.photos) == 1 and isinstance(notifier.photos[0][1], bytes)
    image = await store.image_by_id(next(iter(cab.photos.values())))
    assert image["purpose"] == engine.PHOTO_PURPOSE
    # Ничего не изменилось, но снимок уже в этом чате — второй раз тот же не
    # шлём, снимаем заново (живая находка 2026-10-02).
    assert await svc.tool_take_photo(GUEST, GUEST, {}) == cabinet.TOOL_PHOTO_STARTED
    await _drain(svc)
    assert len(link.generated()) == 2
    # В другой чат — тот же снимок по file_id, mycraft не трогаем.
    reply = await svc.tool_take_photo(-100, GUEST, {})
    assert "уже отправлен" in reply
    assert len(link.generated()) == 2
    assert notifier.photos[-1][1] == "file-2"
    # Новая особенность — новый снимок и в другом чате.
    cab = await cabinet.load(store, GUEST)
    cab.add(["на полу мокрые следы"])
    await cabinet.save(store, cab)
    await svc.tool_take_photo(-100, GUEST, {})
    await _drain(svc)
    assert len(link.generated()) == 3


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


def test_director_parses_alfred():
    assert parse_decision(json.dumps({"alfred": "back"}), 0).alfred == "back"
    assert parse_decision(json.dumps({"alfred": " Side "}), 0).alfred == "side"
    for junk in ("front", "", 1, True, ["back"], None):
        assert parse_decision(json.dumps({"alfred": junk}), 0).alfred is None
    assert parse_decision("{}", 0).alfred is None
    run = Run("radio", 1, 1)
    text = build_director_input(radio.RADIO, run, finale_allowed=False, place="К", outside="ночь")
    assert '"alfred"' in text


def test_scene_description_with_alfred():
    cab = cabinet.Cabinet(user_id=1, features=["f1", "f2", "f3"])
    outside = Outside(phase="night", weather=None, local_time="00:00")
    phrase = cabinet.SCENE_ALFRED_EN["back"]
    description, _ = photo_description(cab, outside, "", "лампа вспыхнула", alfred="back")
    # После главного, до особенностей; особенностей остаётся одна.
    assert description.index("лампа вспыхнула") < description.index(phrase)
    assert description.index(phrase) < description.index("f3")
    assert "f2" not in description
    description, _ = photo_description(cab, outside, "стол", None, alfred="side")
    assert description.startswith("стол") and cabinet.SCENE_ALFRED_EN["side"] in description
    description, _ = photo_description(cab, outside, "", "лампа вспыхнула", alfred="front")
    assert "Alfred" not in description


async def test_alfred_in_scene_frame(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="active"))
    await _scene_turn(
        svc,
        link,
        {"stage": 0, "effect": "лампа вспыхнула", "mood": "гниль", "photo": True, "alfred": "back"},
    )
    gen = link.generated()[-1]
    assert gen["loras"] == [["rottech", 0.8], list(cabinet.ALFRED_LORA)]
    assert cabinet.SCENE_ALFRED_EN["back"] in gen["description"]
    params = json.loads((await store.image_by_id(1))["params"])
    assert params["alfred"] == "back"
    cab = await cabinet.load(store, GUEST)
    assert not cab.photos  # такие кадры в кэш повторного показа не идут
    # Без поля — как раньше.
    await _scene_turn(svc, link, {"stage": 1, "mood": "гниль", "photo": "стол"})
    gen = link.generated()[-1]
    assert gen["loras"] == [["rottech", 0.8]]
    assert "Alfred" not in gen["description"] + gen["context"]


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
    assert await svc.tool_take_photo(GUEST, GUEST, {}) == cabinet.TOOL_PHOTO_STARTED
    await _drain(svc)
    (directive,) = link.lines()
    assert "туман" not in directive and "сова на шкафу" in directive
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


async def test_alfred_line_comes_with_the_photo_as_its_caption(store):
    """Живая находка 2026-10-02: Альфред описывал снимок до того, как он
    был готов, а потом — длинно. Теперь тул молчит, а короткая реплика на
    то, что вышло, — подписью к самому снимку и ходом треда."""
    link = FakeLink()
    link.inspect = {"description": "Камин, у огня спит рыжая собака.", "missing": []}
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    reply = await svc.tool_take_photo(
        GUEST, GUEST, {"focus": "собака", "expect": ["собака"]}, dialogue_id=55
    )
    assert reply == cabinet.TOOL_PHOTO_STARTED
    await _drain(svc)
    (directive,) = link.lines()
    assert "рыжая собака" in directive and "сфотографировал собака" in directive
    assert "сова" in directive  # где Альфред — к реплике
    ((_, _, caption),) = notifier.photos
    assert caption == "<b>Альфред:</b> Готово, сэр."
    turns = await store.ai_turns_for_dialogue(GUEST, 55)
    assert [(t["role"], t["content"]) for t in turns] == [("assistant", "Готово, сэр.")]
    assert svc.spoken == []


async def test_take_photo_joins_the_scene_photo_and_speaks_with_it(store):
    """Живая находка 2026-10-02: снимок сцены (Ведущий, в фоне) уже шёл, а
    take_photo отвечал «занято» — Альфред говорил до снимка и звал тул
    снова. Теперь ход присоединяется: реплика — подписью к снимку сцены."""
    link = FakeLink()
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    started = await svc._start_photo(
        GUEST,
        GUEST,
        focus="передатчик",
        caption=engine.PHOTO_SCENE_CAPTION,
        happening="по корпусу прошла рябь",
        outside=Outside(phase="night", weather=None, local_time="00:00"),
        message_thread_id=None,
        trigger_message_id=None,
    )
    assert started
    reply = await svc.tool_take_photo(GUEST, GUEST, {"caption": "Рябь"}, dialogue_id=56)
    assert reply == cabinet.TOOL_PHOTO_STARTED
    # Второй ход к тому же снимку уже не присоединится.
    assert await svc.tool_take_photo(GUEST, GUEST, {}) == cabinet.TOOL_PHOTO_BUSY
    await svc.wait_photo(GUEST)
    await _drain(svc)
    assert len(link.generated()) == 1
    ((_, _, caption),) = notifier.photos
    assert caption == "<b>Альфред:</b> Готово, сэр."
    turns = await store.ai_turns_for_dialogue(GUEST, 56)
    assert [(t["role"], t["content"]) for t in turns] == [("assistant", "Готово, сэр.")]


async def test_without_photo_check_alfred_tells_what_he_shot(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "меч на стене"})
    await _drain(svc)
    (directive,) = link.lines()
    assert "На снимке: меч на стене" in directive


async def test_scene_frames_are_not_narrated(store):
    link = FakeLink()
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc._start_photo(
        GUEST,
        GUEST,
        focus="туман",
        caption="кадр",
        happening=None,
        outside=await svc._transylvania.outside(svc._now()),
        message_thread_id=None,
        trigger_message_id=None,
    )
    await _drain(svc)
    assert link.lines() == [] and notifier.photos[0][2] == "кадр"


async def test_photo_statuses_follow_the_phases(store, monkeypatch):
    """Ход ждёт снимок: промптер — «наводит фотоаппарат», рисование и
    подпись — «проявляет снимок»."""
    monkeypatch.setattr(engine, "PHOTO_PHASE_POLL_S", 0)
    phases = ["compose", "compose", "draw"]
    gate = asyncio.Event()

    class PhasedLink(FakeLink):
        async def command(self, action, args, dst=None, timeout=None):
            if action == "chat_progress":
                if phases:
                    return {"partial": phases.pop(0), "done": False}
                gate.set()
                return {"partial": "draw", "done": True}
            if action == "generate_image":
                await gate.wait()
            return await super().command(action, args, dst, timeout)

    link = PhasedLink()
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    shown: list[str] = []

    async def on_status(text):
        shown.append(text)

    await svc.tool_take_photo(GUEST, GUEST, {"focus": "камин"})
    await svc.wait_photo(GUEST, on_status)
    assert shown == [
        cabinet.PHOTO_STATUS_AIMING,
        cabinet.PHOTO_STATUS_DEVELOPING,
        cabinet.PHOTO_STATUS_DEVELOPING,
    ]
    assert len(notifier.photos) == 1
    # Снимка нет — ждать нечего.
    await svc.wait_photo(GUEST, on_status)


async def test_missing_subject_makes_alfred_offer_a_retake(store):
    link = FakeLink()
    link.inspect = {"description": "Пустой камин, собаки нет.", "missing": ["собака у камина"]}
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    args = {"expect": ["собака у камина"]}
    await svc.tool_take_photo(GUEST, GUEST, args, dialogue_id=55)
    await _drain(svc)
    assert len(notifier.photos) == 1  # снимок всё равно у гостя
    # Удивление — подписью к тому же снимку, не отдельной репликой.
    (directive,) = link.lines()
    assert "собака у камина" in directive and "переснять" in directive
    assert svc.spoken == []
    # Промах не становится общим видом кабинета.
    assert (await cabinet.load(store, GUEST)).photos == {}
    # Снова мимо — шутка про капризную плёнку, но переснять всё равно можно.
    await svc.tool_take_photo(GUEST, GUEST, args, dialogue_id=55)
    await _drain(svc)
    assert "капризн" in link.lines()[-1]
    first, second = link.generated()
    # Первый снимок — без упора, пересъёмка — с упором на пропущенное.
    assert "emphasize" not in first
    assert second["emphasize"] == ["собака у камина"]


async def test_missing_subject_on_a_scene_frame_is_a_separate_line(store):
    link = FakeLink()
    link.inspect = {"description": "Пустой стол.", "missing": ["туман"]}
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc._start_photo(
        GUEST,
        GUEST,
        focus="туман",
        caption="кадр",
        happening=None,
        outside=await svc._transylvania.outside(svc._now()),
        message_thread_id=None,
        trigger_message_id=None,
        expect=["туман"],
        dialogue_id=55,
    )
    await _drain(svc)
    ((directive, where),) = svc.spoken
    assert "туман" in directive and where["trigger_message_id"] == 101


def test_retake_emphasis_matches_rephrased_items():
    prev = {"missing": ["Собака у камина"]}
    assert engine.retake_emphasis(prev, ["собака", "кресло"]) == ["собака"]
    assert engine.retake_emphasis(prev, ["кресло"]) == []
    assert engine.retake_emphasis(None, ["собака"]) == []
    assert engine.retake_emphasis({"missing": []}, ["собака"]) == []


async def test_lost_photo_is_announced_not_silent(store):
    """Живая находка 2026-10-02: mycraft перезапустили посреди съёмки —
    Альфред «снял», а снимка нет и ни слова. Теперь гостю говорят."""
    from sa_home_bot.proto.messages import ProtoError

    class DeadLink(FakeLink):
        async def command(self, action, args, dst=None, timeout=None):
            if action == "generate_image":
                raise ProtoError("unavailable", "mycraft: соединение закрыто")
            return await super().command(action, args, dst, timeout)

    svc, notifier = _make(store, DeadLink())
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "камин"})
    await _drain(svc)
    assert notifier.photos == []
    ((chat, text),) = notifier.sent
    assert chat == GUEST and text in cabinet.PHOTO_LOST_TEXTS
    assert await store.get_state(cabinet.PHOTO_PENDING_KEY) == "{}"
    # Снова можно снимать — камера не «занята» пропавшим снимком.
    assert await svc.tool_take_photo(GUEST, GUEST, {}) == cabinet.TOOL_PHOTO_STARTED
    await _drain(svc)


async def test_photo_cut_by_restart_is_announced_on_start(store):
    link = FakeLink()
    svc, notifier = _make(store, link)
    await svc._mark_photo_pending(GUEST, None, 77)
    # Бот перезапущен: новая служба находит недоснятый снимок.
    fresh, fresh_notifier = _make(store, link)
    await fresh.recover()
    assert [c for c, _ in fresh_notifier.sent] == [GUEST]
    assert fresh_notifier.sent[0][1] in cabinet.PHOTO_LOST_TEXTS
    await fresh.recover()
    assert len(fresh_notifier.sent) == 1


async def test_delivered_photo_leaves_nothing_pending(store):
    svc, notifier = _make(store, FakeLink())
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "камин"})
    await _drain(svc)
    assert len(notifier.photos) == 1 and notifier.sent == []
    assert await store.get_state(cabinet.PHOTO_PENDING_KEY) == "{}"


# --- портрет Альфреда к приветствию (2026-10-05) ---


async def test_greeting_portrait_draws_alfred_in_guests_study_with_light(store):
    link = FakeLink()
    svc, notifier = _make(store, link)
    assert svc.start_greeting_portrait(GUEST, GUEST, trigger_message_id=55)
    # Пока рисуется — второй в тот же чат не начинаем.
    assert not svc.start_greeting_portrait(GUEST, GUEST)
    await _drain(svc)
    (gen,) = link.generated()
    assert gen["loras"] == [list(cabinet.ALFRED_LORA)]
    assert gen["description"].startswith(cabinet.PORTRAIT_SUBJECT_EN)
    # Свет — тот же, что у снимков кабинета (ночь, пасмурно); обстановка гостя.
    assert "candlelight" in gen["description"] and "треснувший портрет" in gen["description"]
    assert cabinet.CANON_EN in gen["description"]
    (chat, photo, caption) = notifier.photos[0]
    assert chat == GUEST and isinstance(photo, bytes) and caption is None
    # Особенности кабинета придуманы и сохранены, как при первом снимке.
    assert (await cabinet.load(store, GUEST)).features
    # Портрет — не снимок кабинета: повторный показ снимков его не берёт.
    assert not (await cabinet.load(store, GUEST)).photos
    assert svc.start_greeting_portrait(GUEST, GUEST)
    await _drain(svc)
    assert len(link.generated()) == 2


async def test_greeting_portrait_failure_is_silent(store):
    class Down(FakeLink):
        async def command(self, action, args, dst=None, timeout=None):
            if action == "generate_image":
                raise TimeoutError
            return await super().command(action, args, dst, timeout)

    svc, notifier = _make(store, Down())
    assert svc.start_greeting_portrait(GUEST, GUEST)
    await _drain(svc)
    assert notifier.photos == [] and notifier.sent == []
    assert svc.start_greeting_portrait(GUEST, GUEST)


async def test_selfie_puts_alfred_into_the_same_study_light_and_mood(store):
    """«Сфоткай себя» (2026-10-05): тот же кабинет гостя, свет и настроение
    сцены, что у снимка кабинета, но в кадре сам Альфред — LoRA облика."""
    link = FakeLink()
    svc, notifier = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    reply = await svc.tool_take_photo(
        GUEST, GUEST, {"selfie": True, "focus": "у окна с бокалом вина"}, dialogue_id=7
    )
    assert reply == cabinet.TOOL_PHOTO_STARTED
    await _drain(svc)
    (gen,) = link.generated()
    assert gen["mode"] == "free" and gen["loras"] == [list(cabinet.ALFRED_LORA)]
    assert gen["description"].startswith("Main subject: Alfred")
    assert "у окна с бокалом вина" in gen["description"]
    assert "candlelight" in gen["description"] and "сова" in gen["description"]
    (directive,) = link.lines()
    assert "сфотографировал себя" in directive
    image = await store.image_by_id(1)
    assert json.loads(image["params"])["selfie"] is True
    # Снимок себя — не общий вид кабинета: повторным показом не станет.
    assert not (await cabinet.load(store, GUEST)).photos
    assert len(notifier.photos) == 1
    # Настроение сцены — вторая LoRA к облику, модель — от настроения.
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="active", mood="rot"))
    await svc.tool_take_photo(GUEST, GUEST, {"selfie": True})
    await _drain(svc)
    gen = link.generated()[-1]
    assert gen["model"] == "revanim"
    assert gen["loras"] == [list(cabinet.ALFRED_LORA), ["rottech", 0.8]]
    assert gen["description"].startswith(cabinet.SELFIE_SUBJECT_EN)


async def test_selfie_by_word_without_flag(store):
    link = FakeLink()
    svc, _ = _make(store, link)
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    await svc.tool_take_photo(GUEST, GUEST, {"focus": "себя"})
    await _drain(svc)
    await svc.tool_take_photo(GUEST, GUEST, {"caption": "Селфи"})
    await _drain(svc)
    for gen in link.generated():
        assert gen["loras"] == [list(cabinet.ALFRED_LORA)]
        assert gen["description"].startswith(cabinet.SELFIE_SUBJECT_EN)


# --- эмоциональное селфи к ответу (2026-10-05) ---

def _mood(emotion="anger", intensity=3, action="slamming a book shut") -> str:
    return json.dumps({"emotion": emotion, "intensity": intensity, "action": action})


async def _mood_svc(store, link, *, roll=0.0):
    svc, notifier = _make(store, link)
    svc._rng = lambda: roll
    await cabinet.save(store, cabinet.Cabinet(user_id=GUEST, features=["сова"]))
    return svc, notifier


async def _selfie(svc, reply="Это возмутительно, сэр & всё такое.", **kw):
    return await svc.mood_selfie(
        GUEST, GUEST, "ты дурак", reply, message_thread_id=9, trigger_message_id=42, **kw
    )


async def test_mood_selfie_carries_the_reply_as_caption_with_face_emphasized(store):
    link = FakeLink()
    link.director_replies = [_mood()]
    svc, notifier = await _mood_svc(store, link)
    statuses: list[str] = []

    async def on_status(text):
        statuses.append(text)

    sent_id = await _selfie(svc, on_status=on_status)
    assert sent_id == 101
    (gen,) = link.generated()
    assert gen["loras"] == [list(cabinet.ALFRED_LORA)] and gen["mode"] == "free"
    face = cabinet.SELFIE_EMOTION_FACE_EN["anger"]
    assert gen["emphasize"] == [face]
    assert gen["description"].startswith("Main subject: Alfred")
    assert face in gen["description"] and "slamming a book shut" in gen["description"]
    assert "candlelight" in gen["description"] and "сова" in gen["description"]
    # Сверка зрением не включалась.
    assert "expect" not in gen and "keep_key" not in gen
    # Подпись — сам ответ (html-экранированный), а не новая реплика LLM.
    (chat, _photo, caption) = notifier.photos[0]
    assert chat == GUEST
    assert caption == engine.ALFRED_PHOTO_PREFIX + "Это возмутительно, сэр &amp; всё такое."
    assert link.lines() == []
    assert statuses[0] == cabinet.PHOTO_STATUS_AIMING
    image = await store.image_by_id(1)
    assert image["purpose"] == cabinet.SELFIE_MOOD_PURPOSE and image["message_id"] == 101
    params = json.loads(image["params"])
    assert params["selfie"] is True and params["emotion"] == "anger"


async def test_mood_selfie_about_him_uses_action_only_and_scene_mood(store):
    link = FakeLink()
    link.director_replies = [_mood("about_him", 2, "pointing at the old radio on his desk.")]
    svc, _ = await _mood_svc(store, link, roll=0.1)
    await svc._state.save_run(Run("radio", GUEST, GUEST, status="active", mood="rot"))
    assert await _selfie(svc) is not None
    (gen,) = link.generated()
    assert gen["emphasize"] == ["pointing at the old radio on his desk"]
    assert gen["model"] == "revanim"
    assert gen["loras"] == [list(cabinet.ALFRED_LORA), ["rottech", 0.8]]


@pytest.mark.parametrize(
    ("reply", "roll"),
    [
        (_mood("none", 3), 0.0),
        (_mood("anger", 1), 0.0),
        (_mood("anger", 2), 0.3),  # шанс 0.25
        (_mood("anger", 3), 0.6),  # шанс 0.6
        (_mood("rage", 3), 0.0),
        ("не json", 0.0),
        ("{}", 0.0),
    ],
)
async def test_mood_selfie_classifier_and_roll_decide(store, reply, roll):
    link = FakeLink()
    link.director_replies = [reply]
    svc, notifier = await _mood_svc(store, link, roll=roll)
    assert await _selfie(svc) is None
    assert link.generated() == [] and notifier.photos == []


@pytest.mark.parametrize(("intensity", "roll"), [(2, 0.24), (3, 0.59)])
async def test_mood_selfie_roll_thresholds_pass(store, intensity, roll):
    link = FakeLink()
    link.director_replies = [_mood("joy", intensity)]
    svc, notifier = await _mood_svc(store, link, roll=roll)
    assert await _selfie(svc) is not None
    assert len(notifier.photos) == 1


async def test_mood_selfie_cooldown_daily_limit_and_length_without_gpu(store):
    link = FakeLink()
    link.director_replies = [_mood()] * 10
    svc, notifier = await _mood_svc(store, link)
    assert await _selfie(svc) is not None
    # Сразу же — кулдаун: классификатор даже не спрашиваем.
    asked = len(link.calls)
    assert await _selfie(svc) is None
    assert len(link.calls) == asked
    # Через каждые 3 часа можно; всего — SELFIE_MOOD_DAILY в сутки (images
    # пишутся по часам службы — двигаем их).
    start = datetime(2026, 9, 30, 21, 0, tzinfo=UTC)
    clock = {"now": start}
    svc._now = lambda: clock["now"]
    for _ in range(cabinet.SELFIE_MOOD_DAILY - 1):
        clock["now"] += timedelta(hours=3)
        assert await _selfie(svc) is not None
    clock["now"] += timedelta(hours=3)
    assert await _selfie(svc) is None
    # Слишком длинная реплика не влезает в подпись.
    clock["now"] += timedelta(days=2)
    asked = len(link.calls)
    assert await _selfie(svc, reply="а" * (cabinet.PHOTO_LINE_MAX + 1)) is None
    assert len(link.calls) == asked


async def test_mood_selfie_generation_failure_is_silent_and_not_counted(store):
    class Down(FakeLink):
        async def command(self, action, args, dst=None, timeout=None):
            if action == "generate_image":
                raise TimeoutError
            return await super().command(action, args, dst, timeout)

    link = Down()
    link.director_replies = [_mood()]
    svc, notifier = await _mood_svc(store, link)
    assert await _selfie(svc) is None
    assert notifier.photos == []
    # Чат не залип «занятым»; неудача не съела кулдаун.
    assert GUEST not in svc._photo_busy
    assert await svc._mood_selfie_gated(GUEST, "Хорошо.")


async def test_mood_selfie_send_failure_returns_none(store):
    link = FakeLink()
    link.director_replies = [_mood()]
    svc, notifier = await _mood_svc(store, link)

    async def refuse(*_a, **_kw):
        return None

    notifier.send_photo_ex = refuse
    assert await _selfie(svc) is None


async def test_mood_selfie_needs_node_and_photo_sending(store):
    link = FakeLink()
    svc, _ = await _mood_svc(store, link)
    svc._get_node_link = lambda: None
    assert await _selfie(svc) is None
    svc2, _ = await _mood_svc(store, link)
    svc2._notifier = object()
    assert await _selfie(svc2) is None
