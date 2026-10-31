from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp

from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.utils.files import write_private

TOKEN_PATH = "~/.config/sa-home-bot/devlab-token"
CONNECT_TIMEOUT = aiohttp.ClientTimeout(total=15)
CLAUDE_BINARY = "claude"

Fetch = Callable[[str, str], Awaitable[str]]
Runner = Callable[[list[str]], int]


class ConnectError(Exception):
    pass


def hub_url(settings: Settings) -> str:
    cfg = settings.devlab_hub
    return f"http://{cfg.hub_node}:{cfg.port}"


async def fetch_token(base_url: str, swarm_token: str) -> str:
    try:
        async with aiohttp.ClientSession(timeout=CONNECT_TIMEOUT) as session:
            async with session.post(
                base_url + protocol.HTTP_CONNECT_PATH,
                headers={protocol.HEADER_SWARM_TOKEN: swarm_token},
            ) as resp:
                if resp.status != 200:
                    detail = (await resp.text())[:200]
                    raise ConnectError(f"хаб ответил HTTP {resp.status} {detail}".strip())
                return str((await resp.json())[protocol.FIELD_TOKEN])
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise ConnectError(f"хаб {base_url} недоступен: {type(exc).__name__}") from exc


def mcp_add_command(url: str, token_path: Path) -> list[str]:
    return [
        CLAUDE_BINARY,
        "mcp",
        "add",
        "--scope",
        "user",
        "--transport",
        "http",
        protocol.MCP_CLIENT_NAME,
        url + protocol.HTTP_MCP_PATH,
        "--header",
        f"Authorization: Bearer {token_path.read_text().strip()}",
    ]


def mcp_add_hint(url: str, token_path: Path) -> str:
    return (
        f"{CLAUDE_BINARY} mcp add --scope user --transport http {protocol.MCP_CLIENT_NAME} "
        f'{url}{protocol.HTTP_MCP_PATH} --header "Authorization: Bearer $(cat {token_path})"'
    )


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


async def connect(
    settings: Settings,
    *,
    token_path: str = TOKEN_PATH,
    fetch: Fetch = fetch_token,
    runner: Runner = _run,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    url = hub_url(settings)
    try:
        token = await fetch(url, settings.swarm.token)
    except ConnectError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    path = Path(token_path).expanduser()
    write_private(path, token + "\n", enforce_mode=True, make_parents=True)
    print(f"Токен записан в {path}")
    if which(CLAUDE_BINARY) is None:
        print("claude не найден в PATH. Подключи MCP вручную:")
        print(mcp_add_hint(url, path))
        return 0
    code = runner(mcp_add_command(url, path))
    if code != 0:
        print(f"claude mcp add завершился с кодом {code}. Команда для ручного запуска:")
        print(mcp_add_hint(url, path))
        return code
    print(f"MCP «{protocol.MCP_CLIENT_NAME}» подключён: {url}{protocol.HTTP_MCP_PATH}")
    return 0
