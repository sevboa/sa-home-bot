"""Адаптер host-метрик VPS (sensors/host.py): парсинг /proc на фикстурах."""

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
# +40 user, +10 system, +100 idle, +5 iowait, +45 steal → total delta 200
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
    assert "guest" not in parsed  # guest/guest_nice в сумму не берём


def test_cpu_deltas_steal_and_iowait_over_window():
    steal, iowait = cpu_deltas(parse_proc_stat_cpu(_STAT_1), parse_proc_stat_cpu(_STAT_2))
    assert steal == pytest.approx(22.5)  # 45 / 200 * 100
    assert iowait == pytest.approx(2.5)  # 5 / 200 * 100


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


# --- /proc/net/dev, conntrack, стойла ядра (этап 40) ---

# Поля после «iface:»: rx(bytes packets errs drop fifo frame compressed multicast)
#                      tx(bytes packets errs drop fifo colls carrier compressed)
_NETDEV_1 = """Inter-|   Receive   |  Transmit
 face |b p e d f fr c m|b p e d f co ca c
    lo: 100 2 0 0 0 0 0 0 100 2 0 0 0 0 0 0
  eth0: 5000 40 3 1 0 0 0 0 4000 30 0 2 0 0 0 0
  eth1: 10 1 100 100 0 0 0 0 10 1 50 50 0 0 0 0
"""
# eth1 исчезает во втором снимке; eth0: +2 rx_errs, +1 rx_drop, +3 tx_errs, +0 tx_drop
_NETDEV_2 = """Inter-|   Receive   |  Transmit
 face |b p e d f fr c m|b p e d f co ca c
    lo: 200 4 0 0 0 0 0 0 200 4 0 0 0 0 0 0
  eth0: 9000 80 5 2 0 0 0 0 8000 60 3 2 0 0 0 0
"""


def test_parse_proc_net_dev_skips_lo_and_headers():
    parsed = parse_proc_net_dev(_NETDEV_1)
    assert set(parsed) == {"eth0", "eth1"}
    assert parsed["eth0"] == (3, 1, 0, 2)  # rx_errs, rx_drop, tx_errs, tx_drop


def test_net_err_delta_sums_live_interfaces_only():
    prev = parse_proc_net_dev(_NETDEV_1)
    cur = parse_proc_net_dev(_NETDEV_2)
    # eth0: (5-3)+(2-1)+(3-0)+(2-2) = 6; eth1 исчез — не считаем
    assert net_err_delta(prev, cur) == 6


def test_net_err_delta_ignores_counter_reset():
    prev = {"eth0": (10, 10, 10, 10)}
    cur = {"eth0": (0, 0, 0, 0)}  # ребут интерфейса — счётчики обнулились
    assert net_err_delta(prev, cur) == 0


def test_parse_conntrack_percentage_and_missing():
    assert parse_conntrack("32768", "131072") == pytest.approx(25.0)
    assert parse_conntrack("", "131072") is None  # модуль не загружен — файла нет
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
    assert "psi_memory" not in by_metric  # пустой файл — метрику не эмитим
    assert "conntrack_pct" not in by_metric  # нет файлов nf_conntrack — не эмитим
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
    assert by_metric["net_err_rate"].value == pytest.approx(6.0)  # 6 ошибок за 60с окно
    assert by_metric["kernel_stall_events"].value == pytest.approx(2.0)
    assert by_metric["kernel_stall_events"].unit == "/скан"


def test_host_sensor_auto_enabled_on_vps(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[node]\nkind = "vps"\n', encoding="utf-8")
    s = Settings.load(cfg)
    assert s.sensors.host.enabled is True
    assert s.sensors.cpu.enabled is False  # coretemp виртуалки — бесполезен
    assert s.sensors.disks.enabled is False  # SMART у виртуального диска нет


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
