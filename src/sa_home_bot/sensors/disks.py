from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.models import (
    DISK_FAIL,
    DISK_OK,
    DISK_WARN,
    KIND_DISK,
    DiskSummary,
    SensorReading,
    SmartSnapshot,
)
from sa_home_bot.domain.smart import MONITORED_SMART_ATTRS
from sa_home_bot.utils.requirements import (
    Requirement,
    RequirementStatus,
    looks_like_permission_error,
    requirements_registry,
)

log = logging.getLogger(__name__)

SMARTCTL_TIMEOUT_S = 20

SMARTCTL_REQUIREMENT = Requirement(
    program="smartctl",
    package="smartmontools",
    note="температура и здоровье дисков по SMART",
)

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
    name: str
    dev_type: str | None = None


def is_smart_capable(name: str) -> bool:
    return not name.startswith(UNSUPPORTED_PREFIXES)


def parse_device_spec(spec: str) -> DiskTarget | None:
    spec = spec.strip()
    if not spec:
        return None
    name, sep, dev_type = spec.rpartition(":")
    if not sep:
        return DiskTarget(spec, None)
    name = name.strip()
    if not name:
        return None
    return DiskTarget(name, dev_type.strip() or None)


def parse_scan(data: dict) -> list[DiskTarget]:
    targets: list[DiskTarget] = []
    for d in data.get("devices", []):
        name = d.get("name")
        if not name:
            continue
        targets.append(DiskTarget(name, d.get("type") or None))
    return targets


def read_args(target: DiskTarget) -> list[str]:
    args: list[str] = []
    if target.dev_type:
        args += ["-d", target.dev_type]
    args += ["-j", "-A", target.name]
    return args


