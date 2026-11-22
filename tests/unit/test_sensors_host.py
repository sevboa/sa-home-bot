import shutil
from datetime import UTC, datetime

import pytest

from sa_home_bot.config import Settings
from sa_home_bot.sensors import host
from sa_home_bot.sensors.host import (
    KERNEL_JOURNAL_REQUIREMENT,
    count_kernel_stalls,
    cpu_deltas,
    net_err_delta,
    parse_conntrack,
    parse_loadavg,
    parse_meminfo,
    parse_pressure_some_avg60,
    parse_proc_net_dev,
    parse_proc_stat_cpu,
    parse_vmstat,
    read_host_sync,
)
from sa_home_bot.utils.requirements import RequirementStatus, requirements_registry

_HAS_JOURNALCTL = shutil.which("journalctl") is not None

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

_STAT_1 = "cpu  100 0 50 800 20 0 5 10 0 0\ncpu0 50 0 25 400 10 0 2 5 0 0\n"
_STAT_2 = "cpu  140 0 60 900 25 0 5 55 0 0\ncpu0 70 0 30 450 12 0 2 30 0 0\n"

_MEMINFO = """MemTotal:        4000000 kB
MemFree:          200000 kB
MemAvailable:     800000 kB
SwapTotal:       1000000 kB
SwapFree:         750000 kB
"""

_PRESSURE = (
    "some avg10=0.10 avg60=3.42 avg300=1.00 total=123456\n"
    "full avg10=0.00 avg60=0.50 total=1\n"
)


def test_parse_proc_stat_cpu_reads_aggregate_line():
    parsed = parse_proc_stat_cpu(_STAT_1)
    assert parsed["steal"] == 10
    assert parsed["iowait"] == 20
    assert "guest" not in parsed


def test_cpu_deltas_steal_and_iowait_over_window():
    steal, iowait = cpu_deltas(parse_proc_stat_cpu(_STAT_1), parse_proc_stat_cpu(_STAT_2))
    assert steal == pytest.approx(22.5)
    assert iowait == pytest.approx(2.5)


def test_cpu_deltas_zero_window_is_safe():
    snap = parse_proc_stat_cpu(_STAT_1)
    assert cpu_deltas(snap, snap) == (0.0, 0.0)


def test_parse_meminfo_and_derived_percentages():
    mi = parse_meminfo(_MEMINFO)
    assert mi["MemAvailable"] == 800000
    assert mi.get("MemAvailable", 0) / mi["MemTotal"] * 100 == pytest.approx(20.0)
    used = mi["SwapTotal"] - mi["SwapFree"]
    assert used / mi["SwapTotal"] * 100 == pytest.approx(25.0)


def test_parse_loadavg():
    assert parse_loadavg("1.53 0.80 0.42 1/234 5678") == pytest.approx(1.53)
    assert parse_loadavg("") is None


def test_parse_pressure_some_avg60():
    assert parse_pressure_some_avg60(_PRESSURE) == pytest.approx(3.42)
    assert parse_pressure_some_avg60("full avg60=1.0 total=1") is None


def test_parse_vmstat_delta_for_oom():
    d1 = parse_vmstat("oom_kill 3\npswpin 10\n")
    d2 = parse_vmstat("oom_kill 5\npswpin 12\n")
    assert d2["oom_kill"] - d1["oom_kill"] == 2


_NETDEV_1 = """Inter-|   Receive   |  Transmit
 face |b p e d f fr c m|b p e d f co ca c
    lo: 100 2 0 0 0 0 0 0 100 2 0 0 0 0 0 0
  eth0: 5000 40 3 1 0 0 0 0 4000 30 0 2 0 0 0 0
  eth1: 10 1 100 100 0 0 0 0 10 1 50 50 0 0 0 0
"""
_NETDEV_2 = """Inter-|   Receive   |  Transmit
 face |b p e d f fr c m|b p e d f co ca c
    lo: 200 4 0 0 0 0 0 0 200 4 0 0 0 0 0 0
  eth0: 9000 80 5 2 0 0 0 0 8000 60 3 2 0 0 0 0
"""


