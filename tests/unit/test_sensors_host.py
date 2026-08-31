"""Адаптер host-метрик VPS (sensors/host.py): парсинг /proc на фикстурах."""

from datetime import UTC, datetime

import pytest

from sa_home_bot.config import Settings
from sa_home_bot.sensors import host
from sa_home_bot.sensors.host import (
    cpu_deltas,
    parse_loadavg,
    parse_meminfo,
    parse_pressure_some_avg60,
    parse_proc_stat_cpu,
    parse_vmstat,
    read_host_sync,
)

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

    by_metric = {r.metric: r for r in read_host_sync(NOW, 5.0)}
    assert by_metric["steal_pct"].value == pytest.approx(22.5)
    assert by_metric["load_per_core"].value == pytest.approx(0.45)
    assert by_metric["mem_available_pct"].value == pytest.approx(20.0)
    assert by_metric["swap_used_pct"].value == pytest.approx(25.0)
    assert by_metric["psi_cpu"].value == pytest.approx(3.42)
    assert "psi_memory" not in by_metric  # пустой файл — метрику не эмитим
    assert by_metric["steal_pct"].component_id == "host:steal_pct"
    assert by_metric["steal_pct"].unit == "%"


def test_host_sensor_auto_enabled_on_vps(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[node]\nkind = "vps"\n', encoding="utf-8")
    s = Settings.load(cfg)
    assert s.sensors.host.enabled is True
    assert s.sensors.cpu.enabled is False  # coretemp виртуалки — бесполезен


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
        "[sensors.cpu]\nenabled = true\n",
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.sensors.host.enabled is False
    assert s.sensors.cpu.enabled is True


def test_host_threshold_override_in_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[node]\nkind = "vps"\n[sensors.host.thresholds.steal_pct]\nwarn = 5\ncrit = 15\n',
        encoding="utf-8",
    )
    s = Settings.load(cfg)
    assert s.sensors.host.thresholds["steal_pct"].warn == 5
