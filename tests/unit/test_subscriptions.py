import logging
from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramNetworkError

from sa_home_bot.config import GuestSubscriptionConfig, SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook, warn_group_log_config
from sa_home_bot.subscriptions.guests import render
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [
            SubscriptionConfig(
                name="me",
                chat_id=1,
                event_types=["*"],
                allowed_commands=["status", "scan_now"],
            ),
            SubscriptionConfig(
                name="room",
                chat_id=2,
                event_types=["overheat_started"],
                allowed_commands=[],
            ),
        ]
    )


def test_wildcard_accepts_everything():
    sub = Subscription("me", 1, frozenset({"*"}))
    assert sub.accepts_event("overheat_started")
    assert sub.accepts_event("system")


def test_specific_event_type_filter():
    sub = Subscription("room", 2, frozenset({"overheat_started"}))
    assert sub.accepts_event("overheat_started")
    assert not sub.accepts_event("overheat_cleared")
    assert not sub.accepts_event("system")


def test_allows_command():
    sub = Subscription("me", 1, allowed_commands=frozenset({"status"}))
    assert sub.allows_command("status")
    assert not sub.allows_command("scan_now")


def test_allows_action_full_and_bare_forms():
    sub = Subscription(
        "me", 1, allowed_commands=frozenset({"restart@node", "scan_now"})
    )
    assert sub.allows_action("restart", "node")
    assert not sub.allows_action("restart", "monitor")
    assert not sub.allows_action("stop", "node")
    assert sub.allows_action("scan_now", "monitor")


def test_wildcard_allows_any_command():
    sub = Subscription("me", 1, allowed_commands=frozenset({"*"}))
    assert sub.allows_command("status")
    assert sub.allows_command("anything")


def test_wildcard_allows_any_action_of_any_service():
    sub = Subscription("me", 1, allowed_commands=frozenset({"*"}))
    assert sub.allows_action("restart", "node")
    assert sub.allows_action("check_update", "node")
    assert sub.allows_action("scan_now", "monitor")


def test_service_wildcard_allows_only_that_service():
    sub = Subscription("me", 1, allowed_commands=frozenset({"*@node"}))
    assert sub.allows_action("restart", "node")
    assert sub.allows_action("check_update", "node")
    assert not sub.allows_action("scan_now", "monitor")
    assert not sub.allows_command("status")


def test_broken_blocks_everything():
    sub = Subscription(
        "me", 1, frozenset({"*"}), frozenset({"status", "restart@node"})
    ).with_broken()
    assert not sub.accepts_event("system")
    assert not sub.allows_command("status")
    assert not sub.allows_action("restart", "node")


def test_book_for_chat_and_accepting():
    book = _book()
    assert book.for_chat(1).name == "me"
    assert book.for_chat(999) is None
    accepting = {s.chat_id for s in book.accepting("overheat_started")}
    assert accepting == {1, 2}
    accepting_cleared = {s.chat_id for s in book.accepting("overheat_cleared")}
    assert accepting_cleared == {1}


async def test_validate_on_startup_marks_broken():
    book = _book()

    class FakeBot:
        async def get_chat(self, chat_id, request_timeout=None):
            if chat_id == 2:
                raise RuntimeError("chat not found")
            return object()

    issues = await book.validate_on_startup(FakeBot())
    assert [i.chat_id for i in issues] == [2]
    assert book.for_chat(2).broken is True
    assert book.for_chat(1).broken is False


async def test_validate_on_startup_survives_a_transient_network_blip(monkeypatch):
    import asyncio

    async def _no_delay(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_delay)
    book = _book()
    calls = {"n": 0}

    class FlakyBot:
        async def get_chat(self, chat_id, request_timeout=None):
            calls["n"] += 1
            if chat_id == 1 and calls["n"] == 1:
                raise TelegramNetworkError(method=None, message="timeout")
            return object()

    issues = await book.validate_on_startup(FlakyBot())
    assert issues == []
    assert book.for_chat(1).broken is False
    assert book.for_chat(2).broken is False


def test_guest_package_bytes_are_stable():
    from sa_home_bot.subscriptions.guests import HEADER, render

    guest = Subscription(
        name='Гость "Ы" \\ путь',
        chat_id=7,
        event_types=frozenset({"b", "a"}),
        allowed_commands=frozenset({"/status"}),
        invited_by_chat_id=1,
        invited_at="2026-01-01T00:00:00+00:00",
        invited_user="тест\tи\nперенос",
    )
    expected = (
        HEADER + "\n[[guest_subscriptions]]\n"
        'name = "Гость \\"Ы\\" \\\\ путь"\n'
        "chat_id = 7\n"
        'event_types = ["a", "b"]\n'
        'allowed_commands = ["/status"]\n'
        "invited_by_chat_id = 1\n"
        'invited_at = "2026-01-01T00:00:00+00:00"\n'
        'invited_user = "тест\\tи\\nперенос"\n'
    )
    assert render([guest]).decode("utf-8") == expected


