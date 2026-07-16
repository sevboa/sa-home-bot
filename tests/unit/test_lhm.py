"""Датчики Windows через LibreHardwareMonitor: чистые парсеры дерева LHM.

pythonnet/dll в тестах не трогаем — парсеры работают с деревом простых
dict'ов (фикстуры), как smartctl-парсеры работают с JSON-фикстурами.
"""

import sys

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
