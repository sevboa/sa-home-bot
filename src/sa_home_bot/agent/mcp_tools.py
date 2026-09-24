"""MCP-сервер умений роя для будущего агента на mycraft (Этап 42.4, вторая
попытка — первая, HTTP + bearer-токены на Telegram-подписку на alfred, была
неправильной формой и откачена, см. IMPLEMENTATION_PLAN.md и план
``vast-skipping-crane.md``).

Реальный клиент этого сервера — не человек с внешним MCP-клиентом, а сам
инференс-стек агента (Phase 2 плана: Pydantic AI + Ollama), который будет жить
на mycraft — там же, где Ollama, отдельно от Telegram-бота на alfred.
``bot.tools`` спроектирован переиспользуемым без aiogram специально для таких
случаев (его уже импортирует служба ``tasks`` — см. её докстринг и
``ToolContext.emit``): любой процесс со своим ``node_link`` (подключением к
ЛОКАЛЬНОЙ ноде роя, см. ``tasks/app.py::run_tasks`` за тем же приёмом —
``ServiceLink`` к ``settings.node.socket`` без всего остального бота)
дотягивается через ``Address(node, service)`` до любой другой службы роя,
независимо от того, где физически исполняется вызывающий код. mycraft несёт
``memory``/``graph_memory``/``net`` (Этап 42.1) и, как любая нода, свой
собственный ``node.socket`` — агенту, работающему на mycraft, сетевой мост на
alfred для большинства тулов не нужен вовсе.

Транспорт — MCP ``stdio`` (``mcp.server.stdio``), не HTTP: агент и этот
процесс будут жить на одной машине, доверие = тот факт, что один процесс
породил другой (или их запустил один и тот же оператор) — сети и токенов не
нужно вовсе, в отличие от прежней HTTP-версии, где обязательным был bearer
``swarm.token``.

Права — фиксированная ``Subscription`` "агента", не per-Telegram-chat: этот
сервер не обслуживает конкретного Telegram-собеседника, а сам агент как
системного участника роя. Заводится как обычная владельческая подписка в
``config.toml`` (``[[subscriptions]]``, ``chat_id=AGENT_SENTINEL_CHAT_ID``) —
какие именно права дать агенту, решает пользователь правкой конфига, не код
(см. пример и комментарий в ``config.example.toml``). Нет такой подписки —
``tools_for(None)`` уже даёт fail-closed набор (тот же принцип, что у службы
``tasks`` без подписки, см. ``bot.tools.tools_for``).

Набор тулов — не весь ``tools_for(subscription)``, а минус ``_EXCLUDED_TOOLS``
(см. константу ниже) — тулы, завязанные на живые Telegram-объекты бота на
alfred, которых здесь физически нет; эта версия не пытается эмулировать мост
к ним (отдельная, более поздняя задача).
"""

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

if TYPE_CHECKING:  # pragma: no cover — только для аннотаций
    from mcp.server.context import ServerRequestContext

log = logging.getLogger(__name__)

SERVER_NAME = "sa-home-bot-agent-tools"

# Сентинел-chat_id агентской подписки — тот же приём, что уже используют
# vpn/service.py::NODE_SENTINEL_CHAT_ID (=0, "весь канал ноды" в квотах) и
# node/fixups.py::VPN_PROBE_CHAT_ID (=0, пробник vpn_check): зарезервированное
# значение для "не настоящего Telegram-чата, а системного участника роя".
# Намеренно НЕ 0 — та величина уже занята другим сентинелом в другом
# контексте (учёт квот VPN), заводить омоним не стоит, даже если таблицы не
# пересекаются. Реальный Telegram chat_id никогда не бывает -1: приватные
# чаты — положительные (около 9-10 цифр), группы — отрицательные, но по
# модулю уже с первых присвоенных id на порядки больше единицы (обычные
# группы) или заведомо в форме -100xxxxxxxxxx (супергруппы/каналы) —
# коллизия с -1 исключена практически.
AGENT_SENTINEL_CHAT_ID = -1

