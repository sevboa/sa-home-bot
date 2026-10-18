from __future__ import annotations

import pytest

from sa_home_bot.agent.mcp_tools import AGENT_SENTINEL_CHAT_ID, TELEGRAM_ONLY_TOOLS, AgentMcpTools
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import Settings, SubscriptionConfig
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, ProtoError


class FakeNodeLink:
    display_name = "нода"

    def __init__(self, own=None):
        self._own = own or {"node": "mycraft", "kind": "server", "peers": []}
        self.commands: list[str] = []

    async def get_state(self, dst=None):
        if dst is None:
            return self._own
        raise ServiceUnavailableError("нет связи")

    async def command(self, action, args=None, dst=None, timeout=None):
        self.commands.append(action)
        raise ProtoError(ERR_UNKNOWN_ACTION, f"нет действия {action}")


def _settings(allowed_commands: list[str] | None = None) -> Settings:
    subs = []
    if allowed_commands is not None:
        subs.append(
            SubscriptionConfig(
                name="agent", chat_id=AGENT_SENTINEL_CHAT_ID, allowed_commands=allowed_commands
            )
        )
    return Settings(subscriptions=subs)


def _tool_names(list_result) -> set[str]:
    return {t.name for t in list_result.tools}


class _Params:
    def __init__(self, name: str, arguments: dict | None = None) -> None:
        self.name = name
        self.arguments = arguments


async def test_list_tools_without_agent_subscription_is_fail_closed():
    server = AgentMcpTools(settings=_settings(None), node_link=FakeNodeLink())
    result = await server._handle_list_tools(None, None)
    names = _tool_names(result)
    assert "calc" in names
    assert "swarm_status" not in names


async def test_list_tools_reflects_agent_subscription_rights():
    server = AgentMcpTools(settings=_settings(["nodes"]), node_link=FakeNodeLink())
    result = await server._handle_list_tools(None, None)
    names = _tool_names(result)
    assert "swarm_status" in names
    assert "torrents" not in names


async def test_excluded_tools_never_appear_even_with_full_rights():
    server = AgentMcpTools(settings=_settings(["*"]), node_link=FakeNodeLink())
    result = await server._handle_list_tools(None, None)
    names = _tool_names(result)
    assert names.isdisjoint(TELEGRAM_ONLY_TOOLS)
    assert "swarm_status" in names


async def test_call_tool_calc_runs_real_handler():
    server = AgentMcpTools(settings=_settings(None), node_link=FakeNodeLink())
    result = await server._handle_call_tool(None, _Params("calc", {"expression": "2+2"}))
    assert result.is_error is False
    assert result.content[0].text == "4"


async def test_call_tool_swarm_status_reaches_node_link():
    server = AgentMcpTools(settings=_settings(["nodes"]), node_link=FakeNodeLink())
    result = await server._handle_call_tool(None, _Params("swarm_status", {"what": "nodes"}))
    assert result.is_error is False
    assert "mycraft" in result.content[0].text


@pytest.mark.parametrize(
    ("allowed_commands", "tool", "arguments", "expected_in_message"),
    [
        (["*"], "vpn", {"action": "usage"}, "неизвестный инструмент"),
        (None, "swarm_status", {"what": "nodes"}, "неизвестный инструмент"),
        (None, "bogus_tool", {}, "bogus_tool"),
    ],
    ids=["telegram-only-with-full-rights", "no-rights", "unknown-name"],
)
async def test_call_tool_is_denied(allowed_commands, tool, arguments, expected_in_message):
    server = AgentMcpTools(settings=_settings(allowed_commands), node_link=FakeNodeLink())
    result = await server._handle_call_tool(None, _Params(tool, arguments))
    assert result.is_error is True
    assert expected_in_message in result.content[0].text


def test_telegram_only_flag_marks_expected_tools():
    assert TELEGRAM_ONLY_TOOLS == {
        "dismiss",
        "remind",
        "tell",
        "notify_guest",
        "voice_mode",
        "look_at_photo",
        "guests_list",
        "recall_tool_result",
        "vpn",
        "swarm_events",
        "act",
        "search",
        "walk",
        "go",
    }
