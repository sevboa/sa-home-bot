"""Стык 42.6.2 (bot/tools.py::tool_propose_relationship,
EVENT_RESPOND_RELATIONSHIP) ↔ 44.1/44.2 (schedule_agent_dialogue,
_handle_task_result) — весь путь propose → outreach → B отвечает →
инициатор уведомлён, на ОДНОМ реальном Store, без переизобретения meta/
relationship_id вручную. test_relationship_tools.py и
test_respond_relationship_bridge.py уже проверяют каждую половину
изолированно (второй — на фейковом Store с готовой строкой) — здесь именно
стык, по образцу test_proactive_dialogue.py (44.3)."""

from __future__ import annotations

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.config import GuestSubscriptionConfig, Settings, SubscriptionConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

GUEST_A = 301  # инициатор (Вася)
GUEST_B = 302  # адресат (Настя)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=1, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Вася", chat_id=GUEST_A, allowed_commands=["chat@llm"], invited_user="Вася"
            ),
            GuestSubscriptionConfig(
                name="Настя", chat_id=GUEST_B, allowed_commands=["chat@llm"], invited_user="Настя"
            ),
        ],
    )


class FakeNodeLink:
    """Тот же двойник, что в test_relationship_tools.py/test_proactive_dialogue.py
    — не импортируется оттуда намеренно (тестовые модули друг друга не
    тянут), только форма важна для стыка."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, object]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        return {"task_id": 1}


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self._next_id = 1000

    async def send_direct(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        self._next_id += 1
        return self._next_id


class FakeEmit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.events.append((event_type, data))


async def test_propose_outreach_response_end_to_end_on_real_store(store):
    book = _book()

    # --- 1. A предлагает связь (живой /ai, ctx.store реальный) ---
    node_link = FakeNodeLink()
    proposer_ctx = ai_tools.ToolContext(
        chat_id=GUEST_A,
        dialogue_id=1,
        trigger_message_id=1,
        settings=Settings(),
        node_link=node_link,
        book=book,
        store=store,
        notifier=None,
        emit=None,
    )
    result = await ai_tools.tool_propose_relationship(
        proposer_ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "Настя" in result

    row = await store.pending_relationship_for(GUEST_B)
    assert row is not None
    relationship_id = row["id"]

    _action, create_args, dst = node_link.calls[0]
    assert dst == Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME)
    real_meta = create_args["meta"]
    directive = create_args["args"]["messages"][0]["content"]
    assert "Вася" in directive
    assert "друг" in directive
    assert f"relationship_id={relationship_id}" in directive
    assert "dialogue_id" not in real_meta

    # --- 2. Доставка B — рождение нового треда (44.2), реальный meta ---
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier, store)
    delivery_event = make_event(
        task_protocol.EVENT_TASK_RESULT,
        {
            "task_id": 1,
            "meta": real_meta,
            "ok": True,
            # То, что B реально увидит первым — в проде это пересказ модели,
            # здесь для стыка достаточно самой директивы.
            "result": {"response": directive},
        },
        src=Address(node="mycraft", service="tasks"),
    )
    await handler(delivery_event)
    assert len(notifier.sent) == 1
    b_chat_id, b_text = notifier.sent[0]
    assert b_chat_id == GUEST_B
    assert "Вася" in b_text
    # dialogue_id родился из sent_id первого сообщения B (44.2) — запись
    # в ai_turns о нём реально есть.
    b_turns = await store.ai_turns_for_dialogue(GUEST_B, notifier._next_id)
    assert len(b_turns) == 1

    # --- 3. B отвечает СОГЛАСИЕМ из своей проактивной сессии (нет
    # ctx.store/ctx.notifier там — Этап 44/tasks, см. _respond_relationship) ---
    emit = FakeEmit()
    responder_ctx = ai_tools.ToolContext(
        chat_id=GUEST_B,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        node_link=None,
        book=book,
        store=None,
        notifier=None,
        emit=emit,
    )
    confirm_result = await ai_tools.tool_confirm_relationship(
        responder_ctx, {"relationship_id": relationship_id}
    )
    assert "принято" in confirm_result
    assert len(emit.events) == 1
    event_type, event_data = emit.events[0]
    assert event_type == task_protocol.EVENT_RESPOND_RELATIONSHIP
    assert event_data == {
        "relationship_id": relationship_id,
        "accepted": True,
        "responder_chat_id": GUEST_B,
    }

    # --- 4. Мост EVENT_RESPOND_RELATIONSHIP пишет реальный Store и уведомляет A ---
    bridge_event = make_event(
        task_protocol.EVENT_RESPOND_RELATIONSHIP,
        event_data,
        src=Address(node="mycraft", service="tasks"),
    )
    await handler(bridge_event)

    updated = await store.get_relationship(relationship_id)
    assert updated is not None
    assert updated["status"] == "confirmed"
    assert updated["confirmed_at"] is not None

    assert len(notifier.sent) == 2
    a_chat_id, a_text = notifier.sent[1]
    assert a_chat_id == GUEST_A
    assert "Настя" in a_text
    assert "подтвердил" in a_text
