"""«Альфред в городе» (Этап 51): разбор срока, остаток, потолок 24 ч,
продление, перехват (владелец тоже), проход планировщика (напоминание,
«задерживается», принудительный возврат) и разбор очереди при возвращении
(порядок, голосовые, повтор после падения).

Модель, wake и распознавание замоканы: проверяется оркестрация (bot/away.py,
bot/away_return.py, хендлеры), а не LLM."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from sa_home_bot import wake_core
from sa_home_bot.bot import ai_flow, away, away_return, voice_stt
from sa_home_bot.bot.away import (
    ALFRED_AWAY_KEY,
    MAX_AWAY,
    AwayError,
    AwayService,
    AwayState,
    clamp_until,
    format_remaining,
    note_text,
    parse_away_args,
    parse_duration,
    parse_extend,
    status_text,
)
from sa_home_bot.bot.away_return import AwayReturn, AwayRunner
from sa_home_bot.bot.handlers import ai as ai_handler
from sa_home_bot.bot.handlers import away as away_handler
from sa_home_bot.bot.handlers import draw as draw_handler
from sa_home_bot.bot.handlers import interactives as interactives_handler
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

T0 = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
UTC_TZ = UTC


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def _settings(tmp_path) -> Settings:
    settings = Settings()
    settings.llm.response_mode = "typing_plain"
    settings.database.path = tmp_path / "bot.sqlite"
    return settings


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "bot.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(store, tmp_path, clock):
    return AwayService(store, _settings(tmp_path), clock=clock)


# ------------------------------------------------------------------ фейки


class FakeBot:
    def __init__(self, files: dict[str, bytes] | None = None, fail: bool = False) -> None:
        self.files = files or {}
        self.fail = fail
        self.downloads: list[object] = []

    async def download(self, obj, destination=None):
        self.downloads.append(obj)
        if self.fail:
            raise RuntimeError("не скачалось")
        data = self.files.get(getattr(obj, "file_id", obj), b"audio-bytes")
        if destination is not None:
            destination.write_bytes(data)
            return None
        return io.BytesIO(data)


@dataclass
class FakeChat:
    id: int
    type: str = "private"


@dataclass
class FakeUser:
    id: int = 7
    first_name: str = "Гость"
    last_name: str | None = None
    username: str | None = None

    @property
    def full_name(self) -> str:
        return self.first_name


@dataclass
class FakeFile:
    file_id: str
    duration: int = 5


@dataclass
class FakeSticker:
    emoji: str | None = "😂"


class FakeMessage:
    _next_id = 5000

    def __init__(
        self,
        chat_id=1,
        text=None,
        *,
        chat_type="private",
        voice=None,
        video_note=None,
        audio=None,
        photo=None,
        caption=None,
        sticker=None,
        thread=None,
        user=None,
        bot=None,
    ):
        self.chat = FakeChat(chat_id, chat_type)
        self.message_id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.text = text
        self.caption = caption
        self.voice = voice
        self.video_note = video_note
        self.audio = audio
        self.photo = photo
        self.sticker = sticker
        self.message_thread_id = thread
        self.is_topic_message = bool(thread)
        self.from_user = user if user is not None else FakeUser()
        self.reply_to_message = None
        self.quote = None
        self.entities = None
        self.bot = bot or FakeBot()
        self.sent: list[str] = []

    async def reply(self, text, **kwargs):
        self.sent.append(text)

    async def answer(self, text, **kwargs):
        self.sent.append(text)


class FakeNotifier:
    def __init__(self, bot=None) -> None:
        self.bot = bot or FakeBot()
        self.sent: list[tuple[int, str, object]] = []

    async def send_direct(
        self, chat_id, text, reply_to_message_id=None, reply_markup=None, message_thread_id=None
    ):
        self.sent.append((chat_id, text, reply_markup))
        return 1


def _owner_book() -> SubscriptionBook:
    return SubscriptionBook(
        [Subscription(chat_id=999, name="owner", allowed_commands=frozenset({"*"}))]
    )


async def _away(service, hours=3, **kwargs) -> AwayState:
    state, _ = await service.start(service.now() + timedelta(hours=hours), **kwargs)
    return state


# ------------------------------------------------------------- разбор срока


def test_parse_duration_forms():
    assert parse_duration("3ч") == (timedelta(hours=3), "")
    assert parse_duration("1ч30м датасет") == (timedelta(hours=1, minutes=30), "датасет")
    assert parse_duration("90 минут") == (timedelta(minutes=90), "")
    assert parse_duration("1.5h") == (timedelta(minutes=90), "")
    assert parse_duration("2 часа обучение LoRA") == (timedelta(hours=2), "обучение LoRA")
    # число без единицы и чужое слово — не срок
    assert parse_duration("3") is None
    assert parse_duration("датасет 3ч") is None


def test_parse_away_args_duration_and_reason():
    until, reason = parse_away_args("3ч обновление", T0, UTC_TZ)
    assert until == T0 + timedelta(hours=3)
    assert reason == "обновление"


def test_parse_away_args_clock_today_and_tomorrow_rollover():
    tz = ZoneInfo("Asia/Almaty")  # T0 = 17:00 по Алматы
    until, reason = parse_away_args("до 23:00 датасет", T0, tz)
    assert until == datetime(2026, 10, 4, 18, 0, tzinfo=UTC)  # 23:00 Алматы
    assert reason == "датасет"
    # 16:00 по Алматы уже прошло — это завтра
    until, _ = parse_away_args("до 16:00", T0, tz)
    assert until == datetime(2026, 10, 5, 11, 0, tzinfo=UTC)


def test_parse_away_args_tomorrow():
    until, reason = parse_away_args("завтра 10:00 переезд", T0, UTC_TZ)
    assert until == datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    assert reason == "переезд"


@pytest.mark.parametrize("text", ["", "   ", "скоро", "3", "до 25:00", "завтра", "0ч"])
def test_parse_away_args_rejects(text):
    with pytest.raises(AwayError):
        parse_away_args(text, T0, UTC_TZ)


def test_parse_extend():
    assert parse_extend("+1ч") == timedelta(hours=1)
    assert parse_extend("+30м") == timedelta(minutes=30)
    with pytest.raises(AwayError):
        parse_extend("+скоро")


# ---------------------------------------------------------- остаток времени


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (2 * 3600 + 15 * 60, "через 2 ч 15 мин"),
        (2 * 3600 + 14 * 60 + 10, "через 2 ч 15 мин"),  # минуты вверх
        (3600, "через 1 ч"),
        (40 * 60, "через 40 мин"),
        (61, "через 2 мин"),
        (59, "меньше чем через минуту"),
        (1, "меньше чем через минуту"),
        (0, "вот-вот должен вернуться"),
        (-500, "вот-вот должен вернуться"),
    ],
)
def test_format_remaining(seconds, expected):
    assert format_remaining(T0 + timedelta(seconds=seconds), T0) == expected


def _state(**kwargs) -> AwayState:
    base = dict(until=T0 + timedelta(hours=3), started_at=T0)
    base.update(kwargs)
    return AwayState(**base)


def test_note_text_variants_use_castle_clock():
    # T0 = 12:00 UTC = 15:00 в Бухаресте (EEST)
    state = _state(until=T0 + timedelta(hours=3))
    assert note_text(state, T0) == (
        "Альфред спустился в город по делам. Обещал вернуться к 18:00 по замковым "
        "часам (через 3 ч)."
    )
    extended = _state(until=T0 + timedelta(minutes=40), extended=1)
    assert note_text(extended, T0) == (
        "Альфред задерживается в городе — передаёт, что будет к 15:40 (через 40 мин)."
    )
    overdue = _state(until=T0 - timedelta(minutes=5))
    assert note_text(overdue, T0) == "Альфред задерживается в городе — вот-вот должен вернуться."
    returning = _state(phase=away.PHASE_RETURNING)
    assert "вот-вот должен вернуться" in note_text(returning, T0)


def test_note_text_marks_tomorrow():
    state = _state(until=T0 + timedelta(hours=12))
    assert "к 03:00 завтра" in note_text(state, T0)


def test_status_text():
    state = _state(extended=1, set_by="claude", reason="датасет <b>")
    text = status_text(state, T0, UTC_TZ)
    assert text == (
        "Альфред в городе до 15:00 (через 3 ч), продлевали 1 раз, включил: Claude, "
        "причина: датасет &lt;b&gt;."
    )
    assert "Альфред и так дома" in status_text(None, T0, UTC_TZ)


# ------------------------------------------------------------------- потолок


def test_clamp_until():
    until, clamped = clamp_until(T0 + timedelta(hours=30), T0)
    assert until == T0 + MAX_AWAY and clamped
    until, clamped = clamp_until(T0 + timedelta(hours=2), T0)
    assert until == T0 + timedelta(hours=2) and not clamped


async def test_start_clamps_to_ceiling(service):
    state, clamped = await service.start(service.now() + timedelta(hours=40))
    assert clamped
    assert state.until == state.started_at + MAX_AWAY


async def test_start_refuses_past_and_double(service):
    with pytest.raises(AwayError):
        await service.start(service.now() - timedelta(minutes=1))
    await _away(service)
    with pytest.raises(AwayError, match="уже в городе"):
        await _away(service)


async def test_extend_counts_resets_notes_and_respects_ceiling(service, clock):
    await _away(service, hours=3, reason="датасет", set_by="claude")
    msg = FakeMessage(1, "привет")
    await service.intercept(msg, kind=away.KIND_TEXT, text="привет", dialogue_id=msg.message_id)
    assert (await service.load()).noted

    state = await service.extend(timedelta(hours=1))
    assert state.extended == 1
    assert state.until == T0 + timedelta(hours=4)
    assert state.noted == {}  # записка после продления — заново
    assert state.set_by == "claude" and state.reason == "датасет"

    # до потолка осталось 20 часов: +30ч урезается, дальше продлевать нельзя
    state = await service.extend(timedelta(hours=30))
    assert state.until == T0 + MAX_AWAY
    with pytest.raises(AwayError, match="Потолок"):
        await service.extend(timedelta(minutes=30))


async def test_extend_after_deadline_counts_from_now(service, clock):
    await _away(service, hours=1)
    clock.advance(hours=2)  # срок прошёл, Альфред «задерживается»
    state = await service.extend(timedelta(hours=1))
    assert state.until == clock.now + timedelta(hours=1)


async def test_extend_without_away_fails(service):
    with pytest.raises(AwayError):
        await service.extend(timedelta(hours=1))


async def test_back_immediate_and_with_queue(service):
    await _away(service)
    state, immediate = await service.back()
    assert immediate and await service.load() is None

    await _away(service)
    msg = FakeMessage(1, "ау")
    await service.intercept(msg, kind=away.KIND_TEXT, text="ау", dialogue_id=msg.message_id)
    state, immediate = await service.back()
    assert not immediate
    assert (await service.load()).phase == away.PHASE_RETURNING
    with pytest.raises(AwayError):
        await service.extend(timedelta(hours=1))


async def test_corrupted_state_means_home(store, service):
    await store.set_state(ALFRED_AWAY_KEY, "{не json")
    assert await service.load() is None
    msg = FakeMessage(1, "привет")
    assert not await service.intercept(msg, kind=away.KIND_TEXT, text="привет")


# ------------------------------------------------------------------- перехват


async def test_intercept_when_home_does_nothing(service, store):
    msg = FakeMessage(1, "привет")
    assert not await service.intercept(msg, kind=away.KIND_TEXT, text="привет", dialogue_id=1)
    assert msg.sent == []
    assert await store.ai_turn(1, msg.message_id) is None


async def test_intercept_records_turn_pending_and_notes(service, store, clock):
    await _away(service)
    first = FakeMessage(1, "привет", user=FakeUser(7, "Аня", username="anya"))
    assert await service.intercept(first, kind=away.KIND_TEXT, text="привет", dialogue_id=111)
    assert first.sent and "Обещал вернуться к 18:00" in first.sent[0]

    turn = await store.ai_turn(1, first.message_id)
    assert turn["role"] == "user" and turn["content"] == "привет"
    assert turn["user_name"] == "Аня (@anya)"
    state = await service.load()
    entry = state.pending["1"]
    assert entry["dialogue_id"] == 111
    assert entry["last_message_id"] == first.message_id
    assert entry["user"]["username"] == "anya"

    # второе сообщение сводится в тот же диалог, записка не чаще раза в 30 мин
    second = FakeMessage(1, "ты тут?")
    await service.intercept(second, kind=away.KIND_TEXT, text="ты тут?", dialogue_id=222)
    assert second.sent == []
    assert (await store.ai_turn(1, second.message_id))["dialogue_id"] == 111
    entry = (await service.load()).pending["1"]
    assert entry["last_message_id"] == second.message_id and entry["dialogue_id"] == 111

    clock.advance(minutes=31)
    third = FakeMessage(1, "ау")
    await service.intercept(third, kind=away.KIND_TEXT, text="ау", dialogue_id=333)
    assert third.sent  # прошло N минут — записка снова


async def test_intercept_note_again_after_extend(service):
    await _away(service)
    a = FakeMessage(1, "раз")
    await service.intercept(a, kind=away.KIND_TEXT, text="раз", dialogue_id=1)
    b = FakeMessage(1, "два")
    await service.intercept(b, kind=away.KIND_TEXT, text="два", dialogue_id=1)
    assert a.sent and not b.sent
    await service.extend(timedelta(hours=1))
    c = FakeMessage(1, "три")
    await service.intercept(c, kind=away.KIND_TEXT, text="три", dialogue_id=1)
    assert c.sent and "задерживается" in c.sent[0]


async def test_intercept_uses_latest_thread_of_chat(service, store, clock):
    await store.record_ai_turn(1, 40, 40, "user", "старое", clock.now)
    await store.record_ai_turn(1, 41, 40, "assistant", "ответ", clock.now)
    await _away(service)
    msg = FakeMessage(1, "новое")
    await service.intercept(msg, kind=away.KIND_TEXT, text="новое", dialogue_id=msg.message_id)
    assert (await store.ai_turn(1, msg.message_id))["dialogue_id"] == 40


async def test_intercept_topic_keeps_topic_dialogue(service, store):
    await _away(service)
    msg = FakeMessage(1, "в топике", thread=77)
    await service.intercept(msg, kind=away.KIND_TEXT, text="в топике", dialogue_id=77)
    entry = (await service.load()).pending["1"]
    assert entry["dialogue_id"] == 77 and entry["thread_id"] == 77 and entry["is_topic"]


async def test_intercept_photo_sticker_and_summon(service, store):
    await _away(service)
    photo = FakeMessage(1, photo=[object()], caption="смотри")
    await service.intercept(photo, dialogue_id=1)
    assert (await store.ai_turn(1, photo.message_id))["content"] == "[фото] смотри"
    sticker = FakeMessage(1, sticker=FakeSticker("😂"))
    await service.intercept(sticker, dialogue_id=1)
    assert (await store.ai_turn(1, sticker.message_id))["content"] == "[стикер] 😂"
    # голый /alfred: записки достаточно, в очередь не встаёт
    await service.clear()  # снять очередь и начать заново
    await _away(service)
    summon = FakeMessage(2, "/alfred")
    assert await service.intercept(summon, kind=away.KIND_SUMMON, dialogue_id=1)
    assert summon.sent
    assert await store.ai_turn(2, summon.message_id) is None
    assert "2" not in (await service.load()).pending


async def test_intercept_downloads_voice_immediately(service, store, tmp_path):
    await _away(service)
    bot = FakeBot({"v1": b"OGG-DATA"})
    voice = FakeMessage(1, voice=FakeFile("v1", 12), bot=bot)
    assert await service.intercept(voice, dialogue_id=1)
    assert (await store.ai_turn(1, voice.message_id))["content"] == (
        "[голосовое, ждёт распознавания]"
    )
    (row,) = await store.away_media_for_chat(1)
    assert row["kind"] == "voice" and row["duration_s"] == 12 and row["file_id"] == "v1"
    assert row["path"].startswith(str(tmp_path))  # рядом с БД бота, не /mnt/scratch
    assert open(row["path"], "rb").read() == b"OGG-DATA"

    note = FakeMessage(1, video_note=FakeFile("vn1", 3))
    audio = FakeMessage(1, audio=FakeFile("a1", 60))
    await service.intercept(note, dialogue_id=1)
    await service.intercept(audio, dialogue_id=1)
    assert [r["kind"] for r in await store.away_media_for_chat(1)] == [
        "voice", "video_note", "audio"
    ]
    assert (await store.ai_turn(1, note.message_id))["content"].startswith("[кружочек")
    assert (await store.ai_turn(1, audio.message_id))["content"].startswith("[аудио")


async def test_intercept_download_failure_keeps_file_id(service, store):
    await _away(service)
    voice = FakeMessage(1, voice=FakeFile("v9", 4), bot=FakeBot(fail=True))
    await service.intercept(voice, dialogue_id=1)
    (row,) = await store.away_media_for_chat(1)
    assert row["path"] is None and row["file_id"] == "v9"


async def test_intercept_duplicate_update_is_ignored(service, store):
    await _away(service)
    msg = FakeMessage(1, "раз")
    await service.intercept(msg, kind=away.KIND_TEXT, text="раз", dialogue_id=1)
    await service.intercept(msg, kind=away.KIND_TEXT, text="раз", dialogue_id=1)
    assert len(await store.ai_turns_for_dialogue(1, 1)) == 1


# ------------------------------------------------------- хендлеры: перехват


def _boom(*args, **kwargs):
    raise AssertionError("модель/зрение/голос не должны вызываться, пока Альфред в городе")


@pytest.fixture
def no_model(monkeypatch):
    async def boom(*args, **kwargs):
        _boom()

    monkeypatch.setattr(ai_flow, "request_alfred", boom)
    monkeypatch.setattr(voice_stt, "transcribe_voice_message", boom)
    monkeypatch.setattr(ai_handler, "_ask_and_reply", boom)
    monkeypatch.setattr(ai_handler, "_handle_photo_message", boom)
    monkeypatch.setattr(ai_handler, "_handle_sticker_message", boom)


def _ai_kwargs(store, service, tmp_path):
    return dict(
        node_link=None,
        store=store,
        config=_settings(tmp_path),
        book=_owner_book(),
        notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
        away=service,
    )


_OWNER_SUB = Subscription(chat_id=1, name="me", allowed_commands=frozenset({"*"}))


async def test_cmd_ai_intercepted_even_for_owner(store, service, tmp_path, no_model):
    await _away(service)
    msg = FakeMessage(1, "/alfred расскажи анекдот")
    await ai_handler.cmd_ai(msg, **_ai_kwargs(store, service, tmp_path))
    assert msg.sent and "Альфред спустился в город" in msg.sent[0]
    turn = await store.ai_turn(1, msg.message_id)
    assert turn["content"] == "расскажи анекдот"
    assert "1" in (await service.load()).pending


async def test_all_ai_entry_points_intercepted(store, service, tmp_path, no_model):
    await _away(service)
    kw = _ai_kwargs(store, service, tmp_path)
    kw["subscription"] = _OWNER_SUB

    text = FakeMessage(1, "привет")
    await ai_handler.on_private_message(text, **kw)
    photo = FakeMessage(1, photo=[object()])
    await ai_handler.on_private_photo(photo, **kw)
    voice = FakeMessage(1, voice=FakeFile("v", 3))
    await ai_handler.on_private_voice(voice, **kw)
    sticker = FakeMessage(1, sticker=FakeSticker())
    await ai_handler.on_private_sticker(sticker, **kw)
    reply = FakeMessage(1, "а ещё")
    await ai_handler.on_ai_reply(reply, ai_dialogue_id=50, **kw)
    group = FakeMessage(5, "@bot вопрос", chat_type="supergroup")
    await ai_handler.on_group_mention(group, mention_prompt="вопрос", **kw)
    note = FakeMessage(1, video_note=FakeFile("vn", 2))
    await ai_handler.on_private_other_media(note, away=service, subscription=_OWNER_SUB)

    state = await service.load()
    assert set(state.pending) == {"1", "5"}
    for m in (text, photo, voice, sticker, reply, note):
        assert await store.ai_turn(1, m.message_id) is not None
    assert (await store.ai_turn(5, group.message_id))["content"] == "вопрос"
    # голос ушёл в очередь на распознавание, а не в voice_stt
    assert len(await store.away_media_for_chat(1)) == 2  # голосовое + кружочек


async def test_start_dialogue_intercepted(store, service, tmp_path, no_model):
    await _away(service)
    msg = FakeMessage(1, "КОД")
    kw = _ai_kwargs(store, service, tmp_path)
    kw.pop("away")
    result = await ai_handler.start_dialogue(msg, "поприветствуй", away=service, **kw)
    assert result is None and msg.sent


async def test_cmd_ai_works_when_home(store, service, tmp_path, monkeypatch):
    calls = []

    async def fake_ask(message, *args, **kwargs):
        calls.append(message)
        return "ответ"

    monkeypatch.setattr(ai_handler, "_ask_and_reply", fake_ask)
    msg = FakeMessage(1, "/alfred привет")
    await ai_handler.cmd_ai(msg, **_ai_kwargs(store, service, tmp_path))
    assert calls == [msg]


async def test_draw_intercepted_but_service_subcommands_work(store, service, tmp_path):
    await _away(service)
    from aiogram.filters import CommandObject

    gen = FakeMessage(1, "/draw scene кот")
    command = CommandObject(prefix="/", command="draw", args="scene кот")
    await draw_handler.cmd_draw(
        gen, command, node_link=None, store=store, config=_settings(tmp_path), away=service
    )
    assert gen.sent and "в город" in gen.sent[0]

    helper = FakeMessage(1, "/draw help")
    await draw_handler.cmd_draw(
        helper,
        CommandObject(prefix="/", command="draw", args="help"),
        node_link=None,
        store=store,
        config=_settings(tmp_path),
        away=service,
    )
    assert helper.sent and "в город" not in helper.sent[0]


async def test_interactive_click_answers_with_note(service):
    await _away(service)
    answers = []

    class Cb:
        data = "ia:x:y"
        message = None
        from_user = FakeUser()

        async def answer(self, text=None, show_alert=False):
            answers.append((text, show_alert))

    await interactives_handler.cb_interactive(Cb(), interactives=None, away=service)
    assert answers and "Альфред спустился в город" in answers[0][0] and answers[0][1]


# ------------------------------------------------------- команды владельца


async def test_cmd_away_status_set_extend_back(store, service, tmp_path, clock):
    from aiogram.filters import CommandObject

    config = _settings(tmp_path)
    runner_kicks = []

    class Runner:
        def kick(self):
            runner_kicks.append(1)

    async def call(cmd, args=None):
        msg = FakeMessage(1, f"/{cmd}")
        if cmd == "away":
            await away_handler.cmd_away(
                msg, CommandObject(prefix="/", command=cmd, args=args), config,
                away=service, subscription=_OWNER_SUB,
            )
        else:
            await away_handler.cmd_back(
                msg, away=service, away_runner=Runner(), subscription=_OWNER_SUB
            )
        return msg.sent[-1]

    assert "и так дома" in await call("away")
    assert "спустился в город" in await call("away", "3ч датасет")
    state = await service.load()
    assert state.reason == "датасет" and state.set_by == "admin"
    assert "уже в городе" in await call("away", "1ч")
    assert "Продлил" in await call("away", "+1ч")
    assert (await service.load()).extended == 1
    assert "Не понял срок" in await call("away", "скоро")
    assert "вернулся" in await call("back")
    assert await service.load() is None
    assert "и так дома" in await call("back")


async def test_away_commands_owner_only(store, service, tmp_path):
    from aiogram.filters import CommandObject

    guest = Subscription(chat_id=2, name="g", allowed_commands=frozenset({"chat@llm", "away"}))
    msg = FakeMessage(2, "/away 3ч")
    await away_handler.cmd_away(
        msg, CommandObject(prefix="/", command="away", args="3ч"), _settings(tmp_path),
        away=service, subscription=guest,
    )
    assert await service.load() is None  # право «away» без «*» — не владелец


async def test_away_buttons(store, service, tmp_path):
    await _away(service)
    edited = []

    class Msg:
        async def edit_text(self, text, reply_markup=None):
            edited.append(text)

    class Cb:
        def __init__(self, data):
            self.data = data
            self.message = Msg()
            self.from_user = FakeUser()

        async def answer(self, *a, **k):
            pass

    config = _settings(tmp_path)
    await away_handler.cb_away(Cb("away:ext:30"), config, away=service, subscription=_OWNER_SUB)
    assert (await service.load()).until == T0 + timedelta(hours=3, minutes=30)
    await away_handler.cb_away(Cb("away:back"), config, away=service, subscription=_OWNER_SUB)
    assert await service.load() is None
    assert "Продлил" in edited[0] and "вернулся" in edited[1]


# ----------------------------------------------- проход: напоминание, потолок


class NoReturn:
    async def run_pass(self):
        return 0


async def test_runner_reminder_once_overdue_stays_and_ceiling(service, clock):
    notifier = FakeNotifier()
    runner = AwayRunner(service, NoReturn(), _owner_book(), notifier)
    await _away(service, hours=1)

    await runner.tick()
    assert notifier.sent == []  # до срока больше 15 минут

    clock.advance(minutes=46)  # до срока 14 минут
    await runner.tick()
    await runner.tick()
    assert len(notifier.sent) == 1  # напоминание один раз
    chat_id, text, markup = notifier.sent[0]
    assert chat_id == 999 and "через 14 мин" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels == ["+30м", "+1ч", "+2ч", "Вернуться сейчас"]

    clock.advance(hours=2)  # срок давно прошёл — режим НЕ снят
    await runner.tick()
    state = await service.load()
    assert state is not None and state.phase == away.PHASE_AWAY
    assert "вот-вот должен вернуться" in note_text(state, clock.now)
    assert len(notifier.sent) == 1

    # продление → новое напоминание перед новым сроком
    await service.extend(timedelta(hours=1))
    clock.advance(minutes=50)
    await runner.tick()
    assert len(notifier.sent) == 2

    # потолок 24 ч — принудительное возвращение
    clock.now = T0 + MAX_AWAY
    msg = FakeMessage(3, "ау")
    await service.intercept(msg, kind=away.KIND_TEXT, text="ау", dialogue_id=1)
    await runner.tick()
    assert (await service.load()).phase == away.PHASE_RETURNING
    assert "потолок" in notifier.sent[-1][1]


async def test_runner_ceiling_without_queue_clears(service, clock):
    notifier = FakeNotifier()
    runner = AwayRunner(service, NoReturn(), _owner_book(), notifier)
    await _away(service, hours=20)
    clock.advance(hours=25)
    await runner.tick()
    assert await service.load() is None
    assert "потолок" in notifier.sent[-1][1]


async def test_runner_delivers_claude_notices_to_owner(service):
    notifier = FakeNotifier()
    runner = AwayRunner(service, NoReturn(), _owner_book(), notifier)
    await service.queue_notice("Claude отправил Альфреда в город.")
    await runner.tick()
    assert [(c, t) for c, t, _ in notifier.sent] == [(999, "Claude отправил Альфреда в город.")]
    await runner.tick()
    assert len(notifier.sent) == 1  # очередь опустела


# ------------------------------------------------------------ возвращение


class ReturnEnv:
    """Стенд разбора очереди: _ask_and_reply, wake и STT замоканы."""

    def __init__(self, store, service, tmp_path, monkeypatch):
        self.store = store
        self.service = service
        self.asked: list[dict] = []
        self.transcribed: list[bytes] = []
        self.ready = wake_core.READY
        self.reply: str | None = "Вернулся, сэр."
        self.transcript = "распознанный текст"
        self.fail_for: set[int] = set()
        self.notifier = FakeNotifier()
        self.stt_error: Exception | None = None

        async def fake_ask(message, node_link, store_, config, book, notifier, dialogue_id,
                           history, active, tool_calls, rich, **kwargs):
            self.asked.append(
                {
                    "chat_id": message.chat.id,
                    "message_id": message.message_id,
                    "dialogue_id": dialogue_id,
                    "history": [dict(m) for m in history],
                    "user": message.from_user,
                    "thread": message.message_thread_id,
                }
            )
            if message.chat.id in self.fail_for:
                return None
            return self.reply

        async def fake_ready(*args, **kwargs):
            return self.ready

        async def fake_stt(node_link, raw, chat_id, config):
            if self.stt_error is not None:
                raise self.stt_error
            self.transcribed.append(raw)
            return self.transcript if not callable(self.transcript) else self.transcript(raw)

        monkeypatch.setattr(ai_handler, "_ask_and_reply", fake_ask)
        monkeypatch.setattr(wake_core, "ensure_service_ready", fake_ready)
        monkeypatch.setattr(voice_stt, "transcribe_bytes", fake_stt)
        self.returner = AwayReturn(
            service,
            get_node_link=lambda: object(),
            store=store,
            config=_settings(tmp_path),
            book=_owner_book(),
            notifier=self.notifier,
            active_ai_chats=ai_flow.ActiveAiChats(),
            tool_calls=ToolCalls(),
        )

    async def message(self, chat_id, text=None, *, user=None, thread=None, **kw):
        msg = FakeMessage(chat_id, text, user=user, thread=thread, **kw)
        dialogue_id = thread or msg.message_id  # как ai_handler._dialogue_id_for
        if text is not None and not kw:
            await self.service.intercept(
                msg, kind=away.KIND_TEXT, text=text, dialogue_id=dialogue_id
            )
        else:
            await self.service.intercept(msg, dialogue_id=dialogue_id)
        return msg


@pytest.fixture
def env(store, service, tmp_path, monkeypatch):
    return ReturnEnv(store, service, tmp_path, monkeypatch)


async def test_return_waits_for_llm_and_does_nothing_before_back(env, service):
    await _away(service)
    await env.message(1, "привет")
    assert await env.returner.run_pass() == 0  # фаза away — не разбираем
    assert env.asked == []

    await service.back()
    env.ready = wake_core.UNREACHABLE
    assert await env.returner.run_pass() == 0
    assert env.asked == []  # модель не готова — ждём, чат остаётся в очереди
    assert (await service.load()).pending


async def test_return_chats_one_by_one_in_order_replying_to_last_message(env, service, store):
    await _away(service)
    await env.message(1, "первый", user=FakeUser(7, "Аня", username="anya"))
    b1 = await env.message(2, "второй чат")
    a2 = await env.message(
        1, "и ещё", user=FakeUser(7, "Аня", username="anya")
    )  # тот же чат — одним запросом
    await service.back()

    assert await env.returner.run_pass() == 2
    assert [a["chat_id"] for a in env.asked] == [1, 2]  # по порядку первого сообщения
    first = env.asked[0]
    assert first["message_id"] == a2.message_id  # реплай на последнее
    assert first["user"].username == "anya"
    contents = [m["content"] for m in first["history"]]
    assert contents[-1] == "и ещё" and contents[-2] == away_return.RETURN_NOTE
    assert first["history"][-2]["role"] == "system"
    assert contents[:-2] == ["первый"]
    assert await service.load() is None  # очередь пуста — режим снят
    assert b1.message_id == env.asked[1]["message_id"]


async def test_return_transcribes_voices_in_order_before_asking(env, service, store):
    await _away(service)
    bot = FakeBot({"v1": b"first", "v2": b"second"})
    await env.message(1, "текст до")
    v1 = FakeMessage(1, voice=FakeFile("v1", 5), bot=bot)
    await env.service.intercept(v1, dialogue_id=1)
    v2 = FakeMessage(1, video_note=FakeFile("v2", 5), bot=bot)
    await env.service.intercept(v2, dialogue_id=1)
    paths = [r["path"] for r in await store.away_media_for_chat(1)]
    env.transcript = lambda raw: f"расшифровка {raw.decode()}"
    await service.back()

    assert await env.returner.run_pass() == 1
    assert env.transcribed == [b"first", b"second"]  # по порядку сообщений
    contents = [m["content"] for m in env.asked[0]["history"]]
    assert contents[0] == "текст до"
    # вставка «вернулся из города» — перед последней репликой собеседника
    assert contents[1:] == [
        "расшифровка first", away_return.RETURN_NOTE, "расшифровка second"
    ]
    assert (await store.ai_turn(1, v1.message_id))["content"] == "расшифровка first"
    assert await store.away_media_for_chat(1) == []
    import os

    assert not any(os.path.exists(p) for p in paths)  # файлы удалены


async def test_return_voice_failure_marks_history(env, service, store):
    await _away(service)
    v = FakeMessage(1, voice=FakeFile("v1", 5))
    await env.service.intercept(v, dialogue_id=1)
    env.stt_error = TimeoutError("нет службы")
    await service.back()
    assert await env.returner.run_pass() == 1
    assert (await store.ai_turn(1, v.message_id))["content"] == "[голосовое, не удалось разобрать]"
    assert env.asked  # ответ всё равно один


async def test_return_voice_limit_per_chat(env, service, store):
    await _away(service)
    msgs = []
    for i in range(away.MAX_VOICES_PER_CHAT + 2):
        m = FakeMessage(1, voice=FakeFile(f"v{i}", 5))
        await env.service.intercept(m, dialogue_id=1)
        msgs.append(m)
    await service.back()
    await env.returner.run_pass()
    assert len(env.transcribed) == away.MAX_VOICES_PER_CHAT
    last = await store.ai_turn(1, msgs[-1].message_id)
    assert "прислали ещё несколько голосовых" in last["content"]
    assert await store.away_media_for_chat(1) == []


async def test_return_retry_after_failed_delivery_and_drop(env, service, store):
    await _away(service)
    await env.message(1, "привет")
    await env.message(2, "второй")
    env.fail_for = {1}
    await service.back()

    await env.returner.run_pass()
    state = await service.load()
    assert set(state.pending) == {"1"}  # второй доставлен и снят, первый ждёт
    assert state.attempts == {"1": 1}

    await env.returner.run_pass()
    await env.returner.run_pass()  # третья неудача — чат снимается, владельцу сообщение
    assert await service.load() is None
    assert any("чату 1" in text for _, text, _ in env.notifier.sent)

    env.asked.clear()
    await _away(service)
    await env.message(1, "снова")
    env.fail_for = set()
    await service.back()
    assert await env.returner.run_pass() == 1


async def test_return_resumes_after_crash_midway(env, service, store):
    """Упали после распознавания первого голосового: заглушка уже подменена,
    строка away_media осталась — продолжаем со второго, без повторного STT."""
    await _away(service)
    bot = FakeBot({"v1": b"one", "v2": b"two"})
    v1 = FakeMessage(1, voice=FakeFile("v1", 5), bot=bot)
    v2 = FakeMessage(1, voice=FakeFile("v2", 5), bot=bot)
    await env.service.intercept(v1, dialogue_id=1)
    await env.service.intercept(v2, dialogue_id=1)
    await store.set_ai_turn_content(1, v1.message_id, "уже распознано")  # как до падения
    await service.back()

    assert await env.returner.run_pass() == 1
    assert env.transcribed == [b"two"]
    contents = [m["content"] for m in env.asked[0]["history"]]
    assert contents == ["уже распознано", away_return.RETURN_NOTE, "распознанный текст"]
    assert await store.away_media_for_chat(1) == []


async def test_return_new_message_during_answer_keeps_chat_queued(env, service, store, monkeypatch):
    await _away(service)
    first = await env.message(1, "раз")
    await service.back()
    late = FakeMessage(1, "пока вы отвечали")

    original = ai_handler._ask_and_reply

    async def ask_then_message(*args, **kwargs):
        result = await original(*args, **kwargs)
        if not env.asked[1:]:
            await service.intercept(late, kind=away.KIND_TEXT, text=late.text, dialogue_id=1)
        return result

    monkeypatch.setattr(ai_handler, "_ask_and_reply", ask_then_message)
    assert await env.returner.run_pass() == 2  # второй круг — на новое сообщение
    assert [a["message_id"] for a in env.asked] == [first.message_id, late.message_id]
    assert await service.load() is None


async def test_return_topic_reply_goes_to_topic(env, service):
    await _away(service)
    await env.message(1, "в топике", thread=88)
    await service.back()
    await env.returner.run_pass()
    assert env.asked[0]["thread"] == 88 and env.asked[0]["dialogue_id"] == 88


async def test_back_without_queue_never_calls_model(env, service):
    await _away(service)
    await service.back()
    assert await env.returner.run_pass() == 0
    assert env.asked == []


async def test_state_survives_restart_roundtrip(store, service):
    state = await _away(service, reason="датасет", set_by="claude")
    raw = await store.get_state(ALFRED_AWAY_KEY)
    assert json.loads(raw)["set_by"] == "claude"
    again = AwayState.from_json(raw)
    assert again.until == state.until and again.reason == "датасет"


async def test_set_ai_turn_content_updates_fts(store, clock):
    await store.record_ai_turn(1, 10, 10, "user", "[голосовое, ждёт распознавания]", clock.now)
    await store.set_ai_turn_content(1, 10, "про ёжика")
    cur = await store.db.conn.execute(
        "SELECT content FROM ai_turns_fts WHERE chat_id=1 AND message_id=10"
    )
    rows = await cur.fetchall()
    assert [r["content"] for r in rows] == ["про ежика"]


async def test_update_state_retries_on_concurrent_change(store):
    seen = []

    def fn(old):
        seen.append(old)
        return (old or "") + "x"

    original = store.get_state
    calls = {"n": 0}

    async def racing_get(key):
        value = await original(key)
        if calls["n"] == 0:  # между чтением и записью состояние успел сменить «другой процесс»
            calls["n"] += 1
            await store.set_state(key, "чужое")
        return value

    store.get_state = racing_get
    assert await store.update_state("k", fn) == "чужоеx"
    assert seen == [None, "чужое"]


# --------------------------------------------------------------------- CLI


def _cli_args(*argv):
    import argparse

    from sa_home_bot.away_cli import add_away_subparser

    parser = argparse.ArgumentParser()
    add_away_subparser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["away", *argv])


async def test_cli_set_extend_status_back_and_notices(tmp_path, capsys):
    from sa_home_bot.away_cli import _run

    settings = _settings(tmp_path)
    db = Database(settings.database.path)
    await db.open()
    await apply_migrations(db)
    await db.close()

    assert await _run(_cli_args("set", "3h", "--reason", "датасет"), settings) == 0
    assert "в городе до" in capsys.readouterr().out
    assert await _run(_cli_args("set", "1h"), settings) == 1  # уже в городе
    assert await _run(_cli_args("extend", "1h"), settings) == 0
    assert await _run(_cli_args("status"), settings) == 0
    out = capsys.readouterr().out
    assert "продлевали 1 раз" in out and "включил: Claude" in out and "причина: датасет" in out
    assert await _run(_cli_args("back"), settings) == 0

    db = Database(settings.database.path)
    await db.open()
    try:
        service = AwayService(Store(db), settings)
        assert await service.load() is None
        notices = await service.take_notices()
    finally:
        await db.close()
    # о каждом изменении от Claude бот сообщит владельцу
    assert [n.split(".")[0] for n in notices] == [
        "Claude отправил Альфреда в город",
        "Claude продлил отъезд Альфреда",
        "Claude вернул Альфреда",
    ]


async def test_cli_without_database_refuses(tmp_path, capsys):
    from sa_home_bot.away_cli import _run

    assert await _run(_cli_args("status"), _settings(tmp_path)) == 2
    assert not (tmp_path / "bot.sqlite").exists()


async def test_cli_admin_change_has_no_notice(tmp_path):
    from sa_home_bot.away_cli import _run

    settings = _settings(tmp_path)
    db = Database(settings.database.path)
    await db.open()
    await apply_migrations(db)
    await db.close()
    assert await _run(_cli_args("set", "до 23:59", "--by", "admin"), settings) == 0
    db = Database(settings.database.path)
    await db.open()
    try:
        assert await AwayService(Store(db), settings).take_notices() == []
    finally:
        await db.close()
