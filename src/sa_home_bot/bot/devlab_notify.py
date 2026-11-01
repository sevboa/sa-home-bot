from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from collections.abc import Callable
from typing import Any

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_errors import UNREACHABLE_ERRORS
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import OUTBOX_KIND_TASK, Store
from sa_home_bot.devlab import protocol
from sa_home_bot.proto.addresses import Address
from sa_home_bot.subscriptions.models import WILDCARD
from sa_home_bot.utils.text import clip

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 15.0
CURSOR_KEY = "devlab_inbox_cursor"
NOTICE_KEY = "devlab_notice:{chat_id}:{message_id}"
DEDUP_KEY = "devlab:{seq}"
NOTIFIED_KINDS = (protocol.MSG_QUESTION, protocol.MSG_DONE, protocol.MSG_FAILED)
LEADS = {
    protocol.MSG_QUESTION: ai_lines.DEVLAB_QUESTION_TEXT,
    protocol.MSG_DONE: ai_lines.DEVLAB_DONE_TEXT,
    protocol.MSG_FAILED: ai_lines.DEVLAB_FAILED_TEXT,
}
BODY_FIELDS = {
    protocol.MSG_QUESTION: protocol.PAYLOAD_TEXT,
    protocol.MSG_DONE: protocol.PAYLOAD_SUMMARY,
    protocol.MSG_FAILED: protocol.PAYLOAD_TEXT,
}


def wants_notice(message: dict[str, Any]) -> bool:
    job = message.get(protocol.FIELD_JOB) or {}
    if job.get("kind") != protocol.JOB_KIND_AGENT:
        return False
    kind = message.get("kind")
    if kind not in NOTIFIED_KINDS:
        return False
    return kind != protocol.MSG_QUESTION or job.get("status") == protocol.STATUS_WAITING_OWNER


def facts_block(message: dict[str, Any], max_chars: int) -> str:
    job = message.get(protocol.FIELD_JOB) or {}
    kind = message["kind"]
    job_id = job.get("id", "")
    lines = [
        f"Проект: {html.escape(str(job.get('project', '')))}, задание {html.escape(str(job_id))}",
    ]
    title = job.get(protocol.FIELD_TITLE)
    if title:
        lines.append(f"Заголовок: {html.escape(str(title))}")
    lines.append(f"Модель: {html.escape(str(job.get('model', '')))}")
    lines.append(f"Токены: {job.get('tokens', 0)}, ${float(job.get('cost_usd') or 0):.4f}")
    lines.append(f"Ветка: {html.escape(protocol.AGENT_BRANCH_PREFIX + str(job_id))}")
    payload = message.get(protocol.FIELD_PAYLOAD) or {}
    body = str(payload.get(BODY_FIELDS[kind]) or "").strip()
    if body:
        lines.append("")
        lines.append(html.escape(clip(body, max_chars)))
    return "\n".join(lines)


def compose_text(message: dict[str, Any], max_chars: int) -> str:
    return f"{LEADS[message['kind']]}\n\n{facts_block(message, max_chars)}"


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
        dst = Address(node=self._config.devlab_hub.hub_node, service=protocol.SERVICE_NAME)
        return await node_link.command(
            protocol.ACTION_OWNER_INBOX,
            {protocol.FIELD_SINCE: since},
            dst=dst,
            timeout=CALL_TIMEOUT_S,
        )

    async def _deliver(self, message: dict[str, Any]) -> None:
        text = compose_text(message, self._config.devlab_notify.facts_max_chars)
        job_id = str(message[protocol.FIELD_JOB]["id"])
        seq = message[protocol.FIELD_SEQ]
        for sub in self._book.all():
            if WILDCARD not in sub.allowed_commands:
                continue
            message_id = await self._notifier.send_direct(
                sub.chat_id,
                text,
                outbox_kind=OUTBOX_KIND_TASK,
                dedup_key=DEDUP_KEY.format(seq=seq),
            )
            if message_id is not None and message["kind"] == protocol.MSG_QUESTION:
                key = NOTICE_KEY.format(chat_id=sub.chat_id, message_id=message_id)
                await self._store.set_state(key, job_id)
