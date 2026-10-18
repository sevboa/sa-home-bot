from __future__ import annotations

import asyncio
from collections.abc import Sequence
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands, node_links, status_view
from sa_home_bot.bot.pagination import clamp_offset, nav_row
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.keyboards import back_row, button, refresh_row
from sa_home_bot.proto.addresses import NODE_SERVICE, node_address
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.proto.messages import ActionSpec, Address, ServiceDescription
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.timefmt import format_local

MAX_NAME_LEN = 40
SERVICES_PAGE_SIZE = 6
OWN_NODE_SEGMENT = "-"
ACTION_ASSIGN = "assign"
ACTION_UNASSIGN = "unassign"

NODE_DOWN_TEXT = (
    "⚠️ Нода недоступна — не могу получить состояние служб. "
    "Проверьте: systemctl --user status sa-home-node"
)

LAMP_GREEN = "🟢"
LAMP_ORANGE = "🟠"
LAMP_RED = "🔴"
LAMP_GRAY = "⚪"

_STATUS_LINE = {
    "running": f"{LAMP_GREEN} {{name}} — работает, pid {{pid}}, рестартов {{restarts}}",
    "restarting": f"{LAMP_ORANGE} {{name}} — упала, перезапускается (рестартов {{restarts}})",
    "stopped": f"{LAMP_RED} {{name}} — остановлена",
}

_CARD_STATUS = {
    "running": f"{LAMP_GREEN} работает",
    "restarting": f"{LAMP_ORANGE} перезапускается",
    "stopped": f"{LAMP_RED} остановлена",
}


def node_segment(node_id: str | None) -> str:
    return node_id or OWN_NODE_SEGMENT


def node_from_segment(segment: str | None) -> str | None:
    return None if not segment or segment == OWN_NODE_SEGMENT else segment


def node_unavailable_text(node_id: str | None) -> str:
    if node_id:
        return f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
    return NODE_DOWN_TEXT


def _since_suffix(iso: str | None) -> str:
    if not iso:
        return ""
    return f", с {format_local(iso, '%d.%m %H:%M')}"


def _status_text(svc: dict) -> str:
    return _CARD_STATUS.get(svc.get("status", ""), f"{LAMP_GRAY} {svc.get('status', '?')}")


def _simple_action_buttons(
    subscription: Subscription,
    node_actions: Sequence[ActionSpec],
    node_id: str | None = None,
) -> list[InlineKeyboardButton]:
    return [
        button(action.title, commands.action_callback(action.id, node_id=node_id))
        for action in node_actions
        if not action.params and subscription.allows_action(action.id, NODE_SERVICE)
    ]


def render_node_card_header(state: dict) -> str:
    lines = [f"🕸 <b>Нода {state.get('node', '?')}</b> (v{state.get('version', '?')})"]
    uptime_bits = []
    if state.get("system_uptime_s") is not None:
        uptime_bits.append(f"система {format_duration(state['system_uptime_s'])}")
    if state.get("uptime_s") is not None:
        uptime_bits.append(f"нода {format_duration(state['uptime_s'])}")
    if uptime_bits:
        lines.append("Аптайм: " + " · ".join(uptime_bits))
    update = state.get("update")
    if update and update.get("restart_required"):
        lines.append(f"⚠️ Обновлено до v{update.get('installed')} — ждёт перезапуска (restart_node)")
    return "\n".join(lines)


def _svc_display(node_id: str, name: str) -> str:
    link = node_links.svc_command(node_id, name)
    return link if link is not None else f"<b>{escape(name)}</b>"


