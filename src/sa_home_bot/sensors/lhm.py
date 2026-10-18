from __future__ import annotations

import concurrent.futures
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path

from sa_home_bot.domain.models import KIND_CPU, KIND_DISK, DiskSummary, SensorReading
from sa_home_bot.sensors.disks import KIND_HDD, KIND_NVME, KIND_SSD

log = logging.getLogger(__name__)

DLL_NAME = "LibreHardwareMonitorLib.dll"

HW_CPU = "Cpu"
HW_STORAGE = "Storage"
SENSOR_TEMPERATURE = "Temperature"
SENSOR_COMPOSITE_TEMPERATURE = "Composite Temperature"

_KIND_LABEL = {KIND_HDD: "HDD", KIND_SSD: "SSD", KIND_NVME: "NVMe"}

_TEMP_THRESHOLD_NAMES = {"Warning Temperature", "Critical Temperature"}


class LhmUnavailable(Exception):
    pass


def _walk(tree: list[dict]):
    for node in tree:
        yield node
        yield from _walk(node.get("subhardware", []))


def _temp_sensors(node: dict) -> list[dict]:
    return [
        s
        for s in node.get("sensors", [])
        if s.get("type") == SENSOR_TEMPERATURE
        and (s.get("value") or 0) > 0
        and s.get("name") not in _TEMP_THRESHOLD_NAMES
    ]


def _hw_key(node: dict) -> str:
    return str(node.get("id") or node.get("name") or "?")


def cpu_readings_from_tree(tree: list[dict], now: datetime) -> list[SensorReading]:
    readings: list[SensorReading] = []
    for node in _walk(tree):
        if node.get("type") != HW_CPU:
            continue
        hw_key = _hw_key(node)
        for sensor in _temp_sensors(node):
            name = str(sensor.get("name") or "?")
            readings.append(
                SensorReading(
                    component_id=f"cpu:{hw_key}:{name}",
                    kind=KIND_CPU,
                    label=name,
                    temperature_c=float(sensor["value"]),
                    taken_at=now,
                )
            )
    return readings


def disk_readings_from_tree(tree: list[dict], now: datetime) -> list[SensorReading]:
    readings: list[SensorReading] = []
    for node in _walk(tree):
        if node.get("type") != HW_STORAGE:
            continue
        value = _disk_temp(node)
        if value is None:
            continue
        readings.append(
            SensorReading(
                component_id=f"disk:{_hw_key(node)}",
                kind=KIND_DISK,
                label=str(node.get("name") or _hw_key(node)),
                temperature_c=value,
                taken_at=now,
            )
        )
    return readings


def _disk_temp(node: dict) -> float | None:
    temps = _temp_sensors(node)
    if not temps:
        return None
    primary = next(
        (s for s in temps if s.get("name") in (SENSOR_TEMPERATURE, SENSOR_COMPOSITE_TEMPERATURE)),
        None,
    )
    return float(primary["value"] if primary else max(s["value"] for s in temps))


def _lhm_disk_kind(hw_key: str, name: str) -> str:
    if "/nvme/" in hw_key.lower() or "nvme" in name.lower():
        return KIND_NVME
    if "ssd" in name.lower():
        return KIND_SSD
    return KIND_HDD


def disk_summaries_from_tree(tree: list[dict]) -> list[DiskSummary]:
    storage = [n for n in _walk(tree) if n.get("type") == HW_STORAGE]
    kinds = [_lhm_disk_kind(_hw_key(n), str(n.get("name") or "")) for n in storage]
    per_kind: dict[str, int] = {}
    for kind in kinds:
        per_kind[kind] = per_kind.get(kind, 0) + 1
    counters: dict[str, int] = {}
    summaries: list[DiskSummary] = []
    for node, kind in zip(storage, kinds, strict=True):
        base = _KIND_LABEL[kind]
        if per_kind[kind] > 1:
            counters[kind] = counters.get(kind, 0) + 1
            label = f"{base}{counters[kind]}"
        else:
            label = base
        summaries.append(
            DiskSummary(
                label=label,
                health=None,
                temperature_c=_disk_temp(node),
                free_bytes=None,
                total_bytes=None,
                model=str(node.get("name")) if node.get("name") else None,
                kind=kind,
            )
        )
    return summaries


_computer = None
_last_error: str | None = None
_warned = False
_cpu_temps_missing = False
_runtime_load_attempted = False
_runtime_lock = threading.Lock()

HW_UPDATE_TIMEOUT_S = 5.0
_hw_update_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="lhm-hw-update"
)
_blacklisted_hw_ids: set[str] = set()
_blacklist_lock = threading.Lock()


def _ensure_runtime() -> None:
    global _runtime_load_attempted
    if _runtime_load_attempted:
        return
    with _runtime_lock:
        if _runtime_load_attempted:
            return
        _runtime_load_attempted = True
        try:
            import pythonnet  # noqa: PLC0415
        except ImportError:
            return
        try:
            pythonnet.load(os.environ.get("PYTHONNET_RUNTIME", "netfx"))
        except Exception:  # noqa: BLE001
            pass


