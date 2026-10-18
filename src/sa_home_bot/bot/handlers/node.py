from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from sa_home_bot.bot import actions, apps_view, commands, node_view
from sa_home_bot.bot.handlers import vpn as vpn_handlers
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.ui.redraw import redraw
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.addresses import MONITOR_SERVICE, NODE_SERVICE, node_address
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

router = Router(name="node")

SELF_SERVICE_NAME = "telegram-bot"
ACTION_RESTART_NODE = "restart_node"
ACTION_CHECK_UPDATE = "check_update"

SELF_RESTART_TEXT = "🔄 Перезапускаюсь, вернусь через минуту."
SELF_STOP_TEXT = (
    "⏹ Останавливаюсь. Запустить меня снова можно только с ноды: "
    "<code>nodectl start telegram-bot</code>."
)

SELF_RESTART_CB_KEY = "self_restart_cb"


def _is_self_shutdown(action_id: str, value: str | None, node_id: str | None) -> bool:
    if node_id is not None:
        return False
    if action_id == ACTION_RESTART_NODE:
        return True
    return action_id in ("stop", "restart") and value == SELF_SERVICE_NAME


async def _handle_self_shutdown(
    callback: CallbackQuery, store: Store, node_link: ServiceLink, action_id: str, value: str | None
) -> None:
    if await store.get_state(SELF_RESTART_CB_KEY) == callback.id:
        log.warning("Повторный callback само-рестарта %s — игнорирую", callback.id)
        await callback.answer()
        return
    await store.set_state(SELF_RESTART_CB_KEY, callback.id)

    await callback.answer()
    await callback.message.answer(SELF_STOP_TEXT if action_id == "stop" else SELF_RESTART_TEXT)
    with contextlib.suppress(*SERVICE_CALL_ERRORS, TimeoutError):
        args = {"name": value} if value else {}
        await node_link.command(action_id, args)


async def _run_node_action(
    node_link: ServiceLink, action_id: str, value: str | None, node_id: str | None
) -> tuple[str | None, dict | None]:
    dst = node_address(node_id)
    action = await actions.find_action(node_link, action_id, dst=dst)
    if action is None:
        return "Действие недоступно.", None
    try:
        result = await node_link.command(action.id, actions.build_args(action, value), dst=dst)
    except ServiceUnavailableError:
        return node_view.node_unavailable_text(node_id), None
    except ProtoError as exc:
        return f"⚠️ Ошибка: {exc.message}", None
    return None, result


def _format_check_update(result: dict) -> str:
    running = result.get("running", "?")
    installed = result.get("installed") or "?"
    latest = str(result.get("latest", "?")).lstrip("v")
    if latest not in ("?", installed):
        return (
            f"⬆️ Доступно обновление: v{latest} (сейчас установлено v{installed}, "
            f"запущено v{running}) — нажмите «Обновить»"
        )
    return f"✅ Обновлений нет — установлена последняя версия (v{installed})"


async def _node_action_screen(
    node_link: ServiceLink,
    subscription: Subscription | None,
    action_id: str,
    value: str | None,
    node_id: str | None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    if action_id in (node_view.ACTION_ASSIGN, node_view.ACTION_UNASSIGN):
        if subscription is not None and subscription.allows_command(commands.NODES.name):
            return await node_view.build_services_page_view(node_link, subscription, node_id, 0)
        return await node_view.build_node_card_view(node_link, subscription, node_id)
    if value is not None:
        return await node_view.build_service_card_view(node_link, subscription, value, node_id)
    return await node_view.build_node_card_view(node_link, subscription, node_id)


async def _handle_node_action(
    callback: CallbackQuery,
    node_link: ServiceLink,
    subscription: Subscription | None,
    action_id: str,
    value: str | None,
    node_id: str | None,
) -> None:
    error, result = await _run_node_action(node_link, action_id, value, node_id)
    if error is not None:
        await callback.message.answer(error)
    elif action_id == ACTION_CHECK_UPDATE:
        await callback.message.answer(_format_check_update(result or {}))
    else:
        text, keyboard = await _node_action_screen(
            node_link, subscription, action_id, value, node_id
        )
        await redraw(callback, text, keyboard)
    await callback.answer("Готово" if error is None else None)


@router.callback_query(F.data.startswith(f"{commands.ACTION_CALLBACK_PREFIX}:"))
async def on_dynamic_action(
    callback: CallbackQuery,
    store: Store,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    pending_vpn_secrets: PendingVpnSecrets,
    book: SubscriptionBook,
    gate: Gatekeeper | None = None,
    bot: Bot | None = None,
    subscription: Subscription | None = None,
) -> None:
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    service, action_id, value, node_id = parsed

    if service == vpn_protocol.SERVICE_NAME:
        if subscription is not None:
            await vpn_handlers.handle_action(
                callback,
                node_link,
                notifier,
                config,
                subscription,
                pending_vpn_secrets,
                book,
                gate,
                bot,
                store,
            )
        else:
            await callback.answer()
        return

    if service == NODE_SERVICE and _is_self_shutdown(action_id, value, node_id):
        await _handle_self_shutdown(callback, store, node_link, action_id, value)
        return

    if service == NODE_SERVICE:
        await _handle_node_action(callback, node_link, subscription, action_id, value, node_id)
        return

    if service == apps_view.APPS_SERVICE:
        text, keyboard = await apps_view.run_app_skill(apps_link, subscription, action_id, value)
        await redraw(callback, text, keyboard, disable_web_page_preview=True)
        await callback.answer()
        return

    if service == MONITOR_SERVICE:
        text = await actions.run_action(
            store, node_link, service, action_id, value, node_id=node_id
        )
        await callback.message.answer(text)
        await callback.answer()
        return

    log.warning("Callback для неизвестной службы: %s", callback.data)
    await callback.answer("Неизвестная служба", show_alert=True)
