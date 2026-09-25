"""bot/node_events.py::_handle_respond_relationship — мост записи
guest_relationships для confirm_relationship/reject_relationship, вызванных
внутри проактивной сессии агента установки связи (Этап 42.6.2, служба tasks
— своего Store там нет, см. tasks/protocol.py::EVENT_RESPOND_RELATIONSHIP)."""

from __future__ import annotations

from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

GUEST_A = 301  # инициатор — получает уведомление об ответе
GUEST_B = 302  # адресат — тот, кто отвечает


class FakeNotifier:
    def __init__(self, message_id: int | None = 555) -> None:
        self.sent: list[tuple[int, str]] = []
        self._message_id = message_id

    async def send_direct(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        return self._message_id


class FakeRelationshipStore:
    """Минимальный двойник Store — только то, что нужно
    _handle_respond_relationship: get_relationship/respond_relationship/
    record_ai_turn. Переходы статуса списаны с реального
    db/store.py::respond_relationship (Этап 42.6.1)."""

    def __init__(self, row: dict) -> None:
        self._row = dict(row)
        self.recorded_turns: list[tuple] = []

    async def get_relationship(self, relationship_id):
        if self._row["id"] != relationship_id:
            return None
        return dict(self._row)

    async def respond_relationship(self, relationship_id, accepted, now):
        if self._row["id"] != relationship_id or self._row["status"] != "pending":
            return None
        self._row["status"] = "confirmed" if accepted else "rejected"
        self._row["confirmed_at"] = now.isoformat() if accepted else None
        return dict(self._row)

    async def record_ai_turn(self, chat_id, message_id, dialogue_id, role, content, at, **kw):
        self.recorded_turns.append((chat_id, message_id, dialogue_id, role, content))


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="owner", chat_id=1, allowed_commands=["*"]),
            SubscriptionConfig(name="Вася", chat_id=GUEST_A, allowed_commands=["chat@llm"]),
            SubscriptionConfig(name="Настя", chat_id=GUEST_B, allowed_commands=["chat@llm"]),
        ]
    )


def _pending_row(relationship_id: int = 1) -> dict:
    return {
        "id": relationship_id,
        "guest_a": GUEST_A,
        "guest_b": GUEST_B,
        "relation": "friend",
        "status": "pending",
        "proposed_by": GUEST_A,
        "created_at": "2026-09-26T00:00:00+00:00",
        "confirmed_at": None,
    }


def _event(data: dict):
    return make_event(
        task_protocol.EVENT_RESPOND_RELATIONSHIP, data, src=Address(node="mycraft", service="tasks")
    )


async def test_respond_relationship_confirms_and_notifies_proposer():
    book, notifier, store = _book(), FakeNotifier(), FakeRelationshipStore(_pending_row())
    handler = build_node_event_handler(book, notifier, store)

    await handler(_event({"relationship_id": 1, "accepted": True, "responder_chat_id": GUEST_B}))

    updated = await store.get_relationship(1)
    assert updated["status"] == "confirmed"
    assert len(notifier.sent) == 1
    chat_id, text = notifier.sent[0]
    assert chat_id == GUEST_A
    assert "Настя" in text
    assert "подтвердил" in text
    assert len(store.recorded_turns) == 1


async def test_respond_relationship_rejects_and_notifies_proposer():
    book, notifier, store = _book(), FakeNotifier(), FakeRelationshipStore(_pending_row())
    handler = build_node_event_handler(book, notifier, store)

    await handler(_event({"relationship_id": 1, "accepted": False, "responder_chat_id": GUEST_B}))

    updated = await store.get_relationship(1)
    assert updated["status"] == "rejected"
    chat_id, text = notifier.sent[0]
    assert chat_id == GUEST_A
    assert "отклонил" in text


async def test_respond_relationship_ignores_wrong_responder():
    book, notifier, store = _book(), FakeNotifier(), FakeRelationshipStore(_pending_row())
    handler = build_node_event_handler(book, notifier, store)

    # GUEST_A (инициатор) пытается ответить за GUEST_B — сессия с таким
    # ctx.chat_id в реальности не создалась бы (schedule_agent_dialogue
    # целится в target_chat_id=GUEST_B), но мост всё равно должен отказать.
    await handler(_event({"relationship_id": 1, "accepted": True, "responder_chat_id": GUEST_A}))

    updated = await store.get_relationship(1)
    assert updated["status"] == "pending"
    assert notifier.sent == []


async def test_respond_relationship_ignores_unknown_id():
    book, notifier, store = _book(), FakeNotifier(), FakeRelationshipStore(_pending_row())
    handler = build_node_event_handler(book, notifier, store)

    await handler(_event({"relationship_id": 999, "accepted": True, "responder_chat_id": GUEST_B}))

    assert notifier.sent == []


async def test_respond_relationship_ignores_already_answered():
    row = _pending_row()
    row["status"] = "confirmed"
    book, notifier, store = _book(), FakeNotifier(), FakeRelationshipStore(row)
    handler = build_node_event_handler(book, notifier, store)

    await handler(_event({"relationship_id": 1, "accepted": False, "responder_chat_id": GUEST_B}))

    updated = await store.get_relationship(1)
    assert updated["status"] == "confirmed"  # не переписали повторным ответом
    assert notifier.sent == []


async def test_respond_relationship_ignores_malformed_data():
    book, notifier, store = _book(), FakeNotifier(), FakeRelationshipStore(_pending_row())
    handler = build_node_event_handler(book, notifier, store)

    for data in (
        {"accepted": True, "responder_chat_id": GUEST_B},
        {"relationship_id": 1, "responder_chat_id": GUEST_B},
        {"relationship_id": 1, "accepted": True},
    ):
        await handler(_event(data))

    assert notifier.sent == []
    updated = await store.get_relationship(1)
    assert updated["status"] == "pending"
