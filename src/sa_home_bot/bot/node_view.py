"""Управление нодами — иерархия карточек (ARCHITECTURE §11).

Навигация — ссылками-командами в тексте (масштабируется на любое число нод
и служб, история переходов остаётся в чате): сводка роя → `/node_<id>`
(карточка ноды: мониторинг + службы) → `/svc_<нода>_<служба>` (карточка
службы). Каждая карточка — новое сообщение; inline-кнопки остаются только
за ДЕЙСТВИЯМИ (start/stop/restart, питание, скан, назначить) — их число
ограничено. Действия — из describe, права — `действие@служба`.

Рой равноправен (§11 п. 1): карточка одна на все ноды (своя — частный
случай node_id=None), данные и действия идут через свою ноду с dst-адресом
(«спроси любого», п. 2) — боту не нужно знать, кто отвечает.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands, node_links, status_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ActionSpec, Address, ProtoError
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.models import Subscription

NODE_SERVICE = "node"

NODE_DOWN_TEXT = (
    "⚠️ Нода недоступна — не могу получить состояние служб. "
    "Проверьте: systemctl --user status sa-home-node"
)

# Лампочки статуса — единый словарь для служб (running/restarting/stopped) и
# пиров (alive/dead). ⚪ — статус неизвестен (нет данных).
LAMP_GREEN = "🟢"
LAMP_ORANGE = "🟠"
LAMP_RED = "🔴"
LAMP_GRAY = "⚪"

# {name} — ссылка-команда /svc_… на карточку службы (или <b>имя</b>, если
# имя непредставимо командой Telegram).
_STATUS_LINE = {
    "running": f"{LAMP_GREEN} {{name}} — работает, pid {{pid}}, рестартов {{restarts}}",
    "restarting": (
        f"{LAMP_ORANGE} {{name}} — упала, перезапускается (рестартов {{restarts}})"
    ),
    "stopped": f"{LAMP_RED} {{name}} — остановлена",
}

_CARD_STATUS = {
    "running": f"{LAMP_GREEN} работает",
    "restarting": f"{LAMP_ORANGE} перезапускается",
    "stopped": f"{LAMP_RED} остановлена",
}


def _fmt_since(iso: str | None) -> str:
    if not iso:
        return ""
    local = datetime.fromisoformat(iso).astimezone()
    return f", с {local.strftime('%d.%m %H:%M')}"


def _power_buttons(
    subscription: Subscription,
    node_actions: Sequence[ActionSpec],
    node_id: str | None = None,
) -> list[InlineKeyboardButton]:
    """Кнопки действий без параметров (poweroff/reboot/suspend) — локально
    или на пире (node_id); право — то же `действие@node`, что и у служб."""
    return [
        InlineKeyboardButton(
            text=action.title,
            callback_data=commands.action_callback(action.id, node_id=node_id),
        )
        for action in node_actions
        if not action.params and subscription.allows_action(action.id, NODE_SERVICE)
    ]


# --- Карточка ноды (= /status + службы, единая для своей и пиров) -----------


def render_node_card_header(state: dict) -> str:
    """Заголовок карточки: имя ноды, версия ПО, аптайм системы и ноды."""
    lines = [f"🕸 <b>Нода {state.get('node', '?')}</b> (v{state.get('version', '?')})"]
    uptime_bits = []
    if state.get("system_uptime_s") is not None:
        uptime_bits.append(f"система {format_duration(state['system_uptime_s'])}")
    if state.get("uptime_s") is not None:
        uptime_bits.append(f"нода {format_duration(state['uptime_s'])}")
    if uptime_bits:
        lines.append("Аптайм: " + " · ".join(uptime_bits))
    return "\n".join(lines)


def _svc_display(node_id: str, name: str) -> str:
    """Имя службы как ссылка на её карточку (или жирным, если непредставимо)."""
    link = node_links.svc_command(node_id, name)
    return link if link is not None else f"<b>{escape(name)}</b>"


def render_services_block(state: dict) -> str:
    node_id = state.get("node", "?")
    lines = ["<b>Службы</b> (ссылка — карточка службы):"]
    services = state.get("services", [])
    if not services:
        lines = ["Службы не назначены."]
        return "\n".join(lines)
    for svc in services:
        template = _STATUS_LINE.get(
            svc.get("status", ""), f"{LAMP_GRAY} {{name}} — {{status}}"
        )
        line = template.format(
            name=_svc_display(node_id, svc.get("name", "?")),
            status=svc.get("status", "?"),
            pid=svc.get("pid") or "—",
            restarts=svc.get("restarts", 0),
        )
        if svc.get("status") == "running":
            line += _fmt_since(svc.get("started_at"))
        lines.append(line)
    return "\n".join(lines)


_ACTION_ASSIGN = "assign"


def _assign_buttons(
    subscription: Subscription,
    node_actions: Sequence[ActionSpec],
    assigned: Sequence[str],
    node_id: str | None = None,
) -> list[InlineKeyboardButton]:
    """Кнопка «➕ Назначить X» на каждое ещё не назначенное известное имя."""
    if not subscription.allows_action(_ACTION_ASSIGN, NODE_SERVICE):
        return []
    assign = next((a for a in node_actions if a.id == _ACTION_ASSIGN), None)
    if assign is None or not assign.params:
        return []
    return [
        InlineKeyboardButton(
            text=f"➕ Назначить {name}",
            callback_data=commands.action_callback(_ACTION_ASSIGN, name, node_id),
        )
        for name in (assign.params[0].choices or ())
        if name not in assigned
    ]


def build_node_card_keyboard(
    subscription: Subscription | None,
    monitor_actions: Sequence[ActionSpec],
    service_names: Sequence[str],
    node_actions: Sequence[ActionSpec] = (),
    node_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Кнопки карточки ноды — одинаковые для своей (node_id=None) и пира.

    Только действия: представления/действия монитора + питание + назначение
    служб. Навигация к карточкам служб — ссылками в тексте (см.
    render_services_block), не кнопками. Рой равноправен (ARCHITECTURE §11
    п. 1) — узел лишь несёт node_id в callback'ах, набор кнопок и права не
    зависят от того, чья это физически машина.
    """
    if subscription is None:
        return None
    buttons: list[InlineKeyboardButton] = []
    base = status_view.build_status_keyboard(subscription, monitor_actions, node_id)
    if base is not None:
        buttons.extend(b for row in base.inline_keyboard for b in row)
    buttons.extend(_power_buttons(subscription, node_actions, node_id))
    buttons.extend(_assign_buttons(subscription, node_actions, service_names, node_id))
    return actions.rows(buttons)


