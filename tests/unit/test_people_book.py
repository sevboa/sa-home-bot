"""Карточка человека (Этап 58): PeopleBook, перенос в карточки, профиль
Telegram из апдейтов."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiogram.types import User

from sa_home_bot.bot.middlewares import ProfileMiddleware
from sa_home_bot.config import (
    GuestSubscriptionConfig,
    PersonConfig,
    Settings,
    SubscriptionConfig,
)
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.people import seed
from sa_home_bot.people.book import PeopleBook, split_label
from sa_home_bot.people.claims import ClaimError, build_cards, normalize_claim
from sa_home_bot.subscriptions.book import SubscriptionBook

from .people_helpers import claim, people_book

OWNER = 188548043
KEIN = 518571647
PIETILYA = 348284076
NATASHA = 1243270013


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _book() -> SubscriptionBook:
    def guest(name: str, chat_id: int) -> GuestSubscriptionConfig:
        return GuestSubscriptionConfig(
            name=name, chat_id=chat_id, allowed_commands=["chat@llm"], invited_user=name
        )

    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=OWNER, allowed_commands=["*"])],
        [
            guest("Kein", KEIN),
            guest("Andrey (@AndreyPietilya)", PIETILYA),
            guest("Наташа Сорокина (@nava40a)", NATASHA),
        ],
    )


# --- PeopleBook ----------------------------------------------------------


def test_split_label():
    assert split_label("Наташа Сорокина (@nava40a)") == ("Наташа Сорокина", "nava40a")
    assert split_label("Kein") == ("Kein", "")


def test_display_prefers_card_name_then_telegram_then_subscription():
    book = _book()
    people = people_book(
        book,
        {KEIN: {"name": "Андрей Александрович Севбо"}},
        {KEIN: ("Kein", "kein"), NATASHA: ("Наталья", "nava40a")},
    )
    assert people.display(KEIN) == "Андрей Александрович Севбо (@kein)"
    assert people.display(NATASHA) == "Наталья (@nava40a)"
    assert people.display(PIETILYA) == "Andrey (@AndreyPietilya)"
    assert people.display(777) is None


def test_labels_list_every_name_once_and_skip_owner_key():
    book = _book()
    people = people_book(
        book,
        {
            KEIN: {"name": "Андрей Александрович Севбо", "alias": ["Дрон"]},
            OWNER: {"name": "Алексей Александрович Севбо"},
        },
        {KEIN: ("Kein", "kein")},
    )
    assert people.labels(KEIN) == [
        ("имя", "Андрей Александрович Севбо"),
        ("прозвище", "Дрон"),
        ("имя в Telegram", "Kein"),
        ("ник", "@kein"),
    ]
    assert ("имя при приглашении", "me") not in people.labels(OWNER)


def test_self_claim_beats_later_acquaintance_and_old_names_stay_searchable():
    people = PeopleBook(
        [
            claim(KEIN, "name", "Андрей", by_id=OWNER, at="2026-10-01"),
            claim(KEIN, "name", "Кейн", at="2026-10-02"),
            claim(KEIN, "name", "Андрюха", by_id=OWNER, at="2026-10-03"),
        ],
        [],
    )
    assert people.name(KEIN) == "Кейн"
    assert [label for _k, label in people.labels(KEIN)] == ["Андрюха", "Кейн", "Андрей"]


def test_empty_book_falls_back_to_subscription():
    people = PeopleBook.empty(_book())
    assert people.display(NATASHA) == "Наташа Сорокина (@nava40a)"
    assert people.gender(NATASHA) is None


# --- claims --------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "raw", "value"),
    [
        ("birth_date", "1990-04-29", "1990-04-29"),
        ("timezone", "Europe/Moscow", "Europe/Moscow"),
        ("city", "  Нижний   Новгород ", "Нижний Новгород"),
    ],
)
def test_normalize_new_fields(field, raw, value):
    assert normalize_claim(field, raw) == (field, value)


@pytest.mark.parametrize(
    ("field", "raw"), [("birth_date", "29.04.1990"), ("timezone", "Moscow"), ("hobby", "x")]
)
def test_normalize_rejects_bad_values(field, raw):
    with pytest.raises(ClaimError):
        normalize_claim(field, raw)


def test_build_cards_picks_single_fields():
    cards = build_cards([claim(1, "city", "Алматы"), claim(1, "timezone", "Asia/Almaty")])
    assert cards[1]["city"]["value"] == "Алматы"
    assert cards[1]["birth_date"] is None


# --- перенос (58.2) --------------------------------------------------------


def _settings() -> Settings:
    return Settings(
        people=[
            PersonConfig(
                telegram_username="asevbo",
                telegram_id=OWNER,
                full_name="Алексей Александрович Севбо",
                gender="m",
                city="Алматы",
                timezone="Asia/Almaty",
                birth_date="1990-04-29",
            ),
            PersonConfig(telegram_id=KEIN, full_name="Андрей Александрович Севбо", gender="m"),
            # без id — по нику подписки
            PersonConfig(telegram_username="nava40a", full_name="Наталья Вадимовна", gender="f"),
        ]
    )


async def test_seed_local_moves_config_and_profiles_once(store):
    book = _book()
    now = datetime(2026, 10, 10, tzinfo=UTC)
    await store.record_ai_turn(KEIN, 1, 1, "user", "привет", now, KEIN, "Kein (@kein)")

    await seed.seed_local(store, _settings(), book, now)
    await seed.seed_local(store, _settings(), book, now)  # второй раз — ничего

    people = await PeopleBook.load(store, book)
    assert people.display(KEIN) == "Андрей Александрович Севбо (@kein)"
    assert people.display(NATASHA) == "Наталья Вадимовна (@nava40a)"
    assert people.display(OWNER) == "Алексей Александрович Севбо (@asevbo)"
    assert people.value(OWNER, "timezone") == "Asia/Almaty"
    assert people.card(OWNER)["gender"]["strength"] == "self"
    assert people.card(KEIN)["name"]["strength"] == "acquaintance"
    assert people.card(KEIN)["name"]["by_id"] == OWNER
    assert len(await store.person_claims()) == 5 + 2 + 2


class _GraphLink:
    def __init__(self, result=None, raises=None):
        self.result, self.raises, self.calls = result, raises, []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args))
        if self.raises:
            raise self.raises
        return self.result


async def test_seed_from_graph_keeps_sources_and_retries_when_asleep(store):
    book = _book()
    asleep = _GraphLink(raises=TimeoutError())
    assert await seed.seed_from_graph(store, book, asleep) is False
    assert await store.get_state(seed.SEED_GRAPH_KEY) is None

    awake = _GraphLink(
        {
            "cards": {
                str(NATASHA): {
                    "gender": {"value": "f", "by_id": NATASHA, "strength": "self", "at": "1"},
                    "name": None,
                    "aliases": [
                        {"value": "Ната", "by_id": OWNER, "strength": "acquaintance", "at": "2"}
                    ],
                }
            }
        }
    )
    assert await seed.seed_from_graph(store, book, awake) is True
    assert awake.calls[0] == ("person_cards", {"ids": f"{OWNER},{PIETILYA},{KEIN},{NATASHA}"})
    people = await PeopleBook.load(store, book)
    assert people.card(NATASHA)["gender"]["strength"] == "self"
    assert people.card(NATASHA)["aliases"][0]["by_id"] == OWNER
    assert await seed.seed_from_graph(store, book, asleep) is True  # уже перенесено


# --- профиль Telegram ----------------------------------------------------


async def test_profile_middleware_writes_only_on_change(store):
    middleware = ProfileMiddleware(store)
    calls = []

    async def handler(event, data):
        calls.append(event)

    user = User(id=KEIN, is_bot=False, first_name="Kein", username="kein")
    event = SimpleNamespace(from_user=user)
    await middleware(handler, event, {})
    first = await store.person_profiles()
    await middleware(handler, event, {})
    assert await store.person_profiles() == first  # seen_at не тронут — записи не было

    renamed = SimpleNamespace(
        from_user=User(id=KEIN, is_bot=False, first_name="Андрей", last_name="Севбо")
    )
    await middleware(handler, renamed, {})
    (row,) = await store.person_profiles()
    assert (row["first_name"], row["last_name"], row["username"]) == ("Андрей", "Севбо", "")
    assert len(calls) == 3
