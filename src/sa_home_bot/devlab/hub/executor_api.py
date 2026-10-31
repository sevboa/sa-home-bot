from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import joblogs
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.http import NODES_KEY, PRINCIPAL_KEY, STORE_KEY, roles

LOG_CHUNK_BYTES = 64 * 1024


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
    kinds = body.get(protocol.FIELD_KINDS, [protocol.JOB_KIND_AGENT])
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        raise HubError("claim: kinds — список строк")
    job = await request.app[STORE_KEY].queue.claim(request[PRINCIPAL_KEY].name, models, kinds)
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
async def upload_log(request: web.Request) -> web.Response:
    store = request.app[STORE_KEY]
    job_id = request.headers.get(protocol.HEADER_JOB, "")
    name = joblogs.check_name(request.query.get(protocol.FIELD_NAME, ""))
    job = await store.queue.get_job(job_id)
    if job.kind != protocol.JOB_KIND_TEST or job.executor != request[PRINCIPAL_KEY].name:
        raise HubError(f"задание {job_id!r}: логи принимаются только от своего исполнителя test")
    if job.status == protocol.STATUS_QUEUED:
        raise HubError(f"задание {job_id}: ещё не взято")
    if request.content_length is not None and request.content_length > protocol.LOG_MAX_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=protocol.LOG_MAX_BYTES, actual_size=request.content_length
        )
    try:
        size = await joblogs.save(
            store.logs_root, job_id, name, request.content.iter_chunked(LOG_CHUNK_BYTES)
        )
    except joblogs.LogTooLarge as exc:
        raise web.HTTPRequestEntityTooLarge(
            max_size=protocol.LOG_MAX_BYTES, actual_size=protocol.LOG_MAX_BYTES + 1, text=str(exc)
        ) from exc
    return web.json_response({protocol.FIELD_NAME: name, "bytes": size})


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


@roles(protocol.ROLE_GATEWAY)
async def introspect(request: web.Request) -> web.Response:
    body = await json_body(request)
    token = body.get(protocol.FIELD_TOKEN)
    if not isinstance(token, str) or not token:
        raise HubError("introspect: token — непустая строка")
    address = body.get(protocol.FIELD_ADDRESS)
    if not isinstance(address, str) or not address:
        raise HubError("introspect: address — непустая строка")
    principal = await request.app[STORE_KEY].authenticate(token)
    if principal is None or principal.role != protocol.ROLE_EXECUTOR:
        raise web.HTTPNotFound()
    node_ips = (await request.app[NODES_KEY].addresses()).get(principal.name, frozenset())
    if address not in node_ips:
        raise web.HTTPNotFound()
    return web.json_response(
        {protocol.FIELD_NAME: principal.name, protocol.FIELD_ROLE: principal.role}
    )


def add_routes(app: web.Application) -> None:
    app.router.add_post(protocol.HTTP_CLAIM_PATH, claim)
    app.router.add_post(protocol.HTTP_POST_PATH, post)
    app.router.add_put(protocol.HTTP_LOG_PATH, upload_log)
    app.router.add_post(protocol.HTTP_POLL_PATH, poll)
    app.router.add_post(protocol.HTTP_HEARTBEAT_PATH, heartbeat)
    app.router.add_post(protocol.HTTP_INTROSPECT_PATH, introspect)
