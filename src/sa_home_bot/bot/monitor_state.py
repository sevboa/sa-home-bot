"""Парсинг payload'ов монитора (get_state / события) в доменные объекты.

Обратная сторона сериализации в ``monitor.service`` и ``monitor.dispatch``:
бот получает по протоколу голые dict'ы и восстанавливает из них модели для
рендера. Невалидный payload → KeyError/ValueError, вызывающий решает сам.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sa_home_bot.domain.models import (
    DiskSummary,
    Event,
    HealthState,
    PowerEvent,
    SmartAttrChange,
    SmartChange,
)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def parse_health_state(raw: dict[str, Any]) -> HealthState:
    return HealthState(
        component_id=raw["component_id"],
        kind=raw["kind"],
        label=raw["label"],
        status=raw["status"],
        temperature_c=raw["temperature_c"],
        consecutive_count=0,  # деталь гистерезиса, наружу монитор её не отдаёт
        alerting_since=_dt(raw.get("alerting_since")),
    )


def parse_disk_summary(raw: dict[str, Any]) -> DiskSummary:
    # kind появился позже label: старый монитор его не шлёт — выводим из
    # метки (eMMC узнаваема), остальное честно деградирует к hdd.
    kind = raw.get("kind") or ("emmc" if raw["label"] == "eMMC" else "hdd")
    return DiskSummary(
        label=raw["label"],
        health=raw.get("health"),
        temperature_c=raw.get("temperature_c"),
        free_bytes=raw.get("free_bytes"),
        total_bytes=raw.get("total_bytes"),
        model=raw.get("model"),
        kind=kind,
    )


def parse_outage(raw: dict[str, Any] | None) -> PowerEvent | None:
    if raw is None:
        return None
    return PowerEvent(
        kind=raw["kind"],
        boot_at=_dt(raw["boot_at"]),
        down_at=_dt(raw.get("down_at")),
        up_at=_dt(raw.get("up_at")),
        down_approx=bool(raw.get("down_approx", False)),
    )


def parse_overheat_event(event_type: str, data: dict[str, Any]) -> Event:
    return Event(
        type=event_type,
        component_id=data["component_id"],
        kind=data["kind"],
        label=data["label"],
        temperature_c=data["temperature_c"],
        at=_dt(data["at"]),
    )


def parse_smart_change(event_type: str, data: dict[str, Any]) -> SmartChange:
    return SmartChange(
        component_id=data["component_id"],
        label=data["label"],
        event_type=event_type,
        health_from=data.get("health_from"),
        health_to=data.get("health_to"),
        attr_changes=tuple(
            SmartAttrChange(
                attr_id=c["attr_id"], name=c["name"], old=c["old"], new=c["new"]
            )
            for c in data.get("attr_changes", [])
        ),
        at=_dt(data["at"]),
    )
