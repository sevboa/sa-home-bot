"""bot/recipients.py — find_recipients: как названного человека находят по
[[people]] и подпискам (bot/tools.py::tool_tell)."""

import pytest

from sa_home_bot.bot.recipients import (
    SOURCE_OWNER_ROLE,
    SOURCE_SUBSCRIPTION,
    _matches,
    find_recipients,
)
from sa_home_bot.config import PersonConfig
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription


def _guest(name: str, chat_id: int, invited_user: str | None = None) -> Subscription:
    return Subscription(
        name=name, chat_id=chat_id, invited_user=invited_user if invited_user is not None else name
    )


# --- _matches: сопоставление имени/ника ---


def test_matches_exact_and_prefix_word():
    assert _matches("андрей", "Андрей Иванов")
    assert not _matches("андрюха", "Андрей Иванов")  # намеренно: не угадывать


def test_matches_bare_username_inside_parens():
    # Живой баг 2026-08-01: invited_user вида "Имя Фамилия (@ник)" — голый
    # ник без "@" не находился, хотя имя находило того же человека.
    candidate = "Наташа Сорокина (@nava40a)"
    assert _matches("nava40a", candidate)
    assert _matches("наташа", candidate)


def test_find_recipients_by_username_with_leading_at():
    # query нормализует find_recipients (лишний "@" перед юзернеймом — как
    # его обычно и пишут) — _matches получает уже готовое "nava40a".
    book = SubscriptionBook([_guest("Наташа Сорокина (@nava40a)", 1243270013)])
    assert [r.chat_id for r in find_recipients("@nava40a", book)] == [1243270013]


def test_matches_empty_query_or_candidate_is_false():
    assert not _matches("", "Андрей Иванов")
    assert not _matches("андрей", "")


# --- find_recipients: сборка кандидатов из people + подписок ---


def test_find_recipients_by_username_in_parens():
    book = SubscriptionBook([_guest("Наташа Сорокина (@nava40a)", 1243270013)])
    found = find_recipients("nava40a", book)
    assert [r.chat_id for r in found] == [1243270013]
    assert found[0].source == SOURCE_SUBSCRIPTION


def test_find_recipients_by_people_username_and_full_name():
    people = [
        PersonConfig(
            telegram_username="asevbo",
            telegram_id=188548043,
            full_name="Алексей Александрович Севбо",
            gender="m",
        )
    ]
    book = SubscriptionBook([Subscription(name="me", chat_id=188548043)])
    assert [r.chat_id for r in find_recipients("asevbo", book, people)] == [188548043]
    assert [r.chat_id for r in find_recipients("алексей", book, people)] == [188548043]


def test_find_recipients_ignores_people_without_subscription():
    # "Только подписной чат" — bot/recipients.py: совпадение по [[people]]
    # ничего не даёт, если у человека нет подписки в книге.
    people = [
        PersonConfig(telegram_username="ghost", telegram_id=1, full_name="Призрак", gender="m")
    ]
    assert find_recipients("ghost", SubscriptionBook([]), people) == []


def test_find_recipients_ignores_group_chats():
    book = SubscriptionBook([_guest("Группа Х", -100123)])
    assert find_recipients("группа", book) == []


def test_find_recipients_empty_query():
    book = SubscriptionBook([_guest("Наташа", 1243270013)])
    assert find_recipients("", book) == []


# --- обращение к владельцу по роли, не по имени ---------------------------


def _owner(chat_id: int = 188548043) -> Subscription:
    # Прод-конфиг называет владельческую подписку технически, "me" — не по
    # имени, которое гость мог бы угадать (instances/telegram-bot.alfred.toml).
    return Subscription(name="me", chat_id=chat_id, allowed_commands=frozenset({"*"}))


@pytest.mark.parametrize(
    "query",
    [
        "хозяину", "хозяин", "хозяина", "хозяином",
        "владельцу", "владелец", "владельца", "владельцем",
        "админу", "админ", "администратору", "администратор",
        "графу", "граф",
        "собственнику", "собственник",
        "admin", "owner",
        "ХОЗЯИНУ", "Owner",  # регистр не важен
    ],
)
def test_find_recipients_by_owner_role(query):
    book = SubscriptionBook([_owner()])
    found = find_recipients(query, book, [])
    assert [r.chat_id for r in found] == [188548043]
    assert found[0].source == SOURCE_OWNER_ROLE


def test_owner_role_reference_finds_nobody_without_an_owner():
    # Нет ни одной подписки с "*" (например, книга службы tasks) — роль
    # никого не находит, а не ломается.
    book = SubscriptionBook([_guest("Гость", 600)])
    assert find_recipients("хозяину", book, []) == []


def test_owner_role_reference_does_not_shadow_ordinary_guest_names():
    # Обычный поиск по личному имени гостя не должен внезапно находить ещё
    # и владельца — стебли роли проверяются отдельной веткой, не влияют на
    # остальные пути поиска.
    book = SubscriptionBook([_owner(), _guest("Максим", 601)])
    found = find_recipients("максим", book, [])
    assert [r.chat_id for r in found] == [601]
