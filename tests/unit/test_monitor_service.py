"""MonitorService: describe, get_state, scan_now (без реального железа)."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import pytest_asyncio

from sa_home_bot.config import DiskSensorConfig, SensorsConfig, Settings, TelegramConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.domain.health import compute_health_diff
from sa_home_bot.monitor.service import ACTION_DOWNTIME, ACTION_SCAN_NOW, MonitorService
from sa_home_bot.utils.requirements import requirements_registry
from sa_home_bot.worker.queue import DedupQueue

from .conftest import cpu_policy, make_reading


@pytest.fixture(autouse=True)
def _clean_requirements_registry():
    # Синглтон живёт дольше одного теста — изолируем реальные NEEDS_PRIVILEGE
    # диагнозы между тестами (сама статика per-запусковая, не тухнет).
    requirements_registry.reset()
    yield
    requirements_registry.reset()


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


def test_describe_declares_scan_now_and_downtime(service):
    svc, _, _ = service
    desc = svc.describe()
    assert desc.info.service == "monitor"
    assert "temperature" in desc.capabilities
    assert desc.find_action(ACTION_SCAN_NOW) is not None
    downtime = desc.find_action(ACTION_DOWNTIME)
    assert downtime is not None
    # Параметры необязательные — сервер не потребует их в command; фронтенды
    # не рисуют кнопку (есть params), а зовут действие сами.
    assert {p.name for p in downtime.params} == {"offset", "limit"}
    assert all(not p.required for p in downtime.params)


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


async def test_get_state_flags_missing_smartctl_when_disks_enabled(service, monkeypatch):
    svc, _, _ = service
    # smartctl не найден, остальные утилиты (в т.ч. journalctl) — как обычно.
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "smartctl" else f"/usr/bin/{name}"
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_disk_summaries_sync", return_value=[]),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()

    assert len(state["requirements"]) == 1
    assert state["requirements"][0]["id"] == "smartctl"
    assert state["requirements"][0]["status"] == "missing_program"
    assert "smartmontools" in state["requirements"][0]["hint"]


async def test_get_state_quiet_when_disks_disabled(tmp_path, monkeypatch):
    db = Database(tmp_path / "monitor.sqlite")
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    settings = Settings(
        telegram=TelegramConfig(token="x"),
        sensors=SensorsConfig(disks=DiskSensorConfig(enabled=False)),
        subscriptions=[],
    )
    svc = MonitorService(settings, store, DedupQueue())
    # smartctl всё равно не найден, но диски выключены — не должен попасть в вывод.
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "smartctl" else f"/usr/bin/{name}"
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_disk_summaries_sync", return_value=[]),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()
    await db.close()

    assert state["requirements"] == []  # диски выключены — не шумим


async def test_scan_now_queues_both_jobs_once(service):
    svc, _, queue = service
    first = await svc.run_command(ACTION_SCAN_NOW, {})
    assert first == {"sensor_queued": True, "smart_queued": True}
    # Повтор при непустой очереди дедуплицируется.
    second = await svc.run_command(ACTION_SCAN_NOW, {})
    assert second == {"sensor_queued": False, "smart_queued": False}
    assert queue.qsize() == 2


# --- downtime: история отключений по протоколу ---


def _fake_events(n: int):
    from sa_home_bot.domain.models import POWER_CLEAN, PowerEvent

    return [
        PowerEvent(
            kind=POWER_CLEAN,
            boot_at=datetime(2026, 7, 1 + i, 12, 0, tzinfo=UTC),
            down_at=datetime(2026, 7, 1 + i, 11, 0, tzinfo=UTC),
            up_at=None,
        )
        for i in range(n)
    ]


async def test_downtime_returns_serialized_page(service):
    svc, _, _ = service
    calls = []

    def fake_read(offset, limit):
        calls.append((offset, limit))
        return _fake_events(2), True

    with patch("sa_home_bot.monitor.service.read_power_events_sync", fake_read):
        result = await svc.run_command(ACTION_DOWNTIME, {"offset": 10, "limit": 2})

    assert calls == [(10, 2)]
    assert result["offset"] == 10
    assert result["has_next"] is True
    assert len(result["events"]) == 2
    # Формат события — тот же _outage_dict, что у last_outage в get_state.
    assert result["events"][0]["kind"] == "clean"
    assert "boot_at" in result["events"][0]


async def test_downtime_clamps_bad_args(service):
    svc, _, _ = service
    calls = []

    def fake_read(offset, limit):
        calls.append((offset, limit))
        return [], False

    with patch("sa_home_bot.monitor.service.read_power_events_sync", fake_read):
        await svc.run_command(ACTION_DOWNTIME, {"offset": -5, "limit": 9999})
        await svc.run_command(ACTION_DOWNTIME, {"offset": "мусор", "limit": None})

    assert calls[0] == (0, 50)  # отрицательный offset → 0, limit → максимум
    assert calls[1] == (0, 10)  # непарсибельные значения → дефолты
