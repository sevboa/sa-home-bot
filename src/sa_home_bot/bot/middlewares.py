"""Middleware: контекст чата + авторизация команд (chat-level).

Универсальные команды (help/ping/whoami) проходят без проверок. Управляющие —
только если чат подписной, не broken и имя команды есть в allowed_commands
(см. AUTHORIZATION.md §5).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)

DENIED_TEXT = "⛔️ Команда недоступна в этом чате."


def extract_command(text: str | None) -> str | None:
    if not text or not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0]
    return head[1:].split("@", 1)[0]


class AuthorizationMiddleware(BaseMiddleware):
    def __init__(self, book: SubscriptionBook) -> None:
        self._book = book

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        chat_id = event.chat.id if event.chat else None
        subscription = self._book.for_chat(chat_id) if chat_id is not None else None
        data["subscription"] = subscription

        command = extract_command(event.text)

        # Не команда или универсальная — пропускаем без проверок.
        if command is None or commands.is_universal(command):
            return await handler(event, data)

        # Управляющая команда — проверяем права.
        if commands.is_control(command):
            if subscription is None or subscription.broken or not subscription.allows_command(
                command
            ):
                log.info("Отказ в /%s для chat_id=%s", command, chat_id)
                await event.answer(DENIED_TEXT)
                return None

        return await handler(event, data)


class CallbackAuthorizationMiddleware(BaseMiddleware):
    """Права на кнопки-действия под /status (callback_data «st:<код>»)."""

    def __init__(self, book: SubscriptionBook) -> None:
        self._book = book

    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        chat = event.message.chat if event.message else None
        chat_id = chat.id if chat else None
        subscription = self._book.for_chat(chat_id) if chat_id is not None else None
        data["subscription"] = subscription

        cmd = commands.command_for_callback(event.data)
        # Не наша кнопка — пропускаем (обработается по месту или проигнорируется).
        if cmd is None:
            return await handler(event, data)

        if (
            subscription is None
            or subscription.broken
            or not subscription.allows_command(cmd.name)
        ):
            log.info("Отказ в кнопке %s для chat_id=%s", event.data, chat_id)
            await event.answer("⛔️ Недоступно", show_alert=True)
            return None

        return await handler(event, data)
