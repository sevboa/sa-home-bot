from sentinel_bot.config import SubscriptionConfig
from sentinel_bot.subscriptions.book import SubscriptionBook
from sentinel_bot.subscriptions.models import Subscription


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


def test_broken_blocks_everything():
    sub = Subscription("me", 1, frozenset({"*"}), frozenset({"status"})).with_broken()
    assert not sub.accepts_event("system")
    assert not sub.allows_command("status")


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
        async def get_chat(self, chat_id):
            if chat_id == 2:
                raise RuntimeError("chat not found")
            return object()

    issues = await book.validate_on_startup(FakeBot())
    assert [i.chat_id for i in issues] == [2]
    assert book.for_chat(2).broken is True
    assert book.for_chat(1).broken is False
