from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections.abc import Callable
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.notifier import Notifier, admin_subs
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.config import Settings
from sa_home_bot.db.store import OUTBOX_KIND_TASK, Store
from sa_home_bot.devlab import protocol
from sa_home_bot.proto.addresses import Address
from sa_home_bot.utils.text import clip

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 15.0
CURSOR_KEY = "devlab_inbox_cursor"
LEGACY_STATE_PREFIXES = ("devlab_notice:", "devlab_reply:")
CB_PREFIX = "dl"
CB_RELEASE = "rel"
CB_STOP = "stop"
CB_STOP_STALLED = "stopw"
CB_CANCEL_STALLED = "cnlw"
CB_STOP_CONFIRM = "stopc"
CB_CANCEL = "cnl"
CB_RESUME = "res"
CB_EPIC_RESUME = "epr"
CB_EPIC_STOP = "eps"
CB_EPIC_STOP_CONFIRM = "epsc"
CB_EPIC_CANCEL = "epc"
DEDUP_KEY = "devlab:{seq}"
NOTIFIED_KINDS = (
    protocol.MSG_QUESTION,
    protocol.MSG_DONE,
    protocol.MSG_FAILED,
    protocol.MSG_STALLED,
    protocol.MSG_EPIC_WAITING,
)
LEADS = {
    protocol.MSG_QUESTION: ai_lines.DEVLAB_QUESTION_TEXT,
    protocol.MSG_DONE: ai_lines.DEVLAB_DONE_TEXT,
    protocol.MSG_FAILED: ai_lines.DEVLAB_FAILED_TEXT,
    protocol.MSG_STALLED: ai_lines.DEVLAB_STALLED_TEXT,
    protocol.MSG_EPIC_WAITING: ai_lines.DEVLAB_EPIC_WAITING_TEXT,
}
BODY_FIELDS = {
    protocol.MSG_QUESTION: protocol.PAYLOAD_TEXT,
    protocol.MSG_DONE: protocol.PAYLOAD_SUMMARY,
    protocol.MSG_FAILED: protocol.PAYLOAD_TEXT,
    protocol.MSG_EPIC_WAITING: protocol.PAYLOAD_TEXT,
}


def hub_address(config: Settings) -> Address:
    return Address(node=config.devlab_hub.hub_node, service=protocol.SERVICE_NAME)


def question_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return markup(
        [
            button(ai_lines.DEVLAB_BTN_RELEASE, pack(CB_PREFIX, CB_RELEASE, job_id)),
            button(ai_lines.DEVLAB_BTN_STOP, pack(CB_PREFIX, CB_STOP, job_id)),
        ]
    )


def epic_keyboard(epic_id: str) -> InlineKeyboardMarkup:
    return markup(
        [
            button(ai_lines.DEVLAB_BTN_EPIC_RESUME, pack(CB_PREFIX, CB_EPIC_RESUME, epic_id)),
            button(ai_lines.DEVLAB_BTN_EPIC_STOP, pack(CB_PREFIX, CB_EPIC_STOP, epic_id)),
        ]
    )


def stop_confirm_keyboard(
    job_id: str, cancel: str = CB_CANCEL, confirm: str = CB_STOP_CONFIRM
) -> InlineKeyboardMarkup:
    return markup(
        [
            button(ai_lines.DEVLAB_BTN_STOP_CONFIRM, pack(CB_PREFIX, confirm, job_id)),
            button(ai_lines.DEVLAB_BTN_CANCEL, pack(CB_PREFIX, cancel, job_id)),
        ]
    )


def stalled_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return markup([button(ai_lines.DEVLAB_BTN_STOP, pack(CB_PREFIX, CB_STOP_STALLED, job_id))])


def failed_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return markup([button(ai_lines.DEVLAB_BTN_RESUME, pack(CB_PREFIX, CB_RESUME, job_id))])


def notice_keyboard(kind: str, job_id: str, epic_id: str = "") -> InlineKeyboardMarkup | None:
    if kind == protocol.MSG_EPIC_WAITING:
        return epic_keyboard(epic_id) if epic_id else None
    if kind == protocol.MSG_QUESTION:
        return question_keyboard(job_id)
    if kind == protocol.MSG_FAILED:
        return failed_keyboard(job_id)
    if kind == protocol.MSG_STALLED:
        return stalled_keyboard(job_id)
    return None


def wants_notice(message: dict[str, Any]) -> bool:
    job = message.get(protocol.FIELD_JOB) or {}
    if job.get(protocol.FIELD_KIND) != protocol.JOB_KIND_AGENT:
        return False
    kind = message.get(protocol.FIELD_KIND)
    if kind not in NOTIFIED_KINDS:
        return False
    status = job.get(protocol.FIELD_STATUS)
    if kind == protocol.MSG_QUESTION:
        return status == protocol.STATUS_WAITING_OWNER
    if kind == protocol.MSG_STALLED:
        return status == protocol.STATUS_RUNNING
    if kind == protocol.MSG_EPIC_WAITING:
        return bool((message.get(protocol.FIELD_PAYLOAD) or {}).get(protocol.PARAM_EPIC))
    return True


