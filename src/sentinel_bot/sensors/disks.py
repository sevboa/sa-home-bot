"""Адаптер температуры дисков через smartctl (smartmontools).

Парсинг JSON-вывода вынесен в чистые функции для тестов. Реальные вызовы
подпроцесса делает вызывающий код через run_in_executor.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime

from sentinel_bot.domain.models import KIND_DISK, SensorReading

log = logging.getLogger(__name__)


def parse_scan(data: dict) -> list[str]:
    """Список устройств из `smartctl --scan -j`."""
    return [d["name"] for d in data.get("devices", []) if d.get("name")]


def parse_device_temp(device: str, data: dict, now: datetime) -> SensorReading | None:
    """Достать температуру из `smartctl -j -A /dev/X`."""
    temp = None
    if isinstance(data.get("temperature"), dict):
        temp = data["temperature"].get("current")
    if temp is None:
        # Fallback: атрибут 194 (Temperature_Celsius) в таблице ATA.
        table = data.get("ata_smart_attributes", {}).get("table", [])
        for attr in table:
            if attr.get("id") == 194:
                temp = attr.get("raw", {}).get("value")
                break
    if temp is None:
        return None

    model = data.get("model_name") or data.get("device", {}).get("name") or device
    return SensorReading(
        component_id=f"disk:{device}",
        kind=KIND_DISK,
        label=str(model),
        temperature_c=float(temp),
        taken_at=now,
    )


def _run_smartctl(args: list[str]) -> dict | None:
    smartctl = shutil.which("smartctl")
    if not smartctl:
        log.warning("smartctl не найден (установите smartmontools)")
        return None
    try:
        out = subprocess.run(
            [smartctl, *args],
            capture_output=True,
            text=True,
            timeout=20,
        )
        # smartctl возвращает ненулевой код при некритичных предупреждениях,
        # но JSON всё равно валиден — парсим stdout.
        if out.stdout.strip():
            return json.loads(out.stdout)
    except Exception as exc:  # noqa: BLE001
        log.warning("smartctl %s не сработал: %s", " ".join(args), exc)
    return None


def discover_devices_sync() -> list[str]:
    data = _run_smartctl(["--scan", "-j"])
    return parse_scan(data) if data else []


def read_disks_sync(devices: list[str], now: datetime) -> list[SensorReading]:
    """Снять температуры дисков (блокирующе)."""
    targets = devices or discover_devices_sync()
    readings: list[SensorReading] = []
    for device in targets:
        data = _run_smartctl(["-j", "-A", device])
        if not data:
            continue
        reading = parse_device_temp(device, data, now)
        if reading is not None:
            readings.append(reading)
    return readings