def render_services_block(state: dict) -> str:
    node_id = state.get("node", "?")
    services = state.get("services", [])
    if not services:
        return "Службы не назначены."
    lines = ["<b>Службы</b> (ссылка — карточка службы):"]
    for svc in services:
        name = _svc_display(node_id, svc.get("name", "?"))
        if svc.get("external"):
            running = svc.get("status") == "running"
            lamp = LAMP_GREEN if running else LAMP_RED
            state_text = "работает" if running else "не отвечает"
            lines.append(f"{lamp} {name} — {state_text} (внешняя служба)")
            continue
        template = _STATUS_LINE.get(svc.get("status", ""), f"{LAMP_GRAY} {{name}} — {{status}}")
        line = template.format(
            name=name,
            status=svc.get("status", "?"),
            pid=svc.get("pid") or "—",
            restarts=svc.get("restarts", 0),
        )
        if svc.get("status") == "running":
            line += _since_suffix(svc.get("started_at"))
        lines.append(line)
    return "\n".join(lines)


def render_node_card_summary(state: dict) -> str:
    return f"Служб: {len(state.get('services', []))}"


def _assign_buttons(
    subscription: Subscription,
    node_actions: Sequence[ActionSpec],
    assigned: Sequence[str],
    node_id: str | None = None,
) -> list[InlineKeyboardButton]:
    if not subscription.allows_action(ACTION_ASSIGN, NODE_SERVICE):
        return []
    assign = next((a for a in node_actions if a.id == ACTION_ASSIGN), None)
    if assign is None or not assign.params:
        return []
    return [
        button(f"➕ Назначить {name}", commands.action_callback(ACTION_ASSIGN, name, node_id))
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
    if subscription is None:
        return None
    has_nodes_right = subscription.allows_command(commands.NODES.name)
    buttons: list[InlineKeyboardButton] = []
    base = status_view.build_status_keyboard(subscription, monitor_actions, node_id)
    if base is not None:
        buttons.extend(b for row in base.inline_keyboard for b in row)
    buttons.extend(_simple_action_buttons(subscription, node_actions, node_id))
    if not has_nodes_right:
        buttons.extend(_assign_buttons(subscription, node_actions, service_names, node_id))
    grid = actions.rows(buttons)
    rows: list[list[InlineKeyboardButton]] = list(grid.inline_keyboard) if grid else []

    segment = node_segment(node_id)
    rows.append(refresh_row(commands.panel_callback(commands.SWARM_NODE_CODE, segment)))
    if has_nodes_right:
        rows.append(
            [
                button(
                    f"⚙️ Службы ({len(service_names)})",
                    commands.panel_callback(commands.SWARM_SVCS_CODE, segment, 0),
                )
            ]
        )
        rows.append(back_row(commands.panel_callback(commands.SWARM_NODES_CODE, 0)))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _actions_of(description: ServiceDescription | None) -> tuple[ActionSpec, ...]:
    return description.actions if description is not None else ()


async def _node_actions(node_link: ServiceLink, node_id: str | None) -> Sequence[ActionSpec]:
    if node_id:
        return _actions_of(await node_link.describe(dst=node_address(node_id)))
    return await node_link.actions()


async def build_node_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    node_id: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    monitor_dst = status_view.monitor_dst(node_id)
    state, monitor_summary, monitor_desc, node_actions = await asyncio.gather(
        _node_state(node_link, dst=node_address(node_id)),
        status_view.build_summary_text(node_link, dst=monitor_dst),
        node_link.describe(dst=monitor_dst),
        _node_actions(node_link, node_id),
    )
    if state is None:
        return node_unavailable_text(node_id), None

    service_names = [s.get("name", "?") for s in state.get("services", [])]
    has_nodes_right = subscription is not None and subscription.allows_command(commands.NODES.name)
    services_section = (
        render_node_card_summary(state) if has_nodes_right else render_services_block(state)
    )
    text = "\n\n".join([monitor_summary, render_node_card_header(state), services_section])
    keyboard = build_node_card_keyboard(
        subscription, _actions_of(monitor_desc), service_names, node_actions, node_id
    )
    return text, keyboard


def build_services_list_view(
    state: dict,
    node_id: str | None,
    offset: int,
    subscription: Subscription | None = None,
    node_actions: Sequence[ActionSpec] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    services = state.get("services", [])
    node_name = state.get("node") or node_id or "?"
    lines = [f"⚙️ <b>Службы «{escape(node_name)}»</b> ({len(services)})", ""]
    if not services:
        lines.append("Службы не назначены.")
    page = services[offset : offset + SERVICES_PAGE_SIZE]
    for svc in page:
        lines.append(f"• {escape(svc.get('name', '?'))} — {_status_text(svc)}")

    segment = node_segment(node_id)
    buttons = [
        [
            button(
                f"⚙️ {svc.get('name', '?')}"[:MAX_NAME_LEN],
                commands.panel_callback(commands.SWARM_SVC_CODE, segment, svc.get("name", "?")),
            )
        ]
        for svc in page
    ]
    if subscription is not None:
        assigned = [s.get("name", "?") for s in services]
        assign_grid = actions.rows(_assign_buttons(subscription, node_actions, assigned, node_id))
        if assign_grid:
            buttons.extend(assign_grid.inline_keyboard)
    nav = nav_row(
        offset,
        SERVICES_PAGE_SIZE,
        len(services),
        lambda o: commands.panel_callback(commands.SWARM_SVCS_CODE, segment, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(back_row(commands.panel_callback(commands.SWARM_NODE_CODE, segment)))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_services_page_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    node_id: str | None,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    state = await _node_state(node_link, dst=node_address(node_id))
    if state is None:
        return node_unavailable_text(node_id), None
    offset = clamp_offset(offset, SERVICES_PAGE_SIZE, len(state.get("services", [])))
    node_actions: Sequence[ActionSpec] = ()
    if subscription is not None:
        node_actions = await _node_actions(node_link, node_id)
    return build_services_list_view(state, node_id, offset, subscription, node_actions)


def render_service_card(node_name: str, svc: dict) -> str:
    node_display = node_links.node_command(node_name) or escape(node_name)
    lines = [
        f"⚙️ <b>Служба {escape(svc.get('name', '?'))}</b> · нода {node_display}",
        "",
        f"Статус: {_status_text(svc)}"
        + (f", pid {svc['pid']}" if svc.get("pid") else "")
        + _since_suffix(svc.get("started_at") if svc.get("status") == "running" else None),
    ]
    if svc.get("external"):
        lines.append("Запуском управляет не нода (внешняя служба).")
        return "\n".join(lines)
    lines.append(f"Рестартов после падений: {svc.get('restarts', 0)}")
    if svc.get("last_exit_code") is not None:
        lines.append(f"Последний код выхода: {svc['last_exit_code']}")
    return "\n".join(lines)


def build_service_card_keyboard(
    subscription: Subscription | None,
    node_actions: Sequence[ActionSpec],
    service_name: str,
    node_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    if subscription is None:
        return None
    base = actions.build_choice_keyboard(
        subscription, node_actions, NODE_SERVICE, service_name, node_id
    )
    rows: list[list[InlineKeyboardButton]] = list(base.inline_keyboard) if base else []
    segment = node_segment(node_id)
    rows.append(
        refresh_row(commands.panel_callback(commands.SWARM_SVC_CODE, segment, service_name))
    )
    rows.append(back_row(commands.panel_callback(commands.SWARM_SVCS_CODE, segment, 0)))
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_service_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    service_name: str,
    node_id: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    state = await _node_state(node_link, dst=node_address(node_id))
    if state is None:
        return node_unavailable_text(node_id), None
    svc = next((s for s in state.get("services", []) if s.get("name") == service_name), None)
    if svc is None:
        return f"Служба «{service_name}» не найдена на ноде.", None
    node_actions = await _node_actions(node_link, node_id)
    keyboard = build_service_card_keyboard(subscription, node_actions, service_name, node_id)
    return render_service_card(state.get("node", "?"), svc), keyboard


async def _node_state(node_link: ServiceLink, dst: Address | None = None) -> dict | None:
    try:
        return await node_link.get_state(dst=dst)
    except SERVICE_CALL_ERRORS:
        return None
