"""Управление нодами — иерархия карточек (ARCHITECTURE §11).

Список нод («st:nodes», команда /nodes) → карточка ноды («st:nodecard»,
= /status: мониторинг машины + службы под супервизией) → карточка службы
(«st:svc:<имя>» — данные службы + кнопки управления из describe ноды).
Кнопки управления строятся динамически: действия — из describe, права —
`действие@node`; wake — по праву команды `wake`.

Рой равноправен (§11 п. 1): карточки и кнопки пиров («st:nodecard:<id>»,
«st:svc:<имя>:<id>») устроены так же, как у своей ноды, только несут
node_id пира в callback — фактическое исполнение маршрутизирует нода
(«спроси любого», п. 2), боту не нужно знать, кто отвечает.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands, status_view
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
    return actions.rows(buttons)


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


_ACTION_ASSIGN = "assign"


def _assign_buttons(
    subscription: Subscription,
    node_actions: Sequence[ActionSpec],
    assigned: Sequence[str],
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
            callback_data=commands.action_callback(_ACTION_ASSIGN, name),
        )
        for name in (assign.params[0].choices or ())
        if name not in assigned
    ]


def build_node_card_keyboard(
    subscription: Subscription | None,
    monitor_actions: Sequence[ActionSpec],
    service_names: Sequence[str],
    node_actions: Sequence[ActionSpec] = (),
) -> InlineKeyboardMarkup | None:
    """Представления мониторинга + действия монитора + карточки служб + питание."""
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
    buttons.extend(_power_buttons(subscription, node_actions))
    buttons.extend(_assign_buttons(subscription, node_actions, service_names))
    return actions.rows(buttons)


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
        subscription, await monitor_link.actions(), service_names, await node_link.actions()
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
    node_id: str,
    service_names: Sequence[str] = (),
    node_actions: Sequence[ActionSpec] = (),
) -> InlineKeyboardMarkup | None:
    """Карточки служб + питание пира — те же права и кнопки, что у своей ноды
    (ARCHITECTURE §11 п. 1: рой равноправен, управление не зависит от того,
    чья это нода физически) — только каждая кнопка несёт node_id пира."""
    if subscription is None or not subscription.allows_command(commands.NODES.name):
        return None
    buttons: list[InlineKeyboardButton] = [
        InlineKeyboardButton(
            text=f"⚙️ {name}",
            callback_data=(
                f"{commands.CALLBACK_PREFIX}:{commands.SERVICE_CARD_CODE}:{name}:{node_id}"
            ),
        )
        for name in service_names
    ]
    buttons.extend(_power_buttons(subscription, node_actions, node_id))
    buttons.append(
        InlineKeyboardButton(
            text="🔙 Список нод",
            callback_data=f"{commands.CALLBACK_PREFIX}:{commands.NODES_CODE}",
        )
    )
    return actions.rows(buttons)


async def build_remote_node_card_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    node_id: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    node_dst = Address(node=node_id, service=NODE_SERVICE)
    state = await _node_state(node_link, dst=node_dst)
    if state is None:
        return f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит).", None
    # Датчики — через тот же node_link (маршрутизация «спроси любого»):
    # если у пира нет назначения monitor, get_state ответит unknown_dst,
    # build_summary_text превратит это в честный MONITOR_DOWN_TEXT.
    monitor_summary = await status_view.build_summary_text(
        node_link, dst=Address(node=node_id, service=status_view.MONITOR_SERVICE)
    )
    service_names = [s.get("name", "?") for s in state.get("services", [])]
    desc = await node_link.describe(dst=node_dst)
    node_actions = desc.actions if desc is not None else ()
    text = f"{monitor_summary}\n\n{render_remote_node_card(state)}"
    keyboard = build_remote_node_card_keyboard(subscription, node_id, service_names, node_actions)
    return text, keyboard


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
