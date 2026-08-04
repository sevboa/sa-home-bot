"""Иерархия /swarm (алиас /nodes) — «телефонное меню» роя, по образцу
/guests (bot/guests_view.py): панель → умения/ноды → карточка → под-экраны,
всё одним сообщением (``edit_text``), «Назад» ведёт на уровень выше.

Модуль только рендерит (текст + клавиатура) из уже готовых данных — сетевые
вызовы (сбор reports, describe apps, проверка обновлений) и запись
состояния — в bot/handlers/swarm_panel.py. Карточки ноды/службы и их
клавиатуры — в bot/node_view.py (общие с /status, /node_<id>, /svc_*);
здесь — только панель, список умений/карточка умения, список нод.

Соглашение о node_id в callback_data — как в node_view.py: «-» для своей
ноды (пустой сегмент нельзя, дальше в коде почти всегда идёт ещё один
сегмент), иначе — буквальный node_id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import apps_view, commands, swarm_view
from sa_home_bot.bot.pagination import nav_row
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.version import version_key
from sa_home_bot.wake_core import NodeReport

# Нод/умений на страницу немного (домашний рой, не десятки машин/приложений)
# — как у /guests (guests_view.GUEST_PAGE_SIZE).
NODES_PAGE_SIZE = 5
SKILLS_PAGE_SIZE = 6


def _cb(code: str, *parts: object) -> str:
    return ":".join([commands.CALLBACK_PREFIX, code, *(str(p) for p in parts)])


def _node_part(node_id: str, own_node_id: str | None) -> str:
    return "-" if node_id == own_node_id else node_id


# --- Панель -------------------------------------------------------------


def _version_line(reports: Sequence[NodeReport]) -> str | None:
    versions = {
        r.node_id: r.state["version"]
        for r in reports
        if r.state is not None and r.state.get("version")
    }
    if not versions:
        return None
    latest = max(versions.values(), key=version_key)
    if all(v == latest for v in versions.values()):
        return f"Версия: v{latest}"
    return f"Версия: ≤v{latest} (есть отставшие — см. «Проверить обновления»)"


def _unique_service_count(reports: Sequence[NodeReport]) -> int:
    names = {
        s.get("name")
        for r in reports
        if r.state is not None
        for s in r.state.get("services", [])
        if s.get("name")
    }
    return len(names)


def _unique_skill_count(app_actions: Sequence[ActionSpec]) -> int:
    return sum(1 for a in app_actions if not a.params)


def build_panel_view(
    reports: Sequence[NodeReport],
    app_actions: Sequence[ActionSpec],
    now: datetime,
    wake_buttons: Sequence[InlineKeyboardButton] = (),
) -> tuple[str, InlineKeyboardMarkup]:
    """Сводная панель: сколько нод/в сети, версия (единая или с разъездом),
    сколько уникальных служб и умений в рое, последний сбой (если был).

    ``wake_buttons`` — уже готовые кнопки будильника (ручная из [wake] +
    точечные на уснувшие ноды с известными реквизитами), собирает вызывающий
    через swarm_view.wake_rows/offline_wake_rows (там же и store).
    """
    total = len(reports)
    online = sum(1 for r in reports if r.alive and r.state is not None)
    lines = [f"{swarm_view.SWARM_HEADER}: {total} нод, в сети {online}"]
    version_line = _version_line(reports)
    if version_line:
        lines.append(version_line)
    lines.append(f"Служб: {_unique_service_count(reports)} уникальных")
    lines.append(f"Умений: {_unique_skill_count(app_actions)} уникальных")
    failure = swarm_view.last_failure_line(reports, now)
    if failure:
        lines.append(failure)

    buttons: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="🧩 Умения", callback_data=_cb(commands.SWARM_SKILLS_CODE, 0)
            )
        ],
        [InlineKeyboardButton(text="🖥 Ноды", callback_data=_cb(commands.SWARM_NODES_CODE, 0))],
        [
            InlineKeyboardButton(
                text="🔄 Проверить обновления", callback_data=_cb(commands.SWARM_UPDATE_CODE)
            )
        ],
    ]
    buttons.extend([b] for b in wake_buttons)
    buttons.append(
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=_cb(commands.SWARM_PANEL_CODE))]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Список умений --------------------------------------------------------


def _visible_skills(
    app_actions: Sequence[ActionSpec], subscription: Subscription
) -> list[ActionSpec]:
    return [
        a
        for a in app_actions
        if not a.params and subscription.allows_action(a.id, apps_view.APPS_SERVICE)
    ]


def build_skills_list_view(
    app_actions: Sequence[ActionSpec], subscription: Subscription, offset: int
) -> tuple[str, InlineKeyboardMarkup]:
    skills = _visible_skills(app_actions, subscription)
    lines = [f"🧩 <b>Умения</b> ({len(skills)})", ""]
    if not skills:
        lines.append("Нет доступных умений.")
    page = skills[offset : offset + SKILLS_PAGE_SIZE]
    buttons = [
        [
            InlineKeyboardButton(
                text=action.title, callback_data=_cb(commands.SWARM_SKILL_CODE, action.id)
            )
        ]
        for action in page
    ]
    nav = nav_row(
        offset, SKILLS_PAGE_SIZE, len(skills), lambda o: _cb(commands.SWARM_SKILLS_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.SWARM_PANEL_CODE))]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_skill_card_view(
    text: str, keyboard: InlineKeyboardMarkup | None
) -> tuple[str, InlineKeyboardMarkup]:
    """Тонкая обёртка над apps_view.run_app_skill: та же карточка умения,
    плюс «⬅️ Назад» к списку умений панели. apps_view.py не трогаем — у
    карточки есть и другой, не-панельный путь (голая команда умения,
    bot/handlers/apps.py), которому чужая кнопка с правом `nodes` не нужна.
    """
    rows = list(keyboard.inline_keyboard) if keyboard is not None else []
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.SWARM_SKILLS_CODE, 0))]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# --- Список нод -------------------------------------------------------


def build_nodes_list_view(
    reports: Sequence[NodeReport],
    offset: int,
    wakeable_ids: Sequence[str],
    own_node_id: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Список нод — как guests_view.build_list_view: строка на ноду
    (swarm_view.node_line — лампа/версия/службы/температуры), кнопка выбора,
    плюс точечная кнопка будильника у спящих нод с известными реквизитами
    (``wakeable_ids`` — swarm_view.offline_wakeable_ids)."""
    lines = [f"🖥 <b>Ноды</b> ({len(reports)})", ""]
    page = reports[offset : offset + NODES_PAGE_SIZE]
    lines.extend(swarm_view.node_line(r) for r in page)
    wakeable = set(wakeable_ids)

    buttons: list[list[InlineKeyboardButton]] = []
    for r in page:
        node_part = _node_part(r.node_id, own_node_id)
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"🖥 {r.node_id}",
                    callback_data=_cb(commands.SWARM_NODE_CODE, node_part),
                )
            ]
        )
        if r.node_id in wakeable:
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=f"🔌 Разбудить {r.node_id}",
                        callback_data=commands.wake_callback(r.node_id),
                    )
                ]
            )
    nav = nav_row(
        offset, NODES_PAGE_SIZE, len(reports), lambda o: _cb(commands.SWARM_NODES_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔄 Обновить", callback_data=_cb(commands.SWARM_NODES_CODE, offset)
            )
        ]
    )
    buttons.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.SWARM_PANEL_CODE))]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Проверка обновлений --------------------------------------------------


def build_update_result_view(summary_text: str) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🔄 <b>Обновления роя</b>\n\n{summary_text}"
    buttons = [
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.SWARM_PANEL_CODE))]
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)
