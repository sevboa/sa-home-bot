from __future__ import annotations

from typing import Any

from sa_home_bot.bot.tools import (
    acquaintance,
    calc,
    chat_controls,
    currency,
    get_time,
    guests_list,
    interactive_tools,
    memory,
    node_manage,
    notify,
    people_tools,
    pictures,
    remind,
    swarm,
    tell,
    torrents,
    vpn,
    weather,
    web_search,
)
from sa_home_bot.bot.tools.core import ToolHandler, ToolKit, ToolSpec
from sa_home_bot.subscriptions.models import Subscription

TOOLS: tuple[ToolSpec, ...] = (
    calc.CALC,
    weather.GET_WEATHER,
    currency.CONVERT_CURRENCY,
    get_time.GET_TIME,
    pictures.LOOK_AT_PHOTO,
    swarm.SWARM_STATUS,
    node_manage.NODE_MANAGE,
    swarm.SWARM_EVENTS,
    torrents.TORRENTS,
    memory.MEMORY,
    vpn.VPN,
    chat_controls.DISMISS,
    chat_controls.VOICE_MODE,
    web_search.WEB_SEARCH,
    chat_controls.RECALL_TOOL_RESULT,
    tell.TELL,
    guests_list.GUESTS_LIST,
    notify.NOTIFY_PERSONA,
    notify.NOTIFY_GUEST,
    remind.REMIND,
    acquaintance.REQUEST_ACQUAINTANCE,
    people_tools.NOTE_PERSON,
    people_tools.FIND_PERSON,
    acquaintance.MY_ACQUAINTANCES,
    interactive_tools.SWAP_RADIO,
    interactive_tools.TAKE_PHOTO,
    interactive_tools.SEARCH,
    interactive_tools.ACT,
    interactive_tools.WALK,
    interactive_tools.GO,
    interactive_tools.MANOR_ITEMS,
    interactive_tools.ITEM_ACTION,
    pictures.GENERATE_IMAGE,
    pictures.FIND_IMAGE,
)


def tools_for(subscription: Subscription | None) -> ToolKit:
    declarations: list[dict[str, Any]] = []
    handlers: dict[str, ToolHandler] = {}
    for spec in TOOLS:
        if subscription is None:
            if spec.requires is not None or spec.variants is not None:
                continue
            declaration: dict[str, Any] | None = spec.declaration
        else:
            declaration = spec.declaration_for(subscription)
        if declaration is None:
            continue
        declarations.append(declaration)
        handlers[spec.name] = spec.handler
    return ToolKit(declarations=declarations, handlers=handlers)
