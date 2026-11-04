from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import (
    DeleteMessage,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    TelegramMethod,
)
from aiogram.types import Message

from sa_home_bot.bot.ai_context import display_name
from sa_home_bot.bot.ai_turns import PHOTO_MARKER, STICKER_MARKER
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_PLAIN
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)

GROUP_LOG_KEEP = 1000
GROUP_TYPES = ("group", "supergroup")
MEDIA_MARKERS = {
    "photo": PHOTO_MARKER,
    "voice": "[голосовое]",
    "video_note": "[кружочек]",
    "audio": "[аудио]",
    "video": "[видео]",
    "animation": "[гифка]",
    "document": "[документ]",
    "location": "[геолокация]",
    "contact": "[контакт]",
}

EDIT_METHODS = (
    EditMessageText,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageReplyMarkup,
)


def message_content(message: Message, *, strip_prefix: bool = False) -> str | None:
    if message.text:
        text = message.text
        if strip_prefix:
            text = text.removeprefix(ALFRED_PREFIX_PLAIN)
        return text
    content_type = message.content_type
    if content_type == "sticker":
        emoji = message.sticker.emoji if message.sticker else ""
        return f"{STICKER_MARKER} {emoji}" if emoji else STICKER_MARKER
    if content_type == "poll":
        question = message.poll.question if message.poll else ""
        return f"[опрос: {question}]" if question else "[опрос]"
    marker = MEDIA_MARKERS.get(content_type)
    if marker is None:
        return None
    caption = (message.caption or "").strip()
    if strip_prefix:
        caption = caption.removeprefix(ALFRED_PREFIX_PLAIN)
    return f"{marker} {caption}" if caption else marker


def reply_target(message: Message) -> int | None:
    reply = message.reply_to_message
    if reply is None:
        return None
    if message.is_topic_message and reply.message_id == message.message_thread_id:
        return None
    return reply.message_id


def author_of(message: Message) -> tuple[int | None, str | None]:
    if message.from_user is not None:
        return message.from_user.id, display_name(message.from_user)
    if message.sender_chat is not None:
        return message.sender_chat.id, message.sender_chat.title
    return None, None


class GroupLogMiddleware(BaseMiddleware):
    def __init__(self, book: SubscriptionBook, store: Any) -> None:
        self._book = book
        self._store = store

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            chat = event.chat
            if chat is not None and chat.type in GROUP_TYPES:
                subscription = self._book.for_chat(chat.id)
                if subscription is not None and subscription.group_log:
                    if event.migrate_to_chat_id is not None:
                        log.warning(
                            "Миграция чата: from=%s to=%s", chat.id, event.migrate_to_chat_id
                        )
                    else:
                        content = message_content(event)
                        if content is not None:
                            user_id, user_name = author_of(event)
                            reply_to = reply_target(event)
                            try:
                                await self._store.record_group_message(
                                    chat.id,
                                    event.message_id,
                                    created_at=event.date,
                                    user_id=user_id,
                                    user_name=user_name,
                                    content=content,
                                    reply_to=reply_to,
                                    from_alfred=False,
                                    keep=GROUP_LOG_KEEP,
                                )
                            except Exception:
                                log.warning(
                                    "Не удалось записать сообщение группы chat_id=%s message_id=%s",
                                    chat.id,
                                    event.message_id,
                                    exc_info=True,
                                )
        return await handler(event, data)


def edited_message_handler(
    book: SubscriptionBook, store: Any
) -> Callable[[Message], Awaitable[None]]:
    async def on_edited_message(message: Message) -> None:
        chat = message.chat
        if chat is not None and chat.type in GROUP_TYPES:
            subscription = book.for_chat(chat.id)
            if subscription is not None and subscription.group_log:
                content = message_content(message)
                if content is not None:
                    try:
                        await store.edit_group_message(chat.id, message.message_id, content)
                    except Exception:
                        log.warning(
                            "Не удалось обновить сообщение группы chat_id=%s message_id=%s",
                            chat.id,
                            message.message_id,
                            exc_info=True,
                        )

    return on_edited_message


class GroupLogOutgoing(BaseRequestMiddleware):
    def __init__(self, book: SubscriptionBook, store: Any) -> None:
        self._book = book
        self._store = store

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[Any],
        bot: Any,
        method: TelegramMethod[Any],
    ) -> Any:
        result = await make_request(bot, method)
        try:
            await self._remember(method, result)
        except Exception:
            log.warning("Не удалось записать реплику бота в журнал группы", exc_info=True)
        return result

    async def _remember(self, method: TelegramMethod[Any], result: Any) -> None:
        if isinstance(method, DeleteMessage) and result:
            chat_id = int(method.chat_id)
            sub = self._book.for_chat(chat_id)
            if sub is not None and sub.group_log:
                await self._store.delete_group_message(chat_id, method.message_id)
            return

        if isinstance(method, EDIT_METHODS) and isinstance(result, Message):
            content = message_content(result, strip_prefix=True)
            sub = self._book.for_chat(result.chat.id)
            if content and sub is not None and sub.group_log:
                await self._store.edit_group_message(result.chat.id, result.message_id, content)
            return

        messages: list[Message] = []
        if isinstance(result, Message):
            messages = [result]
        elif isinstance(result, list):
            messages = [m for m in result if isinstance(m, Message)]

        for message in messages:
            content = message_content(message, strip_prefix=True)
            if content is None:
                continue
            chat_id = message.chat.id
            sub = self._book.for_chat(chat_id)
            if sub is None or not sub.group_log:
                continue
            user_id = message.from_user.id if message.from_user else None
            reply_to = reply_target(message)
            await self._store.record_group_message(
                chat_id,
                message.message_id,
                created_at=message.date,
                user_id=user_id,
                user_name="Альфред",
                content=content,
                reply_to=reply_to,
                from_alfred=True,
                keep=GROUP_LOG_KEEP,
            )
