from __future__ import annotations

from sa_home_bot.bot import vpn_callbacks
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol


def allows(subscription: Subscription | None, action_id: str) -> bool:
    return subscription is not None and subscription.allows_action(action_id, vpn_callbacks.SERVICE)


def is_admin(subscription: Subscription | None) -> bool:
    return allows(subscription, vpn_protocol.ACTION_PEERS)
