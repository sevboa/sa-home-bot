"""MCP stdio-сервер умений роя для агента (Этап 42.4, agent/mcp_tools.py).

Хендлеры (``_handle_list_tools``/``_handle_call_tool``) тестируются напрямую
(поддельный ``ctx`` — они его не читают вовсе, права резолвятся один раз при
старте из фиксированной подписки, не из запроса), не через реальный
stdio-процесс: это проверяет саму логику (чёрный список + tools_for), не
хрупкость живого пайпа. Реальный stdio-транспорт (spawn+``mcp.stdio_client``)
проверен смоуком отдельно, см. отчёт задачи."""

from __future__ import annotations

from sa_home_bot.agent.mcp_tools import _EXCLUDED_TOOLS, AGENT_SENTINEL_CHAT_ID, AgentMcpTools
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.config import Settings, SubscriptionConfig
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, ProtoError


class FakeNodeLink:
    """Тот же приём, что tests/unit/test_tasks_service.py::FakeNodeLink —
    минимальная своя нода без пиров, достаточно, чтобы swarm_status/
    node_manage реально прошли через node_link, не упав на "нет связи"."""

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
    assert "calc" in names  # requires=None — виден всем, см. bot/tools.py::TOOLS
    assert "swarm_status" not in names  # требует права "nodes" — подписки нет вовсе


async def test_list_tools_reflects_agent_subscription_rights():
    server = AgentMcpTools(settings=_settings(["nodes"]), node_link=FakeNodeLink())
    result = await server._handle_list_tools(None, None)
    names = _tool_names(result)
    assert "swarm_status" in names  # право "nodes" — есть
    assert "torrents" not in names  # права на torrents — нет


async def test_excluded_tools_never_appear_even_with_full_rights():
    """Чёрный список (Telegram-специфичные тулы) применяется ПОВЕРХ прав —
    даже подписка-владелец ("*") их не видит, см. докстринг модуля."""
    server = AgentMcpTools(settings=_settings(["*"]), node_link=FakeNodeLink())
    result = await server._handle_list_tools(None, None)
    names = _tool_names(result)
    assert names.isdisjoint(_EXCLUDED_TOOLS)
    assert "swarm_status" in names  # право есть — переносимый тул виден


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


async def test_call_tool_excluded_tool_is_denied_even_with_full_rights():
    server = AgentMcpTools(settings=_settings(["*"]), node_link=FakeNodeLink())
    result = await server._handle_call_tool(None, _Params("vpn", {"action": "usage"}))
    assert result.is_error is True
    assert "неизвестный инструмент" in result.content[0].text


async def test_call_tool_privileged_tool_without_rights_is_denied():
    server = AgentMcpTools(settings=_settings(None), node_link=FakeNodeLink())
    result = await server._handle_call_tool(None, _Params("swarm_status", {"what": "nodes"}))
    assert result.is_error is True
    assert "неизвестный инструмент" in result.content[0].text


async def test_call_tool_unknown_name_is_denied():
    server = AgentMcpTools(settings=_settings(None), node_link=FakeNodeLink())
    result = await server._handle_call_tool(None, _Params("bogus_tool", {}))
    assert result.is_error is True
    assert "bogus_tool" in result.content[0].text
