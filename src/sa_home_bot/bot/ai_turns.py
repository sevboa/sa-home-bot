from __future__ import annotations

import base64
from typing import Any

from aiogram.types import Message

from sa_home_bot.bot import dialogue_context, voice_stt
from sa_home_bot.bot.ai_context import display_name
from sa_home_bot.bot.ai_flow import photo_key_for
from sa_home_bot.bot.alfred_voice import ALFRED_PREFIX_HTML
from sa_home_bot.bot.rich_stream import RichStreamSession
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.utils.clock import utc_now

OPENING_PROMPT = (
    "Тебя только что позвали, без конкретного вопроса. Поприветствуй "
    "коротко, в характере — дай понять, что ты здесь и готов слушать."
)
EMPTY_REPLY_PROMPT = (
    "Пользователь ответил в этом треде, не написав никакого текста "
    "(например, геолокацией или документом без подписи). Отреагируй коротко, "
    "в характере — переспроси или отметь, что не расслышал."
)
PHOTO_MARKER = "[фото]"
PHOTO_NO_CAPTION_PROMPT = (
    "Пользователь прислал фото без подписи. Посмотри на изображение и "
    "опиши, что видишь, или мягко уточни, что именно интересует."
)
PHOTO_TOO_LARGE_TEXT = (
    ALFRED_PREFIX_HTML + "Простите, это фото слишком тяжёлое — пришлите, "
    "пожалуйста, поменьше или сожмите перед отправкой."
)
MAX_RAW_IMAGE_B64_BYTES = 800_000
STICKER_MARKER = "[стикер]"
STICKER_PROMPT = (
    "Пользователь прислал стикер (эмодзи-тег: {emoji}). Посмотри на "
    "картинку — что на ней и какую эмоцию или жест она передаёт. Это не "
    "фото, а стикер — реплика без слов в переписке, поэтому в ответе не "
    "пересказывай картинку дословно, а отреагируй на неё так, как обычно "
    "реагируют на стикер в чате (эмоция, шутка, реакция). Ответь коротко, "
    "в характере."
)
STICKER_PROMPT_NO_IMAGE = (
    "Пользователь прислал стикер (эмодзи-тег: {emoji}), но превью-картинка "
    "недоступна. Отреагируй на сам факт стикера с этим эмодзи — коротко, "
    "в характере, не выдумывай, что на нём изображено."
)
STICKER_LOOKING_TEXT_PLAIN = "Альфред разглядывает стикер"
PHOTO_LOOKING_TEXT_PLAIN = "Альфред разглядывает фото"
NO_EMOJI_LABEL = "не указан"


async def record_user_turn(
    store: Store,
    message: Message,
    dialogue_id: int,
    content: str,
    *,
    photo_path: str | None = None,
) -> None:
    sender = message.from_user
    await store.record_ai_turn(
        message.chat.id,
        message.message_id,
        dialogue_id,
        "user",
        content,
        utc_now(),
        user_id=sender.id if sender else None,
        user_name=display_name(sender),
        photo_path=photo_path,
    )


async def load_thread_history(
    store: Store, config: Settings, message: Message, dialogue_id: int
) -> dialogue_context.DialogueHistory:
    return await dialogue_context.load_history(store, config, message.chat.id, dialogue_id)


async def record_text_turn(
    store: Store, config: Settings, message: Message, dialogue_id: int, text: str
) -> dialogue_context.DialogueHistory:
    await record_user_turn(store, message, dialogue_id, text)
    return await load_thread_history(store, config, message, dialogue_id)


async def _download_base64(message: Message, file: Any) -> str:
    buf = await message.bot.download(file)
    return base64.b64encode(buf.read()).decode()


async def photo_history(
    message: Message,
    store: Store,
    config: Settings,
    dialogue_id: int,
    rich_session: RichStreamSession | None,
) -> list[dict[str, Any]] | None:
    if rich_session is not None:
        await rich_session.push_status(PHOTO_LOOKING_TEXT_PLAIN)
    photo_b64 = await _download_base64(message, message.photo[-1])
    if len(photo_b64) > MAX_RAW_IMAGE_B64_BYTES:
        return None

    caption = (message.caption or "").strip()
    persisted = f"{PHOTO_MARKER} {caption}".strip() if caption else PHOTO_MARKER
    await record_user_turn(
        store, message, dialogue_id, persisted, photo_path=photo_key_for(message)
    )
    history = await load_thread_history(store, config, message, dialogue_id)
    if history:
        history[-1] = {
            "role": "user",
            "content": caption or PHOTO_NO_CAPTION_PROMPT,
            "raw_image": photo_b64,
        }
    return history


async def sticker_history(
    message: Message,
    store: Store,
    config: Settings,
    dialogue_id: int,
    rich_session: RichStreamSession | None,
) -> list[dict[str, Any]]:
    sticker = message.sticker
    emoji = sticker.emoji
    thumb_b64: str | None = None
    photo_key: str | None = None

    if sticker.thumbnail is not None:
        if rich_session is not None:
            await rich_session.push_status(STICKER_LOOKING_TEXT_PLAIN)
        candidate = await _download_base64(message, sticker.thumbnail)
        if len(candidate) <= MAX_RAW_IMAGE_B64_BYTES:
            thumb_b64 = candidate
            photo_key = photo_key_for(message)

    persisted = f"{STICKER_MARKER} {emoji}".strip() if emoji else STICKER_MARKER
    await record_user_turn(store, message, dialogue_id, persisted, photo_path=photo_key)
    history = await load_thread_history(store, config, message, dialogue_id)
    if history:
        label = emoji or NO_EMOJI_LABEL
        if thumb_b64:
            history[-1] = {
                "role": "user",
                "content": STICKER_PROMPT.format(emoji=label),
                "raw_image": thumb_b64,
            }
        else:
            history[-1] = {
                "role": "user",
                "content": STICKER_PROMPT_NO_IMAGE.format(emoji=label),
            }
    return history


async def voice_history(
    message: Message,
    store: Store,
    config: Settings,
    node_link: ServiceLink,
    dialogue_id: int,
    rich_session: RichStreamSession | None,
) -> list[dict[str, Any]] | None:
    transcript = await voice_stt.transcribe_voice_message(
        message, node_link, store, config, rich_session
    )
    if transcript is None:
        return None
    return await record_text_turn(store, config, message, dialogue_id, transcript)


async def history_with_directive(
    store: Store, config: Settings, message: Message, dialogue_id: int, directive: str
) -> list[dict[str, Any]]:
    history = await load_thread_history(store, config, message, dialogue_id)
    history.append({"role": "user", "content": directive})
    return history
