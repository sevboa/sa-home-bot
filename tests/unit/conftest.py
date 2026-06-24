"""Общие фикстуры и хелперы для тестов."""

from __future__ import annotations

from datetime import UTC, datetime

from sa_home_bot.domain.models import KIND_CPU, SensorReading
from sa_home_bot.domain.policy import ComponentPolicy, FixedThresholdPolicy

BASE_TIME = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)


def make_reading(
    temp: float,
    component_id: str = "cpu:pkg",
    kind: str = KIND_CPU,
    label: str = "Package",
    at: datetime | None = None,
) -> SensorReading:
    return SensorReading(
        component_id=component_id,
        kind=kind,
        label=label,
        temperature_c=temp,
        taken_at=at or BASE_TIME,
    )


def cpu_policy(
    warn: float = 80.0,
    crit: float = 90.0,
    delta: float = 5.0,
    to_alert: int = 3,
    to_clear: int = 3,
) -> ComponentPolicy:
    return ComponentPolicy(
        policy=FixedThresholdPolicy(warn, crit, delta),
        consecutive_to_alert=to_alert,
        consecutive_to_clear=to_clear,
    )
