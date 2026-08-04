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

import asyncio
from collections.abc import Sequence
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands, node_links, status_view
from sa_home_bot.bot.pagination import clamp_offset, nav_row
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ActionSpec, Address, ProtoError
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.models import Subscription

NODE_SERVICE = "node"

# Список служб на глубоком уровне (пагинация) без имени/имени-эмодзи в
# запасе под текст кнопки — как в guests_view.
_MAX_NAME_LEN = 40
SERVICES_PAGE_SIZE = 6


def _node_part(node_id: str | None) -> str:
    """node_id для сегмента callback_data: «-» для своей ноды (пусто нельзя —
    после него в «sw_svcs»/«sw_svc» всегда идёт ещё один сегмент)."""
    return node_id or "-"

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


def _simple_action_buttons(
    subscription: Subscription,
    node_actions: Sequence[ActionSpec],
    node_id: str | None = None,
) -> list[InlineKeyboardButton]:
    """Кнопки действий без параметров (питание, restart_node, самообновление
    check_update/update и т.п.) — локально или на пире (node_id); право —
    то же `действие@node`, что и у служб. Название не про питание конкретно —
    любое зеро-параметрическое действие ноды попадает сюда автоматически."""
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
    update = state.get("update")
    if update and update.get("restart_required"):
        lines.append(
            f"⚠️ Обновлено до v{update.get('installed')} — ждёт перезапуска "
            f"(restart_node)"
        )
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
        name = _svc_display(node_id, svc.get("name", "?"))
        # Внешне управляемая (llm): pid/рестарты/время старта чужого процесса
        # ноде неизвестны — не показываем «pid —, рестартов 0», это читалось
        # бы как поломка. Только честный статус по связи со службой.
        if svc.get("external"):
            lamp = LAMP_GREEN if svc.get("status") == "running" else LAMP_RED
            state_text = "работает" if svc.get("status") == "running" else "не отвечает"
            lines.append(f"{lamp} {name} — {state_text} (внешняя служба)")
            continue
        template = _STATUS_LINE.get(
            svc.get("status", ""), f"{LAMP_GRAY} {{name}} — {{status}}"
        )
        line = template.format(
            name=name,
            status=svc.get("status", "?"),
            pid=svc.get("pid") or "—",
            restarts=svc.get("restarts", 0),
        )
        if svc.get("status") == "running":
            line += _fmt_since(svc.get("started_at"))
        lines.append(line)
    return "\n".join(lines)


def render_node_card_summary(state: dict) -> str:
    """Короткая замена render_services_block — просто счётчик, без деталей.

    Показывается тем, у кого есть право `nodes` (и вместе с ним — отдельный
    постраничный экран со списком служб и кнопка «⚙️ Службы» на карточке,
    см. build_node_card_keyboard/build_services_list_view). У кого права нет
    — как раньше, полный список инлайн (render_services_block): другого
    способа увидеть его у них нет, отбирать не за чем.
    """
    return f"Служб: {len(state.get('services', []))}"


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

    Действия (представления/действия монитора + питание/самообновление) идут
    сеткой по 2 в ряд, как раньше. Плюс — отдельными строками внизу:
    «🔄 Обновить» (та же карточка, свежие данные — доступно всем, ничего
    нового не открывает) и, только при праве `nodes`, «⚙️ Службы» (переход
    на отдельный постраничный экран, bot/handlers/swarm_panel.py) и
    «⬅️ Назад» (к списку нод той же панели). У кого права `nodes` нет —
    список служб виден как раньше, инлайн в тексте карточки
    (render_services_block), без кнопок навигации по нему.

    «➕ Назначить X» — на карточке только у тех, у кого нет права `nodes` (им
    больше некуда её деть, свой единственный экран — эта карточка); у кого
    право есть, кнопка переехала глубже, на экран «Службы»
    (build_services_list_view) — там ей самое место, среди уже назначенных.
    """
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

    node_part = _node_part(node_id)
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=f"{commands.CALLBACK_PREFIX}:{commands.SWARM_NODE_CODE}:{node_part}",
            )
        ]
    )
    if has_nodes_right:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⚙️ Службы ({len(service_names)})",
                    callback_data=(
                        f"{commands.CALLBACK_PREFIX}:{commands.SWARM_SVCS_CODE}:{node_part}:0"
                    ),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data=f"{commands.CALLBACK_PREFIX}:{commands.SWARM_NODES_CODE}:0",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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

    Четыре сетевых запроса (state ноды, state монитора, describe монитора,
    describe/actions ноды) идут ПАРАЛЛЕЛЬНО (asyncio.gather) — важно, чтобы
    карточка пира (путь идёт через свою ноду, лишний хоп) открывалась
    быстро, а не ждала четыре круговых рейса подряд. Каждый из вызовов уже
    сам гасит сетевые ошибки (_node_state / status_view.build_summary_text /
    ServiceLink.describe / ServiceLink.actions — см. их докстринги), поэтому
    gather не нужно оборачивать доп. try/except.
    """
    node_dst = Address(node=node_id, service=NODE_SERVICE) if node_id else None
    monitor_dst = Address(node=node_id, service=status_view.MONITOR_SERVICE)

    state, monitor_summary, monitor_desc, node_desc_or_actions = await asyncio.gather(
        _node_state(node_link, dst=node_dst),
        status_view.build_summary_text(node_link, dst=monitor_dst),
        node_link.describe(dst=monitor_dst),
        node_link.describe(dst=node_dst) if node_id else node_link.actions(),
    )
    if state is None:
        return (
            f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
            if node_id
            else NODE_DOWN_TEXT
        ), None

    monitor_actions = monitor_desc.actions if monitor_desc is not None else ()
    if node_id:
        node_actions = node_desc_or_actions.actions if node_desc_or_actions is not None else ()
    else:
        node_actions = node_desc_or_actions

    service_names = [s.get("name", "?") for s in state.get("services", [])]
    has_nodes_right = subscription is not None and subscription.allows_command(
        commands.NODES.name
    )
    services_section = (
        render_node_card_summary(state) if has_nodes_right else render_services_block(state)
    )
    text = "\n\n".join([monitor_summary, render_node_card_header(state), services_section])
    keyboard = build_node_card_keyboard(
        subscription, monitor_actions, service_names, node_actions, node_id
    )
    return text, keyboard


