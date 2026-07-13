"""bot/node_events.py: node_joined → системное уведомление подписчикам."""

from sa_home_bot.bot.node_events import build_node_event_handler, render_node_joined
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text))
        return 1


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [
            SubscriptionConfig(name="all", chat_id=1, event_types=["*"]),
            SubscriptionConfig(name="heat_only", chat_id=2, event_types=["overheat_started"]),
        ]
    )


def test_render_node_joined_mentions_id_and_endpoint():
    text = render_node_joined("arch-t480", "tcp://100.110.58.31:8710")
    assert "arch-t480" in text
    assert "tcp://100.110.58.31:8710" in text


async def test_handler_broadcasts_system_on_node_joined():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier)

    env = make_event(
        "node_joined",
        {"node_id": "arch-t480", "endpoint": "tcp://100.110.58.31:8710"},
        src=Address(node="alfred", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_node_joined("arch-t480", "tcp://100.110.58.31:8710"))]


async def test_handler_ignores_other_event_types():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier)

    env = make_event("service_started", {"name": "monitor"}, src=Address(node="alfred"))
    await handler(env)

    assert notifier.sent == []


async def test_handler_ignores_event_without_node_id():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier)

    env = make_event("node_joined", {"endpoint": "tcp://x:1"}, src=Address(node="alfred"))
    await handler(env)

    assert notifier.sent == []
