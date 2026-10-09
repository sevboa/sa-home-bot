"""Этап 50: сжатие истории /ai — оценка окна, сборка истории с кратким
содержанием, дословный возврат деталей (FTS5), защита от двойного сжатия,
страховка при done_reason=length (bot/dialogue_context.py, bot/ai_flow.py)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest_asyncio

from sa_home_bot.bot import ai_flow, dialogue_context
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.llm_chat import ChatStats
from sa_home_bot.proto.messages import Address
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

CHAT = 1
DIALOGUE = 100
DST = Address(node="mycraft", service="llm")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _settings(**llm) -> Settings:
    return Settings(llm=LlmConfig(request_timeout_s=5.0, **llm))


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


@pytest_asyncio.fixture(autouse=True)
def _fresh_state():
    dialogue_context.COMPRESSOR = dialogue_context.DialogueCompressor()
    ai_flow._profile_cache.clear()
    ai_flow._seen_num_ctx.clear()
    yield
    ai_flow._profile_cache.clear()
    ai_flow._seen_num_ctx.clear()


async def _turn(store, message_id, role, content, dialogue_id=DIALOGUE, name=None):
    await store.record_ai_turn(
        CHAT, message_id, dialogue_id, role, content, NOW, user_name=name
    )


async def _long_dialogue(store, pairs: int, reply_len: int = 50) -> None:
    """Тред из ``pairs`` пар реплик: message_id 1, 2, 3 … по порядку."""
    for i in range(pairs):
        await _turn(store, 2 * i + 1, "user", f"вопрос номер {i}")
        await _turn(store, 2 * i + 2, "assistant", f"ответ {i} " + "х" * reply_len)


# --- оценка порога ---


def test_estimate_by_chars_without_measure():
    settings = _settings(context_base_tokens=12000, context_chars_per_token=3.2)
    assert dialogue_context.estimate_prompt_tokens(settings, 32000, None) == 22000


def test_estimate_takes_larger_of_measure_and_chars():
    settings = _settings(context_base_tokens=12000, context_chars_per_token=3.2)
    # Замер больше оценки по символам (база реально толще) — берём замер + прирост.
    state = {"prompt_tokens": 25000, "sent_chars": 30000}
    assert dialogue_context.estimate_prompt_tokens(settings, 33200, state) == 26000
    # Замер занижен кэшем префикса Ollama — выигрывает оценка по символам.
    low = {"prompt_tokens": 500, "sent_chars": 30000}
    assert dialogue_context.estimate_prompt_tokens(settings, 32000, low) == 22000


def test_threshold_is_share_of_window():
    assert dialogue_context.threshold_tokens(_settings(), 32768) == int(32768 * 0.70)
    settings = _settings(context_compress_ratio=0.5)
    assert dialogue_context.threshold_tokens(settings, 32768) == 16384


def test_looks_overflowed():
    stats = ChatStats()
    stats.absorb({"prompt_eval_count": 1000, "done_reason": "length", "num_ctx": 32768})
    assert dialogue_context.looks_overflowed("обрыв на полусл", stats, 32768)
    ok = ChatStats()
    ok.absorb({"prompt_eval_count": 1000, "done_reason": "stop", "num_ctx": 32768})
    assert not dialogue_context.looks_overflowed("ответ", ok, 32768)
    assert not dialogue_context.looks_overflowed("", ok, 32768)  # пусто, но окно свободно
    full = ChatStats()
    full.absorb({"prompt_eval_count": 31800, "done_reason": "stop", "num_ctx": 32768})
    assert dialogue_context.looks_overflowed("", full, 32768)


def test_step_away_lines_have_no_letter_r():
    # Идут мимо Логопеда — без «р» верны и для картавого Альфреда, и для
    # «чистой речи» гостя.
    for line in dialogue_context.STEP_AWAY_LINES:
        assert "р" not in line.lower(), line


# --- сборка истории ---


async def test_load_history_without_summary_is_full_dialogue(store):
    await _long_dialogue(store, 3)
    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    assert [m["role"] for m in history] == ["user", "assistant"] * 3
    assert history.summary_upto is None
    assert history.max_hidden_id is None
    assert history.compressible == 0


async def test_history_shows_photo_tool_rounds_after_the_request(store):
    """Живая находка 2026-10-03: в истории были только тексты, и на «дай
    фото кабинета» модель отвечала словами, как в прошлой подписи. Вызов
    take_photo теперь виден в истории — как в живом раунде тулов."""
    await _turn(store, 1, "user", "покажи кабинет")
    await store.record_tool_call(
        chat_id=CHAT, dialogue_id=DIALOGUE, trigger_message_id=1, tool_name="take_photo",
        args={"caption": "Вид кабинета"}, result="Снимок делается." + "x" * 500, at=NOW,
    )
    await store.record_tool_call(
        chat_id=CHAT, dialogue_id=DIALOGUE, trigger_message_id=1, tool_name="get_time",
        args={}, result="12:00", at=NOW,
    )
    await _turn(store, 2, "assistant", "Вот, прошу.")
    await _turn(store, 3, "user", "спасибо")
    await _turn(store, 4, "assistant", "Пожалуйста.")
    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    roles = [m["role"] for m in history]
    assert roles == ["user", "assistant", "tool", "assistant", "user", "assistant"]
    call = history[1]["tool_calls"]
    assert call == [{"function": {"name": "take_photo", "arguments": {"caption": "Вид кабинета"}}}]
    assert history[2]["name"] == "take_photo"
    assert len(history[2]["content"]) == dialogue_context.HISTORY_TOOL_RESULT_MAX
    # Обрезка по бюджету не оставляет раунд тула без хода собеседника.
    trimmed = dialogue_context.trim_to_budget(list(history)[1:], 10_000)
    assert trimmed[0]["role"] == "assistant" and "tool_calls" not in trimmed[0]


async def test_history_keeps_person_id_from_find_person(store):
    """Живая находка 2026-10-09: id из find_person жил один ход, и на «да,
    хочу связь» модель выдумала recipient_id=123456789."""
    found = "Нашёл: Андрей Александрович Севбо — id 518571647 — мужчина"
    await _turn(store, 1, "user", "а как @kein есть такой?")
    await store.record_tool_call(
        chat_id=CHAT, dialogue_id=DIALOGUE, trigger_message_id=1, tool_name="find_person",
        args={"description": "@kein"}, result=found + " " + "x" * 300, at=NOW,
    )
    await _turn(store, 2, "assistant", "Это Андрей Александрович.")
    await _turn(store, 3, "user", "да хочу связь")
    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant", "user"]
    assert history[2]["content"].startswith(found)
    assert len(history[2]["content"]) > dialogue_context.HISTORY_TOOL_RESULT_MAX


async def test_load_history_with_summary_and_short_old_replies(store):
    await _long_dialogue(store, 12, reply_len=900)  # message_id 1..24
    await store.add_dialogue_summary(CHAT, DIALOGUE, 8, "Говорили о пчёлах.", NOW)
    settings = _settings(context_keep_recent_turns=10, context_old_reply_max_chars=600)
    history = await dialogue_context.load_history(store, settings, CHAT, DIALOGUE)

    assert history[0] == {
        "role": "system",
        "content": dialogue_context.SUMMARY_PREFIX + "Говорили о пчёлах.",
    }
    # Ходы 9..24 — 16 сообщений; дословно последние 10, старые 6 — укорочены
    # только у Альфреда.
    turns = history[1:]
    assert len(turns) == 16
    assert turns[0]["content"] == "вопрос номер 4"
    old, recent = turns[:6], turns[6:]
    for msg in old:
        if msg["role"] == "assistant":
            assert msg["content"].endswith(dialogue_context.SHORTENED_MARK)
            assert len(msg["content"]) <= 600 + len(dialogue_context.SHORTENED_MARK)
    assert all(not m["content"].endswith(dialogue_context.SHORTENED_MARK) for m in recent)
    assert history.summary_upto == 8
    assert history.compressible == 6
    assert history.max_hidden_id == 14  # последний укороченный ответ
    assert 24 in history.verbatim_ids and 10 not in history.verbatim_ids


async def test_load_history_before_message_id(store):
    await _long_dialogue(store, 3)
    history = await dialogue_context.load_history(
        store, _settings(), CHAT, DIALOGUE, before_message_id=5
    )
    assert len(history) == 4


def test_trim_to_budget_keeps_summary_and_last_turn():
    messages = [
        {"role": "system", "content": dialogue_context.SUMMARY_PREFIX + "итог"},
        {"role": "user", "content": "а" * 500},
        {"role": "assistant", "content": "б" * 500},
        {"role": "user", "content": "в" * 500},
    ]
    trimmed = dialogue_context.trim_to_budget(messages, 700)
    assert trimmed == [messages[0], messages[3]]


# --- дословный возврат (FTS5) ---


def test_fts_query_drops_stop_words_and_stems():
    query = dialogue_context.fts_query("Альфред, а какие глаза были у медведя?")
    assert '"медве"' in query  # «медведя» → «медве»: найдёт и «медведь»
    assert '"глаз"' in query
    assert "альфред" not in query and "какие" not in query


async def test_recall_finds_bear_by_inflected_word(store):
    await _turn(store, 1, "user", "Придумай мне медведя для сказки", name="Сева")
    await _turn(
        store, 2, "assistant", "Извольте: бурый медведь по имени Тихон, у него зелёные глаза."
    )
    await _turn(store, 3, "user", "А теперь дракона")
    await _turn(store, 4, "assistant", "Дракон Аргус, красный, с медными рогами.")
    for i in range(5, 17):
        await _turn(store, i, "user" if i % 2 else "assistant", f"болтовня {i}")
    await store.add_dialogue_summary(CHAT, DIALOGUE, 4, "Придумали медведя и дракона.", NOW)
    await _turn(store, 17, "user", "Какого цвета глаза у медведя?")

    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    block, count = await dialogue_context.recall_verbatim(
        store, _settings(), CHAT, DIALOGUE, "Какого цвета глаза у медведя?", history
    )
    assert count == 2  # пара: реплика собеседника + ответ Альфреда
    assert block.startswith(dialogue_context.RECALL_HEADER)
    assert "Сева: Придумай мне медведя для сказки" in block
    assert "у него зелёные глаза" in block
    assert "Дракон" not in block


async def test_recall_skips_turns_already_verbatim(store):
    await _turn(store, 1, "user", "медведь с зелёными глазами")
    await _turn(store, 2, "assistant", "запомнил")
    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    block, count = await dialogue_context.recall_verbatim(
        store, _settings(), CHAT, DIALOGUE, "что с медведем?", history
    )
    assert (block, count) == (None, 0)


async def test_recall_respects_budget(store):
    await _turn(store, 1, "user", "медведь " + "а" * 3000)
    await _turn(store, 2, "assistant", "медведь " + "б" * 3000)
    await store.add_dialogue_summary(CHAT, DIALOGUE, 2, "итог", NOW)
    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    settings = _settings(context_recall_budget_chars=3500)
    block, count = await dialogue_context.recall_verbatim(
        store, settings, CHAT, DIALOGUE, "медведь", history
    )
    assert count == 1  # пара не влезает — только само найденное, целиком
    assert len(block) < 3500 + len(dialogue_context.RECALL_HEADER) + 50


async def test_fts_backfill_on_migration(store):
    await _turn(store, 1, "user", "медведь с зелёными глазами")
    await store.db.conn.execute("DELETE FROM ai_turns_fts")
    await store.db.conn.commit()
    await apply_migrations(store.db)
    hits = await store.search_dialogue_turns(
        CHAT, DIALOGUE, '"зелен"', max_message_id=10, limit=5
    )
    assert [h["message_id"] for h in hits] == [1]


# --- сжатие ---


class SummaryLink:
    """Служба llm, отвечающая только на role=summarizer; ответ можно
    придержать событием (проверка двойного запуска)."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls: list[dict] = []
        self.gate = gate

    async def command(self, action, args=None, dst=None, timeout=None):
        assert action == "chat" and args["role"] == "summarizer"
        self.calls.append(args)
        if self.gate is not None:
            await self.gate.wait()
        return {"response": f"итог №{len(self.calls)}", "done_reason": "stop"}


