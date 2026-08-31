"""Host-метрики VPS: здоровье ноды по /proc, а не по термозонам и SMART.

У виртуалки нет температур, зато есть свои индикаторы деградации: переподписка
провайдером (CPU steal), контеншн хранилища (iowait), давление памяти, заполнение
маленького диска. Модель показания здесь **отдельная** от температурной
(`domain/models.SensorReading`) — значение это %, load или счётчик, не °C — но
гистерезисный КА переиспользуется (`domain/hysteresis.reconcile_band`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.hysteresis import reconcile_band
from sa_home_bot.domain.models import ALERTING, OK, KnownState
from sa_home_bot.domain.policy import BAND_MID, BAND_OVER, BAND_UNDER

KIND_HOST = "host"

EVENT_HOST_DEGRADED = "host_degraded"  # host-метрика ушла за порог
EVENT_HOST_RECOVERED = "host_recovered"  # вернулась в норму

DIR_ABOVE = "above"  # тревога при росте (steal, iowait, load, диск…)
DIR_BELOW = "below"  # тревога при падении (свободная память)


@dataclass(frozen=True)
class HostMetricSpec:
    """Определение одной host-метрики: смысл, единица, направление, пороги."""

    metric: str
    label: str
    unit: str  # "%" | "" (load) | "/мин" (oom)
    direction: str  # DIR_ABOVE | DIR_BELOW
    warn: float
    crit: float
    hysteresis: float  # мёртвая зона возврата (в единицах метрики)
    hint: str  # доп. фраза в тексте оповещения

    def band(self, value: float) -> str:
        if self.direction == DIR_ABOVE:
            if value >= self.warn:
                return BAND_OVER
            if value <= self.warn - self.hysteresis:
                return BAND_UNDER
            return BAND_MID
        # DIR_BELOW: низкое значение — плохо
        if value <= self.warn:
            return BAND_OVER
        if value >= self.warn + self.hysteresis:
            return BAND_UNDER
        return BAND_MID

    def with_thresholds(self, warn: float, crit: float) -> HostMetricSpec:
        """Копия с порогами из конфига ноды (дефолты перекрываются)."""
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


# Реестр метрик + дефолтные пороги (перекрываются в [sensors.host] конфига ноды).
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
}


def fmt_host_value(value: float, unit: str) -> str:
    """Человекочитаемое значение: «32%», «1.8», «2/мин»."""
    if unit == "%":
        return f"{value:.0f}%"
    if unit == "":
        return f"{value:.1f}"
    return f"{value:.0f}{unit}"


@dataclass(frozen=True)
class HostMetricReading:
    """Мгновенное показание одной host-метрики."""

    component_id: str  # "host:steal_pct"
    metric: str
    label: str
    value: float
    unit: str
    taken_at: datetime


@dataclass(frozen=True)
class HostMetricState:
    """Вычисленное состояние host-метрики (выход reconciliation, в БД)."""

    component_id: str
    metric: str
    label: str
    value: float
    unit: str
    status: str  # OK | ALERTING
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
    """Событие host-метрики, производное от перехода. Рассылается подписчикам."""

    type: str  # EVENT_HOST_DEGRADED | EVENT_HOST_RECOVERED
    component_id: str
    metric: str
    label: str
    value: float
    unit: str
    hint: str
    at: datetime


@dataclass(frozen=True)
class HostMetricPolicy:
    """Спека метрики + параметры анти-дребезга (из конфига ноды)."""

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
    """Сравнить срез host-показаний с известным состоянием (та же машина, что у температур)."""
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


def classify_host_events(transitions: list[HostTransition]) -> list[HostEvent]:
    events: list[HostEvent] = []
    for tr in transitions:
        if tr.from_status == OK and tr.to_status == ALERTING:
            event_type = EVENT_HOST_DEGRADED
        elif tr.from_status == ALERTING and tr.to_status == OK:
            event_type = EVENT_HOST_RECOVERED
        else:
            continue
        events.append(
            HostEvent(
                type=event_type,
                component_id=tr.component_id,
                metric=tr.metric,
                label=tr.label,
                value=tr.value,
                unit=tr.unit,
                hint=tr.hint,
                at=tr.at,
            )
        )
    return events
