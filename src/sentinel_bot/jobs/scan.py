"""SensorScanJob — снять срез, reconcile, разослать pending-уведомления.

Инварианты (ARCHITECTURE §4.2, §9):
- состояние коммитится одной транзакцией (apply_diff);
- отправка — отдельный шаг; notified_* выставляется только после успеха,
  поэтому падение между записью и отправкой не теряет и не дублирует.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sentinel_bot.db.store import NOTIF_ALERT, NOTIF_CLEARED
from sentinel_bot.domain.health import compute_health_diff
from sentinel_bot.domain.models import (
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    KIND_CPU,
    Event,
    HealthState,
    SensorReading,
)
from sentinel_bot.domain.policy import ComponentPolicy, FixedThresholdPolicy
from sentinel_bot.domain.render import render_event
from sentinel_bot.jobs.base import JobContext, JobResult

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

    def _build_resolver(self, config):
        cpu_cfg = config.sensors.cpu
        disk_cfg = config.sensors.disks
        cpu_policy = ComponentPolicy(
            policy=FixedThresholdPolicy(cpu_cfg.warn_c, cpu_cfg.crit_c, cpu_cfg.hysteresis_delta_c),
            consecutive_to_alert=cpu_cfg.consecutive_to_alert,
            consecutive_to_clear=cpu_cfg.consecutive_to_clear,
        )
        disk_policy = ComponentPolicy(
            policy=FixedThresholdPolicy(
                disk_cfg.warn_c, disk_cfg.crit_c, disk_cfg.hysteresis_delta_c
            ),
            consecutive_to_alert=disk_cfg.consecutive_to_alert,
            consecutive_to_clear=disk_cfg.consecutive_to_clear,
        )

        def resolve(reading: SensorReading) -> ComponentPolicy:
            return cpu_policy if reading.kind == KIND_CPU else disk_policy

        return resolve

    async def run(self, ctx: JobContext) -> JobResult:
        now = _now()
        readings = await ctx.sensors.read_all()
        resolver = self._build_resolver(ctx.config)
        known = await ctx.store.get_known_states()

        diff = compute_health_diff(readings, known, resolver, now)
        await ctx.store.apply_diff(diff, now)

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