# Тулы, завязанные на живые Telegram-объекты бота (ctx.notifier/ctx.store/
# ctx.dismissal/ctx.book, или на историю живого треда ctx.history) — на
# mycraft их физически нет, мост к ним — отдельная, более поздняя задача
# (см. план vast-skipping-crane.md, Phase 2). Разбор по каждому тулу (каталог
# составлен разведкой bot/tools.py в этой же сессии):
#   dismiss         — мутирует ctx.dismissal (DismissalBox), исполняется уже
#                      ПОСЛЕ ответа хендлером bot/handlers/ai.py — без живого
#                      Telegram-хода исполнять нечего.
#   remind          — требует ctx.chat_id/ctx.dialogue_id/ctx.trigger_message_id
#                      живого треда и берёт снимок ctx.history; вне диалога
#                      сам тул честно отказывает, но семантика "запомнить и
#                      напомнить в ЭТОМ чате" здесь не имеет смысла вовсе.
#   tell/notify_guest — доставка личного сообщения через ctx.notifier/ctx.emit
#                      в чужой Telegram-чат, поиск получателя по ctx.book.
#   voice_mode       — переключает формат ОТВЕТА (voice/text) конкретного
#                      Telegram chat_id — вне Telegram-рендеринга бессмысленно.
#   look_at_photo    — ctx.store.latest_photo_turn(chat_id, dialogue_id):
#                      история присланных в Telegram-тред фото конкретного чата.
#   guests_list      — ctx.book.guests(): список гостей-подписчиков БОТА,
#                      внутренняя ACL-модель Telegram-развёртывания, не роя.
#   recall_tool_result — ctx.tool_result_cache живёт только в памяти ОДНОГО
#                      прохода run_chat_loop; для дискретных вызовов этого
#                      MCP-сервера кэша просто не существует, тул всегда
#                      отвечал бы "ничего не сохранено".
#   vpn              — ветки с доставкой секрета (issue/reissue/apk) шлют его
#                      строго через ctx.notifier.send_document/photo/direct в
#                      Telegram; тул неразделим на уровне ToolSpec на действие
#                      (один ToolSpec на все action), поэтому исключается
#                      целиком, а не только опасные ветки.
#   swarm_events     — НЕ было в исходном плане, добавлено при реализации:
#                      тул читает ctx.store.recent_events — SQLite-журнал уже
#                      отправленных ботом Telegram-уведомлений (db/store.py на
#                      alfred), которого на mycraft нет. В отличие от
#                      swarm_status (тот опрашивает рой ЖИВЬЁМ через
#                      node_link, доступен) — это исторический журнал именно
#                      бота, не текущее состояние роя.
_EXCLUDED_TOOLS = frozenset(
    {
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
    }
)


def _mcp_tool(declaration: dict[str, Any]) -> types.Tool:
    """OpenAI function-calling JSON (``ToolSpec.declaration``) → MCP ``Tool`` —
    оба формата описывают одно и то же (имя/описание/JSON Schema параметров),
    это перекладка полей в другую обёртку, не конвертация смысла."""
    fn = declaration["function"]
    return types.Tool(
        name=fn["name"],
        description=fn.get("description", ""),
        input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
    )


class AgentMcpTools:
    """MCP-сервер над подмножеством ``bot.tools`` для агента на mycraft.

    ``subscription`` резолвится ОДИН раз при старте, не на каждый вызов — в
    отличие от прежней HTTP-версии (много разных вызывающих, права зависели
    от запроса), здесь вызывающий один — решается конфигом, а не запросом.
    Правка прав = правка ``config.toml`` и рестарт, тот же принцип, что у
    владельческих подписок вообще (см. ``subscriptions/book.py``)."""

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
                if declaration["function"]["name"] not in _EXCLUDED_TOOLS
            ]
        )

    async def _handle_call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        # Чёрный список проверяется здесь тоже, не только в tools/list —
        # клиент мог запомнить имя из более раннего list_tools до правки
        # _EXCLUDED_TOOLS, либо просто выдумать имя (тот же приём, что
        # llm_chat.run_chat_loop использует для незаявленных тулов).
        if params.name in _EXCLUDED_TOOLS:
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
        except Exception as exc:  # noqa: BLE001 — сбой тула не должен ронять MCP-сессию
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
    """Standalone-точка входа: ``python -m sa_home_bot.agent.mcp_tools
    [--config ...]``. Логи — в stderr (``configure_logging``), stdout
    зарезервирован под MCP JSON-RPC (``stdio_server``) — их смешение сломало
    бы протокол."""
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
