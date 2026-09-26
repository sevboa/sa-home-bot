"""Тулы связей между гостями: request_relationship_form (Этап 45 —
заменил preview/propose/confirm/reject: модель только открывает форму,
решают кнопки), общая проверка relationship_conflict и my_relationships.
Сам жизненный цикл формы (кнопки, экспирация, речь + форма) — в
test_pending_actions.py."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.pending_actions import PendingActions
from sa_home_bot.config import GuestSubscriptionConfig, PersonConfig, Settings, SubscriptionConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook

OWNER_CHAT = 1
GUEST_A = 301  # инициатор
GUEST_B = 302  # адресат
GUEST_C = 303
STRANGER_CHAT = 999  # неизвестный chat_id — не в book


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _guest(name: str, chat_id: int, *, family: bool = False) -> GuestSubscriptionConfig:
    return GuestSubscriptionConfig(
        name=name,
        chat_id=chat_id,
        allowed_commands=["chat@llm"],
        invited_user=name,
        family=family,
    )


def _book(*, a_family: bool = False, b_family: bool = False) -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [
            _guest("Вася", GUEST_A, family=a_family),
            _guest("Настя", GUEST_B, family=b_family),
            _guest("Игорь", GUEST_C),
        ],
    )


class FakeNodeLink:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, object]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}, dst))
        return {"task_id": len(self.calls)}


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        return 1000 + len(self.sent)

    async def edit_text(self, chat_id, message_id, text, reply_markup=None):
        return True


def _ctx(store, *, chat_id, book, settings=None, with_forms=True):
    settings = settings or Settings()
    node_link = FakeNodeLink()
    notifier = FakeNotifier()
    pending = (
        PendingActions(store, notifier, settings, lambda: node_link) if with_forms else None
    )
    ctx = ai_tools.ToolContext(
        chat_id=chat_id,
        dialogue_id=1,
        trigger_message_id=1,
        settings=settings,
        node_link=node_link,
        book=book,
        store=store,
        notifier=notifier,
        pending_actions=pending,
    )
    return ctx, notifier, node_link


async def _confirmed(store, a, b, relation):
    now = datetime.now(tz=UTC)
    return await store.add_confirmed_relationship(a, b, relation, now, now)


# --- request_relationship_form ---


async def test_form_creates_draft_but_sends_nothing_yet(store):
    ctx, notifier, node_link = _ctx(store, chat_id=GUEST_A, book=_book())

    result = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "acquaintance"}
    )

    assert "НИЧЕГО не отправлено" in result
    assert "Настя" in result and "знакомый" in result
    rows = await store.open_pending_actions(chat_id=GUEST_A)
    assert len(rows) == 1
    assert rows[0]["status"] == "draft"
    assert rows[0]["payload"]["relation"] == "acquaintance"
    # Форма уходит ПОСЛЕ ответа Альфреда (flush_drafts), не из тула; адресат
    # о черновике не знает вовсе.
    assert notifier.sent == []
    assert await store.open_pending_actions(chat_id=GUEST_B) == []
    # Будильник экспирации поставлен, речь адресату — нет.
    assert [c[1]["action"] for c in node_link.calls] == ["timer"]


async def test_form_unavailable_without_pending_actions(store):
    # Служба tasks (chat_loop) — форм там показать некому.
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book(), with_forms=False)
    result = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "недоступно" in result
    assert await store.open_pending_actions() == []


async def test_form_input_errors(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    unknown = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": STRANGER_CHAT, "relation": "friend"}
    )
    bad_relation = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "enemy"}
    )
    self_target = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_A, "relation": "friend"}
    )
    assert "гость не найден" in unknown
    assert "relation должен быть" in bad_relation
    assert "самому себе" in self_target
    assert await store.open_pending_actions() == []


async def test_form_family_flag_no_longer_matters(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book(a_family=True, b_family=True))
    result = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "family"}
    )
    assert "Форма открыта" in result


async def test_form_duplicate_open_draft_is_refused(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    again = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "уже открыта" in again
    assert len(await store.open_pending_actions()) == 1


async def test_form_already_confirmed_relation(store):
    await _confirmed(store, GUEST_A, GUEST_B, "friend")
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "уже подтверждённая связь" in result
    assert await store.open_pending_actions() == []


async def test_form_spouse_exclusivity(store):
    await _confirmed(store, GUEST_B, GUEST_C, "spouse")
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    target_married = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )
    assert "у адресата (Настя) уже есть супруг(а)" in target_married

    ctx_c, _, _ = _ctx(store, chat_id=GUEST_C, book=_book())
    initiator_married = await ai_tools.tool_request_relationship_form(
        ctx_c, {"target_chat_id": GUEST_A, "relation": "spouse"}
    )
    assert "у инициатора уже есть супруг(а)" in initiator_married
    # Не супружеская связь с тем же человеком — можно.
    friend = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert "Форма открыта" in friend


async def test_form_owner_never_named_in_third_person_and_not_me(store):
    # Живые находки 2026-09-26: владелец технически "me" (не для показа), а
    # его настоящее ФИО в тексте, адресованном ЕМУ САМОМУ, Gemma озвучивала
    # слово в слово. Тул отвечает инициатору — его имени там нет вовсе; в
    # payload (форма адресату) — настоящее имя, не "me".
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [_guest("Настя", GUEST_B)],
    )
    settings = Settings(
        people=[
            PersonConfig(
                telegram_id=OWNER_CHAT,
                telegram_username="asevbo",
                full_name="Алексей Александрович Севбо",
                gender="m",
            ),
            PersonConfig(telegram_id=GUEST_B, full_name="Наташа Сорокина", gender="f"),
        ]
    )
    ctx, _, _ = _ctx(store, chat_id=OWNER_CHAT, book=book, settings=settings)

    result = await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "spouse"}
    )

    assert "Алексей Александрович Севбо" not in result
    assert "asevbo" not in result
    assert "«вы»" in result
    # Падеж — на служебном слове, имя именительным лейблом.
    assert "адресат — Наташа Сорокина" in result
    payload = (await store.open_pending_actions())[0]["payload"]
    assert payload["initiator_name"] == "Алексей Александрович Севбо (@asevbo)"
    assert payload["addressee_name"] == "Наташа Сорокина"


# --- relationship_conflict (повторная проверка на «Отправить»/«Принять») ---


async def test_conflict_ignores_the_form_itself(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    row = (await store.open_pending_actions())[0]
    assert (
        await ai_tools.relationship_conflict(
            store, GUEST_A, GUEST_B, "friend", "Настя", ignore_action_id=row["id"]
        )
        is None
    )
    assert "уже открыта" in await ai_tools.relationship_conflict(
        store, GUEST_A, GUEST_B, "friend", "Настя"
    )


async def test_conflict_pending_in_reverse_direction(store):
    # Встречная заявка B → A уже отправлена — A не может слать свою.
    now = datetime.now(tz=UTC)
    row = await store.create_pending_action(
        "relationship", GUEST_B, GUEST_A, {"relation": "friend"}, now, now
    )
    await store.transition_pending_action(row["id"], ("draft",), "pending", now, expires_at=now)
    result = await ai_tools.relationship_conflict(store, GUEST_A, GUEST_B, "friend", "Настя")
    assert "уже отправлено адресату (Настя)" in result


# --- my_relationships ---


async def test_my_relationships_no_store():
    ctx = ai_tools.ToolContext(
        chat_id=GUEST_A,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        book=_book(),
    )
    assert "недоступно" in await ai_tools.tool_my_relationships(ctx, {})


async def test_my_relationships_caller_unknown(store):
    ctx, _, _ = _ctx(store, chat_id=STRANGER_CHAT, book=_book())
    assert "недоступно" in await ai_tools.tool_my_relationships(ctx, {})


async def test_my_relationships_empty(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    assert await ai_tools.tool_my_relationships(ctx, {}) == "подтверждённых связей нет"


async def test_my_relationships_both_roles_and_types(store):
    await _confirmed(store, GUEST_B, GUEST_A, "acquaintance")
    await _confirmed(store, GUEST_A, GUEST_C, "family")
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Настя" in result and "знакомый" in result
    assert "Игорь" in result and "родство" in result


async def test_my_relationships_pairwise_spouse(store):
    await _confirmed(store, GUEST_A, GUEST_B, "spouse")
    ctx, _, _ = _ctx(store, chat_id=GUEST_B, book=_book())
    result = await ai_tools.tool_my_relationships(ctx, {})
    assert "Вася" in result and "супруг(а)" in result


async def test_my_relationships_silent_about_open_forms(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    await ai_tools.tool_request_relationship_form(
        ctx, {"target_chat_id": GUEST_B, "relation": "friend"}
    )
    assert await ai_tools.tool_my_relationships(ctx, {}) == "подтверждённых связей нет"
