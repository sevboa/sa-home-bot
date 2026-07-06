from sa_home_bot.bot.lifecycle import (
    broadcast_system,
    render_link_restored,
    render_shutdown,
    render_startup,
)
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        self.sent.append((chat_id, text))
        return 1


def test_render_startup_clean_vs_crash():
    assert "снова на посту" in render_startup(clean=True)
    assert "сбоя" in render_startup(clean=False)


def _outage(kind):
    from datetime import UTC, datetime

    from sa_home_bot.domain.models import POWER_UNEXPECTED, PowerEvent

    return PowerEvent(
        kind=kind,
        boot_at=datetime(2026, 7, 4, 12, 15, tzinfo=UTC),
        down_at=datetime(2026, 7, 4, 15, 12, tzinfo=UTC),
        up_at=datetime(2026, 7, 5, 0, 23, tzinfo=UTC),
        down_approx=(kind == POWER_UNEXPECTED),
    )


def test_render_startup_appends_power_loss_details():
    from sa_home_bot.domain.models import POWER_UNEXPECTED

    text = render_startup(clean=False, last_outage=_outage(POWER_UNEXPECTED))
    assert "потеря питания" in text
    assert "Последнее отключение" in text
    assert "9h 11m" in text  # длительность простоя приложена (d/h/m/s)


def test_render_startup_ignores_clean_last_outage():
    from sa_home_bot.domain.models import POWER_CLEAN

    # Если последнее отключение штатное — деталей о питании не показываем.
    text = render_startup(clean=False, last_outage=_outage(POWER_CLEAN))
    assert "Последнее отключение" not in text


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