def test_parse_proc_net_dev_skips_lo_and_headers():
    parsed = parse_proc_net_dev(_NETDEV_1)
    assert set(parsed) == {"eth0", "eth1"}
    assert parsed["eth0"] == (3, 1, 0, 2)


def test_net_err_delta_counts_errs_only_not_drops():
    prev = parse_proc_net_dev(_NETDEV_1)
    cur = parse_proc_net_dev(_NETDEV_2)
    assert net_err_delta(prev, cur) == 5


def test_net_err_delta_ignores_pure_drop_growth():
    prev = {"eth0": (0, 100, 0, 5)}
    cur = {"eth0": (0, 900, 0, 40)}
    assert net_err_delta(prev, cur) == 0


def test_net_err_delta_ignores_counter_reset():
    prev = {"eth0": (10, 10, 10, 10)}
    cur = {"eth0": (0, 0, 0, 0)}
    assert net_err_delta(prev, cur) == 0


def test_parse_conntrack_percentage_and_missing():
    assert parse_conntrack("32768", "131072") == pytest.approx(25.0)
    assert parse_conntrack("", "131072") is None
    assert parse_conntrack("10", "0") is None


@pytest.mark.parametrize(
    "line",
    [
        "INFO: task foo:123 blocked for more than 120 seconds.",
        "rcu: INFO: rcu_sched detected stalls on CPUs/tasks:",
        "clocksource: Marking clocksource 'tsc' as unstable",
        "watchdog: BUG: soft lockup - CPU#0 stuck for 22s!",
        "NETDEV WATCHDOG: eth0 (virtio_net): transmit queue 0 timed out",
        "virtio_net virtio0 eth0: TX timeout",
        "hung_task: blocked tasks",
    ],
)
def test_count_kernel_stalls_matches_known_patterns(line):
    assert count_kernel_stalls(f"normal line\n{line}\nanother normal line") == 1


def test_count_kernel_stalls_ignores_benign_lines():
    text = "Linux version 6.12\nsystemd[1]: Started foo.\nEXT4-fs (sda1): mounted\n"
    assert count_kernel_stalls(text) == 0


@pytest.mark.skipif(not _HAS_JOURNALCTL, reason="нужен journalctl в PATH")
def test_read_kernel_stall_count_needs_privilege_when_not_in_group(monkeypatch):
    requirements_registry.reset()
    monkeypatch.setattr(host, "_can_read_kernel_journal", lambda: False)
    assert host._read_kernel_stall_count(NOW, NOW) is None
    assert (
        requirements_registry.status_for(KERNEL_JOURNAL_REQUIREMENT)
        is RequirementStatus.NEEDS_PRIVILEGE
    )
    requirements_registry.reset()


@pytest.mark.skipif(not _HAS_JOURNALCTL, reason="нужен journalctl в PATH")
def test_read_kernel_stall_count_parses_journal_output(monkeypatch):
    requirements_registry.reset()
    monkeypatch.setattr(host, "_can_read_kernel_journal", lambda: True)

    class _Proc:
        returncode = 0
        stdout = "quiet\nrcu_sched detected stalls\nTX timeout\n"
        stderr = ""

    monkeypatch.setattr(host.subprocess, "run", lambda *a, **k: _Proc())
    assert host._read_kernel_stall_count(NOW, NOW) == 2
    requirements_registry.reset()


def test_read_host_sync_non_linux_returns_empty(monkeypatch):
    monkeypatch.setattr(host, "_read", lambda path: "")
    assert read_host_sync(NOW, 0.0) == []


