"""Адаптер host-метрик VPS: читает /proc и statvfs, без smartctl и psutil.

Всё дёшево, кроме двух снимков /proc/{stat,vmstat,net/dev} с паузой между ними
(нужна дельта за окно для steal/iowait/oom/ошибок NIC) — вызов идёт из executor
(`SensorSource.read_host`), поэтому пауза на воркер-потоке приемлема. Стойла ядра
(`kernel_stall_events`) — единственный вызов подпроцесса (`journalctl -k`), тоже
из executor; недоступность (не Linux, нет прав) молча пропускается и
объясняется через `requirements_registry`.

Парсинг вынесен в чистые функции — тестируются на фикстурах без реального /proc.
"""

from __future__ import annotations

import grp
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timedelta

from sa_home_bot.domain.host import METRICS, HostMetricReading
from sa_home_bot.utils.requirements import (
    Requirement,
    RequirementStatus,
    looks_like_permission_error,
    requirements_registry,
)

log = logging.getLogger(__name__)

# Ключ app_state: верхняя граница прошлого скана kernel-журнала (ISO-время).
# Двигается каждым прогоном, сужая окно `journalctl -k -S … -U …`.
KERNEL_SCAN_STATE_KEY = "host:kernel_scan_at"

# Доступ к kernel-журналу без root: нужна группа systemd-journal или adm
# (у `journalctl -k` иначе просто пусто, без явной ошибки — поэтому проверяем
# членство в группе заранее, а не по факту вызова). Отдельный ключ реестра:
# программа делится с power.py::JOURNALCTL_REQUIREMENT, но права нужны другие.
KERNEL_JOURNAL_REQUIREMENT = Requirement(
    program="journalctl",
    package="systemd",
    platforms=("linux",),
    key="journalctl-kernel",
    note="счётчик стойл ядра/гипервизора (нужна группа systemd-journal)",
)

_JOURNAL_GROUPS = ("systemd-journal", "adm")

# Паттерны строк kernel-журнала, означающих стойло ядра/фриз VM гипервизором
# (ровно сценарий jeeves-vpn-hypervisor-stalls).
_STALL_PATTERN = re.compile(
    r"hung_task"
    r"|rcu_(?:sched|preempt)[^\n]*stall"
    r"|clocksource[^\n]*(?:unstable|Marking)"
    r"|soft lockup"
    r"|BUG: soft"
    r"|NETDEV WATCHDOG"
    r"|TX timeout"
    r"|blocked for more than \d+ seconds",
    re.IGNORECASE,
)

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


# Поля /proc/net/dev после «iface:»: 8 rx + 8 tx (порядок ядра).
#   rx: bytes packets errs drop fifo frame compressed multicast
#   tx: bytes packets errs drop fifo colls carrier compressed
_NETDEV_RX_ERRS, _NETDEV_RX_DROP = 2, 3
_NETDEV_TX_ERRS, _NETDEV_TX_DROP = 10, 11


def parse_proc_net_dev(text: str) -> dict[str, tuple[int, int, int, int]]:
    """/proc/net/dev → {iface: (rx_errs, rx_drop, tx_errs, tx_drop)}, без `lo`."""
    out: dict[str, tuple[int, int, int, int]] = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue  # строки-заголовки
        iface = name.strip()
        if iface == "lo":
            continue
        nums = rest.split()
        if len(nums) < 16:
            continue
        try:
            vals = [int(x) for x in nums]
        except ValueError:
            continue
        out[iface] = (
            vals[_NETDEV_RX_ERRS],
            vals[_NETDEV_RX_DROP],
            vals[_NETDEV_TX_ERRS],
            vals[_NETDEV_TX_DROP],
        )
    return out


def net_err_delta(
    prev: dict[str, tuple[int, int, int, int]],
    cur: dict[str, tuple[int, int, int, int]],
) -> int:
    """Суммарный прирост errs+drop (rx+tx) по интерфейсам, живым в обоих снимках."""
    total = 0
    for iface, cur_vals in cur.items():
        prev_vals = prev.get(iface)
        if prev_vals is None:
            continue  # интерфейс появился между снимками — дельту не считаем
        total += sum(max(0, c - p) for c, p in zip(cur_vals, prev_vals, strict=True))
    return total


