"""Сборка Bot/Dispatcher и цепочки middleware.

Меню команд живёт в `bot/menu.py` (его правят и старт, и хендлеры), здесь —
порядок роутеров и барьеры: `SilenceGate` на входе для чужих, авторизация
команд для своих (AUTHORIZATION.md §5, §10).
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from sa_home_bot.bot.handlers import (
    ai,
    apps,
    basic,
    control,
    invites,
    node,
    node_links,
    power,
    stats,
    status,
    swarm_panel,
    tool_debug,
    torrents,
    torrents_panel,
    vpn,
    wake,
)
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.menu import (  # noqa: F401 — реэкспорт: меню переехало в
    # bot/menu.py, но привычные импорты из setup остаются рабочими.
    build_menu_commands,
    set_bot_commands,
)
from sa_home_bot.bot.middlewares import (
    AuthorizationMiddleware,
    CallbackAuthorizationMiddleware,
    SilenceGate,
)
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)


def build_bot(token: str, proxy: str = "") -> Bot:
    session = AiohttpSession(proxy=proxy) if proxy else None
    return Bot(
        token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )


def build_dispatcher(book: SubscriptionBook, gate: Gatekeeper) -> Dispatcher:
    dp = Dispatcher()
    # Гейт молчания — САМЫЙ первый и на update целиком: чужой чат не должен
    # доходить ни до одного роутера, каким бы путём он ни пришёл.
    dp.update.outer_middleware(SilenceGate(book, gate))
    dp.message.middleware(AuthorizationMiddleware(book))
    dp.callback_query.middleware(CallbackAuthorizationMiddleware(book))
    # invites рано: ловит /invite, /guests и то единственное сообщение, каким
    # чужой чат стал своим (JustAdmitted) — до всех широких фильтров.
    dp.include_router(invites.router)
    dp.include_router(basic.router)
    # tool_debug: единственный обработчик своего callback-префикса, к
    # сообщениям и командам не относится вовсе.
    dp.include_router(tool_debug.router)
    # ai: команда /ai + узкий фильтр реплаев на свои же диалоги (резолвится
    # по ai_turns, не по дереву Telegram-реплаев) — не пересекается с другими
    # роутерами, но пусть проверяется рано.
    dp.include_router(ai.router)
    # wake и swarm_panel раньше status: их callback'ы («st:wake», «st:sw_*»)
    # точечные, а on_status_action ловит весь префикс «st:».
    dp.include_router(wake.router)
    dp.include_router(swarm_panel.router)
    dp.include_router(torrents_panel.router)
    dp.include_router(status.router)
    dp.include_router(stats.router)
    dp.include_router(control.router)
    dp.include_router(power.router)
    dp.include_router(node.router)
    # node_links до apps: ловит фиксированные префиксы /node_* и /svc_*.
    dp.include_router(node_links.router)
    # torrents: документы/magnet-ссылки, не пересекается с командами.
    dp.include_router(torrents.router)
    # vpn: команда /vpn; её кнопки идут по общей "act:"-схеме и разбираются
    # в node.on_dynamic_action (уже подключён выше), не здесь.
    dp.include_router(vpn.router)
    # apps последним из "точных" роутеров: ловит динамические команды-скилы,
    # остальное игнорирует.
    dp.include_router(apps.router)
    # ai.catchall — самый широкий фильтр из всех (любой текст в личке,
    # @упоминание в группе), поэтому регистрируется самым последним: должен
    # получить только то, что не разобрал никто более специфичный выше
    # (magnet-ссылки в torrents, команды в остальных роутерах и т.д.).
    dp.include_router(ai.catchall_router)
    return dp
