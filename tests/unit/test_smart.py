"""Мониторинг SMART-деградации: дельта снимков, рендер, парсинг, интеграция job."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_asyncio

from sa_home_bot.bot.dispatch import TelegramEventDispatcher
from sa_home_bot.config import (
    DiskSensorConfig,
    SensorsConfig,
    Settings,
    SubscriptionConfig,
    TelegramConfig,
)
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import (
    DISK_FAIL,
    DISK_OK,
    DISK_WARN,
    EVENT_SMART_DEGRADED,
    EVENT_SMART_RECOVERED,
    SmartSnapshot,
)
from sa_home_bot.domain.render import render_smart_change
from sa_home_bot.domain.smart import diff_smart
from sa_home_bot.jobs.base import JobContext
from sa_home_bot.jobs.smart import SmartScanJob
from sa_home_bot.sensors.disks import parse_smart_snapshot
from sa_home_bot.subscriptions.book import SubscriptionBook

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def _snap(attrs: dict[int, int], health: str | None = DISK_OK, **kw) -> SmartSnapshot:
    return SmartSnapshot(
        component_id=kw.get("component_id", "disk:/dev/sdb"),
        label=kw.get("label", "ST9250315AS"),
        health=health,
        attrs=attrs,
        taken_at=kw.get("taken_at", NOW),
    )


# --- diff_smart ---


def test_first_observation_no_event():
    assert diff_smart(None, _snap({5: 31})) is None


def test_no_change_no_event():
    prev = _snap({5: 31, 197: 0})
    assert diff_smart(prev, _snap({5: 31, 197: 0})) is None


def test_counter_growth_is_degraded():
    prev = _snap({5: 174, 197: 0})
    change = diff_smart(prev, _snap({5: 177, 197: 4}))
    assert change is not None
    assert change.event_type == EVENT_SMART_DEGRADED
    changed = {c.attr_id: (c.old, c.new) for c in change.attr_changes}
    assert changed == {5: (174, 177), 197: (0, 4)}


def test_counter_decrease_is_recovered():
    # Pending обнулился после перезаписи — восстановление.
    prev = _snap({197: 28})
    change = diff_smart(prev, _snap({197: 0}))
    assert change is not None
    assert change.event_type == EVENT_SMART_RECOVERED


def test_degradation_dominates_mixed_changes():
    # Один счётчик вырос, другой упал — деградация приоритетнее.
    prev = _snap({5: 174, 197: 28})
    change = diff_smart(prev, _snap({5: 177, 197: 0}))
    assert change.event_type == EVENT_SMART_DEGRADED


def test_health_class_worsening_alone_is_degraded():
    prev = _snap({}, health=DISK_OK)
    change = diff_smart(prev, _snap({}, health=DISK_FAIL))
    assert change is not None
    assert change.event_type == EVENT_SMART_DEGRADED
    assert change.health_from == DISK_OK and change.health_to == DISK_FAIL


def test_health_becoming_available_is_not_a_change():
    # None -> ok: SMART просто стал доступен, тревожить не о чем.
    prev = _snap({}, health=None)
    assert diff_smart(prev, _snap({}, health=DISK_OK)) is None


def test_unmonitored_attr_ignored():
    # id 9 (Power_On_Hours) не отслеживается — рост не даёт события.
    prev = _snap({9: 24000})
    assert diff_smart(prev, _snap({9: 24001})) is None


# --- parse_smart_snapshot / _raw_attrs ---


def _attr(aid, val, string=None):
    return {"id": aid, "raw": {"value": val, "string": string if string is not None else str(val)}}


def _smartctl(passed=True, table=None):
    return {
        "model_name": "ST9250315AS",
        "smart_status": {"passed": passed},
        "ata_smart_attributes": {"table": table or []},
    }


def test_parse_snapshot_extracts_monitored_attrs_only():
    data = _smartctl(table=[_attr(5, 31), _attr(9, 24000), _attr(197, 2)])  # 9 не отслеживается
    snap = parse_smart_snapshot("disk:/dev/sda", data, NOW)
    assert snap.attrs == {5: 31, 197: 2}
    assert snap.health == DISK_WARN  # pending=2
    assert snap.label == "ST9250315AS"


def test_parse_snapshot_uses_leading_token_of_raw_string():
    # Hitachi: raw.value=589855 упаковано, но реальный счётчик = 31 из '31 (0 9)'.
    data = _smartctl(table=[_attr(5, 589855, string="31 (0 9)")])
    snap = parse_smart_snapshot("disk:/dev/sdb", data, NOW)
    assert snap.attrs == {5: 31}


def test_parse_snapshot_failed_health():
    snap = parse_smart_snapshot("disk:/dev/sdb", _smartctl(passed=False), NOW)
    assert snap.health == DISK_FAIL


# --- render_smart_change ---


def test_render_degraded_lists_attrs_and_health():
    change = diff_smart(
        _snap({5: 174, 197: 0}, health=DISK_OK),
        _snap({5: 177, 197: 4}, health=DISK_WARN),
    )
    text = render_smart_change(change)
    assert "ухудшение" in text
    assert "Reallocated_Sector_Ct: 174 → <b>177</b>" in text
    assert "Current_Pending_Sector: 0 → <b>4</b>" in text
    assert "предупреждение" in text


def test_render_recovered():
    change = diff_smart(_snap({197: 28}), _snap({197: 0}))
    text = render_smart_change(change)
    assert "улучшение" in text
    assert "🔻" in text


# --- SmartScanJob (реальная БД) ---


class FakeSmartSensors:
    def __init__(self, sequence: list[list[SmartSnapshot]]) -> None:
        self._seq = sequence
        self._i = 0

    async def read_smart_snapshots(self):
        snaps = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return snaps


class FakeNotifier:
    def __init__(self, fail=False) -> None:
        self.sent: list[tuple[int, str]] = []
        self._id = 200
        self._fail = fail

    async def send_direct(self, chat_id, text, reply_to_message_id=None):
        if self._fail:
            return None
        self._id += 1
        self.sent.append((chat_id, text))
        return self._id


def _settings() -> Settings:
    return Settings(
        telegram=TelegramConfig(token="x"),
        sensors=SensorsConfig(disks=DiskSensorConfig(devices=["/dev/sdb:sat"])),
        subscriptions=[],
    )


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "smart.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _ctx(store, sensors, notifier) -> JobContext:
    book = SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=1, event_types=["*"], allowed_commands=[])]
    )
    return JobContext(
        store=store,
        sensors=sensors,
        dispatcher=TelegramEventDispatcher(notifier, book, store),
        config=_settings(),
    )


async def test_smart_health_map_keys_by_realpath(store):
    await store.save_smart_snapshot(_snap({197: 1}, health=DISK_WARN, component_id="disk:/dev/sda"))
    await store.save_smart_snapshot(_snap({197: 0}, health=DISK_OK, component_id="disk:/dev/sdb"))
    m = await store.get_smart_health_map()
    assert m == {"/dev/sda": DISK_WARN, "/dev/sdb": DISK_OK}


async def test_job_first_run_records_baseline_no_alert(store):
    notifier = FakeNotifier()
    sensors = FakeSmartSensors([[_snap({5: 174, 197: 0})]])
    ctx = _ctx(store, sensors, notifier)

    result = await SmartScanJob().run(ctx)

    assert result.alerts_sent == 0
    assert notifier.sent == []
    saved = await store.get_smart_snapshot("disk:/dev/sdb")
    assert saved.attrs == {5: 174, 197: 0}


async def test_job_alerts_on_degradation(store):
    notifier = FakeNotifier()
    sensors = FakeSmartSensors(
        [[_snap({5: 174, 197: 0})], [_snap({5: 174, 197: 3})]]
    )
    ctx = _ctx(store, sensors, notifier)

    await SmartScanJob().run(ctx)  # baseline
    result = await SmartScanJob().run(ctx)  # деградация

    assert result.alerts_sent == 1
    assert len(notifier.sent) == 1
    assert "ухудшение" in notifier.sent[0][1]
    # baseline сдвинулся на текущий снимок.
    assert (await store.get_smart_snapshot("disk:/dev/sdb")).attrs == {5: 174, 197: 3}


async def test_job_keeps_baseline_when_delivery_fails(store):
    notifier = FakeNotifier(fail=True)
    sensors = FakeSmartSensors(
        [[_snap({197: 0})], [_snap({197: 5})], [_snap({197: 5})]]
    )
    ctx = _ctx(store, sensors, notifier)

    await SmartScanJob().run(ctx)  # baseline 0
    await SmartScanJob().run(ctx)  # деградация, доставка провалилась → baseline НЕ сдвигаем
    # baseline остался прежним (0), а не 5.
    assert (await store.get_smart_snapshot("disk:/dev/sdb")).attrs == {197: 0}

    # Бот вернулся — повтор доставляет и сдвигает baseline.
    notifier._fail = False
    result = await SmartScanJob().run(ctx)
    assert result.alerts_sent == 1
    assert (await store.get_smart_snapshot("disk:/dev/sdb")).attrs == {197: 5}
