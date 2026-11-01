from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.types import CallbackQuery, ForceReply, Message

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.devlab_notify import (
    CB_ANSWER,
    CB_CANCEL,
    CB_PREFIX,
    CB_RELEASE,
    CB_RESUME,
    CB_STOP,
    CB_STOP_CONFIRM,
    NOTICE_KEY,
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

REPLY_KEY = "devlab_reply:{chat_id}:{message_id}"
REPLY_TTL_S = 24 * 3600
CALL_TIMEOUT_S = 15.0
POST_KINDS = {
    CB_RELEASE: protocol.MSG_RELEASE,
    CB_STOP_CONFIRM: protocol.MSG_STOP,
    CB_RESUME: protocol.MSG_RESUME,
}


@dataclass(frozen=True)
class ReplyTarget:
    job_id: str
    key: str
    expired: bool


def _is_owner(subscription: Subscription | None) -> bool:
    return subscription is not None and subscription.is_owner


async def _hub(
    node_link: ServiceLink, config: Settings, action: str, args: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    try:
        reply = await node_link.command(
            action, args, dst=hub_address(config), timeout=CALL_TIMEOUT_S
        )
    except UNREACHABLE_ERRORS as exc:
        if isinstance(exc, ProtoError) and exc.code == ERR_BAD_REQUEST:
            return None, ai_lines.DEVLAB_REFUSED_TEXT.format(reason=exc.message)
        log.warning("devlab: хаб недоступен: %s", exc)
        return None, ai_lines.DEVLAB_HUB_DOWN_TEXT
    return reply, ""


def _post_args(job_id: str, kind: str, text: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {protocol.FIELD_ID: job_id, protocol.FIELD_KIND: kind}
    if text is not None:
        args[protocol.PAYLOAD_TEXT] = text
    return args


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
    store,
    node_link: ServiceLink | None = None,
    devlab_notifier: DevlabNotifier | None = None,
    subscription: Subscription | None = None,
) -> None:
    if not _is_owner(subscription):
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
    elif code == CB_ANSWER:
        await _ask_answer(callback, node_link, config, store, job_id)
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
    reply, error = await _hub(node_link, config, protocol.ACTION_OWNER_POST, args)
    if reply is None:
        await callback.answer(error, show_alert=True)
        if error != ai_lines.DEVLAB_HUB_DOWN_TEXT:
            await _clear_markup(callback)
        return
    await callback.answer(
        ai_lines.DEVLAB_POSTED_TOAST.format(status=reply.get(protocol.FIELD_STATUS, ""))
    )
    await _clear_markup(callback)
    _kick(devlab_notifier)


async def _ask_answer(
    callback: CallbackQuery, node_link: ServiceLink, config: Settings, store, job_id: str
) -> None:
    args = {protocol.FIELD_ID: job_id}
    job, error = await _hub(node_link, config, protocol.ACTION_OWNER_JOB, args)
    if job is None:
        await callback.answer(error, show_alert=True)
        return
    status = job.get(protocol.FIELD_STATUS, "")
    if status != protocol.STATUS_WAITING_OWNER:
        await callback.answer(ai_lines.DEVLAB_ALREADY_TOAST.format(status=status), show_alert=True)
        await _clear_markup(callback)
        return
    await callback.answer()
    sent = await callback.message.answer(
        ai_lines.DEVLAB_REPLY_PROMPT.format(job_id=job_id),
        reply_markup=ForceReply(input_field_placeholder=ai_lines.DEVLAB_REPLY_PLACEHOLDER),
    )
    user_id = callback.from_user.id if callback.from_user is not None else callback.message.chat.id
    key = REPLY_KEY.format(chat_id=callback.message.chat.id, message_id=sent.message_id)
    entry = {"job_id": job_id, "user_id": user_id, "created": time.time()}
    await store.set_state(key, json.dumps(entry))


def _parse_entry(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        entry = json.loads(raw)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and entry.get("job_id") else None


class DevlabReplyFilter(Filter):
    async def __call__(
        self, message: Message, store=None, subscription: Subscription | None = None
    ) -> bool | dict:
        reply = message.reply_to_message
        text = (message.text or "").strip()
        if (
            store is None
            or reply is None
            or message.chat is None
            or message.from_user is None
            or not text
            or text.startswith("/")
            or not _is_owner(subscription)
        ):
            return False
        chat_id = message.chat.id
        prompt_key = REPLY_KEY.format(chat_id=chat_id, message_id=reply.message_id)
        entry = _parse_entry(await store.get_state(prompt_key))
        if entry is not None:
            if entry.get("user_id") != message.from_user.id:
                return False
            expired = time.time() - float(entry.get("created", 0)) > REPLY_TTL_S
            return {"devlab_target": ReplyTarget(str(entry["job_id"]), prompt_key, expired)}
        notice_key = NOTICE_KEY.format(chat_id=chat_id, message_id=reply.message_id)
        job_id = await store.get_state(notice_key)
        if job_id:
            return {"devlab_target": ReplyTarget(job_id, notice_key, False)}
        return False


@router.message(DevlabReplyFilter())
async def on_devlab_reply(
    message: Message,
    devlab_target: ReplyTarget,
    config: Settings,
    store,
    node_link: ServiceLink | None = None,
    devlab_notifier: DevlabNotifier | None = None,
    subscription: Subscription | None = None,
) -> None:
    if not _is_owner(subscription) or node_link is None:
        return
    if devlab_target.expired:
        await store.delete_state(devlab_target.key)
        await message.answer(ai_lines.DEVLAB_PROMPT_EXPIRED_TEXT)
        return
    text = (message.text or "").strip()
    reply, error = await _hub(
        node_link,
        config,
        protocol.ACTION_OWNER_POST,
        _post_args(devlab_target.job_id, protocol.MSG_ANSWER, text),
    )
    if reply is None:
        if error != ai_lines.DEVLAB_HUB_DOWN_TEXT:
            await store.delete_state(devlab_target.key)
        await message.answer(error)
        return
    await store.delete_state(devlab_target.key)
    await message.answer(ai_lines.DEVLAB_SENT_TEXT)
    _kick(devlab_notifier)