async def test_compress_writes_summary_and_keeps_recent(store):
    await _long_dialogue(store, 10)  # 1..20
    link = SummaryLink()
    settings = _settings(context_keep_recent_turns=6)
    wrote = await dialogue_context.COMPRESSOR.compress_now(
        link, store, settings, CHAT, DIALOGUE, DST, num_ctx=32768
    )
    assert wrote
    summary = await store.latest_dialogue_summary(CHAT, DIALOGUE)
    assert summary["upto_message_id"] == 14
    assert summary["summary"] == "итог №1"
    request = link.calls[0]["messages"][0]["content"]
    assert "вопрос номер 0" in request and "вопрос номер 7" not in request
    # ai_turns не тронута.
    assert len(await store.ai_turns_for_dialogue(CHAT, DIALOGUE)) == 20
    history = await dialogue_context.load_history(store, settings, CHAT, DIALOGUE)
    assert history[0]["content"].endswith("итог №1")
    assert len(history) == 1 + 6


async def test_double_compression_is_prevented(store):
    await _long_dialogue(store, 10)
    gate = asyncio.Event()
    link = SummaryLink(gate)
    settings = _settings(context_keep_recent_turns=6)
    compressor = dialogue_context.COMPRESSOR
    assert compressor.schedule(link, store, settings, CHAT, DIALOGUE, DST, num_ctx=32768)
    await asyncio.sleep(0)
    assert compressor.is_running(CHAT, DIALOGUE)
    assert not compressor.schedule(link, store, settings, CHAT, DIALOGUE, DST, num_ctx=32768)
    # Синхронное сжатие во время фонового ждёт его замка и, перечитав БД,
    # видит, что сжимать нечего.
    sync = asyncio.create_task(
        compressor.compress_now(link, store, settings, CHAT, DIALOGUE, DST, num_ctx=32768)
    )
    await asyncio.sleep(0)
    gate.set()
    assert await sync is False
    await asyncio.sleep(0)
    assert len(link.calls) == 1
    assert not compressor.is_running(CHAT, DIALOGUE)


