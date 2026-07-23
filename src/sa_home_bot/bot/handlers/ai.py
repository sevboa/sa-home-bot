"""/alfred (видимый в меню) и /ai (скрытый алиас, как /swarm↔/nodes) —
диалог с Альфредом (LLM-служба на winpc), продолжение через reply.

Дизайн диалога — LLM_INTEGRATION_PLAN.md §6 (dialogue_id = message_id
команды, которой начат тред; реплай-цепочка резолвится через ai_turns, не
через дерево Telegram-реплаев). Presence/wake-сценарий и форматирование
ответа — bot/ai_flow.py.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message

from sa_home_bot.bot import ai_flow, commands
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="ai")
log = logging.getLogger(__name__)

ALFRED_PREFIX = "<b>Альфред:</b> "
# Открывающая реплика без текста после /alfred — сам Альфред, не системное
# "диалог начат" (не должно читаться как интерфейс бота, см. обсуждение с
# пользователем 2026-07-23). Искажение «р» — как в Агнольд/Альбегт: это
# фиксированная строка, не вывод модели, поэтому пишем сами.
OPENING_TEXT = ALFRED_PREFIX + "Да, сэг? Слушаю вас."


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


@router.message(Command(commands.ALFRED.name, commands.AI.name))
async def cmd_ai(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
) -> None:
    dialogue_id = message.message_id
    parts = (message.text or "").split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""
    now = datetime.now(tz=UTC)

    if not prompt:
        sent = await message.answer(OPENING_TEXT)
        # Пустой content — при первом reply история для LLM не тянет заглушку
        # (ai_turns_for_dialogue отфильтровывает пустые content на выборке).
        await store.record_ai_turn(
            message.chat.id, sent.message_id, dialogue_id, "assistant", "", now
        )
        return

    await store.record_ai_turn(message.chat.id, dialogue_id, dialogue_id, "user", prompt, now)
    await _ask_and_reply(
        message,
        node_link,
        store,
        config,
        book,
        notifier,
        dialogue_id,
        [{"role": "user", "content": prompt}],
    )


@router.message(AiReplyContinuation())
async def on_ai_reply(
    message: Message,
    ai_dialogue_id: int,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    subscription: Subscription | None = None,
) -> None:
    # AuthorizationMiddleware не проверяет права на не-командные сообщения —
    # проверяем право сами (защита от продолжения треда в чате, у которого
    # право chat@llm с тех пор отозвали).
    right = commands.required_right(commands.ALFRED.name)
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
    await _ask_and_reply(
        message, node_link, store, config, book, notifier, ai_dialogue_id, history
    )


async def _ask_and_reply(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    dialogue_id: int,
    history: list[dict[str, str]],
) -> None:
    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        raw = await ai_flow.request_alfred(
            message, node_link, store, config, history, book, notifier
        )
    except Exception as exc:  # noqa: BLE001 — страховка: баг тут не должен быть молчаливым
        log.exception("ai: необработанная ошибка в диалоге chat=%s", message.chat.id)
        await message.answer("<b>Альфред:</b> Прошу прощения, что-то пошло не так, сэр.")
        await ai_flow.notify_admins(
            book,
            notifier,
            f"🔥 /ai (chat={message.chat.id}): необработанное исключение {exc!r}",
        )
        return
    if raw is None:
        return  # недоступность/ошибка уже сообщена пользователю (ai_flow)
    sent = await message.reply(_format_answer(raw))
    await store.record_ai_turn(
        message.chat.id, sent.message_id, dialogue_id, "assistant", raw, datetime.now(tz=UTC)
    )