def parse_conntrack(count_text: str, max_text: str) -> float | None:
    """Заполнение таблицы conntrack в % (count/max·100). None — модуль не загружен."""
    try:
        count = int(count_text.strip())
        limit = int(max_text.strip())
    except ValueError:
        return None
    if limit <= 0:
        return None
    return count / limit * 100


def count_kernel_stalls(text: str) -> int:
    """Число строк kernel-журнала, подходящих под паттерны стойл ядра."""
    return sum(1 for line in text.splitlines() if _STALL_PATTERN.search(line))


def _can_read_kernel_journal() -> bool:
    """root или членство в systemd-journal/adm — иначе `journalctl -k` пуст."""
    if os.geteuid() == 0:
        return True
    try:
        names = {grp.getgrgid(gid).gr_name for gid in os.getgroups()}
    except (KeyError, OSError):
        return False
    return any(g in names for g in _JOURNAL_GROUPS)


def _read_kernel_stall_count(since: datetime, until: datetime) -> int | None:
    """Стойла ядра за окно [since, until) через `journalctl -k`. None — недоступно."""
    if not KERNEL_JOURNAL_REQUIREMENT.available():
        return None  # не Linux / нет journalctl — молчим (причину даст requirements)
    if not _can_read_kernel_journal():
        requirements_registry.report(
            KERNEL_JOURNAL_REQUIREMENT, RequirementStatus.NEEDS_PRIVILEGE
        )
        return None
    args = [
        "journalctl", "-k", "--no-pager", "-o", "cat",
        "-S", f"@{int(since.timestamp())}",
        "-U", f"@{int(until.timestamp())}",
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("journalctl -k не выполнился: %s", exc)
        return None
    if proc.returncode != 0:
        if looks_like_permission_error(proc.stderr):
            requirements_registry.report(
                KERNEL_JOURNAL_REQUIREMENT, RequirementStatus.NEEDS_PRIVILEGE
            )
        else:
            log.warning("journalctl -k код %s: %s", proc.returncode, proc.stderr.strip())
        return None
    requirements_registry.report(KERNEL_JOURNAL_REQUIREMENT, RequirementStatus.OK)
    return count_kernel_stalls(proc.stdout)


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


def read_host_sync(
    now: datetime,
    sample_window_s: float = 10.0,
    kernel_since: datetime | None = None,
) -> list[HostMetricReading]:
    """Снять срез host-метрик (блокирующе). Не-Linux / нет /proc → пустой список.

    ``kernel_since`` — нижняя граница окна скана kernel-журнала (для
    ``kernel_stall_events``); ``None`` — первый прогон, берём ``now -
    sample_window_s``. Верхняя граница — ``now``; вызывающий сдвигает курсор
    в ``app_state`` на ``now`` после успешного среза.
    """
    stat1 = _read("/proc/stat")
    vm1 = _read("/proc/vmstat")
    netdev1 = _read("/proc/net/dev")
    if not stat1:
        return []
    time.sleep(max(0.0, sample_window_s))
    stat2 = _read("/proc/stat")
    vm2 = _read("/proc/vmstat")
    netdev2 = _read("/proc/net/dev")

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

    window_min = max(sample_window_s, 1.0) / 60.0

    if vm1 and vm2:
        d1, d2 = parse_vmstat(vm1), parse_vmstat(vm2)
        oom = max(0, d2.get("oom_kill", 0) - d1.get("oom_kill", 0))
        out.append(_reading("oom_kills", oom / window_min, now))

    if netdev1 and netdev2:
        delta = net_err_delta(parse_proc_net_dev(netdev1), parse_proc_net_dev(netdev2))
        out.append(_reading("net_err_rate", delta / window_min, now))

    count_text = _read("/proc/sys/net/netfilter/nf_conntrack_count")
    max_text = _read("/proc/sys/net/netfilter/nf_conntrack_max")
    if count_text and max_text:
        pct = parse_conntrack(count_text, max_text)
        if pct is not None:
            out.append(_reading("conntrack_pct", pct, now))

    since = kernel_since if kernel_since is not None else now - timedelta(seconds=sample_window_s)
    stalls = _read_kernel_stall_count(since, now)
    if stalls is not None:
        out.append(_reading("kernel_stall_events", float(stalls), now))

    return out