async def test_load_history_waits_for_running_compression(store):
    await _long_dialogue(store, 10)
    gate = asyncio.Event()
    link = SummaryLink(gate)
    settings = _settings(context_keep_recent_turns=6)
    dialogue_context.COMPRESSOR.schedule(link, store, settings, CHAT, DIALOGUE, DST, num_ctx=32768)
    loading = asyncio.create_task(
        dialogue_context.load_history(store, settings, CHAT, DIALOGUE)
    )
    await asyncio.sleep(0.01)
    assert not loading.done()
    gate.set()
    history = await loading
    assert history[0]["content"].endswith("итог №1")


async def test_load_history_gives_up_waiting_and_uses_old_state(store):
    await _long_dialogue(store, 10)
    gate = asyncio.Event()
    link = SummaryLink(gate)
    settings = _settings(context_keep_recent_turns=6, context_compress_wait_s=0.01)
    dialogue_context.COMPRESSOR.schedule(link, store, settings, CHAT, DIALOGUE, DST, num_ctx=32768)
    history = await dialogue_context.load_history(store, settings, CHAT, DIALOGUE)
    assert history.summary_upto is None and len(history) == 20  # целиком, ничего не потеряно
    gate.set()
    await asyncio.sleep(0.01)


# --- request_alfred: порог и страховка ---


