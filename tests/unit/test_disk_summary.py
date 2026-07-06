"""Сбор сводки по дискам: классификация SMART, разбор lsblk, метки."""

from __future__ import annotations

from sa_home_bot.domain.models import DISK_FAIL, DISK_OK, DISK_WARN
from sa_home_bot.sensors.disks import (
    DiskTarget,
    _extract_temp,
    _label_disks,
    health_args,
    parse_health,
    parse_lsblk_disks,
)


def _smart(passed, pending=0, uncorrectable=0, temp=None):
    data = {"smart_status": {"passed": passed}}
    table = []
    if pending is not None:
        table.append({"id": 197, "raw": {"value": pending}})
    if uncorrectable is not None:
        table.append({"id": 198, "raw": {"value": uncorrectable}})
    if temp is not None:
        data["temperature"] = {"current": temp}
    data["ata_smart_attributes"] = {"table": table}
    return data


def test_parse_health_ok():
    assert parse_health(_smart(True, pending=0, uncorrectable=0)) == DISK_OK


def test_parse_health_warning_on_pending():
    assert parse_health(_smart(True, pending=1, uncorrectable=0)) == DISK_WARN


def test_parse_health_warning_on_uncorrectable():
    assert parse_health(_smart(True, pending=0, uncorrectable=3)) == DISK_WARN


def test_parse_health_failed():
    assert parse_health(_smart(False)) == DISK_FAIL


def test_parse_health_unavailable():
    assert parse_health({}) is None  # нет smart_status


def test_extract_temp_prefers_temperature_block():
    assert _extract_temp({"temperature": {"current": 34}}) == 34.0


def test_extract_temp_fallback_attr194():
    data = {"ata_smart_attributes": {"table": [{"id": 194, "raw": {"value": 40}}]}}
    assert _extract_temp(data) == 40.0


def test_extract_temp_none():
    assert _extract_temp({}) is None


def test_health_args_includes_type_and_flags():
    args = health_args(DiskTarget("/dev/sda", "sat"))
    assert args == ["-d", "sat", "-j", "-H", "-A", "/dev/sda"]
    assert health_args(DiskTarget("/dev/sda", None)) == ["-j", "-H", "-A", "/dev/sda"]


# Форма реального `lsblk -J -b`: диски + служебные mmcblk*boot* + разделы.
LSBLK = {
    "blockdevices": [
        {
            "name": "sda", "type": "disk", "tran": "usb", "model": "ST9250315AS",
            "path": "/dev/sda",
            "children": [{"name": "sda1", "type": "part", "mountpoints": ["/mnt/scratch"]}],
        },
        {
            "name": "mmcblk0", "type": "disk", "tran": "mmc", "model": None,
            "path": "/dev/mmcblk0",
            "children": [
                {"name": "mmcblk0p2", "type": "part", "mountpoints": ["[SWAP]"]},
                {"name": "mmcblk0p3", "type": "part", "mountpoints": ["/"]},
            ],
        },
        {"name": "mmcblk0boot0", "type": "disk", "path": "/dev/mmcblk0boot0"},
    ]
}


def test_parse_lsblk_skips_boot_and_collects_mounts():
    disks = parse_lsblk_disks(LSBLK)
    names = {d.path for d in disks}
    assert names == {"/dev/sda", "/dev/mmcblk0"}  # boot0 отброшен
    sda = next(d for d in disks if d.path == "/dev/sda")
    assert sda.mountpoints == ("/mnt/scratch",)
    mmc = next(d for d in disks if d.path == "/dev/mmcblk0")
    assert mmc.mountpoints == ("/",)  # SWAP исключён
    assert mmc.is_mmc is True and sda.is_mmc is False


def test_label_disks_hdd_numbering_and_emmc():
    disks = parse_lsblk_disks(LSBLK)
    labels = dict((label, d.path) for label, d in _label_disks(disks))
    assert labels["HDD1"] == "/dev/sda"
    assert labels["eMMC"] == "/dev/mmcblk0"


def test_label_disks_emmc_comes_first():
    # eMMC без температуры (SMART недоступен) должна открывать список дисков
    # (в /status — сразу после CPU), а не оказываться в конце.
    disks = parse_lsblk_disks(LSBLK)
    ordered_labels = [label for label, _ in _label_disks(disks)]
    assert ordered_labels[0] == "eMMC"
