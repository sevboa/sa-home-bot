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
from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription


class FakeBot:
    def __init__(self) -> None:
        self.typing_chats: list[int] = []

    async def send_chat_action(self, chat_id, action):
        self.typing_chats.append(chat_id)


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

    def __init__(self, chat_id, text=None, reply_to=None, chat_type="private", entities=None):
        self.chat = FakeChat(chat_id, type=chat_type)
        self.message_id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.text = text
        self.caption = None
        self.reply_to_message = reply_to
        self.entities = entities
        self.bot = FakeBot()
        self.sent: list[str] = []
        self.from_user = None
        self.quote = None

    async def answer(self, text, **kwargs):
        sent = FakeMessage(self.chat.id)
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
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        seen_history.append(history)
        return "Да, сэг? Слушаю вас"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
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
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        return None  # ai_flow уже сообщил пользователю сама

    monkeypatch.setattr(ai_flow, "request_alfred", fake_unavailable)
    message = FakeMessage(1, text="/alfred")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
    )

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert rows == []  # директива нигде не сохраняется — начать нечего


async def test_cmd_ai_with_text_calls_ai_flow_and_records_both_turns(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        seen_history.append(history)
        return "Добгый день, сэ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/ai привет")  # алиас — работает так же, как /alfred

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
    )

    assert seen_history == [[{"role": "user", "content": "привет"}]]
    assert message.bot.typing_chats == [1]
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
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        return long_answer

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="/alfred расскажи историю")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
    )

    expected_chunks = ai_handler.chunk_text(ai_handler._format_answer(long_answer))
    assert len(expected_chunks) > 1  # проверка не имеет смысла на одном чанке
    assert message.sent == expected_chunks

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert rows[-1]["role"] == "assistant"
    assert rows[-1]["content"] == long_answer  # в БД — цельный текст, не куски


async def test_cmd_ai_returns_none_from_ai_flow_sends_nothing_extra(store, monkeypatch):
    async def fake_unavailable(
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        return None  # ai_flow уже сообщил пользователю сама (не тестируем тут)

    monkeypatch.setattr(ai_flow, "request_alfred", fake_unavailable)
    message = FakeMessage(1, text="/alfred привет")

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(),
    )

    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert len(rows) == 1  # только реплика юзера, ответ ассистента не записан
    assert rows[0]["role"] == "user"


async def test_cmd_ai_unhandled_exception_apologizes_and_notifies_admin(store, monkeypatch):
    async def boom(
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(ai_flow, "request_alfred", boom)
    message = FakeMessage(1, text="/alfred привет")
    notifier = FakeNotifier()

    await ai_handler.cmd_ai(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=notifier,
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
        notifier=FakeNotifier(),
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
        message, node_link, store_, config, history, dialogue_id, book, notifier
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
        notifier=FakeNotifier(),
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
        message, node_link, store_, config, history, dialogue_id, book, notifier
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
        notifier=FakeNotifier(),
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
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        seen_history.append(history)
        return "Здгавствуйте, сэ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="добрый вечер", chat_type="private")

    await ai_handler.on_private_message(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": "добрый вечер"}]]
    assert message.sent == [ai_handler._format_answer("Здгавствуйте, сэ")]
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert [r["role"] for r in rows] == ["user", "assistant"]


async def test_on_private_message_continues_latest_dialogue(store, monkeypatch):
    await store.record_ai_turn(1, 500, 500, "user", "первый вопрос", _now())
    await store.record_ai_turn(1, 501, 500, "assistant", "первый ответ", _now())

    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        seen_history.append(history)
        return "втогой ответ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="а что насчёт этого?", chat_type="private")

    await ai_handler.on_private_message(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), subscription=_sub("chat@llm"),
    )

    assert seen_history == [
        [
            {"role": "user", "content": "первый вопрос"},
            {"role": "assistant", "content": "первый ответ"},
            {"role": "user", "content": "а что насчёт этого?"},
        ]
    ]
    rows = await store.ai_turns_for_dialogue(1, 500)
    assert len(rows) == 4  # старый тред пополнился, новый не завёлся


async def test_on_private_message_denied_without_right(store):
    message = FakeMessage(1, text="привет", chat_type="private")

    await ai_handler.on_private_message(
        message, node_link=None, store=store, config=Settings(),
        book=_admin_book(), notifier=FakeNotifier(), subscription=_sub(),
    )

    assert message.sent == []
    rows = await store.ai_turns_for_dialogue(1, message.message_id)
    assert rows == []


async def test_on_group_mention_with_text_starts_fresh_dialogue(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        seen_history.append(history)
        return "Слушаю, сэ"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="@alfredbot какая погода?", chat_type="group")

    await ai_handler.on_group_mention(
        message, mention_prompt="какая погода?", node_link=None, store=store,
        config=Settings(), book=_admin_book(), notifier=FakeNotifier(),
        subscription=_sub("chat@llm"),
    )

    assert seen_history == [[{"role": "user", "content": "какая погода?"}]]
    assert message.sent == [ai_handler._format_answer("Слушаю, сэ")]


async def test_on_group_mention_without_text_asks_model_for_greeting(store, monkeypatch):
    seen_history = []

    async def fake_request(
        message, node_link, store_, config, history, dialogue_id, book, notifier
    ):
        seen_history.append(history)
        return "Да, сэг?"

    monkeypatch.setattr(ai_flow, "request_alfred", fake_request)
    message = FakeMessage(1, text="@alfredbot", chat_type="group")

    await ai_handler.on_group_mention(
        message, mention_prompt="", node_link=None, store=store,
        config=Settings(), book=_admin_book(), notifier=FakeNotifier(),
        subscription=_sub("chat@llm"),
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
        subscription=_sub(),
    )

    assert message.sent == []