def dll_candidates(configured: str = "") -> list[Path]:
    if configured:
        return [Path(configured)]
    candidates: list[Path] = []
    for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if not base:
            continue
        candidates.append(Path(base) / "sa-home-bot" / DLL_NAME)
        candidates.append(Path(base) / "LibreHardwareMonitor" / DLL_NAME)
    return candidates


def find_dll(configured: str = "") -> Path | None:
    for candidate in dll_candidates(configured):
        if candidate.is_file():
            return candidate
    return None


def _open_computer(dll: Path):
    global _computer
    if _computer is not None:
        return _computer
    _ensure_runtime()
    try:
        import clr
    except ImportError as exc:
        raise LhmUnavailable(
            "нет пакета pythonnet — переустановите ноду: pipx install "
            '"sa-home-bot[windows] @ git+…"'
        ) from exc
    sys.path.append(str(dll.parent))
    clr.AddReference(dll.stem)
    from LibreHardwareMonitor import Hardware  # noqa: PLC0415

    computer = Hardware.Computer()
    computer.IsCpuEnabled = True
    computer.IsStorageEnabled = True
    computer.Open()
    _computer = computer
    return computer


def _update_with_timeout(hw) -> None:
    hw_id = str(hw.Identifier)
    if hw_id in _blacklisted_hw_ids:
        return
    future = _hw_update_pool.submit(hw.Update)
    try:
        future.result(timeout=HW_UPDATE_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        with _blacklist_lock:
            _blacklisted_hw_ids.add(hw_id)
        log.error(
            "LHM: %s (%s) не ответил на Update() за %.0fс — исключаю из "
            "дальнейших сканов (похоже на зависшее съёмное устройство)",
            hw_id,
            hw.Name,
            HW_UPDATE_TIMEOUT_S,
        )


def _hardware_node(hw) -> dict:
    _update_with_timeout(hw)
    return {
        "id": str(hw.Identifier),
        "type": str(hw.HardwareType),
        "name": str(hw.Name),
        "sensors": [
            {
                "type": str(s.SensorType),
                "name": str(s.Name),
                "value": float(s.Value) if s.Value is not None else None,
            }
            for s in hw.Sensors
        ],
        "subhardware": [
            n for n in (_safe_hardware_node(sub) for sub in hw.SubHardware) if n is not None
        ],
    }


def _safe_hardware_node(hw) -> dict | None:
    try:
        return _hardware_node(hw)
    except Exception:  # noqa: BLE001
        log.warning("LHM: узел %s пропущен из-за ошибки", hw.Identifier, exc_info=True)
        return None


def read_tree_sync(dll_path: str = "") -> list[dict]:
    dll = find_dll(dll_path)
    if dll is None:
        looked = ", ".join(str(c) for c in dll_candidates(dll_path)) or "—"
        raise LhmUnavailable(
            f"не найден {DLL_NAME} (искал: {looked}) — скачайте LibreHardwareMonitor "
            "и/или укажите путь в [sensors.lhm].dll_path"
        )
    computer = _open_computer(dll)
    return [n for n in (_safe_hardware_node(hw) for hw in computer.Hardware) if n is not None]


def _safe_tree(dll_path: str) -> list[dict]:
    global _last_error, _warned
    try:
        tree = read_tree_sync(dll_path)
    except LhmUnavailable as exc:
        _last_error = str(exc)
    except Exception as exc:  # noqa: BLE001
        _last_error = f"ошибка LibreHardwareMonitor: {exc}"
    else:
        _last_error = None
        _warned = False
        return tree
    if not _warned:
        log.warning("Температуры через LHM недоступны: %s", _last_error)
        _warned = True
    return []


def read_cpu_readings_sync(dll_path: str, now: datetime) -> list[SensorReading]:
    global _cpu_temps_missing
    tree = _safe_tree(dll_path)
    readings = cpu_readings_from_tree(tree, now)
    _cpu_temps_missing = not readings and any(n.get("type") == HW_CPU for n in _walk(tree))
    return readings


def read_disk_readings_sync(dll_path: str, now: datetime) -> list[SensorReading]:
    return disk_readings_from_tree(_safe_tree(dll_path), now)


def read_disk_summaries_sync(dll_path: str) -> list[DiskSummary]:
    return disk_summaries_from_tree(_safe_tree(dll_path))


def lhm_problem(dll_path: str = "") -> dict | None:
    if sys.platform != "win32":
        return None
    hint: str | None = None
    _ensure_runtime()
    try:
        import clr  # noqa: F401
    except ImportError:
        hint = (
            "нет пакета pythonnet — переустановите ноду: pipx install "
            '"sa-home-bot[windows] @ git+…" — температуры CPU/дисков'
        )
    if hint is None and find_dll(dll_path) is None:
        hint = (
            f"не найден {DLL_NAME} — скачайте LibreHardwareMonitor и укажите "
            "[sensors.lhm].dll_path — температуры CPU/дисков"
        )
    if hint is None:
        hint = _last_error
    if hint is not None:
        return {"id": "lhm", "status": "missing_program", "hint": hint}
    if _cpu_temps_missing:
        return {
            "id": "lhm",
            "status": "needs_privilege",
            "hint": (
                "LHM не видит температур CPU — запустите ноду от имени "
                "администратора (драйвер датчиков требует прав)"
            ),
        }
    return None
