from __future__ import annotations

import socket
from dataclasses import asdict
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.mcp_tools import job_view
from sa_home_bot.devlab.hub.nodes import NodeDirectory
from sa_home_bot.devlab.hub.queue import DEFAULT_PAGE, Job
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import undeclared_action
from sa_home_bot.utils.text import clip

TITLE_MAX_CHARS = 120
OWNER_INBOX_MAX_LIMIT = 200

NAME_PARAM = ActionParam(name="name", title="Имя токена")
TOKEN_ACTIONS = (
    ActionSpec(
        id=protocol.ACTION_ISSUE_TOKEN,
        title="Выпустить токен",
        params=(NAME_PARAM, ActionParam(name="role", title="Роль", choices=protocol.ROLES)),
    ),
    ActionSpec(
        id=protocol.ACTION_REVOKE_TOKEN,
        title="Отозвать токен",
        params=(
            NAME_PARAM,
            ActionParam(name="role", title="Роль", required=False, choices=protocol.ROLES),
        ),
    ),
)
OWNER_ACTIONS = (
    ActionSpec(
        id=protocol.ACTION_OWNER_INBOX,
        title="Ящик всех клиентов (владелец)",
        params=(
            ActionParam(name=protocol.FIELD_SINCE, title="Курсор", required=False),
            ActionParam(name=protocol.FIELD_LIMIT, title="Сколько сообщений", required=False),
        ),
    ),
    ActionSpec(
        id=protocol.ACTION_OWNER_JOB,
        title="Задание (владелец)",
        params=(ActionParam(name=protocol.FIELD_ID, title="Номер задания"),),
    ),
    ActionSpec(
        id=protocol.ACTION_OWNER_POST,
        title="Сообщение за клиента задания (владелец)",
        params=(
            ActionParam(name=protocol.FIELD_ID, title="Номер задания"),
            ActionParam(name=protocol.FIELD_KIND, title="Вид", choices=protocol.CLIENT_MESSAGES),
            ActionParam(name=protocol.PAYLOAD_TEXT, title="Текст", required=False),
            ActionParam(name=protocol.PAYLOAD_MODEL, title="Модель", required=False),
        ),
    ),
)


def task_title(task: str) -> str:
    for line in task.splitlines():
        title = line.lstrip("#").strip()
        if title:
            return clip(title, TITLE_MAX_CHARS)
    return ""


def owner_job_view(job: Job) -> dict[str, Any]:
    view = job_view(job, with_task=False)
    view[protocol.FIELD_TITLE] = task_title(job.task)
    return view


def _int_arg(args: dict[str, Any], name: str, default: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise HubError(f"{name}: целое число")
    try:
        return int(value)
    except ValueError as exc:
        raise HubError(f"{name}: целое число") from exc


class HubService:
    def __init__(self, store: HubStore, nodes: NodeDirectory) -> None:
        self._store = store
        self._nodes = nodes
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=protocol.SERVICE_NAME, version=__version__),
            actions=TOKEN_ACTIONS + OWNER_ACTIONS,
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": protocol.SERVICE_NAME,
            "active_tokens": await self._store.count_active(),
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._dispatch(action, args)
        except HubError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc

    async def _owner_inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        queue = self._store.queue
        since = _int_arg(args, protocol.FIELD_SINCE, 0)
        limit = _int_arg(args, protocol.FIELD_LIMIT, DEFAULT_PAGE)
        if since < 0:
            return {protocol.FIELD_MESSAGES: [], protocol.FIELD_CURSOR: await queue.max_seq()}
        if limit < 1:
            raise HubError("limit не меньше 1")
        pairs = await queue.inbox_all(since, min(limit, OWNER_INBOX_MAX_LIMIT))
        messages = [
            {**asdict(message), protocol.FIELD_JOB: owner_job_view(job)} for message, job in pairs
        ]
        return {
            protocol.FIELD_MESSAGES: messages,
            protocol.FIELD_CURSOR: pairs[-1][0].seq if pairs else since,
        }

    async def _owner_job(self, args: dict[str, Any]) -> dict[str, Any]:
        queue = self._store.queue
        job = await queue.get_job(str(args.get(protocol.FIELD_ID, "")))
        question = await queue.last_message(job.id, protocol.MSG_QUESTION)
        text = question.payload.get(protocol.PAYLOAD_TEXT, "") if question else ""
        return {**owner_job_view(job), protocol.FIELD_QUESTION: text}

    async def _owner_post(self, args: dict[str, Any]) -> dict[str, Any]:
        queue = self._store.queue
        job = await queue.get_job(str(args.get(protocol.FIELD_ID, "")))
        kind = str(args.get(protocol.FIELD_KIND, ""))
        text = str(args.get(protocol.PAYLOAD_TEXT) or "")
        if kind == protocol.MSG_ANSWER and not text.strip():
            raise HubError("ответ не должен быть пустым")
        payload: dict[str, Any] = {protocol.PAYLOAD_VIA: protocol.VIA_TELEGRAM}
        if text.strip():
            payload[protocol.PAYLOAD_TEXT] = text
        if args.get(protocol.PAYLOAD_MODEL) is not None:
            payload[protocol.PAYLOAD_MODEL] = args[protocol.PAYLOAD_MODEL]
        message = await queue.post_from_client(job.client, job.id, kind, payload)
        after = await queue.get_job(job.id)
        return {protocol.FIELD_SEQ: message.seq, protocol.FIELD_STATUS: after.status}

    async def _dispatch(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == protocol.ACTION_OWNER_INBOX:
            return await self._owner_inbox(args)
        if action == protocol.ACTION_OWNER_JOB:
            return await self._owner_job(args)
        if action == protocol.ACTION_OWNER_POST:
            return await self._owner_post(args)
        name = str(args.get("name", ""))
        if action == protocol.ACTION_ISSUE_TOKEN:
            role = str(args.get("role", ""))
            known = await self._nodes.addresses()
            if len(known) > 1 and name.strip() not in known:
                nodes = ", ".join(sorted(known))
                raise HubError(f"имя токена должно быть нодой роя: {name!r} нет среди {nodes}")
            return {"name": name, "role": role, "token": await self._store.issue_token(name, role)}
        if action == protocol.ACTION_REVOKE_TOKEN:
            await self._store.revoke_token(name, str(args.get("role", "")))
            return {"name": name, "revoked": True}
        raise undeclared_action(action)
