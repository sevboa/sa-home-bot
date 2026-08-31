"""SensorScanJob — снять срез, reconcile, разослать pending-уведомления.

Инварианты (ARCHITECTURE §4.2, §9):
- состояние коммитится одной транзакцией (apply_diff);
- отправка — отдельный шаг; notified_* выставляется только когда диспетчер
  принял событие (handled), поэтому падение между записью и отправкой не
  теряет и не дублирует.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sa_home_bot.domain.health import compute_health_diff
from sa_home_bot.domain.host import (
    EVENT_HOST_DEGRADED,
    EVENT_HOST_RECOVERED,
    METRICS,
    HostEvent,
    HostMetricPolicy,
    HostMetricReading,
    HostMetricState,
    compute_host_diff,
)
from sa_home_bot.domain.models import (
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    KIND_CPU,
    KIND_GPU,
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
from sa_home_bot.jobs.base import JobContext, JobResult

log = logging.getLogger(__name__)

DEDUP_KEY = "sensor-scan"
JOB_TYPE = "sensor_scan"


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _cfg_for_kind(config, kind: str):
    """Секция sensors.* по виду показания: cpu/gpu — свои, всё остальное (диски)
    — disk_cfg (дефолт, как и раньше до появления GPU)."""
    if kind == KIND_CPU:
        return config.sensors.cpu
    if kind == KIND_GPU:
        return config.sensors.gpu
    return config.sensors.disks


class SensorScanJob:
    @property
    def dedup_key(self) -> str:
        return DEDUP_KEY

    @property
    def job_type(self) -> str:
        return JOB_TYPE

    def _build_resolver(self, config, stats: dict[str, BaselineStats]):
        def resolve(reading: SensorReading) -> ComponentPolicy:
            cfg = _cfg_for_kind(config, reading.kind)
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
        sensors_cfg = ctx.config.sensors
        uses_baseline = "baseline" in (
            sensors_cfg.cpu.mode,
            sensors_cfg.gpu.mode,
            sensors_cfg.disks.mode,
        )

        # Статистику берём по ПРОШЛЫМ показаниям — текущее ещё не записано,
        # чтобы аномалия оценивалась относительно накопленной нормы.
        stats: dict[str, BaselineStats] = {}
        if uses_baseline:
            for r in readings:
                cfg = _cfg_for_kind(ctx.config, r.kind)
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

        host_scanned, host_transitions, host_alerts, host_clears = await self._scan_host(ctx, now)

        await self._refresh_disk_summaries(ctx)

        return JobResult(
            components_scanned=len(readings) + host_scanned,
            transitions=len(diff.transitions) + host_transitions,
            alerts_sent=alerts_sent + host_alerts,
            clears_sent=clears_sent + host_clears,
        )

    def _host_resolver(self, config):
        host_cfg = config.sensors.host

        def resolve(reading: HostMetricReading) -> HostMetricPolicy:
            spec = METRICS[reading.metric]
            override = host_cfg.thresholds.get(reading.metric)
            if override is not None:
                spec = spec.with_thresholds(override.warn, override.crit)
            return HostMetricPolicy(
                spec=spec,
                consecutive_to_alert=host_cfg.consecutive_to_alert,
                consecutive_to_clear=host_cfg.consecutive_to_clear,
            )

        return resolve

    async def _scan_host(self, ctx: JobContext, now: datetime) -> tuple[int, int, int, int]:
        """Срез host-метрик VPS (пусто на server/workstation — [sensors.host] выключен)."""
        readings = await ctx.sensors.read_host()
        if not readings:
            return 0, 0, 0, 0
        known = await ctx.store.get_known_host_states()
        diff = compute_host_diff(readings, known, self._host_resolver(ctx.config), now)
        await ctx.store.apply_host_diff(diff, now)
        await ctx.store.record_host_readings(readings)

        alerts = 0
        for state in await ctx.store.pending_host_alerts():
            event = _host_event_from_state(state, EVENT_HOST_DEGRADED, now)
            result = await ctx.dispatcher.dispatch_host_alert(event)
            if result.handled:
                await ctx.store.mark_host_alert_notified(state.component_id, now)
            if result.delivered:
                alerts += 1
        clears = 0
        for state in await ctx.store.pending_host_clears():
            event = _host_event_from_state(state, EVENT_HOST_RECOVERED, now)
            result = await ctx.dispatcher.dispatch_host_clear(event)
            if result.handled:
                await ctx.store.mark_host_cleared_notified(state.component_id, now)
            if result.delivered:
                clears += 1
        return len(readings), len(diff.transitions), alerts, clears

    async def _dispatch_alerts(self, ctx: JobContext, now: datetime) -> int:
        sent = 0
        for state in await ctx.store.pending_alerts():
            event = _event_from_state(state, EVENT_OVERHEAT_STARTED, now)
            result = await ctx.dispatcher.dispatch_alert(event)
            if result.handled:
                await ctx.store.mark_alert_notified(state.component_id, now)
            if result.delivered:
                sent += 1
        return sent

    async def _dispatch_clears(self, ctx: JobContext, now: datetime) -> int:
        sent = 0
        for state in await ctx.store.pending_clears():
            event = _event_from_state(state, EVENT_OVERHEAT_CLEARED, now)
            result = await ctx.dispatcher.dispatch_clear(event)
            if result.handled:
                await ctx.store.mark_cleared_notified(state.component_id, now)
            if result.delivered:
                sent += 1
        return sent

    async def _refresh_disk_summaries(self, ctx: JobContext) -> None:
        """Пересчитать DiskSummary (для /status) и закэшировать в БД.

        Тот же живой опрос (smartctl/LHM), что раньше делал `MonitorService.
        get_state()` синхронно на каждый запрос бота — здесь он уходит в
        фон на кадансе scan_cron, а бот читает готовый кэш (см.
        `Store.get_disk_summaries`).
        """
        health_overrides = await ctx.store.get_smart_health_map()
        disks = await ctx.sensors.read_disk_summaries(health_overrides)
        await ctx.store.save_disk_summaries(disks)


def _host_event_from_state(
    state: HostMetricState, event_type: str, now: datetime
) -> HostEvent:
    use_alert_time = event_type == EVENT_HOST_DEGRADED and state.alerting_since is not None
    return HostEvent(
        type=event_type,
        component_id=state.component_id,
        metric=state.metric,
        label=state.label,
        value=state.value,
        unit=state.unit,
        hint=METRICS[state.metric].hint if state.metric in METRICS else "",
        at=state.alerting_since if use_alert_time else now,
    )


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
