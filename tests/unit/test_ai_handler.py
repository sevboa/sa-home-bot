"""/alfred (+ скрытый алиас /ai): новый диалог (с текстом/без), продолжение
через reply, права на реплай, уведомление админов о сбоях. Presence/wake-
сценарий уже покрыт test_ai_flow.py — здесь он замокан
(ai_flow.request_alfred), тестируется только оркестрация хендлера
(bot/handlers/ai.py): запись ai_turns, резолв reply, форматирование."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest_asyncio

from sa_home_bot.bot import ai_flow
from sa_home_bot.bot.handlers import ai as ai_handler
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription


class FakeBot:
    def __init__(self) -> None:
        self.typing_chats: list[int] = []
        self.typing_threads: list[int | None] = []

    async def send_chat_action(self, chat_id, action, message_thread_id=None):
        self.typing_chats.append(chat_id)
        self.typing_threads.append(message_thread_id)


@dataclass
class FakeChat:
    id: int
    type: str = "private"


@dataclass
class FakeEntity:
    type: str
    offset: int
    length: int


class FakeMessage:
    _next_id = 1000

    def __init__(
        self, chat_id, text=None, reply_to=None, chat_type="private", entities=None,
        message_thread_id=None,
    ):
        self.chat = FakeChat(chat_id, type=chat_type)
        self.message_id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.text = text
        self.caption = None
        self.reply_to_message = reply_to
        self.entities = entities
        self.message_thread_id = message_thread_id
        self.bot = FakeBot()
        self.sent: list[str] = []
        self.from_user = None
        self.quote = None

    async def answer(self, text, **kwargs):
        # aiogram реально пробрасывает message_thread_id из контекста
        # исходного сообщения — здесь тот же эффект, для реалистичности тестов.
        sent = FakeMessage(self.chat.id, message_thread_id=self.message_thread_id)
        sent.text = text
        self.sent.append(text)
        return sent

    async def reply(self, text, **kwargs):
        return await self.answer(text)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text))
        return 1


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _sub(*allowed: str) -> Subscription:
    return Subscription(chat_id=1, name="me", allowed_commands=frozenset(allowed))


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _admin_book() -> SubscriptionBook:
    return SubscriptionBook(
        [Subscription(chat_id=999, name="admin", allowed_commands=frozenset({"*"}))]
    )


async def test_cmd_ai_without_text_asks_model_for_greeting(store, monkeypatch):
    # Без текста — не заготовленная строка, а сама модель здоровается
    # (решение пользователя 2026-07-23: не экономить обращения к локальной
    # модели). Директива-приветствие в историю не пишется, только ответ.
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Да, сэг? Слушаю вас"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert seen_history == [[{"role": "user", "content": ai_handler.OPENING_PROMPT}]]
    assert message.sent == [ai_handler._format_answer("Да, сэг? Слушаю вас")]
    # Только ответ ассистента — сама директива не осела в истории диалога.
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["content"] == "Да, сэг? Слушаю вас"


async def test_cmd_ai_without_text_unavailable_records_nothing(store, monkeypatch):
    async def fake_unavailable(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        return None  # ai_flow уже сообщил пользователю сама

    monkeypatch.setattr(ai_flow, "request_alfred", fake_unavailable)
    message = FakeMessage(1, text="/alfred")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert rows == []  # директива нигде не сохраняется — начать нечего


async def test_cmd_ai_with_text_calls_ai_flow_and_records_both_turns(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Добгый день, сэ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/ai привет")  # алиас — работает так же, как /alfred

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert seen_history == [[{"role": "user", "content": "привет"}]]
    assert message.sent == [ai_handler._format_answer("Добгый день, сэ")]

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "привет"
    assert rows[1]["content"] == "Добгый день, сэ"


async def test_cmd_ai_long_response_is_split_across_telegram_messages(store, monkeypatch):
    # Промпт (llm/prompt.py) просит модель уложиться в ~3500 знаков — но это
    # не гарантия, а Telegram режёт на 4096. Без чанкования в
    # _send_alfred_reply длинный ответ уронил бы хендлер TelegramBadRequest.
    long_answer = "Жили-были. " * 500

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        return long_answer

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred расскажи историю")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    expected_chunks = ai_handler.chunk_text(ai_handler._format_answer(long_answer))
    assert len(expected_chunks) > 1  # проверка не имеет смысла на одном чанке
    assert message.sent == expected_chunks

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == long_answer  # в БД — цельный текст, не куски


async def test_cmd_ai_returns_none_from_ai_flow_sends_nothing_extra(store, monkeypatch):
    async def fake_unavailable(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        return None  # ai_flow уже сообщил пользователю сама (не тестируем тут)

    monkeypatch.setattr(ai_flow, "request_alfred", fake_unavailable)
    message = FakeMessage(1, text="/alfred привет")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert len(rows) == 1  # только реплика юзера, ответ ассистента не записан
    assert rows[0]["role"] == "user"


async def test_cmd_ai_unhandled_exception_apologizes_and_notifies_admin(store, monkeypatch):
    async def boom(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(ai_flow, "request_alfred", boom)
    message = FakeMessage(1, text="/alfred привет")
    notifier = FakeNotifier()

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=notifier, active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert message.sent == ["<b>Альфред:</b> Прошу прощения, что-то пошло не так, сэр."]
    assert len(notifier.sent) == 1
    admin_chat_id, admin_text = notifier.sent[0]
    assert admin_chat_id == 999
    assert "RuntimeError" in admin_text
    # Ответ ассистента не записан — только реплика юзера.
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert len(rows) == 1
    assert rows[0]["role"] == "user"


# --- active_ai_chats: регистрация chat_id на время запроса (живая находка
# 2026-07-24 — bot/app.py::_shutdown рассылает RESTART_TEXT по этому
# множеству перед закрытием сессии, см. ai_flow.ActiveAiChats) ---


async def test_ask_and_reply_registers_chat_while_request_in_flight(store, monkeypatch):
    active_ai_chats = ai_flow.ActiveAiChats()
    seen_snapshot = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_snapshot.append(active_ai_chats.snapshot())
        return "ответ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred привет")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=active_ai_chats,
        tool_calls=ToolCalls(),
    )

    assert list(seen_snapshot[0]) == [1]  # зарегистрирован во время самого запроса
    assert active_ai_chats.snapshot() == {}  # и снят по завершении


async def test_ask_and_reply_unregisters_chat_even_on_exception(store, monkeypatch):
    active_ai_chats = ai_flow.ActiveAiChats()

    async def boom(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        raise RuntimeError("бум")

    monkeypatch.setattr(ai_flow, "request_alfred", boom)
    message = FakeMessage(1, text="/alfred привет")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=active_ai_chats,
        tool_calls=ToolCalls(),
    )

    assert active_ai_chats.snapshot() == {}


async def test_ai_reply_continuation_filter_matches_known_turn(store):
    await store.record_ai_turn(1, 500, 500, "assistant", "", _now())
    filt = ai_handler.AiReplyContinuation()
    hit = FakeMessage(1, text="продолжение", reply_to=FakeMessage(1))
    hit.reply_to_message.message_id = 500
    result = await filt(hit, store)
    assert result == {"ai_dialogue_id": 500}


async def test_ai_reply_continuation_filter_ignores_unrelated_reply(store):
    filt = ai_handler.AiReplyContinuation()
    other = FakeMessage(1, text="просто ответ на что-то ещё", reply_to=FakeMessage(1))
    other.reply_to_message.message_id = 999
    assert await filt(other, store) is False


async def test_ai_reply_continuation_filter_ignores_non_reply(store):
    filt = ai_handler.AiReplyContinuation()
    plain = FakeMessage(1, text="просто сообщение")
    assert await filt(plain, store) is False


async def test_on_ai_reply_denied_without_right(store):
    await store.record_ai_turn(1, 500, 500, "assistant", "", _now())
    message = FakeMessage(1, text="продолжение")

    await ai_handler.on_ai_reply(
        message,
        ai_dialogue_id=500,
        node_link=None,
        store=store,
        config=Settings(),
        book=_admin_book(),
        notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
        subscription=_sub(),  # без права chat@llm
    )

    assert message.sent == []
    rows = await store.ai_turns_for_dialogue(1, 500)
    assert len(rows) == 1  # заглушка, без нового хода


async def test_on_ai_reply_appends_history_and_answers(store, monkeypatch):
    await store.record_ai_turn(1, 500, 500, "user", "первый вопрос", _now())
    await store.record_ai_turn(1, 501, 500, "assistant", "первый ответ", _now())

    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "втогой ответ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="продолжаю")

    await ai_handler.on_ai_reply(
        message,
        ai_dialogue_id=500,
        node_link=None,
        store=store,
        config=Settings(),
        book=_admin_book(),
        notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
        subscription=_sub("chat@llm"),
    )

    assert seen_history == [
        [
            {"role": "user", "content": "первый вопрос"},
            {"role": "assistant", "content": "первый ответ"},
            {"role": "user", "content": "продолжаю"},
        ]
    ]
    assert message.sent == [ai_handler._format_answer("втогой ответ")]


async def test_on_ai_reply_without_text_still_asks_model(store, monkeypatch):
    # Реплай стикером/фото без подписи (message.text пуст) — раньше молча
    # игнорировался, теперь модель всё равно спрашивают, с пометкой, что
    # ход был пустым (директива не пишется в ai_turns).
    await store.record_ai_turn(1, 500, 500, "assistant", "", _now())

    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Простите, не расслышал, сэр"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text=None)  # стикер и т.п. — text=None

    await ai_handler.on_ai_reply(
        message,
        ai_dialogue_id=500,
        node_link=None,
        store=store,
        config=Settings(),
        book=_admin_book(),
        notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
        subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": ai_handler.EMPTY_REPLY_PROMPT}]]
    assert message.sent == [ai_handler._format_answer("Простите, не расслышал, сэр")]

    # Директива не осела в истории — только ответ ассистента.
    rows = await store.ai_turns_for_dialogue(1, 500)
    assert [r["role"] for r in rows] == ["assistant", "assistant"]
    assert rows[-1]["content"] == "Простите, не расслышал, сэр"


# --- неявные триггеры: любое сообщение в личке, @упоминание в группе
# (живая просьба пользователя 2026-07-23) ---


async def test_private_chat_text_filter_matches_plain_text():
    filt = ai_handler.PrivateChatText()
    msg = FakeMessage(1, text="как дела", chat_type="private")
    assert await filt(msg) is True


async def test_private_chat_text_filter_ignores_commands():
    filt = ai_handler.PrivateChatText()
    msg = FakeMessage(1, text="/status", chat_type="private")
    assert await filt(msg) is False


async def test_private_chat_text_filter_ignores_groups():
    filt = ai_handler.PrivateChatText()
    msg = FakeMessage(1, text="как дела", chat_type="group")
    assert await filt(msg) is False


async def test_private_chat_text_filter_ignores_empty_text():
    filt = ai_handler.PrivateChatText()
    msg = FakeMessage(1, text=None, chat_type="private")
    assert await filt(msg) is False


async def test_group_mention_filter_matches_and_strips_mention():
    filt = ai_handler.GroupMention()
    text = "@alfredbot как погода?"
    entities = [FakeEntity(type="mention", offset=0, length=len("@alfredbot"))]
    msg = FakeMessage(1, text=text, chat_type="group", entities=entities)
    result = await filt(msg, bot_username="alfredbot")
    assert result == {"mention_prompt": "как погода?"}


async def test_group_mention_filter_ignores_other_mentions():
    filt = ai_handler.GroupMention()
    text = "@someone_else привет"
    entities = [FakeEntity(type="mention", offset=0, length=len("@someone_else"))]
    msg = FakeMessage(1, text=text, chat_type="group", entities=entities)
    assert await filt(msg, bot_username="alfredbot") is False


async def test_group_mention_filter_ignores_private_chat():
    filt = ai_handler.GroupMention()
    text = "@alfredbot привет"
    entities = [FakeEntity(type="mention", offset=0, length=len("@alfredbot"))]
    msg = FakeMessage(1, text=text, chat_type="private", entities=entities)
    assert await filt(msg, bot_username="alfredbot") is False


async def test_on_private_message_starts_new_dialogue_when_none_exists(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Здгавствуйте, сэ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="добрый вечер", chat_type="private")

    await ai_handler.on_private_message(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": "добрый вечер"}]]
    assert message.sent == [ai_handler._format_answer("Здгавствуйте, сэ")]
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert [r["role"] for r in rows] == ["user", "assistant"]


async def test_on_private_message_always_starts_new_dialogue_even_with_prior_history(
    store, monkeypatch
):
    # Без reply — всегда новый тред, а не бесконечное пополнение самого
    # свежего (живой баг 2026-08-01: история в личке росла без предела).
    # Продолжить старый тред по-прежнему можно реплаем на сообщение бота.
    await store.record_ai_turn(1, 500, 500, "user", "первый вопрос", _now())
    await store.record_ai_turn(1, 501, 500, "assistant", "первый ответ", _now())

    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "втогой ответ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="а что насчёт этого?", chat_type="private")

    await ai_handler.on_private_message(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": "а что насчёт этого?"}]]
    old_thread = await store.ai_turns_for_dialogue(1, 500)
    assert len(old_thread) == 2  # старый тред не тронут
    new_thread = await store.ai_turns_for_dialogue(1, message.message_id)
    assert [r["role"] for r in new_thread] == ["user", "assistant"]


async def test_on_private_message_denied_without_right(store):
    message = FakeMessage(1, text="привет", chat_type="private")

    await ai_handler.on_private_message(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub(),
    )

    assert message.sent == []
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert rows == []


async def test_on_group_mention_with_text_starts_fresh_dialogue(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Слушаю, сэ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="@alfredbot какая погода?", chat_type="group")

    await ai_handler.on_group_mention(
        message, mention_prompt="какая погода?", node_link=None, store=store,
        config=Settings(), book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": "какая погода?"}]]
    assert message.sent == [ai_handler._format_answer("Слушаю, сэ")]


async def test_on_group_mention_without_text_asks_model_for_greeting(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Да, сэг?"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="@alfredbot", chat_type="group")

    await ai_handler.on_group_mention(
        message, mention_prompt="", node_link=None, store=store,
        config=Settings(), book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": ai_handler.OPENING_PROMPT}]]
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert len(rows) == 1  # директива-приветствие не записана, только ответ
    assert rows[0]["role"] == "assistant"


async def test_on_group_mention_denied_without_right(store):
    message = FakeMessage(1, text="@alfredbot привет", chat_type="group")

    await ai_handler.on_group_mention(
        message, mention_prompt="привет", node_link=None, store=store,
        config=Settings(), book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub(),
    )

    assert message.sent == []


# --- роспуск: гасим только ПОСЛЕ того, как прощание уехало в чат ---


async def test_dismissal_runs_after_the_farewell_is_sent(store, monkeypatch):
    seen = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        dismissal.mode = "off"
        return "Всего добгого, сэг"

    async def fake_perform(message, node_link, mode, book, notifier):
        # Снимок на момент выключения: прощание уже отправлено, ход записан.
        seen.append((mode, list(message.sent), await store.ai_turns_for_dialogue(1, dialogue_id)))

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    monkeypatch.setattr(ai_flow, "perform_dismissal", fake_perform)
    message = FakeMessage(1, text="/alfred спасибо, свободен")
    dialogue_id = message.message_id

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert len(seen) == 1
    mode, sent_at_that_moment, rows = seen[0]
    assert mode == "off"
    assert sent_at_that_moment == [ai_handler._format_answer("Всего добгого, сэг")]
    assert [r["role"] for r in rows] == ["user", "assistant"]


async def test_no_dismissal_when_alfred_never_answered(store, monkeypatch):
    """Ответа не было (нода не отозвалась) — прощаться не с чем, и гасить
    машину по намерению, о котором пользователь ничего не услышал, нельзя."""
    performed = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        dismissal.mode = "off"
        return None

    async def fake_perform(message, node_link, mode, book, notifier):
        performed.append(mode)

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    monkeypatch.setattr(ai_flow, "perform_dismissal", fake_perform)
    message = FakeMessage(1, text="/alfred свободен")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert performed == []


# --- Telegram Private Chat Topics: dialogue_id = id топика, любое
# сообщение внутри него — продолжение без reply (этап 32, 2026-08-01) ---


async def test_cmd_ai_in_topic_dialogue_id_is_the_topic_not_the_message(store, monkeypatch):
    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        return "ответ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred привет", message_thread_id=777)

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    # Тред привязан к топику (777), а не к message_id этого сообщения —
    # ai_turns_for_dialogue по message_id ничего не найдёт.
    assert await store.ai_turns_for_dialogue(1, message.message_id) == []
    rows = await store.ai_turns_for_dialogue(1, 777)
    assert [r["role"] for r in rows] == ["user", "assistant"]


async def test_cmd_ai_in_topic_continues_existing_topic_history(store, monkeypatch):
    await store.record_ai_turn(1, 601, 777, "user", "первый вопрос в топике", _now())
    await store.record_ai_turn(1, 602, 777, "assistant", "первый ответ в топике", _now())

    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "втогой ответ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    # /alfred внутри топика — НЕ сбрасывает контекст, в отличие от /alfred
    # вне топика (там каждый вызов — новый тред).
    message = FakeMessage(1, text="/alfred а что насчёт этого?", message_thread_id=777)

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert seen_history == [
        [
            {"role": "user", "content": "первый вопрос в топике"},
            {"role": "assistant", "content": "первый ответ в топике"},
            {"role": "user", "content": "а что насчёт этого?"},
        ]
    ]


async def test_cmd_ai_in_topic_without_text_continues_with_opening_prompt(store, monkeypatch):
    await store.record_ai_turn(1, 601, 777, "user", "первый вопрос", _now())
    await store.record_ai_turn(1, 602, 777, "assistant", "первый ответ", _now())

    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Да, сэг?"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred", message_thread_id=777)

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    # Голый /alfred внутри топика с уже начатой историей — не заменяет её
    # заготовкой, а дописывает директиву-приветствие в конец.
    assert seen_history == [
        [
            {"role": "user", "content": "первый вопрос"},
            {"role": "assistant", "content": "первый ответ"},
            {"role": "user", "content": ai_handler.OPENING_PROMPT},
        ]
    ]


async def test_cmd_ai_in_fresh_topic_without_text_is_same_as_outside_topic(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return "Да, сэг?"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred", message_thread_id=777)

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert seen_history == [[{"role": "user", "content": ai_handler.OPENING_PROMPT}]]


async def test_on_private_message_in_topic_continues_same_topic_without_reply(store, monkeypatch):
    # В отличие от чата без топиков (см. test_on_private_message_always_
    # starts_new_dialogue_even_with_prior_history) — внутри топика reply не
    # нужен вовсе, любое сообщение в нём само по себе продолжение.
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        seen_history.append(history)
        return f"ответ {len(seen_history)}"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)

    first = FakeMessage(1, text="первое сообщение", chat_type="private", message_thread_id=777)
    await ai_handler.on_private_message(
        first, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub("chat@llm"),
    )

    second = FakeMessage(1, text="второе сообщение", chat_type="private", message_thread_id=777)
    await ai_handler.on_private_message(
        second, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
        active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(), subscription=_sub("chat@llm"),
    )

    assert seen_history[0] == [{"role": "user", "content": "первое сообщение"}]
    assert seen_history[1] == [
        {"role": "user", "content": "первое сообщение"},
        {"role": "assistant", "content": "ответ 1"},
        {"role": "user", "content": "второе сообщение"},
    ]
    rows = await store.ai_turns_for_dialogue(1, 777)
    assert [r["role"] for r in rows] == ["user", "assistant", "user", "assistant"]


# typing-индикатор: раньше проверялся здесь (разовый вызов на приём
# сообщения), теперь живёт внутри ai_flow.request_alfred как keep-alive
# вокруг самого вызова модели (решение пользователя 2026-08-01) — эта функция
# в тестах хендлера как раз замокана, так что покрытие переехало целиком в
# test_ai_flow.py (test_typing_*).


async def test_empty_model_answer_is_not_sent_as_a_bare_prefix(store, monkeypatch):
    """Живая находка: у модели кончалось окно контекста и она возвращала
    пустой текст — в чат уходило сообщение из одного «Альфред:»."""

    async def fake_empty(
        message, node_link, store_, config, history, dialogue_id, book, notifier, dismissal=None,
        tool_calls=None
    ):
        return "   "

    monkeypatch.setattr(ai_flow, "request_alfred", fake_empty)
    message = FakeMessage(1, text="/alfred привет")
    notifier = FakeNotifier()

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=notifier, active_ai_chats=ai_flow.ActiveAiChats(),
        tool_calls=ToolCalls(),
    )

    assert message.sent == [ai_flow.ALBERT_HICCUP]
    assert notifier.sent and "пустой ответ" in notifier.sent[0][1]
    # Пустая реплика — не ход диалога: в истории её нет.
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert [r["role"] for r in rows] == ["user"]
