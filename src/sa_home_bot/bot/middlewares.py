"""Middleware: гейт молчания, контекст чата и авторизация команд (chat-level).

Два разных барьера, не путать:

- `SilenceGate` — на входе, для **чужих**: чат без подписки не получает
  вообще ничего (AUTHORIZATION.md §10). Он не отвечает «недоступно», не
  печатает «набирает сообщение» — просто ничего не происходит.
- `AuthorizationMiddleware` — для **своих**: универсальные команды
  (ping) проходят без проверок, управляющие требуют права в
  allowed_commands не-broken подписки (§5). Здесь отказ виден и объясняется:
  человек уже впущен, скрывать от него нечего.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, Update

from sa_home_bot.bot import commands
from sa_home_bot.bot.invites import Gatekeeper
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)

DENIED_TEXT = "⛔️ Команда недоступна в этом чате."


def _update_chat_id(update: Update) -> int | None:
    """chat_id апдейта любого типа. None — апдейт вне чата (inline-запрос и
    прочее, чего бот не поддерживает): для гейта это тем более чужой."""
    for event in (
        update.message,
        update.edited_message,
        update.channel_post,
        update.edited_channel_post,
        update.my_chat_member,
        update.chat_member,
    ):
        if event is not None and event.chat is not None:
            return event.chat.id
    callback = update.callback_query
    if callback is not None and callback.message is not None and callback.message.chat:
        return callback.message.chat.id
    return None


def invite_candidate(message: Message | None) -> str | None:
    """Текст, который стоит проверить как инвайт-код.

    Два пути: deep-link `t.me/<bot>?start=КОД` (Telegram присылает его как
    `/start КОД`) — основной, и код, присланный текстом, — запасной, для
    групп, куда ссылка не годится.
    """
    if message is None or not message.text:
        return None
    text = message.text.strip()
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else None
    return text


class SilenceGate(BaseMiddleware):
    """Чужой чат не получает ничего, кроме возможности предъявить инвайт.

    Стоит на `dp.update` (outer), а не на `message`: путей к боту сегодня
    минимум шесть — команда, реплай в треде, любое сообщение в личке,
    упоминание в группе, .torrent-файл, callback-кнопка. Проверять каждый
    отдельно значит однажды забыть один; здесь же вход ровно один.

    Неверный код молчит так же, как всё остальное: ответ «код неверный» был
    бы подтверждением, что бот здесь есть и ждёт правильный.
    """

    def __init__(self, book: SubscriptionBook, gate: Gatekeeper) -> None:
        self._book = book
        self._gate = gate

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        chat_id = _update_chat_id(event)
        if chat_id is None:
            return None
        if self._book.for_chat(chat_id) is not None:
            return await handler(event, data)

        message = event.message
        admission = await self._gate.try_admit(
            chat_id,
            invite_candidate(message),
            user_id=message.from_user.id if message and message.from_user else None,
            user_name=_user_name(message),
            chat_title=message.chat.title if message and message.chat else None,
        )
        if admission is None:
            log.debug("Гейт: апдейт из неподписного chat_id=%s проигнорирован", chat_id)
            return None
        # Впустили — этот же апдейт идёт дальше: приветствие пишет хендлер
        # (bot/handlers/invites.py), у которого есть все зависимости.
        data["admission"] = admission
        return await handler(event, data)


def _user_name(message: Message | None) -> str | None:
    """Как подписать гостя в /guests: имя профиля плюс @username, если есть."""
    user = message.from_user if message else None
    if user is None:
        return None
    return f"{user.full_name} (@{user.username})" if user.username else user.full_name


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

        # Управляющая команда — проверяем права (алиасы делят одно право).
        if commands.is_control(command):
            right = commands.required_right(command)
            if subscription is None or subscription.broken or not subscription.allows_command(
                right
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

        # Динамические действия служб («act:<служба>:<действие>[:<значение>]»):
        # право — `действие@служба` из describe, не имя команды бота.
        action = commands.parse_action_callback(event.data)
        if action is not None:
            service, action_id, _, _ = action
            if subscription is None or not subscription.allows_action(action_id, service):
                log.info("Отказ в действии %s для chat_id=%s", event.data, chat_id)
                await event.answer("⛔️ Недоступно", show_alert=True)
                return None
            return await handler(event, data)

        cmd = commands.command_for_callback(event.data)
        # Не наша кнопка — пропускаем (обработается по месту или проигнорируется).
        if cmd is None:
            return await handler(event, data)

        if (
            subscription is None
            or subscription.broken
            or not subscription.allows_command(cmd.right or cmd.name)
        ):
            log.info("Отказ в кнопке %s для chat_id=%s", event.data, chat_id)
            await event.answer("⛔️ Недоступно", show_alert=True)
            return None

        return await handler(event, data)
