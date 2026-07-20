"""Источник температур на Windows: LibreHardwareMonitor (LHM).

На Windows `psutil.sensors_temperatures()` не существует, а WMI на десктопах
обычно пуст — температуры CPU и дисков читаем через LibreHardwareMonitorLib.dll
(pythonnet), решение этапа 19 от 2026-07-10. DLL и объект ``Computer``
загружаются один раз на процесс (инициализация дорогая), на каждый срез —
только ``Update()``.

Чистые парсеры работают с деревом простых dict'ов (см. ``_hardware_node``) —
pythonnet нужен только на реальной Windows, тесты гоняются на фикстурах на
любой ОС. Блокирующие вызовы делает вызывающий код через run_in_executor
(инвариант ARCHITECTURE.md §9.6).
"""

from __future__ import annotations

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

# Имена HardwareType/SensorType из LHM (str(enum) даёт имя члена).
HW_CPU = "Cpu"
HW_STORAGE = "Storage"
SENSOR_TEMPERATURE = "Temperature"
SENSOR_COMPOSITE_TEMPERATURE = "Composite Temperature"  # основной сенсор NVMe в LHM

_KIND_LABEL = {KIND_HDD: "HDD", KIND_SSD: "SSD", KIND_NVME: "NVMe"}

# Сенсоры типа Temperature, которые НЕ являются показаниями — это настроенные
# LHM пороги (у NVMe: 80°C и там, и там), max()-fallback иначе принимает их
# за самое горячее реальное показание (живой баг 2026-07-17: 55°C → 80°C).
_TEMP_THRESHOLD_NAMES = {"Warning Temperature", "Critical Temperature"}


class LhmUnavailable(Exception):
    """LHM недоступен (нет pythonnet / dll / ошибка .NET) — причина в тексте."""


# --- Чистые парсеры (тестируются на фикстурах) ---


def _walk(tree: list[dict]):
    """Все узлы дерева hardware, включая subhardware (в глубину)."""
    for node in tree:
        yield node
        yield from _walk(node.get("subhardware", []))


def _temp_sensors(node: dict) -> list[dict]:
    """Температурные сенсоры с правдоподобным значением: None и ≤0°C — мусор
    (без прав администратора LHM не грузит драйвер и отдаёт нули)."""
    return [
        s
        for s in node.get("sensors", [])
        if s.get("type") == SENSOR_TEMPERATURE
        and (s.get("value") or 0) > 0
        and s.get("name") not in _TEMP_THRESHOLD_NAMES
    ]


def _hw_key(node: dict) -> str:
    """Стабильный ключ железки: LHM Identifier ("/amdcpu/0", "/nvme/1")."""
    return str(node.get("id") or node.get("name") or "?")


def cpu_readings_from_tree(tree: list[dict], now: datetime) -> list[SensorReading]:
    """Температуры CPU из дерева LHM — по одной записи на сенсор (как на
    Linux psutil даёт по записи на ядро/package)."""
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
    """Температуры дисков — одна запись на физический диск.

    У NVMe LHM отдаёт несколько температурных сенсоров ("Composite
    Temperature", "Temperature #1/2", плюс пороги "Warning/Critical
    Temperature" — не показания, отфильтрованы в `_temp_sensors`):
    приоритет основному "Temperature"/"Composite Temperature", иначе
    максимум остальных — для алертов интересен самый горячий датчик.
    """
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
    """Вид носителя по LHM Identifier и имени (модели).

    Точной классификации LHM не даёт: NVMe виден по identifier ("/nvme/N"),
    SATA SSD — только эвристикой по модели, остальное считаем HDD.
    """
    if "/nvme/" in hw_key.lower() or "nvme" in name.lower():
        return KIND_NVME
    if "ssd" in name.lower():
        return KIND_SSD
    return KIND_HDD


def disk_summaries_from_tree(tree: list[dict]) -> list[DiskSummary]:
    """Сводка по дискам для /status из дерева LHM (Windows-путь).

    SMART-здоровье и свободное место пока None: health придёт со SmartScanJob
    (smartctl для Windows), а сопоставление физический диск ↔ буквы томов
    требует WMI — следующая итерация. Метки — как на Linux: номер только
    когда дисков одного вида больше одного.
    """
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


# --- Обвязка pythonnet (работает только на Windows) ---

_computer = None  # кэш LHM Computer: Open() дорогой, живёт до конца процесса
_last_error: str | None = None  # последняя причина недоступности (для requirements)
_warned = False  # предупреждение в лог — один раз, не каждым сканом
_cpu_temps_missing = False  # CPU в дереве есть, температур нет — типично «не админ»
_runtime_load_attempted = False  # pythonnet.load() вызван (успешно или нет) — не повторяем
_runtime_lock = threading.Lock()
# LHM не thread-safe: CPU и диски читаются из разных потоков executor'а
# (SensorSource.read_all → asyncio.gather) плюс отдельно из SmartScanJob —
# конкурентный hw.Update() на общем _computer ронял LHM с
# IndexOutOfRangeException внутри StorageDevice.ToggleSpaceSensors()
# (живой баг 2026-07-20). Сериализуем весь проход по дереву датчиков.
_computer_lock = threading.Lock()


