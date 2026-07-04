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
    return dt.strftime("%d.%m %H:%M")


def render_downtime(events: list[PowerEvent]) -> str:
    """Сообщение /downtime — список последних отключений машины."""
    if not events:
        return "Нет данных об отключениях (журнал `last` пуст или недоступен)."
    lines = ["<b>Последние отключения</b>", ""]
    for e in events:
        if e.kind == POWER_CLEAN:
            when = _fmt_dt(e.shutdown_at) if e.shutdown_at else "?"
            lines.append(f"🔌 штатно — {when}")
        else:
            tail = f", поднялась {_fmt_dt(e.next_boot_at)}" if e.next_boot_at else ""
            lines.append(
                f"⚡ внезапно — работала с {_fmt_dt(e.boot_at)}, оборвана{tail}"
            )
    return "\n".join(lines)
