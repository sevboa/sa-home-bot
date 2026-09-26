"""Тулы связей между гостями (Этап 42.6.2): propose_relationship,
confirm_relationship, reject_relationship. Мост EVENT_RESPOND_RELATIONSHIP
(проактивная сессия -> бот) проверяется отдельно в
test_respond_relationship_bridge.py — здесь только сами обработчики тулов."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import GuestSubscriptionConfig, PersonConfig, Settings, SubscriptionConfig
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


# --- preview_relationship (42.6.6: обязательный первый шаг, без побочных
# эффектов — инициатор должен сам увидеть, что будет отправлено, прежде чем
# это реально уйдёт адресату) ---


async def test_preview_relationship_no_side_effects(store):
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)

    result = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "acquaintance"}
    )

    assert "Вася" in result and "Настя" in result and "знакомый" in result
    assert "propose_relationship" in result
    assert str(GUEST_B) in result
    # Ничего не записано и никуда не дозвонились — это только предпросмотр.
    assert await store.pending_relationship_for(GUEST_B) is None
    assert len(node_link.calls) == 0


async def test_preview_relationship_unknown_target(store):
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": STRANGER_CHAT, "relation": "friend"}
    )
    assert "не найден" in result


async def test_preview_relationship_invalid_relation(store):
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "bestie"}
    )
    assert "friend" in result and "acquaintance" in result


async def test_preview_relationship_family_flag_no_longer_matters(store):
    # 42.6.7: групповой флаг Subscription.family больше НЕ блокирует и не
    # заменяет предложение связи — он тут вообще не читается.
    book = _book(a_family=True, b_family=True)
    ctx = _ctx(store, chat_id=GUEST_A, book=book)
    result = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "Настя" in result and "друг" in result
    assert "родня" not in result


async def test_preview_relationship_does_not_need_node_link(store):
    # Предпросмотр ничего не рассылает, поэтому доступен даже без node_link.
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "family"}
    )
    assert "Настя" in result and "родство" in result


async def test_preview_relationship_spouse_exclusivity(store):
    row = await store.propose_relationship(GUEST_A, GUEST_C, "spouse", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book_with_third(a_family=False, c_family=False))
    result = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )
    assert "уже состоит в супружеской связи" in result


async def test_preview_then_propose_same_args_matches(store):
    # Ровно сценарий из директивы preview: те же target_chat_id/relation.
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)

    preview = await ai_tools.tool_preview_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "acquaintance"}
    )
    assert f"target_chat_id={GUEST_B}" in preview
    assert 'relation="acquaintance"' in preview

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "acquaintance"}
    )
    assert "Настя" in result
    row = await store.pending_relationship_for(GUEST_B)
    assert row is not None and row["relation"] == "acquaintance"


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


async def test_propose_relationship_family_flag_does_not_block(store):
    # 42.6.7: групповой флаг больше не блокирует и не заменяет предложение
    # ('family' в guest_relationships — единственный источник родства теперь).
    book = _book(a_family=True, b_family=True)
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)
    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "Настя" in result
    assert await store.pending_relationship_for(GUEST_B) is not None


async def test_propose_relationship_family_creates_pending(store):
    # 42.6.5/42.6.7: 'family' — точечная связь конкретной пары, флаг
    # Subscription.family вообще не участвует ни в проверке, ни в тексте.
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "family"}
    )

    assert "Настя" in result
    row = await store.pending_relationship_for(GUEST_B)
    assert row is not None
    assert row["relation"] == "family"
    directive = node_link.calls[0][1]["args"]["messages"][0]["content"]
    assert "родство" in directive


async def test_propose_relationship_spouse_creates_pending(store):
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )

    assert "Настя" in result
    row = await store.pending_relationship_for(GUEST_B)
    assert row is not None and row["relation"] == "spouse"


async def test_propose_relationship_spouse_blocked_if_proposer_already_married(store):
    row = await store.propose_relationship(GUEST_A, GUEST_C, "spouse", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(
        store,
        chat_id=GUEST_A,
        book=_book_with_third(a_family=False, c_family=False),
        node_link=FakeNodeLink(),
    )

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )

    assert "уже состоит в супружеской связи" in result
    assert await store.pending_relationship_for(GUEST_B) is None


async def test_propose_relationship_spouse_blocked_if_target_already_married(store):
    row = await store.propose_relationship(GUEST_B, GUEST_C, "spouse", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(
        store,
        chat_id=GUEST_A,
        book=_book_with_third(a_family=False, c_family=False),
        node_link=FakeNodeLink(),
    )

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )

    assert "уже есть супруг(а)" in result
    assert await store.pending_relationship_for(GUEST_B) is None


async def test_propose_relationship_spouse_pending_does_not_block_other_target(store):
    # Исключительность проверяется по ПОДТВЕРЖДЁННЫМ связям — висящая заявка
    # (ещё не confirmed) не должна мешать предложить спор другому.
    node_link = FakeNodeLink()
    ctx = _ctx(
        store,
        chat_id=GUEST_A,
        book=_book_with_third(a_family=False, c_family=False),
        node_link=node_link,
    )
    await store.propose_relationship(GUEST_A, GUEST_C, "spouse", datetime.now(tz=UTC))

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )

    assert "Настя" in result
    assert await store.pending_relationship_for(GUEST_B) is not None


async def test_my_relationships_pairwise_family(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "family", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Настя" in result and "родство" in result


async def test_my_relationships_pairwise_spouse(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "spouse", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Настя" in result and "супруг(а)" in result


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


async def test_propose_relationship_directive_clarifies_alfred_is_not_a_party(store):
    # Живая находка 2026-09-26: старая формулировка директивы ("...установить
    # с тобой связь...") модель читала так, будто СВЯЗЬ ЗАВОДИТСЯ С НЕЙ САМОЙ
    # (Альфредом) — пользователь получил "мне предложили стать моим
    # супругом" вместо "Алексей предлагает связь с вами". Директива обязана
    # явно называть Альфреда курьером, а не стороной связи.
    book = _book()
    node_link = FakeNodeLink()
    ctx = _ctx(store, chat_id=GUEST_A, book=book, node_link=node_link)

    await ai_tools.tool_propose_relationship(ctx, {"target_chat_id": GUEST_B, "relation": "spouse"})

    directive = node_link.calls[0][1]["args"]["messages"][0]["content"]
    assert "Альфред" in directive
    assert "НЕ участник" in directive
    assert "не с тобой" in directive
    assert "Вася" in directive  # сторона связи — инициатор, назван по имени


async def test_propose_relationship_owner_proposer_uses_real_name_not_me(store):
    # Живая находка 2026-09-26: Subscription.name владельца в проде технически
    # "me" (намеренно — гость не должен подобрать владельца по имени через
    # recipients.py), но это же значение утекало в директиву адресату:
    # "Гость «me» предложил(а)...". Там, где владелец известен в
    # settings.people, директива обязана называть его настоящим именем.
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Настя", chat_id=GUEST_B, allowed_commands=["chat@llm"], invited_user="Настя"
            )
        ],
    )
    node_link = FakeNodeLink()
    settings = Settings(
        people=[
            PersonConfig(
                telegram_id=OWNER_CHAT, full_name="Алексей Александрович Севбо", gender="m"
            )
        ]
    )
    ctx = ai_tools.ToolContext(
        chat_id=OWNER_CHAT,
        dialogue_id=1,
        trigger_message_id=1,
        settings=settings,
        node_link=node_link,
        book=book,
        store=store,
    )

    result = await ai_tools.tool_propose_relationship(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )

    assert "me" not in result
    directive = node_link.calls[0][1]["args"]["messages"][0]["content"]
    assert "Алексей Александрович Севбо" in directive
    assert "«me»" not in directive


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


# --- my_relationships (42.6.3) ---

GUEST_C = 303  # третий гость — для проверки "семья + friend одновременно"


def _book_with_third(*, a_family: bool, c_family: bool) -> SubscriptionBook:
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
                family=False,
            ),
            GuestSubscriptionConfig(
                name="Игорь",
                chat_id=GUEST_C,
                allowed_commands=["chat@llm"],
                invited_user="Игорь",
                family=c_family,
            ),
        ],
    )


async def test_my_relationships_no_store():
    ctx = ai_tools.ToolContext(
        chat_id=GUEST_A,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        book=_book(),
    )
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "недоступно" in result


async def test_my_relationships_caller_unknown(store):
    ctx = _ctx(store, chat_id=STRANGER_CHAT, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "недоступно" in result


async def test_my_relationships_empty(store):
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert result == "подтверждённых связей нет"


async def test_my_relationships_only_friend(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Настя" in result and "друг" in result


async def test_my_relationships_symmetry_other_role(store):
    # A здесь guest_b строки (предложение шло от B к A) — должен всё равно
    # увидеть Настю как знакомую.
    row = await store.propose_relationship(GUEST_B, GUEST_A, "acquaintance", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Настя" in result and "знакомый" in result


async def test_my_relationships_family_and_friend_together(store):
    # 42.6.7: родство — такая же явная пара в guest_relationships, как и
    # друг/знакомый; флаг Subscription.family здесь ни при чём.
    book = _book_with_third(a_family=False, c_family=False)
    row_family = await store.propose_relationship(GUEST_A, GUEST_C, "family", datetime.now(tz=UTC))
    await store.respond_relationship(row_family["id"], True, datetime.now(tz=UTC))
    row_friend = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    await store.respond_relationship(row_friend["id"], True, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=book)
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Игорь" in result and "родство" in result
    assert "Настя" in result and "друг" in result


async def test_my_relationships_silent_about_pending(store):
    await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert result == "подтверждённых связей нет"


async def test_my_relationships_silent_about_rejected(store):
    row = await store.propose_relationship(GUEST_A, GUEST_B, "friend", datetime.now(tz=UTC))
    await store.respond_relationship(row["id"], False, datetime.now(tz=UTC))
    ctx = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert result == "подтверждённых связей нет"
