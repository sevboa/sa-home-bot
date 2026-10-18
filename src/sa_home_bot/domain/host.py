from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.hysteresis import reconcile_band
from sa_home_bot.domain.models import OK, KnownState
from sa_home_bot.domain.policy import BAND_MID, BAND_OVER, BAND_UNDER

EVENT_HOST_DEGRADED = "host_degraded"
EVENT_HOST_RECOVERED = "host_recovered"

DIR_ABOVE = "above"
DIR_BELOW = "below"


@dataclass(frozen=True)
class HostMetricSpec:
    metric: str
    label: str
    unit: str
    direction: str
    warn: float
    crit: float
    hysteresis: float
    hint: str

    def band(self, value: float) -> str:
        if self.direction == DIR_ABOVE:
            if value >= self.warn:
                return BAND_OVER
            if value <= self.warn - self.hysteresis:
                return BAND_UNDER
            return BAND_MID
        if value <= self.warn:
            return BAND_OVER
        if value >= self.warn + self.hysteresis:
            return BAND_UNDER
        return BAND_MID

    def with_thresholds(self, warn: float, crit: float) -> HostMetricSpec:
        return HostMetricSpec(
            metric=self.metric,
            label=self.label,
            unit=self.unit,
            direction=self.direction,
            warn=warn,
            crit=crit,
            hysteresis=self.hysteresis,
            hint=self.hint,
        )


METRICS: dict[str, HostMetricSpec] = {
    "steal_pct": HostMetricSpec(
        "steal_pct", "CPU steal", "%", DIR_ABOVE, 10.0, 25.0, 3.0,
        "вероятна переподписка ноды провайдером",
    ),
    "iowait_pct": HostMetricSpec(
        "iowait_pct", "iowait", "%", DIR_ABOVE, 15.0, 40.0, 4.0,
        "хранилище ноды не успевает за нагрузкой",
    ),
    "load_per_core": HostMetricSpec(
        "load_per_core", "load/ядро", "", DIR_ABOVE, 1.5, 4.0, 0.4,
        "нода перегружена",
    ),
    "mem_available_pct": HostMetricSpec(
        "mem_available_pct", "RAM свободно", "%", DIR_BELOW, 15.0, 7.0, 4.0,
        "мало свободной памяти",
    ),
    "swap_used_pct": HostMetricSpec(
        "swap_used_pct", "swap занято", "%", DIR_ABOVE, 25.0, 60.0, 6.0,
        "активный своппинг — памяти не хватает",
    ),
    "disk_used_pct": HostMetricSpec(
        "disk_used_pct", "диск / заполнен", "%", DIR_ABOVE, 80.0, 92.0, 3.0,
        "мало места на корневом разделе",
    ),
    "psi_cpu": HostMetricSpec(
        "psi_cpu", "PSI cpu", "%", DIR_ABOVE, 20.0, 50.0, 5.0,
        "давление по CPU — задачи ждут процессор",
    ),
    "psi_memory": HostMetricSpec(
        "psi_memory", "PSI mem", "%", DIR_ABOVE, 20.0, 50.0, 5.0,
        "давление по памяти",
    ),
    "psi_io": HostMetricSpec(
        "psi_io", "PSI io", "%", DIR_ABOVE, 20.0, 50.0, 5.0,
        "давление по I/O",
    ),
    "oom_kills": HostMetricSpec(
        "oom_kills", "OOM-kill", "/мин", DIR_ABOVE, 1.0, 1.0, 1.0,
        "ядро убивало процессы из-за нехватки памяти",
    ),
    "conntrack_pct": HostMetricSpec(
        "conntrack_pct", "conntrack", "%", DIR_ABOVE, 80.0, 95.0, 3.0,
        "таблица conntrack почти полна — новые соединения дропаются",
    ),
    "net_err_rate": HostMetricSpec(
        "net_err_rate", "ошибки NIC", "/мин", DIR_ABOVE, 1.0, 10.0, 1.0,
        "интерфейс теряет пакеты — `ip -s link`, проверь линк/драйвер virtio",
    ),
    "kernel_stall_events": HostMetricSpec(
        "kernel_stall_events", "стойла ядра", "/скан", DIR_ABOVE, 1.0, 1.0, 1.0,
        "ядро логировало стойла — вероятны фризы VM гипервизором",
    ),
}


def fmt_host_value(value: float, unit: str) -> str:
    if unit == "%":
        return f"{value:.0f}%"
    if unit == "":
        return f"{value:.1f}"
    return f"{value:.0f}{unit}"


@dataclass(frozen=True)
class HostMetricReading:
    component_id: str
    metric: str
    label: str
    value: float
    unit: str
    taken_at: datetime


@dataclass(frozen=True)
class HostMetricState:
    component_id: str
    metric: str
    label: str
    value: float
    unit: str
    status: str
    consecutive_count: int
    alerting_since: datetime | None


@dataclass(frozen=True)
class HostTransition:
    component_id: str
    metric: str
    label: str
    value: float
    unit: str
    hint: str
    from_status: str
    to_status: str
    at: datetime


@dataclass(frozen=True)
class HostHealthDiff:
    states: list[HostMetricState]
    transitions: list[HostTransition]


@dataclass(frozen=True)
class HostEvent:
    type: str
    component_id: str
    metric: str
    label: str
    value: float
    unit: str
    hint: str
    at: datetime


@dataclass(frozen=True)
class HostMetricPolicy:
    spec: HostMetricSpec
    consecutive_to_alert: int
    consecutive_to_clear: int


HostPolicyResolver = Callable[[HostMetricReading], HostMetricPolicy]


def compute_host_diff(
    current: list[HostMetricReading],
    known: dict[str, KnownState],
    resolve_policy: HostPolicyResolver,
    now: datetime,
) -> HostHealthDiff:
    states: list[HostMetricState] = []
    transitions: list[HostTransition] = []
    for reading in current:
        hpolicy = resolve_policy(reading)
        prev = known.get(reading.component_id)
        prev_status = prev.status if prev else OK
        band = hpolicy.spec.band(reading.value)
        res = reconcile_band(
            band,
            prev_status=prev_status,
            prev_count=prev.consecutive_count if prev else 0,
            alerting_since=prev.alerting_since if prev else None,
            consecutive_to_alert=hpolicy.consecutive_to_alert,
            consecutive_to_clear=hpolicy.consecutive_to_clear,
            now=now,
        )
        states.append(
            HostMetricState(
                component_id=reading.component_id,
                metric=reading.metric,
                label=reading.label,
                value=reading.value,
                unit=reading.unit,
                status=res.status,
                consecutive_count=res.consecutive_count,
                alerting_since=res.alerting_since,
            )
        )
        if res.transitioned_to is not None:
            transitions.append(
                HostTransition(
                    component_id=reading.component_id,
                    metric=reading.metric,
                    label=reading.label,
                    value=reading.value,
                    unit=reading.unit,
                    hint=hpolicy.spec.hint,
                    from_status=prev_status,
                    to_status=res.transitioned_to,
                    at=now,
                )
            )
    return HostHealthDiff(states=states, transitions=transitions)
