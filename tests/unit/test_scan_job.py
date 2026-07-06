"""Интеграция SensorScanJob: норма → перегрев → норма через реальную БД."""

import pytest_asyncio

from sa_home_bot.bot.dispatch import TelegramEventDispatcher
from sa_home_bot.config import (
    CpuSensorConfig,
    SensorsConfig,
    Settings,
    SubscriptionConfig,
    TelegramConfig,
)
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.jobs.base import JobContext
from sa_home_bot.jobs.scan import SensorScanJob
from sa_home_bot.subscriptions.book import SubscriptionBook

from .conftest import make_reading


class FakeSensors:
    def __init__(self, temps: list[float]) -> None:
        self._temps = temps
        self._i = 0

    async def read_all(self):
        temp = self._temps[min(self._i, len(self._temps) - 1)]
        self._i += 1
        return [make_reading(temp)]


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self._id = 100

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        self._id += 1
        self.sent.append((chat_id, text, reply_to_message_id))
        return self._id


def _settings() -> Settings:
    return Settings(
        telegram=TelegramConfig(token="x"),
        sensors=SensorsConfig(
            cpu=CpuSensorConfig(
                warn_c=80.0,
                crit_c=90.0,
                hysteresis_delta_c=5.0,
                consecutive_to_alert=2,
                consecutive_to_clear=2,
            )
        ),
        subscriptions=[],
    )


@pytest_asyncio.fixture
async def ctx(tmp_path):
    db = Database(tmp_path / "scan.sqlite")
    await db.open()
    await apply_migrations(db)
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, event_types=["*"], allowed_commands=[])]
    )
    notifier = FakeNotifier()
    sensors = FakeSensors([40, 95, 95, 95, 40, 40])
    store = Store(db)
    context = JobContext(
        store=store,
        sensors=sensors,
        dispatcher=TelegramEventDispatcher(notifier, book, store),
        config=_settings(),
    )
    yield context, notifier
    await db.close()


async def test_overheat_then_clear_full_cycle(ctx):
    context, notifier = ctx
    job = SensorScanJob()

    results = [await job.run(context) for _ in range(6)]

    alerts = sum(r.alerts_sent for r in results)
    clears = sum(r.clears_sent for r in results)
    assert alerts == 1
    assert clears == 1

    # Ровно два сообщения: перегрев, затем остыл reply'ем на перегрев.
    assert len(notifier.sent) == 2
    alert_msg, clear_msg = notifier.sent
    assert alert_msg[2] is None
    assert "Перегрев" in alert_msg[1]
    assert "остыл" in clear_msg[1]
    # reply_to_message_id остывшего = message_id перегрева (101).
    assert clear_msg[2] == 101

    # Итоговое состояние — ok, ничего не висит pending.
    final = await context.store.get_all_states()
    assert final[0].status == "ok"
    assert await context.store.pending_alerts() == []
    assert await context.store.pending_clears() == []


async def test_no_duplicate_alert_while_still_hot(ctx):
    context, notifier = ctx
    job = SensorScanJob()
    # Первые 4 тика: 40,95,95,95 — alert на 3-м, на 4-м дубля быть не должно.
    for _ in range(4):
        await job.run(context)
    alert_sends = [s for s in notifier.sent if "Перегрев" in s[1]]
    assert len(alert_sends) == 1
