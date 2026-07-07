"""Раздел /node: состояние ноды и её служб + кнопки управления из describe.

Кнопки строятся полностью динамически: действия — из describe ноды, значения
параметра — из его choices (имена служб), права — `действие@node`. Новая
capability на ноде = новая кнопка без изменения кода бота.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ActionSpec, ProtoError
from sa_home_bot.subscriptions.models import Subscription

NODE_SERVICE = "node"

NODE_DOWN_TEXT = (
    "⚠️ Нода недоступна — не могу получить состояние служб. "
    "Проверьте: systemctl --user status sa-home-node"
)

_STATUS_LINE = {
    "running": "✅ <b>{name}</b> — работает, pid {pid}, рестартов {restarts}",
    "restarting": "🔄 <b>{name}</b> — упала, перезапускается (рестартов {restarts})",
    "stopped": "⏹ <b>{name}</b> — остановлена",
}


def _fmt_since(iso: str | None) -> str:
    if not iso:
        return ""
    local = datetime.fromisoformat(iso).astimezone()
    return f", с {local.strftime('%d.%m %H:%M')}"


def render_node_state(state: dict) -> str:
    lines = [f"🖥 <b>Нода {state.get('node', '?')}</b> (v{state.get('version', '?')})", ""]
    services = state.get("services", [])
    if not services:
        lines.append("Службы не назначены.")
        return "\n".join(lines)
    for svc in services:
        template = _STATUS_LINE.get(
            svc.get("status", ""), "❔ <b>{name}</b> — {status}"
        )
        line = template.format(
            name=svc.get("name", "?"),
            status=svc.get("status", "?"),
            pid=svc.get("pid") or "—",
            restarts=svc.get("restarts", 0),
        )
        if svc.get("status") == "running":
            line += _fmt_since(svc.get("started_at"))
        lines.append(line)
    return "\n".join(lines)


def build_node_keyboard(
    subscription: Subscription | None,
    actions: Sequence[ActionSpec],
) -> InlineKeyboardMarkup | None:
    """Кнопки действий ноды: действие × значение его параметра (из choices)."""
    if subscription is None:
        return None
    buttons: list[InlineKeyboardButton] = []
    for action in actions:
        if not subscription.allows_action(action.id, NODE_SERVICE):
            continue
        param = action.params[0] if action.params else None
        if param is None:
            buttons.append(
                InlineKeyboardButton(
                    text=action.title,
                    callback_data=(
                        f"{commands.ACTION_CALLBACK_PREFIX}:{NODE_SERVICE}:{action.id}"
                    ),
                )
            )
        elif param.choices:
            buttons.extend(
                InlineKeyboardButton(
                    text=f"{action.title} · {choice}",
                    callback_data=(
                        f"{commands.ACTION_CALLBACK_PREFIX}:{NODE_SERVICE}:{action.id}:{choice}"
                    ),
                )
                for choice in param.choices
            )
        # Параметр без choices — кнопку не построить (нужен свободный ввод).
    if not buttons:
        return None
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_node_view(
    link: ServiceLink, subscription: Subscription | None
) -> tuple[str, InlineKeyboardMarkup | None]:
    try:
        state = await link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return NODE_DOWN_TEXT, None
    keyboard = build_node_keyboard(subscription, await link.actions())
    return render_node_state(state), keyboard
