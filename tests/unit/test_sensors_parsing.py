from collections import namedtuple

from sa_home_bot.sensors.cpu import parse_lm_sensors, parse_psutil_temps
from sa_home_bot.sensors.disks import (
    DiskTarget,
    is_smart_capable,
    parse_device_spec,
    parse_device_temp,
    parse_scan,
    read_args,
)

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


def test_parse_scan_carries_device_type():
    data = {
        "devices": [
            {"name": "/dev/sda", "type": "sntjmicron"},
            {"name": "/dev/nvme0", "type": "nvme"},
            {"name": "/dev/sdb"},  # type отсутствует
            {},  # без name — пропускается
        ]
    }
    assert parse_scan(data) == [
        DiskTarget("/dev/sda", "sntjmicron"),
        DiskTarget("/dev/nvme0", "nvme"),
        DiskTarget("/dev/sdb", None),
    ]


def test_parse_device_spec_plain_and_typed():
    assert parse_device_spec("/dev/sda") == DiskTarget("/dev/sda", None)
    assert parse_device_spec("/dev/sda:sntjmicron") == DiskTarget("/dev/sda", "sntjmicron")
    assert parse_device_spec("  /dev/sdb : sat ") == DiskTarget("/dev/sdb", "sat")


def test_parse_device_spec_invalid():
    assert parse_device_spec("") is None
    assert parse_device_spec("   ") is None
    assert parse_device_spec(":sat") is None


def test_read_args_includes_device_type():
    assert read_args(DiskTarget("/dev/sda", "sntjmicron")) == [
        "-d",
        "sntjmicron",
        "-j",
        "-A",
        "/dev/sda",
    ]
    assert read_args(DiskTarget("/dev/sda", None)) == ["-j", "-A", "/dev/sda"]


def test_is_smart_capable_filters_emmc_and_friends():
    assert is_smart_capable("/dev/sda")
    assert is_smart_capable("/dev/nvme0")
    assert not is_smart_capable("/dev/mmcblk0")
    assert not is_smart_capable("/dev/loop0")
    assert not is_smart_capable("/dev/sr0")


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


def test_read_disks_sync_threads_type_and_isolates_failures(monkeypatch):
    from sa_home_bot.sensors import disks

    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(args)
        if "/dev/sda" in args:
            return {"model_name": "USB SSD", "temperature": {"current": 47}}
        if "/dev/sdb" in args:
            return None  # больной диск: таймаут/ошибка чтения
        return None

    monkeypatch.setattr(disks, "_run_smartctl", fake_run)

    specs = ["/dev/sda:sntjmicron", "/dev/sdb:sat", "/dev/mmcblk0"]
    readings = disks.read_disks_sync(specs, BASE_TIME)

    # eMMC не опрашивается вовсе; sda даёт показание, сбой sdb не роняет sda.
    assert ["-d", "sntjmicron", "-j", "-A", "/dev/sda"] in calls
    assert ["-d", "sat", "-j", "-A", "/dev/sdb"] in calls
    assert not any("/dev/mmcblk0" in c for c in calls)
    assert len(readings) == 1
    assert readings[0].component_id == "disk:/dev/sda"
    assert readings[0].temperature_c == 47.0


def test_read_disks_sync_autoscans_when_no_specs(monkeypatch):
    from sa_home_bot.sensors import disks

    monkeypatch.setattr(
        disks,
        "discover_devices_sync",
        lambda: [DiskTarget("/dev/sda", "sntjmicron")],
    )
    monkeypatch.setattr(
        disks,
        "_run_smartctl",
        lambda args: {"model_name": "Disk", "temperature": {"current": 40}},
    )

    readings = disks.read_disks_sync([], BASE_TIME)
    assert len(readings) == 1
    assert readings[0].temperature_c == 40.0
