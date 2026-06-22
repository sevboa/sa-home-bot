from sentinel_bot.bot.lifecycle import (
    broadcast_system,
    render_link_restored,
    render_shutdown,
    render_startup,
)
from sentinel_bot.config import SubscriptionConfig
from sentinel_bot.subscriptions.book import SubscriptionBook


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text))
        return 1


def test_render_startup_clean_vs_crash():
    assert "снова на посту" in render_startup(clean=True)
    assert "сбоя" in render_startup(clean=False)


def test_render_shutdown_and_link_restored():
    assert "офлайн" in render_shutdown()
    assert "восстановлена" in render_link_restored(125)


async def test_broadcast_system_only_to_accepting():
    book = SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="all", chat_id=1, event_types=["*"]),
            SubscriptionConfig(name="sys", chat_id=2, event_types=["system"]),
            SubscriptionConfig(name="heat_only", chat_id=3, event_types=["overheat_started"]),
        ]
    )
    notifier = FakeNotifier()
    sent = await broadcast_system(book, notifier, "test")
    assert sent == 2
    assert {chat for chat, _ in notifier.sent} == {1, 2}


async def test_broadcast_skips_broken():
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="x", chat_id=1, event_types=["*"])]
    )

    class FailBot:
        async def get_chat(self, chat_id):
            raise RuntimeError("down")

    await book.validate_on_startup(FailBot())
    notifier = FakeNotifier()
    assert await broadcast_system(book, notifier, "t") == 0
