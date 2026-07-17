"""Датчики Windows через LibreHardwareMonitor: чистые парсеры дерева LHM.

pythonnet/dll в тестах не трогаем — парсеры работают с деревом простых
dict'ов (фикстуры), как smartctl-парсеры работают с JSON-фикстурами.
"""

import sys
import types

from sa_home_bot.sensors import lhm
from sa_home_bot.sensors.lhm import (
    cpu_readings_from_tree,
    disk_readings_from_tree,
    disk_summaries_from_tree,
    dll_candidates,
    find_dll,
    lhm_problem,
)

from .conftest import BASE_TIME


def _cpu_node(**overrides):
    node = {
        "id": "/amdcpu/0",
        "type": "Cpu",
        "name": "AMD Ryzen 7 5800X",
        "sensors": [
            {"type": "Temperature", "name": "Core (Tctl/Tdie)", "value": 61.5},
            {"type": "Temperature", "name": "CCD1 (Tdie)", "value": 58.0},
            {"type": "Load", "name": "CPU Total", "value": 12.0},
            {"type": "Temperature", "name": "Сломанный", "value": None},
        ],
        "subhardware": [],
    }
    node.update(overrides)
    return node


def _nvme_node():
    return {
        "id": "/nvme/0",
        "type": "Storage",
        "name": "Samsung SSD 980 PRO 1TB",
        "sensors": [
            {"type": "Temperature", "name": "Temperature", "value": 45.0},
            {"type": "Temperature", "name": "Temperature 1", "value": 52.0},
            {"type": "Temperature", "name": "Temperature 2", "value": 60.0},
            {"type": "Level", "name": "Used Space", "value": 70.0},
        ],
        "subhardware": [],
    }


def _hdd_node():
    return {
        "id": "/hdd/0",
        "type": "Storage",
        "name": "WDC WD40EZRZ",
        "sensors": [{"type": "Temperature", "name": "Temperature", "value": 38.0}],
        "subhardware": [],
    }


def test_cpu_readings():
    readings = cpu_readings_from_tree([_cpu_node()], BASE_TIME)
    assert [r.component_id for r in readings] == [
        "cpu:/amdcpu/0:Core (Tctl/Tdie)",
        "cpu:/amdcpu/0:CCD1 (Tdie)",
    ]
    assert readings[0].kind == "cpu"
    assert readings[0].label == "Core (Tctl/Tdie)"
    assert readings[0].temperature_c == 61.5
    assert readings[0].taken_at == BASE_TIME


def test_cpu_readings_ignore_storage_and_other_sensor_types():
    tree = [_cpu_node(), _nvme_node()]
    readings = cpu_readings_from_tree(tree, BASE_TIME)
    assert all(r.component_id.startswith("cpu:") for r in readings)
    assert len(readings) == 2  # Load и value=None отфильтрованы


def test_cpu_readings_from_subhardware():
    board = {
        "id": "/motherboard",
        "type": "Motherboard",
        "name": "B550",
        "sensors": [],
        "subhardware": [_cpu_node()],
    }
    assert len(cpu_readings_from_tree([board], BASE_TIME)) == 2


def test_disk_readings_prefer_primary_temperature():
    readings = disk_readings_from_tree([_nvme_node(), _hdd_node()], BASE_TIME)
    assert len(readings) == 2
    nvme = readings[0]
    # приоритет основному сенсору "Temperature", а не максимуму (60.0)
    assert nvme.component_id == "disk:/nvme/0"
    assert nvme.temperature_c == 45.0
    assert nvme.label == "Samsung SSD 980 PRO 1TB"
    assert nvme.kind == "disk"


def test_disk_readings_max_fallback_without_primary():
    node = _nvme_node()
    node["sensors"] = [
        {"type": "Temperature", "name": "Temperature 1", "value": 52.0},
        {"type": "Temperature", "name": "Temperature 2", "value": 60.0},
    ]
    readings = disk_readings_from_tree([node], BASE_TIME)
    assert readings[0].temperature_c == 60.0


def test_disk_without_temp_sensors_skipped():
    node = _hdd_node()
    node["sensors"] = []
    assert disk_readings_from_tree([node], BASE_TIME) == []


def test_disk_reading_uses_composite_and_ignores_threshold_sensors():
    # Живой баг 2026-07-17: реальные имена LHM для NVMe — "Composite
    # Temperature" (не "Temperature") плюс пороги Warning/Critical (те же
    # 80°C, что и предел устройства) — max()-fallback путал их с показаниями.
    node = _nvme_node()
    node["sensors"] = [
        {"type": "Temperature", "name": "Composite Temperature", "value": 55.0},
        {"type": "Temperature", "name": "Temperature #1", "value": 54.85},
        {"type": "Temperature", "name": "Temperature #2", "value": 60.85},
        {"type": "Temperature", "name": "Warning Temperature", "value": 80.0},
        {"type": "Temperature", "name": "Critical Temperature", "value": 80.0},
    ]
    readings = disk_readings_from_tree([node], BASE_TIME)
    assert readings[0].temperature_c == 55.0


