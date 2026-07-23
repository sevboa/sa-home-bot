"""/ai — диалог с Альфредом (LLM-служба на winpc), продолжение через reply.

Дизайн диалога — LLM_INTEGRATION_PLAN.md §6 (dialogue_id = message_id
команды /ai, reply-цепочка резолвится через ai_turns, не через дерево
Telegram-реплаев). Presence/wake-сценарий и форматирование ответа —
bot/ai_flow.py.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message

from sa_home_bot.bot import ai_flow, commands
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="ai")
log = logging.getLogger(__name__)

STUB_TEXT = "Диалог начат. Ответьте на это сообщение, чтобы продолжить."
ALFRED_PREFIX = "<b>Альфред:</b> "


def _format_answer(raw: str) -> str:
    return ALFRED_PREFIX + html.escape(raw.strip())


class AiReplyContinuation(Filter):
    """Реплай на сообщение бота из уже начатого треда /ai — резолвится по
    ai_turns (не по дереву Telegram-реплаев). Реплай на что-то другое эту
    фичу не трогает — обычный пропуск дальше по цепочке роутеров."""

    async def __call__(self, message: Message, store: Store) -> bool | dict:
        if message.reply_to_message is None or message.chat is None:
            return False
        row = await store.ai_turn(message.chat.id, message.reply_to_message.message_id)
        if row is None:
            return False
        return {"ai_dialogue_id": row["dialogue_id"]}


@router.message(Command(commands.AI.name))
async def cmd_ai(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
) -> None:
    dialogue_id = message.message_id
    parts = (message.text or "").split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""
    now = datetime.now(tz=UTC)

    if not prompt:
        sent = await message.answer(STUB_TEXT)
        # Пустой content — при первом reply история для LLM не тянет заглушку
        # (ai_turns_for_dialogue отфильтровывает пустые content на выборке).
        await store.record_ai_turn(
            message.chat.id, sent.message_id, dialogue_id, "assistant", "", now
        )
        return

    await store.record_ai_turn(message.chat.id, dialogue_id, dialogue_id, "user", prompt, now)
    await _ask_and_reply(
        message, node_link, store, config, dialogue_id, [{"role": "user", "content": prompt}]
    )


@router.message(AiReplyContinuation())
async def on_ai_reply(
    message: Message,
    ai_dialogue_id: int,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    # AuthorizationMiddleware не проверяет права на не-командные сообщения —
    # проверяем право сами (защита от продолжения треда в чате, у которого
    # право chat@llm с тех пор отозвали).
    right = commands.required_right(commands.AI.name)
    if subscription is None or not subscription.allows_command(right):
        return
    text = (message.text or "").strip()
    if not text:
        return

    now = datetime.now(tz=UTC)
    await store.record_ai_turn(
        message.chat.id, message.message_id, ai_dialogue_id, "user", text, now
    )
    history_rows = await store.ai_turns_for_dialogue(message.chat.id, ai_dialogue_id)
    history = [
        {"role": r["role"], "content": r["content"]} for r in history_rows if r["content"]
    ]
    await _ask_and_reply(message, node_link, store, config, ai_dialogue_id, history)


async def _ask_and_reply(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    dialogue_id: int,
    history: list[dict[str, str]],
) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")
    raw = await ai_flow.request_alfred(message, node_link, store, config, history)
    if raw is None:
        return  # недоступность уже сообщена пользователю (ai_flow)
    sent = await message.reply(_format_answer(raw))
    await store.record_ai_turn(
        message.chat.id, sent.message_id, dialogue_id, "assistant", raw, datetime.now(tz=UTC)
    )
