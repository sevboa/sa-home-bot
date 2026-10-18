from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks, vpn_nodes, vpn_rights
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.ui.redraw import redraw
from sa_home_bot.bot.vpn_format import GB
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

SERVICE = vpn_callbacks.SERVICE

VPN_UNAVAILABLE = "⚠️ Служба VPN недоступна — попробуйте позже."
NO_VPN_ACCESS = "📶 <b>VPN</b>\n\nДоступ пока не выдан — попросите владельца открыть вам локацию."
SERVICE_UNAVAILABLE_NOTICE = "⚠️ Служба VPN недоступна."
SECRET_PRIVATE_ONLY = "Секрет доступа выдаётся только в личке."
DEVICE_NOT_FOUND = "Устройство не найдено — обновите список."
NOT_ALLOWED = "⛔️ Недоступно"
GUEST_GONE = "Гость больше не в списке."


@dataclass
class Ctx:
    callback: CallbackQuery
    node_link: ServiceLink
    notifier: Notifier
    config: Settings
    subscription: Subscription
    pending: PendingVpnSecrets
    book: SubscriptionBook | None = None
    gate: Gatekeeper | None = None
    bot: Bot | None = None
    store: object | None = None

    @property
    def chat_id(self) -> int:
        return self.callback.message.chat.id

    @property
    def thread_id(self) -> int | None:
        return self.callback.message.message_thread_id

    @property
    def ttl_s(self) -> float:
        return self.config.vpn.config_message_ttl_s

    @property
    def is_private(self) -> bool:
        return is_private(self.chat_id)

    async def require_private(self, text: str = SECRET_PRIVATE_ONLY) -> bool:
        if self.is_private:
            return True
        await self.callback.answer(text, show_alert=True)
        return False

    async def edit(self, text: str, keyboard: InlineKeyboardMarkup | None, **options) -> None:
        await redraw(self.callback, text, keyboard, **options)

    async def alert(self, text: str) -> None:
        await self.callback.answer(text, show_alert=True)

    async def call_or_alert(self, action: str, payload: dict, dst: Address) -> dict | None:
        try:
            return await self.node_link.command(action, payload, dst=dst)
        except ProtoError as exc:
            await self.alert(f"⚠️ {exc.message}")
        except ServiceUnavailableError:
            await self.alert(SERVICE_UNAVAILABLE_NOTICE)
        return None

    async def call_or_notice(self, action: str, payload: dict, dst: Address) -> dict | None:
        try:
            return await self.node_link.command(action, payload, dst=dst)
        except ProtoError as exc:
            await self.callback.message.answer(f"⚠️ {exc.message}")
        except ServiceUnavailableError:
            await self.callback.message.answer(SERVICE_UNAVAILABLE_NOTICE)
        return None

    async def need_dst(self, node_id: str | None) -> Address | None:
        dst = await vpn_nodes.resolve_vpn_dst(self.node_link, server=node_id)
        if dst is None:
            await self.alert(SERVICE_UNAVAILABLE_NOTICE)
        return dst


def is_private(chat_id: int) -> bool:
    return chat_id > 0


def node_address(node: str) -> Address:
    return Address(node=node, service=SERVICE)


async def command_or_none(
    node_link: ServiceLink, action: str, payload: dict, node: str
) -> dict | None:
    try:
        return await node_link.command(action, payload, dst=node_address(node))
    except (ProtoError, ServiceUnavailableError) as exc:
        log.warning("vpn: %s на %s не удалось: %s", action, node, exc)
        return None


async def fetch_card(
    node_link: ServiceLink, chat_id: int, subscription: Subscription | None = None
) -> tuple[str | None, list[dict], list[dict]]:
    answered = await vpn_nodes.usage_for_chat(node_link, chat_id)
    down = vpn_nodes.unavailable_servers(answered) if vpn_rights.is_admin(subscription) else []
    if not answered and not down:
        return VPN_UNAVAILABLE, [], []
    allowed = vpn_nodes.allowed_servers(answered)
    if not allowed and not down:
        return NO_VPN_ACCESS, [], []
    return None, allowed, down


def self_serve_nodes(servers: list[dict], config: Settings) -> list[str]:
    threshold = config.vpn.warn_remaining_gb * GB
    return [
        server["node"]
        for server in servers
        if server.get("node")
        and vpn_nodes.is_allowed(server)
        and server.get("remaining_bytes", 0) <= threshold
    ]


async def live_servers_for(node_link: ServiceLink, chat_id: int) -> list[dict]:
    live = await vpn_nodes.live_vpn_servers(node_link)
    servers = await vpn_nodes.fanout_usage(node_link, chat_id)
    allowed = {server.get("node") for server in servers if vpn_nodes.is_allowed(server)}
    return [item for item in live if item.get("node") in allowed]
