"""MonitorService: describe, get_state, scan_now (без реального железа)."""

import sys
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import pytest_asyncio

from sa_home_bot.config import (
    DiskSensorConfig,
    GpuSensorConfig,
    HostSensorConfig,
    SensorsConfig,
    Settings,
    TelegramConfig,
)
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

    # Железо не трогаем: блокирующие читатели подменяются. Диски — кэш из
    # Store (см. SensorScanJob), тут просто пуст (SensorScanJob не запускался).
    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()

    assert state["service"] == "monitor"
    assert state["health"][0]["component_id"] == "cpu:pkg"
    assert state["health"][0]["status"] == "ok"
    assert state["health"][0]["temperature_c"] == 42.0
    assert state["last_outage"] is None
    assert state["thresholds"]["cpu"]["warn_c"] == 80.0
    assert state["thresholds"]["gpu"]["warn_c"] == 80.0  # дефолт GpuSensorConfig


async def test_get_state_flags_missing_smartctl_when_disks_enabled(service, monkeypatch):
    svc, _, _ = service
    # smartctl не найден, остальные утилиты (в т.ч. journalctl) — как обычно.
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "smartctl" else f"/usr/bin/{name}"
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
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
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()
    await db.close()

    assert state["requirements"] == []  # диски выключены — не шумим


@pytest.mark.skipif(sys.platform != "linux", reason="kernel-журнал только на Linux")
async def test_get_state_surfaces_kernel_journal_requirement_when_host_enabled(
    tmp_path, monkeypatch
):
    from sa_home_bot.sensors.host import KERNEL_JOURNAL_REQUIREMENT
    from sa_home_bot.utils.requirements import RequirementStatus

    db = Database(tmp_path / "monitor.sqlite")
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    settings = Settings(
        telegram=TelegramConfig(token="x"),
        sensors=SensorsConfig(
            host=HostSensorConfig(enabled=True), disks=DiskSensorConfig(enabled=False)
        ),
        subscriptions=[],
    )
    svc = MonitorService(settings, store, DedupQueue())
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    requirements_registry.report(
        KERNEL_JOURNAL_REQUIREMENT, RequirementStatus.NEEDS_PRIVILEGE
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()
    await db.close()

    kernel = [r for r in state["requirements"] if r["id"] == "journalctl-kernel"]
    assert len(kernel) == 1
    assert kernel[0]["status"] == "needs_privilege"
    assert "systemd-journal" in kernel[0]["hint"]


async def test_get_state_quiet_kernel_journal_when_host_disabled(tmp_path, monkeypatch):
    from sa_home_bot.sensors.host import KERNEL_JOURNAL_REQUIREMENT
    from sa_home_bot.utils.requirements import RequirementStatus

    db = Database(tmp_path / "monitor.sqlite")
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    settings = Settings(telegram=TelegramConfig(token="x"), subscriptions=[])
    svc = MonitorService(settings, store, DedupQueue())
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    requirements_registry.report(
        KERNEL_JOURNAL_REQUIREMENT, RequirementStatus.NEEDS_PRIVILEGE
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()
    await db.close()

    assert state["requirements"] == []  # host-датчик выключен — kernel-журнал никто не читает


async def test_get_state_quiet_when_gpu_disabled_by_default(tmp_path, monkeypatch):
    """gpu.enabled=False (дефолт) — отсутствие nvidia-smi не должно шуметь на
    нодах без видеокарты (все, кроме mycraft)."""
    db = Database(tmp_path / "monitor.sqlite")
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    settings = Settings(telegram=TelegramConfig(token="x"), subscriptions=[])
    svc = MonitorService(settings, store, DedupQueue())
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "nvidia-smi" else f"/usr/bin/{name}"
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()
    await db.close()

    assert state["requirements"] == []


async def test_get_state_flags_missing_nvidia_smi_when_gpu_enabled(tmp_path, monkeypatch):
    db = Database(tmp_path / "monitor.sqlite")
    await db.open()
    await apply_migrations(db)
    store = Store(db)
    settings = Settings(
        telegram=TelegramConfig(token="x"),
        sensors=SensorsConfig(gpu=GpuSensorConfig(enabled=True)),
        subscriptions=[],
    )
    svc = MonitorService(settings, store, DedupQueue())
    monkeypatch.setattr(
        "shutil.which", lambda name: None if name == "nvidia-smi" else f"/usr/bin/{name}"
    )

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()
    await db.close()

    assert len(state["requirements"]) == 1
    assert state["requirements"][0]["id"] == "nvidia-smi"
    assert state["requirements"][0]["status"] == "missing_program"


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


# --- host-метрики VPS (этап 38) ---


def test_describe_declares_host_capability_and_trend(service):
    from sa_home_bot.monitor.service import ACTION_HOST_TREND

    svc, _, _ = service
    desc = svc.describe()
    assert "host" in desc.capabilities
    trend = desc.find_action(ACTION_HOST_TREND)
    assert trend is not None
    assert {p.name for p in trend.params} == {"metric", "hours"}


async def test_get_state_carries_host_health_and_thresholds(service):
    from sa_home_bot.domain.host import HostHealthDiff, HostMetricState

    svc, store, _ = service
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    st = HostMetricState("host:steal_pct", "steal_pct", "CPU steal", 32.0, "%", "alerting", 0, now)
    await store.apply_host_diff(HostHealthDiff(states=[st], transitions=[]), now)

    with (
        patch("sa_home_bot.monitor.service.read_uptime_sync", return_value=None),
        patch("sa_home_bot.monitor.service.read_power_events_sync", return_value=([], False)),
    ):
        state = await svc.get_state()

    assert state["host_health"][0]["metric"] == "steal_pct"
    assert state["host_health"][0]["value"] == 32.0
    assert state["host_thresholds"]["steal_pct"]["warn"] == 10.0
    assert state["host_thresholds"]["mem_available_pct"]["direction"] == "below"


async def test_host_trend_action_rejects_unknown_metric(service):
    svc, _, _ = service
    from sa_home_bot.monitor.service import ACTION_HOST_TREND

    res = await svc.run_command(ACTION_HOST_TREND, {"metric": "nonsense"})
    assert res["error"] == "unknown_metric"

    ok = await svc.run_command(ACTION_HOST_TREND, {"metric": "steal_pct", "hours": 3})
    assert ok["metric"] == "steal_pct" and ok["hours"] == 3 and ok["buckets"] == []