async def build_node_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    node_id: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Единая карточка ноды: своей (node_id=None) или пира.

    Все данные — через свою ноду («спроси любого», §11 п. 2): и состояние
    ноды, и сводка её монитора идут по dst-адресации, включая свой же
    монитор (лишний хоп через локальный unix-сокет дёшев, зато путь один).
    Нет назначения monitor / монитор лежит — честный MONITOR_DOWN_TEXT.
    """
    node_dst = Address(node=node_id, service=NODE_SERVICE) if node_id else None
    state = await _node_state(node_link, dst=node_dst)
    if state is None:
        return (
            f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
            if node_id
            else NODE_DOWN_TEXT
        ), None

    monitor_dst = Address(node=node_id, service=status_view.MONITOR_SERVICE)
    monitor_summary = await status_view.build_summary_text(node_link, dst=monitor_dst)
    monitor_desc = await node_link.describe(dst=monitor_dst)
    monitor_actions = monitor_desc.actions if monitor_desc is not None else ()

    if node_id:
        desc = await node_link.describe(dst=node_dst)
        node_actions = desc.actions if desc is not None else ()
    else:
        node_actions = await node_link.actions()

    service_names = [s.get("name", "?") for s in state.get("services", [])]
    text = "\n\n".join(
        [monitor_summary, render_node_card_header(state), render_services_block(state)]
    )
    keyboard = build_node_card_keyboard(
        subscription, monitor_actions, service_names, node_actions, node_id
    )
    return text, keyboard


# --- Карточка службы ---------------------------------------------------------


def render_service_card(node_name: str, svc: dict) -> str:
    status = _CARD_STATUS.get(svc.get("status", ""), f"{LAMP_GRAY} {svc.get('status', '?')}")
    # Имя ноды — ссылка на её карточку (обратный переход без кнопки).
    node_display = node_links.node_command(node_name) or escape(node_name)
    lines = [
        f"⚙️ <b>Служба {escape(svc.get('name', '?'))}</b> · нода {node_display}",
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
    node_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    """Действия ноды, применимые к этой службе (параметр name из choices).

    node_id — служба на пире: та же кнопка, но с адресом пира в callback.
    """
    return actions.build_choice_keyboard(
        subscription, node_actions, NODE_SERVICE, service_name, node_id
    )


async def build_service_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    service_name: str,
    node_id: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    dst = Address(node=node_id, service=NODE_SERVICE) if node_id else None
    state = await _node_state(node_link, dst=dst)
    if state is None:
        return (
            f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
            if node_id
            else NODE_DOWN_TEXT
        ), None
    svc = next(
        (s for s in state.get("services", []) if s.get("name") == service_name), None
    )
    if svc is None:
        return f"Служба «{service_name}» не найдена на ноде.", None
    if node_id:
        desc = await node_link.describe(dst=dst)
        node_actions = desc.actions if desc is not None else ()
    else:
        node_actions = await node_link.actions()
    keyboard = build_service_card_keyboard(subscription, node_actions, service_name, node_id)
    return render_service_card(state.get("node", "?"), svc), keyboard


# --- Общее -------------------------------------------------------------------


async def _node_state(node_link: ServiceLink, dst: Address | None = None) -> dict | None:
    try:
        return await node_link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        return None
