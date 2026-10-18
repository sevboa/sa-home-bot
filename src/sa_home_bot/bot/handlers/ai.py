from __future__ import annotations

import logging
from typing import Any

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message

from sa_home_bot.bot import ai_flow, ai_turns, commands
from sa_home_bot.bot.ai_reply import (
    TurnServices,
    ask_and_reply,
    dialogue_id_of,
    rich_session_for,
)
from sa_home_bot.bot.away import KIND_SUMMON, KIND_TEXT, AwayService
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.pending_actions import PendingActions
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="ai")
catchall_router = Router(name="ai_catchall")
log = logging.getLogger(__name__)

PRIVATE = "private"
GROUP_TYPES = ("group", "supergroup")


def _may_talk_to_alfred(subscription: Subscription | None) -> bool:
    right = commands.required_right(commands.ALFRED.name)
    return subscription is not None and subscription.allows_command(right)


def _greeting_portrait(message: Message, interactives: Interactives | None) -> None:
    if interactives is None or message.chat is None or message.from_user is None:
        return
    interactives.start_greeting_portrait(
        message.chat.id,
        message.from_user.id,
        message_thread_id=message.message_thread_id,
        trigger_message_id=message.message_id,
    )


class AiReplyContinuation(Filter):
    async def __call__(self, message: Message, store: Store) -> bool | dict:
        if message.reply_to_message is None or message.chat is None:
            return False
        row = await store.ai_turn(message.chat.id, message.reply_to_message.message_id)
        if row is None:
            return False
        return {"ai_dialogue_id": row["dialogue_id"]}


class PrivateChatContent(Filter):
    def __init__(self, *attributes: str) -> None:
        self._attributes = attributes

    async def __call__(self, message: Message) -> bool:
        return (
            message.chat is not None
            and message.chat.type == PRIVATE
            and any(bool(getattr(message, name)) for name in self._attributes)
        )


class PrivateChatText(Filter):
    async def __call__(self, message: Message) -> bool:
        return (
            message.chat is not None
            and message.chat.type == PRIVATE
            and bool(message.text)
            and not message.text.startswith("/")
        )


class PrivateChatPhoto(PrivateChatContent):
    def __init__(self) -> None:
        super().__init__("photo")


class PrivateChatVoice(PrivateChatContent):
    def __init__(self) -> None:
        super().__init__("voice")


class PrivateChatSticker(PrivateChatContent):
    def __init__(self) -> None:
        super().__init__("sticker")


class PrivateChatOtherMedia(PrivateChatContent):
    def __init__(self) -> None:
        super().__init__("video_note", "audio")


class GroupMention(Filter):
    async def __call__(self, message: Message, bot_username: str) -> bool | dict:
        if message.chat is None or message.chat.type not in GROUP_TYPES:
            return False
        if not message.text or not message.entities:
            return False
        mention = f"@{bot_username}".lower()
        for entity in message.entities:
            if entity.type != "mention":
                continue
            piece = message.text[entity.offset : entity.offset + entity.length]
            if piece.lower() == mention:
                rest = message.text[: entity.offset] + message.text[entity.offset + entity.length :]
                return {"mention_prompt": rest.strip()}
        return False


def _services(
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    pending_actions: PendingActions | None,
    interactives: Interactives | None,
) -> TurnServices:
    return TurnServices(
        node_link=node_link,
        store=store,
        config=config,
        book=book,
        notifier=notifier,
        active_ai_chats=active_ai_chats,
        tool_calls=tool_calls,
        pending_actions=pending_actions,
        interactives=interactives,
    )


@router.message(Command(commands.ALFRED.name, commands.AI.name))
async def cmd_ai(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    dialogue_id = dialogue_id_of(message)
    in_topic = message.message_thread_id is not None
    parts = (message.text or "").split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""

    if away is not None and await away.intercept(
        message, kind=KIND_TEXT if prompt else KIND_SUMMON, text=prompt, dialogue_id=dialogue_id
    ):
        return

    if prompt:
        await ai_turns.record_user_turn(store, message, dialogue_id, prompt)

    if in_topic:
        history = await ai_turns.load_thread_history(store, config, message, dialogue_id)
        if not prompt:
            history.append({"role": "user", "content": ai_turns.OPENING_PROMPT})
    elif prompt:
        history = [{"role": "user", "content": prompt}]
    else:
        history = [{"role": "user", "content": ai_turns.OPENING_PROMPT}]
    if not prompt:
        _greeting_portrait(message, interactives)

    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, dialogue_id, history, rich_session_for(message, config))


