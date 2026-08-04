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


async def test_issue_ttl_override_replaces_config_default(store, tmp_path):
    """/invite <часы> (bot/handlers/invites.py::cmd_invite) переопределяет
    [invites].ttl_s разово, не трогая конфиг."""
    from datetime import UTC, datetime

    cfg = InvitesConfig(ttl_s=3600.0)
    gate = _gate(store, tmp_path, cfg=cfg)
    before = datetime.now(tz=UTC)
    _code, expires = await gate.issue(chat_id=1, user_id=None, ttl_s=10 * 3600.0)
    assert 9.9 * 3600 < (expires - before).total_seconds() < 10.1 * 3600


async def test_issue_without_ttl_uses_config_default(store, tmp_path):
    from datetime import UTC, datetime

    cfg = InvitesConfig(ttl_s=1800.0)
    gate = _gate(store, tmp_path, cfg=cfg)
    before = datetime.now(tz=UTC)
    _code, expires = await gate.issue(chat_id=1, user_id=None)
    assert 1790 < (expires - before).total_seconds() < 1810


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


# --- точечная правка прав гостя (решение 2026-08-04) ----------------------


async def test_add_guest_right_updates_book_and_package(store, tmp_path):
    path = tmp_path / "telegram-bot.test.guests.toml"
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    await gate.try_admit(77, code)

    updated = gate.add_guest_right(77, "usage@vpn")
    assert updated is not None
    assert updated.allows_command("usage@vpn")
    assert gate._book.for_chat(77).allows_command("usage@vpn")  # noqa: SLF001

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "usage@vpn" in data["guest_subscriptions"][0]["allowed_commands"]


async def test_remove_guest_right_updates_book_and_package(store, tmp_path):
    path = tmp_path / "telegram-bot.test.guests.toml"
    cfg = InvitesConfig(grant_commands=["chat@llm", "search@net"])
    gate = _gate(store, tmp_path, cfg=cfg)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    await gate.try_admit(77, code)

    updated = gate.remove_guest_right(77, "search@net")
    assert updated is not None
    assert not updated.allows_command("search@net")
    assert updated.allows_command("chat@llm")  # прочие права не тронуты

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["guest_subscriptions"][0]["allowed_commands"] == ["chat@llm"]


async def test_guest_rights_cannot_touch_owner_subscription(store, tmp_path):
    # Владельческая подписка (chat_id=1 в _book()) — не гость: точечная
    # правка прав её не касается, как и revoke_guest.
    gate = _gate(store, tmp_path)
    assert gate.add_guest_right(1, "usage@vpn") is None
    assert gate.remove_guest_right(1, "invite") is None


def test_guest_right_on_unknown_chat_is_noop(store, tmp_path):
    gate = _gate(store, tmp_path)
    assert gate.add_guest_right(9999, "usage@vpn") is None
    assert gate.remove_guest_right(9999, "usage@vpn") is None


# --- флаг «семья» гостя (решение 2026-08-04) ------------------------------


async def test_set_guest_family_updates_book_and_package(store, tmp_path):
    path = tmp_path / "telegram-bot.test.guests.toml"
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    await gate.try_admit(77, code)

    updated = gate.set_guest_family(77, True)
    assert updated is not None
    assert updated.family is True
    assert gate._book.for_chat(77).family is True  # noqa: SLF001

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["guest_subscriptions"][0]["family"] is True

    reverted = gate.set_guest_family(77, False)
    assert reverted is not None
    assert reverted.family is False
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    assert data["guest_subscriptions"][0]["family"] is False


async def test_set_guest_family_cannot_touch_owner_subscription(store, tmp_path):
    gate = _gate(store, tmp_path)
    assert gate.set_guest_family(1, True) is None


def test_set_guest_family_on_unknown_chat_is_noop(store, tmp_path):
    gate = _gate(store, tmp_path)
    assert gate.set_guest_family(9999, True) is None


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


def test_render_round_trips_family_flag():
    guest = Subscription(name="Наташа", chat_id=6, family=True)
    data = tomllib.loads(render([guest]).decode("utf-8"))
    cfg = GuestSubscriptionConfig(**data["guest_subscriptions"][0])
    assert cfg.family is True


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
    for text in ("/start", "/ping", "/status", "привет", "ZZZZ9999"):
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
        for text in ("/start", "/ping", "привет"):
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


# --- приветствие гостя ---------------------------------------------------


def test_welcome_prompt_demands_the_menu_hint():
    """Решение пользователя 2026-07-30: приветствие пишет модель, но подсказка
    про меню команд в нём обязательна (/help выпилен 2026-08-04)."""
    from sa_home_bot.bot.handlers import invites as invite_handlers

    assert invite_handlers.MENU_HINT_KEYWORD in invite_handlers.WELCOME_PROMPT.lower()
    assert invite_handlers.MENU_HINT_KEYWORD in invite_handlers.FALLBACK_HINT.lower()


async def test_welcome_prompt_uses_what_memory_knows(store, tmp_path, monkeypatch):
    """Обычный путь подмешивания памяти ищет по тексту реплики, а реплика тут —
    инвайт-код; поэтому память спрашивается отдельно, по имени гостя."""
    from types import SimpleNamespace

    from sa_home_bot.bot import ai_flow
    from sa_home_bot.bot.handlers import invites as invite_handlers

    asked: list[str] = []

    async def fake_recall(node_link, chat_id, query):
        asked.append(query)
        return ["любит чай без сахара"]

    monkeypatch.setattr(ai_flow, "recall_facts", fake_recall)
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    admission = await gate.try_admit(77, code, user_name="Наташа Сорокина (@nava40a)")

    message = SimpleNamespace(chat=SimpleNamespace(id=77))
    prompt = await invite_handlers._welcome_prompt(None, message, admission.subscription)

    # Запрос — по имени, без хвоста «(@username)».
    assert asked == ["Наташа Сорокина"]
    assert "любит чай без сахара" in prompt
    assert "как со знакомым" in prompt
    # требование про меню не теряется, когда есть что вспомнить
    assert invite_handlers.MENU_HINT_KEYWORD in prompt.lower()


