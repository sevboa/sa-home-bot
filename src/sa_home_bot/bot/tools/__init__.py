from __future__ import annotations

import random

from sa_home_bot.bot.tools.acquaintance import (
    _DECL_REQUEST_ACQUAINTANCE,
    RELATION_ACQUAINTANCE,
    acquaintance_conflict,
    acquaintance_roster,
    are_acquainted,
    tool_my_acquaintances,
    tool_request_acquaintance,
)
from sa_home_bot.bot.tools.calc import tool_calc
from sa_home_bot.bot.tools.chat_controls import (
    DISMISS_MODEL,
    DISMISS_OFF,
    DISMISS_SLEEP,
    tool_dismiss,
    tool_recall_tool_result,
    tool_voice_mode,
)
from sa_home_bot.bot.tools.core import (
    CANARY_TOOLS,
    SERIAL_DEFERRED,
    SERIAL_TOOLS,
    ActionRight,
    CommandRight,
    DismissalBox,
    ToolContext,
    ToolHandler,
    ToolKit,
    ToolSpec,
    VariantRights,
    for_context,
)
from sa_home_bot.bot.tools.currency import tool_convert_currency
from sa_home_bot.bot.tools.delivery import (
    NEED_RECIPIENT_ID,
    TELL_MAX_PER_RECIPIENT_PER_HOUR,
    TELL_MAX_TOTAL_PER_HOUR,
    parse_recipient_id,
)
from sa_home_bot.bot.tools.get_time import WEEKDAYS_RU, place_now, tool_get_time
from sa_home_bot.bot.tools.guests_list import tool_guests_list
from sa_home_bot.bot.tools.interactive_tools import (
    tool_act,
    tool_go,
    tool_item_action,
    tool_manor_items,
    tool_search,
    tool_swap_radio,
    tool_take_photo,
    tool_walk,
)
from sa_home_bot.bot.tools.memory import tool_memory
from sa_home_bot.bot.tools.node_manage import tool_node_manage
from sa_home_bot.bot.tools.notify import (
    _NOTIFY_PERSONAS,
    render_notify,
    tool_notify_guest,
    tool_notify_persona,
)
from sa_home_bot.bot.tools.people_tools import tool_find_person, tool_note_person
from sa_home_bot.bot.tools.pictures import (
    ACTION_LOOK_AT_PHOTO,
    tool_find_image,
    tool_generate_image,
    tool_look_at_photo,
)
from sa_home_bot.bot.tools.registry import TOOLS, tools_for
from sa_home_bot.bot.tools.remind import (
    EVENT_RESTART_APPLIED,
    EVENT_UPDATE_FINISHED,
    schedule_agent_dialogue,
    tool_remind,
)
from sa_home_bot.bot.tools.swarm import tool_swarm_events, tool_swarm_status
from sa_home_bot.bot.tools.tell import render_tell, tool_tell
from sa_home_bot.bot.tools.torrents import tool_torrents
from sa_home_bot.bot.tools.vpn import tool_vpn
from sa_home_bot.bot.tools.weather import tool_get_weather
from sa_home_bot.bot.tools.web_search import tool_web_search
from sa_home_bot.config import reminder_reason
from sa_home_bot.proto.addresses import LLM_NODE, LLM_SERVICE
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

__all__ = [
    "ACTION_LOOK_AT_PHOTO",
    "ActionRight",
    "CANARY_TOOLS",
    "CommandRight",
    "DISMISS_MODEL",
    "DISMISS_OFF",
    "DISMISS_SLEEP",
    "DismissalBox",
    "EVENT_RESTART_APPLIED",
    "EVENT_UPDATE_FINISHED",
    "LLM_NODE",
    "LLM_SERVICE",
    "NEED_RECIPIENT_ID",
    "ProtoError",
    "RELATION_ACQUAINTANCE",
    "SERIAL_DEFERRED",
    "SERIAL_TOOLS",
    "TELL_MAX_PER_RECIPIENT_PER_HOUR",
    "TELL_MAX_TOTAL_PER_HOUR",
    "TOOLS",
    "ToolContext",
    "ToolHandler",
    "ToolKit",
    "ToolSpec",
    "VariantRights",
    "WEEKDAYS_RU",
    "_DECL_REQUEST_ACQUAINTANCE",
    "_NOTIFY_PERSONAS",
    "acquaintance_conflict",
    "acquaintance_roster",
    "are_acquainted",
    "for_context",
    "parse_recipient_id",
    "place_now",
    "random",
    "reminder_reason",
    "render_notify",
    "render_tell",
    "schedule_agent_dialogue",
    "tool_act",
    "tool_calc",
    "tool_convert_currency",
    "tool_dismiss",
    "tool_find_image",
    "tool_find_person",
    "tool_generate_image",
    "tool_get_time",
    "tool_get_weather",
    "tool_go",
    "tool_guests_list",
    "tool_item_action",
    "tool_look_at_photo",
    "tool_manor_items",
    "tool_memory",
    "tool_my_acquaintances",
    "tool_node_manage",
    "tool_note_person",
    "tool_notify_guest",
    "tool_notify_persona",
    "tool_recall_tool_result",
    "tool_remind",
    "tool_request_acquaintance",
    "tool_search",
    "tool_swap_radio",
    "tool_swarm_events",
    "tool_swarm_status",
    "tool_take_photo",
    "tool_tell",
    "tool_torrents",
    "tool_voice_mode",
    "tool_vpn",
    "tool_walk",
    "tool_web_search",
    "tools_for",
    "vpn_protocol",
]
