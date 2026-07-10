"""Управление нодами — иерархия карточек (ARCHITECTURE §11).

Список нод («st:nodes», команда /nodes) → карточка ноды («st:nodecard»,
= /status: мониторинг машины + службы под супервизией) → карточка службы
(«st:svc:<имя>» — данные службы + кнопки управления из describe ноды).
Кнопки управления строятся динамически: действия — из describe, права —
`действие@node`; wake — по праву команды `wake`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, status_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import WakeConfig
from sa_home_bot.proto.messages import ActionSpec, ProtoError
from sa_home_bot.subscriptions.models import Subscription

NODE_SERVICE = "node"

NODES_HEADER = "🕸 <b>Ноды роя</b>"

NODE_DOWN_TEXT = (
    "⚠️ Нода недоступна — не могу получить состояние служб. "
    "Проверьте: systemctl --user status sa-home-node"
)

# Домашний ПК известен рою только адресом для WoL, своей ноды на нём ещё нет.
REMOTE_STUB_TEXT = (
    "💻 <b>Домашний ПК</b> — вне роя (нода ещё не развёрнута), Wake-on-LAN"
)

WAKE_BUTTON_TEXT = "🔌 Разбудить ПК"

_STATUS_LINE = {
    "running": "✅ <b>{name}</b> — работает, pid {pid}, рестартов {restarts}",
    "restarting": "🔄 <b>{name}</b> — упала, перезапускается (рестартов {restarts})",
    "stopped": "⏹ <b>{name}</b> — остановлена",
}

_CARD_STATUS = {
    "running": "✅ работает",
    "restarting": "🔄 перезапускается",
    "stopped": "⏹ остановлена",
}


def _fmt_since(iso: str | None) -> str:
    if not iso:
        return ""
    local = datetime.fromisoformat(iso).astimezone()
    return f", с {local.strftime('%d.%m %H:%M')}"


def _wake_rows(subscription: Subscription, wake: WakeConfig | None) -> list[InlineKeyboardButton]:
    if wake is None or not wake.mac or not subscription.allows_command(commands.WAKE.name):
        return []
    return [
        InlineKeyboardButton(
            text=WAKE_BUTTON_TEXT,
            callback_data=f"{commands.CALLBACK_PREFIX}:{commands.WAKE_CODE}",
        )
    ]


def _rows(buttons: list[InlineKeyboardButton]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    )


# --- Список нод -------------------------------------------------------------


def render_nodes_list(state: dict | None, wake: WakeConfig | None) -> str:
    lines = [NODES_HEADER, ""]
    if state is None:
        lines.append(NODE_DOWN_TEXT)
    else:
        services = state.get("services", [])
        running = sum(1 for s in services if s.get("status") == "running")
        lines.append(
            f"🖥 <b>{state.get('node', '?')}</b> — 🟢 в сети, "
            f"службы: {running}/{len(services)} работают"
        )
    if wake is not None and wake.mac:
        lines.append(REMOTE_STUB_TEXT)
    return "\n".join(lines)


def build_nodes_list_keyboard(
    subscription: Subscription | None,
    node_name: str | None,
    wake: WakeConfig | None = None,
) -> InlineKeyboardMarkup | None:
    if subscription is None:
        return None
    buttons: list[InlineKeyboardButton] = []
    if node_name and subscription.allows_command(commands.STATUS.name):
        buttons.append(
            InlineKeyboardButton(
                text=f"📋 Карточка {node_name}",
                callback_data=f"{commands.CALLBACK_PREFIX}:{commands.NODE_CARD_CODE}",
            )
        )
    buttons.extend(_wake_rows(subscription, wake))
    return _rows(buttons)


async def build_nodes_list_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    wake: WakeConfig | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    state = await _node_state(node_link)
    node_name = state.get("node") if state is not None else None
    return (
        render_nodes_list(state, wake),
        build_nodes_list_keyboard(subscription, node_name, wake),
    )


# --- Карточка ноды (= /status + службы) -------------------------------------


def render_services_block(state: dict) -> str:
    lines = [f"<b>Службы ноды {state.get('node', '?')}</b> (v{state.get('version', '?')}):"]
    services = state.get("services", [])
    if not services:
        lines.append("Службы не назначены.")
        return "\n".join(lines)
    for svc in services:
        template = _STATUS_LINE.get(svc.get("status", ""), "❔ <b>{name}</b> — {status}")
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


def build_node_card_keyboard(
    subscription: Subscription | None,
    monitor_actions: Sequence[ActionSpec],
    service_names: Sequence[str],
) -> InlineKeyboardMarkup | None:
    """Представления мониторинга + действия монитора + карточки служб."""
    if subscription is None:
        return None
    buttons: list[InlineKeyboardButton] = []
    base = status_view.build_status_keyboard(subscription, monitor_actions)
    if base is not None:
        buttons.extend(b for row in base.inline_keyboard for b in row)
    if subscription.allows_command(commands.NODES.name):
        buttons.extend(
            InlineKeyboardButton(
                text=f"⚙️ {name}",
                callback_data=(
                    f"{commands.CALLBACK_PREFIX}:{commands.SERVICE_CARD_CODE}:{name}"
                ),
            )
            for name in service_names
        )
    return _rows(buttons)


async def build_node_card_view(
    monitor_link: ServiceLink,
    node_link: ServiceLink,
    subscription: Subscription | None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    summary = await status_view.build_summary_text(monitor_link)
    state = await _node_state(node_link)
    services_block = render_services_block(state) if state is not None else NODE_DOWN_TEXT
    service_names = (
        [s.get("name", "?") for s in state.get("services", [])] if state is not None else []
    )
    keyboard = build_node_card_keyboard(
        subscription, await monitor_link.actions(), service_names
    )
    return f"{summary}\n\n{services_block}", keyboard


# --- Карточка службы ---------------------------------------------------------


def render_service_card(node_name: str, svc: dict) -> str:
    status = _CARD_STATUS.get(svc.get("status", ""), f"❔ {svc.get('status', '?')}")
    lines = [
        f"⚙️ <b>Служба {svc.get('name', '?')}</b> · нода {node_name}",
        "",
        f"Статус: {status}"
        + (f", pid {svc['pid']}" if svc.get("pid") else "")
        + _fmt_since(svc.get("started_at") if svc.get("status") == "running" else None),
        f"Рестартов после падений: {svc.get('restarts', 0)}",
    ]
    if svc.get("last_exit_code") is not None:
        lines.append(f"Последний код выхода: {svc['last_exit_code']}")
    return "\n".join(lines)


def build_service_card_keyboard(
    subscription: Subscription | None,
    node_actions: Sequence[ActionSpec],
    service_name: str,
) -> InlineKeyboardMarkup | None:
    """Действия ноды, применимые к этой службе (параметр name из choices)."""
    if subscription is None:
        return None
    buttons: list[InlineKeyboardButton] = []
    for action in node_actions:
        if not subscription.allows_action(action.id, NODE_SERVICE):
            continue
        param = action.params[0] if action.params else None
        if param is None or not param.choices or service_name not in param.choices:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=action.title,
                callback_data=(
                    f"{commands.ACTION_CALLBACK_PREFIX}:{NODE_SERVICE}:"
                    f"{action.id}:{service_name}"
                ),
            )
        )
    return _rows(buttons)


async def build_service_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    service_name: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    state = await _node_state(node_link)
    if state is None:
        return NODE_DOWN_TEXT, None
    svc = next(
        (s for s in state.get("services", []) if s.get("name") == service_name), None
    )
    if svc is None:
        return f"Служба «{service_name}» не найдена на ноде.", None
    keyboard = build_service_card_keyboard(
        subscription, await node_link.actions(), service_name
    )
    return render_service_card(state.get("node", "?"), svc), keyboard


# --- Общее -------------------------------------------------------------------


async def _node_state(node_link: ServiceLink) -> dict | None:
    try:
        return await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None
