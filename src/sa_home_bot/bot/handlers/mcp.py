"""/mcp_token, /mcp_revoke — выпуск и отзыв bearer-токена для MCP-сервера
над тулами бота (Этап 42.4, bot/mcp_server.py).

Права уже проверены AuthorizationMiddleware (право `mcp`, см. commands.py) —
здесь только сама выдача/отзыв. Токен показывается ровно один раз: в БД
(db/store.py::issue_mcp_token) остаётся только его хеш, повторно посмотреть
его нельзя — только перевыпустить (это молча отзывает прежний, см. schema.sql
про PRIMARY KEY(chat_id)).
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store

router = Router(name="mcp")


@router.message(Command(commands.MCP_TOKEN.name))
async def cmd_mcp_token(message: Message, store: Store, config: Settings) -> None:
    token = await store.issue_mcp_token(message.chat.id, datetime.now(tz=UTC))
    disabled_note = (
        "\n\n⚠️ MCP-сервер сейчас выключен в конфиге ([mcp].enabled=false) — "
        "подключиться пока некуда."
        if not config.mcp.enabled
        else ""
    )
    await message.answer(
        "🔌 <b>MCP-токен</b>\n\n"
        f"<code>{token}</code>\n\n"
        f"Эндпоинт: <code>http://{config.mcp.host}:{config.mcp.port}/mcp</code>\n\n"
        "<i>Токен даёт те же права, что у этого чата в Telegram, и показывается "
        "только сейчас — сохраните его. Посмотреть повторно нельзя, только "
        "перевыпустить командой (перевыпуск отзывает прежний); отозвать без "
        f"замены — /mcp_revoke.</i>{disabled_note}"
    )


@router.message(Command(commands.MCP_REVOKE.name))
async def cmd_mcp_revoke(message: Message, store: Store) -> None:
    ok = await store.revoke_mcp_token(message.chat.id)
    await message.answer(
        "🔌 MCP-токен отозван." if ok else "🔌 У этого чата и так не было MCP-токена."
    )
