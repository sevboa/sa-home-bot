"""bot/recipients.py — find_recipients: как названного человека находят по
карточкам людей (Этап 58) и подпискам (bot/tools.py::tool_tell)."""

import pytest

from sa_home_bot.bot.recipients import (
    SOURCE_OWNER_ROLE,
    SOURCE_SUBSCRIPTION,
    _matches,
    find_recipients,
)
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

from .people_helpers import people_book


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


def test_find_recipients_by_card_name_and_telegram_username():
    book = SubscriptionBook([Subscription(name="me", chat_id=188548043)])
    people = people_book(
        book,
        {188548043: {"name": "Алексей Александрович Севбо"}},
        {188548043: ("Алексей Севбо", "asevbo")},
    )
    assert [r.chat_id for r in find_recipients("asevbo", book, people)] == [188548043]
    assert [r.chat_id for r in find_recipients("алексей", book, people)] == [188548043]


def test_find_recipients_ignores_people_without_subscription():
    # "Только подписной чат" — bot/recipients.py: совпадение по карточке
    # ничего не даёт, если у человека нет подписки в книге.
    book = SubscriptionBook([])
    people = people_book(book, {1: {"name": "Призрак"}}, {1: ("Призрак", "ghost")})
    assert find_recipients("ghost", book, people) == []


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
        "хозяину",
        "хозяин",
        "хозяина",
        "хозяином",
        "владельцу",
        "владелец",
        "владельца",
        "владельцем",
        "админу",
        "админ",
        "администратору",
        "администратор",
        "графу",
        "граф",
        "собственнику",
        "собственник",
        "admin",
        "owner",
        "ХОЗЯИНУ",
        "Owner",  # регистр не важен
    ],
)
def test_find_recipients_by_owner_role(query):
    book = SubscriptionBook([_owner()])
    found = find_recipients(query, book, people_book(book))
    assert [r.chat_id for r in found] == [188548043]
    assert found[0].source == SOURCE_OWNER_ROLE


def test_owner_role_reference_finds_nobody_without_an_owner():
    # Нет ни одной подписки с "*" (например, книга службы tasks) — роль
    # никого не находит, а не ломается.
    book = SubscriptionBook([_guest("Гость", 600)])
    assert find_recipients("хозяину", book, people_book(book)) == []


def test_owner_role_reference_does_not_shadow_ordinary_guest_names():
    # Обычный поиск по личному имени гостя не должен внезапно находить ещё
    # и владельца — стебли роли проверяются отдельной веткой, не влияют на
    # остальные пути поиска.
    book = SubscriptionBook([_owner(), _guest("Максим", 601)])
    found = find_recipients("максим", book, people_book(book))
    assert [r.chat_id for r in found] == [601]


# --- лейбл "Имя (@ник)" с чужим именем: ищем по нику ---


def test_find_recipients_label_with_misnamed_person_uses_handle():
    # Живой баг 2026-09-27: модель передала "Наталья (@nava40a)", гостья
    # вошла как "Наташа Сорокина (@nava40a)" — имя не совпало, ник совпал.
    book = SubscriptionBook(
        [
            _guest("Наташа Сорокина (@nava40a)", 1243270013),
            _guest("Наталья Петрова (@other_nat)", 555),
        ]
    )
    assert [r.chat_id for r in find_recipients("Наталья (@nava40a)", book)] == [1243270013]


def test_find_recipients_label_handle_via_telegram_profile():
    book = SubscriptionBook([_guest("Наташа", 1243270013)])
    people = people_book(book, {1243270013: {"name": "Н. В."}}, {1243270013: ("", "nava40a")})
    assert [r.chat_id for r in find_recipients("Наталья (@nava40a)", book, people)] == [1243270013]


def test_find_recipients_label_unknown_handle_falls_back_to_name():
    book = SubscriptionBook([_guest("Наташа Сорокина (@nava40a)", 1243270013)])
    assert [r.chat_id for r in find_recipients("Наташа (@gone_nick)", book)] == [1243270013]
    assert find_recipients("Никодим (@gone_nick)", book) == []


def test_find_recipients_handle_is_exact_not_prefix():
    book = SubscriptionBook([_guest("Наташа Сорокина (@nava40a)", 1243270013)])
    assert find_recipients("Наталья (@nava)", book) == []


def test_find_recipients_by_chat_id_in_query():
    book = SubscriptionBook(
        [_guest("Наташа Сорокина (@nava40a)", 1243270013), _guest("Наталья", 555555)]
    )
    assert [r.chat_id for r in find_recipients("Наталья (id 1243270013)", book)] == [1243270013]
    assert [r.chat_id for r in find_recipients("1243270013", book)] == [1243270013]
    # Чужой id — не подписчик: к имени не откатываемся, писать некому.
    assert find_recipients("Наталья (id 99999999)", book) == []


# --- живые находки 2026-09-27: имя+фамилия без отчества, people без id ---


def test_matches_multiword_query_skips_middle_name():
    assert _matches("алексей севбо", "Алексей Александрович Севбо")
    assert _matches("севбо алексей", "Алексей Александрович Севбо")
    assert _matches("алекс севб", "Алексей Александрович Севбо")
    assert not _matches("павел севбо", "Алексей Александрович Севбо")
    # Одно слово кандидата не закрывает два слова запроса.
    assert not _matches("алексей алексей", "Алексей Александрович Севбо")


def test_find_recipients_owner_by_first_and_last_name():
    book = SubscriptionBook(
        [_guest("me", 188548043, invited_user=""), _guest("Kein", 518571647)]
    )
    people = people_book(
        book,
        {
            188548043: {"name": "Алексей Александрович Севбо"},
            518571647: {"name": "Андрей Александрович Севбо"},
        },
    )
    assert [r.chat_id for r in find_recipients("Алексей Севбо", book, people)] == [188548043]


def test_find_recipients_by_card_name_beside_invite_name():
    book = SubscriptionBook([_guest("Наташа Сорокина (@nava40a)", 1243270013)])
    people = people_book(book, {1243270013: {"name": "Наталья Вадимовна"}})
    found = find_recipients("Наталья Вадимовна", book, people)
    assert [r.chat_id for r in found] == [1243270013]
    assert [r.chat_id for r in find_recipients("Наталья", book, people)] == [1243270013]


def test_find_recipients_finds_brother_by_card_name_not_subscription_label():
    # Живая находка 2026-10-10: подписка брата владельца — «Kein», имя —
    # только в карточке. Display — имя из карточки с ником из Telegram.
    book = SubscriptionBook([_guest("Kein", 518571647), _guest("Andrey (@AndreyPietilya)", 348)])
    people = people_book(
        book, {518571647: {"name": "Андрей Александрович Севбо"}}, {518571647: ("Kein", "kein")}
    )
    found = find_recipients("Андрей Севбо", book, people)
    assert [r.chat_id for r in found] == [518571647]
    assert found[0].display == "Андрей Александрович Севбо (@kein)"
    assert [r.chat_id for r in find_recipients("@kein", book, people)] == [518571647]
