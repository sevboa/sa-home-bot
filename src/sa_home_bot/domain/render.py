"""Тексты сообщений о событиях здоровья (HTML). Без БД и aiogram."""

from __future__ import annotations

from html import escape

from sa_home_bot.domain.models import (
    ALERTING,
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    KIND_CPU,
    POWER_CLEAN,
    Event,
    HealthState,
    PowerEvent,
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