def parse_device_temp(device: str, data: dict, now: datetime) -> SensorReading | None:
    temp = None
    if isinstance(data.get("temperature"), dict):
        temp = data["temperature"].get("current")
    if temp is None:
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
    diagnosis = SMARTCTL_REQUIREMENT.diagnose()
    if diagnosis is not RequirementStatus.OK:
        requirements_registry.report(SMARTCTL_REQUIREMENT, diagnosis)
        log.warning("smartctl недоступен: %s", SMARTCTL_REQUIREMENT.install_hint())
        return None
    try:
        out = subprocess.run(
            ["smartctl", *args],
            capture_output=True,
            text=True,
            timeout=SMARTCTL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log.warning("smartctl %s: таймаут %dс", " ".join(args), SMARTCTL_TIMEOUT_S)
        return None
    except Exception as exc:
        log.warning("smartctl %s не сработал: %s", " ".join(args), exc)
        return None
    if not out.stdout.strip():
        if looks_like_permission_error(out.stderr):
            requirements_registry.report(SMARTCTL_REQUIREMENT, RequirementStatus.NEEDS_PRIVILEGE)
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        log.warning("smartctl %s: невалидный JSON: %s", " ".join(args), exc)
        return None
    requirements_registry.report(SMARTCTL_REQUIREMENT, RequirementStatus.OK)
    return data


def discover_devices_sync() -> list[DiskTarget]:
    data = _run_smartctl(["--scan", "-j"])
    if not data:
        return []
    return [t for t in parse_scan(data) if is_smart_capable(t.name)]


def _resolve_targets(specs: list[str]) -> list[DiskTarget]:
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


def read_disks_sync(
    specs: list[str], now: datetime, lhm_dll_path: str = ""
) -> list[SensorReading]:
    if sys.platform == "win32":
        from sa_home_bot.sensors import lhm

        return lhm.read_disk_readings_sync(lhm_dll_path, now)
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


KIND_HDD = "hdd"
KIND_SSD = "ssd"
KIND_NVME = "nvme"
KIND_EMMC = "emmc"

_KIND_LABEL = {KIND_HDD: "HDD", KIND_SSD: "SSD", KIND_NVME: "NVMe", KIND_EMMC: "eMMC"}


@dataclass(frozen=True)
class BlockDisk:
    path: str
    kind: str
    mountpoints: tuple[str, ...]
    model: str | None


_NVME_NAMESPACE_RE = re.compile(r"^(/dev/nvme\d+)n\d+$")


def nvme_controller_path(path: str) -> str | None:
    m = _NVME_NAMESPACE_RE.match(path)
    return m.group(1) if m else None


def health_args(target: DiskTarget) -> list[str]:
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

    pending = _attr(197)
    uncorrectable = _attr(198)
    return DISK_WARN if (pending or uncorrectable) else DISK_OK


def _is_rotational(rota) -> bool:
    if isinstance(rota, str):
        return rota.strip() != "0"
    if rota is None:
        return True
    return bool(rota)


def _disk_kind(name: str, tran: str | None, rota) -> str:
    if tran == "mmc" or name.startswith("mmcblk"):
        return KIND_EMMC
    if tran == "nvme" or name.startswith("nvme"):
        return KIND_NVME
    if not _is_rotational(rota):
        return KIND_SSD
    return KIND_HDD


def _is_empty_media(size) -> bool:
    if size is None:
        return False
    if isinstance(size, str):
        size = size.strip()
        if not size:
            return False
    try:
        return int(size) == 0
    except (TypeError, ValueError):
        return False


def parse_lsblk_disks(data: dict) -> list[BlockDisk]:
    disks: list[BlockDisk] = []
    for dev in data.get("blockdevices", []):
        if dev.get("type") != "disk" or "boot" in (dev.get("name") or ""):
            continue
        if _is_empty_media(dev.get("size")):
            continue
        mps: list[str] = []
        _collect_mountpoints(dev, mps)
        name = dev.get("name") or ""
        disks.append(
            BlockDisk(
                path=dev.get("path") or f"/dev/{name}",
                kind=_disk_kind(name, dev.get("tran"), dev.get("rota")),
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
            ["lsblk", "-J", "-b", "-o", "NAME,TYPE,MOUNTPOINT,TRAN,MODEL,PATH,ROTA,SIZE"],
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
    ordered = sorted(disks, key=lambda x: (x.kind != KIND_EMMC, x.path))
    per_kind: dict[str, int] = {}
    for d in ordered:
        per_kind[d.kind] = per_kind.get(d.kind, 0) + 1
    counters: dict[str, int] = {}
    labelled: list[tuple[str, BlockDisk]] = []
    for d in ordered:
        base = _KIND_LABEL[d.kind]
        if per_kind[d.kind] > 1:
            counters[d.kind] = counters.get(d.kind, 0) + 1
            label = f"{base}{counters[d.kind]}"
        else:
            label = base
        labelled.append((label, d))
    return labelled


def read_disk_summaries_sync(
    specs: list[str],
    health_overrides: dict[str, str | None] | None = None,
    lhm_dll_path: str = "",
) -> list[DiskSummary]:
    if sys.platform == "win32":
        from sa_home_bot.sensors import lhm

        return lhm.read_disk_summaries_sync(lhm_dll_path)
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
        real = os.path.realpath(disk.path)
        if disk.kind == KIND_NVME:
            controller = nvme_controller_path(disk.path)
            if controller is not None:
                real = os.path.realpath(controller)
        live_health, temp = smart.get(real, (None, None))
        if health_overrides is not None and real in health_overrides:
            health = health_overrides[real]
        else:
            health = live_health
        free, total = _disk_usage(disk.mountpoints)
        summaries.append(
            DiskSummary(
                label=label,
                health=health,
                temperature_c=temp,
                free_bytes=free,
                total_bytes=total,
                model=disk.model,
                kind=disk.kind,
            )
        )
    return summaries


def _raw_count(raw: dict) -> int | None:
    s = raw.get("string")
    if isinstance(s, str):
        head = s.strip().split(" ", 1)[0]
        try:
            return int(head)
        except ValueError:
            pass
    v = raw.get("value")
    return int(v) if v is not None else None


def _raw_attrs(data: dict) -> dict[int, int]:
    out: dict[int, int] = {}
    for a in data.get("ata_smart_attributes", {}).get("table", []):
        aid = a.get("id")
        if aid in MONITORED_SMART_ATTRS:
            val = _raw_count(a.get("raw", {}))
            if val is not None:
                out[aid] = val
    return out


def parse_smart_snapshot(component_id: str, data: dict, now: datetime) -> SmartSnapshot:
    model = data.get("model_name") or data.get("device", {}).get("name") or component_id
    return SmartSnapshot(
        component_id=component_id,
        label=str(model),
        health=parse_health(data),
        attrs=_raw_attrs(data),
        taken_at=now,
    )


def read_smart_snapshots_sync(specs: list[str], now: datetime) -> list[SmartSnapshot]:
    snapshots: list[SmartSnapshot] = []
    seen: set[str] = set()
    for target in _resolve_targets(specs):
        if not is_smart_capable(target.name):
            continue
        real = os.path.realpath(target.name)
        if real in seen:
            continue
        seen.add(real)
        data = _run_smartctl(health_args(target))
        if not data:
            continue
        snapshots.append(parse_smart_snapshot(f"disk:{real}", data, now))
    return snapshots
