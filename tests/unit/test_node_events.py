"""bot/node_events.py: node_joined/update_finished → системное уведомление."""

from sa_home_bot.bot.node_events import (
    build_node_event_handler,
    render_node_joined,
    render_update_finished,
)
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


# --- update_finished: самообновление ноды (без рестарта — только диагностика) ---


def test_render_update_finished_success_mentions_restart_node():
    text = render_update_finished("arch-t480", True, "0.22.0", None)
    assert "arch-t480" in text and "0.22.0" in text
    assert "restart_node" in text


def test_render_update_finished_failure_shows_error():
    text = render_update_finished("arch-t480", False, None, "network unreachable")
    assert "arch-t480" in text
    assert "network unreachable" in text
    assert "не удалось" in text


async def test_handler_broadcasts_on_update_finished_success():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier)

    # Событие описывает саму себя: src — та нода, что обновилась (в отличие
    # от node_joined, где src — сосед, принявший присоединение).
    env = make_event(
        "update_finished",
        {"ok": True, "version": "0.22.0", "error": None},
        src=Address(node="arch-t480", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_update_finished("arch-t480", True, "0.22.0", None))]


async def test_handler_broadcasts_on_update_finished_failure():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier)

    env = make_event(
        "update_finished",
        {"ok": False, "version": None, "error": "boom"},
        src=Address(node="alfred", service="node"),
    )
    await handler(env)

    assert notifier.sent == [(1, render_update_finished("alfred", False, None, "boom"))]


async def test_handler_ignores_update_finished_without_src_node():
    book = _book()
    notifier = FakeNotifier()
    handler = build_node_event_handler(book, notifier)

    env = make_event("update_finished", {"ok": True, "version": "0.22.0"}, src=None)
    await handler(env)

    assert notifier.sent == []
