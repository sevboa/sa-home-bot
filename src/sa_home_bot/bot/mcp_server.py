"""MCP-сервер над тулами бота (Этап 42.4) — переносимость инструментов
Альфреда (память, тулы роя, remind и т.д., см. bot/tools.py) между моделями
и клиентами, не завязанная на самодельный протокол роя (proto/).

Живёт ВНУТРИ процесса бота, а не отдельной службой роя: тулам нужны живые
``notifier``/``store``/``book``/``node_link`` — у бота они уже есть, а у
службы tasks (второй пользователь bot/tools.py) их нет, поэтому она ходит
через мост-событие (см. докстринг ``bot.tools.ToolContext.emit``). Здесь
мост не нужен — MCP-сервер строит ``ToolContext`` из тех же живых объектов,
что и polling-хендлеры /ai.

Авторизация — БЕЗ встроенного OAuth-слоя SDK (``mcp.server.auth``): та ветка
рассчитана на полноценный authorization server (RFC 9728 protected-resource
metadata, `/.well-known/...`, dynamic client registration) — у нас же токен
самостоятельно выпускает сам бот (``/mcp_token``, db/store.py::
issue_mcp_token) для уже существующей Telegram-подписки, отдельная схема
прав не нужна: MCP-клиент действует ровно с правами той подписки, чей
bearer-токен предъявил (``Subscription``/``tools_for`` — тот же код, что и у
живого /ai, см. bot/tools.py). Поэтому здесь используется низкоуровневый
``mcp.server.lowlevel.Server`` (не ``MCPServer`` — тот требует ``auth=
AuthSettings(issuer_url=..., resource_server_url=...)``, если передан
``token_verifier``), и токен читается вручную из заголовка Authorization
запроса, до которого низкоуровневый ``Server`` даёт добраться через
``ServerRequestContext.request`` (сырой Starlette ``Request``, см.
``mcp.server.runner._make_context``).

``stateless=True`` у ``StreamableHTTPSessionManager`` — тул-вызовы независимы
друг от друга (в отличие от долгоживущей MCP-сессии с ресурсами/подписками),
и без session-tracking не нужно ничего чистить по таймауту на слабом CPU
alfred (см. CLAUDE.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager

from sa_home_bot import __version__
from sa_home_bot.bot import tools as bot_tools

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from mcp.server.context import ServerRequestContext

    from sa_home_bot.bot.notifier import Notifier
    from sa_home_bot.bot.service_link import ServiceLink
    from sa_home_bot.config import Settings
    from sa_home_bot.db.store import Store
    from sa_home_bot.subscriptions.book import SubscriptionBook
    from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

SERVER_NAME = "sa-home-bot"


def _mcp_tool(declaration: dict[str, Any]) -> types.Tool:
    """OpenAI function-calling JSON (ToolSpec.declaration) → MCP Tool —
    оба формата описывают одно и то же (имя/описание/JSON Schema
    параметров), поэтому это не конвертация смысла, а просто перекладка
    полей в другую обёртку."""
    fn = declaration["function"]
    return types.Tool(
        name=fn["name"],
        description=fn.get("description", ""),
        input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
    )


class McpServer:
    def __init__(
        self,
        *,
        settings: Settings,
        book: SubscriptionBook,
        notifier: Notifier,
        store: Store,
        node_link: ServiceLink,
        host: str,
        port: int,
    ) -> None:
        self._settings = settings
        self._book = book
        self._notifier = notifier
        self._store = store
        self._node_link = node_link
        self._host = host
        self._port = port

        self._server: Server[dict[str, Any]] = Server(
            SERVER_NAME,
            version=__version__,
            on_list_tools=self._handle_list_tools,
            on_call_tool=self._handle_call_tool,
        )
        self._session_manager = StreamableHTTPSessionManager(app=self._server, stateless=True)
        self._asgi_app = StreamableHTTPASGIApp(self._session_manager)
        self._uvicorn_server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def _resolve_subscription(
        self, ctx: ServerRequestContext[Any, Any]
    ) -> Subscription | None:
        """Bearer-токен запроса → Subscription. Нет заголовка/токена
        неизвестен/подписки для него уже нет — None, что для
        ``tools_for(None)`` уже даёт fail-closed набор тулов (тот же
        принцип, что у службы tasks без подписки, см. докстринг
        ``bot.tools.tools_for``) — здесь его переизобретать не нужно."""
        request = ctx.request
        if request is None:
            return None
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return None
        token = auth_header[len("bearer ") :].strip()
        if not token:
            return None
        chat_id = await self._store.resolve_mcp_token(token)
        if chat_id is None:
            return None
        return self._book.for_chat(chat_id)

    async def _handle_list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        subscription = await self._resolve_subscription(ctx)
        toolkit = bot_tools.tools_for(subscription)
        return types.ListToolsResult(tools=[_mcp_tool(d) for d in toolkit.declarations])

    async def _handle_call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        subscription = await self._resolve_subscription(ctx)
        # Тот же отфильтрованный комплект, что ушёл в tools/list: тул, на
        # который у этой подписки нет прав, модель/клиент не увидит в списке
        # вовсе, а прямой вызов по имени (в обход списка) сюда всё равно не
        # пройдёт — права проверены один раз и тем же кодом, что у /ai (см.
        # llm_chat.py::run_chat_loop, тот же приём для неизвестного тула).
        toolkit = bot_tools.tools_for(subscription)
        handler = toolkit.handlers.get(params.name)
        if handler is None:
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=f"неизвестный инструмент: {params.name}")
                ],
                is_error=True,
            )
        tool_ctx = bot_tools.ToolContext(
            chat_id=subscription.chat_id if subscription else None,
            dialogue_id=None,
            trigger_message_id=None,
            settings=self._settings,
            node_link=self._node_link,
            subscription=subscription,
            book=self._book,
            notifier=self._notifier,
            store=self._store,
            author=subscription.name if subscription else None,
        )
        try:
            result_text = await handler(tool_ctx, dict(params.arguments or {}))
        except Exception as exc:  # noqa: BLE001 — сбой тула не должен ронять MCP-сессию
            log.exception("mcp_server: тул %s упал", params.name)
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=f"внутренняя ошибка инструмента: {exc}")
                ],
                is_error=True,
            )
        return types.CallToolResult(content=[types.TextContent(type="text", text=result_text)])

    async def start(self) -> None:
        """uvicorn — фоновой задачей ВНУТРИ уже работающего event loop бота
        (не отдельный процесс): ``lifespan="off"``, потому что ASGI-обвязка
        MCP не отвечает на lifespan-события сама, а собственный жизненный
        цикл сессий (``StreamableHTTPSessionManager.run()``) заводим и
        закрываем вручную вокруг ``serve()``."""
        config = uvicorn.Config(
            self._asgi_app,
            host=self._host,
            port=self._port,
            lifespan="off",
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)

        async def _serve() -> None:
            async with self._session_manager.run():
                await self._uvicorn_server.serve()  # type: ignore[union-attr]

        self._task = asyncio.create_task(_serve(), name="mcp-server")
        log.info("MCP-сервер запущен на %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