def test_disk_summaries_kinds_and_labels():
    hdd2 = _hdd_node()
    hdd2["id"] = "/hdd/1"
    hdd2["name"] = "ST4000DM004"
    sata_ssd = _hdd_node()
    sata_ssd["id"] = "/hdd/2"
    sata_ssd["name"] = "Crucial MX500 SSD"
    summaries = disk_summaries_from_tree([_nvme_node(), _hdd_node(), hdd2, sata_ssd])
    by_label = {s.label: s for s in summaries}
    # единственный NVMe и SSD — без номера; HDD два — нумеруются
    assert set(by_label) == {"NVMe", "HDD1", "HDD2", "SSD"}
    assert by_label["NVMe"].kind == "nvme"
    assert by_label["SSD"].kind == "ssd"
    assert by_label["NVMe"].temperature_c == 45.0
    assert by_label["NVMe"].model == "Samsung SSD 980 PRO 1TB"
    # health/место пока не определяются через LHM
    assert by_label["NVMe"].health is None
    assert by_label["NVMe"].free_bytes is None


def test_dll_candidates_configured_wins(tmp_path):
    dll = tmp_path / "LibreHardwareMonitorLib.dll"
    assert dll_candidates(str(dll)) == [dll]
    assert find_dll(str(dll)) is None
    dll.write_bytes(b"")
    assert find_dll(str(dll)) == dll


def test_lhm_problem_none_outside_windows():
    assert sys.platform != "win32"
    assert lhm_problem() is None


def test_lhm_problem_on_windows_without_pythonnet(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    problem = lhm_problem()
    assert problem is not None
    assert problem["id"] == "lhm"
    assert "pythonnet" in problem["hint"]


def test_safe_read_returns_empty_and_remembers_error(monkeypatch):
    # dll не найден → пустой срез, причина доступна для requirements
    monkeypatch.setattr(lhm, "find_dll", lambda configured="": None)
    assert lhm.read_cpu_readings_sync("", BASE_TIME) == []
    assert "LibreHardwareMonitorLib.dll" in (lhm._last_error or "")


def test_zero_and_negative_temperatures_filtered():
    # LHM без прав администратора отдаёт нули — это не «CPU замёрз»
    node = _cpu_node()
    node["sensors"] = [
        {"type": "Temperature", "name": "Core", "value": 0.0},
        {"type": "Temperature", "name": "CCD1", "value": -1.0},
    ]
    assert cpu_readings_from_tree([node], BASE_TIME) == []
    disk = _hdd_node()
    disk["sensors"] = [{"type": "Temperature", "name": "Temperature", "value": 0.0}]
    assert disk_readings_from_tree([disk], BASE_TIME) == []


def test_ensure_runtime_loads_netfx_once(monkeypatch):
    monkeypatch.setattr(lhm, "_runtime_load_attempted", False)
    calls: list[str] = []
    fake = types.SimpleNamespace(load=lambda runtime: calls.append(runtime))
    monkeypatch.setitem(sys.modules, "pythonnet", fake)
    monkeypatch.delenv("PYTHONNET_RUNTIME", raising=False)
    lhm._ensure_runtime()
    assert calls == ["netfx"]


def test_ensure_runtime_does_not_reload_on_second_call(monkeypatch):
    # Живой баг 2026-07-17: lhm_problem() (event loop) и реальное чтение
    # датчиков (executor-поток) оба звали _ensure_runtime() — без защиты от
    # повторного вызова конкурентная повторная загрузка ломала pythonnet
    # ещё сильнее ('_add_pending_namespaces'), а не просто не помогала.
    monkeypatch.setattr(lhm, "_runtime_load_attempted", False)
    calls: list[str] = []
    fake = types.SimpleNamespace(load=lambda runtime: calls.append(runtime))
    monkeypatch.setitem(sys.modules, "pythonnet", fake)
    lhm._ensure_runtime()
    monkeypatch.setenv("PYTHONNET_RUNTIME", "coreclr")  # даже смена env не триггерит повтор
    lhm._ensure_runtime()
    assert calls == ["netfx"]  # второго вызова load() не было


def test_ensure_runtime_sticky_after_failed_load(monkeypatch):
    # Неудачная попытка тоже помечается — повтор не чинит, только рискует
    # гонкой при параллельном вызове.
    monkeypatch.setattr(lhm, "_runtime_load_attempted", False)
    calls: list[str] = []

    def failing_load(runtime):
        calls.append(runtime)
        raise RuntimeError("already loaded a different runtime")

    fake = types.SimpleNamespace(load=failing_load)
    monkeypatch.setitem(sys.modules, "pythonnet", fake)
    lhm._ensure_runtime()
    lhm._ensure_runtime()
    assert calls == ["netfx"]  # вторая попытка не предпринималась


def test_lhm_problem_hints_admin_when_cpu_has_no_temps(monkeypatch):
    # дерево читается, CPU есть, температур нет → подсказка про администратора
    node = _cpu_node()
    node["sensors"] = [{"type": "Temperature", "name": "Core", "value": 0.0}]
    monkeypatch.setattr(lhm, "read_tree_sync", lambda dll_path="": [node])
    assert lhm.read_cpu_readings_sync("", BASE_TIME) == []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "clr", types.ModuleType("clr"))
    monkeypatch.setattr(lhm, "find_dll", lambda configured="": object())
    problem = lhm.lhm_problem()
    assert problem is not None
    assert problem["status"] == "needs_privilege"
    assert "администратора" in problem["hint"]
    # с настоящими температурами подсказка уходит
    monkeypatch.setattr(lhm, "read_tree_sync", lambda dll_path="": [_cpu_node()])
    assert lhm.read_cpu_readings_sync("", BASE_TIME) != []
    assert lhm.lhm_problem() is None
