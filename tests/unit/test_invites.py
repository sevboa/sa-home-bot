"""Приватный вход: коды, гейт молчания, гостевой пакет (AUTHORIZATION.md §10)."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace

import pytest
import pytest_asyncio

from sa_home_bot.bot import invites
from sa_home_bot.bot.middlewares import SilenceGate, invite_candidate
from sa_home_bot.config import (
    GuestSubscriptionConfig,
    InvitesConfig,
    SubscriptionConfig,
)
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.guests import GuestStore, render
from sa_home_bot.subscriptions.models import Subscription

from .conftest import BASE_TIME


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "invites.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, *args, **kwargs):
        self.sent.append((chat_id, text))
        return 1


def _book(*, admin_chat: int = 1) -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="admin", chat_id=admin_chat, allowed_commands=["*"])]
    )


def _gate(store, tmp_path, book=None, notifier=None, cfg=None) -> invites.Gatekeeper:
    return invites.Gatekeeper(
        cfg or InvitesConfig(),
        store,
        book or _book(),
        GuestStore(tmp_path / "telegram-bot.test.guests.toml"),
        notifier or FakeNotifier(),
    )


# --- формат кода ---------------------------------------------------------


def test_generated_code_is_in_alphabet():
    code = invites.generate_code()
    assert len(code) == invites.CODE_LENGTH
    assert all(ch in invites.ALPHABET for ch in code)


@pytest.mark.parametrize(
    "raw",
    ["ABCD1234", "abcd1234", "ABCD-1234", "  abcd 1234 ", "ABCD_1234"],
)
def test_normalize_accepts_human_variants(raw):
    assert invites.normalize(raw) == "ABCD1234"


def test_normalize_reads_confusable_letters_as_digits():
    # Crockford: O читается как 0, I и L — как 1 (человек списывает с экрана).
    assert invites.normalize("O1IL2345") == "01112345"


@pytest.mark.parametrize("raw", [None, "", "привет", "ABCD123", "ABCD12345", "x" * 40])
def test_normalize_rejects_everything_else(raw):
    assert invites.normalize(raw) is None


def test_format_code_is_grouped():
    assert invites.format_code("ABCD1234") == "ABCD-1234"


def test_invite_candidate_prefers_deep_link_payload():
    msg = SimpleNamespace(text="/start ABCD1234")
    assert invite_candidate(msg) == "ABCD1234"
    assert invite_candidate(SimpleNamespace(text="ABCD1234")) == "ABCD1234"
    assert invite_candidate(SimpleNamespace(text="/start")) is None
    assert invite_candidate(None) is None


# --- лимитер -------------------------------------------------------------


def test_limiter_allows_up_to_limit_then_stops():
    limiter = invites.AttemptLimiter(limit=2)
    assert limiter.register(5) is True
    assert limiter.register(5) is True
    assert limiter.register(5) is False
    # Момент исчерпания — ровно один, чтобы админа не заваливало.
    assert limiter.exhausted_now(5) is True
    limiter.register(5)
    assert limiter.exhausted_now(5) is False


def test_limiter_window_slides():
    limiter = invites.AttemptLimiter(limit=1)
    assert limiter.register(5, now=0.0) is True
    assert limiter.register(5, now=10.0) is False
    assert limiter.register(5, now=10_000.0) is True


# --- выдача и погашение --------------------------------------------------


async def test_issued_code_admits_once(store, tmp_path):
    gate = _gate(store, tmp_path)
    code, _expires = await gate.issue(chat_id=1, user_id=10)

    admission = await gate.try_admit(77, invites.format_code(code), user_name="Гость")
    assert admission is not None
    assert admission.subscription.chat_id == 77
    assert admission.subscription.is_guest
    assert admission.invited_by_chat_id == 1
    # Второй раз тот же код не работает — он одноразовый.
    assert await gate.try_admit(78, code) is None


async def test_admitted_guest_gets_configured_rights(store, tmp_path):
    cfg = InvitesConfig(grant_commands=["chat@llm"], grant_events=[])
    gate = _gate(store, tmp_path, cfg=cfg)
    code, _ = await gate.issue(chat_id=1, user_id=None)

    admission = await gate.try_admit(77, code)
    sub = admission.subscription
    assert sub.allows_command("chat@llm")
    assert not sub.allows_command("status")
    assert not sub.accepts_event("cpu_alert")


async def test_revoked_code_is_dead(store, tmp_path):
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    assert await gate.revoke_code(code) is True
    assert await gate.try_admit(77, code) is None


async def test_wrong_code_never_admits(store, tmp_path):
    gate = _gate(store, tmp_path)
    await gate.issue(chat_id=1, user_id=None)
    assert await gate.try_admit(77, "ZZZZ9999") is None


async def test_bruteforce_exhausts_limit_and_warns_admin(store, tmp_path):
    notifier = FakeNotifier()
    cfg = InvitesConfig(max_attempts_per_hour=2)
    gate = _gate(store, tmp_path, notifier=notifier, cfg=cfg)
    code, _ = await gate.issue(chat_id=1, user_id=None)

    for _ in range(3):
        assert await gate.try_admit(77, "ZZZZ9999") is None
    # Админ узнал ровно один раз.
    assert len(notifier.sent) == 1
    assert notifier.sent[0][0] == 1
    # И даже верный код после исчерпания лимита уже не пускает.
    assert await gate.try_admit(77, code) is None


async def test_invites_disabled_without_guest_package(store):
    gate = invites.Gatekeeper(
        InvitesConfig(), store, _book(), GuestStore(None), FakeNotifier()
    )
    assert gate.enabled is False
    code, _ = await gate.issue(chat_id=1, user_id=None)
    assert await gate.try_admit(77, code) is None


async def test_guest_can_be_revoked(store, tmp_path):
    gate = _gate(store, tmp_path)
    book = gate._book  # noqa: SLF001 — проверяем именно состав книги
    code, _ = await gate.issue(chat_id=1, user_id=None)
    await gate.try_admit(77, code)

    assert gate.revoke_guest(77) is True
    assert book.for_chat(77) is None
    # Владельческую подписку тем же путём снять нельзя.
    assert gate.revoke_guest(1) is False


# --- гостевой пакет ------------------------------------------------------


async def test_admission_is_written_to_guest_package(store, tmp_path):
    path = tmp_path / "telegram-bot.test.guests.toml"
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    await gate.try_admit(77, code, user_name="Гость (@guest)")

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert [g["chat_id"] for g in data["guest_subscriptions"]] == [77]
    assert data["guest_subscriptions"][0]["invited_by_chat_id"] == 1

    gate.revoke_guest(77)
    assert tomllib.loads(path.read_text(encoding="utf-8")).get("guest_subscriptions") is None


def test_render_is_deterministic():
    # Пакет реплицируется по хешу содержимого — одна и та же выдача обязана
    # давать те же байты, иначе ревизия росла бы на ровном месте.
    guests = [
        Subscription(name="b", chat_id=2, allowed_commands=frozenset({"z", "a"})),
        Subscription(name="a", chat_id=1, allowed_commands=frozenset({"a", "z"})),
    ]
    assert render(guests) == render(list(reversed(guests)))


def test_render_escapes_quotes():
    guest = Subscription(name='Гость "кавычки"', chat_id=5)
    data = tomllib.loads(render([guest]).decode("utf-8"))
    assert data["guest_subscriptions"][0]["name"] == 'Гость "кавычки"'


# --- книга подписок ------------------------------------------------------


def test_guests_are_loaded_from_config():
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="admin", chat_id=1, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="гость", chat_id=77, event_types=[], allowed_commands=["chat@llm"]
            )
        ],
    )
    guest = book.for_chat(77)
    assert guest is not None and guest.is_guest
    assert [g.chat_id for g in book.guests()] == [77]


def test_owner_subscription_wins_over_stale_guest_record():
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="admin", chat_id=1, allowed_commands=["*"])],
        [GuestSubscriptionConfig(name="гость", chat_id=1, allowed_commands=["chat@llm"])],
    )
    sub = book.for_chat(1)
    assert sub.allows_command("status")  # права админа, не урезанные гостевые
    assert book.guests() == []


# --- гейт молчания -------------------------------------------------------


def _update(chat_id: int, text: str | None = None, *, kind: str = "message"):
    message = (
        SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, title=None),
            text=text,
            from_user=SimpleNamespace(id=chat_id, full_name="Гость", username=None),
        )
        if text is not None
        else None
    )
    empty = dict.fromkeys(
        (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "my_chat_member",
            "chat_member",
            "callback_query",
        )
    )
    if kind == "callback":
        empty["callback_query"] = SimpleNamespace(message=message)
    else:
        empty["message"] = message
    return SimpleNamespace(**empty)


async def _passthrough(event, data):
    return "HANDLED"


async def test_gate_passes_subscribed_chat(store, tmp_path):
    book = _book()
    gate = SilenceGate(book, _gate(store, tmp_path, book=book))
    assert await gate(_passthrough, _update(1, "/status"), {}) == "HANDLED"


async def test_gate_swallows_everything_from_stranger(store, tmp_path):
    book = _book()
    gate = SilenceGate(book, _gate(store, tmp_path, book=book))
    for text in ("/start", "/help", "/ping", "/whoami", "привет", "ZZZZ9999"):
        assert await gate(_passthrough, _update(999, text), {}) is None
    # Кнопка из чужого чата — тоже ничего.
    assert await gate(_passthrough, _update(999, "любой", kind="callback"), {}) is None


async def test_gate_admits_on_valid_code_and_marks_update(store, tmp_path):
    book = _book()
    keeper = _gate(store, tmp_path, book=book)
    code, _ = await keeper.issue(chat_id=1, user_id=None)
    gate = SilenceGate(book, keeper)

    data: dict = {}
    assert await gate(_passthrough, _update(77, f"/start {code}"), data) == "HANDLED"
    assert data["admission"].subscription.chat_id == 77
    # Впущенный чат дальше проходит как свой, без всяких кодов.
    assert await gate(_passthrough, _update(77, "привет"), {}) == "HANDLED"


# --- интеграция с диспетчером --------------------------------------------


async def test_stranger_update_reaches_no_router(store, tmp_path):
    """Гейт зарегистрирован так, что чужой апдейт не доходит ни до одного
    роутера: проверяем на настоящем Dispatcher, а не на middleware в вакууме."""
    from aiogram import Bot, Router
    from aiogram.types import Chat, Message, Update, User

    from sa_home_bot.bot.setup import build_dispatcher

    book = _book()
    dp = build_dispatcher(book, _gate(store, tmp_path, book=book))
    seen: list[str] = []
    spy = Router(name="spy")

    @spy.message()
    async def _catch_all(message: Message) -> None:  # pragma: no cover — не должен вызваться
        seen.append(message.text or "")

    dp.include_router(spy)
    bot = Bot("123456:fake-token-for-tests")
    chat = Chat(id=999, type="private")
    user = User(id=999, is_bot=False, first_name="Чужой")

    try:
        for text in ("/start", "/help", "привет"):
            update = Update(
                update_id=1,
                message=Message(
                    message_id=1, date=BASE_TIME, chat=chat, from_user=user, text=text
                ),
            )
            await dp.feed_update(bot, update)
    finally:
        await bot.session.close()

    assert seen == []


# --- код, присланный в подписной чат -------------------------------------


async def test_known_code_is_recognised_inside(store, tmp_path):
    """Живая находка 2026-07-30: код, присланный уже подписанным человеком,
    уезжал в разговор с Альфредом и оттуда в веб-поиск. Распознаём такой код
    до того, как он попадёт куда-либо ещё."""
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)

    known = await gate.known_code(invites.format_code(code))
    assert known is not None
    found_code, row = known
    assert found_code == code
    assert row["issued_by_chat_id"] == 1


async def test_random_lookalike_string_is_not_a_code(store, tmp_path):
    """Восемь символов из алфавита — слишком слабый признак сам по себе:
    перехватывать по нему любую похожую строку в живом разговоре нельзя."""
    gate = _gate(store, tmp_path)
    await gate.issue(chat_id=1, user_id=None)
    assert await gate.known_code("ZZZZ9999") is None
    assert await gate.known_code("привет, как дела") is None


async def test_redeemed_code_is_still_recognisable(store, tmp_path):
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    await gate.try_admit(77, code)

    known = await gate.known_code(code)
    assert known is not None
    assert known[1]["redeemed_at"] is not None
