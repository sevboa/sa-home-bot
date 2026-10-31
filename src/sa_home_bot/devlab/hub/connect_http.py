from __future__ import annotations

import hmac

from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.http import NODES_KEY, STORE_KEY, SWARM_TOKEN_KEY, public
from sa_home_bot.devlab.hub.nodes import node_at


def _swarm_token_ok(request: web.Request) -> bool:
    expected = request.app[SWARM_TOKEN_KEY]
    given = request.headers.get(protocol.HEADER_SWARM_TOKEN, "")
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


@public
async def connect(request: web.Request) -> web.Response:
    if not _swarm_token_ok(request):
        raise web.HTTPUnauthorized()
    name = node_at(await request.app[NODES_KEY].addresses(), request.remote)
    if name is None:
        raise web.HTTPForbidden()
    store = request.app[STORE_KEY]
    current = await store.active_role(name)
    if current is not None and current != protocol.ROLE_CLIENT:
        raise HubError(f"у ноды {name!r} уже есть токен роли {current}: connect его не трогает")
    token = await store.issue_token(name, protocol.ROLE_CLIENT, replace=True)
    return web.json_response(
        {
            protocol.FIELD_NAME: name,
            protocol.FIELD_ROLE: protocol.ROLE_CLIENT,
            protocol.FIELD_TOKEN: token,
        }
    )


def add_routes(app: web.Application) -> None:
    app.router.add_post(protocol.HTTP_CONNECT_PATH, connect)
