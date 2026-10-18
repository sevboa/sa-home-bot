from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from sa_home_bot import wake_core
from sa_home_bot.bot import apps_view, commands, node_view, swarm_panel, swarm_view
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.callback_data import int_or
from sa_home_bot.bot.ui.redraw import redraw
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)

router = Router(name="swarm_panel")

ScreenView = tuple[str, InlineKeyboardMarkup | None]


def _part(parts: list[str], index: int) -> str | None:
    return parts[index] if len(parts) > index else None


async def _collect_reports(
    node_link: ServiceLink, store: Store
) -> tuple[dict | None, list[wake_core.NodeReport]]:
    try:
        own_state = await node_link.get_state()
    except SERVICE_CALL_ERRORS:
        return None, []
    reports = await wake_core.collect_reports(node_link, own_state, with_monitor=True)
    await swarm_view.remember_wake_info(store, reports)
    return own_state, reports


async def _wake_buttons(
    subscription: Subscription | None,
    store: Store,
    config: Settings,
    reports: list[wake_core.NodeReport],
):
    if subscription is None:
        return []
    return swarm_view.wake_rows(subscription, config.wake) + await swarm_view.offline_wake_rows(
        subscription, store, reports
    )


async def _panel_view(
    node_link: ServiceLink,
    apps_link: ServiceLink,
    store: Store,
    config: Settings,
    subscription: Subscription | None,
) -> ScreenView:
    own_state, reports = await _collect_reports(node_link, store)
    if own_state is None:
        return node_view.NODE_DOWN_TEXT, None
    app_actions = await apps_link.actions()
    wake_buttons = await _wake_buttons(subscription, store, config, reports)
    return swarm_panel.build_panel_view(reports, app_actions, utc_now(), wake_buttons)


async def _nodes_view(
    node_link: ServiceLink, store: Store, subscription: Subscription | None, offset: int
) -> ScreenView:
    own_state, reports = await _collect_reports(node_link, store)
    if own_state is None:
        return node_view.NODE_DOWN_TEXT, None
    wakeable_ids = (
        await swarm_view.offline_wakeable_ids(subscription, store, reports)
        if subscription is not None
        else []
    )
    return swarm_panel.build_nodes_list_view(reports, offset, wakeable_ids, own_state.get("node"))


async def _render_screen(
    code: str,
    parts: list[str],
    node_link: ServiceLink,
    apps_link: ServiceLink,
    store: Store,
    config: Settings,
    subscription: Subscription | None,
) -> ScreenView | None:
    if code == commands.SWARM_PANEL_CODE:
        return await _panel_view(node_link, apps_link, store, config, subscription)
    if code == commands.SWARM_SKILLS_CODE:
        if subscription is None:
            return None
        app_actions = await apps_link.actions()
        offset = int_or(_part(parts, 2))
        return swarm_panel.build_skills_list_view(app_actions, subscription, offset)
    if code == commands.SWARM_SKILL_CODE:
        skill_text, skill_keyboard = await apps_view.run_app_skill(
            apps_link, subscription, _part(parts, 2) or ""
        )
        return swarm_panel.build_skill_card_view(skill_text, skill_keyboard)
    if code == commands.SWARM_NODES_CODE:
        return await _nodes_view(node_link, store, subscription, int_or(_part(parts, 2)))
    node_id = node_view.node_from_segment(_part(parts, 2))
    if code == commands.SWARM_NODE_CODE:
        return await node_view.build_node_card_view(node_link, subscription, node_id)
    if code == commands.SWARM_SVCS_CODE:
        return await node_view.build_services_page_view(
            node_link, subscription, node_id, int_or(_part(parts, 3))
        )
    if code == commands.SWARM_SVC_CODE:
        return await node_view.build_service_card_view(
            node_link, subscription, _part(parts, 3) or "", node_id
        )
    return None


@router.message(Command(commands.SWARM.name, commands.NODES.name))
async def cmd_swarm(
    message: Message,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    store: Store,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    text, keyboard = await _panel_view(node_link, apps_link, store, config, subscription)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith(f"{commands.CALLBACK_PREFIX}:sw_"))
async def on_swarm_screen(
    callback: CallbackQuery,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    store: Store,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    parts = (callback.data or "").split(":")
    code = _part(parts, 1) or ""

    if code == commands.SWARM_UPDATE_CODE:
        await callback.answer("Проверяю обновления…")
        summary = await wake_core.check_updates_summary(node_link)
        text, keyboard = swarm_panel.build_update_result_view(summary)
        await redraw(callback, text, keyboard)
        return

    if code == commands.SWARM_SKILL_CODE and (
        subscription is None
        or not subscription.allows_action(_part(parts, 2) or "", apps_view.APPS_SERVICE)
    ):
        await callback.answer("⛔️ Недоступно", show_alert=True)
        return

    view = await _render_screen(code, parts, node_link, apps_link, store, config, subscription)
    if view is not None:
        await redraw(callback, *view)
    await callback.answer()