def test_group_log_flag_false_without_list():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="me", chat_id=1, event_types=["*"], allowed_commands=["*"]),
            SubscriptionConfig(name="room", chat_id=-1001, event_types=["*"], allowed_commands=[]),
        ],
        group_log_chat_ids=[],
    )
    assert book.for_chat(1).group_log is False
    assert book.for_chat(-1001).group_log is False


def test_group_log_flag_true_for_group_in_list():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="me", chat_id=1, event_types=["*"], allowed_commands=["*"]),
            SubscriptionConfig(name="room", chat_id=-1001, event_types=["*"], allowed_commands=[]),
        ],
        group_log_chat_ids=[-1001],
    )
    assert book.for_chat(1).group_log is False
    assert book.for_chat(-1001).group_log is True


def test_group_log_flag_false_for_group_not_in_list():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="room1", chat_id=-1001, event_types=["*"], allowed_commands=[]),
            SubscriptionConfig(name="room2", chat_id=-1002, event_types=["*"], allowed_commands=[]),
        ],
        group_log_chat_ids=[-1001],
    )
    assert book.for_chat(-1001).group_log is True
    assert book.for_chat(-1002).group_log is False


def test_group_log_positive_id_never_gets_flag():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="me", chat_id=1, event_types=["*"], allowed_commands=["*"]),
        ],
        group_log_chat_ids=[1],
    )
    assert book.for_chat(1).group_log is False


def test_guest_keeps_group_log_after_rights_change():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="room", chat_id=-1001, event_types=["*"], allowed_commands=[]),
        ],
        guests=[
            GuestSubscriptionConfig(
                name="guest",
                chat_id=-1002,
                event_types=["*"],
                allowed_commands=["status"],
                invited_by_chat_id=1,
                invited_at="2026-01-01T00:00:00+00:00",
                invited_user="test",
            ),
        ],
        group_log_chat_ids=[-1001, -1002],
    )
    guest = book.for_chat(-1002)
    assert guest.group_log is True
    assert guest.is_guest

    updated = guest.with_allowed_commands(frozenset({"status", "scan_now"}))
    book.add(updated)
    assert book.for_chat(-1002).group_log is True


def test_new_guest_gets_group_log_on_add():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="room", chat_id=-1001, event_types=["*"], allowed_commands=[]),
        ],
        group_log_chat_ids=[-1001, -1002],
    )
    guest = Subscription(
        name="new_guest",
        chat_id=-1002,
        event_types=frozenset(["*"]),
        allowed_commands=frozenset(["status"]),
        source="guest",
        invited_by_chat_id=1,
        invited_at="2026-01-01T00:00:00+00:00",
        invited_user="test",
    )
    book.add(guest)
    assert book.for_chat(-1002).group_log is True


def test_subscription_loses_group_log_when_chat_id_not_in_list():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="room", chat_id=-1001, event_types=["*"], allowed_commands=[]),
        ],
        group_log_chat_ids=[-1001],
    )
    sub = Subscription(
        name="other",
        chat_id=-1002,
        event_types=frozenset(["*"]),
        allowed_commands=frozenset(["*"]),
        group_log=True,
    )
    book.add(sub)
    assert book.for_chat(-1002).group_log is False


@pytest.mark.parametrize(
    ("chat_id", "allowed", "chat_ids", "expected"),
    [
        (1, ["*"], [1], "положительный id"),
        (-1001, [], [-1001, -9999], "нет в подписках"),
        (-1001, ["*"], [-1001], "имеет право '*'"),
    ],
)
def test_warn_group_log_config_reports(caplog, chat_id, allowed, chat_ids, expected):
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="x", chat_id=chat_id, allowed_commands=allowed)],
        group_log_chat_ids=chat_ids,
    )
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.subscriptions.book"):
        warn_group_log_config(book, chat_ids)
    assert any(expected in r.message for r in caplog.records)


def test_warn_group_log_config_silent_on_correct_list(caplog):
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="room", chat_id=-1001, allowed_commands=["status"])],
        group_log_chat_ids=[-1001],
    )
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.subscriptions.book"):
        warn_group_log_config(book, [-1001])
    assert not caplog.records


def test_guest_package_ignores_group_log():
    guest = Subscription(name="g", chat_id=-1, source=SOURCE_GUEST)
    assert render([replace(guest, group_log=True)]) == render([guest])
