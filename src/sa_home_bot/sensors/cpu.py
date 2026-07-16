"""Адаптер температуры CPU: psutil, fallback на `sensors -j` (lm-sensors).

На Windows ни psutil, ни lm-sensors температур не дают — там источник
LibreHardwareMonitor (см. `sensors/lhm.py`), диспетчеризация в `read_cpu_sync`.

Парсинг вынесен в чистые функции (`parse_psutil_temps`, `parse_lm_sensors`),
чтобы тестировать на фикстурах без реального железа. Блокирующие вызовы делает
вызывающий код (SensorSource) через run_in_executor.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime

from sa_home_bot.domain.models import KIND_CPU, SensorReading

log = logging.getLogger(__name__)


def parse_psutil_temps(data: dict, now: datetime) -> list[SensorReading]:
    """Разобрать вывод psutil.sensors_temperatures() в SensorReading."""
    readings: list[SensorReading] = []
    for chip, entries in data.items():
        for idx, entry in enumerate(entries):
            # entry — psutil shwtemp(label, current, high, critical) или совместимый.
            current = getattr(entry, "current", None)
            label = getattr(entry, "label", "") or ""
            if current is None:
                continue
            slug = label.strip() or f"{chip}#{idx}"
            human = label.strip() or chip
            readings.append(
                SensorReading(
                    component_id=f"cpu:{chip}:{slug}",
                    kind=KIND_CPU,
                    label=human,
                    temperature_c=float(current),
                    taken_at=now,
                )
            )
    return readings


def parse_lm_sensors(data: dict, now: datetime) -> list[SensorReading]:
    """Разобрать вывод `sensors -j` в SensorReading (fallback)."""
    readings: list[SensorReading] = []
    for chip, features in data.items():
        if not isinstance(features, dict):
            continue
        for feature, values in features.items():
            if not isinstance(values, dict):
                continue
            temp = None
            for key, val in values.items():
                if key.endswith("_input"):
                    temp = val
                    break
            if temp is None:
                continue
            readings.append(
                SensorReading(
                    component_id=f"cpu:{chip}:{feature}",
                    kind=KIND_CPU,
                    label=feature,
                    temperature_c=float(temp),
                    taken_at=now,
                )
            )
    return readings


def read_cpu_sync(now: datetime, lhm_dll_path: str = "") -> list[SensorReading]:
    """Снять температуры CPU (блокирующе). psutil → fallback lm-sensors;
    на Windows — LibreHardwareMonitor."""
    if sys.platform == "win32":
        from sa_home_bot.sensors import lhm

        return lhm.read_cpu_readings_sync(lhm_dll_path, now)
    try:
        import psutil

        data = psutil.sensors_temperatures()
        if data:
            readings = parse_psutil_temps(data, now)
            if readings:
                return readings
    except Exception as exc:  # noqa: BLE001 — источник опционален
        log.warning("psutil.sensors_temperatures недоступен: %s", exc)

    sensors_bin = shutil.which("sensors")
    if sensors_bin:
        try:
            out = subprocess.run(
                [sensors_bin, "-j"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            return parse_lm_sensors(json.loads(out.stdout), now)
        except Exception as exc:  # noqa: BLE001
            log.warning("fallback `sensors -j` не сработал: %s", exc)

    log.warning("Не удалось получить температуру CPU ни одним способом")
    return []
