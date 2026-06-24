import pytest
import pytest_asyncio

from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import NOTIF_ALERT, Store
from sa_home_bot.domain.models import (
    ALERTING,
    OK,
    HealthDiff,
    HealthState,
    Transition,
)

from .conftest import BASE_TIME


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


def _state(status, temp, since=None, count=0):
    return HealthState("cpu:pkg", "cpu", "Package", status, temp, count, since)


async def test_open_migrate_smoke(store):
    assert await store.get_known_states() == {}
    assert await store.get_all_states() == []


async def test_app_state_roundtrip(store):
    assert await store.get_state("x") is None
    await store.set_state("x", "1")
    assert await store.get_state("x") == "1"
    await store.set_state("x", "2")
    assert await store.get_state("x") == "2"


async def test_apply_diff_insert_then_alert_pending(store):
    # Сначала обычное ok-состояние.
    await store.apply_diff(HealthDiff([_state(OK, 40.0)], []), BASE_TIME)
    known = await store.get_known_states()
    assert known["cpu:pkg"].status == OK

    # Переход в alerting.
    tr = Transition("cpu:pkg", "cpu", "Package", OK, ALERTING, 95.0, BASE_TIME)
    await store.apply_diff(HealthDiff([_state(ALERTING, 95.0, BASE_TIME)], [tr]), BASE_TIME)

    pending = await store.pending_alerts()
    assert [p.component_id for p in pending] == ["cpu:pkg"]

    # После пометки доставленным — не pending (идемпотентность).
    await store.mark_alert_notified("cpu:pkg", BASE_TIME)
    assert await store.pending_alerts() == []


async def test_cleared_pending_only_after_alert_notified(store):
    tr_alert = Transition("cpu:pkg", "cpu", "Package", OK, ALERTING, 95.0, BASE_TIME)
    await store.apply_diff(HealthDiff([_state(ALERTING, 95.0, BASE_TIME)], [tr_alert]), BASE_TIME)
    await store.mark_alert_notified("cpu:pkg", BASE_TIME)

    tr_clear = Transition("cpu:pkg", "cpu", "Package", ALERTING, OK, 40.0, BASE_TIME)
    await store.apply_diff(HealthDiff([_state(OK, 40.0)], [tr_clear]), BASE_TIME)

    pending = await store.pending_clears()
    assert [p.component_id for p in pending] == ["cpu:pkg"]
    await store.mark_cleared_notified("cpu:pkg", BASE_TIME)
    assert await store.pending_clears() == []


async def test_new_alert_cycle_resets_flags(store):
    # Полный цикл: alert -> notified -> clear -> notified.
    tr_alert = Transition("cpu:pkg", "cpu", "Package", OK, ALERTING, 95.0, BASE_TIME)
    await store.apply_diff(HealthDiff([_state(ALERTING, 95.0, BASE_TIME)], [tr_alert]), BASE_TIME)
    await store.mark_alert_notified("cpu:pkg", BASE_TIME)
    tr_clear = Transition("cpu:pkg", "cpu", "Package", ALERTING, OK, 40.0, BASE_TIME)
    await store.apply_diff(HealthDiff([_state(OK, 40.0)], [tr_clear]), BASE_TIME)
    await store.mark_cleared_notified("cpu:pkg", BASE_TIME)
    assert await store.pending_alerts() == []
    assert await store.pending_clears() == []

    # Новый перегрев — флаги сброшены, снова pending alert.
    tr_alert2 = Transition("cpu:pkg", "cpu", "Package", OK, ALERTING, 96.0, BASE_TIME)
    await store.apply_diff(HealthDiff([_state(ALERTING, 96.0, BASE_TIME)], [tr_alert2]), BASE_TIME)
    assert [p.component_id for p in await store.pending_alerts()] == ["cpu:pkg"]


async def test_notification_message_id_roundtrip(store):
    await store.apply_diff(HealthDiff([_state(ALERTING, 95.0, BASE_TIME)], []), BASE_TIME)
    await store.record_notification("cpu:pkg", 555, NOTIF_ALERT, 42, BASE_TIME)
    assert await store.get_alert_message_id("cpu:pkg", 555) == 42
    assert await store.get_alert_message_id("cpu:pkg", 999) is None


async def test_job_runs_lifecycle_and_counts(store):
    rid = await store.start_job_run("sensor_scan", BASE_TIME)
    await store.finish_job_run(rid, "ok", BASE_TIME, metrics_json="{}")
    rid2 = await store.start_job_run("sensor_scan", BASE_TIME)
    await store.finish_job_run(rid2, "error", BASE_TIME, error="boom")
    counts = await store.job_run_counts()
    assert counts.get("ok") == 1
    assert counts.get("error") == 1
    runs = await store.recent_job_runs(limit=10)
    assert len(runs) == 2


@pytest.mark.parametrize("keep", [1, 5])
async def test_prune_job_runs(store, keep):
    for _ in range(10):
        rid = await store.start_job_run("x", BASE_TIME)
        await store.finish_job_run(rid, "ok", BASE_TIME)
    await store.prune_job_runs(keep_last=keep)
    assert len(await store.recent_job_runs(limit=100)) == keep
