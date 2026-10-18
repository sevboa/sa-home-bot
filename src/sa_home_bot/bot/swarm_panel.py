from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import apps_view, commands, swarm_view
from sa_home_bot.bot.node_view import back_row, node_segment, refresh_row
from sa_home_bot.bot.pagination import nav_row
from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.bot.ui.keyboards import button
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.wake_core import NodeReport

NODES_PAGE_SIZE = 5
SKILLS_PAGE_SIZE = 6


def _cb(code: str, *parts: object) -> str:
    return pack(commands.CALLBACK_PREFIX, code, *parts)


def _version_line(reports: Sequence[NodeReport]) -> str | None:
    versions = swarm_view.node_versions(reports)
    if not versions:
        return None
    latest = swarm_view.latest_version(versions)
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
        [button("🧩 Умения", _cb(commands.SWARM_SKILLS_CODE, 0))],
        [button("🖥 Ноды", _cb(commands.SWARM_NODES_CODE, 0))],
        [button("🔄 Проверить обновления", _cb(commands.SWARM_UPDATE_CODE))],
    ]
    buttons.extend([b] for b in wake_buttons)
    buttons.append(refresh_row(_cb(commands.SWARM_PANEL_CODE)))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


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
    buttons = [[button(action.title, _cb(commands.SWARM_SKILL_CODE, action.id))] for action in page]
    nav = nav_row(
        offset, SKILLS_PAGE_SIZE, len(skills), lambda o: _cb(commands.SWARM_SKILLS_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(back_row(_cb(commands.SWARM_PANEL_CODE)))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_skill_card_view(
    text: str, keyboard: InlineKeyboardMarkup | None
) -> tuple[str, InlineKeyboardMarkup]:
    rows = list(keyboard.inline_keyboard) if keyboard is not None else []
    rows.append(back_row(_cb(commands.SWARM_SKILLS_CODE, 0)))
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_nodes_list_view(
    reports: Sequence[NodeReport],
    offset: int,
    wakeable_ids: Sequence[str],
    own_node_id: str | None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"🖥 <b>Ноды</b> ({len(reports)})", ""]
    ordered = swarm_view.order_by_health(reports)
    page = ordered[offset : offset + NODES_PAGE_SIZE]
    lines.extend(swarm_view.node_line(r) for r in page)
    wakeable = set(wakeable_ids)

    buttons: list[list[InlineKeyboardButton]] = []
    for r in page:
        segment = node_segment(None if r.node_id == own_node_id else r.node_id)
        buttons.append([button(f"🖥 {r.node_id}", _cb(commands.SWARM_NODE_CODE, segment))])
        if r.node_id in wakeable:
            buttons.append(
                [button(swarm_view.wake_node_text(r.node_id), commands.wake_callback(r.node_id))]
            )
    nav = nav_row(
        offset, NODES_PAGE_SIZE, len(reports), lambda o: _cb(commands.SWARM_NODES_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(refresh_row(_cb(commands.SWARM_NODES_CODE, offset)))
    buttons.append(back_row(_cb(commands.SWARM_PANEL_CODE)))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_update_result_view(summary_text: str) -> tuple[str, InlineKeyboardMarkup]:
    text = f"🔄 <b>Обновления роя</b>\n\n{summary_text}"
    return text, InlineKeyboardMarkup(inline_keyboard=[back_row(_cb(commands.SWARM_PANEL_CODE))])
