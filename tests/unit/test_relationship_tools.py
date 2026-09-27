"""Тулы знакомства между гостями (Этап 46: связь одна — знакомство):
request_acquaintance (модель только открывает форму, решают кнопки), общая
проверка acquaintance_conflict и my_acquaintances. Сам жизненный цикл
формы (кнопки, экспирация, речь + форма) — в test_pending_actions.py,
передача сообщений знакомым — в test_tell.py."""

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


def _guest(name: str, chat_id: int) -> GuestSubscriptionConfig:
    return GuestSubscriptionConfig(
        name=name, chat_id=chat_id, allowed_commands=["chat@llm"], invited_user=name
    )


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [_guest("Вася", GUEST_A), _guest("Настя", GUEST_B), _guest("Игорь", GUEST_C)],
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


async def _acquainted(store, a, b):
    now = datetime.now(tz=UTC)
    return await store.add_confirmed_relationship(a, b, "acquaintance", now, now)


async def _request(ctx, recipient):
    return await ai_tools.tool_request_acquaintance(ctx, {"recipient": recipient})


# --- request_acquaintance ---


async def test_form_creates_draft_but_sends_nothing_yet(store):
    ctx, notifier, node_link = _ctx(store, chat_id=GUEST_A, book=_book())

    result = await _request(ctx, "Настя")

    assert "НИЧЕГО не отправлено" in result
    assert "знакомства" in result and "адресат — Настя" in result
    assert "передавать сообщения" in result
    rows = await store.open_pending_actions(chat_id=GUEST_A)
    assert len(rows) == 1
    assert rows[0]["status"] == "draft"
    assert rows[0]["addressee"] == GUEST_B
    assert rows[0]["payload"]["relation"] == "acquaintance"
    # Форма уходит ПОСЛЕ ответа Альфреда (flush_drafts), не из тула; адресат
    # о черновике не знает вовсе.
    assert notifier.sent == []
    assert await store.open_pending_actions(chat_id=GUEST_B) == []
    # Будильник экспирации поставлен, речь адресату — нет.
    assert [c[1]["action"] for c in node_link.calls] == ["timer"]


def test_declaration_has_no_relation_type():
    """Этап 46: тип связи модель больше не выбирает — только адресата."""
    params = ai_tools._DECL_REQUEST_ACQUAINTANCE["function"]["parameters"]  # noqa: SLF001
    assert set(params["properties"]) == {"recipient"}
    names = {spec.name for spec in ai_tools.TOOLS}
    assert {"request_acquaintance", "my_acquaintances"} <= names
    assert not names & {"request_relationship_form", "my_relationships"}


async def test_form_unavailable_without_pending_actions(store):
    # Служба tasks (chat_loop) — форм там показать некому.
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book(), with_forms=False)
    assert "недоступно" in await _request(ctx, "Настя")
    assert await store.open_pending_actions() == []


async def test_form_input_errors(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    assert "не сказано" in await ai_tools.tool_request_acquaintance(ctx, {})
    assert "не знаю" in await _request(ctx, "Никодим")
    assert "самому себе" in await _request(ctx, "Вася")
    assert await store.open_pending_actions() == []


async def test_form_ambiguous_recipient_asks_to_clarify(store):
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="owner", chat_id=OWNER_CHAT, allowed_commands=["*"])],
        [_guest("Вася", GUEST_A), _guest("Настя", GUEST_B), _guest("Настя", GUEST_C)],
    )
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=book)
    assert "уточни" in await _request(ctx, "Настя")
    assert await store.open_pending_actions() == []


async def test_form_duplicate_open_draft_is_refused(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    await _request(ctx, "Настя")
    assert "уже открыта" in await _request(ctx, "Настя")
    assert len(await store.open_pending_actions()) == 1


async def test_form_already_acquainted_either_direction(store):
    await _acquainted(store, GUEST_B, GUEST_A)
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    assert "уже знакомы" in await _request(ctx, "Настя")
    assert await store.open_pending_actions() == []


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

    result = await _request(ctx, "Настя")

    assert "Алексей Александрович Севбо" not in result
    assert "asevbo" not in result
    assert "«вы»" in result
    # Падеж — на служебном слове, имя именительным лейблом.
    assert "адресат — Наташа Сорокина" in result
    payload = (await store.open_pending_actions())[0]["payload"]
    assert payload["initiator_name"] == "Алексей Александрович Севбо (@asevbo)"
    assert payload["addressee_name"] == "Наташа Сорокина"


# --- acquaintance_conflict (повторная проверка на «Отправить»/«Принять») ---


async def test_conflict_ignores_the_form_itself(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    await _request(ctx, "Настя")
    row = (await store.open_pending_actions())[0]
    assert (
        await ai_tools.acquaintance_conflict(
            store, GUEST_A, GUEST_B, "Настя", ignore_action_id=row["id"]
        )
        is None
    )
    assert "уже открыта" in await ai_tools.acquaintance_conflict(
        store, GUEST_A, GUEST_B, "Настя"
    )


async def test_conflict_counter_offer_points_to_its_form(store):
    # Встречное предложение B → A уже отправлено — A надо лишь нажать
    # «Принять» в форме B, а не слать своё.
    now = datetime.now(tz=UTC)
    row = await store.create_pending_action(
        "relationship", GUEST_B, GUEST_A, {"relation": "acquaintance"}, now, now
    )
    await store.transition_pending_action(row["id"], ("draft",), "pending", now, expires_at=now)
    result = await ai_tools.acquaintance_conflict(store, GUEST_A, GUEST_B, "Настя")
    assert "уже сам предложил вам знакомство" in result and "Принять" in result


async def test_are_acquainted_is_symmetric_and_pairwise(store):
    await _acquainted(store, GUEST_A, GUEST_B)
    assert await ai_tools.are_acquainted(store, GUEST_A, GUEST_B)
    assert await ai_tools.are_acquainted(store, GUEST_B, GUEST_A)
    assert not await ai_tools.are_acquainted(store, GUEST_A, GUEST_C)


# --- my_acquaintances ---


async def test_my_acquaintances_no_store():
    ctx = ai_tools.ToolContext(
        chat_id=GUEST_A,
        dialogue_id=None,
        trigger_message_id=None,
        settings=Settings(),
        book=_book(),
    )
    assert "недоступно" in await ai_tools.tool_my_acquaintances(ctx, {})


async def test_my_acquaintances_caller_unknown(store):
    ctx, _, _ = _ctx(store, chat_id=STRANGER_CHAT, book=_book())
    assert "недоступно" in await ai_tools.tool_my_acquaintances(ctx, {})


async def test_my_acquaintances_empty(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    assert await ai_tools.tool_my_acquaintances(ctx, {}) == "подтверждённых знакомств нет"


async def test_my_acquaintances_both_roles(store):
    await _acquainted(store, GUEST_B, GUEST_A)
    await _acquainted(store, GUEST_A, GUEST_C)
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    result = await ai_tools.tool_my_acquaintances(ctx, {})
    assert "Настя" in result and "Игорь" in result
    assert "передавать сообщения" in result


async def test_my_acquaintances_silent_about_open_forms(store):
    ctx, _, _ = _ctx(store, chat_id=GUEST_A, book=_book())
    await _request(ctx, "Настя")
    assert await ai_tools.tool_my_acquaintances(ctx, {}) == "подтверждённых знакомств нет"
