"""Адаптер host-метрик VPS: читает /proc и statvfs, без smartctl и psutil.

Всё дёшево, кроме двух снимков /proc/stat и /proc/vmstat с паузой между ними
(нужна дельта за окно для steal/iowait/swap-io/oom) — вызов идёт из executor
(`SensorSource.read_host`), поэтому пауза на воркер-потоке приемлема.

Парсинг вынесен в чистые функции — тестируются на фикстурах без реального /proc.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from sa_home_bot.domain.host import METRICS, HostMetricReading

log = logging.getLogger(__name__)

# Поля первой строки /proc/stat (jiffies), в порядке ядра. guest/guest_nice уже
# входят в user/nice — в сумму «всего времени» их не включаем (двойной счёт).
_STAT_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def parse_proc_stat_cpu(text: str) -> dict[str, int]:
    """Агрегированная строка ``cpu`` из /proc/stat → {поле: jiffies}."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            nums = [int(x) for x in line.split()[1:]]
            return {name: nums[i] for i, name in enumerate(_STAT_FIELDS) if i < len(nums)}
    return {}


def cpu_deltas(prev: dict[str, int], cur: dict[str, int]) -> tuple[float, float]:
    """(steal_pct, iowait_pct) за интервал между двумя снимками /proc/stat."""
    total = sum(cur.get(k, 0) - prev.get(k, 0) for k in _STAT_FIELDS)
    if total <= 0:
        return 0.0, 0.0
    steal = (cur.get("steal", 0) - prev.get("steal", 0)) / total * 100
    iowait = (cur.get("iowait", 0) - prev.get("iowait", 0)) / total * 100
    return max(0.0, steal), max(0.0, iowait)


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo → {ключ: значение в kB}."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        rest = rest.strip().split()
        if rest:
            try:
                out[key.strip()] = int(rest[0])
            except ValueError:
                continue
    return out


def parse_loadavg(text: str) -> float | None:
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def parse_pressure_some_avg60(text: str) -> float | None:
    """`some avg60=...` из /proc/pressure/{cpu,memory,io} — доля времени в %."""
    for line in text.splitlines():
        if line.startswith("some"):
            for tok in line.split():
                if tok.startswith("avg60="):
                    try:
                        return float(tok.split("=", 1)[1])
                    except ValueError:
                        return None
    return None


def parse_vmstat(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out


def _reading(metric: str, value: float, now: datetime) -> HostMetricReading:
    spec = METRICS[metric]
    return HostMetricReading(
        component_id=f"host:{metric}",
        metric=metric,
        label=spec.label,
        value=round(value, 2),
        unit=spec.unit,
        taken_at=now,
    )


def read_host_sync(now: datetime, sample_window_s: float = 10.0) -> list[HostMetricReading]:
    """Снять срез host-метрик (блокирующе). Не-Linux / нет /proc → пустой список."""
    stat1 = _read("/proc/stat")
    vm1 = _read("/proc/vmstat")
    if not stat1:
        return []
    time.sleep(max(0.0, sample_window_s))
    stat2 = _read("/proc/stat")
    vm2 = _read("/proc/vmstat")

    out: list[HostMetricReading] = []

    steal, iowait = cpu_deltas(parse_proc_stat_cpu(stat1), parse_proc_stat_cpu(stat2))
    out.append(_reading("steal_pct", steal, now))
    out.append(_reading("iowait_pct", iowait, now))

    load1 = parse_loadavg(_read("/proc/loadavg"))
    if load1 is not None:
        out.append(_reading("load_per_core", load1 / (os.cpu_count() or 1), now))

    mi = parse_meminfo(_read("/proc/meminfo"))
    if mi.get("MemTotal"):
        out.append(
            _reading("mem_available_pct", mi.get("MemAvailable", 0) / mi["MemTotal"] * 100, now)
        )
    if mi.get("SwapTotal"):
        used = mi["SwapTotal"] - mi.get("SwapFree", 0)
        out.append(_reading("swap_used_pct", used / mi["SwapTotal"] * 100, now))

    try:
        st = os.statvfs("/")
        if st.f_blocks:
            used_blocks = st.f_blocks - st.f_bfree
            out.append(_reading("disk_used_pct", used_blocks / st.f_blocks * 100, now))
    except OSError as exc:
        log.warning("statvfs('/') не сработал: %s", exc)

    for metric, path in (
        ("psi_cpu", "/proc/pressure/cpu"),
        ("psi_memory", "/proc/pressure/memory"),
        ("psi_io", "/proc/pressure/io"),
    ):
        text = _read(path)
        if not text:
            continue  # ядро без PSI — метрику просто не эмитим
        val = parse_pressure_some_avg60(text)
        if val is not None:
            out.append(_reading(metric, val, now))

    if vm1 and vm2:
        d1, d2 = parse_vmstat(vm1), parse_vmstat(vm2)
        oom = max(0, d2.get("oom_kill", 0) - d1.get("oom_kill", 0))
        window_min = max(sample_window_s, 1.0) / 60.0
        out.append(_reading("oom_kills", oom / window_min, now))

    return out
