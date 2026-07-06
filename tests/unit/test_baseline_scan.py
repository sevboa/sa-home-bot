"""SensorScanJob в режиме mode="baseline": аномалия ловится раньше fixed-порога."""

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
                warn_c=80.0,  # fixed никогда не сработает на скачке до 60°C
                crit_c=90.0,
                hysteresis_delta_c=5.0,
                consecutive_to_alert=2,
                consecutive_to_clear=2,
                mode="baseline",
                baseline_window=200,
                baseline_min_samples=5,
                baseline_k_sigma=4.0,
                baseline_min_std_c=3.0,
            )
        ),
        subscriptions=[],
    )


@pytest_asyncio.fixture
async def ctx(tmp_path):
    db = Database(tmp_path / "baseline.sqlite")
    await db.open()
    await apply_migrations(db)
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, event_types=["*"], allowed_commands=[])]
    )
    notifier = FakeNotifier()
    # 60 ровных тиков ~40°C (норма) + устойчивый скачок до 60°C.
    sensors = FakeSensors([40.0] * 60 + [60.0] * 4)
    store = Store(db)
    context = JobContext(
        store=store,
        sensors=sensors,
        dispatcher=TelegramEventDispatcher(notifier, book, store),
        config=_settings(),
    )
    yield context, notifier
    await db.close()


async def test_baseline_catches_anomaly_below_fixed_warn(ctx):
    context, notifier = ctx
    job = SensorScanJob()

    results = [await job.run(context) for _ in range(64)]
    alerts = sum(r.alerts_sent for r in results)

    # Ровно один алерт — на онсете аномалии (60°C >> baseline ~40°C),
    # хотя fixed warn=80 не был бы превышен ни разу.
    assert alerts == 1
    assert any("Перегрев" in s[1] for s in notifier.sent)
    # История показаний писалась (baseline включён).
    assert (await context.store.baseline_stats("cpu:pkg", window=200)).count == 64


async def test_no_alert_while_warming_up_on_flat_temperature(ctx):
    context, notifier = ctx
    job = SensorScanJob()
    # Первые 60 тиков — ровные 40°C, ни одного алерта.
    for _ in range(60):
        await job.run(context)
    assert notifier.sent == []