def test_read_host_sync_builds_readings_from_proc(monkeypatch):
    files = {
        "/proc/stat": [_STAT_1, _STAT_2],
        "/proc/vmstat": ["oom_kill 0\n", "oom_kill 0\n"],
        "/proc/loadavg": "0.90 0.5 0.4 1/100 200",
        "/proc/meminfo": _MEMINFO,
        "/proc/pressure/cpu": _PRESSURE,
        "/proc/pressure/memory": "",
        "/proc/pressure/io": "",
    }
    calls: dict[str, int] = {}

    def fake_read(path: str) -> str:
        val = files.get(path, "")
        if isinstance(val, list):
            i = calls.get(path, 0)
            calls[path] = i + 1
            return val[min(i, len(val) - 1)]
        return val

    monkeypatch.setattr(host, "_read", fake_read)
    monkeypatch.setattr(host.time, "sleep", lambda _s: None)
    monkeypatch.setattr(host.os, "cpu_count", lambda: 2)
    monkeypatch.setattr(host, "_read_kernel_stall_count", lambda since, until: None)

    by_metric = {r.metric: r for r in read_host_sync(NOW, 5.0)}
    assert by_metric["steal_pct"].value == pytest.approx(22.5)
    assert by_metric["load_per_core"].value == pytest.approx(0.45)
    assert by_metric["mem_available_pct"].value == pytest.approx(20.0)
    assert by_metric["swap_used_pct"].value == pytest.approx(25.0)
    assert by_metric["psi_cpu"].value == pytest.approx(3.42)
    assert "psi_memory" not in by_metric
    assert "conntrack_pct" not in by_metric
    assert by_metric["steal_pct"].component_id == "host:steal_pct"
    assert by_metric["steal_pct"].unit == "%"


def test_read_host_sync_emits_net_conntrack_and_kernel_stalls(monkeypatch):
    files = {
        "/proc/stat": [_STAT_1, _STAT_2],
        "/proc/vmstat": ["", ""],
        "/proc/net/dev": [_NETDEV_1, _NETDEV_2],
        "/proc/sys/net/netfilter/nf_conntrack_count": "32768",
        "/proc/sys/net/netfilter/nf_conntrack_max": "131072",
    }
    calls: dict[str, int] = {}

    def fake_read(path: str) -> str:
        val = files.get(path, "")
        if isinstance(val, list):
            i = calls.get(path, 0)
            calls[path] = i + 1
            return val[min(i, len(val) - 1)]
        return val

    monkeypatch.setattr(host, "_read", fake_read)
    monkeypatch.setattr(host.time, "sleep", lambda _s: None)
    monkeypatch.setattr(host, "_read_kernel_stall_count", lambda since, until: 2)

    by_metric = {r.metric: r for r in read_host_sync(NOW, 60.0)}
    assert by_metric["conntrack_pct"].value == pytest.approx(25.0)
    assert by_metric["net_err_rate"].value == pytest.approx(5.0)
    assert by_metric["kernel_stall_events"].value == pytest.approx(2.0)
    assert by_metric["kernel_stall_events"].unit == "/скан"


