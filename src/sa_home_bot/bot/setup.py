from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from sa_home_bot.bot.handlers import (
    ai,
    apps,
    away,
    basic,
    control,
    draw,
    interactives,
    invites,
    items,
    node,
    node_links,
    pending_actions,
    status,
    swarm_panel,
    tool_debug,
    torrents,
    torrents_panel,
    vpn,
    wake,
)
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.menu import set_bot_commands  # noqa: F401
from sa_home_bot.bot.middlewares import (
    AuthorizationMiddleware,
    CallbackAuthorizationMiddleware,
    ProfileMiddleware,
    SilenceGate,
)
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)


def build_bot(token: str, proxy: str = "") -> Bot:
    session = AiohttpSession(proxy=proxy) if proxy else None
    return Bot(
        token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )


def build_dispatcher(
    book: SubscriptionBook, gate: Gatekeeper, store: Any | None = None
) -> Dispatcher:
    dp = Dispatcher()
    dp.update.outer_middleware(SilenceGate(book, gate))
    if store is not None:
        profiles = ProfileMiddleware(store)
        dp.message.outer_middleware(profiles)
        dp.callback_query.outer_middleware(profiles)
    dp.message.middleware(AuthorizationMiddleware(book))
    dp.callback_query.middleware(CallbackAuthorizationMiddleware(book))
    dp.include_router(invites.router)
    dp.include_router(basic.router)
    dp.include_router(away.router)
    dp.include_router(tool_debug.router)
    dp.include_router(pending_actions.router)
    dp.include_router(interactives.router)
    dp.include_router(items.router)
    dp.include_router(draw.router)
    dp.include_router(ai.router)
    dp.include_router(wake.router)
    dp.include_router(swarm_panel.router)
    dp.include_router(torrents_panel.router)
    dp.include_router(status.router)
    dp.include_router(control.router)
    dp.include_router(node.router)
    dp.include_router(node_links.router)
    dp.include_router(torrents.router)
    dp.include_router(vpn.router)
    dp.include_router(apps.router)
    dp.include_router(ai.catchall_router)
    return dp