async def test_welcome_prompt_without_memory_is_the_plain_directive(store, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sa_home_bot.bot import ai_flow
    from sa_home_bot.bot.handlers import invites as invite_handlers

    async def fake_recall(node_link, chat_id, query):
        return []

    monkeypatch.setattr(ai_flow, "recall_facts", fake_recall)
    gate = _gate(store, tmp_path)
    code, _ = await gate.issue(chat_id=1, user_id=None)
    admission = await gate.try_admit(77, code, user_name="Никто")

    message = SimpleNamespace(chat=SimpleNamespace(id=77))
    prompt = await invite_handlers._welcome_prompt(None, message, admission.subscription)
    assert prompt == invite_handlers.WELCOME_PROMPT


# --- текст сообщения с кодом ---------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(3600, "60 минут"), (60, "1 минута"), (180, "3 минуты"), (720, "12 минут"), (0, "0 минут")],
)
def test_expiry_is_spelled_in_minutes(seconds, expected):
    from datetime import UTC, datetime, timedelta

    from sa_home_bot.bot.handlers import invites as invite_handlers

    expires = datetime.now(tz=UTC) + timedelta(seconds=seconds)
    assert invite_handlers._format_expiry(expires) == expected


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(2, "2 часа"), (5, "5 часов"), (47, "47 часов")],
)
def test_expiry_is_spelled_in_hours_beyond_90_minutes(hours, expected):
    from datetime import UTC, datetime, timedelta

    from sa_home_bot.bot.handlers import invites as invite_handlers

    expires = datetime.now(tz=UTC) + timedelta(hours=hours)
    assert invite_handlers._format_expiry(expires) == expected


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(48, "2 дня"), (24 * 5, "5 дней"), (24 * 30, "30 дней")],
)
def test_expiry_is_spelled_in_days_beyond_48_hours(hours, expected):
    from datetime import UTC, datetime, timedelta

    from sa_home_bot.bot.handlers import invites as invite_handlers

    expires = datetime.now(tz=UTC) + timedelta(hours=hours)
    assert invite_handlers._format_expiry(expires) == expected


# --- /invite <часы> --------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_parse_invite_hours_defaults_to_none(raw):
    from sa_home_bot.bot.handlers import invites as invite_handlers

    assert invite_handlers._parse_invite_hours(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("3", 3.0), ("0.5", 0.5), ("2,5", 2.5)])
def test_parse_invite_hours_accepts_numbers(raw, expected):
    from sa_home_bot.bot.handlers import invites as invite_handlers

    assert invite_handlers._parse_invite_hours(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "не число", "9999", "abc3"])
def test_parse_invite_hours_rejects_invalid_or_out_of_range(raw):
    from sa_home_bot.bot.handlers import invites as invite_handlers

    with pytest.raises(ValueError):
        invite_handlers._parse_invite_hours(raw)


class _FakeMessage:
    def __init__(self, chat_id: int = 1, user_id: int | None = 10) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id) if user_id is not None else None
        self.answers: list[str] = []

    async def answer(self, text: str, *args, **kwargs) -> None:
        self.answers.append(text)


def _command(args: str | None) -> object:
    from aiogram.filters import CommandObject

    return CommandObject(command="invite", args=args)


async def test_cmd_invite_without_args_uses_config_default(store, tmp_path):
    from sa_home_bot.bot.handlers import invites as invite_handlers

    gate = _gate(store, tmp_path, cfg=InvitesConfig(ttl_s=3600.0))
    message = _FakeMessage()
    await invite_handlers.cmd_invite(message, _command(None), gate, "sa_home_test_bot")

    assert len(message.answers) == 1
    assert "60 минут" in message.answers[0]


async def test_cmd_invite_with_hours_overrides_default(store, tmp_path):
    from sa_home_bot.bot.handlers import invites as invite_handlers

    gate = _gate(store, tmp_path, cfg=InvitesConfig(ttl_s=3600.0))
    message = _FakeMessage()
    await invite_handlers.cmd_invite(message, _command("3"), gate, "sa_home_test_bot")

    assert len(message.answers) == 1
    assert "3 часа" in message.answers[0]


async def test_cmd_invite_rejects_bad_hours_without_issuing_code(store, tmp_path):
    from sa_home_bot.bot.handlers import invites as invite_handlers

    gate = _gate(store, tmp_path, cfg=InvitesConfig(ttl_s=3600.0))
    message = _FakeMessage()
    await invite_handlers.cmd_invite(message, _command("не число"), gate, "sa_home_test_bot")

    assert message.answers == [invite_handlers.BAD_INVITE_HOURS_TEXT]
    # Код не выпущен — открытых приглашений от этого чата нет.
    assert await gate.open_codes() == []


def test_guest_list_shows_local_time():
    """Живая находка 2026-07-30: /guests показывал UTC («вошёл 05:02» вместо
    10:02) — обрезка ISO-строки вместо перевода в местное время."""
    from datetime import UTC, datetime

    from sa_home_bot.bot.handlers import invites as invite_handlers

    moment = datetime(2026, 7, 30, 5, 2, tzinfo=UTC)
    expected = moment.astimezone().strftime("%Y-%m-%d %H:%M")
    assert invite_handlers._local_time(moment.isoformat()) == expected
    # Мусор не роняет список — показываем как есть.
    assert invite_handlers._local_time("не дата") == "не дата"
