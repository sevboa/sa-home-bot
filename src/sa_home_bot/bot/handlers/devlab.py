from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from sa_home_bot.bot import ai_lines, commands
from sa_home_bot.bot.devlab_jobs_view import (
    CB_JOBS,
    CODE_CARD,
    CODE_PAGE,
    CODE_TASK,
    PAGE_SIZE,
    render_card,
    render_page,
)
from sa_home_bot.bot.devlab_notify import (
    CB_CANCEL,
    CB_CANCEL_STALLED,
    CB_PREFIX,
    CB_RELEASE,
    CB_RESUME,
    CB_STOP,
    CB_STOP_CONFIRM,
    CB_STOP_STALLED,
    DevlabNotifier,
    hub_address,
    question_keyboard,
    stalled_keyboard,
    stop_confirm_keyboard,
)
from sa_home_bot.bot.middlewares import DENIED_ALERT_TEXT, DENIED_TEXT
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.callback_data import int_or, unpack
from sa_home_bot.bot.ui.redraw import redraw_rich
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


def _counts_by_status(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        status = job.get(protocol.FIELD_STATUS, "")
        counts[status] = counts.get(status, 0) + 1
    return counts


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
    elif code == CB_STOP_STALLED:
        await _clear_markup(callback, stop_confirm_keyboard(job_id, CB_CANCEL_STALLED))
        await callback.answer()
    elif code == CB_CANCEL_STALLED:
        await _clear_markup(callback, stalled_keyboard(job_id))
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


@router.message(Command(commands.JOBS.name))
async def cmd_jobs(
    message: Message,
    config: Settings,
    node_link: ServiceLink | None = None,
    subscription: Subscription | None = None,
) -> None:
    if not _is_owner(subscription, message.from_user):
        await message.answer(DENIED_TEXT)
        return
    if node_link is None:
        return
    result = await _hub(
        node_link,
        config,
        protocol.ACTION_OWNER_JOBS,
        {protocol.FIELD_OFFSET: 0, protocol.FIELD_LIMIT: PAGE_SIZE},
    )
    if result.reply is None:
        await message.answer(result.error)
        return
    jobs = result.reply.get(protocol.FIELD_JOBS, [])
    total = result.reply.get(protocol.FIELD_TOTAL, 0)
    counts = _counts_by_status(jobs)
    rich_msg, keyboard, _ = render_page(jobs, 0, total, now=datetime.now(UTC), counts=counts)
    await message.answer_rich(rich_message=rich_msg, reply_markup=keyboard)


@router.callback_query(F.data.startswith(f"{CB_JOBS}:"))
async def on_jobs_callback(
    callback: CallbackQuery,
    config: Settings,
    node_link: ServiceLink | None = None,
    subscription: Subscription | None = None,
) -> None:
    if not _is_owner(subscription, callback.from_user):
        await callback.answer(DENIED_ALERT_TEXT, show_alert=True)
        return
    if node_link is None or callback.message is None:
        await callback.answer()
        return
    data = callback.data or ""
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != CB_JOBS:
        await callback.answer()
        return
    code = parts[1]
    if code == CODE_PAGE:
        offset = int_or(parts[2] if len(parts) > 2 else None, 0)
        while True:
            result = await _hub(
                node_link,
                config,
                protocol.ACTION_OWNER_JOBS,
                {protocol.FIELD_OFFSET: offset, protocol.FIELD_LIMIT: PAGE_SIZE},
            )
            if result.reply is None:
                await callback.answer(result.error, show_alert=True)
                return
            jobs = result.reply.get(protocol.FIELD_JOBS, [])
            total = result.reply.get(protocol.FIELD_TOTAL, 0)
            if not jobs and total > 0:
                from sa_home_bot.bot.pagination import clamp_offset

                clamped = clamp_offset(offset, PAGE_SIZE, total)
                if clamped == offset:
                    break
                offset = clamped
                continue
            counts = _counts_by_status(jobs)
            rich_msg, keyboard, _ = render_page(
                jobs, offset, total, now=datetime.now(UTC), counts=counts
            )
            await redraw_rich(callback, rich_msg, keyboard)
            break
    elif code == CODE_CARD:
        if len(parts) < 4:
            await callback.answer()
            return
        job_id = parts[2]
        offset = int_or(parts[3], 0)
        result = await _hub(
            node_link, config, protocol.ACTION_OWNER_JOB, {protocol.FIELD_ID: job_id}
        )
        if result.reply is None:
            await callback.answer(result.error, show_alert=True)
            return
        job = result.reply.get(protocol.FIELD_JOB)
        if job is None:
            await callback.answer("Задание не найдено", show_alert=True)
            return
        rich_msg, keyboard = render_card(job, offset, now=datetime.now(UTC))
        await redraw_rich(callback, rich_msg, keyboard)
    elif code == CODE_TASK:
        if len(parts) < 3:
            await callback.answer()
            return
        job_id = parts[2]
        result = await _hub(
            node_link, config, protocol.ACTION_OWNER_TASK, {protocol.FIELD_ID: job_id}
        )
        if result.reply is None:
            await callback.answer(result.error, show_alert=True)
            return
        task_text = result.reply.get(protocol.FIELD_TASK, "")
        if not task_text:
            await callback.answer("Текста задачи нет", show_alert=True)
            return
        title = result.reply.get(protocol.FIELD_TITLE, "")
        caption = title[:1000] if title else ""
        document = BufferedInputFile(task_text.encode("utf-8"), filename=f"task-{job_id}.md")
        await callback.message.answer_document(document, caption=caption)
        await callback.answer()
    await callback.answer()
