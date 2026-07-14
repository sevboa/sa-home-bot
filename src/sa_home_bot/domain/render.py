"""Тексты сообщений о событиях здоровья (HTML). Без БД и aiogram."""

from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from sa_home_bot.domain.models import (
    ALERTING,
    DISK_FAIL,
    DISK_OK,
    DISK_WARN,
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    EVENT_SMART_DEGRADED,
    KIND_CPU,
    POWER_CLEAN,
    DiskSummary,
    Event,
    HealthState,
    PowerEvent,
    SmartChange,
)


def _kind_word(kind: str) -> str:
    return "CPU" if kind == KIND_CPU else "Диск"


def render_event(event: Event) -> str:
    """Текст уведомления для события перегрева/возврата к норме."""
    label = escape(event.label)
    temp = f"{event.temperature_c:.1f}°C"
    when = event.at.strftime("%H:%M:%S")
    kind = _kind_word(event.kind)
    body = f"\nТемпература: <b>{temp}</b>\nВремя: {when}"
    if event.type == EVENT_OVERHEAT_STARTED:
        return f"🔥 <b>Перегрев</b> — {kind} «{label}»{body}"
    if event.type == EVENT_OVERHEAT_CLEARED:
        return f"✅ <b>Норма</b> — {kind} «{label}» остыл{body}"
    return f"ℹ️ {kind} «{label}»: {temp} ({when})"


_HEALTH_WORD = {DISK_OK: "норма", DISK_WARN: "предупреждение", DISK_FAIL: "СБОЙ"}


def _health_word(health: str | None) -> str:
    return _HEALTH_WORD.get(health, "н/д")


def render_smart_change(change: SmartChange) -> str:
    """Текст уведомления об изменении SMART-здоровья диска."""
    label = escape(change.label)
    if change.event_type == EVENT_SMART_DEGRADED:
        lines = [f"⚠️ <b>SMART: ухудшение</b> — диск «{label}»"]
    else:
        lines = [f"✅ <b>SMART: улучшение</b> — диск «{label}»"]
    if change.health_to != change.health_from:
        lines.append(
            f"Здоровье: {_health_word(change.health_from)} → "
            f"<b>{_health_word(change.health_to)}</b>"
        )
    for c in change.attr_changes:
        arrow = "🔺" if c.new > c.old else "🔻"
        lines.append(f"{arrow} {escape(c.name)}: {c.old} → <b>{c.new}</b>")
    return "\n".join(lines)


def render_state_line(state: HealthState) -> str:
    """Одна строка состояния компонента для /status."""
    icon = "🔥" if state.status == ALERTING else "✅"
    label = escape(state.label)
    temp = f"{state.temperature_c:.1f}°C"
    kind = _kind_word(state.kind)
    suffix = ""
    if state.status == ALERTING and state.alerting_since is not None:
        suffix = f" с {state.alerting_since.strftime('%H:%M:%S')}"
    return f"{icon} {kind} «{label}»: <b>{temp}</b>{suffix}"


def _fmt_dt(dt) -> str:
    # last даёт время с локальным offset, journal — в UTC; приводим к единой
    # локальной TZ процесса, чтобы обе метки печатались в одном поясе.
    return dt.astimezone().strftime("%m/%d %H:%M")


def _fmt_duration(td) -> str:
    """Простой в d/h/m/s, только ненулевые единицы: «9m» / «8h 26m» / «1d 2h»."""
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def _fmt_uptime_short(td) -> str:
    """Аптайм одной крупнейшей единицей по-английски: «1 d» / «5 h» / «17 m»."""
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} d"
    if hours:
        return f"{hours} h"
    if minutes:
        return f"{minutes} m"
    return "&lt;1 m"  # &lt; — «<» экранирован для HTML parse_mode


