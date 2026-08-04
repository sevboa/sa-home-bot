"""/swarm (алиас /nodes) — панель роя, иерархия «sw_*» в одном сообщении.

По образцу bot/handlers/invites.py::on_guest_screen: bot/swarm_panel.py и
bot/node_view.py строят текст и клавиатуру каждого экрана (чистые функции),
здесь — разбор callback'а, сетевые вызовы и редрав (``edit_text``). Права уже
проверены CallbackAuthorizationMiddleware (см. таблицу кодов в commands.py).
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot import wake_core
from sa_home_bot.bot import apps_view, commands, node_view, swarm_panel, swarm_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="swarm_panel")


def _parse_int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _node_id(part: str | None) -> str | None:
    """Сегмент callback_data → node_id («-» — своя нода, см. node_view._node_part)."""
    return None if not part or part == "-" else part


async def _redraw(callback: CallbackQuery, text: str, keyboard) -> None:
    if callback.message is None:
        return
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


async def _collect_reports(
    node_link: ServiceLink, store: Store
) -> tuple[dict | None, list[wake_core.NodeReport]]:
    """Своё состояние + веерный опрос роя (с монитором — панели/списку нод
    нужны температуры/статусы), плюс запоминание WoL-реквизитов, как раньше
    делал swarm_view.build_swarm_view."""
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
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


@router.message(Command(commands.SWARM.name, commands.NODES.name))
async def cmd_swarm(
    message: Message,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    store: Store,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    own_state, reports = await _collect_reports(node_link, store)
    if own_state is None:
        await message.answer(node_view.NODE_DOWN_TEXT)
        return
    app_actions = await apps_link.actions()
    wake_buttons = await _wake_buttons(subscription, store, config, reports)
    text, keyboard = swarm_panel.build_panel_view(
        reports, app_actions, datetime.now(tz=UTC), wake_buttons
    )
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
    code = parts[1] if len(parts) > 1 else ""

    if code == commands.SWARM_PANEL_CODE:
        own_state, reports = await _collect_reports(node_link, store)
        if own_state is None:
            await _redraw(callback, node_view.NODE_DOWN_TEXT, None)
            await callback.answer()
            return
        app_actions = await apps_link.actions()
        wake_buttons = await _wake_buttons(subscription, store, config, reports)
        text, keyboard = swarm_panel.build_panel_view(
            reports, app_actions, datetime.now(tz=UTC), wake_buttons
        )
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_SKILLS_CODE:
        offset = _parse_int(parts[2] if len(parts) > 2 else None)
        if subscription is None:
            await callback.answer()
            return
        app_actions = await apps_link.actions()
        text, keyboard = swarm_panel.build_skills_list_view(app_actions, subscription, offset)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_SKILL_CODE:
        app_id = parts[2] if len(parts) > 2 else ""
        if subscription is None or not subscription.allows_action(
            app_id, apps_view.APPS_SERVICE
        ):
            await callback.answer("⛔️ Недоступно", show_alert=True)
            return
        skill_text, skill_keyboard = await apps_view.run_app_skill(
            apps_link, subscription, app_id
        )
        text, keyboard = swarm_panel.build_skill_card_view(skill_text, skill_keyboard)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_NODES_CODE:
        offset = _parse_int(parts[2] if len(parts) > 2 else None)
        own_state, reports = await _collect_reports(node_link, store)
        if own_state is None:
            await _redraw(callback, node_view.NODE_DOWN_TEXT, None)
            await callback.answer()
            return
        wakeable_ids = (
            await swarm_view.offline_wakeable_ids(subscription, store, reports)
            if subscription is not None
            else []
        )
        text, keyboard = swarm_panel.build_nodes_list_view(
            reports, offset, wakeable_ids, own_state.get("node")
        )
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_NODE_CODE:
        node_id = _node_id(parts[2] if len(parts) > 2 else None)
        text, keyboard = await node_view.build_node_card_view(node_link, subscription, node_id)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_SVCS_CODE:
        node_id = _node_id(parts[2] if len(parts) > 2 else None)
        offset = _parse_int(parts[3] if len(parts) > 3 else None)
        text, keyboard = await node_view.build_services_page_view(node_link, node_id, offset)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_SVC_CODE:
        node_id = _node_id(parts[2] if len(parts) > 2 else None)
        name = parts[3] if len(parts) > 3 else ""
        text, keyboard = await node_view.build_service_card_view(
            node_link, subscription, name, node_id
        )
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.SWARM_UPDATE_CODE:
        # Проверка идёт через LAN/оверлей до нескольких секунд (веерный опрос
        # wake_core.collect_reports внутри check_updates_summary) — отвечаем
        # тостом сразу, редрав — по готовности.
        await callback.answer("Проверяю обновления…")
        summary = await wake_core.check_updates_summary(node_link)
        text, keyboard = swarm_panel.build_update_result_view(summary)
        await _redraw(callback, text, keyboard)
        return

    await callback.answer()
