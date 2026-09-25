"""Тулы связей между гостями (Этап 42.6.2): propose_relationship,
confirm_relationship, reject_relationship. Мост EVENT_RESPOND_RELATIONSHIP
(проактивная сессия -> бот) проверяется отдельно в
test_respond_relationship_bridge.py — здесь только сами обработчики тулов."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import GuestSubscriptionConfig, Settings, SubscriptionConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

OWNER_CHAT = 1
GUEST_A = 301  # инициатор (proposer)
GUEST_B = 302  # адресат
STRANGER_CHAT = 999  # неизвестный chat_id — не в book


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _book(*, a_family: bool = False, b_family: bool = False) -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Вася",
                chat_id=GUEST_A,
                allowed_commands=["chat@llm"],
                invited_user="Вася",
                family=a_family,
            ),
            GuestSubscriptionConfig(
                name="Настя",
                chat_id=GUEST_B,
                allowed_commands=["chat@llm"],
                invited_user="Настя",
                family=b_family,
            ),
        ],
    )


class FakeNodeLink:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, object]] = []
        self._raises = raises

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        if self._raises is not None:
            raise self._raises
        return {"task_id": 1}


class FakeNotifier:
    def __init__(self, message_id: int | None = 777) -> None:
        self.sent: list[tuple[int, str]] = []
        self._message_id = message_id

    async def send_direct(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        return self._message_id


class FakeEmit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


def _ctx(store, *, chat_id, book, node_link=None, notifier=None, emit=None):
    return ai_tools.ToolContext(
        chat_id=chat_id,
        dialogue_id=1,
        trigger_message_id=1,
        settings=Settings(),
        node_link=node_link,
        book=book,
        store=store,
        notifier=notifier,
        emit=emit,
    )


# --- propose_relationship ---


async def test_propose_relationship_unknown_target(store):
    ctx = _ctx(store, chat_id=GUEST_A, book=_book(), node_link=FakeNodeLink())
    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": STRANGER_CHAT, "relation": "friend"}
    )
    assert "не найден" in result


async def test_propose_relationship_invalid_relation(store):
    ctx = _ctx(store, chat_id=GUEST_A, book=_book(), node_link=FakeNodeLink())
    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "bestie"}
    )
    assert "friend" in result and "acquaintance" in result


async def test_propose_relationship_self(store):
    ctx = _ctx(store, chat_id=GUEST_A, book=_book(), node_link=FakeNodeLink())
    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_A, "relation": "friend"}
    )
    assert "самому себе" in result


async def test_propose_relationship_already_family(store):
    book = _book(a_family=True, b_family=True)
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=FakeNodeLink())
    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "родня" in result


async def test_propose_relationship_creates_pending_and_dispatches_dialogue(store):
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )

    assert "Настя" in result
    row = await store.pending_relationship_for(GUEST_B)
    assert row is not None
    assert (row["guest_a"], row["guest_b"], row["relation"]) == (GUEST_A, GUEST_B, "friend")

    assert len(node_link.calls) == 1
    action, args, dst = node_link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    assert dst == Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME)
    assert args["dst_node"] == ai_tools.LLM_NODE
    assert args["meta"]["chat_id"] == GUEST_B
    assert "dialogue_id" not in args["meta"]
    directive = args["args"]["messages"][0]["content"]
    assert "Вася" in directive
    assert "друг" in directive
    assert f"relationship_id={row['id']}" in directive


async def test_propose_relationship_duplicate_pending_does_not_redispatch(store):
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)
    await ai_tools.tool_propose_relationship(ctx, {"target_chat_id": GUEST_B, "relation": "friend"})

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )

    assert "уже отправлено" in result
    assert len(node_link.calls) == 1  # не переотправили


async def test_propose_relationship_already_confirmed_relation(store):
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)
    row = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )

    assert "уже подтверждённая связь" in result
    assert len(node_link.calls) == 0


async def test_propose_relationship_reports_task_service_error(store):
    ctx = _ctx(
        store,
        chat_id=GUEST_A,
        book=_book(),
        node_link=FakeNodeLink(raises=ServiceUnavailableError("недоступно")),
    )
    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "не удалось отправить предложение" in result
    assert await store.pending_relationship_for(GUEST_B) is not None  # запись уже создана


# --- confirm_relationship / reject_relationship: прямой путь (живой /ai) ---


async def test_confirm_relationship_direct_path(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    book = _book()
    notifier = FakeNotifier()
    ctx = _ctx(store, chat_id=GUEST_B, book=book, notifier=notifier)

    result = await ai_tools.tool_confirm_relationship(ctx, {"relationship_id": row["id"]})

    assert "принято" in result
    updated = await store.get_relationship(row["id"])
    assert updated["status"] == "confirmed"
    assert len(notifier.sent) == 1
    chat_id, text = notifier.sent[0]
    assert chat_id == GUEST_A
    assert "Настя" in text and "подтвердил" in text


async def test_reject_relationship_direct_path(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "acquaintance", datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_B, book=_book(), notifier=FakeNotifier())

    result = await ai_tools.tool_reject_relationship(ctx, {"relationship_id": row["id"]})

    assert "принято" in result
    updated = await store.get_relationship(row["id"])
    assert updated["status"] == "rejected"


async def test_confirm_relationship_wrong_addressee(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    # GUEST_A (сам инициатор) пытается подтвердить своё же предложение.
    ctx = _ctx(store, chat_id=GUEST_A, book=_book(), notifier=FakeNotifier())

    result = await ai_tools.tool_confirm_relationship(ctx, {"relationship_id": row["id"]})

    assert "адресовано не тебе" in result
    updated = await store.get_relationship(row["id"])
    assert updated["status"] == "pending"


async def test_confirm_relationship_unknown_id(store):
    ctx = _ctx(store, chat_id=GUEST_B, book=_book(), notifier=FakeNotifier())
    result = await ai_tools.tool_confirm_relationship(ctx, {"relationship_id": 9999})
    assert "нет такого предложения" in result


async def test_confirm_relationship_already_answered(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_B, book=_book(), notifier=FakeNotifier())

    result = await ai_tools.tool_confirm_relationship(ctx, {"relationship_id": row["id"]})
    assert "уже ответили" in result


# --- confirm_relationship / reject_relationship: мост (проактивная сессия) ---


async def test_confirm_relationship_bridge_path_without_store():
    emit = FakeEmit()
    ctx = ai_tools.ToolContext(
        chat_id=GUEST_B,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        book=_book(),
        emit=emit,
    )
    result = await ai_tools.tool_confirm_relationship(ctx, {"relationship_id": 42})

    assert "принято" in result
    assert len(emit.events) == 1
    event_type, data = emit.events[0]
    assert event_type == task_protocol.EVENT_RESPOND_RELATIONSHIP
    assert data == {"relationship_id": 42, "accepted": True, "responder_chat_id": GUEST_B}


async def test_reject_relationship_bridge_path_without_store():
    emit = FakeEmit()
    ctx = ai_tools.ToolContext(
        chat_id=GUEST_B,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        book=_book(),
        emit=emit,
    )
    result = await ai_tools.tool_reject_relationship(ctx, {"relationship_id": 42})

    assert "принято" in result
    event_type, data = emit.events[0]
    assert data["accepted"] is False


async def test_confirm_relationship_unavailable_without_store_or_emit():
    ctx = ai_tools.ToolContext(
        chat_id=GUEST_B,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        book=_book(),
    )
    result = await ai_tools.tool_confirm_relationship(ctx, {"relationship_id": 1})
    assert "недоступно" in result