def render_outage_line(e: PowerEvent) -> str:
    """Одна строка отключения: «⚡ 07/05 10:57 - 07/05 11:06 (9m)»."""
    icon = "🔌" if e.kind == POWER_CLEAN else "⚡"
    down = _fmt_dt(e.down_at) if e.down_at else "?"
    up = _fmt_dt(e.up_at) if e.up_at else "?"
    tail = f" ({_fmt_duration(e.downtime)})" if e.downtime is not None else ""
    return f"{icon} {down} - {up}{tail}"


def render_downtime(events: list[PowerEvent], offset: int = 0) -> str:
    """Сообщение /downtime — страница отключений машины (постранично по 10)."""
    if not events:
        if offset:
            return "Дальше отключений нет."
        return "Нет данных об отключениях (журнал `last` пуст или недоступен)."
    header = (
        "<b>Последние отключения</b>"
        if offset == 0
        else f"<b>Отключения {offset + 1}–{offset + len(events)}</b>"
    )
    lines = [header, ""]
    lines.extend(render_outage_line(e) for e in events)
    return "\n".join(lines)


def render_status_full(states: list[HealthState]) -> str:
    """Подробный статус компонентов (/status_full) — по строке на компонент."""
    if not states:
        return "Пока нет данных — сканер ещё не снимал срез."
    alerting = [s for s in states if s.status == ALERTING]
    header = "🔥 <b>Есть перегрев!</b>" if alerting else "✅ <b>Всё в норме.</b>"
    lines = [header, ""]
    lines.extend(render_state_line(s) for s in states)
    return "\n".join(lines)


_DISK_ICON = {DISK_OK: "✅", DISK_WARN: "⚠️", DISK_FAIL: "❌"}

# Пороги «настроения» (🥶/🙂/🥵/🔥) — те же warn_c/crit_c, что уже настроены в
# config.sensors для реальных алертов (не независимая шкала): 🥵 совпадает с
# приближением к предупреждению, 🔥 — с уже сработавшим алертом. Так эмодзи не
# противоречит факту наличия/отсутствия уведомления.
#
# «Холодно» — порог ниже реальных алертов, датащитами не регламентируется
# (нижний предел там 0°C, что бессмысленно как бытовой ориентир): для дисков —
# обычная комнатная температура (по паспортам Seagate Momentus 5400.6 и Hitachi
# Travelstar Z5K320 — рабочий диапазон 0-60°C, ниже комнатной работающий диск
# практически не бывает); для CPU — измеренный холостой ход этой машины
# 33-37°C (см. CLAUDE.md), Tjunction max Celeron N3350 ≈105°C.
_DISK_TEMP_COLD_C = 25.0
_CPU_TEMP_COLD_C = 35.0


def _temp_mood(temp_c: float, cold: float, warn_c: float, crit_c: float) -> str:
    if temp_c >= crit_c:
        return "🔥"
    if temp_c >= warn_c:
        return "🥵"
    if temp_c >= cold:
        return "🙂"
    return "🥶"


def _disk_temp_mood(temp_c: float, warn_c: float, crit_c: float) -> str:
    return _temp_mood(temp_c, _DISK_TEMP_COLD_C, warn_c, crit_c)


def _cpu_temp_mood(temp_c: float, warn_c: float, crit_c: float) -> str:
    return _temp_mood(temp_c, _CPU_TEMP_COLD_C, warn_c, crit_c)


def _fmt_gb(nbytes: int) -> str:
    return f"{nbytes / 1e9:.0f}"


def _disk_usage(d: DiskSummary) -> str:
    """Занятое место: «7 из 57 ГБ (10%)» (было — свободное; теперь занятое)."""
    if not d.total_bytes:
        return "не смонтирован"
    used = d.total_bytes - (d.free_bytes or 0)
    pct = round(used / d.total_bytes * 100)
    return f"{_fmt_gb(used)} из {_fmt_gb(d.total_bytes)} ГБ ({pct}%)"


