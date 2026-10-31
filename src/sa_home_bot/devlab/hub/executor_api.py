from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.http import PRINCIPAL_KEY, STORE_KEY, roles


async def json_body(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HubError("тело запроса: ожидается JSON") from exc
    if not isinstance(body, dict):
        raise HubError("тело запроса: ожидается JSON-объект")
    return body


@roles(protocol.ROLE_EXECUTOR)
async def claim(request: web.Request) -> web.Response:
    body = await json_body(request)
    models = body.get(protocol.FIELD_MODELS)
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise HubError("claim: models — список строк")
    job = await request.app[STORE_KEY].queue.claim(request[PRINCIPAL_KEY].name, models)
    return web.json_response({protocol.FIELD_JOB: asdict(job) if job else None})


@roles(protocol.ROLE_EXECUTOR)
async def post(request: web.Request) -> web.Response:
    body = await json_body(request)
    job_id = body.get(protocol.FIELD_JOB_ID)
    kind = body.get(protocol.FIELD_KIND)
    payload = body.get(protocol.FIELD_PAYLOAD) or {}
    if not isinstance(job_id, str) or not isinstance(kind, str) or not isinstance(payload, dict):
        raise HubError("post: нужны job_id и kind строками, payload объектом")
    message = await request.app[STORE_KEY].queue.post_from_executor(
        job_id, kind, payload, executor=request[PRINCIPAL_KEY].name
    )
    return web.json_response({protocol.FIELD_SEQ: message.seq})


@roles(protocol.ROLE_EXECUTOR)
async def poll(request: web.Request) -> web.Response:
    body = await json_body(request)
    since = body.get(protocol.FIELD_SINCE, 0)
    if isinstance(since, bool) or not isinstance(since, int) or since < 0:
        raise HubError("poll: since — неотрицательное целое")
    messages = await request.app[STORE_KEY].queue.poll(
        since=since, executor=request[PRINCIPAL_KEY].name
    )
    cursor = messages[-1].seq if messages else since
    return web.json_response(
        {
            protocol.FIELD_MESSAGES: [asdict(message) for message in messages],
            protocol.FIELD_CURSOR: cursor,
        }
    )


@roles(protocol.ROLE_EXECUTOR, protocol.ROLE_GATEWAY)
async def heartbeat(request: web.Request) -> web.Response:
    await request.app[STORE_KEY].heartbeat(request[PRINCIPAL_KEY], await json_body(request))
    return web.json_response({protocol.FIELD_OK: True})


def add_routes(app: web.Application) -> None:
    app.router.add_post(protocol.HTTP_CLAIM_PATH, claim)
    app.router.add_post(protocol.HTTP_POST_PATH, post)
    app.router.add_post(protocol.HTTP_POLL_PATH, poll)
    app.router.add_post(protocol.HTTP_HEARTBEAT_PATH, heartbeat)
