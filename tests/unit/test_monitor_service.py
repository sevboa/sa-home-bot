"""MonitorService: describe, get_state, scan_now (без реального железа)."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest_asyncio

from sa_home_bot.config import Settings, TelegramConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.domain.health import compute_health_diff
from sa_home_bot.monitor.service import ACTION_SCAN_NOW, MonitorService
from sa_home_bot.worker.queue import DedupQueue

from .conftest import cpu_policy, make_reading


@pytest_asyncio.fixture
async def service(tmp_path):
    db = Database(tmp_path / "monitor.sqlite")
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    queue = DedupQueue()
    settings = Settings(telegram=TelegramConfig(token="x"), subscriptions=[])
    yield MonitorService(settings, store, queue), store, queue
    await db.close()


def test_describe_declares_scan_now(service):
    svc, _, _ = service
    desc = svc.describe()
    assert desc.info.service == "monitor"
    assert "temperature" in desc.capabilities
    assert desc.find_action(ACTION_SCAN_NOW) is not None


async def test_get_state_returns_health_from_store(service):
    svc, store, _ = service
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    diff = compute_health_diff([make_reading(42.0)], {}, lambda r: cpu_policy(), now)
    await store.apply_diff(diff, now)

    # Железо не трогаем: блокирующие читатели подменяются.
    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_disk_summaries_sync", return_value=[]),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()

    assert state["service"] == "monitor"
    assert state["health"][0]["component_id"] == "cpu:pkg"
    assert state["health"][0]["status"] == "ok"
    assert state["health"][0]["temperature_c"] == 42.0
    assert state["last_outage"] is None
    assert state["thresholds"]["cpu"]["warn_c"] == 80.0


async def test_scan_now_queues_both_jobs_once(service):
    svc, _, queue = service
    first = await svc.run_command(ACTION_SCAN_NOW, {})
    assert first == {"sensor_queued": True, "smart_queued": True}
    # Повтор при непустой очереди дедуплицируется.
    second = await svc.run_command(ACTION_SCAN_NOW, {})
    assert second == {"sensor_queued": False, "smart_queued": False}
    assert queue.qsize() == 2