def test_host_sensor_auto_enabled_on_vps(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[node]\nkind = "vps"\n', encoding="utf-8")
    s = Settings.load(cfg)
    assert s.sensors.host.enabled is True
    assert s.sensors.cpu.enabled is False
    assert s.sensors.disks.enabled is False


def test_host_sensor_off_by_default_on_server(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[node]\nkind = "server"\n', encoding="utf-8")
    s = Settings.load(cfg)
    assert s.sensors.host.enabled is False
    assert s.sensors.cpu.enabled is True


def test_explicit_host_and_cpu_flags_win_on_vps(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[node]\nkind = "vps"\n[sensors.host]\nenabled = false\n'
        "[sensors.cpu]\nenabled = true\n[sensors.disks]\nenabled = true\n",
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.sensors.host.enabled is False
    assert s.sensors.cpu.enabled is True
    assert s.sensors.disks.enabled is True


def test_host_threshold_override_in_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[node]\nkind = "vps"\n[sensors.host.thresholds.steal_pct]\nwarn = 5\ncrit = 15\n',
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.sensors.host.thresholds["steal_pct"].warn == 5


def test_cpu_busy_pct_excludes_idle_and_iowait():
    busy = host.cpu_busy_pct(parse_proc_stat_cpu(_STAT_1), parse_proc_stat_cpu(_STAT_2))
    assert busy == pytest.approx(100 * (200 - 100 - 5) / 200)
    snap = parse_proc_stat_cpu(_STAT_1)
    assert host.cpu_busy_pct(snap, snap) == 0.0


def test_parse_loadavgs_and_uptime():
    assert host.parse_loadavgs("1.5 0.8 0.4 1/2 3") == (1.5, 0.8, 0.4)
    assert host.parse_loadavgs("1.5") is None
    assert host.parse_uptime("123.45 99.0") == pytest.approx(123.45)
    assert host.parse_uptime("") is None


@pytest.mark.parametrize(
    ("name", "has_device", "expected"),
    [
        ("enp1s0", True, True),
        ("tailscale0", False, True),
        ("lo", True, False),
        ("veth12ab", True, False),
        ("vprobe0", True, False),
        ("docker0", True, False),
        ("br-1a2b", True, False),
        ("awg0", False, False),
    ],
)
def test_metric_iface(name, has_device, expected):
    assert host.metric_iface(name, has_device) is expected


def test_net_rates_per_interface_bytes_per_second():
    prev = host.parse_net_bytes(_NETDEV_1)
    cur = host.parse_net_bytes(_NETDEV_2)
    rates = host.net_rates(prev, cur, 10.0, lambda name: name != "lo")
    assert rates == {"eth0": (400.0, 400.0)}


def test_parse_mount_points_keeps_real_filesystems_under_root_and_mnt():
    text = (
        "/dev/mmcblk0p2 / ext4 rw 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        "/dev/mmcblk0p1 /boot/efi vfat rw 0 0\n"
        "/dev/sda1 /mnt/data ext4 rw 0 0\n"
        "/dev/sdb1 /mnt/my\\040disk ext4 rw 0 0\n"
        "/dev/sda1 /mnt/data ext4 rw 0 0\n"
        "/dev/loop0 /snap/x squashfs ro 0 0\n"
    )
    assert host.parse_mount_points(text) == ["/", "/mnt/data", "/mnt/my disk"]


def test_read_host_sync_emits_gauges_with_instances(monkeypatch):
    files = {
        "/proc/stat": _STAT_1,
        "/proc/net/dev": _NETDEV_1,
        "/proc/meminfo": _MEMINFO,
        "/proc/loadavg": "1.0 2.0 3.0 1/2 3",
        "/proc/uptime": "500.5 1.0",
        "/proc/mounts": "/dev/x /mnt/data ext4 rw 0 0\n",
    }
    monkeypatch.setattr(host, "_read", lambda path: files.get(path, ""))
    monkeypatch.setattr(host.time, "sleep", lambda _s: None)
    monkeypatch.setattr(host.os, "cpu_count", lambda: 2)
    fake_stat = type("S", (), {"f_blocks": 100, "f_bfree": 25})()
    monkeypatch.setattr(host.os, "statvfs", lambda path: fake_stat)
    monkeypatch.setattr(host, "_iface_has_device", lambda name: True)
    monkeypatch.setattr(host, "_count_processes", lambda: 42)
    monkeypatch.setattr(host, "_read_kernel_stall_count", lambda since, until: None)
    by_id = {r.component_id: r for r in read_host_sync(NOW, 5.0)}
    assert by_id["host:disk_pct:/mnt/data"].value == 75.0
    assert by_id["host:disk_pct:/mnt/data"].metric == "disk_pct"
    assert by_id["host:mem_used_pct"].value == 80.0
    assert by_id["host:mem_available_bytes"].value == 800000 * 1024
    assert (by_id["host:load5"].value, by_id["host:load15"].value) == (2.0, 3.0)
    assert by_id["host:uptime_s"].value == 500.5
    assert by_id["host:procs"].value == 42
    assert {"host:net_rx_bps:eth0", "host:net_tx_bps:eth1"} <= set(by_id)
    assert "host:cpu_busy_pct" in by_id
