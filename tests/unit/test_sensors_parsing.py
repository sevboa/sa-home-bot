from collections import namedtuple

from sentinel_bot.sensors.cpu import parse_lm_sensors, parse_psutil_temps
from sentinel_bot.sensors.disks import parse_device_temp, parse_scan

from .conftest import BASE_TIME

Shw = namedtuple("Shw", ["label", "current", "high", "critical"])


def test_parse_psutil_temps():
    data = {
        "coretemp": [
            Shw("Package id 0", 55.0, 80.0, 100.0),
            Shw("Core 0", 50.0, 80.0, 100.0),
        ]
    }
    readings = parse_psutil_temps(data, BASE_TIME)
    assert len(readings) == 2
    pkg = readings[0]
    assert pkg.component_id == "cpu:coretemp:Package id 0"
    assert pkg.temperature_c == 55.0
    assert pkg.kind == "cpu"


def test_parse_psutil_skips_none_current():
    data = {"acpi": [Shw("", None, None, None)]}
    assert parse_psutil_temps(data, BASE_TIME) == []


def test_parse_lm_sensors():
    data = {
        "coretemp-isa-0000": {
            "Package id 0": {"temp1_input": 60.0, "temp1_max": 80.0},
        }
    }
    readings = parse_lm_sensors(data, BASE_TIME)
    assert len(readings) == 1
    assert readings[0].temperature_c == 60.0
    assert readings[0].label == "Package id 0"


def test_parse_scan():
    data = {"devices": [{"name": "/dev/sda"}, {"name": "/dev/nvme0"}, {}]}
    assert parse_scan(data) == ["/dev/sda", "/dev/nvme0"]


def test_parse_device_temp_from_temperature_block():
    data = {"model_name": "Samsung SSD", "temperature": {"current": 48}}
    reading = parse_device_temp("/dev/sda", data, BASE_TIME)
    assert reading.component_id == "disk:/dev/sda"
    assert reading.temperature_c == 48.0
    assert reading.label == "Samsung SSD"
    assert reading.kind == "disk"


def test_parse_device_temp_ata_attribute_fallback():
    data = {
        "ata_smart_attributes": {
            "table": [{"id": 194, "raw": {"value": 42}}],
        }
    }
    reading = parse_device_temp("/dev/sdb", data, BASE_TIME)
    assert reading.temperature_c == 42.0


def test_parse_device_temp_returns_none_when_absent():
    assert parse_device_temp("/dev/sdc", {"model_name": "x"}, BASE_TIME) is None