class FakeBot:
    async def send_chat_action(self, chat_id, action, message_thread_id=None):
        return None


class FakeMessage:
    chat = SimpleNamespace(id=CHAT, type="private")
    message_id = 21
    message_thread_id = None
    from_user = None
    reply_to_message = None
    quote = None
    text = "а что дальше?"

    def __init__(self) -> None:
        self.answers: list[str] = []
        self.bot = FakeBot()

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class FakeNotifier:
    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None):
        return 1


def _book() -> SubscriptionBook:
    return SubscriptionBook(
        [
            Subscription(chat_id=999, name="admin", allowed_commands=frozenset({"*"})),
            Subscription(chat_id=CHAT, name="guest", allowed_commands=frozenset({"*@node"})),
        ]
    )


class AlfredLink:
    """Служба llm для request_alfred: router/persona по очереди из
    ``chat_results``, summarizer — отдельно."""

    def __init__(self, chat_results) -> None:
        self.chat_results = list(chat_results)
        self.chat_calls: list[dict] = []
        self.summary_calls: list[dict] = []

    async def describe(self, dst=None):
        return None

    async def get_state(self, dst=None):
        if dst is None:
            return {"node": "alfred", "services": [], "peers": []}
        if dst.service == "llm":
            return {"asleep": False}
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        if action == "recall":
            return {"facts": [], "count": 0}
        if action == "search":
            return {"facts": [], "count": 0}
        if action == "warmup":
            return {"asleep": False}
        assert action == "chat"
        if args.get("role") == "summarizer":
            self.summary_calls.append(args)
            return {"response": "Собеседник обсуждал вопросы 0–6.", "done_reason": "stop"}
        self.chat_calls.append(args)
        return self.chat_results.pop(0)


def _ok(text, prompt=1000):
    return {"response": text, "prompt_eval_count": prompt, "done_reason": "stop", "num_ctx": 32768}


