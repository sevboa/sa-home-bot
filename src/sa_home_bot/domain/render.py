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
    return dt.astimezone().strftime("%d.%m %H:%M")


def _fmt_duration(td) -> str:
    """Простой в человекочитаемом виде: «2 д 3 ч» / «9 ч 11 м» / «17 м»."""
    total = int(td.total_seconds())
    if total < 0:
        total = 0
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} д {hours} ч"
    if hours:
        return f"{hours} ч {minutes} м"
    if minutes:
        return f"{minutes} м"
    return "&lt;1 м"  # &lt; — «<» экранирован для HTML parse_mode


def render_outage_line(e: PowerEvent) -> str:
    """Одна строка отключения: «⚡ внезапно · ≈ 04.07 15:12 → 05.07 00:23 · простой 9 ч 11 м»."""
    icon, word = ("🔌", "штатно") if e.kind == POWER_CLEAN else ("⚡", "внезапно")
    down = ("≈ " if e.down_approx else "") + _fmt_dt(e.down_at) if e.down_at else "?"
    up = _fmt_dt(e.up_at) if e.up_at else "?"
    span = f"{down} → {up}"
    tail = f" · простой {_fmt_duration(e.downtime)}" if e.downtime is not None else ""
    return f"{icon} <b>{word}</b> · {span}{tail}"


def render_downtime(events: list[PowerEvent]) -> str:
    """Сообщение /downtime — список последних отключений машины."""
    if not events:
        return "Нет данных об отключениях (журнал `last` пуст или недоступен)."
    lines = ["<b>Последние отключения</b>", ""]
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


def _fmt_gb(nbytes: int | None) -> str:
    return "?" if nbytes is None else f"{nbytes / 1e9:.0f}"


def render_disk_line(d: DiskSummary) -> str:
    """Строка диска в сводке: «HDD1 ⚠️ 31°C · своб. 137 / 245 ГБ»."""
    icon = _DISK_ICON.get(d.health, "❔")  # ❔ — SMART недоступен (eMMC)
    parts = [f"{escape(d.label)} {icon}"]
    if d.temperature_c is not None:
        parts.append(f"{d.temperature_c:.0f}°C")
    if d.total_bytes:
        parts.append(f"· своб. {_fmt_gb(d.free_bytes)} / {_fmt_gb(d.total_bytes)} ГБ")
    else:
        parts.append("· не смонтирован")
    return " ".join(parts)


def render_status_summary(
    now: datetime,
    uptime: timedelta | None,
    cpu_states: list[HealthState],
    disks: list[DiskSummary],
    last_outage: PowerEvent | None,
) -> str:
    """Краткая сводка (/status): время отчёта, аптайм, температуры, диски, отключение."""
    lines = [f"📊 <b>Сводка</b> — {_fmt_dt(now)}"]
    if uptime is not None:
        lines.append(f"⏱ Аптайм: {_fmt_duration(uptime)}")

    cpu = [s for s in cpu_states if s.kind == KIND_CPU]
    if cpu:
        hot = any(s.status == ALERTING for s in cpu)
        tmax = max(s.temperature_c for s in cpu)
        lines.append(f"{'🔥' if hot else '✅'} CPU: {tmax:.1f}°C")

    if disks:
        lines.append("💽 <b>Диски</b>")
        lines.extend(render_disk_line(d) for d in disks)

    if last_outage is not None:
        lines.append("")
        lines.append("Последнее отключение:")
        lines.append(render_outage_line(last_outage))
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