# --- Список служб ноды (отдельный экран, право `nodes`) ---------------------


def build_services_list_view(
    state: dict,
    node_id: str | None,
    offset: int,
    subscription: Subscription | None = None,
    node_actions: Sequence[ActionSpec] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    """Постраничный список служб — как guests_view.build_perms_view: строка
    на службу + кнопка её карточки, ниже — «➕ Назначить X» на каждое ещё не
    назначенное имя (переехало сюда с карточки ноды — тут её место, среди
    уже назначенных), «⬅️ Назад» на карточку ноды."""
    services = state.get("services", [])
    node_name = state.get("node") or node_id or "?"
    lines = [f"⚙️ <b>Службы «{escape(node_name)}»</b> ({len(services)})", ""]
    if not services:
        lines.append("Службы не назначены.")
    page = services[offset : offset + SERVICES_PAGE_SIZE]
    for svc in page:
        status = _CARD_STATUS.get(svc.get("status", ""), f"{LAMP_GRAY} {svc.get('status', '?')}")
        lines.append(f"• {escape(svc.get('name', '?'))} — {status}")

    node_part = _node_part(node_id)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"⚙️ {svc.get('name', '?')}"[:_MAX_NAME_LEN],
                callback_data=(
                    f"{commands.CALLBACK_PREFIX}:{commands.SWARM_SVC_CODE}:"
                    f"{node_part}:{svc.get('name', '?')}"
                ),
            )
        ]
        for svc in page
    ]
    if subscription is not None:
        assigned = [s.get("name", "?") for s in services]
        assign_grid = actions.rows(
            _assign_buttons(subscription, node_actions, assigned, node_id)
        )
        if assign_grid:
            buttons.extend(assign_grid.inline_keyboard)
    nav = nav_row(
        offset,
        SERVICES_PAGE_SIZE,
        len(services),
        lambda o: f"{commands.CALLBACK_PREFIX}:{commands.SWARM_SVCS_CODE}:{node_part}:{o}",
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"{commands.CALLBACK_PREFIX}:{commands.SWARM_NODE_CODE}:{node_part}",
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_services_page_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    node_id: str | None,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Обёртка над build_services_list_view — сама получает state ноды
    (своей или пира), как build_node_card_view/build_service_card_view.

    node_actions — нужны только ради choices действия `assign` (кнопки
    «➕ Назначить X» на этом экране, см. build_services_list_view), поэтому
    запрашиваются только когда есть подписка (без неё кнопок всё равно не
    будет)."""
    dst = Address(node=node_id, service=NODE_SERVICE) if node_id else None
    state = await _node_state(node_link, dst=dst)
    if state is None:
        return (
            f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
            if node_id
            else NODE_DOWN_TEXT
        ), None
    offset = clamp_offset(offset, SERVICES_PAGE_SIZE, len(state.get("services", [])))
    node_actions: Sequence[ActionSpec] = ()
    if subscription is not None:
        if node_id:
            desc = await node_link.describe(dst=dst)
            node_actions = desc.actions if desc is not None else ()
        else:
            node_actions = await node_link.actions()
    return build_services_list_view(state, node_id, offset, subscription, node_actions)


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
    ]
    if svc.get("external"):
        # Процесс не наш — супервизор его не поднимает, счётчика рестартов и
        # кода выхода у ноды нет (см. node/service.py::_services_state).
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
    """Действия ноды, применимые к этой службе (параметр name из choices),
    плюс «🔄 Обновить» (та же карточка) и «⬅️ Назад» (к списку служб той же
    ноды). node_id — служба на пире: те же кнопки, но с адресом пира.

    Право на сам переход сюда — `nodes` (как /svc_<node>_<svc>, см.
    bot/handlers/node_links.py) — CallbackAuthorizationMiddleware уже
    проверила его до вызова, поэтому «Назад»/«Обновить» здесь безусловны.
    """
    if subscription is None:
        return None
    base = actions.build_choice_keyboard(
        subscription, node_actions, NODE_SERVICE, service_name, node_id
    )
    rows: list[list[InlineKeyboardButton]] = list(base.inline_keyboard) if base else []
    node_part = _node_part(node_id)
    rows.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=(
                    f"{commands.CALLBACK_PREFIX}:{commands.SWARM_SVC_CODE}:"
                    f"{node_part}:{service_name}"
                ),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"{commands.CALLBACK_PREFIX}:{commands.SWARM_SVCS_CODE}:{node_part}:0",
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
