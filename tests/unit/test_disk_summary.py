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
# ROTA у современного lsblk — bool; sda/sdb — вращающиеся USB-HDD.
LSBLK = {
    "blockdevices": [
        {
            "name": "sda", "type": "disk", "tran": "usb", "model": "ST9250315AS",
            "path": "/dev/sda", "rota": True,
            "children": [{"name": "sda1", "type": "part", "mountpoints": ["/mnt/scratch"]}],
        },
        {
            "name": "mmcblk0", "type": "disk", "tran": "mmc", "model": None,
            "path": "/dev/mmcblk0", "rota": False,
            "children": [
                {"name": "mmcblk0p2", "type": "part", "mountpoints": ["[SWAP]"]},
                {"name": "mmcblk0p3", "type": "part", "mountpoints": ["/"]},
            ],
        },
        {"name": "mmcblk0boot0", "type": "disk", "path": "/dev/mmcblk0boot0"},
    ]
}

# Ноутбук с NVMe (arch-t480) + SATA SSD со строковым ROTA (старый формат lsblk).
LSBLK_MIXED = {
    "blockdevices": [
        {
            "name": "nvme0n1", "type": "disk", "tran": "nvme", "model": "SAMSUNG 970",
            "path": "/dev/nvme0n1", "rota": False,
        },
        {
            "name": "sda", "type": "disk", "tran": "sata", "model": "Crucial MX500",
            "path": "/dev/sda", "rota": "0",
        },
        {
            "name": "sdb", "type": "disk", "tran": "usb", "model": "WD Blue",
            "path": "/dev/sdb", "rota": "1",
        },
        {
            "name": "sdc", "type": "disk", "tran": "usb", "model": "Hitachi",
            "path": "/dev/sdc",  # без ROTA (старый lsblk) → считаем HDD
        },
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
    assert mmc.kind == "emmc" and sda.kind == "hdd"


def test_parse_lsblk_kinds_nvme_ssd_hdd():
    kinds = {d.path: d.kind for d in parse_lsblk_disks(LSBLK_MIXED)}
    assert kinds == {
        "/dev/nvme0n1": "nvme",
        "/dev/sda": "ssd",  # строковый ROTA "0"
        "/dev/sdb": "hdd",  # строковый ROTA "1"
        "/dev/sdc": "hdd",  # ROTA отсутствует — деградация к hdd
    }


def test_label_disks_single_of_kind_without_number():
    disks = parse_lsblk_disks(LSBLK)
    labels = dict((label, d.path) for label, d in _label_disks(disks))
    # По одному диску каждого вида — метки без номера.
    assert labels["HDD"] == "/dev/sda"
    assert labels["eMMC"] == "/dev/mmcblk0"


def test_label_disks_numbering_only_when_multiple():
    labels = [label for label, _ in _label_disks(parse_lsblk_disks(LSBLK_MIXED))]
    # NVMe и SSD по одному — без номера; HDD два — HDD1/HDD2.
    assert labels == ["NVMe", "SSD", "HDD1", "HDD2"]


def test_label_disks_emmc_comes_first():
    # eMMC без температуры (SMART недоступен) должна открывать список дисков
    # (в /status — сразу после CPU), а не оказываться в конце.
    disks = parse_lsblk_disks(LSBLK)
    ordered_labels = [label for label, _ in _label_disks(disks)]
    assert ordered_labels[0] == "eMMC"


def test_parse_disk_summary_kind_fallback_for_old_monitor():
    # Ответ монитора старой версии (без поля kind) — бот не падает: eMMC
    # выводится из метки, остальное деградирует к hdd.
    from sa_home_bot.bot.monitor_state import parse_disk_summary

    old_hdd = parse_disk_summary({"label": "HDD1", "model": "X"})
    assert old_hdd.kind == "hdd"
    old_emmc = parse_disk_summary({"label": "eMMC"})
    assert old_emmc.kind == "emmc"
    new_nvme = parse_disk_summary({"label": "NVMe", "kind": "nvme"})
    assert new_nvme.kind == "nvme"