async def test_threshold_adds_step_away_instruction(store):
    await _long_dialogue(store, 10)  # 1..20
    await _turn(store, 21, "user", "а что дальше?")
    settings = _settings(context_keep_recent_turns=6)
    history = await dialogue_context.load_history(store, settings, CHAT, DIALOGUE)
    # Прошлый ход показал промпт почти под порогом.
    await store.save_dialogue_context_state(
        CHAT, DIALOGUE, prompt_tokens=25000, sent_chars=100, num_ctx=32768,
        done_reason="stop", at=NOW,
    )
    link = AlfredLink([_ok(ai_flow.ROUTE_OK), _ok("Ответ. Позвольте отлучиться.")])
    raw = await ai_flow.request_alfred(
        FakeMessage(), link, store, settings, history, DIALOGUE, _book(), FakeNotifier()
    )
    assert raw == "Ответ. Позвольте отлучиться."
    assert history.compress_after
    persona_messages = link.chat_calls[1]["messages"]
    assert persona_messages[-2]["content"] == dialogue_context.STEP_AWAY_INSTRUCTION
    assert persona_messages[-1]["content"] == "а что дальше?"
    # Замер этого хода сохранён для следующего.
    state = await store.dialogue_context_state(CHAT, DIALOGUE)
    assert state["prompt_tokens"] == 1000 and state["num_ctx"] == 32768


async def test_below_threshold_no_instruction(store):
    await _long_dialogue(store, 10)
    await _turn(store, 21, "user", "а что дальше?")
    history = await dialogue_context.load_history(store, _settings(), CHAT, DIALOGUE)
    link = AlfredLink([_ok(ai_flow.ROUTE_OK), _ok("Ответ.")])
    await ai_flow.request_alfred(
        FakeMessage(), link, store, _settings(), history, DIALOGUE, _book(), FakeNotifier()
    )
    assert not history.compress_after
    contents = [m.get("content") for m in link.chat_calls[1]["messages"]]
    assert dialogue_context.STEP_AWAY_INSTRUCTION not in contents


async def test_rescue_on_length_compresses_and_regenerates(store):
    await _long_dialogue(store, 10)  # 1..20
    await _turn(store, 21, "user", "а что дальше?")
    settings = _settings(context_keep_recent_turns=6)
    history = await dialogue_context.load_history(store, settings, CHAT, DIALOGUE)
    truncated = {
        "response": "Обрыв на полу",
        "prompt_eval_count": 31800,
        "done_reason": "length",
        "num_ctx": 32768,
    }
    link = AlfredLink(
        [_ok(ai_flow.ROUTE_OK), truncated, _ok(ai_flow.ROUTE_OK), _ok("Полный ответ.")]
    )
    message = FakeMessage()
    raw = await ai_flow.request_alfred(
        message, link, store, settings, history, DIALOGUE, _book(), FakeNotifier()
    )
    assert raw == "Полный ответ."
    # Одна реплика в образе вместо «Альбегта» и обрывка.
    assert len(message.answers) == 1
    assert any(line in message.answers[0] for line in dialogue_context.STEP_AWAY_LINES)
    assert ai_flow.ALBERT_HICCUP not in message.answers
    # Сжали синхронно (keep = 6 // 2 = 3 → хвост с реплики собеседника).
    assert len(link.summary_calls) == 1
    summary = await store.latest_dialogue_summary(CHAT, DIALOGUE)
    assert summary is not None
    retry = link.chat_calls[3]["messages"]
    assert retry[0]["content"].startswith(dialogue_context.SUMMARY_PREFIX)
    assert retry[-1]["content"] == "а что дальше?"
    assert len(retry) < len(link.chat_calls[1]["messages"])
    assert not history.compress_after  # фоновое сжатие больше не нужно


async def test_rescue_falls_back_to_trim_when_still_overflowing(store):
    await _long_dialogue(store, 10)
    await _turn(store, 21, "user", "а что дальше?")
    settings = _settings(context_keep_recent_turns=6)
    history = await dialogue_context.load_history(store, settings, CHAT, DIALOGUE)
    truncated = {"response": "", "prompt_eval_count": 32700, "done_reason": "length",
                 "num_ctx": 32768}
    link = AlfredLink(
        [
            _ok(ai_flow.ROUTE_OK), truncated,
            _ok(ai_flow.ROUTE_OK), truncated,
            _ok(ai_flow.ROUTE_OK), _ok("Коротко, но ответ."),
        ]
    )
    raw = await ai_flow.request_alfred(
        FakeMessage(), link, store, settings, history, DIALOGUE, _book(), FakeNotifier()
    )
    assert raw == "Коротко, но ответ."
