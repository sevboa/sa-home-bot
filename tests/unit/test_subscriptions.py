from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription


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
    # Полная форма `действие@служба`.
    assert sub.allows_action("restart", "node")
    assert not sub.allows_action("restart", "monitor")  # другая служба
    assert not sub.allows_action("stop", "node")  # другое действие
    # Голое имя действия — совместимость со старыми конфигами: любая служба.
    assert sub.allows_action("scan_now", "monitor")


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
        async def get_chat(self, chat_id):
            if chat_id == 2:
                raise RuntimeError("chat not found")
            return object()

    issues = await book.validate_on_startup(FakeBot())
    assert [i.chat_id for i in issues] == [2]
    assert book.for_chat(2).broken is True
    assert book.for_chat(1).broken is False
