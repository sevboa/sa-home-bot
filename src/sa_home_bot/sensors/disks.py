"""Адаптер температуры дисков через smartctl (smartmontools).

Парсинг JSON-вывода вынесен в чистые функции для тестов. Реальные вызовы
подпроцесса делает вызывающий код через run_in_executor.

Каждое устройство опрашивается с типом адаптера (``-d``): без него USB-мосты
(например JMicron, ``sntjmicron``) отдают ``temperature: null``. Тип берётся из
``smartctl --scan -j`` либо задаётся вручную в конфиге как ``/dev/sda:тип``.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.models import KIND_DISK, SensorReading

log = logging.getLogger(__name__)

# Таймаут одного вызова smartctl. Защищает скан от зависшего USB-моста:
# опрос диска прерывается, остальные диски опрашиваются дальше.
SMARTCTL_TIMEOUT_S = 20

# Префиксы устройств без поддержки SMART — не опрашиваем (eMMC, optical, loop,
# software-RAID, device-mapper, ram/zram). Иначе — спам ошибок в лог каждый скан.
UNSUPPORTED_PREFIXES = (
    "/dev/mmcblk",
    "/dev/loop",
    "/dev/sr",
    "/dev/md",
    "/dev/dm-",
    "/dev/zram",
    "/dev/ram",
)


@dataclass(frozen=True)
class DiskTarget:
    """Устройство для опроса smartctl: путь + опциональный тип адаптера (``-d``)."""

    name: str
    dev_type: str | None = None


def is_smart_capable(name: str) -> bool:
    """Поддерживает ли устройство SMART (по имени; eMMC/loop/raid — нет)."""
    return not name.startswith(UNSUPPORTED_PREFIXES)


def parse_device_spec(spec: str) -> DiskTarget | None:
    """Разобрать запись конфига: ``/dev/sda`` или ``/dev/sda:sntjmicron``.

    Тип отделяется последним двоеточием. Пустая или некорректная запись → None.
    """
    spec = spec.strip()
    if not spec:
        return None
    name, sep, dev_type = spec.rpartition(":")
    if not sep:
        return DiskTarget(spec, None)
    name = name.strip()
    if not name:  # запись вида ":type" без устройства
        return None
    return DiskTarget(name, dev_type.strip() or None)


def parse_scan(data: dict) -> list[DiskTarget]:
    """Список устройств из ``smartctl --scan -j`` с типом адаптера (``-d``)."""
    targets: list[DiskTarget] = []
    for d in data.get("devices", []):
        name = d.get("name")
        if not name:
            continue
        targets.append(DiskTarget(name, d.get("type") or None))
    return targets


def read_args(target: DiskTarget) -> list[str]:
    """Аргументы smartctl для чтения атрибутов: ``[-d тип] -j -A /dev/X``."""
    args: list[str] = []
    if target.dev_type:
        args += ["-d", target.dev_type]
    args += ["-j", "-A", target.name]
    return args


def parse_device_temp(device: str, data: dict, now: datetime) -> SensorReading | None:
    """Достать температуру из ``smartctl -j -A /dev/X``."""
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
            timeout=SMARTCTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Зависший USB-мост/больной диск: не валим скан остальных устройств.
        log.warning("smartctl %s: таймаут %dс", " ".join(args), SMARTCTL_TIMEOUT_S)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("smartctl %s не сработал: %s", " ".join(args), exc)
        return None
    # smartctl возвращает ненулевой код при некритичных предупреждениях,
    # но JSON всё равно валиден — парсим stdout.
    if not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        log.warning("smartctl %s: невалидный JSON: %s", " ".join(args), exc)
        return None


def discover_devices_sync() -> list[DiskTarget]:
    """Автоопределение дисков через ``smartctl --scan`` (с типом, без не-SMART)."""
    data = _run_smartctl(["--scan", "-j"])
    if not data:
        return []
    return [t for t in parse_scan(data) if is_smart_capable(t.name)]


def _resolve_targets(specs: list[str]) -> list[DiskTarget]:
    """Из конфиг-записей собрать список устройств; пусто → автоскан."""
    if not specs:
        return discover_devices_sync()
    targets: list[DiskTarget] = []
    for spec in specs:
        target = parse_device_spec(spec)
        if target is None:
            log.warning("disks.devices: некорректная запись %r — пропущена", spec)
            continue
        targets.append(target)
    return targets


def read_disks_sync(specs: list[str], now: datetime) -> list[SensorReading]:
    """Снять температуры дисков (блокирующе).

    Опрос каждого устройства изолирован: ошибка/таймаут одного не роняет
    остальные. Устройства без SMART (eMMC и т.п.) молча пропускаются.
    """
    readings: list[SensorReading] = []
    for target in _resolve_targets(specs):
        if not is_smart_capable(target.name):
            continue
        data = _run_smartctl(read_args(target))
        if not data:
            continue
        reading = parse_device_temp(target.name, data, now)
        if reading is not None:
            readings.append(reading)
    return readings
