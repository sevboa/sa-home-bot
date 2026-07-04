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
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.models import (
    DISK_FAIL,
    DISK_OK,
    DISK_WARN,
    KIND_DISK,
    DiskSummary,
    SensorReading,
)

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


# --- Сводка по дискам для /status (SMART-здоровье + температура + место) ---


@dataclass(frozen=True)
class BlockDisk:
    """Физический диск из lsblk: путь, тип носителя, точки монтирования."""

    path: str  # /dev/sda
    is_mmc: bool  # eMMC/SD — SMART недоступен
    mountpoints: tuple[str, ...]
    model: str | None


def health_args(target: DiskTarget) -> list[str]:
    """Аргументы smartctl для здоровья+атрибутов: ``[-d тип] -j -H -A /dev/X``."""
    args: list[str] = []
    if target.dev_type:
        args += ["-d", target.dev_type]
    args += ["-j", "-H", "-A", target.name]
    return args


def _extract_temp(data: dict) -> float | None:
    temp = None
    if isinstance(data.get("temperature"), dict):
        temp = data["temperature"].get("current")
    if temp is None:
        table = data.get("ata_smart_attributes", {}).get("table", [])
        for attr in table:
            if attr.get("id") == 194:
                temp = attr.get("raw", {}).get("value")
                break
    return float(temp) if temp is not None else None


def parse_health(data: dict) -> str | None:
    """Классифицировать SMART-здоровье: DISK_OK | DISK_WARN | DISK_FAIL | None.

    FAILED → диск при смерти. PASSED, но с pending/uncorrectable секторами →
    предупреждение. Иначе — норма. Нет smart_status → None (недоступно).
    """
    passed = data.get("smart_status", {}).get("passed")
    if passed is None:
        return None
    if passed is False:
        return DISK_FAIL

    def _attr(idn: int) -> int:
        for a in data.get("ata_smart_attributes", {}).get("table", []):
            if a.get("id") == idn:
                return a.get("raw", {}).get("value") or 0
        return 0

    pending = _attr(197)  # Current_Pending_Sector
    uncorrectable = _attr(198)  # Offline_Uncorrectable
    return DISK_WARN if (pending or uncorrectable) else DISK_OK


def parse_lsblk_disks(data: dict) -> list[BlockDisk]:
    """Разобрать ``lsblk -J`` в список физических дисков (type=disk)."""
    disks: list[BlockDisk] = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk" or "boot" in (dev.get("name") or ""):
            continue  # пропускаем mmcblkXbootY и не-диски
        mps: list[str] = []
        _collect_mountpoints(dev, mps)
        tran = dev.get("tran")
        name = dev.get("name") or ""
        disks.append(
            BlockDisk(
                path=dev.get("path") or f"/dev/{name}",
                is_mmc=(tran == "mmc") or name.startswith("mmcblk"),
                mountpoints=tuple(mps),
                model=dev.get("model"),
            )
        )
    return disks


def _collect_mountpoints(node: dict, out: list[str]) -> None:
    mp = node.get("mountpoint")
    if not mp and isinstance(node.get("mountpoints"), list):
        mp = next((m for m in node["mountpoints"] if m), None)
    if mp and mp != "[SWAP]":
        out.append(mp)
    for child in node.get("children", []):
        _collect_mountpoints(child, out)


def _disk_usage(mountpoints: tuple[str, ...]) -> tuple[int | None, int | None]:
    """Суммарные (free, total) байты по смонтированным ФС диска."""
    import psutil

    free = total = 0
    seen = False
    for mp in mountpoints:
        try:
            u = psutil.disk_usage(mp)
        except OSError:
            continue
        free += u.free
        total += u.total
        seen = True
    return (free, total) if seen else (None, None)


def _list_block_disks_sync() -> list[BlockDisk]:
    if shutil.which("lsblk") is None:
        return []
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,MOUNTPOINT,TRAN,MODEL,PATH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("lsblk не сработал: %s", exc)
        return []
    try:
        return parse_lsblk_disks(json.loads(out.stdout))
    except json.JSONDecodeError as exc:
        log.warning("lsblk: невалидный JSON: %s", exc)
        return []


def _label_disks(disks: list[BlockDisk]) -> list[tuple[str, BlockDisk]]:
    """Назначить метки: не-mmc диски → HDD1, HDD2…; mmc → eMMC."""
    labelled: list[tuple[str, BlockDisk]] = []
    hdd_n = 0
    for d in sorted(disks, key=lambda x: (x.is_mmc, x.path)):
        if d.is_mmc:
            label = "eMMC"
        else:
            hdd_n += 1
            label = f"HDD{hdd_n}"
        labelled.append((label, d))
    return labelled


def read_disk_summaries_sync(specs: list[str]) -> list[DiskSummary]:
    """Собрать сводку по ВСЕМ физическим дискам (блокирующе, через executor).

    SMART-здоровье/температура снимаются только для дисков с известным типом
    адаптера (из конфига), сопоставление по реальному пути (by-id → /dev/sdX) —
    иначе автоопределение USB-моста виснет. Свободное место — по точкам
    монтирования, для всех дисков (включая eMMC без SMART).
    """
    # SMART по реальному пути устройства.
    smart: dict[str, tuple[str | None, float | None]] = {}
    for target in _resolve_targets(specs):
        if not is_smart_capable(target.name):
            continue
        data = _run_smartctl(health_args(target))
        if not data:
            continue
        real = os.path.realpath(target.name)
        smart[real] = (parse_health(data), _extract_temp(data))

    summaries: list[DiskSummary] = []
    for label, disk in _label_disks(_list_block_disks_sync()):
        health, temp = smart.get(os.path.realpath(disk.path), (None, None))
        free, total = _disk_usage(disk.mountpoints)
        summaries.append(
            DiskSummary(
                label=label,
                health=health,
                temperature_c=temp,
                free_bytes=free,
                total_bytes=total,
                model=disk.model,
            )
        )
    return summaries
