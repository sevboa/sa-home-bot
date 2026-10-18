from __future__ import annotations

from sa_home_bot.domain.models import DISK_FAIL, DISK_OK, DISK_WARN
from sa_home_bot.sensors.disks import (
    KIND_NVME,
    BlockDisk,
    DiskTarget,
    _extract_temp,
    _label_disks,
    health_args,
    nvme_controller_path,
    parse_health,
    parse_lsblk_disks,
    read_disk_summaries_sync,
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
    assert parse_health({}) is None


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
            "path": "/dev/sdc",
        },
    ]
}


def test_parse_lsblk_skips_boot_and_collects_mounts():
    disks = parse_lsblk_disks(LSBLK)
    names = {d.path for d in disks}
    assert names == {"/dev/sda", "/dev/mmcblk0"}
    sda = next(d for d in disks if d.path == "/dev/sda")
    assert sda.mountpoints == ("/mnt/scratch",)
    mmc = next(d for d in disks if d.path == "/dev/mmcblk0")
    assert mmc.mountpoints == ("/",)
    assert mmc.kind == "emmc" and sda.kind == "hdd"


def test_parse_lsblk_kinds_nvme_ssd_hdd():
    kinds = {d.path: d.kind for d in parse_lsblk_disks(LSBLK_MIXED)}
    assert kinds == {
        "/dev/nvme0n1": "nvme",
        "/dev/sda": "ssd",
        "/dev/sdb": "hdd",
        "/dev/sdc": "hdd",
    }


def test_label_disks_single_of_kind_without_number():
    disks = parse_lsblk_disks(LSBLK)
    labels = dict((label, d.path) for label, d in _label_disks(disks))
    assert labels["HDD"] == "/dev/sda"
    assert labels["eMMC"] == "/dev/mmcblk0"


def test_label_disks_numbering_only_when_multiple():
    labels = [label for label, _ in _label_disks(parse_lsblk_disks(LSBLK_MIXED))]
    assert labels == ["NVMe", "SSD", "HDD1", "HDD2"]


def test_label_disks_emmc_comes_first():
    disks = parse_lsblk_disks(LSBLK)
    ordered_labels = [label for label, _ in _label_disks(disks)]
    assert ordered_labels[0] == "eMMC"


LSBLK_CARD_READER = {
    "blockdevices": [
        {
            "name": "sda", "type": "disk", "tran": "usb", "model": "Card Reader",
            "path": "/dev/sda", "rota": False, "size": 0,
        },
        {
            "name": "sdb", "type": "disk", "tran": "usb", "model": "Card Reader",
            "path": "/dev/sdb", "rota": False, "size": "0",
        },
        {
            "name": "sdc", "type": "disk", "tran": "usb", "model": "Samsung SD",
            "path": "/dev/sdc", "rota": False, "size": 32000000000,
        },
    ]
}


def test_parse_lsblk_skips_empty_card_reader_slots():
    disks = parse_lsblk_disks(LSBLK_CARD_READER)
    assert [d.path for d in disks] == ["/dev/sdc"]


def test_parse_lsblk_keeps_disk_without_size_field():
    disks = parse_lsblk_disks(LSBLK)
    assert {d.path for d in disks} == {"/dev/sda", "/dev/mmcblk0"}


def test_nvme_controller_path_strips_namespace_suffix():
    assert nvme_controller_path("/dev/nvme0n1") == "/dev/nvme0"
    assert nvme_controller_path("/dev/nvme1n2") == "/dev/nvme1"


def test_nvme_controller_path_none_for_non_nvme():
    assert nvme_controller_path("/dev/sda") is None
    assert nvme_controller_path("/dev/nvme0") is None


def test_read_disk_summaries_matches_nvme_controller_to_namespace_device(monkeypatch):
    from sa_home_bot.sensors import disks

    monkeypatch.setattr(
        disks, "discover_devices_sync", lambda: [DiskTarget("/dev/nvme0", "nvme")]
    )
    monkeypatch.setattr(
        disks, "_run_smartctl", lambda args: _smart(True, pending=0, uncorrectable=0, temp=43)
    )
    monkeypatch.setattr(
        disks,
        "_list_block_disks_sync",
        lambda: [BlockDisk(path="/dev/nvme0n1", kind=KIND_NVME, mountpoints=(), model="Samsung")],
    )

    summaries = read_disk_summaries_sync([])
    assert len(summaries) == 1
    assert summaries[0].temperature_c == 43.0
    assert summaries[0].health == DISK_OK


def test_parse_disk_summary_kind_fallback_for_old_monitor():
    from sa_home_bot.bot.monitor_state import parse_disk_summary

    old_hdd = parse_disk_summary({"label": "HDD1", "model": "X"})
    assert old_hdd.kind == "hdd"
    old_emmc = parse_disk_summary({"label": "eMMC"})
    assert old_emmc.kind == "emmc"
    new_nvme = parse_disk_summary({"label": "NVMe", "kind": "nvme"})
    assert new_nvme.kind == "nvme"
