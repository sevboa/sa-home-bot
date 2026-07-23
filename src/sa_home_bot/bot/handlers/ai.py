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
# catchall — неявные триггеры (без /alfred, без reply): любое сообщение в
# личке, упоминание бота через @ в группе. Заведён отдельным роутером —
# регистрируется в setup.py ПОСЛЕДНИМ (после apps), чтобы не перехватывать
# то, что предназначено другим роутерам (magnet-ссылки в torrents и т.п.);
# router (выше) с точными фильтрами (команда, реплай на свой тред)
# остаётся зарегистрирован рано, как раньше.
catchall_router = Router(name="ai_catchall")
log = logging.getLogger(__name__)

ALFRED_PREFIX = "<b>Альфред:</b> "
# Без текста после /alfred — не системная заглушка "диалог начат", а сама
# модель здоровается (решение пользователя 2026-07-23: локальная модель,
# незачем экономить обращения к ней ради заготовленной строки — иначе одна
# и та же неизменная фраза на каждый /alfred быстро выдаёт, что это бот).
# Сама директива в историю диалога НЕ попадает — только то, что модель
# ответит на неё, как на первый (и единственный пока) ход ассистента.
OPENING_PROMPT = (
    "Тебя только что позвали, без конкретного вопроса. Поприветствуй "
    "коротко, в характере — дай понять, что ты здесь и готов слушать."
)
# Реплай в тред без текста (стикер/фото/голосовое без подписи и т.п.) — тоже
# не молчим (раньше просто игнорировали, диалог как будто не реагировал),
# а сообщаем модели, что ход был пустым — та же логика, что и OPENING_PROMPT:
# директива не сохраняется как ход диалога, только ответ на неё.
EMPTY_REPLY_PROMPT = (
    "Пользователь ответил в этом треде, не написав никакого текста "
    "(например, стикером или фото без подписи). Отреагируй коротко, в "
    "характере — переспроси или отметь, что не расслышал."
)


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


class PrivateChatText(Filter):
    """Любое обычное текстовое сообщение в личке — там нет других
    собеседников и других команд, которым текст мог бы предназначаться, так
    что молчаливое "разговариваем с Альфредом по умолчанию" уместно.
    Команды (текст с "/") сюда не попадают — их разбирают другие роутеры."""

    async def __call__(self, message: Message) -> bool:
        return (
            message.chat is not None
            and message.chat.type == "private"
            and bool(message.text)
            and not message.text.startswith("/")
        )


class GroupMention(Filter):
    """Упоминание бота через @username в группе — единственный неявный
    триггер там (в отличие от личек группа шумная, отвечать на каждое
    сообщение нельзя)."""

    async def __call__(self, message: Message, bot_username: str) -> bool | dict:
        if message.chat is None or message.chat.type not in ("group", "supergroup"):
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

    if prompt:
        now = datetime.now(tz=UTC)
        sender = message.from_user
        await store.record_ai_turn(
            message.chat.id,
            dialogue_id,
            dialogue_id,
            "user",
            prompt,
            now,
            user_id=sender.id if sender else None,
            user_name=ai_flow.display_name(sender),
        )
        history = [{"role": "user", "content": prompt}]
    else:
        # Директива-приветствие не сохраняется как ход диалога — только то,
        # что модель на неё ответит (см. OPENING_PROMPT выше).
        history = [{"role": "user", "content": OPENING_PROMPT}]

    await _ask_and_reply(
        message, node_link, store, config, book, notifier, dialogue_id, history
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

    if text:
        now = datetime.now(tz=UTC)
        sender = message.from_user
        await store.record_ai_turn(
            message.chat.id,
            message.message_id,
            ai_dialogue_id,
            "user",
            text,
            now,
            user_id=sender.id if sender else None,
            user_name=ai_flow.display_name(sender),
        )
        history_rows = await store.ai_turns_for_dialogue(message.chat.id, ai_dialogue_id)
        history = [
            {"role": r["role"], "content": r["content"]} for r in history_rows if r["content"]
        ]
    else:
        # Пустой ход не пишем в ai_turns (как OPENING_PROMPT) — модель видит
        # директиву только в этом запросе, история треда её не запоминает.
        history_rows = await store.ai_turns_for_dialogue(message.chat.id, ai_dialogue_id)
        history = [
            {"role": r["role"], "content": r["content"]} for r in history_rows if r["content"]
        ]
        history.append({"role": "user", "content": EMPTY_REPLY_PROMPT})

    await _ask_and_reply(
        message, node_link, store, config, book, notifier, ai_dialogue_id, history
    )


@catchall_router.message(PrivateChatText())
async def on_private_message(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    subscription: Subscription | None = None,
) -> None:
    # Не команда — AuthorizationMiddleware её не проверяла, права смотрим сами
    # (как в on_ai_reply); в неавторизованной личке просто молчим.
    right = commands.required_right(commands.ALFRED.name)
    if subscription is None or not subscription.allows_command(right):
        return

    text = message.text.strip()
    dialogue_id = await store.latest_ai_dialogue(message.chat.id)
    if dialogue_id is None:
        # Первое сообщение в этой личке — новый тред, как /alfred.
        dialogue_id = message.message_id

    now = datetime.now(tz=UTC)
    sender = message.from_user
    await store.record_ai_turn(
        message.chat.id,
        message.message_id,
        dialogue_id,
        "user",
        text,
        now,
        user_id=sender.id if sender else None,
        user_name=ai_flow.display_name(sender),
    )
    history_rows = await store.ai_turns_for_dialogue(message.chat.id, dialogue_id)
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows if r["content"]]

    await _ask_and_reply(message, node_link, store, config, book, notifier, dialogue_id, history)


@catchall_router.message(GroupMention())
async def on_group_mention(
    message: Message,
    mention_prompt: str,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    subscription: Subscription | None = None,
) -> None:
    right = commands.required_right(commands.ALFRED.name)
    if subscription is None or not subscription.allows_command(right):
        return

    dialogue_id = message.message_id
    if mention_prompt:
        now = datetime.now(tz=UTC)
        sender = message.from_user
        await store.record_ai_turn(
            message.chat.id,
            dialogue_id,
            dialogue_id,
            "user",
            mention_prompt,
            now,
            user_id=sender.id if sender else None,
            user_name=ai_flow.display_name(sender),
        )
        history = [{"role": "user", "content": mention_prompt}]
    else:
        # Позвали без текста — та же заглушка-директива, что и голый /alfred.
        history = [{"role": "user", "content": OPENING_PROMPT}]

    await _ask_and_reply(message, node_link, store, config, book, notifier, dialogue_id, history)


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
            message, node_link, store, config, history, dialogue_id, book, notifier
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
