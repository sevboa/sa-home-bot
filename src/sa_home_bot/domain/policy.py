"""Политика порогов: классификация показания относительно зоны нагрева.

MVP — `FixedThresholdPolicy` (фиксированные пороги из конфига). Этап 2 добавит
`BaselinePolicy` со скользящей статистикой; контракт `ThresholdPolicy` не
изменится, job и БД-схема тоже.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sa_home_bot.domain.models import SensorReading

# Зоны относительно порогов (с учётом гистерезисной полосы).
BAND_OVER = "over"  # temp >= warn — тянет в alerting
BAND_UNDER = "under"  # temp <= warn - delta — тянет в ok
BAND_MID = "mid"  # мёртвая зона между ними — сбрасывает счётчик гистерезиса


class ThresholdPolicy(Protocol):
    def band(self, reading: SensorReading) -> str:
        """Вернуть BAND_OVER | BAND_UNDER | BAND_MID для показания."""
        ...


@dataclass(frozen=True)
class FixedThresholdPolicy:
    """Сравнение с фиксированным warn-порогом и гистерезисной дельтой."""

    warn_c: float
    crit_c: float
    hysteresis_delta_c: float

    def band(self, reading: SensorReading) -> str:
        temp = reading.temperature_c
        if temp >= self.warn_c:
            return BAND_OVER
        if temp <= self.warn_c - self.hysteresis_delta_c:
            return BAND_UNDER
        return BAND_MID


@dataclass(frozen=True)
class BaselineStats:
    """Скользящая статистика показаний компонента за окно (из таблицы readings)."""

    count: int
    mean: float
    std: float  # стандартное отклонение (популяционное)


@dataclass(frozen=True)
class BaselinePolicy:
    """Адаптивный порог: аномальное отклонение от нормальной температуры.

    Порог = ``min(warn_c, mean + k_sigma * max(std, min_std_c))``. То есть baseline
    может только опустить порог ниже фиксированного ``warn_c`` (раньше поймать
    перегрев на обычно холодной машине), но никогда не поднимает его выше — ``warn_c``
    остаётся жёсткой страховкой. Пока накоплено меньше ``min_samples`` показаний,
    работает как ``FixedThresholdPolicy`` (холодный старт). Гистерезис — тот же, что
    у фиксированной политики: полоса ``[warn - delta, warn)`` сбрасывает счётчик.
    """

    warn_c: float
    crit_c: float
    hysteresis_delta_c: float
    stats: BaselineStats
    min_samples: int
    k_sigma: float
    min_std_c: float

    def _effective_warn(self) -> float:
        if self.stats.count < self.min_samples:
            return self.warn_c
        dynamic = self.stats.mean + self.k_sigma * max(self.stats.std, self.min_std_c)
        return min(self.warn_c, dynamic)

    def band(self, reading: SensorReading) -> str:
        temp = reading.temperature_c
        warn = self._effective_warn()
        if temp >= warn:
            return BAND_OVER
        if temp <= warn - self.hysteresis_delta_c:
            return BAND_UNDER
        return BAND_MID


@dataclass(frozen=True)
class ComponentPolicy:
    """Политика + параметры анти-дребезга для одного вида компонента."""

    policy: ThresholdPolicy
    consecutive_to_alert: int
    consecutive_to_clear: int
