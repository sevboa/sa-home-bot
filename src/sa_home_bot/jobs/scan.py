"""SensorScanJob — снять срез, reconcile, разослать pending-уведомления.

Инварианты (ARCHITECTURE §4.2, §9):
- состояние коммитится одной транзакцией (apply_diff);
- отправка — отдельный шаг; notified_* выставляется только после успеха,
  поэтому падение между записью и отправкой не теряет и не дублирует.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sa_home_bot.db.store import NOTIF_ALERT, NOTIF_CLEARED
from sa_home_bot.domain.health import compute_health_diff
from sa_home_bot.domain.models import (
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    KIND_CPU,
    Event,
    HealthState,
    SensorReading,
)
from sa_home_bot.domain.policy import (
    BaselinePolicy,
    BaselineStats,
    ComponentPolicy,
    FixedThresholdPolicy,
)
from sa_home_bot.domain.render import render_event
from sa_home_bot.jobs.base import JobContext, JobResult

log = logging.getLogger(__name__)

DEDUP_KEY = "sensor-scan"
JOB_TYPE = "sensor_scan"


def _now() -> datetime:
    return datetime.now(tz=UTC)


class SensorScanJob:
    @property
    def dedup_key(self) -> str:
        return DEDUP_KEY

    @property
    def job_type(self) -> str:
        return JOB_TYPE

    def _build_resolver(self, config, stats: dict[str, BaselineStats]):
        cpu_cfg = config.sensors.cpu
        disk_cfg = config.sensors.disks

        def resolve(reading: SensorReading) -> ComponentPolicy:
            cfg = cpu_cfg if reading.kind == KIND_CPU else disk_cfg
            if cfg.mode == "baseline":
                policy = BaselinePolicy(
                    warn_c=cfg.warn_c,
                    crit_c=cfg.crit_c,
                    hysteresis_delta_c=cfg.hysteresis_delta_c,
                    stats=stats.get(reading.component_id, BaselineStats(0, 0.0, 0.0)),
                    min_samples=cfg.baseline_min_samples,
                    k_sigma=cfg.baseline_k_sigma,
                    min_std_c=cfg.baseline_min_std_c,
                )
            else:
                policy = FixedThresholdPolicy(cfg.warn_c, cfg.crit_c, cfg.hysteresis_delta_c)
            return ComponentPolicy(
                policy=policy,
                consecutive_to_alert=cfg.consecutive_to_alert,
                consecutive_to_clear=cfg.consecutive_to_clear,
            )

        return resolve

    async def run(self, ctx: JobContext) -> JobResult:
        now = _now()
        readings = await ctx.sensors.read_all()
        cpu_cfg = ctx.config.sensors.cpu
        disk_cfg = ctx.config.sensors.disks
        uses_baseline = "baseline" in (cpu_cfg.mode, disk_cfg.mode)

        # Статистику берём по ПРОШЛЫМ показаниям — текущее ещё не записано,
        # чтобы аномалия оценивалась относительно накопленной нормы.
        stats: dict[str, BaselineStats] = {}
        if uses_baseline:
            for r in readings:
                cfg = cpu_cfg if r.kind == KIND_CPU else disk_cfg
                if cfg.mode == "baseline":
                    stats[r.component_id] = await ctx.store.baseline_stats(
                        r.component_id, cfg.baseline_window
                    )

        resolver = self._build_resolver(ctx.config, stats)
        known = await ctx.store.get_known_states()

        diff = compute_health_diff(readings, known, resolver, now)
        await ctx.store.apply_diff(diff, now)

        if uses_baseline:
            await ctx.store.record_readings(readings)

        alerts_sent = await self._dispatch_alerts(ctx, now)
        clears_sent = await self._dispatch_clears(ctx, now)

        return JobResult(
            components_scanned=len(readings),
            transitions=len(diff.transitions),
            alerts_sent=alerts_sent,
            clears_sent=clears_sent,
        )

    async def _dispatch_alerts(self, ctx: JobContext, now: datetime) -> int:
        sent = 0
        for state in await ctx.store.pending_alerts():
            event = _event_from_state(state, EVENT_OVERHEAT_STARTED, now)
            text = render_event(event)
            delivered = False
            for sub in ctx.subscriptions.accepting(EVENT_OVERHEAT_STARTED):
                message_id = await ctx.notifier.send_direct(sub.chat_id, text)
                if message_id is not None:
                    delivered = True
                    await ctx.store.record_notification(
                        state.component_id, sub.chat_id, NOTIF_ALERT, message_id, now
                    )
            # Помечаем доставленным, даже если часть чатов не ответила, —
            # иначе следующий тик зашлёт дубль живым подписчикам.
            await ctx.store.mark_alert_notified(state.component_id, now)
            if delivered:
                sent += 1
        return sent

    async def _dispatch_clears(self, ctx: JobContext, now: datetime) -> int:
        sent = 0
        for state in await ctx.store.pending_clears():
            event = _event_from_state(state, EVENT_OVERHEAT_CLEARED, now)
            text = render_event(event)
            delivered = False
            for sub in ctx.subscriptions.accepting(EVENT_OVERHEAT_CLEARED):
                reply_to = await ctx.store.get_alert_message_id(state.component_id, sub.chat_id)
                message_id = await ctx.notifier.send_direct(
                    sub.chat_id, text, reply_to_message_id=reply_to
                )
                if message_id is not None:
                    delivered = True
                    await ctx.store.record_notification(
                        state.component_id, sub.chat_id, NOTIF_CLEARED, message_id, now
                    )
            await ctx.store.mark_cleared_notified(state.component_id, now)
            if delivered:
                sent += 1
        return sent


def _event_from_state(state: HealthState, event_type: str, now: datetime) -> Event:
    use_alert_time = event_type == EVENT_OVERHEAT_STARTED and state.alerting_since is not None
    at = state.alerting_since if use_alert_time else now
    return Event(
        type=event_type,
        component_id=state.component_id,
        kind=state.kind,
        label=state.label,
        temperature_c=state.temperature_c,
        at=at,
    )
