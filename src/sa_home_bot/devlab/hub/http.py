from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.nodes import NodeDirectory
from sa_home_bot.devlab.hub.store import HubStore, Principal

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

BEARER_PREFIX = "bearer "
PUBLIC_ATTR = "devlab_public"
ROLES_ATTR = "devlab_roles"
PRINCIPAL_KEY = web.RequestKey("principal", Principal)
STORE_KEY = web.AppKey("store", HubStore)
NODES_KEY = web.AppKey("nodes", NodeDirectory)
SWARM_TOKEN_KEY = web.AppKey("swarm_token", str)


def public(handler: Handler) -> Handler:
    setattr(handler, PUBLIC_ATTR, True)
    return handler


def roles(*allowed: str) -> Callable[[Handler], Handler]:
    def mark(handler: Handler) -> Handler:
        setattr(handler, ROLES_ATTR, frozenset(allowed))
        return handler

    return mark


def _bearer(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith(BEARER_PREFIX):
        return header[len(BEARER_PREFIX) :].strip()
    return ""


@web.middleware
async def auth_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    if getattr(handler, PUBLIC_ATTR, False):
        return await handler(request)
    token = _bearer(request)
    principal = await request.app[STORE_KEY].authenticate(token) if token else None
    if principal is None:
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
    node_ips = (await request.app[NODES_KEY].addresses()).get(principal.name, frozenset())
    if request.remote not in node_ips:
        raise web.HTTPForbidden()
    allowed = getattr(handler, ROLES_ATTR, None)
    if allowed is not None and principal.role not in allowed:
        raise web.HTTPForbidden()
    request[PRINCIPAL_KEY] = principal
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except HubError as exc:
        return web.json_response({protocol.FIELD_ERROR: str(exc)}, status=400)


@public
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": protocol.SERVICE_NAME})


def build_app(store: HubStore, nodes: NodeDirectory, swarm_token: str = "") -> web.Application:
    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app[STORE_KEY] = store
    app[NODES_KEY] = nodes
    app[SWARM_TOKEN_KEY] = swarm_token
    app.router.add_get(protocol.HTTP_HEALTH_PATH, health)
    return app