def facts_block(message: dict[str, Any], max_chars: int) -> str:
    job = message.get(protocol.FIELD_JOB) or {}
    kind = message[protocol.FIELD_KIND]
    job_id = job.get(protocol.FIELD_ID, "")
    lines = [
        f"Проект: {html.escape(str(job.get('project', '')))}, задание {html.escape(str(job_id))}",
    ]
    title = job.get(protocol.FIELD_TITLE)
    if title:
        lines.append(f"Заголовок: {html.escape(str(title))}")
    lines.append(f"Модель: {html.escape(str(job.get(protocol.PAYLOAD_MODEL, '')))}")
    tokens = job.get(protocol.PAYLOAD_TOKENS, 0)
    cost = float(job.get(protocol.PAYLOAD_COST_USD) or 0)
    lines.append(f"Токены: {tokens}, ${cost:.4f}")
    lines.append(f"Ветка: {html.escape(protocol.AGENT_BRANCH_PREFIX + str(job_id))}")
    payload = message.get(protocol.FIELD_PAYLOAD) or {}
    if kind == protocol.MSG_EPIC_WAITING:
        lines.append(f"Эпик: {html.escape(str(payload.get(protocol.PARAM_EPIC, '')))}")
    if kind == protocol.MSG_STALLED:
        lines.append(f"Без вестей: {int(payload.get(protocol.PAYLOAD_SILENT_MIN) or 0)} мин")
        alive = "на связи" if payload.get(protocol.PAYLOAD_EXECUTOR_ALIVE) else "не отвечает"
        lines.append(f"Исполнитель: {alive}")
    body = str(payload.get(BODY_FIELDS.get(kind, "")) or "").strip()
    if body:
        lines.append("")
        lines.append(html.escape(clip(body, max_chars)))
    return "\n".join(lines)


def compose_text(message: dict[str, Any], max_chars: int) -> str:
    text = f"{LEADS[message['kind']]}\n\n{facts_block(message, max_chars)}"
    if message[protocol.FIELD_KIND] == protocol.MSG_QUESTION:
        text += f"\n\n{ai_lines.DEVLAB_ANSWER_HINT}"
    return text


class DevlabNotifier:
    def __init__(
        self,
        *,
        get_node_link: Callable[[], ServiceLink | None],
        store: Store,
        config: Settings,
        book: Any,
        notifier: Notifier,
    ) -> None:
        self._get_node_link = get_node_link
        self._store = store
        self._config = config
        self._book = book
        self._notifier = notifier
        self._kick = asyncio.Event()

    def kick(self) -> None:
        self._kick.set()

    async def run(self) -> None:
        poll_s = self._config.devlab_notify.poll_s
        try:
            await self.purge_legacy_keys()
        except Exception:
            log.warning("devlab_notify: старые ключи ответа не убраны", exc_info=True)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("devlab_notify: проход не удался")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._kick.wait(), poll_s)
            self._kick.clear()

    async def purge_legacy_keys(self) -> None:
        for prefix in LEGACY_STATE_PREFIXES:
            for key in await self._store.state_keys(prefix):
                await self._store.delete_state(key)

    async def tick(self) -> int:
        node_link = self._get_node_link()
        if node_link is None:
            return 0
        raw = await self._store.get_state(CURSOR_KEY)
        since = int(raw) if raw is not None else -1
        try:
            reply = await self._call(node_link, since)
        except UNREACHABLE_ERRORS as exc:
            log.warning("devlab_notify: хаб недоступен: %s", exc)
            return 0
        if raw is None:
            await self._store.set_state(CURSOR_KEY, str(reply[protocol.FIELD_CURSOR]))
            return 0
        sent = 0
        for message in reply[protocol.FIELD_MESSAGES]:
            if wants_notice(message):
                await self._deliver(message)
                sent += 1
            await self._store.set_state(CURSOR_KEY, str(message[protocol.FIELD_SEQ]))
        return sent

    async def _call(self, node_link: ServiceLink, since: int) -> dict[str, Any]:
        return await node_link.command(
            protocol.ACTION_OWNER_INBOX,
            {protocol.FIELD_SINCE: since},
            dst=hub_address(self._config),
            timeout=CALL_TIMEOUT_S,
        )

    async def _deliver(self, message: dict[str, Any]) -> None:
        subs = admin_subs(self._book)
        text = compose_text(message, self._config.devlab_notify.facts_max_chars)
        job_id = str(message[protocol.FIELD_JOB][protocol.FIELD_ID])
        seq = message[protocol.FIELD_SEQ]
        payload = message.get(protocol.FIELD_PAYLOAD) or {}
        keyboard = notice_keyboard(
            message[protocol.FIELD_KIND], job_id, str(payload.get(protocol.PARAM_EPIC) or "")
        )
        for sub in subs:
            await self._notifier.send_direct(
                sub.chat_id,
                text,
                reply_markup=keyboard,
                outbox_kind=OUTBOX_KIND_TASK,
                dedup_key=DEDUP_KEY.format(seq=seq),
            )