@router.message(AiReplyContinuation())
async def on_ai_reply(
    message: Message,
    ai_dialogue_id: int,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    subscription: Subscription | None = None,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    if away is not None and await away.intercept(
        message, dialogue_id=ai_dialogue_id, continuing=True
    ):
        return
    text = (message.text or "").strip()
    rich_session = rich_session_for(message, config)

    if message.photo:
        history = await ai_turns.photo_history(message, store, config, ai_dialogue_id, rich_session)
        if history is None:
            await message.answer(ai_turns.PHOTO_TOO_LARGE_TEXT)
            return
    elif message.voice:
        history = await ai_turns.voice_history(
            message, store, config, node_link, ai_dialogue_id, rich_session
        )
        if history is None:
            return
    elif message.sticker:
        history = await ai_turns.sticker_history(
            message, store, config, ai_dialogue_id, rich_session
        )
    elif text:
        history = await ai_turns.record_text_turn(store, config, message, ai_dialogue_id, text)
    else:
        history = await ai_turns.history_with_directive(
            store, config, message, ai_dialogue_id, ai_turns.EMPTY_REPLY_PROMPT
        )

    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, ai_dialogue_id, history, rich_session)


@catchall_router.message(PrivateChatText())
async def on_private_message(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    subscription: Subscription | None = None,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    text = message.text.strip()
    dialogue_id = dialogue_id_of(message)
    if away is not None and await away.intercept(
        message, kind=KIND_TEXT, text=text, dialogue_id=dialogue_id
    ):
        return
    history = await ai_turns.record_text_turn(store, config, message, dialogue_id, text)
    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, dialogue_id, history, rich_session_for(message, config))


@catchall_router.message(PrivateChatPhoto())
async def on_private_photo(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    subscription: Subscription | None = None,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    dialogue_id = dialogue_id_of(message)
    if away is not None and await away.intercept(message, dialogue_id=dialogue_id):
        return
    rich_session = rich_session_for(message, config)
    history = await ai_turns.photo_history(message, store, config, dialogue_id, rich_session)
    if history is None:
        await message.answer(ai_turns.PHOTO_TOO_LARGE_TEXT)
        return
    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, dialogue_id, history, rich_session)


@catchall_router.message(PrivateChatVoice())
async def on_private_voice(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    subscription: Subscription | None = None,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    dialogue_id = dialogue_id_of(message)
    if away is not None and await away.intercept(message, dialogue_id=dialogue_id):
        return
    rich_session = rich_session_for(message, config)
    history = await ai_turns.voice_history(
        message, store, config, node_link, dialogue_id, rich_session
    )
    if history is None:
        return
    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, dialogue_id, history, rich_session)


@catchall_router.message(PrivateChatSticker())
async def on_private_sticker(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    subscription: Subscription | None = None,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    dialogue_id = dialogue_id_of(message)
    if away is not None and await away.intercept(message, dialogue_id=dialogue_id):
        return
    rich_session = rich_session_for(message, config)
    history = await ai_turns.sticker_history(message, store, config, dialogue_id, rich_session)
    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, dialogue_id, history, rich_session)


@catchall_router.message(PrivateChatOtherMedia())
async def on_private_other_media(
    message: Message,
    away: AwayService | None = None,
    subscription: Subscription | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    if away is not None:
        await away.intercept(message, dialogue_id=dialogue_id_of(message))


@catchall_router.message(GroupMention())
async def on_group_mention(
    message: Message,
    mention_prompt: str,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    subscription: Subscription | None = None,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> None:
    if not _may_talk_to_alfred(subscription):
        return
    dialogue_id = dialogue_id_of(message)
    if away is not None and await away.intercept(
        message,
        kind=KIND_TEXT if mention_prompt else KIND_SUMMON,
        text=mention_prompt,
        dialogue_id=dialogue_id,
    ):
        return
    if mention_prompt:
        await ai_turns.record_user_turn(store, message, dialogue_id, mention_prompt)
        history: list[dict[str, Any]] = [{"role": "user", "content": mention_prompt}]
    else:
        history = [{"role": "user", "content": ai_turns.OPENING_PROMPT}]
        _greeting_portrait(message, interactives)

    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    await ask_and_reply(message, services, dialogue_id, history, rich_session_for(message, config))


async def start_dialogue(
    message: Message,
    prompt: str,
    *,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    pending_actions: PendingActions | None = None,
    interactives: Interactives | None = None,
    away: AwayService | None = None,
) -> str | None:
    if away is not None and await away.intercept(message, kind=KIND_SUMMON):
        return None
    _greeting_portrait(message, interactives)
    services = _services(
        node_link,
        store,
        config,
        book,
        notifier,
        active_ai_chats,
        tool_calls,
        pending_actions,
        interactives,
    )
    return await ask_and_reply(
        message,
        services,
        dialogue_id_of(message),
        [{"role": "user", "content": prompt}],
        rich_session_for(message, config),
    )
