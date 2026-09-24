"""Интерактивный CLI для ручного тестирования агента (Этап 42.4, Phase 2 —
план ``vast-skipping-crane.md``): Pydantic AI + Ollama + MCP-тулы из
``agent/mcp_tools.py``, всё на одной машине (mycraft).

Не подключается к Telegram и не трогает живой ``/ai`` (``bot/ai_flow.py``/
``llm_chat.py``/``tasks/service.py``/``llm/service.py``) — отдельный, ничего
не заменяющий путь для дог-фудинга агент-фреймворка, пока не решено, как он
будет запускаться по-настоящему (см. «Phase 2» в плане: по требованию,
отдельный action службы ``llm``, отдельная служба роя — не решено).

MCP-тулы — тем же процессом ``python -m sa_home_bot.agent.mcp_tools``,
запущенным как дочерний (``fastmcp.client.transports.StdioTransport`` —
доверие=процесс, без сети/токенов, см. докстринг ``agent/mcp_tools.py``).

Персонажный промпт — то же поле, что использует живой ``/ai``
(``settings.llm.persona_prompt``, см. ``llm/service.py``: там это ровно
``system[0]`` без какой-либо дополнительной сборки) — не тянем
``llm/service.py`` целиком (Coqui/faster-whisper и т.п. там тяжёлые), только
лёгкий фоллбэк из ``llm/prompt.py`` (без тяжёлых импортов), если
``persona_prompt`` пуст (свежий чекаут/CI)."""

from __future__ import annotations

import argparse
import asyncio
import sys

from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.ollama import OllamaProvider

from sa_home_bot.config import Settings
from sa_home_bot.llm.prompt import DEFAULT_PERSONA_PROMPT

MCP_TOOLS_MODULE = "sa_home_bot.agent.mcp_tools"


def _build_agent(settings: Settings, config_path: str | None) -> Agent:
    model = OpenAIChatModel(
        model_name=settings.llm.model,
        provider=OllamaProvider(base_url=f"{settings.llm.ollama_url}/v1"),
    )
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", MCP_TOOLS_MODULE, *(["--config", config_path] if config_path else [])],
    )
    toolset = MCPToolset(Client(transport))
    system_prompt = settings.llm.persona_prompt or DEFAULT_PERSONA_PROMPT
    return Agent(model, toolsets=[toolset], system_prompt=system_prompt)


async def _run_once(agent: Agent, text: str) -> None:
    async with agent:
        result = await agent.run(text)
    print(result.output)


async def _run_interactive(agent: Agent) -> None:
    history: list[ModelMessage] = []
    print("Агент готов (Ctrl+D/Ctrl+C — выход).")
    async with agent:
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line.strip():
                continue
            result = await agent.run(line, message_history=history)
            history.extend(result.new_messages())
            print(result.output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sa-home-bot-agent-chat",
        description="CLI-чат с агентом Pydantic AI + MCP-тулы (Этап 42.4, Phase 2)",
    )
    parser.add_argument("--config", "-c", default=None, help="путь к config.toml")
    parser.add_argument(
        "--once",
        default=None,
        metavar="TEXT",
        help="один обмен (для неинтерактивного смоук-теста) вместо REPL",
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2
    agent = _build_agent(settings, args.config)
    if args.once is not None:
        asyncio.run(_run_once(agent, args.once))
    else:
        asyncio.run(_run_interactive(agent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
