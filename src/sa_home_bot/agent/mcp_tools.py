from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import TYPE_CHECKING, Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from sa_home_bot import __version__
from sa_home_bot.bot import tools as bot_tools
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.logging import configure_logging

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.context import ServerRequestContext

log = logging.getLogger(__name__)

SERVER_NAME = "sa-home-bot-agent-tools"

AGENT_SENTINEL_CHAT_ID = -1

TELEGRAM_ONLY_TOOLS = frozenset(spec.name for spec in bot_tools.TOOLS if spec.telegram_only)


def _mcp_tool(declaration: dict[str, Any]) -> types.Tool:
    fn = declaration["function"]
    return types.Tool(
        name=fn["name"],
        description=fn.get("description", ""),
        input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
    )


class AgentMcpTools:
    def __init__(self, *, settings: Settings, node_link: ServiceLink) -> None:
        self._settings = settings
        self._node_link = node_link
        book = SubscriptionBook.from_config(settings.subscriptions, settings.guest_subscriptions)
        self._subscription: Subscription | None = book.for_chat(AGENT_SENTINEL_CHAT_ID)
        if self._subscription is None:
            log.warning(
                "Нет [[subscriptions]] с chat_id=%s в конфиге — агенту не "
                "выставлен ни один тул с requires (fail-closed, см. "
                "bot.tools.tools_for); добавьте подписку, см. пример в "
                "config.example.toml",
                AGENT_SENTINEL_CHAT_ID,
            )
        self._server: Server[dict[str, Any]] = Server(
            SERVER_NAME,
            version=__version__,
            on_list_tools=self._handle_list_tools,
            on_call_tool=self._handle_call_tool,
        )

    def _toolkit(self) -> bot_tools.ToolKit:
        return bot_tools.tools_for(self._subscription)

    async def _handle_list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        toolkit = self._toolkit()
        return types.ListToolsResult(
            tools=[
                _mcp_tool(declaration)
                for declaration in toolkit.declarations
                if declaration["function"]["name"] not in TELEGRAM_ONLY_TOOLS
            ]
        )

    async def _handle_call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name in TELEGRAM_ONLY_TOOLS:
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=f"неизвестный инструмент: {params.name}")
                ],
                is_error=True,
            )
        toolkit = self._toolkit()
        handler = toolkit.handlers.get(params.name)
        if handler is None:
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=f"неизвестный инструмент: {params.name}")
                ],
                is_error=True,
            )
        tool_ctx = bot_tools.ToolContext(
            chat_id=AGENT_SENTINEL_CHAT_ID,
            dialogue_id=None,
            trigger_message_id=None,
            settings=self._settings,
            node_link=self._node_link,
            subscription=self._subscription,
            author="агент",
        )
        try:
            result_text = await handler(tool_ctx, dict(params.arguments or {}))
        except Exception as exc:  # noqa: BLE001
            log.exception("agent.mcp_tools: тул %s упал", params.name)
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=f"внутренняя ошибка инструмента: {exc}")
                ],
                is_error=True,
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=result_text)])

    async def run_stdio(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self._server.run(
                read_stream, write_stream, self._server.create_initialization_options()
            )


async def _run(settings: Settings) -> None:
    node_link = ServiceLink(
        settings.node.socket, token=settings.swarm.token, display_name="нода (agent-mcp-tools)"
    )
    await node_link.start()
    tools = AgentMcpTools(settings=settings, node_link=node_link)
    try:
        await tools.run_stdio()
    finally:
        await node_link.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sa-home-bot-agent-mcp-tools",
        description="MCP stdio-сервер умений роя для агента на mycraft (Этап 42.4).",
    )
    parser.add_argument("--config", "-c", default=None, help="путь к config.toml")
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    configure_logging(settings.logging.level, settings.logging.format)
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
