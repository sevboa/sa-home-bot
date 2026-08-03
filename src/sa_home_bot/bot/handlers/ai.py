"""/alfred (видимый в меню) и /ai (скрытый алиас, как /swarm↔/nodes) —
диалог с Альфредом (LLM-служба на winpc), продолжение через reply.

Дизайн диалога — LLM_INTEGRATION_PLAN.md §6 (dialogue_id = message_id
команды, которой начат тред; реплай-цепочка резолвится через ai_turns, не
через дерево Telegram-реплаев). Presence/wake-сценарий и форматирование
ответа — bot/ai_flow.py.

**Telegram Private Chat Topics (этап 32, IMPLEMENTATION_PLAN.md, добавлено
2026-08-01):** если сообщение отправлено внутри топика личного чата
(`message.message_thread_id` не пуст), `dialogue_id` — это id топика, а не
message_id сообщения. Дальше топик и есть тред: любое сообщение внутри
него — продолжение, без обязательного reply на сообщение бота (в отличие
от треда без топика, где reply по-прежнему единственный способ
продолжить). `aiogram` сам пробрасывает `message_thread_id` в
`message.answer()`/`.reply()` — ответ уезжает в тот же топик без
дополнительных параметров. Живой спайк на боевом боте (2026-08-01):
`createForumTopic`/`sendMessage(message_thread_id=…)`/`editMessageText`
внутри топика личного чата отработали с первой попытки — регрессия Bot
API 10.0, которую ловили другие проекты (`sendMessage` с
`message_thread_id` → `400: message thread not found`), на этом боте не
воспроизвелась.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message

from sa_home_bot.bot import ai_flow, commands
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.notifier import Notifier, chunk_text
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
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


def _dialogue_id_for(message: Message) -> int:
    """id диалога: id топика (Private Chat Topics), если сообщение отправлено
    внутри него — весь топик тогда один тред. Иначе — message_id этого
    сообщения, как раньше (новый тред на каждое непарное сообщение вне
    топика)."""
    return message.message_thread_id or message.message_id


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
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
) -> None:
    dialogue_id = _dialogue_id_for(message)
    in_topic = message.message_thread_id is not None
    parts = (message.text or "").split(maxsplit=1)
    prompt = parts[1].strip() if len(parts) > 1 else ""

    if prompt:
        now = datetime.now(tz=UTC)
        sender = message.from_user
        await store.record_ai_turn(
            message.chat.id,
            message.message_id,
            dialogue_id,
            "user",
            prompt,
            now,
            user_id=sender.id if sender else None,
            user_name=ai_flow.display_name(sender),
        )

    if in_topic:
        # Топик — уже тред сам по себе: /alfred внутри него не сбрасывает
        # контекст (в отличие от /alfred вне топика, который всегда начинает
        # диалог заново), а продолжает то, что там уже накопилось — как
        # обычное сообщение в этом же топике (см. on_private_message).
        history_rows = await store.ai_turns_for_dialogue(message.chat.id, dialogue_id)
        history = [
            {"role": r["role"], "content": r["content"]} for r in history_rows if r["content"]
        ]
        if not prompt:
            history.append({"role": "user", "content": OPENING_PROMPT})
    elif prompt:
        history = [{"role": "user", "content": prompt}]
    else:
        # Директива-приветствие не сохраняется как ход диалога — только то,
        # что модель на неё ответит (см. OPENING_PROMPT выше).
        history = [{"role": "user", "content": OPENING_PROMPT}]

    await _ask_and_reply(
        message, node_link, store, config, book, notifier, dialogue_id, history,
        active_ai_chats, tool_calls,
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
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
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
        message, node_link, store, config, book, notifier, ai_dialogue_id, history,
        active_ai_chats, tool_calls,
    )


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
) -> None:
    # Не команда — AuthorizationMiddleware её не проверяла, права смотрим сами
    # (как в on_ai_reply); в неавторизованной личке просто молчим.
    right = commands.required_right(commands.ALFRED.name)
    if subscription is None or not subscription.allows_command(right):
        return

    text = message.text.strip()
    # Без топика и без reply — всегда новый тред (как /alfred), а не
    # продолжение самого свежего: иначе история в личке растёт бесконечно,
    # ничем не ограниченная (живой баг 2026-08-01). Продолжить такой тред
    # по-прежнему можно реплаем — см. AiReplyContinuation. Внутри топика
    # (Private Chat Topics) эта проблема решена нормально: dialogue_id — id
    # топика, любое сообщение в нём само по себе продолжение, reply не нужен.
    dialogue_id = _dialogue_id_for(message)

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

    await _ask_and_reply(
        message, node_link, store, config, book, notifier, dialogue_id, history,
        active_ai_chats, tool_calls,
    )


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
) -> None:
    right = commands.required_right(commands.ALFRED.name)
    if subscription is None or not subscription.allows_command(right):
        return

    dialogue_id = _dialogue_id_for(message)
    if mention_prompt:
        now = datetime.now(tz=UTC)
        sender = message.from_user
        await store.record_ai_turn(
            message.chat.id,
            message.message_id,
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

    await _ask_and_reply(
        message, node_link, store, config, book, notifier, dialogue_id, history,
        active_ai_chats, tool_calls,
    )


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
) -> str | None:
    """Начать тред директивой, как голый /alfred, — но не по команде человека.

    Нужна приватному входу (bot/handlers/invites.py): гостя встречает сам
    Альфред, а не заготовленная строка. Директива в историю не пишется —
    только ответ на неё (та же логика, что у OPENING_PROMPT).

    Возвращает текст ответа Альфреда; None — не нашли (сообщение об этом
    пользователю уже ушло, но вызывающий может добавить своё, см. приветствие
    гостя).
    """
    return await _ask_and_reply(
        message,
        node_link,
        store,
        config,
        book,
        notifier,
        _dialogue_id_for(message),
        [{"role": "user", "content": prompt}],
        active_ai_chats,
        tool_calls,
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
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
) -> str | None:
    """Текст ответа Альфреда, если он реально уехал в чат, иначе None.

    Возврат нужен приветствию гостя (bot/handlers/invites.py): не нашли
    Альфреда — впущенного нельзя оставить без единого слова, а нашли —
    надо убедиться, что в приветствии есть обещанная подсказка про /help.
    Обычным путям (/alfred, реплай, личка) результат не нужен, они его
    игнорируют.
    """
    # Регистрация задачи (не просто chat_id — см. докстринг ActiveAiChats,
    # второй заход) на время запроса: bot/app.py::_shutdown() перед разрывом
    # связи со службами уведомляет RESTART_TEXT'ом и ОТМЕНЯЕТ эту задачу —
    # без этого редеплой во время долгого think-ответа либо обрывает запрос
    # голой сетевой ошибкой, либо (после первой попытки чинить) шлёт голую
    # ошибку ВМЕСТЕ с RESTART_TEXT — гонка, не гарантия.
    chat_id = message.chat.id if message.chat else None
    task = asyncio.current_task()
    active_ai_chats.register(chat_id, task)
    try:
        return await _do_ask_and_reply(
            message, node_link, store, config, book, notifier, dialogue_id, history, tool_calls
        )
    finally:
        active_ai_chats.unregister(chat_id, task)


async def _do_ask_and_reply(
    message: Message,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    dialogue_id: int,
    history: list[dict[str, str]],
    tool_calls: ToolCalls,
) -> str | None:
    # typing-индикатор (keep-alive, пока модель реально готовит ответ) —
    # внутри ai_flow.request_alfred, вокруг самого вызова модели (не здесь и
    # не сразу на приём сообщения: во время presence-проверки и прогрева
    # контейнера/машины у пользователя уже есть текстовые «шаги»/«Агнольд»,
    # решение пользователя 2026-08-01, см. bot/notifier.py::typing_action).
    # Ячейка под «Альфреда отпустили» (тул dismiss): сам тул ничего не гасит —
    # ответ в этот момент ещё генерируется той самой моделью, которую предстоит
    # выгрузить (см. bot/tools.py::DismissalBox). Исполняем ниже, после того
    # как прощание реально уехало в чат.
    dismissal = ai_tools.DismissalBox()
    # Ремарка «Логопеда» (llm/speech_therapy.py) заполняется во время
    # запроса, но отправляется отдельным сообщением ПОСЛЕ основного ответа
    # ниже — см. ai_flow.SpeechRemarkBox про то, почему не сразу.
    speech_remark = ai_flow.SpeechRemarkBox()
    try:
        raw = await ai_flow.request_alfred(
            message, node_link, store, config, history, dialogue_id, book, notifier, dismissal,
            tool_calls, speech_remark,
        )
    except Exception as exc:  # noqa: BLE001 — страховка: баг тут не должен быть молчаливым
        log.exception("ai: необработанная ошибка в диалоге chat=%s", message.chat.id)
        await message.answer("<b>Альфред:</b> Прошу прощения, что-то пошло не так, сэр.")
        await ai_flow.notify_admins(
            book,
            notifier,
            f"🔥 /ai (chat={message.chat.id}): необработанное исключение {exc!r}",
        )
        return None
    if raw is None:
        return None  # недоступность/ошибка уже сообщена пользователю (ai_flow)
    if not raw.strip():
        # Живая находка 2026-07-29: модель может вернуть пустой текст (у неё
        # кончилось окно контекста — декларации тулов плюс выдача поиска), и
        # пользователь получал сообщение из одного префикса «Альфред:».
        # Пустая реплика не ход диалога: в ai_turns не пишем, отвечаем тем же
        # персонажем, что и на сбой генерации, а подробности — админу.
        log.warning("ai: пустой ответ модели (chat=%s)", message.chat.id)
        await message.answer(ai_flow.ALBERT_HICCUP)
        await ai_flow.notify_admins(
            book, notifier, f"⚠️ /ai (chat={message.chat.id}): модель вернула пустой ответ"
        )
        return None
    sent = await _send_alfred_reply(message, raw)
    await store.record_ai_turn(
        message.chat.id, sent.message_id, dialogue_id, "assistant", raw, datetime.now(tz=UTC)
    )
    if speech_remark.text is not None:
        # Отдельным сообщением, БЕЗ html.escape (см. _format_answer) —
        # ремарка сама генерирует безопасный HTML (llm/speech_therapy.py:
        # слова-подстановки берутся из regex [А-Яа-яЁё]+, спецсимволов не
        # бывает), экранировать её как обычный текст модели — снова сломать
        # <i>-тег буквально в тег текста.
        await message.answer(speech_remark.text)
    if dismissal.mode is not None:
        # Ход диалога уже записан: если машину выключат, а тред потом
        # продолжат реплаем — история не потеряется, Альфреда просто разбудят.
        await ai_flow.perform_dismissal(message, node_link, dismissal.mode, book, notifier)
    return raw


async def _send_alfred_reply(message: Message, raw: str) -> Message:
    """Отправить ответ Альфреда, при необходимости несколькими сообщениями.

    Промпт (llm/prompt.py) просит модель укладываться в ~3500 знаков и
    самой предлагать продолжение — но это не гарантия (модель может
    промахнуться), а Telegram режёт сообщение на 4096 символах. Без этой
    страховки длинный ответ ронял бы весь хендлер необработанным
    TelegramBadRequest. Возвращает последнее отправленное сообщение —
    именно на него отвечает пользователь, продолжая тред."""
    chunks = chunk_text(_format_answer(raw))
    sent = await message.reply(chunks[0])
    for chunk in chunks[1:]:
        sent = await message.answer(chunk)
    return sent
