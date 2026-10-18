from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, scan_limit
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ActionSpec, Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

RATE_LIMITED_SERVICES = frozenset({"monitor"})

ALREADY_QUEUED_TEXT = "⏳ Уже в очереди — дождитесь результата."


def unavailable_text(link: ServiceLink) -> str:
    return f"⚠️ Служба «{link.display_name}» недоступна — попробуйте позже."


async def find_action(
    link: ServiceLink, action_id: str, dst: Address | None = None
) -> ActionSpec | None:
    if dst is None:
        source = await link.actions()
    else:
        desc = await link.describe(dst=dst)
        source = desc.actions if desc is not None else ()
    for action in source:
        if action.id == action_id:
            return action
    return None


def build_args(action: ActionSpec, value: str | None) -> dict:
    if value is None or not action.params:
        return {}
    return {action.params[0].name: value}


def render_result(action: ActionSpec, result: dict) -> str:
    flags = [v for v in result.values() if isinstance(v, bool)]
    if flags and not any(flags):
        return ALREADY_QUEUED_TEXT
    return f"✅ Принято: {action.title}"


async def run_action(
    store: Store,
    link: ServiceLink,
    service: str,
    action_id: str,
    value: str | None = None,
    node_id: str | None = None,
) -> str:
    dst = Address(node=node_id, service=service)
    action = await find_action(link, action_id, dst=dst)
    if action is None:
        return unavailable_text(link) if not link.connected else "Действие недоступно."

    limited = service in RATE_LIMITED_SERVICES
    key = f"{node_id}:{service}:{action_id}" if node_id else f"{service}:{action_id}"
    decision = None
    if limited:
        now = datetime.now(tz=UTC)
        decision = scan_limit.decide(await store.get_action_ticks(key), now)
        if not decision.allowed:
            return decision.reason

    try:
        result = await link.command(action.id, build_args(action, value), dst=dst)
    except ServiceUnavailableError:
        return unavailable_text(link)
    except ProtoError as exc:
        return f"⚠️ Ошибка: {exc.message}"
    except (ConnectionError, OSError, TimeoutError):
        return f"⚠️ Служба «{link.display_name}» не ответила вовремя — попробуйте позже."

    text = render_result(action, result)
    if limited and decision is not None and text != ALREADY_QUEUED_TEXT:
        await store.set_action_ticks(key, list(decision.ticks))
    return text


def rows(buttons: list[InlineKeyboardButton]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    )


def build_choice_keyboard(
    subscription: Subscription | None,
    service_actions: Sequence[ActionSpec],
    service: str,
    value: str,
    node_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    if subscription is None:
        return None
    buttons = [
        InlineKeyboardButton(
            text=action.title,
            callback_data=commands.action_callback(action.id, value, node_id, service=service),
        )
        for action in service_actions
        if subscription.allows_action(action.id, service)
        and action.params
        and action.params[0].choices
        and value in action.params[0].choices
    ]
    return rows(buttons)
