from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sa_home_bot.domain.models import SensorReading

BAND_OVER = "over"
BAND_UNDER = "under"
BAND_MID = "mid"


class ThresholdPolicy(Protocol):
    def band(self, reading: SensorReading) -> str:
        ...


@dataclass(frozen=True)
class FixedThresholdPolicy:
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
    count: int
    mean: float
    std: float


@dataclass(frozen=True)
class BaselinePolicy:
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
    policy: ThresholdPolicy
    consecutive_to_alert: int
    consecutive_to_clear: int
