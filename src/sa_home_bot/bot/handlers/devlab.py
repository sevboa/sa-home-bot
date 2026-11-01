from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.devlab_notify import (
    CB_CANCEL,
    CB_PREFIX,
    CB_RELEASE,
    CB_RESUME,
    CB_STOP,
    CB_STOP_CONFIRM,
    DevlabNotifier,
    hub_address,
    question_keyboard,
    stop_confirm_keyboard,
)
from sa_home_bot.bot.middlewares import DENIED_ALERT_TEXT
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.callback_data import unpack
from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="devlab")

CALL_TIMEOUT_S = 15.0
POST_KINDS = {
    CB_RELEASE: protocol.MSG_RELEASE,
    CB_STOP_CONFIRM: protocol.MSG_STOP,
    CB_RESUME: protocol.MSG_RESUME,
}


@dataclass(frozen=True)
class HubResult:
    reply: dict[str, Any] | None = None
    error: str = ""
    down: bool = False


def _is_owner(subscription: Subscription | None, user) -> bool:
    return (
        subscription is not None
        and subscription.is_owner
        and user is not None
        and user.id == subscription.chat_id
    )


async def _hub(
    node_link: ServiceLink, config: Settings, action: str, args: dict[str, Any]
) -> HubResult:
    try:
        reply = await node_link.command(
            action, args, dst=hub_address(config), timeout=CALL_TIMEOUT_S
        )
    except ProtoError as exc:
        if exc.code == ERR_BAD_REQUEST:
            return HubResult(error=ai_lines.DEVLAB_REFUSED_TEXT.format(reason=exc.message))
        log.warning("devlab: хаб недоступен: %s", exc)
        return HubResult(error=ai_lines.DEVLAB_HUB_DOWN_TEXT, down=True)
    except UNREACHABLE_ERRORS as exc:
        log.warning("devlab: хаб недоступен: %s", exc)
        return HubResult(error=ai_lines.DEVLAB_HUB_DOWN_TEXT, down=True)
    return HubResult(reply=reply)


def _post_args(job_id: str, kind: str) -> dict[str, Any]:
    return {protocol.FIELD_ID: job_id, protocol.FIELD_KIND: kind}


async def _clear_markup(callback: CallbackQuery, markup=None) -> None:
    if callback.message is None:
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as exc:
        log.info("devlab: кнопки не поправить: %s", exc)


def _kick(devlab_notifier: DevlabNotifier | None) -> None:
    if devlab_notifier is not None:
        devlab_notifier.kick()


@router.callback_query(F.data.startswith(f"{CB_PREFIX}:"))
async def on_devlab_callback(
    callback: CallbackQuery,
    config: Settings,
    node_link: ServiceLink | None = None,
    devlab_notifier: DevlabNotifier | None = None,
    subscription: Subscription | None = None,
) -> None:
    if not _is_owner(subscription, callback.from_user):
        await callback.answer(DENIED_ALERT_TEXT, show_alert=True)
        return
    pieces = unpack(callback.data, CB_PREFIX, 2)
    if pieces is None or node_link is None or callback.message is None:
        await callback.answer()
        return
    code, job_id = pieces
    if code == CB_STOP:
        await _clear_markup(callback, stop_confirm_keyboard(job_id))
        await callback.answer()
    elif code == CB_CANCEL:
        await _clear_markup(callback, question_keyboard(job_id))
        await callback.answer()
    elif code in POST_KINDS:
        await _post_button(callback, node_link, config, job_id, POST_KINDS[code], devlab_notifier)
    else:
        await callback.answer()


async def _post_button(
    callback: CallbackQuery,
    node_link: ServiceLink,
    config: Settings,
    job_id: str,
    kind: str,
    devlab_notifier: DevlabNotifier | None,
) -> None:
    args = _post_args(job_id, kind)
    result = await _hub(node_link, config, protocol.ACTION_OWNER_POST, args)
    if result.reply is None:
        await callback.answer(result.error, show_alert=True)
        if not result.down:
            await _clear_markup(callback)
        return
    await callback.answer(
        ai_lines.DEVLAB_POSTED_TOAST.format(status=result.reply.get(protocol.FIELD_STATUS, ""))
    )
    await _clear_markup(callback)
    _kick(devlab_notifier)
