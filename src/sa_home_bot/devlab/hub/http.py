from __future__ import annotations

from collections.abc import Awaitable, Callable

from aiohttp import web

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.store import HubStore, Principal

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

BEARER_PREFIX = "bearer "
PUBLIC_ATTR = "devlab_public"
ROLES_ATTR = "devlab_roles"
PRINCIPAL_KEY = web.RequestKey("principal", Principal)
STORE_KEY = web.AppKey("store", HubStore)


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
    allowed = getattr(handler, ROLES_ATTR, None)
    if allowed is not None and principal.role not in allowed:
        raise web.HTTPForbidden()
    request[PRINCIPAL_KEY] = principal
    return await handler(request)


@public
async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": protocol.SERVICE_NAME})


def build_app(store: HubStore) -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app[STORE_KEY] = store
    app.router.add_get(protocol.HTTP_HEALTH_PATH, health)
    return app
