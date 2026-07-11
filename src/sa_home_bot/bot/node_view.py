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
from sa_home_bot.proto.messages import ActionSpec, Address, ProtoError
from sa_home_bot.runtime import format_duration
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

# Лампочки статуса — единый словарь для служб (running/restarting/stopped) и
# пиров (alive/dead). ⚪ — статус неизвестен (нет данных).
LAMP_GREEN = "🟢"
LAMP_ORANGE = "🟠"
LAMP_RED = "🔴"
LAMP_GRAY = "⚪"

_STATUS_LINE = {
    "running": f"{LAMP_GREEN} <b>{{name}}</b> — работает, pid {{pid}}, рестартов {{restarts}}",
    "restarting": (
        f"{LAMP_ORANGE} <b>{{name}}</b> — упала, перезапускается (рестартов {{restarts}})"
    ),
    "stopped": f"{LAMP_RED} <b>{{name}}</b> — остановлена",
}

_CARD_STATUS = {
    "running": f"{LAMP_GREEN} работает",
    "restarting": f"{LAMP_ORANGE} перезапускается",
    "stopped": f"{LAMP_RED} остановлена",
}


def _peer_lamp(alive: bool) -> str:
    return LAMP_GREEN if alive else LAMP_RED


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
            f"{LAMP_GREEN} <b>{state.get('node', '?')}</b> (эта нода) — "
            f"службы: {running}/{len(services)} работают"
        )
        for peer in state.get("peers", []):
            lamp = _peer_lamp(bool(peer.get("alive")))
            note = "" if peer.get("alive") else " — не в сети"
            lines.append(f"{lamp} <b>{peer.get('id', '?')}</b>{note}")
    if wake is not None and wake.mac:
        lines.append(REMOTE_STUB_TEXT)
    return "\n".join(lines)


def build_nodes_list_keyboard(
    subscription: Subscription | None,
    node_name: str | None,
    peers: Sequence[dict] = (),
    wake: WakeConfig | None = None,
) -> InlineKeyboardMarkup | None:
    if subscription is None:
        return None
    buttons: list[InlineKeyboardButton] = []
    # «st:nodecard[…]» проверяется правом STATUS (см. commands._ALL_CALLBACK_ACTIONS)
    # и для локальной карточки, и для карточек пиров — единое правило.
    if subscription.allows_command(commands.STATUS.name):
        if node_name:
            buttons.append(
                InlineKeyboardButton(
                    text=f"📋 Карточка {node_name}",
                    callback_data=f"{commands.CALLBACK_PREFIX}:{commands.NODE_CARD_CODE}",
                )
            )
        buttons.extend(
            InlineKeyboardButton(
                text=f"📋 Карточка {peer.get('id', '?')}",
                callback_data=(
                    f"{commands.CALLBACK_PREFIX}:{commands.NODE_CARD_CODE}:{peer.get('id', '')}"
                ),
            )
            for peer in peers
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
    peers = state.get("peers", []) if state is not None else []
    return (
        render_nodes_list(state, wake),
        build_nodes_list_keyboard(subscription, node_name, peers, wake),
    )


# --- Карточка ноды (= /status + службы) -------------------------------------


def render_services_block(state: dict) -> str:
    lines = [f"<b>Службы ноды {state.get('node', '?')}</b> (v{state.get('version', '?')}):"]
    services = state.get("services", [])
    if not services:
        lines.append("Службы не назначены.")
        return "\n".join(lines)
    for svc in services:
        template = _STATUS_LINE.get(
            svc.get("status", ""), f"{LAMP_GRAY} <b>{{name}}</b> — {{status}}"
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


# --- Карточка удалённой ноды (read-only, «спроси любого») -------------------


def render_remote_node_card(state: dict) -> str:
    lines = [f"🕸 <b>Нода {state.get('node', '?')}</b> (v{state.get('version', '?')})"]
    uptime_bits = []
    if state.get("system_uptime_s") is not None:
        uptime_bits.append(f"система {format_duration(state['system_uptime_s'])}")
    if state.get("uptime_s") is not None:
        uptime_bits.append(f"нода {format_duration(state['uptime_s'])}")
    if uptime_bits:
        lines.append("Аптайм: " + " · ".join(uptime_bits))
    lines.append("")
    lines.append(render_services_block(state))
    return "\n".join(lines)


def build_remote_node_card_keyboard(
    subscription: Subscription | None,
) -> InlineKeyboardMarkup | None:
    if subscription is None or not subscription.allows_command(commands.NODES.name):
        return None
    return _rows(
        [
            InlineKeyboardButton(
                text="🔙 Список нод",
                callback_data=f"{commands.CALLBACK_PREFIX}:{commands.NODES_CODE}",
            )
        ]
    )


async def build_remote_node_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    node_id: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    state = await _node_state(node_link, dst=Address(node=node_id, service=NODE_SERVICE))
    if state is None:
        return f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит).", None
    return render_remote_node_card(state), build_remote_node_card_keyboard(subscription)


# --- Карточка службы ---------------------------------------------------------


def render_service_card(node_name: str, svc: dict) -> str:
    status = _CARD_STATUS.get(svc.get("status", ""), f"{LAMP_GRAY} {svc.get('status', '?')}")
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


async def _node_state(node_link: ServiceLink, dst: Address | None = None) -> dict | None:
    try:
        return await node_link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        return None