# Вид носителя → слово в заголовке строки диска.
_KIND_WORD = {"hdd": "HDD", "ssd": "SSD", "nvme": "NVMe", "emmc": "eMMC"}


def render_disk_line(d: DiskSummary, warn_c: float, crit_c: float) -> str:
    """Строка диска: «NVMe Samsung 970» + «27°C 🥶| 117 из 245 ГБ (50%)».

    У eMMC SMART физически нет — иконку здоровья не показываем вовсе
    («eMMC: 7 из 57 ГБ (10%)»). У SMART-способных дисков ❔ значит ровно
    «данных SMART нет» (нет прав/программы/холодный старт); причину объясняет
    ⚠️-строка requirements в сводке. `warn_c`/`crit_c` — пороги алертов дисков
    из config.sensors.disks (та же шкала, что и для реальных уведомлений).
    """
    word = _KIND_WORD.get(d.kind, "HDD")
    is_emmc = d.kind == "emmc"
    heading = word if is_emmc else f"{word} {escape(d.model) if d.model else '?'}"
    icon = "" if is_emmc else _DISK_ICON.get(d.health, "❔") + " "
    usage = _disk_usage(d)
    if d.temperature_c is None:
        return f"{icon}{heading}: {usage}"
    mood = _disk_temp_mood(d.temperature_c, warn_c, crit_c)
    return f"{icon}{heading}\n {d.temperature_c:.0f}°C {mood}| {usage}"


def render_status_summary(
    now: datetime,
    uptime: timedelta | None,
    cpu_states: list[HealthState],
    disks: list[DiskSummary],
    last_outage: PowerEvent | None,
    cpu_warn_c: float,
    cpu_crit_c: float,
    disk_warn_c: float,
    disk_crit_c: float,
) -> str:
    """Краткая сводка (/status): время отчёта, аптайм, температуры, диски, отключение.

    `*_warn_c`/`*_crit_c` — пороги алертов из config.sensors (те же, что уже
    настроены для реальных уведомлений о перегреве/SMART) — используются и
    для эмодзи-«настроения» температуры, чтобы иконка не противоречила факту
    срабатывания алерта.
    """
    lines = [now.astimezone().strftime("%Y-%m-%d %H:%M")]
    if uptime is not None:
        lines.append(f"uptime: {_fmt_uptime_short(uptime)}")
    if last_outage is not None:
        lines.append(f"last off: {render_outage_line(last_outage)}")

    body: list[str] = []
    cpu = [s for s in cpu_states if s.kind == KIND_CPU]
    if cpu:
        hot = any(s.status == ALERTING for s in cpu)
        tmax = max(s.temperature_c for s in cpu)
        mood = _cpu_temp_mood(tmax, cpu_warn_c, cpu_crit_c)
        body.append(f"{'🔥' if hot else '✅'} CPU: {tmax:.1f}°C {mood}")
    body.extend(render_disk_line(d, disk_warn_c, disk_crit_c) for d in disks)

    if body:
        lines.append("")
        lines.extend(body)
    return "\n".join(lines)


def _fmt_run(run: dict) -> str:
    icon = {"ok": "✅", "error": "❌", "running": "⏳"}.get(run["status"], "•")
    started = run["started_at"]
    try:
        started = datetime.fromisoformat(started).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    return f"{icon} {run['job_type']} @ {started}"


def render_stats(counts: dict, runs: list[dict]) -> str:
    """Сводка прогонов сканера (/stats) из job_runs."""
    if not runs:
        return "Прогонов сканера ещё не было."
    total = sum(counts.values())
    lines = [
        "<b>Статистика сканера</b>",
        f"Всего прогонов: {total} (ok={counts.get('ok', 0)}, "
        f"error={counts.get('error', 0)}, running={counts.get('running', 0)})",
        "",
        "Последние:",
    ]
    lines.extend(_fmt_run(r) for r in runs)
    return "\n".join(lines)