def _ensure_runtime() -> None:
    """Явно загрузить .NET-runtime ДО первого ``import clr`` — РОВНО ОДИН РАЗ.

    Автозагрузка pythonnet на живой машине (2026-07-17) подняла runtime не
    полностью — ``clr`` остался пустышкой без ``AddReference``; с явным
    "netfx" работает. PYTHONNET_RUNTIME уважается.

    Живой баг 2026-07-17 (второй заход): вызывался без защиты от повторного/
    параллельного вызова — ``lhm_problem()`` (requirements-проверка в
    ``get_state()``, event loop) и реальное чтение датчиков (sensor_scan,
    executor-поток) оба звали ``_ensure_runtime()``, и при совпадении по
    времени ДВА потока одновременно инициализировали pythonnet — гонка
    ломала ``clr`` ещё сильнее (`no attribute '_add_pending_namespaces'`,
    внутренняя структура CLR-хостинга, не просто "AddReference отсутствует").
    Инициализация .NET-рантайма в принципе рассчитана на один раз за
    процесс — лочим и не повторяем, даже если первая попытка не удалась
    (повтор только усугубляет, не чинит).
    """
    global _runtime_load_attempted
    if _runtime_load_attempted:
        return
    with _runtime_lock:
        if _runtime_load_attempted:  # ждали лок, пока другой поток уже попробовал
            return
        _runtime_load_attempted = True
        try:
            import pythonnet  # noqa: PLC0415 — ставится только на Windows
        except ImportError:
            return  # нет pythonnet — import clr скажет об этом понятной подсказкой
        try:
            pythonnet.load(os.environ.get("PYTHONNET_RUNTIME", "netfx"))
        except Exception:  # noqa: BLE001
            pass


def dll_candidates(configured: str = "") -> list[Path]:
    """Где искать LibreHardwareMonitorLib.dll, если путь не задан в конфиге."""
    if configured:
        return [Path(configured)]
    candidates: list[Path] = []
    for env in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = os.environ.get(env)
        if not base:
            continue
        # куда кладёт deploy/install-node.ps1 / рядом с обычной установкой LHM
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
        import clr  # pythonnet; ставится только на Windows
    except ImportError as exc:
        raise LhmUnavailable(
            "нет пакета pythonnet — переустановите ноду: pipx install "
            '"sa-home-bot[windows] @ git+…"'
        ) from exc
    sys.path.append(str(dll.parent))
    clr.AddReference(dll.stem)
    from LibreHardwareMonitor import Hardware  # noqa: PLC0415 — появляется после AddReference

    computer = Hardware.Computer()
    computer.IsCpuEnabled = True
    computer.IsStorageEnabled = True
    computer.Open()
    _computer = computer
    return computer


def _hardware_node(hw) -> dict:
    """LHM IHardware → дерево простых dict'ов (формат чистых парсеров выше)."""
    hw.Update()
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
        "subhardware": [_hardware_node(sub) for sub in hw.SubHardware],
    }


def read_tree_sync(dll_path: str = "") -> list[dict]:
    """Снять срез дерева датчиков LHM (блокирующе, через executor вызывающего)."""
    dll = find_dll(dll_path)
    if dll is None:
        looked = ", ".join(str(c) for c in dll_candidates(dll_path)) or "—"
        raise LhmUnavailable(
            f"не найден {DLL_NAME} (искал: {looked}) — скачайте LibreHardwareMonitor "
            "и/или укажите путь в [sensors.lhm].dll_path"
        )
    with _computer_lock:
        computer = _open_computer(dll)
        return [_hardware_node(hw) for hw in computer.Hardware]


def _safe_tree(dll_path: str) -> list[dict]:
    """Дерево LHM или пустой срез: причина запоминается для requirements,
    в лог — одним предупреждением, не каждым сканом."""
    global _last_error, _warned
    try:
        tree = read_tree_sync(dll_path)
    except LhmUnavailable as exc:
        _last_error = str(exc)
    except Exception as exc:  # noqa: BLE001 — .NET/interop не должен ронять скан
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
    """Проблема LHM в формате requirements монитора (``{id,status,hint}``).

    None — всё в порядке или не Windows (на Linux LHM не нужен). Формат — как
    у ``RequirementRegistry.problem_for``, но LHM — не программа в PATH
    (dll + pythonnet), поэтому диагностика своя.
    """
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
        hint = _last_error  # ошибка последнего реального чтения (если была)
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
